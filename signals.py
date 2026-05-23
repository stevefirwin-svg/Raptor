"""
Raptor v5.5 — Dual-Book Signal Engine
======================================
Two separate, non-conflicting signal books:

  Book 1 — MOMENTUM: Trend continuation entries on pullbacks.
            Factors: ma_stack, macd_accel, adx_dir, rel_strength,
                     obv_r2, accum_dist, price_cloud, vol_ratio
            Gate:    micro == TRENDING, ADX > 25, price > 50 EMA
            Entry:   pullback to 8/21 EMA on declining volume
            Exit:    wide trail, momentum_break path, no fixed target

  Book 2 — MEAN_REVERSION: Panic-low entries, reversion to mean.
            Factors: rsi_mr, bollinger_z, crowd_panic, ma_distance,
                     bb_squeeze, rev_momentum, atr_pctile
            Gate:    RSI(5) < 30, price below lower BB, volume spike
            Entry:   exhaustion candle + divergence confirmation
            Exit:    tight trail targeting 20-day mean, 5-day hold cap

Bottom/Top Detector: Bulkowski-validated candlestick patterns +
                     RSI divergence. Shared across both books.

Research basis:
  - Jegadeesh & Titman (1993) — momentum persistence
  - Asness, Moskowitz & Pedersen (2013) — momentum everywhere
  - De Bondt & Thaler (1985) — mean reversion from overreaction
  - Bulkowski (2008) — candlestick pattern reliability database
  - Grinold & Kahn — conviction-proportional allocation
  - Wilder (1978) — RSI divergence as reversal signal
"""

import json, logging, os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from config import RaptorConfig

logger = logging.getLogger("raptor.signals")
MIN_BARS_REQUIRED = 80

# ── Trade type constants ───────────────────────────────────────────────────────
MOMENTUM       = "MOMENTUM"
MEAN_REVERSION = "MEAN_REVERSION"

# ── Factor name registries per book ───────────────────────────────────────────
MOMENTUM_FACTORS = [
    "ma_stack", "macd_accel", "adx_dir", "rel_strength",
    "obv_r2", "accum_dist", "price_cloud", "vol_ratio",
]
MR_FACTORS = [
    "rsi_mr", "bollinger_z", "crowd_panic", "ma_distance",
    "bb_squeeze", "rev_momentum", "atr_pctile",
]
FACTOR_NAMES = MOMENTUM_FACTORS + MR_FACTORS  # Full registry for AdaptiveWeights

# ── Signal dataclass — extended with trade identity ────────────────────────────
@dataclass
class Signal:
    symbol:              str
    side:                str
    trade_type:          str   # MOMENTUM | MEAN_REVERSION
    composite_score:     float
    book_conviction:     float # conviction within own book (0-1)
    composite_percentile:float
    t_statistic:         float
    factor_scores:       Dict[str, float]
    factor_contributions:Dict[str, float]
    factors_positive:    int
    regime:              str
    pattern_signal:      str   # candlestick/divergence pattern detected, or ""
    sentiment_score:     float
    atr:                 float
    entry_price:         float
    stop_price:          float
    take_profit:         float
    kelly_fraction:      float
    hold_target_days:    int
    leverage_qualified:  bool
    confirmation_type:   str
    timestamp:           str


# ══════════════════════════════════════════════════════════════════════════════
# RAW FACTOR COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

