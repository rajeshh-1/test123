"""
live_multi_test.py — Executa 1 trade por coin (BTC, ETH, SOL, XRP) em paralelo.

Cada coin roda em uma thread separada, esperando sua janela de 45s e
executando exatamente uma ordem. O resultado de resolucao e PnL e
impresso ao final de cada thread, sem bloquear as demais.

Uso:
    python live_multi_test.py --usd 1.0 --live --confirm I_UNDERSTAND_LIVE_ORDER
"""
import argparse
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

from live_executor import (
    BEST_PRESET,
    BUY,
    CHAIN_ID,
    CLOB_HOST,
    LIVE_CONFIRM_PHRASE,
    MarketOrderArgs,
    OrderType,
    _infer_resolution_from_market,
    _parse_json_field,
    _safe_float,
    append_audit_row,
    build_auto_slug,
    build_client,
    check_geoblock,
    get_asks,
    get_event_by_slug,
    load_env_file,
    parse_end_date_utc,
    poll_order_fill,
    simulate_vwap_for_usd,
    wait_for_entry_window,
)

# --- Color helpers ---
RESET = ""
GREEN = ""
RED = ""
CYAN = ""
YELLOW = ""
MAGENTA = ""

COIN_COLORS = {
    "btc": "",
    "eth": "",
    "sol": "",
    "xrp": "",
}


def coin_log(coin: str, msg: str):
    print(f"[{coin.upper()}] {msg}", flush=True)


def poll_market_resolution_once(slug: str, token_id: str, coin: str, max_wait: int = 420):
    """Non-blocking resolution poller for a single coin's market."""
    coin_log(coin, f"[RESOLUTION] Aguardando resolucao (max {max_wait}s)...")
    for elapsed in range(0, max_wait, 5):
        try:
            event = get_event_by_slug(slug)
            for m in event.get("markets", []):
                parsed_ids = _parse_json_field(m.get("clobTokenIds", []))
                if any(tid == token_id for tid in parsed_ids):
                    inferred = _infer_resolution_from_market(m)
                    if inferred["resolved"]:
                        coin_log(coin, f"[RESOLVED] winner={inferred['winner']} uma_status={inferred['uma_status']} prices={inferred['prices']}")
                        return {"resolved": True, "winner": inferred["winner"], "market": m}
                    end_dt = parse_end_date_utc(m.get("endDate"))
                    tte = int((end_dt - datetime.now(timezone.utc)).total_seconds()) if end_dt else "?"
                    coin_log(coin, f"  [{elapsed}s] nao resolvido, tte={tte}s closed={inferred['closed']}")
                    break
        except Exception as e:
            coin_log(coin, f"  [{elapsed}s] erro: {e}")
        time.sleep(5)
    coin_log(coin, "[RESOLUTION] timeout.")
    return {"resolved": False, "winner": None, "market": {}}


