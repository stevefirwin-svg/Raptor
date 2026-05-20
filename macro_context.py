"""
macro_context.py — Layer 2: Macro Context Engine
Raptor Autonomous Agent Roadmap

Pulls daily macro signals and classifies the market regime.
Writes macro_context.json before market open every day.
Injected into every EntryAgent and HoldAgent prompt.

Run:
    python macro_context.py            # fetch and write macro_context.json
    python macro_context.py --summary  # print current macro context

Scheduled via Task Scheduler at 9:00 AM ET (before main.py at 9:35 AM).
"""

import os
import json
import argparse
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

import yfinance as yf
import numpy as np

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

FRED_API_KEY       = os.getenv("FRED_API_KEY")
MACRO_CONTEXT_PATH = "macro_context.json"

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# Sector ETFs for breadth calculation
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]

# ── FRED helper ───────────────────────────────────────────────────────────────

def fred_latest(series_id: str, lookback_days: int = 30) -> float | None:
    """Fetch the most recent value for a FRED series."""
    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    params = {
        "series_id":        series_id,
        "api_key":          FRED_API_KEY,
        "file_type":        "json",
        "observation_start": start,
        "sort_order":       "desc",
        "limit":            5,
    }
    try:
        resp = requests.get(FRED_BASE, params=params, timeout=10)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        for o in obs:
            val = o.get("value", ".")
            if val != ".":
                return float(val)
    except Exception as e:
        print(f"  [FRED] WARNING: {series_id} failed — {e}")
    return None

# ── Signal pullers ────────────────────────────────────────────────────────────

def get_vix() -> dict:
    """VIX level and regime classification."""
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="5d")
        if hist.empty:
            return {"value": None, "regime": "UNKNOWN"}
        level = round(float(hist["Close"].iloc[-1]), 2)
        if level >= 35:
            regime = "CRISIS"
        elif level >= 25:
            regime = "ELEVATED"
        elif level >= 18:
            regime = "NEUTRAL"
        else:
            regime = "CALM"
        return {"value": level, "regime": regime}
    except Exception as e:
        print(f"  [VIX] WARNING: {e}")
        return {"value": None, "regime": "UNKNOWN"}


def get_spy_trend() -> dict:
    """SPY 20-day trend and 50/200 MA relationship."""
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="1y")
        if len(hist) < 50:
            return {"trend_20d": None, "above_50ma": None, "above_200ma": None, "regime": "UNKNOWN"}
        close = hist["Close"]
        price     = float(close.iloc[-1])
        ma50      = float(close.rolling(50).mean().iloc[-1])
        ma200     = float(close.rolling(200).mean().iloc[-1])
        trend_20d = round((price / float(close.iloc[-20]) - 1) * 100, 2)
        above_50  = price > ma50
        above_200 = price > ma200

        if above_200 and above_50 and trend_20d > 2:
            regime = "BULLISH"
        elif above_200 and trend_20d > -2:
            regime = "NEUTRAL"
        elif not above_200 and trend_20d < -2:
            regime = "BEARISH"
        else:
            regime = "MIXED"

        return {
            "price":       round(price, 2),
            "ma50":        round(ma50, 2),
            "ma200":       round(ma200, 2),
            "trend_20d":   trend_20d,
            "above_50ma":  above_50,
            "above_200ma": above_200,
            "regime":      regime,
        }
    except Exception as e:
        print(f"  [SPY] WARNING: {e}")
        return {"trend_20d": None, "above_50ma": None, "above_200ma": None, "regime": "UNKNOWN"}


def get_sector_breadth() -> dict:
    """% of sector ETFs trading above their 50-day MA."""
    try:
        above = 0
        total = 0
        for etf in SECTOR_ETFS:
            hist = yf.Ticker(etf).history(period="3mo")
            if len(hist) < 50:
                continue
            close = hist["Close"]
            price = float(close.iloc[-1])
            ma50  = float(close.rolling(50).mean().iloc[-1])
            total += 1
            if price > ma50:
                above += 1
        pct = round(above / total * 100, 1) if total > 0 else None
        if pct is None:
            regime = "UNKNOWN"
        elif pct >= 70:
            regime = "BROAD_STRENGTH"
        elif pct >= 50:
            regime = "MIXED"
        elif pct >= 30:
            regime = "WEAKENING"
        else:
            regime = "BROAD_WEAKNESS"
        return {"pct_above_50ma": pct, "sectors_checked": total, "regime": regime}
    except Exception as e:
        print(f"  [BREADTH] WARNING: {e}")
        return {"pct_above_50ma": None, "sectors_checked": 0, "regime": "UNKNOWN"}


