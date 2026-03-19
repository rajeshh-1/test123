import argparse
import csv
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


GAMMA_HOST = "https://gamma-api.polymarket.com"
COINS = ("btc", "eth", "sol", "xrp")
TIMEFRAMES = ("5m", "15m")


def parse_json_field(value):
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except Exception:
            return []
    return value


def floor_to_bucket(dt_utc: datetime, bucket_minutes: int) -> datetime:
    minute = (dt_utc.minute // bucket_minutes) * bucket_minutes
    return dt_utc.replace(minute=minute, second=0, microsecond=0)


def build_future_slug(coin: str, timeframe: str, lookahead_blocks: int):
    now = datetime.now(timezone.utc)
    bucket = 5 if timeframe == "5m" else 15
    current_start = floor_to_bucket(now, bucket)
    future_start = current_start + timedelta(minutes=bucket * lookahead_blocks)
    future_end = future_start + timedelta(minutes=bucket)
    slug = f"{coin}-updown-{timeframe}-{int(future_start.timestamp())}"
    return slug, future_start, future_end


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
    return {
        "slug": slug,
        "question": str(market.get("question", "")),
        "condition_id": str(market.get("conditionId", "")),
        "active": bool(market.get("active", False)),
        "closed": bool(market.get("closed", False)),
        "accepting_orders": bool(market.get("acceptingOrders", False)),
        "end_date_iso": str(market.get("endDateIso", "")),
        "outcomes_count": len(outcomes),
        "token_ids_count": len(token_ids),
    }


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
        description="Monitora mercados futuros (proximo bloco) e salva CSV em logs/mercados_futuros."
    )
    parser.add_argument("--interval", type=float, default=1.0, help="Intervalo por mercado, em segundos.")
    parser.add_argument("--max-seconds", type=int, default=0, help="0 = infinito.")
    parser.add_argument("--lookahead-blocks", type=int, default=1, help="1 = proximo bloco, 2 = bloco seguinte.")
    parser.add_argument("--coins", default="btc,eth,sol,xrp", help="Lista separada por virgula.")
    parser.add_argument("--timeframes", default="5m,15m", help="Lista separada por virgula.")
    parser.add_argument("--out-dir", default="logs/mercados_futuros", help="Pasta dos CSVs.")
    args = parser.parse_args()

    coins = tuple(x.strip().lower() for x in args.coins.split(",") if x.strip())
    timeframes = tuple(x.strip().lower() for x in args.timeframes.split(",") if x.strip())

    invalid_coins = [c for c in coins if c not in COINS]
    invalid_tfs = [t for t in timeframes if t not in TIMEFRAMES]
    if invalid_coins:
        raise ValueError(f"coins invalidas: {invalid_coins}. validas={COINS}")
    if invalid_tfs:
        raise ValueError(f"timeframes invalidos: {invalid_tfs}. validos={TIMEFRAMES}")
    if args.lookahead_blocks < 1:
        raise ValueError("--lookahead-blocks deve ser >= 1")

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
        "lookahead_blocks",
        "future_start_utc",
        "future_end_utc",
        "seconds_to_future_start",
        "slug",
        "market_found",
        "question",
        "condition_id",
        "active",
        "closed",
        "accepting_orders",
        "end_date_iso",
        "outcomes_count",
        "token_ids_count",
    ]

    watchers = []
    now = time.time()
    for coin in coins:
        for timeframe in timeframes:
            csv_path = os.path.join(args.out_dir, f"{coin}_{timeframe}_futuro.csv")
            ensure_csv(csv_path, fields)
            watchers.append(
                {
                    "coin": coin,
                    "timeframe": timeframe,
                    "csv_path": csv_path,
                    "next_poll": now,
                    "last_slug": "",
                }
            )

    print(
        f"[WATCH-FUTURO] mercados={len(watchers)} interval={args.interval}s "
        f"lookahead_blocks={args.lookahead_blocks} out_dir={args.out_dir}"
    )
    for w in watchers:
        print(f"[CSV] {w['coin']} {w['timeframe']} -> {w['csv_path']}")

    started = time.time()
    while True:
        now = time.time()
        if args.max_seconds > 0 and (now - started) >= args.max_seconds:
            print("[WATCH-FUTURO] finalizado por max_seconds.")
            return

        for w in watchers:
            if now < w["next_poll"]:
                continue

            coin = w["coin"]
            timeframe = w["timeframe"]
            slug, future_start, future_end = build_future_slug(coin, timeframe, args.lookahead_blocks)
            ts = datetime.now(timezone.utc)
            tte = (future_start - ts).total_seconds()

            try:
                info = fetch_market_info(session, slug)
                found = info is not None
                if slug != w["last_slug"]:
                    print(
                        f"[FUTURO] {coin}/{timeframe} slug={slug} "
                        f"start={future_start.isoformat()} found={found}"
                    )
                    w["last_slug"] = slug

                append_row(
                    w["csv_path"],
                    fields,
                    {
                        "timestamp_utc": ts.isoformat(),
                        "coin": coin,
                        "timeframe": timeframe,
                        "lookahead_blocks": args.lookahead_blocks,
                        "future_start_utc": future_start.isoformat(),
                        "future_end_utc": future_end.isoformat(),
                        "seconds_to_future_start": round(tte, 3),
                        "slug": slug,
                        "market_found": found,
                        "question": info["question"] if info else "",
                        "condition_id": info["condition_id"] if info else "",
                        "active": info["active"] if info else "",
                        "closed": info["closed"] if info else "",
                        "accepting_orders": info["accepting_orders"] if info else "",
                        "end_date_iso": info["end_date_iso"] if info else "",
                        "outcomes_count": info["outcomes_count"] if info else "",
                        "token_ids_count": info["token_ids_count"] if info else "",
                    },
                )
            except Exception as e:
                print(f"{ts.isoformat()} | {coin}/{timeframe} erro={e}")

            w["next_poll"] = now + max(0.05, args.interval)

        time.sleep(0.02)


if __name__ == "__main__":
    main()
