"""
factor_lab.py — Factor IC Validation Lab
==========================================
Measures whether each signal factor has genuine predictive power.

For each factor, computes:
  - Information Coefficient (IC): Spearman correlation between
    factor z-score at snapshot time and realized forward return
  - IC t-statistic: statistical significance of IC
  - Rolling IC: 30d / 60d / 120d windows (stability check)
  - IC by regime: does the factor work in all regimes or only some?
  - IC decay: does signal decay fast (1d) or persist (10d)?
  - Turnover proxy: how stable are factor rankings day-to-day?

Data sources (Option C — both, weighted):
  - outcome_log.json:  trade outcomes with factor_scores at entry
                       (primary — clean, realized returns, weight=2)
  - hold_history.json: daily snapshots with factor_scores mid-hold
                       (secondary — more observations, weight=1)

Runs: Daily after close or on-demand.
Output: factor_ic_report.json + console report

Research basis:
  - Grinold & Kahn (Active Portfolio Management) — IC/ICIR framework
  - Fama (1976) — factor validity requires out-of-sample IC > 0
  - Qian, Hua & Sorensen (2007) — Quantitative Equity Portfolio Mgmt
"""

import json, os, logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import numpy as np
from scipy import stats as scipy_stats

logger = logging.getLogger("raptor.factor_lab")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTCOME_LOG = os.path.join(BASE_DIR, "outcome_log.json")
HOLD_HIST   = os.path.join(BASE_DIR, "hold_history.json")
LEDGER      = os.path.join(BASE_DIR, "position_ledger.json")
IC_REPORT   = os.path.join(BASE_DIR, "factor_ic_report.json")

# Forward return windows to evaluate (days)
FORWARD_WINDOWS = [1, 3, 5, 10]

# Minimum observations required for a valid IC estimate
MIN_OBS_IC      = 15   # warn below this
MIN_OBS_TSTAT   = 30   # t-stat only meaningful above this

# IC significance threshold (Grinold: IC > 0.05 is meaningful)
IC_MEANINGFUL   = 0.05
IC_TSTAT_MIN    = 1.5  # t-stat threshold for factor retention

# Factor registries (from signals.py)
MOMENTUM_FACTORS = [
    "ma_stack", "macd_accel", "adx_dir", "rel_strength",
    "obv_r2", "accum_dist", "price_cloud", "vol_ratio",
]
MR_FACTORS = [
    "rsi_mr", "bollinger_z", "crowd_panic", "ma_distance",
    "bb_squeeze", "rev_momentum", "atr_pctile",
]
ALL_FACTORS = MOMENTUM_FACTORS + MR_FACTORS


# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_outcome_observations() -> List[Dict]:
    """
    Load factor z-scores + realized returns from outcome_log.json.
    Each record: {symbol, factor_scores, return, hold_days, regime, trade_type, date}
    Weight = 2 (primary source — clean realized returns).
    """
    if not os.path.exists(OUTCOME_LOG):
        logger.warning("outcome_log.json not found")
        return []

    with open(OUTCOME_LOG) as f:
        outcomes = json.load(f)

    # Cross-reference with ledger for factor_scores at entry
    ledger_meta = {}
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            ledger = json.load(f)
        for pos in ledger.get("closed", []):
            sym = pos.get("symbol", "")
            meta = pos.get("metadata", {})
            if meta.get("factor_scores"):
                ledger_meta[sym] = meta

    observations = []
    for t in outcomes:
        pnl = t.get("actual_pnl_pct")
        if pnl is None:
            continue

        sym = t.get("symbol", "")
        # Exclude crypto — different system, different distribution
        if "/" in sym:
            continue
        # Exclude zero-hold artifacts
        if (t.get("hold_days") or 0) == 0:
            continue
        # Exclude partial exits (math_trim) — pnl_pct reflects the mark at trim time,
        # not the terminal realized return. IC computed against partial returns measures
        # trim-timing accuracy, not factor directional predictive power.
        # Only full terminal exits produce valid IC signal.
        exit_path = t.get("actual_exit_path", "")
        if exit_path == "math_trim":
            continue
        # Normalize pnl: values >1 are percentages (e.g. 5.2 = 5.2%), convert to decimal
        pnl = pnl / 100.0 if abs(pnl) > 1.0 else pnl

        regime       = t.get("regime", "NEUTRAL") if isinstance(t.get("regime"), str) else "NEUTRAL"
        trade_type   = t.get("trade_type") or "MOMENTUM"
        entry_date   = t.get("entry_date", "")
        hold_days    = t.get("hold_days", 0) or 1

        # Factor scores: prefer ledger metadata (captured at actual entry)
        # Fall back to any factor_scores stored directly in outcome record
        factor_scores = {}
        if sym in ledger_meta and ledger_meta[sym].get("factor_scores"):
            factor_scores = ledger_meta[sym]["factor_scores"]
        elif t.get("factor_scores"):
            factor_scores = t["factor_scores"]

        if not factor_scores:
            continue  # Can't compute IC without factor scores

        observations.append({
            "symbol":        sym,
            "factor_scores": factor_scores,
            "return":        float(pnl),
            "hold_days":     int(hold_days),
            "regime":        regime,
            "trade_type":    trade_type,
            "date":          entry_date,
            "source":        "outcome",
            "weight":        2,
        })

    logger.info("Outcome observations loaded: %d (with factor scores: %d)",
                len(outcomes), len(observations))
    return observations


