"""
dsr.py — Deflated Sharpe Ratio
===============================
Implements Bailey & López de Prado (2014), "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality",
Journal of Portfolio Management, 40(5), pp. 94-107.

Why this matters
----------------
A raw Sharpe Ratio computed on a single return series appears to measure
risk-adjusted skill.  In practice, any strategy that was iterated over
multiple parameter sets, signal definitions, or structural fixes will have
a Sharpe that is inflated by selection bias — we observed the best version,
not the average version.

The Deflated Sharpe Ratio (DSR) answers:
    "What is the probability that the observed Sharpe Ratio reflects
     genuine skill rather than luck, given how many trials were run?"

It is the p-value of the hypothesis that SR > SR* (the expected maximum
Sharpe from N_trials independent random strategies with the same length
and distributional properties as the observed strategy).

Mathematical components
-----------------------
1. Observed annualised Sharpe Ratio:
       SR_obs = mean(r) / std(r) * sqrt(T)
   where T = number of non-overlapping periods (trades here, not days).

2. Expected maximum Sharpe from N_trials independent trials
   (Jobson & Korkie correction for non-normality, Bailey & López de Prado eq. 8):
       SR* = ((1 - γ * E[Z]) * Φ^{-1}(1 - 1/N_trials)
              + γ * sqrt(V[Z])) * Φ^{-1}(1 - 1/N_trials))

   Where Z = Euler–Mascheroni adjustment; simplified closed form used here:
       SR*(N_trials, T) ≈ (1 - γ) * Φ^{-1}(1 - 1/N_trials)
                          + γ * sqrt(2 * log(N_trials) - log(log(N_trials)) - log(4π))
   (Bailey & López de Prado 2014, eq. 10 — the EV of the maximum of N_trials
   standard normals, scaled by the precision of our SR estimate)

   In practice for small N_trials we use the exact expected-maximum formula
   via the order-statistic expectation of the standard normal.

3. Variance of the SR estimator (accounting for skewness and excess kurtosis):
       V[SR] = (1/T) * (1 + SR²/2 * (κ - 1) - SR * γ₁)
   where γ₁ = skewness, κ = kurtosis (not excess), T = number of trades.

4. DSR = Φ((SR_obs - SR*) / sqrt(V[SR]))
   This is the probability the true Sharpe exceeds the benchmark SR*.
   DSR > 0.95 → strong evidence of genuine skill at 5% significance.

Raptor-specific application
----------------------------
- Return series = per-trade PnL% from outcome_log.json (IC-valid only)
- T = number of IC-valid trades
- N_trials = number of distinct strategy-altering commits (structural changes,
  not daily pushes) — a conservative lower bound on the search space
- Annualisation: trades are the natural period (not calendar days) since
  the strategy sizes per-trade, not per-day. SR_obs is per-trade SR
  scaled to annual by sqrt(trades_per_year) where trades_per_year is
  estimated from the observed hold_days distribution.

Output
------
{
  "sr_obs":          float  — observed annualised Sharpe
  "sr_star":         float  — expected max SR from N_trials (the benchmark)
  "dsr":             float  — DSR in [0,1] — probability SR > SR*
  "dsr_pct":         float  — DSR * 100 for display
  "verdict":         str    — "STRONG" / "MODERATE" / "WEAK" / "INSUFFICIENT"
  "n_trades":        int    — number of trades in the calculation
  "n_trials":        int    — strategy iterations (search space size)
  "skewness":        float
  "excess_kurtosis": float
  "trades_per_year": float
  "var_sr":          float  — variance of the SR estimator
  "note":            str    — interpretation for the pitch deck
}
"""

import json
import math
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from scipy import stats as scipy_stats

logger = logging.getLogger("raptor.dsr")

OUTCOME_LOG_PATH = Path("outcome_log.json")

