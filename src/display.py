"""
display.py
──────────
Streamlit rendering helpers. All functions accept a DataFrame and/or
an agg-stats dict and produce UI elements. No data logic lives here.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st


# ─── Colour helpers ────────────────────────────────────────────────────────────
def _retention_colour(pct: float) -> str:
    """Return a hex colour for a retention percentage."""
    if pct >= 80:
        return "#14F195"   # green
    if pct >= 40:
        return "#FFD700"   # amber
    return "#FF4B4B"       # red


def _retention_badge(pct: float) -> str:
    """HTML badge for inline display."""
    colour = _retention_colour(pct)
    return (
        f'<span style="background:{colour}22;color:{colour};'
        f'border-radius:6px;padding:2px 8px;font-weight:600;">'
        f"{pct:.1f}%</span>"
    )


def _shorten_wallet(wallet: str, chars: int = 6) -> str:
    if len(wallet) <= chars * 2 + 2:
        return wallet
    return f"{wallet[:chars]}…{wallet[-chars:]}"


def _solscan_link(wallet: str) -> str:
    short = _shorten_wallet(wallet)
    return f'<a href="https://solscan.io/account/{wallet}" target="_blank">{short}</a>'


# ─── Aggregate metrics band ────────────────────────────────────────────────────
def render_aggregate_metrics(agg: dict) -> None:
    cols = st.columns(5)
    metrics = [
        ("Wallets Analyzed", f"{agg['wallet_count']:,}", None),
        ("Total Bought", _fmt_tokens(agg["total_bought"]), None),
        ("Total Still Held", _fmt_tokens(agg["total_held"]), None),
        ("SOL Deployed", f"{agg['total_sol_spent']:,.1f} ◎", None),
        (
            "Cohort Retention",
            f"{agg['cohort_retention_pct']:.1f}%",
            _delta_colour(agg["cohort_retention_pct"]),
        ),
    ]
    for col, (label, value, delta) in zip(cols, metrics):
        col.metric(label, value, delta=delta)

    st.markdown("&nbsp;")

    # Status breakdown mini bar
    total = agg["wallet_count"]
    if total > 0:
        fh = agg["full_holders"]
        ph = agg["partial_holders"]
        so = agg["sold_out"]
        st.markdown(
            f"**Holders:** "
            f"🟢 Full Hold: **{fh}** &nbsp;|&nbsp; "
            f"🟡 Partial: **{ph}** &nbsp;|&nbsp; "
            f"🔴 Sold Out: **{so}**"
        )


# ─── Category breakdown table ──────────────────────────────────────────────────
def render_category_breakdown(df: pd.DataFrame) -> None:
    if "category" not in df.columns:
        st.info("Run Smart Wallet Classification mode to see category breakdown.")
        return

    cat_df = (
        df.groupby("category")
        .agg(
            Wallets=("wallet", "count"),
            Bought=("net_bought", "sum"),
            Held=("still_held", "sum"),
            SOL_Spent=("sol_spent", "sum"),
        )
        .reset_index()
    )
    cat_df["Retention"] = (cat_df["Held"] / cat_df["Bought"] * 100).clip(0, 100)
    cat_df = cat_df.sort_values("Retention", ascending=False)

    # Render as a styled HTML table
    rows_html = ""
    for _, row in cat_df.iterrows():
        badge = _retention_badge(row["Retention"])
        rows_html += f"""
        <tr>
            <td>{row['category']}</td>
            <td>{int(row['Wallets']):,}</td>
            <td>{_fmt_tokens(row['Bought'])}</td>
            <td>{_fmt_tokens(row['Held'])}</td>
            <td>{row['SOL_Spent']:,.1f} ◎</td>
            <td>{badge}</td>
        </tr>
        """

    st.markdown(
        f"""
        <table style="width:100%;border-collapse:collapse;">
          <thead>
            <tr style="border-bottom:2px solid #333;color:#888;font-size:0.85rem;">
              <th style="text-align:left;padding:8px">Category</th>
              <th style="text-align:right;padding:8px">Wallets</th>
              <th style="text-align:right;padding:8px">Total Bought</th>
              <th style="text-align:right;padding:8px">Still Held</th>
              <th style="text-align:right;padding:8px">SOL Spent</th>
              <th style="text-align:right;padding:8px">Retention</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


# ─── Wallet-level cohort table ─────────────────────────────────────────────────
def render_cohort_table(df: pd.DataFrame) -> None:
    """Render the main wallet retention table with search + sort."""
    display_cols = {
        "wallet": "Wallet",
        "net_bought": "Bought",
        "still_held": "Current Holding",
        "pct_retained": "Retention %",
        "sol_spent": "SOL Spent",
        "avg_entry_price": "Avg Entry (SOL)",
        "status": "Status",
    }
    if "category" in df.columns:
        display_cols["category"] = "Category"

    present_cols = [c for c in display_cols if c in df.columns]
    render_df = df[present_cols].copy()
    render_df.columns = [display_cols[c] for c in present_cols]

    # Search filter
    search = st.text_input("🔍 Search wallet address", placeholder="Paste wallet to filter…")
    if search:
        render_df = render_df[render_df["Wallet"].str.contains(search, case=False, na=False)]

    # Format numbers
    for col in ["Bought", "Current Holding"]:
        if col in render_df.columns:
            render_df[col] = render_df[col].apply(lambda x: f"{x:,.0f}")

    for col in ["SOL Spent", "Avg Entry (SOL)"]:
        if col in render_df.columns:
            render_df[col] = render_df[col].apply(lambda x: f"{x:.4f}")

    if "Retention %" in render_df.columns:
        render_df["Retention %"] = render_df["Retention %"].apply(lambda x: f"{x:.1f}%")

    # Shorten wallet addresses
    if "Wallet" in render_df.columns:
        render_df["Wallet"] = render_df["Wallet"].apply(_shorten_wallet)

    st.dataframe(
        render_df,
        use_container_width=True,
        height=min(600, 50 + len(render_df) * 38),
    )

    st.caption(f"Showing {len(render_df):,} wallets")


# ─── Utilities ─────────────────────────────────────────────────────────────────
def _fmt_tokens(n: float) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:.0f}"


def _delta_colour(pct: float) -> str:
    if pct >= 70:
        return "normal"
    if pct >= 40:
        return "off"
    return "inverse"
