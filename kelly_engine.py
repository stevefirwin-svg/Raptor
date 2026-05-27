"""
kelly_engine.py — Kelly Sizing from Realized Outcomes
=======================================================
Derives correct Kelly fraction from actual trade returns.

Operating modes:
  SHADOW:  computes outcome-based Kelly but does NOT override
           current sizing. Logs divergence. Activates automatically.
  ACTIVE:  replaces current Kelly when n_trades >= MIN_TRADES_ACTIVE.
           Requires explicit config flag to enable.

Bootstrap Kelly (2026-05-24):
  The μ/σ² formula assumes iid normal returns.
  Actual data: kurtosis ~10, skewness ~2.8 (right-skewed from big winners).
  Fix: bootstrap the FULL recommended-f pipeline (not raw f*) to get a
  proper confidence interval. Use P25 of bootstrapped final-f as the
  conservative production estimate.

  Bootstrapping raw f* alone is misleading for right-skewed distributions —
  P25 of raw f* is dominated by the tail structure, not parameter uncertainty.
  Bootstrapping the full pipeline (Bayesian + half-Kelly + DD constraint)
  gives a stable, meaningful confidence interval on what we'd actually use.

  With exponential decay weighting, recent trades are sampled more often,
  so the estimate reflects the current regime rather than pooled history.

Research basis:
  - Kelly (1956) — original criterion
  - Thorp (2006) — half-Kelly, bootstrap under parameter uncertainty
  - Grinold & Kahn — Bayesian shrinkage toward prior
  - Vince (1992) — drawdown-constrained Kelly
  - Asness, Moskowitz & Pedersen (2013) — exponential decay half-life
"""

import json, os, logging, math
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger("raptor.kelly_engine")

BASE_DIR    = Path(__file__).parent
OUTCOME_LOG = BASE_DIR / "outcome_log.json"
KELLY_FILE  = BASE_DIR / "kelly_estimates.json"

MIN_TRADES_SHADOW = 10
MIN_TRADES_ACTIVE = 100

F_PRIOR      = 0.02    # conservative prior: 2% per trade
N_PRIOR      = 50      # prior weight (reduce to 20 when 60+ tagged trades)
HALF_KELLY   = 0.50
F_CAP_MOM    = 0.12
F_CAP_MR     = 0.08
F_FLOOR      = 0.01
MAX_DD       = 0.15    # 15% max portfolio drawdown tolerance
DECAY_LAMBDA = 0.005   # half-life ≈ 139 days (same as AdaptiveWeights)
N_BOOTSTRAP  = 10_000  # bootstrap resamples


# ── Atomic write ──────────────────────────────────────────────────────────────
def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


# ── Data loading ──────────────────────────────────────────────────────────────
def load_outcomes() -> List[Dict]:
    """
    Load clean equity outcome records.
    - Crypto excluded (contain '/')
    - Zero hold_days excluded (fill artifacts)
    - pnl_pct normalized to decimal fraction
    - Pre-v5.5 records assigned trade_type=MOMENTUM
    """
    if not OUTCOME_LOG.exists():
        return []
    data = json.loads(OUTCOME_LOG.read_text())

    clean = []
    for t in data:
        pnl = t.get("actual_pnl_pct")
        if pnl is None:
            continue
        if "/" in t.get("symbol", ""):
            continue
        if (t.get("hold_days") or 0) == 0:
            continue
        # Exclude math_trim (partial exits) — pnl_pct is mark-at-trim, not terminal return.
        # Including partials biases Kelly toward trim-timing accuracy, not full-trade E[R].
        # Only terminal exits (trailing_stop, math_exit, hard_stop, thesis_invalid,
        # time_decay) represent the full realized outcome of a trade.
        if t.get("actual_exit_path") == "math_trim":
            continue
        pnl_norm   = pnl / 100.0 if abs(pnl) > 1.0 else pnl
        trade_type = t.get("trade_type") or "MOMENTUM"
        clean.append({**t, "actual_pnl_pct": pnl_norm, "trade_type": trade_type})

    return clean


