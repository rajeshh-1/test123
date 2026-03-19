from arb_engine.edge import calculate_edge_from_legs
from arb_engine.pretrade import PreTradeRequest, validate_pretrade


def _best_ask(orderbook: dict) -> tuple[float, float]:
    asks = orderbook.get("asks", [])
    best = min(asks, key=lambda x: x["price"])
    return float(best["price"]), float(best["size"])


def test_mock_orderbook_pipeline_accepts_trade():
    kalshi_book_yes = {"asks": [{"price": 0.44, "size": 50.0}, {"price": 0.45, "size": 80.0}]}
    poly_book_down = {"asks": [{"price": 0.45, "size": 70.0}, {"price": 0.46, "size": 100.0}]}
    k_px, k_liq = _best_ask(kalshi_book_yes)
    p_px, p_liq = _best_ask(poly_book_down)

    edge = calculate_edge_from_legs(
        kalshi_leg_price=k_px,
        poly_leg_price=p_px,
        fee_kalshi_bps=0.0,
        fee_poly_bps=25.0,
        slippage_expected_bps=5.0,
        custo_leg_risk=0.005,
    )
    decision = validate_pretrade(
        PreTradeRequest(
            strategy="A_KALSHI_YES_PLUS_POLY_DOWN",
            market_key_k="BTC15M_2026-03-19T12:15:00Z",
            market_key_p="BTC15M_2026-03-19T12:15:00Z",
            semantic_equivalent=True,
            resolution_compatible=True,
            edge=edge,
            min_edge_pct=5.0,
            liquidity_k=k_liq,
            liquidity_p=p_liq,
            min_liquidity=10.0,
        )
    )
    assert edge.edge_liquido > 0
    assert decision.ok is True


def test_mock_orderbook_pipeline_rejects_by_liquidity():
    kalshi_book_yes = {"asks": [{"price": 0.44, "size": 2.0}]}
    poly_book_down = {"asks": [{"price": 0.45, "size": 3.0}]}
    k_px, k_liq = _best_ask(kalshi_book_yes)
    p_px, p_liq = _best_ask(poly_book_down)

    edge = calculate_edge_from_legs(
        kalshi_leg_price=k_px,
        poly_leg_price=p_px,
        fee_kalshi_bps=0.0,
        fee_poly_bps=25.0,
        slippage_expected_bps=0.0,
        custo_leg_risk=0.0,
    )
    decision = validate_pretrade(
        PreTradeRequest(
            strategy="A_KALSHI_YES_PLUS_POLY_DOWN",
            market_key_k="BTC15M_2026-03-19T12:15:00Z",
            market_key_p="BTC15M_2026-03-19T12:15:00Z",
            semantic_equivalent=True,
            resolution_compatible=True,
            edge=edge,
            min_edge_pct=5.0,
            liquidity_k=k_liq,
            liquidity_p=p_liq,
            min_liquidity=5.0,
        )
    )
    assert decision.ok is False
    assert decision.reason_code == "insufficient_liquidity"

