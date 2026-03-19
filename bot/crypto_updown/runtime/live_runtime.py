from dataclasses import dataclass
from datetime import datetime, timezone
import random
import time
from typing import Any, Callable, Optional

from bot.core.reason_codes import (
    ACCEPTED,
    CIRCUIT_BREAKER_TRIGGERED,
    HEDGE_FAILED,
    KILL_SWITCH_ACTIVE,
    LEG_TIMEOUT,
    PARTIAL_FILL,
)
from bot.crypto_updown.runtime.execution_profile import ExecutionProfile


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class LegOrderRequest:
    leg_name: str
    venue: str
    side: str
    price: float
    quantity: float
    timeout_sec: float


@dataclass(frozen=True)
class LegExecutionResult:
    status: str
    filled_qty: float
    reason_code: str
    detail: str
    elapsed_sec: float = 0.0
    metadata: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ExecutionDecision:
    accepted: bool
    reason_code: str
    detail: str
    leg_a: Optional[LegExecutionResult] = None
    leg_b: Optional[LegExecutionResult] = None
    hedge_attempted: bool = False
    hedge_ok: bool = False


class CryptoExecutionRuntime:
    def __init__(self, *, risk_guard: Any, store: Any = None, event_logger: Any = None) -> None:
        self.risk_guard = risk_guard
        self.store = store
        self.event_logger = event_logger

    def _log_event(self, event_type: str, payload: dict) -> None:
        if self.event_logger is None:
            return
        try:
            self.event_logger.log(event_type, payload)
        except Exception:
            pass

    def _record_skip(
        self,
        *,
        reason_code: str,
        detail: str,
        market_key: str,
        strategy: str,
        edge_liquido_pct: Optional[float],
        liq_k: Optional[float],
        liq_p: Optional[float],
    ) -> None:
        if self.store is None:
            return
        try:
            self.store.record_skip(
                ts_utc=_iso_utc_now(),
                reason_code=reason_code,
                detail=detail,
                market_key_k=market_key,
                market_key_p=market_key,
                strategy=strategy,
                edge_liquido_pct=edge_liquido_pct,
                liq_k=liq_k,
                liq_p=liq_p,
                metadata={},
            )
        except Exception:
            pass

    def _record_leg(self, *, trade_id: str, market_key: str, leg: LegOrderRequest, result: LegExecutionResult) -> None:
        fill_price = float(leg.price)
        metadata = dict(result.metadata or {})
        if "effective_price" in metadata:
            try:
                fill_price = float(metadata["effective_price"])
            except Exception:
                fill_price = float(leg.price)
        if self.store is not None:
            try:
                self.store.record_order(
                    ts_utc=_iso_utc_now(),
                    venue=leg.venue,
                    trade_id=trade_id,
                    market_key=market_key,
                    order_id="",
                    client_order_id=f"{trade_id}_{leg.leg_name}",
                    side=leg.side.lower(),
                    action="buy",
                    price=leg.price,
                    quantity=leg.quantity,
                    status=result.status,
                    metadata={
                        "reason_code": result.reason_code,
                        "detail": result.detail,
                        "elapsed_sec": result.elapsed_sec,
                        "simulation": metadata,
                    },
                )
            except Exception:
                pass
            if float(result.filled_qty) > 0:
                try:
                    self.store.record_fill(
                        ts_utc=_iso_utc_now(),
                        venue=leg.venue,
                        trade_id=trade_id,
                        market_key=market_key,
                        order_id="",
                        fill_price=fill_price,
                        fill_qty=result.filled_qty,
                        fee=0.0,
                        metadata={
                            "leg_name": leg.leg_name,
                            "reason_code": result.reason_code,
                            "simulation": metadata,
                        },
                    )
                except Exception:
                    pass
        self._log_event(
            "leg_execution",
            {
                "trade_id": trade_id,
                "market_key": market_key,
                "leg_name": leg.leg_name,
                "venue": leg.venue,
                "side": leg.side,
                "price": leg.price,
                "quantity": leg.quantity,
                "status": result.status,
                "filled_qty": result.filled_qty,
                "reason_code": result.reason_code,
                "detail": result.detail,
                "elapsed_sec": result.elapsed_sec,
                "simulation": metadata,
            },
        )

    @staticmethod
    def _latency_for_venue_ms(profile: ExecutionProfile, venue: str) -> int:
        venue_norm = str(venue).strip().lower()
        if venue_norm == "kalshi":
            return int(profile.latency_ms_kalshi)
        return int(profile.latency_ms_poly)

    def build_simulated_leg_executor(
        self,
        *,
        profile: ExecutionProfile,
        rng: random.Random | None = None,
        simulate_sleep: bool = False,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> Callable[[LegOrderRequest], LegExecutionResult]:
        local_rng = rng or random.Random()
        sleeper = sleep_fn or time.sleep

        def _execute_leg(leg: LegOrderRequest) -> LegExecutionResult:
            latency_ms = self._latency_for_venue_ms(profile, leg.venue)
            elapsed_sec = max(0.0, float(latency_ms) / 1000.0)
            if simulate_sleep and elapsed_sec > 0:
                sleeper(elapsed_sec)

            quantity = max(0.0, float(leg.quantity))
            if local_rng.random() < float(profile.timeout_prob):
                return LegExecutionResult(
                    status=LEG_TIMEOUT,
                    filled_qty=0.0,
                    reason_code=LEG_TIMEOUT,
                    detail="simulated leg timeout",
                    elapsed_sec=elapsed_sec,
                    metadata={
                        "profile_name": profile.name,
                        "latency_ms": latency_ms,
                        "effective_price": float(leg.price),
                        "requested_price": float(leg.price),
                        "book_haircut_pct": float(profile.book_haircut_pct),
                        "timed_out": True,
                    },
                )

            depth_factor = max(0.0, 1.0 - (float(profile.book_haircut_pct) / 100.0))
            raw_depth_factor = 0.70 + (1.60 * local_rng.random())
            raw_depth = quantity * raw_depth_factor
            effective_depth = raw_depth * depth_factor
            filled_qty = min(quantity, effective_depth)

            if filled_qty > 0.0 and local_rng.random() < float(profile.partial_fill_prob):
                partial_factor = 0.35 + (0.5 * local_rng.random())
                filled_qty = min(filled_qty, quantity * partial_factor)

            adverse_bps = float(profile.adverse_drift_bps) + float(profile.slippage_extra_bps)
            adverse_multiplier = 1.0 + (adverse_bps / 10000.0)
            effective_price = float(leg.price) * adverse_multiplier

            if filled_qty + 1e-9 < quantity:
                return LegExecutionResult(
                    status=PARTIAL_FILL,
                    filled_qty=max(0.0, filled_qty),
                    reason_code=PARTIAL_FILL,
                    detail="simulated partial fill",
                    elapsed_sec=elapsed_sec,
                    metadata={
                        "profile_name": profile.name,
                        "latency_ms": latency_ms,
                        "effective_price": effective_price,
                        "requested_price": float(leg.price),
                        "book_haircut_pct": float(profile.book_haircut_pct),
                        "raw_depth": raw_depth,
                        "raw_depth_factor": raw_depth_factor,
                        "adverse_drift_bps": float(profile.adverse_drift_bps),
                        "slippage_extra_bps": float(profile.slippage_extra_bps),
                        "timed_out": False,
                    },
                )

            return LegExecutionResult(
                status="filled",
                filled_qty=quantity,
                reason_code=ACCEPTED,
                detail="simulated full fill",
                elapsed_sec=elapsed_sec,
                metadata={
                    "profile_name": profile.name,
                    "latency_ms": latency_ms,
                    "effective_price": effective_price,
                    "requested_price": float(leg.price),
                    "book_haircut_pct": float(profile.book_haircut_pct),
                    "raw_depth": raw_depth,
                    "raw_depth_factor": raw_depth_factor,
                    "adverse_drift_bps": float(profile.adverse_drift_bps),
                    "slippage_extra_bps": float(profile.slippage_extra_bps),
                    "timed_out": False,
                },
            )

        return _execute_leg

    def execute(
        self,
        *,
        trade_id: str,
        market_key: str,
        strategy: str,
        current_equity: float,
        open_positions: int,
        edge_liquido_pct: Optional[float],
        liq_k: Optional[float],
        liq_p: Optional[float],
        pretrade_revalidate: Callable[[], tuple[bool, str, str]],
        leg_a: LegOrderRequest,
        leg_b: LegOrderRequest,
        execute_leg: Callable[[LegOrderRequest], LegExecutionResult],
        hedge_flatten: Optional[Callable[[LegExecutionResult, LegExecutionResult], bool]] = None,
    ) -> ExecutionDecision:
        guard = self.risk_guard.evaluate_entry(current_equity=current_equity, open_positions=open_positions)
        if not guard.ok:
            reason = KILL_SWITCH_ACTIVE if guard.reason_code == KILL_SWITCH_ACTIVE else CIRCUIT_BREAKER_TRIGGERED
            self._record_skip(
                reason_code=reason,
                detail=guard.detail,
                market_key=market_key,
                strategy=strategy,
                edge_liquido_pct=edge_liquido_pct,
                liq_k=liq_k,
                liq_p=liq_p,
            )
            self._log_event(
                "execution_blocked",
                {
                    "trade_id": trade_id,
                    "market_key": market_key,
                    "strategy": strategy,
                    "reason_code": reason,
                    "detail": guard.detail,
                },
            )
            return ExecutionDecision(False, reason, guard.detail)

        ok, reason_code, detail = pretrade_revalidate()
        if not ok:
            self._record_skip(
                reason_code=reason_code,
                detail=detail,
                market_key=market_key,
                strategy=strategy,
                edge_liquido_pct=edge_liquido_pct,
                liq_k=liq_k,
                liq_p=liq_p,
            )
            self._log_event(
                "pretrade_revalidate_failed",
                {
                    "trade_id": trade_id,
                    "market_key": market_key,
                    "strategy": strategy,
                    "reason_code": reason_code,
                    "detail": detail,
                },
            )
            return ExecutionDecision(False, reason_code, detail)

        res_a = execute_leg(leg_a)
        self._record_leg(trade_id=trade_id, market_key=market_key, leg=leg_a, result=res_a)
        if res_a.reason_code == LEG_TIMEOUT:
            msg = f"{leg_a.leg_name} timeout: {res_a.detail}"
            self._record_skip(
                reason_code=LEG_TIMEOUT,
                detail=msg,
                market_key=market_key,
                strategy=strategy,
                edge_liquido_pct=edge_liquido_pct,
                liq_k=liq_k,
                liq_p=liq_p,
            )
            return ExecutionDecision(False, LEG_TIMEOUT, msg, leg_a=res_a)

        res_b = execute_leg(leg_b)
        self._record_leg(trade_id=trade_id, market_key=market_key, leg=leg_b, result=res_b)
        if res_b.reason_code == LEG_TIMEOUT:
            msg = f"{leg_b.leg_name} timeout: {res_b.detail}"
            self._record_skip(
                reason_code=LEG_TIMEOUT,
                detail=msg,
                market_key=market_key,
                strategy=strategy,
                edge_liquido_pct=edge_liquido_pct,
                liq_k=liq_k,
                liq_p=liq_p,
            )
            return ExecutionDecision(False, LEG_TIMEOUT, msg, leg_a=res_a, leg_b=res_b)

        partial = False
        if res_a.status == PARTIAL_FILL or float(res_a.filled_qty) < float(leg_a.quantity):
            partial = True
        if res_b.status == PARTIAL_FILL or float(res_b.filled_qty) < float(leg_b.quantity):
            partial = True

        if partial:
            hedge_ok = bool(hedge_flatten(res_a, res_b)) if hedge_flatten is not None else False
            reason = PARTIAL_FILL if hedge_ok else HEDGE_FAILED
            detail = "partial fill handled by hedge path" if hedge_ok else "partial fill and hedge failed"
            self._record_skip(
                reason_code=reason,
                detail=detail,
                market_key=market_key,
                strategy=strategy,
                edge_liquido_pct=edge_liquido_pct,
                liq_k=liq_k,
                liq_p=liq_p,
            )
            self._log_event(
                "partial_fill",
                {
                    "trade_id": trade_id,
                    "market_key": market_key,
                    "strategy": strategy,
                    "reason_code": reason,
                    "detail": detail,
                    "hedge_ok": hedge_ok,
                    "leg_a_status": res_a.status,
                    "leg_b_status": res_b.status,
                    "leg_a_filled_qty": res_a.filled_qty,
                    "leg_b_filled_qty": res_b.filled_qty,
                },
            )
            return ExecutionDecision(
                accepted=False,
                reason_code=reason,
                detail=detail,
                leg_a=res_a,
                leg_b=res_b,
                hedge_attempted=True,
                hedge_ok=hedge_ok,
            )

        self._log_event(
            "execution_accepted",
            {
                "trade_id": trade_id,
                "market_key": market_key,
                "strategy": strategy,
                "reason_code": ACCEPTED,
                "detail": "both legs filled",
            },
        )
        return ExecutionDecision(True, ACCEPTED, "both legs filled", leg_a=res_a, leg_b=res_b)