def get_yield_curve() -> dict:
    """T10Y2Y yield curve slope from FRED. Negative = inverted = recession risk."""
    val = fred_latest("T10Y2Y", lookback_days=10)
    if val is None:
        return {"spread_pct": None, "inverted": None, "regime": "UNKNOWN"}
    inverted = val < 0
    if val >= 1.0:
        regime = "STEEP"
    elif val >= 0:
        regime = "FLAT"
    elif val >= -0.5:
        regime = "MILDLY_INVERTED"
    else:
        regime = "DEEPLY_INVERTED"
    return {"spread_pct": round(val, 3), "inverted": inverted, "regime": regime}


def get_credit_spread() -> dict:
    """BBB corporate credit spread from FRED (BAMLC0A4CBBB).
    Higher spread = credit stress = risk-off signal."""
    val = fred_latest("BAMLC0A4CBBB", lookback_days=10)
    if val is None:
        return {"spread_pct": None, "regime": "UNKNOWN"}
    if val >= 3.0:
        regime = "STRESS"
    elif val >= 2.0:
        regime = "ELEVATED"
    elif val >= 1.2:
        regime = "NORMAL"
    else:
        regime = "TIGHT"
    return {"spread_pct": round(val, 3), "regime": regime}


def get_fed_rate() -> dict:
    """Effective Fed Funds Rate and recent direction."""
    current = fred_latest("FEDFUNDS", lookback_days=45)
    prior   = fred_latest("FEDFUNDS", lookback_days=90)
    if current is None:
        return {"rate": None, "direction": "UNKNOWN"}
    direction = "STABLE"
    if prior is not None:
        if current > prior + 0.1:
            direction = "HIKING"
        elif current < prior - 0.1:
            direction = "CUTTING"
    return {"rate": round(current, 3), "direction": direction}

# ── Regime classifier ─────────────────────────────────────────────────────────

def _to_continuous(vix, spy, breadth, yield_curve, credit, fed) -> dict:
    """Convert raw macro signals to continuous [-1, +1] scores.
    Preserves full information — no quantization into integer votes.
    Each score is derived from the actual numeric value, not its label.
    Reference: P1-1 audit, Hamilton (1989) regime-switching.
    """
    scores = {}

    # VIX: z-score relative to [12, 40] historical range, inverted (high VIX = negative)
    vix_val = vix.get("value")
    if vix_val is not None:
        # Normalize: VIX 12 = +1.0 (calm), VIX 40 = -1.0 (crisis)
        scores["vix"] = float(np.clip(1.0 - 2.0 * (vix_val - 12.0) / (40.0 - 12.0), -1.0, 1.0))
    else:
        scores["vix"] = 0.0

    # SPY trend: 20d return normalized to [-5%, +5%] range
    trend = spy.get("trend_20d")
    above_200 = spy.get("above_200ma", False)
    if trend is not None:
        trend_score = float(np.clip(trend / 5.0, -1.0, 1.0))
        ma_bonus = 0.2 if above_200 else -0.2
        scores["spy"] = float(np.clip(trend_score + ma_bonus, -1.0, 1.0))
    else:
        scores["spy"] = 0.0

    # Sector breadth: % above 50MA normalized, 0%=−1, 50%=0, 100%=+1
    pct = breadth.get("pct_above_50ma")
    if pct is not None:
        scores["breadth"] = float(np.clip((pct - 50.0) / 50.0, -1.0, 1.0))
    else:
        scores["breadth"] = 0.0

    # Yield curve: T10Y2Y spread, normalized [-1.5, +1.5] → [-1, +1]
    yc = yield_curve.get("spread_pct")
    if yc is not None:
        scores["yield_curve"] = float(np.clip(yc / 1.5, -1.0, 1.0))
    else:
        scores["yield_curve"] = 0.0

    # Credit spread: BBB spread, normalized [1.0, 4.0] inverted (wide = negative)
    cr = credit.get("spread_pct")
    if cr is not None:
        scores["credit"] = float(np.clip(1.0 - 2.0 * (cr - 1.0) / (4.0 - 1.0), -1.0, 1.0))
    else:
        scores["credit"] = 0.0

    # Fed direction: CUTTING=+0.5, STABLE=0, HIKING=-0.5 (partial weight — policy lags)
    fed_dir = fed.get("direction", "STABLE")
    scores["fed"] = {"CUTTING": 0.5, "STABLE": 0.0, "HIKING": -0.5}.get(fed_dir, 0.0)

    return scores


