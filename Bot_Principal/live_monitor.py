import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
except Exception:
    ClobClient = None
    MarketOrderArgs = None
    OrderType = None
    BUY = None


def load_env_file(path=".env"):
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_env_file(os.path.join(os.path.dirname(__file__), "..", ".env"))


# ==========================================
# CONFIGURACOES
# ==========================================
KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", os.path.join(os.path.dirname(__file__), "kalshi-key.pem.txt")).strip()
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

KALSHI_TICKER = "KXCS2GAME-26FEB26MOUZ.NK27-MOUZ.N"
POLY_SLUG = "cs2-k271-mouzn-2026-02-26"

# ESTRATEGIA DERIVADA DOS MELHORES RESULTADOS OBSERVADOS
# (vwap975, risco 3%, trigger por volta de 45s)
ENTRY_TRIGGER_SECONDS = 45
ENTRY_MIN_SECONDS = 10
MAX_VWAP_PER_LEG = 0.975
MIN_SPREAD_PCT = 0.20
RISK_PCT = 0.03
MIN_TRADE_USD = 1.0

# EXECUCAO
ENABLE_LIVE_POLY_ORDER = False
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_LIVE_ORDER"
LIVE_CONFIRM = os.getenv("LIVE_CONFIRM", "")
CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"

SIMULATED_BANKROLL_USD = 100.0

session = requests.Session()


@dataclass
class CandidateLeg:
    name: str
    kalshi_side: str
    poly_token_id: str
    kalshi_vwap: float
    poly_vwap: float
    total_cost: float
    spread_pct: float


# ==========================================
# AUTENTICACAO KALSHI
# ==========================================
if not KEY_ID:
    raise RuntimeError("missing KALSHI_API_KEY_ID")
with open(KEY_PATH, "rb") as f:
    kalshi_private_key = serialization.load_pem_private_key(f.read(), password=None)


def get_kalshi_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = ts + method + path
    sig = kalshi_private_key.sign(
        msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-API-KEY": KEY_ID,
        "KALSHI-API-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-API-TIMESTAMP": ts,
    }


# ==========================================
# VWAP / BOOK HELPERS
# ==========================================
def get_kalshi_orderbook(ticker):
    path = f"/markets/{ticker}/orderbook"
    try:
        r = session.get(KALSHI_BASE + path, headers=get_kalshi_headers("GET", path), timeout=5)
        return r.json().get("orderbook", {})
    except Exception:
        return {}


def get_kalshi_vwap(target_side, ob, quantity_needed):
    try:
        raw_orders = ob.get("no", []) if target_side == "yes" else ob.get("yes", [])
        if not raw_orders:
            return None

        asks = []
        for price_cents, size in raw_orders:
            ask_price_cents = 100 - price_cents
            asks.append((ask_price_cents, float(size)))
        asks.sort(key=lambda x: x[0])

        shares_collected = 0.0
        total_cost = 0.0
        for ask_price_cents, size in asks:
            price = ask_price_cents / 100.0
            if shares_collected + size >= quantity_needed:
                needed = quantity_needed - shares_collected
                total_cost += needed * price
                shares_collected += needed
                break
            total_cost += size * price
            shares_collected += size

        if shares_collected < quantity_needed:
            return None
        return total_cost / quantity_needed
    except Exception:
        return None


def get_poly_asks(clob_token_id):
    if not clob_token_id:
        return []
    url = f"{CLOB_HOST}/book?token_id={clob_token_id}"
    try:
        res = session.get(url, timeout=5).json()
        asks = res.get("asks", [])
        parsed = [{"price": float(x["price"]), "size": float(x["size"])} for x in asks]
        parsed.sort(key=lambda x: x["price"])
        return parsed
    except Exception:
        return []


def get_poly_vwap(clob_token_id, quantity_needed):
    asks = get_poly_asks(clob_token_id)
    if not asks:
        return None
    shares_collected = 0.0
    total_cost = 0.0
    for ask in asks:
        price, size = ask["price"], ask["size"]
        if shares_collected + size >= quantity_needed:
            needed = quantity_needed - shares_collected
            total_cost += needed * price
            shares_collected += needed
            break
        total_cost += size * price
        shares_collected += size
    if shares_collected < quantity_needed:
        return None
    return total_cost / quantity_needed


def simulate_poly_fill_by_usd(clob_token_id, usd_amount):
    asks = get_poly_asks(clob_token_id)
    if not asks:
        return None
    remaining = usd_amount
    shares = 0.0
    spent = 0.0
    for ask in asks:
        level_cost = ask["price"] * ask["size"]
        if remaining >= level_cost:
            shares += ask["size"]
            spent += level_cost
            remaining -= level_cost
        else:
            part = remaining / ask["price"]
            shares += part
            spent += remaining
            remaining = 0.0
            break
    if shares <= 0:
        return None
    return {
        "shares": shares,
        "spent": spent,
        "vwap": spent / shares,
        "top_ask": asks[0]["price"],
        "slippage": (spent / shares) - asks[0]["price"],
    }


