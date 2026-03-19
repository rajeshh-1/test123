import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPILE_CMD = [
    sys.executable,
    "-m",
    "compileall",
    "-q",
    "bot",
    "scripts",
    "tests",
    "logs/arb_engine",
    "logs/run_arb_dry_run.py",
    "logs/live_direct_arb.py",
    "logs/analyze_arb.py",
]
TEST_CMD = [sys.executable, "-m", "pytest", "-q"]


def _run(cmd: list[str]) -> int:
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quality gate helper for environments without make.")
    parser.add_argument("target", choices=["compile", "test", "check"])
    args = parser.parse_args(argv)

    if args.target == "compile":
        return _run(COMPILE_CMD)
    if args.target == "test":
        return _run(TEST_CMD)

    rc = _run(COMPILE_CMD)
    if rc != 0:
        return rc
    return _run(TEST_CMD)


if __name__ == "__main__":
    raise SystemExit(main())
