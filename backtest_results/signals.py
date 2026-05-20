"""
Raptor v5.4 — Merged Adaptive Engine
======================================
v5.3 architecture (16 factors, per-stock regime, market scaling)
+ Meta Raptor innovations (inverse-vol weighting, aggressive sizing,
  score-rank entry)

KEY CHANGES FROM v5.3:
  1. INVERSE-VOLATILITY FACTOR WEIGHTING (from Meta Raptor)
     Factors with high cross-sectional dispersion today get
     upweighted. Factors that aren't differentiating get
     downweighted. Self-tuning every scan, zero static weights.
     Applied ON TOP of regime and micro-regime multipliers.

  2. SCORE-RANK ENTRY (from Meta Raptor)
     No hard t-stat cutoff. Take top N stocks by composite
     score where composite > 0. The cross-sectional ranking
     IS the signal — if a stock is in the 99th percentile of
     the universe, it's a trade regardless of the t-stat.

  3. KELLY CAP AT 12% (from Meta Raptor)
     More aggressive sizing for high-conviction signals.
     Combined with market momentum scaling (0.5-1.0x),
     this self-regulates in weak markets.

  4. ZERO-CONTRIBUTION THRESHOLD AT 0.10 (from Meta Raptor)
     More aggressive at killing dead factors per stock.

PRESERVED FROM v5.3:
  - 16 factors with crowd_panic and reversal_momentum
  - Per-stock micro-regime detection (TRENDING/REVERTING/MIXED)
  - Market-level momentum scaling
  - Adaptive ridge regression learning from closed trades
  - Surgical leverage module
  - Cross-sectional z-scoring and orthogonalization
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from config import RaptorConfig

logger = logging.getLogger("raptor.signals")

MIN_BARS_REQUIRED = 80


@dataclass
class Signal:
    symbol: str
    side: str
    composite_score: float
    composite_percentile: float
    t_statistic: float
    factor_scores: Dict[str, float]
    factor_contributions: Dict[str, float]
    factors_positive: int
    regime: str
    sentiment_score: float
    atr: float
    entry_price: float
    stop_price: float
    take_profit: float
    kelly_fraction: float
    hold_target_days: int
    leverage_qualified: bool
    confirmation_type: str
    timestamp: str


# =========================================================================
# FACTORS — 16 stock-level, all stateless
# =========================================================================

class Factors:

    @staticmethod
    def rsi_mr(c, period=5):
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
        rsi = 100 - 100 / (1 + gain / (loss + 1e-10))
        return float((50 - rsi.iloc[-1]) / 50)

    @staticmethod
    def bollinger_z(c, period=20):
        sma = c.rolling(period).mean().iloc[-1]
        std = c.rolling(period).std().iloc[-1]
        return float(-(c.iloc[-1] - sma) / std) if std > 1e-10 else 0.0

    @staticmethod
    def crowd_panic(df):
        c, v = df["close"], df["volume"]
        avg_vol = v.iloc[-21:-1].mean()
        if avg_vol <= 0:
            return 0.0
        panic = 0.0
        for i in [-1, -2, -3]:
            if len(c) < abs(i) + 1:
                continue
            ret = c.iloc[i] / c.iloc[i - 1] - 1
            if ret < 0:
                panic += (v.iloc[i] / avg_vol) * abs(ret)
        return float(panic)

    @staticmethod
    def ma_distance(c):
        e8 = c.ewm(span=8, adjust=False).mean().iloc[-1]
        e21 = c.ewm(span=21, adjust=False).mean().iloc[-1]
        e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
        avg = (e8 + e21 + e50) / 3
        return float(-(c.iloc[-1] - avg) / avg) if avg != 0 else 0.0

    @staticmethod
    def hurst(c, max_lag=20):
        rets = np.log(c / c.shift(1)).dropna().values
        if len(rets) < max_lag * 2:
            return np.nan
        rs_pts = []
        for lag in range(2, max_lag + 1):
            n_sub = len(rets) // lag
            if n_sub < 1:
                continue
            rs_list = []
            for i in range(n_sub):
                sub = rets[i * lag:(i + 1) * lag]
                dev = np.cumsum(sub - sub.mean())
                R = dev.max() - dev.min()
                S = sub.std()
                if S > 1e-10:
                    rs_list.append(R / S)
            if rs_list:
                rs_pts.append((np.log(lag), np.log(np.mean(rs_list))))
        if len(rs_pts) < 4:
            return np.nan
        x = np.array([p[0] for p in rs_pts])
        y = np.array([p[1] for p in rs_pts])
        return float(0.5 - np.polyfit(x, y, 1)[0])

    @staticmethod
    def ma_stack(c):
        e8 = c.ewm(span=8, adjust=False).mean()
        e21 = c.ewm(span=21, adjust=False).mean()
        e50 = c.ewm(span=50, adjust=False).mean()
        order = float((e8.iloc[-1] > e21.iloc[-1]) + (e21.iloc[-1] > e50.iloc[-1]) - 1)
        s8 = (e8.iloc[-1] / e8.iloc[-5] - 1) if len(e8) >= 5 else 0
        s21 = (e21.iloc[-1] / e21.iloc[-5] - 1) if len(e21) >= 5 else 0
        s50 = (e50.iloc[-1] / e50.iloc[-5] - 1) if len(e50) >= 5 else 0
        slope = np.clip((s8 + s21 + s50) / 3 * 50, -0.4, 0.4)
        return float(order * 0.6 + slope)

    @staticmethod
    def macd_accel(c, fast=12, slow=26, sig=9):
        ef = c.ewm(span=fast, adjust=False).mean()
        es = c.ewm(span=slow, adjust=False).mean()
        hist = ef - es - (ef - es).ewm(span=sig, adjust=False).mean()
        y = hist.iloc[-5:].values
        return float(np.polyfit(np.arange(5), y, 1)[0] / c.iloc[-1])

    @staticmethod
    def adx_dir(df, period=14):
        h, l, c = df["high"], df["low"], df["close"]
        pdm = h.diff().clip(lower=0)
        mdm = (-l.diff()).clip(lower=0)
        pdm[pdm < mdm] = 0.0
        mdm[mdm < pdm] = 0.0
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr_s = tr.ewm(span=period, adjust=False).mean()
        pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / atr_s
        mdi = 100 * mdm.ewm(span=period, adjust=False).mean() / atr_s
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
        adx = dx.ewm(span=period, adjust=False).mean()
        sign = 1.0 if pdi.iloc[-1] > mdi.iloc[-1] else -1.0
        return float(adx.iloc[-1] * sign)

    @staticmethod
    def adx_raw(df, period=14):
        h, l, c = df["high"], df["low"], df["close"]
        pdm = h.diff().clip(lower=0)
        mdm = (-l.diff()).clip(lower=0)
        pdm[pdm < mdm] = 0.0
        mdm[mdm < pdm] = 0.0
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr_s = tr.ewm(span=period, adjust=False).mean()
        pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / atr_s
        mdi = 100 * mdm.ewm(span=period, adjust=False).mean() / atr_s
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
        return float(dx.ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def price_cloud(c):
        e8 = c.ewm(span=8, adjust=False).mean().iloc[-1]
        e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
        mid = (e8 + e50) / 2
        width = abs(e8 - e50)
        return float((c.iloc[-1] - mid) / width) if width > 1e-10 else 0.0

    @staticmethod
    def vol_ratio(v):
        avg = v.iloc[-21:-1].mean()
        return float(np.log(v.iloc[-1] / avg)) if avg > 0 else np.nan

    @staticmethod
    def obv_r2(df, lb=10):
        obv = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
        y = obv.iloc[-lb:].values
        y_s = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), y_s)
        return float(s * r ** 2)

    @staticmethod
    def accum_dist(df, lb=10):
        clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"] + 1e-10)
        ad = (clv * df["volume"]).cumsum()
        y = ad.iloc[-lb:].values
        y_s = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), y_s)
        return float(s * abs(r))

    @staticmethod
    def atr_pctile(df, atr_p=14, lb=60):
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr_s = tr.rolling(atr_p).mean().dropna()
        if len(atr_s) < lb:
            return np.nan
        pct = scipy_stats.percentileofscore(atr_s.iloc[-lb:].values, atr_s.iloc[-1]) / 100
        return float(-(pct - 0.5) * 2)

    @staticmethod
    def bb_squeeze(c, period=20, lb=60):
        bw = (4 * c.rolling(period).std() / c.rolling(period).mean()).dropna()
        if len(bw) < lb:
            return np.nan
        pct = scipy_stats.percentileofscore(bw.iloc[-lb:].values, bw.iloc[-1]) / 100
        return float(-(pct - 0.5) * 2)

    @staticmethod
    def rel_strength(sym_c, spy_c, period=10):
        if len(spy_c) < period:
            return np.nan
        return float((sym_c.iloc[-1] / sym_c.iloc[-period]) -
                     (spy_c.iloc[-1] / spy_c.iloc[-period]))

    @staticmethod
    def reversal_momentum(df, lookback=3):
        c, l_col, h = df["close"], df["low"], df["high"]
        tr = pd.concat([h - l_col, (h - c.shift(1)).abs(), (l_col - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return np.nan
        return float((c.iloc[-1] - l_col.iloc[-lookback:].min()) / atr)

    @staticmethod
    def atr(df, period=14):
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    @staticmethod
    def check_leverage(df, spy_bars, rsi_val, bb_z):
        if spy_bars is None or len(spy_bars) < 205:
            return False
        spy_c = spy_bars["close"]
        sma200 = spy_c.rolling(200).mean()
        if not (spy_c.iloc[-1] > sma200.iloc[-1] and sma200.iloc[-1] > sma200.iloc[-5]):
            return False
        if rsi_val >= 30 or bb_z < 2.0:
            return False
        c, h, l = df["close"], df["high"], df["low"]
        ema20 = c.ewm(span=20, adjust=False).mean()
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        kelt_lower = ema20 - 1.5 * tr.rolling(14).mean()
        if c.iloc[-1] >= kelt_lower.iloc[-1]:
            return False
        avg_vol = df["volume"].iloc[-21:-1].mean()
        if avg_vol <= 0 or df["volume"].iloc[-1] / avg_vol < 1.5:
            return False
        return True


# =========================================================================
# ADAPTIVE RIDGE — learns from closed trades
# =========================================================================

class AdaptiveWeights:
    WEIGHT_FILE = "adaptive_weights.json"
    MIN_TRADES = 30
    MAX_ALPHA = 0.30
    RIDGE_LAMBDA = 1.0

    def __init__(self, factor_names, base_dir="."):
        self.factor_names = factor_names
        self.path = os.path.join(base_dir, self.WEIGHT_FILE)
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {"trades": [], "ridge_beta": None, "n_trades": 0}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def record_trade(self, factor_zscores, realized_return):
        row = {fn: factor_zscores.get(fn, 0.0) for fn in self.factor_names}
        row["y"] = realized_return
        self.data["trades"].append(row)
        self.data["n_trades"] = len(self.data["trades"])
        self._fit()
        self._save()

    def _fit(self):
        trades = self.data["trades"]
        if len(trades) < self.MIN_TRADES:
            self.data["ridge_beta"] = None
            return
        X = np.array([[t.get(fn, 0) for fn in self.factor_names] for t in trades])
        y = np.array([t["y"] for t in trades])
        k = len(self.factor_names)
        try:
            beta = np.linalg.solve(X.T @ X + self.RIDGE_LAMBDA * np.eye(k), X.T @ y)
            self.data["ridge_beta"] = beta.tolist()
        except np.linalg.LinAlgError:
            self.data["ridge_beta"] = None

    def blend_weights(self, base_weights):
        if self.data["ridge_beta"] is None:
            return base_weights
        beta = np.abs(np.array(self.data["ridge_beta"]))
        if beta.sum() < 1e-10:
            return base_weights
        normalized = beta / beta.sum()
        ridge_adj = {fn: float(normalized[i]) for i, fn in enumerate(self.factor_names)}
        n = self.data["n_trades"]
        alpha = min(self.MAX_ALPHA, self.MAX_ALPHA * (n - self.MIN_TRADES) / (2 * self.MIN_TRADES))
        alpha = max(0, alpha)
        blended = {}
        for fn in base_weights:
            blended[fn] = (1 - alpha) * base_weights[fn] + alpha * ridge_adj.get(fn, base_weights[fn])
        tot = sum(blended.values())
        return {k: v / tot for k, v in blended.items()}


# =========================================================================
# ENGINE
# =========================================================================

FACTOR_NAMES = [
    "rsi_mr", "bollinger_z", "crowd_panic", "ma_distance", "hurst",
    "ma_stack", "macd_accel", "adx_dir", "price_cloud",
    "vol_ratio", "obv_r2", "accum_dist",
    "atr_pctile", "bb_squeeze", "rel_strength",
    "rev_momentum",
]

FACTOR_CLUSTERS = {
    "rsi_mr": "mr", "bollinger_z": "mr", "crowd_panic": "mr",
    "ma_distance": "mr", "hurst": "mr",
    "ma_stack": "trend", "macd_accel": "trend", "adx_dir": "trend",
    "price_cloud": "trend",
    "vol_ratio": "vol", "obv_r2": "vol", "accum_dist": "vol",
    "atr_pctile": "volat", "bb_squeeze": "volat", "rel_strength": "volat",
    "rev_momentum": "rev",
}

MICRO_MULT = {
    "TRENDING":  {"mr": 0.6, "trend": 1.5, "vol": 1.0, "volat": 0.8, "rev": 0.5},
    "REVERTING": {"mr": 1.5, "trend": 0.6, "vol": 1.1, "volat": 1.2, "rev": 1.5},
    "MIXED":     {"mr": 1.0, "trend": 1.0, "vol": 1.0, "volat": 1.0, "rev": 1.0},
}

REGIME_MULT = {
    "EXPANSION": {"mr": 0.8, "trend": 1.3, "vol": 1.0, "volat": 0.8, "rev": 0.7},
    "BULLISH":   {"mr": 0.9, "trend": 1.2, "vol": 1.0, "volat": 0.9, "rev": 0.8},
    "NEUTRAL":   {"mr": 1.0, "trend": 1.0, "vol": 1.0, "volat": 1.0, "rev": 1.0},
    "BEARISH":   {"mr": 1.3, "trend": 0.7, "vol": 1.1, "volat": 1.2, "rev": 1.3},
    "CRISIS":    {"mr": 1.5, "trend": 0.5, "vol": 1.2, "volat": 1.4, "rev": 1.5},
}


class QuantSignalEngine:

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self.rcfg = cfg.risk
        self.f = Factors()
        self.adaptive = AdaptiveWeights(
            FACTOR_NAMES,
            os.path.dirname(os.path.abspath(__file__)),
        )

    def _raw(self, sym, bars, spy_bars):
        c, v = bars["close"], bars["volume"]
        spy_c = spy_bars["close"] if spy_bars is not None else pd.Series(dtype=float)
        return {
            "rsi_mr":       self.f.rsi_mr(c),
            "bollinger_z":  self.f.bollinger_z(c),
            "crowd_panic":  self.f.crowd_panic(bars),
            "ma_distance":  self.f.ma_distance(c),
            "hurst":        self.f.hurst(c),
            "ma_stack":     self.f.ma_stack(c),
            "macd_accel":   self.f.macd_accel(c),
            "adx_dir":      self.f.adx_dir(bars),
            "price_cloud":  self.f.price_cloud(c),
            "vol_ratio":    self.f.vol_ratio(v),
            "obv_r2":       self.f.obv_r2(bars),
            "accum_dist":   self.f.accum_dist(bars),
            "atr_pctile":   self.f.atr_pctile(bars),
            "bb_squeeze":   self.f.bb_squeeze(c),
            "rel_strength": self.f.rel_strength(c, spy_c),
            "rev_momentum": self.f.reversal_momentum(bars),
        }

    def _detect_micro(self, hurst_raw, bars):
        H = hurst_raw if not (isinstance(hurst_raw, float) and np.isnan(hurst_raw)) else 0.0
        actual_H = 0.5 - H
        adx = self.f.adx_raw(bars)
        if actual_H > 0.55 and adx > 25:
            return "TRENDING"
        elif actual_H < 0.45 and adx < 20:
            return "REVERTING"
        return "MIXED"

    def _market_scale(self, spy_bars):
        if spy_bars is None or len(spy_bars) < 21:
            return 1.0
        spy_c = spy_bars["close"]
        roc = (spy_c.iloc[-1] / spy_c.iloc[-21]) - 1.0
        if roc > 0.02:
            return 1.0
        elif roc > -0.02:
            return 0.8
        return 0.5

    def generate_signals(self, bars_dict, macro_data, sentiment_dict, spy_bars=None):
        regime = macro_data.get("regime", "NEUTRAL")
        if regime == "CRISIS" and self.rcfg.halt_in_crisis:
            return []

        market_scale = self._market_scale(spy_bars)

        # Step 1: compute raw factors for all stocks
        raw = {}
        micros = {}
        for sym, bars in bars_dict.items():
            if len(bars) < MIN_BARS_REQUIRED:
                continue
            try:
                r = self._raw(sym, bars, spy_bars)
                raw[sym] = r
                micros[sym] = self._detect_micro(r["hurst"], bars)
            except Exception:
                continue

        if len(raw) < 10:
            return []

        # Step 2: build z-score matrix
        syms = list(raw.keys())
        zmat = {}
        for fn in FACTOR_NAMES:
            vals = []
            for s in syms:
                v = raw[s].get(fn, np.nan)
                if isinstance(v, float) and np.isnan(v):
                    vals.append(np.nan)
                else:
                    vals.append(v)
            arr = np.array([v for v in vals if not (isinstance(v, float) and np.isnan(v))])
            if len(arr) < 5:
                for s in syms:
                    zmat.setdefault(s, {})[fn] = 0.0
                continue
            mu, sig = arr.mean(), arr.std()
            if sig < 1e-10:
                for s in syms:
                    zmat.setdefault(s, {})[fn] = 0.0
                continue
            for i, s in enumerate(syms):
                v = vals[i]
                if isinstance(v, float) and np.isnan(v):
                    zmat.setdefault(s, {})[fn] = 0.0
                else:
                    zmat.setdefault(s, {})[fn] = float(np.clip((v - mu) / sig, -3, 3))

        # Step 3: INVERSE-VOLATILITY WEIGHTING (from Meta Raptor)
        # Factors with high cross-sectional dispersion today = more informative
        factor_dispersions = {}
        for fn in FACTOR_NAMES:
            zvals = [zmat[s][fn] for s in syms]
            factor_dispersions[fn] = np.std(zvals) + 1e-6

        inv_vol_weights = {}
        for fn in FACTOR_NAMES:
            inv_vol_weights[fn] = 1.0 / factor_dispersions[fn]
        iv_total = sum(inv_vol_weights.values())
        inv_vol_weights = {fn: v / iv_total for fn, v in inv_vol_weights.items()}

        # Step 4: Score each stock with per-stock adaptive weights
        scored = []
        all_comp = []
        for sym in syms:
            micro = micros.get(sym, "MIXED")
            macro_m = REGIME_MULT.get(regime, REGIME_MULT["NEUTRAL"])
            micro_m = MICRO_MULT.get(micro, MICRO_MULT["MIXED"])
            cl = FACTOR_CLUSTERS

            # Layer 1: regime + micro regime multipliers on inverse-vol base
            w = {}
            for fn in FACTOR_NAMES:
                c = cl[fn]
                w[fn] = inv_vol_weights[fn] * macro_m[c] * micro_m[c]
            w_tot = sum(w.values())
            w = {fn: v / w_tot for fn, v in w.items()}

            # Layer 2: adaptive ridge blend
            w = self.adaptive.blend_weights(w)

            # Layer 3: zero-contribution elimination (threshold 0.10)
            z = zmat[sym]
            active = {fn: z[fn] for fn in FACTOR_NAMES if abs(z[fn]) > 0.10}
            if len(active) < 3:
                active = {fn: z[fn] for fn in FACTOR_NAMES}

            aw_sum = sum(w[fn] for fn in active)
            if aw_sum < 1e-10:
                continue

            comp = sum(z[fn] * w[fn] / aw_sum for fn in active)
            all_comp.append(comp)

            contribs = {}
            for fn in FACTOR_NAMES:
                if fn in active:
                    contribs[fn] = round(z[fn] * w[fn] / aw_sum, 6)
                else:
                    contribs[fn] = 0.0

            t_stat = comp / (np.std([z[fn] for fn in FACTOR_NAMES]) + 0.5)

            scored.append({
                "sym": sym, "comp": comp, "t": t_stat,
                "contribs": contribs, "micro": micro, "w": w,
            })

        if not scored:
            return []

        # Step 5: SCORE-RANK ENTRY (from Meta Raptor)
        # Take top N with positive composite — no hard t-stat cutoff
        scored.sort(key=lambda x: x["comp"], reverse=True)
        top = [s for s in scored if s["comp"] > 0][:self.cfg.execution.max_orders_per_scan * 2]

        comp_arr = np.array(all_comp)
        signals = []

        for s in top:
            sym = s["sym"]
            bars = bars_dict[sym]
            entry = float(bars["close"].iloc[-1])
            atr_val = self.f.atr(bars, self.rcfg.atr_period)
            if atr_val <= 0 or entry <= 0:
                continue

            # Stop adapts to micro-regime
            micro = s["micro"]
            if micro == "TRENDING":
                stop_mult = self.rcfg.initial_stop_atr_mult
            elif micro == "REVERTING":
                stop_mult = 2.0
            else:
                stop_mult = 2.5
            stop = round(max(entry - stop_mult * atr_val, 0.01), 2)

            # KELLY: cap at 12% (from Meta Raptor), scaled by conviction + market
            base_kelly = self.rcfg.kelly_fraction * (0.5 + min(abs(s["t"]) / 3.0, 1.0))
            kelly = float(np.clip(base_kelly * market_scale, 0.02, 0.12))

            if regime == "BEARISH":
                kelly *= self.rcfg.reduce_in_bearish

            # Leverage check
            delta = bars["close"].diff()
            gain = delta.clip(lower=0).ewm(span=5, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(span=5, adjust=False).mean()
            rsi_raw = float((100 - 100 / (1 + gain / (loss + 1e-10))).iloc[-1])
            bb_z = raw[sym]["bollinger_z"]
            leverage = self.f.check_leverage(bars, spy_bars, rsi_raw, bb_z)
            if leverage and abs(s["t"]) >= 2.0:
                kelly = min(kelly * 2.0, 0.20)

            pctile = scipy_stats.percentileofscore(comp_arr, s["comp"]) / 100.0

            atr_p = raw[sym].get("atr_pctile", 0)
            hold = max(1, min(30, int(16 + 14 * (atr_p if not (isinstance(atr_p, float) and np.isnan(atr_p)) else 0))))

            rev_m = raw[sym].get("rev_momentum", 0)
            conf = "reversal_momentum" if (isinstance(rev_m, (int, float)) and not np.isnan(rev_m) and rev_m > 0.5) else "adaptive"

            signals.append(Signal(
                symbol=sym, side="BUY",
                composite_score=round(s["comp"], 4),
                composite_percentile=round(pctile, 4),
                t_statistic=round(s["t"], 4),
                factor_scores={fn: round(zmat[sym][fn], 4) for fn in FACTOR_NAMES},
                factor_contributions=s["contribs"],
                factors_positive=sum(1 for fn in FACTOR_NAMES if zmat[sym][fn] > 0),
                regime=f"{regime}/{micro}",
                sentiment_score=0.0,
                atr=round(atr_val, 4),
                entry_price=entry,
                stop_price=stop,
                take_profit=0.0,
                kelly_fraction=round(kelly, 4),
                hold_target_days=hold,
                leverage_qualified=leverage,
                confirmation_type=conf,
                timestamp=str(bars.index[-1]),
            ))

        signals.sort(key=lambda x: x.composite_score, reverse=True)
        signals = signals[:self.cfg.execution.max_orders_per_scan]

        rc = {}
        for m in micros.values():
            rc[m] = rc.get(m, 0) + 1
        logger.info("v5.4 Signals: %d from %d | Macro=%s Scale=%.1f | Micro=%s",
                     len(signals), len(raw), regime, market_scale, rc)

        return signals

    def get_diagnostics(self, bars_dict, macro_data, sentiment_dict, spy_bars=None):
        signals = self.generate_signals(bars_dict, macro_data, sentiment_dict, spy_bars)
        regime = macro_data.get("regime", "NEUTRAL")
        scale = self._market_scale(spy_bars)

        L = []
        L.append("=" * 80)
        L.append("  RAPTOR v5.4 MERGED ADAPTIVE DIAGNOSTICS")
        L.append("=" * 80)
        L.append(f"  Macro: {regime} | Market scale: {scale:.1f}x")
        L.append(f"  Signals: {len(signals)}")
        L.append("")
        if signals:
            for s in signals:
                L.append(f"  {s.symbol:8s}  comp={s.composite_score:+.4f}  t={s.t_statistic:+.3f}  "
                         f"kelly={s.kelly_fraction:.3f}  stop=${s.stop_price:.2f}  "
                         f"[{s.regime}] [{s.confirmation_type}]")
                top_c = sorted(s.factor_contributions.items(), key=lambda x: -abs(x[1]))
                for fn, c in top_c[:8]:
                    if abs(c) > 0.001:
                        L.append(f"      {fn:18s}  z={s.factor_scores.get(fn,0):+6.3f}  c={c:+.6f}")
                L.append("")
        else:
            L.append("  No signals. Patience.")
        L.append("=" * 80)
        return "\n".join(L)