# ==========================================
# MARKET RESOLUTION HELPERS
# ==========================================
def fetch_poly_market_info(slug):
    try:
        r = session.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
        events = r.json()
        if not events:
            return None, None, None
        mkt = events[0].get("markets", [])[0]
        outcomes = json.loads(mkt.get("outcomes", "[]"))
        clobs = json.loads(mkt.get("clobTokenIds", "[]"))
        return outcomes, clobs, mkt
    except Exception as e:
        print("Erro buscando Poly:", e)
        return None, None, None


def fetch_kalshi_market_info(ticker):
    try:
        path = f"/markets/{ticker}"
        r = session.get(KALSHI_BASE + path, headers=get_kalshi_headers("GET", path), timeout=5)
        m = r.json().get("market", {})
        if m:
            return m.get("yes_sub_title", "YES"), m.get("no_sub_title", "NO")
    except Exception as e:
        print("Erro buscando Kalshi:", e)
    return "YES", "NO"


def parse_end_date_utc(poly_mkt):
    end_raw = poly_mkt.get("endDate")
    if not end_raw:
        return None
    try:
        return datetime.fromisoformat(end_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def build_poly_client():
    if ClobClient is None:
        raise RuntimeError("py-clob-client nao instalado.")
    private_key = os.getenv("POLY_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("Defina POLY_PRIVATE_KEY no ambiente.")
    signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    funder = os.getenv("POLY_FUNDER")
    client = ClobClient(CLOB_HOST, key=private_key, chain_id=CHAIN_ID, signature_type=signature_type, funder=funder)
    api_key = os.getenv("POLY_API_KEY")
    api_secret = os.getenv("POLY_API_SECRET")
    api_passphrase = os.getenv("POLY_API_PASSPHRASE")
    if api_key and api_secret and api_passphrase:
        client.set_api_creds({"key": api_key, "secret": api_secret, "passphrase": api_passphrase})
    else:
        client.set_api_creds(client.create_or_derive_api_creds())
    return client


def execute_poly_fok_buy(poly_client, token_id, usd_amount):
    order = MarketOrderArgs(token_id=token_id, amount=float(usd_amount), side=BUY, order_type=OrderType.FOK)
    signed = poly_client.create_market_order(order)
    return poly_client.post_order(signed, OrderType.FOK)


def pick_best_leg(k_yes_team, k_no_team, outcomes, p_yes_index, p_no_index, ob, target_shares):
    k_vwap_yes = get_kalshi_vwap("yes", ob, target_shares) or 1.0
    k_vwap_no = get_kalshi_vwap("no", ob, target_shares) or 1.0
    p_vwap_yes = get_poly_vwap(outcomes["p_yes_clob"], target_shares) or 1.0
    p_vwap_no = get_poly_vwap(outcomes["p_no_clob"], target_shares) or 1.0

    cost_a = k_vwap_yes + p_vwap_no
    cost_b = k_vwap_no + p_vwap_yes
    spread_a = ((1.0 - cost_a) / max(cost_a, 1e-9)) * 100.0
    spread_b = ((1.0 - cost_b) / max(cost_b, 1e-9)) * 100.0

    leg_a = CandidateLeg(
        name=f"A: YES Kalshi {k_yes_team} + Poly {outcomes['labels'][p_no_index]}",
        kalshi_side="yes",
        poly_token_id=outcomes["p_no_clob"],
        kalshi_vwap=k_vwap_yes,
        poly_vwap=p_vwap_no,
        total_cost=cost_a,
        spread_pct=spread_a,
    )
    leg_b = CandidateLeg(
        name=f"B: NO Kalshi {k_no_team} + Poly {outcomes['labels'][p_yes_index]}",
        kalshi_side="no",
        poly_token_id=outcomes["p_yes_clob"],
        kalshi_vwap=k_vwap_no,
        poly_vwap=p_vwap_yes,
        total_cost=cost_b,
        spread_pct=spread_b,
    )
    return leg_a if leg_a.spread_pct >= leg_b.spread_pct else leg_b, leg_a, leg_b


def live_monitor():
    print("=" * 70)
    print(f"LIVE MONITOR ESTRATEGICO: Kalshi [{KALSHI_TICKER}] <> Poly [{POLY_SLUG}]")
    print(
        f"Params: trigger={ENTRY_TRIGGER_SECONDS}s, min={ENTRY_MIN_SECONDS}s, "
        f"max_vwap={MAX_VWAP_PER_LEG}, min_spread={MIN_SPREAD_PCT}%, risk={RISK_PCT*100:.1f}%"
    )
    print("=" * 70)

    k_yes_team, k_no_team = fetch_kalshi_market_info(KALSHI_TICKER)
    labels, clobs, poly_mkt = fetch_poly_market_info(POLY_SLUG)
    if not clobs or not poly_mkt:
        print("Falha ao resolver IDs Polymarket.")
        return

    # Mantido o mapeamento usado no fluxo atual do projeto.
    k_yes_team = "MOUZ NXT"
    k_no_team = "K27"
    p_yes_index = labels.index(k_yes_team) if k_yes_team in labels else 1
    p_no_index = labels.index(k_no_team) if k_no_team in labels else 0

    outcomes = {
        "labels": labels,
        "p_yes_clob": clobs[p_yes_index],
        "p_no_clob": clobs[p_no_index],
    }

    end_date_utc = parse_end_date_utc(poly_mkt)
    if not end_date_utc:
        print("endDate nao encontrado. Nao e possivel aplicar estrategia temporal.")
        return

    poly_client = None
    if ENABLE_LIVE_POLY_ORDER:
        if LIVE_CONFIRM != LIVE_CONFIRM_PHRASE:
            print("LIVE_CONFIRM invalido. Para live set LIVE_CONFIRM corretamente.")
            return
        try:
            poly_client = build_poly_client()
        except Exception as e:
            print(f"Falha ao iniciar cliente Poly live: {e}")
            return

    print(f"Mapeamento: Kalshi YES={k_yes_team} | NO={k_no_team}")
    print(f"Poly YES token={outcomes['p_yes_clob']} | NO token={outcomes['p_no_clob']}")
    print("Loop iniciado (tick 5s).")

    simulated_bankroll = SIMULATED_BANKROLL_USD
    already_executed = False

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            tte = (end_date_utc - now_utc).total_seconds()
            timestamp = now_utc.strftime("%H:%M:%S")

            ob = get_kalshi_orderbook(KALSHI_TICKER)
            if not ob:
                print(f"[{timestamp}] sem orderbook Kalshi.")
                time.sleep(5)
                continue

            k_yes_ask = min([100 - lvl[0] for lvl in ob.get("no", [])]) / 100.0 if ob.get("no") else 1.0
            k_no_ask = min([100 - lvl[0] for lvl in ob.get("yes", [])]) / 100.0 if ob.get("yes") else 1.0
            p_yes_top = get_poly_asks(outcomes["p_yes_clob"])
            p_no_top = get_poly_asks(outcomes["p_no_clob"])
            p_yes_ask = p_yes_top[0]["price"] if p_yes_top else 1.0
            p_no_ask = p_no_top[0]["price"] if p_no_top else 1.0

            top_cost_a = k_yes_ask + p_no_ask
            top_cost_b = k_no_ask + p_yes_ask
            best_top_cost = min(top_cost_a, top_cost_b)
            target_shares = max(1.0, (simulated_bankroll * RISK_PCT) / max(best_top_cost, 1e-9))

            best, leg_a, leg_b = pick_best_leg(k_yes_team, k_no_team, outcomes, p_yes_index, p_no_index, ob, target_shares)

            print(f"[{timestamp}] TTE={tte:.1f}s | Shares={target_shares:.2f} | Bankroll(sim)=${simulated_bankroll:.2f}")
            print(
                f"  {leg_a.name}: kalshi={leg_a.kalshi_vwap:.3f} poly={leg_a.poly_vwap:.3f} "
                f"cost={leg_a.total_cost:.3f} spread={leg_a.spread_pct:+.2f}%"
            )
            print(
                f"  {leg_b.name}: kalshi={leg_b.kalshi_vwap:.3f} poly={leg_b.poly_vwap:.3f} "
                f"cost={leg_b.total_cost:.3f} spread={leg_b.spread_pct:+.2f}%"
            )

            should_enter_window = ENTRY_MIN_SECONDS <= tte <= ENTRY_TRIGGER_SECONDS
            if should_enter_window and not already_executed:
                poly_fill = simulate_poly_fill_by_usd(best.poly_token_id, max(MIN_TRADE_USD, simulated_bankroll * RISK_PCT))
                if not poly_fill:
                    print("  [SKIP] sem liquidez no token Poly escolhido.")
                elif poly_fill["vwap"] > MAX_VWAP_PER_LEG:
                    print(
                        f"  [SKIP] vwap Poly {poly_fill['vwap']:.4f} > limite {MAX_VWAP_PER_LEG:.4f}"
                    )
                elif best.spread_pct < MIN_SPREAD_PCT:
                    print(
                        f"  [SKIP] spread {best.spread_pct:+.2f}% < minimo {MIN_SPREAD_PCT:.2f}%"
                    )
                else:
                    usd_order = max(MIN_TRADE_USD, simulated_bankroll * RISK_PCT)
                    print(
                        f"  [SIGNAL] Entrada aprovada | leg={best.name} | usd={usd_order:.2f} "
                        f"| poly_vwap={poly_fill['vwap']:.4f} | slippage={poly_fill['slippage']:.5f}"
                    )
                    if ENABLE_LIVE_POLY_ORDER:
                        try:
                            resp = execute_poly_fok_buy(poly_client, best.poly_token_id, usd_order)
                            print(f"  [LIVE] Ordem Poly FOK enviada: {resp}")
                        except Exception as e:
                            print(f"  [LIVE-ERRO] Falha ao enviar ordem Poly: {e}")
                    else:
                        print("  [PAPER] ENABLE_LIVE_POLY_ORDER=False (sem envio real).")
                    already_executed = True

            if tte < 0:
                print("Mercado expirou. Encerrando monitor.")
                break

            print("-" * 90)
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nMonitoramento encerrado pelo usuario.")
            break
        except Exception as e:
            print(f"Erro no loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    live_monitor()