class Factors:
    """All raw factor computations. Stateless — all inputs explicit."""

    # ── Mean Reversion factors ─────────────────────────────────────────────────
    @staticmethod
    def rsi_mr(c, period=5):
        """RSI mean-reversion score. Positive when oversold."""
        d = c.diff()
        g = d.clip(lower=0).ewm(span=period, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(span=period, adjust=False).mean()
        rsi = 100 - 100 / (1 + g / (l + 1e-10))
        return float((50 - rsi.iloc[-1]) / 50)

    @staticmethod
    def rsi_raw(c, period=14):
        """Raw RSI value 0-100."""
        d = c.diff()
        g = d.clip(lower=0).ewm(span=period, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(span=period, adjust=False).mean()
        return float(100 - 100 / (1 + g / (l + 1e-10)).iloc[-1])

    @staticmethod
    def bollinger_z(c, period=20):
        """Negative z-score vs Bollinger mid. Positive when below lower band."""
        m = c.rolling(period).mean().iloc[-1]
        s = c.rolling(period).std().iloc[-1]
        return float(-(c.iloc[-1] - m) / s) if s > 1e-10 else 0.0

    @staticmethod
    def crowd_panic(df):
        """Volume-weighted panic score. High = exhaustion selling."""
        c, v = df["close"], df["volume"]
        av = v.iloc[-21:-1].mean()
        if av <= 0:
            return 0.0
        p = 0.0
        for i in [-1, -2, -3]:
            if len(c) < abs(i) + 1:
                continue
            r = c.iloc[i] / c.iloc[i-1] - 1
            if r < 0:
                p += (v.iloc[i] / av) * abs(r)
        return float(p)

    @staticmethod
    def ma_distance(c):
        """Distance below moving average composite. Positive when below EMAs."""
        e8  = c.ewm(span=8,  adjust=False).mean().iloc[-1]
        e21 = c.ewm(span=21, adjust=False).mean().iloc[-1]
        e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
        a = (e8 + e21 + e50) / 3
        return float(-(c.iloc[-1] - a) / a) if a != 0 else 0.0

    @staticmethod
    def bb_squeeze(c, period=20, lb=60):
        """Bollinger bandwidth percentile. High = squeeze = volatility compression."""
        bw = (4 * c.rolling(period).std() / c.rolling(period).mean()).dropna()
        if len(bw) < lb:
            return np.nan
        return float(-(scipy_stats.percentileofscore(bw.iloc[-lb:].values, bw.iloc[-1]) / 100 - 0.5) * 2)

    @staticmethod
    def reversal_momentum(df, lookback=3):
        """Price recovery from recent low relative to ATR. MR entry strength."""
        c, lo, hi = df["close"], df["low"], df["high"]
        tr = pd.concat([hi - lo,
                        (hi - c.shift(1)).abs(),
                        (lo - c.shift(1)).abs()], axis=1).max(axis=1)
        a = tr.rolling(14).mean().iloc[-1]
        if pd.isna(a) or a <= 0:
            return np.nan
        return float((c.iloc[-1] - lo.iloc[-lookback:].min()) / a)

    @staticmethod
    def atr_pctile(df, atr_p=14, lb=60):
        """ATR percentile. High = volatility spike = potential exhaustion."""
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l,
                        (h - c.shift(1)).abs(),
                        (l - c.shift(1)).abs()], axis=1).max(axis=1)
        a = tr.rolling(atr_p).mean().dropna()
        if len(a) < lb:
            return np.nan
        # For MR: HIGH atr_pctile = volatility spike = positive signal
        return float((scipy_stats.percentileofscore(a.iloc[-lb:].values, a.iloc[-1]) / 100 - 0.5) * 2)

    # ── Momentum factors ───────────────────────────────────────────────────────
    @staticmethod
    def ma_stack(c):
        """EMA alignment score. Positive when 8 > 21 > 50."""
        e8  = c.ewm(span=8,  adjust=False).mean()
        e21 = c.ewm(span=21, adjust=False).mean()
        e50 = c.ewm(span=50, adjust=False).mean()
        order = float((e8.iloc[-1] > e21.iloc[-1]) + (e21.iloc[-1] > e50.iloc[-1]) - 1)
        slope = np.clip(sum((e.iloc[-1] / e.iloc[-5] - 1) for e in [e8, e21, e50]) / 3 * 50, -0.4, 0.4)
        return float(order * 0.6 + slope)

    @staticmethod
    def macd_accel(c, fast=12, slow=26, sig=9):
        """MACD histogram slope — positive = accelerating momentum."""
        ef = c.ewm(span=fast, adjust=False).mean()
        es = c.ewm(span=slow, adjust=False).mean()
        h  = ef - es - (ef - es).ewm(span=sig, adjust=False).mean()
        return float(np.polyfit(np.arange(5), h.iloc[-5:].values, 1)[0] / c.iloc[-1])

    @staticmethod
    def adx_dir(df, period=14):
        """Signed ADX. Positive when uptrend has structure."""
        h, l, c = df["high"], df["low"], df["close"]
        pdm = h.diff().clip(lower=0)
        mdm = (-l.diff()).clip(lower=0)
        pdm[pdm < mdm] = 0.0
        mdm[mdm < pdm] = 0.0
        tr  = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        a   = tr.ewm(span=period, adjust=False).mean()
        pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / a
        mdi = 100 * mdm.ewm(span=period, adjust=False).mean() / a
        dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
        adx = dx.ewm(span=period, adjust=False).mean()
        return float(adx.iloc[-1] * (1.0 if pdi.iloc[-1] > mdi.iloc[-1] else -1.0))

    @staticmethod
    def adx_raw(df, period=14):
        """Raw ADX value (unsigned trend strength)."""
        h, l, c = df["high"], df["low"], df["close"]
        pdm = h.diff().clip(lower=0)
        mdm = (-l.diff()).clip(lower=0)
        pdm[pdm < mdm] = 0.0
        mdm[mdm < pdm] = 0.0
        tr  = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        a   = tr.ewm(span=period, adjust=False).mean()
        pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / a
        mdi = 100 * mdm.ewm(span=period, adjust=False).mean() / a
        dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
        return float(dx.ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def rel_strength(sym_c, spy_c, period=10):
        """Relative strength vs SPY. Positive = outperforming."""
        if len(spy_c) < period:
            return np.nan
        return float((sym_c.iloc[-1] / sym_c.iloc[-period]) - (spy_c.iloc[-1] / spy_c.iloc[-period]))

    @staticmethod
    def obv_r2(df, lb=10):
        """OBV trend slope * R². Positive = volume confirming uptrend."""
        obv = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
        y   = obv.iloc[-lb:].values
        ys  = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), ys)
        return float(s * r ** 2)

    @staticmethod
    def accum_dist(df, lb=10):
        """Accumulation/distribution slope. Positive = institutional accumulation."""
        clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"] + 1e-10)
        ad  = (clv * df["volume"]).cumsum()
        y   = ad.iloc[-lb:].values
        ys  = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), ys)
        return float(s * abs(r))

    @staticmethod
    def price_cloud(c):
        """Position relative to EMA cloud midpoint. Positive = above cloud."""
        e8  = c.ewm(span=8,  adjust=False).mean().iloc[-1]
        e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
        w   = abs(e8 - e50)
        return float((c.iloc[-1] - (e8 + e50) / 2) / w) if w > 1e-10 else 0.0

    @staticmethod
    def vol_ratio(v):
        """Log volume ratio vs 20-day avg. Positive = above-average volume."""
        a = v.iloc[-21:-1].mean()
        return float(np.log(v.iloc[-1] / a)) if a > 0 else np.nan

    # ── Shared utilities ───────────────────────────────────────────────────────
    @staticmethod
    def hurst(c, max_lag=20):
        """Hurst exponent. >0.5 = trending, <0.5 = reverting."""
        r = np.log(c / c.shift(1)).dropna().values
        if len(r) < max_lag * 2:
            return np.nan
        pts = []
        for lag in range(2, max_lag + 1):
            ns = len(r) // lag
            if ns < 1:
                continue
            rl = []
            for i in range(ns):
                sub = r[i*lag:(i+1)*lag]
                d = np.cumsum(sub - sub.mean())
                R = d.max() - d.min()
                S = sub.std()
                if S > 1e-10:
                    rl.append(R / S)
            if rl:
                pts.append((np.log(lag), np.log(np.mean(rl))))
        if len(pts) < 4:
            return np.nan
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        return float(np.polyfit(x, y, 1)[0])  # raw Hurst H

    @staticmethod
    def atr(df, period=14):
        """ATR value."""
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    @staticmethod
    def check_leverage(df, spy_bars, rsi_val, bb_z):
        """Leveraged ETF qualification check."""
        if spy_bars is None or len(spy_bars) < 205:
            return False
        spy_c  = spy_bars["close"]
        sma200 = spy_c.rolling(200).mean()
        if not (spy_c.iloc[-1] > sma200.iloc[-1] and sma200.iloc[-1] > sma200.iloc[-5]):
            return False
        if rsi_val >= 30 or bb_z < 2.0:
            return False
        c, h, l = df["close"], df["high"], df["low"]
        ema20   = c.ewm(span=20, adjust=False).mean()
        tr      = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        kl      = ema20 - 1.5 * tr.rolling(14).mean()
        if c.iloc[-1] >= kl.iloc[-1]:
            return False
        av = df["volume"].iloc[-21:-1].mean()
        if av <= 0 or df["volume"].iloc[-1] / av < 1.5:
            return False
        return True


