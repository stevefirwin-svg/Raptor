"""
kelly_engine.py — Kelly Sizing from Realized Outcomes
=======================================================
Derives correct Kelly fraction from actual trade returns.

Current system maps conviction → Kelly without knowing whether
conviction predicts returns. This destroys accounts when a
high-conviction signal has no real edge.

Correct Kelly requires:
  f* = μ / σ²   (continuous returns form)

Where μ and σ² are estimated from realized outcomes, not
from signal strength.

Operating modes:
  SHADOW:  computes outcome-based Kelly but does NOT override
           current sizing. Logs divergence between current and
           optimal. Activates automatically.
  ACTIVE:  replaces current Kelly when n_trades >= MIN_TRADES_ACTIVE.
           Requires explicit config flag to enable.

Shadow mode runs from day 1. Active mode gated at 100+ clean trades.
This lets you observe how wrong the current sizing is while
accumulating the data needed to fix it.

Research basis:
  - Kelly (1956) — original criterion
  - Thorp (2006) — half-Kelly practical implementation
  - Grinold & Kahn — Bayesian shrinkage toward prior
  - Vince (1992) — optimal f and drawdown relationship
"""

import json, os, logging, math
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger("raptor.kelly_engine")

BASE_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)))
OUTCOME_LOG  = os.path.join(BASE_DIR, "outcome_log.json")
KELLY_FILE   = os.path.join(BASE_DIR, "kelly_estimates.json")

# Shadow → Active threshold
MIN_TRADES_SHADOW  = 10   # start computing estimates
MIN_TRADES_ACTIVE  = 100  # allow override of current sizing (requires config flag too)

# Bayesian prior (Thorp 2006 recommendation for unknown systems)
# Prior centered at 2% (conservative) with weight equivalent to 50 trades
F_PRIOR       = 0.02
N_PRIOR       = 50

# Half-Kelly safety factor (standard practitioner choice)
HALF_KELLY    = 0.50

# Hard caps regardless of Kelly estimate
F_CAP_MOM     = 0.12   # max for momentum trades
F_CAP_MR      = 0.08   # max for mean-reversion trades
F_FLOOR       = 0.01   # minimum (never go to zero)

# Drawdown tolerance for drawdown-constrained Kelly
MAX_DD_TOLERANCE = 0.15  # 15% max portfolio drawdown


def load_outcomes() -> List[Dict]:
    """Load clean outcome records with realized returns."""
    if not os.path.exists(OUTCOME_LOG):
        return []
    with open(OUTCOME_LOG) as f:
        data = json.load(f)
    clean = [t for t in data
             if t.get("actual_pnl_pct") is not None
             and t.get("actual_exit_path") not in [None, ""]]
    return clean


def empirical_kelly(returns: np.ndarray) -> Tuple[float, float, float]:
    """
    Compute empirical Kelly from realized return distribution.

    f* = μ / σ²   (continuous returns approximation)

    Also compute:
      - win rate
      - avg win / avg loss ratio (for discrete Kelly check)
      - Sharpe ratio

    Returns: (f_star, sharpe, win_rate)
    """
    n = len(returns)
    if n < 2:
        return 0.0, 0.0, 0.0

    mu    = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    var   = sigma ** 2

    f_star = mu / var if var > 1e-8 else 0.0

    sharpe   = mu / sigma if sigma > 1e-8 else 0.0
    win_rate = float(np.mean(returns > 0))

    return f_star, sharpe, win_rate


def bayesian_kelly(returns: np.ndarray, f_prior: float = F_PRIOR,
                   n_prior: int = N_PRIOR) -> float:
    """
    Bayesian shrinkage of empirical Kelly toward a conservative prior.

    f_posterior = (N × f* + n_prior × f_prior) / (N + n_prior)

    The prior represents our belief about system edge before we have data.
    n_prior=50 is deliberately heavy early (less than 50 trades means
    we trust the prior almost as much as the data).

    As N grows toward 200+, the prior becomes negligible and we trust
    the empirical distribution.
    """
    f_star, _, _ = empirical_kelly(returns)
    n = len(returns)
    f_posterior = (n * f_star + n_prior * f_prior) / (n + n_prior)
    return float(f_posterior)


