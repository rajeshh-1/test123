import argparse
import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import requests


GAMMA_URL = "https://gamma-api.polymarket.com/events"
TRADES_URL = "https://data-api.polymarket.com/trades"


@dataclass
class TradePattern:
    slug: str
    side: str
    outcome: str
    price: float
    size: float


PRESET_MAR4_BTC = [
    TradePattern("btc-updown-5m-1772633100", "BUY", "Up", 0.59, 4.1753),
    TradePattern("btc-updown-5m-1772635200", "BUY", "Down", 0.89, 2.8022),
    TradePattern("btc-updown-5m-1772635500", "BUY", "Up", 0.886, 19.6907),
    TradePattern("btc-updown-5m-1772635800", "BUY", "Up", 0.669, 7.3821),
    TradePattern("btc-updown-5m-1772637000", "BUY", "Down", 0.05, 49.9718),
    TradePattern("btc-updown-5m-1772638200", "BUY", "Down", 0.945, 5.2876),
]


def _to_float(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def load_patterns_from_file(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for row in data:
        out.append(
            TradePattern(
                slug=str(row["slug"]),
                side=str(row.get("side", "BUY")).upper(),
                outcome=str(row["outcome"]),
                price=float(row["price"]),
                size=float(row["size"]),
            )
        )
    return out


def get_condition_id_by_slug(slug: str) -> str | None:
    resp = requests.get(GAMMA_URL, params={"slug": slug}, timeout=20)
    resp.raise_for_status()
    events = resp.json()
    if not events:
        return None
    markets = events[0].get("markets", [])
    if not markets:
        return None
    return str(markets[0].get("conditionId", "")).strip() or None


def fetch_all_trades_for_market(condition_id: str, page_size: int, max_pages: int):
    trades = []
    offset = 0
    pages = 0
    while pages < max_pages:
        resp = requests.get(
            TRADES_URL,
            params={"market": condition_id, "limit": page_size, "offset": offset},
            timeout=25,
        )
        if resp.status_code == 400:
            # Data API can return 400 for offset beyond available range.
            break
        resp.raise_for_status()
        chunk = resp.json()
        if not isinstance(chunk, list) or not chunk:
            break
        trades.extend(chunk)
        if len(chunk) < page_size:
            break
        pages += 1
        offset += len(chunk)
    return trades


def fetch_user_trades(user: str, page_size: int, max_pages: int):
    trades = []
    offset = 0
    pages = 0
    while pages < max_pages:
        resp = requests.get(
            TRADES_URL,
            params={"user": user, "limit": page_size, "offset": offset},
            timeout=25,
        )
        if resp.status_code == 400:
            break
        resp.raise_for_status()
        chunk = resp.json()
        if not isinstance(chunk, list) or not chunk:
            break
        trades.extend(chunk)
        if len(chunk) < page_size:
            break
        pages += 1
        offset += len(chunk)
    return trades


def aggregate_market_trades(trades: list[dict]):
    grouped = {}
    for t in trades:
        wallet = str(t.get("proxyWallet", "")).lower().strip()
        side = str(t.get("side", "")).upper().strip()
        outcome = str(t.get("outcome", "")).strip().lower()
        size = _to_float(t.get("size", 0.0))
        price = _to_float(t.get("price", 0.0))
        if not wallet or not side or not outcome or size <= 0 or price <= 0:
            continue
        key = (wallet, side, outcome)
        agg = grouped.get(key)
        if not agg:
            agg = {"size": 0.0, "notional": 0.0, "min_ts": None, "max_ts": None}
            grouped[key] = agg
        agg["size"] += size
        agg["notional"] += size * price
        ts = t.get("timestamp")
        if isinstance(ts, (int, float)):
            if agg["min_ts"] is None or ts < agg["min_ts"]:
                agg["min_ts"] = ts
            if agg["max_ts"] is None or ts > agg["max_ts"]:
                agg["max_ts"] = ts

    out = {}
    for key, agg in grouped.items():
        total_size = agg["size"]
        out[key] = {
            "size": total_size,
            "avg_price": (agg["notional"] / total_size) if total_size > 0 else 0.0,
            "min_ts": agg["min_ts"],
            "max_ts": agg["max_ts"],
        }
    return out


def aggregate_patterns_for_wallet(
    user: str,
    patterns: list[TradePattern],
    page_size: int,
    max_pages: int,
    price_tol: float,
    size_tol: float,
):
    slugs = {p.slug for p in patterns}
    trades = fetch_user_trades(user, page_size=page_size, max_pages=max_pages)
    trades = [t for t in trades if str(t.get("slug", "")).strip() in slugs]
    agg = aggregate_market_trades(trades)
    details = []
    matches = 0
    for i, p in enumerate(patterns):
        key = (user.lower().strip(), p.side.upper(), p.outcome.lower())
        matched = None
        # aggregate_market_trades key has no slug, so build per slug manually
        per_slug = aggregate_market_trades([t for t in trades if str(t.get("slug", "")) == p.slug])
        if key in per_slug:
            matched = per_slug[key]
            ok = True
            strict_ok = (abs(matched["avg_price"] - p.price) <= price_tol) and (abs(matched["size"] - p.size) <= size_tol)
        else:
            ok = False
            strict_ok = False
        details.append(
            {
                "pattern_key": f"{i}:{p.slug}",
                "slug": p.slug,
                "side": p.side,
                "outcome": p.outcome,
                "target_price": p.price,
                "target_size": p.size,
                "wallet_has_side_outcome": ok,
                "wallet_strict_match": strict_ok,
                "matched_avg_price": matched["avg_price"] if matched else None,
                "matched_total_size": matched["size"] if matched else None,
            }
        )
        if strict_ok:
            matches += 1
    return matches, details


def main():
    parser = argparse.ArgumentParser(
        description="Encontra wallet por assinatura de trades em mercados BTC updown."
    )
    parser.add_argument("--patterns-json", default="", help="Arquivo JSON com lista de patterns.")
    parser.add_argument("--preset", default="mar4_btc", choices=["mar4_btc"], help="Preset pronto.")
    parser.add_argument("--price-tol", type=float, default=0.015, help="Tolerancia absoluta de preco.")
    parser.add_argument("--size-tol", type=float, default=0.5, help="Tolerancia absoluta de size/shares.")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--min-matches", type=int, default=3, help="Minimo de patterns batidas para listar.")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--verify-top", type=int, default=5, help="Valida top N candidatos via query por user.")
    parser.add_argument("--out", default="logs/wallet_pattern_candidates.csv")
    args = parser.parse_args()

    if args.patterns_json:
        patterns = load_patterns_from_file(args.patterns_json)
    else:
        patterns = PRESET_MAR4_BTC

    by_slug = defaultdict(list)
    for i, p in enumerate(patterns):
        by_slug[p.slug].append((i, p))

    score = defaultdict(int)
    matches = defaultdict(list)

    for slug, items in by_slug.items():
        condition_id = get_condition_id_by_slug(slug)
        if not condition_id:
            print(f"[WARN] slug sem conditionId: {slug}")
            continue
        trades = fetch_all_trades_for_market(condition_id, args.page_size, args.max_pages)
        print(f"[SCAN] slug={slug} condition={condition_id} trades={len(trades)}")

        aggregated = aggregate_market_trades(trades)
        for (wallet, side, outcome), agg in aggregated.items():
            for idx, p in items:
                if side != p.side.upper():
                    continue
                if outcome != p.outcome.lower():
                    continue
                if abs(agg["avg_price"] - p.price) > args.price_tol:
                    continue
                if abs(agg["size"] - p.size) > args.size_tol:
                    continue
                key = f"{idx}:{p.slug}"
                if key not in {m["pattern_key"] for m in matches[wallet]}:
                    score[wallet] += 1
                    matches[wallet].append(
                        {
                            "pattern_key": key,
                            "slug": p.slug,
                            "side": p.side,
                            "outcome": p.outcome,
                            "target_price": p.price,
                            "target_size": p.size,
                            "matched_avg_price": agg["avg_price"],
                            "matched_total_size": agg["size"],
                            "min_ts": agg["min_ts"],
                            "max_ts": agg["max_ts"],
                        }
                    )

    rows = []
    for wallet, n in score.items():
        if n < args.min_matches:
            continue
        rows.append(
            {
                "wallet": wallet,
                "matches": n,
                "patterns_total": len(patterns),
                "coverage": round(n / max(1, len(patterns)), 4),
                "details_json": json.dumps(matches[wallet], ensure_ascii=False),
                "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        )

    rows.sort(key=lambda r: (r["matches"], r["coverage"]), reverse=True)
    rows = rows[: args.top]

    # Optional verification pass querying by user to avoid missing old trades in heavy markets.
    for row in rows[: max(0, args.verify_top)]:
        verified_matches, verified_details = aggregate_patterns_for_wallet(
            user=row["wallet"],
            patterns=patterns,
            page_size=args.page_size,
            max_pages=args.max_pages,
            price_tol=args.price_tol,
            size_tol=args.size_tol,
        )
        row["verified_matches"] = verified_matches
        row["verified_coverage"] = round(verified_matches / max(1, len(patterns)), 4)
        row["verified_details_json"] = json.dumps(verified_details, ensure_ascii=False)

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "wallet",
                "matches",
                "patterns_total",
                "coverage",
                "verified_matches",
                "verified_coverage",
                "details_json",
                "verified_details_json",
                "scanned_at_utc",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"[DONE] candidates={len(rows)} out={args.out}")
    if rows:
        print("[TOP]")
        for r in rows[:5]:
            print(f"  {r['wallet']} matches={r['matches']} coverage={r['coverage']:.2%}")


if __name__ == "__main__":
    main()