# ══════════════════════════════════════════════════════════════════════════════
# BOTTOM / TOP DETECTOR
# Bulkowski (2008) validated patterns. Only patterns with >60% reversal rate.
# ══════════════════════════════════════════════════════════════════════════════

class BottomTopDetector:
    """
    Detects candlestick-based and divergence-based reversal signals.
    Returns a string label or "" if no pattern detected.

    Bulkowski reliability rates used as inclusion threshold (>60%):
      Hammer:              60.4%  — bottom
      Bullish Engulfing:   63.0%  — bottom
      Morning Star:        61.5%  — bottom
      Piercing Line:       62.1%  — bottom
      Three White Soldiers:65.4%  — bottom (rare)
      Bearish Engulfing:   60.6%  — top
      Evening Star:        61.8%  — top
      Dark Cloud Cover:    62.3%  — top
      Three Black Crows:   65.1%  — top (rare)
      Shooting Star:       60.8%  — top

    RSI divergence (Wilder 1978): not in Bulkowski but empirically
    confirmed in academic literature as high-quality reversal signal.
    """

    @staticmethod
    def _body(o, c):
        return abs(c - o)

    @staticmethod
    def _range(h, l):
        return h - l + 1e-10

    @classmethod
    def detect_bottom(cls, df) -> str:
        """
        Returns pattern name if a bottom formation is detected, else "".
        Checks last completed candle (iloc[-1]) and prior candles as needed.
        """
        if len(df) < 4:
            return ""
        o  = df["open"].values
        h  = df["high"].values
        l  = df["low"].values
        c  = df["close"].values
        v  = df["volume"].values

        # Shorthand for last 3 bars
        o1, h1, l1, c1 = o[-1], h[-1], l[-1], c[-1]  # most recent
        o2, h2, l2, c2 = o[-2], h[-2], l[-2], c[-2]  # prior
        o3, h3, l3, c3 = o[-3], h[-3], l[-3], c[-3]  # two bars ago

        avg_vol = np.mean(v[-21:-1]) if len(v) > 21 else np.mean(v[:-1])
        body1   = cls._body(o1, c1)
        rng1    = cls._range(h1, l1)
        body2   = cls._body(o2, c2)

        # ── Hammer (Bulkowski 60.4%) ──────────────────────────────────────────
        # Bullish body small, lower shadow >= 2x body, upper shadow tiny,
        # appears after downtrend, above-average volume preferred
        lower_shadow1 = min(o1, c1) - l1
        upper_shadow1 = h1 - max(o1, c1)
        if (body1 > 0 and
                lower_shadow1 >= 2.0 * body1 and
                upper_shadow1 <= 0.3 * body1 and
                c2 < o2 and  # prior candle was bearish
                v[-1] >= avg_vol * 0.8):
            return "hammer"

        # ── Bullish Engulfing (Bulkowski 63.0%) ───────────────────────────────
        # Current bullish candle body fully engulfs prior bearish body,
        # prior close < prior open (bearish), current close > prior open
        if (c1 > o1 and       # current bullish
                c2 < o2 and   # prior bearish
                o1 < c2 and   # open below prior close
                c1 > o2 and   # close above prior open
                body1 > body2 and
                v[-1] >= avg_vol * 1.0):
            return "bullish_engulfing"

        # ── Morning Star (Bulkowski 61.5%) ────────────────────────────────────
        # 3-bar: bearish candle, small doji/star body gap down, bullish candle
        body_mid = cls._body(o2, c2)
        if (c3 < o3 and                          # bar-3 bearish
                body_mid <= 0.3 * cls._body(o3, c3) and  # bar-2 small body (star)
                c1 > o1 and                       # bar-1 bullish
                c1 > (o3 + c3) / 2 and           # closes above midpoint of bar-3
                v[-1] >= avg_vol * 0.8):
            return "morning_star"

        # ── Piercing Line (Bulkowski 62.1%) ───────────────────────────────────
        # Bearish prior candle, current opens below prior low, closes above midpoint
        if (c2 < o2 and          # prior bearish
                o1 < l2 and      # open below prior low (gap down)
                c1 > o1 and      # current bullish
                c1 > (o2 + c2) / 2 and  # closes above midpoint
                c1 < o2):        # but below prior open (not full engulf)
            return "piercing_line"

        # ── Three White Soldiers (Bulkowski 65.4%) ────────────────────────────
        # 3 consecutive bullish candles each closing near high, each higher
        if (c1 > o1 and c2 > o2 and c3 > o3 and       # all bullish
                c1 > c2 > c3 and                        # each higher close
                o1 > o2 > o3 and                        # each higher open
                (h1 - c1) <= 0.2 * cls._body(o1, c1) and  # closes near high
                (h2 - c2) <= 0.2 * cls._body(o2, c2)):
            return "three_white_soldiers"

        # ── RSI Bullish Divergence (Wilder 1978) ─────────────────────────────
        # Price makes lower low, RSI makes higher low — hidden buying pressure
        if len(df) >= 10:
            prices = df["close"].values
            closes_series = pd.Series(prices)
            d = closes_series.diff()
            g = d.clip(lower=0).ewm(span=5, adjust=False).mean()
            lo = (-d.clip(upper=0)).ewm(span=5, adjust=False).mean()
            rsi_series = 100 - 100 / (1 + g / (lo + 1e-10))
            rsi_vals = rsi_series.values
            # Compare last two local lows (simplified: last bar vs 5 bars ago)
            if (prices[-1] < prices[-6] and         # price: lower low
                    rsi_vals[-1] > rsi_vals[-6] and  # RSI: higher low
                    rsi_vals[-1] < 45):              # still in oversold zone
                return "rsi_bull_divergence"

        return ""

    @classmethod
    def detect_top(cls, df) -> str:
        """
        Returns pattern name if a top formation is detected, else "".
        Used for exit timing on momentum positions.
        """
        if len(df) < 4:
            return ""
        o = df["open"].values
        h = df["high"].values
        l = df["low"].values
        c = df["close"].values
        v = df["volume"].values

        o1, h1, l1, c1 = o[-1], h[-1], l[-1], c[-1]
        o2, h2, l2, c2 = o[-2], h[-2], l[-2], c[-2]
        o3, h3, l3, c3 = o[-3], h[-3], l[-3], c[-3]

        avg_vol = np.mean(v[-21:-1]) if len(v) > 21 else np.mean(v[:-1])
        body1   = cls._body(o1, c1)
        body2   = cls._body(o2, c2)

        upper_shadow1 = h1 - max(o1, c1)
        lower_shadow1 = min(o1, c1) - l1

        # ── Shooting Star (Bulkowski 60.8%) ───────────────────────────────────
        if (body1 > 0 and
                upper_shadow1 >= 2.0 * body1 and
                lower_shadow1 <= 0.3 * body1 and
                c2 > o2 and
                v[-1] >= avg_vol * 0.8):
            return "shooting_star"

        # ── Bearish Engulfing (Bulkowski 60.6%) ───────────────────────────────
        if (c1 < o1 and
                c2 > o2 and
                o1 > c2 and
                c1 < o2 and
                body1 > body2 and
                v[-1] >= avg_vol * 1.0):
            return "bearish_engulfing"

        # ── Evening Star (Bulkowski 61.8%) ────────────────────────────────────
        body_mid = cls._body(o2, c2)
        if (c3 > o3 and
                body_mid <= 0.3 * cls._body(o3, c3) and
                c1 < o1 and
                c1 < (o3 + c3) / 2 and
                v[-1] >= avg_vol * 0.8):
            return "evening_star"

        # ── Dark Cloud Cover (Bulkowski 62.3%) ────────────────────────────────
        if (c2 > o2 and
                o1 > h2 and
                c1 < o1 and
                c1 < (o2 + c2) / 2 and
                c1 > o2):
            return "dark_cloud_cover"

        # ── Three Black Crows (Bulkowski 65.1%) ───────────────────────────────
        if (c1 < o1 and c2 < o2 and c3 < o3 and
                c1 < c2 < c3 and
                o1 < o2 < o3 and
                (l1 - c1) <= 0.2 * cls._body(o1, c1) and
                (l2 - c2) <= 0.2 * cls._body(o2, c2)):
            return "three_black_crows"

        # ── RSI Bearish Divergence ─────────────────────────────────────────────
        if len(df) >= 10:
            prices = df["close"].values
            closes_series = pd.Series(prices)
            d = closes_series.diff()
            g = d.clip(lower=0).ewm(span=14, adjust=False).mean()
            lo = (-d.clip(upper=0)).ewm(span=14, adjust=False).mean()
            rsi_series = 100 - 100 / (1 + g / (lo + 1e-10))
            rsi_vals = rsi_series.values
            if (prices[-1] > prices[-6] and
                    rsi_vals[-1] < rsi_vals[-6] and
                    rsi_vals[-1] > 60):
                return "rsi_bear_divergence"

        return ""


