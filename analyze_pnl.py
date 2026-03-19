import argparse
import datetime as dt
import sys
from typing import Dict, Optional

import pandas as pd
import requests


def fetch_winner_idx(slug: str, timeout: int = 6) -> Optional[int]:
    """Return winning outcome index (0/1) using Gamma outcomePrices; None if unknown."""
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=timeout)
        resp.raise_for_status()
        evs = resp.json()
    except Exception:
        return None
    if not evs:
        return None
    market = evs[0].get("markets", [{}])[0]
    prices = market.get("outcomePrices", [])
    if prices and len(prices) == 2:
        try:
            p0 = float(prices[0])
            p1 = float(prices[1])
            return 0 if p0 > p1 else 1
        except Exception:
            return None
    winner = market.get("winner")
    outcomes = market.get("outcomes", [])
    if winner is not None and outcomes and winner in outcomes:
        return outcomes.index(winner)
    return None


def analyze(trades_path: str, target_date: dt.date):
    df = pd.read_csv(trades_path)
    if "timestamp" not in df.columns:
        print("timestamp column not found", file=sys.stderr)
        sys.exit(1)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    day_df = df[df["ts"].dt.date == target_date]
    if day_df.empty:
        print(f"Nenhuma trade em {target_date}")
        return

    slugs = day_df["slug"].unique().tolist()
    winners: Dict[str, Optional[int]] = {}
    for slug in slugs:
        winners[slug] = fetch_winner_idx(slug)

    pnl = 0.0
    rows = []
    day_df = day_df.sort_values("ts")
    for _, r in day_df.iterrows():
        side = str(r.get("side", "")).lower()
        if side != "buy":
            continue
        slug = r["slug"]
        win_idx = winners.get(slug)
        price = float(r["price"])
        size = float(r["size"])
        cost = price * size
        if win_idx is None:
            rows.append((r["ts"], slug, r["outcome"], "unknown", -cost))
            pnl -= cost
            continue
        won = int(r.get("outcomeIndex", -1)) == int(win_idx)
        payout = size if won else 0.0
        pnl += payout - cost
        rows.append((r["ts"], slug, r["outcome"], "win" if won else "loss", payout - cost))

    print(f"Trades BUY em {target_date}: {len(rows)}")
    print(f"PnL estimado do dia: {pnl:.4f} USDC")
    # streaks
    streak = max_streak = 0
    for _, _, _, res, _ in rows:
        if res == "loss":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    print(f"Maior sequencia de perdas: {max_streak}")

    # top losses por slug
    agg = {}
    for _, slug, _, res, pnl_row in rows:
        agg.setdefault(slug, 0.0)
        agg[slug] += pnl_row
    worst = sorted(agg.items(), key=lambda x: x[1])[:5]
    print("Piores slugs do dia (pnl):")
    for slug, v in worst:
        print(f"  {slug}: {v:.4f}")


def main():
    parser = argparse.ArgumentParser(description="Analisa PnL diário a partir do CSV de trades exportado.")
    parser.add_argument("--trades", required=True, help="CSV de trades (export_activity.py).")
    parser.add_argument("--date", default=None, help="Data alvo (YYYY-MM-DD). Default: hoje UTC.")
    args = parser.parse_args()
    target_date = dt.date.fromisoformat(args.date) if args.date else dt.datetime.utcnow().date()
    analyze(args.trades, target_date)


if __name__ == "__main__":
    main()
