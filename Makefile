.PHONY: compile test check

compile:
	python -m compileall -q bot scripts tests logs/arb_engine logs/run_arb_dry_run.py logs/live_direct_arb.py logs/analyze_arb.py

test:
	python -m pytest -q

check: compile test