# ══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE WEIGHTS (unchanged — ridge regression + IC boost)
# ══════════════════════════════════════════════════════════════════════════════

class AdaptiveWeights:
    WEIGHT_FILE  = "adaptive_weights.json"
    MIN_TRADES   = 30
    MAX_ALPHA    = 0.30
    RIDGE_LAMBDA = 1.0

    def __init__(self, factor_names, base_dir="."):
        self.factor_names = factor_names
        self.path         = os.path.join(base_dir, self.WEIGHT_FILE)
        self.data         = self._load()
        self._ic_cache    = None

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {"trades": [], "ridge_beta": None, "n_trades": 0}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def record_trade(self, zscores, ret):
        row = {fn: zscores.get(fn, 0.0) for fn in self.factor_names}
        row["y"] = ret
        self.data["trades"].append(row)
        self.data["n_trades"] = len(self.data["trades"])
        self._fit()
        self._save()

    def _get_ic_boost(self):
        n = len(self.data["trades"])
        if n < 20:
            return {}
        if self._ic_cache and self._ic_cache[0] == n:
            return self._ic_cache[1]
        recent = self.data["trades"][-50:]
        ic = {fn: sum(1 for t in recent if t.get(fn, 0) * t.get("y", 0) > 0) / len(recent) - 0.5
              for fn in self.factor_names}
        self._ic_cache = (n, ic)
        return ic

    def _fit(self):
        t = self.data["trades"]
        if len(t) < self.MIN_TRADES:
            self.data["ridge_beta"] = None
            return
        X = np.array([[tr.get(fn, 0) for fn in self.factor_names] for tr in t])
        y = np.array([tr["y"] for tr in t])
        k = len(self.factor_names)
        try:
            self.data["ridge_beta"] = np.linalg.solve(
                X.T @ X + self.RIDGE_LAMBDA * np.eye(k), X.T @ y
            ).tolist()
        except Exception:
            self.data["ridge_beta"] = None

    def blend_weights(self, base, book_factors):
        """Blend base weights with ridge + IC, restricted to book_factors."""
        relevant = {fn: base[fn] for fn in book_factors if fn in base}
        if self.data["ridge_beta"] is None and not self.data.get("ic_weights"):
            return relevant
        blended = dict(relevant)
        n = self.data["n_trades"]
        if self.data["ridge_beta"] is not None:
            b   = np.abs(np.array([self.data["ridge_beta"][self.factor_names.index(fn)]
                                   for fn in book_factors if fn in self.factor_names]))
            if b.sum() > 1e-10:
                norm = b / b.sum()
                ra   = {fn: float(norm[i]) for i, fn in enumerate(book_factors)}
                a    = min(self.MAX_ALPHA, self.MAX_ALPHA * (n - self.MIN_TRADES) / (2 * self.MIN_TRADES))
                a    = max(0, a)
                blended = {fn: (1 - a) * relevant[fn] + a * ra.get(fn, relevant[fn]) for fn in book_factors}
        ic_boost = self._get_ic_boost()
        if ic_boost:
            blended = {fn: blended.get(fn, 0) * (1.0 + ic_boost.get(fn, 0)) for fn in book_factors}
        tot = sum(blended.values())
        return {fn: v / tot for fn, v in blended.items()} if tot > 1e-10 else relevant


