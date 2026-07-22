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

Drawdown-constraint rework (2026-06-17, session 7):
  PRIOR BUG: _dd_constrained_f used f_max = dd_tolerance / (sigma * sqrt(T)),
  an ad hoc volatility-scaling heuristic with no probabilistic meaning — it
  does not say what it limits the *probability* of, only a rough magnitude.

  REPLACEMENT: derived from first principles via the continuous (GBM)
  approximation to fractional-Kelly betting. Betting fraction lambda of
  full Kelly makes log-wealth a drifted Brownian motion with
      drift     m = gamma * lambda * (1 - lambda/2)
      variance  v = gamma * lambda^2,      gamma = mu^2 / sigma^2
  The probability that an equity excursion from a peak ever reaches a
  drawdown depth d = -ln(beta) (beta = 1 - dd_tolerance), via the
  exponential martingale and optional stopping, collapses to:

      P(drawdown episode reaches beta) = beta ** ((2 - lambda) / lambda)

  Note mu, sigma, and gamma all cancel — the drawdown PROFILE of
  fractional-Kelly betting depends only on lambda, not on the edge size.
  This lets us invert the formula: given a target tolerance on how deep a
  drawdown is acceptable (beta) and a target probability of ever breaching
  it (p_tol), solve directly for the lambda (fraction of full Kelly) that
  is consistent with that risk budget:

      lambda* = 2 / (1 + ln(p_tol)/ln(beta))

  f_dd_constrained = lambda* * f_star  (f_star = naive empirical mu/sigma^2,
  NOT the Bayesian-shrunk f — the lambda fraction is defined relative to
  full Kelly, so it must scale the full-Kelly point estimate, not an
  already-shrunk one. Mixing the two would double-apply conservatism in an
  unprincipled way and make the resulting number impossible to interpret
  against the derivation above.)

  Verified consequence (worked by hand before being coded — see chat log
  2026-06-17): with dd_tolerance=0.12, p_tol=0.05, lambda* ~ 0.082 — i.e.
  half-Kelly (lambda=0.5) is roughly 6x too aggressive for a hard 12%
  drawdown cap at any reasonable breach tolerance. Half-Kelly alone is
  mathematically inconsistent with a tight drawdown cap; the old Bayesian
  shrinkage toward F_PRIOR was doing the real risk-limiting work by
  accident, and that protection decays as n grows and the prior's grip
  weakens. This rewrite makes the drawdown budget an explicit, persistent
  constraint instead of an artifact of small-sample shrinkage.

  TODO:DERIVE — P_TOL (target probability of ever breaching dd_tolerance)
  is NOT yet derived from data. It is currently a placeholder consistent
  with conventional risk-budget practice (95% confidence / 5% tail,
  mirroring the dsr.py and EVT-tail conventions already used elsewhere in
  this codebase), not a number fit to Raptor's own equity curve. A proper
  derivation requires either (a) an explicit utility/ruin-cost function from
  Steve, or (b) enough live drawdown episodes to calibrate p_tol against
  the actual cost of triggering margin_guard.py / drawdown_reduction_factor.
  Gated at DATA-60 (enough independent positions to estimate sigma on
  paths long enough to observe real excursion behavior, not just
  per-trade return dispersion). Flagged explicitly rather than hidden in a
  default — do not treat P_TOL as settled.

  Fat-tail correction to f* — diagnostic only, NOT in the production path:
  A fourth-order Taylor expansion of E[ln(1+fR)] gives
      f* ~= f_kelly_naive * (1 + s*eta - kappa*eta^2),   eta = mu/sigma
  where s = skewness, kappa = (full, not excess) kurtosis. Positive skew
  raises true Kelly above the naive mu/sigma^2 estimate; fat tails (kappa)
  lower it; they cross at eta* = s/kappa. This is reported alongside the
  diagnostics as `f_star_correction_factor` so Steve can see which way the
  empirical bootstrap *should* lean, but it is NOT used to adjust
  production sizing: at kappa~8-10 the expansion's own convergence
  condition (|fR| < 1) is marginal, the 5th moment barely exists, and
  resampling actual bounded (post-stop) outcomes — which the bootstrap
  already does — sidesteps the truncation problem entirely rather than
  trusting a 4th-order polynomial near its breakdown point.

