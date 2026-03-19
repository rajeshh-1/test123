import argparse
import csv
import os
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
COINS = ("btc", "eth", "sol", "xrp")
TIMEFRAMES = ("5m", "15m")


def build_current_slug(coin: str, timeframe: str) -> str:
    now = datetime.now(timezone.utc)
    bucket = 5 if timeframe == "5m" else 15
    block_start = now.replace(minute=(now.minute // bucket) * bucket, second=0, microsecond=0)
    return f"{coin}-updown-{timeframe}-{int(block_start.timestamp())}"


def parse_json_field(value):
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except Exception:
            return []
    return value


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return float(default)


def fetch_market_info(session: requests.Session, slug: str):
    r = session.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=10)
    r.raise_for_status()
    events = r.json()
    if not events:
        return None
    event = events[0]
    market = None
    for m in event.get("markets", []):
        if "up or down" in str(m.get("question", "")).lower():
            market = m
            break
    if not market:
        return None
    outcomes = parse_json_field(market.get("outcomes", [])) or []
    token_ids = parse_json_field(market.get("clobTokenIds", [])) or []
    if len(outcomes) != len(token_ids) or len(outcomes) < 2:
        return None
    return {
        "slug": slug,
        "question": str(market.get("question", "")),
        "condition_id": str(market.get("conditionId", "")),
        "outcomes": [str(x) for x in outcomes],
        "token_ids": [str(x) for x in token_ids],
    }


def fetch_midpoint(session: requests.Session, token_id: str) -> float:
    r = session.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=6)
    r.raise_for_status()
    data = r.json()
    return safe_float(data.get("mid", 0.0), 0.0)


def ensure_csv(path: str, fields):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    if not os.path.isfile(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()


def append_row(path: str, fields, row):
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(
        description="Monitora BTC/ETH/SOL/XRP em 5m e 15m e salva CSVs separados."
    )
    parser.add_argument("--interval", type=float, default=1.0, help="Intervalo por mercado, em segundos.")
    parser.add_argument("--max-seconds", type=int, default=0, help="0 = infinito.")
    parser.add_argument("--out-dir", default="logs", help="Pasta de saída dos CSVs.")
    args = parser.parse_args()

    session = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    fields = [
        "timestamp_utc",
        "coin",
        "timeframe",
        "slug",
        "question",
        "condition_id",
        "up",
        "down",
        "sum",
    ]

    watchers = []
    now = time.time()
    for coin in COINS:
        for timeframe in TIMEFRAMES:
            csv_path = os.path.join(args.out_dir, f"{coin}_{timeframe}_prices.csv")
            ensure_csv(csv_path, fields)
            watchers.append(
                {
                    "coin": coin,
                    "timeframe": timeframe,
                    "csv_path": csv_path,
                    "current_slug": "",
                    "market_info": None,
                    "next_poll": now,
                }
            )

    print(
        f"[WATCH-ALL] mercados={len(watchers)} interval={args.interval}s "
        f"max_seconds={args.max_seconds} out_dir={args.out_dir}"
    )
    for w in watchers:
        print(f"[CSV] {w['coin']} {w['timeframe']} -> {w['csv_path']}")

    started = time.time()
    while True:
        now = time.time()
        if args.max_seconds > 0 and (now - started) >= args.max_seconds:
            print("[WATCH-ALL] finalizado por max_seconds.")
            return

        for w in watchers:
            if now < w["next_poll"]:
                continue

            coin = w["coin"]
            timeframe = w["timeframe"]
            slug = build_current_slug(coin, timeframe)
            try:
                if slug != w["current_slug"] or w["market_info"] is None:
                    w["market_info"] = fetch_market_info(session, slug)
                    w["current_slug"] = slug
                    if w["market_info"]:
                        print(f"[MARKET] {coin}/{timeframe} {slug} OK")
                    else:
                        print(f"[MARKET] {coin}/{timeframe} {slug} sem mercado")

                if w["market_info"]:
                    prices = {}
                    for i, token_id in enumerate(w["market_info"]["token_ids"][:2]):
                        outcome = w["market_info"]["outcomes"][i]
                        prices[outcome] = fetch_midpoint(session, token_id)
                    ts = datetime.now(timezone.utc).isoformat()
                    up = prices.get("Up", prices.get("Yes", 0.0))
                    down = prices.get("Down", prices.get("No", 0.0))
                    total = up + down
                    print(f"{ts} | {coin}/{timeframe} up={up:.4f} down={down:.4f} sum={total:.4f}")
                    append_row(
                        w["csv_path"],
                        fields,
                        {
                            "timestamp_utc": ts,
                            "coin": coin,
                            "timeframe": timeframe,
                            "slug": w["market_info"].get("slug", ""),
                            "question": w["market_info"].get("question", ""),
                            "condition_id": w["market_info"].get("condition_id", ""),
                            "up": round(up, 8),
                            "down": round(down, 8),
                            "sum": round(total, 8),
                        },
                    )
            except Exception as e:
                print(
                    f"{datetime.now(timezone.utc).isoformat()} | {coin}/{timeframe} erro={e}"
                )

            w["next_poll"] = now + max(0.05, args.interval)

        time.sleep(0.02)


if __name__ == "__main__":
    main()
