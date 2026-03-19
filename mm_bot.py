#!/usr/bin/env python3
"""
mm_bot.py — Market Making Bot para Polymarket (DRY RUN)
========================================================
Arquitetura: asyncio event-driven
Mercados:    CS2, NBA, Futebol (pre-jogo)
Capital:     $100 (Kelly 0.25x, max $10/lado/mercado)

Componentes:
  1. MarketScanner          — Gamma API REST, busca mercados elegíveis
  2. AsyncOrderbookListener — WebSocket L2 book em tempo real
  3. DynamicQuoteCalculator — spread = max(0.01, 1.5 * volatility_60s)
  4. InventoryManager       — Skewing + garantia Q_min + VAR 20%
  5. FIFODryRunEngine       — fill simulado: size_ahead t=0, check t=120ms
  6. PnLLogger              — CSV com spread_captured, rebate_est, etc.

Uso:
  python mm_bot.py --markets cs2,nba,soccer --max-markets 5
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

# ── Constantes ────────────────────────────────────────────────────────────────
GAMMA_HOST         = "https://gamma-api.polymarket.com"
CLOB_HOST          = "https://clob.polymarket.com"
WS_URL             = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CHAIN_ID           = 137
TICK_SIZE          = 0.01

MIN_VOLUME_24H     = 5_000   # USDC (conforme regras: >$5k)
MIN_TTL_HOURS      = 1.0     # 1h (conforme regras: 1h-48h)
MAX_TTL_HOURS      = 48.0    # 48h (conforme regras)
CANCEL_BEFORE_MIN  = 45      # cancela tudo N minutos antes do evento
VOLATILITY_WINDOW  = 60      # segundos para calculo de vol
MID_DRIFT_THRESH   = 0.005   # requota se mid mover > 0.5 centavos

# Keywords para identificar categoria pelo slug
MARKET_TAGS: Dict[str, List[str]] = {
    "cs2":    ["cs2", "counter-strike", "csgo", "esl", "pala"],
    "nba":    ["nba", "basketball", "warriors", "lakers", "celtics", "ncaa"],
    "soccer": ["soccer", "epl", "premier", "serie-a", "laliga", "bundesliga",
               "ucl", "champions", "copa", "mls", "futebol", "betis", "madrid", "barca"],
    "crypto": ["crypto", "bitcoin", "eth", "solana", "price-of"],
    "politics": ["politics", "trump", "biden", "white-house", "election"],
}


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class BookEntry:
    price: float
    size:  float


@dataclass
class MMMarket:
    condition_id: str
    question:     str
    slug:         str
    category:     str   # cs2 | nba | soccer
    token_yes:    str
    token_no:     str
    end_time:     float  # unix timestamp
    volume_24h:   float


@dataclass
class Quotes:
    bid:         float
    ask:         float
    half_spread: float


@dataclass
class OpenOrder:
    order_id:    str
    side:        str    # BID | ASK
    price:       float
    size_shares: float
    size_usdc:   float
    token_id:    str
    posted_at:   float  # unix ts
    size_ahead:  float = 0.0   # FIFO snapshot no momento do post (t=0)
    active_at:   float = 0.0   # unix ts em que a latencia de rede já passou
                               # = posted_at + latency_ms/1000
                               # antes desse momento a ordem ainda esta "viajando"


@dataclass
class FillResult:
    size:  float
    price: float


# ── MarketState ────────────────────────────────────────────────────────────────

class MarketState:
    """
    Mantém o L2 book local atualizado por deltas do WebSocket.
    Mutable por referência — o ws_listener e simulate_fill compartilham o mesmo objeto.
    """

    def __init__(self, market: MMMarket):
        self.market      = market
        self.bids:        List[BookEntry] = []
        self.asks:        List[BookEntry] = []
        self.mid_history: deque          = deque(maxlen=300)
        self.last_mid:    Optional[float] = None
        self.posted_mid:  Optional[float] = None
        self.open_orders: List[OpenOrder] = []
        self.lock         = asyncio.Lock()
        self.ws_connected = False

    # ── Book management ───────────────────────────────────────────────────────

    def apply_snapshot(self, bids: list, asks: list) -> None:
        self.bids = sorted(
            [BookEntry(float(b["price"]), float(b["size"]))
             for b in bids if float(b.get("size", 0)) > 0],
            key=lambda x: -x.price,
        )
        self.asks = sorted(
            [BookEntry(float(a["price"]), float(a["size"]))
             for a in asks if float(a.get("size", 0)) > 0],
            key=lambda x: x.price,
        )
        self._update_mid()

    def apply_delta(self, changes: list, side: str) -> None:
        book = self.bids if side.upper() in ("BUY", "BID") else self.asks
        for c in changes:
            price = float(c["price"])
            size  = float(c["size"])
            book[:] = [e for e in book if abs(e.price - price) > 1e-6]
            if size > 0:
                book.append(BookEntry(price, size))
        if side.upper() in ("BUY", "BID"):
            self.bids.sort(key=lambda x: -x.price)
        else:
            self.asks.sort(key=lambda x: x.price)
        self._update_mid()

    def _update_mid(self) -> None:
        if self.bids and self.asks:
            mid = (self.bids[0].price + self.asks[0].price) / 2.0
            self.last_mid = mid
            self.mid_history.append((time.time(), mid))

    # ── Derived metrics ───────────────────────────────────────────────────────

    def best_mid(self) -> Optional[float]:
        return self.last_mid

    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def volatility_60s(self) -> float:
        """Desvio padrao do mid nos ultimos 60 segundos."""
        now    = time.time()
        recent = [m for t, m in self.mid_history if now - t <= VOLATILITY_WINDOW]
        if len(recent) < 3:
            return 0.02   # default 2 cents sem dados suficientes
        mean = sum(recent) / len(recent)
        variance = sum((m - mean) ** 2 for m in recent) / len(recent)
        return math.sqrt(variance)

    def volume_at_price(self, side: str, price: float) -> float:
        """Volume ja na fila neste nivel (snapshot FIFO t=0)."""
        book = self.bids if side == "BID" else self.asks
        for e in book:
            if abs(e.price - price) < 1e-4:
                return e.size
        return 0.0

    def mid_drifted(self) -> bool:
        if self.posted_mid is None or self.last_mid is None:
            return True
        return abs(self.last_mid - self.posted_mid) > MID_DRIFT_THRESH


# ── DynamicQuoteCalculator ────────────────────────────────────────────────────

class DynamicQuoteCalculator:
    """
    Spread dinamico baseado na Quadratic Spread Function:
        S(v, s) = ((v - s) / v)^2 * b
    Maximizar S -> minimizar s, sujeito a s >= 1.5 * vol (cobre selecao adversa).
    """

    def __init__(self, v_max: float = 0.05):
        self.v_max = v_max   # tolerancia maxima da plataforma (5 centavos)

    def optimal_half_spread(self, volatility: float) -> float:
        s_min = max(TICK_SIZE, 1.5 * volatility)
        s_max = min(self.v_max, 0.05)
        return round(min(max(s_min, TICK_SIZE), s_max), 2)

    def get_quotes(self, state: MarketState) -> Optional[Quotes]:
        mid = state.best_mid()
        if mid is None:
            return None
        if mid < 0.04 or mid > 0.96:
            return None
        hs  = self.optimal_half_spread(state.volatility_60s)
        bid = round(max(TICK_SIZE, mid - hs), 2)
        ask = round(min(1.0 - TICK_SIZE, mid + hs), 2)
        if bid >= ask:
            return None
        return Quotes(bid=bid, ask=ask, half_spread=hs)


# ── InventoryManager ─────────────────────────────────────────────────────────

class InventoryManager:
    """
    Kelly fracionario + Inventory Skewing + garantia Q_min.
    Regra de Rebate: Q_epoch = min(Q_bid, Q_ask) -> sempre postar os dois lados.
    """

    def __init__(self, bankroll: float = 100.0,
                 kelly_fraction: float = 0.25,
                 max_per_side: float = 10.0):
        self.bankroll       = bankroll
        self.kelly_fraction = kelly_fraction
        self.max_per_side   = max_per_side
        self.long_usdc:  Dict[str, float] = {}
        self.short_usdc: Dict[str, float] = {}

    def skew_quotes(self, quotes: Quotes, token_id: str) -> Quotes:
        """
        Desloca os quotes proporcionalmente ao desequilibrio de inventario.
        Muito long (YES) -> empurra quotes para baixo (facilita vender YES).
        Muito short      -> empurra quotes para cima (facilita comprar YES).
        """
        long  = self.long_usdc.get(token_id, 0.0)
        short = self.short_usdc.get(token_id, 0.0)
        total = long + short + 1e-6
        imbalance   = long - short
        skew_factor = min(0.5, abs(imbalance) / total)
        skew        = round(skew_factor * quotes.half_spread, 2)

        if imbalance > 0:
            return Quotes(
                bid=round(max(TICK_SIZE, quotes.bid - skew), 2),
                ask=round(max(TICK_SIZE, quotes.ask - skew), 2),
                half_spread=quotes.half_spread,
            )
        elif imbalance < 0:
            return Quotes(
                bid=round(min(0.99, quotes.bid + skew), 2),
                ask=round(min(0.99, quotes.ask + skew), 2),
                half_spread=quotes.half_spread,
            )
        return quotes

    def q_min_ok(self, token_id: str) -> bool:
        """Q_min = min(Q_bid, Q_ask) > 0 — exigencia do rebate."""
        return (self.long_usdc.get(token_id, 0.0) > 0
                and self.short_usdc.get(token_id, 0.0) > 0)

    def sizes_usdc(self, token_id: str) -> Tuple[float, float]:
        """Retorna (bid_size, ask_size) independentes baseados no inventario."""
        long  = self.long_usdc.get(token_id, 0.0)
        short = self.short_usdc.get(token_id, 0.0)
        
        kelly_size = round(self.bankroll * self.kelly_fraction * 0.25, 2)
        
        bid_size = round(max(0.0, min(kelly_size, self.max_per_side - long)), 2)
        ask_size = round(max(0.0, min(kelly_size, self.max_per_side - short)), 2)
        
        return bid_size, ask_size

    def global_exposure(self) -> float:
        return sum(self.long_usdc.values()) + sum(self.short_usdc.values())

    def record_fill(self, side: str, token_id: str, usdc: float) -> None:
        if side == "BID":
            # Reduz short antes de aumentar long
            current_short = self.short_usdc.get(token_id, 0.0)
            if current_short > 0:
                reduction = min(current_short, usdc)
                self.short_usdc[token_id] -= reduction
                usdc -= reduction
            
            if usdc > 0:
                self.long_usdc[token_id] = self.long_usdc.get(token_id, 0.0) + usdc
        else:
            # Reduz long antes de aumentar short
            current_long = self.long_usdc.get(token_id, 0.0)
            if current_long > 0:
                reduction = min(current_long, usdc)
                self.long_usdc[token_id] -= reduction
                usdc -= reduction
            
            if usdc > 0:
                self.short_usdc[token_id] = self.short_usdc.get(token_id, 0.0) + usdc


# ── PnLLogger ─────────────────────────────────────────────────────────────────

class PnLLogger:

    FIELDS = [
        "timestamp_utc", "market", "category", "side",
        "posted_price", "fill_price", "size_usdc",
        "spread_captured_usdc", "rebate_est_usdc",
        "volatility_60s", "half_spread_used",
        "inventory_skew", "q_min_ok", "latency_ms",
    ]

    def __init__(self, path: str = "mm_dryrun_log.csv"):
        self.path = Path(path)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def log_fill(self, **kwargs) -> None:
        row = {k: kwargs.get(k, "") for k in self.FIELDS}
        # Garante que o diretório existe antes de escrever
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore").writerow(row)

    def estimate_rebate(self, category: str, size_usdc: float, price: float) -> float:
        """Estimativa do rebate de maker via estrutura de Taker Fee 2026."""
        p = max(0.01, min(0.99, price))
        if category in ("cs2", "nba", "soccer"):
            fee_rate, exponent, rebate_pct = 0.0175, 1, 0.25
        else:
            fee_rate, exponent, rebate_pct = 0.25, 2, 0.20
        shares    = size_usdc / p
        fee_total = fee_rate * p * ((p * (1 - p)) ** exponent) * shares
        return round(fee_total * rebate_pct, 6)


# ── MarketScanner ─────────────────────────────────────────────────────────────

class MarketScanner:

    def __init__(self, categories: List[str],
                 max_per_category: int = 30,
                 min_volume: Optional[float] = None,
                 extra_slugs: Optional[List[str]] = None):
        self.categories  = categories
        self.max_per_category = max_per_category
        self.min_volume  = min_volume if min_volume is not None else MIN_VOLUME_24H
        self.extra_slugs = extra_slugs or []   # slugs especificos para teste
        self.session     = self._build_session()

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        retries = Retry(total=3, backoff_factor=0.5,
                        status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods=["GET"])
        s.mount("https://", HTTPAdapter(max_retries=retries))
        return s

    def _slug_matches(self, slug: str, category: str) -> bool:
        sl = slug.lower()
        return any(kw in sl for kw in MARKET_TAGS.get(category, [category]))

    def _time_ok(self, end_date_str: str) -> Tuple[bool, float]:
        try:
            dt = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
            ts = dt.timestamp()
            hours = (ts - time.time()) / 3600
            return MIN_TTL_HOURS <= hours <= MAX_TTL_HOURS, ts
        except Exception:
            return False, 0.0

    def _parse_volume(self, market: dict) -> float:
        for key in ("volume24hr", "volume", "volumeNum"):
            v = market.get(key)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
        return 0.0

    def _parse_token_ids(self, market: dict) -> Optional[Tuple[str, str]]:
        raw = market.get("clobTokenIds", [])
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(ids, list) and len(ids) >= 2:
            return str(ids[0]), str(ids[1])
        return None

    def _fetch_by_slug(self, slug: str) -> List[dict]:
        """Busca um evento especifico pelo slug exato."""
        try:
            r = self.session.get(
                f"{GAMMA_HOST}/events",
                params={"slug": slug}, timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.warning(f"[SCAN] Slug lookup erro ({slug}): {e}")
            return []

    def _infer_category(self, slug: str) -> str:
        for category in ["cs2", "nba", "soccer"]:
            if self._slug_matches(slug, category):
                return category
        return "other"

    def scan(self) -> List[MMMarket]:
        results: Dict[str, MMMarket] = {}

        # Prioridade 1: slugs especificos passados via --slugs
        for slug in self.extra_slugs:
            if len(results) >= self.max_markets:
                break
            for event in self._fetch_by_slug(slug):
                for market in event.get("markets", []):
                    if len(results) >= self.max_markets:
                        break
                    if not market.get("acceptingOrders", False):
                        continue
                    token_ids = self._parse_token_ids(market)
                    if not token_ids:
                        continue
                    condition_id = str(market.get("conditionId", ""))
                    if not condition_id or condition_id in results:
                        continue
                    _, end_ts = self._time_ok(market.get("endDate", ""))
                    results[condition_id] = MMMarket(
                        condition_id=condition_id,
                        question=str(market.get("question", slug))[:80],
                        slug=slug.lower(),
                        category=self._infer_category(slug),
                        token_yes=token_ids[0],
                        token_no=token_ids[1],
                        end_time=end_ts if end_ts else time.time() + 3600,
                        volume_24h=self._parse_volume(market),
                    )

        # Prioridade 2: scan automatico por categoria
        cat_counts = {c: sum(1 for m in results.values() if m.category == c) for c in self.categories}
        
        try:
            resp = self.session.get(
                f"{GAMMA_HOST}/events",
                params={
                    "active":    "true",
                    "closed":    "false",
                    "limit":     1000,
                    "order":     "volume24hr",
                    "ascending": "false",
                },
                timeout=20,
            )
            resp.raise_for_status()
            data   = resp.json()
            events = data if isinstance(data, list) else data.get("events", [])
        except Exception as e:
            logging.warning(f"[SCAN] Gamma API erro: {e}")
            return list(results.values())

        for event in events:
            slug = str(event.get("slug", "")).lower()

            # Identifica categoria pelo slug
            matched_category = None
            for category in self.categories:
                if self._slug_matches(slug, category):
                    matched_category = category
                    break
            
            if matched_category is None:
                continue
            
            # Se ja atingiu o limite para esta categoria, pula o evento
            if cat_counts.get(matched_category, 0) >= self.max_per_category:
                continue

            for market in event.get("markets", []):
                if cat_counts.get(matched_category, 0) >= self.max_per_category:
                    break
                
                if market.get("closed") or not market.get("active", False):
                    continue
                if not market.get("acceptingOrders", False):
                    continue

                ok, end_ts = self._time_ok(market.get("endDate", ""))
                if not ok:
                    continue

                token_ids = self._parse_token_ids(market)
                if token_ids is None:
                    continue

                condition_id = str(market.get("conditionId", ""))
                if not condition_id or condition_id in results:
                    continue

                results[condition_id] = MMMarket(
                    condition_id=condition_id,
                    question=str(market.get("question", slug))[:80],
                    slug=slug,
                    category=matched_category,
                    token_yes=token_ids[0],
                    token_no=token_ids[1],
                    end_time=end_ts,
                    volume_24h=self._parse_volume(market),
                )
                cat_counts[matched_category] += 1

        markets = list(results.values())
        by_cat  = {c: sum(1 for m in markets if m.category == c) for c in self.categories}
        logging.info(
            f"[SCAN] {len(markets)} mercados | "
            + " ".join(f"{c}:{n}" for c, n in by_cat.items())
            + " | " + " | ".join(m.slug[:22] for m in markets[:4])
        )
        return markets



# ── MMBot ─────────────────────────────────────────────────────────────────────

class MMBot:

    def __init__(self, args: argparse.Namespace):
        self.args       = args
        self.categories = [c.strip() for c in args.markets.split(",") if c.strip()]
        extra_slugs     = [s.strip() for s in args.slugs.split(",") if s.strip()] if getattr(args, 'slugs', '') else []
        self.scanner    = MarketScanner(
            self.categories, 
            max_per_category=args.max_per_category,
            min_volume=args.min_volume,
            extra_slugs=extra_slugs
        )
        self.calculator = DynamicQuoteCalculator()
        self.inventory  = InventoryManager(
            bankroll=args.bankroll,
            max_per_side=round(args.bankroll * 0.10, 2),
        )
        self.logger      = PnLLogger(args.log_file)
        self.states:     Dict[str, MarketState] = {}
        self.stop_event  = asyncio.Event()
        self._order_ctr  = 0
        self._quotes_posted = 0   # contador acumulativo
        self._fills_sim     = 0   # contador acumulativo de fills simulados

    def _next_oid(self, side: str) -> str:
        self._order_ctr += 1
        return f"dry-{side[0]}-{self._order_ctr:05d}"

    # ── WebSocket Listener ────────────────────────────────────────────────────

    async def ws_listener(self, state: MarketState) -> None:
        """
        Conexao persistente com reconexao exponential backoff.
        state e mutavel por referencia — simulate_fill compartilha o mesmo objeto,
        recebendo o book ao vivo apos o asyncio.sleep().
        """
        if websockets is None:
            logging.error("[WS] Instale: pip install websockets")
            return

        token_id = state.market.token_yes
        backoff  = 1.0

        while not self.stop_event.is_set():
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=20, ping_timeout=10, close_timeout=5
                ) as ws:
                    await ws.send(json.dumps({
                        "assets_ids": [token_id],
                        "type":       "Market",
                    }))
                    state.ws_connected = True
                    backoff = 1.0
                    logging.info(
                        f"[WS] Conectado | {state.market.slug[:30]} "
                        f"| token={token_id[:16]}..."
                    )

                    # Bootstrap: puxa snapshot REST para ter book imediato
                    # Assim o primeiro quote sai em segundos, sem esperar delta
                    await self._bootstrap_book(state)

                    async for raw in ws:
                        if self.stop_event.is_set():
                            break
                        try:
                            data = json.loads(raw)
                            # O WS pode retornar uma msg unica ou uma lista de msgs
                            messages = data if isinstance(data, list) else [data]

                            for msg in messages:
                                etype = (msg.get("event_type") or msg.get("type", "")).lower()

                                if etype == "book":
                                    async with state.lock:
                                        state.apply_snapshot(
                                            msg.get("bids", []), msg.get("asks", [])
                                        )
                                    await self._on_book_update(state)

                                elif etype == "price_change":
                                    changes = msg.get("changes", [])
                                    buys  = [c for c in changes
                                             if str(c.get("side","")).upper() in ("BUY","BID")]
                                    sells = [c for c in changes
                                             if str(c.get("side","")).upper() in ("SELL","ASK")]
                                    async with state.lock:
                                        if buys:  state.apply_delta(buys,  "BID")
                                        if sells: state.apply_delta(sells, "ASK")
                                    await self._on_book_update(state)
                        except Exception as e:
                            logging.debug(f"[WS] Msg invalida: {e}")

            except Exception as e:
                if self.stop_event.is_set():
                    break
                state.ws_connected = False
                logging.warning(
                    f"[WS] Desconectado ({state.market.slug[:25]}): {e}. "
                    f"Reconectando em {backoff:.0f}s..."
                )
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)

    async def _bootstrap_book(self, state: MarketState) -> None:
        """Puxaa o REST snapshot do CLOB ao conectar, gera o primeiro quote imediatamente."""
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: __import__('requests').get(
                    f"{CLOB_HOST}/book",
                    params={"token_id": state.market.token_yes},
                    timeout=8,
                )
            )
            if resp.ok:
                b = resp.json()
                async with state.lock:
                    state.apply_snapshot(b.get("bids", []), b.get("asks", []))
                logging.info(
                    f"[BOOTSTRAP] {state.market.slug[:28]:30s} | "
                    f"bid={state.best_bid():.2f} mid={state.best_mid():.2f} ask={state.best_ask():.2f}"
                )
                # Forca o primeiro quote sem esperar drift
                await self._post_quotes(state)
        except Exception as e:
            logging.warning(f"[BOOTSTRAP] Erro ao buscar book REST: {e}")

    # ── Quote Logic ──────────────────────────────────────────────────────────

    async def _on_book_update(self, state: MarketState) -> None:
        """
        Chamado em cada delta WS.
        1. Checa fills para ordens ativas cujo active_at ja passou
        2. Se mid driftar, cancela/reposta quotes
        """
        await self._check_fills_for_state(state)
        if state.mid_drifted():
            await self._post_quotes(state)

    async def _check_fills_for_state(self, state: MarketState) -> None:
        """
        Percorre state.open_orders e verifica fill para cada ordem cujo
        active_at (posted_at + latency) ja passou.

        Semantica correta:
          - size_ahead: snapshot no momento do post (t=0) — imutavel
          - book (state.bids/asks): ao vivo no momento desta checagem
          - A ordem fica viva e e retestada em cada delta ate ser cancelada
        """
        now      = time.time()
        to_fill  = []

        for order in list(state.open_orders):
            # Ainda dentro da janela de latencia de rede — ordem ainda viajando
            if now < order.active_at:
                continue

            fill: Optional[FillResult] = None

            if order.side == "BID":
                # Procura asks no book ao vivo que cruzem nosso bid
                for ask in list(state.asks):
                    if ask.price <= order.price:
                        # FIFO: so preenche o que excede o que estava na frente
                        available = ask.size - order.size_ahead
                        if available > 0:
                            fill = FillResult(
                                size=min(order.size_shares, available),
                                price=ask.price,
                            )
                        break   # testa apenas o melhor nivel de preco

            elif order.side == "ASK":
                # Procura bids no book ao vivo que cruzem nosso ask
                for bid in list(state.bids):
                    if bid.price >= order.price:
                        available = bid.size - order.size_ahead
                        if available > 0:
                            fill = FillResult(
                                size=min(order.size_shares, available),
                                price=bid.price,
                            )
                        break

            if fill is not None:
                to_fill.append((order, fill))

        # Processa fills (fora do loop para evitar mutacao)
        for order, fill in to_fill:
            if order not in state.open_orders:
                continue   # ja foi cancelada entre os deltas
            state.open_orders.remove(order)
            # Calcula o skew atual para o log
            quotes_base = self.calculator.get_quotes(state)
            skew_val = 0.0
            if quotes_base:
                quotes_skewed = self.inventory.skew_quotes(quotes_base, order.token_id)
                skew_val = round(quotes_skewed.bid - quotes_base.bid, 4)
            
            await self._record_fill(order, fill, state, skew_val)

    async def _record_fill(self, order: OpenOrder, fill: FillResult,
                           state: MarketState, skew: float = 0.0) -> None:
        """Registra o fill no inventario e no CSV."""
        filled_usdc = round(fill.size * fill.price, 4)
        self.inventory.record_fill(order.side, order.token_id, filled_usdc)
        self._fills_sim += 1

        # Para calcular spread capturado precisamos saber as quotes originais.
        # Usamos o half_spread atual do calculator para estimar.
        hs = self.calculator.optimal_half_spread(state.volatility_60s)
        mid_est = state.best_mid() or fill.price
        if order.side == "BID":
            spread_captured = (mid_est + hs) - fill.price   # ask - fill
        else:
            spread_captured = fill.price - (mid_est - hs)   # fill - bid
        spread_usdc = round(spread_captured * fill.size, 4)

        rebate = self.logger.estimate_rebate(
            state.market.category, filled_usdc, fill.price
        )
        q_ok   = self.inventory.q_min_ok(order.token_id)

        self.logger.log_fill(
            timestamp_utc        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            market               = state.market.question[:50],
            category             = state.market.category,
            side                 = order.side,
            posted_price         = order.price,
            fill_price           = fill.price,
            size_usdc            = filled_usdc,
            spread_captured_usdc = spread_usdc,
            rebate_est_usdc      = rebate,
            volatility_60s       = round(state.volatility_60s, 5),
            half_spread_used     = hs,
            inventory_skew       = skew,
            q_min_ok             = q_ok,
            latency_ms           = self.args.latency_ms,
        )

        logging.info(
            f"[FILL-SIM] {state.market.slug[:25]:25s} | "
            f"{order.side} @ {fill.price:.2f} (post={order.price:.2f}) | "
            f"${filled_usdc:.2f} | spread=${spread_usdc:.4f} | "
            f"rebate~${rebate:.5f} | Qmin={'OK' if q_ok else 'LOW'}"
        )

    async def _post_quotes(self, state: MarketState) -> None:
        now = time.time()

        # Cancela ordens ativas (inclusive as que nunca foram preenchidas)
        if state.open_orders:
            logging.debug(f"[CANCEL] {len(state.open_orders)} | {state.market.slug[:25]}")
            state.open_orders.clear()

        # Verifica proxima expiracao
        if (state.market.end_time - now) < CANCEL_BEFORE_MIN * 60:
            logging.info(f"[EXPIRY] <{CANCEL_BEFORE_MIN}min: {state.market.slug[:30]}")
            return

        quotes = self.calculator.get_quotes(state)
        if quotes is None:
            return

        # Inventory skew
        skew_before  = quotes.bid
        quotes       = self.inventory.skew_quotes(quotes, state.market.token_yes)
        skew_applied = round(quotes.bid - skew_before, 4)

        # VAR global 20%
        # Se ultrapassar VAR, apenas permitimos ordens que REDUZAM a exposição existente.
        exposure = self.inventory.global_exposure()
        at_var_limit = exposure >= self.args.bankroll * 0.20
        
        if at_var_limit:
            logging.warning(f"[VAR] Exposicao global ${exposure:.2f} >= 20% — permitindo apenas ordens redutoras")

        bid_size, ask_size = self.inventory.sizes_usdc(state.market.token_yes)
        
        # Filtro VAR: Se atingiu limite, so pode comprar se estiver short, so pode vender se estiver long
        token_id = state.market.token_yes
        is_long  = self.inventory.long_usdc.get(token_id, 0.0) > 0.01
        is_short = self.inventory.short_usdc.get(token_id, 0.0) > 0.01

        if at_var_limit:
            if not is_short: bid_size = 0.0
            if not is_long:  ask_size = 0.0

        if bid_size < 1.0 and ask_size < 1.0:
            if at_var_limit:
                logging.debug(f"[VAR] Sem ordens redutoras para {state.market.slug[:25]}")
            return

        latency = self.args.latency_ms / 1000.0
        mid     = state.best_mid()
        q_ok    = self.inventory.q_min_ok(state.market.token_yes)

        # BID 
        if bid_size >= 1.0:
            bid_order = OpenOrder(
                order_id    = self._next_oid("BID"),
                side        = "BID",
                price       = quotes.bid,
                size_shares = round(bid_size / max(quotes.bid, 0.01), 2),
                size_usdc   = bid_size,
                token_id    = token_id,
                posted_at   = now,
                size_ahead  = state.volume_at_price("BID", quotes.bid),
                active_at   = now + latency,
            )
            state.open_orders.append(bid_order)
            self._quotes_posted += 1

        # ASK
        if ask_size >= 1.0:
            ask_order = OpenOrder(
                order_id    = self._next_oid("ASK"),
                side        = "ASK",
                price       = quotes.ask,
                size_shares = round(ask_size / max(quotes.ask, 0.01), 2),
                size_usdc   = ask_size,
                token_id    = token_id,
                posted_at   = now,
                size_ahead  = state.volume_at_price("ASK", quotes.ask),
                active_at   = now + latency,
            )
            state.open_orders.append(ask_order)
            self._quotes_posted += 1

        state.posted_mid = mid

        logging.info(
            f"[QUOTE] {state.market.slug[:28]:30s} | "
            f"B={quotes.bid:.2f}(${bid_size}) A={quotes.ask:.2f}(${ask_size}) | "
            f"hs={quotes.half_spread:.3f} vol={state.volatility_60s:.4f} | "
            f"skew={skew_applied:+.4f} Qmin={'OK' if q_ok else 'LOW'}"
        )
        # NOTA: nao ha mais create_task() — o fill e checado em cada _on_book_update


    # ── Market Scan Loop ──────────────────────────────────────────────────────

    async def market_scan_loop(self) -> None:
        active:        set   = set()
        rescan_event: asyncio.Event = asyncio.Event()

        while not self.stop_event.is_set():
            markets = await asyncio.to_thread(self.scanner.scan)

            for market in markets:
                if market.condition_id not in active:
                    state = MarketState(market)
                    self.states[market.condition_id] = state
                    active.add(market.condition_id)
                    asyncio.create_task(self.ws_listener(state))
                    mins = round((market.end_time - time.time()) / 60)
                    logging.info(
                        f"[NEW] {market.question[:55]} "
                        f"[{market.category.upper()}] | fecha em {mins}min"
                    )

            # Remove mercados expirados (>5min após fim)
            expired = [
                cid for cid, s in self.states.items()
                if time.time() > s.market.end_time + 300
            ]
            for cid in expired:
                logging.info(f"[EXPIRED] Removendo mercado expirado: {self.states[cid].market.slug[:30]}")
                del self.states[cid]
                active.discard(cid)

            # Detecta mercados prestes a expirar (< CANCEL_BEFORE_MIN)
            expiring_soon = [
                cid for cid, s in self.states.items()
                if 0 < (s.market.end_time - time.time()) < CANCEL_BEFORE_MIN * 60
            ]

            # Decide o timeout para o proximo scan:
            # - Se algum mercado esta quase expirando → rescan em 60s
            # - Caso normal → espera scan_interval completo
            if expiring_soon:
                next_scan = 60
                logging.info(
                    f"[SCAN] {len(expiring_soon)} mercado(s) perto de expirar "
                    f"— proximo scan em {next_scan}s"
                )
            else:
                next_scan = self.args.scan_interval

            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=next_scan
                )
            except asyncio.TimeoutError:
                pass


    # ── Status Loop ───────────────────────────────────────────────────────────

    async def status_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(30)
            total_long   = sum(self.inventory.long_usdc.values())
            total_short  = sum(self.inventory.short_usdc.values())
            open_orders  = sum(len(s.open_orders) for s in self.states.values())
            ws_connected = sum(1 for s in self.states.values() if s.ws_connected)
            csv_kb = (
                self.logger.path.stat().st_size / 1024
                if self.logger.path.exists() else 0
            )
            logging.info(
                f"[STATUS] Mercados={len(self.states)} WS={ws_connected} "
                f"Ordens={open_orders} | "
                f"Long=${total_long:.2f} Short=${total_short:.2f} | "
                f"VAR={self.inventory.global_exposure() / self.args.bankroll * 100:.1f}% | "
                f"Quotes={self._quotes_posted} Fills={self._fills_sim} | "
                f"CSV={csv_kb:.1f}KB"
            )

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logging.info("=" * 65)
        logging.info("  MM BOT  |  Polymarket Market Making  |  DRY RUN")
        logging.info(f"  Capital: ${self.args.bankroll}  |  Mercados: {self.categories}")
        logging.info(f"  Latencia simulada: {self.args.latency_ms}ms")
        logging.info(f"  Cancela: {CANCEL_BEFORE_MIN}min antes do evento")
        logging.info(f"  Log: {self.args.log_file}")
        logging.info("=" * 65)

        if websockets is None:
            logging.error("ERRO: pip install websockets")
            return

        await asyncio.gather(
            self.market_scan_loop(),
            self.status_loop(),
        )


# ── Entry Point ───────────────────────────────────────────────────────────────

def load_env(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polymarket MM Bot — Dry Run Simulator"
    )
    p.add_argument("--markets",               default="cs2,nba,soccer,crypto,politics")
    p.add_argument("--max-per-category",      type=int,   default=200)
    p.add_argument("--bankroll",              type=float, default=100.0)
    p.add_argument("--min-volume",            type=float, default=2000.0)
    p.add_argument("--scan-interval",         type=int,   default=60)
    p.add_argument("--cancel-before-minutes", type=int,   default=45)
    p.add_argument("--latency-ms",            type=int,   default=120)
    p.add_argument("--logs-dir",              default="logs")
    p.add_argument("--log-file",              default=None)
    p.add_argument("--env-file",              default=".env")
    p.add_argument("--slugs",                 default="",
                   help="Slugs especificos separados por virgula, ex: cs2-9z-fdb-2026-03-04,nba-por-mem-2026-03-04")
    p.add_argument("--verbose",               action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    load_env(args.env_file)

    # Configuração de Logs e Pastas
    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Nome do arquivo de log do sistema (texto)
    sys_log_file = logs_dir / f"mm_bot_{timestamp}.log"
    
    # Nome do arquivo de resultados (CSV)
    if not args.log_file:
        args.log_file = str(logs_dir / f"mm_dryrun_log_{timestamp}.csv")

    # Configura logging para console E arquivo
    log_format = "%(asctime)s  %(message)s"
    date_format = "%H:%M:%S"
    
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.FileHandler(sys_log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

    logging.info(f"[INIT] Arquivo de log: {sys_log_file}")
    logging.info(f"[INIT] Arquivo CSV: {args.log_file}")

    bot = MMBot(args)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logging.info("[MM BOT] Encerrado pelo usuario.")


if __name__ == "__main__":
    main()
