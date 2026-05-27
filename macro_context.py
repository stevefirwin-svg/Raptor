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

def classify_macro(vix, spy, breadth, yield_curve, credit, fed) -> tuple:
    """
    Classify overall macro into: RISK_ON / NEUTRAL / RISK_OFF / CRISIS
    Returns (regime_label: str, macro_score: float)

    Scoring approach:
      Each signal votes +1 (bullish), 0 (neutral), or -1 (bearish).
      Score >= 3  -> RISK_ON
      Score >= 0  -> NEUTRAL
      Score >= -2 -> RISK_OFF
      Score <  -2 -> CRISIS

    Hard overrides:
      VIX CRISIS                    -> CRISIS regardless
      Credit STRESS + VIX ELEVATED  -> RISK_OFF minimum

    macro_score: raw integer score normalized to [-1, 1] (max possible ±8).
    Written to macro_context.json so signals.py can use continuous probability
    weighting instead of hard regime threshold switches.
    """
    score = 0

    # VIX
    vix_reg = vix.get("regime", "UNKNOWN")
    if vix_reg == "CALM":
        score += 1
    elif vix_reg in ("ELEVATED", "CRISIS"):
        score -= 1
    if vix_reg == "CRISIS":
        score -= 1  # extra penalty

    # SPY trend
    spy_reg = spy.get("regime", "UNKNOWN")
    if spy_reg == "BULLISH":
        score += 2
    elif spy_reg == "NEUTRAL":
        score += 1
    elif spy_reg == "BEARISH":
        score -= 2

    # Sector breadth
    br_reg = breadth.get("regime", "UNKNOWN")
    if br_reg == "BROAD_STRENGTH":
        score += 1
    elif br_reg == "WEAKENING":
        score -= 1
    elif br_reg == "BROAD_WEAKNESS":
        score -= 2

    # Yield curve
    yc_reg = yield_curve.get("regime", "UNKNOWN")
    if yc_reg == "STEEP":
        score += 1
    elif yc_reg == "MILDLY_INVERTED":
        score -= 1
    elif yc_reg == "DEEPLY_INVERTED":
        score -= 2

    # Credit spread
    cr_reg = credit.get("regime", "UNKNOWN")
    if cr_reg == "TIGHT":
        score += 1
    elif cr_reg == "ELEVATED":
        score -= 1
    elif cr_reg == "STRESS":
        score -= 2

    # Fed direction
    fed_dir = fed.get("direction", "UNKNOWN")
    if fed_dir == "CUTTING":
        score += 1
    elif fed_dir == "HIKING":
        score -= 1

    # Hard overrides
    if vix_reg == "CRISIS":
        return "CRISIS", float(np.clip(score / 8.0, -1.0, 1.0))
    if cr_reg == "STRESS" and vix_reg in ("ELEVATED", "CRISIS"):
        return "RISK_OFF", float(np.clip(score / 8.0, -1.0, 1.0))

    # TODO:DERIVE — vote boundaries (>=3 RISK_ON, >=0 NEUTRAL, >=-2 RISK_OFF)
    # are round numbers on an arbitrary integer scale. SPY vote counts double;
    # VIX CRISIS penalizes twice — weights without empirical basis.
    # The continuous macro_score (score/8.0) mitigates this downstream in
    # signals.py _regime_blend(). The label is still used in halt_in_crisis
    # and STANDBY logic. Target: replace with ARCH-2 gate (HMM/Kalman).
    # Max score theoretically ±8 but that requires all signals aligned;
    # realistic range is approximately ±5. Normalization by 8 is conservative.
    if score >= 3:
        label = "RISK_ON"
    elif score >= 0:
        label = "NEUTRAL"
    elif score >= -2:
        label = "RISK_OFF"
    else:
        label = "CRISIS"

    return label, float(np.clip(score / 8.0, -1.0, 1.0))

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

    regime, macro_score = classify_macro(vix, spy, breadth, yield_curve, credit, fed)

    context = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "macro_regime": regime,
        "macro_score":  macro_score,  # continuous [-1,1] for probability weighting in signals.py
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

    print(f"\n[MacroContext] Regime: {regime}  Score: {macro_score:+.3f}")
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
