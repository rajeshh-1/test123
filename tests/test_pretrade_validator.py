from arb_engine.edge import calculate_edge_from_legs
from arb_engine.pretrade import PreTradeRequest, validate_pretrade


def _base_req():
    edge = calculate_edge_from_legs(
        kalshi_leg_price=0.45,
        poly_leg_price=0.45,
        fee_kalshi_bps=0.0,
        fee_poly_bps=25.0,
        slippage_expected_bps=5.0,
        custo_leg_risk=0.005,
        payout_esperado=1.0,
    )
    return PreTradeRequest(
        strategy="A_KALSHI_YES_PLUS_POLY_DOWN",
        market_key_k="BTC15M_2026-03-19T12:15:00Z",
        market_key_p="BTC15M_2026-03-19T12:15:00Z",
        semantic_equivalent=True,
        resolution_compatible=True,
        edge=edge,
        min_edge_pct=5.0,
        liquidity_k=20.0,
        liquidity_p=20.0,
        min_liquidity=5.0,
    )


def test_pretrade_accepts_valid_request():
    decision = validate_pretrade(_base_req())
    assert decision.ok is True
    assert decision.reason_code == "accepted"


def test_pretrade_rejects_semantic_mismatch():
    req = _base_req()
    req = PreTradeRequest(**{**req.__dict__, "semantic_equivalent": False})
    decision = validate_pretrade(req)
    assert decision.ok is False
    assert decision.reason_code == "semantic_mismatch"


def test_pretrade_rejects_resolution_mismatch():
    req = _base_req()
    req = PreTradeRequest(**{**req.__dict__, "resolution_compatible": False})
    decision = validate_pretrade(req)
    assert decision.ok is False
    assert decision.reason_code == "resolution_rule_mismatch"


def test_pretrade_rejects_low_liquidity():
    req = _base_req()
    req = PreTradeRequest(**{**req.__dict__, "liquidity_p": 1.0})
    decision = validate_pretrade(req)
    assert decision.ok is False
    assert decision.reason_code == "insufficient_liquidity"

