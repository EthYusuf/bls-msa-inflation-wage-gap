"""Streamlit dashboard for BLS MSA Food/Energy Inflation vs. Average Hourly Earnings."""

from __future__ import annotations

import os
from typing import List, Tuple

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config import (
    DEFAULT_END_YEAR,
    DEFAULT_START_YEAR,
    MAX_SUPPORTED_YEAR,
    MIN_SUPPORTED_YEAR,
    MSA_SERIES_MAP,
)
from data_fetcher import BLSDataFetcher
from data_processor import process_msa_data

st.set_page_config(
    page_title="U.S. Metro Inflation vs. Wage Gap (BLS)",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(ttl=3600, show_spinner=False)
def load_and_process_data(
    selected_msas: Tuple[str, ...],
    start_year: int,
    end_year: int,
    api_key: str,
    fill_method: str,
    force_refresh: bool,
    _schema_version: str = "en_v1",
):
    """Fetch raw BLS data and compute normalized indices and gaps."""
    fetcher = BLSDataFetcher(api_key=api_key or None)
    raw_df, fetch_meta = fetcher.fetch_msa_data(
        selected_msas=list(selected_msas),
        start_year=start_year,
        end_year=end_year,
        force_refresh=force_refresh,
    )
    processed_df, summary_df = process_msa_data(raw_df, fill_method=fill_method)
    return raw_df, processed_df, summary_df, fetch_meta


def render_trend_chart(msa_df, msa_name: str) -> go.Figure:
    """Create 100-based multi-line chart for Earnings vs. Food CPI vs. Energy CPI."""
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=msa_df["date"],
            y=msa_df["earnings_index"],
            mode="lines",
            name="Avg. Hourly Earnings (Index)",
            line=dict(color="#1f77b4", width=3),
            hovertemplate="%{x|%Y-%m}: <b>%{y:.2f}</b><extra>Hourly Earnings</extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=msa_df["date"],
            y=msa_df["food_index"],
            mode="lines",
            name="Food Inflation - CPI (Index)",
            line=dict(color="#d62728", width=2.5),
            hovertemplate="%{x|%Y-%m}: <b>%{y:.2f}</b><extra>Food CPI</extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=msa_df["date"],
            y=msa_df["energy_index"],
            mode="lines",
            name="Energy Inflation - CPI (Index)",
            line=dict(color="#ff7f0e", width=2.5, dash="dot"),
            hovertemplate="%{x|%Y-%m}: <b>%{y:.2f}</b><extra>Energy CPI</extra>",
        )
    )
    fig.add_hline(
        y=100,
        line_dash="dash",
        line_color="gray",
        annotation_text="Base Period (t0 = 100)",
        annotation_position="bottom right",
    )
    fig.update_layout(
        title=f"{msa_name} — Rebased Wages vs. Food & Energy CPI (t0 = 100)",
        xaxis_title="Date",
        yaxis_title="Index (t0 = 100)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=430,
        margin=dict(l=20, r=20, t=70, b=20),
    )
    return fig


def render_gap_chart(msa_df, msa_name: str) -> go.Figure:
    """Create area chart showing the wage-inflation gap over time."""
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=msa_df["date"],
            y=msa_df["food_gap"],
            mode="lines",
            name="Food Gap (Earnings - Food)",
            fill="tozeroy",
            line=dict(color="#d62728", width=2),
            fillcolor="rgba(214, 39, 40, 0.22)",
            hovertemplate="%{x|%Y-%m}: <b>%{y:+.2f} pts</b><extra>Food Gap</extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=msa_df["date"],
            y=msa_df["energy_gap"],
            mode="lines",
            name="Energy Gap (Earnings - Energy)",
            fill="tozeroy",
            line=dict(color="#ff7f0e", width=2),
            fillcolor="rgba(255, 127, 14, 0.18)",
            hovertemplate="%{x|%Y-%m}: <b>%{y:+.2f} pts</b><extra>Energy Gap</extra>",
        )
    )
    fig.add_hline(
        y=0,
        line_color="#2ca02c",
        line_width=1.5,
        annotation_text="Parity Line (0)",
        annotation_position="top left",
    )
    fig.update_layout(
        title=f"{msa_name} — Purchasing Power Gap (Negative = Wages Lagged)",
        xaxis_title="Date",
        yaxis_title="Index Point Difference",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=430,
        margin=dict(l=20, r=20, t=70, b=20),
    )
    return fig


