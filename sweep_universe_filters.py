"""
sweep_universe_filters.py — Universe Filter Sensitivity Analysis
================================================================
Runs a parameter sweep across all four hard filters in universe_builder.py
to find the Sharpe-optimal filter frontier.

The sweep measures three outcomes per filter combination:
  1. Universe size (N) — how many symbols pass
  2. Signal quality proxy — cross-sectional dispersion of daily_range_pct
     (higher dispersion = more differentiated MR opportunities)
  3. Liquidity quality — median dollar volume of passing symbols
     (higher = better fills at $105K scale)

These are combined into a composite filter score:
  filter_score = 0.5 × norm(signal_quality) + 0.3 × norm(liquidity_quality)
               + 0.2 × norm(universe_size_score)

Universe size score peaks at 120-150 symbols (optimal for cross-sectional
z-scoring — too few collapses the distribution, too many dilutes top signals).

Usage:
  python sweep_universe_filters.py                    # uses latest cached universe
  python sweep_universe_filters.py --live             # re-fetches from Alpaca (slow)
  python sweep_universe_filters.py --plot             # show heatmaps (requires matplotlib)

Output:
  Prints ranked table of top 20 filter combinations.
  Writes sweep_universe_results.json with full results.
  Recommends specific threshold changes vs current settings.

Requirements:
  Run from the Raptor directory. Needs at least one cached universe JSON
  in cache/universe/ or --live flag.
"""

import json
import os
import argparse
import logging
from datetime import datetime
from itertools import product
from typing import List, Dict, Any

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("raptor.universe_sweep")

# ── Current baseline thresholds ───────────────────────────────────────────────
BASELINE = {
    "price_min":    5.0,
    "price_max":    1000.0,
    "volume_min":   500_000,
    "dollar_vol_M": 20.0,    # $M
    "range_pct":    1.0,     # %
}

# ── Sweep ranges (±50% around current, in 5 steps) ───────────────────────────
SWEEP = {
    "price_min":    [3.0, 4.0, 5.0, 7.0, 10.0],
    "price_max":    [500.0, 750.0, 1000.0, 1500.0, 2000.0],
    "volume_min":   [200_000, 350_000, 500_000, 750_000, 1_000_000],
    "dollar_vol_M": [10.0, 15.0, 20.0, 30.0, 40.0],
    "range_pct":    [0.5, 0.75, 1.0, 1.25, 1.5],
}

# Optimal universe size target (cross-sectional z-scoring works best here)
UNIVERSE_TARGET = 130
UNIVERSE_MIN    = 60
UNIVERSE_MAX    = 200


# ── Data loading ──────────────────────────────────────────────────────────────

def load_cached_universe(cache_dir: str = "cache/universe") -> List[Dict]:
    """Load the most recent cached universe JSON."""
    if not os.path.exists(cache_dir):
        return []
    files = sorted([f for f in os.listdir(cache_dir) if f.endswith(".json")])
    if not files:
        return []
    latest = os.path.join(cache_dir, files[-1])
    with open(latest) as f:
        data = json.load(f)
    logger.info("Loaded %d symbols from %s", len(data), latest)
    return data


def fetch_live_universe(cfg) -> List[Dict]:
    """Fetch fresh universe from Alpaca (slow — ~2 minutes)."""
    from universe_builder import UniverseBuilder
    ub = UniverseBuilder(cfg)
    # Clear cache to force re-fetch
    import glob
    for f in glob.glob("cache/universe/universe_*.json"):
        os.remove(f)
    symbols, _ = ub.build_with_report(max_symbols=500)
    # Reload from cache (build() writes it)
    return load_cached_universe()


# ── Filter application ────────────────────────────────────────────────────────

