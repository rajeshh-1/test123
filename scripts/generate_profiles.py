import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bot.crypto_updown.runtime.execution_profile import (
    generate_execution_profiles_30,
    save_profiles_json,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate 30 pessimistic execution profiles for crypto runtime stress tests.")
    parser.add_argument("--out-file", default="configs/execution_profiles_30.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profiles = generate_execution_profiles_30()
    output = save_profiles_json(profiles, Path(args.out_file))
    print(f"generated_profiles={len(profiles)}")
    print(f"output_file={output.resolve()}")
    print(f"baseline_profile={profiles[0].name}")
    print(f"stress_profile={profiles[-5].name}")
    print(f"crash_profile={profiles[-1].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
