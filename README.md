# 🔭 SOL Token Retention Tracker

Analyze buyer retention for any Solana token across a custom time window. Track who bought, how much they still hold, and whether smart money or whales are keeping their bags.

## What it does

1. **Fetch Swaps** — Pulls all on-chain swap activity for a token during a user-defined window (e.g. June 8 2PM → June 9 2PM)
2. **Identify Net Buyers** — Wallets where `tokens_in > tokens_out` during the period
3. **Current Balances** — Live queries each wallet's current token balance
4. **Retention Calculation** — `% retained = current_holding / net_bought × 100`
5. **Filter Modes** — Slice by All Buyers, Top N, Whale threshold, or Smart Wallet classification

## Output example

| Wallet | Bought | Current Holding | Retention |
|--------|--------|-----------------|-----------|
| ABC...XYZ | 100,000 | 90,000 | 90% |
| DEF...UVW | 50,000 | 0 | 0% |

**Cohort totals:** Total Bought, Total Held, Cohort Retention %

## Filter modes

| Mode | Description |
|------|-------------|
| All Buyers | Full cohort |
| Top N Buyers | Largest accumulators — often most predictive |
| Whale Filter | Wallets that spent ≥ X SOL |
| Smart Wallet Classification | Classify into Smart Money / Whale / Retail / Sniper / Fresh Wallet |

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/sol-retention-tracker
cd sol-retention-tracker
pip install -r requirements.txt
streamlit run app.py
```

## RPC / API

The app works with any Solana RPC, but **Helius is strongly recommended** for production use — it provides enriched transaction data that makes swap parsing far more accurate.

Get a free key at [helius.dev](https://helius.dev) and paste the full RPC URL into the sidebar.

Without a Helius key, the app falls back to the public Solana RPC (slower, rate-limited, less accurate swap parsing).

## Demo mode

Enter `DEMO` as the token address to run with synthetic data and test all UI features without an RPC call.

## Deploy to Streamlit Cloud

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your repo, set main file to `app.py`
4. Set your RPC URL as a secret: `RPC_URL = "https://mainnet.helius-rpc.com/?api-key=..."`

## Project structure

```
app.py                  ← Streamlit entry point
src/
  data_fetcher.py       ← Helius + RPC data layer
  analyzer.py           ← Retention logic + wallet classification
  display.py            ← Streamlit rendering helpers
  demo_data.py          ← Synthetic data for testing
requirements.txt
```

## Wallet categories

| Category | Criteria |
|----------|----------|
| Smart Money | High SOL spend + high retention |
| Whale | ≥ 50 SOL spent |
| Sniper | Bought within 2 min of window open |
| Fresh Wallet | Very low SOL history |
| Retail | Everything else |

Thresholds are configurable in `src/analyzer.py`.
