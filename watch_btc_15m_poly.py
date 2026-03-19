"""
watch_btc_15m_poly.py

BTC 15m Polymarket monitor with robust CSV schema handling for arbitrage analysis.
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
TIMEFRAME = "15m"
BUCKET_MIN = 15
SCHEMA_VERSION = "2.0"
DEFAULT_ROLLOVER_GUARD_SEC = 3.0


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.25,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    return s


def current_block_ts() -> int:
    now = datetime.now(timezone.utc)
    minute = (now.minute // BUCKET_MIN) * BUCKET_MIN
    block_start = now.replace(minute=minute, second=0, microsecond=0)
    return int(block_start.timestamp())


def build_slug(block_ts: int) -> str:
    return f"btc-updown-{TIMEFRAME}-{block_ts}"


def safe_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return value or []


def slug_start_ts(slug: str):
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except Exception:
        return None


def market_close_from_slug(slug: str) -> str:
    start_ts = slug_start_ts(slug)
    if start_ts is None:
        return ""
    dt = datetime.fromtimestamp(start_ts, tz=timezone.utc) + timedelta(minutes=15)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_market_key(market_close_utc: str) -> str:
    return f"BTC15M_{market_close_utc}" if market_close_utc else ""


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
        markets = event.get("markets", [])
        market = markets[0] if markets else None
    if not market:
        return None

    outcomes = parse_json_field(market.get("outcomes", []))
    token_ids = parse_json_field(market.get("clobTokenIds", []))
    if len(outcomes) < 2 or len(token_ids) < 2:
        return None

    up_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("up", "yes")), 0)
    down_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("down", "no")), 1)

    market_close_utc = market_close_from_slug(slug)

    return {
        "slug": slug,
        "question": str(market.get("question", "")),
        "token_up": str(token_ids[up_idx]),
        "token_down": str(token_ids[down_idx]),
        "label_up": str(outcomes[up_idx]),
        "label_down": str(outcomes[down_idx]),
        "active": bool(market.get("active", False)),
        "closed": bool(market.get("closed", False)),
        "market_close_utc": market_close_utc,
        "market_key": make_market_key(market_close_utc),
    }


def fetch_midpoint(session: requests.Session, token_id: str):
    r = session.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=6)
    r.raise_for_status()
    return safe_float(r.json().get("mid"))


def fetch_book(session: requests.Session, token_id: str):
    r = session.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=6)
    r.raise_for_status()
    return r.json()


def parse_book(book: dict):
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    bid_prices = sorted(
        [safe_float(b.get("price")) for b in bids if safe_float(b.get("price")) is not None],
        reverse=True,
    )
    ask_prices = sorted(
        [safe_float(a.get("price")) for a in asks if safe_float(a.get("price")) is not None],
    )

    best_bid = bid_prices[0] if bid_prices else None
    best_ask = ask_prices[0] if ask_prices else None
    spread = round(best_ask - best_bid, 4) if (best_bid is not None and best_ask is not None) else None

    bid_liq = sum(safe_float(b.get("size"), 0.0) for b in bids)
    ask_liq = sum(safe_float(a.get("size"), 0.0) for a in asks)

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "bid_liq": round(bid_liq, 2),
        "ask_liq": round(ask_liq, 2),
    }


# ---------------------------------------------------------------------------
# CSV SCHEMA
# ---------------------------------------------------------------------------
CSV_FIELDS = [
    "timestamp_utc",
    "schema_version",
    "row_status",
    "error_code",
    "market_key",
    "market_close_utc",
    "slug",
    "question",
    "up_mid",
    "up_best_bid",
    "up_best_ask",
    "up_spread",
    "up_bid_liq",
    "up_ask_liq",
    "down_mid",
    "down_best_bid",
    "down_best_ask",
    "down_spread",
    "down_bid_liq",
    "down_ask_liq",
    "mid_sum",
    "ask_sum",
]

NUMERIC_FIELDS = [
    "up_mid",
    "up_best_bid",
    "up_best_ask",
    "up_spread",
    "up_bid_liq",
    "up_ask_liq",
    "down_mid",
    "down_best_bid",
    "down_best_ask",
    "down_spread",
    "down_bid_liq",
    "down_ask_liq",
    "mid_sum",
    "ask_sum",
]


def build_legacy_path(path: str) -> str:
    root, ext = os.path.splitext(path)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    legacy = f"{root}.legacy_{ts}{ext or '.csv'}"
    idx = 1
    while os.path.exists(legacy):
        legacy = f"{root}.legacy_{ts}_{idx}{ext or '.csv'}"
        idx += 1
    return legacy


def ensure_csv_schema(path: str, fields: list):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)

    expected = ",".join(fields)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            first_line = f.readline().strip("\r\n")
        if first_line and first_line != expected:
            legacy_path = build_legacy_path(path)
            os.replace(path, legacy_path)
            print(f"[CSV] schema changed; rotating file {path} -> {legacy_path}")

    if (not os.path.isfile(path)) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields, extrasaction="ignore").writeheader()


def append_csv(path: str, fields: list, row: dict):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields, extrasaction="ignore").writerow(row)


# ---------------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------------
def normalize_error_codes(codes: list[str]) -> str:
    seen = set()
    unique = []
    for code in codes:
        if not code:
            continue
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return "|".join(unique)


def validate_row(row: dict, error_codes: list[str]):
    codes = list(error_codes)

    for required in ("timestamp_utc", "market_key", "market_close_utc", "slug"):
        if not str(row.get(required, "")).strip():
            codes.append("type_mismatch")
            break

    for field in NUMERIC_FIELDS:
        value = row.get(field, "")
        if value in ("", None):
            continue
        if safe_float(value) is None:
            codes.append("type_mismatch")
            break

    if row.get("up_best_ask", "") in ("", None) or row.get("down_best_ask", "") in ("", None):
        codes.append("missing_book_side")

    code_txt = normalize_error_codes(codes)
    status = "invalid" if code_txt else "valid"
    return status, code_txt


def fmt(v):
    x = safe_float(v)
    return f"{x:.4f}" if x is not None else "N/A"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Monitor BTC 15m Polymarket to CSV.")
    parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds.")
    parser.add_argument("--max-seconds", type=int, default=0, help="Max runtime in seconds (0 = infinite).")
    parser.add_argument("--csv-file", default="logs/poly_btc_15m_ticks.csv", help="Output CSV file.")
    parser.add_argument("--no-book", action="store_true", help="Disable orderbook fetch (midpoint only).")
    parser.add_argument(
        "--rollover-guard-sec",
        type=float,
        default=DEFAULT_ROLLOVER_GUARD_SEC,
        help="Mark rows invalid for this many seconds after market rollover.",
    )
    args = parser.parse_args()

    session = build_session()
    ensure_csv_schema(args.csv_file, CSV_FIELDS)

    print("=" * 80)
    print(f"  POLYMARKET BTC 15-MIN MONITOR | interval={args.interval}s")
    print(f"  csv={args.csv_file} | book={'OFF' if args.no_book else 'ON'} | schema={SCHEMA_VERSION}")
    print("=" * 80)

    started = time.time()
    current_slug = ""
    market_info = None
    last_discovery = 0.0
    discovery_interval = 20.0
    last_market_switch_ts = 0.0
    last_market_key = ""

    while True:
        now = time.time()
        if args.max_seconds > 0 and (now - started) >= args.max_seconds:
            print("[MONITOR] finished by --max-seconds.")
            break

        block_ts = current_block_ts()
        new_slug = build_slug(block_ts)

        if new_slug != current_slug or (market_info is None and now - last_discovery >= discovery_interval):
            try:
                info = fetch_market_info(session, new_slug)
                if info:
                    market_info = info
                    current_slug = new_slug
                    last_discovery = now
                    if info.get("market_key") and info["market_key"] != last_market_key:
                        last_market_key = info["market_key"]
                        last_market_switch_ts = now
                    print(
                        f"[MARKET] {info['slug']} | {info['question']} | "
                        f"UP={info['label_up']} DOWN={info['label_down']} | "
                        f"active={info['active']} closed={info['closed']}"
                    )
                else:
                    if new_slug != current_slug:
                        print(f"[MARKET] slug={new_slug} not available yet.")
                    current_slug = new_slug
                    last_discovery = now
            except Exception as e:
                print(f"[DISCOVERY] error for slug={new_slug}: {e}")
                time.sleep(2)
                continue

        if not market_info:
            print(f"[WAIT] waiting market for slug={new_slug}...")
            time.sleep(max(args.interval, 3.0))
            last_discovery = 0.0
            continue

        ts_iso = datetime.now(timezone.utc).isoformat()
        error_codes = []

        if (now - last_market_switch_ts) <= max(0.0, args.rollover_guard_sec):
            error_codes.append("market_rollover_race")

        try:
            token_up = market_info["token_up"]
            token_down = market_info["token_down"]

            up_mid = fetch_midpoint(session, token_up)
            down_mid = fetch_midpoint(session, token_down)

            up_book = {"best_bid": None, "best_ask": None, "spread": None, "bid_liq": 0.0, "ask_liq": 0.0}
            down_book = {"best_bid": None, "best_ask": None, "spread": None, "bid_liq": 0.0, "ask_liq": 0.0}

            if not args.no_book:
                try:
                    up_book = parse_book(fetch_book(session, token_up))
                except Exception:
                    error_codes.append("ws_disconnect_timeout")
                try:
                    down_book = parse_book(fetch_book(session, token_down))
                except Exception:
                    error_codes.append("ws_disconnect_timeout")

            mid_sum = round(up_mid + down_mid, 4) if (up_mid is not None and down_mid is not None) else ""
            ask_sum = (
                round(up_book["best_ask"] + down_book["best_ask"], 4)
                if (up_book["best_ask"] is not None and down_book["best_ask"] is not None)
                else ""
            )

            row = {
                "timestamp_utc": ts_iso,
                "schema_version": SCHEMA_VERSION,
                "row_status": "valid",
                "error_code": "",
                "market_key": market_info.get("market_key", ""),
                "market_close_utc": market_info.get("market_close_utc", ""),
                "slug": market_info.get("slug", ""),
                "question": market_info.get("question", ""),
                "up_mid": up_mid if up_mid is not None else "",
                "up_best_bid": up_book["best_bid"] if up_book["best_bid"] is not None else "",
                "up_best_ask": up_book["best_ask"] if up_book["best_ask"] is not None else "",
                "up_spread": up_book["spread"] if up_book["spread"] is not None else "",
                "up_bid_liq": up_book["bid_liq"],
                "up_ask_liq": up_book["ask_liq"],
                "down_mid": down_mid if down_mid is not None else "",
                "down_best_bid": down_book["best_bid"] if down_book["best_bid"] is not None else "",
                "down_best_ask": down_book["best_ask"] if down_book["best_ask"] is not None else "",
                "down_spread": down_book["spread"] if down_book["spread"] is not None else "",
                "down_bid_liq": down_book["bid_liq"],
                "down_ask_liq": down_book["ask_liq"],
                "mid_sum": mid_sum,
                "ask_sum": ask_sum,
            }

            row_status, error_code = validate_row(row, error_codes)
            row["row_status"] = row_status
            row["error_code"] = error_code
            append_csv(args.csv_file, CSV_FIELDS, row)

            print(
                f"{ts_iso[-15:-7]} | status={row_status} err={error_code or '-'} | "
                f"UP mid={fmt(up_mid)} bid={fmt(up_book['best_bid'])} ask={fmt(up_book['best_ask'])} | "
                f"DN mid={fmt(down_mid)} bid={fmt(down_book['best_bid'])} ask={fmt(down_book['best_ask'])} | "
                f"sum_ask={fmt(ask_sum)} close={market_info.get('market_close_utc', 'N/A')}"
            )

        except Exception as e:
            err_txt = str(e).lower()
            if "timeout" in err_txt:
                error_codes.append("ws_disconnect_timeout")
            else:
                error_codes.append("type_mismatch")

            row = {
                "timestamp_utc": ts_iso,
                "schema_version": SCHEMA_VERSION,
                "row_status": "invalid",
                "error_code": normalize_error_codes(error_codes),
                "market_key": market_info.get("market_key", ""),
                "market_close_utc": market_info.get("market_close_utc", ""),
                "slug": market_info.get("slug", ""),
                "question": market_info.get("question", ""),
                "up_mid": "",
                "up_best_bid": "",
                "up_best_ask": "",
                "up_spread": "",
                "up_bid_liq": "",
                "up_ask_liq": "",
                "down_mid": "",
                "down_best_bid": "",
                "down_best_ask": "",
                "down_spread": "",
                "down_bid_liq": "",
                "down_ask_liq": "",
                "mid_sum": "",
                "ask_sum": "",
            }
            append_csv(args.csv_file, CSV_FIELDS, row)
            print(f"{ts_iso} | ERROR: {e}")

        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    main()