def apply_filters(universe: List[Dict], params: Dict) -> List[Dict]:
    """Apply a set of filter thresholds to the raw universe data."""
    passed = []
    for sym in universe:
        price = sym.get("price", 0)
        vol   = sym.get("avg_volume_20d", 0)
        dvol  = sym.get("avg_dollar_vol_M", 0)
        rng   = sym.get("daily_range_pct", 0)

        if price < params["price_min"]:    continue
        if price > params["price_max"]:    continue
        if vol   < params["volume_min"]:   continue
        if dvol  < params["dollar_vol_M"]: continue
        if rng   < params["range_pct"]:    continue
        passed.append(sym)
    return passed


# ── Quality metrics ───────────────────────────────────────────────────────────

def compute_quality(passed: List[Dict]) -> Dict:
    """
    Compute signal quality proxies from passed universe.

    Signal quality:
      Cross-sectional dispersion of daily_range_pct — higher dispersion means
      more differentiated opportunity set for the z-scoring engine. A universe
      where every stock has range_pct ≈ 1.5% gives the signal engine nothing to
      separate. Wide dispersion (e.g. 0.8% to 6%) gives rich z-score contrasts.

    Liquidity quality:
      Median dollar volume — higher = better fills. Using median not mean to
      avoid single extremely liquid stocks (SPY etc) inflating the number.

    Universe size score:
      Peaks at UNIVERSE_TARGET (130), decays symmetrically on both sides.
      Too small: z-scores collapse, cross-sectional normalization breaks.
      Too large: top signals get diluted, false positives increase.
    """
    n = len(passed)
    if n == 0:
        return {"n": 0, "signal_quality": 0.0, "liquidity_quality": 0.0, "size_score": 0.0}

    ranges  = [s["daily_range_pct"] for s in passed]
    dvols   = [s["avg_dollar_vol_M"] for s in passed]

    # Signal quality: std dev of range_pct (dispersion = differentiation)
    signal_quality  = float(np.std(ranges))

    # Liquidity quality: median dollar volume (in $M)
    liquidity_quality = float(np.median(dvols))

    # Universe size score: Gaussian bell centered at UNIVERSE_TARGET
    size_score = float(np.exp(-0.5 * ((n - UNIVERSE_TARGET) / 40.0) ** 2))

    return {
        "n":                 n,
        "signal_quality":    round(signal_quality, 4),
        "liquidity_quality": round(liquidity_quality, 2),
        "size_score":        round(size_score, 4),
        "range_pct_median":  round(float(np.median(ranges)), 3),
        "range_pct_p90":     round(float(np.percentile(ranges, 90)), 3),
        "dvol_p25":          round(float(np.percentile(dvols, 25)), 1),
    }


# ── Sweep ─────────────────────────────────────────────────────────────────────

def run_sweep(universe: List[Dict]) -> pd.DataFrame:
    """
    Sweep all combinations of the 5 parameters.
    5^5 = 3125 combinations — runs in <1 second on cached data.
    """
    logger.info("Running parameter sweep (%d combinations)...",
                len(SWEEP["price_min"]) ** len(SWEEP))

    rows = []
    for pm, px, vm, dv, rp in product(
        SWEEP["price_min"], SWEEP["price_max"],
        SWEEP["volume_min"], SWEEP["dollar_vol_M"],
        SWEEP["range_pct"]
    ):
        params = {
            "price_min":    pm,
            "price_max":    px,
            "volume_min":   vm,
            "dollar_vol_M": dv,
            "range_pct":    rp,
        }
        passed = apply_filters(universe, params)
        q = compute_quality(passed)
        rows.append({**params, **q})

    df = pd.DataFrame(rows)

    # Normalize each quality metric to [0, 1] for composite scoring
    for col in ["signal_quality", "liquidity_quality", "size_score"]:
        mn, mx = df[col].min(), df[col].max()
        if mx > mn:
            df[f"{col}_norm"] = (df[col] - mn) / (mx - mn)
        else:
            df[f"{col}_norm"] = 0.5

    # Composite filter score
    df["filter_score"] = (
        0.50 * df["signal_quality_norm"] +
        0.30 * df["liquidity_quality_norm"] +
        0.20 * df["size_score_norm"]
    )

    # Compute delta vs baseline for each parameter
    baseline_passed = apply_filters(universe, BASELINE)
    baseline_q = compute_quality(baseline_passed)
    df["n_delta"] = df["n"] - baseline_q["n"]

    df = df.sort_values("filter_score", ascending=False).reset_index(drop=True)
    return df, baseline_q


