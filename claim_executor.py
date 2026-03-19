import argparse
import json
import os
import time
from typing import Any

import requests
from eth_account import Account
from web3 import Web3


DATA_API_HOST = "https://data-api.polymarket.com"
CHAIN_ID = 137
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

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


def load_env_file(path: str):
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


def _normalize_hex_32(value: str):
    if not value:
        return None
    v = str(value).strip().lower()
    if not v.startswith("0x") or len(v) != 66:
        return None
    try:
        int(v, 16)
    except Exception:
        return None
    return v


def signer_address_from_private_key(private_key: str):
    return Account.from_key(private_key).address


def build_web3():
    rpc = os.getenv("POLY_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        raise RuntimeError(f"Nao conectou no RPC: {rpc}")
    return w3


def get_redeemable_positions(user: str, condition_id: str | None = None, size: int = 200):
    params = {"user": user, "redeemable": "true", "size": str(size)}
    if condition_id:
        params["conditionId"] = condition_id
    r = requests.get(f"{DATA_API_HOST}/positions", params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def group_positions_for_redeem(positions: list[dict[str, Any]]):
    grouped = {}
    for p in positions:
        condition_id = _normalize_hex_32(p.get("conditionId", ""))
        if not condition_id:
            continue
        neg_risk = bool(p.get("negativeRisk", False))
        key = (condition_id, neg_risk)
        grouped.setdefault(key, []).append(p)
    return grouped


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
    signer = signer_address_from_private_key(private_key)
    w3 = build_web3()
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


def watch_redeemable(
    private_key: str,
    users_to_check: list[str],
    condition_id: str | None,
    poll_interval_seconds: int,
    max_wait_seconds: int,
    auto_claim: bool,
    claim_wallet: str | None = None,
):
    started = time.time()
    users = [u for u in users_to_check if u]
    if not users:
        raise RuntimeError("Sem endereco para consultar positions/redeemable.")

    while (time.time() - started) <= max_wait_seconds:
        elapsed = int(time.time() - started)
        all_positions = []
        for user in users:
            try:
                positions = get_redeemable_positions(user, condition_id=condition_id)
                print(f"[CLAIM-WATCH] user={user} redeemable={len(positions)}")
                all_positions.extend(positions)
            except Exception as e:
                print(f"[CLAIM-WATCH] user={user} erro ao consultar positions: {e}")

        grouped = group_positions_for_redeem(all_positions)
        if grouped:
            print(f"[CLAIM-WATCH] Encontrou {len(grouped)} condition(s) redeemable apos {elapsed}s.")
            if not auto_claim:
                return {"found": True, "positions": len(all_positions), "claimed": 0, "txs": []}

            txs = []
            for (cond, neg_risk), pos_list in grouped.items():
                if claim_wallet:
                    # Direct on-chain redeem burns positions from msg.sender; skip other wallets.
                    wallets = {str(p.get("proxyWallet", "")).lower() for p in pos_list}
                    if claim_wallet.lower() not in wallets:
                        print(
                            f"[CLAIM] skip condition={cond} (positions em wallet diferente do signer: {wallets})"
                        )
                        continue
                labels = ",".join(sorted({str(p.get('outcome', '')) for p in pos_list if p.get("outcome")}))
                print(f"[CLAIM] condition={cond} negRisk={neg_risk} outcomes={labels}")
                try:
                    result = claim_condition_onchain(private_key, cond, negative_risk=neg_risk, index_sets=[1, 2])
                    txs.append(result)
                except Exception as e:
                    print(f"[CLAIM] Falha ao claim condition={cond}: {e}")
            return {"found": True, "positions": len(all_positions), "claimed": len(txs), "txs": txs}

        print(f"[CLAIM-WATCH] Sem redeemable. proxima consulta em {poll_interval_seconds}s...")
        time.sleep(max(1, int(poll_interval_seconds)))

    return {"found": False, "positions": 0, "claimed": 0, "txs": []}


def main():
    parser = argparse.ArgumentParser(description="Runner separado para monitorar e executar claim Polymarket.")
    parser.add_argument("--env-file", default=".env.claim", help="Arquivo .env dedicado ao claim runner.")
    parser.add_argument("--condition-id", default=None, help="Filtra apenas um conditionId (0x...).")
    parser.add_argument("--interval-seconds", type=int, default=180, help="Intervalo de poll (default 180s).")
    parser.add_argument("--max-wait-seconds", type=int, default=3600, help="Tempo maximo de espera do watcher.")
    parser.add_argument("--auto-claim", action="store_true", help="Se achar redeemable, envia tx on-chain.")
    args = parser.parse_args()

    load_env_file(args.env_file)
    private_key = os.getenv("POLY_PRIVATE_KEY", "")
    signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    funder = os.getenv("POLY_FUNDER", "").strip()
    if not private_key:
        raise RuntimeError(f"POLY_PRIVATE_KEY ausente em {args.env_file}.")

    signer = signer_address_from_private_key(private_key)
    users = [signer]
    if funder.startswith("0x"):
        users.append(funder)
    users = list(dict.fromkeys(users))

    can_claim_funder_direct = bool(funder and signer.lower() == funder.lower())
    if args.auto_claim and signature_type == 2 and not can_claim_funder_direct:
        print(
            "[WARN] signature_type=2 com signer != POLY_FUNDER. "
            "Claim direto via tx on-chain so funciona para posicoes na wallet do signer. "
            "Posicoes da proxy/funder exigem fluxo Safe/relayer."
        )

    print(
        f"[CLAIM-RUNNER] env={args.env_file} users={users} interval={args.interval_seconds}s "
        f"max_wait={args.max_wait_seconds}s auto_claim={args.auto_claim}"
    )
    result = watch_redeemable(
        private_key=private_key,
        users_to_check=users,
        condition_id=args.condition_id,
        poll_interval_seconds=args.interval_seconds,
        max_wait_seconds=args.max_wait_seconds,
        auto_claim=args.auto_claim,
        claim_wallet=signer if args.auto_claim else None,
    )
    print(
        f"[CLAIM-RUNNER] done found={result['found']} positions={result['positions']} "
        f"claimed={result['claimed']}"
    )
    if result["txs"]:
        print(json.dumps(result["txs"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