def render_comparison_heatmap(summary_df) -> go.Figure:
    """Create a cross-MSA heatmap of inflation, wage growth, and gaps."""
    metric_cols = [
        "Food Inflation (%)",
        "Energy Inflation (%)",
        "Wage Growth (%)",
        "Food Gap (pts)",
        "Energy Gap (pts)",
        "Net Real Diff (%)",
    ]
    heat_df = summary_df.set_index("Metro Area")[metric_cols]
    fig = px.imshow(
        heat_df,
        text_auto=".1f",
        aspect="auto",
        color_continuous_scale="RdYlGn",
        color_continuous_midpoint=0,
        title="Cross-Metro Inflation, Wage Growth & Purchasing Power Gap Heatmap",
    )
    fig.update_layout(
        xaxis_title="Metric",
        yaxis_title="Metropolitan Area (MSA)",
        height=380,
        margin=dict(l=20, r=20, t=60, b=20),
    )
    return fig


def main() -> None:
    """Run the Streamlit dashboard application."""
    st.title("🇺🇸 U.S. Metro Inflation vs. Average Hourly Earnings Gap")
    st.markdown(
        "Interactive 100-rebased comparison of **Food CPI**, **Energy CPI**, and "
        "**Average Hourly Earnings (Total Private)** across major U.S. Metropolitan "
        "Statistical Areas using the **U.S. Bureau of Labor Statistics (BLS) Public Data API v2**."
    )

    all_msas: List[str] = list(MSA_SERIES_MAP.keys())

    with st.sidebar:
        st.header("⚙️ Analysis Parameters")
        selected_msas = st.multiselect(
            "Metropolitan Areas (MSA)",
            options=all_msas,
            default=all_msas,
            help="Select one or multiple MSAs for single-metro inspection or cross-metro comparison.",
        )

        year_range = st.slider(
            "Year Range",
            min_value=MIN_SUPPORTED_YEAR,
            max_value=MAX_SUPPORTED_YEAR,
            value=(DEFAULT_START_YEAR, DEFAULT_END_YEAR),
            help="Defaults to the trailing 5-year window.",
        )

        fill_label = st.selectbox(
            "Missing Month Alignment (Bi-monthly CPI)",
            options=["Linear Interpolation + ffill", "Forward Fill (ffill) Only"],
            index=0,
            help="Aligns MSAs that publish CPI bi-monthly (Dallas, Miami) onto a monthly grid.",
        )
        fill_method = "ffill" if fill_label == "Forward Fill (ffill) Only" else "linear_ffill"

        default_key = os.getenv("BLS_API_KEY", "")
        api_key = st.text_input(
            "BLS API Key (Optional)",
            value=default_key,
            type="password",
            help="Without a key, the app runs within BLS v2 unregistered limits (25 requests/day).",
        )

        force_refresh = st.button("🔄 Refresh Data from API", use_container_width=True)
        if force_refresh:
            st.cache_data.clear()

        st.divider()
        st.caption(
            "BLS Series Templates:\n"
            "• Food CPI: `CUURS[MSA]SAF1`\n"
            "• Energy CPI: `CUURS[MSA]SA0E`\n"
            "• Hourly Earnings: `SMU[ST][MSA]0500000003`"
        )

    if not selected_msas:
        st.warning("Please select at least one Metropolitan Statistical Area (MSA) from the sidebar.")
        return

    start_year, end_year = year_range

    try:
        with st.spinner("Fetching and processing BLS time series..."):
            raw_df, processed_df, summary_df, fetch_meta = load_and_process_data(
                selected_msas=tuple(selected_msas),
                start_year=start_year,
                end_year=end_year,
                api_key=api_key,
                fill_method=fill_method,
                force_refresh=force_refresh,
            )
    except Exception as exc:
        st.error(f"An error occurred while loading or processing data: {exc}")
        return

    # Data source status banner
    if fetch_meta.get("used_fallback"):
        st.warning(
            f"⚠️ Synthetic fallback data was used for {fetch_meta['fallback_count']} "
            "series due to BLS API reachability or quota limits. "
            f"(Cached: {fetch_meta['cached_count']}, API: {fetch_meta['api_count']})"
        )
    else:
        st.success(
            f"✅ Data ready — From SQLite cache: {fetch_meta['cached_count']} series | "
            f"From BLS API v2: {fetch_meta['api_count']} series"
        )

    # Primary MSA selector for KPI cards and detailed time-series charts
    if len(selected_msas) > 1:
        focus_msa = st.selectbox(
            "Select a Metro Area for Detailed KPIs & Time-Series Charts:",
            options=selected_msas,
            index=0,
        )
    else:
        focus_msa = selected_msas[0]

    focus_summary = summary_df[summary_df["MSA"] == focus_msa]
    focus_ts = processed_df[processed_df["msa_name"] == focus_msa]

    if focus_summary.empty or focus_ts.empty:
        st.warning(f"No data available to display for {focus_msa}.")
        return

    row = focus_summary.iloc[0]

    # Top KPI Cards
    st.subheader(f"📌 {row['Metro Area']} Summary KPIs ({row['Start']} → {row['End']})")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Cumulative Food Inflation", f"{row['Food Inflation (%)']:+.2f}%")
    k2.metric("Cumulative Energy Inflation", f"{row['Energy Inflation (%)']:+.2f}%")
    k3.metric(
        "Cumulative Wage Growth",
        f"{row['Wage Growth (%)']:+.2f}%",
        delta=f"Latest Wage: ${row['Latest Hourly Wage ($)']:.2f}/hr",
    )
    k4.metric(
        "Net Real Diff (Wage - Avg CPI)",
        f"{row['Net Real Diff (%)']:+.2f}%",
        delta="Wages Lagged" if row["Net Real Diff (%)"] < 0 else "Real Gain",
        delta_color="normal" if row["Net Real Diff (%)"] >= 0 else "inverse",
    )

    st.divider()

    # Main Charts
    col_left, col_right = st.columns(2)
    with col_left:
        st.plotly_chart(
            render_trend_chart(focus_ts, row["Metro Area"]),
            use_container_width=True,
        )
    with col_right:
        st.plotly_chart(
            render_gap_chart(focus_ts, row["Metro Area"]),
            use_container_width=True,
        )

    # Cross-MSA Comparison Table & Heatmap
    st.divider()
    st.subheader("🗺️ Cross-Metropolitan Comparison")

    tab_table, tab_heat = st.tabs(["Comparison Table", "Heatmap"])
    with tab_table:
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
    with tab_heat:
        st.plotly_chart(
            render_comparison_heatmap(summary_df),
            use_container_width=True,
        )

    # CSV Export Buttons
    st.divider()
    st.subheader("📥 Export Data")
    d1, d2 = st.columns(2)
    with d1:
        raw_csv = raw_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download Raw BLS Data (CSV)",
            data=raw_csv,
            file_name=f"bls_raw_msa_{start_year}_{end_year}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with d2:
        processed_csv = processed_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download Processed & Rebased Data (CSV)",
            data=processed_csv,
            file_name=f"bls_processed_msa_{start_year}_{end_year}.csv",
            mime="text/csv",
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
