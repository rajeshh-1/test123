import argparse
import csv
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import median

import requests


LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
ACTIVITY_URL = "https://data-api.polymarket.com/activity"


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def fetch_leaderboard(time_period: str, order_by: str, limit: int) -> list[dict]:
    resp = requests.get(
        LEADERBOARD_URL,
        params={"timePeriod": time_period, "orderBy": order_by, "limit": limit},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def fetch_activity_stats(
    user: str,
    start_ts: int,
    end_ts: int,
    page_size: int,
    max_pages: int,
    early_stop_trades: int,
    only_updown: bool,
):
    offset = 0
    pages = 0
    trade_count = 0
    buy_count = 0
    sell_count = 0
    buy_notional = 0.0
    sell_notional = 0.0
    sizes = []
    slugs = set()
    truncated = False

    while pages < max_pages:
        resp = requests.get(
            ACTIVITY_URL,
            params={
                "user": user,
                "type": "TRADE",
                "start": start_ts,
                "end": end_ts,
                "limit": page_size,
                "offset": offset,
            },
            timeout=20,
        )
        resp.raise_for_status()
        chunk = resp.json()
        if not isinstance(chunk, list) or not chunk:
            break

        for trade in chunk:
            slug = str(trade.get("slug", "")).strip()
            if only_updown and "-updown-" not in slug:
                continue

            trade_count += 1
            side = str(trade.get("side", "")).upper()
            usdc_size = abs(_to_float(trade.get("usdcSize", 0.0), 0.0))
            sizes.append(usdc_size)
            if slug:
                slugs.add(slug)

            if side == "BUY":
                buy_count += 1
                buy_notional += usdc_size
            elif side == "SELL":
                sell_count += 1
                sell_notional += usdc_size

            if early_stop_trades > 0 and trade_count > early_stop_trades:
                truncated = True
                break

        if truncated:
            break

        pages += 1
        offset += len(chunk)
        if len(chunk) < page_size:
            break

    avg_size = (sum(sizes) / len(sizes)) if sizes else 0.0
    med_size = median(sizes) if sizes else 0.0
    max_size = max(sizes) if sizes else 0.0
    sell_ratio = (sell_count / trade_count) if trade_count else 0.0

    return {
        "trade_count": trade_count,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "sell_ratio": sell_ratio,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "avg_usdc_size": avg_size,
        "median_usdc_size": med_size,
        "max_usdc_size": max_size,
        "unique_markets": len(slugs),
        "truncated": truncated,
    }


def analyze_wallet(entry: dict, args, start_ts: int, end_ts: int):
    wallet = str(entry.get("proxyWallet", "")).strip()
    if not wallet:
        return None

    pnl = _to_float(entry.get("pnl", 0.0), 0.0)
    vol = _to_float(entry.get("vol", 0.0), 0.0)
    pnl_per_vol = (pnl / vol) if vol > 0 else 0.0

    if pnl < args.min_pnl:
        return None
    if pnl_per_vol < args.min_pnl_per_vol:
        return None

    stats = fetch_activity_stats(
        user=wallet,
        start_ts=start_ts,
        end_ts=end_ts,
        page_size=args.page_size,
        max_pages=args.max_pages,
        early_stop_trades=args.early_stop_trades,
        only_updown=args.only_updown,
    )

    if stats["trade_count"] < args.min_trades:
        return None
    if stats["trade_count"] > args.max_trades:
        return None
    if stats["sell_ratio"] < args.min_sell_ratio:
        return None
    if stats["median_usdc_size"] > args.max_median_usdc_size:
        return None

    score = (pnl_per_vol * 100.0) + math.log1p(max(pnl, 0.0)) + (stats["sell_ratio"] * 10.0)
    return {
        "score": round(score, 6),
        "rank": entry.get("rank", ""),
        "wallet": wallet,
        "userName": entry.get("userName", ""),
        "pnl_period": round(pnl, 6),
        "vol_period": round(vol, 6),
        "pnl_per_vol": round(pnl_per_vol, 6),
        "trade_count": stats["trade_count"],
        "buy_count": stats["buy_count"],
        "sell_count": stats["sell_count"],
        "sell_ratio": round(stats["sell_ratio"], 6),
        "unique_markets": stats["unique_markets"],
        "avg_usdc_size": round(stats["avg_usdc_size"], 6),
        "median_usdc_size": round(stats["median_usdc_size"], 6),
        "max_usdc_size": round(stats["max_usdc_size"], 6),
        "truncated": stats["truncated"],
        "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Encontra wallets candidatas para copytrade (poucas trades, boas e com SELL)."
    )
    parser.add_argument("--time-period", default="MONTH", choices=["DAY", "WEEK", "MONTH", "ALL"])
    parser.add_argument("--order-by", default="PNL", choices=["PNL", "VOL"])
    parser.add_argument("--leaderboard-limit", type=int, default=200)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--min-pnl", type=float, default=100.0)
    parser.add_argument("--min-pnl-per-vol", type=float, default=0.03)
    parser.add_argument("--min-trades", type=int, default=8)
    parser.add_argument("--max-trades", type=int, default=120)
    parser.add_argument("--min-sell-ratio", type=float, default=0.10)
    parser.add_argument("--max-median-usdc-size", type=float, default=500.0)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--early-stop-trades", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--only-updown",
        action="store_true",
        default=True,
        help="Considera apenas trades com slug contendo '-updown-'.",
    )
    parser.add_argument(
        "--include-all-slugs",
        action="store_true",
        help="Desativa filtro only-updown e considera todos os mercados.",
    )
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--out", default="logs/copy_wallet_candidates.csv")
    args = parser.parse_args()
    if args.include_all_slugs:
        args.only_updown = False

    now_ts = int(time.time())
    start_ts = now_ts - int(args.lookback_days * 86400)

    leaderboard = fetch_leaderboard(args.time_period, args.order_by, args.leaderboard_limit)
    rows = []
    prefiltered = []
    for entry in leaderboard:
        pnl = _to_float(entry.get("pnl", 0.0), 0.0)
        vol = _to_float(entry.get("vol", 0.0), 0.0)
        ratio = (pnl / vol) if vol > 0 else 0.0
        if pnl >= args.min_pnl and ratio >= args.min_pnl_per_vol:
            prefiltered.append(entry)

    print(
        f"[SCAN] leaderboard={len(leaderboard)} prefiltered={len(prefiltered)} "
        f"lookback_days={args.lookback_days} workers={args.workers}"
    )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(analyze_wallet, entry, args, start_ts, now_ts): entry
            for entry in prefiltered
        }
        for fut in as_completed(futures):
            entry = futures[fut]
            wallet = str(entry.get("proxyWallet", "")).strip()
            try:
                row = fut.result()
            except Exception as e:
                print(f"[WARN] wallet={wallet} erro={e}")
                continue
            if not row:
                continue
            rows.append(row)
            print(
                f"[OK] {row['wallet']} pnl={row['pnl_period']:.2f} pnl/vol={row['pnl_per_vol']:.4f} "
                f"trades={row['trade_count']} sell_ratio={row['sell_ratio']:.2%}"
            )

    rows.sort(key=lambda r: (r["score"], r["pnl_period"]), reverse=True)
    top_rows = rows[: args.top]

    fieldnames = [
        "score",
        "rank",
        "wallet",
        "userName",
        "pnl_period",
        "vol_period",
        "pnl_per_vol",
        "trade_count",
        "buy_count",
        "sell_count",
        "sell_ratio",
        "unique_markets",
        "avg_usdc_size",
        "median_usdc_size",
        "max_usdc_size",
        "truncated",
        "only_updown",
        "scanned_at_utc",
    ]
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in top_rows:
            row["only_updown"] = args.only_updown
            writer.writerow(row)

    print(f"[DONE] candidatos={len(top_rows)} arquivo={args.out}")


if __name__ == "__main__":
    main()
