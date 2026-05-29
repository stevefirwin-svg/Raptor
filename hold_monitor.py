"""
Raptor Hold Monitor — 8-Layer Position Health Scoring System
=============================================================
Computes continuous health scores for all open positions using:
  - Pre-entry 5-day trajectory (starts BEFORE entry)
  - Daily factor snapshot persistence
  - 8-layer weighted health score
  - Dynamic trim sizing (math-driven, not static)
  - Tier: STRENGTHENING / STABLE / DECAYING / INSUFFICIENT_DATA

Runs twice daily:
  - Morning (~9:52 AM) alongside exit monitor
  - Close (~4:15 PM) before recap email

Output:
  - hold_history.json  : full factor trajectory (ML training data)
  - hold_health.json   : today's scores, tiers, trim recommendations

Usage:
  python hold_monitor.py           # Full run
  python hold_monitor.py --pre     # Pre-entry scan only
  python hold_monitor.py --debug   # Verbose output
"""

import os
import json
import logging
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

from config import CONFIG
from data_feeds import DataManager
from signals import QuantSignalEngine, FACTOR_NAMES

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

HISTORY_FILE = "hold_history.json"
HEALTH_FILE  = "hold_health.json"
LEDGER_FILE  = "position_ledger.json"

LAYER_WEIGHTS = {
    "composite_slope":  0.25,
    "factor_agreement": 0.20,
    "price_momentum":   0.15,
    "cluster_health":   0.15,
    "volume":           0.10,
    "volatility":       0.08,
    "stop_distance":    0.05,
    "hold_duration":    0.02,
}

TIER_STRONG = 0.20
TIER_STABLE = -0.15
MIN_DAYS    = 3

CLUSTERS = {
    "MR":    ["rsi_mr", "bollinger_z", "crowd_panic", "ma_distance", "hurst"],
    "TREND": ["ma_stack", "macd_accel", "adx_dir", "price_cloud"],
    "VOL":   ["vol_ratio", "obv_r2", "accum_dist"],
    "VOLAT": ["atr_pctile", "bb_squeeze", "rel_strength"],
    "REV":   ["rev_momentum"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("raptor.hold_monitor")


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────

def load_history() -> Dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}, "pre_entry": {}}


def save_history(history: Dict):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f, indent=2, default=str)
    os.replace(tmp, HISTORY_FILE)


