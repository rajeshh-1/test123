import argparse
import csv
import os
import sys
from datetime import datetime, timezone

import requests


TRADES_URL = "https://data-api.polymarket.com/trades"
POSITIONS_URL = "https://data-api.polymarket.com/positions"


def fetch_trades(user: str, limit: int = 1000) -> list[dict]:
    results = []
    offset = 0
    while True:
        resp = requests.get(
            TRADES_URL,
            params={"user": user, "limit": limit, "offset": offset, "takerOnly": False},
            timeout=15,
        )
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        results.extend(chunk)
        offset += len(chunk)
        if len(chunk) < limit:
            break
    return results


def fetch_positions(user: str) -> list[dict]:
    resp = requests.get(
        POSITIONS_URL,
        params={"user": user, "size": 500},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def write_csv(path: str, rows: list[dict]):
    if not rows:
        open(path, "w", encoding="utf-8").close()
        return
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Exporta trades e posições (redeemable/atuais) da Polymarket para CSV."
    )
    parser.add_argument("--user", required=True, help="Endereço (funder/proxy) a consultar.")
    parser.add_argument("--out-dir", default=".", help="Diretório de saída dos CSVs.")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    try:
        trades = fetch_trades(args.user)
        positions = fetch_positions(args.user)
    except Exception as e:
        print(f"[ERRO] Falha ao consultar API: {e}", file=sys.stderr)
        sys.exit(1)

    trades_path = os.path.join(args.out_dir, f"trades_{ts}.csv")
    positions_path = os.path.join(args.out_dir, f"positions_{ts}.csv")
    write_csv(trades_path, trades)
    write_csv(positions_path, positions)

    print(f"Trades: {len(trades)} -> {trades_path}")
    print(f"Positions: {len(positions)} -> {positions_path}")


if __name__ == "__main__":
    main()