# ══════════════════════════════════════════════════════════════════════════════
# MOMENTUM SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class MomentumSignalEngine:
    """
    Generates momentum/trend-continuation signals.

    Gate requirements (all must pass):
      - Hurst H > 0.55 (trending) OR ADX > 25 (structured trend)
      - Price above 50 EMA
      - Positive relative strength vs SPY (10-day)
      - EMA stack: 8 > 21 or 21 > 50 (at minimum partial alignment)

    Entry timing: pullback — price near 8 or 21 EMA on declining volume.
    Top detector used for signal suppression near exhaustion.
    """

    GATE_ADX_MIN      = 22.0   # ADX threshold for trend structure
    GATE_HURST_MIN    = 0.52   # Hurst H threshold (trending)
    PULLBACK_EMA_SPAN = 21     # EMA to measure pullback proximity

    def __init__(self):
        self.f   = Factors()
        self.btd = BottomTopDetector()

    def score(self, sym: str, bars: pd.DataFrame, spy_bars: Optional[pd.DataFrame],
              zscores: Dict[str, float], adx_val: float, hurst_h: float) -> Optional[Dict]:
        """
        Returns scored momentum candidate dict or None if gates not met.
        zscores: pre-computed cross-sectional z-scores for MOMENTUM_FACTORS.
        """
        c   = bars["close"]
        e8  = c.ewm(span=8,  adjust=False).mean()
        e21 = c.ewm(span=21, adjust=False).mean()
        e50 = c.ewm(span=50, adjust=False).mean()
        price = c.iloc[-1]

        # ── Hard gates ────────────────────────────────────────────────────────
        trend_confirmed = (hurst_h > self.GATE_HURST_MIN) or (adx_val > self.GATE_ADX_MIN)
        if not trend_confirmed:
            return None
        if price < e50.iloc[-1]:   # Must be above 50 EMA
            return None

        spy_c = spy_bars["close"] if spy_bars is not None else pd.Series(dtype=float)
        rs = self.f.rel_strength(c, spy_c, period=10)
        if not isinstance(rs, float) or np.isnan(rs) or rs < -0.01:
            return None  # Must have positive or near-neutral RS

        # ── Pullback quality — price near 8 or 21 EMA ─────────────────────────
        dist_e8  = abs(price - e8.iloc[-1])  / price
        dist_e21 = abs(price - e21.iloc[-1]) / price
        pullback_quality = 1.0 - min(dist_e8, dist_e21)  # higher = closer to EMA

        # ── Top detection — suppress signal if exhaustion forming ─────────────
        top_pattern = self.btd.detect_top(bars)
        if top_pattern in ("bearish_engulfing", "evening_star", "three_black_crows"):
            return None  # Hard suppress on high-reliability top patterns

        # ── Composite score (momentum factors only) ───────────────────────────
        z = {fn: zscores.get(fn, 0.0) for fn in MOMENTUM_FACTORS}
        # Equal base weights, blended with inverse-vol
        base_w = {fn: 1.0 / len(MOMENTUM_FACTORS) for fn in MOMENTUM_FACTORS}
        # Apply pullback quality as a multiplier on vol_ratio (confirms pullback on low vol)
        base_w["vol_ratio"] *= max(0.5, 1.0 - pullback_quality)  # lower vol on pullback = better

        wt   = sum(base_w.values())
        base_w = {fn: v / wt for fn, v in base_w.items()}
        comp = sum(z[fn] * base_w[fn] for fn in MOMENTUM_FACTORS)

        return {
            "sym":           sym,
            "comp":          comp,
            "book":          MOMENTUM,
            "top_pattern":   top_pattern,
            "pullback_quality": pullback_quality,
            "adx":           adx_val,
            "hurst_h":       hurst_h,
            "z":             z,
        }