def drawdown_constrained_kelly(returns: np.ndarray,
                                dd_tolerance: float = MAX_DD_TOLERANCE) -> float:
    """
    Kelly fraction that limits expected maximum drawdown.

    From Vince (1992): for a sequence of trades, the expected max drawdown
    scales approximately as:
      E[MaxDD] ≈ f × σ × sqrt(T)

    Rearranging for a target max drawdown:
      f_max = dd_tolerance / (σ × sqrt(T_horizon))

    We use T_horizon = 252 (one year of daily exposure).
    """
    if len(returns) < 5:
        return dd_tolerance  # conservative fallback

    sigma = float(np.std(returns, ddof=1))
    if sigma < 1e-8:
        return dd_tolerance

    T_horizon = 252
    f_max_dd  = dd_tolerance / (sigma * math.sqrt(T_horizon))
    return float(f_max_dd)


def per_book_kelly(outcomes: List[Dict]) -> Dict[str, Dict]:
    """
    Compute Kelly separately for MOMENTUM and MEAN_REVERSION books.
    Each book has its own return distribution and optimal sizing.
    """
    books = {
        "MOMENTUM":       [t for t in outcomes if t.get("trade_type") == "MOMENTUM"],
        "MEAN_REVERSION": [t for t in outcomes if t.get("trade_type") == "MEAN_REVERSION"],
        "ALL":            outcomes,
    }

    results = {}
    for book, trades in books.items():
        n = len(trades)
        if n < MIN_TRADES_SHADOW:
            results[book] = {
                "n_trades":     n,
                "status":       "COLLECTING_DATA",
                "mode":         "SHADOW",
                "f_current_avg": None,
                "f_empirical":  None,
                "f_bayesian":   None,
                "f_half_kelly": None,
                "f_dd_constrained": None,
                "f_recommended": None,
                "win_rate":     None,
                "sharpe":       None,
                "mu":           None,
                "sigma":        None,
                "divergence":   None,
                "interpretation": f"Need {MIN_TRADES_SHADOW} trades to estimate. Have {n}.",
            }
            continue

        returns = np.array([t["actual_pnl_pct"] for t in trades], dtype=float)
        mu      = float(np.mean(returns))
        sigma   = float(np.std(returns, ddof=1))

        f_star, sharpe, win_rate = empirical_kelly(returns)
        f_bayes   = bayesian_kelly(returns)
        f_half    = f_bayes * HALF_KELLY
        f_dd      = drawdown_constrained_kelly(returns)
        f_cap     = F_CAP_MOM if book == "MOMENTUM" else (F_CAP_MR if book == "MEAN_REVERSION" else F_CAP_MOM)
        f_rec     = float(np.clip(min(f_half, f_dd), F_FLOOR, f_cap))

        # Average current kelly from metadata
        current_kellys = [t.get("kelly_fraction") or
                          t.get("metadata", {}).get("kelly_fraction")
                          for t in trades]
        current_kellys = [k for k in current_kellys if k is not None]
        f_current = float(np.mean(current_kellys)) if current_kellys else None

        divergence = None
        if f_current is not None and f_rec > 0:
            divergence = round((f_current - f_rec) / f_rec * 100, 1)

        mode = "SHADOW" if n < MIN_TRADES_ACTIVE else "ACTIVE_ELIGIBLE"

        results[book] = {
            "n_trades":          n,
            "status":            "OK",
            "mode":              mode,
            "mu":                round(mu, 4),
            "sigma":             round(sigma, 4),
            "win_rate":          round(win_rate, 3),
            "sharpe":            round(sharpe, 3),
            "f_empirical":       round(f_star, 4),
            "f_bayesian":        round(f_bayes, 4),
            "f_half_kelly":      round(f_half, 4),
            "f_dd_constrained":  round(f_dd, 4),
            "f_recommended":     round(f_rec, 4),
            "f_current_avg":     round(f_current, 4) if f_current else None,
            "divergence_pct":    divergence,
            "cap_applied":       f_cap,
            "interpretation":    _interpret(f_rec, f_current, n, mode),
        }

    return results


