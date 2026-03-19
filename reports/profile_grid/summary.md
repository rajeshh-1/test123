# Profile Grid Summary

- total_profiles: 30

## Top 5 perfis recomendados (robustez)
- baseline: score=0.8764 pnl_per_trade=0.5842 timeout_rate=0.0083 hedge_failed_rate=0.0033 dd=1.03%
- latency_high_low_deg_01: score=0.8594 pnl_per_trade=0.5706 timeout_rate=0.0083 hedge_failed_rate=0.0183 dd=1.07%
- latency_high_low_deg_03: score=0.8005 pnl_per_trade=0.5032 timeout_rate=0.0683 hedge_failed_rate=0.0117 dd=1.73%
- latency_high_low_deg_02: score=0.7722 pnl_per_trade=0.4459 timeout_rate=0.0733 hedge_failed_rate=0.0183 dd=1.04%
- latency_high_low_deg_04: score=0.7038 pnl_per_trade=0.3520 timeout_rate=0.0700 hedge_failed_rate=0.0183 dd=2.08%

## Top 5 perfis perigosos
- book_bad_latency_mid_05: score=0.3428 pnl_per_trade=-0.0233 timeout_rate=0.0233 hedge_failed_rate=0.0167 dd=13.97%
- stress_mix_05: score=0.3474 pnl_per_trade=-0.0197 timeout_rate=0.0467 hedge_failed_rate=0.0183 dd=13.30%
- stress_mix_04: score=0.3626 pnl_per_trade=-0.0158 timeout_rate=0.0200 hedge_failed_rate=0.0167 dd=12.64%
- crash_tail_02: score=0.3698 pnl_per_trade=-0.0194 timeout_rate=0.0333 hedge_failed_rate=0.0150 dd=11.66%
- crash_tail_01: score=0.3721 pnl_per_trade=-0.0192 timeout_rate=0.0350 hedge_failed_rate=0.0083 dd=11.52%

## Zona segura sugerida para parametros live
- latency_ms: 200..3000
- adverse_drift_bps: 2.00..6.00
- book_haircut_pct: 10.00..10.00
- partial_fill_prob: 0.05..0.15
- timeout_prob: 0.01..0.05
