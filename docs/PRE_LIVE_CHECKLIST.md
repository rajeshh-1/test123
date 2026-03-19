# Pre-Live Checklist

Use este checklist antes de habilitar qualquer execucao live.

## A) Credenciais e Seguranca
- [ ] Credenciais validas para venue (sem hardcode no codigo).
- [ ] `.env` carregado e sem segredos em logs/prints.
- [ ] Arquivos de chave com permissao restrita no host.

## B) Validacao de Ambiente
- [ ] `python scripts/crypto_cli.py --execution-mode live --enable-live-prod --live-confirmation I_UNDERSTAND_LIVE_ARB` retorna `config_validated=true`.
- [ ] `python scripts/sports_cli.py --execution-mode live --enable-live-prod --live-confirmation I_UNDERSTAND_LIVE_ARB` retorna `config_validated=true` (quando aplicavel).
- [ ] Quality gate verde (`python scripts/quality_gate.py check`).

## C) Dry Run e Qualidade
- [ ] Dry run recente aprovado (`replay` e `live-shadow` sem excecoes nao tratadas).
- [ ] Logs de rejeicao e incidentes sendo persistidos em JSONL e SQLite.
- [ ] Cobertura de testes no minimo do gate definido.

## D) Risco e Controles
- [ ] Kill switch testado (ativar/desativar e verificar bloqueio de entrada).
- [ ] Circuit breaker testado (streak, drawdown, max open positions).
- [ ] Limites de risco revisados para o dia (edge, liquidez, timeout, tamanho).

## E) Operacao e Monitoramento
- [ ] Monitoramento ativo para feeds, timeouts e reason_codes.
- [ ] Alerta para `partial_fill`, `hedge_failed`, `circuit_breaker_triggered`, `kill_switch_active`.
- [ ] Responsavel de plantao definido para resposta a incidente.

## F) Rollback
- [ ] Plano de rollback definido e validado.
- [ ] Procedimento de stop seguro conhecido por operador:
  - criar `logs/kill_switch.flag`
  - aguardar bloqueio de novas entradas
  - encerrar processo e coletar evidencias
