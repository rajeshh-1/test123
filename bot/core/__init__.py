"""Shared core primitives for all arbitrage domains."""

from .edge import EdgeInputs, EdgeResult, calculate_edge_from_legs, calculate_net_edge
from .pretrade import PreTradeDecision, PreTradeRequest, validate_pretrade

__all__ = [
    "EdgeInputs",
    "EdgeResult",
    "PreTradeDecision",
    "PreTradeRequest",
    "calculate_net_edge",
    "calculate_edge_from_legs",
    "validate_pretrade",
]
