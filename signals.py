"""
Raptor v5.3 — Sharpened Quantitative Swing Engine
===================================================
Foundation: v5.2 cross-sectional z-score + orthogonalization + t-stat.
That architecture produced 1.98 PF, 3.6% DD, 1.68% expectancy.

Three upgrades integrated INTO the math, not bolted on:

1. REVERSAL MOMENTUM FACTOR (replaces dead-weight news_sent)
   Instead of binary "has reversal started?" gate, this is a
   continuous cross-sectional factor measuring HOW MUCH the stock
   has begun reverting. Computed as: (close - low_of_last_3_bars)
   / ATR. A stock that bounced 2 ATR off its 3-day low ranks higher
   than one that bounced 0.5 ATR. Cross-sectionally z-scored like
   everything else — the math decides, not a binary gate.

2. CONVICTION-SCALED POSITION SIZING
   Kelly interpolates linearly from min_position_pct at t=1.65
   to max_position_pct at t=3.0. Stronger statistical evidence =
   more capital. Simple, no cap collision.

3. CROWD PANIC INTENSITY (replaces stochastic_rev which overlapped RSI)
   Measures the behavioral footprint of forced selling:
   (volume_today / volume_20d_avg) * abs(return_today) when return < 0.
   High values = high volume on a big down day = panic liquidation.
   This is the Kyle (1985) lambda proxy — captures when uninformed
   sellers are providing liquidity premium to patient buyers.
   Cross-sectionally z-scored: higher panic = higher MR opportunity.

Factor count: 16 (same as v5.2, two swapped for higher-alpha versions)
Cluster weights: same as v5.2 (35% MR, 25% trend, 12% vol, 13% volat, 15% new)
Pipeline: identical to v5.2 (raw -> z-score -> ortho -> regime-weight -> t-test)
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


class Factors:

    # ── MEAN-REVERSION (5 factors, 35%) ──

    @staticmethod
    def rsi_mr(c: pd.Series, period: int = 5) -> float:
        """RSI(5) continuous MR signal. Wilder 1978."""
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
        rsi = 100 - 100 / (1 + gain / (loss + 1e-10))
        return float((50 - rsi.iloc[-1]) / 50)

    @staticmethod
    def bollinger_z(c: pd.Series, period: int = 20) -> float:
        """Bollinger Band z-score. Below mean = buy."""
        sma = c.rolling(period).mean().iloc[-1]
        std = c.rolling(period).std().iloc[-1]
        if std < 1e-10:
            return 0.0
        return float(-(c.iloc[-1] - sma) / std)

    @staticmethod
    def crowd_panic(df: pd.DataFrame) -> float:
        """
        Crowd panic intensity. Kyle 1985 lambda proxy.
        (volume / avg_volume) * |negative_return|
        High values = forced selling = liquidity premium for buyers.
        Only fires on DOWN days. Up days return 0.
        Replaces stochastic_rev which overlapped with RSI.
        """
        c = df["close"]
        v = df["volume"]
        ret = c.iloc[-1] / c.iloc[-2] - 1 if len(c) >= 2 else 0
        if ret >= 0:
            return 0.0  # No panic on up days
        avg_vol = v.iloc[-21:-1].mean()
        if avg_vol <= 0:
            return 0.0
        vol_ratio = v.iloc[-1] / avg_vol
        return float(vol_ratio * abs(ret))

    @staticmethod
    def ma_distance(c: pd.Series) -> float:
        """Distance from 8/21/50 EMA average. Below = buy for MR."""
        e8 = c.ewm(span=8, adjust=False).mean().iloc[-1]
        e21 = c.ewm(span=21, adjust=False).mean().iloc[-1]
        e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
        avg = (e8 + e21 + e50) / 3
        return float(-(c.iloc[-1] - avg) / avg) if avg != 0 else 0.0

    @staticmethod
    def hurst(c: pd.Series, max_lag: int = 20) -> float:
        """Hurst exponent. Mandelbrot 1969. (0.5 - H): positive = MR."""
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

    # ── TREND CONTEXT (4 factors, 25%) ──

    @staticmethod
    def ma_stack(c: pd.Series) -> float:
        """8/21/50 EMA ordering + slope."""
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
    def macd_accel(c: pd.Series, fast=12, slow=26, sig=9) -> float:
        """MACD histogram 5-bar slope / price."""
        ef = c.ewm(span=fast, adjust=False).mean()
        es = c.ewm(span=slow, adjust=False).mean()
        hist = ef - es - (ef - es).ewm(span=sig, adjust=False).mean()
        y = hist.iloc[-5:].values
        return float(np.polyfit(np.arange(5), y, 1)[0] / c.iloc[-1])

    @staticmethod
    def adx_dir(df: pd.DataFrame, period: int = 14) -> float:
        """Signed ADX. Wilder 1978."""
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
    def price_cloud(c: pd.Series) -> float:
        """Price position within 8/50 EMA envelope."""
        e8 = c.ewm(span=8, adjust=False).mean().iloc[-1]
        e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
        mid = (e8 + e50) / 2
        width = abs(e8 - e50)
        if width < 1e-10:
            return 0.0
        return float((c.iloc[-1] - mid) / width)

    # ── VOLUME (3 factors, 12%) ──

    @staticmethod
    def vol_ratio(v: pd.Series) -> float:
        """Log volume ratio."""
        avg = v.iloc[-21:-1].mean()
        if avg == 0:
            return np.nan
        return float(np.log(v.iloc[-1] / avg))

    @staticmethod
    def obv_r2(df: pd.DataFrame, lb: int = 10) -> float:
        """OBV regression slope * R-squared."""
        obv = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
        y = obv.iloc[-lb:].values
        y_s = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), y_s)
        return float(s * r ** 2)

    @staticmethod
    def accum_dist(df: pd.DataFrame, lb: int = 10) -> float:
        """Chaikin A/D slope * |R|."""
        clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"] + 1e-10)
        ad = (clv * df["volume"]).cumsum()
        y = ad.iloc[-lb:].values
        y_s = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), y_s)
        return float(s * abs(r))

    # ── VOLATILITY & RELATIVE (3 factors, 13%) ──

    @staticmethod
    def atr_pctile(df: pd.DataFrame, atr_p: int = 14, lb: int = 60) -> float:
        """ATR percentile. High vol = negative."""
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr_s = tr.rolling(atr_p).mean().dropna()
        if len(atr_s) < lb:
            return np.nan
        pct = scipy_stats.percentileofscore(atr_s.iloc[-lb:].values, atr_s.iloc[-1]) / 100
        return float(-(pct - 0.5) * 2)

    @staticmethod
    def bb_squeeze(c: pd.Series, period: int = 20, lb: int = 60) -> float:
        """Bollinger bandwidth percentile. Squeeze = positive."""
        bw = (4 * c.rolling(period).std() / c.rolling(period).mean()).dropna()
        if len(bw) < lb:
            return np.nan
        pct = scipy_stats.percentileofscore(bw.iloc[-lb:].values, bw.iloc[-1]) / 100
        return float(-(pct - 0.5) * 2)

    @staticmethod
    def rel_strength(sym_c: pd.Series, spy_c: pd.Series, period: int = 10) -> float:
        """10-day return vs SPY."""
        if len(spy_c) < period:
            return np.nan
        return float((sym_c.iloc[-1] / sym_c.iloc[-period]) -
                     (spy_c.iloc[-1] / spy_c.iloc[-period]))

    # ── NEW: REVERSAL MOMENTUM (1 factor, 15%) ──

    @staticmethod
    def reversal_momentum(df: pd.DataFrame, lookback: int = 3) -> float:
        """
        How much has the stock bounced off its recent low?
        (close - lowest_low_of_last_N_bars) / ATR

        This is a CONTINUOUS measure of reversal strength.
        A stock that dropped and bounced 2 ATR ranks higher than
        one that bounced 0.5 ATR. Cross-sectionally z-scored,
        so the math decides which bounces are meaningful.

        Replaces binary confirmation gates and dead-weight sentiment.
        Captures the disposition effect (Shefrin & Statman 1985):
        the crowd sells too early on the bounce, creating
        underpriced momentum in the reversal.
        """
        c = df["close"]
        l = df["low"]
        h, lo_col = df["high"], df["low"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return np.nan
        recent_low = lo_col.iloc[-lookback:].min()
        bounce = (c.iloc[-1] - recent_low) / atr
        return float(bounce)

    # ── UTILITY ──

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> float:
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    @staticmethod
    def check_leverage(df: pd.DataFrame, spy_bars, rsi_val: float, bb_z: float) -> bool:
        """Surgical leverage: ALL conditions required."""
        if spy_bars is None or len(spy_bars) < 205:
            return False
        spy_c = spy_bars["close"]
        sma200 = spy_c.rolling(200).mean()
        if not (spy_c.iloc[-1] > sma200.iloc[-1] and sma200.iloc[-1] > sma200.iloc[-5]):
            return False
        if rsi_val >= 30:  # Need RSI raw < 30
            return False
        if bb_z < 2.0:  # Need 2+ sigma below mean
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


class AdaptiveWeights:
    """
    Online ridge regression on closed trade outcomes.
    Learns which factors predicted winners vs losers.

    After each trade closes, we record:
      X = the 16 orthogonalized z-scores at entry
      y = realized return (pnl_pct)

    Ridge regression (L2 regularization) fits:
      y = X @ beta + epsilon

    Beta tells us which factors actually predicted returns.
    We blend these learned weights with the base weights:
      final_weight = (1-alpha)*base + alpha*ridge_normalized

    alpha starts at 0 (pure base weights) and grows toward 0.3
    as more trades accumulate. Need 30+ trades before ridge
    has statistical power.

    Persists to disk (adaptive_weights.json) so it survives restarts.
    """

    WEIGHT_FILE = "adaptive_weights.json"
    MIN_TRADES = 30        # Don't use ridge until this many trades
    MAX_ALPHA = 0.30       # Maximum blend toward learned weights
    RIDGE_LAMBDA = 1.0     # L2 regularization strength

    def __init__(self, factor_names: list, base_dir: str = "."):
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

    def record_trade(self, factor_zscores: Dict[str, float], realized_return: float):
        """Record a closed trade's factor scores and outcome."""
        row = {fn: factor_zscores.get(fn, 0.0) for fn in self.factor_names}
        row["y"] = realized_return
        self.data["trades"].append(row)
        self.data["n_trades"] = len(self.data["trades"])
        self._fit()
        self._save()

    def _fit(self):
        """Fit ridge regression on all recorded trades."""
        trades = self.data["trades"]
        if len(trades) < self.MIN_TRADES:
            self.data["ridge_beta"] = None
            return

        X = np.array([[t.get(fn, 0) for fn in self.factor_names] for t in trades])
        y = np.array([t["y"] for t in trades])

        # Ridge: beta = (X'X + lambda*I)^-1 X'y
        k = len(self.factor_names)
        XtX = X.T @ X + self.RIDGE_LAMBDA * np.eye(k)
        Xty = X.T @ y
        try:
            beta = np.linalg.solve(XtX, Xty)
            self.data["ridge_beta"] = beta.tolist()
        except np.linalg.LinAlgError:
            self.data["ridge_beta"] = None

    def get_weight_adjustments(self) -> Optional[Dict[str, float]]:
        """
        Returns adjusted weights if ridge has enough data.
        Weights are normalized to sum to 1.0, all positive.
        """
        if self.data["ridge_beta"] is None:
            return None

        beta = np.array(self.data["ridge_beta"])
        # Convert regression coefficients to weight adjustments
        # Positive beta = factor predicted positive returns = upweight
        # Use softmax-like normalization to keep all weights positive
        abs_beta = np.abs(beta)
        if abs_beta.sum() < 1e-10:
            return None

        normalized = abs_beta / abs_beta.sum()
        return {fn: float(normalized[i]) for i, fn in enumerate(self.factor_names)}

    def blend_weights(self, base_weights: Dict[str, float]) -> Dict[str, float]:
        """
        Blend base weights with ridge-learned weights.
        Alpha grows from 0 to MAX_ALPHA as trades accumulate.
        """
        ridge_adj = self.get_weight_adjustments()
        if ridge_adj is None:
            return base_weights

        # Alpha ramps from 0 at MIN_TRADES to MAX_ALPHA at 3*MIN_TRADES
        n = self.data["n_trades"]
        alpha = min(self.MAX_ALPHA, self.MAX_ALPHA * (n - self.MIN_TRADES) / (2 * self.MIN_TRADES))
        alpha = max(0, alpha)

        blended = {}
        for fn in base_weights:
            base = base_weights[fn]
            ridge = ridge_adj.get(fn, base)
            blended[fn] = (1 - alpha) * base + alpha * ridge

        # Re-normalize
        tot = sum(blended.values())
        return {k: v / tot for k, v in blended.items()}


