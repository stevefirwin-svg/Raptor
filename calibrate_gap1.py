"""
calibrate_gap1.py — GAP 1 Trail Modifier Calibration
======================================================
Reads backtest trades.csv (output of backtest.py with composite_proxy
and health_proxy columns) and derives optimal thresholds for:

  1. signal_strength threshold (currently 0.3/-0.3)
     — the breakpoint where we widen/tighten the trail
  2. wide modifier (currently 1.3)
     — how much to widen trail for strong-signal positions
  3. tight modifier (currently 0.75)
     — how much to tighten trail for weak-signal positions

Method:
  For each candidate threshold/modifier combination, simulate the
  _trail_mult() modifier effect on the trade population and compute
  the resulting Sharpe improvement vs the neutral baseline (modifier=1.0).

  This is NOT a full re-backtest — it's a parameter sweep over the
  existing trade population to find the modifier settings that would
  have maximized risk-adjusted returns, given the composite_proxy
  values already recorded per trade.

Usage:
  python calibrate_gap1.py                          # default results dir
  python calibrate_gap1.py --dir backtest_results   # custom dir
  python calibrate_gap1.py --plot                   # show heatmap (requires matplotlib)

Output:
  Prints recommended threshold and modifier values.
  Writes calibration_gap1.json with full sweep results.
"""

import json
import os
import argparse
import numpy as np
import pandas as pd
from itertools import product


RESULTS_DIR = "backtest_results"
OUTPUT_FILE = "calibration_gap1.json"


def load_trades(results_dir: str) -> pd.DataFrame:
    path = os.path.join(results_dir, "trades.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"trades.csv not found at {path}\n"
            f"Run backtest.py first to generate trade data."
        )
    df = pd.read_csv(path)

    required = ["composite_proxy", "health_proxy", "pnl_pct", "hold_days", "exit_reason"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"trades.csv is missing columns: {missing}\n"
            f"Re-run backtest.py — these columns were added in the GAP E update."
        )

    # Drop rows with missing proxy values (pre-GAP-E trades)
    df = df.dropna(subset=["composite_proxy", "health_proxy", "pnl_pct"])
    print(f"Loaded {len(df)} trades with composite/health proxy data.")
    return df


def compute_signal_strength(df: pd.DataFrame) -> pd.Series:
    """Mirror of exit_monitor._trail_mult(): signal_strength = (composite + health) / 2"""
    return (df["composite_proxy"] + df["health_proxy"]) / 2.0


def sharpe_from_returns(returns: np.ndarray, avg_hold_days: float) -> float:
    """Per-trade Sharpe annualized by hold period."""
    if len(returns) < 3 or returns.std() == 0:
        return 0.0
    ann = np.sqrt(252.0 / max(avg_hold_days, 1.0))
    return float(returns.mean() / returns.std() * ann)


def simulate_modifier_effect(
    df: pd.DataFrame,
    signal_strength: pd.Series,
    threshold: float,
    wide_mult: float,
    tight_mult: float,
) -> dict:
    """
    Simulate the effect of a given modifier configuration on PnL.

    For trades where the modifier would have widened the trail (strong signal):
      - The trail was wider → position held longer → we proxy this as
        pnl_pct * wide_mult (captures the directional benefit of staying in longer).
    For trades where the modifier would have tightened the trail (weak signal):
      - The trail was tighter → losses cut faster → we proxy as
        pnl_pct * tight_mult for losing trades (losses are smaller).

    This is a linear approximation, not a full re-simulation. It measures
    directional consistency — does widening help winners? Does tightening
    help losers? — rather than exact P&L.
    """
    r = df["pnl_pct"].values.copy()
    strength = signal_strength.values

    for i, (ret, s) in enumerate(zip(r, strength)):
        if s > threshold:
            # Strong signal — wider trail benefits winning trades
            if ret > 0:
                r[i] = ret * wide_mult
            # Losing trades held longer = slightly more loss (honest accounting)
            else:
                r[i] = ret * (1 + (wide_mult - 1) * 0.3)
        elif s < -threshold:
            # Weak signal — tighter trail benefits losing trades
            if ret < 0:
                r[i] = ret * tight_mult  # tight_mult < 1 → smaller loss
            # Winning trades cut earlier = slightly less gain
            else:
                r[i] = ret * (1 - (1 - tight_mult) * 0.3)

    avg_hold = df["hold_days"].mean()
    sharpe = sharpe_from_returns(r, avg_hold)
    win_rate = float((r > 0).mean() * 100)
    avg_pnl = float(r.mean() * 100)

    return {
        "sharpe":   round(sharpe, 4),
        "win_rate": round(win_rate, 2),
        "avg_pnl":  round(avg_pnl, 4),
        "n_strong": int((signal_strength > threshold).sum()),
        "n_weak":   int((signal_strength < -threshold).sum()),
        "n_neutral": int(((signal_strength >= -threshold) & (signal_strength <= threshold)).sum()),
    }


