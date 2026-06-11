import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import pytz

from src.data_fetcher import fetch_swaps, fetch_wallet_balances
from src.demo_data import generate_demo_buyers
from src.analyzer import (
    identify_net_buyers,
    calculate_retention,
    classify_wallets,
    aggregate_stats,
    top_buyer_retention,
    whale_retention,
    smart_wallet_retention,
)
from src.display import (
    render_cohort_table,
    render_aggregate_metrics,
    render_category_breakdown,
)

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SOL Token Retention Tracker",
    page_icon="🔭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styles ────────────────────────────────────────────────────────────────────
st.markdown(
    """
    .main-header {
        font-size: 2rem;
        font-weight: 700;
        color: #9945FF;
        margin-bottom: 0.25rem;
    }
    .sub-header {
        color: #888;
        margin-bottom: 2rem;
        font-size: 0.95rem;
    }
    .metric-card {
        background: #1a1a2e;
        border-radius: 12px;
        padding: 1.2rem 1.5rem;
        border: 1px solid #2d2d4e;
    }
    .section-title {
        font-size: 1.1rem;
        font-weight: 600;
        color: #14F195;
        margin-bottom: 0.75rem;
    }
    div[data-testid="stMetric"] {
        background: #1a1a2e;
        border-radius: 10px;
        padding: 0.8rem 1rem;
        border: 1px solid #2d2d4e;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown('<div class="main-header">🔭 SOL Token Retention Tracker</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">Analyze buyer retention for any Solana token across a custom time window</div>',
    unsafe_allow_html=True,
)

# ── Sidebar inputs ────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ Configuration")

    token_address = st.text_input(
        "Token Address",
        placeholder="e.g. EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        help="Solana SPL token contract address",
    )

    st.markdown("**Time Window**")
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Start Date", value=datetime.now().date() - timedelta(days=1))
        start_time = st.time_input("Start Time", value=datetime.strptime("14:00", "%H:%M").time())
    with col2:
        end_date = st.date_input("End Date", value=datetime.now().date())
        end_time = st.time_input("End Time", value=datetime.strptime("14:00", "%H:%M").time())

    start_dt = datetime.combine(start_date, start_time).replace(tzinfo=pytz.UTC)
    end_dt = datetime.combine(end_date, end_time).replace(tzinfo=pytz.UTC)

    st.markdown("---")
    st.markdown("### 🔍 Filters")

    filter_mode = st.selectbox(
        "Analysis Mode",
        options=["All Buyers", "Top N Buyers", "Whale Filter", "Smart Wallet Classification"],
        help="Choose how to slice the buyer cohort",
    )

    if filter_mode == "Top N Buyers":
        top_n = st.slider("Top N Buyers", min_value=10, max_value=200, value=50, step=10)
    elif filter_mode == "Whale Filter":
        min_sol_spent = st.number_input(
            "Min SOL Spent", min_value=1.0, max_value=10000.0, value=50.0, step=5.0
        )
    elif filter_mode == "Smart Wallet Classification":
        st.info("Classifies wallets into Smart Money, Whale, Retail, Sniper, Fresh Wallet")

    st.markdown("---")
    rpc_url = st.text_input(
        "Custom RPC URL (optional)",
        placeholder="https://mainnet.helius-rpc.com/?api-key=...",
        type="password",
        help="Helius or QuickNode RPC recommended for reliable data",
    )

    run_analysis = st.button("🚀 Run Analysis", type="primary", use_container_width=True)

# ── Main panel ────────────────────────────────────────────────────────────────
if not run_analysis:
    st.info(
        "👈 Enter a token address and time window in the sidebar, then click **Run Analysis**."
    )
    with st.expander("ℹ️ How it works"):
        st.markdown(
            """
            1. **Fetch Swaps** — Pulls all on-chain swaps for the token during the window using the Solana RPC + DeFi APIs.
            2. **Identify Net Buyers** — Wallets where `tokens_in > tokens_out` during the period.
            3. **Current Balances** — Queries each wallet's current token balance.
            4. **Retention Calc** — `retained = current / net_bought`, `% retained = retained / net_bought * 100`.
            5. **Filters** — Slice by top buyers, whale threshold, or smart money classification.
            """
        )
    st.stop()

if not token_address or len(token_address) < 32:
    st.error("Please enter a valid Solana token address.")
    st.stop()

if start_dt >= end_dt:
    st.error("Start datetime must be before end datetime.")
    st.stop()

# ── Analysis pipeline ─────────────────────────────────────────────────────────
DEMO_MODE = token_address.upper() == "DEMO"

if DEMO_MODE:
    st.info("🎭 **Demo mode** — showing synthetic data. Enter a real token address for live analysis.")
    buyers_df = generate_demo_buyers(n=200)
else:
    with st.spinner("Fetching swap transactions…"):
        try:
            swaps_df = fetch_swaps(token_address, start_dt, end_dt, rpc_url=rpc_url or None)
        except Exception as e:
            st.error(f"Error fetching swaps: {e}")
            st.stop()

    if swaps_df.empty:
        st.warning("No swaps found for this token in the specified window.")
        st.stop()

    st.success(f"Found **{len(swaps_df):,}** swap transactions across **{swaps_df['wallet'].nunique():,}** wallets.")

    with st.spinner("Identifying net buyers…"):
        buyers_df = identify_net_buyers(swaps_df)

    if buyers_df.empty:
        st.warning("No net buyers identified in this period.")
        st.stop()

    with st.spinner(f"Querying current balances for {len(buyers_df):,} wallets…"):
        buyers_df = fetch_wallet_balances(buyers_df, token_address, rpc_url=rpc_url or None)

    with st.spinner("Calculating retention…"):
        buyers_df = calculate_retention(buyers_df)

# ── Apply filter mode ─────────────────────────────────────────────────────────
if filter_mode == "Top N Buyers":
    display_df = top_buyer_retention(buyers_df, top_n)
    cohort_label = f"Top {top_n} Buyers"
elif filter_mode == "Whale Filter":
    display_df = whale_retention(buyers_df, min_sol_spent)
    cohort_label = f"Whales (≥ {min_sol_spent} SOL)"
elif filter_mode == "Smart Wallet Classification":
    display_df = classify_wallets(buyers_df)
    cohort_label = "All Buyers (Classified)"
else:
    display_df = buyers_df.copy()
    cohort_label = "All Buyers"

# ── Display results ───────────────────────────────────────────────────────────
st.markdown(f"## 📊 Results — {cohort_label}")

agg = aggregate_stats(display_df)
render_aggregate_metrics(agg)

st.markdown("---")

if filter_mode == "Smart Wallet Classification":
    st.markdown('<div class="section-title">Retention by Wallet Category</div>', unsafe_allow_html=True)
    render_category_breakdown(display_df)
    st.markdown("---")

st.markdown('<div class="section-title">Wallet-Level Retention</div>', unsafe_allow_html=True)
render_cohort_table(display_df)

# ── Download ──────────────────────────────────────────────────────────────────
st.markdown("---")
csv = display_df.to_csv(index=False)
st.download_button(
    "⬇️ Export CSV",
    data=csv,
    file_name=f"retention_{token_address[:8]}_{start_date}.csv",
    mime="text/csv",
)
