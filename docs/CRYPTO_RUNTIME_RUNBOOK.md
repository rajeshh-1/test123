# Crypto Runtime Runbook

## Visao Geral do Fluxo
1. Validar configuracao e risco com `scripts/crypto_cli.py`.
2. Rodar em paper (`live-shadow`) via `logs/run_arb_dry_run.py`.
3. Revisar saidas em CSV, JSONL e SQLite.
4. So considerar live apos checklist pre-live aprovado.

## Comandos Paper
Validacao de config:
```bash
python scripts/crypto_cli.py --execution-mode paper --min-edge-pct 5 --min-liquidity 1
```

Runtime paper (shadow):
```bash
python logs/run_arb_dry_run.py --mode live-shadow --execution-mode paper --runtime-sec 120
```

Replay historico:
```bash
python logs/run_arb_dry_run.py --mode replay --execution-mode paper
```

## Comandos Live
Validacao de config live:
```bash
python scripts/crypto_cli.py --execution-mode live --enable-live-prod --live-confirmation I_UNDERSTAND_LIVE_ARB
```

Runtime live (controlado):
```bash
python logs/run_arb_dry_run.py --mode live-prod --execution-mode live --enable-live-prod --live-confirmation I_UNDERSTAND_LIVE_ARB --kalshi-order-live true --allow-single-leg-risk true
```

## Variaveis de Ambiente Obrigatorias
Obrigatorias para live:
- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY_PATH`
- `POLY_PRIVATE_KEY`

Recomendadas:
- `POLY_RPC_URL` (nonce guard)
- `ARB_KILL_SWITCH_PATH` (ex.: `logs/kill_switch.flag`)
- `ARB_KILL_SWITCH` (`true/false`)

## Validacao Pre-Start
1. `python scripts/crypto_cli.py ...` deve retornar `config_validated=true`.
2. `python scripts/quality_gate.py check` deve estar verde.
3. Confirmar kill switch desativado:
```bash
if exist logs\kill_switch.flag echo KILL_SWITCH_ATIVO
```
4. Confirmar dry run recente sem excecoes nao tratadas.

## Leitura de Logs (JSONL / SQLite)
JSONL:
```bash
Get-Content logs\arb_events.jsonl -Tail 40
```

SQLite (contagens):
```bash
python -c "import sqlite3; c=sqlite3.connect('logs/arb_runtime.sqlite'); print('orders',c.execute('select count(*) from orders').fetchone()[0]); print('fills',c.execute('select count(*) from fills').fetchone()[0]); print('skips',c.execute('select count(*) from skips').fetchone()[0]); c.close()"
```

## Stop Seguro
1. Acionar kill switch:
```bash
type nul > logs\kill_switch.flag
```
2. Aguardar o runtime parar de abrir novas entradas e fechar ciclo atual.
3. Encerrar processo (`Ctrl+C`).
4. Coletar resumo final e logs.
5. Para retomar, remover flag:
```bash
del logs\kill_switch.flag
```

## Troubleshooting Rapido
- Sintoma: `circuit_breaker_triggered`.
  - Acao: verificar `losses_streak`, `drawdown` e `max_open_positions` no summary e no JSONL.
- Sintoma: `kill_switch_active`.
  - Acao: checar arquivo `logs/kill_switch.flag` e env `ARB_KILL_SWITCH`.
- Sintoma: `partial_fill`.
  - Acao: revisar `hedge_attempt` no JSONL e reduzir risco por trade.
- Sintoma: `leg_timeout`.
  - Acao: aumentar `--leg-timeout-sec` apenas se latencia observada justificar.
- Sintoma: `invalid_market_mismatch`.
  - Acao: revisar alinhamento de `market_key` entre feeds antes de retomar.
