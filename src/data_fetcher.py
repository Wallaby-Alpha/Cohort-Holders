"""
data_fetcher.py
───────────────
All on-chain data retrieval. Two public functions are consumed by the app:

    fetch_swaps(token_address, start_dt, end_dt, rpc_url) → pd.DataFrame
    fetch_wallet_balances(buyers_df, token_address, rpc_url) → pd.DataFrame

Data sources:
  • Helius Enhanced Transactions API  (preferred — rich swap parsing)
  • Solana RPC getSignaturesForAddress (fallback)
  • Solana RPC getTokenAccountsByOwner  (balances)
"""

from __future__ import annotations

import time
import logging
from datetime import datetime
from typing import Optional

import requests
import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
SOL_DECIMALS = 9
RATE_LIMIT_SLEEP = 0.15   # seconds between RPC calls
MAX_WALLETS_PER_RUN = 2000  # safety cap


# ══════════════════════════════════════════════════════════════════════════════
# Public: fetch_swaps
# ══════════════════════════════════════════════════════════════════════════════
def fetch_swaps(
    token_address: str,
    start_dt: datetime,
    end_dt: datetime,
    rpc_url: Optional[str] = None,
) -> pd.DataFrame:
    """
    Return a DataFrame of swap events for *token_address* in [start_dt, end_dt].

    Columns:
        wallet          str     – signer / trader wallet
        token_in        float   – tokens received by wallet (buy side)
        token_out       float   – tokens sent by wallet (sell side)
        sol_spent       float   – SOL paid (buy) or received (sell)
        timestamp       datetime
        tx_sig          str
    """
    rpc = rpc_url or DEFAULT_RPC

    # Detect Helius key in the URL and use enhanced transactions if available
    if "helius" in rpc.lower():
        api_key = _extract_helius_key(rpc)
        if api_key:
            return _fetch_helius(token_address, start_dt, end_dt, api_key)

    # Generic RPC fallback (slower, less rich)
    return _fetch_rpc(token_address, start_dt, end_dt, rpc)


# ══════════════════════════════════════════════════════════════════════════════
# Public: fetch_wallet_balances
# ══════════════════════════════════════════════════════════════════════════════
def fetch_wallet_balances(
    buyers_df: pd.DataFrame,
    token_address: str,
    rpc_url: Optional[str] = None,
) -> pd.DataFrame:
    """
    Append `current_balance` column to buyers_df by querying each wallet's
    current token account balance via getTokenAccountsByOwner.
    """
    rpc = rpc_url or DEFAULT_RPC
    wallets = buyers_df["wallet"].unique().tolist()

    if len(wallets) > MAX_WALLETS_PER_RUN:
        st.warning(
            f"Limiting balance queries to {MAX_WALLETS_PER_RUN} wallets "
            f"(found {len(wallets)})."
        )
        wallets = wallets[:MAX_WALLETS_PER_RUN]

    progress = st.progress(0, text="Fetching balances…")
    balances: dict[str, float] = {}

    for i, wallet in enumerate(wallets):
        balances[wallet] = _get_token_balance(wallet, token_address, rpc)
        if i % 10 == 0:
            progress.progress((i + 1) / len(wallets), text=f"Fetching balances… {i+1}/{len(wallets)}")
        time.sleep(RATE_LIMIT_SLEEP)

    progress.empty()
    buyers_df = buyers_df.copy()
    buyers_df["current_balance"] = buyers_df["wallet"].map(balances).fillna(0.0)
    return buyers_df


# ══════════════════════════════════════════════════════════════════════════════
# Helius Enhanced Transactions path
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_helius(
    token_address: str,
    start_dt: datetime,
    end_dt: datetime,
    api_key: str,
) -> pd.DataFrame:
    """Use Helius Enhanced Transactions API for rich swap data."""
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    rows: list[dict] = []
    before_sig: Optional[str] = None
    page_size = 100
    max_pages = 50  # safety: 5000 txs max

    url = HELIUS_TX_URL.format(address=token_address)
    headers = {"Content-Type": "application/json"}

    for _ in range(max_pages):
        params: dict = {"api-key": api_key, "limit": page_size, "type": "SWAP"}
        if before_sig:
            params["before"] = before_sig

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            txs = resp.json()
        except requests.RequestException as exc:
            logger.warning("Helius request failed: %s", exc)
            break

        if not txs:
            break

        for tx in txs:
            ts = tx.get("timestamp", 0)
            if ts < start_ts:
                # We've gone past our window — stop paging
                return _rows_to_df(rows)
            if ts > end_ts:
                continue

            for event in _parse_helius_swap(tx, token_address):
                rows.append(event)

        before_sig = txs[-1].get("signature")
        time.sleep(RATE_LIMIT_SLEEP)

    return _rows_to_df(rows)


