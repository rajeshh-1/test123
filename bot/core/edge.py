from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeInputs:
    payout_esperado: float
    preco_total: float
    fees: float
    slippage_esperado: float
    custo_leg_risk: float


@dataclass(frozen=True)
class EdgeResult:
    payout_esperado: float
    preco_total: float
    fees: float
    slippage_esperado: float
    custo_leg_risk: float
    custo_total: float
    edge_liquido: float
    edge_liquido_pct: float
    positivo: bool


def _safe_non_negative(name: str, value: float) -> float:
    out = float(value)
    if out < 0:
        raise ValueError(f"{name} must be non-negative")
    return out


def calculate_net_edge(inp: EdgeInputs) -> EdgeResult:
    payout = float(inp.payout_esperado)
    if payout <= 0:
        raise ValueError("payout_esperado must be > 0")
    preco_total = _safe_non_negative("preco_total", inp.preco_total)
    fees = _safe_non_negative("fees", inp.fees)
    slippage = _safe_non_negative("slippage_esperado", inp.slippage_esperado)
    leg_risk = _safe_non_negative("custo_leg_risk", inp.custo_leg_risk)
    custo_total = preco_total + fees + slippage + leg_risk
    edge_liquido = payout - custo_total
    base = custo_total if custo_total > 0 else payout
    edge_pct = (edge_liquido / base) * 100.0
    return EdgeResult(
        payout_esperado=payout,
        preco_total=preco_total,
        fees=fees,
        slippage_esperado=slippage,
        custo_leg_risk=leg_risk,
        custo_total=custo_total,
        edge_liquido=edge_liquido,
        edge_liquido_pct=edge_pct,
        positivo=edge_liquido > 0,
    )


def calculate_edge_from_legs(
    *,
    kalshi_leg_price: float,
    poly_leg_price: float,
    fee_kalshi_bps: float,
    fee_poly_bps: float,
    slippage_expected_bps: float,
    custo_leg_risk: float,
    payout_esperado: float = 1.0,
) -> EdgeResult:
    k_px = _safe_non_negative("kalshi_leg_price", kalshi_leg_price)
    p_px = _safe_non_negative("poly_leg_price", poly_leg_price)
    fee_k = _safe_non_negative("fee_kalshi_bps", fee_kalshi_bps) / 10000.0
    fee_p = _safe_non_negative("fee_poly_bps", fee_poly_bps) / 10000.0
    slip = _safe_non_negative("slippage_expected_bps", slippage_expected_bps) / 10000.0

    preco_total = k_px + p_px
    fees = (k_px * fee_k) + (p_px * fee_p)
    slippage = preco_total * slip
    return calculate_net_edge(
        EdgeInputs(
            payout_esperado=float(payout_esperado),
            preco_total=preco_total,
            fees=fees,
            slippage_esperado=slippage,
            custo_leg_risk=float(custo_leg_risk),
        )
    )