def run_coin(coin: str, args, results: dict):
    """Full flow for a single coin: wait entry window → continuous scan → order → resolve → PnL."""
    slug = None
    try:
        coin_log(coin, "Iniciando...")
        slug = build_auto_slug(coin, "5m", args.entry_seconds)
        coin_log(coin, f"Slug: {slug}")

        # --- Phase 1: Wait until we enter the entry window ---
        from live_executor import select_token_from_slug, get_event_by_slug, parse_end_date_utc
        coin_log(coin, f"Aguardando janela de entrada (<= {args.entry_seconds}s)...")
        last_print = 0.0
        while True:
            selected = select_token_from_slug(slug, None)
            if not selected.end_date_utc:
                raise RuntimeError("Nao foi possivel ler endDate do mercado.")
            tte = (selected.end_date_utc - datetime.now(timezone.utc)).total_seconds()
            if tte <= 0:
                raise RuntimeError("Mercado expirou sem atingir janela de entrada.")
            if tte <= args.entry_seconds:
                break  # Entered the window!
            now_ts = time.time()
            if now_ts - last_print >= 5:  # Print every 5s to show life
                coin_log(coin, f"  [aguardando] tte={tte:.0f}s | mid={selected.midpoint:.4f} | janela abre em ~{tte - args.entry_seconds:.0f}s")
                last_print = now_ts
            time.sleep(0.8)

        # --- Phase 2: Continuous scan WITHIN the entry window ---
        coin_log(coin, f"Janela aberta! Monitorando até tte <= {args.min_entry_seconds}s...")
        chosen = None
        chosen_sim = None
        chosen_allowed_vwap = None

        while True:
            selected = select_token_from_slug(slug, None)
            tte = (selected.end_date_utc - datetime.now(timezone.utc)).total_seconds() if selected.end_date_utc else 0

            if tte < args.min_entry_seconds:
                coin_log(coin, f"SKIP: janela encerrada (tte={tte:.1f}s < min {args.min_entry_seconds}s) sem condicao favoravel.")
                results[coin] = {"status": "skipped", "reason": "window_expired_no_entry"}
                return

            coin_log(coin, f"  [scan] tte={tte:.1f}s outcome={selected.outcome} mid={selected.midpoint:.4f}")

            # Gate 1: min_favored_price
            if selected.midpoint < args.min_favored_price:
                time.sleep(0.8)
                continue

            # Gate 2: Liquidity + VWAP
            asks = get_asks(selected.token_id)
            sim = simulate_vwap_for_usd(asks, args.usd)
            if not sim:
                coin_log(coin, f"  [gate] sem liquidez no livro (tte={tte:.1f}s), aguardando...")
                time.sleep(0.8)
                continue

            allowed_vwap = min(args.max_vwap, sim["top_ask"] * (1 + args.max_slippage_bps / 10000))
            if sim["vwap"] > allowed_vwap:
                coin_log(coin, f"  [gate] vwap {sim['vwap']:.4f} > {allowed_vwap:.4f}, aguardando...")
                time.sleep(0.8)
                continue

            # All conditions met!
            chosen = selected
            chosen_sim = sim
            chosen_allowed_vwap = allowed_vwap
            coin_log(coin, f"CONDICOES ATINGIDAS: outcome={chosen.outcome} mid={chosen.midpoint:.4f} vwap={sim['vwap']:.4f} tte={tte:.1f}s")
            break

        # --- Phase 3: Preview ---
        coin_log(coin, f"PREVIEW: top_ask={chosen_sim['top_ask']:.4f} vwap={chosen_sim['vwap']:.4f} shares={chosen_sim['shares']:.4f}")

        if not args.live:
            coin_log(coin, "DRY-RUN: ordem nao enviada.")
            results[coin] = {"status": "dry_run_ok", "outcome": chosen.outcome, "midpoint": chosen.midpoint}
            return

        # --- Phase 4: Post order ---
        # Stagger API calls to avoid race condition when all coins trigger simultaneously
        COIN_ORDER = ["btc", "eth", "sol", "xrp"]
        delay = COIN_ORDER.index(coin) * 0.6 if coin in COIN_ORDER else 0
        if delay > 0:
            coin_log(coin, f"  [stagger] aguardando {delay:.1f}s antes de enviar para evitar colisao...")
            time.sleep(delay)
        client = build_client()
        market_order = MarketOrderArgs(
            token_id=chosen.token_id,
            amount=float(args.usd),
            side=BUY,
            order_type=getattr(OrderType, args.order_type),
        )
        signed = client.create_market_order(market_order)
        resp = client.post_order(signed, getattr(OrderType, args.order_type))

        order_id = ""
        if isinstance(resp, dict):
            order_id = resp.get("orderID", resp.get("order_id", resp.get("id", "")))
        coin_log(coin, f"ORDEM: order_id={order_id} status={resp.get('status') if isinstance(resp, dict) else resp}")

        # --- Phase 5: Poll fill ---
        fill = {"status": "NO_ORDER_ID", "fill_price": 0, "fill_shares": 0, "fill_usd": 0}
        if order_id:
            fill = poll_order_fill(client, order_id, max_wait=15)
        coin_log(coin, f"FILL: status={fill['status']} price={fill['fill_price']:.4f} shares={fill['fill_shares']:.4f} usd=${fill['fill_usd']:.4f}")

        # --- Phase 6: Resolution ---
        resolution = poll_market_resolution_once(slug, chosen.token_id, coin, max_wait=420)

        pnl = 0.0
        if resolution["resolved"]:
            outcome_prices = [_safe_float(p, 0.0) for p in (_parse_json_field(resolution["market"].get("outcomePrices", [])) or [])]
            outcomes_list = _parse_json_field(resolution["market"].get("outcomes", [])) or []
            our_idx = next((i for i, o in enumerate(outcomes_list) if str(o).strip().lower() == str(chosen.outcome).strip().lower()), None)
            payout_per_share = outcome_prices[our_idx] if (our_idx is not None and our_idx < len(outcome_prices)) else None

            if str(chosen.outcome).strip().lower() == str(resolution["winner"]).strip().lower():
                pnl = fill["fill_shares"] - fill["fill_usd"]
                coin_log(coin, f"WIN! PnL = +${pnl:.4f} 🎉")
            elif payout_per_share is not None:
                pnl = fill["fill_shares"] * payout_per_share - fill["fill_usd"]
                coin_log(coin, f"SETTLED: winner={resolution['winner']} PnL = ${pnl:+.4f}")
            else:
                pnl = -fill["fill_usd"]
                coin_log(coin, f"LOSS: winner={resolution['winner']} PnL = -${abs(pnl):.4f}")
        else:
            coin_log(coin, "PENDING: mercado nao resolveu no timeout.")

        results[coin] = {
            "status": "live_completed" if resolution["resolved"] else "live_sent_pending",
            "outcome": chosen.outcome,
            "fill_price": fill["fill_price"],
            "fill_shares": fill["fill_shares"],
            "fill_usd": fill["fill_usd"],
            "pnl": pnl,
            "order_id": order_id,
            "winner": resolution.get("winner"),
        }

        # --- Append to audit CSV ---
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        audit = {
            "timestamp_utc": _dt.now(_tz.utc).isoformat(),
            "mode": "live",
            "status": results[coin]["status"],
            "reason": f"fill={fill['status']} resolved={resolution['resolved']}",
            "slug": slug,
            "question": chosen.question,
            "market_id": chosen.market_id,
            "outcome": chosen.outcome,
            "token_id": chosen.token_id,
            "usd": args.usd,
            "order_type": args.order_type,
            "max_vwap": args.max_vwap,
            "max_slippage_bps": args.max_slippage_bps,
            "midpoint": chosen.midpoint,
            "top_ask": chosen_sim["top_ask"],
            "vwap_simulado": chosen_sim["vwap"],
            "limite_vwap": chosen_allowed_vwap,
            "shares_simuladas": chosen_sim["shares"],
            "order_id": order_id,
            "fill_price_real": fill["fill_price"],
            "fill_shares": fill["fill_shares"],
            "fill_usd": fill["fill_usd"],
            "slippage_bps_real": round(((fill["fill_price"] - chosen_sim["top_ask"]) / chosen_sim["top_ask"]) * 10000, 2) if fill["fill_price"] > 0 and chosen_sim["top_ask"] > 0 else 0,
            "fill_status": fill["status"],
            "pnl_usd": round(pnl, 6),
            "response_json": _json.dumps(resp, ensure_ascii=False) if isinstance(resp, dict) else str(resp),
        }
        append_audit_row(audit)

    except Exception as e:
        coin_log(coin, f"ERRO: {e}")
        results[coin] = {"status": "error", "reason": str(e)}