# ══════════════════════════════════════════════════════════════════════════════
# MEAN REVERSION SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class MeanReversionSignalEngine:
    """
    Generates mean-reversion signals from panic/oversold extremes.

    Gate requirements (all must pass):
      - RSI(5) < 35 (oversold — threshold calibrated for 5-day RSI)
      - Price below 20-day Bollinger lower band OR below 20-day SMA
      - crowd_panic > 0 (volume spike on down days)
      - Bottom pattern detected (Bulkowski pattern OR RSI divergence)

    Entry: panic exhaustion + at least one reversal confirmation.
    Hard cap: 5-day hold target. Exit at 20-day mean or earlier.
    """

    GATE_RSI_MAX    = 35.0   # RSI(5) must be below this
    GATE_BB_MAX     = 0.0    # bollinger_z must be positive (below band)
    GATE_PANIC_MIN  = 0.005  # minimum crowd_panic score

    def __init__(self):
        self.f   = Factors()
        self.btd = BottomTopDetector()

    def score(self, sym: str, bars: pd.DataFrame, spy_bars: Optional[pd.DataFrame],
              zscores: Dict[str, float], rsi5_val: float, bb_z_val: float,
              panic_val: float) -> Optional[Dict]:
        """
        Returns scored MR candidate dict or None if gates not met.
        """
        c = bars["close"]

        # ── Hard gates ────────────────────────────────────────────────────────
        if rsi5_val > self.GATE_RSI_MAX:
            return None
        if bb_z_val < self.GATE_BB_MAX:   # bb_z positive = below band
            return None
        if panic_val < self.GATE_PANIC_MIN:
            return None

        # ── Bottom pattern required ───────────────────────────────────────────
        bottom_pattern = self.btd.detect_bottom(bars)
        # Allow entry even without candle pattern if RSI divergence is present
        # (divergence alone is sufficient confirmation per Wilder)
        if not bottom_pattern:
            return None

        # ── Mean reversion target: 20-day SMA ────────────────────────────────
        sma20  = float(c.rolling(20).mean().iloc[-1])
        price  = float(c.iloc[-1])
        dist_to_mean = (sma20 - price) / price  # how far below mean (positive = upside)
        if dist_to_mean < 0.005:   # less than 0.5% below mean — not enough reversion room
            return None

        # ── Composite score (MR factors only) ────────────────────────────────
        z      = {fn: zscores.get(fn, 0.0) for fn in MR_FACTORS}
        base_w = {fn: 1.0 / len(MR_FACTORS) for fn in MR_FACTORS}
        # Boost pattern-confirmed signals
        pattern_boost = 1.3 if bottom_pattern in (
            "bullish_engulfing", "morning_star", "three_white_soldiers"
        ) else 1.0
        wt     = sum(base_w.values())
        base_w = {fn: v / wt for fn, v in base_w.items()}
        comp   = sum(z[fn] * base_w[fn] for fn in MR_FACTORS) * pattern_boost

        return {
            "sym":            sym,
            "comp":           comp,
            "book":           MEAN_REVERSION,
            "bottom_pattern": bottom_pattern,
            "dist_to_mean":   dist_to_mean,
            "rsi5":           rsi5_val,
            "z":              z,
        }


# ══════════════════════════════════════════════════════════════════════════════
# COMPOSITE RANKER — unified conviction score across both books
# ══════════════════════════════════════════════════════════════════════════════

class CompositeRanker:
    """
    Scores and ranks signals from both books on a unified conviction scale.
    Grinold & Kahn: allocation proportional to information ratio per signal.
    No fixed per-book quotas — conviction drives allocation.
    """

    @staticmethod
    def rank(momentum_candidates: List[Dict],
             mr_candidates: List[Dict],
             max_signals: int = 10) -> List[Dict]:
        """
        Normalize each book's scores to [0, 1] within-book (percentile rank),
        then combine into a unified list sorted by conviction.
        Returns top max_signals entries with book label preserved.
        """
        def _normalize(candidates):
            if not candidates:
                return []
            scores = [c["comp"] for c in candidates]
            mn, mx = min(scores), max(scores)
            rng = mx - mn if mx > mn else 1.0
            for c in candidates:
                c["book_conviction"] = (c["comp"] - mn) / rng
            return candidates

        mom_norm = _normalize(momentum_candidates)
        mr_norm  = _normalize(mr_candidates)

        combined = mom_norm + mr_norm
        combined.sort(key=lambda x: x["book_conviction"], reverse=True)
        return combined[:max_signals]


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SIGNAL ENGINE — public API (backward compatible)
# ══════════════════════════════════════════════════════════════════════════════