def run_sweep(df: pd.DataFrame) -> pd.DataFrame:
    """
    Grid search over threshold, wide_mult, tight_mult.

    Candidate ranges chosen to bracket the current live values:
      threshold:   [0.1, 0.2, 0.3, 0.4, 0.5]   (current: 0.3)
      wide_mult:   [1.1, 1.2, 1.3, 1.4, 1.5]   (current: 1.3)
      tight_mult:  [0.6, 0.65, 0.7, 0.75, 0.8] (current: 0.75)
    """
    signal_strength = compute_signal_strength(df)

    # Baseline: neutral modifier (all 1.0x) — what GAP 1 replaces
    baseline_r = df["pnl_pct"].values
    avg_hold = df["hold_days"].mean()
    baseline_sharpe = sharpe_from_returns(baseline_r, avg_hold)
    baseline_wr = float((baseline_r > 0).mean() * 100)

    print(f"\nBaseline (neutral modifier): Sharpe={baseline_sharpe:.4f}  WR={baseline_wr:.1f}%")
    print("\nRunning parameter sweep...")

    thresholds  = [0.1, 0.2, 0.3, 0.4, 0.5]
    wide_mults  = [1.1, 1.2, 1.3, 1.4, 1.5]
    tight_mults = [0.60, 0.65, 0.70, 0.75, 0.80]

    rows = []
    for thr, wm, tm in product(thresholds, wide_mults, tight_mults):
        result = simulate_modifier_effect(df, signal_strength, thr, wm, tm)
        rows.append({
            "threshold":   thr,
            "wide_mult":   wm,
            "tight_mult":  tm,
            "sharpe":      result["sharpe"],
            "sharpe_delta": round(result["sharpe"] - baseline_sharpe, 4),
            "win_rate":    result["win_rate"],
            "avg_pnl":     result["avg_pnl"],
            "n_strong":    result["n_strong"],
            "n_weak":      result["n_weak"],
            "n_neutral":   result["n_neutral"],
        })

    sweep = pd.DataFrame(rows).sort_values("sharpe_delta", ascending=False)
    return sweep, baseline_sharpe, baseline_wr


def print_report(sweep: pd.DataFrame, baseline_sharpe: float, baseline_wr: float,
                 df: pd.DataFrame):
    """Print calibration report with top configurations."""

    best = sweep.iloc[0]
    current = sweep[
        (sweep["threshold"] == 0.3) &
        (sweep["wide_mult"] == 1.3) &
        (sweep["tight_mult"] == 0.75)
    ]

    print("\n" + "=" * 65)
    print("  GAP 1 CALIBRATION REPORT")
    print("=" * 65)

    print(f"\n  BASELINE (modifier=neutral, all 1.0x)")
    print(f"    Sharpe:   {baseline_sharpe:.4f}")
    print(f"    Win Rate: {baseline_wr:.1f}%")

    print(f"\n  CURRENT LIVE SETTINGS (threshold=0.3, wide=1.3x, tight=0.75x)")
    if not current.empty:
        c = current.iloc[0]
        print(f"    Sharpe:       {c['sharpe']:.4f}  (delta: {c['sharpe_delta']:+.4f})")
        print(f"    Win Rate:     {c['win_rate']:.1f}%")
        print(f"    Strong trades: {c['n_strong']}  |  Weak: {c['n_weak']}  |  Neutral: {c['n_neutral']}")
    else:
        print("    (not in sweep grid)")

    print(f"\n  OPTIMAL SETTINGS (maximize Sharpe delta over baseline)")
    print(f"    threshold:  {best['threshold']}")
    print(f"    wide_mult:  {best['wide_mult']}")
    print(f"    tight_mult: {best['tight_mult']}")
    print(f"    Sharpe:     {best['sharpe']:.4f}  (delta: {best['sharpe_delta']:+.4f})")
    print(f"    Win Rate:   {best['win_rate']:.1f}%")
    print(f"    Strong:     {best['n_strong']}  |  Weak: {best['n_weak']}  |  Neutral: {best['n_neutral']}")

    print(f"\n  TOP 10 CONFIGURATIONS")
    print(f"  {'Threshold':>10} {'Wide':>6} {'Tight':>7} {'Sharpe':>8} {'Delta':>8} {'WR%':>6}")
    print("  " + "-" * 52)
    for _, row in sweep.head(10).iterrows():
        print(f"  {row['threshold']:>10.2f} {row['wide_mult']:>6.2f} {row['tight_mult']:>7.2f} "
              f"{row['sharpe']:>8.4f} {row['sharpe_delta']:>+8.4f} {row['win_rate']:>6.1f}%")

    # GAP 1 validation summary
    strength = compute_signal_strength(df)
    q33 = strength.quantile(0.33)
    q67 = strength.quantile(0.67)
    strong_pnl = df.loc[strength > q67, "pnl_pct"].mean() * 100
    weak_pnl   = df.loc[strength < q33, "pnl_pct"].mean() * 100
    delta = strong_pnl - weak_pnl

    print(f"\n  GAP 1 HYPOTHESIS TEST")
    print(f"    Strong signal trades avg PnL: {strong_pnl:+.3f}%")
    print(f"    Weak signal trades avg PnL:   {weak_pnl:+.3f}%")
    print(f"    Delta (strong - weak):        {delta:+.3f}%")
    if delta > 0:
        print(f"    Verdict: GAP 1 VALIDATED — strong signals outperform weak")
    else:
        print(f"    Verdict: GAP 1 INCONCLUSIVE — proxy may need refinement")

    print("\n" + "=" * 65)


