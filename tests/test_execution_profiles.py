import random

import pytest

from bot.core.reason_codes import ACCEPTED
from bot.core.risk.guards import CircuitBreaker, RiskLimits
from bot.crypto_updown.runtime.execution_profile import (
    DRIFT_VALUES_BPS,
    HAIRCUT_VALUES_PCT,
    LATENCY_VALUES_MS,
    PARTIAL_VALUES,
    TIMEOUT_VALUES,
    ExecutionProfile,
    compute_robustness_score,
    generate_execution_profiles_30,
)
from bot.crypto_updown.runtime.live_runtime import CryptoExecutionRuntime, LegOrderRequest


def test_generate_exactly_30_profiles_unique_names():
    profiles = generate_execution_profiles_30()
    assert len(profiles) == 30
    assert len({p.name for p in profiles}) == 30
    assert profiles[0].name == "baseline"


def test_generated_profiles_respect_expected_ranges():
    profiles = generate_execution_profiles_30()
    for p in profiles:
        assert p.latency_ms_kalshi in LATENCY_VALUES_MS
        assert p.latency_ms_poly in LATENCY_VALUES_MS
        assert p.adverse_drift_bps in DRIFT_VALUES_BPS
        assert p.book_haircut_pct in HAIRCUT_VALUES_PCT
        assert p.partial_fill_prob in PARTIAL_VALUES
        assert p.timeout_prob in TIMEOUT_VALUES
        assert 0.0 <= p.slippage_extra_bps


def test_execution_profile_validations():
    with pytest.raises(ValueError):
        ExecutionProfile(
            name="bad",
            latency_ms_kalshi=-1,
            latency_ms_poly=10,
            adverse_drift_bps=2.0,
            book_haircut_pct=10.0,
            partial_fill_prob=0.1,
            timeout_prob=0.1,
            slippage_extra_bps=1.0,
        )
    with pytest.raises(ValueError):
        ExecutionProfile(
            name="bad",
            latency_ms_kalshi=10,
            latency_ms_poly=10,
            adverse_drift_bps=2.0,
            book_haircut_pct=120.0,
            partial_fill_prob=0.1,
            timeout_prob=0.1,
            slippage_extra_bps=1.0,
        )
    with pytest.raises(ValueError):
        ExecutionProfile(
            name="bad",
            latency_ms_kalshi=10,
            latency_ms_poly=10,
            adverse_drift_bps=2.0,
            book_haircut_pct=10.0,
            partial_fill_prob=1.2,
            timeout_prob=0.1,
            slippage_extra_bps=1.0,
        )


def test_simulated_executor_is_reproducible_with_fixed_seed():
    profile = generate_execution_profiles_30()[8]
    guard_a = CircuitBreaker(RiskLimits(max_losses_streak=9, max_daily_drawdown_pct=99.0, max_open_positions=1), day_start_equity=100.0)
    guard_b = CircuitBreaker(RiskLimits(max_losses_streak=9, max_daily_drawdown_pct=99.0, max_open_positions=1), day_start_equity=100.0)
    runtime_a = CryptoExecutionRuntime(risk_guard=guard_a)
    runtime_b = CryptoExecutionRuntime(risk_guard=guard_b)

    leg_a = LegOrderRequest(leg_name="leg_a", venue="kalshi", side="yes", price=0.45, quantity=10.0, timeout_sec=1.0)
    leg_b = LegOrderRequest(leg_name="leg_b", venue="polymarket", side="down", price=0.46, quantity=10.0, timeout_sec=1.0)
    exec_a = runtime_a.build_simulated_leg_executor(profile=profile, rng=random.Random(42), simulate_sleep=False)
    exec_b = runtime_b.build_simulated_leg_executor(profile=profile, rng=random.Random(42), simulate_sleep=False)

    out_a_1 = exec_a(leg_a)
    out_a_2 = exec_a(leg_b)
    out_b_1 = exec_b(leg_a)
    out_b_2 = exec_b(leg_b)

    assert out_a_1.status == out_b_1.status
    assert out_a_1.reason_code == out_b_1.reason_code
    assert out_a_1.filled_qty == out_b_1.filled_qty
    assert out_a_1.metadata == out_b_1.metadata
    assert out_a_2.status == out_b_2.status
    assert out_a_2.reason_code == out_b_2.reason_code
    assert out_a_2.filled_qty == out_b_2.filled_qty
    assert out_a_2.metadata == out_b_2.metadata
    assert out_a_1.reason_code != ACCEPTED or out_a_1.metadata["effective_price"] >= leg_a.price


def test_robustness_score_formula():
    score = compute_robustness_score(
        normalized_pnl_per_trade=0.8,
        edge_capture_ratio=0.7,
        timeout_rate=0.1,
        hedge_failed_rate=0.2,
        max_drawdown_pct_norm=0.3,
    )
    expected = (0.30 * 0.8) + (0.20 * 0.7) + (0.20 * 0.9) + (0.15 * 0.8) + (0.15 * 0.7)
    assert score == pytest.approx(expected, abs=1e-6)
