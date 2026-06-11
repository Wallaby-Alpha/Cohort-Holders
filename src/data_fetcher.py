"""
data_fetcher.py
───────────────
Fetches on-chain swap data for a Solana token using Helius APIs.

Strategy:
  1. Use Helius /v1/mintlist + /v0/token-metadata to confirm token exists
  2. Use Helius searchTransactions (POST /v1/transactions) to pull swaps
     by mint — this is the correct endpoint for token-based swap lookup
  3. Fall back to getSignaturesForAddress on the mint's largest token accounts
     if the above returns nothing

Public API:
    fetch_swaps(token_address, start_dt, end_dt, rpc_url) → pd.DataFrame
    fetch_wallet_balances(buyers_df, token_address, rpc_url) → pd.DataFrame
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

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
RATE_LIMIT_SLEEP = 0.2
MAX_WALLETS_PER_RUN = 2000
PAGE_SIZE = 100


# ══════════════════════════════════════════════════════════════════════════════
# Public: fetch_swaps
# ══════════════════════════════════════════════════════════════════════════════
def fetch_swaps(
    token_address: str,
    start_dt: datetime,
    end_dt: datetime,
    rpc_url: Optional[str] = None,
) -> pd.DataFrame:
    api_key = _extract_helius_key(rpc_url) if rpc_url else None

    if api_key:
        st.info("🔗 Using Helius API for swap data…")
        df = _fetch_helius_search(token_address, start_dt, end_dt, api_key)
        if not df.empty:
            return df
        # If search returned nothing, try the signatures fallback
        st.warning("Helius search returned 0 results — trying signature fallback…")
        return _fetch_via_signatures(token_address, start_dt, end_dt, api_key, rpc_url)

    st.warning("⚠️ No Helius key — using public RPC (slow, may be incomplete)")
    return _fetch_rpc_fallback(token_address, start_dt, end_dt, rpc_url or DEFAULT_RPC)


# ══════════════════════════════════════════════════════════════════════════════
# Public: fetch_wallet_balances
# ══════════════════════════════════════════════════════════════════════════════
def fetch_wallet_balances(
    buyers_df: pd.DataFrame,
    token_address: str,
    rpc_url: Optional[str] = None,
) -> pd.DataFrame:
    rpc = rpc_url or DEFAULT_RPC
    # For balance queries, use the Helius RPC endpoint directly
    if rpc_url and "helius" in rpc_url:
        rpc = rpc_url  # already a full URL

    wallets = buyers_df["wallet"].unique().tolist()
    if len(wallets) > MAX_WALLETS_PER_RUN:
        st.warning(f"Capping balance queries at {MAX_WALLETS_PER_RUN} wallets.")
        wallets = wallets[:MAX_WALLETS_PER_RUN]

    progress = st.progress(0, text="Fetching current balances…")
    balances: dict[str, float] = {}

    for i, wallet in enumerate(wallets):
        balances[wallet] = _get_token_balance(wallet, token_address, rpc)
        if i % 10 == 0:
            progress.progress((i + 1) / len(wallets), text=f"Balances {i+1}/{len(wallets)}…")
        time.sleep(RATE_LIMIT_SLEEP)

    progress.empty()
    out = buyers_df.copy()
    out["current_balance"] = out["wallet"].map(balances).fillna(0.0)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Helius: search transactions by mint (correct approach)
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_helius_search(
    token_address: str,
    start_dt: datetime,
    end_dt: datetime,
    api_key: str,
) -> pd.DataFrame:
    """
    Use Helius Enhanced Transactions API querying by token mint.
    Endpoint: GET https://api.helius.xyz/v0/addresses/{mint}/transactions
    with type=SWAP — but paginate carefully and filter by timestamp.
    """
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    rows: list[dict] = []
    before_sig: Optional[str] = None
    url = f"https://api.helius.xyz/v0/addresses/{token_address}/transactions"

    status = st.empty()

    for page in range(100):  # max 10,000 txs
        params: dict = {
            "api-key": api_key,
            "limit": PAGE_SIZE,
            "commitment": "confirmed",
        }
        if before_sig:
            params["before"] = before_sig

        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            st.error(f"Network error contacting Helius: {e}")
            break

        if resp.status_code == 401:
            st.error("❌ Invalid Helius API key. Please check and re-enter.")
            break
        if resp.status_code == 429:
            st.warning("Rate limited by Helius — waiting 2s…")
            time.sleep(2)
            continue
        if not resp.ok:
            st.error(f"Helius API error {resp.status_code}: {resp.text[:200]}")
            break

        txs = resp.json()
        if not isinstance(txs, list) or len(txs) == 0:
            break

        oldest_ts = txs[-1].get("timestamp", 0)
        status.info(f"📦 Page {page+1} — {len(rows)} swaps found so far… (oldest: {datetime.utcfromtimestamp(oldest_ts).strftime('%Y-%m-%d %H:%M')} UTC)")

        for tx in txs:
            ts = tx.get("timestamp", 0)

            # Gone past our window — stop
            if ts < start_ts:
                status.empty()
                return _rows_to_df(rows)

            # Not yet in our window — skip
            if ts > end_ts:
                continue

            tx_type = tx.get("type", "")
            # Accept SWAP and UNKNOWN (some DEX txs come through as UNKNOWN)
            if tx_type not in ("SWAP", "UNKNOWN", ""):
                continue

            parsed = _parse_helius_tx(tx, token_address)
            rows.extend(parsed)

        before_sig = txs[-1].get("signature")
        time.sleep(RATE_LIMIT_SLEEP)

    status.empty()
    return _rows_to_df(rows)


def _parse_helius_tx(tx: dict, token_address: str) -> list[dict]:
    """Parse a single Helius enhanced transaction into swap rows."""
    rows = []
    ts = datetime.utcfromtimestamp(tx.get("timestamp", 0))
    sig = tx.get("signature", "")

    token_transfers = tx.get("tokenTransfers", []) or []
    native_transfers = tx.get("nativeTransfers", []) or []

    # Build per-wallet token flow for our mint
    token_flow: dict[str, float] = {}
    for tt in token_transfers:
        if str(tt.get("mint", "")) != token_address:
            continue
        raw_amount = tt.get("tokenAmount", 0)
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue

        sender = tt.get("fromUserAccount", "") or ""
        receiver = tt.get("toUserAccount", "") or ""

        # Skip program-owned accounts (they're liquidity pools, not traders)
        if sender and not _is_likely_program(sender):
            token_flow[sender] = token_flow.get(sender, 0.0) - amount
        if receiver and not _is_likely_program(receiver):
            token_flow[receiver] = token_flow.get(receiver, 0.0) + amount

    if not token_flow:
        return rows

    # Build per-wallet SOL flow
    sol_flow: dict[str, float] = {}
    for nt in native_transfers:
        try:
            amount_sol = float(nt.get("amount", 0)) / 1e9
        except (TypeError, ValueError):
            continue
        sender = nt.get("fromUserAccount", "") or ""
        receiver = nt.get("toUserAccount", "") or ""
        if sender:
            sol_flow[sender] = sol_flow.get(sender, 0.0) - amount_sol
        if receiver:
            sol_flow[receiver] = sol_flow.get(receiver, 0.0) + amount_sol

    # Also check swap events if present (newer Helius format)
    swap_events = (tx.get("events") or {}).get("swap") or {}
    if swap_events:
        rows.extend(_parse_swap_event(swap_events, token_address, ts, sig))
        return rows

    for wallet, net_tokens in token_flow.items():
        net_sol = sol_flow.get(wallet, 0.0)
        rows.append({
            "wallet": wallet,
            "token_in": max(net_tokens, 0.0),
            "token_out": max(-net_tokens, 0.0),
            "sol_spent": max(-net_sol, 0.0),
            "timestamp": ts,
            "tx_sig": sig,
        })

    return rows


def _parse_swap_event(event: dict, token_address: str, ts: datetime, sig: str) -> list[dict]:
    """Parse the structured swap event block in newer Helius responses."""
    rows = []
    native_input = event.get("nativeInput") or {}
    native_output = event.get("nativeOutput") or {}
    token_inputs = event.get("tokenInputs") or []
    token_outputs = event.get("tokenOutputs") or []

    # Find wallets involved with our token
    for ti in token_inputs:
        if str(ti.get("mint", "")) == token_address:
            wallet = ti.get("userAccount", "")
            amount = float(ti.get("rawTokenAmount", {}).get("tokenAmount", 0) or 0)
            sol_received = float(native_output.get("amount", 0) or 0) / 1e9
            if wallet:
                rows.append({
                    "wallet": wallet,
                    "token_in": 0.0,
                    "token_out": amount,
                    "sol_spent": 0.0,
                    "timestamp": ts,
                    "tx_sig": sig,
                })

    for to_ in token_outputs:
        if str(to_.get("mint", "")) == token_address:
            wallet = to_.get("userAccount", "")
            amount = float(to_.get("rawTokenAmount", {}).get("tokenAmount", 0) or 0)
            sol_spent = float(native_input.get("amount", 0) or 0) / 1e9
            if wallet:
                rows.append({
                    "wallet": wallet,
                    "token_in": amount,
                    "token_out": 0.0,
                    "sol_spent": sol_spent,
                    "timestamp": ts,
                    "tx_sig": sig,
                })

    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Fallback: get largest token accounts → pull their tx signatures
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_via_signatures(
    token_address: str,
    start_dt: datetime,
    end_dt: datetime,
    api_key: str,
    rpc_url: str,
) -> pd.DataFrame:
    """
    Alternative: get the largest token accounts for this mint,
    then pull transactions for each account and parse swaps.
    Useful when the mint-level query returns nothing (e.g. low-volume tokens).
    """
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    # Get largest holders — these accounts had activity
    accounts = _get_largest_token_accounts(token_address, rpc_url)
    if not accounts:
        return _rows_to_df([])

    st.info(f"Scanning {len(accounts)} token accounts for swap activity…")
    all_rows: list[dict] = []
    seen_sigs: set[str] = set()

    progress = st.progress(0)
    for i, acct in enumerate(accounts[:50]):  # cap at 50 accounts
        url = f"https://api.helius.xyz/v0/addresses/{acct}/transactions"
        before_sig = None

        for _ in range(20):
            params: dict = {"api-key": api_key, "limit": PAGE_SIZE}
            if before_sig:
                params["before"] = before_sig

            try:
                resp = requests.get(url, params=params, timeout=20)
                if not resp.ok:
                    break
                txs = resp.json()
            except Exception:
                break

            if not txs:
                break

            for tx in txs:
                ts_val = tx.get("timestamp", 0)
                sig = tx.get("signature", "")
                if ts_val < start_ts:
                    break
                if ts_val > end_ts or sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                parsed = _parse_helius_tx(tx, token_address)
                all_rows.extend(parsed)

            if txs[-1].get("timestamp", 0) < start_ts:
                break
            before_sig = txs[-1].get("signature")
            time.sleep(RATE_LIMIT_SLEEP)

        progress.progress((i + 1) / min(len(accounts), 50))

    progress.empty()
    return _rows_to_df(all_rows)


def _get_largest_token_accounts(token_address: str, rpc_url: str) -> list[str]:
    """Return up to 20 largest token account addresses for a mint."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenLargestAccounts",
        "params": [token_address, {"commitment": "confirmed"}],
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=15)
        result = resp.json().get("result", {}).get("value", [])
        return [r["address"] for r in result]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Pure RPC fallback (no Helius)
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_rpc_fallback(
    token_address: str,
    start_dt: datetime,
    end_dt: datetime,
    rpc: str,
) -> pd.DataFrame:
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    sigs = _get_signatures(token_address, start_ts, end_ts, rpc)
    if not sigs:
        return _rows_to_df([])

    rows = []
    progress = st.progress(0, text=f"Parsing {len(sigs)} transactions…")
    for i, sig_info in enumerate(sigs):
        tx_data = _get_transaction(sig_info["signature"], rpc)
        if tx_data:
            rows.extend(_parse_rpc_tx(tx_data, token_address, sig_info["signature"]))
        if i % 20 == 0:
            progress.progress((i + 1) / max(len(sigs), 1))
        time.sleep(RATE_LIMIT_SLEEP)

    progress.empty()
    return _rows_to_df(rows)


