"""
RAPTOR ANALYTICS v4.0  —  analytics.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Enhanced reporting:
  - Filled order history with P&L
  - Win rate by sector, by direction, by score tier
  - Factor analysis: which factors predicted winners?
  - Drawdown analysis
  - Risk-adjusted returns (Sharpe, Sortino)

Run: python analytics.py
"""

import os
import logging
import numpy as np
import pandas as pd

import config
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import OrderStatus, QueryOrderStatus

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("Raptor.Analytics")


def run():
    client = TradingClient(
        config.ALPACA_API_KEY,
        config.ALPACA_SECRET_KEY,
        paper=config.PAPER_TRADING,
    )

    print(f"\n{'='*70}")
    print("RAPTOR v4.0 — PERFORMANCE REPORT")
    print(f"{'='*70}\n")

    # ── Account Summary ───────────────────────────────────────────────────
    try:
        acct = client.get_account()
        equity = float(acct.equity)
        cash = float(acct.cash)
        positions = client.get_all_positions()
        print(f"  Equity:       ${equity:>12,.2f}")
        print(f"  Cash:         ${cash:>12,.2f}")
        print(f"  Positions:    {len(positions):>12}")
        print()
    except Exception as e:
        print(f"  Account error: {e}\n")

    # ── Alpaca Trade History ──────────────────────────────────────────────
    req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=200, nested=True)
    orders = client.get_orders(req)
    filled = [o for o in orders if o.status == OrderStatus.FILLED]

    if filled:
        print(f"  Filled orders: {len(filled)}")
        print(f"  {'─'*60}")
        for o in filled[-20:]:  # show last 20
            t = o.filled_at.strftime("%Y-%m-%d %H:%M") if o.filled_at else "?"
            qty = o.filled_qty
            px = float(o.filled_avg_price) if o.filled_avg_price else 0.0
            print(
                f"  [{t}] {o.symbol:6} | {o.side.value.upper():4} | "
                f"qty:{qty:>6} | avg:${px:>8.2f}"
            )
        print()

    # ── Trade Log Analysis ────────────────────────────────────────────────
    if os.path.exists(config.TRADE_LOG_FILE):
        df = pd.read_csv(config.TRADE_LOG_FILE)
        exits = df[df["action"].str.startswith("EXIT", na=False)]

        if len(exits) > 0:
            print(f"  {'='*60}")
            print(f"  TRADE LOG ANALYSIS ({len(exits)} exits)")
            print(f"  {'='*60}\n")

            pnl = exits["pnl"].astype(float)
            wins = pnl > 0
            losses = pnl < 0

            print(f"  Win Rate:     {wins.mean():.1%}")
            print(f"  Avg Win:      ${pnl[wins].mean():>8.2f}" if wins.any() else "")
            print(f"  Avg Loss:     ${pnl[losses].mean():>8.2f}" if losses.any() else "")
            print(f"  Total P&L:    ${pnl.sum():>8.2f}")
            print(f"  Avg Hold:     {exits['hold_minutes'].mean():.1f} min")

            # Profit factor
            gross_profit = pnl[wins].sum() if wins.any() else 0
            gross_loss = abs(pnl[losses].sum()) if losses.any() else 1
            print(f"  Profit Factor: {gross_profit / gross_loss:.2f}")

            # Sharpe (daily-ish approximation)
            if len(pnl) > 5:
                sharpe = pnl.mean() / (pnl.std() + 1e-9) * np.sqrt(252)
                print(f"  Sharpe (ann):  {sharpe:.2f}")

            # Sortino
            downside = pnl[pnl < 0]
            if len(downside) > 2:
                sortino = pnl.mean() / (downside.std() + 1e-9) * np.sqrt(252)
                print(f"  Sortino (ann): {sortino:.2f}")

            # Win rate by direction
            print(f"\n  {'─'*40}")
            print(f"  Win Rate by Direction:")
            for d in ["long", "short"]:
                sub = exits[exits["direction"] == d]
                if len(sub) > 0:
                    wr = (sub["pnl"].astype(float) > 0).mean()
                    print(f"    {d:6}: {wr:.1%} ({len(sub)} trades)")

            # Win rate by score tier
            print(f"\n  {'─'*40}")
            print(f"  Win Rate by Score Tier:")
            exits_scored = exits[exits["score"].notna()].copy()
            if len(exits_scored) > 0:
                exits_scored["score"] = exits_scored["score"].astype(float)
                for lo, hi, label in [
                    (0.8, 1.0, "Elite (0.80+)"),
                    (0.7, 0.8, "Strong (0.70-0.80)"),
                    (0.6, 0.7, "Good (0.60-0.70)"),
                    (0.5, 0.6, "Marginal (0.55-0.60)"),
                ]:
                    sub = exits_scored[
                        (exits_scored["score"] >= lo) &
                        (exits_scored["score"] < hi)
                    ]
                    if len(sub) > 0:
                        wr = (sub["pnl"].astype(float) > 0).mean()
                        avg = sub["pnl"].astype(float).mean()
                        print(f"    {label:24}: {wr:.1%} | "
                              f"avg=${avg:+.2f} | n={len(sub)}")

            # Factor correlations with P&L
            factor_cols = [
                "hurst", "ir", "rsi", "ofi", "vpa",
                "entropy", "smart_money", "autocorr",
            ]
            available = [c for c in factor_cols if c in exits.columns]
            if available and len(exits) > 10:
                print(f"\n  {'─'*40}")
                print(f"  Factor Correlation with P&L:")
                for col in available:
                    vals = pd.to_numeric(exits[col], errors="coerce")
                    if vals.notna().sum() > 5:
                        corr = vals.corr(pnl)
                        if not np.isnan(corr):
                            bar = "█" * int(abs(corr) * 20)
                            sign = "+" if corr > 0 else "-"
                            print(f"    {col:15}: {sign}{abs(corr):.3f} {bar}")

        else:
            print("  No exit records in trade log yet.\n")
    else:
        print(f"  No trade log at {config.TRADE_LOG_FILE}")
        print("  Run the bot to start collecting data.\n")


if __name__ == "__main__":
    run()
