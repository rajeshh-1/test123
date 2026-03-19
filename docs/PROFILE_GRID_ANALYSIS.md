# Profile Grid Analysis (Crypto Up/Down)

## Objetivo
Executar stress-test paper-only do runtime crypto com 30 perfis pessimistas e comparar robustez operacional antes de ajustar parametros para live.

## Componentes
- `bot/crypto_updown/runtime/execution_profile.py`
  - `ExecutionProfile` com validacoes de range.
  - gerador deterministico `generate_execution_profiles_30()`.
  - utilitarios `load_profiles_json`, `save_profiles_json`, `normalize_metric`.
  - calculo de score `compute_robustness_score`.
- `scripts/generate_profiles.py`
  - gera `configs/execution_profiles_30.json`.
- `scripts/run_profile_grid.py`
  - executa os 30 perfis em batch (paper/simulado).
  - gera `profile_results.csv`, `profile_results.json`, `summary.md`.

## Formula de Robustez
Score implementado:

```text
robustness_score =
  (0.30 * normalized_pnl_per_trade) +
  (0.20 * edge_capture_ratio) +
  (0.20 * (1 - timeout_rate)) +
  (0.15 * (1 - hedge_failed_rate)) +
  (0.15 * (1 - max_drawdown_pct_norm))
```

Detalhes:
- `normalized_pnl_per_trade`: normalizacao min-max entre os 30 perfis.
- `max_drawdown_pct_norm`: normalizacao min-max de drawdown.
- taxas devem estar em `[0,1]`.
- score final e clampado em `[0,1]`.

## Metricas por Perfil
- `trades_attempted`
- `trades_accepted`
- `fill_full_rate`
- `partial_fill_rate`
- `timeout_rate`
- `hedge_failed_rate`
- `avg_edge_predicted_pct`
- `avg_edge_captured_pct`
- `edge_capture_ratio`
- `pnl_total`
- `pnl_per_trade`
- `max_drawdown_pct`
- `breaker_trigger_count`
- `skip_rate`
- `robustness_score`

## Comandos
Gerar perfis:

```bash
python scripts/generate_profiles.py --out-file configs/execution_profiles_30.json
```

Rodar grid:

```bash
python scripts/run_profile_grid.py --profiles-file configs/execution_profiles_30.json --runtime-sec 600 --seed 42 --out-dir reports/profile_grid
```

## Saidas
- `reports/profile_grid/profile_results.csv`
- `reports/profile_grid/profile_results.json`
- `reports/profile_grid/summary.md`

## Leitura Rapida
- Priorizar perfis com:
  - `robustness_score` alto,
  - `edge_capture_ratio` alto,
  - `timeout_rate` e `hedge_failed_rate` baixos,
  - `max_drawdown_pct` baixo.
- Evitar calibrar live por ranking de `pnl_total` isolado.
- A zona sugerida em `summary.md` deve ser tratada como ponto de partida para shadow run, nao como liberacao automatica de live.