def load_history_observations() -> List[Dict]:
    """
    Load factor z-scores + forward returns from hold_history.json.
    For each snapshot, the forward return is the actual PnL from
    the snapshot date to position close. Weight = 1 (secondary).

    Only snapshots with non-empty factor_scores are included.
    Forward return = realized PnL of the trade (approximation —
    the actual per-snapshot forward return requires matching snapshot
    date to future price, which we can't do without historical prices.
    We use realized trade return as the best available proxy.)
    """
    if not os.path.exists(HOLD_HIST):
        logger.warning("hold_history.json not found")
        return []

    with open(HOLD_HIST) as f:
        history = json.load(f)

    # Cross-reference realized returns from outcome_log
    realized = {}
    if os.path.exists(OUTCOME_LOG):
        with open(OUTCOME_LOG) as f:
            outcomes = json.load(f)
        for t in outcomes:
            if t.get("actual_pnl_pct") is not None:
                realized[t["symbol"]] = t["actual_pnl_pct"]

    positions = history.get("positions", {})
    observations = []

    for sym, pos_data in positions.items():
        snaps = pos_data.get("snapshots", [])
        ret   = realized.get(sym)
        if ret is None:
            continue  # No realized return to correlate against

        for snap in snaps:
            factor_scores = snap.get("factor_scores", {})
            if not factor_scores or len(factor_scores) == 0:
                continue

            regime     = snap.get("regime", "NEUTRAL")
            if "/" in str(regime):
                regime = str(regime).split("/")[0]

            observations.append({
                "symbol":        sym,
                "factor_scores": factor_scores,
                "return":        float(ret) / 100.0 if abs(float(ret)) > 1.0 else float(ret),
                "hold_days":     snap.get("days_held", 0),
                "regime":        regime,
                "trade_type":    snap.get("trade_type", "UNKNOWN"),
                "date":          snap.get("timestamp", "")[:10],
                "source":        "history",
                "weight":        1,
            })

    logger.info("History observations loaded: %d", len(observations))
    return observations


# ══════════════════════════════════════════════════════════════════════════════
# IC COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def compute_ic(factor_vals: np.ndarray, returns: np.ndarray,
               weights: Optional[np.ndarray] = None) -> Tuple[float, float, int]:
    """
    Spearman rank IC between factor values and forward returns.
    Weighted version: observations with higher weight are duplicated
    (approximation — proper weighted Spearman not in scipy).

    Returns: (IC, t_stat, n_obs)
    IC = Spearman(factor, return)
    t_stat = IC × sqrt(n-2) / sqrt(1-IC²)
    """
    if len(factor_vals) < MIN_OBS_IC:
        return 0.0, 0.0, len(factor_vals)

    # Apply weights by replication (integer weights 1 or 2)
    if weights is not None:
        idx = np.repeat(np.arange(len(factor_vals)), weights.astype(int))
        factor_vals = factor_vals[idx]
        returns     = returns[idx]

    # Remove NaN pairs
    mask = ~(np.isnan(factor_vals) | np.isnan(returns))
    f    = factor_vals[mask]
    r    = returns[mask]
    n    = len(f)

    if n < MIN_OBS_IC:
        return 0.0, 0.0, n

    ic, _ = scipy_stats.spearmanr(f, r)
    ic = float(ic) if not np.isnan(ic) else 0.0

    # t-statistic for IC significance
    if abs(ic) < 1.0 - 1e-10 and n > 2:
        t = ic * np.sqrt(n - 2) / np.sqrt(1 - ic**2 + 1e-10)
    else:
        t = 0.0

    return round(ic, 4), round(float(t), 4), n


