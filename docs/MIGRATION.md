# Migration Guide (Phase 2)

## Objective
Keep legacy commands working while the internal structure migrates to `bot/core` and `scripts/`.

## Legacy commands (compatible)
- `python logs/run_arb_dry_run.py --mode replay`
- `python logs/analyze_arb.py --min-edge-pct 5`
- `python watch_btc_15m_kalshi.py --interval 0.1`
- `python watch_btc_15m_poly.py --interval 0.1`

All of them still work, but may print warnings:
- `DEPRECATED: use scripts/arb_cli.py`
- `DEPRECATED: use bot.core.*`

## New initial entrypoint
- `python scripts/arb_cli.py --execution-mode paper --min-edge-pct 5 --min-liquidity 1`

## Module mapping
- `logs/arb_engine/config.py` -> `bot/core/config.py`
- `logs/arb_engine/edge.py` -> `bot/core/edge.py`
- `logs/arb_engine/pretrade.py` -> `bot/core/pretrade.py`
- `logs/arb_engine/persistence.py` -> `bot/core/storage/sqlite_store.py`
- `logs/arb_engine/jsonl_log.py` -> `bot/core/storage/jsonl_logger.py`
- `logs/kalshi_order_client.py` -> `bot/core/execution/kalshi_client.py`

## Planned next step
In Phase 3, tests and smoke tests will be standardized around the new entrypoint and legacy wrappers.
