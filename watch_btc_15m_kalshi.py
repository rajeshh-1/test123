"""
watch_btc_15m_kalshi.py

BTC 15m Kalshi monitor with robust CSV schema handling for arbitrage analysis.
"""

import argparse
import base64
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_SERIES = "KXBTC15M"
SCHEMA_VERSION = "2.0"
DEFAULT_ROLLOVER_GUARD_SEC = 3.0
KEY_ID_ENV = "KALSHI_API_KEY_ID"
KEY_PATH_ENV = "KALSHI_PRIVATE_KEY_PATH"
DEFAULT_KEY_PATH = os.path.join(os.path.dirname(__file__), "Bot_Principal", "kalshi-key.pem.txt")


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------
_private_key = None
_key_id_cache = ""
_key_path_cache = ""


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _read_kalshi_auth() -> tuple[str, str]:
    key_id = str(os.getenv(KEY_ID_ENV, "")).strip()
    key_path = str(os.getenv(KEY_PATH_ENV, DEFAULT_KEY_PATH)).strip()
    if not key_id:
        raise RuntimeError(f"missing {KEY_ID_ENV}")
    if not key_path:
        raise RuntimeError(f"missing {KEY_PATH_ENV}")
    return key_id, key_path


def _load_private_key() -> tuple[str, Any]:
    global _private_key, _key_id_cache, _key_path_cache
    key_id, key_path = _read_kalshi_auth()
    if _private_key is not None and _key_id_cache == key_id and _key_path_cache == key_path:
        return key_id, _private_key
    path_obj = Path(key_path).expanduser().resolve()
    if not path_obj.exists():
        raise FileNotFoundError(f"kalshi private key file not found: {path_obj}")
    with path_obj.open("rb") as fh:
        _private_key = serialization.load_pem_private_key(fh.read(), password=None)
    _key_id_cache = key_id
    _key_path_cache = str(path_obj)
    return key_id, _private_key