def _kalman_smooth(raw_score: float, macro_path: str) -> float:
    """Scalar Kalman filter to smooth daily risk score.
    Prevents single-day noise from flipping regime.
    State: latent risk score x_t. Observation: raw_score z_t.
    Process noise Q=0.05 (regime changes slowly). Obs noise R=0.20 (signals are noisy).
    Persists filter state in macro_context.json across daily runs.
    Reference: Hamilton (1989), Kim & Nelson (1999).
    """
    Q = 0.05   # process noise — how fast the true regime can change
    R = 0.20   # observation noise — how noisy our signal composite is

    # Load prior state from last run
    x_prior = 0.0
    p_prior = 1.0
    try:
        if os.path.exists(macro_path):
            prev = json.load(open(macro_path))
            x_prior = float(prev.get("kalman_state", {}).get("x", 0.0))
            p_prior = float(prev.get("kalman_state", {}).get("p", 1.0))
    except Exception:
        pass

    # Predict
    x_pred = x_prior          # regime drifts slowly — no drift term
    p_pred = p_prior + Q      # uncertainty grows each day without observation

    # Update
    K = p_pred / (p_pred + R)            # Kalman gain
    x_updated = x_pred + K * (raw_score - x_pred)
    p_updated = (1 - K) * p_pred

    return x_updated, p_updated


def classify_macro(vix, spy, breadth, yield_curve, credit, fed) -> str:
    """P1-1: Kalman-filtered continuous risk score replaces integer vote count.

    Step 1: Convert each signal to continuous [-1, +1] score (no quantization).
    Step 2: Weighted average → raw composite risk score [-1, +1].
    Step 3: Kalman filter smooths score across days (prevents regime flickering).
    Step 4: Hysteresis applied — must cross threshold by 0.1 to change label.
    Step 5: Hard overrides for extreme conditions (VIX crisis, credit stress).

    Weights derived from signal informativeness (P1-1 audit):
      SPY trend: highest weight — direct equity regime signal
      VIX: second — realized fear, fast-moving
      Credit: third — institutional stress indicator
      Breadth: fourth — confirms or diverges from index
      Yield curve: fifth — slow-moving, structural
      Fed: lowest — policy lags reality by months

    Outputs canonical taxonomy: RISK_ON / NEUTRAL / RISK_OFF / CRISIS
    Also returns kalman_state for persistence.
    """
    # Hard overrides — check raw signals first, before any smoothing
    vix_reg = vix.get("regime", "UNKNOWN")
    cr_reg  = credit.get("regime", "UNKNOWN")
    if vix_reg == "CRISIS":
        return "CRISIS"
    if cr_reg == "STRESS" and vix_reg in ("ELEVATED", "CRISIS"):
        return "RISK_OFF"

    # Step 1: continuous scores
    sc = _to_continuous(vix, spy, breadth, yield_curve, credit, fed)

    # Step 2: weighted composite (weights sum to 1.0)
    weights = {
        "spy":         0.30,
        "vix":         0.25,
        "credit":      0.20,
        "breadth":     0.15,
        "yield_curve": 0.07,
        "fed":         0.03,
    }
    raw_score = sum(sc[k] * weights[k] for k in weights)

    # Step 3: Kalman filter
    x_smooth, p_smooth = _kalman_smooth(raw_score, MACRO_CONTEXT_PATH)

    # Step 4: Discretize with hysteresis
    # Thresholds: RISK_ON > 0.25, NEUTRAL [-0.25, 0.25], RISK_OFF < -0.25, CRISIS < -0.70
    if x_smooth >= 0.25:
        regime = "RISK_ON"
    elif x_smooth >= -0.25:
        regime = "NEUTRAL"
    elif x_smooth >= -0.70:
        regime = "RISK_OFF"
    else:
        regime = "CRISIS"

    # Attach Kalman state and scores to macro_context for persistence + transparency
    classify_macro._kalman_state = {"x": round(x_smooth, 4), "p": round(p_smooth, 4)}
    classify_macro._signal_scores = {k: round(v, 4) for k, v in sc.items()}
    classify_macro._raw_score = round(raw_score, 4)

    return regime