def _parse_helius_swap(tx: dict, token_address: str) -> list[dict]:
    """
    Extract wallet-level token flows from a Helius enhanced transaction.
    Returns a list of row dicts (one per involved wallet).
    """
    rows = []
    fee_payer = tx.get("feePayer", "")
    ts = datetime.utcfromtimestamp(tx.get("timestamp", 0))
    sig = tx.get("signature", "")

    token_transfers = tx.get("tokenTransfers", [])
    native_transfers = tx.get("nativeTransfers", [])

    # Map wallet → net token flow for this token
    token_flow: dict[str, float] = {}
    for tt in token_transfers:
        if tt.get("mint") != token_address:
            continue
        amount = tt.get("tokenAmount", 0)
        sender = tt.get("fromUserAccount", "")
        receiver = tt.get("toUserAccount", "")
        if sender:
            token_flow[sender] = token_flow.get(sender, 0) - amount
        if receiver:
            token_flow[receiver] = token_flow.get(receiver, 0) + amount

    # Map wallet → SOL flow
    sol_flow: dict[str, float] = {}
    for nt in native_transfers:
        amount_sol = nt.get("amount", 0) / 1e9
        sender = nt.get("fromUserAccount", "")
        receiver = nt.get("toUserAccount", "")
        if sender:
            sol_flow[sender] = sol_flow.get(sender, 0) - amount_sol
        if receiver:
            sol_flow[receiver] = sol_flow.get(receiver, 0) + amount_sol

    for wallet, net_tokens in token_flow.items():
        net_sol = sol_flow.get(wallet, 0.0)
        rows.append(
            {
                "wallet": wallet,
                "token_in": max(net_tokens, 0),
                "token_out": max(-net_tokens, 0),
                "sol_spent": max(-net_sol, 0),   # positive = SOL paid
                "timestamp": ts,
                "tx_sig": sig,
            }
        )

    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Generic RPC fallback
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_rpc(
    token_address: str,
    start_dt: datetime,
    end_dt: datetime,
    rpc: str,
) -> pd.DataFrame:
    """
    Fallback: use getSignaturesForAddress + getTransaction.
    This is slower and less reliable — use Helius for production.
    """
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    sigs = _get_signatures(token_address, start_ts, end_ts, rpc)
    rows = []

    progress = st.progress(0, text=f"Parsing {len(sigs)} transactions…")
    for i, sig_info in enumerate(sigs):
        tx_data = _get_transaction(sig_info["signature"], rpc)
        if tx_data:
            for row in _parse_rpc_tx(tx_data, token_address, sig_info["signature"]):
                rows.append(row)
        if i % 20 == 0:
            progress.progress((i + 1) / max(len(sigs), 1))
        time.sleep(RATE_LIMIT_SLEEP)

    progress.empty()
    return _rows_to_df(rows)


def _get_signatures(
    address: str, start_ts: int, end_ts: int, rpc: str
) -> list[dict]:
    """Paginate getSignaturesForAddress within the time window."""
    sigs = []
    before = None
    page_size = 1000

    while True:
        payload: dict = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": page_size, "commitment": "confirmed"}],
        }
        if before:
            payload["params"][1]["before"] = before

        try:
            resp = requests.post(rpc, json=payload, timeout=30)
            result = resp.json().get("result", [])
        except Exception:
            break

        if not result:
            break

        for item in result:
            block_time = item.get("blockTime", 0)
            if block_time < start_ts:
                return sigs
            if start_ts <= block_time <= end_ts:
                sigs.append(item)

        before = result[-1]["signature"]
        time.sleep(RATE_LIMIT_SLEEP)

    return sigs


def _get_transaction(sig: str, rpc: str) -> Optional[dict]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    }
    try:
        resp = requests.post(rpc, json=payload, timeout=20)
        return resp.json().get("result")
    except Exception:
        return None


def _parse_rpc_tx(tx: dict, token_address: str, sig: str) -> list[dict]:
    """Minimal parsing of a getTransaction response."""
    rows = []
    if not tx or tx.get("meta", {}).get("err"):
        return rows

    ts = datetime.utcfromtimestamp(tx.get("blockTime", 0))
    pre_balances = tx["meta"].get("preTokenBalances", [])
    post_balances = tx["meta"].get("postTokenBalances", [])
    account_keys = [
        ak["pubkey"] if isinstance(ak, dict) else ak
        for ak in tx["transaction"]["message"].get("accountKeys", [])
    ]

    def _bal_map(balances: list) -> dict[str, float]:
        m: dict[str, float] = {}
        for b in balances:
            if b.get("mint") == token_address:
                idx = b["accountIndex"]
                owner = b.get("owner", account_keys[idx] if idx < len(account_keys) else "")
                m[owner] = float(b["uiTokenAmount"]["uiAmount"] or 0)
        return m

    pre = _bal_map(pre_balances)
    post = _bal_map(post_balances)
    wallets = set(pre) | set(post)

    pre_sol = tx["meta"].get("preBalances", [])
    post_sol = tx["meta"].get("postBalances", [])

    for wallet in wallets:
        delta = post.get(wallet, 0) - pre.get(wallet, 0)
        if delta == 0:
            continue
        # Estimate SOL spent (rough: check account key index)
        sol_delta = 0.0
        if wallet in account_keys:
            idx = account_keys.index(wallet)
            if idx < len(pre_sol) and idx < len(post_sol):
                sol_delta = (post_sol[idx] - pre_sol[idx]) / 1e9

        rows.append(
            {
                "wallet": wallet,
                "token_in": max(delta, 0),
                "token_out": max(-delta, 0),
                "sol_spent": max(-sol_delta, 0),
                "timestamp": ts,
                "tx_sig": sig,
            }
        )
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Balance query
# ══════════════════════════════════════════════════════════════════════════════
def _get_token_balance(wallet: str, token_address: str, rpc: str) -> float:
    """Return current ui token balance for wallet."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": token_address},
            {"encoding": "jsonParsed"},
        ],
    }
    try:
        resp = requests.post(rpc, json=payload, timeout=15)
        result = resp.json().get("result", {})
        accounts = result.get("value", [])
        if not accounts:
            return 0.0
        total = sum(
            float(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
            for acc in accounts
        )
        return total
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════
def _rows_to_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=["wallet", "token_in", "token_out", "sol_spent", "timestamp", "tx_sig"]
        )
    return pd.DataFrame(rows)


def _extract_helius_key(rpc_url: str) -> Optional[str]:
    """Pull api-key from a Helius RPC URL."""
    if "api-key=" in rpc_url:
        return rpc_url.split("api-key=")[-1].split("&")[0]
    return None
