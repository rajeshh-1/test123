import argparse
from pathlib import Path

import pandas as pd


def parse_args():
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Analyze Kalshi vs Polymarket BTC 15m arbitrage logs.")
    parser.add_argument("--kalshi-file", default=str(script_dir / "kalshi_btc_15m_ticks.csv"))
    parser.add_argument("--poly-file", default=str(script_dir / "poly_btc_15m_ticks.csv"))
    parser.add_argument("--opps-csv", default=str(script_dir / "arb_opportunities.csv"))
    parser.add_argument("--diag-csv", default=str(script_dir / "arb_diagnostics.csv"))
    parser.add_argument("--summary-file", default=str(script_dir / "arb_results.txt"))
    parser.add_argument("--tolerance-sec", type=float, default=1.0)
    parser.add_argument("--fee-poly-bps", type=float, default=25.0)
    parser.add_argument("--fee-kalshi-bps", type=float, default=0.0)
    parser.add_argument("--min-edge-pct", type=float, default=5.0)
    parser.add_argument("--slippage-expected-bps", type=float, default=0.0)
    parser.add_argument("--leg-risk-cost", type=float, default=0.0)
    parser.add_argument("--payout-expected", type=float, default=1.0)
    return parser.parse_args()


def ensure_col(df: pd.DataFrame, col: str, default):
    if col not in df.columns:
        df[col] = default


def to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True)


def to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def market_key_from_close(series: pd.Series) -> pd.Series:
    dt = to_utc(series)
    return "BTC15M_" + dt.dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_diag(df: pd.DataFrame, source: str, error_code: str, detail: str) -> pd.DataFrame:
    out = pd.DataFrame()
    out["timestamp_utc"] = df.get("timestamp_utc", pd.Series(dtype="datetime64[ns, UTC]"))
    out["sec"] = df.get("sec", pd.Series(dtype="datetime64[ns, UTC]"))
    out["source"] = source
    out["row_status"] = "invalid"
    out["error_code"] = error_code
    out["detail"] = detail
    out["market_key_k"] = df.get("market_key", "")
    out["market_key_p"] = ""
    return out


def prepare_kalshi(df: pd.DataFrame) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    diags = []

    ensure_col(df, "timestamp_utc", "")
    ensure_col(df, "row_status", "valid")
    ensure_col(df, "error_code", "")
    ensure_col(df, "market_close_utc", "")
    ensure_col(df, "close_time", "")
    ensure_col(df, "market_key", "")
    ensure_col(df, "ticker", "")
    ensure_col(df, "yes_ask", "")
    ensure_col(df, "no_ask", "")

    df["timestamp_utc"] = to_utc(df["timestamp_utc"])
    close_series = df["market_close_utc"].where(df["market_close_utc"].astype(str).str.len() > 0, df["close_time"])
    close_dt = to_utc(close_series)
    df["market_close_utc"] = close_dt.dt.strftime("%Y-%m-%dT%H:%M:%SZ").fillna("")

    missing_key = df["market_key"].astype(str).str.len() == 0
    df.loc[missing_key, "market_key"] = market_key_from_close(close_series).fillna("")

    df["yes_ask"] = to_num(df["yes_ask"])
    df["no_ask"] = to_num(df["no_ask"])
    df["ob_yes_best_ask"] = to_num(df.get("ob_yes_best_ask", pd.Series(index=df.index, dtype="float64")))
    df["ob_no_best_ask"] = to_num(df.get("ob_no_best_ask", pd.Series(index=df.index, dtype="float64")))

    df["sec"] = df["timestamp_utc"].dt.floor("s")

    upstream_invalid = df["row_status"].astype(str).str.lower().ne("valid")
    if upstream_invalid.any():
        invalid_df = df.loc[upstream_invalid].copy()
        invalid_df["detail"] = invalid_df["error_code"].replace("", "upstream_invalid")
        diags.append(build_diag(invalid_df, "kalshi", "upstream_invalid", "row_status!=valid"))

    base_invalid = df["timestamp_utc"].isna() | (df["market_key"].astype(str).str.len() == 0) | df["sec"].isna()
    if base_invalid.any():
        diags.append(build_diag(df.loc[base_invalid], "kalshi", "type_mismatch", "missing timestamp/market_key"))

    quote_missing = df["yes_ask"].isna() | df["no_ask"].isna()
    if quote_missing.any():
        diags.append(build_diag(df.loc[quote_missing], "kalshi", "missing_book_side", "missing yes_ask or no_ask"))

    valid = (~upstream_invalid) & (~base_invalid) & (~quote_missing)
    clean = df.loc[valid].copy()
    clean = clean.sort_values("timestamp_utc").groupby(["market_key", "sec"], as_index=False).tail(1)
    return clean, diags