def save_results(sweep: pd.DataFrame, best_config: dict, results_dir: str):
    out = {
        "best_config": best_config,
        "current_config": {"threshold": 0.3, "wide_mult": 1.3, "tight_mult": 0.75},
        "top_10": sweep.head(10).to_dict(orient="records"),
        "generated_at": pd.Timestamp.now().isoformat(),
    }
    path = os.path.join(results_dir, OUTPUT_FILE)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  Results saved → {path}")
    print(f"\n  To apply optimal settings, update exit_monitor.py _trail_mult():")
    print(f"    if signal_strength > {best_config['threshold']}: modifier = {best_config['wide_mult']}")
    print(f"    elif signal_strength < -{best_config['threshold']}: modifier = {best_config['tight_mult']}")


def plot_heatmap(sweep: pd.DataFrame, threshold: float = 0.3):
    """Optional: heatmap of Sharpe delta at fixed threshold."""
    try:
        import matplotlib.pyplot as plt
        sub = sweep[sweep["threshold"] == threshold]
        pivot = sub.pivot(index="wide_mult", columns="tight_mult", values="sharpe_delta")
        fig, ax = plt.subplots(figsize=(8, 5))
        im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto")
        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels([f"{c:.2f}" for c in pivot.columns])
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels([f"{i:.2f}" for i in pivot.index])
        ax.set_xlabel("tight_mult")
        ax.set_ylabel("wide_mult")
        ax.set_title(f"Sharpe Delta (threshold={threshold}) — GAP 1 Calibration")
        plt.colorbar(im, ax=ax, label="Sharpe delta vs baseline")
        plt.tight_layout()
        plt.savefig(f"backtest_results/gap1_heatmap_thr{threshold}.png", dpi=120)
        print(f"  Heatmap saved → backtest_results/gap1_heatmap_thr{threshold}.png")
        plt.show()
    except ImportError:
        print("  matplotlib not available — skipping heatmap")


def main():
    parser = argparse.ArgumentParser(description="GAP 1 Trail Modifier Calibration")
    parser.add_argument("--dir", default=RESULTS_DIR, help="Backtest results directory")
    parser.add_argument("--plot", action="store_true", help="Show Sharpe delta heatmap")
    args = parser.parse_args()

    df = load_trades(args.dir)
    sweep, baseline_sharpe, baseline_wr = run_sweep(df)
    print_report(sweep, baseline_sharpe, baseline_wr, df)

    best = sweep.iloc[0]
    best_config = {
        "threshold":  float(best["threshold"]),
        "wide_mult":  float(best["wide_mult"]),
        "tight_mult": float(best["tight_mult"]),
        "sharpe":     float(best["sharpe"]),
        "sharpe_delta": float(best["sharpe_delta"]),
    }
    save_results(sweep, best_config, args.dir)

    if args.plot:
        for thr in [0.2, 0.3, 0.4]:
            plot_heatmap(sweep, threshold=thr)


if __name__ == "__main__":
    main()
