"""
Verify dynamic macro regime is actually varying in backtest.
Run: python diagnose_regime.py
"""
import json
import numpy as np
import pandas as pd
from config import CONFIG
from backtest import Backtester

bt = Backtester(CONFIG)

# Load SPY from cache if available
import os, glob
cache_files = glob.glob("cache/backtest_bars/SPY_*.parquet")
if not cache_files:
    print("No SPY cache found — run backtest first to build cache")
    exit()

spy = pd.read_parquet(cache_files[0])
print(f"SPY bars loaded: {len(spy)} ({spy.index[0].date()} to {spy.index[-1].date()})")

# Sample regime on every 20th trading day
regimes = []
dates_sample = spy.index[::20]
for date in dates_sample:
    window = spy.loc[spy.index <= date].tail(250)
    if len(window) < 22:
        continue
    m = bt._compute_backtest_macro(window, date)
    regimes.append({
        "date": str(date.date()),
        "regime": m["regime"],
        "spy_trend": round(m.get("spy_trend", 0)*100, 2),
        "rvol": round(m.get("rvol", 0)*100, 2),
    })

df = pd.DataFrame(regimes)
print(f"\nRegime distribution across {len(df)} sample dates:")
print(df["regime"].value_counts().to_string())
print(f"\nSample of regime changes:")
print(df[["date","regime","spy_trend","rvol"]].to_string(index=False))

# Check CRISIS periods match known crash dates
crisis = df[df["regime"] == "CRISIS"]
if not crisis.empty:
    print(f"\nCRISIS periods detected: {len(crisis)}")
    print(crisis[["date","spy_trend","rvol"]].to_string(index=False))
else:
    print("\nNo CRISIS periods — check thresholds")