# ── Decay weights ─────────────────────────────────────────────────────────────
def _decay_weights(outcomes: List[Dict]) -> np.ndarray:
    """
    Exponential decay weight per trade — same λ as AdaptiveWeights.
    Recent trades weighted higher. Used as bootstrap sampling probabilities.
    """
    today = date.today()
    weights = []
    for t in outcomes:
        ts = t.get("entry_date") or t.get("tagged_at") or ""
        try:
            days_ago = (today - date.fromisoformat(ts[:10])).days
        except Exception:
            days_ago = 365
        weights.append(math.exp(-DECAY_LAMBDA * days_ago))
    arr = np.array(weights, dtype=float)
    return arr / arr.sum() if arr.sum() > 1e-10 else np.ones(len(arr)) / len(arr)


# ── Core Kelly formulas ───────────────────────────────────────────────────────
def _empirical_f(returns: np.ndarray) -> float:
    """f* = μ / σ²  (continuous returns form)."""
    mu  = returns.mean()
    var = returns.var(ddof=1)
    return float(mu / var) if var > 1e-8 else 0.0


def _bayesian_f(returns: np.ndarray, f_prior: float = F_PRIOR,
                n_prior: int = N_PRIOR) -> float:
    """Bayesian shrinkage: f_post = (N×f* + n_prior×f_prior) / (N + n_prior)."""
    f_star = _empirical_f(returns)
    n      = len(returns)
    return float((n * f_star + n_prior * f_prior) / (n + n_prior))


def _dd_constrained_f(returns: np.ndarray,
                       dd_tolerance: float = MAX_DD) -> float:
    """
    f_max = dd_tolerance / (σ × √T_horizon)
    Limits expected max drawdown to dd_tolerance over T_horizon=252 days.
    Reference: Vince (1992).
    """
    sigma = float(np.std(returns, ddof=1))
    if sigma < 1e-8:
        return dd_tolerance
    return float(dd_tolerance / (sigma * math.sqrt(252)))


def _recommended_f(returns: np.ndarray, f_cap: float) -> float:
    """Full pipeline: empirical → Bayesian → half-Kelly → DD constraint → clip."""
    f_bayes = _bayesian_f(returns)
    f_half  = f_bayes * HALF_KELLY
    f_dd    = _dd_constrained_f(returns)
    return float(np.clip(min(f_half, f_dd), F_FLOOR, f_cap))


# ── Bootstrap Kelly ───────────────────────────────────────────────────────────
def bootstrap_kelly(returns: np.ndarray,
                    decay_probs: np.ndarray,
                    f_cap: float,
                    n_boot: int = N_BOOTSTRAP,
                    seed: int = 42) -> Dict:
    """
    Bootstrap confidence interval on the FINAL recommended f.

    Approach:
      For each resample, run the full pipeline:
        empirical f* → Bayesian shrinkage → half-Kelly → DD constraint → clip

      This captures parameter uncertainty in f_recommended, not just in f*.
      Bootstrapping raw f* alone is misleading for right-skewed return
      distributions (big winners inflate μ and make f* wildly unstable).

    Decay-weighted sampling: recent trades sampled more frequently,
    reflecting current regime rather than pooled history.

    Returns percentiles of the bootstrapped f_recommended distribution:
      p10 — conservative estimate (recommended for production)
      p25 — moderately conservative
      p50 — central estimate
      p75 — optimistic
      std — stability measure (high std = more data needed)

    Reference: Thorp (2006) — bootstrap Kelly under parameter uncertainty.
    """
    n   = len(returns)
    rng = np.random.default_rng(seed)

    boot_f_rec  = np.empty(n_boot)
    boot_f_star = np.empty(n_boot)

    for i in range(n_boot):
        idx    = rng.choice(n, size=n, replace=True, p=decay_probs)
        sample = returns[idx]

        mu_s  = sample.mean()
        var_s = sample.var(ddof=1) if n > 1 else 1e-8
        boot_f_star[i] = mu_s / var_s if var_s > 1e-8 else 0.0

        boot_f_rec[i] = _recommended_f(sample, f_cap)

    return {
        # Full-pipeline bootstrap (what we actually use)
        "f_rec_p10":    float(np.percentile(boot_f_rec, 10)),
        "f_rec_p25":    float(np.percentile(boot_f_rec, 25)),
        "f_rec_p50":    float(np.percentile(boot_f_rec, 50)),
        "f_rec_p75":    float(np.percentile(boot_f_rec, 75)),
        "f_rec_std":    float(boot_f_rec.std()),
        # Raw f* bootstrap (diagnostic — shows distribution instability)
        "f_star_p10":   float(np.percentile(boot_f_star, 10)),
        "f_star_p25":   float(np.percentile(boot_f_star, 25)),
        "f_star_p50":   float(np.percentile(boot_f_star, 50)),
        "f_star_pct_negative": float((boot_f_star < 0).mean() * 100),
        "n_boot":       n_boot,
    }