def compute_rolling_ic(observations: List[Dict], factor: str,
                       window_days: int = 60) -> List[Dict]:
    """
    Rolling IC computed over a sliding window of observations sorted by date.
    Returns list of {date, ic, n} dicts.
    """
    # Sort by date
    dated = [(o["date"], o) for o in observations if o["date"] and factor in o["factor_scores"]]
    dated.sort(key=lambda x: x[0])

    if len(dated) < MIN_OBS_IC:
        return []

    results = []
    for i in range(MIN_OBS_IC, len(dated) + 1):
        # Use last window_days worth of observations (approximate by count)
        window = [d[1] for d in dated[max(0, i-window_days):i]]
        f_vals = np.array([o["factor_scores"].get(factor, np.nan) for o in window])
        r_vals = np.array([o["return"] for o in window])
        w_vals = np.array([o["weight"] for o in window])
        ic, t, n = compute_ic(f_vals, r_vals, w_vals)
        results.append({
            "date": dated[i-1][0],
            "ic":   ic,
            "t":    t,
            "n":    n,
        })
    return results


def compute_regime_ic(observations: List[Dict], factor: str) -> Dict[str, Dict]:
    """IC breakdown by macro regime."""
    regimes = {}
    for o in observations:
        r = o.get("regime", "UNKNOWN").split("/")[0]
        regimes.setdefault(r, []).append(o)

    result = {}
    for regime, obs in regimes.items():
        f_vals = np.array([o["factor_scores"].get(factor, np.nan) for o in obs])
        r_vals = np.array([o["return"] for o in obs])
        w_vals = np.array([o["weight"] for o in obs])
        ic, t, n = compute_ic(f_vals, r_vals, w_vals)
        result[regime] = {"ic": ic, "t_stat": t, "n": n}
    return result


def compute_book_ic(observations: List[Dict], factor: str) -> Dict[str, Dict]:
    """IC breakdown by trade type (MOMENTUM vs MEAN_REVERSION)."""
    books = {}
    for o in observations:
        b = o.get("trade_type", "UNKNOWN")
        books.setdefault(b, []).append(o)

    result = {}
    for book, obs in books.items():
        f_vals = np.array([o["factor_scores"].get(factor, np.nan) for o in obs])
        r_vals = np.array([o["return"] for o in obs])
        w_vals = np.array([o["weight"] for o in obs])
        ic, t, n = compute_ic(f_vals, r_vals, w_vals)
        result[book] = {"ic": ic, "t_stat": t, "n": n}
    return result


# ══════════════════════════════════════════════════════════════════════════════
# FACTOR COVARIANCE (for orthogonalization input)
# ══════════════════════════════════════════════════════════════════════════════

