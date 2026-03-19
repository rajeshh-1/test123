import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import requests
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from web3 import Web3


GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
DATA_API_HOST = "https://data-api.polymarket.com"
CHAIN_ID = 137
AUDIT_CSV = "live_orders_audit.csv"
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_LIVE_ORDER"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
LOGS_DIR = Path("logs")

CTF_REDEEM_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# Preset vencedor atual dos logs:
# f88_v975_en5_d0_r30_m50_t45
BEST_PRESET = {
    "min_favored_price": 0.88,
    "max_vwap": 0.975,
    "risk_pct": 0.03,
    "min_usd": 0.50,
    "entry_seconds": 45,
    "min_entry_seconds": 10,
    "max_slippage_bps": 40.0,
}


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
            # Keep exported env vars with higher priority.
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class SelectedToken:
    market_id: str
    condition_id: str
    question: str
    outcome: str
    token_id: str
    midpoint: float
    end_date_utc: datetime | None


def get_event_by_slug(slug: str):
    r = requests.get(f"{GAMMA_HOST}/events?slug={slug}", timeout=10)
    r.raise_for_status()
    events = r.json()
    if not events:
        raise RuntimeError(f"Nenhum evento encontrado para slug={slug}")
    return events[0]


def build_auto_slug(coin: str, timeframe: str, entry_seconds: int = 90):
    now = datetime.now(timezone.utc)
    bucket = 5 if timeframe == "5m" else 15
    block_start = now.replace(minute=(now.minute // bucket) * bucket, second=0, microsecond=0)
    block_end_ts = int(block_start.timestamp()) + bucket * 60
    tte = block_end_ts - int(now.timestamp())
    # If current block doesn't have enough TTE for entry, use next block
    if tte < entry_seconds:
        next_start = int(block_start.timestamp()) + bucket * 60
        print(f"[AUTO-SLUG] bloco atual tte={tte}s < {entry_seconds}s, avancando para proximo bloco")
        return f"{coin}-updown-{timeframe}-{next_start}"
    start_ts = int(block_start.timestamp())
    return f"{coin}-updown-{timeframe}-{start_ts}"


def _parse_json_field(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _coin_from_slug(slug: str) -> str:
    if not slug:
        return "generic"
    return slug.split("-")[0] if "-" in slug else "generic"


def _infer_resolution_from_market(market: dict):
    """Infers resolution and winner using fields that are actually returned by Gamma."""
    outcomes = _parse_json_field(market.get("outcomes", [])) or []
    outcome_prices = _parse_json_field(market.get("outcomePrices", [])) or []
    prices = [_safe_float(p, 0.0) for p in outcome_prices]

    uma_status = str(market.get("umaResolutionStatus", "")).strip().lower()
    closed = bool(market.get("closed", False))
    accepting_orders = bool(market.get("acceptingOrders", True))

    winner = None
    winner_idx = None
    if outcomes and prices and len(outcomes) == len(prices):
        # For binary markets at resolution we expect 1.0 / 0.0.
        winner_idx = max(range(len(prices)), key=lambda i: prices[i])
        if prices[winner_idx] >= 0.999:
            winner = str(outcomes[winner_idx])

    resolved = (
        (uma_status == "resolved")
        or (winner is not None)
        or (closed and not accepting_orders and any(p >= 0.999 for p in prices))
    )
    return {
        "resolved": resolved,
        "winner": winner,
        "winner_idx": winner_idx,
        "prices": prices,
        "uma_status": uma_status,
        "closed": closed,
        "accepting_orders": accepting_orders,
    }


def select_token_from_slug(slug: str, preferred_outcome: str | None = None) -> SelectedToken:
    event = get_event_by_slug(slug)
    market = next((m for m in event.get("markets", []) if not m.get("closed", False)), None)
    if not market:
        raise RuntimeError("Nenhum mercado aberto encontrado neste evento.")

    outcomes = _parse_json_field(market.get("outcomes", []))
    token_ids = _parse_json_field(market.get("clobTokenIds", []))
    if not outcomes or not token_ids or len(outcomes) != len(token_ids):
        raise RuntimeError("Campo outcomes/clobTokenIds invalido no mercado.")

    mids = []
    for tid in token_ids:
        try:
            mr = requests.get(f"{CLOB_HOST}/midpoint?token_id={tid}", timeout=6)
            mr.raise_for_status()
            mids.append(float(mr.json().get("mid", 0.0)))
        except Exception:
            mids.append(0.0)

    if preferred_outcome:
        normalized = preferred_outcome.strip().lower()
        idx = next((i for i, o in enumerate(outcomes) if str(o).strip().lower() == normalized), None)
        if idx is None:
            raise RuntimeError(f"Outcome '{preferred_outcome}' nao encontrado. Outcomes disponiveis: {outcomes}")
    else:
        idx = max(range(len(outcomes)), key=lambda i: mids[i])

    return SelectedToken(
        market_id=str(market["id"]),
        condition_id=str(market.get("conditionId", "")),
        question=market.get("question", ""),
        outcome=str(outcomes[idx]),
        token_id=str(token_ids[idx]),
        midpoint=float(mids[idx]),
        end_date_utc=parse_end_date_utc(market.get("endDate")),
    )


def parse_end_date_utc(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def wait_for_entry_window(slug, preferred_outcome, entry_seconds, min_entry_seconds):
    print(
        f"[WAIT] aguardando janela de entrada: <= {entry_seconds}s e >= {min_entry_seconds}s para expirar..."
    )
    while True:
        selected = select_token_from_slug(slug, preferred_outcome)
        if not selected.end_date_utc:
            raise RuntimeError("Nao foi possivel ler endDate do mercado para controlar janela de entrada.")
        tte = (selected.end_date_utc - datetime.now(timezone.utc)).total_seconds()
        print(f"[WAIT] tte={tte:.1f}s | outcome={selected.outcome} | midpoint={selected.midpoint:.4f}")
        if min_entry_seconds <= tte <= entry_seconds:
            return selected
        if tte < 0:
            raise RuntimeError("Mercado ja expirou durante a espera.")
        time.sleep(0.2)


def _evaluate_entry_candidate(
    selected: SelectedToken,
    usd_to_use: float,
    min_favored_price: float,
    max_vwap: float,
    max_slippage_bps: float,
):
    if selected.end_date_utc:
        tte = (selected.end_date_utc - datetime.now(timezone.utc)).total_seconds()
    else:
        tte = None

    if selected.midpoint < min_favored_price:
        return {
            "ok": False,
            "reason": "favored_below_min",
            "selected": selected,
            "sim": None,
            "allowed_vwap": None,
            "tte": tte,
        }

    asks = get_asks(selected.token_id)
    sim = simulate_vwap_for_usd(asks, usd_to_use)
    if not sim:
        return {
            "ok": False,
            "reason": "no_liquidity",
            "selected": selected,
            "sim": None,
            "allowed_vwap": None,
            "tte": tte,
        }

    allowed_by_slippage = sim["top_ask"] * (1.0 + (max_slippage_bps / 10000.0))
    allowed_vwap = min(max_vwap, allowed_by_slippage)
    if sim["vwap"] > allowed_vwap:
        return {
            "ok": False,
            "reason": "vwap_limit",
            "selected": selected,
            "sim": sim,
            "allowed_vwap": allowed_vwap,
            "tte": tte,
        }

    return {
        "ok": True,
        "reason": "ok",
        "selected": selected,
        "sim": sim,
        "allowed_vwap": allowed_vwap,
        "tte": tte,
    }


def prepare_entry_with_retries_in_window(
    slug: str,
    preferred_outcome: str | None,
    usd_to_use: float,
    min_favored_price: float,
    max_vwap: float,
    max_slippage_bps: float,
    entry_seconds: int,
    min_entry_seconds: int,
):
    print(
        f"[ENTRY-WINDOW] monitorando entrada dinamica: tte <= {entry_seconds}s e >= {min_entry_seconds}s"
    )
    while True:
        selected = select_token_from_slug(slug, preferred_outcome)
        if not selected.end_date_utc:
            raise RuntimeError("Nao foi possivel ler endDate do mercado para controlar janela de entrada.")

        tte = (selected.end_date_utc - datetime.now(timezone.utc)).total_seconds()
        if tte < min_entry_seconds:
            raise RuntimeError(
                f"Janela de entrada encerrada sem sinal valido (tte={tte:.1f}s < {min_entry_seconds}s)."
            )
        if tte > entry_seconds:
            print(f"[ENTRY-WINDOW] aguardando inicio da janela | tte={tte:.1f}s")
            time.sleep(0.5)
            continue

        evaluation = _evaluate_entry_candidate(
            selected=selected,
            usd_to_use=usd_to_use,
            min_favored_price=min_favored_price,
            max_vwap=max_vwap,
            max_slippage_bps=max_slippage_bps,
        )
        reason = evaluation["reason"]
        sim = evaluation["sim"]
        if evaluation["ok"]:
            print(
                f"[ENTRY-WINDOW] sinal valido | tte={tte:.1f}s midpoint={selected.midpoint:.4f} "
                f"top_ask={sim['top_ask']:.4f} vwap={sim['vwap']:.4f}"
            )
            return evaluation

        if reason == "no_liquidity":
            extra = "sem liquidez"
        elif reason == "favored_below_min":
            extra = f"midpoint={selected.midpoint:.4f} < min={min_favored_price:.4f}"
        elif reason == "vwap_limit" and sim:
            extra = f"vwap={sim['vwap']:.4f} > limite={evaluation['allowed_vwap']:.4f}"
        else:
            extra = reason
        print(f"[ENTRY-WINDOW] bloqueado ({reason}) | tte={tte:.1f}s | {extra} | tentando novamente...")
        time.sleep(0.8)


def append_decision_row(path: str, row: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp_utc",
        "slug",
        "question",
        "outcome",
        "coin",
        "entry_seconds",
        "min_entry_seconds",
        "tte",
        "midpoint",
        "top_ask",
        "vwap",
        "allowed_vwap",
        "reason",
    ]
    target = Path(path)
    exists = target.is_file()
    with target.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _order_history_path(slug: str) -> Path:
    coin = _coin_from_slug(slug)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"order_history_{coin}.json"


def _load_recent_orders(path: Path, horizon_seconds: int = 3600) -> List[float]:
    now = time.time()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [t for t in data if now - float(t) <= horizon_seconds]
    except Exception:
        return []


def _save_recent_orders(path: Path, timestamps: List[float]):
    try:
        path.write_text(json.dumps(timestamps, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get_asks(token_id: str):
    r = requests.get(f"{CLOB_HOST}/book?token_id={token_id}", timeout=10)
    r.raise_for_status()
    asks = r.json().get("asks", [])
    parsed = [{"price": float(x["price"]), "size": float(x["size"])} for x in asks]
    parsed.sort(key=lambda x: x["price"])
    return parsed


def simulate_vwap_for_usd(asks, usd_amount: float):
    remaining = usd_amount
    shares = 0.0
    spent = 0.0
    if not asks:
        return None
    for ask in asks:
        level_cost = ask["price"] * ask["size"]
        if remaining >= level_cost:
            shares += ask["size"]
            spent += level_cost
            remaining -= level_cost
        else:
            s = remaining / ask["price"]
            shares += s
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
        "unfilled_usd": remaining,
    }


def check_geoblock():
    try:
        r = requests.get("https://polymarket.com/api/geoblock", timeout=8)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def build_client():
    private_key = os.getenv("POLY_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("Defina POLY_PRIVATE_KEY no ambiente.")
    signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    funder_raw = os.getenv("POLY_FUNDER", "")
    # Only use funder for proxy wallets (type 2) with a real hex address
    funder = funder_raw if (signature_type == 2 and funder_raw.startswith("0x") and "YOUR" not in funder_raw.upper()) else None

    client = ClobClient(
        CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=signature_type,
        funder=funder,
    )
    api_key = os.getenv('POLY_API_KEY')
    api_secret = os.getenv('POLY_API_SECRET')
    api_passphrase = os.getenv('POLY_API_PASSPHRASE')
    if api_key and api_secret and api_passphrase:
        client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase))
    else:
        client.set_api_creds(client.create_or_derive_api_creds())
    return client


def append_audit_row(row: dict):
    fieldnames = [
        "timestamp_utc",
        "mode",
        "status",
        "reason",
        "slug",
        "question",
        "market_id",
        "outcome",
        "token_id",
        "usd",
        "order_type",
        "max_vwap",
        "max_slippage_bps",
        "midpoint",
        "top_ask",
        "vwap_simulado",
        "limite_vwap",
        "shares_simuladas",
        "order_id",
        "fill_price_real",
        "fill_shares",
        "fill_usd",
        "slippage_bps_real",
        "fill_status",
        "pnl_usd",
        "response_json",
    ]
    exists = os.path.isfile(AUDIT_CSV)
    with open(AUDIT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def poll_order_fill(client, order_id: str, max_wait: int = 15):
    """Polls the CLOB for actual fill data on the order."""
    print(f"[FILL-POLL] Aguardando fill do order_id={order_id} (max {max_wait}s)...")
    for attempt in range(max_wait):
        try:
            order_data = client.get_order(order_id)
            status = order_data.get("status", "UNKNOWN") if isinstance(order_data, dict) else "UNKNOWN"
            size_matched = float(order_data.get("size_matched", 0)) if isinstance(order_data, dict) else 0.0
            price = float(order_data.get("price", 0)) if isinstance(order_data, dict) else 0.0
            print(f"  [{attempt+1}s] status={status} size_matched={size_matched} price={price}")
            if status in ("MATCHED", "FILLED", "CLOSED") or size_matched > 0:
                return {
                    "status": status,
                    "fill_price": price,
                    "fill_shares": size_matched,
                    "fill_usd": size_matched * price,
                    "raw": order_data,
                }
            if status in ("CANCELED", "CANCELLED", "EXPIRED"):
                return {"status": status, "fill_price": 0, "fill_shares": 0, "fill_usd": 0, "raw": order_data}
        except Exception as e:
            print(f"  [{attempt+1}s] erro ao consultar: {e}")
        time.sleep(1)
    return {"status": "TIMEOUT", "fill_price": 0, "fill_shares": 0, "fill_usd": 0, "raw": {}}


def poll_market_resolution(
    slug: str,
    token_id: str,
    max_wait: int = 360,
    poll_interval_seconds: int = 5,
    condition_id: str | None = None,
    our_outcome: str | None = None,
):
    """Polls market status until resolution or timeout (default 6min for 5m markets)."""
    interval = max(1, int(poll_interval_seconds))
    print(f"[RESOLUTION] Aguardando resolucao do mercado (max {max_wait}s, poll={interval}s)...")
    elapsed = 0
    while elapsed <= max_wait:
        try:
            event = get_event_by_slug(slug)
            for m in event.get("markets", []):
                if any(tid == token_id for tid in _parse_json_field(m.get("clobTokenIds", []))):
                    inferred = _infer_resolution_from_market(m)
                    if inferred["resolved"]:
                        print(
                            "  [RESOLVED] "
                            f"winner={inferred['winner']} "
                            f"uma_status={inferred['uma_status']} "
                            f"prices={inferred['prices']}"
                        )
                        return {"resolved": True, "winner": inferred["winner"], "market": m}
                    end_raw = m.get("endDate")
                    end_dt = parse_end_date_utc(end_raw)
                    if end_dt:
                        tte = (end_dt - datetime.now(timezone.utc)).total_seconds()
                        print(
                            f"  [{elapsed}s] nao resolvido ainda, tte={tte:.0f}s "
                            f"uma_status={inferred['uma_status']} "
                            f"closed={inferred['closed']} "
                            f"accepting_orders={inferred['accepting_orders']}"
                        )
                    # Fallback: if UI already shows claimable, Data API may know before Gamma fields update.
                    if condition_id:
                        rp = _find_redeemable_position(condition_id, our_outcome)
                        if rp is not None:
                            payout_per_share = _safe_float(rp.get("curPrice", 0.0), 0.0)
                            pos_outcome = str(rp.get("outcome", "")).strip()
                            winner = pos_outcome if payout_per_share >= 0.999 else None
                            print(
                                "  [RESOLVED-POSITIONS] "
                                f"outcome={pos_outcome} curPrice={payout_per_share:.4f} redeemable={rp.get('redeemable')}"
                            )
                            return {
                                "resolved": True,
                                "winner": winner,
                                "market": m,
                                "resolved_via": "positions_redeemable",
                                "payout_per_share": payout_per_share,
                            }
        except Exception as e:
            print(f"  [{elapsed}s] erro ao consultar resolucao: {e}")
        time.sleep(interval)
        elapsed += interval
    print("[RESOLUTION] timeout - mercado nao resolveu no periodo de espera.")
    return {"resolved": False, "winner": None, "market": {}}


def _build_resolution_users():
    users = []
    private_key = os.getenv("POLY_PRIVATE_KEY", "")
    funder = os.getenv("POLY_FUNDER", "")
    try:
        if private_key:
            users.append(_signer_address_from_private_key(private_key))
    except Exception:
        pass
    if funder and funder.startswith("0x"):
        users.append(funder)
    return list(dict.fromkeys([u for u in users if u]))


def _find_redeemable_position(condition_id: str, outcome: str | None = None):
    users = _build_resolution_users()
    if not users:
        return None
    for user in users:
        try:
            positions = get_redeemable_positions(user, condition_id=condition_id)
        except Exception:
            continue
        if not positions:
            continue
        if outcome:
            normalized = str(outcome).strip().lower()
            p = next((x for x in positions if str(x.get("outcome", "")).strip().lower() == normalized), None)
            if p:
                return p
        return positions[0]
    return None


def append_pnl_log(path: str, row: dict):
    exists = os.path.isfile(path)
    fields = [
        "timestamp_utc",
        "entry_timestamp_utc",
        "resolved_timestamp_utc",
        "slug",
        "question",
        "condition_id",
        "outcome",
        "token_id",
        "order_id",
        "fill_status",
        "fill_shares",
        "fill_usd",
        "payout_per_share",
        "pnl_usd",
        "pnl_status",
        "resolved_via",
        "winner",
    ]
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def get_redeemable_positions(user: str, condition_id: str | None = None, size: int = 200):
    params = {"user": user, "redeemable": "true", "size": str(size)}
    if condition_id:
        params["conditionId"] = condition_id
    r = requests.get(f"{DATA_API_HOST}/positions", params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _normalize_hex_32(value: str):
    if not value:
        return None
    v = str(value).strip().lower()
    if not v.startswith("0x"):
        return None
    if len(v) != 66:
        return None
    try:
        int(v, 16)
    except Exception:
        return None
    return v


def _group_positions_for_redeem(positions: list[dict]):
    grouped = {}
    for p in positions:
        condition_id = _normalize_hex_32(p.get("conditionId", ""))
        if not condition_id:
            continue
        neg_risk = bool(p.get("negativeRisk", False))
        key = (condition_id, neg_risk)
        grouped.setdefault(key, []).append(p)
    return grouped


def _build_web3():
    rpc = os.getenv("POLY_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        raise RuntimeError(f"Nao conectou no RPC: {rpc}")
    return w3


def _signer_address_from_private_key(private_key: str):
    return Account.from_key(private_key).address


def claim_condition_onchain(
    private_key: str,
    condition_id: str,
    negative_risk: bool = False,
    collateral_token: str = USDC_E_ADDRESS,
    index_sets: list[int] | None = None,
):
    cond = _normalize_hex_32(condition_id)
    if not cond:
        raise RuntimeError(f"conditionId invalido para claim: {condition_id}")

    index_sets = index_sets or [1, 2]
    signer = _signer_address_from_private_key(private_key)
    w3 = _build_web3()
    contract_address = NEG_RISK_ADAPTER_ADDRESS if negative_risk else CTF_ADDRESS
    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=CTF_REDEEM_ABI)
    nonce = w3.eth.get_transaction_count(signer, "pending")
    gas_price = w3.eth.gas_price

    tx = contract.functions.redeemPositions(
        Web3.to_checksum_address(collateral_token),
        b"\x00" * 32,
        bytes.fromhex(cond[2:]),
        index_sets,
    ).build_transaction(
        {
            "from": signer,
            "nonce": nonce,
            "chainId": CHAIN_ID,
            "gasPrice": gas_price,
            "value": 0,
        }
    )
    try:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    except Exception:
        tx["gas"] = 450000

    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = tx_hash.hex()
    print(f"[CLAIM] tx enviada: {tx_hex} | conditionId={cond} | negativeRisk={negative_risk}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    print(f"[CLAIM] receipt status={receipt.status} block={receipt.blockNumber}")
    if int(receipt.status) != 1:
        raise RuntimeError(f"Claim falhou on-chain. tx={tx_hex}")
    return {"tx_hash": tx_hex, "status": int(receipt.status), "block": int(receipt.blockNumber)}


def watch_and_optionally_claim(
    private_key: str,
    users_to_check: list[str],
    condition_id: str | None,
    poll_interval_seconds: int = 180,
    max_wait_seconds: int = 3600,
    do_claim: bool = False,
):
    started = time.time()
    claimable_found = []
    users = [u for u in users_to_check if u]
    if not users:
        raise RuntimeError("Sem endereco para consultar positions/redeemable.")

    while (time.time() - started) <= max_wait_seconds:
        cycle = int(time.time() - started)
        all_positions = []
        for user in users:
            try:
                positions = get_redeemable_positions(user, condition_id=condition_id)
                all_positions.extend(positions)
                print(f"[CLAIM-WATCH] user={user} redeemable={len(positions)}")
            except Exception as e:
                print(f"[CLAIM-WATCH] user={user} erro ao consultar positions: {e}")

        grouped = _group_positions_for_redeem(all_positions)
        if grouped:
            print(f"[CLAIM-WATCH] Encontrou {len(grouped)} condition(s) redeemable apos {cycle}s.")
            claimable_found = all_positions
            if not do_claim:
                return {"found": True, "claimed": [], "positions": all_positions}

            claimed = []
            for (cond, neg_risk), pos_list in grouped.items():
                try:
                    labels = ",".join(sorted({str(p.get('outcome', '')) for p in pos_list if p.get("outcome")}))
                    print(f"[CLAIM] condition={cond} negRisk={neg_risk} outcomes={labels}")
                    result = claim_condition_onchain(private_key, cond, negative_risk=neg_risk, index_sets=[1, 2])
                    claimed.append({"condition_id": cond, "negative_risk": neg_risk, "tx": result})
                except Exception as e:
                    print(f"[CLAIM] Falha ao claim condition={cond}: {e}")
            return {"found": True, "claimed": claimed, "positions": all_positions}

        print(f"[CLAIM-WATCH] Sem redeemable. proxima consulta em {poll_interval_seconds}s...")
        time.sleep(max(1, int(poll_interval_seconds)))

    return {"found": False, "claimed": [], "positions": claimable_found}


def main():
    load_env_file(".env")
    parser = argparse.ArgumentParser(description="Executor live Polymarket com FOK/FAK e controle de slippage.")
    parser.add_argument("--preset", choices=["best_logs_5m"], default="best_logs_5m")
    parser.add_argument(
        "--btc-once-test",
        action="store_true",
        help="Modo teste rapido: forca BTC + auto-slug 5m + dry-run (nao envia ordem).",
    )
    parser.add_argument("--slug", default=None, help="Ex.: btc-updown-5m-1772537700")
    parser.add_argument("--auto-slug", choices=["5m", "15m"], default=None, help="Gera slug automaticamente pelo timestamp atual.")
    parser.add_argument("--coin", choices=["btc", "eth", "sol", "xrp"], default="btc", help="Usado com --auto-slug.")
    parser.add_argument("--outcome", default=None, help="Ex.: Up ou Down. Se omitido, escolhe favorito.")
    parser.add_argument("--usd", type=float, default=None, help="Valor em USD. Se omitido, usa bankroll*risk_pct.")
    parser.add_argument("--bankroll", type=float, default=100.0, help="Usado para calcular size quando --usd nao for passado.")
    parser.add_argument("--risk-pct", type=float, default=BEST_PRESET["risk_pct"])
    parser.add_argument("--min-usd", type=float, default=BEST_PRESET["min_usd"])
    parser.add_argument("--min-favored-price", type=float, default=BEST_PRESET["min_favored_price"])
    parser.add_argument("--order-type", choices=["FOK", "FAK"], default="FOK")
    parser.add_argument("--max-vwap", type=float, default=BEST_PRESET["max_vwap"], help="Preco medio maximo aceito.")
    parser.add_argument(
        "--max-slippage-bps",
        type=float,
        default=BEST_PRESET["max_slippage_bps"],
        help="Slippage maximo sobre top ask em bps.",
    )
    parser.add_argument("--wait-entry-window", action="store_true", help="Espera janela de entrada antes de avaliar/enviar.")
    parser.add_argument("--entry-seconds", type=int, default=BEST_PRESET["entry_seconds"])
    parser.add_argument("--min-entry-seconds", type=int, default=BEST_PRESET["min_entry_seconds"])
    parser.add_argument("--live", action="store_true", help="Sem isso, roda apenas em dry-run.")
    parser.add_argument("--confirm", default="", help=f"Para live, informe exatamente: {LIVE_CONFIRM_PHRASE}")
    parser.add_argument("--resolution-max-wait-seconds", type=int, default=420, help="Timeout maximo para aguardar resolucao.")
    parser.add_argument("--resolution-poll-seconds", type=int, default=5, help="Intervalo de poll da resolucao (min 1s).")
    parser.add_argument("--defer-resolution", action="store_true", help="Nao bloqueia aguardando resolucao; finaliza ciclo com PnL pendente.")
    parser.add_argument("--pnl-log-file", default=None, help="Arquivo CSV enxuto para registrar apenas PnL final por trade.")
    parser.add_argument("--decision-log-file", default=None, help="CSV de decisoes (midpoint/top_ask/vwap) para auditoria rapida.")
    parser.add_argument(
        "--max-orders-per-hour",
        type=int,
        default=0,
        help="Limite de ordens LIVE por hora por ativo; 0 desativa. Usa logs/order_history_{coin}.json para persistir.",
    )
    parser.add_argument("--claim-watch", action="store_true", help="Monitora positions redeemable em loop.")
    parser.add_argument("--claim-watch-only", action="store_true", help="Nao envia ordem; apenas monitora e opcionalmente da claim.")
    parser.add_argument("--claim-condition-id", default=None, help="Filtra watcher por conditionId especifico (0x...).")
    parser.add_argument("--claim-interval-seconds", type=int, default=180, help="Intervalo de consulta de claim (default 180s).")
    parser.add_argument("--claim-max-wait-seconds", type=int, default=3600, help="Tempo maximo do watcher de claim.")
    parser.add_argument("--auto-claim", action="store_true", help="Quando encontrar redeemable, envia tx redeemPositions.")
    args = parser.parse_args()

    if args.btc_once_test:
        # Facilita smoke test de ponta a ponta sem risco de envio real.
        args.slug = None
        args.auto_slug = "5m"
        args.coin = "btc"
        args.live = False
        args.wait_entry_window = False
        if args.usd is None:
            args.usd = max(args.min_usd, 1.0)

    if args.slug is not None and args.auto_slug is not None:
        raise RuntimeError("Use apenas um: --slug ou --auto-slug.")
    if not args.claim_watch_only and args.slug is None and args.auto_slug is None:
        raise RuntimeError("Informe --slug ou use --auto-slug (ou use --claim-watch-only).")

    if args.claim_watch_only:
        load_env_file(".env")
        private_key = os.getenv("POLY_PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("Defina POLY_PRIVATE_KEY no ambiente para watcher/claim.")
        signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
        funder = os.getenv("POLY_FUNDER", "")
        signer_addr = _signer_address_from_private_key(private_key)
        users_to_check = [signer_addr]
        if funder and funder.startswith("0x"):
            users_to_check.append(funder)

        if args.auto_claim and signature_type == 2 and (not funder or signer_addr.lower() != funder.lower()):
            raise RuntimeError(
                "Auto-claim com signature_type=2 requer a private key da POLY_FUNDER para enviar tx on-chain."
            )

        print(
            f"[CLAIM-WATCH] only-mode users={users_to_check} interval={args.claim_interval_seconds}s "
            f"max_wait={args.claim_max_wait_seconds}s auto_claim={args.auto_claim}"
        )
        result = watch_and_optionally_claim(
            private_key=private_key,
            users_to_check=users_to_check,
            condition_id=args.claim_condition_id,
            poll_interval_seconds=args.claim_interval_seconds,
            max_wait_seconds=args.claim_max_wait_seconds,
            do_claim=args.auto_claim,
        )
        print(
            f"[CLAIM-WATCH] found={result.get('found')} "
            f"positions={len(result.get('positions', []))} "
            f"claimed={len(result.get('claimed', []))}"
        )
        if result.get("claimed"):
            print(json.dumps(result["claimed"], indent=2, ensure_ascii=False))
        return

    effective_slug = args.slug if args.slug else build_auto_slug(args.coin, args.auto_slug, args.entry_seconds)
    if args.auto_slug:
        print(f"[AUTO-SLUG] coin={args.coin} timeframe={args.auto_slug} -> {effective_slug}")

    usd_to_use = args.usd if args.usd is not None else max(args.min_usd, args.bankroll * args.risk_pct)

    mode = "live" if args.live else "dry_run"
    entry_ts = datetime.now(timezone.utc).isoformat()
    audit = {
        "timestamp_utc": entry_ts,
        "mode": mode,
        "status": "started",
        "reason": "",
        "slug": effective_slug,
        "question": "",
        "market_id": "",
        "outcome": args.outcome or "",
        "token_id": "",
        "usd": usd_to_use,
        "order_type": args.order_type,
        "max_vwap": args.max_vwap,
        "max_slippage_bps": args.max_slippage_bps,
        "midpoint": "",
        "top_ask": "",
        "vwap_simulado": "",
        "limite_vwap": "",
        "shares_simuladas": "",
        "response_json": "",
    }

    geo = check_geoblock()
    if geo is not None:
        print(f"[GEO] blocked={geo.get('blocked')} country={geo.get('country')} region={geo.get('region')}")
        if geo.get("blocked"):
            audit["status"] = "blocked"
            audit["reason"] = "geoblock"
            append_audit_row(audit)
            raise RuntimeError("IP bloqueado por geoblock. Abortando.")

    if args.wait_entry_window:
        prepared = prepare_entry_with_retries_in_window(
            slug=effective_slug,
            preferred_outcome=args.outcome,
            usd_to_use=usd_to_use,
            min_favored_price=args.min_favored_price,
            max_vwap=args.max_vwap,
            max_slippage_bps=args.max_slippage_bps,
            entry_seconds=args.entry_seconds,
            min_entry_seconds=args.min_entry_seconds,
        )
    else:
        selected_now = select_token_from_slug(effective_slug, args.outcome)
        if selected_now.end_date_utc:
            tte = (selected_now.end_date_utc - datetime.now(timezone.utc)).total_seconds()
            print(f"[INFO] tte atual: {tte:.1f}s")
        prepared = _evaluate_entry_candidate(
            selected=selected_now,
            usd_to_use=usd_to_use,
            min_favored_price=args.min_favored_price,
            max_vwap=args.max_vwap,
            max_slippage_bps=args.max_slippage_bps,
        )
        if not prepared["ok"]:
            selected_now = prepared["selected"]
            reason = prepared["reason"]
            if reason == "favored_below_min":
                msg = f"favored_below_min ({selected_now.midpoint:.4f} < {args.min_favored_price:.4f})"
            elif reason == "no_liquidity":
                msg = "no_liquidity"
            elif reason == "vwap_limit":
                sim_now = prepared["sim"]
                msg = f"vwap_limit ({sim_now['vwap']:.4f} > {prepared['allowed_vwap']:.4f})" if sim_now else "vwap_limit"
            else:
                msg = reason
            audit["status"] = "blocked"
            audit["reason"] = msg
            audit["question"] = selected_now.question
            audit["market_id"] = selected_now.market_id
            audit["outcome"] = selected_now.outcome
            audit["token_id"] = selected_now.token_id
            audit["midpoint"] = selected_now.midpoint
            append_audit_row(audit)
            raise RuntimeError(f"Ordem bloqueada: {msg}")

    selected = prepared["selected"]
    sim = prepared["sim"]
    allowed_vwap = prepared["allowed_vwap"]
    audit["question"] = selected.question
    audit["market_id"] = selected.market_id
    audit["outcome"] = selected.outcome
    audit["token_id"] = selected.token_id
    audit["midpoint"] = selected.midpoint

    audit["top_ask"] = sim["top_ask"]
    audit["vwap_simulado"] = sim["vwap"]
    audit["limite_vwap"] = allowed_vwap
    audit["shares_simuladas"] = sim["shares"]

    # Revalidacao final imediata antes de enviar a ordem
    recheck = _evaluate_entry_candidate(
        selected=select_token_from_slug(effective_slug, preferred_outcome=selected.outcome),
        usd_to_use=usd_to_use,
        min_favored_price=args.min_favored_price,
        max_vwap=args.max_vwap,
        max_slippage_bps=args.max_slippage_bps,
    )
    if not recheck["ok"]:
        reason = f"recheck_{recheck['reason']}"
        audit["status"] = "blocked"
        audit["reason"] = reason
        append_audit_row(audit)
        if args.decision_log_file:
            append_decision_row(
                args.decision_log_file,
                {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "slug": effective_slug,
                    "question": selected.question,
                    "outcome": selected.outcome,
                    "coin": _coin_from_slug(effective_slug),
                    "entry_seconds": args.entry_seconds,
                    "min_entry_seconds": args.min_entry_seconds,
                    "tte": recheck.get("tte", ""),
                    "midpoint": recheck["selected"].midpoint,
                    "top_ask": (recheck["sim"] or {}).get("top_ask", ""),
                    "vwap": (recheck["sim"] or {}).get("vwap", ""),
                    "allowed_vwap": recheck.get("allowed_vwap", ""),
                    "reason": reason,
                },
            )
        raise RuntimeError(f"Ordem bloqueada na revalidacao final: {reason}")
    selected = recheck["selected"]
    sim = recheck["sim"]
    allowed_vwap = recheck["allowed_vwap"]
    audit["midpoint"] = selected.midpoint
    audit["top_ask"] = sim["top_ask"]
    audit["vwap_simulado"] = sim["vwap"]
    audit["limite_vwap"] = allowed_vwap
    audit["shares_simuladas"] = sim["shares"]

    print("=== PREVIEW ORDEM ===")
    print(f"question: {selected.question}")
    print(f"outcome: {selected.outcome}")
    print(f"token_id: {selected.token_id}")
    print(f"midpoint: {selected.midpoint:.6f}")
    print(f"usd: {usd_to_use:.4f}")
    print(f"min_favored_price: {args.min_favored_price:.4f}")
    print(f"top_ask: {sim['top_ask']:.6f}")
    print(f"vwap_simulado: {sim['vwap']:.6f}")
    print(f"limite_vwap: {allowed_vwap:.6f}")
    print(f"shares_simuladas: {sim['shares']:.6f}")
    print(f"order_type: {args.order_type}")

    if sim["vwap"] > allowed_vwap:
        audit["status"] = "blocked"
        audit["reason"] = f"vwap_limit ({sim['vwap']:.4f} > {allowed_vwap:.4f})"
        append_audit_row(audit)
        raise RuntimeError("Ordem bloqueada: VWAP simulado acima do limite.")

    if args.decision_log_file:
        append_decision_row(
            args.decision_log_file,
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "slug": effective_slug,
                "question": selected.question,
                "outcome": selected.outcome,
                "coin": _coin_from_slug(effective_slug),
                "entry_seconds": args.entry_seconds,
                "min_entry_seconds": args.min_entry_seconds,
                "tte": recheck.get("tte", ""),
                "midpoint": selected.midpoint,
                "top_ask": sim["top_ask"],
                "vwap": sim["vwap"],
                "allowed_vwap": allowed_vwap,
                "reason": "final_ok",
            },
        )

    if not args.live:
        print("[DRY-RUN] Ordem nao enviada. Use --live para enviar de verdade.")
        audit["status"] = "dry_run_ok"
        audit["reason"] = "preview_only"
        append_audit_row(audit)
        return

    if args.confirm.strip() != LIVE_CONFIRM_PHRASE:
        audit["status"] = "blocked"
        audit["reason"] = "missing_confirm"
        append_audit_row(audit)
        raise RuntimeError(f'Para live use --confirm "{LIVE_CONFIRM_PHRASE}"')

    # Rate limit por hora persistente por ativo
    if args.max_orders_per_hour and args.max_orders_per_hour > 0:
        history_path = _order_history_path(effective_slug)
        recent_orders = _load_recent_orders(history_path, horizon_seconds=3600)
        if len(recent_orders) >= args.max_orders_per_hour:
            audit["status"] = "blocked"
            audit["reason"] = f"rate_limit_hour ({len(recent_orders)} >= {args.max_orders_per_hour})"
            append_audit_row(audit)
            raise RuntimeError("Limite de ordens por hora atingido para este ativo.")

    client = build_client()
    market_order = MarketOrderArgs(
        token_id=selected.token_id,
        amount=float(usd_to_use),
        side=BUY,
        order_type=getattr(OrderType, args.order_type),
    )
    signed = client.create_market_order(market_order)
    resp = client.post_order(signed, getattr(OrderType, args.order_type))
    if args.live and args.max_orders_per_hour and args.max_orders_per_hour > 0:
        recent_orders.append(time.time())
        _save_recent_orders(history_path, recent_orders)

    print("=== RESPOSTA CLOB ===")
    print(json.dumps(resp, indent=2, ensure_ascii=False) if isinstance(resp, dict) else resp)

    # --- Extract order ID ---
    order_id = ""
    if isinstance(resp, dict):
        order_id = resp.get("orderID", resp.get("order_id", resp.get("id", "")))
    audit["order_id"] = order_id
    audit["response_json"] = json.dumps(resp, ensure_ascii=False) if isinstance(resp, dict) else str(resp)

    # --- Poll fill data ---
    fill = {"status": "NO_ORDER_ID", "fill_price": 0, "fill_shares": 0, "fill_usd": 0}
    if order_id:
        fill = poll_order_fill(client, order_id, max_wait=15)

    audit["fill_status"] = fill["status"]
    audit["fill_price_real"] = fill["fill_price"]
    audit["fill_shares"] = fill["fill_shares"]
    audit["fill_usd"] = fill["fill_usd"]

    # --- Slippage real ---
    if fill["fill_price"] > 0 and sim["top_ask"] > 0:
        slippage_real = ((fill["fill_price"] - sim["top_ask"]) / sim["top_ask"]) * 10000.0
    else:
        slippage_real = 0.0
    audit["slippage_bps_real"] = round(slippage_real, 2)

    # --- Wait for market resolution and compute PnL ---
    if args.defer_resolution:
        resolution = {"resolved": False, "winner": None, "market": {}, "resolved_via": "deferred"}
        print("[RESOLUTION] defer_resolution ativo: nao aguardando settlement neste ciclo.")
        print("[PENDING] PnL pendente para conciliacao posterior.")
    else:
        resolution = poll_market_resolution(
            effective_slug,
            selected.token_id,
            max_wait=args.resolution_max_wait_seconds,
            poll_interval_seconds=args.resolution_poll_seconds,
            condition_id=selected.condition_id,
            our_outcome=selected.outcome,
        )

    pnl = 0.0
    if resolution["resolved"]:
        outcomes_list = _parse_json_field(resolution["market"].get("outcomes", []))
        outcome_prices = [_safe_float(p, 0.0) for p in (_parse_json_field(resolution["market"].get("outcomePrices", [])) or [])]
        winning_outcome = resolution["winner"]
        our_outcome = selected.outcome
        if outcomes_list and outcome_prices and len(outcomes_list) == len(outcome_prices):
            our_idx = next(
                (i for i, o in enumerate(outcomes_list) if str(o).strip().lower() == str(our_outcome).strip().lower()),
                None,
            )
        else:
            our_idx = None
        payout_per_share = outcome_prices[our_idx] if (our_idx is not None and our_idx < len(outcome_prices)) else None
        if payout_per_share is None and resolution.get("payout_per_share") is not None:
            payout_per_share = _safe_float(resolution.get("payout_per_share"), 0.0)

        if str(our_outcome).strip().lower() == str(winning_outcome).strip().lower():
            # Won: shares pay $1 each
            pnl = fill["fill_shares"] - fill["fill_usd"]
            print(f"\n[WIN] Nosso outcome '{our_outcome}' GANHOU! PnL = +${pnl:.4f}")
        elif payout_per_share is not None:
            payout_value = fill["fill_shares"] * payout_per_share
            pnl = payout_value - fill["fill_usd"]
            print(
                f"\n[SETTLED] winner={winning_outcome} | payout_per_share={payout_per_share:.6f} "
                f"-> PnL = ${pnl:+.4f}"
            )
        else:
            # Lost: shares worth $0
            pnl = -fill["fill_usd"]
            print(f"\n[LOSS] Nosso outcome '{our_outcome}' PERDEU (winner={winning_outcome}). PnL = -${abs(pnl):.4f}")
    else:
        print("\n[PENDING] Mercado nao resolveu no periodo de espera. PnL pendente.")

    audit["pnl_usd"] = round(pnl, 6)
    audit["status"] = "live_completed" if resolution["resolved"] else "live_sent_pending"
    audit["reason"] = f"fill={fill['status']} resolved={resolution['resolved']}"

    # --- Final Report ---
    print("\n" + "=" * 55)
    print("         RELATORIO PnL — TESTE BTC UNICO")
    print("=" * 55)
    print(f"  Slug:              {effective_slug}")
    print(f"  Pergunta:          {selected.question}")
    print(f"  Outcome apostado:  {selected.outcome}")
    print(f"  Order ID:          {order_id}")
    print(f"  Fill Status:       {fill['status']}")
    print(f"  ---")
    print(f"  USD enviado:       ${usd_to_use:.4f}")
    print(f"  Top Ask (book):    {sim['top_ask']:.6f}")
    print(f"  VWAP simulado:     {sim['vwap']:.6f}")
    print(f"  Fill price real:   {fill['fill_price']:.6f}")
    print(f"  Shares recebidas:  {fill['fill_shares']:.6f}")
    print(f"  USD gasto real:    ${fill['fill_usd']:.4f}")
    print(f"  Slippage real:     {slippage_real:.2f} bps")
    print(f"  ---")
    if resolution["resolved"]:
        print(f"  Mercado resolvido: SIM")
        print(f"  Winner:            {resolution['winner']}")
        print(f"  PnL:               ${pnl:+.4f}")
    else:
        print(f"  Mercado resolvido: NAO (timeout)")
        print(f"  PnL:               PENDENTE")
    print("=" * 55)

    if args.pnl_log_file:
        pnl_status = "RESOLVED" if resolution["resolved"] else "PENDING"
        resolved_ts = datetime.now(timezone.utc).isoformat() if resolution["resolved"] else ""
        append_pnl_log(
            args.pnl_log_file,
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "entry_timestamp_utc": entry_ts,
                "resolved_timestamp_utc": resolved_ts,
                "slug": effective_slug,
                "question": selected.question,
                "condition_id": selected.condition_id,
                "outcome": selected.outcome,
                "token_id": selected.token_id,
                "order_id": order_id,
                "fill_status": fill["status"],
                "fill_shares": fill["fill_shares"],
                "fill_usd": fill["fill_usd"],
                "payout_per_share": resolution.get("payout_per_share", ""),
                "pnl_usd": round(pnl, 6) if resolution["resolved"] else "",
                "pnl_status": pnl_status,
                "resolved_via": resolution.get("resolved_via", "gamma"),
                "winner": resolution.get("winner", ""),
            },
        )
        print(f"[PNL-LOG] registrado em {args.pnl_log_file} status={pnl_status}")

    if args.claim_watch:
        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
        funder = os.getenv("POLY_FUNDER", "")
        signer_addr = _signer_address_from_private_key(private_key) if private_key else ""
        users_to_check = [signer_addr] if signer_addr else []
        if funder and funder.startswith("0x"):
            users_to_check.append(funder)
        users_to_check = list(dict.fromkeys(users_to_check))

        if args.auto_claim and signature_type == 2 and (not funder or signer_addr.lower() != funder.lower()):
            print(
                "[CLAIM-WATCH] auto-claim desativado: signature_type=2 sem private key da POLY_FUNDER. "
                "Mantendo apenas monitoramento."
            )
            do_claim = False
        else:
            do_claim = args.auto_claim

        target_condition_id = args.claim_condition_id or selected.condition_id
        print(
            f"[CLAIM-WATCH] users={users_to_check} condition={target_condition_id} "
            f"interval={args.claim_interval_seconds}s max_wait={args.claim_max_wait_seconds}s auto_claim={do_claim}"
        )
        claim_result = watch_and_optionally_claim(
            private_key=private_key,
            users_to_check=users_to_check,
            condition_id=target_condition_id,
            poll_interval_seconds=args.claim_interval_seconds,
            max_wait_seconds=args.claim_max_wait_seconds,
            do_claim=do_claim,
        )
        print(
            f"[CLAIM-WATCH] resultado found={claim_result.get('found')} "
            f"positions={len(claim_result.get('positions', []))} "
            f"claimed={len(claim_result.get('claimed', []))}"
        )
        if claim_result.get("claimed"):
            print(json.dumps(claim_result["claimed"], indent=2, ensure_ascii=False))

    append_audit_row(audit)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERRO] {e}")
        sys.exit(1)