# ── Distribution diagnostics ──────────────────────────────────────────────────
def return_diagnostics(returns: np.ndarray) -> Dict:
    """Skewness, kurtosis, normality test. Justifies why bootstrap is needed."""
    n     = len(returns)
    mu    = returns.mean()
    sigma = returns.std(ddof=1)
    if sigma < 1e-8:
        return {}
    z         = (returns - mu) / sigma
    skewness  = float(np.mean(z ** 3))
    kurtosis  = float(np.mean(z ** 4))   # excess kurtosis = kurtosis - 3
    win_rate  = float((returns > 0).mean())
    avg_win   = float(returns[returns > 0].mean()) if (returns > 0).any() else 0.0
    avg_loss  = float(returns[returns <= 0].mean()) if (returns <= 0).any() else 0.0
    return {
        "n":         n,
        "mu_pct":    round(mu * 100, 3),
        "sigma_pct": round(sigma * 100, 3),
        "skewness":  round(skewness, 2),
        "kurtosis":  round(kurtosis, 2),   # normal = 3.0
        "win_rate":  round(win_rate, 3),
        "avg_win_pct":  round(avg_win * 100, 3),
        "avg_loss_pct": round(avg_loss * 100, 3),
        "normal_assumption_valid": kurtosis < 5.0 and abs(skewness) < 1.0,
    }


