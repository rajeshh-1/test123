## Execucao em massa (4 mercados + claim)

Abrir 5 terminais no diretorio do projeto.

### Terminal 1 - BTC (loop)
```bash
python run_market_loop.py --coin btc --timeframe 5m --extra-args "--wait-entry-window --entry-seconds 45 --min-entry-seconds 10 --usd 1.0 --order-type FOK"
```

### Terminal 2 - ETH (loop)
```bash
python run_market_loop.py --coin eth --timeframe 5m --extra-args "--wait-entry-window --entry-seconds 45 --min-entry-seconds 10 --usd 1.0 --order-type FOK"
```

### Terminal 3 - SOL (loop)
```bash
python run_market_loop.py --coin sol --timeframe 5m --extra-args "--wait-entry-window --entry-seconds 45 --min-entry-seconds 10 --usd 1.0 --order-type FOK"
```

### Terminal 4 - XRP (loop)
```bash
python run_market_loop.py --coin xrp --timeframe 5m --extra-args "--wait-entry-window --entry-seconds 45 --min-entry-seconds 10 --usd 1.0 --order-type FOK"
```

### Terminal 5 - Claim (loop paralelo)
```bash
python run_claim_loop.py --env-file .env.claim --interval-seconds 180 --max-claims 10
```

## Observacoes
- `run_market_loop.py` usa `LIVE_CONFIRM` do `.env`.
- `run_claim_loop.py` chama `claim_relayer.mjs` (SAFE/PROXY auto-detect).
- Para tentar claimar tudo (inclusive posicoes sem payout), use `--include-zero` no claim loop.
