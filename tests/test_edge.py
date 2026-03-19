import pytest

from arb_engine.edge import EdgeInputs, calculate_edge_from_legs, calculate_net_edge


def test_edge_formula_with_fee_slippage_legrisk():
    result = calculate_edge_from_legs(
        kalshi_leg_price=0.44,
        poly_leg_price=0.46,
        fee_kalshi_bps=0.0,
        fee_poly_bps=25.0,
        slippage_expected_bps=10.0,
        custo_leg_risk=0.01,
        payout_esperado=1.0,
    )
    assert result.custo_total == pytest.approx(0.91205, rel=1e-9)
    assert result.edge_liquido == pytest.approx(0.08795, rel=1e-9)
    assert result.edge_liquido_pct == pytest.approx(9.643112, rel=1e-6)
    assert result.positivo is True


def test_edge_negative_when_total_cost_above_payout():
    result = calculate_net_edge(
        EdgeInputs(
            payout_esperado=1.0,
            preco_total=0.98,
            fees=0.02,
            slippage_esperado=0.01,
            custo_leg_risk=0.03,
        )
    )
    assert result.custo_total == pytest.approx(1.04, rel=1e-9)
    assert result.edge_liquido == pytest.approx(-0.04, rel=1e-9)
    assert result.positivo is False


def test_edge_rejects_negative_inputs():
    with pytest.raises(ValueError):
        calculate_net_edge(
            EdgeInputs(
                payout_esperado=1.0,
                preco_total=0.9,
                fees=-0.01,
                slippage_esperado=0.0,
                custo_leg_risk=0.0,
            )
        )

