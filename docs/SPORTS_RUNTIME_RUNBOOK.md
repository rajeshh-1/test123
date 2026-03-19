# Sports Runtime Runbook

## Visao Geral do Fluxo
1. Validar configuracao de dominio sports com `scripts/sports_cli.py`.
2. Executar monitoramento/analise em paper.
3. Revisar logs e sinais antes de qualquer operacao live.

Observacao: nesta fase, sports tem CLI e contratos de dominio; execucao live dedicada ainda depende da fase seguinte de consolidacao.

## Comandos Paper
Validacao de config sports:
```bash
python scripts/sports_cli.py --execution-mode paper --market-scope moneyline
```

Monitoramento legado (paper/simulacao):
```bash
python watch_future_updown_markets.py
python watch_all_updown_prices.py
```

## Comandos Live
Validacao de gate live (sem ordens reais):
```bash
python scripts/sports_cli.py --execution-mode live --enable-live-prod --live-confirmation I_UNDERSTAND_LIVE_ARB --market-scope moneyline
```

## Variaveis de Ambiente Obrigatorias
Para validacao de sports nesta fase:
- sem credencial obrigatoria adicional no CLI.

Se usar scripts legados de venues:
- manter chaves no `.env` (sem hardcode).
- validar que o script legado nao escreve segredos em log.

## Validacao Pre-Start
1. `python scripts/sports_cli.py ...` retorna `config_validated=true`.
2. `python scripts/quality_gate.py check` verde.
3. Confirmar escopo correto (`moneyline`, `spread`, etc.).
4. Confirmar timezone/event date padronizados em UTC na analise.

## Leitura de Logs (JSONL / SQLite)
Se rodando somente CLI de validacao, nao ha escrita operacional.

Se rodando scripts legados:
- conferir saidas CSV/TXT geradas no projeto.
- padronizar auditoria manual em arquivos de `logs/` antes de cada decisao.

## Stop Seguro
1. Encerrar processo com `Ctrl+C`.
2. Confirmar ausencia de processos python remanescentes.
3. Registrar timestamp de parada e estado de arquivos de log.

## Troubleshooting Rapido
- Sintoma: `execution_mode` invalido.
  - Acao: usar apenas `paper` ou `live`.
- Sintoma: `live mode requires --enable-live-prod`.
  - Acao: incluir flag explicitamente somente em janela autorizada.
- Sintoma: `live mode requires --live-confirmation ...`.
  - Acao: confirmar frase completa sem variacao.
- Sintoma: mismatch de evento/time.
  - Acao: revisar matching por evento/time/date/scope antes de retomar.