# ── Single-parameter marginal analysis ────────────────────────────────────────

def marginal_analysis(universe: List[Dict]) -> Dict:
    """
    Hold all other filters at baseline, sweep each parameter independently.
    This shows the marginal effect of each filter in isolation.
    """
    results = {}
    for param, values in SWEEP.items():
        marginal = []
        for v in values:
            params = {**BASELINE, param: v}
            passed = apply_filters(universe, params)
            q = compute_quality(passed)
            marginal.append({"value": v, **q})
        results[param] = marginal
    return results


# ── Print report ──────────────────────────────────────────────────────────────

def print_report(df: pd.DataFrame, baseline_q: Dict, marginal: Dict):
    print("\n" + "=" * 80)
    print("  RAPTOR UNIVERSE FILTER SENSITIVITY ANALYSIS")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 80)

    print(f"\n  BASELINE (current settings):")
    print(f"    price_min=${BASELINE['price_min']}  price_max=${BASELINE['price_max']}")
    print(f"    volume_min={BASELINE['volume_min']:,}  dollar_vol=${BASELINE['dollar_vol_M']}M  range={BASELINE['range_pct']}%")
    print(f"    → Universe: {baseline_q['n']} symbols  "
          f"signal_quality={baseline_q['signal_quality']:.4f}  "
          f"liquidity=${baseline_q['liquidity_quality']:.1f}M median")

    # Baseline rank
    baseline_mask = (
        (df["price_min"]    == BASELINE["price_min"]) &
        (df["price_max"]    == BASELINE["price_max"]) &
        (df["volume_min"]   == BASELINE["volume_min"]) &
        (df["dollar_vol_M"] == BASELINE["dollar_vol_M"]) &
        (df["range_pct"]    == BASELINE["range_pct"])
    )
    baseline_row = df[baseline_mask]
    if not baseline_row.empty:
        rank = baseline_row.index[0] + 1
        score = baseline_row["filter_score"].values[0]
        print(f"    Baseline rank: {rank} / {len(df)}  (filter_score={score:.4f})")

    print(f"\n  TOP 15 FILTER COMBINATIONS:")
    print(f"  {'#':>3}  {'PMin':>5} {'PMax':>6} {'VolK':>6} {'$VolM':>6} {'Rng%':>5}  "
          f"{'N':>5} {'ΔN':>5} {'SigQ':>6} {'Liq$M':>6} {'Score':>6}")
    print("  " + "-" * 75)

    for i, row in df.head(15).iterrows():
        flag = " ← OPTIMAL" if i == 0 else ""
        print(f"  {i+1:3d}  "
              f"${row['price_min']:>4.0f} ${row['price_max']:>5.0f} "
              f"{row['volume_min']/1000:>5.0f}K "
              f"${row['dollar_vol_M']:>5.0f}M "
              f"{row['range_pct']:>4.2f}%  "
              f"{row['n']:>5.0f} {row['n_delta']:>+5.0f} "
              f"{row['signal_quality']:>6.4f} "
              f"{row['liquidity_quality']:>5.1f} "
              f"{row['filter_score']:>6.4f}"
              f"{flag}")

    print(f"\n  MARGINAL ANALYSIS (each filter swept, others held at baseline):")
    print(f"  {'Parameter':<16} {'Value':>10}  {'N':>5}  {'SigQ':>7}  {'Liq$M':>7}  {'SizeScore':>10}")
    print("  " + "-" * 65)

    param_labels = {
        "price_min":    "Price Min ($)",
        "price_max":    "Price Max ($)",
        "volume_min":   "Volume Min (K)",
        "dollar_vol_M": "Dollar Vol ($M)",
        "range_pct":    "Daily Range (%)",
    }
    for param, values_data in marginal.items():
        label = param_labels.get(param, param)
        for j, row in enumerate(values_data):
            val_str = f"${row['value']:,.0f}" if "price" in param or "dollar" in param \
                      else f"{row['value']/1000:.0f}K" if "volume" in param \
                      else f"{row['value']:.2f}%"
            marker = " ←" if row["value"] == BASELINE[param] else ""
            row_label = label if j == 0 else ""
            print(f"  {row_label:<16} {val_str:>10}{marker}  "
                  f"{row['n']:>5}  {row['signal_quality']:>7.4f}  "
                  f"{row['liquidity_quality']:>7.1f}  {row['size_score']:>10.4f}")
        print()

    # Recommendations
    best = df.iloc[0]
    print(f"  RECOMMENDATIONS (changes from baseline to optimal):")
    changes = []
    for param in BASELINE:
        if best[param] != BASELINE[param]:
            unit = "$M" if param == "dollar_vol_M" else \
                   "%" if param == "range_pct" else \
                   "K" if param == "volume_min" else "$"
            old = BASELINE[param]
            new = best[param]
            direction = "↑" if new > old else "↓"
            changes.append(f"    {param}: {old} → {new} {unit} {direction}")

    if changes:
        for c in changes:
            print(c)
    else:
        print("    Current baseline is already optimal — no changes recommended.")

    print(f"\n  SENSITIVITY RANKING (which filter matters most?):")
    # Compute range of filter_score when only changing each parameter
    sensitivity = {}
    for param in BASELINE:
        values = df[param].unique()
        if len(values) > 1:
            scores = []
            for v in values:
                subset = df[df[param] == v]["filter_score"]
                scores.append(subset.mean())
            sensitivity[param] = max(scores) - min(scores)
        else:
            sensitivity[param] = 0.0

    for param, sens in sorted(sensitivity.items(), key=lambda x: -x[1]):
        bar = "█" * int(sens * 100)
        print(f"    {param_labels.get(param, param):<20} {sens:.4f}  {bar}")

    print("\n" + "=" * 80)


