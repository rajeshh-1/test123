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


def main():
    parser = argparse.ArgumentParser(
        description="Monitora preço do mercado BTC up/down em tempo real (5m ou 15m)."
    )
    parser.add_argument("--coin", default="btc", choices=["btc", "eth", "sol", "xrp"])
    parser.add_argument("--timeframe", default="5m", choices=["5m", "15m"])
    parser.add_argument("--interval", type=float, default=0.2, help="Intervalo de polling em segundos.")
    parser.add_argument("--max-seconds", type=int, default=0, help="0 = infinito.")
    parser.add_argument("--csv-file", default="logs/btc_5m_prices.csv", help="Arquivo CSV para salvar os ticks.")
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

    started = time.time()
    current_slug = ""
    market_info = None
    csv_path = args.csv_file
    csv_fields = [
        "timestamp_utc",
        "slug",
        "question",
        "condition_id",
        "up",
        "down",
        "sum",
    ]
    if csv_path:
        csv_dir = os.path.dirname(csv_path)
        if csv_dir:
            os.makedirs(csv_dir, exist_ok=True)
        if not os.path.isfile(csv_path):
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
                writer.writeheader()

    print(
        f"[WATCH] coin={args.coin} timeframe={args.timeframe} interval={args.interval}s "
        f"max_seconds={args.max_seconds} csv_file={csv_path}"
    )

    while True:
        now = time.time()
        if args.max_seconds > 0 and (now - started) >= args.max_seconds:
            print("[WATCH] finalizado por max_seconds.")
            return

        slug = build_current_slug(args.coin, args.timeframe)
        if slug != current_slug or market_info is None:
            market_info = fetch_market_info(session, slug)
            current_slug = slug
            if market_info:
                print(f"[MARKET] {market_info['slug']} | {market_info['question']}")
            else:
                print(f"[MARKET] slug={slug} sem mercado encontrado.")

        if not market_info:
            time.sleep(max(0.05, args.interval))
            continue

        try:
            prices = {}
            for i, token_id in enumerate(market_info["token_ids"][:2]):
                outcome = market_info["outcomes"][i]
                prices[outcome] = fetch_midpoint(session, token_id)
            ts = datetime.now(timezone.utc).isoformat()
            up = prices.get("Up", prices.get("Yes", 0.0))
            down = prices.get("Down", prices.get("No", 0.0))
            print(f"{ts} | up={up:.4f} down={down:.4f} sum={up+down:.4f}")
            if csv_path:
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
                    writer.writerow(
                        {
                            "timestamp_utc": ts,
                            "slug": market_info.get("slug", ""),
                            "question": market_info.get("question", ""),
                            "condition_id": market_info.get("condition_id", ""),
                            "up": round(up, 8),
                            "down": round(down, 8),
                            "sum": round(up + down, 8),
                        }
                    )
        except Exception as e:
            print(f"{datetime.now(timezone.utc).isoformat()} | erro={e}")

        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    main()
