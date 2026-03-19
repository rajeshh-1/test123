from dataclasses import dataclass

from .edge import EdgeResult


@dataclass(frozen=True)
class PreTradeRequest:
    strategy: str
    market_key_k: str
    market_key_p: str
    semantic_equivalent: bool
    resolution_compatible: bool
    edge: EdgeResult
    min_edge_pct: float
    liquidity_k: float
    liquidity_p: float
    min_liquidity: float


@dataclass(frozen=True)
class PreTradeDecision:
    ok: bool
    reason_code: str
    detail: str


def validate_pretrade(req: PreTradeRequest) -> PreTradeDecision:
    if not req.market_key_k or not req.market_key_p or req.market_key_k != req.market_key_p:
        return PreTradeDecision(False, "invalid_market_mismatch", "market_key mismatch")
    if not req.semantic_equivalent:
        return PreTradeDecision(False, "semantic_mismatch", "markets are not semantically equivalent")
    if not req.resolution_compatible:
        return PreTradeDecision(False, "resolution_rule_mismatch", "resolution rules are incompatible")
    if req.edge.edge_liquido <= 0:
        return PreTradeDecision(False, "negative_edge", f"edge_liquido={req.edge.edge_liquido:.6f}")
    if req.edge.edge_liquido_pct < float(req.min_edge_pct):
        return PreTradeDecision(
            False,
            "below_min_edge",
            f"edge_liquido_pct={req.edge.edge_liquido_pct:.6f} < min_edge_pct={float(req.min_edge_pct):.6f}",
        )
    liq_k = max(0.0, float(req.liquidity_k))
    liq_p = max(0.0, float(req.liquidity_p))
    liq_min = max(0.0, float(req.min_liquidity))
    if liq_k < liq_min or liq_p < liq_min:
        return PreTradeDecision(
            False,
            "insufficient_liquidity",
            f"liq_k={liq_k:.6f} liq_p={liq_p:.6f} min={liq_min:.6f}",
        )
    return PreTradeDecision(True, "accepted", "pretrade validator passed")