def print_final_report(results: dict, args):
    print("\n" + "=" * 60)
    print("       RELATORIO FINAL — MULTI-COIN LIVE TEST")
    print("=" * 60)
    total_pnl = 0.0
    total_usd = 0.0
    for coin, r in results.items():
        color = COIN_COLORS.get(coin, RESET)
        status = r.get("status", "?")
        if status in ("live_completed", "live_sent_pending"):
            pnl = r.get("pnl", 0.0)
            total_pnl += pnl
            total_usd += r.get("fill_usd", 0.0)
            pnl_str = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"
            winner_str = f"winner={r.get('winner')}" if r.get("winner") else "PENDING"
            print(f"{color}  {coin.upper():4s} | {r.get('outcome','?')} @ {r.get('fill_price',0):.4f} | PnL={pnl_str} | {winner_str}{RESET}")
        else:
            print(f"{color}  {coin.upper():4s} | {status} ({r.get('reason', '')}){RESET}")
    print("-" * 60)
    print(f"  Total USD gasto:  ${total_usd:.4f}")
    print(f"  Total PnL:        ${total_pnl:+.4f}")
    print("=" * 60)


def main():
    load_env_file(".env")
    parser = argparse.ArgumentParser(description="Multi-coin live tester — 1 trade por coin em paralelo.")
    parser.add_argument("--coins", nargs="+", default=["btc", "eth", "sol", "xrp"], help="Coins para rodar.")
    parser.add_argument("--usd", type=float, default=1.0, help="USD por trade.")
    parser.add_argument("--min-favored-price", type=float, default=BEST_PRESET["min_favored_price"])
    parser.add_argument("--max-vwap", type=float, default=BEST_PRESET["max_vwap"])
    parser.add_argument("--max-slippage-bps", type=float, default=BEST_PRESET["max_slippage_bps"])
    parser.add_argument("--entry-seconds", type=int, default=BEST_PRESET["entry_seconds"])
    parser.add_argument("--min-entry-seconds", type=int, default=BEST_PRESET["min_entry_seconds"])
    parser.add_argument("--order-type", choices=["FOK", "FAK"], default="FAK")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if args.live and args.confirm.strip() != LIVE_CONFIRM_PHRASE:
        print(f'[ERRO] Para live use --confirm "{LIVE_CONFIRM_PHRASE}"')
        sys.exit(1)

    # Geoblock check
    geo = check_geoblock()
    if geo and geo.get("blocked"):
        print(f"[GEO] Bloqueado: {geo}")
        sys.exit(1)

    coins = [c.lower() for c in args.coins]
    print(f"{'='*60}")
    print(f"  MULTI-COIN LIVE TEST — coins={coins} usd=${args.usd} live={args.live}")
    print(f"  min_favored={args.min_favored_price} max_vwap={args.max_vwap} entry_seconds={args.entry_seconds}s")
    print(f"{'='*60}\n")

    results = {}
    threads = []
    for coin in coins:
        t = threading.Thread(target=run_coin, args=(coin, args, results), daemon=True, name=f"coin-{coin}")
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print_final_report(results, args)


if __name__ == "__main__":
    main()
