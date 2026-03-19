import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


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


def main():
    parser = argparse.ArgumentParser(
        description="Loop runner para live_executor.py (1 terminal por moeda/mercado)."
    )
    parser.add_argument("--coin", choices=["btc", "eth", "sol", "xrp"], required=True)
    parser.add_argument("--timeframe", choices=["5m", "15m"], default="5m")
    parser.add_argument("--pause-seconds", type=int, default=2, help="Pausa entre ciclos.")
    parser.add_argument("--max-cycles", type=int, default=0, help="0 = infinito.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--python-bin",
        default=sys.executable,
        help="Interpretador Python para chamar live_executor.py.",
    )
    parser.add_argument(
        "--extra-args",
        default="",
        help='Args extras para live_executor.py, ex: "--usd 1.0 --order-type FOK".',
    )
    args = parser.parse_args()

    load_env_file(args.env_file)
    confirm = os.getenv("LIVE_CONFIRM", "I_UNDERSTAND_LIVE_ORDER")

    cycle = 0
    while True:
        cycle += 1
        if args.max_cycles > 0 and cycle > args.max_cycles:
            print("[MARKET-LOOP] max-cycles atingido. encerrando.")
            return

        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n[MARKET-LOOP] ciclo={cycle} coin={args.coin} timeframe={args.timeframe} ts={ts}")

        cmd = [
            args.python_bin,
            "live_executor.py",
            "--auto-slug",
            args.timeframe,
            "--coin",
            args.coin,
            "--live",
            "--confirm",
            confirm,
        ]
        if args.extra_args.strip():
            cmd.extend(args.extra_args.strip().split())

        print("[MARKET-LOOP] cmd:", " ".join(cmd))
        proc = subprocess.run(cmd, text=True)
        print(f"[MARKET-LOOP] rc={proc.returncode}")
        time.sleep(max(1, int(args.pause_seconds)))


if __name__ == "__main__":
    main()
