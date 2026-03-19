from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


LATENCY_VALUES_MS = (200, 600, 1200, 2000, 3000)
DRIFT_VALUES_BPS = (2.0, 6.0, 12.0, 20.0, 30.0)
HAIRCUT_VALUES_PCT = (10.0, 25.0, 40.0, 55.0, 70.0)
PARTIAL_VALUES = (0.05, 0.15, 0.30, 0.45)
TIMEOUT_VALUES = (0.01, 0.05, 0.10, 0.20, 0.25)


def _in_01(value: float, *, field_name: str) -> None:
    if not (0.0 <= float(value) <= 1.0):
        raise ValueError(f"{field_name} must be between 0 and 1")


def _clamp_01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    latency_ms_kalshi: int
    latency_ms_poly: int
    adverse_drift_bps: float
    book_haircut_pct: float
    partial_fill_prob: float
    timeout_prob: float
    slippage_extra_bps: float

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("name must be non-empty")
        if int(self.latency_ms_kalshi) < 0:
            raise ValueError("latency_ms_kalshi must be >= 0")
        if int(self.latency_ms_poly) < 0:
            raise ValueError("latency_ms_poly must be >= 0")
        if float(self.book_haircut_pct) < 0.0 or float(self.book_haircut_pct) > 100.0:
            raise ValueError("book_haircut_pct must be between 0 and 100")
        if float(self.adverse_drift_bps) < 0.0:
            raise ValueError("adverse_drift_bps must be >= 0")
        if float(self.slippage_extra_bps) < 0.0:
            raise ValueError("slippage_extra_bps must be >= 0")
        _in_01(float(self.partial_fill_prob), field_name="partial_fill_prob")
        _in_01(float(self.timeout_prob), field_name="timeout_prob")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "latency_ms_kalshi": int(self.latency_ms_kalshi),
            "latency_ms_poly": int(self.latency_ms_poly),
            "adverse_drift_bps": float(self.adverse_drift_bps),
            "book_haircut_pct": float(self.book_haircut_pct),
            "partial_fill_prob": float(self.partial_fill_prob),
            "timeout_prob": float(self.timeout_prob),
            "slippage_extra_bps": float(self.slippage_extra_bps),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ExecutionProfile":
        return cls(
            name=str(row.get("name", "")).strip(),
            latency_ms_kalshi=int(row.get("latency_ms_kalshi", 0)),
            latency_ms_poly=int(row.get("latency_ms_poly", 0)),
            adverse_drift_bps=float(row.get("adverse_drift_bps", 0.0)),
            book_haircut_pct=float(row.get("book_haircut_pct", 0.0)),
            partial_fill_prob=float(row.get("partial_fill_prob", 0.0)),
            timeout_prob=float(row.get("timeout_prob", 0.0)),
            slippage_extra_bps=float(row.get("slippage_extra_bps", 0.0)),
        )


def _profile(
    *,
    name: str,
    latency: int,
    drift_bps: float,
    haircut_pct: float,
    partial_prob: float,
    timeout_prob: float,
    slippage_bps: float,
) -> ExecutionProfile:
    return ExecutionProfile(
        name=name,
        latency_ms_kalshi=int(latency),
        latency_ms_poly=int(latency),
        adverse_drift_bps=float(drift_bps),
        book_haircut_pct=float(haircut_pct),
        partial_fill_prob=float(partial_prob),
        timeout_prob=float(timeout_prob),
        slippage_extra_bps=float(slippage_bps),
    )