def prepare_poly(df: pd.DataFrame) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    diags = []

    ensure_col(df, "timestamp_utc", "")
    ensure_col(df, "row_status", "valid")
    ensure_col(df, "error_code", "")
    ensure_col(df, "market_close_utc", "")
    ensure_col(df, "market_key", "")
    ensure_col(df, "slug", "")
    ensure_col(df, "up_best_ask", "")
    ensure_col(df, "down_best_ask", "")

    df["timestamp_utc"] = to_utc(df["timestamp_utc"])

    missing_close = df["market_close_utc"].astype(str).str.len() == 0
    if missing_close.any():
        slug_ts = pd.to_numeric(df.loc[missing_close, "slug"].astype(str).str.extract(r"(\d+)$")[0], errors="coerce")
        close_dt = pd.to_datetime(slug_ts, unit="s", utc=True, errors="coerce") + pd.Timedelta(minutes=15)
        df.loc[missing_close, "market_close_utc"] = close_dt.dt.strftime("%Y-%m-%dT%H:%M:%SZ").fillna("")

    missing_key = df["market_key"].astype(str).str.len() == 0
    df.loc[missing_key, "market_key"] = market_key_from_close(df["market_close_utc"]).fillna("")

    df["up_best_ask"] = to_num(df["up_best_ask"])
    df["down_best_ask"] = to_num(df["down_best_ask"])
    df["sec"] = df["timestamp_utc"].dt.floor("s")

    upstream_invalid = df["row_status"].astype(str).str.lower().ne("valid")
    if upstream_invalid.any():
        diags.append(build_diag(df.loc[upstream_invalid], "poly", "upstream_invalid", "row_status!=valid"))

    base_invalid = df["timestamp_utc"].isna() | (df["market_key"].astype(str).str.len() == 0) | df["sec"].isna()
    if base_invalid.any():
        diags.append(build_diag(df.loc[base_invalid], "poly", "type_mismatch", "missing timestamp/market_key"))

    quote_missing = df["up_best_ask"].isna() | df["down_best_ask"].isna()
    if quote_missing.any():
        diags.append(build_diag(df.loc[quote_missing], "poly", "missing_book_side", "missing up_best_ask or down_best_ask"))

    valid = (~upstream_invalid) & (~base_invalid) & (~quote_missing)
    clean = df.loc[valid].copy()
    clean = clean.sort_values("timestamp_utc").groupby(["market_key", "sec"], as_index=False).tail(1)
    return clean, diags


