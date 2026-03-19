# Legacy Status (Phase 5.1)

## Legenda
- `keep (wrapper)`: manter por compatibilidade; aponta para entrypoint oficial.
- `deprecated`: ainda existe, mas nao e caminho recomendado; possui plano de remocao.
- `remove now`: sem uso operacional suportado; candidato a remocao imediata.

## Inventory
| Item | Tipo | Status | Replacement/Owner | Target removal date | Notes |
|---|---|---|---|---|---|
| `scripts/crypto_cli.py` | CLI | official | - | - | entrypoint oficial crypto |
| `scripts/sports_cli.py` | CLI | official | - | - | entrypoint oficial sports |
| `scripts/quality_gate.py` | CLI | official | - | - | gate oficial local |
| `scripts/arb_cli.py` | CLI | keep (wrapper) | `scripts/crypto_cli.py` | 2026-09-30 | compatibilidade de comando legado |
| `logs/run_arb_dry_run.py` | runner | keep (wrapper) | `scripts/crypto_cli.py` + runtime crypto | 2026-10-31 | ainda usado por `run_arb_bot.bat` |
| `run_arb_bot.bat` | launcher | keep (wrapper) | official CLIs | 2026-12-31 | menu legado com fluxos novos |
| `start_btc_15m_monitors.bat` | launcher | deprecated | `scripts/crypto_cli.py` + runtime | 2026-09-30 | atalho legado de monitores |
| `watch_btc_15m_kalshi.py` | feed script | deprecated | runtime crypto feed | 2026-10-31 | mantido por troubleshooting |
| `watch_btc_15m_poly.py` | feed script | deprecated | runtime crypto feed | 2026-10-31 | mantido por troubleshooting |
| `logs/analyze_arb.py` | analyzer | deprecated | runtime crypto + diagnostics | 2026-10-31 | uso principal migrou para runtime/dry-run |
| `logs/arb_engine/*.py` | wrappers | keep (wrapper) | `bot/core/*` | 2026-09-30 | compat import legado |
| `logs/kalshi_order_client.py` | wrapper | keep (wrapper) | `bot/core/execution/kalshi_client.py` | 2026-09-30 | compat import legado |
| `Bot_Principal/*.py` | legacy sports | remove now | `bot/sports/*` + `scripts/sports_cli.py` | 2026-08-31 | sem caminho oficial de release |
| `Testes_e_Logs/*.py` | debug/adhoc | remove now | tests oficiais em `tests/` | 2026-08-31 | fora do fluxo suportado |
| `live_executor.py` | legacy executor | deprecated | runtime crypto | 2026-09-30 | nao oficial para RC |
| `live_multi_test.py` | adhoc | remove now | `tests/*` | 2026-08-31 | teste manual nao versionado |
| `market_hunter.py` | legacy strategy | deprecated | dominio sports oficial | 2026-10-31 | fora do escopo RC |
| `mm_bot.py` / `mm_bot_cursor.py` | legacy strategy | deprecated | dominio sports oficial | 2026-10-31 | fora do escopo RC |
| `crypto_5m_simulator.py` / `crypto_15m_simulator.py` | simulator | deprecated | runtime dry-run + tests | 2026-10-31 | manter ate consolidacao final |
| `watch_all_updown_prices.py` / `watch_future_updown_markets.py` | sports feeds | deprecated | `bot/sports/*` runtime | 2026-10-31 | migracao de dominio em andamento |
