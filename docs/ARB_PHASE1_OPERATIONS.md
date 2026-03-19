# ARB BTC Kalshi x Polymarket - Operacao Fase 1

## 1) Configuracao
- Copie `.env.arb.example` para `.env`.
- Preencha ao menos:
  - `KALSHI_API_KEY_ID`
  - `KALSHI_PRIVATE_KEY_PATH`
  - `POLY_PRIVATE_KEY` (obrigatorio se `--execution-mode live`)

## 2) Modos de execucao
- `paper` (default): nao envia ordens reais.
- `live`: exige:
  - `--enable-live-prod`
  - `--live-confirmation I_UNDERSTAND_LIVE_ARB`

## 3) Comandos principais
- Replay:
```bat
python logs\run_arb_dry_run.py --mode replay --execution-mode paper
```

- Live-Shadow (feed direto, sem ordem real):
```bat
python logs\run_arb_dry_run.py --mode live-shadow --execution-mode paper --runtime-sec 120
```

- Live-Prod (ainda com travas):
```bat
python logs\run_arb_dry_run.py --mode live-prod --execution-mode live --enable-live-prod --live-confirmation I_UNDERSTAND_LIVE_ARB
```

## 4) Parametros de risco Fase 1
- `--min-edge-pct`: limiar minimo de edge liquido (%).
- `--min-liquidity`: liquidez minima (shares) em ambos os lados.
- `--slippage-expected-bps`: slippage esperado em bps.
- `--leg-risk-cost`: custo absoluto por share para risco de perna.
- `--payout-esperado`: payout alvo (default `1.0`).

## 5) Persistencia e auditoria
- SQLite: `logs/arb_runtime.sqlite`
  - tabelas: `orders`, `fills`, `pnl`, `skips`
- JSONL: `logs/arb_events.jsonl`
  - eventos estruturados de decisao e ciclo runtime

## 6) Runbook de incidentes (fase 1)
- `missing_kalshi_credentials`:
  - validar `.env` e path da chave PEM.
- `semantic_mismatch` / `invalid_market_mismatch`:
  - interromper trading da janela e revisar mapeamento de `market_key`.
- `insufficient_liquidity`:
  - reduzir `max_shares_per_trade` ou aguardar melhora de book.
- `negative_edge` / `below_min_edge`:
  - revisar fees/slippage/leg risk, nao forcar execucao.
- `guard_degraded`:
  - verificar `POLY_RPC_URL`, manter em paper ate restaurar.

