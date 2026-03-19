import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_required_operational_docs_exist():
    required = [
        ROOT / "docs" / "CRYPTO_RUNTIME_RUNBOOK.md",
        ROOT / "docs" / "SPORTS_RUNTIME_RUNBOOK.md",
        ROOT / "docs" / "INCIDENT_PLAYBOOKS.md",
        ROOT / "docs" / "PRE_LIVE_CHECKLIST.md",
    ]
    for path in required:
        assert path.exists(), f"missing required doc: {path.name}"


def test_runbooks_contain_required_sections():
    crypto = (ROOT / "docs" / "CRYPTO_RUNTIME_RUNBOOK.md").read_text(encoding="utf-8")
    sports = (ROOT / "docs" / "SPORTS_RUNTIME_RUNBOOK.md").read_text(encoding="utf-8")
    for needle in [
        "Visao Geral do Fluxo",
        "Comandos Paper",
        "Comandos Live",
        "Variaveis de Ambiente Obrigatorias",
        "Validacao Pre-Start",
        "Leitura de Logs",
        "Stop Seguro",
        "Troubleshooting Rapido",
    ]:
        assert needle in crypto
        assert needle in sports


def test_incident_playbooks_contain_required_scenarios():
    text = (ROOT / "docs" / "INCIDENT_PLAYBOOKS.md").read_text(encoding="utf-8")
    scenarios = [
        "API down / timeout persistente",
        "Orderbook sem liquidez",
        "Partial fill recorrente",
        "Hedge failure",
        "Market mismatch",
        "Circuit breaker acionado",
        "Kill switch acionado indevidamente",
        "Divergencia de PnL entre fontes",
    ]
    for scenario in scenarios:
        assert scenario in text


def test_documented_paper_commands_smoke():
    cmds = [
        ["scripts/crypto_cli.py", "--execution-mode", "paper", "--min-edge-pct", "5", "--min-liquidity", "1"],
        ["scripts/sports_cli.py", "--execution-mode", "paper", "--market-scope", "moneyline"],
        ["scripts/arb_cli.py", "--execution-mode", "paper", "--min-edge-pct", "5", "--min-liquidity", "1"],
        ["logs/run_arb_dry_run.py", "--help"],
    ]
    for cmd in cmds:
        res = _run(cmd)
        assert res.returncode == 0, f"command failed: {' '.join(cmd)}\nstdout={res.stdout}\nstderr={res.stderr}"