class QuantSignalEngine:
    """
    Public API — wraps both books. Downstream files import this class only.
    generate_signals() returns List[Signal] as before, now with trade_type field.
    """

    def __init__(self, cfg: RaptorConfig):
        self.cfg    = cfg
        self.rcfg   = cfg.risk
        self.mom_engine = MomentumSignalEngine()
        self.mr_engine  = MeanReversionSignalEngine()
        self.ranker     = CompositeRanker()
        self.f          = Factors()
        self.adaptive   = AdaptiveWeights(FACTOR_NAMES, os.path.dirname(os.path.abspath(__file__)))
        self._last_full_signals: Dict[str, Signal] = {}

    def _market_scale(self, spy_bars) -> float:
        if spy_bars is None or len(spy_bars) < 21:
            return 1.0
        spy_c  = spy_bars["close"]
        roc_20 = (spy_c.iloc[-1] / spy_c.iloc[-21]) - 1.0
        if len(spy_c) >= 6:
            roc_5 = (spy_c.iloc[-1] / spy_c.iloc[-6]) - 1.0
            if roc_20 > 0.01 and roc_5 < -0.02:
                return 0.5
            if roc_20 < -0.01 and roc_5 > 0.02:
                return 1.0
        if roc_20 > 0.02:  return 1.0
        elif roc_20 > -0.02: return 0.8
        return 0.5

    def _compute_raw(self, sym: str, bars: pd.DataFrame,
                     spy_bars: Optional[pd.DataFrame]) -> Optional[Dict]:
        """Compute all raw factor values for one symbol."""
        c, v = bars["close"], bars["volume"]
        h, l = bars["high"], bars["low"]
        spy_c = spy_bars["close"] if spy_bars is not None else pd.Series(dtype=float)

        try:
            raw = {
                # MR factors
                "rsi_mr":      self.f.rsi_mr(c),
                "bollinger_z": self.f.bollinger_z(c),
                "crowd_panic": self.f.crowd_panic(bars),
                "ma_distance": self.f.ma_distance(c),
                "bb_squeeze":  self.f.bb_squeeze(c),
                "rev_momentum":self.f.reversal_momentum(bars),
                "atr_pctile":  self.f.atr_pctile(bars),
                # Momentum factors
                "ma_stack":    self.f.ma_stack(c),
                "macd_accel":  self.f.macd_accel(c),
                "adx_dir":     self.f.adx_dir(bars),
                "rel_strength":self.f.rel_strength(c, spy_c),
                "obv_r2":      self.f.obv_r2(bars),
                "accum_dist":  self.f.accum_dist(bars),
                "price_cloud": self.f.price_cloud(c),
                "vol_ratio":   self.f.vol_ratio(v),
                # Shared intermediates
                "_adx_raw":    self.f.adx_raw(bars),
                "_hurst_h":    self.f.hurst(c),    # raw H value
                "_rsi5_val":   self.f.rsi_raw(c, period=5),
                "_atr":        self.f.atr(bars),
            }
        except Exception as e:
            logger.debug("Raw compute failed %s: %s", sym, e)
            return None
        return raw

    def _crosssectional_z(self, syms: List[str],
                           raw: Dict[str, Dict]) -> Dict[str, Dict[str, float]]:
        """
        Robust cross-sectional z-scoring (median/MAD) for all factors.
        Returns {sym: {factor: zscore}}.
        """
        zmat = {}
        for fn in FACTOR_NAMES:
            vals = [raw[s].get(fn, np.nan) for s in syms]
            arr  = np.array([v for v in vals if not (isinstance(v, float) and np.isnan(v))])
            if len(arr) < 5:
                for s in syms:
                    zmat.setdefault(s, {})[fn] = 0.0
                continue
            mu  = np.median(arr)
            sig = np.median(np.abs(arr - mu)) * 1.4826
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
        return zmat

    def _orthogonalize(self, syms: List[str],
                        zmat: Dict[str, Dict[str, float]],
                        book_factors: List[str]) -> Dict[str, Dict[str, float]]:
        """
        Factor orthogonalization: replace w^T x with w^T Σ⁻¹ x.
        Removes double-counting of correlated factors (e.g. EMA stack
        and MACD both capture the same trend signal).

        Approach: Σ is computed from the current cross-section.
        We apply Σ^(-1/2) whitening to the factor z-scores per symbol.
        This decorrelates factors so each dimension carries independent info.

        Reference: Grinold & Kahn (Active Portfolio Management) Ch. 6
        Condition number check: if Σ is near-singular (condition > 100),
        fall back to diagonal (variance-only) adjustment to avoid
        numerical instability from a poorly conditioned covariance matrix.

        Requires ≥ len(book_factors) + 5 symbols for stable estimation.
        Falls back to original z-scores if universe is too small.
        """
        n_sym = len(syms)
        n_fac = len(book_factors)

        if n_sym < n_fac + 5:
            return zmat  # Not enough symbols for stable Σ estimation

        # Build factor matrix X: rows=symbols, cols=book_factors
        X = np.array([[zmat[s].get(fn, 0.0) for fn in book_factors]
                      for s in syms], dtype=float)

        # Cross-sectional covariance (Pearson — factors are already z-scored)
        # Add small ridge for numerical stability
        cov = np.cov(X.T) + np.eye(n_fac) * 1e-4

        try:
            cond = np.linalg.cond(cov)
            if cond > 100:
                # Near-singular — use diagonal only (variance adjustment)
                inv_cov = np.diag(1.0 / np.diag(cov))
            else:
                inv_cov = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            return zmat  # Fall back gracefully

        # Apply Σ⁻¹ adjustment: x_orth = Σ⁻¹ x (per symbol)
        # Then normalize rows so scores remain in comparable range
        X_orth = X @ inv_cov
        # Re-scale to maintain interpretability: unit variance across symbols
        col_std = np.std(X_orth, axis=0) + 1e-8
        X_orth  = X_orth / col_std

        # Write back orthogonalized z-scores into zmat copy
        zmat_orth = {s: dict(zmat[s]) for s in syms}  # shallow copy
        for i, s in enumerate(syms):
            for j, fn in enumerate(book_factors):
                zmat_orth[s][fn] = float(np.clip(X_orth[i, j], -3, 3))

        return zmat_orth

    def generate_signals(self, bars_dict: Dict[str, pd.DataFrame],
                         macro_data: Dict, sentiment_dict: Dict,
                         spy_bars: Optional[pd.DataFrame] = None) -> List[Signal]:

        regime       = macro_data.get("regime", "NEUTRAL")
        if regime == "CRISIS" and self.rcfg.halt_in_crisis:
            return []
        market_scale = self._market_scale(spy_bars)

        # ── Step 1: compute raw factors for all symbols ────────────────────────
        raw = {}
        for sym, bars in bars_dict.items():
            if len(bars) < MIN_BARS_REQUIRED:
                continue
            r = self._compute_raw(sym, bars, spy_bars)
            if r is not None:
                raw[sym] = r

        if len(raw) < 10:
            return []

        syms = list(raw.keys())

        # ── Step 2: cross-sectional z-scores ──────────────────────────────────
        zmat = self._crosssectional_z(syms, raw)

        # ── Step 3: orthogonalize per book (Σ⁻¹ whitening) ──────────────────
        # Removes double-counting of correlated factors within each book.
        # Applied independently to momentum and MR factor sets.
        zmat_mom = self._orthogonalize(syms, zmat, MOMENTUM_FACTORS)
        zmat_mr  = self._orthogonalize(syms, zmat, MR_FACTORS)

        # ── Step 4: score each book independently ──────────────────────────────
        mom_candidates = []
        mr_candidates  = []

        for sym in syms:
            bars    = bars_dict[sym]
            r       = raw[sym]
            z       = zmat[sym]
            adx_val = r.get("_adx_raw", 0.0)
            hurst_h = r.get("_hurst_h", 0.5)
            hurst_h = hurst_h if not (isinstance(hurst_h, float) and np.isnan(hurst_h)) else 0.5
            rsi5    = r.get("_rsi5_val", 50.0)
            bb_z    = r.get("bollinger_z", 0.0)
            panic   = r.get("crowd_panic", 0.0)

            # Momentum book — uses orthogonalized z-scores
            z_mom = zmat_mom[sym]
            mom = self.mom_engine.score(sym, bars, spy_bars, z_mom, adx_val, hurst_h)
            if mom is not None and mom["comp"] > 0:
                mom_candidates.append(mom)

            # MR book — uses orthogonalized z-scores
            z_mr = zmat_mr[sym]
            mr = self.mr_engine.score(sym, bars, spy_bars, z_mr, rsi5, bb_z, panic)
            if mr is not None and mr["comp"] > 0:
                mr_candidates.append(mr)

        logger.info("v5.5 Books: MOMENTUM=%d candidates  MEAN_REVERSION=%d candidates  Scale=%.1f",
                    len(mom_candidates), len(mr_candidates), market_scale)

        # ── Step 5: unified ranking ────────────────────────────────────────────
        ranked = self.ranker.rank(mom_candidates, mr_candidates,
                                  max_signals=self.cfg.execution.max_orders_per_scan * 2)

        if not ranked:
            return []

        all_convictions = [c["book_conviction"] for c in ranked]
        conv_arr        = np.array(all_convictions)

        # ── Step 6: build Signal objects ──────────────────────────────────────
        signals = []
        for cand in ranked:
            sym   = cand["sym"]
            bars  = bars_dict[sym]
            r     = raw[sym]
            book  = cand["book"]
            z     = zmat[sym]
            price = float(bars["close"].iloc[-1])
            atr_val = r.get("_atr", 0.0)
            if atr_val <= 0 or price <= 0:
                continue

            # ── Stop placement differs by book ────────────────────────────────
            if book == MOMENTUM:
                # Wider stop — trend needs room
                stop_mult = self.rcfg.initial_stop_atr_mult  # e.g. 2.0
                hold_days = 15
                take_profit = 0.0  # no fixed target — trail exit
                pattern   = cand.get("top_pattern", "")
                conf_type = "momentum_pullback"
            else:
                # Tighter stop — MR has defined target, cut fast if wrong
                stop_mult = 1.5
                # Hold target: distance to mean in days (approx) — cap at 5
                hold_days = min(5, max(2, int(cand.get("dist_to_mean", 0.02) / 0.005)))
                # Take-profit at 20-day SMA
                sma20       = float(bars["close"].rolling(20).mean().iloc[-1])
                take_profit = round(sma20, 2)
                pattern     = cand.get("bottom_pattern", "")
                conf_type   = "mr_exhaustion"

            stop = round(max(price - stop_mult * atr_val, 0.01), 2)

            # ── Kelly sizing by book ───────────────────────────────────────────
            # MR gets smaller Kelly — binary outcome, tighter stop
            base_kelly = self.rcfg.kelly_fraction
            if book == MOMENTUM:
                t_val  = cand["comp"] / (np.std(list(z.values())) + 0.5)
                kelly  = float(np.clip(base_kelly * (0.5 + min(abs(t_val) / 3.0, 1.0))
                                       * market_scale, 0.02, 0.12))
            else:
                kelly  = float(np.clip(base_kelly * 0.6 * market_scale, 0.02, 0.08))

            if regime == "BEARISH":
                kelly *= self.rcfg.reduce_in_bearish

            # ── T-statistic proxy ──────────────────────────────────────────────
            t_stat = cand["comp"] / (np.std(list(z.values())) + 0.5)

            # ── Composite percentile across all candidates ─────────────────────
            pctile = float(scipy_stats.percentileofscore(conv_arr, cand["book_conviction"]) / 100.0)

            # ── Regime label ──────────────────────────────────────────────────
            hurst_h = r.get("_hurst_h", 0.5)
            hurst_h = hurst_h if not (isinstance(hurst_h, float) and np.isnan(hurst_h)) else 0.5
            adx_val_r = r.get("_adx_raw", 0.0)
            if hurst_h > 0.55 and adx_val_r > 25:   micro = "TRENDING"
            elif hurst_h < 0.45 and adx_val_r < 20: micro = "REVERTING"
            else:                                     micro = "MIXED"
            regime_label = f"{regime}/{micro}"

            sig = Signal(
                symbol=sym,
                side="BUY",
                trade_type=book,
                composite_score=round(cand["comp"], 4),
                book_conviction=round(cand["book_conviction"], 4),
                composite_percentile=round(pctile, 4),
                t_statistic=round(t_stat, 4),
                factor_scores={fn: round(z.get(fn, 0.0), 4) for fn in FACTOR_NAMES},
                factor_contributions={fn: round(z.get(fn, 0.0) / len(FACTOR_NAMES), 6)
                                      for fn in FACTOR_NAMES},
                factors_positive=sum(1 for fn in FACTOR_NAMES if z.get(fn, 0) > 0),
                regime=regime_label,
                pattern_signal=pattern,
                sentiment_score=0.0,
                atr=round(atr_val, 4),
                entry_price=price,
                stop_price=stop,
                take_profit=take_profit,
                kelly_fraction=round(kelly, 4),
                hold_target_days=hold_days,
                leverage_qualified=False,
                confirmation_type=conf_type,
                timestamp=str(bars.index[-1]),
            )
            signals.append(sig)

        # ── Store full signal map for hold_monitor / exit_monitor ─────────────
        self._last_full_signals = {s.symbol: s for s in signals}

        # Cap at max_orders_per_scan
        signals = signals[:self.cfg.execution.max_orders_per_scan]

        mom_count = sum(1 for s in signals if s.trade_type == MOMENTUM)
        mr_count  = sum(1 for s in signals if s.trade_type == MEAN_REVERSION)
        logger.info("v5.5 Final signals: %d MOMENTUM  %d MEAN_REVERSION  (scale=%.1f  regime=%s)",
                    mom_count, mr_count, market_scale, regime)

        return signals