# ── Number of strategy-altering iterations (N_trials) ────────────────────────
# Conservative count: each structural fix that changed signal logic, exit math,
# stop placement, or universe definition represents one "trial" in the search
# space. Daily operational commits (git push, log updates) do not count.
# Source: git log of commits tagged fix/feat/critical/correct since inception.
# Updated manually each session that alters strategy math.
# TODO: automate by parsing git log for structural keywords at session end.
#
# Current count (2026-06-10):
#   1. v5.0 initial signal design
#   2. Trail multiplier tier correction (collapsed 1.0x → 2.5x)
#   3. submit_order dead-code fix (execution restored)
#   4. Double-trim guard stale snapshot fix
#   5. EntryAgent deterministic gate (LLM → math)
#   6. Sector neutralization
#   7. MR book suspension
#   8. Factor IC adaptive weights
#   9. Soft z-score shrinkage replacing hard threshold
#  10. Ledoit-Wolf factor covariance
#
# This is a lower bound — overfits in backtest parameter choices not counted.
N_TRIALS_DEFAULT = 10


# ── Data loading ──────────────────────────────────────────────────────────────

def load_ic_valid_returns() -> np.ndarray:
    """
    Load per-trade PnL% from outcome_log.json, IC-valid trades only.
    Excludes: pre_label era, crypto, unknown exit path, null PnL.
    Returns array of returns as decimals (pct / 100).
    """
    if not OUTCOME_LOG_PATH.exists():
        return np.array([])

    try:
        recs = json.loads(OUTCOME_LOG_PATH.read_text())
    except Exception:
        return np.array([])

    ic_valid = [
        r for r in recs
        if r.get("actual_exit_path") not in ("unknown", "pre_label", "crypto")
        and r.get("actual_pnl_pct") is not None
    ]

    if not ic_valid:
        return np.array([])

    return np.array([r["actual_pnl_pct"] / 100.0 for r in ic_valid])


def _estimate_trades_per_year(returns: np.ndarray) -> float:
    """
    Estimate annualisation factor from outcome_log hold_days distribution.
    Falls back to 252 / median_hold_days if direct count unavailable.
    """
    try:
        recs = json.loads(OUTCOME_LOG_PATH.read_text())
        ic_valid = [
            r for r in recs
            if r.get("actual_exit_path") not in ("unknown", "pre_label", "crypto")
            and r.get("hold_days") is not None
        ]
        hold_days = [r["hold_days"] for r in ic_valid if r["hold_days"] and r["hold_days"] > 0]
        if not hold_days:
            return 252.0 / 5.0   # fallback: assume 5-day average hold
        median_hold = float(np.median(hold_days))
        return round(252.0 / max(median_hold, 1.0), 2)
    except Exception:
        return 252.0 / 5.0


# ── SR* (expected maximum SR from N_trials) ───────────────────────────────────

def _expected_max_sr(n_trials: int, t_trades: int, sr_obs: float) -> float:
    """
    Expected maximum Sharpe Ratio from n_trials independent random strategies,
    each estimated on t_trades observations.

    Uses the exact expected maximum of n_trials standard normal order statistics,
    scaled by the standard error of the SR estimator.

    Bailey & López de Prado (2014) eq. 10 — closed form approximation:
        E[max SR | N, T] ≈ μ_N + σ_N * sr_se
    where μ_N = E[max of N standard normals], σ_N = std of that max,
    and sr_se = standard error of per-period SR before annualisation.

    For the expected max of N standard normals we use the Gumbel approximation:
        μ_N ≈ Φ^{-1}(1 - 1/N)     (mode of the Gumbel distribution)
    which is accurate for N >= 5.  For N < 5 we use the exact order-statistic
    expectation via numerical integration.
    """
    if n_trials <= 1:
        return 0.0

    # Standard error of per-trade SR (unadjusted for non-normality here —
    # non-normality adjustment is in V[SR] below)
    sr_se = 1.0 / math.sqrt(max(t_trades, 2))

    if n_trials >= 5:
        # Gumbel mode approximation
        mu_n = scipy_stats.norm.ppf(1.0 - 1.0 / n_trials)
    else:
        # Exact: E[X_(N:N)] via numerical integration for small N
        def _integrand(x):
            return x * scipy_stats.norm.pdf(x) * scipy_stats.norm.cdf(x) ** (n_trials - 1)
        from scipy import integrate
        mu_n, _ = integrate.quad(_integrand, -10, 10)

    sr_star = mu_n * sr_se
    return float(sr_star)


# ── Variance of SR estimator (Mertens 2002 / Lo 2002) ────────────────────────