Research basis:
  - Kelly (1956) — original criterion
  - Thorp (2006) — half-Kelly, bootstrap under parameter uncertainty
  - Grinold & Kahn — Bayesian shrinkage toward prior
  - Vince (1992) — drawdown-constrained Kelly (informal originator of the
    concept; the exact closed-form excursion probability used here is the
    standard first-passage / exponential-martingale result for drifted
    Brownian motion under optional stopping, applied to fractional-Kelly
    log-wealth — see e.g. Browne (1999) "Reaching goals and survival" for
    the general apparatus)
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
MAX_DD       = 0.15    # dd_tolerance: drawdown depth we are budgeting against
P_TOL        = 0.05    # TODO:DERIVE — target probability of ever breaching MAX_DD.
                        # Placeholder at conventional 5% tail; see module docstring.
                        # Gated at DATA-60 for proper derivation from live equity curve.
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
        # SCHEMA/LOGIC FIX 2026-07-01 (Tier 1/2 audit): outcome_tracker.py always
        # writes actual_pnl_pct as a percentage (e.g. 0.4093 means +0.41%, per
        # its own `(exit-entry)/entry*100` computation). The old heuristic
        # "divide by 100 only if abs()>1.0" left near-breakeven trades
        # un-normalized, so e.g. a real +0.41% trade (WFC) or -0.20% trade
        # (CSX, UBER) was fed into the Kelly f*=mu/sigma^2 math as a +41% or
        # -20% return — corrupting mean/variance/skew and f_recommended for
        # every trade under ~1% magnitude. dsr.py already uses the correct
        # unconditional /100.0 normalization for the same field — matched here.
        pnl_norm   = pnl / 100.0
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


def _fat_tail_correction_factor(skewness: float, kurtosis: float, eta: float) -> float:
    """
    DIAGNOSTIC ONLY — see module docstring. Fourth-order correction factor
    to naive Kelly from skew/kurtosis of the per-trade return distribution:

        correction = 1 + s*eta - kappa*eta^2

    s = skewness, kappa = FULL kurtosis (not excess; normal = 3.0),
    eta = per-trade Sharpe (mu/sigma). Crossover where skew and tail risk
    exactly offset is eta* = s/kappa.

    Returned for visibility in diagnostics; NEVER multiplied into a
    production f. Production sizing relies on the bootstrap, which
    captures these moments empirically without the 4th-order truncation
    risk this formula carries at high kurtosis.
    """
    if kurtosis <= 1e-8:
        return 1.0
    return float(1.0 + skewness * eta - kurtosis * (eta ** 2))


def _lambda_for_drawdown_budget(dd_tolerance: float, p_tol: float) -> float:
    """
    Solve beta**((2-lambda)/lambda) = p_tol for lambda, where
    beta = 1 - dd_tolerance.

    lambda is the fraction of FULL Kelly (f*) consistent with: "the
    probability that any single drawdown excursion from a new equity high
    ever reaches depth dd_tolerance is at most p_tol."

    Derivation: see module docstring. Closed form:
        r      = ln(p_tol) / ln(beta)
        lambda = 2 / (r + 1)

    Requires 0 < p_tol < beta < 1 for the result to be economically
    meaningful (p_tol must be a tail probability strictly below the
    unconstrained per-episode breach probability beta itself; p_tol >= beta
    would mean "I'm fine with this happening at least as often as it would
    at full Kelly," which defeats the purpose of a budget). Returns None
    if the inputs are out of that range rather than emitting a number that
    doesn't mean what it claims to mean.
    """
    beta = 1.0 - dd_tolerance
    if not (0.0 < p_tol < beta < 1.0):
        logger.warning(
            "_lambda_for_drawdown_budget: inputs out of range "
            "(dd_tolerance=%.4f -> beta=%.4f, p_tol=%.4f) — "
            "p_tol must be strictly less than beta. Returning None.",
            dd_tolerance, beta, p_tol)
        return None
    r = math.log(p_tol) / math.log(beta)
    lam = 2.0 / (r + 1.0)
    return float(lam)


