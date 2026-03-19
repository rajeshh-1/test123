from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bot.core.edge import calculate_edge_from_legs
from bot.core.pretrade import PreTradeRequest, validate_pretrade
from bot.core.reason_codes import (
    ACCEPTED,
    BELOW_MIN_EDGE,
    CIRCUIT_BREAKER_TRIGGERED,
    HEDGE_FAILED,
    INSUFFICIENT_LIQUIDITY,
    KILL_SWITCH_ACTIVE,
    LEG_TIMEOUT,
    PARTIAL_FILL,
)
from bot.core.risk.guards import CircuitBreaker, RiskLimits
from bot.core.storage.jsonl_logger import JsonlLogger
from bot.core.storage.sqlite_store import ArbSQLiteStore
from bot.crypto_updown.runtime import (
    CryptoExecutionRuntime,
    LegOrderRequest,
    compute_robustness_score,
    load_profiles_json,
    normalize_metric,
)


@dataclass
class ProfileMetrics:
    profile_name: str
    latency_ms_kalshi: int
    latency_ms_poly: int
    adverse_drift_bps: float
    book_haircut_pct: float
    partial_fill_prob: float
    timeout_prob: float
    slippage_extra_bps: float
    trades_attempted: int
    trades_accepted: int
    fill_full_rate: float
    partial_fill_rate: float
    timeout_rate: float
    hedge_failed_rate: float
    avg_edge_predicted_pct: float
    avg_edge_captured_pct: float
    edge_capture_ratio: float
    pnl_total: float
    pnl_per_trade: float
    max_drawdown_pct: float
    breaker_trigger_count: int
    skip_rate: float
    robustness_score: float

    def to_dict(self) -> dict:
        return {
            "profile_name": self.profile_name,
            "latency_ms_kalshi": self.latency_ms_kalshi,
            "latency_ms_poly": self.latency_ms_poly,
            "adverse_drift_bps": round(self.adverse_drift_bps, 6),
            "book_haircut_pct": round(self.book_haircut_pct, 6),
            "partial_fill_prob": round(self.partial_fill_prob, 6),
            "timeout_prob": round(self.timeout_prob, 6),
            "slippage_extra_bps": round(self.slippage_extra_bps, 6),
            "trades_attempted": self.trades_attempted,
            "trades_accepted": self.trades_accepted,
            "fill_full_rate": round(self.fill_full_rate, 6),
            "partial_fill_rate": round(self.partial_fill_rate, 6),
            "timeout_rate": round(self.timeout_rate, 6),
            "hedge_failed_rate": round(self.hedge_failed_rate, 6),
            "avg_edge_predicted_pct": round(self.avg_edge_predicted_pct, 6),
            "avg_edge_captured_pct": round(self.avg_edge_captured_pct, 6),
            "edge_capture_ratio": round(self.edge_capture_ratio, 6),
            "pnl_total": round(self.pnl_total, 6),
            "pnl_per_trade": round(self.pnl_per_trade, 6),
            "max_drawdown_pct": round(self.max_drawdown_pct, 6),
            "breaker_trigger_count": self.breaker_trigger_count,
            "skip_rate": round(self.skip_rate, 6),
            "robustness_score": round(self.robustness_score, 6),
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch runner for pessimistic execution profile grid (paper-only).")
    parser.add_argument("--profiles-file", default="configs/execution_profiles_30.json")
    parser.add_argument("--runtime-sec", type=int, default=600)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", default="reports/profile_grid")
    parser.add_argument("--min-edge-pct", type=float, default=5.0)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--trade-notional-usd", type=float, default=10.0)
    parser.add_argument("--initial-equity", type=float, default=100.0)
    return parser.parse_args(argv)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if abs(float(denominator)) < 1e-12:
        return 0.0
    return float(numerator) / float(denominator)


def _sample_legs(rng: random.Random) -> tuple[LegOrderRequest, LegOrderRequest]:
    quantity = round(2.0 + (rng.random() * 8.0), 4)
    kalshi_price = round(0.32 + (rng.random() * 0.23), 6)
    poly_price = round(0.32 + (rng.random() * 0.23), 6)
    leg_a = LegOrderRequest(
        leg_name="leg_a",
        venue="kalshi",
        side="yes",
        price=kalshi_price,
        quantity=quantity,
        timeout_sec=2.0,
    )
    leg_b = LegOrderRequest(
        leg_name="leg_b",
        venue="polymarket",
        side="down",
        price=poly_price,
        quantity=quantity,
        timeout_sec=2.0,
    )
    return leg_a, leg_b


def _captured_edge_pct(*, predicted_edge_pct: float, decision, profile) -> float:
    latency_penalty_pct = ((profile.latency_ms_kalshi + profile.latency_ms_poly) / 1000.0) * 0.03
    execution_penalty_pct = 0.0
    if decision.leg_a and decision.leg_b:
        try:
            effective_total = float(decision.leg_a.metadata.get("effective_price", 0.0) if decision.leg_a.metadata else 0.0)
            effective_total += float(decision.leg_b.metadata.get("effective_price", 0.0) if decision.leg_b.metadata else 0.0)
            requested_total = float(decision.leg_a.metadata.get("requested_price", 0.0) if decision.leg_a.metadata else 0.0)
            requested_total += float(decision.leg_b.metadata.get("requested_price", 0.0) if decision.leg_b.metadata else 0.0)
            if requested_total > 0:
                execution_penalty_pct = ((effective_total - requested_total) / requested_total) * 100.0
        except Exception:
            execution_penalty_pct = 0.0

    if decision.reason_code == ACCEPTED:
        return float(predicted_edge_pct) - latency_penalty_pct - execution_penalty_pct
    if decision.reason_code == PARTIAL_FILL:
        return -0.75 - (float(profile.book_haircut_pct) * 0.02)
    if decision.reason_code == HEDGE_FAILED:
        return -1.5 - (float(profile.book_haircut_pct) * 0.03)
    if decision.reason_code == LEG_TIMEOUT:
        return -0.9 - (float(profile.timeout_prob) * 3.0)
    if decision.reason_code in {CIRCUIT_BREAKER_TRIGGERED, KILL_SWITCH_ACTIVE}:
        return 0.0
    if decision.reason_code in {BELOW_MIN_EDGE, INSUFFICIENT_LIQUIDITY}:
        return 0.0
    return float(predicted_edge_pct) - 1.0


def _simulate_profile(*, profile, args: argparse.Namespace, profile_idx: int, out_dir: Path) -> ProfileMetrics:
    profile_seed = int(args.seed) + (profile_idx * 7919)
    rng = random.Random(profile_seed)

    db_file = out_dir / f"{profile.name}.sqlite"
    jsonl_file = out_dir / f"{profile.name}.jsonl"
    if db_file.exists():
        db_file.unlink()
    if jsonl_file.exists():
        jsonl_file.unlink()

    store = ArbSQLiteStore(str(db_file))
    logger = JsonlLogger(str(jsonl_file))
    guard = CircuitBreaker(
        RiskLimits(
            max_losses_streak=50,
            max_daily_drawdown_pct=80.0,
            max_open_positions=1,
            kill_switch_path="",
        ),
        day_start_equity=float(args.initial_equity),
    )
    runtime = CryptoExecutionRuntime(risk_guard=guard, store=store, event_logger=logger)
    execute_leg = runtime.build_simulated_leg_executor(profile=profile, rng=rng, simulate_sleep=False)

    equity = float(args.initial_equity)
    peak_equity = equity
    max_drawdown_pct = 0.0
    attempted = 0
    accepted = 0
    partial_count = 0
    timeout_count = 0
    hedge_failed_count = 0
    breaker_count = 0
    skipped = 0
    predicted_edges: list[float] = []
    captured_edges: list[float] = []
    pnl_total = 0.0

    for idx in range(max(1, int(args.runtime_sec))):
        attempted += 1
        trade_id = f"{profile.name.upper()}_{idx:05d}"
        market_key = "BTC15M_PROFILE_SIM"
        strategy = "A_KALSHI_YES_PLUS_POLY_DOWN"
        liq_k = round(1.0 + (rng.random() * 12.0), 4)
        liq_p = round(1.0 + (rng.random() * 12.0), 4)
        leg_a, leg_b = _sample_legs(rng)
        edge = calculate_edge_from_legs(
            kalshi_leg_price=leg_a.price,
            poly_leg_price=leg_b.price,
            fee_kalshi_bps=0.0,
            fee_poly_bps=25.0,
            slippage_expected_bps=5.0,
            custo_leg_risk=0.005,
            payout_esperado=1.0,
        )
        predicted_edges.append(float(edge.edge_liquido_pct))
        pretrade = validate_pretrade(
            PreTradeRequest(
                strategy=strategy,
                market_key_k=market_key,
                market_key_p=market_key,
                semantic_equivalent=True,
                resolution_compatible=True,
                edge=edge,
                min_edge_pct=float(args.min_edge_pct),
                liquidity_k=liq_k,
                liquidity_p=liq_p,
                min_liquidity=float(args.min_liquidity),
            )
        )
        decision = runtime.execute(
            trade_id=trade_id,
            market_key=market_key,
            strategy=strategy,
            current_equity=equity,
            open_positions=0,
            edge_liquido_pct=float(edge.edge_liquido_pct),
            liq_k=liq_k,
            liq_p=liq_p,
            pretrade_revalidate=lambda p=pretrade: (p.ok, p.reason_code, p.detail),
            leg_a=leg_a,
            leg_b=leg_b,
            execute_leg=execute_leg,
            hedge_flatten=lambda _a, _b: (rng.random() > max(0.05, float(profile.timeout_prob))),
        )

        if decision.reason_code in {CIRCUIT_BREAKER_TRIGGERED, KILL_SWITCH_ACTIVE}:
            breaker_count += 1
        if decision.reason_code == PARTIAL_FILL:
            partial_count += 1
        if decision.reason_code == LEG_TIMEOUT:
            timeout_count += 1
        if decision.reason_code == HEDGE_FAILED:
            hedge_failed_count += 1
        if not decision.accepted:
            skipped += 1
        else:
            accepted += 1

        captured_edge = _captured_edge_pct(predicted_edge_pct=float(edge.edge_liquido_pct), decision=decision, profile=profile)
        captured_edges.append(float(captured_edge))
        realized_pnl = (captured_edge / 100.0) * float(args.trade_notional_usd)
        expected_pnl = (float(edge.edge_liquido_pct) / 100.0) * float(args.trade_notional_usd)
        pnl_total += realized_pnl
        equity += realized_pnl
        peak_equity = max(peak_equity, equity)
        drawdown = _safe_ratio(max(0.0, peak_equity - equity), max(1e-12, peak_equity)) * 100.0
        max_drawdown_pct = max(max_drawdown_pct, drawdown)
        guard.record_trade_result(realized_pnl=realized_pnl, current_equity=equity)

        store.record_pnl(
            ts_utc="1970-01-01T00:00:00Z",
            trade_id=trade_id,
            market_key=market_key,
            expected_pnl=expected_pnl,
            realized_pnl=realized_pnl,
            status=decision.reason_code,
            metadata={
                "profile_name": profile.name,
                "predicted_edge_pct": float(edge.edge_liquido_pct),
                "captured_edge_pct": float(captured_edge),
                "seed": profile_seed,
            },
        )

    store.close()

    avg_pred = mean(predicted_edges) if predicted_edges else 0.0
    avg_cap = mean(captured_edges) if captured_edges else 0.0
    return ProfileMetrics(
        profile_name=profile.name,
        latency_ms_kalshi=int(profile.latency_ms_kalshi),
        latency_ms_poly=int(profile.latency_ms_poly),
        adverse_drift_bps=float(profile.adverse_drift_bps),
        book_haircut_pct=float(profile.book_haircut_pct),
        partial_fill_prob=float(profile.partial_fill_prob),
        timeout_prob=float(profile.timeout_prob),
        slippage_extra_bps=float(profile.slippage_extra_bps),
        trades_attempted=attempted,
        trades_accepted=accepted,
        fill_full_rate=_safe_ratio(accepted, attempted),
        partial_fill_rate=_safe_ratio(partial_count, attempted),
        timeout_rate=_safe_ratio(timeout_count, attempted),
        hedge_failed_rate=_safe_ratio(hedge_failed_count, attempted),
        avg_edge_predicted_pct=avg_pred,
        avg_edge_captured_pct=avg_cap,
        edge_capture_ratio=_safe_ratio(avg_cap, avg_pred) if abs(avg_pred) > 1e-12 else 0.0,
        pnl_total=pnl_total,
        pnl_per_trade=_safe_ratio(pnl_total, attempted),
        max_drawdown_pct=max_drawdown_pct,
        breaker_trigger_count=breaker_count,
        skip_rate=_safe_ratio(skipped, attempted),
        robustness_score=0.0,
    )


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_summary(path: Path, metrics: list[ProfileMetrics]) -> None:
    ranked = sorted(metrics, key=lambda m: m.robustness_score, reverse=True)
    risky = sorted(metrics, key=lambda m: m.robustness_score)
    top5 = ranked[:5]
    bottom5 = risky[:5]

    safe_latency = sorted([m.latency_ms_kalshi for m in top5])
    safe_drift = sorted([m.adverse_drift_bps for m in top5])
    safe_haircut = sorted([m.book_haircut_pct for m in top5])
    safe_timeout = sorted([m.timeout_prob for m in top5])
    safe_partial = sorted([m.partial_fill_prob for m in top5])

    lines = [
        "# Profile Grid Summary",
        "",
        f"- total_profiles: {len(metrics)}",
        "",
        "## Top 5 perfis recomendados (robustez)",
    ]
    for m in top5:
        lines.append(
            f"- {m.profile_name}: score={m.robustness_score:.4f} pnl_per_trade={m.pnl_per_trade:.4f} "
            f"timeout_rate={m.timeout_rate:.4f} hedge_failed_rate={m.hedge_failed_rate:.4f} dd={m.max_drawdown_pct:.2f}%"
        )
    lines.append("")
    lines.append("## Top 5 perfis perigosos")
    for m in bottom5:
        lines.append(
            f"- {m.profile_name}: score={m.robustness_score:.4f} pnl_per_trade={m.pnl_per_trade:.4f} "
            f"timeout_rate={m.timeout_rate:.4f} hedge_failed_rate={m.hedge_failed_rate:.4f} dd={m.max_drawdown_pct:.2f}%"
        )
    lines.append("")
    lines.append("## Zona segura sugerida para parametros live")
    lines.append(f"- latency_ms: {safe_latency[0]}..{safe_latency[-1]}")
    lines.append(f"- adverse_drift_bps: {safe_drift[0]:.2f}..{safe_drift[-1]:.2f}")
    lines.append(f"- book_haircut_pct: {safe_haircut[0]:.2f}..{safe_haircut[-1]:.2f}")
    lines.append(f"- partial_fill_prob: {safe_partial[0]:.2f}..{safe_partial[-1]:.2f}")
    lines.append(f"- timeout_prob: {safe_timeout[0]:.2f}..{safe_timeout[-1]:.2f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _apply_scores(metrics: list[ProfileMetrics]) -> None:
    pnl_norm = normalize_metric([m.pnl_per_trade for m in metrics])
    dd_norm = normalize_metric([m.max_drawdown_pct for m in metrics])
    for idx, metric in enumerate(metrics):
        metric.robustness_score = compute_robustness_score(
            normalized_pnl_per_trade=pnl_norm[idx],
            edge_capture_ratio=metric.edge_capture_ratio,
            timeout_rate=metric.timeout_rate,
            hedge_failed_rate=metric.hedge_failed_rate,
            max_drawdown_pct_norm=dd_norm[idx],
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles_json(args.profiles_file)
    if len(profiles) != 30:
        raise ValueError(f"expected exactly 30 profiles, got {len(profiles)}")

    metrics = [_simulate_profile(profile=p, args=args, profile_idx=idx, out_dir=out_dir) for idx, p in enumerate(profiles)]
    _apply_scores(metrics)
    rows = [m.to_dict() for m in sorted(metrics, key=lambda m: m.robustness_score, reverse=True)]

    csv_path = out_dir / "profile_results.csv"
    json_path = out_dir / "profile_results.json"
    summary_path = out_dir / "summary.md"
    _write_csv(csv_path, rows)
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=True), encoding="utf-8")
    _write_summary(summary_path, metrics)

    print(f"profiles_processed={len(rows)}")
    print(f"profiles_file={Path(args.profiles_file).resolve()}")
    print(f"csv_file={csv_path}")
    print(f"json_file={json_path}")
    print(f"summary_file={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
