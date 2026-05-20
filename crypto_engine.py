"""
Raptor Crypto Engine v1.0 — BTC & ETH Only
=============================================
Separate engine. Same math where it applies.
1-hour bars, 24/7 operation, 10% of account capital.

RESEARCH BASIS:
  - BTC trends at highs, mean-reverts at lows (QuantPedia 2022/2024)
  - Intraday momentum + reversal both present (Wen et al. 2022)
  - RSI, Bollinger, VWAP reversion most effective factors (Tan 2025)
  - Volatility-adjusted sizing critical (crypto vol is 3-5x equities)

STRATEGY: Dual-regime per asset
  TRENDING (Hurst > 0.55, ADX > 25):
    Buy breakouts above 20-period high, trail with 2.5x ATR
  REVERTING (Hurst < 0.45, RSI < 25):
    Buy dips at lower Bollinger band, target mid-band, tight stop

WHAT'S STRIPPED FROM RAPTOR:
  - No FRED macro regime (irrelevant to crypto)
  - No cross-sectional z-scoring (only 2 assets)
  - No relative strength vs SPY
  - No crowd panic (different volume patterns 24/7)
  - No inverse-vol weighting (need 10+ assets)

WHAT'S KEPT:
  - RSI mean-reversion
  - Bollinger z-score
  - Volume ratio
  - ATR trailing stops (time-dependent)
  - Hurst + ADX regime detection
  - Kelly sizing (vol-adjusted)
  - Momentum break exit

Usage:
  python crypto_engine.py              # Scan + execute
  python crypto_engine.py --dry-run    # Scan only
  python crypto_engine.py --status     # Show positions
  Start_Crypto.bat                     # Loop every 60 min, 24/7
"""

import logging
import os
import sys
import json
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/crypto_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("raptor.crypto")

# =========================================================================
# CONFIG
# =========================================================================
SYMBOLS = ["BTC/USD", "ETH/USD"]
CAPITAL_FRACTION = 0.10          # 10% of account
MAX_POSITION_PCT = 0.06          # 6% per trade (of total account)
KELLY_BASE = 0.12                # Base Kelly fraction
KELLY_CAP = 0.06                 # Max 6% of total account per crypto position
ATR_PERIOD = 14
INITIAL_STOP_ATR = 2.5           # Wider than equities — crypto is volatile
TRAIL_ATR = 2.0                  # Trail multiplier
LOOKBACK_BARS = 100              # 100 hours of history
MIN_BARS = 50
RSI_PERIOD = 14
BB_PERIOD = 20
HURST_LAG = 20
ADX_PERIOD = 14

LEDGER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_ledger.json")

# =========================================================================
# DATA
# =========================================================================
class CryptoData:
    def __init__(self):
        self.api_key = os.getenv("ALPACA_API_KEY", "")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        self._client = None
        self._trading = None

    @property
    def data_client(self):
        if self._client is None:
            from alpaca.data.historical import CryptoHistoricalDataClient
            self._client = CryptoHistoricalDataClient(
                api_key=self.api_key, secret_key=self.secret_key
            )
        return self._client

    @property
    def trading_client(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient
            self._trading = TradingClient(
                api_key=self.api_key, secret_key=self.secret_key, paper=True
            )
        return self._trading

    def get_bars(self, symbol, hours=LOOKBACK_BARS):
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame
        try:
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Hour,
                start=datetime.now() - timedelta(hours=hours + 10),
                end=datetime.now(),
            )
            bars = self.data_client.get_crypto_bars(request)
            if symbol in bars.data and len(bars.data[symbol]) >= MIN_BARS:
                rows = [{
                    "open": float(b.open), "high": float(b.high),
                    "low": float(b.low), "close": float(b.close),
                    "volume": float(b.volume),
                } for b in bars.data[symbol]]
                return pd.DataFrame(rows)
        except Exception as e:
            logger.error("Bar fetch failed %s: %s", symbol, e)
        return None

    def get_account(self):
        acct = self.trading_client.get_account()
        return {"equity": float(acct.equity), "cash": float(acct.cash)}

    def get_positions(self):
        try:
            positions = self.trading_client.get_all_positions()
            return [{
                "symbol": str(p.symbol),
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pnl": float(p.unrealized_pl),
                "unrealized_pnl_pct": float(p.unrealized_plpc),
            } for p in positions if "/" in str(p.symbol) or "BTC" in str(p.symbol) or "ETH" in str(p.symbol)]
        except Exception as e:
            logger.error("Position fetch failed: %s", e)
            return []

    def submit_order(self, symbol, qty, side, order_type="market"):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        try:
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            )
            order = self.trading_client.submit_order(request)
            return {"status": str(order.status), "id": str(order.id)}
        except Exception as e:
            return {"error": str(e)}