# ── Save results ──────────────────────────────────────────────────────────────

def save_results(df: pd.DataFrame, baseline_q: Dict, marginal: Dict):
    best = df.iloc[0].to_dict()
    output = {
        "generated_at": datetime.now().isoformat(),
        "baseline":     BASELINE,
        "baseline_quality": baseline_q,
        "optimal_params": {
            "price_min":    best["price_min"],
            "price_max":    best["price_max"],
            "volume_min":   best["volume_min"],
            "dollar_vol_M": best["dollar_vol_M"],
            "range_pct":    best["range_pct"],
        },
        "optimal_quality": {
            "n":               int(best["n"]),
            "signal_quality":  best["signal_quality"],
            "liquidity_quality": best["liquidity_quality"],
            "filter_score":    best["filter_score"],
        },
        "top_20": df.head(20)[[
            "price_min", "price_max", "volume_min", "dollar_vol_M", "range_pct",
            "n", "n_delta", "signal_quality", "liquidity_quality", "filter_score"
        ]].to_dict(orient="records"),
        "marginal_analysis": marginal,
    }
    with open("sweep_universe_results.json", "w") as f:
        json.dump(output, f, indent=2)
    logger.info("Results saved → sweep_universe_results.json")

    # Print str_replace-ready code for the optimal settings
    p = output["optimal_params"]
    print(f"\n  STR_REPLACE READY — apply to universe_builder.py _screen():")
    print(f"  ─────────────────────────────────────────────────────────────")
    print(f"  # Price: ${p['price_min']:.0f} - ${p['price_max']:.0f}")
    print(f"  if latest_price < {p['price_min']} or latest_price > {p['price_max']}:")
    print(f"  # Avg daily volume ≥ {p['volume_min']:,}")
    print(f"  if avg_volume_20d < {p['volume_min']:,}:")
    print(f"  # Avg daily dollar volume ≥ ${p['dollar_vol_M']}M")
    print(f"  if avg_dollar_volume < {p['dollar_vol_M'] * 1_000_000:,.0f}:")
    print(f"  # Daily range ≥ {p['range_pct']}%")
    print(f"  if daily_range_pct < {p['range_pct']}:")