def _interpret(f_rec: float, f_current: Optional[float],
               n: int, mode: str) -> str:
    """Human-readable interpretation of Kelly estimate."""
    if mode == "COLLECTING_DATA":
        return f"Need {MIN_TRADES_SHADOW} trades to estimate. Have {n}."

    parts = [f"Recommended: {f_rec*100:.2f}% per trade."]

    if f_current is not None:
        diff = f_current - f_rec
        if abs(diff) < 0.005:
            parts.append("Current sizing approximately correct.")
        elif diff > 0:
            parts.append(f"OVERSIZED by {diff*100:.2f}pp — current {f_current*100:.2f}% vs optimal {f_rec*100:.2f}%.")
        else:
            parts.append(f"UNDERSIZED by {abs(diff)*100:.2f}pp — leaving edge on table.")

    if mode == "ACTIVE_ELIGIBLE":
        parts.append(f"ACTIVE MODE ELIGIBLE ({n} trades ≥ {MIN_TRADES_ACTIVE}).")
        parts.append("Enable in config to override current Kelly.")
    else:
        parts.append(f"SHADOW MODE ({n}/{MIN_TRADES_ACTIVE} trades for active).")

    return " ".join(parts)


def run_kelly_engine() -> Dict:
    """Full Kelly analysis. Saves to kelly_estimates.json."""
    outcomes = load_outcomes()
    n = len(outcomes)
    logger.info("Kelly Engine: %d clean outcome records", n)

    book_results = per_book_kelly(outcomes)

    report = {
        "generated_at":   datetime.now().isoformat(),
        "n_total":        n,
        "min_for_shadow": MIN_TRADES_SHADOW,
        "min_for_active": MIN_TRADES_ACTIVE,
        "half_kelly":     HALF_KELLY,
        "f_prior":        F_PRIOR,
        "n_prior":        N_PRIOR,
        "books":          book_results,
    }

    with open(KELLY_FILE, "w") as f:
        json.dump(report, f, indent=2)

    _print_report(report)
    return report


def _print_report(report: Dict):
    print("\n" + "=" * 65)
    print("  KELLY ENGINE — OUTCOME-DERIVED POSITION SIZING")
    print(f"  Generated: {report['generated_at'][:19]}")
    print(f"  Total clean outcomes: {report['n_total']}")
    print("=" * 65)

    for book, r in report["books"].items():
        if book == "ALL" and len(report["books"]) > 1:
            print()
        print(f"\n  [{book}]")
        if r["status"] == "COLLECTING_DATA":
            print(f"    {r['interpretation']}")
            continue

        print(f"    Trades: {r['n_trades']}  Win rate: {r['win_rate']*100:.1f}%  "
              f"Sharpe: {r['sharpe']:.3f}")
        print(f"    μ={r['mu']*100:.3f}%  σ={r['sigma']*100:.3f}%")
        print(f"    Empirical Kelly:    {r['f_empirical']*100:.3f}%")
        print(f"    Bayesian Kelly:     {r['f_bayesian']*100:.3f}%")
        print(f"    Half-Kelly:         {r['f_half_kelly']*100:.3f}%")
        print(f"    DD-Constrained:     {r['f_dd_constrained']*100:.3f}%")
        print(f"    ➤ Recommended:      {r['f_recommended']*100:.3f}%  "
              f"[cap={r['cap_applied']*100:.0f}%]")
        if r["f_current_avg"]:
            print(f"    Current avg Kelly:  {r['f_current_avg']*100:.3f}%  "
                  f"(divergence: {r['divergence_pct']:+.1f}%)")
        print(f"    Mode: {r['mode']}")
        print(f"    → {r['interpretation']}")

    print("\n  Shadow mode: Kelly computed but current sizing unchanged.")
    print(f"  Active mode eligible at {report['min_for_active']} trades.")
    print(f"  Report saved: kelly_estimates.json")
    print("=" * 65 + "\n")


def get_recommended_kelly(trade_type: str = "MOMENTUM") -> Optional[float]:
    """
    Read recommended Kelly from saved estimates.
    Called by signals.py when ACTIVE mode enabled.
    Returns None in SHADOW mode (don't override current sizing).
    """
    if not os.path.exists(KELLY_FILE):
        return None
    try:
        with open(KELLY_FILE) as f:
            report = json.load(f)
        book = report["books"].get(trade_type, {})
        if book.get("mode") == "ACTIVE_ELIGIBLE" and book.get("f_recommended"):
            return float(book["f_recommended"])
    except Exception:
        pass
    return None


if __name__ == "__main__":
    run_kelly_engine()