def analyze(args):
    kalshi_path = Path(args.kalshi_file)
    poly_path = Path(args.poly_file)
    opps_path = Path(args.opps_csv)
    diag_path = Path(args.diag_csv)
    summary_path = Path(args.summary_file)

    if not kalshi_path.exists():
        raise FileNotFoundError(f"Kalshi file not found: {kalshi_path}")
    if not poly_path.exists():
        raise FileNotFoundError(f"Poly file not found: {poly_path}")

    k_raw = pd.read_csv(kalshi_path)
    p_raw = pd.read_csv(poly_path)

    k, k_diags = prepare_kalshi(k_raw.copy())
    p, p_diags = prepare_poly(p_raw.copy())

    diags = []
    diags.extend(k_diags)
    diags.extend(p_diags)

    k_keys = k[["market_key", "sec"]].copy()
    p_keys = p[["market_key", "sec"]].copy()
    shared_keys = len(set(map(tuple, k_keys.to_numpy())) & set(map(tuple, p_keys.to_numpy())))

    merged = pd.merge(
        k,
        p,
        on=["market_key", "sec"],
        how="inner",
        suffixes=("_k", "_p"),
    )

    inflation_factor = (len(merged) / shared_keys) if shared_keys else 0.0

    k_unmatched = k_keys.merge(p_keys, on=["market_key", "sec"], how="left", indicator=True)
    k_unmatched = k_unmatched[k_unmatched["_merge"] == "left_only"]
    if not k_unmatched.empty:
        diag = k_unmatched.copy()
        diag["timestamp_utc"] = pd.NaT
        diag["source"] = "join"
        diag["row_status"] = "invalid"
        diag["error_code"] = "invalid_market_mismatch"
        diag["detail"] = "kalshi_key_without_poly_match"
        diag["market_key_k"] = diag["market_key"]
        diag["market_key_p"] = ""
        diags.append(diag[["timestamp_utc", "sec", "source", "row_status", "error_code", "detail", "market_key_k", "market_key_p"]])

    p_unmatched = p_keys.merge(k_keys, on=["market_key", "sec"], how="left", indicator=True)
    p_unmatched = p_unmatched[p_unmatched["_merge"] == "left_only"]
    if not p_unmatched.empty:
        diag = p_unmatched.copy()
        diag["timestamp_utc"] = pd.NaT
        diag["source"] = "join"
        diag["row_status"] = "invalid"
        diag["error_code"] = "invalid_market_mismatch"
        diag["detail"] = "poly_key_without_kalshi_match"
        diag["market_key_k"] = ""
        diag["market_key_p"] = diag["market_key"]
        diags.append(diag[["timestamp_utc", "sec", "source", "row_status", "error_code", "detail", "market_key_k", "market_key_p"]])

    k_sec = k.sort_values("timestamp_utc").groupby("sec", as_index=False).tail(1)[["sec", "market_key"]]
    p_sec = p.sort_values("timestamp_utc").groupby("sec", as_index=False).tail(1)[["sec", "market_key"]]
    sec_cmp = pd.merge(k_sec, p_sec, on="sec", suffixes=("_k", "_p"))
    sec_mismatch = sec_cmp[sec_cmp["market_key_k"] != sec_cmp["market_key_p"]]
    if not sec_mismatch.empty:
        diag = sec_mismatch.copy()
        diag["timestamp_utc"] = pd.NaT
        diag["source"] = "join"
        diag["row_status"] = "invalid"
        diag["error_code"] = "market_rollover_race"
        diag["detail"] = "different market_key at same second"
        diags.append(diag[["timestamp_utc", "sec", "source", "row_status", "error_code", "detail", "market_key_k", "market_key_p"]])

    if not merged.empty:
        merged["time_diff_sec"] = (
            (merged["timestamp_utc_k"] - merged["timestamp_utc_p"]).abs().dt.total_seconds()
        )
        stale = merged["time_diff_sec"] > float(args.tolerance_sec)
        if stale.any():
            stale_df = merged.loc[stale, ["timestamp_utc_k", "sec", "market_key"]].copy()
            stale_df.rename(columns={"timestamp_utc_k": "timestamp_utc"}, inplace=True)
            stale_df["source"] = "join"
            stale_df["row_status"] = "invalid"
            stale_df["error_code"] = "stale_quotes"
            stale_df["detail"] = f"time_diff_sec>{args.tolerance_sec}"
            stale_df["market_key_k"] = stale_df["market_key"]
            stale_df["market_key_p"] = stale_df["market_key"]
            diags.append(stale_df[["timestamp_utc", "sec", "source", "row_status", "error_code", "detail", "market_key_k", "market_key_p"]])
        merged = merged.loc[~stale].copy()
    else:
        merged["time_diff_sec"] = pd.Series(dtype="float64")

    if not merged.empty:
        missing_join_quotes = (
            merged["yes_ask"].isna()
            | merged["no_ask"].isna()
            | merged["up_best_ask"].isna()
            | merged["down_best_ask"].isna()
        )
        if missing_join_quotes.any():
            miss_df = merged.loc[missing_join_quotes, ["timestamp_utc_k", "sec", "market_key"]].copy()
            miss_df.rename(columns={"timestamp_utc_k": "timestamp_utc"}, inplace=True)
            miss_df["source"] = "join"
            miss_df["row_status"] = "invalid"
            miss_df["error_code"] = "missing_book_side"
            miss_df["detail"] = "missing ask values after merge"
            miss_df["market_key_k"] = miss_df["market_key"]
            miss_df["market_key_p"] = miss_df["market_key"]
            diags.append(miss_df[["timestamp_utc", "sec", "source", "row_status", "error_code", "detail", "market_key_k", "market_key_p"]])
        merged = merged.loc[~missing_join_quotes].copy()

    fee_k = float(args.fee_kalshi_bps) / 10000.0
    fee_p = float(args.fee_poly_bps) / 10000.0
    slip_rate = float(args.slippage_expected_bps) / 10000.0
    leg_risk_cost = float(args.leg_risk_cost)
    payout_expected = float(args.payout_expected)

    if merged.empty:
        merged["price_total_a"] = pd.Series(dtype="float64")
        merged["price_total_b"] = pd.Series(dtype="float64")
        merged["fees_a"] = pd.Series(dtype="float64")
        merged["fees_b"] = pd.Series(dtype="float64")
        merged["slippage_a"] = pd.Series(dtype="float64")
        merged["slippage_b"] = pd.Series(dtype="float64")
        merged["cost_a"] = pd.Series(dtype="float64")
        merged["cost_b"] = pd.Series(dtype="float64")
        merged["edge_liquido_a"] = pd.Series(dtype="float64")
        merged["edge_liquido_b"] = pd.Series(dtype="float64")
        merged["edge_a_pct"] = pd.Series(dtype="float64")
        merged["edge_b_pct"] = pd.Series(dtype="float64")
    else:
        merged["price_total_a"] = merged["yes_ask"] + merged["down_best_ask"]
        merged["price_total_b"] = merged["no_ask"] + merged["up_best_ask"]
        merged["fees_a"] = (merged["yes_ask"] * fee_k) + (merged["down_best_ask"] * fee_p)
        merged["fees_b"] = (merged["no_ask"] * fee_k) + (merged["up_best_ask"] * fee_p)
        merged["slippage_a"] = merged["price_total_a"] * slip_rate
        merged["slippage_b"] = merged["price_total_b"] * slip_rate
        merged["cost_a"] = merged["price_total_a"] + merged["fees_a"] + merged["slippage_a"] + leg_risk_cost
        merged["cost_b"] = merged["price_total_b"] + merged["fees_b"] + merged["slippage_b"] + leg_risk_cost
        merged["edge_liquido_a"] = payout_expected - merged["cost_a"]
        merged["edge_liquido_b"] = payout_expected - merged["cost_b"]
        merged["edge_a_pct"] = (merged["edge_liquido_a"] / merged["cost_a"]) * 100.0
        merged["edge_b_pct"] = (merged["edge_liquido_b"] / merged["cost_b"]) * 100.0

    min_edge = float(args.min_edge_pct)
    opp_a = merged[(merged["edge_liquido_a"] > 0.0) & (merged["edge_a_pct"] >= min_edge)].copy()
    opp_b = merged[(merged["edge_liquido_b"] > 0.0) & (merged["edge_b_pct"] >= min_edge)].copy()

    opp_a["strategy"] = "A_KALSHI_YES_PLUS_POLY_DOWN"
    opp_a["cost"] = opp_a["cost_a"]
    opp_a["edge_pct"] = opp_a["edge_a_pct"]
    opp_a["edge_liquido"] = opp_a["edge_liquido_a"]
    opp_a["kalshi_leg_price"] = opp_a["yes_ask"]
    opp_a["poly_leg_price"] = opp_a["down_best_ask"]
    opp_a["price_total"] = opp_a["price_total_a"]
    opp_a["fees"] = opp_a["fees_a"]
    opp_a["slippage_esperado"] = opp_a["slippage_a"]
    opp_a["custo_leg_risk"] = leg_risk_cost

    opp_b["strategy"] = "B_KALSHI_NO_PLUS_POLY_UP"
    opp_b["cost"] = opp_b["cost_b"]
    opp_b["edge_pct"] = opp_b["edge_b_pct"]
    opp_b["edge_liquido"] = opp_b["edge_liquido_b"]
    opp_b["kalshi_leg_price"] = opp_b["no_ask"]
    opp_b["poly_leg_price"] = opp_b["up_best_ask"]
    opp_b["price_total"] = opp_b["price_total_b"]
    opp_b["fees"] = opp_b["fees_b"]
    opp_b["slippage_esperado"] = opp_b["slippage_b"]
    opp_b["custo_leg_risk"] = leg_risk_cost

    common_cols = [
        "sec",
        "market_key",
        "timestamp_utc_k",
        "timestamp_utc_p",
        "time_diff_sec",
        "ticker",
        "slug",
        "strategy",
        "cost",
        "edge_pct",
        "edge_liquido",
        "price_total",
        "fees",
        "slippage_esperado",
        "custo_leg_risk",
        "kalshi_leg_price",
        "poly_leg_price",
        "yes_ask",
        "no_ask",
        "up_best_ask",
        "down_best_ask",
        "fee_kalshi_bps",
        "fee_poly_bps",
        "slippage_expected_bps",
        "payout_expected",
    ]

    for frame in (opp_a, opp_b):
        frame["fee_kalshi_bps"] = args.fee_kalshi_bps
        frame["fee_poly_bps"] = args.fee_poly_bps
        frame["slippage_expected_bps"] = args.slippage_expected_bps
        frame["payout_expected"] = args.payout_expected

    opportunities = pd.concat(
        [opp_a[common_cols], opp_b[common_cols]],
        axis=0,
        ignore_index=True,
    ).sort_values(["edge_pct", "cost"], ascending=[False, True])

    if diags:
        diagnostics = pd.concat(diags, axis=0, ignore_index=True)
    else:
        diagnostics = pd.DataFrame(
            columns=["timestamp_utc", "sec", "source", "row_status", "error_code", "detail", "market_key_k", "market_key_p"]
        )

    opps_path.parent.mkdir(parents=True, exist_ok=True)
    diag_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    opportunities.to_csv(opps_path, index=False)
    diagnostics.to_csv(diag_path, index=False)

    lines = []
    lines.append(f"kalshi_rows_raw={len(k_raw)}")
    lines.append(f"poly_rows_raw={len(p_raw)}")
    lines.append(f"kalshi_rows_valid={len(k)}")
    lines.append(f"poly_rows_valid={len(p)}")
    lines.append(f"merged_rows={len(merged)}")
    lines.append(f"shared_keys={shared_keys}")
    lines.append(f"merge_inflation_factor={inflation_factor:.4f}")
    lines.append(f"opportunities_total={len(opportunities)}")
    lines.append(f"opportunities_A={len(opp_a)}")
    lines.append(f"opportunities_B={len(opp_b)}")
    lines.append(f"max_edge_A_pct={(opp_a['edge_pct'].max() if not opp_a.empty else 0.0):.4f}")
    lines.append(f"max_edge_B_pct={(opp_b['edge_pct'].max() if not opp_b.empty else 0.0):.4f}")
    lines.append(f"fee_kalshi_bps={args.fee_kalshi_bps}")
    lines.append(f"fee_poly_bps={args.fee_poly_bps}")
    lines.append(f"slippage_expected_bps={args.slippage_expected_bps}")
    lines.append(f"leg_risk_cost={args.leg_risk_cost}")
    lines.append(f"payout_expected={args.payout_expected}")

    if not diagnostics.empty:
        lines.append("diagnostics_by_error_code:")
        counts = diagnostics["error_code"].fillna("").replace("", "unknown").value_counts()
        for code, count in counts.items():
            lines.append(f"  - {code}: {int(count)}")
    else:
        lines.append("diagnostics_by_error_code:")
        lines.append("  - none: 0")

    summary_text = "\n".join(lines) + "\n"
    summary_path.write_text(summary_text, encoding="utf-8")
    print(summary_text, end="")


def main():
    args = parse_args()
    try:
        analyze(args)
    except Exception as e:
        print(f"Error in analysis: {e}")
        raise


if __name__ == "__main__":
    main()