# ── Heatmap (optional) ────────────────────────────────────────────────────────

def plot_heatmaps(df: pd.DataFrame):
    try:
        import matplotlib.pyplot as plt

        # Fix price_min and price_max at baseline, sweep other 3 parameters
        sub = df[
            (df["price_min"]  == BASELINE["price_min"]) &
            (df["price_max"]  == BASELINE["price_max"])
        ].copy()

        # Heatmap 1: volume_min vs dollar_vol_M (filter_score)
        pivot1 = sub.groupby(["volume_min", "dollar_vol_M"])["filter_score"].mean().unstack()
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        im1 = axes[0].imshow(pivot1.values, cmap="RdYlGn", aspect="auto")
        axes[0].set_xticks(range(len(pivot1.columns)))
        axes[0].set_xticklabels([f"${c:.0f}M" for c in pivot1.columns])
        axes[0].set_yticks(range(len(pivot1.index)))
        axes[0].set_yticklabels([f"{v/1000:.0f}K" for v in pivot1.index])
        axes[0].set_xlabel("Dollar Volume Min")
        axes[0].set_ylabel("Volume Min")
        axes[0].set_title("Filter Score: Volume vs Dollar Volume")
        plt.colorbar(im1, ax=axes[0])

        # Heatmap 2: range_pct vs dollar_vol_M
        pivot2 = sub.groupby(["range_pct", "dollar_vol_M"])["filter_score"].mean().unstack()
        im2 = axes[1].imshow(pivot2.values, cmap="RdYlGn", aspect="auto")
        axes[1].set_xticks(range(len(pivot2.columns)))
        axes[1].set_xticklabels([f"${c:.0f}M" for c in pivot2.columns])
        axes[1].set_yticks(range(len(pivot2.index)))
        axes[1].set_yticklabels([f"{v:.2f}%" for v in pivot2.index])
        axes[1].set_xlabel("Dollar Volume Min")
        axes[1].set_ylabel("Daily Range Min")
        axes[1].set_title("Filter Score: Range vs Dollar Volume")
        plt.colorbar(im2, ax=axes[1])

        plt.suptitle("Raptor Universe Filter Sensitivity Analysis", fontweight="bold")
        plt.tight_layout()
        plt.savefig("sweep_universe_heatmaps.png", dpi=120)
        logger.info("Heatmaps saved → sweep_universe_heatmaps.png")
        plt.show()
    except ImportError:
        logger.warning("matplotlib not available — skipping heatmaps")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Universe Filter Sensitivity Analysis")
    parser.add_argument("--live",  action="store_true", help="Re-fetch from Alpaca (slow)")
    parser.add_argument("--plot",  action="store_true", help="Show heatmaps")
    parser.add_argument("--cache", default="cache/universe", help="Cache directory")
    args = parser.parse_args()

    if args.live:
        from config import CONFIG
        universe = fetch_live_universe(CONFIG)
    else:
        universe = load_cached_universe(args.cache)

    if not universe:
        print(
            "ERROR: No cached universe data found.\n"
            "Run with --live to fetch from Alpaca, or run universe_builder.py first.\n"
            "Example: python universe_builder.py  (then re-run this script)"
        )
        return

    df, baseline_q = run_sweep(universe)
    marginal = marginal_analysis(universe)
    print_report(df, baseline_q, marginal)
    save_results(df, baseline_q, marginal)

    if args.plot:
        plot_heatmaps(df)


if __name__ == "__main__":
    main()