def _var_sr(returns: np.ndarray, sr_per_trade: float) -> float:
    """
    Variance of the Sharpe Ratio estimator accounting for non-normality.

    Mertens (2002) formula, also used in Bailey & López de Prado (2014):
        V[SR] = (1/T) * (1 + SR²/2 * (κ - 1) - SR * γ₁)
    where:
        T  = number of trades
        κ  = kurtosis (NOT excess; normal = 3)
        γ₁ = skewness (normal = 0)
        SR = per-trade Sharpe (not annualised)

    This variance is larger than the naive 1/T when returns are fat-tailed
    (κ > 3) or negatively skewed (γ₁ < 0), correctly widening the confidence
    interval around our SR estimate.
    """
    t = len(returns)
    if t < 4:
        return 1.0 / max(t, 1)

    skew     = float(scipy_stats.skew(returns))
    kurt     = float(scipy_stats.kurtosis(returns, fisher=False))  # NOT excess (normal=3)
    sr2      = sr_per_trade ** 2

    v = (1.0 / t) * (1.0 + sr2 / 2.0 * (kurt - 1.0) - sr_per_trade * skew)
    return float(max(v, 1e-10))   # floor: prevent sqrt(0)


# ── Main DSR computation ──────────────────────────────────────────────────────

def compute_dsr(
    returns:   Optional[np.ndarray] = None,
    n_trials:  int = N_TRIALS_DEFAULT,
    min_trades: int = 10,
) -> dict:
    """
    Compute the Deflated Sharpe Ratio.

    Parameters
    ----------
    returns   : array of per-trade returns as decimals. If None, loads from
                outcome_log.json automatically.
    n_trials  : number of distinct strategy iterations (search space size).
    min_trades: minimum IC-valid trades required for a meaningful DSR.

    Returns
    -------
    dict with all components — see module docstring.
    """
    if returns is None:
        returns = load_ic_valid_returns()

    n = len(returns)
    if n < min_trades:
        return {
            "sr_obs":          None,
            "sr_star":         None,
            "dsr":             None,
            "dsr_pct":         None,
            "verdict":         "INSUFFICIENT",
            "n_trades":        n,
            "n_trials":        n_trials,
            "skewness":        None,
            "excess_kurtosis": None,
            "trades_per_year": None,
            "var_sr":          None,
            "note": (
                f"Need ≥ {min_trades} IC-valid trades for DSR. "
                f"Have {n}. Gate: 60+ trades unlocks MATH-1/ARCH-1."
            ),
        }

    # ── Per-trade SR (before annualisation) ──────────────────────────────────
    mu      = float(np.mean(returns))
    sigma   = float(np.std(returns, ddof=1))
    if sigma < 1e-10:
        return {"verdict": "INSUFFICIENT", "n_trades": n, "note": "Zero variance in returns."}

    sr_per_trade = mu / sigma

    # ── Annualised SR ─────────────────────────────────────────────────────────
    trades_per_year = _estimate_trades_per_year(returns)
    sr_obs          = sr_per_trade * math.sqrt(trades_per_year)

    # ── Distribution moments ─────────────────────────────────────────────────
    skewness        = float(scipy_stats.skew(returns))
    excess_kurtosis = float(scipy_stats.kurtosis(returns, fisher=True))   # excess (normal=0)

    # ── SR* benchmark ────────────────────────────────────────────────────────
    sr_star_per_trade = _expected_max_sr(n_trials, n, sr_per_trade)
    sr_star           = sr_star_per_trade * math.sqrt(trades_per_year)

    # ── Variance of SR estimator ─────────────────────────────────────────────
    var_sr_val = _var_sr(returns, sr_per_trade)

    # ── DSR = Φ((SR_obs - SR*) / sqrt(V[SR] * trades_per_year)) ─────────────
    # Annualise the standard error consistently with SR_obs
    sr_se_annualised = math.sqrt(var_sr_val * trades_per_year)
    if sr_se_annualised < 1e-10:
        dsr = 0.5
    else:
        z   = (sr_obs - sr_star) / sr_se_annualised
        dsr = float(scipy_stats.norm.cdf(z))

    # ── Verdict ───────────────────────────────────────────────────────────────
    if   dsr >= 0.95:  verdict = "STRONG"
    elif dsr >= 0.80:  verdict = "MODERATE"
    elif dsr >= 0.50:  verdict = "WEAK"
    else:              verdict = "NOISE"

    # ── Interpretation note for pitch deck ───────────────────────────────────
    if verdict == "STRONG":
        note = (
            f"DSR={dsr:.1%} — {dsr:.1%} probability the observed SR ({sr_obs:.2f}) "
            f"reflects genuine alpha above the SR* benchmark ({sr_star:.2f}) after "
            f"correcting for {n_trials} strategy iterations. p < {1-dsr:.3f}."
        )
    elif verdict == "MODERATE":
        note = (
            f"DSR={dsr:.1%} — moderate evidence of skill (SR={sr_obs:.2f} vs SR*={sr_star:.2f}). "
            f"Accumulate more IC-valid trades to strengthen the claim. "
            f"Current n={n}; target n=60+ for MATH-1 gate."
        )
    elif verdict == "WEAK":
        note = (
            f"DSR={dsr:.1%} — SR ({sr_obs:.2f}) is above SR* ({sr_star:.2f}) but "
            f"not distinguishable from luck at the current sample size (n={n}). "
            f"Expected: DSR rises as n grows if skill is genuine."
        )
    else:
        note = (
            f"DSR={dsr:.1%} — observed SR ({sr_obs:.2f}) is not reliably above "
            f"SR* ({sr_star:.2f}). Either n={n} is too small, or returns are driven "
            f"by the {n_trials} strategy iterations rather than genuine alpha."
        )

    return {
        "sr_obs":          round(sr_obs, 4),
        "sr_star":         round(sr_star, 4),
        "dsr":             round(dsr, 6),
        "dsr_pct":         round(dsr * 100, 2),
        "verdict":         verdict,
        "n_trades":        n,
        "n_trials":        n_trials,
        "skewness":        round(skewness, 4),
        "excess_kurtosis": round(excess_kurtosis, 4),
        "trades_per_year": round(trades_per_year, 2),
        "var_sr":          round(var_sr_val, 8),
        "note":            note,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(result: Optional[dict] = None) -> None:
    if result is None:
        result = compute_dsr()

    print("\n══════════════════════════════════════════════════════")
    print("  DEFLATED SHARPE RATIO  (Bailey & López de Prado 2014)")
    print("══════════════════════════════════════════════════════")

    if result.get("verdict") == "INSUFFICIENT":
        print(f"  Status  : {result['verdict']}")
        print(f"  Trades  : {result['n_trades']} (need ≥ 10 for calculation)")
        print(f"  Note    : {result['note']}")
        print("══════════════════════════════════════════════════════\n")
        return

    verdict_icons = {"STRONG": "✓✓", "MODERATE": "✓", "WEAK": "~", "NOISE": "✗"}
    icon = verdict_icons.get(result["verdict"], "?")

    print(f"  Verdict         : {icon} {result['verdict']}")
    print(f"  DSR             : {result['dsr_pct']:.1f}%  (prob SR > SR*)")
    print(f"  Observed SR     : {result['sr_obs']:.3f}  (annualised)")
    print(f"  Benchmark SR*   : {result['sr_star']:.3f}  (expected max from {result['n_trials']} trials)")
    print(f"  Trades          : {result['n_trades']}  (IC-valid only)")
    print(f"  Trades/year est : {result['trades_per_year']:.1f}")
    print(f"  Skewness        : {result['skewness']:+.3f}  (normal=0)")
    print(f"  Excess kurtosis : {result['excess_kurtosis']:+.3f}  (normal=0, fat tails>0)")
    print(f"  V[SR]           : {result['var_sr']:.6f}")
    print(f"\n  Interpretation  : {result['note']}")
    print("══════════════════════════════════════════════════════\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
    parser = argparse.ArgumentParser(description="Raptor Deflated Sharpe Ratio")
    parser.add_argument("--n-trials", type=int, default=N_TRIALS_DEFAULT,
                        help=f"Number of strategy iterations (default: {N_TRIALS_DEFAULT})")
    parser.add_argument("--min-trades", type=int, default=10,
                        help="Minimum IC-valid trades required (default: 10)")
    args = parser.parse_args()
    result = compute_dsr(n_trials=args.n_trials, min_trades=args.min_trades)
    print_report(result)