# ── Per-book analysis ─────────────────────────────────────────────────────────
def per_book_kelly(outcomes: List[Dict]) -> Dict:
    books = {
        "MOMENTUM":       [t for t in outcomes if t.get("trade_type") == "MOMENTUM"],
        "MEAN_REVERSION": [t for t in outcomes if t.get("trade_type") == "MEAN_REVERSION"],
    }

    results = {}
    for book, trades in books.items():
        n     = len(trades)
        f_cap = F_CAP_MOM if book == "MOMENTUM" else F_CAP_MR

        if n < MIN_TRADES_SHADOW:
            results[book] = {
                "n_trades":        n,
                "status":          "COLLECTING_DATA",
                "mode":            "SHADOW",
                "f_recommended":   None,
                "f_bootstrap_p25": None,
                "f_bootstrap_p10": None,
                "interpretation":  f"Need {MIN_TRADES_SHADOW} trades. Have {n}.",
            }
            continue

        returns      = np.array([t["actual_pnl_pct"] for t in trades], dtype=float)
        decay_probs  = _decay_weights(trades)
        diag         = return_diagnostics(returns)

        # Point estimates
        f_star  = _empirical_f(returns)
        f_bayes = _bayesian_f(returns)
        f_half  = f_bayes * HALF_KELLY
        f_dd    = _dd_constrained_f(returns)
        f_point = float(np.clip(min(f_half, f_dd), F_FLOOR, f_cap))

        # Bootstrap confidence interval on final recommended f
        boot    = bootstrap_kelly(returns, decay_probs, f_cap)

        # Production estimate: P25 of bootstrapped final f
        # Conservative but not extreme — accounts for parameter uncertainty
        # while respecting that our distribution is right-skewed (big winners)
        f_production = boot["f_rec_p25"]

        # Current average Kelly from trade metadata
        current_ks = [t.get("kelly_fraction") or
                      t.get("metadata", {}).get("kelly_fraction")
                      for t in trades]
        current_ks = [k for k in current_ks if k is not None]
        f_current  = float(np.mean(current_ks)) if current_ks else None

        mode = "SHADOW" if n < MIN_TRADES_ACTIVE else "ACTIVE_ELIGIBLE"

        results[book] = {
            "n_trades":          n,
            "status":            "OK",
            "mode":              mode,
            "diagnostics":       diag,
            # Point estimates
            "f_empirical":       round(f_star,  4),
            "f_bayesian":        round(f_bayes, 4),
            "f_half_kelly":      round(f_half,  4),
            "f_dd_constrained":  round(f_dd,    4),
            "f_point":           round(f_point, 4),
            # Bootstrap CI
            "bootstrap":         {k: round(v, 4) if isinstance(v, float) else v
                                  for k, v in boot.items()},
            # Production recommendation
            "f_recommended":     round(f_production, 4),   # P25 of bootstrap
            "f_recommended_basis": "bootstrap_p25_final_f",
            "f_current_avg":     round(f_current, 4) if f_current else None,
            "cap_applied":       f_cap,
            "interpretation":    _interpret(f_production, f_point, f_current, n, mode, boot),
        }

    return results


# ── Interpretation ────────────────────────────────────────────────────────────
def _interpret(f_prod: float, f_point: float, f_current: Optional[float],
               n: int, mode: str, boot: Dict) -> str:
    parts = [f"Recommended (bootstrap P25): {f_prod*100:.2f}% per trade."]
    parts.append(f"Point estimate: {f_point*100:.2f}%.  "
                 f"Bootstrap range: {boot['f_rec_p10']*100:.2f}%–{boot['f_rec_p75']*100:.2f}%.")

    if boot["f_star_pct_negative"] > 5:
        parts.append(f"WARNING: {boot['f_star_pct_negative']:.0f}% of raw f* resamples "
                     f"are negative — distribution is unstable, bootstrap protection active.")

    if f_current is not None:
        diff = f_current - f_prod
        if abs(diff) < 0.005:
            parts.append("Current sizing approximately correct.")
        elif diff > 0:
            parts.append(f"OVERSIZED by {diff*100:.2f}pp vs bootstrap estimate.")
        else:
            parts.append(f"UNDERSIZED by {abs(diff)*100:.2f}pp vs bootstrap estimate.")

    if mode == "ACTIVE_ELIGIBLE":
        parts.append(f"ACTIVE MODE ELIGIBLE ({n} >= {MIN_TRADES_ACTIVE} trades).")
    else:
        parts.append(f"SHADOW MODE ({n}/{MIN_TRADES_ACTIVE} trades for active).")

    return " ".join(parts)


# ── Main runner ───────────────────────────────────────────────────────────────
def run_kelly_engine() -> Dict:
    outcomes = load_outcomes()
    n        = len(outcomes)
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
        "decay_lambda":   DECAY_LAMBDA,
        "n_bootstrap":    N_BOOTSTRAP,
        "books":          book_results,
    }

    _atomic_write(KELLY_FILE, report)
    _print_report(report)
    return report