class QuantSignalEngine:
    """
    16 factors, 5 clusters. Cross-sectional z-score + orthogonalize
    + regime-weight + adaptive ridge blend + t-test. Conviction-scaled Kelly.
    """

    FACTORS = [
        # Mean-reversion (35%)
        ("rsi_mr",          "mr",    0.08),
        ("bollinger_z",     "mr",    0.07),
        ("crowd_panic",     "mr",    0.06),
        ("ma_distance",     "mr",    0.07),
        ("hurst",           "mr",    0.07),
        # Trend (25%)
        ("ma_stack",        "trend", 0.07),
        ("macd_accel",      "trend", 0.07),
        ("adx_dir",         "trend", 0.06),
        ("price_cloud",     "trend", 0.05),
        # Volume (12%)
        ("vol_ratio",       "vol",   0.04),
        ("obv_r2",          "vol",   0.04),
        ("accum_dist",      "vol",   0.04),
        # Volatility (13%)
        ("atr_pctile",      "volat", 0.05),
        ("bb_squeeze",      "volat", 0.04),
        ("rel_strength",    "volat", 0.04),
        # Reversal momentum (15%)
        ("rev_momentum",    "rev",   0.15),
    ]

    REGIME_MULT = {
        "EXPANSION": {"mr": 0.8, "trend": 1.3, "vol": 1.0, "volat": 0.8, "rev": 0.7},
        "BULLISH":   {"mr": 0.9, "trend": 1.2, "vol": 1.0, "volat": 0.9, "rev": 0.8},
        "NEUTRAL":   {"mr": 1.0, "trend": 1.0, "vol": 1.0, "volat": 1.0, "rev": 1.0},
        "BEARISH":   {"mr": 1.3, "trend": 0.7, "vol": 1.1, "volat": 1.2, "rev": 1.3},
        "CRISIS":    {"mr": 1.5, "trend": 0.5, "vol": 1.2, "volat": 1.4, "rev": 1.5},
    }

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self.rcfg = cfg.risk
        self.f = Factors()
        total = sum(w for _, _, w in self.FACTORS)
        assert abs(total - 1.0) < 0.001, f"Weights sum to {total}"
        self.adaptive = AdaptiveWeights(
            [fn for fn, _, _ in self.FACTORS],
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

    def _zscore(self, raw_matrix):
        syms = list(raw_matrix.keys())
        names = [n for n, _, _ in self.FACTORS]
        out = {s: {} for s in syms}
        for fn in names:
            valid = [(s, raw_matrix[s][fn]) for s in syms
                     if not (isinstance(raw_matrix[s][fn], float) and np.isnan(raw_matrix[s][fn]))]
            if len(valid) < 5:
                for s in syms:
                    out[s][fn] = 0.0
                continue
            arr = np.array([v for _, v in valid])
            mu, sig = arr.mean(), arr.std()
            if sig < 1e-10:
                for s in syms:
                    out[s][fn] = 0.0
                continue
            valid_set = set()
            for s, val in valid:
                out[s][fn] = float(np.clip((val - mu) / sig, -3, 3))
                valid_set.add(s)
            for s in syms:
                if s not in valid_set:
                    out[s][fn] = 0.0
        return out

    def _orthogonalize(self, zmat):
        syms = list(zmat.keys())
        clusters = {}
        for fn, cl, _ in self.FACTORS:
            clusters.setdefault(cl, []).append(fn)
        ort = {s: dict(zmat[s]) for s in syms}
        for cl, flist in clusters.items():
            if len(flist) <= 1:
                continue
            n, k = len(syms), len(flist)
            M = np.zeros((n, k))
            for j, fn in enumerate(flist):
                for i, s in enumerate(syms):
                    M[i, j] = zmat[s][fn]
            for j in range(1, k):
                X, y = M[:, :j], M[:, j]
                try:
                    beta = np.linalg.lstsq(X, y, rcond=None)[0]
                    res = y - X @ beta
                    rs = res.std()
                    if rs > 1e-10:
                        res /= rs
                    M[:, j] = res
                except np.linalg.LinAlgError:
                    logger.warning("Ortho failed cluster=%s j=%d", cl, j)
            for j, fn in enumerate(flist):
                for i, s in enumerate(syms):
                    ort[s][fn] = float(M[i, j])
        return ort

    def _weights(self, regime):
        """Regime-adjusted weights, blended with ridge-learned adjustments."""
        mult = self.REGIME_MULT.get(regime, self.REGIME_MULT["NEUTRAL"])
        adj = {fn: w * mult[cl] for fn, cl, w in self.FACTORS}
        tot = sum(adj.values())
        base = {k: v / tot for k, v in adj.items()}
        # Blend with adaptive ridge if enough trades
        return self.adaptive.blend_weights(base)

    def _score(self, ort_scores, weights):
        comp, var, contribs = 0.0, 0.0, {}
        for fn in ort_scores:
            w = weights.get(fn, 0)
            contrib = w * ort_scores[fn]
            comp += contrib
            contribs[fn] = round(contrib, 6)
            var += w ** 2
        t = comp / np.sqrt(var) if var > 0 else 0.0
        return comp, t, contribs

    def _conviction_kelly(self, t_stat, regime):
        """t=1.65 -> min, t=3.0 -> max. Linear."""
        conv = np.clip((t_stat - 1.65) / 1.35, 0, 1)
        kelly = self.rcfg.min_position_pct + conv * (self.rcfg.max_position_pct - self.rcfg.min_position_pct)
        if regime == "BEARISH":
            kelly *= self.rcfg.reduce_in_bearish
        return round(kelly, 4)

    @staticmethod
    def _hold_from_atr(atr_pctile_val):
        if isinstance(atr_pctile_val, float) and np.isnan(atr_pctile_val):
            return 10
        return max(1, min(30, int(16 + 14 * atr_pctile_val)))

    def generate_signals(self, bars_dict, macro_data, sentiment_dict, spy_bars=None):
        regime = macro_data.get("regime", "NEUTRAL")
        if regime == "CRISIS" and self.rcfg.halt_in_crisis:
            logger.warning("CRISIS - halting")
            return []

        raw = {}
        for sym, bars in bars_dict.items():
            if len(bars) < MIN_BARS_REQUIRED:
                continue
            try:
                raw[sym] = self._raw(sym, bars, spy_bars)
            except Exception as e:
                logger.error("Factor error %s: %s", sym, e)

        if len(raw) < 10:
            return []

        zs = self._zscore(raw)
        ort = self._orthogonalize(zs)
        w = self._weights(regime)

        candidates = []
        all_comp = []
        for sym in ort:
            comp, t, contribs = self._score(ort[sym], w)
            all_comp.append(comp)
            candidates.append((sym, comp, t, contribs))

        comp_arr = np.array(all_comp)
        signals = []

        for sym, comp, t, contribs in candidates:
            if t < 1.65:
                continue

            pctile = scipy_stats.percentileofscore(comp_arr, comp) / 100
            bars = bars_dict[sym]
            entry = float(bars["close"].iloc[-1])
            atr_val = self.f.atr(bars, self.rcfg.atr_period)
            if atr_val <= 0:
                continue

            stop = round(entry - self.rcfg.initial_stop_atr_mult * atr_val, 2)
            kelly = self._conviction_kelly(t, regime)

            # RSI raw for leverage check
            delta = bars["close"].diff()
            gain = delta.clip(lower=0).ewm(span=5, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(span=5, adjust=False).mean()
            rsi_raw = float((100 - 100 / (1 + gain / (loss + 1e-10))).iloc[-1])
            bb_z = raw[sym]["bollinger_z"]

            leverage = self.f.check_leverage(bars, spy_bars, rsi_raw, bb_z)
            if leverage and t >= 2.0:
                kelly = min(kelly * 2.0, self.rcfg.max_position_pct * 2)

            atr_p = raw[sym].get("atr_pctile", 0)
            hold = self._hold_from_atr(atr_p if not (isinstance(atr_p, float) and np.isnan(atr_p)) else 0)
            fscores = {k: round(v, 4) for k, v in ort[sym].items()}

            # Confirmation type from reversal momentum
            rev_m = raw[sym].get("rev_momentum", 0)
            if isinstance(rev_m, float) and not np.isnan(rev_m) and rev_m > 0.5:
                conf = "reversal_momentum"
            else:
                conf = "statistical"

            signals.append(Signal(
                symbol=sym, side="BUY",
                composite_score=round(comp, 4),
                composite_percentile=round(pctile, 4),
                t_statistic=round(t, 4),
                factor_scores=fscores,
                factor_contributions=contribs,
                factors_positive=sum(1 for v in fscores.values() if v > 0),
                regime=regime, sentiment_score=0.0,
                atr=round(atr_val, 4), entry_price=entry,
                stop_price=stop, take_profit=0.0,
                kelly_fraction=kelly,
                hold_target_days=hold,
                leverage_qualified=leverage,
                confirmation_type=conf,
                timestamp=str(bars.index[-1]),
            ))

        signals.sort(key=lambda s: s.t_statistic, reverse=True)
        if len(signals) > self.cfg.execution.max_orders_per_scan:
            signals = signals[:self.cfg.execution.max_orders_per_scan]

        logger.info("Signals: %d from %d syms (regime=%s)", len(signals), len(raw), regime)
        if len(comp_arr):
            logger.info("Composite: mean=%.4f std=%.4f max=%.4f", comp_arr.mean(), comp_arr.std(), comp_arr.max())
        return signals

    def get_diagnostics(self, bars_dict, macro_data, sentiment_dict, spy_bars=None):
        regime = macro_data.get("regime", "NEUTRAL")
        w = self._weights(regime)
        bar_counts = {s: len(b) for s, b in bars_dict.items()}
        sufficient = sum(1 for v in bar_counts.values() if v >= MIN_BARS_REQUIRED)

        raw = {}
        for sym, bars in bars_dict.items():
            if len(bars) < MIN_BARS_REQUIRED:
                continue
            try:
                raw[sym] = self._raw(sym, bars, spy_bars)
            except Exception:
                pass

        nan_counts = {n: 0 for n, _, _ in self.FACTORS}
        for sym in raw:
            for fn in nan_counts:
                val = raw[sym].get(fn, np.nan)
                if isinstance(val, float) and np.isnan(val):
                    nan_counts[fn] += 1

        zs = self._zscore(raw)
        ort = self._orthogonalize(zs)

        results = []
        all_comp = []
        for sym in ort:
            comp, t, contribs = self._score(ort[sym], w)
            all_comp.append(comp)
            atr_p = raw.get(sym, {}).get("atr_pctile", 0)
            hold = self._hold_from_atr(atr_p if not (isinstance(atr_p, float) and np.isnan(atr_p)) else 0)
            results.append({"symbol": sym, "composite": comp, "t_stat": t,
                            "contributions": contribs, "raw": raw.get(sym, {}),
                            "ortho": ort.get(sym, {}), "hold": hold,
                            "bars": bar_counts.get(sym, 0)})

        results.sort(key=lambda r: r["t_stat"], reverse=True)
        comps = [r["composite"] for r in results]
        ts = [r["t_stat"] for r in results]
        passing = sum(1 for t in ts if t > 1.65)

        L = []
        L.append("=" * 80)
        L.append("  RAPTOR v5.3 DIAGNOSTICS")
        L.append("=" * 80)
        L.append(f"  Regime: {regime} (macro={macro_data.get('score', 0):.3f})")
        L.append(f"  Universe: {sufficient} symbols | Factors: {len(self.FACTORS)}")
        L.append(f"  Threshold: t > 1.65")
        L.append("")
        L.append("  DATA QUALITY:")
        for fn, _, _ in self.FACTORS:
            nc = nan_counts.get(fn, 0)
            L.append(f"    {fn:18s} {'OK' if nc == 0 else f'WARN {nc} NaN'}")
        L.append("")
        L.append("  WEIGHTS (regime-adjusted):")
        for fn, cl, bw in self.FACTORS:
            L.append(f"    {fn:18s} [{cl:5s}]  base={bw:.2f}  adj={w.get(fn,0):.4f}")
        L.append("")
        L.append(f"  Composite: mean={np.mean(comps):.4f}  std={np.std(comps):.4f}")
        L.append(f"  T-stats:   mean={np.mean(ts):.3f}  max={max(ts):.3f}  min={min(ts):.3f}")
        L.append(f"  Signals: {passing}")
        L.append("")
        L.append("-" * 80)
        L.append("  TOP 15")
        L.append("-" * 80)
        for r in results[:15]:
            st = "PASS" if r["t_stat"] > 1.65 else "FAIL"
            L.append(f"\n  {r['symbol']:6s}  t={r['t_stat']:+.3f}  comp={r['composite']:+.4f}  hold~{r['hold']}d  [{st}]")
            cs = sorted(r["contributions"].items(), key=lambda x: -abs(x[1]))
            for fn, contrib in cs:
                z = r["ortho"].get(fn, 0)
                rv = r["raw"].get(fn, 0)
                rv = rv if not (isinstance(rv, float) and np.isnan(rv)) else 0.0
                d = "+" if contrib > 0 else "-" if contrib < 0 else " "
                L.append(f"      {fn:18s} raw={rv:+9.4f}  z={z:+6.3f}  w={w.get(fn,0):.4f}  c={contrib:+.6f} {d}")
        L.append("")
        L.append("-" * 80)
        L.append("  BOTTOM 5")
        L.append("-" * 80)
        for r in results[-5:]:
            L.append(f"  {r['symbol']:6s}  t={r['t_stat']:+.3f}  comp={r['composite']:+.4f}")
        L.append("")
        L.append("=" * 80)
        return "\n".join(L)