# =========================================================================
# SIGNALS — stripped to what works on crypto
# =========================================================================
class CryptoSignals:

    @staticmethod
    def rsi(c, period=RSI_PERIOD):
        d = c.diff()
        g = d.clip(lower=0).ewm(span=period, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(span=period, adjust=False).mean()
        return float((100 - 100 / (1 + g / (l + 1e-10))).iloc[-1])

    @staticmethod
    def bollinger(c, period=BB_PERIOD):
        m = c.rolling(period).mean().iloc[-1]
        s = c.rolling(period).std().iloc[-1]
        if s < 1e-10:
            return 0.0, m, m, m
        z = (c.iloc[-1] - m) / s
        upper = m + 2 * s
        lower = m - 2 * s
        return float(z), float(m), float(upper), float(lower)

    @staticmethod
    def atr(df, period=ATR_PERIOD):
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    @staticmethod
    def vol_ratio(v):
        avg = v.iloc[-21:-1].mean()
        return float(v.iloc[-1] / avg) if avg > 0 else 1.0

    @staticmethod
    def hurst(c, max_lag=HURST_LAG):
        r = np.log(c / c.shift(1)).dropna().values
        if len(r) < max_lag * 2:
            return 0.5
        pts = []
        for lag in range(2, max_lag + 1):
            ns = len(r) // lag
            if ns < 1:
                continue
            rl = []
            for i in range(ns):
                sub = r[i * lag:(i + 1) * lag]
                d = np.cumsum(sub - sub.mean())
                R = d.max() - d.min()
                S = sub.std()
                if S > 1e-10:
                    rl.append(R / S)
            if rl:
                pts.append((np.log(lag), np.log(np.mean(rl))))
        if len(pts) < 4:
            return 0.5
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        return float(np.polyfit(x, y, 1)[0])

    @staticmethod
    def adx(df, period=ADX_PERIOD):
        h, l, c = df["high"], df["low"], df["close"]
        pdm = h.diff().clip(lower=0)
        mdm = (-l.diff()).clip(lower=0)
        pdm[pdm < mdm] = 0.0
        mdm[mdm < pdm] = 0.0
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        a = tr.ewm(span=period, adjust=False).mean()
        pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / a
        mdi = 100 * mdm.ewm(span=period, adjust=False).mean() / a
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
        adx_val = dx.ewm(span=period, adjust=False).mean()
        direction = 1.0 if pdi.iloc[-1] > mdi.iloc[-1] else -1.0
        return float(adx_val.iloc[-1]), direction

    @staticmethod
    def momentum_20(c):
        if len(c) < 21:
            return 0.0
        return float(c.iloc[-1] / c.iloc[-21] - 1)

    @staticmethod
    def near_high(c, lookback=20):
        """How close price is to N-period high. 1.0 = at high."""
        hi = c.tail(lookback).max()
        lo = c.tail(lookback).min()
        rng = hi - lo
        if rng < 1e-10:
            return 0.5
        return float((c.iloc[-1] - lo) / rng)

    @staticmethod
    def detect_regime(hurst_val, adx_val):
        if hurst_val > 0.55 and adx_val > 25:
            return "TRENDING"
        elif hurst_val < 0.45 and adx_val < 20:
            return "REVERTING"
        return "MIXED"


# =========================================================================
# LEDGER
# =========================================================================
class CryptoLedger:
    def __init__(self):
        self.path = LEDGER_FILE
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {"positions": {}, "trades": []}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def record_entry(self, symbol, qty, price, stop, regime):
        self.data["positions"][symbol] = {
            "qty": qty, "entry_price": price, "stop": stop,
            "high_water": price, "regime": regime,
            "entry_time": datetime.now().isoformat(),
        }
        self._save()

    def record_exit(self, symbol, price, reason):
        pos = self.data["positions"].pop(symbol, None)
        if pos:
            pnl = (price - pos["entry_price"]) / pos["entry_price"]
            self.data["trades"].append({
                "symbol": symbol, "entry": pos["entry_price"],
                "exit": price, "pnl_pct": round(pnl * 100, 2),
                "reason": reason, "time": datetime.now().isoformat(),
            })
        self._save()

    def get_position(self, symbol):
        return self.data["positions"].get(symbol)

    def update_high_water(self, symbol, price):
        if symbol in self.data["positions"]:
            pos = self.data["positions"][symbol]
            if price > pos["high_water"]:
                pos["high_water"] = price
                self._save()

    def has_position(self, symbol):
        return symbol in self.data["positions"]


# =========================================================================
# ENGINE
# =========================================================================
def run_crypto(dry_run=False):
    logger.info("=" * 50)
    logger.info("RAPTOR CRYPTO v1.0 - %s", datetime.now().isoformat())
    logger.info("=" * 50)

    data = CryptoData()
    sig = CryptoSignals()
    ledger = CryptoLedger()

    account = data.get_account()
    equity = account["equity"]
    crypto_budget = equity * CAPITAL_FRACTION
    crypto_positions = data.get_positions()

    logger.info("Equity: $%.2f | Crypto budget: $%.2f | Positions: %d",
                equity, crypto_budget, len(crypto_positions))

    # Check exits first
    for pos in crypto_positions:
        symbol = pos["symbol"]
        # Alpaca returns BTCUSD, data API needs BTC/USD
        data_symbol = symbol if "/" in symbol else symbol[:3] + "/" + symbol[3:]
        price = pos["current_price"]
        entry = pos["avg_entry"]
        pnl_pct = pos["unrealized_pnl_pct"]

        bars = data.get_bars(data_symbol)
        if bars is None:
            continue

        atr_val = sig.atr(bars)
        ledger_pos = ledger.get_position(symbol) or ledger.get_position(data_symbol)
        high_water = ledger_pos["high_water"] if ledger_pos else entry
        ledger.update_high_water(symbol, price)
        if data_symbol != symbol:
            ledger.update_high_water(data_symbol, price)
        high_water = max(high_water, price)

        reason = None

        # Hard stop
        hard_stop = entry - INITIAL_STOP_ATR * atr_val
        if price <= hard_stop:
            reason = "hard_stop"

        # Trailing stop
        if reason is None:
            trail = high_water - TRAIL_ATR * atr_val
            if trail > hard_stop and price <= trail:
                reason = "trail_profit" if price > entry else "trail_loss"

        # Momentum break — close below 8-EMA for 3 bars
        if reason is None and pnl_pct > 0.005:
            ema8 = bars["close"].ewm(span=8, adjust=False).mean()
            if (bars["close"].iloc[-1] < ema8.iloc[-1] and
                bars["close"].iloc[-2] < ema8.iloc[-2] and
                bars["close"].iloc[-3] < ema8.iloc[-3]):
                reason = "momentum_break"

        # Profit target — up 3x ATR, at new high
        if reason is None:
            profit_atr = (high_water - entry) / atr_val if atr_val > 0 else 0
            if profit_atr >= 3.0 and price >= high_water:
                reason = "profit_target"

        if reason:
            logger.info("EXIT %s @ $%.2f [%s] pnl=%+.1f%%",
                       symbol, price, reason, pnl_pct * 100)
            if not dry_run:
                result = data.submit_order(symbol, pos["qty"], "SELL")
                if "error" not in result:
                    ledger.record_exit(symbol, price, reason)
                    logger.info("  SOLD: %s", result["status"])
                else:
                    logger.error("  FAILED: %s", result["error"])
        else:
            logger.info("HOLD %s @ $%.2f pnl=%+.1f%%", symbol, price, pnl_pct * 100)

    # Check entries
    for symbol in SYMBOLS:
        if ledger.has_position(symbol):
            continue

        # Check if we already hold via Alpaca (non-ledger positions)
        held = {p["symbol"] for p in crypto_positions}
        if symbol in held or symbol.replace("/", "") in held:
            continue

        bars = data.get_bars(symbol)
        if bars is None or len(bars) < MIN_BARS:
            logger.info("SKIP %s: insufficient data", symbol)
            continue

        c = bars["close"]
        price = float(c.iloc[-1])
        atr_val = sig.atr(bars)
        rsi_val = sig.rsi(c)
        bb_z, bb_mid, bb_upper, bb_lower = sig.bollinger(c)
        hurst_val = sig.hurst(c)
        adx_val, adx_dir = sig.adx(bars)
        vol_r = sig.vol_ratio(bars["volume"])
        mom = sig.momentum_20(c)
        nearness = sig.near_high(c)
        regime = sig.detect_regime(hurst_val, adx_val)

        logger.info("%s: $%.2f RSI=%.1f BB_z=%.2f H=%.2f ADX=%.1f Mom=%.1f%% [%s]",
                   symbol, price, rsi_val, bb_z, hurst_val, adx_val, mom*100, regime)

        signal = False
        entry_type = None

        # TRENDING UP: buy breakouts
        if regime == "TRENDING" and adx_dir > 0:
            if nearness > 0.85 and mom > 0.03 and vol_r > 1.2:
                signal = True
                entry_type = "trend_breakout"

        # TRENDING DOWN + OVERSOLD: buy the capitulation bounce
        # Research: BTC mean-reverts after drawdowns (QuantPedia 2022)
        elif regime == "TRENDING" and adx_dir < 0:
            if rsi_val < 40 and bb_z < -1.0:
                signal = True
                entry_type = "trend_reversal"

        # REVERTING REGIME: buy dips
        elif regime == "REVERTING":
            if rsi_val < 45 and bb_z < -0.8:
                signal = True
                entry_type = "mr_dip"

        # MIXED: consolidation entry — range-bound near lower half
        elif regime == "MIXED":
            if rsi_val < 45 and bb_z < -0.5 and mom > -0.05:
                signal = True
                entry_type = "consolidation"

        if signal:
            # Kelly sizing — vol-adjusted
            realized_vol = np.log(c / c.shift(1)).dropna().tail(20).std() * np.sqrt(8760)
            vol_penalty = min(1.0, 0.30 / (realized_vol + 0.01))  # Target 30% annual vol
            kelly = min(KELLY_BASE * vol_penalty, KELLY_CAP)
            dollars = equity * kelly
            qty = round(dollars / price, 6)

            stop = round(price - INITIAL_STOP_ATR * atr_val, 2)

            logger.info("SIGNAL %s [%s] qty=%.6f kelly=%.3f stop=$%.2f vol=%.0f%%",
                       symbol, entry_type, qty, kelly, stop, realized_vol * 100)

            if not dry_run:
                result = data.submit_order(symbol, qty, "BUY")
                if "error" not in result:
                    ledger.record_entry(symbol, qty, price, stop, regime)
                    logger.info("  BOUGHT: %s", result["status"])
                else:
                    logger.error("  FAILED: %s", result["error"])
        else:
            logger.info("NO SIGNAL %s", symbol)

    # Summary
    trades = ledger.data.get("trades", [])
    if trades:
        wins = [t for t in trades if t["pnl_pct"] > 0]
        logger.info("History: %d trades, %d wins (%.0f%%), avg pnl=%+.1f%%",
                   len(trades), len(wins),
                   len(wins)/len(trades)*100 if trades else 0,
                   sum(t["pnl_pct"] for t in trades)/len(trades))


def show_status():
    data = CryptoData()
    ledger = CryptoLedger()
    positions = data.get_positions()
    print(f"\nCrypto positions: {len(positions)}")
    for p in positions:
        print(f"  {p['symbol']}: qty={p['qty']} entry=${p['avg_entry']:.2f} "
              f"price=${p['current_price']:.2f} pnl={p['unrealized_pnl_pct']*100:+.1f}%")
    trades = ledger.data.get("trades", [])
    if trades:
        wins = [t for t in trades if t["pnl_pct"] > 0]
        total_pnl = sum(t["pnl_pct"] for t in trades)
        print(f"\nTrade history: {len(trades)} trades, {len(wins)} wins "
              f"({len(wins)/len(trades)*100:.0f}%), total pnl={total_pnl:+.1f}%")
    else:
        print("\nNo trade history yet.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Raptor Crypto v1.0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_crypto(dry_run=args.dry_run)