def compute_factor_covariance(observations: List[Dict]) -> Dict:
    """
    Empirical factor covariance matrix from snapshot data.
    Used by signals.py orthogonalization (Σ⁻¹ weighting).
    Returns: {matrix: [[...]], factors: [...], condition_number: float}
    """
    factors_present = [f for f in ALL_FACTORS
                       if any(f in o["factor_scores"] for o in observations)]

    if len(factors_present) < 2:
        return {"factors": factors_present, "matrix": None,
                "condition_number": None, "n_obs": len(observations)}

    # Build factor matrix — rows=observations, cols=factors
    X = []
    for o in observations:
        row = [o["factor_scores"].get(f, 0.0) for f in factors_present]
        X.append(row)

    X = np.array(X, dtype=float)

    # Covariance matrix (Pearson — factors are already z-scored)
    cov = np.cov(X.T)
    if cov.ndim == 0:
        cov = np.array([[float(cov)]])

    # Condition number (high = near-singular = highly correlated factors)
    try:
        cond = float(np.linalg.cond(cov))
    except Exception:
        cond = None

    # Correlation matrix for display
    with np.errstate(divide='ignore', invalid='ignore'):
        std = np.sqrt(np.diag(cov))
        corr = cov / np.outer(std, std)
        corr = np.nan_to_num(corr)

    return {
        "factors":          factors_present,
        "covariance":       [[round(v, 4) for v in row] for row in cov.tolist()],
        "correlation":      [[round(v, 4) for v in row] for row in corr.tolist()],
        "condition_number": round(cond, 1) if cond else None,
        "n_obs":            len(observations),
        "warning":          "HIGH COLLINEARITY" if (cond and cond > 50) else "OK",
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN REPORT
# ══════════════════════════════════════════════════════════════════════════════

def run_factor_lab() -> Dict:
    """
    Full IC validation pass. Returns report dict and saves to factor_ic_report.json.
    """
    logger.info("=" * 60)
    logger.info("FACTOR IC VALIDATION LAB")
    logger.info("=" * 60)

    # Load data
    outcome_obs = load_outcome_observations()
    history_obs = load_history_observations()
    all_obs     = outcome_obs + history_obs

    total_n = len(all_obs)
    logger.info("Total observations: %d (outcome=%d, history=%d)",
                total_n, len(outcome_obs), len(history_obs))

    if total_n < MIN_OBS_IC:
        logger.warning("Insufficient data for IC computation (need %d, have %d)",
                       MIN_OBS_IC, total_n)
        report = {
            "generated_at":     datetime.now().isoformat(),
            "n_observations":   total_n,
            "status":           "INSUFFICIENT_DATA",
            "min_required":     MIN_OBS_IC,
            "factors":          {},
            "covariance":       {},
        }
        with open(IC_REPORT, "w") as f:
            json.dump(report, f, indent=2)
        return report

    # Factor covariance (for orthogonalization)
    cov_result = compute_factor_covariance(all_obs)

    # Per-factor IC analysis
    factor_results = {}
    for factor in ALL_FACTORS:
        obs_with_factor = [o for o in all_obs if factor in o.get("factor_scores", {})]
        n = len(obs_with_factor)

        if n < 3:
            factor_results[factor] = {
                "book":         "MOMENTUM" if factor in MOMENTUM_FACTORS else "MEAN_REVERSION",
                "n_obs":        n,
                "ic_overall":   None,
                "t_stat":       None,
                "status":       "NO_DATA",
                "verdict":      "INSUFFICIENT_DATA",
            }
            continue

        f_vals = np.array([o["factor_scores"][factor] for o in obs_with_factor])
        r_vals = np.array([o["return"] for o in obs_with_factor])
        w_vals = np.array([o["weight"] for o in obs_with_factor])

        ic, t_stat, n_eff = compute_ic(f_vals, r_vals, w_vals)

        # Rolling IC (30d and 60d windows)
        rolling_30 = compute_rolling_ic(obs_with_factor, factor, window_days=30)
        rolling_60 = compute_rolling_ic(obs_with_factor, factor, window_days=60)

        # IC by regime
        regime_ic = compute_regime_ic(obs_with_factor, factor)

        # IC by book
        book_ic = compute_book_ic(obs_with_factor, factor)

        # IC stability: std of rolling IC (lower = more stable)
        ic_stability = round(float(np.std([r["ic"] for r in rolling_60])), 4) if len(rolling_60) >= 3 else None

        # Verdict
        if n < MIN_OBS_TSTAT:
            verdict = "COLLECTING_DATA"
        elif abs(ic) >= IC_MEANINGFUL and abs(t_stat) >= IC_TSTAT_MIN:
            verdict = "VALID_POSITIVE" if ic > 0 else "VALID_NEGATIVE"
        elif abs(t_stat) < IC_TSTAT_MIN:
            verdict = "NOT_SIGNIFICANT"
        else:
            verdict = "WEAK"

        factor_results[factor] = {
            "book":          "MOMENTUM" if factor in MOMENTUM_FACTORS else "MEAN_REVERSION",
            "n_obs":         n,
            "n_effective":   n_eff,
            "ic_overall":    ic,
            "t_stat":        t_stat,
            "ic_stability":  ic_stability,
            "regime_ic":     regime_ic,
            "book_ic":       book_ic,
            "rolling_30":    rolling_30[-5:] if rolling_30 else [],  # last 5 points
            "rolling_60":    rolling_60[-5:] if rolling_60 else [],
            "verdict":       verdict,
            "status":        "OK" if n >= MIN_OBS_TSTAT else "LOW_DATA",
        }

    report = {
        "generated_at":    datetime.now().isoformat(),
        "n_observations":  total_n,
        "n_outcome":       len(outcome_obs),
        "n_history":       len(history_obs),
        "status":          "OK",
        "factors":         factor_results,
        "covariance":      cov_result,
        "thresholds": {
            "ic_meaningful":  IC_MEANINGFUL,
            "t_stat_min":     IC_TSTAT_MIN,
            "min_obs_tstat":  MIN_OBS_TSTAT,
        },
    }

    with open(IC_REPORT, "w") as f:
        json.dump(report, f, indent=2)

    _print_report(report)
    return report


def _print_report(report: Dict):
    """Print human-readable IC report to console."""
    print("\n" + "=" * 70)
    print("  FACTOR IC VALIDATION REPORT")
    print(f"  Generated: {report['generated_at'][:19]}")
    print(f"  Observations: {report['n_observations']} "
          f"(outcome={report['n_outcome']}, history={report['n_history']})")
    print("=" * 70)

    factors = report.get("factors", {})
    if not factors:
        print("  No factor data available yet.")
        return

    # Group by book
    for book_name, book_factors in [("MOMENTUM", MOMENTUM_FACTORS),
                                     ("MEAN_REVERSION", MR_FACTORS)]:
        print(f"\n  {book_name} BOOK")
        print(f"  {'Factor':<18} {'N':>5} {'IC':>7} {'t-stat':>7} "
              f"{'Stability':>10} {'Verdict'}")
        print("  " + "-" * 65)

        for fn in book_factors:
            r = factors.get(fn, {})
            ic    = r.get("ic_overall")
            t     = r.get("t_stat")
            n     = r.get("n_obs", 0)
            stab  = r.get("ic_stability")
            verd  = r.get("verdict", "NO_DATA")

            ic_str   = f"{ic:+.4f}" if ic is not None else "   N/A"
            t_str    = f"{t:+.3f}" if t is not None else "   N/A"
            stab_str = f"{stab:.4f}" if stab is not None else "      N/A"

            # Flag significant factors
            flag = " ★" if (ic is not None and abs(ic) >= IC_MEANINGFUL
                            and t is not None and abs(t) >= IC_TSTAT_MIN) else ""
            warn = " ⚠" if verd in ("VALID_NEGATIVE", "NOT_SIGNIFICANT") else ""

            print(f"  {fn:<18} {n:>5} {ic_str:>7} {t_str:>7} "
                  f"{stab_str:>10}  {verd}{flag}{warn}")

    # Covariance summary
    cov = report.get("covariance", {})
    if cov:
        cond = cov.get("condition_number")
        warn = cov.get("warning", "")
        print(f"\n  FACTOR COVARIANCE")
        print(f"  Condition number: {cond}  [{warn}]")
        print(f"  Observations: {cov.get('n_obs', 0)}")
        if warn == "HIGH COLLINEARITY":
            print("  ⚠ HIGH COLLINEARITY DETECTED — orthogonalization is critical")

    print("\n  ★ = IC significant (|IC|≥0.05, |t|≥1.5)")
    print("  ⚠ = Factor has negative or weak IC — review before next session")
    print("  COLLECTING_DATA = insufficient observations for conclusion")
    print(f"\n  Report saved: factor_ic_report.json")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    run_factor_lab()
