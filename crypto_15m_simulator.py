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

colorama.init(autoreset=True)


@dataclass
class StrategyConfig:
    name: str
    min_favored_price: float
    max_vwap: float
    min_net_edge: float
    min_depth_ratio: float


class CryptoMomentumSimulator:
    def __init__(self, initial_bankroll=100.0, compounding_percent=0.01, strategies=None):
        self.initial_bankroll = initial_bankroll
        self.compounding_percent = compounding_percent
        self.session = requests.Session()
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.clob_url = "https://clob.polymarket.com"
        self.tracked_markets = {}
        self.processed_markets = set()
        self.last_resolve_print = 0
        self.last_scoreboard_print = 0

        self.default_fee_bps = 1000
        self.fee_rate_cache = {}

        self.strategies = strategies if strategies else self._build_default_strategies()
        self.states = {
            cfg.name: {
                "bankroll": float(initial_bankroll),
                "active": {},
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
                    "skips_error": 0,
                },
            }
            for cfg in self.strategies
        }
        self.cfg_by_name = {cfg.name: cfg for cfg in self.strategies}

    def _build_default_strategies(self):
        presets = []
        for min_fav in (0.90, 0.93, 0.95):
            for max_vwap in (0.985, 0.975, 0.965, 0.955):
                for min_edge in (0.000, 0.005, 0.010):
                    depth = 1.0 if max_vwap >= 0.975 else 1.5
                    name = f"fav{int(min_fav*100)}_vwap{int(max_vwap*1000)}_edge{int(min_edge*1000)}"
                    presets.append(
                        StrategyConfig(
                            name=name,
                            min_favored_price=min_fav,
                            max_vwap=max_vwap,
                            min_net_edge=min_edge,
                            min_depth_ratio=depth,
                        )
                    )

        presets.insert(
            0,
            StrategyConfig(
                name="baseline_fav90",
                min_favored_price=0.90,
                max_vwap=1.0,
                min_net_edge=-1.0,
                min_depth_ratio=0.0,
            ),
        )
        return presets

    def get_upcoming_markets(self):
        try:
            now = datetime.now(timezone.utc)
            block_start_minute = (now.minute // 15) * 15
            block_start = now.replace(minute=block_start_minute, second=0, microsecond=0)
            start_ts = int(block_start.timestamp())
            upcoming = []

            for coin in ("btc", "eth", "sol", "xrp"):
                slug = f"{coin}-updown-15m-{start_ts}"
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
                    time_to_expiry = (end_date - now).total_seconds()
                    if 0 < time_to_expiry <= 900:
                        print(
                            colorama.Fore.CYAN
                            + f"[NOVO] {m['question']} ({coin.upper()}) expira em {int(time_to_expiry // 60)}m{int(time_to_expiry % 60)}s"
                        )
                        upcoming.append(
                            {
                                "id": m["id"],
                                "slug": slug,
                                "question": m["question"],
                                "end_date": end_date,
                            }
                        )
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
            asks = r.json().get("asks", [])
            asks = [{"price": float(x["price"]), "size": float(x["size"])} for x in asks]
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

    def simulate_fill_from_asks(self, asks, investment_usd):
        if not asks:
            return None
        remaining = investment_usd
        shares = 0.0
        for ask in asks:
            cost_lvl = ask["price"] * ask["size"]
            if remaining >= cost_lvl:
                shares += ask["size"]
                remaining -= cost_lvl
            else:
                shares += remaining / ask["price"]
                remaining = 0.0
                break
        if shares <= 0:
            return None
        spent = investment_usd - remaining
        vwap = spent / shares
        return {
            "shares": shares,
            "vwap": vwap,
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
            h = state["history"]
            total_profit = sum(t.get("profit", 0.0) for t in h)
            entries = state["stats"]["entries"]
            wins = state["stats"]["wins"]
            win_rate = (wins / entries * 100.0) if entries else 0.0
            rows.append((total_profit, win_rate, entries, name, state["bankroll"]))
        rows.sort(key=lambda x: x[0], reverse=True)

        print(colorama.Fore.WHITE + "\n=== SCOREBOARD ESTRATEGIAS ===")
        for i, (profit, win_rate, entries, name, bankroll) in enumerate(rows[:12], start=1):
            print(f"{i:02d}. {name} | PnL: ${profit:.4f} | WinRate: {win_rate:.1f}% | Entradas: {entries} | Banca: ${bankroll:.2f}")
        self.save_strategy_summary(rows)

    def save_trade_csv(self, strategy_name, trade):
        filename = "simulation_15m_strategy_history.csv"
        file_exists = os.path.isfile(filename)
        fieldnames = [
            "strategy",
            "market_id",
            "question",
            "outcome",
            "favored_price",
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
        row = trade.copy()
        row["strategy"] = strategy_name
        cfg = self.cfg_by_name[strategy_name]
        row["min_favored_price"] = cfg.min_favored_price
        row["max_vwap"] = cfg.max_vwap
        row["min_net_edge"] = cfg.min_net_edge
        row["min_depth_ratio"] = cfg.min_depth_ratio
        if isinstance(row.get("entry_time"), datetime):
            row["entry_time"] = row["entry_time"].isoformat()
        with open(filename, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                w.writeheader()
            w.writerow(row)

    def save_strategy_summary(self, sorted_rows):
        filename = "strategy_scoreboard.csv"
        with open(filename, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["rank", "strategy", "pnl", "win_rate", "entries", "bankroll"])
            for idx, (profit, win_rate, entries, name, bankroll) in enumerate(sorted_rows, start=1):
                w.writerow([idx, name, f"{profit:.6f}", f"{win_rate:.2f}", entries, f"{bankroll:.6f}"])

    def evaluate_market_for_all_strategies(self, market_info):
        print(colorama.Fore.YELLOW + f"[T-10s] Avaliando: {market_info['question']}")
        fresh = self.get_market_tokens_via_event(market_info["slug"], market_info["id"])
        if not fresh or "tokens" not in fresh:
            return
        tokens = fresh["tokens"]
        if len(tokens) < 2:
            return

        clob_prices = self.get_clob_midpoints(tokens)
        for t in tokens:
            t["price"] = clob_prices.get(t.get("token_id"), t.get("price", 0))
        favored = max(tokens, key=lambda t: float(t.get("price", 0)))
        favored_price = float(favored.get("price", 0))
        token_id = favored.get("token_id")
        if not token_id:
            return

        asks = self.get_orderbook_asks(token_id)
        if not asks:
            for s in self.states.values():
                s["stats"]["skips_liquidity"] += 1
            print(colorama.Fore.RED + "Ignorado geral: livro sem asks para o favorito.")
            return

        processed = 0
        for cfg in self.strategies:
            state = self.states[cfg.name]
            if market_info["id"] in state["active"]:
                continue

            if favored_price < cfg.min_favored_price:
                state["stats"]["skips_low_fav"] += 1
                continue

            investment = max(1.0, state["bankroll"] * self.compounding_percent)
            fill = self.simulate_fill_from_asks(asks, investment)
            if not fill:
                state["stats"]["skips_liquidity"] += 1
                continue
            if fill["vwap"] > cfg.max_vwap:
                state["stats"]["skips_vwap"] += 1
                continue

            depth_available = self.available_cost_under_price(asks, cfg.max_vwap)
            depth_ratio = depth_available / max(investment, 1e-9)
            if depth_ratio < cfg.min_depth_ratio:
                state["stats"]["skips_depth"] += 1
                continue

            fee_paid = self.estimate_buy_fee_usd(token_id, fill["shares"], fill["vwap"])
            fee_bps = self.get_base_fee_bps(token_id)
            total_cost = fill["total_spent"] + fee_paid
            if total_cost > state["bankroll"]:
                state["stats"]["skips_error"] += 1
                continue
            fee_pct_notional = fee_paid / max(fill["total_spent"], 1e-9)
            break_even_win_rate = total_cost / max(fill["shares"], 1e-9)
            slippage_real_vs_top_ask = fill["vwap"] - fill["top_ask"]
            net_edge = (fill["shares"] - total_cost) / max(total_cost, 1e-9)
            if net_edge < cfg.min_net_edge:
                state["stats"]["skips_edge"] += 1
                continue

            state["bankroll"] -= total_cost
            trade = {
                "market_id": market_info["id"],
                "question": market_info["question"],
                "token_id": token_id,
                "outcome": favored.get("outcome"),
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
                "favored_price": favored_price,
            }
            state["active"][market_info["id"]] = trade
            state["stats"]["entries"] += 1
            processed += 1

        if processed:
            print(colorama.Fore.GREEN + f"Entradas feitas em {processed} estrategias para esse mercado.")

    def check_resolutions(self):
        pending_market_ids = set()
        for state in self.states.values():
            pending_market_ids.update(state["active"].keys())
        if not pending_market_ids:
            return

        now = datetime.now(timezone.utc)
        for market_id in list(pending_market_ids):
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
                    print(colorama.Fore.LIGHTBLACK_EX + "[RESOLVE] API nao respondeu, aguardando...")
                    self.last_resolve_print = time.time()
                continue

            tokens = fresh.get("tokens", [])
            is_closed = fresh.get("closed", False)
            end_date = datetime.fromisoformat(fresh["endDate"].replace("Z", "+00:00"))
            time_since_expiry = (now - end_date).total_seconds()

            winner_token = None
            if is_closed:
                winner_token = next((t for t in tokens if float(t.get("price", 0)) >= 0.99), None)
                if not winner_token:
                    winner_token = next((t for t in tokens if t.get("winner") is True), None)
            elif time_since_expiry > 30:
                clob_prices = self.get_clob_midpoints(tokens)
                winner_token = next((t for t in tokens if clob_prices.get(t.get("token_id"), 0) >= 0.95), None)
            else:
                continue

            if not winner_token:
                continue

            for name, state in self.states.items():
                trade = state["active"].get(market_id)
                if not trade:
                    continue
                is_win = winner_token["token_id"] == trade["token_id"]
                if is_win:
                    payout = trade["shares_bought"] * 1.0
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
                self.save_trade_csv(name, trade)
                del state["active"][market_id]

    def loop(self):
        print(colorama.Fore.MAGENTA + "Iniciando simulador 15m em modo otimizacao.")
        print(f"Estrategias carregadas: {len(self.strategies)}")
        print(f"Banca inicial por estrategia: ${self.initial_bankroll:.2f} | Risco: {self.compounding_percent * 100:.2f}%")

        last_fetch_time = 0
        last_print_time = 0

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
                to_remove = []
                should_print = (now_ts - last_print_time) >= 2
                if should_print:
                    active_total = sum(len(s["active"]) for s in self.states.values())
                    print(colorama.Fore.WHITE + f"\n[LIVE {now.strftime('%H:%M:%S')}] mercados={len(self.tracked_markets)} ativos={active_total}")
                    last_print_time = now_ts

                for market_id, slug in list(self.tracked_markets.items()):
                    if market_id in self.processed_markets:
                        continue
                    fresh = self.get_market_tokens_via_event(slug, market_id)
                    if not fresh:
                        to_remove.append(market_id)
                        continue
                    if fresh.get("closed", False):
                        to_remove.append(market_id)
                        continue

                    end_date = datetime.fromisoformat(fresh["endDate"].replace("Z", "+00:00"))
                    tte = (end_date - now).total_seconds()
                    if should_print:
                        mids = self.get_clob_midpoints(fresh.get("tokens", []))
                        odds = []
                        for t in fresh.get("tokens", []):
                            p = mids.get(t.get("token_id"), t.get("price", 0))
                            odds.append(f"{t.get('outcome')}: {p * 100:.1f}%")
                        mins = int(max(0, tte) // 60)
                        secs = int(max(0, tte) % 60)
                        print(f"[{mins:02d}m{secs:02d}s] {fresh['question']} -> {' | '.join(odds)}")

                    if 0 < tte <= 10:
                        self.evaluate_market_for_all_strategies({"id": market_id, "slug": slug, "question": fresh["question"]})
                        self.processed_markets.add(market_id)
                    elif tte < 0:
                        to_remove.append(market_id)

                for rm in to_remove:
                    self.tracked_markets.pop(rm, None)
                    self.processed_markets.discard(rm)

            except Exception as e:
                print(colorama.Fore.RED + f"Erro no loop principal: {e}")
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
    cfg = StrategyConfig(
        name="single_custom",
        min_favored_price=args.min_favored_price,
        max_vwap=args.max_vwap,
        min_net_edge=args.min_net_edge,
        min_depth_ratio=args.min_depth_ratio,
    )
    return [cfg]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulador Polymarket 15m com otimizacao de filtros.")
    parser.add_argument("--initial-bankroll", type=float, default=100.0)
    parser.add_argument("--risk", type=float, default=0.01, help="Percentual da banca por trade (ex.: 0.01 = 1%%).")
    parser.add_argument("--single", action="store_true", help="Roda apenas uma estrategia custom.")
    parser.add_argument("--min-favored-price", type=float, default=0.90)
    parser.add_argument("--max-vwap", type=float, default=0.97)
    parser.add_argument("--min-net-edge", type=float, default=0.0)
    parser.add_argument("--min-depth-ratio", type=float, default=1.0)
    args = parser.parse_args()

    log_filename = f"simulator_15m_opt_{datetime.now().strftime('%Y-%m-%d')}.log"
    sys.stdout = TeeLogger(log_filename)
    print(f"Log: {log_filename}")

    custom_strategies = build_custom_strategies(args)
    bot = CryptoMomentumSimulator(
        initial_bankroll=args.initial_bankroll,
        compounding_percent=args.risk,
        strategies=custom_strategies,
    )
    bot.loop()