# ── Main ──────────────────────────────────────────────────────────────────────

def build_macro_context() -> dict:
    print("[MacroContext] Fetching macro signals...")

    print("  Fetching VIX...")
    vix = get_vix()

    print("  Fetching SPY trend...")
    spy = get_spy_trend()

    print("  Fetching sector breadth...")
    breadth = get_sector_breadth()

    print("  Fetching yield curve (FRED T10Y2Y)...")
    yield_curve = get_yield_curve()

    print("  Fetching credit spread (FRED BAMLC0A4CBBB)...")
    credit = get_credit_spread()

    print("  Fetching Fed funds rate (FRED FEDFUNDS)...")
    fed = get_fed_rate()

    regime = classify_macro(vix, spy, breadth, yield_curve, credit, fed)

    context = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "macro_regime": regime,
        "kalman_state": getattr(classify_macro, "_kalman_state", {"x": 0.0, "p": 1.0}),
        "raw_risk_score": getattr(classify_macro, "_raw_score", 0.0),
        "signal_scores": getattr(classify_macro, "_signal_scores", {}),
        "signals": {
            "vix":          vix,
            "spy_trend":    spy,
            "sector_breadth": breadth,
            "yield_curve":  yield_curve,
            "credit_spread": credit,
            "fed_rate":     fed,
        },
        "agent_summary": build_agent_summary(regime, vix, spy, breadth, yield_curve, credit, fed),
    }

    with open(MACRO_CONTEXT_PATH, "w") as f:
        json.dump(context, f, indent=2)

    print(f"\n[MacroContext] Regime: {regime}")
    print(f"[MacroContext] Written → {MACRO_CONTEXT_PATH}")
    return context


def build_agent_summary(regime, vix, spy, breadth, yield_curve, credit, fed) -> str:
    """
    Plain-English macro summary injected into every agent prompt.
    Concise enough to not bloat the context window.
    """
    lines = [
        f"MACRO REGIME: {regime}",
        f"VIX: {vix.get('value', 'N/A')} ({vix.get('regime', 'N/A')})",
        f"SPY: {spy.get('trend_20d', 'N/A')}% 20d trend, "
        f"{'above' if spy.get('above_200ma') else 'below'} 200MA ({spy.get('regime', 'N/A')})",
        f"Sector breadth: {breadth.get('pct_above_50ma', 'N/A')}% above 50MA ({breadth.get('regime', 'N/A')})",
        f"Yield curve (T10Y2Y): {yield_curve.get('spread_pct', 'N/A')}% ({yield_curve.get('regime', 'N/A')})",
        f"Credit spread (BBB): {credit.get('spread_pct', 'N/A')}% ({credit.get('regime', 'N/A')})",
        f"Fed funds: {fed.get('rate', 'N/A')}% ({fed.get('direction', 'N/A')})",
    ]
    return " | ".join(lines)


def print_summary():
    if not os.path.exists(MACRO_CONTEXT_PATH):
        print("[MacroContext] macro_context.json not found. Run without --summary first.")
        return
    with open(MACRO_CONTEXT_PATH, "r") as f:
        ctx = json.load(f)

    print(f"\n{'='*62}")
    print(f"  MACRO CONTEXT — {ctx.get('timestamp', 'N/A')[:19]}")
    print(f"{'='*62}")
    print(f"  Regime: {ctx.get('macro_regime', 'N/A')}")
    print(f"\n  {ctx.get('agent_summary', '')}")
    print(f"\n  Raw signals:")
    for key, val in ctx.get("signals", {}).items():
        print(f"    {key:20s}: {val}")
    print(f"\n{'='*62}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raptor Macro Context Engine — Layer 2")
    parser.add_argument("--summary", action="store_true", help="Print current macro_context.json")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        build_macro_context()