def kalshi_headers(method: str, path: str) -> dict:
    key_id, priv = _load_private_key()
    ts = str(int(time.time() * 1000))
    msg = ts + method + path
    sig = priv.sign(
        msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-API-KEY": key_id,
        "KALSHI-API-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-API-TIMESTAMP": ts,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    return s


# ---------------------------------------------------------------------------
# API HELPERS
# ---------------------------------------------------------------------------
def fetch_open_markets(session: requests.Session, series_ticker: str) -> list:
    path = f"/markets?series_ticker={series_ticker}&status=open&limit=50"
    r = session.get(KALSHI_BASE + path, headers=kalshi_headers("GET", path), timeout=10)
    r.raise_for_status()
    return [m for m in r.json().get("markets", []) if m.get("status") == "active"]


def fetch_market(session: requests.Session, ticker: str) -> dict:
    path = f"/markets/{ticker}"
    r = session.get(KALSHI_BASE + path, headers=kalshi_headers("GET", path), timeout=8)
    r.raise_for_status()
    return r.json().get("market", {})


def fetch_orderbook(session: requests.Session, ticker: str, depth: int = 5) -> dict:
    path = f"/markets/{ticker}/orderbook?depth={depth}"
    r = session.get(KALSHI_BASE + path, headers=kalshi_headers("GET", path), timeout=8)
    r.raise_for_status()
    return r.json().get("orderbook_fp", {})


def safe_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_iso_utc(value: str):
    if not value:
        return None
    txt = str(value).strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_utc(dt_obj) -> str:
    if dt_obj is None:
        return ""
    return dt_obj.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_market_key(market_close_utc: str) -> str:
    return f"BTC15M_{market_close_utc}" if market_close_utc else ""


def parse_ob_dollars(ob: dict):
    yes_entries = ob.get("yes_dollars", [])
    no_entries = ob.get("no_dollars", [])

    yes_prices = [safe_float(e[0]) for e in yes_entries if len(e) >= 2 and safe_float(e[0]) is not None]
    no_prices = [safe_float(e[0]) for e in no_entries if len(e) >= 2 and safe_float(e[0]) is not None]

    yes_sizes = [safe_float(e[1], 0.0) for e in yes_entries if len(e) >= 2]
    no_sizes = [safe_float(e[1], 0.0) for e in no_entries if len(e) >= 2]

    ob_yes_best_bid = max(yes_prices, default=None)
    ob_no_best_bid = max(no_prices, default=None)

    ob_yes_best_ask = (1.0 - ob_no_best_bid) if ob_no_best_bid is not None else None
    ob_no_best_ask = (1.0 - ob_yes_best_bid) if ob_yes_best_bid is not None else None

    ob_yes_depth = sum(yes_sizes)
    ob_no_depth = sum(no_sizes)

    return {
        "ob_yes_best_bid": round(ob_yes_best_bid, 4) if ob_yes_best_bid is not None else "",
        "ob_yes_best_ask": round(ob_yes_best_ask, 4) if ob_yes_best_ask is not None else "",
        "ob_no_best_bid": round(ob_no_best_bid, 4) if ob_no_best_bid is not None else "",
        "ob_no_best_ask": round(ob_no_best_ask, 4) if ob_no_best_ask is not None else "",
        "ob_yes_depth": round(ob_yes_depth, 2),
        "ob_no_depth": round(ob_no_depth, 2),
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
    "ticker",
    "title",
    "yes_sub_title",
    "floor_strike",
    "close_time",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "last_price",
    "spread_yes",
    "spread_no",
    "volume",
    "open_interest",
    "ob_yes_best_bid",
    "ob_yes_best_ask",
    "ob_no_best_bid",
    "ob_no_best_ask",
    "ob_yes_depth",
    "ob_no_depth",
]

NUMERIC_FIELDS = [
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "last_price",
    "spread_yes",
    "spread_no",
    "volume",
    "open_interest",
    "ob_yes_best_bid",
    "ob_yes_best_ask",
    "ob_no_best_bid",
    "ob_no_best_ask",
    "ob_yes_depth",
    "ob_no_depth",
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


def validate_row(row: dict, error_codes: list[str]) -> tuple[str, str]:
    codes = list(error_codes)

    for required in ("timestamp_utc", "market_key", "market_close_utc", "ticker"):
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

    if row.get("yes_ask", "") in ("", None) or row.get("no_ask", "") in ("", None):
        codes.append("missing_book_side")

    if row.get("ob_yes_best_ask", "") in ("", None) or row.get("ob_no_best_ask", "") in ("", None):
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
    parser = argparse.ArgumentParser(description="Monitor BTC 15m Kalshi to CSV.")
    parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds.")
    parser.add_argument("--max-seconds", type=int, default=0, help="Max runtime in seconds (0 = infinite).")
    parser.add_argument("--csv-file", default="logs/kalshi_btc_15m_ticks.csv", help="Output CSV file.")
    parser.add_argument("--series-ticker", default=DEFAULT_SERIES, help="Kalshi series ticker.")
    parser.add_argument("--ob-depth", type=int, default=5, help="Orderbook depth.")
    parser.add_argument(
        "--rollover-guard-sec",
        type=float,
        default=DEFAULT_ROLLOVER_GUARD_SEC,
        help="Mark rows invalid for this many seconds after market rollover.",
    )
    args = parser.parse_args()

    load_env_file(str((Path(__file__).resolve().parent / ".env")))
    _load_private_key()
    session = build_session()
    ensure_csv_schema(args.csv_file, CSV_FIELDS)

    print("=" * 80)
    print(f"  KALSHI BTC 15-MIN MONITOR | series={args.series_ticker}")
    print(f"  interval={args.interval}s | csv={args.csv_file} | schema={SCHEMA_VERSION}")
    print("=" * 80)

    started = time.time()
    cached_markets = []
    last_discovery = 0.0
    discovery_interval = 20.0

    last_market_key_by_ticker = {}
    last_market_switch_ts_by_ticker = {}

    while True:
        now = time.time()
        if args.max_seconds > 0 and (now - started) >= args.max_seconds:
            print("[MONITOR] finished by --max-seconds.")
            break

        if now - last_discovery >= discovery_interval:
            try:
                cached_markets = fetch_open_markets(session, args.series_ticker)
                tickers = [m.get("ticker", "?") for m in cached_markets]
                print(f"[DISCOVERY] active markets={len(cached_markets)} | {tickers}")
                last_discovery = now
            except Exception as e:
                print(f"[DISCOVERY] error: {e}")
                time.sleep(3)
                continue

        if not cached_markets:
            print(f"[WAIT] no active market for {args.series_ticker}.")
            time.sleep(max(args.interval, 5.0))
            last_discovery = 0.0
            continue

        for mkt in cached_markets:
            ticker = mkt.get("ticker", "")
            if not ticker:
                continue

            ts_iso = datetime.now(timezone.utc).isoformat()
            error_codes = []

            try:
                m = fetch_market(session, ticker)

                close_dt = parse_iso_utc(str(m.get("close_time", "")))
                market_close_utc = to_iso_utc(close_dt)
                market_key = make_market_key(market_close_utc)

                prev_key = last_market_key_by_ticker.get(ticker, "")
                if market_key and market_key != prev_key:
                    last_market_key_by_ticker[ticker] = market_key
                    last_market_switch_ts_by_ticker[ticker] = now

                switch_ts = last_market_switch_ts_by_ticker.get(ticker, 0.0)
                if (now - switch_ts) <= max(0.0, args.rollover_guard_sec):
                    error_codes.append("market_rollover_race")

                yes_bid = safe_float(m.get("yes_bid_dollars"))
                yes_ask = safe_float(m.get("yes_ask_dollars"))
                no_bid = safe_float(m.get("no_bid_dollars"))
                no_ask = safe_float(m.get("no_ask_dollars"))
                last_price = safe_float(m.get("last_price_dollars"))
                volume = safe_float(m.get("volume_fp"))
                open_interest = safe_float(m.get("open_interest_fp"))
                floor = m.get("floor_strike", "")

                spread_yes = round(yes_ask - yes_bid, 4) if (yes_ask is not None and yes_bid is not None) else ""
                spread_no = round(no_ask - no_bid, 4) if (no_ask is not None and no_bid is not None) else ""

                ob_data = {
                    "ob_yes_best_bid": "",
                    "ob_yes_best_ask": "",
                    "ob_no_best_bid": "",
                    "ob_no_best_ask": "",
                    "ob_yes_depth": 0.0,
                    "ob_no_depth": 0.0,
                }
                try:
                    ob_data = parse_ob_dollars(fetch_orderbook(session, ticker, depth=args.ob_depth))
                except Exception:
                    error_codes.append("ws_disconnect_timeout")

                row = {
                    "timestamp_utc": ts_iso,
                    "schema_version": SCHEMA_VERSION,
                    "row_status": "valid",
                    "error_code": "",
                    "market_key": market_key,
                    "market_close_utc": market_close_utc,
                    "ticker": ticker,
                    "title": m.get("title", ""),
                    "yes_sub_title": m.get("yes_sub_title", ""),
                    "floor_strike": floor,
                    "close_time": m.get("close_time", ""),
                    "yes_bid": yes_bid if yes_bid is not None else "",
                    "yes_ask": yes_ask if yes_ask is not None else "",
                    "no_bid": no_bid if no_bid is not None else "",
                    "no_ask": no_ask if no_ask is not None else "",
                    "last_price": last_price if last_price is not None else "",
                    "spread_yes": spread_yes,
                    "spread_no": spread_no,
                    "volume": volume if volume is not None else "",
                    "open_interest": open_interest if open_interest is not None else "",
                    **ob_data,
                }

                row_status, error_code = validate_row(row, error_codes)
                row["row_status"] = row_status
                row["error_code"] = error_code

                append_csv(args.csv_file, CSV_FIELDS, row)

                print(
                    f"{ts_iso[-15:-7]} | {ticker} | "
                    f"status={row_status} err={error_code or '-'} | "
                    f"YES {fmt(yes_bid)}/{fmt(yes_ask)} NO {fmt(no_bid)}/{fmt(no_ask)} | "
                    f"OB Y {fmt(ob_data['ob_yes_best_bid'])}/{fmt(ob_data['ob_yes_best_ask'])} "
                    f"N {fmt(ob_data['ob_no_best_bid'])}/{fmt(ob_data['ob_no_best_ask'])} | "
                    f"close={market_close_utc or 'N/A'}"
                )

            except Exception as e:
                fallback_close = to_iso_utc(parse_iso_utc(str(mkt.get("close_time", ""))))
                fallback_key = make_market_key(fallback_close)
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
                    "market_key": fallback_key,
                    "market_close_utc": fallback_close,
                    "ticker": ticker,
                    "title": "",
                    "yes_sub_title": "",
                    "floor_strike": "",
                    "close_time": str(mkt.get("close_time", "")),
                    "yes_bid": "",
                    "yes_ask": "",
                    "no_bid": "",
                    "no_ask": "",
                    "last_price": "",
                    "spread_yes": "",
                    "spread_no": "",
                    "volume": "",
                    "open_interest": "",
                    "ob_yes_best_bid": "",
                    "ob_yes_best_ask": "",
                    "ob_no_best_bid": "",
                    "ob_no_best_ask": "",
                    "ob_yes_depth": "",
                    "ob_no_depth": "",
                }
                append_csv(args.csv_file, CSV_FIELDS, row)
                print(f"{ts_iso} | {ticker} | ERROR: {e}")

        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    main()
