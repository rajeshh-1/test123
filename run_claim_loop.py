import argparse
import subprocess
import time
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(
        description="Loop runner para claim_relayer.mjs (claim em paralelo)."
    )
    parser.add_argument("--env-file", default=".env.claim")
    parser.add_argument("--interval-seconds", type=int, default=180)
    parser.add_argument("--max-claims", type=int, default=10)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 = infinito.")
    parser.add_argument("--node-bin", default="node")
    parser.add_argument(
        "--include-zero",
        action="store_true",
        help="Se ativo, tenta claimar tambem posicoes sem payout estimado.",
    )
    args = parser.parse_args()

    cycle = 0
    while True:
        cycle += 1
        if args.max_cycles > 0 and cycle > args.max_cycles:
            print("[CLAIM-LOOP] max-cycles atingido. encerrando.")
            return

        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n[CLAIM-LOOP] ciclo={cycle} ts={ts}")
        cmd = [
            args.node_bin,
            "claim_relayer.mjs",
            "--env-file",
            args.env_file,
            "--max-claims",
            str(args.max_claims),
        ]
        if args.include_zero:
            cmd.append("--include-zero")
        print("[CLAIM-LOOP] cmd:", " ".join(cmd))
        proc = subprocess.run(cmd, text=True)
        print(f"[CLAIM-LOOP] rc={proc.returncode} | dormindo {args.interval_seconds}s")
        time.sleep(max(5, int(args.interval_seconds)))


if __name__ == "__main__":
    main()