def _get_signatures(address: str, start_ts: int, end_ts: int, rpc: str) -> list[dict]:
    sigs = []
    before = None
    for _ in range(50):
        payload: dict = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": 1000, "commitment": "confirmed"}],
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
            bt = item.get("blockTime", 0)
            if bt < start_ts:
                return sigs
            if start_ts <= bt <= end_ts:
                sigs.append(item)
        before = result[-1]["signature"]
        time.sleep(RATE_LIMIT_SLEEP)
    return sigs


def _get_transaction(sig: str, rpc: str) -> Optional[dict]:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    }
    try:
        return requests.post(rpc, json=payload, timeout=20).json().get("result")
    except Exception:
        return None


def _parse_rpc_tx(tx: dict, token_address: str, sig: str) -> list[dict]:
    rows = []
    if not tx or tx.get("meta", {}).get("err"):
        return rows
    ts = datetime.utcfromtimestamp(tx.get("blockTime", 0))
    pre_tok = tx["meta"].get("preTokenBalances", [])
    post_tok = tx["meta"].get("postTokenBalances", [])
    keys = [ak["pubkey"] if isinstance(ak, dict) else ak
            for ak in tx["transaction"]["message"].get("accountKeys", [])]

    def bal_map(bals):
        m = {}
        for b in bals:
            if b.get("mint") == token_address:
                idx = b["accountIndex"]
                owner = b.get("owner", keys[idx] if idx < len(keys) else "")
                m[owner] = float(b["uiTokenAmount"].get("uiAmount") or 0)
        return m

    pre = bal_map(pre_tok)
    post = bal_map(post_tok)
    pre_sol = tx["meta"].get("preBalances", [])
    post_sol = tx["meta"].get("postBalances", [])

    for wallet in set(pre) | set(post):
        delta = post.get(wallet, 0) - pre.get(wallet, 0)
        if delta == 0:
            continue
        sol_delta = 0.0
        if wallet in keys:
            idx = keys.index(wallet)
            if idx < len(pre_sol) and idx < len(post_sol):
                sol_delta = (post_sol[idx] - pre_sol[idx]) / 1e9
        rows.append({
            "wallet": wallet,
            "token_in": max(delta, 0),
            "token_out": max(-delta, 0),
            "sol_spent": max(-sol_delta, 0),
            "timestamp": ts,
            "tx_sig": sig,
        })
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Balance query
# ══════════════════════════════════════════════════════════════════════════════
def _get_token_balance(wallet: str, token_address: str, rpc: str) -> float:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": token_address}, {"encoding": "jsonParsed"}],
    }
    try:
        resp = requests.post(rpc, json=payload, timeout=15)
        accounts = resp.json().get("result", {}).get("value", [])
        return sum(
            float(a["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0)
            for a in accounts
        )
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════
_KNOWN_PROGRAMS = {
    "11111111111111111111111111111111",         # System program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Token program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe8bv",  # ATA program
    "So11111111111111111111111111111111111111112",   # Wrapped SOL
}

def _is_likely_program(address: str) -> bool:
    """Heuristic: program addresses tend to be all digits/short or known."""
    return address in _KNOWN_PROGRAMS


def _rows_to_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["wallet", "token_in", "token_out", "sol_spent", "timestamp", "tx_sig"])
    return pd.DataFrame(rows)


def _extract_helius_key(rpc_url: str) -> Optional[str]:
    if not rpc_url:
        return None
    if "api-key=" in rpc_url:
        return rpc_url.split("api-key=")[-1].split("&")[0]
    return None
