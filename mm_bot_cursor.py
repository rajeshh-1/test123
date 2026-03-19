import argparse
import asyncio
import csv
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

try:
    import websockets  # type: ignore
except ImportError:  # pragma: no cover - o usuário deve instalar websockets
    websockets = None  # type: ignore


GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/"
CLOB_HTTP_HOST = "https://clob.polymarket.com"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_slugs(raw: str) -> List[str]:
    """
    Aceita uma lista separada por vírgula de slugs ou URLs do Polymarket e retorna slugs.
    Ex.: 'https://polymarket.com/sports/nba/nba-por-mem-2026-03-04' -> 'nba-por-mem-2026-03-04'
    """
    if not raw:
        return []
    items = [x.strip() for x in raw.split(",") if x.strip()]
    out: List[str] = []
    for it in items:
        if "polymarket.com" in it:
            it = it.split("?")[0].rstrip("/")
            it = it.split("/")[-1]
        out.append(it)
    # dedupe preservando ordem
    seen = set()
    deduped: List[str] = []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


@dataclass
class Config:
    markets: List[str]
    slugs: List[str]
    bankroll: float
    kelly_risk_fraction: float
    max_per_side_usd: float
    max_exposure_global_frac: float
    max_exposure_per_market_frac: float
    size_cutoff_usd: float
    latency_ms: int
    scan_interval_s: int
    cancel_before_minutes: int
    log_file: str
    rebate_fraction_sports: float = 0.2
    fee_rate_sports: float = 0.0175
    fee_exp_sports: int = 1


@dataclass
class MMMarket:
    market_id: str
    title: str
    token_id: str
    outcome: str
    tag: str
    resolution_ts: float


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class Quotes:
    bid: float
    ask: float
    half_spread: float


@dataclass
class OpenOrder:
    market: MMMarket
    side: str  # "BID" ou "ASK"
    price: float
    size_shares: float
    size_usd: float
    posted_at: float
    size_ahead: float = 0.0


@dataclass
class FillResult:
    market: MMMarket
    side: str
    fill_size_shares: float
    fill_price: float
    timestamp: float
    posted_price: float


@dataclass
class MarketState:
    market: MMMarket
    bids: List[BookLevel] = field(default_factory=list)
    asks: List[BookLevel] = field(default_factory=list)
    mid_history: deque = field(default_factory=lambda: deque(maxlen=200))
    mid_ts_history: deque = field(default_factory=lambda: deque(maxlen=200))
    inventory_shares: float = 0.0
    volume_bid_usd: float = 0.0
    volume_ask_usd: float = 0.0
    last_book_update: float = 0.0

    def apply_delta(self, delta: dict) -> None:
        """Atualiza o livro L2 a partir de um delta do WebSocket."""
        # Formato esperado: {"type":"l2_update","bids":[...],"asks":[...]} ou snapshot inicial
        bids = delta.get("bids")
        asks = delta.get("asks")

        if isinstance(bids, list):
            new_bids: List[BookLevel] = []
            for b in bids:
                try:
                    price = float(b[0] if isinstance(b, list) else b.get("price"))
                    size = float(b[1] if isinstance(b, list) else b.get("size"))
                except Exception:
                    continue
                if size <= 0:
                    continue
                new_bids.append(BookLevel(price=price, size=size))
            new_bids.sort(key=lambda x: x.price, reverse=True)
            self.bids = new_bids

        if isinstance(asks, list):
            new_asks: List[BookLevel] = []
            for a in asks:
                try:
                    price = float(a[0] if isinstance(a, list) else a.get("price"))
                    size = float(a[1] if isinstance(a, list) else a.get("size"))
                except Exception:
                    continue
                if size <= 0:
                    continue
                new_asks.append(BookLevel(price=price, size=size))
            new_asks.sort(key=lambda x: x.price)
            self.asks = new_asks

        self.last_book_update = time.time()

    def best_mid(self, size_cutoff_usd: float) -> Optional[float]:
        """Midpoint ajustado ignorando 'poeira' menor que size_cutoff_usd."""
        best_bid = None
        for b in self.bids:
            if b.size * b.price >= size_cutoff_usd:
                best_bid = b.price
                break
        best_ask = None
        for a in self.asks:
            if a.size * a.price >= size_cutoff_usd:
                best_ask = a.price
                break
        if best_bid is None or best_ask is None:
            return None
        mid = (best_bid + best_ask) / 2.0
        self.mid_history.append(mid)
        self.mid_ts_history.append(time.time())
        return mid

    def volatility_60s(self, size_cutoff_usd: float) -> float:
        """Desvio padrão dos mids ajustados nos últimos ~60s."""
        now = time.time()
        # filtra amostras mais velhas que 60s
        while self.mid_ts_history and now - self.mid_ts_history[0] > 60:
            self.mid_ts_history.popleft()
            self.mid_history.popleft()
        if len(self.mid_history) < 5:
            return 0.0
        m = sum(self.mid_history) / len(self.mid_history)
        var = sum((x - m) ** 2 for x in self.mid_history) / max(len(self.mid_history) - 1, 1)
        return var ** 0.5

    def volume_at_price(self, side: str, price: float) -> float:
        levels = self.bids if side == "BID" else self.asks
        for lvl in levels:
            if abs(lvl.price - price) < 1e-9:
                return lvl.size
        return 0.0


