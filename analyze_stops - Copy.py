"""
GAP 3 — Hard Stop Analysis
Derives ATR multiplier from actual backtest trade data.
"""
import pandas as pd
import numpy as np

df = pd.read_csv("backtest_results/trades.csv")
stops = df[df["exit_reason"] == "hard_stop"]
trails = df[df["exit_reason"].isin(["trail_loss", "trail_profit"])]
all_exits = df

print("=" * 55)
print("  HARD STOP ANALYSIS — GAP 3 RESEARCH")
print("=" * 55)
print(f"\n  Total trades:        {len(df)}")
print(f"  Hard stops:          {len(stops)} ({len(stops)/len(df)*100:.1f}%)")
print(f"  Trail exits:         {len(trails)} ({len(trails)/len(df)*100:.1f}%)")

print(f"\n  Hard stop outcomes:")
print(f"    Avg pnl:           {stops['pnl_pct'].mean()*100:.2f}%")
print(f"    Median pnl:        {stops['pnl_pct'].median()*100:.2f}%")
print(f"    Win rate:          {(stops['pnl_pct']>0).mean()*100:.1f}%")
print(f"    Avg hold days:     {stops['hold_days'].mean():.1f}")

# Composite score distribution at hard stop entries
print(f"\n  Composite score at hard-stop entries:")
print(f"    Mean:              {stops['composite_score'].mean():.3f}")
print(f"    Median:            {stops['composite_score'].median():.3f}")
print(f"    Std:               {stops['composite_score'].std():.3f}")
print(f"    25th pctile:       {stops['composite_score'].quantile(0.25):.3f}")
print(f"    75th pctile:       {stops['composite_score'].quantile(0.75):.3f}")

# Compare hard stop pnl vs trail pnl
print(f"\n  Comparison — avg pnl by exit type:")
for reason, grp in df.groupby("exit_reason"):
    print(f"    {reason:20s}  avg={grp['pnl_pct'].mean()*100:+.2f}%  n={len(grp)}")

# Hard stops by hold duration buckets
print(f"\n  Hard stops by hold duration:")
buckets = [(1,3,"early"),(4,10,"mid"),(11,20,"late"),(21,999,"final")]
for lo, hi, label in buckets:
    grp = stops[(stops["hold_days"] >= lo) & (stops["hold_days"] <= hi)]
    if len(grp):
        print(f"    Day {lo:2d}-{hi:3d} ({label:6s}):  n={len(grp):3d}  avg={grp['pnl_pct'].mean()*100:+.2f}%")

# Kelly / composite score segments for hard stops
print(f"\n  Hard stops by composite score quartile:")
for q_lo, q_hi, label in [(0,.25,"bottom 25%"),(.25,.50,"25-50%"),(.50,.75,"50-75%"),(.75,1.0,"top 25%")]:
    lo_val = stops["composite_score"].quantile(q_lo)
    hi_val = stops["composite_score"].quantile(q_hi)
    grp = stops[(stops["composite_score"] >= lo_val) & (stops["composite_score"] <= hi_val)]
    if len(grp):
        print(f"    {label:12s}  comp=[{lo_val:.2f},{hi_val:.2f}]  n={len(grp):3d}  avg={grp['pnl_pct'].mean()*100:+.2f}%")

print("\n" + "=" * 55)