def generate_execution_profiles_30() -> list[ExecutionProfile]:
    # Exactly 30 deterministic profiles:
    # 1 baseline, 5 latency-high low-degradation, 6 bad-book medium-latency,
    # 13 mixed stress, 5 crash-tail.
    specs = [
        ("baseline", 200, 2.0, 10.0, 0.05, 0.01, 1.0),
        ("latency_high_low_deg_01", 1200, 2.0, 10.0, 0.05, 0.01, 2.0),
        ("latency_high_low_deg_02", 2000, 2.0, 10.0, 0.05, 0.05, 2.0),
        ("latency_high_low_deg_03", 3000, 2.0, 10.0, 0.05, 0.05, 3.0),
        ("latency_high_low_deg_04", 3000, 6.0, 10.0, 0.15, 0.05, 3.0),
        ("latency_high_low_deg_05", 2000, 6.0, 25.0, 0.15, 0.05, 4.0),
        ("book_bad_latency_mid_01", 600, 6.0, 40.0, 0.15, 0.05, 4.0),
        ("book_bad_latency_mid_02", 600, 12.0, 55.0, 0.30, 0.10, 6.0),
        ("book_bad_latency_mid_03", 1200, 12.0, 55.0, 0.30, 0.10, 8.0),
        ("book_bad_latency_mid_04", 1200, 20.0, 70.0, 0.30, 0.10, 10.0),
        ("book_bad_latency_mid_05", 600, 20.0, 70.0, 0.45, 0.20, 12.0),
        ("book_bad_latency_mid_06", 1200, 30.0, 70.0, 0.45, 0.25, 15.0),
        ("stress_mix_01", 200, 6.0, 25.0, 0.15, 0.05, 2.0),
        ("stress_mix_02", 200, 12.0, 25.0, 0.15, 0.05, 3.0),
        ("stress_mix_03", 600, 12.0, 40.0, 0.15, 0.10, 5.0),
        ("stress_mix_04", 600, 20.0, 40.0, 0.30, 0.10, 7.0),
        ("stress_mix_05", 1200, 20.0, 40.0, 0.30, 0.20, 9.0),
        ("stress_mix_06", 1200, 30.0, 55.0, 0.30, 0.20, 11.0),
        ("stress_mix_07", 2000, 12.0, 25.0, 0.30, 0.10, 6.0),
        ("stress_mix_08", 2000, 20.0, 55.0, 0.30, 0.20, 12.0),
        ("stress_mix_09", 2000, 30.0, 55.0, 0.45, 0.20, 14.0),
        ("stress_mix_10", 3000, 12.0, 25.0, 0.30, 0.10, 7.0),
        ("stress_mix_11", 3000, 20.0, 40.0, 0.45, 0.20, 13.0),
        ("stress_mix_12", 3000, 30.0, 55.0, 0.45, 0.25, 16.0),
        ("stress_mix_13", 1200, 6.0, 25.0, 0.05, 0.05, 3.0),
        ("crash_tail_01", 2000, 30.0, 70.0, 0.45, 0.25, 18.0),
        ("crash_tail_02", 3000, 30.0, 70.0, 0.45, 0.25, 20.0),
        ("crash_tail_03", 3000, 20.0, 70.0, 0.45, 0.25, 18.0),
        ("crash_tail_04", 3000, 30.0, 55.0, 0.45, 0.25, 17.0),
        ("crash_tail_05", 2000, 30.0, 70.0, 0.30, 0.25, 16.0),
    ]
    profiles = [
        _profile(
            name=name,
            latency=latency,
            drift_bps=drift_bps,
            haircut_pct=haircut_pct,
            partial_prob=partial_prob,
            timeout_prob=timeout_prob,
            slippage_bps=slippage_bps,
        )
        for (name, latency, drift_bps, haircut_pct, partial_prob, timeout_prob, slippage_bps) in specs
    ]
    if len(profiles) != 30:
        raise RuntimeError("profile grid must contain exactly 30 profiles")
    unique_names = {p.name for p in profiles}
    if len(unique_names) != 30:
        raise RuntimeError("profile names must be unique")
    return profiles


def save_profiles_json(profiles: Iterable[ExecutionProfile], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [p.to_dict() for p in profiles]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return path


def load_profiles_json(path: str | Path) -> list[ExecutionProfile]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("profiles file must contain a JSON list")
    return [ExecutionProfile.from_dict(item) for item in raw]


def normalize_metric(values: list[float]) -> list[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if abs(hi - lo) < 1e-12:
        return [1.0 for _ in values]
    return [_clamp_01((float(v) - lo) / (hi - lo)) for v in values]


def compute_robustness_score(
    *,
    normalized_pnl_per_trade: float,
    edge_capture_ratio: float,
    timeout_rate: float,
    hedge_failed_rate: float,
    max_drawdown_pct_norm: float,
) -> float:
    score = (
        0.30 * _clamp_01(normalized_pnl_per_trade)
        + 0.20 * _clamp_01(edge_capture_ratio)
        + 0.20 * (1.0 - _clamp_01(timeout_rate))
        + 0.15 * (1.0 - _clamp_01(hedge_failed_rate))
        + 0.15 * (1.0 - _clamp_01(max_drawdown_pct_norm))
    )
    return round(_clamp_01(score), 6)