class MarketScanner:
    def __init__(self, session: requests.Session, config: Config) -> None:
        self.session = session
        self.config = config

    def _is_sports_title(self, title: str) -> Optional[str]:
        t = title.lower()
        if "cs2" in t or "counter-strike" in t or "esl" in t:
            return "cs2"
        if "nba" in t or "basketball" in t:
            return "nba"
        if "soccer" in t or "football" in t or "serie a" in t or "premier league" in t:
            return "soccer"
        return None

    def _infer_sport(self, ev: dict, market: dict) -> Optional[str]:
        """
        Tenta identificar CS2 / NBA / Soccer combinando tags do Polymarket e título.
        Isso evita depender só de palavras exatas no título.
        """
        title = str(market.get("question") or ev.get("title") or "").lower()
        raw_tags = ev.get("tags") or market.get("tags") or []
        if isinstance(raw_tags, str):
            tags = [raw_tags]
        else:
            tags = [str(t) for t in raw_tags]

        # Esports (inclui CS2, LoL, etc). Refinamos por título para pegar CS2.
        if any(t in tags for t in ("64", "65", "66", "89", "90", "1000000", "esports")):
            if "cs2" in title or "counter-strike" in title or "cs:go" in title:
                return "cs2"

        # Basketball / NBA
        if "4" in tags:
            if "nba" in title or "basketball" in title:
                return "nba"

        # Vários IDs conhecidos de futebol/soccer no Polymarket
        if any(t in tags for t in ("1", "2", "3", "5", "6", "7", "9", "26", "28", "30", "33", "sports")):
            if "soccer" in title or "football" in title:
                return "soccer"

        # Fallback: heurística antiga baseada só no título
        return self._is_sports_title(title)

    def scan(self) -> List[MMMarket]:
        markets: List[MMMarket] = []
        # Se o usuário passou slugs (URLs), usamos isso para evitar depender de tags/paginação.
        events: List[dict] = []
        if self.config.slugs:
            for slug in self.config.slugs:
                try:
                    resp = self.session.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=20)
                    if resp.status_code != 200:
                        print(f"[MM-BOT][SCAN] slug {slug}: status {resp.status_code}")
                        continue
                    data = resp.json()
                    if data:
                        events.append(data[0])
                except Exception as e:
                    print(f"[MM-BOT][SCAN] erro ao buscar slug {slug}: {e}")
            print(f"[MM-BOT][SCAN] eventos carregados por slug: {len(events)}")
        else:
            # Fallback: busca por tags esportivas/esports em lote.
            sports_tags = [
                "1",
                "2",
                "3",
                "4",
                "5",
                "6",
                "7",
                "9",
                "26",
                "28",
                "30",
                "33",
                "64",
                "65",
                "66",
                "89",
                "90",
                "1000000",
                "esports",
                "sports",
            ]

            all_events: Dict[str, dict] = {}
            total_raw = 0
            for tag in sports_tags:
                try:
                    params = {
                        "active": "true",
                        "closed": "false",
                        "limit": 1000,
                        "tag_id": tag,
                    }
                    resp = self.session.get(f"{GAMMA_HOST}/events", params=params, timeout=15)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    evs = data if isinstance(data, list) else data.get("events", [])
                    total_raw += len(evs)
                    for ev in evs:
                        ev_id = str(ev.get("id") or ev.get("conditionId") or ev.get("slug") or "")
                        if not ev_id:
                            continue
                        # evita duplicar o mesmo evento vindo por múltiplas tags
                        if ev_id not in all_events:
                            all_events[ev_id] = ev
                except Exception as e:
                    print(f"[MM-BOT][SCAN] erro ao chamar Gamma API com tag {tag}: {e}")
                    continue

            events = list(all_events.values())
            print(
                f"[MM-BOT][SCAN] eventos brutos recebidos (todas tags esportivas): "
                f"{total_raw} | únicos após dedupe: {len(events)}"
            )

        now_ts = time.time()
        for ev in events:
            for m in ev.get("markets", []):
                question = str(m.get("question") or ev.get("title") or "").strip()
                tag = self._infer_sport(ev, m)
                if not tag:
                    # Mercado ignorado por não ser CS2/NBA/Soccer
                    continue
                if tag not in self.config.markets:
                    continue
                if not m.get("acceptingOrders", True) or m.get("closed", False):
                    continue
                end_raw = m.get("endDate") or ev.get("endDate")
                if not end_raw:
                    continue
                try:
                    end_ts = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                tte = end_ts - now_ts
                # Jogos ao vivo podem ter tte < 1h. Mantemos apenas o teto.
                if tte < 0 or tte > 48 * 3600:
                    continue
                clobs = m.get("clobTokenIds") or []
                if isinstance(clobs, str):
                    try:
                        clobs = json.loads(clobs)
                    except Exception:
                        clobs = []
                if not isinstance(clobs, list) or len(clobs) < 2:
                    continue
                outcomes = m.get("outcomes") or []
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = []
                if not isinstance(outcomes, list) or len(outcomes) < 2:
                    continue

                # Para manter o bot simples (dry run), market-make apenas no 1º token (outcome[0]).
                token_id = str(clobs[0])
                outcome = str(outcomes[0])
                markets.append(
                    MMMarket(
                        market_id=str(m.get("conditionId") or m.get("id")),
                        title=question,
                        token_id=token_id,
                        outcome=outcome,
                        tag=tag,
                        resolution_ts=end_ts,
                    )
                )
        print(f"[MM-BOT][SCAN] mercados elegíveis encontrados: {len(markets)}")
        return markets


