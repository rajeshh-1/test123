# Migration Guide

## Objective
Keep legacy commands working while the internal structure migrates to `bot/core` and `scripts/`.

## Legacy commands (compatible)
- `python logs/run_arb_dry_run.py --mode replay`
- `python logs/analyze_arb.py --min-edge-pct 5`
- `python watch_btc_15m_kalshi.py --interval 0.1`
- `python watch_btc_15m_poly.py --interval 0.1`

All of them still work, but may print warnings:
- `DEPRECATED: use scripts/crypto_cli.py`
- `DEPRECATED: use bot.core.*`

## New initial entrypoint
- `python scripts/arb_cli.py --execution-mode paper --min-edge-pct 5 --min-liquidity 1`

## Domain entrypoints (Phase 4.1)
- Crypto:
  - new: `python scripts/crypto_cli.py --execution-mode paper --min-edge-pct 5 --min-liquidity 1`
  - legacy wrapper: `python scripts/arb_cli.py --execution-mode paper --min-edge-pct 5 --min-liquidity 1`
- Sports:
  - new: `python scripts/sports_cli.py --execution-mode paper --market-scope moneyline`
  - legacy flows stay available (watchers/legacy scripts) and will be migrated gradually.

## Module mapping
- `logs/arb_engine/config.py` -> `bot/core/config.py`
- `logs/arb_engine/edge.py` -> `bot/core/edge.py`
- `logs/arb_engine/pretrade.py` -> `bot/core/pretrade.py`
- `logs/arb_engine/persistence.py` -> `bot/core/storage/sqlite_store.py`
- `logs/arb_engine/jsonl_log.py` -> `bot/core/storage/jsonl_logger.py`
- `logs/kalshi_order_client.py` -> `bot/core/execution/kalshi_client.py`

## Operational Status
Domain split and crypto runtime hardening are active in phase 4.x.

## Quality gate commands (Phase 3)
- `make compile`
- `make test`
- `make check`

If `make` is not available, run:
- `python scripts/quality_gate.py check`
- or run commands directly:
  - `python -m compileall -q bot scripts tests logs/arb_engine logs/run_arb_dry_run.py logs/live_direct_arb.py logs/analyze_arb.py`
  - `python -m pytest -q`

## Quando Usar Cada CLI
| Situacao | CLI recomendado | Observacao |
|---|---|---|
| Validar configuracao e risco de crypto | `python scripts/crypto_cli.py ...` | Principal para BTC up/down |
| Validar configuracao e escopo sports | `python scripts/sports_cli.py ...` | Principal para dominio sports |
| Compatibilidade com comando antigo | `python scripts/arb_cli.py ...` | Wrapper legado com warning deprecado |