def _dd_constrained_f(returns: np.ndarray,
                       dd_tolerance: float = MAX_DD,
                       p_tol: float = P_TOL) -> float:
    """
    f_dd = lambda* x f_star

    where lambda* is the fraction of full Kelly consistent with at most
    p_tol probability of any drawdown excursion ever reaching dd_tolerance
    (see _lambda_for_drawdown_budget and module docstring for the full
    first-passage derivation). Scales f_star (naive full-Kelly point
    estimate), not the Bayesian-shrunk f — lambda is defined relative to
    full Kelly, so mixing scales would make the result uninterpretable
    against its own derivation.

    Falls back to f_star itself (no constraint applied, i.e. lambda=1) if
    the lambda solve is out of range — this can only happen if dd_tolerance
    or p_tol are misconfigured (see _lambda_for_drawdown_budget), and
    failing open to "unconstrained" rather than silently clamping to zero
    makes a misconfiguration visible in the output instead of masking it
    as an aggressive-looking number that's actually a math error.
    """
    f_star = _empirical_f(returns)
    lam = _lambda_for_drawdown_budget(dd_tolerance, p_tol)
    if lam is None:
        return float(f_star)
    return float(lam * f_star)


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
    eta       = float(mu / sigma)
    f_correction = _fat_tail_correction_factor(skewness, kurtosis, eta)
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
        "per_trade_sharpe_eta":    round(eta, 4),
        # Diagnostic only — see _fat_tail_correction_factor docstring.
        # Direction (>1 means skew dominates, true Kelly likely above naive
        # f*; <1 means fat tails dominate, true Kelly likely below) is more
        # trustworthy than the magnitude at this kurtosis level.
        "f_star_correction_factor_DIAGNOSTIC_ONLY": round(f_correction, 3),
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
        lam_dd  = _lambda_for_drawdown_budget(MAX_DD, P_TOL)

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
            "dd_budget_lambda":  round(lam_dd, 4) if lam_dd is not None else None,
            "dd_budget_inputs":  {"dd_tolerance": MAX_DD, "p_tol": P_TOL,
                                   "p_tol_status": "TODO:DERIVE — see module docstring"},
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
        "max_dd_tolerance": MAX_DD,
        "p_tol":          P_TOL,
        "p_tol_status":   "TODO:DERIVE — placeholder, see kelly_engine.py module docstring",
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
    print(f"  DD budget: tolerance={report['max_dd_tolerance']*100:.0f}%  "
          f"p_tol={report['p_tol']*100:.0f}% ({report['p_tol_status']})")
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
        print(f"    Per-trade Sharpe (η)={d['per_trade_sharpe_eta']:.3f}  "
              f"Fat-tail f* correction (diagnostic only)="
              f"{d['f_star_correction_factor_DIAGNOSTIC_ONLY']:.3f}x")
        print()
        print(f"    Point estimates:")
        print(f"      Empirical f*:     {r['f_empirical']*100:7.3f}%")
        print(f"      Bayesian f:       {r['f_bayesian']*100:7.3f}%")
        print(f"      Half-Kelly:       {r['f_half_kelly']*100:7.3f}%")
        lam_str = f"{r['dd_budget_lambda']:.4f}" if r['dd_budget_lambda'] is not None else "N/A"
        print(f"      DD-Constrained:   {r['f_dd_constrained']*100:7.3f}%   "
              f"(lambda*={lam_str} of full Kelly)")
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