def optimal_half_spread(mid: float, volatility: float, v_max: float = 0.05) -> float:
    """Proxy heurístico para o spread ótimo: cobre ~1.5x vol sem afastar demais."""
    s_min = max(0.01, 1.5 * volatility)
    s_max = min(v_max, 0.05)
    hs = min(s_min, s_max)
    return round(hs, 2)


def get_quotes(state: MarketState, config: Config) -> Optional[Quotes]:
    mid = state.best_mid(config.size_cutoff_usd)
    if mid is None:
        return None
    if mid < 0.02 or mid > 0.98:
        return None
    # Evita o corredor de maior fee (meio da curva)
    if 0.45 <= mid <= 0.55:
        return None
    vol = state.volatility_60s(config.size_cutoff_usd)
    hs = optimal_half_spread(mid, vol)
    bid = round(max(0.01, mid - hs), 2)
    ask = round(min(0.99, mid + hs), 2)
    return Quotes(bid=bid, ask=ask, half_spread=hs)


def skew_quotes(quotes: Quotes, state: MarketState, config: Config) -> Quotes:
    mid = state.best_mid(config.size_cutoff_usd)
    if mid is None:
        return quotes
    # Inventário positivo = long no token; negativo = short (simulado).
    inv_usdc = state.inventory_shares * mid
    imbalance = inv_usdc
    total = abs(inv_usdc) + 1e-3
    skew_factor = min(0.5, abs(imbalance) / total)
    skew = skew_factor * quotes.half_spread
    if imbalance > 0:
        # Muito long → empurra quotes para baixo (facilita vender / reduz compras)
        return Quotes(
            bid=round(max(0.01, quotes.bid - skew), 2),
            ask=round(max(quotes.bid, quotes.ask - skew), 2),
            half_spread=quotes.half_spread,
        )
    if imbalance < 0:
        # Muito short → empurra para cima (facilita recomprar / reduz vendas)
        return Quotes(
            bid=round(min(quotes.ask, quotes.bid + skew), 2),
            ask=round(min(0.99, quotes.ask + skew), 2),
            half_spread=quotes.half_spread,
        )
    return quotes


