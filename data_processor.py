"""Data cleaning, monthly alignment, rebasing (t0=100), and gap calculation."""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from config import MSA_METADATA


def _align_and_fill_msa(
    msa_df: pd.DataFrame, fill_method: str = "linear_ffill"
) -> pd.DataFrame:
    """Pivot a single MSA's metrics to monthly columns and fill missing months."""
    pivoted = msa_df.pivot_table(
        index="date", columns="metric", values="value", aggfunc="mean"
    ).sort_index()

    required_cols = ["food_cpi", "energy_cpi", "hourly_earnings"]
    for col in required_cols:
        if col not in pivoted.columns:
            pivoted[col] = np.nan

    pivoted = pivoted[required_cols]
    if pivoted.empty:
        return pivoted

    full_index = pd.date_range(
        start=pivoted.index.min(), end=pivoted.index.max(), freq="MS"
    )
    pivoted = pivoted.reindex(full_index)
    pivoted.index.name = "date"

    if fill_method == "ffill":
        pivoted = pivoted.ffill().bfill()
    else:
        # Linear interpolation for bi-monthly CPI gaps, plus ffill/bfill at boundaries
        pivoted = pivoted.interpolate(method="linear", limit_area="inside")
        pivoted = pivoted.ffill().bfill()

    return pivoted


def _rebase_series(series: pd.Series) -> pd.Series:
    """Rebase a time series so that its first valid observation equals 100.0."""
    valid = series.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=series.index)
    base_value = float(valid.iloc[0])
    if base_value == 0.0 or np.isnan(base_value):
        raise ValueError("Base period (t0) value is zero or invalid; cannot rebase.")
    return (series / base_value) * 100.0


def process_msa_data(
    raw_df: pd.DataFrame,
    fill_method: str = "linear_ffill",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Clean raw BLS observations, align monthly series, rebase to t0=100, and compute gaps.

    Returns:
        processed_df: Monthly time series for all MSAs with raw values, 100-based indices,
                      and wage-inflation gaps.
        summary_df:   Cross-MSA summary table with cumulative % changes and net real gaps.
    """
    if raw_df is None or raw_df.empty:
        raise ValueError("No raw data available to process.")

    df = raw_df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["date", "value", "msa_name", "metric"])

    if df.empty:
        raise ValueError("No valid rows with parseable dates and numeric values found.")

    processed_frames: List[pd.DataFrame] = []
    summary_rows: List[Dict[str, object]] = []

    for msa_name, msa_group in df.groupby("msa_name"):
        try:
            aligned = _align_and_fill_msa(msa_group, fill_method=fill_method)
            aligned = aligned.dropna(
                subset=["food_cpi", "energy_cpi", "hourly_earnings"]
            )
            if aligned.empty:
                continue

            aligned["food_index"] = _rebase_series(aligned["food_cpi"])
            aligned["energy_index"] = _rebase_series(aligned["energy_cpi"])
            aligned["earnings_index"] = _rebase_series(aligned["hourly_earnings"])

            # Wage - Inflation Gaps (negative => wages lagged behind inflation)
            aligned["food_gap"] = aligned["earnings_index"] - aligned["food_index"]
            aligned["energy_gap"] = aligned["earnings_index"] - aligned["energy_index"]
            aligned["combined_cpi_index"] = (
                aligned["food_index"] + aligned["energy_index"]
            ) / 2.0
            aligned["net_real_gap"] = (
                aligned["earnings_index"] - aligned["combined_cpi_index"]
            )

            aligned["msa_name"] = msa_name
            aligned["short_name"] = MSA_METADATA.get(msa_name, {}).get(
                "short_name", msa_name
            )
            aligned = aligned.reset_index()
            processed_frames.append(aligned)

            latest = aligned.iloc[-1]
            first = aligned.iloc[0]
            food_pct = float(latest["food_index"] - 100.0)
            energy_pct = float(latest["energy_index"] - 100.0)
            earnings_pct = float(latest["earnings_index"] - 100.0)
            food_gap_val = float(latest["food_gap"])
            energy_gap_val = float(latest["energy_gap"])
            net_real_diff = float(latest["net_real_gap"])

            summary_rows.append(
                {
                    "MSA": msa_name,
                    "Metro Area": MSA_METADATA.get(msa_name, {}).get("short_name", msa_name),
                    "Start": first["date"].strftime("%Y-%m"),
                    "End": latest["date"].strftime("%Y-%m"),
                    "Food Inflation (%)": round(food_pct, 2),
                    "Energy Inflation (%)": round(energy_pct, 2),
                    "Wage Growth (%)": round(earnings_pct, 2),
                    "Food Gap (pts)": round(food_gap_val, 2),
                    "Energy Gap (pts)": round(energy_gap_val, 2),
                    "Net Real Diff (%)": round(net_real_diff, 2),
                    "Latest Hourly Wage ($)": round(float(latest["hourly_earnings"]), 2),
                }
            )
        except Exception as exc:
            raise RuntimeError(f"Error processing data for {msa_name}: {exc}") from exc

    if not processed_frames:
        raise ValueError("Could not produce aligned time series for the selected filters.")

    processed_df = (
        pd.concat(processed_frames, ignore_index=True)
        .sort_values(["msa_name", "date"])
        .reset_index(drop=True)
    )
    summary_df = (
        pd.DataFrame(summary_rows)
        .sort_values("Net Real Diff (%)", ascending=True)
        .reset_index(drop=True)
    )

    return processed_df, summary_df
