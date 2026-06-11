"""
demo_data.py
────────────
Generate realistic mock data for UI testing without hitting any RPC.
Activated automatically in app.py when token_address == "DEMO".
"""

from __future__ import annotations

import random
import string
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


def random_wallet() -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=44))


CATEGORIES = ["Smart Money", "Whale", "Retail", "Sniper", "Fresh Wallet"]
CATEGORY_WEIGHTS = [0.05, 0.10, 0.55, 0.15, 0.15]

STATUS_MAP = {
    (80, 100): "Full Hold",
    (1, 80): "Partial Hold",
    (0, 1): "Sold Out",
}


def generate_demo_buyers(n: int = 150, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)

    wallets = [random_wallet() for _ in range(n)]
    categories = rng.choice(CATEGORIES, size=n, p=CATEGORY_WEIGHTS)

    # Sol spent follows a power law
    sol_spent = rng.pareto(1.5, n) * 5 + 0.1

    # Net bought correlated with sol spent + noise
    net_bought = sol_spent * rng.uniform(800, 4000, n)

    # Retention varies by category
    retention_mu = {
        "Smart Money": 82,
        "Whale": 76,
        "Retail": 38,
        "Sniper": 55,
        "Fresh Wallet": 12,
    }
    pct_retained = np.array(
        [
            np.clip(rng.normal(retention_mu[c], 20), 0, 100)
            for c in categories
        ]
    )

    still_held = net_bought * pct_retained / 100
    avg_entry = sol_spent / net_bought

    now = datetime.utcnow()
    first_buy = [now - timedelta(hours=rng.uniform(0, 20)) for _ in range(n)]
    last_buy = [fb + timedelta(minutes=rng.uniform(0, 60)) for fb in first_buy]

    def _status(pct: float) -> str:
        if pct >= 80:
            return "Full Hold"
        if pct > 0.5:
            return "Partial Hold"
        return "Sold Out"

    df = pd.DataFrame(
        {
            "wallet": wallets,
            "total_bought": net_bought * rng.uniform(1.0, 1.3, n),
            "total_sold": net_bought * rng.uniform(0, 0.3, n),
            "net_bought": net_bought,
            "sol_spent": sol_spent,
            "avg_entry_price": avg_entry,
            "first_buy": first_buy,
            "last_buy": last_buy,
            "current_balance": still_held,
            "still_held": still_held,
            "pct_retained": pct_retained,
            "realized_tokens": net_bought - still_held,
            "unrealized_value_sol": still_held * avg_entry,
            "status": [_status(p) for p in pct_retained],
            "category": categories,
            "seconds_after_open": rng.uniform(0, 7200, n),
        }
    )

    return df.sort_values("net_bought", ascending=False).reset_index(drop=True)
