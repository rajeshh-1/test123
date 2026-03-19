import argparse
import csv
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
DATA_API_HOST = "https://data-api.polymarket.com"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return value


def build_current_slug(coin: str, timeframe: str) -> str:
    now = datetime.now(timezone.utc)
    bucket = 5 if timeframe == "5m" else 15
    block_start = now.replace(minute=(now.minute // bucket) * bucket, second=0, microsecond=0)
    return f"{coin}-updown-{timeframe}-{int(block_start.timestamp())}"


@dataclass
class VirtualOrder:
    id: str
    slug: str
    condition_id: str
    token_id: str
    outcome: str
    side: str
    limit_price: float
    remaining_size: float
    queue_ahead: float
    placed_ts: float
    created_iso: str
    status: str = "OPEN"


@dataclass
class MarketSnapshot:
    slug: str
    condition_id: str
    question: str
    end_ts: float
    closed: bool
    accepting_orders: bool
    outcomes: list[str] = field(default_factory=list)
    token_ids: list[str] = field(default_factory=list)


class MarketHunter:
    def __init__(self, args):
        self.args = args
        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=0.4,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.low_prices = [float(x) for x in args.low_prices.split(",") if x.strip()]
        self.coins = [x.strip().lower() for x in args.coins.split(",") if x.strip()]
        self.events_log = Path(args.log_file)
        self.state_file = Path(args.state_file)
        self.events_log.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        self.open_orders: list[VirtualOrder] = []
        self.inventory_qty: dict[str, float] = {}
        self.inventory_cost: dict[str, float] = {}
        self.token_meta: dict[str, dict] = {}
        self.condition_tokens: dict[str, dict[str, str]] = {}
        self.last_trade_keys: set[str] = set()
        self.realized_pnl_usdc = 0.0
        self.fee_rate_cache: dict[str, int] = {}
        self.last_state_save = 0.0
        self.start_ts = time.time()

    def _event_row(self, event: str, data: dict):
        fields = [
            "timestamp_utc",
            "event",
            "slug",
            "condition_id",
            "token_id",
            "outcome",
            "order_id",
            "price",
            "size",
            "queue_ahead",
            "inventory_qty",
            "inventory_cost",
            "realized_pnl_usdc",
            "notes",
        ]
        row = {
            "timestamp_utc": utc_now_iso(),
            "event": event,
            "slug": data.get("slug", ""),
            "condition_id": data.get("condition_id", ""),
            "token_id": data.get("token_id", ""),
            "outcome": data.get("outcome", ""),
            "order_id": data.get("order_id", ""),
            "price": data.get("price", ""),
            "size": data.get("size", ""),
            "queue_ahead": data.get("queue_ahead", ""),
            "inventory_qty": data.get("inventory_qty", ""),
            "inventory_cost": data.get("inventory_cost", ""),
            "realized_pnl_usdc": round(self.realized_pnl_usdc, 8),
            "notes": data.get("notes", ""),
        }
        exists = self.events_log.is_file()
        with self.events_log.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def _save_state(self):
        now = time.time()
        if now - self.last_state_save < self.args.save_interval_seconds:
            return
        self.last_state_save = now
        payload = {
            "timestamp_utc": utc_now_iso(),
            "realized_pnl_usdc": self.realized_pnl_usdc,
            "inventory_qty": self.inventory_qty,
            "inventory_cost": self.inventory_cost,
            "open_orders": [o.__dict__ for o in self.open_orders if o.status == "OPEN"],
        }
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def fetch_event_market(self, slug: str) -> MarketSnapshot | None:
        resp = self.session.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=12)
        resp.raise_for_status()
        events = resp.json()
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

        outcomes = _parse_json_field(market.get("outcomes", [])) or []
        token_ids = _parse_json_field(market.get("clobTokenIds", [])) or []
        if len(outcomes) != len(token_ids) or len(outcomes) < 2:
            return None

        end_raw = market.get("endDate")
        if not end_raw:
            return None
        end_ts = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")).timestamp()

        snap = MarketSnapshot(
            slug=slug,
            condition_id=str(market.get("conditionId", "")),
            question=str(market.get("question", "")),
            end_ts=end_ts,
            closed=bool(market.get("closed", False)),
            accepting_orders=bool(market.get("acceptingOrders", True)),
            outcomes=[str(x) for x in outcomes],
            token_ids=[str(x) for x in token_ids],
        )
        self.condition_tokens[snap.condition_id] = {snap.outcomes[i]: snap.token_ids[i] for i in range(len(snap.outcomes))}
        for i, tid in enumerate(snap.token_ids):
            self.token_meta[tid] = {
                "slug": snap.slug,
                "condition_id": snap.condition_id,
                "outcome": snap.outcomes[i],
                "question": snap.question,
            }
        return snap

    def get_orderbook(self, token_id: str) -> tuple[list[dict], list[dict]]:
        resp = self.session.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        bids = [{"price": _safe_float(x.get("price")), "size": _safe_float(x.get("size"))} for x in data.get("bids", [])]
        asks = [{"price": _safe_float(x.get("price")), "size": _safe_float(x.get("size"))} for x in data.get("asks", [])]
        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])
        return bids, asks

    def _size_at_bid_price(self, bids: list[dict], price: float) -> float:
        for b in bids:
            if abs(b["price"] - price) < 1e-9:
                return b["size"]
        return 0.0

    def estimate_fee_usdc(self, token_id: str, shares: float, price: float) -> float:
        base_bps = self.get_base_fee_bps(token_id)
        p = max(0.0001, min(0.9999, float(price)))
        fee_rate = base_bps / 10000.0
        return shares * p * fee_rate * ((p * (1.0 - p)) ** 2)

    def get_base_fee_bps(self, token_id: str) -> int:
        if token_id in self.fee_rate_cache:
            return self.fee_rate_cache[token_id]
        try:
            resp = self.session.get(f"{CLOB_HOST}/fee-rate", params={"token_id": token_id}, timeout=8)
            if resp.status_code == 200:
                base_fee = int(resp.json().get("base_fee", self.args.default_fee_bps))
            else:
                base_fee = int(self.args.default_fee_bps)
        except Exception:
            base_fee = int(self.args.default_fee_bps)
        self.fee_rate_cache[token_id] = base_fee
        return base_fee

    def _already_has_open_order(self, token_id: str, price: float) -> bool:
        for o in self.open_orders:
            if o.status == "OPEN" and o.token_id == token_id and abs(o.limit_price - price) < 1e-9 and o.side == "BUY":
                return True
        return False

    def place_low_tick_orders(self, market: MarketSnapshot):
        now = time.time()
        tte = market.end_ts - now
        if tte < self.args.min_tte_quote_seconds:
            return
        if market.closed or not market.accepting_orders:
            return

        for i, token_id in enumerate(market.token_ids[:2]):
            outcome = market.outcomes[i]
            bids, _ = self.get_orderbook(token_id)
            for p in self.low_prices:
                if self._already_has_open_order(token_id, p):
                    continue
                shares = self.args.usd_per_order / max(p, 1e-6)
                queue_ahead = self._size_at_bid_price(bids, p) + self.args.queue_buffer_shares
                order = VirtualOrder(
                    id=uuid.uuid4().hex[:12],
                    slug=market.slug,
                    condition_id=market.condition_id,
                    token_id=token_id,
                    outcome=outcome,
                    side="BUY",
                    limit_price=p,
                    remaining_size=shares,
                    queue_ahead=queue_ahead,
                    placed_ts=now + (self.args.latency_ms / 1000.0),
                    created_iso=utc_now_iso(),
                )
                self.open_orders.append(order)
                self._event_row(
                    "ORDER_PLACE",
                    {
                        "slug": market.slug,
                        "condition_id": market.condition_id,
                        "token_id": token_id,
                        "outcome": outcome,
                        "order_id": order.id,
                        "price": p,
                        "size": shares,
                        "queue_ahead": queue_ahead,
                        "notes": "dry_run_limit_buy",
                    },
                )

    def fetch_recent_market_trades(self, condition_id: str) -> list[dict]:
        resp = self.session.get(
            f"{DATA_API_HOST}/trades",
            params={"market": condition_id, "limit": self.args.market_trades_limit, "offset": 0},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        fresh = []
        for t in data:
            key = "|".join(
                [
                    str(t.get("transactionHash", "")),
                    str(t.get("asset", "")),
                    str(t.get("side", "")),
                    str(t.get("price", "")),
                    str(t.get("size", "")),
                    str(t.get("timestamp", "")),
                ]
            )
            if key in self.last_trade_keys:
                continue
            self.last_trade_keys.add(key)
            fresh.append(t)
        fresh.sort(key=lambda x: _safe_float(x.get("timestamp", 0)))
        if len(self.last_trade_keys) > 200000:
            self.last_trade_keys = set(list(self.last_trade_keys)[-120000:])
        return fresh

    def _inventory_add(self, token_id: str, shares: float, total_cost: float):
        self.inventory_qty[token_id] = self.inventory_qty.get(token_id, 0.0) + shares
        self.inventory_cost[token_id] = self.inventory_cost.get(token_id, 0.0) + total_cost

    def _inventory_remove(self, token_id: str, shares: float) -> float:
        qty = self.inventory_qty.get(token_id, 0.0)
        cost = self.inventory_cost.get(token_id, 0.0)
        if qty <= 0 or shares <= 0:
            return 0.0
        remove = min(shares, qty)
        avg_cost = cost / max(qty, 1e-12)
        removed_cost = remove * avg_cost
        new_qty = qty - remove
        new_cost = max(0.0, cost - removed_cost)
        if new_qty <= 1e-9:
            new_qty = 0.0
            new_cost = 0.0
        self.inventory_qty[token_id] = new_qty
        self.inventory_cost[token_id] = new_cost
        return removed_cost

    def apply_trade_fills(self, market: MarketSnapshot, trades: list[dict]):
        for t in trades:
            token_id = str(t.get("asset", ""))
            side = str(t.get("side", "")).upper().strip()
            price = _safe_float(t.get("price", 0.0))
            size = _safe_float(t.get("size", 0.0))
            ts = _safe_float(t.get("timestamp", 0.0))
            if size <= 0 or price <= 0:
                continue

            # Passive bid fill proxy: taker SELL at/under our bid price.
            if side != "SELL":
                continue

            for order in self.open_orders:
                if order.status != "OPEN":
                    continue
                if order.condition_id != market.condition_id:
                    continue
                if order.token_id != token_id or order.side != "BUY":
                    continue
                if ts < order.placed_ts:
                    continue
                if price > order.limit_price + 1e-12:
                    continue
                if order.remaining_size <= 1e-12:
                    continue

                fillable = size
                if order.queue_ahead > 0:
                    q_consume = min(order.queue_ahead, fillable)
                    order.queue_ahead -= q_consume
                    fillable -= q_consume

                if fillable <= 0:
                    continue
                fill_size = min(order.remaining_size, fillable)
                if fill_size <= 0:
                    continue

                fee = self.estimate_fee_usdc(order.token_id, fill_size, order.limit_price)
                cost = (fill_size * order.limit_price) + fee
                self._inventory_add(order.token_id, fill_size, cost)
                order.remaining_size -= fill_size
                if order.remaining_size <= 1e-9:
                    order.status = "FILLED"

                self._event_row(
                    "FILL_BUY",
                    {
                        "slug": order.slug,
                        "condition_id": order.condition_id,
                        "token_id": order.token_id,
                        "outcome": order.outcome,
                        "order_id": order.id,
                        "price": order.limit_price,
                        "size": round(fill_size, 8),
                        "queue_ahead": round(order.queue_ahead, 8),
                        "inventory_qty": round(self.inventory_qty.get(order.token_id, 0.0), 8),
                        "inventory_cost": round(self.inventory_cost.get(order.token_id, 0.0), 8),
                        "notes": f"trade_ts={int(ts)}",
                    },
                )

    def _simulate_sell_vwap(self, bids: list[dict], qty: float):
        remaining = qty
        proceeds = 0.0
        sold = 0.0
        for b in bids:
            if remaining <= 0:
                break
            take = min(remaining, b["size"])
            sold += take
            proceeds += take * b["price"]
            remaining -= take
        vwap = proceeds / sold if sold > 0 else 0.0
        return {"sold": sold, "proceeds": proceeds, "vwap": vwap, "unfilled": remaining}

    def evaluate_take_profit(self, market: MarketSnapshot):
        for token_id in market.token_ids[:2]:
            qty = self.inventory_qty.get(token_id, 0.0)
            if qty <= self.args.min_inventory_shares:
                continue
            bids, _ = self.get_orderbook(token_id)
            if not bids:
                continue
            best_bid = bids[0]["price"]
            if best_bid < self.args.take_profit_price:
                continue

            target_qty = min(qty, self.args.max_sell_shares_per_cycle)
            sim = self._simulate_sell_vwap(bids, target_qty)
            if sim["sold"] <= 0:
                continue
            fee = self.estimate_fee_usdc(token_id, sim["sold"], sim["vwap"])
            removed_cost = self._inventory_remove(token_id, sim["sold"])
            pnl = sim["proceeds"] - fee - removed_cost
            self.realized_pnl_usdc += pnl

            meta = self.token_meta.get(token_id, {})
            self._event_row(
                "SELL_TP",
                {
                    "slug": meta.get("slug", market.slug),
                    "condition_id": meta.get("condition_id", market.condition_id),
                    "token_id": token_id,
                    "outcome": meta.get("outcome", ""),
                    "price": round(sim["vwap"], 6),
                    "size": round(sim["sold"], 8),
                    "inventory_qty": round(self.inventory_qty.get(token_id, 0.0), 8),
                    "inventory_cost": round(self.inventory_cost.get(token_id, 0.0), 8),
                    "notes": f"net={sim['proceeds']-fee:.6f} pnl={pnl:.6f}",
                },
            )

    def evaluate_merge_or_pair_sell(self, market: MarketSnapshot):
        if len(market.token_ids) < 2:
            return
        token_a = market.token_ids[0]
        token_b = market.token_ids[1]
        qty_a = self.inventory_qty.get(token_a, 0.0)
        qty_b = self.inventory_qty.get(token_b, 0.0)
        pairs = min(qty_a, qty_b)
        if pairs < self.args.merge_min_pairs:
            return

        bids_a, _ = self.get_orderbook(token_a)
        bids_b, _ = self.get_orderbook(token_b)
        sell_a = self._simulate_sell_vwap(bids_a, pairs)
        sell_b = self._simulate_sell_vwap(bids_b, pairs)
        fee_a = self.estimate_fee_usdc(token_a, sell_a["sold"], sell_a["vwap"]) if sell_a["sold"] > 0 else 0.0
        fee_b = self.estimate_fee_usdc(token_b, sell_b["sold"], sell_b["vwap"]) if sell_b["sold"] > 0 else 0.0
        net_sell = (sell_a["proceeds"] + sell_b["proceeds"]) - fee_a - fee_b
        net_merge = (pairs * 1.0) - self.args.merge_flat_cost_usdc - (pairs * self.args.merge_per_pair_cost_usdc)

        if net_merge > (net_sell + self.args.merge_safety_usdc):
            removed_a = self._inventory_remove(token_a, pairs)
            removed_b = self._inventory_remove(token_b, pairs)
            pnl = net_merge - (removed_a + removed_b)
            self.realized_pnl_usdc += pnl
            self._event_row(
                "MERGE",
                {
                    "slug": market.slug,
                    "condition_id": market.condition_id,
                    "price": 1.0,
                    "size": round(pairs, 8),
                    "notes": f"net_merge={net_merge:.6f} net_sell={net_sell:.6f} pnl={pnl:.6f}",
                },
            )
            return

        if not self.args.sell_when_better_than_merge:
            return
        if net_sell <= (net_merge + self.args.merge_safety_usdc):
            return
        if sell_a["sold"] <= 0 or sell_b["sold"] <= 0:
            return

        removed_a = self._inventory_remove(token_a, sell_a["sold"])
        removed_b = self._inventory_remove(token_b, sell_b["sold"])
        pnl = net_sell - (removed_a + removed_b)
        self.realized_pnl_usdc += pnl
        self._event_row(
            "SELL_PAIR",
            {
                "slug": market.slug,
                "condition_id": market.condition_id,
                "size": round(min(sell_a["sold"], sell_b["sold"]), 8),
                "notes": f"net_sell={net_sell:.6f} net_merge={net_merge:.6f} pnl={pnl:.6f}",
            },
        )

    def cancel_stale_orders(self):
        now = time.time()
        for o in self.open_orders:
            if o.status != "OPEN":
                continue
            age = now - (o.placed_ts - (self.args.latency_ms / 1000.0))
            if age >= self.args.order_stale_seconds:
                o.status = "CANCELED"
                self._event_row(
                    "ORDER_CANCEL",
                    {
                        "slug": o.slug,
                        "condition_id": o.condition_id,
                        "token_id": o.token_id,
                        "outcome": o.outcome,
                        "order_id": o.id,
                        "price": o.limit_price,
                        "size": o.remaining_size,
                        "queue_ahead": o.queue_ahead,
                        "notes": f"stale_age={age:.1f}s",
                    },
                )

    def run(self):
        print("[MARKET-HUNTER] dry_run=TRUE")
        print(
            f"[MARKET-HUNTER] coins={self.coins} timeframe={self.args.timeframe} "
            f"low_prices={self.low_prices} usd_per_order={self.args.usd_per_order}"
        )
        while True:
            try:
                now = time.time()
                if self.args.max_runtime_seconds > 0 and (now - self.start_ts) >= self.args.max_runtime_seconds:
                    print("[MARKET-HUNTER] max runtime reached, exiting.")
                    return

                for coin in self.coins:
                    slug = build_current_slug(coin, self.args.timeframe)
                    market = self.fetch_event_market(slug)
                    if not market:
                        continue
                    self.place_low_tick_orders(market)
                    recent = self.fetch_recent_market_trades(market.condition_id)
                    self.apply_trade_fills(market, recent)
                    self.evaluate_take_profit(market)
                    self.evaluate_merge_or_pair_sell(market)

                self.cancel_stale_orders()
                self._save_state()
            except Exception as e:
                print(f"[MARKET-HUNTER][ERRO] {e}")
            time.sleep(max(1, int(self.args.loop_seconds)))


def main():
    parser = argparse.ArgumentParser(
        description="Dry-run de Market Making low-tick + decisao SELL vs MERGE para mercados updown."
    )
    parser.add_argument("--coins", default="btc,eth,sol,xrp")
    parser.add_argument("--timeframe", choices=["5m", "15m"], default="5m")
    parser.add_argument("--loop-seconds", type=int, default=1)
    parser.add_argument("--low-prices", default="0.01,0.02")
    parser.add_argument("--usd-per-order", type=float, default=1.0)
    parser.add_argument("--latency-ms", type=int, default=300)
    parser.add_argument("--queue-buffer-shares", type=float, default=10.0)
    parser.add_argument("--min-tte-quote-seconds", type=int, default=20)
    parser.add_argument("--take-profit-price", type=float, default=0.03)
    parser.add_argument("--min-inventory-shares", type=float, default=0.0001)
    parser.add_argument("--max-sell-shares-per-cycle", type=float, default=500.0)
    parser.add_argument("--merge-min-pairs", type=float, default=5.0)
    parser.add_argument("--merge-flat-cost-usdc", type=float, default=0.01)
    parser.add_argument("--merge-per-pair-cost-usdc", type=float, default=0.0)
    parser.add_argument("--merge-safety-usdc", type=float, default=0.01)
    parser.add_argument("--sell-when-better-than-merge", action="store_true")
    parser.add_argument("--order-stale-seconds", type=int, default=120)
    parser.add_argument("--market-trades-limit", type=int, default=400)
    parser.add_argument("--default-fee-bps", type=int, default=200)
    parser.add_argument("--save-interval-seconds", type=int, default=30)
    parser.add_argument("--max-runtime-seconds", type=int, default=0)
    parser.add_argument("--log-file", default="logs/market_hunter_events.csv")
    parser.add_argument("--state-file", default="logs/market_hunter_state.json")
    args = parser.parse_args()

    hunter = MarketHunter(args)
    hunter.run()


if __name__ == "__main__":
    main()