# ── Report printer ────────────────────────────────────────────────────────────
def _print_report(report: Dict) -> None:
    print("\n" + "=" * 65)
    print("  KELLY ENGINE — BOOTSTRAP OUTCOME-DERIVED SIZING")
    print(f"  Generated: {report['generated_at'][:19]}")
    print(f"  Clean outcomes: {report['n_total']}  |  "
          f"Bootstrap resamples: {report['n_bootstrap']:,}")
    print("=" * 65)

    for book, r in report["books"].items():
        print(f"\n  [{book}]")
        if r["status"] == "COLLECTING_DATA":
            print(f"    {r['interpretation']}")
            continue

        d    = r["diagnostics"]
        boot = r["bootstrap"]

        print(f"    Trades: {r['n_trades']}  "
              f"Win rate: {d['win_rate']*100:.1f}%  "
              f"Avg win: {d['avg_win_pct']:+.2f}%  "
              f"Avg loss: {d['avg_loss_pct']:+.2f}%")
        print(f"    μ={d['mu_pct']:+.3f}%  σ={d['sigma_pct']:.3f}%  "
              f"Skew={d['skewness']:+.2f}  Kurt={d['kurtosis']:.2f}  "
              f"(normal={d['normal_assumption_valid']})")
        print()
        print(f"    Point estimates:")
        print(f"      Empirical f*:     {r['f_empirical']*100:7.3f}%")
        print(f"      Bayesian f:       {r['f_bayesian']*100:7.3f}%")
        print(f"      Half-Kelly:       {r['f_half_kelly']*100:7.3f}%")
        print(f"      DD-Constrained:   {r['f_dd_constrained']*100:7.3f}%")
        print(f"      Point f_rec:      {r['f_point']*100:7.3f}%")
        print()
        print(f"    Bootstrap CI (decay-weighted, {boot['n_boot']:,} resamples):")
        print(f"      Raw f* P10/P50/P75: "
              f"{boot['f_star_p10']*100:.2f}% / "
              f"{boot['f_star_p50']*100:.2f}% / "
              f"  (f*<0: {boot['f_star_pct_negative']:.1f}%)")
        print(f"      Final f  P10:     {boot['f_rec_p10']*100:7.3f}%  ← conservative")
        print(f"      Final f  P25:     {boot['f_rec_p25']*100:7.3f}%  ← PRODUCTION")
        print(f"      Final f  P50:     {boot['f_rec_p50']*100:7.3f}%  ← central")
        print(f"      Final f  P75:     {boot['f_rec_p75']*100:7.3f}%  ← optimistic")
        print(f"      Std dev:          {boot['f_rec_std']*100:7.3f}%  "
              f"({'stable' if boot['f_rec_std'] < 0.01 else 'uncertain — need more data'})")
        print()
        print(f"    ➤ Recommended: {r['f_recommended']*100:.3f}%  "
              f"[basis: bootstrap P25 | cap: {r['cap_applied']*100:.0f}%]")
        if r["f_current_avg"]:
            print(f"    Current avg:   {r['f_current_avg']*100:.3f}%")
        print(f"    Mode: {r['mode']}")
        print(f"    → {r['interpretation']}")

    print("\n  Shadow mode: Kelly computed, current sizing unchanged.")
    print(f"  Active mode at {report['min_for_active']} trades.")
    print("=" * 65 + "\n")


# ── API for signals.py ────────────────────────────────────────────────────────
def get_recommended_kelly(trade_type: str = "MOMENTUM") -> Optional[float]:
    """
    Read recommended Kelly from saved estimates.
    Returns None in SHADOW mode — don't override current sizing.
    """
    if not KELLY_FILE.exists():
        return None
    try:
        report = json.loads(KELLY_FILE.read_text())
        book   = report["books"].get(trade_type, {})
        if book.get("mode") == "ACTIVE_ELIGIBLE" and book.get("f_recommended"):
            return float(book["f_recommended"])
    except Exception:
        pass
    return None


if __name__ == "__main__":
    run_kelly_engine()