def q_min_value(state: MarketState, config: Config) -> float:
    return min(state.volume_bid_usd, state.volume_ask_usd)


@dataclass
class RiskManager:
    config: Config
    exposure_global_usd: float = 0.0
    exposure_per_market: Dict[str, float] = field(default_factory=dict)

    def size_usd_for_order(self, market_state: MarketState) -> float:
        base = self.config.bankroll * self.config.kelly_risk_fraction * 0.25
        base = min(base, self.config.max_per_side_usd)
        market_id = market_state.market.market_id
        market_expo = self.exposure_per_market.get(market_id, 0.0)
        if self.exposure_global_usd + base > self.config.bankroll * self.config.max_exposure_global_frac:
            return 0.0
        if market_expo + base > self.config.bankroll * self.config.max_exposure_per_market_frac:
            return 0.0
        return base

    def apply_fill(self, fill: FillResult) -> None:
        delta = fill.fill_size_shares * fill.fill_price
        self.exposure_global_usd += delta
        mid = fill.fill_price
        market_id = fill.market.market_id
        self.exposure_per_market[market_id] = self.exposure_per_market.get(market_id, 0.0) + delta


class DryRunOrderManager:
    def __init__(self, config: Config, risk_manager: RiskManager) -> None:
        self.config = config
        self.risk_manager = risk_manager
        self.open_orders: List[OpenOrder] = []

    def post_or_refresh(self, state: MarketState, quotes: Quotes) -> None:
        now = time.time()
        # Cancela ordens antigas
        new_open: List[OpenOrder] = []
        for o in self.open_orders:
            tte = state.market.resolution_ts - now
            age = now - o.posted_at
            if tte < self.config.cancel_before_minutes * 60 or age > 300:
                continue
            if o.market.market_id == state.market.market_id and o.side == "BID":
                continue
            if o.market.market_id == state.market.market_id and o.side == "ASK":
                continue
            new_open.append(o)
        self.open_orders = new_open
        # Como estamos repostando "1x bid + 1x ask" por mercado, tratamos volume atual como o tamanho das ordens ativas.
        state.volume_bid_usd = 0.0
        state.volume_ask_usd = 0.0

        size_usd = self.risk_manager.size_usd_for_order(state)
        if size_usd <= 0:
            return
        mid = state.best_mid(self.config.size_cutoff_usd)
        if mid is None or mid <= 0:
            return
        bid_shares = size_usd / max(quotes.bid, 1e-6)
        ask_shares = size_usd / max(quotes.ask, 1e-6)

        bid_order = OpenOrder(
            market=state.market,
            side="BID",
            price=quotes.bid,
            size_shares=bid_shares,
            size_usd=size_usd,
            posted_at=now,
            size_ahead=state.volume_at_price("BID", quotes.bid),
        )
        ask_order = OpenOrder(
            market=state.market,
            side="ASK",
            price=quotes.ask,
            size_shares=ask_shares,
            size_usd=size_usd,
            posted_at=now,
            size_ahead=state.volume_at_price("ASK", quotes.ask),
        )
        state.volume_bid_usd = size_usd
        state.volume_ask_usd = size_usd
        self.open_orders.append(bid_order)
        self.open_orders.append(ask_order)

    def simulate_fills(self, state: MarketState) -> List[FillResult]:
        now = time.time()
        fills: List[FillResult] = []
        remaining_orders: List[OpenOrder] = []
        for o in self.open_orders:
            # simplicidade: aplica latência mínima antes de considerar fill
            if now - o.posted_at < self.config.latency_ms / 1000.0:
                remaining_orders.append(o)
                continue
            if o.market.market_id != state.market.market_id:
                remaining_orders.append(o)
                continue

            if o.side == "BID":
                # procura asks que cruza nossa bid
                filled = False
                for a in state.asks:
                    if a.price <= o.price and a.size > o.size_ahead:
                        fill_size = min(o.size_shares, a.size - o.size_ahead)
                        if fill_size <= 0:
                            break
                        fills.append(
                            FillResult(
                                market=o.market,
                                side=o.side,
                                fill_size_shares=fill_size,
                                fill_price=a.price,
                                timestamp=now,
                                posted_price=o.price,
                            )
                        )
                        # Atualiza inventário (comprou)
                        state.inventory_shares += fill_size
                        state.volume_bid_usd = 0.0
                        self.risk_manager.apply_fill(fills[-1])
                        filled = True
                        break
                if not filled:
                    remaining_orders.append(o)
            elif o.side == "ASK":
                filled = False
                for b in state.bids:
                    if b.price >= o.price and b.size > o.size_ahead:
                        fill_size = min(o.size_shares, b.size - o.size_ahead)
                        if fill_size <= 0:
                            break
                        fills.append(
                            FillResult(
                                market=o.market,
                                side=o.side,
                                fill_size_shares=fill_size,
                                fill_price=b.price,
                                timestamp=now,
                                posted_price=o.price,
                            )
                        )
                        # Atualiza inventário (vendeu)
                        state.inventory_shares -= fill_size
                        state.volume_ask_usd = 0.0
                        self.risk_manager.apply_fill(fills[-1])
                        filled = True
                        break
                if not filled:
                    remaining_orders.append(o)
        self.open_orders = remaining_orders
        return fills