def load_health() -> Dict:
    if os.path.exists(HEALTH_FILE):
        try:
            with open(HEALTH_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_health(health: Dict):
    tmp = HEALTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(health, f, indent=2, default=str)
    os.replace(tmp, HEALTH_FILE)


def load_ledger() -> Dict:
    if not os.path.exists(LEDGER_FILE):
        return {}
    try:
        with open(LEDGER_FILE) as f:
            data = json.load(f)
        result = {}
        for key, val in data.get("positions", {}).items():
            sym = val.get("symbol", key.split(":")[-1])
            result[sym] = val
        return result
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_snapshot(sym: str, signal, bars: pd.DataFrame,
                   position: Optional[Dict] = None,
                   ledger_meta: Optional[Dict] = None) -> Dict:
    """Build one daily snapshot for a symbol — all metrics needed for scoring."""
    today  = datetime.now().isoformat()
    close  = bars["close"]
    high   = bars["high"]
    low    = bars["low"]
    vol    = bars["volume"]

    price_now = float(close.iloc[-1])
    price_5d  = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
    roc_5d    = (price_now / price_5d) - 1.0

    hh = hl = 0
    if len(high) >= 4:
        hh = int(high.iloc[-1] > high.iloc[-2] and high.iloc[-2] > high.iloc[-3])
        hl = int(low.iloc[-1]  > low.iloc[-2]  and low.iloc[-2]  > low.iloc[-3])

    ranges        = high - low
    close_pos     = ((close - low) / ranges.replace(0, np.nan)).fillna(0.5)
    close_pos_5d  = float(close_pos.iloc[-5:].mean()) if len(close_pos) >= 5 else 0.5

    vol_ma20  = float(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else float(vol.mean())
    vol_ratio = float(vol.iloc[-1]) / (vol_ma20 + 1e-9)

    obv       = (np.sign(close.diff()) * vol).fillna(0).cumsum()
    obv_slope = float(np.polyfit(range(min(5, len(obv))),
                                  obv.iloc[-5:].values, 1)[0]) if len(obv) >= 5 else 0.0

    price_diff = close.diff().iloc[-5:]
    vol_5d     = vol.iloc[-5:]
    up_vol     = float(vol_5d[price_diff > 0].sum())
    dn_vol     = float(vol_5d[price_diff < 0].sum())
    ud_ratio   = up_vol / (dn_vol + 1e-9)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr_now      = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else float(tr.mean())
    atr_10d      = float(tr.rolling(10).mean().iloc[-1]) if len(tr) >= 10 else atr_now
    atr_expansion = atr_now / (atr_10d + 1e-9)

    bb_mid   = close.rolling(20).mean()
    bb_std   = close.rolling(20).std()
    bb_width = float((2 * bb_std / (bb_mid + 1e-9)).iloc[-1]) if len(close) >= 20 else 0.0

    factor_scores        = getattr(signal, "factor_scores", {})
    factor_contributions = getattr(signal, "factor_contributions", {})
    composite            = getattr(signal, "composite_score", 0.0)
    t_stat               = getattr(signal, "t_statistic", 0.0)
    factors_positive     = getattr(signal, "factors_positive", 0)
    regime               = getattr(signal, "regime", "UNKNOWN")

    cluster_scores = {}
    for cluster, factors in CLUSTERS.items():
        vals = [factor_scores.get(fn, 0.0) for fn in factors if fn in factor_scores]
        cluster_scores[cluster] = float(np.mean(vals)) if vals else 0.0

    position_data = {}
    if position:
        entry_price = float(position.get("avg_entry", price_now))
        qty         = float(position.get("qty", 0))
        market_val  = float(position.get("market_value", price_now * qty))
        pnl_pct     = float(position.get("unrealized_pnl_pct", 0)) * 100
        meta        = (ledger_meta or {}).get("metadata", {})
        _raw_stop   = meta.get("stop")
        if _raw_stop is not None:
            stop_price    = float(_raw_stop)
            stop_dist_atr = (price_now - stop_price) / (atr_now + 1e-9)
        else:
            # No stop in ledger metadata — do not fabricate a price proxy.
            # entry_price * 0.92 was eliminated: it invents a value with zero ATR basis.
            # Layer 7 (_score_stop_distance) returns (0.0, "no_stop_data") when dist is None.
            logger.warning("No stop in ledger metadata for %s — stop_dist_atr skipped", sym)
            stop_price    = None
            stop_dist_atr = None

        entry_date_str = (ledger_meta or {}).get("entry_date", str(date.today()))
        try:
            entry_dt  = datetime.strptime(entry_date_str[:10], "%Y-%m-%d").date()
            days_held = (date.today() - entry_dt).days
        except Exception:
            days_held = 0

        hold_target = getattr(signal, "hold_target_days", 15)
        position_data = {
            "entry_price":   entry_price,
            "current_price": price_now,
            "qty":           qty,
            "market_value":  market_val,
            "pnl_pct":       pnl_pct,
            "stop_price":    stop_price,
            "stop_dist_atr": round(stop_dist_atr, 3) if stop_dist_atr is not None else None,
            "days_held":     days_held,
            "hold_target":   hold_target,
            "hold_ratio":    round(days_held / max(hold_target, 1), 3),
        }

    return {
        "timestamp":             today,
        "symbol":                sym,
        "composite":             round(composite, 4),
        "t_stat":                round(t_stat, 4),
        "factors_positive":      factors_positive,
        "factor_agreement":      round(factors_positive / len(FACTOR_NAMES), 4),
        "factor_scores":         {k: round(v, 4) for k, v in factor_scores.items()},
        "factor_contributions":  {k: round(v, 6) for k, v in factor_contributions.items()},
        "cluster_scores":        {k: round(v, 4) for k, v in cluster_scores.items()},
        "regime":                regime,
        "price":                 round(price_now, 4),
        "roc_5d":                round(roc_5d, 4),
        "higher_highs":          hh,
        "higher_lows":           hl,
        "close_pos_5d":          round(close_pos_5d, 4),
        "vol_ratio":             round(vol_ratio, 4),
        "obv_slope":             round(obv_slope, 4),
        "ud_ratio":              round(ud_ratio, 4),
        "atr":                   round(atr_now, 4),
        "atr_expansion":         round(atr_expansion, 4),
        "bb_width":              round(bb_width, 4),
        **position_data,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8-LAYER HEALTH SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _score_composite_slope(snapshots: List[Dict]) -> Tuple[float, str]:
    """Layer 1: Linear regression slope of composite score. +0.10/day = +1.0."""
    if len(snapshots) < 2:
        return 0.0, "insufficient_data"
    n      = min(5, len(snapshots))
    comps  = [s["composite"] for s in snapshots[-n:]]
    slope  = float(np.polyfit(range(len(comps)), comps, 1)[0])
    score  = float(np.clip(slope / 0.10, -1.0, 1.0))
    return score, f"slope={slope:+.4f}/day over {n}d"


def _score_factor_agreement(snapshots: List[Dict]) -> Tuple[float, str]:
    """Layer 2: FAR breadth + trend. <6/16 = strong sell (Frazzini & Pedersen 2014)."""
    latest    = snapshots[-1]
    far       = latest.get("factor_agreement", 0.5)
    n_pos     = latest.get("factors_positive", 8)
    far_trend = (snapshots[-1]["factor_agreement"] - snapshots[-3]["factor_agreement"]
                 if len(snapshots) >= 3 else 0.0)
    base      = (far - 0.5) * 2.0
    trend_adj = float(np.clip(far_trend * 3.0, -0.30, 0.30))
    score     = float(np.clip(base + trend_adj, -1.0, 1.0))
    return score, f"FAR={n_pos}/16  trend={far_trend:+.3f}"


def _score_price_momentum(snapshots: List[Dict]) -> Tuple[float, str]:
    """Layer 3: ROC(5d), higher highs/lows, close position, ROC trend over 3 days."""
    latest   = snapshots[-1]
    roc      = latest.get("roc_5d", 0.0)
    hh       = latest.get("higher_highs", 0)
    hl       = latest.get("higher_lows", 0)
    cp       = latest.get("close_pos_5d", 0.5)
    roc_s    = float(np.clip(roc / 0.10, -1.0, 1.0))
    struct_s = (hh + hl - 1.0)
    cp_s     = float(np.clip((cp - 0.5) * 4.0, -1.0, 1.0))

    # ROC trend: is momentum accelerating or decelerating over last 3 snapshots?
    # Consecutive ROC decline = early warning even before price breaks down.
    if len(snapshots) >= 3:
        rocs = [s.get("roc_5d", 0.0) for s in snapshots[-3:]]
        roc_delta = rocs[-1] - rocs[0]  # positive = accelerating, negative = decelerating
        roc_trend_s = float(np.clip(roc_delta / 0.05, -1.0, 1.0))
    else:
        roc_trend_s = 0.0

    score = float(np.clip(
        0.35 * roc_s + 0.30 * struct_s + 0.20 * cp_s + 0.15 * roc_trend_s,
        -1.0, 1.0))
    return score, f"ROC5={roc:+.2%}  HH={hh} HL={hl}  ClosePos={cp:.2f}  ROCtrend={roc_trend_s:+.2f}"


def _score_cluster_health(snapshots: List[Dict]) -> Tuple[float, str]:
    """Layer 4: Cluster-level z-scores. TREND+MR weighted highest."""
    cs      = snapshots[-1].get("cluster_scores", {})
    weights = {"TREND": 0.35, "MR": 0.30, "VOL": 0.15, "VOLAT": 0.12, "REV": 0.08}
    score   = 0.0
    details = []
    for cluster, weight in weights.items():
        cv    = cs.get(cluster, 0.0)
        cs_   = float(np.clip(cv / 1.5, -1.0, 1.0))
        score += weight * cs_
        details.append(f"{cluster}={cv:+.2f}")
    return float(np.clip(score, -1.0, 1.0)), "  ".join(details)


def _score_volume(snapshots: List[Dict]) -> Tuple[float, str]:
    """Layer 5: OBV slope + UD ratio + OBV trend over 3 days (institutional footprint)."""
    latest    = snapshots[-1]
    obv_slope = latest.get("obv_slope", 0.0)
    ud_ratio  = latest.get("ud_ratio", 1.0)

    # Normalize OBV slope by rolling std across available snapshots.
    # Previous: abs(obv_slope) / max(abs(obv_slope), 1000) — hardcoded 1000 floor
    # produced cross-sectional bias: high-volume stocks (large OBV units) were
    # systematically penalized vs low-volume stocks. Fix: normalize by the std
    # of OBV slopes seen for this position, making the score self-relative.
    # Fallback to abs value when fewer than 3 snapshots (std undefined).
    all_slopes = [s.get("obv_slope", 0.0) for s in snapshots]
    if len(all_slopes) >= 3:
        slope_std = float(np.std(all_slopes)) or 1.0
        mag = min(1.0, abs(obv_slope) / (slope_std + 1e-9))
    else:
        # Only 1-2 snapshots: can't compute std, use unit clip
        mag = min(1.0, abs(obv_slope) / (abs(obv_slope) + 1e-9)) if obv_slope != 0 else 0.0
    sign      = np.sign(obv_slope)
    obv_score = float(sign * mag)
    if ud_ratio > 1.5:
        ud_score = min(1.0, (ud_ratio - 1.0) / 2.0)
    elif ud_ratio < 0.67:
        ud_score = max(-1.0, (ud_ratio - 1.0))
    else:
        ud_score = (ud_ratio - 1.0) / 0.5

    # OBV trend: consecutive declining OBV slope = institutional distribution signal.
    # Uses linear fit over last 3 snapshots to detect direction of OBV momentum.
    if len(snapshots) >= 3:
        obv_slopes = [s.get("obv_slope", 0.0) for s in snapshots[-3:]]
        # Normalize by max magnitude to get direction score
        max_mag = max(abs(v) for v in obv_slopes) or 1.0
        norm = [v / max_mag for v in obv_slopes]
        obv_trend_s = float(np.clip(np.polyfit([0,1,2], norm, 1)[0] * 2.0, -1.0, 1.0))
    else:
        obv_trend_s = 0.0

    score = float(np.clip(
        0.35 * obv_score + 0.35 * ud_score + 0.30 * obv_trend_s,
        -1.0, 1.0))
    return score, f"OBV_slope={obv_slope:.0f}  UD_ratio={ud_ratio:.2f}  OBVtrend={obv_trend_s:+.2f}"


def _score_volatility(snapshots: List[Dict], pnl_pct: float = 0.0) -> Tuple[float, str]:
    """Layer 6: ATR expansion context — good if winning, bad if losing."""
    latest  = snapshots[-1]
    atr_exp = latest.get("atr_expansion", 1.0)
    bb_w    = latest.get("bb_width", 0.05)
    if atr_exp > 1.20:
        score = 0.5 if pnl_pct > 0 else -0.8
    elif atr_exp < 0.80:
        score = 0.0
    else:
        score = 0.2
    score += 0.1 * np.sign(pnl_pct) if bb_w > 0.10 else 0.0
    return float(np.clip(score, -1.0, 1.0)), f"ATR_exp={atr_exp:.2f}  BB_width={bb_w:.3f}"


def _score_stop_distance(snapshots: List[Dict]) -> Tuple[float, str]:
    """Layer 7: Stop distance in ATR units. <1.5 ATR = dangerous."""
    dist = snapshots[-1].get("stop_dist_atr", 2.0)
    if not dist:
        return 0.0, "no_stop_data"
    score = float(np.clip((dist - 1.5) / 1.5, -1.0, 1.0))
    return score, f"stop_dist={dist:.2f} ATR"


def _score_hold_duration(snapshots: List[Dict]) -> Tuple[float, str]:
    """Layer 8: Hold ratio vs target. Penalises unprofitable overstays."""
    latest     = snapshots[-1]
    hold_ratio = latest.get("hold_ratio", 0.5)
    pnl_pct    = latest.get("pnl_pct", 0.0)
    composite  = latest.get("composite", 0.0)
    if hold_ratio < 0.5:
        score = 0.0
    elif hold_ratio < 0.8:
        score = 0.1 if pnl_pct > 0 else -0.2
    elif hold_ratio < 1.5:
        score = 0.2 if pnl_pct > 0 else -0.4
    else:
        score = 0.1 if (pnl_pct > 0 and composite >= -0.5) else -0.6
    return float(np.clip(score, -1.0, 1.0)), f"hold_ratio={hold_ratio:.2f}  pnl={pnl_pct:+.1f}%"


def compute_health_score(snapshots: List[Dict]) -> Dict:
    """Compute 8-layer weighted health score from trajectory."""
    if not snapshots:
        return {"tier": "NO_DATA", "health": 0.0, "layers": {}, "decay_driver": ""}

    pnl_pct = snapshots[-1].get("pnl_pct", 0.0)

    scores = {
        "composite_slope":  _score_composite_slope(snapshots),
        "factor_agreement": _score_factor_agreement(snapshots),
        "price_momentum":   _score_price_momentum(snapshots),
        "cluster_health":   _score_cluster_health(snapshots),
        "volume":           _score_volume(snapshots),
        "volatility":       _score_volatility(snapshots, pnl_pct),
        "stop_distance":    _score_stop_distance(snapshots),
        "hold_duration":    _score_hold_duration(snapshots),
    }

    layers = {
        k: {"score": round(v[0], 4), "detail": v[1],
            "weight": LAYER_WEIGHTS[k]}
        for k, v in scores.items()
    }

    health = float(np.clip(
        sum(layers[k]["score"] * layers[k]["weight"] for k in layers),
        -1.0, 1.0
    ))

    if len(snapshots) < MIN_DAYS:
        tier = "INSUFFICIENT_DATA"
    elif health >= TIER_STRONG:
        tier = "STRENGTHENING"
    elif health >= TIER_STABLE:
        tier = "STABLE"
    else:
        tier = "DECAYING"

    decay_driver = ""
    if tier == "DECAYING":
        worst = min(layers, key=lambda k: layers[k]["score"] * layers[k]["weight"])
        decay_driver = f"{worst}: {layers[worst]['detail']}"

    # Pass raw stop_dist_atr float to compute_trim so it doesn't need to parse
    # the string detail field (H-5). Read from latest snapshot in trajectory.
    stop_dist_raw = snapshots[-1].get("stop_dist_atr")  # float or None

    return {
        "tier":              tier,
        "health":            round(health, 4),
        "layers":            layers,
        "decay_driver":      decay_driver,
        "pnl_pct":           round(pnl_pct, 2),
        "days_history":      len(snapshots),
        "stop_dist_atr_raw": stop_dist_raw,  # H-5: raw float for compute_trim
    }


# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC TRIM SIZING
# ─────────────────────────────────────────────────────────────────────────────

def compute_trim(health_result: Dict, position: Dict) -> Dict:
    """
    Continuous dynamic trim function — no static floors or ceilings.
    trim% = f(severity, stop_proximity, FAR, composite_slope, pnl_pct)
    Output: exact share count. Can recommend 0% to 100%.
    """
    tier   = health_result.get("tier", "STABLE")
    health = health_result.get("health", 0.0)
    layers = health_result.get("layers", {})
    pnl    = health_result.get("pnl_pct", 0.0)
    qty    = float(position.get("qty", 0))

    if tier not in ("DECAYING",) or qty <= 0:
        return {"action": "HOLD", "trim_pct": 0.0,
                "trim_shares": 0, "action_label": "", "reasoning": "", "components": {}}

    # Component 1: Severity — how far below TIER_STABLE
    severity = float(np.clip((TIER_STABLE - health) / (1.0 + TIER_STABLE), 0.0, 1.0))

    # Component 2: Stop proximity multiplier (H-5 fix)
    # Read stop_dist_atr directly from health_result["stop_dist_atr_raw"] — the
    # raw float passed through from the snapshot — instead of parsing the string
    # detail field ("stop_dist=X.XX ATR"), which was fragile and could silently
    # return wrong values on format changes.
    # compute_health_score now injects stop_dist_atr_raw; fall back gracefully if absent.
    dist_atr = health_result.get("stop_dist_atr_raw")
    if dist_atr is None:
        layer7_detail = layers.get("stop_distance", {}).get("detail", "")
        if layer7_detail == "no_stop_data":
            dist_atr = 2.0   # unknown stop — treat as neutral, not dangerous
        else:
            # Infer from layer score: score = clip((dist-1.5)/1.5, -1, 1) → dist = score*1.5+1.5
            layer7_score = layers.get("stop_distance", {}).get("score", 0.0)
            dist_atr = float(layer7_score * 1.5 + 1.5)
    dist_atr = max(0.0, float(dist_atr))

    stop_mult = 1.50 if dist_atr < 1.0 else \
                1.25 if dist_atr < 1.5 else \
                1.00 if dist_atr < 2.0 else 0.75

    # Component 3: FAR penalty — low agreement amplifies trim
    # TODO:DERIVE — 0.30 cap on far_mult amplification is a round number.
    # Derive from sensitivity analysis: how much does FAR=0/16 vs 8/16 affect
    # subsequent realized PnL? Should be calibrated from outcome data.
    far_score = layers.get("factor_agreement", {}).get("score", 0.0)
    far_mult  = float(1.0 + np.clip(-far_score * 0.30, 0.0, 0.30))

    # Component 4: Slope amplifier — rapid decay adds urgency
    # TODO:DERIVE — 0.20 max slope contribution is a round number.
    # Should be derived from relationship between composite slope rate and
    # subsequent exit outcome quality in trim_log.json.
    slope_score = layers.get("composite_slope", {}).get("score", 0.0)
    slope_adj   = float(np.clip(-slope_score * 0.20, 0.0, 0.20))

    # Component 5: PnL context — protect winners, cut losers faster
    pnl_mult = 0.60 if pnl > 15.0 else \
               0.80 if pnl >  5.0 else \
               1.00 if pnl >  0.0 else \
               1.20 if pnl > -5.0 else 1.40

    raw       = severity * stop_mult * far_mult * pnl_mult + slope_adj
    trim_pct  = float(np.clip(raw, 0.0, 1.0))
    trim_shares = int(round(qty * trim_pct))

    action = ("EXIT"          if trim_pct >= 0.90 else
              "TRIM_MAJOR"    if trim_pct >= 0.50 else
              "TRIM_MODERATE" if trim_pct >= 0.25 else
              "TRIM_MINOR")

    labels = {
        "EXIT":          "Full exit — math supports complete liquidation",
        "TRIM_MAJOR":    f"Major trim {trim_pct:.0%} — significant thesis decay",
        "TRIM_MODERATE": f"Moderate trim {trim_pct:.0%} — thesis weakening",
        "TRIM_MINOR":    f"Minor trim {trim_pct:.0%} — early warning, reduce exposure",
    }

    reasoning = (
        f"severity={severity:.3f} x stop_mult={stop_mult:.2f} x "
        f"far_mult={far_mult:.2f} x pnl_mult={pnl_mult:.2f} + "
        f"slope_adj={slope_adj:.3f} = {raw:.3f} -> {trim_pct:.1%} ({trim_shares} sh)"
    )

    return {
        "action":       action,
        "action_label": labels[action],
        "trim_pct":     round(trim_pct, 4),
        "trim_shares":  trim_shares,
        "reasoning":    reasoning,
        "components":   {
            "severity":  round(severity, 4),
            "stop_mult": stop_mult,
            "far_mult":  round(far_mult, 4),
            "pnl_mult":  pnl_mult,
            "slope_adj": round(slope_adj, 4),
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# PRE-ENTRY TRACKER
# ─────────────────────────────────────────────────────────────────────────────

def update_pre_entry(history: Dict, signals: List,
                     bars_dict: Dict, held_symbols: set) -> Dict:
    """Track 5-day pre-entry health for top signal candidates."""
    pre       = history.setdefault("pre_entry", {})
    today_str = str(date.today())
    cutoff    = str(date.today() - timedelta(days=7))

    # Prune stale or now-held symbols
    stale = [
        sym for sym, data in pre.items()
        if sym in held_symbols or
        (data.get("snapshots") and
         data["snapshots"][-1].get("timestamp", "")[:10] < cutoff)
    ]
    for sym in stale:
        pre.pop(sym, None)

    # Track top 10 candidates
    for sig in signals[:10]:
        sym  = sig.symbol
        bars = bars_dict.get(sym)
        if sym in held_symbols or bars is None or len(bars) < 20:
            continue
        snap = build_snapshot(sym, sig, bars)
        if sym not in pre:
            pre[sym] = {"snapshots": [], "first_seen": today_str}
        # Avoid duplicate same-day entries
        existing = [s["timestamp"][:10] for s in pre[sym]["snapshots"]]
        if today_str not in existing:
            pre[sym]["snapshots"].append(snap)
        else:
            pre[sym]["snapshots"][-1] = snap
        pre[sym]["last_updated"] = today_str
        snaps = pre[sym]["snapshots"]
        if len(snaps) >= 2:
            pre[sym]["pre_entry_health"] = compute_health_score(snaps)

    logger.info("Pre-entry tracker: %d candidates monitored", len(pre))
    return history


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUN
# ─────────────────────────────────────────────────────────────────────────────

def run_monitor(pre_entry_only: bool = False, debug: bool = False) -> Dict:
    if debug:
        logging.getLogger("raptor").setLevel(logging.DEBUG)

    logger.info("=" * 60)
    logger.info("RAPTOR HOLD MONITOR - %s", datetime.now().isoformat())
    logger.info("=" * 60)

    dm        = DataManager(CONFIG)
    engine    = QuantSignalEngine(CONFIG)
    history   = load_history()
    ledger    = load_ledger()
    positions = dm.alpaca.get_positions()
    held      = {p["symbol"] for p in positions}

    # Clean ghost entries from hold_health.json — symbols no longer held in Alpaca.
    # Exits don't clean hold_health themselves; this run is the cleanup point.
    # Without this, closed positions linger in hold_health until the file is regenerated.
    existing_health = load_health()
    ghosts = [sym for sym in existing_health if sym not in held]
    if ghosts:
        for sym in ghosts:
            del existing_health[sym]
        save_health(existing_health)
        logger.info("Cleaned %d ghost(s) from hold_health.json: %s", len(ghosts), ghosts)

    # Universe
    try:
        from universe_builder import UniverseBuilder
        universe = UniverseBuilder(CONFIG).build(max_symbols=150)
    except Exception:
        universe = list(held)
    for sym in held:
        if sym not in universe:
            universe.append(sym)
    if "SPY" not in universe:
        universe.append("SPY")

    logger.info("Fetching data for %d symbols...", len(universe))
    dataset   = dm.get_full_dataset(universe, lookback_days=CONFIG.signals.lookback_days)
    bars_dict = dataset["bars"]
    macro     = dataset["macro"]
    spy_bars  = bars_dict.get("SPY")

    signals    = engine.generate_signals(bars_dict, macro, dataset["sentiment"], spy_bars)
    # Use full uncapped signal map so held symbols outside top-N get real factor scores
    signal_map = getattr(engine, "_last_full_signals", {s.symbol: s for s in signals})

    history = update_pre_entry(history, signals, bars_dict, held)

    if pre_entry_only:
        save_history(history)
        logger.info("Pre-entry scan complete.")
        return {}

    today_str  = str(date.today())
    health_out = {}

    for pos in positions:
        sym         = pos["symbol"]
        bars        = bars_dict.get(sym)
        signal      = signal_map.get(sym)
        ledger_meta = ledger.get(sym, {})

        if bars is None or len(bars) < 5:
            logger.warning("Insufficient bars for %s — skipping", sym)
            continue

        if signal is None:
            # Signal engine did not score this symbol (data gap or universe miss).
            # composite_score = 0.0 (neutral/unknown) — NOT -1.0.
            # Assigning -1.0 would poison Layer 1 composite slope and corrupt
            # health scores on every cycle the engine can't reach the symbol.
            # Unknown != weak. Score neutrally until real data is available.
            class _Dummy:
                composite_score = 0.0; t_statistic = 0.0
                factor_scores = {}; factor_contributions = {}
                factors_positive = 0; regime = "UNKNOWN"; hold_target_days = 15
            signal = _Dummy()
            logger.warning("No signal for held %s — using neutral Dummy (comp=0.0)", sym)

        snapshot    = build_snapshot(sym, signal, bars, pos, ledger_meta)
        pos_history = history["positions"].setdefault(sym, {
            "symbol": sym,
            "entry_date": ledger_meta.get("entry_date", today_str),
            "snapshots": [],
        })

        existing_dates = [s["timestamp"][:10] for s in pos_history["snapshots"]]
        if today_str not in existing_dates:
            pos_history["snapshots"].append(snapshot)
        else:
            pos_history["snapshots"][-1] = snapshot

        pre_snaps    = history["pre_entry"].get(sym, {}).get("snapshots", [])
        trajectory   = pre_snaps + pos_history["snapshots"]
        health_result = compute_health_score(trajectory)
        trim_result   = compute_trim(health_result, pos)

        record = {
            "symbol":           sym,
            "timestamp":        datetime.now().isoformat(),
            "tier":             health_result["tier"],
            "health":           health_result["health"],
            "pnl_pct":          snapshot.get("pnl_pct", 0.0),
            "days_held":        snapshot.get("days_held", 0),
            "days_history":     health_result["days_history"],
            "pre_entry_days":   len(pre_snaps),
            "composite":        snapshot["composite"],
            "factors_positive": snapshot["factors_positive"],
            "stop_dist_atr":    snapshot.get("stop_dist_atr", 0.0),
            "layers":           health_result["layers"],
            "decay_driver":     health_result["decay_driver"],
            "trim":             trim_result,
            "snapshot":         snapshot,
        }
        health_out[sym] = record

        icon = {"STRENGTHENING": "GRN", "STABLE": "YLW",
                "DECAYING": "RED", "INSUFFICIENT_DATA": "---"}.get(
            health_result["tier"], "---")
        logger.info(
            "  [%s] %-6s  health=%+.3f  FAR=%d/16  pnl=%+.1f%%  stop=%.2fATR  -> %s",
            icon, sym, health_result["health"],
            snapshot["factors_positive"],
            snapshot.get("pnl_pct", 0.0),
            snapshot.get("stop_dist_atr") or 0.0,
            trim_result["action"]
        )
        if health_result["tier"] == "DECAYING":
            logger.info("    DECAY: %s", health_result["decay_driver"])
            logger.info("    TRIM:  %s", trim_result["reasoning"])

    save_history(history)
    save_health(health_out)

    # ── Hold Agent screening ──────────────────────────────────────────────────
    try:
        from agent_layer import run_hold_screening
        agent_contexts = [{
            "symbol":           sym,
            "hold_days":        rec["days_held"],
            "unrealized_pct":   rec["pnl_pct"],
            "health_score":     rec["health"],
            "factor_coherence": rec["layers"].get("factor_agreement", 0.5),
            "active_exit_path": rec.get("trim", {}).get("action", "HOLD"),
        } for sym, rec in health_out.items()]
        if agent_contexts:
            agent_decisions = run_hold_screening(agent_contexts)
            for sym, dec in agent_decisions.items():
                if dec.get("decision") == "EXIT" and dec.get("confidence", 0) >= 0.85:
                    logger.warning("HoldAgent EXIT signal: %s -- %s (conf=%.2f)",
                                   sym, dec.get("reasoning", ""), dec["confidence"])
                elif dec.get("decision") == "TRIM":
                    logger.info("HoldAgent TRIM signal: %s -- %s (conf=%.2f)",
                                sym, dec.get("reasoning", ""), dec.get("confidence", 0))
    except Exception as e:
        logger.warning("HoldAgent unavailable (%s) -- proceeding without agent layer.", e)
    # ─────────────────────────────────────────────────────────────────────────

    tiers = [v["tier"] for v in health_out.values()]
    logger.info("=" * 60)
    logger.info("COMPLETE — %d positions | STR=%d  STA=%d  DEC=%d  INS=%d",
                len(health_out),
                tiers.count("STRENGTHENING"), tiers.count("STABLE"),
                tiers.count("DECAYING"),      tiers.count("INSUFFICIENT_DATA"))
    logger.info("=" * 60)
    return health_out


# ─────────────────────────────────────────────────────────────────────────────
# RECAP EMAIL HTML BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_health_html(health_out: Optional[Dict] = None) -> str:
    """Build HTML section for daily_recap.py email."""
    if health_out is None:
        health_out = load_health()
    if not health_out:
        return '<div style="color:#6a6a8a;font-size:12px;padding:8px">No health data — monitor not yet run.</div>'

    tier_meta = {
        "STRENGTHENING":    ("GRN", "#00d4aa"),
        "STABLE":           ("YLW", "#ffa502"),
        "DECAYING":         ("RED", "#ff4757"),
        "INSUFFICIENT_DATA":("---", "#6a6a8a"),
    }
    tier_order = {"DECAYING": 0, "STABLE": 1, "STRENGTHENING": 2, "INSUFFICIENT_DATA": 3}
    sorted_recs = sorted(health_out.values(),
                         key=lambda x: tier_order.get(x["tier"], 4))
    rows = ""
    for rec in sorted_recs:
        sym      = rec["symbol"]
        tier     = rec["tier"]
        health   = rec["health"]
        pnl      = rec["pnl_pct"]
        far      = rec["factors_positive"]
        stop_atr = rec.get("stop_dist_atr") or 0.0  # None stored when stop missing → coerce to 0.0
        pre_days = rec.get("pre_entry_days", 0)
        hist     = rec.get("days_history", 0)
        trim     = rec.get("trim", {})
        decay    = rec.get("decay_driver", "")
        comp     = rec.get("composite", 0.0)

        label, color = tier_meta.get(tier, ("---", "#6a6a8a"))
        pnl_c    = "#00d4aa" if pnl >= 0 else "#ff4757"
        health_c = "#00d4aa" if health >= TIER_STRONG else \
                   "#ffa502" if health >= TIER_STABLE else "#ff4757"
        hist_str = f"{pre_days}pre+{hist}d" if pre_days else f"{hist}d"

        trim_row = ""
        if trim.get("action", "HOLD") != "HOLD":
            tc = "#ff4757" if "EXIT" in trim["action"] else "#ffa502"
            trim_row = (
                f'<tr><td colspan="8" style="padding:3px 10px 7px 28px;'
                f'border-bottom:1px solid #1a1a2e;color:{tc};'
                f'font-size:11px;font-family:Courier New,monospace">'
                f'&#x2937; {trim["action_label"]} | '
                f'{trim["trim_shares"]} shares ({trim["trim_pct"]:.1%})'
                f'</td></tr>'
            )
        decay_row = ""
        if decay:
            decay_row = (
                f'<tr><td colspan="8" style="padding:2px 10px 4px 28px;'
                f'border-bottom:1px solid #1a1a2e;color:#ff4757;'
                f'font-size:10px;font-family:Courier New,monospace">'
                f'&#x2937; Decay: {decay}</td></tr>'
            )

        rows += f"""
        <tr>
          <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#e0e0e0;font-weight:600">[{label}] {sym}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:{color};font-weight:700;text-align:center">{tier.replace('_',' ')}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:{health_c};text-align:right;font-weight:600">{health:+.3f}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:{pnl_c};text-align:right">{pnl:+.1f}%</td>
          <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#a0a0b0;text-align:center">{far}/16</td>
          <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#a0a0b0;text-align:right">{comp:+.3f}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#a0a0b0;text-align:right">{stop_atr:.2f} ATR</td>
          <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#6a6a8a;text-align:right;font-size:11px">{hist_str}</td>
        </tr>{decay_row}{trim_row}"""

    return f"""
    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Position Health Monitor</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <tr style="border-bottom:2px solid #2a2a3e">
        <th style="padding:8px 10px;text-align:left;color:#00d4aa;font-size:11px;text-transform:uppercase">Symbol</th>
        <th style="padding:8px 10px;text-align:center;color:#00d4aa;font-size:11px;text-transform:uppercase">Tier</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Health</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">P&amp;L</th>
        <th style="padding:8px 10px;text-align:center;color:#00d4aa;font-size:11px;text-transform:uppercase">FAR</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Composite</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Stop Dist</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">History</th>
      </tr>
      {rows}
    </table>
    <div style="color:#3a3a5e;font-size:10px;margin-top:6px;padding:0 4px">
      Health: &gt;+0.20=STRENGTHENING | -0.15 to +0.20=STABLE | &lt;-0.15=DECAYING
      | FAR=Factor Agreement Ratio (of 16) | History=pre+hold days
    </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raptor Hold Monitor")
    parser.add_argument("--pre",   action="store_true", help="Pre-entry scan only")
    parser.add_argument("--debug", action="store_true", help="Verbose output")
    args = parser.parse_args()
    run_monitor(pre_entry_only=args.pre, debug=args.debug)



