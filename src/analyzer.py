"""
analyzer.py
───────────
Pure-Python / pandas logic. No I/O, no Streamlit.

Public functions:
    identify_net_buyers(swaps_df)          → buyers_df
    calculate_retention(buyers_df)         → buyers_df (with retention cols)
    classify_wallets(buyers_df)            → buyers_df (with 'category' col)
    aggregate_stats(df)                    → dict
    top_buyer_retention(df, n)             → filtered df
    whale_retention(df, min_sol)           → filtered df
    smart_wallet_retention(df)             → df with category breakdown
"""

from __future__ import annotations

import pandas as pd
import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
# Step 1: Identify net buyers from raw swap rows
# ══════════════════════════════════════════════════════════════════════════════
def identify_net_buyers(swaps_df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate per-wallet swap activity.  Net buyers are wallets where
    total tokens received > total tokens sent during the window.

    Input columns:  wallet, token_in, token_out, sol_spent, timestamp
    Output columns: wallet, total_bought, total_sold, net_bought,
                    sol_spent, avg_entry_price, first_buy, last_buy
    """
    grp = (
        swaps_df.groupby("wallet")
        .agg(
            total_bought=("token_in", "sum"),
            total_sold=("token_out", "sum"),
            sol_spent=("sol_spent", "sum"),
            first_buy=("timestamp", "min"),
            last_buy=("timestamp", "max"),
        )
        .reset_index()
    )

    grp["net_bought"] = grp["total_bought"] - grp["total_sold"]

    # Only keep net buyers (net_bought > 0)
    buyers = grp[grp["net_bought"] > 0].copy()

    # Average entry price: SOL spent / tokens bought
    buyers["avg_entry_price"] = np.where(
        buyers["total_bought"] > 0,
        buyers["sol_spent"] / buyers["total_bought"],
        0.0,
    )

    return buyers.sort_values("net_bought", ascending=False).reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# Step 2: Calculate retention after balances are fetched
# ══════════════════════════════════════════════════════════════════════════════
def calculate_retention(buyers_df: pd.DataFrame) -> pd.DataFrame:
    """
    Requires `current_balance` column (added by fetch_wallet_balances).

    Adds:
        still_held          float  – min(current_balance, net_bought)
        pct_retained        float  – 0-100
        realized_tokens     float  – tokens no longer held
        unrealized_value_sol float – estimated current value in SOL
        status              str    – 'Full Hold', 'Partial Hold', 'Sold Out'
    """
    df = buyers_df.copy()

    df["still_held"] = df[["current_balance", "net_bought"]].min(axis=1).clip(lower=0)
    df["pct_retained"] = np.where(
        df["net_bought"] > 0,
        (df["still_held"] / df["net_bought"] * 100).clip(0, 100),
        0.0,
    )
    df["realized_tokens"] = (df["net_bought"] - df["still_held"]).clip(lower=0)

    # Estimate unrealized value: still_held * avg_entry_price
    # (rough proxy; real-time price would need an oracle)
    df["unrealized_value_sol"] = df["still_held"] * df["avg_entry_price"]

    df["status"] = pd.cut(
        df["pct_retained"],
        bins=[-1, 0.001, 99.999, 100.001],
        labels=["Sold Out", "Partial Hold", "Full Hold"],
    )

    return df


# ══════════════════════════════════════════════════════════════════════════════
# Step 3: Wallet classification
# ══════════════════════════════════════════════════════════════════════════════

# Thresholds (tunable)
WHALE_MIN_SOL = 50.0          # SOL spent to be a whale
SMART_MIN_RETAINED = 60.0     # % retained threshold for smart money heuristic
SNIPER_MAX_SECONDS = 120      # bought within 2 min of first tx in window
FRESH_MAX_TX_COUNT = 5        # wallet with very few txs = fresh wallet

def classify_wallets(buyers_df: pd.DataFrame) -> pd.DataFrame:
    """
    Heuristic classification. In production you'd augment with historical
    win-rate data from an indexer (e.g. Helius, Birdeye).

    Categories (mutually exclusive, priority order):
        Smart Money   – high retention + high SOL spent
        Whale         – high SOL spent
        Sniper        – very early buyer
        Fresh Wallet  – wallet with minimal on-chain history (proxied here
                        by very low total_bought count relative to sol_spent)
        Retail        – everyone else
    """
    df = buyers_df.copy()

    # Earliest buy in the cohort
    earliest = df["first_buy"].min()
    df["seconds_after_open"] = (df["first_buy"] - earliest).dt.total_seconds()

    def _classify(row: pd.Series) -> str:
        is_whale = row["sol_spent"] >= WHALE_MIN_SOL
        is_smart = is_whale and row["pct_retained"] >= SMART_MIN_RETAINED
        is_sniper = row["seconds_after_open"] <= SNIPER_MAX_SECONDS
        # Fresh wallet heuristic: low SOL spend but many tokens (likely a bot
        # with a new wallet), or extremely small sol_spent
        is_fresh = row["sol_spent"] < 0.5 and row["net_bought"] > 0

        if is_smart:
            return "Smart Money"
        if is_whale:
            return "Whale"
        if is_sniper:
            return "Sniper"
        if is_fresh:
            return "Fresh Wallet"
        return "Retail"

    df["category"] = df.apply(_classify, axis=1)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Filter modes
# ══════════════════════════════════════════════════════════════════════════════
def top_buyer_retention(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Return the top-N wallets by net_bought."""
    return df.nlargest(n, "net_bought").copy()


def whale_retention(df: pd.DataFrame, min_sol: float) -> pd.DataFrame:
    """Return wallets that spent >= min_sol SOL."""
    return df[df["sol_spent"] >= min_sol].copy()


def smart_wallet_retention(df: pd.DataFrame) -> pd.DataFrame:
    """Return classified df (same as classify_wallets — alias for clarity)."""
    return classify_wallets(df)


# ══════════════════════════════════════════════════════════════════════════════
# Aggregate statistics
# ══════════════════════════════════════════════════════════════════════════════
def aggregate_stats(df: pd.DataFrame) -> dict:
    """Return cohort-level summary metrics."""
    total_bought = df["net_bought"].sum()
    total_held = df["still_held"].sum()
    cohort_retention = (total_held / total_bought * 100) if total_bought > 0 else 0.0
    total_sol = df["sol_spent"].sum()

    status_counts = df["status"].value_counts().to_dict() if "status" in df.columns else {}

    result: dict = {
        "wallet_count": len(df),
        "total_bought": total_bought,
        "total_held": total_held,
        "total_sold_out": total_bought - total_held,
        "cohort_retention_pct": cohort_retention,
        "total_sol_spent": total_sol,
        "full_holders": status_counts.get("Full Hold", 0),
        "partial_holders": status_counts.get("Partial Hold", 0),
        "sold_out": status_counts.get("Sold Out", 0),
    }

    if "category" in df.columns:
        cat_stats = (
            df.groupby("category")
            .agg(
                wallets=("wallet", "count"),
                avg_retention=("pct_retained", "mean"),
                total_bought=("net_bought", "sum"),
                total_held=("still_held", "sum"),
            )
            .reset_index()
        )
        cat_stats["cohort_retention"] = (
            cat_stats["total_held"] / cat_stats["total_bought"] * 100
        ).clip(0, 100)
        result["by_category"] = cat_stats.to_dict(orient="records")

    return result