class PnLTracker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.spread_captured_usd: float = 0.0
        self.rebates_estimated_usd: float = 0.0
        self.fees_estimated_usd: float = 0.0
        self.realized_pnl_usd: float = 0.0
        self.log_file = config.log_file
        self._csv_fields = [
            "timestamp_utc",
            "market_id",
            "market_title",
            "tag",
            "side",
            "posted_price",
            "fill_price",
            "size_shares",
            "size_usd",
            "mid_at_fill",
            "volatility_60s",
            "half_spread_used",
            "inventory_skew_value",
            "q_min_value",
            "spread_captured_usd",
            "fee_est_usd",
            "rebate_est_usd",
            "pnl_total_usd",
        ]

    def estimate_fee_sports(self, shares: float, price: float) -> float:
        p = max(0.0001, min(0.9999, price))
        fee_rate = self.config.fee_rate_sports
        exp = self.config.fee_exp_sports
        return shares * p * fee_rate * ((p * (1.0 - p)) ** exp)

    def apply_fills(self, state: MarketState, fills: List[FillResult], quotes: Optional[Quotes]) -> None:
        mid = state.best_mid(self.config.size_cutoff_usd)
        vol = state.volatility_60s(self.config.size_cutoff_usd)
        qmin = q_min_value(state, self.config)
        skew_val = 0.0
        if mid is not None:
            inv_usdc = state.inventory_shares * mid
            total = abs(inv_usdc) + 1e-3
            skew_val = inv_usdc / total

        for f in fills:
            fee = self.estimate_fee_sports(f.fill_size_shares, f.fill_price)
            rebate = self.config.rebate_fraction_sports * fee
            spread = 0.0
            if f.side == "BID":
                spread = max(0.0, (quotes.ask if quotes else f.fill_price) - f.fill_price)
            elif f.side == "ASK":
                spread = max(0.0, f.fill_price - (quotes.bid if quotes else f.fill_price))
            spread_usd = spread * f.fill_size_shares
            self.spread_captured_usd += spread_usd
            self.fees_estimated_usd += fee
            self.rebates_estimated_usd += rebate
            self.realized_pnl_usd = self.spread_captured_usd + self.rebates_estimated_usd - self.fees_estimated_usd

            self._log_fill(
                state,
                f,
                mid_at_fill=mid or 0.0,
                vol_60s=vol,
                half_spread_used=quotes.half_spread if quotes else 0.0,
                inventory_skew_value=skew_val,
                q_min=qmin,
                spread_captured_usd=spread_usd,
                fee_est_usd=fee,
                rebate_est_usd=rebate,
                pnl_total_usd=self.realized_pnl_usd,
            )

    def _log_fill(
        self,
        state: MarketState,
        fill: FillResult,
        mid_at_fill: float,
        vol_60s: float,
        half_spread_used: float,
        inventory_skew_value: float,
        q_min: float,
        spread_captured_usd: float,
        fee_est_usd: float,
        rebate_est_usd: float,
        pnl_total_usd: float,
    ) -> None:
        row = {
            "timestamp_utc": utc_now_iso(),
            "market_id": state.market.market_id,
            "market_title": state.market.title,
            "tag": state.market.tag,
            "side": fill.side,
            "posted_price": fill.posted_price,
            "fill_price": fill.fill_price,
            "size_shares": round(fill.fill_size_shares, 8),
            "size_usd": round(fill.fill_size_shares * fill.fill_price, 8),
            "mid_at_fill": round(mid_at_fill, 6),
            "volatility_60s": round(vol_60s, 6),
            "half_spread_used": half_spread_used,
            "inventory_skew_value": round(inventory_skew_value, 6),
            "q_min_value": round(q_min, 6),
            "spread_captured_usd": round(spread_captured_usd, 8),
            "fee_est_usd": round(fee_est_usd, 8),
            "rebate_est_usd": round(rebate_est_usd, 8),
            "pnl_total_usd": round(pnl_total_usd, 8),
        }
        exists = False
        try:
            with open(self.log_file, "r", encoding="utf-8") as _:
                exists = True
        except FileNotFoundError:
            exists = False
        with open(self.log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._csv_fields, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(row)


async def ws_orderbook_listener(state: MarketState) -> None:
    if websockets is None:
        print("[MM-BOT] Biblioteca 'websockets' não instalada. WebSocket desativado (sem livro em tempo real).")
        return
    backoff = 1
    while True:
        try:
            async with websockets.connect(CLOB_WS_URL) as ws:  # type: ignore
                sub_msg = {
                    "type": "subscribe",
                    "channel": "book",
                    "assets_ids": [state.market.token_id],
                }
                await ws.send(json.dumps(sub_msg))
                async for msg in ws:
                    data = json.loads(msg)
                    book = data.get("data") or data
                    state.apply_delta(book)
        except Exception as e:
            print(f"[MM-BOT][WS] erro {e}, reconectando em {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def market_scan_loop(
    scanner: MarketScanner,
    market_states: Dict[str, MarketState],
    config: Config,
) -> None:
    while True:
        try:
            markets = scanner.scan()
            # Garante limite máximo de mercados
            markets = markets[:5]
            if not markets:
                print("[MM-BOT][SCAN] nenhum mercado CS2/NBA/Soccer elegível encontrado nesta varredura.")
            for m in markets:
                if m.market_id not in market_states:
                    market_states[m.market_id] = MarketState(market=m)
                    print(
                        f"[MM-BOT][SCAN] novo mercado adicionado: {m.market_id} | {m.tag.upper()} | "
                        f"{m.title[:80]}... | outcome='{m.outcome}' | token_id={m.token_id}"
                    )
                    asyncio.create_task(ws_orderbook_listener(market_states[m.market_id]))
            # Remove mercados expirados
            now = time.time()
            for mid in list(market_states.keys()):
                if market_states[mid].market.resolution_ts < now:
                    del market_states[mid]
        except Exception as e:
            print(f"[MM-BOT][SCAN] erro {e}")
        await asyncio.sleep(config.scan_interval_s)


async def quote_and_fill_loop(
    market_states: Dict[str, MarketState],
    config: Config,
    order_manager: DryRunOrderManager,
    pnl_tracker: PnLTracker,
) -> None:
    last_log = 0.0
    while True:
        try:
            for state in list(market_states.values()):
                quotes = get_quotes(state, config)
                if quotes is None:
                    continue
                quotes = skew_quotes(quotes, state, config)
                order_manager.post_or_refresh(state, quotes)
                fills = order_manager.simulate_fills(state)
                if fills:
                    pnl_tracker.apply_fills(state, fills, quotes)
                    # Log compacto quando houver fills
                    for f in fills:
                        print(
                            f"[MM-BOT][FILL] {state.market.tag.upper()} {state.market.market_id} "
                            f"{f.side} size={f.fill_size_shares:.4f} price={f.fill_price:.2f}"
                        )
            # Log de heartbeat a cada ~30s
            now = time.time()
            if now - last_log > 30:
                last_log = now
                print(
                    f"[MM-BOT][HEARTBEAT] mercados ativos={len(market_states)} | "
                    f"ordens abertas={len(order_manager.open_orders)}"
                )
        except Exception as e:
            print(f"[MM-BOT][QUOTES] erro {e}")
        await asyncio.sleep(1.0)


async def main_async(args: argparse.Namespace) -> None:
    config = Config(
        markets=[m.strip().lower() for m in args.markets.split(",") if m.strip()],
        slugs=normalize_slugs(args.slugs),
        bankroll=float(args.bankroll),
        kelly_risk_fraction=0.25,
        max_per_side_usd=10.0,
        max_exposure_global_frac=0.2,
        max_exposure_per_market_frac=0.05,
        size_cutoff_usd=5.0,
        latency_ms=int(args.latency_ms),
        scan_interval_s=int(args.scan_interval),
        cancel_before_minutes=int(args.cancel_before_minutes),
        log_file=args.log_file,
    )
    session = requests.Session()
    scanner = MarketScanner(session, config)
    market_states: Dict[str, MarketState] = {}
    risk_manager = RiskManager(config=config)
    order_manager = DryRunOrderManager(config=config, risk_manager=risk_manager)
    pnl_tracker = PnLTracker(config=config)

    await asyncio.gather(
        market_scan_loop(scanner, market_states, config),
        quote_and_fill_loop(market_states, config, order_manager, pnl_tracker),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run de Market Making em mercados CS2/NBA/Soccer do Polymarket.")
    parser.add_argument("--markets", default="cs2,nba,soccer")
    parser.add_argument(
        "--slugs",
        default="",
        help="Slugs ou URLs do Polymarket (separados por vírgula). Ex.: nba-por-mem-2026-03-04",
    )
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--scan-interval", type=int, default=60)
    parser.add_argument("--cancel-before-minutes", type=int, default=45)
    parser.add_argument("--latency-ms", type=int, default=120)
    parser.add_argument("--log-file", default="mm_dryrun_log.csv")
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[MM-BOT] Encerrado pelo usuário.")


if __name__ == "__main__":
    main()

