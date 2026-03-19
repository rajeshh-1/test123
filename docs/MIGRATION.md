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

## Legacy Script Status (Phase 5.1)
Fonte detalhada: `docs/LEGACY_STATUS.md`.

| Script/Entrypoint | Status | Wrapper/Replacement | Target removal date |
|---|---|---|---|
| `scripts/crypto_cli.py` | official | - | - |
| `scripts/sports_cli.py` | official | - | - |
| `scripts/quality_gate.py` | official | - | - |
| `scripts/arb_cli.py` | keep (wrapper) | `scripts/crypto_cli.py` | 2026-09-30 |
| `logs/run_arb_dry_run.py` | keep (wrapper) | runtime crypto + `scripts/crypto_cli.py` | 2026-10-31 |
| `run_arb_bot.bat` | keep (wrapper) | official CLIs | 2026-12-31 |
| `start_btc_15m_monitors.bat` | deprecated | `scripts/crypto_cli.py` | 2026-09-30 |
| `watch_btc_15m_kalshi.py` | deprecated | runtime crypto feed | 2026-10-31 |
| `watch_btc_15m_poly.py` | deprecated | runtime crypto feed | 2026-10-31 |
| `logs/analyze_arb.py` | deprecated | runtime crypto diagnostics | 2026-10-31 |

## Fallback Policy
- Nenhum comando legado sera removido sem wrapper equivalente ativo.
- Todos os wrappers devem emitir warning unico e claro de deprecacao.
- Remocoes so acontecem apos a data-alvo e validacao no `scripts/quality_gate.py`.
