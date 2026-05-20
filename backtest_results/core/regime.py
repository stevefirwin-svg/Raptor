"""
RAPTOR REGIME DETECTOR v4.1  —  core/regime.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Macro regime classification from observable market data.

WHY THIS MATTERS:
  Your signal engine is regime-aware at the MICRO level (Hurst exponent).
  But it has zero awareness of the MACRO environment. A momentum signal
  in a risk-off crash behaves completely differently than in a bull market.

  Ray Dalio's Bridgewater framework classifies every moment into one of
  four macro regimes, then tilts the entire portfolio accordingly:

  ┌──────────────┬──────────────────────────────────────────────────────┐
  │ Regime       │ Market characteristics                              │
  ├──────────────┼──────────────────────────────────────────────────────┤
  │ RISK_ON      │ Falling VIX, positive breadth, bull trend           │
  │ RISK_OFF     │ Rising VIX, negative breadth, bear trend            │
  │ CHOPPY       │ VIX oscillating, no clear trend, mean-reversion     │
  │ TRENDING     │ Low VIX, strong trend, momentum works               │
  └──────────────┴──────────────────────────────────────────────────────┘

Observable signals used (all free via yfinance):
  VIX level + term structure (VIX vs VIX3M)
  SPY trend (20-day vs 50-day EMA)
  SPY realized volatility vs implied
  Breadth: advance-decline proxy via QQQ/SPY ratio

Each regime adjusts:
  - Which types of signals to trust (momentum vs reversion)
  - Position sizing aggressiveness
  - Sector rotation preferences
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

import numpy as np

logger = logging.getLogger("Raptor.Regime")


class MacroRegimeDetector:
    """
    Classifies the current macro environment into one of four regimes.
    Updates every 30 minutes. Uses freely available market data.
    """

    def __init__(self, refresh_minutes: int = 30):
        self.refresh_minutes = refresh_minutes
        self._regime: str = "CHOPPY"  # default safe assumption
        self._confidence: float = 0.0
        self._signals: Dict[str, float] = {}
        self._last_update = datetime.min.replace(tzinfo=timezone.utc)

    def update(self):
        """Refresh regime classification from market data."""
        now = datetime.now(timezone.utc)
        if (now - self._last_update).total_seconds() < self.refresh_minutes * 60:
            return

        try:
            import yfinance as yf

            # Fetch SPY and VIX data
            spy = yf.download("SPY", period="3mo", interval="1d",
                              auto_adjust=True, progress=False)
            vix = yf.download("^VIX", period="3mo", interval="1d",
                              auto_adjust=True, progress=False)

            if spy is None or len(spy) < 30 or vix is None or len(vix) < 10:
                self._last_update = now
                return

            spy_close = spy["Close"].values.flatten() if hasattr(spy["Close"], 'values') \
                else spy["Close"].values
            vix_close = vix["Close"].values.flatten() if hasattr(vix["Close"], 'values') \
                else vix["Close"].values

            # ── Signal 1: SPY Trend (EMA 20 vs EMA 50) ───────────────
            ema20 = self._ema(spy_close, 20)
            ema50 = self._ema(spy_close, 50)
            if ema50 > 0:
                trend_signal = (ema20 - ema50) / ema50  # positive = bullish
            else:
                trend_signal = 0.0
            self._signals["spy_trend"] = trend_signal

            # ── Signal 2: VIX Level ──────────────────────────────────
            current_vix = float(vix_close[-1])
            vix_ma = float(np.mean(vix_close[-20:]))
            vix_signal = (current_vix - vix_ma) / (vix_ma + 1e-9)
            self._signals["vix_deviation"] = vix_signal
            self._signals["vix_level"] = current_vix

            # ── Signal 3: VIX Velocity (5-day change) ────────────────
            if len(vix_close) >= 6:
                vix_velocity = (vix_close[-1] - vix_close[-6]) / (vix_close[-6] + 1e-9)
            else:
                vix_velocity = 0.0
            self._signals["vix_velocity"] = vix_velocity

            # ── Signal 4: SPY Momentum (10-day return) ───────────────
            if len(spy_close) >= 11:
                spy_momentum = (spy_close[-1] - spy_close[-11]) / spy_close[-11]
            else:
                spy_momentum = 0.0
            self._signals["spy_momentum"] = spy_momentum

            # ── Signal 5: Realized vs Implied Vol ────────────────────
            if len(spy_close) >= 21:
                daily_returns = np.diff(np.log(spy_close[-21:]))
                realized_vol = float(np.std(daily_returns) * np.sqrt(252) * 100)
                vol_ratio = current_vix / (realized_vol + 1e-9)
                # vol_ratio > 1.2 = fear premium (market pricing in more risk)
                # vol_ratio < 0.8 = complacency
            else:
                vol_ratio = 1.0
            self._signals["vol_ratio"] = vol_ratio

            # ── Classify Regime ──────────────────────────────────────
            self._classify()
            self._last_update = now

            logger.info(
                f"Regime: {self._regime} (conf={self._confidence:.0%}) | "
                f"VIX={current_vix:.1f} trend={trend_signal:+.3f} "
                f"mom={spy_momentum:+.3f}"
            )

        except Exception as e:
            logger.debug(f"Regime update error: {e}")
            self._last_update = now

    def _classify(self):
        """
        Classify regime based on signal ensemble.
        Uses a simple voting system — each signal votes for a regime.
        """
        vix_level = self._signals.get("vix_level", 20)
        vix_velocity = self._signals.get("vix_velocity", 0)
        spy_trend = self._signals.get("spy_trend", 0)
        spy_momentum = self._signals.get("spy_momentum", 0)
        vol_ratio = self._signals.get("vol_ratio", 1.0)

        votes = {"RISK_ON": 0, "RISK_OFF": 0, "TRENDING": 0, "CHOPPY": 0}

        # VIX level
        if vix_level < 15:
            votes["TRENDING"] += 2
            votes["RISK_ON"] += 1
        elif vix_level < 20:
            votes["RISK_ON"] += 1
        elif vix_level < 28:
            votes["CHOPPY"] += 1
        else:
            votes["RISK_OFF"] += 2

        # VIX velocity (rising VIX = risk off)
        if vix_velocity > 0.10:
            votes["RISK_OFF"] += 2
        elif vix_velocity > 0.03:
            votes["RISK_OFF"] += 1
        elif vix_velocity < -0.05:
            votes["RISK_ON"] += 1
            votes["TRENDING"] += 1

        # SPY trend
        if spy_trend > 0.02:
            votes["RISK_ON"] += 1
            votes["TRENDING"] += 1
        elif spy_trend < -0.02:
            votes["RISK_OFF"] += 1
        else:
            votes["CHOPPY"] += 1

        # SPY momentum
        if spy_momentum > 0.03:
            votes["RISK_ON"] += 1
            votes["TRENDING"] += 1
        elif spy_momentum < -0.03:
            votes["RISK_OFF"] += 1
        else:
            votes["CHOPPY"] += 1

        # Vol ratio (fear premium)
        if vol_ratio > 1.3:
            votes["RISK_OFF"] += 1
        elif vol_ratio < 0.85:
            votes["RISK_ON"] += 1

        # Winner
        total_votes = sum(votes.values())
        winner = max(votes, key=votes.get)
        self._confidence = votes[winner] / (total_votes + 1e-9)
        self._regime = winner

    def _ema(self, data: np.ndarray, period: int) -> float:
        """Simple EMA of the last value."""
        if len(data) < period:
            return float(data[-1])
        alpha = 2.0 / (period + 1)
        ema = data[0]
        for val in data[1:]:
            ema = alpha * val + (1 - alpha) * ema
        return float(ema)

    @property
    def regime(self) -> str:
        return self._regime

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def signals(self) -> Dict[str, float]:
        return self._signals.copy()

    def strategy_adjustments(self) -> Dict:
        """
        Return parameter adjustments for the current regime.
        The signal engine and risk manager use these to adapt.
        """
        adjustments = {
            "RISK_ON": {
                "prefer_momentum": True,
                "prefer_reversion": False,
                "kelly_mult": 1.15,     # slightly more aggressive
                "min_score_adj": -0.03,  # lower bar in strong markets
                "sector_tilt": ["TECH", "GROWTH", "SEMICONDUCTOR"],
            },
            "RISK_OFF": {
                "prefer_momentum": False,
                "prefer_reversion": True,  # fade oversold bounces
                "kelly_mult": 0.60,        # much more conservative
                "min_score_adj": +0.05,    # higher bar in fear
                "sector_tilt": ["ENERGY", "DEFENSE", "HEALTHCARE"],
            },
            "TRENDING": {
                "prefer_momentum": True,
                "prefer_reversion": False,
                "kelly_mult": 1.10,
                "min_score_adj": -0.02,
                "sector_tilt": [],  # follow whatever's trending
            },
            "CHOPPY": {
                "prefer_momentum": False,
                "prefer_reversion": True,
                "kelly_mult": 0.85,
                "min_score_adj": +0.03,
                "sector_tilt": ["INDEX"],  # trade ETFs in chop
            },
        }
        return adjustments.get(self._regime, adjustments["CHOPPY"])
