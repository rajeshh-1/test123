import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import colorama
import requests
from requests.adapters import HTTPAdapter, Retry

colorama.init(autoreset=True)


@dataclass
class StrategyConfig:
    name: str
    min_favored_price: float
    max_vwap: float
    min_net_edge: float
    min_depth_ratio: float
    risk_pct: float
    min_investment: float
    entry_seconds: int


class CryptoMomentumSimulator5m:
    def __init__(
        self,
        initial_bankroll=100.0,
        strategies=None,
        default_fee_bps=200,
        state_file="logs/sim_state.json",
        decisions_log="logs/sim_decisions.jsonl",
        trades_log="simulation_5m_strategy_history.csv",
        resume=False,
        heartbeat_seconds=300,
        save_interval_seconds=120,
        max_errors_before_sleep=3,
        error_sleep_seconds=15,
    ):
        self.initial_bankroll = float(initial_bankroll)
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.clob_url = "https://clob.polymarket.com"
        # Aproxima fee base do book; 200 bps = 2% (mais perto do live CLOB de crypto)
        self.default_fee_bps = int(default_fee_bps)
        self.fee_rate_cache = {}

        self.tracked_markets = {}
        self.last_scoreboard_print = 0
        self.last_resolve_print = 0
        self.last_save = 0
        self.heartbeat_seconds = heartbeat_seconds
        self.save_interval_seconds = save_interval_seconds
        self.max_errors_before_sleep = max_errors_before_sleep
        self.error_sleep_seconds = error_sleep_seconds
        self.state_file = state_file
        self.decisions_log = decisions_log
        self.trades_log = trades_log

        self.strategies = strategies if strategies else self._build_default_strategies()
        self.cfg_by_name = {cfg.name: cfg for cfg in self.strategies}
        self.states = {
            cfg.name: {
                "bankroll": float(initial_bankroll),
                "active": {},
                "processed_markets": set(),
                "history": [],
                "stats": {
                    "entries": 0,
                    "wins": 0,
                    "losses": 0,
                    "skips_low_fav": 0,
                    "skips_liquidity": 0,
                    "skips_vwap": 0,
                    "skips_edge": 0,
                    "skips_depth": 0,
                    "skips_bankroll": 0,
                    "evaluations": 0,
                },
            }
            for cfg in self.strategies
        }
        self.save_strategy_catalog()
        if resume:
            self._load_state()

    @staticmethod
    def _build_default_strategies():
        def edge_tag(edge):
            return f"n{int(abs(edge) * 1000)}" if edge < 0 else f"p{int(edge * 1000)}"

        presets = []
        for min_fav in (0.88, 0.90, 0.93, 0.95):
            for max_vwap in (0.995, 0.985, 0.975, 0.965):
                for min_edge in (-0.005, 0.000, 0.005):
                    for depth in (0.0, 1.0, 1.5):
                        for risk_pct in (0.005, 0.010, 0.020, 0.030):
                            for min_inv in (0.50, 1.00):
                                for entry_seconds in (60, 45, 30, 20, 10):
                                    name = (
                                        f"f{int(min_fav*100)}_v{int(max_vwap*1000)}_"
                                        f"e{edge_tag(min_edge)}_d{int(depth*10)}_"
                                        f"r{int(risk_pct*1000)}_m{int(min_inv*100)}_t{entry_seconds}"
                                    )
                                    presets.append(
                                        StrategyConfig(
                                            name=name,
                                            min_favored_price=min_fav,
                                            max_vwap=max_vwap,
                                            min_net_edge=min_edge,
                                            min_depth_ratio=depth,
                                            risk_pct=risk_pct,
                                            min_investment=min_inv,
                                            entry_seconds=entry_seconds,
                                        )
                                    )

        presets.insert(
            0,
            StrategyConfig(
                name="baseline_5m_relaxed",
                min_favored_price=0.88,
                max_vwap=1.0,
                min_net_edge=-1.0,
                min_depth_ratio=0.0,
                risk_pct=0.01,
                min_investment=1.0,
                entry_seconds=10,
            ),
        )
        return presets

    def save_strategy_catalog(self):
        filename = "strategy_catalog_5m.csv"
        with open(filename, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "strategy",
                    "min_favored_price",
                    "max_vwap",
                    "min_net_edge",
                    "min_depth_ratio",
                    "risk_pct",
                    "min_investment",
                    "entry_seconds",
                ]
            )
            for cfg in self.strategies:
                w.writerow(
                    [
                        cfg.name,
                        cfg.min_favored_price,
                        cfg.max_vwap,
                        cfg.min_net_edge,
                        cfg.min_depth_ratio,
                        cfg.risk_pct,
                        cfg.min_investment,
                        cfg.entry_seconds,
                    ]
                )

    def get_upcoming_markets(self):
        try:
            now = datetime.now(timezone.utc)
            block_start_minute = (now.minute // 5) * 5
            block_start = now.replace(minute=block_start_minute, second=0, microsecond=0)
            start_ts = int(block_start.timestamp())
            upcoming = []

            for coin in ("btc", "eth", "sol", "xrp"):
                slug = f"{coin}-updown-5m-{start_ts}"
                r = self.session.get(f"{self.gamma_url}/events?slug={slug}", timeout=4)
                if r.status_code != 200:
                    continue
                events = r.json()
                if not events:
                    continue
                for m in events[0].get("markets", []):
                    if not m.get("endDate") or m.get("closed", False) or m["id"] in self.tracked_markets:
                        continue
                    end_date = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                    tte = (end_date - now).total_seconds()
                    if 0 < tte <= 300:
                        print(
                            colorama.Fore.CYAN
                            + f"[NOVO 5m] {m['question']} ({coin.upper()}) expira em {int(tte // 60)}m{int(tte % 60)}s"
                        )
                        upcoming.append({"id": m["id"], "slug": slug, "question": m["question"]})
            return upcoming
        except Exception as e:
            print(colorama.Fore.RED + f"Erro ao buscar mercados: {e}")
            return []

    def _normalize_tokens(self, market):
        try:
            outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
            prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])
            clobs = json.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else market.get("clobTokenIds", [])
            tokens = []
            for i, token_id in enumerate(clobs):
                tokens.append(
                    {
                        "token_id": token_id,
                        "outcome": outcomes[i] if i < len(outcomes) else str(i),
                        "price": float(prices[i]) if i < len(prices) else 0.0,
                        "winner": bool(i < len(prices) and float(prices[i]) == 1.0) if market.get("closed", False) else False,
                    }
                )
            return tokens
        except Exception:
            return market.get("tokens", [])

    def get_market_tokens_via_event(self, slug, target_market_id):
        try:
            r = self.session.get(f"{self.gamma_url}/events?slug={slug}", timeout=4)
            if r.status_code != 200:
                return None
            events = r.json()
            if not events:
                return None
            for m in events[0].get("markets", []):
                if m["id"] == target_market_id:
                    m["tokens"] = self._normalize_tokens(m)
                    return m
            return None
        except Exception:
            return None

    def get_clob_midpoints(self, tokens):
        prices = {}
        for t in tokens:
            tid = t.get("token_id")
            if not tid:
                continue
            try:
                r = self.session.get(f"{self.clob_url}/midpoint?token_id={tid}", timeout=3)
                if r.status_code == 200:
                    prices[tid] = float(r.json().get("mid", 0))
                else:
                    prices[tid] = float(t.get("price", 0))
            except Exception:
                prices[tid] = float(t.get("price", 0))
        return prices

    def get_orderbook_asks(self, token_id):
        try:
            r = self.session.get(f"{self.clob_url}/book?token_id={token_id}", timeout=4)
            if r.status_code != 200:
                return []
            asks = [{"price": float(x["price"]), "size": float(x["size"])} for x in r.json().get("asks", [])]
            asks.sort(key=lambda x: x["price"])
            return asks
        except Exception:
            return []

    def get_base_fee_bps(self, token_id):
        if token_id in self.fee_rate_cache:
            return self.fee_rate_cache[token_id]
        try:
            r = self.session.get(f"{self.clob_url}/fee-rate?token_id={token_id}", timeout=4)
            if r.status_code == 200:
                bps = int(r.json().get("base_fee", self.default_fee_bps))
            else:
                bps = self.default_fee_bps
        except Exception:
            bps = self.default_fee_bps
        self.fee_rate_cache[token_id] = bps
        return bps

    def estimate_buy_fee_usd(self, token_id, shares, price):
        # Approximation based on Polymarket crypto fee docs for buy side in collateral.
        p = max(0.0001, min(0.9999, float(price)))
        fee_rate = self.get_base_fee_bps(token_id) / 10000.0
        return shares * p * fee_rate * ((p * (1.0 - p)) ** 2)

    def _log_decision(self, payload: dict):
        try:
            os.makedirs(os.path.dirname(self.decisions_log), exist_ok=True)
            with open(self.decisions_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _save_state(self):
        now = time.time()
        if now - self.last_save < self.save_interval_seconds:
            return
        self.last_save = now
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            serializable_states = {}
            for name, st in self.states.items():
                ser = {
                    "bankroll": st["bankroll"],
                    "active": {
                        k: {**v, "entry_time": v.get("entry_time").isoformat() if isinstance(v.get("entry_time"), datetime) else v.get("entry_time")}
                        for k, v in st["active"].items()
                    },
                    "history": [
                        {**h, "entry_time": h.get("entry_time"), "close_time": h.get("close_time")} for h in st["history"][-500:]
                    ],
                    "stats": st["stats"],
                    "processed_markets": list(st["processed_markets"]),
                }
                serializable_states[name] = ser
            payload = {
                "states": serializable_states,
                "tracked_markets": list(self.tracked_markets.keys()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_state(self):
        try:
            if not os.path.isfile(self.state_file):
                return
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, ser in data.get("states", {}).items():
                if name not in self.states:
                    continue
                st = self.states[name]
                st["bankroll"] = ser.get("bankroll", st["bankroll"])
                st["processed_markets"] = set(ser.get("processed_markets", []))
                st["stats"] = ser.get("stats", st["stats"])
                st["active"] = {}
                for k, v in ser.get("active", {}).items():
                    v["entry_time"] = datetime.fromisoformat(v["entry_time"]) if v.get("entry_time") else None
                    st["active"][k] = v
                st["history"] = ser.get("history", [])
            self.tracked_markets = {mid: {} for mid in data.get("tracked_markets", [])}
            print(colorama.Fore.CYAN + f"[STATE] carregado de {self.state_file}")
        except Exception as e:
            print(colorama.Fore.RED + f"[STATE] falha ao carregar: {e}")

    def simulate_fill_from_asks(self, asks, investment_usd):
        if not asks:
            return None
        remaining = investment_usd
        shares = 0.0
        for ask in asks:
            lvl_cost = ask["price"] * ask["size"]
            if remaining >= lvl_cost:
                shares += ask["size"]
                remaining -= lvl_cost
            else:
                shares += remaining / ask["price"]
                remaining = 0.0
                break
        if shares <= 0:
            return None
        spent = investment_usd - remaining
        return {
            "shares": shares,
            "vwap": spent / shares,
            "total_spent": spent,
            "top_ask": asks[0]["price"],
        }

    def available_cost_under_price(self, asks, max_price):
        total = 0.0
        for ask in asks:
            if ask["price"] > max_price:
                break
            total += ask["price"] * ask["size"]
        return total

    def _print_scoreboard(self):
        now = time.time()
        if now - self.last_scoreboard_print < 60:
            return
        self.last_scoreboard_print = now

        rows = []
        for name, state in self.states.items():
            profit = sum(t.get("profit", 0.0) for t in state["history"])
            entries = state["stats"]["entries"]
            wins = state["stats"]["wins"]
            win_rate = (wins / entries * 100.0) if entries else 0.0
            avg = (profit / entries) if entries else 0.0
            rows.append((profit, win_rate, entries, avg, name, state["bankroll"]))
        rows.sort(key=lambda x: x[0], reverse=True)

        print(colorama.Fore.WHITE + "\n=== SCOREBOARD 5m (TOP 15) ===")
        for i, (pnl, wr, ent, avg, name, bnk) in enumerate(rows[:15], start=1):
            print(f"{i:02d}. {name} | PnL=${pnl:.4f} | WR={wr:.1f}% | N={ent} | Avg=${avg:.4f} | Banca=${bnk:.2f}")
        self.save_strategy_summary(rows)
        self._save_state()

    def save_strategy_summary(self, rows):
        filename = "strategy_scoreboard_5m.csv"
        with open(filename, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["rank", "strategy", "pnl", "win_rate", "entries", "avg_per_trade", "bankroll"])
            for rank, (pnl, wr, ent, avg, name, bnk) in enumerate(rows, start=1):
                w.writerow([rank, name, f"{pnl:.6f}", f"{wr:.2f}", ent, f"{avg:.6f}", f"{bnk:.6f}"])

    def save_trade_csv(self, strategy_name, trade, event_phase="CLOSE"):
        filename = self.trades_log
        exists = os.path.isfile(filename)
        fieldnames = [
            "event_phase",
            "strategy",
            "market_id",
            "slug",
            "question",
            "outcome",
            "favored_price",
            "entry_trigger_seconds",
            "tte_at_entry",
            "risk_pct",
            "min_investment",
            "shares_bought",
            "total_cost",
            "fee_paid",
            "fee_bps",
            "fee_pct_notional",
            "break_even_win_rate",
            "slippage_real_vs_top_ask",
            "vwap",
            "entry_time",
            "close_time",
            "status",
            "profit",
            "bankroll_after",
            "min_favored_price",
            "max_vwap",
            "min_net_edge",
            "min_depth_ratio",
        ]
        cfg = self.cfg_by_name[strategy_name]
        row = trade.copy()
        row["strategy"] = strategy_name
        row["min_favored_price"] = cfg.min_favored_price
        row["max_vwap"] = cfg.max_vwap
        row["min_net_edge"] = cfg.min_net_edge
        row["min_depth_ratio"] = cfg.min_depth_ratio
        row["event_phase"] = event_phase
        if isinstance(row.get("entry_time"), datetime):
            row["entry_time"] = row["entry_time"].isoformat()
        trade_dir = os.path.dirname(filename)
        if trade_dir:
            os.makedirs(trade_dir, exist_ok=True)
        with open(filename, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(row)

    def _evaluate_single_strategy(self, cfg, state, market_info, favored_token, favored_price, asks, tte):
        market_id = market_info["id"]
        state["stats"]["evaluations"] += 1

        if favored_price < cfg.min_favored_price:
            state["stats"]["skips_low_fav"] += 1
            state["processed_markets"].add(market_id)
            return False

        if not asks:
            state["stats"]["skips_liquidity"] += 1
            state["processed_markets"].add(market_id)
            return False

        max_spendable = max(0.0, state["bankroll"])
        raw_investment = max(cfg.min_investment, state["bankroll"] * cfg.risk_pct)
        investment = min(raw_investment, max_spendable)
        if investment < 0.10:
            state["stats"]["skips_bankroll"] += 1
            state["processed_markets"].add(market_id)
            return False

        fill = self.simulate_fill_from_asks(asks, investment)
        if not fill:
            state["stats"]["skips_liquidity"] += 1
            state["processed_markets"].add(market_id)
            return False
        if fill["vwap"] > cfg.max_vwap:
            state["stats"]["skips_vwap"] += 1
            state["processed_markets"].add(market_id)
            return False

        depth_available = self.available_cost_under_price(asks, cfg.max_vwap)
        depth_ratio = depth_available / max(investment, 1e-9)
        if depth_ratio < cfg.min_depth_ratio:
            state["stats"]["skips_depth"] += 1
            state["processed_markets"].add(market_id)
            return False

        fee_paid = self.estimate_buy_fee_usd(favored_token["token_id"], fill["shares"], fill["vwap"])
        fee_bps = self.get_base_fee_bps(favored_token["token_id"])
        total_cost = fill["total_spent"] + fee_paid
        if total_cost > state["bankroll"]:
            state["stats"]["skips_bankroll"] += 1
            state["processed_markets"].add(market_id)
            return False
        fee_pct_notional = fee_paid / max(fill["total_spent"], 1e-9)
        break_even_win_rate = total_cost / max(fill["shares"], 1e-9)
        slippage_real_vs_top_ask = fill["vwap"] - fill["top_ask"]

        net_edge = (fill["shares"] - total_cost) / max(total_cost, 1e-9)
        if net_edge < cfg.min_net_edge:
            state["stats"]["skips_edge"] += 1
            state["processed_markets"].add(market_id)
            return False

        state["bankroll"] -= total_cost
        trade = {
            "market_id": market_id,
            "question": market_info["question"],
            "token_id": favored_token["token_id"],
            "outcome": favored_token["outcome"],
            "favored_price": favored_price,
            "entry_trigger_seconds": cfg.entry_seconds,
            "tte_at_entry": round(tte, 3),
            "risk_pct": cfg.risk_pct,
            "min_investment": cfg.min_investment,
            "shares_bought": fill["shares"],
            "total_cost": total_cost,
            "fee_paid": fee_paid,
            "fee_bps": fee_bps,
            "fee_pct_notional": fee_pct_notional,
            "break_even_win_rate": break_even_win_rate,
            "slippage_real_vs_top_ask": slippage_real_vs_top_ask,
            "vwap": fill["vwap"],
            "entry_time": datetime.now(timezone.utc),
            "status": "PENDING",
            "slug": market_info["slug"],
        }
        state["active"][market_id] = trade
        state["processed_markets"].add(market_id)
        state["stats"]["entries"] += 1
        self.save_trade_csv(cfg.name, trade, event_phase="ENTRY")
        self._log_decision(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "slug": market_info["slug"],
                "market_id": market_id,
                "strategy": cfg.name,
                "favored_price": favored_price,
                "vwap": fill["vwap"],
                "top_ask": fill["top_ask"],
                "tte": tte,
                "investment": investment,
                "total_cost": total_cost,
                "fee_paid": fee_paid,
                "net_edge": net_edge,
                "reason": "entry",
            }
        )
        return True

    def check_resolutions(self):
        pending_ids = set()
        for state in self.states.values():
            pending_ids.update(state["active"].keys())
        if not pending_ids:
            return

        now = datetime.now(timezone.utc)
        for market_id in list(pending_ids):
            sample_trade = None
            for state in self.states.values():
                if market_id in state["active"]:
                    sample_trade = state["active"][market_id]
                    break
            if not sample_trade:
                continue

            fresh = self.get_market_tokens_via_event(sample_trade["slug"], market_id)
            if not fresh:
                if time.time() - self.last_resolve_print > 15:
                    print(colorama.Fore.LIGHTBLACK_EX + "[RESOLVE] aguardando API...")
                    self.last_resolve_print = time.time()
                continue

            tokens = fresh.get("tokens", [])
            is_closed = fresh.get("closed", False)
            end_date = datetime.fromisoformat(fresh["endDate"].replace("Z", "+00:00"))
            time_since_expiry = (now - end_date).total_seconds()

            winner = None
            if is_closed:
                winner = next((t for t in tokens if float(t.get("price", 0)) >= 0.99), None)
                if not winner:
                    winner = next((t for t in tokens if t.get("winner") is True), None)
            elif time_since_expiry > 30:
                mids = self.get_clob_midpoints(tokens)
                winner = next((t for t in tokens if mids.get(t.get("token_id"), 0) >= 0.95), None)
            else:
                continue

            if not winner:
                continue

            for name, state in self.states.items():
                trade = state["active"].get(market_id)
                if not trade:
                    continue
                is_win = winner["token_id"] == trade["token_id"]
                if is_win:
                    payout = trade["shares_bought"]
                    profit = payout - trade["total_cost"]
                    state["bankroll"] += payout
                    trade["status"] = "WIN"
                    trade["profit"] = profit
                    state["stats"]["wins"] += 1
                else:
                    trade["status"] = "LOSS"
                    trade["profit"] = -trade["total_cost"]
                    state["stats"]["losses"] += 1

                trade["close_time"] = datetime.now(timezone.utc).isoformat()
                trade["bankroll_after"] = state["bankroll"]
                state["history"].append(trade)
                self.save_trade_csv(name, trade, event_phase="CLOSE")
                del state["active"][market_id]

    def loop(self):
        print(colorama.Fore.MAGENTA + "Iniciando simulador 5m (exploracao de filtros).")
        print(f"Estrategias carregadas: {len(self.strategies)}")
        print(f"Banca inicial por estrategia: ${self.initial_bankroll:.2f}")

        last_fetch_time = 0
        last_print_time = 0
        last_heartbeat_time = 0
        err_streak = 0

        while True:
            try:
                now_ts = time.time()
                if now_ts - last_fetch_time >= 10:
                    for m in self.get_upcoming_markets():
                        self.tracked_markets[m["id"]] = m["slug"]
                    last_fetch_time = now_ts

                self.check_resolutions()
                self._print_scoreboard()

                now = datetime.now(timezone.utc)
                should_print = (now_ts - last_print_time) >= 2
                to_remove = []
                if should_print:
                    active_total = sum(len(s["active"]) for s in self.states.values())
                    print(colorama.Fore.WHITE + f"\n[LIVE {now.strftime('%H:%M:%S')}] mercados={len(self.tracked_markets)} ativos={active_total}")
                    last_print_time = now_ts
                if now_ts - last_heartbeat_time >= self.heartbeat_seconds:
                    print(colorama.Fore.LIGHTBLACK_EX + f"[HEARTBEAT] {now.isoformat()} tracked={len(self.tracked_markets)}")
                    last_heartbeat_time = now_ts

                for market_id, slug in list(self.tracked_markets.items()):
                    fresh = self.get_market_tokens_via_event(slug, market_id)
                    if not fresh:
                        to_remove.append(market_id)
                        continue
                    if fresh.get("closed", False):
                        to_remove.append(market_id)
                        continue

                    end_date = datetime.fromisoformat(fresh["endDate"].replace("Z", "+00:00"))
                    tte = (end_date - now).total_seconds()
                    if tte <= 0:
                        to_remove.append(market_id)
                        continue

                    if should_print:
                        mids = self.get_clob_midpoints(fresh.get("tokens", []))
                        odds = []
                        for t in fresh.get("tokens", []):
                            p = mids.get(t.get("token_id"), t.get("price", 0))
                            odds.append(f"{t.get('outcome')}: {p * 100:.1f}%")
                        mins = int(tte // 60)
                        secs = int(tte % 60)
                        print(f"[{mins:02d}m{secs:02d}s] {fresh['question']} -> {' | '.join(odds)}")

                    due = []
                    for cfg in self.strategies:
                        state = self.states[cfg.name]
                        if market_id in state["active"] or market_id in state["processed_markets"]:
                            continue
                        if 0 < tte <= cfg.entry_seconds:
                            due.append((cfg, state))
                    if not due:
                        continue

                    tokens = fresh.get("tokens", [])
                    if len(tokens) < 2:
                        for _, state in due:
                            state["processed_markets"].add(market_id)
                        continue

                    mids = self.get_clob_midpoints(tokens)
                    for t in tokens:
                        t["price"] = mids.get(t.get("token_id"), t.get("price", 0))
                    favored = max(tokens, key=lambda t: float(t.get("price", 0)))
                    favored_price = float(favored.get("price", 0))
                    asks = self.get_orderbook_asks(favored.get("token_id"))

                    entered = 0
                    for cfg, state in due:
                        if self._evaluate_single_strategy(cfg, state, {"id": market_id, "slug": slug, "question": fresh["question"]}, favored, favored_price, asks, tte):
                            entered += 1
                    if entered > 0:
                        print(colorama.Fore.GREEN + f"[ENTRADA] {fresh['question']} -> {entered} estrategias entraram (tte={tte:.1f}s)")

                for market_id in to_remove:
                    self.tracked_markets.pop(market_id, None)

            except Exception as e:
                err_streak += 1
                print(colorama.Fore.RED + f"Erro no loop principal: {e}")
                if err_streak >= self.max_errors_before_sleep:
                    print(colorama.Fore.LIGHTBLACK_EX + f"[BACKOFF] err_streak={err_streak} dormindo {self.error_sleep_seconds}s")
                    time.sleep(self.error_sleep_seconds)
            else:
                err_streak = 0
            self._save_state()
            time.sleep(1.0)


class TeeLogger:
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, log_path):
        self.terminal = sys.__stdout__
        self.log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    def write(self, message):
        self.terminal.write(message)
        clean = self.ansi_escape.sub("", message)
        if clean.strip():
            self.log_file.write(clean)
            self.log_file.flush()

    def flush(self):
        self.terminal.flush()
        self.log_file.flush()


def build_custom_strategies(args):
    if not args.single:
        return None
    return [
        StrategyConfig(
            name="single_custom",
            min_favored_price=args.min_favored_price,
            max_vwap=args.max_vwap,
            min_net_edge=args.min_net_edge,
            min_depth_ratio=args.min_depth_ratio,
            risk_pct=args.risk,
            min_investment=args.min_investment,
            entry_seconds=args.entry_seconds,
        )
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulador Polymarket 5m com exploracao ampla de cenarios.")
    parser.add_argument("--initial-bankroll", type=float, default=100.0)
    parser.add_argument("--single", action="store_true", help="Roda apenas 1 estrategia custom.")
    parser.add_argument("--max-strategies", type=int, default=0, help="Limita qtd de estrategias no grid (0 = sem limite).")
    parser.add_argument("--live-preset", action="store_true", help="Usa 1 estrategia com os mesmos limites do live (BEST_PRESET).")
    parser.add_argument("--risk", type=float, default=0.01, help="Apenas para --single. Ex.: 0.02 = 2%% da banca.")
    parser.add_argument("--min-investment", type=float, default=1.0, help="Apenas para --single.")
    parser.add_argument("--entry-seconds", type=int, default=20, help="Apenas para --single. Ex.: 60,45,30,20,10.")
    parser.add_argument("--min-favored-price", type=float, default=0.90, help="Apenas para --single.")
    parser.add_argument("--max-vwap", type=float, default=0.97, help="Apenas para --single.")
    parser.add_argument("--min-net-edge", type=float, default=0.0, help="Apenas para --single.")
    parser.add_argument("--min-depth-ratio", type=float, default=1.0, help="Apenas para --single.")
    parser.add_argument("--fee-bps", type=int, default=200, help="Fee base em bps para simular (default 200 = 2%).")
    parser.add_argument("--resume", action="store_true", help="Retoma estado salvo do simulador.")
    parser.add_argument("--state-file", default="logs/sim_state.json", help="Arquivo de estado para resume/save.")
    parser.add_argument("--decisions-log", default="logs/sim_decisions.jsonl", help="Log JSONL com decisoes/entradas.")
    parser.add_argument("--trades-log", default="simulation_5m_strategy_history.csv", help="CSV de trades (ENTRY/CLOSE).")
    parser.add_argument("--heartbeat-seconds", type=int, default=300, help="Intervalo do heartbeat no stdout.")
    parser.add_argument("--save-interval-seconds", type=int, default=120, help="Intervalo de persistencia do estado.")
    parser.add_argument("--max-errors-before-sleep", type=int, default=3, help="Quantidade de erros seguidos antes de backoff.")
    parser.add_argument("--error-sleep-seconds", type=int, default=15, help="Backoff apos erro em streak.")
    args = parser.parse_args()

    log_filename = f"simulator_5m_opt_{datetime.now().strftime('%Y-%m-%d')}.log"
    sys.stdout = TeeLogger(log_filename)
    print(f"Log: {log_filename}")

    if args.live_preset:
        # espelha os limites do bot live (BEST_PRESET)
        strategies = [
            StrategyConfig(
                name="live_like",
                min_favored_price=0.88,
                max_vwap=0.975,
                min_net_edge=0.0,
                min_depth_ratio=0.0,
                risk_pct=0.03,
                min_investment=0.50,
                entry_seconds=45,
            )
        ]
    else:
        strategies = build_custom_strategies(args)
        if strategies is None:
            strategies = CryptoMomentumSimulator5m._build_default_strategies()
            if args.max_strategies and args.max_strategies > 0:
                strategies = strategies[: args.max_strategies]

    bot = CryptoMomentumSimulator5m(
        initial_bankroll=args.initial_bankroll,
        strategies=strategies,
        default_fee_bps=args.fee_bps,
        state_file=args.state_file,
        decisions_log=args.decisions_log,
        trades_log=args.trades_log,
        resume=args.resume,
        heartbeat_seconds=args.heartbeat_seconds,
        save_interval_seconds=args.save_interval_seconds,
        max_errors_before_sleep=args.max_errors_before_sleep,
        error_sleep_seconds=args.error_sleep_seconds,
    )

    bot.loop()
