# Incident Playbooks

## 1) API down / timeout persistente
### Sinais de deteccao
- `leg_timeout` recorrente
- feed sem atualizacao por varios ciclos
- erros HTTP/WS repetidos

### Impacto
- aumento de leg risk
- entradas com dado stale

### Resposta imediata
1. Ativar kill switch (`logs/kill_switch.flag`).
2. Bloquear novas entradas.
3. Registrar janela do incidente (inicio/fim).

### Rollback / containment
- reduzir para paper (`--execution-mode paper`)
- desativar live ate estabilidade do feed/API

### Criterios para retomar
- latencia normalizada por janela minima definida
- zero timeout por periodo de observacao

## 2) Orderbook sem liquidez
### Sinais de deteccao
- `insufficient_liquidity`
- `missing_book_side`

### Impacto
- ordens nao executam ou viram parcial

### Resposta imediata
1. Nao forcar ordem agressiva.
2. Aumentar filtro minimo de liquidez temporariamente.

### Rollback / containment
- manter apenas monitoramento
- revisar horarios/mercados com melhor profundidade

### Criterios para retomar
- profundidade minima sustentada nas duas pernas

## 3) Partial fill recorrente
### Sinais de deteccao
- `partial_fill` em sequencia
- `hedge_attempt` frequente em JSONL

### Impacto
- exposicao direcional involuntaria

### Resposta imediata
1. Ativar kill switch.
2. Reduzir tamanho por trade.
3. Revisar timeout por perna.

### Rollback / containment
- executar somente paper ate estabilizar fill ratio

### Criterios para retomar
- taxa de parcial abaixo do limite operacional

## 4) Hedge failure
### Sinais de deteccao
- `hedge_failed`

### Impacto
- posicao aberta sem cobertura completa

### Resposta imediata
1. Suspender novas entradas imediatamente.
2. Acionar procedimento manual de flatten.
3. Marcar trade para `pending_review`.

### Rollback / containment
- manter runtime em paper
- revisar conectividade e permissoes de venue

### Criterios para retomar
- hedge path validado em teste controlado
- nenhuma falha de hedge no periodo de observacao

## 5) Market mismatch (resolution/rules)
### Sinais de deteccao
- `invalid_market_mismatch`
- `resolution_rule_mismatch`

### Impacto
- arbitragem invalida por semantica diferente

### Resposta imediata
1. Bloquear trade.
2. Revisar `market_key`, `market_close_utc`, regra de resolucao.

### Rollback / containment
- ajustar matcher/filtros antes de retomar

### Criterios para retomar
- matchers aprovando mercados equivalentes de forma consistente

## 6) Circuit breaker acionado
### Sinais de deteccao
- `circuit_breaker_triggered`

### Impacto
- entradas novas bloqueadas (esperado)

### Resposta imediata
1. Nao desabilitar guard sem diagnostico.
2. Identificar qual limite disparou: streak/drawdown/open positions.

### Rollback / containment
- reduzir exposicao e operar em paper

### Criterios para retomar
- metricas de risco retornaram para faixa aceitavel
- causa raiz documentada

## 7) Kill switch acionado indevidamente
### Sinais de deteccao
- `kill_switch_active` sem ordem operacional

### Impacto
- parada de novas entradas

### Resposta imediata
1. Verificar arquivo `logs/kill_switch.flag`.
2. Verificar env `ARB_KILL_SWITCH`.

### Rollback / containment
- remover causa indevida e registrar mudanca

### Criterios para retomar
- dupla checagem humana de estado do kill switch

## 8) Divergencia de PnL entre fontes
### Sinais de deteccao
- diferenca entre CSV, JSONL e SQLite
- totals inconsistentes no summary

### Impacto
- auditoria e decisao de risco comprometidas

### Resposta imediata
1. Congelar novas entradas.
2. Exportar estado atual (orders/fills/pnl/skips).
3. Reconciliar por `trade_id`.

### Rollback / containment
- usar fonte canonica temporaria (SQLite)
- abrir incidente de reconciliacao

### Criterios para retomar
- divergencia zerada ou explicada por ajuste formal
- reconciliacao assinada no registro operacional
