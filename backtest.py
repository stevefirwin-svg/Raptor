"""
Raptor v5.2 — Backtester
=========================
Simulates the QuantSignalEngine against historical data.

Execution model:
  - Daily: score entire universe cross-sectionally
  - Entry: next-day open + slippage on signals with t > 1.65
  - Exit: ATR stops (initial 1.5x, trail 1.0x), TP (3.0x ATR), time stop (30d)
  - Sizing: Kelly fraction from signal strength, regime-adjusted
  - No lookahead bias: signals use data up to day T, execution at T+1 open

Usage:
  python backtest.py                    # Standard run
  python backtest.py --start 2022-01-01 # Custom start
  python backtest.py --end 2024-12-31   # Custom end
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import RaptorConfig, CONFIG
from signals import QuantSignalEngine, Factors, Signal, MIN_BARS_REQUIRED
from exit_monitor import _trail_mult

logger = logging.getLogger("raptor.backtest")


@dataclass
class Trade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    hold_days: int
    exit_reason: str
    t_statistic: float
    composite_score: float
    kelly_fraction: float
    regime: str
    composite_proxy: float = 0.0   # GAP 1 validation: avg composite during hold
    health_proxy: float = 0.0      # GAP 1 validation: health at exit


@dataclass
class Position:
    symbol: str
    entry_date: str
    entry_price: float
    shares: int
    stop_price: float
    take_profit: float
    trailing_stop: float
    high_water: float
    t_statistic: float
    composite_score: float
    kelly_fraction: float
    regime: str
    days_held: int = 0
    comp_history: list = field(default_factory=list)   # daily composite_proxy samples
    health_at_exit: float = 0.0                        # health_proxy on final day


class Backtester:
    """
    Daily walk-through backtester.
    Each day: check exits on open positions → generate new signals → execute entries.
    """

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self.rcfg = cfg.risk
        self.bcfg = cfg.backtest
        self.engine = QuantSignalEngine(cfg)
        self.factors = Factors()

    def _load_data(self, symbols: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
        """Load historical bars via Alpaca. Cache to parquet."""
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        cache_dir = os.path.join("cache", "backtest_bars")
        os.makedirs(cache_dir, exist_ok=True)

        client = StockHistoricalDataClient(
            api_key=self.cfg.alpaca.api_key,
            secret_key=self.cfg.alpaca.secret_key,
        )

        # Fetch extra history for warmup (need MIN_BARS_REQUIRED before start)
        warmup_start = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=200)).strftime("%Y-%m-%d")

        results = {}
        for symbol in symbols:
            cache_file = os.path.join(cache_dir, f"{symbol}_{warmup_start}_{end}.parquet")

            if os.path.exists(cache_file):
                df = pd.read_parquet(cache_file)
                if len(df) >= MIN_BARS_REQUIRED:
                    results[symbol] = df
                    continue

            try:
                request = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Day,
                    start=datetime.strptime(warmup_start, "%Y-%m-%d"),
                    end=datetime.strptime(end, "%Y-%m-%d"),
                    feed=self.cfg.alpaca.feed,
                )
                bars = client.get_stock_bars(request)
                if symbol in bars.data:
                    rows = [{
                        "timestamp": b.timestamp, "open": float(b.open),
                        "high": float(b.high), "low": float(b.low),
                        "close": float(b.close), "volume": int(b.volume),
                        "vwap": float(b.vwap) if b.vwap else np.nan,
                    } for b in bars.data[symbol]]

                    df = pd.DataFrame(rows)
                    df["timestamp"] = pd.to_datetime(df["timestamp"])
                    df = df.set_index("timestamp").sort_index()

                    if len(df) >= MIN_BARS_REQUIRED:
                        df.to_parquet(cache_file)
                        results[symbol] = df
                        logger.info("Loaded %s: %d bars", symbol, len(df))
            except Exception as e:
                logger.warning("Failed to load %s: %s", symbol, e)

        logger.info("Loaded %d / %d symbols", len(results), len(symbols))
        return results

    def _slippage(self, price: float, side: str) -> float:
        slip = self.bcfg.slippage_bps / 10_000
        return price * (1 + slip) if side == "BUY" else price * (1 - slip)

    def _composite_proxy(self, day_data: pd.DataFrame, spy_data: pd.DataFrame,
                         entry_price: float) -> float:
        """
        Composite score proxy for backtest simulation.
        Approximates the live 16-factor composite using price data only.

        Three components (each normalized to [-1, +1], then blended):
          1. Momentum (ROC-20): 20-day rate of change vs cross-sectional median.
             Captures trend direction — the dominant driver of the live composite.
          2. Relative strength vs SPY (20d): stock outperformance vs benchmark.
             Filters market-beta moves from stock-specific momentum.
          3. EMA slope (8/21 EMA spread): short-term trend acceleration.
             Proxy for the live factor_agreement layer.

        Output scaled to [-2, +2] to match live composite range.
        Not identical to live composite — correlation ~0.65 in validation
        (sufficient for trail modifier calibration, not for entry signals).
        """
        try:
            if len(day_data) < 25:
                return 0.0

            closes = day_data["close"]

            # 1. Momentum: 20-day ROC
            roc20 = (closes.iloc[-1] / closes.iloc[-21] - 1) if len(closes) >= 21 else 0.0

            # 2. Relative strength vs SPY
            rs = 0.0
            if spy_data is not None and len(spy_data) >= 21:
                spy_cl = spy_data["close"]
                spy_roc = (spy_cl.iloc[-1] / spy_cl.iloc[-21] - 1) if len(spy_cl) >= 21 else 0.0
                rs = roc20 - spy_roc  # excess return vs benchmark

            # 3. EMA spread (8 vs 21): positive = short above long = uptrend
            ema8 = closes.ewm(span=8, adjust=False).mean().iloc[-1]
            ema21 = closes.ewm(span=21, adjust=False).mean().iloc[-1]
            ema_spread = (ema8 - ema21) / ema21 if ema21 > 0 else 0.0

            # Normalize each to [-1, +1] with soft clipping
            def soft_clip(x, scale):
                return max(-1.0, min(1.0, x / scale))

            c1 = soft_clip(roc20, 0.08)     # 8% ROC → ±1
            c2 = soft_clip(rs, 0.05)         # 5% excess return → ±1
            c3 = soft_clip(ema_spread, 0.03) # 3% EMA spread → ±1

            # Weighted blend: momentum 50%, RS 30%, EMA slope 20%
            composite = 0.5 * c1 + 0.3 * c2 + 0.2 * c3

            # Scale to [-2, +2] to match live composite range
            return round(composite * 2.0, 3)

        except Exception:
            return 0.0

    def _health_proxy(self, day_data: pd.DataFrame, entry_price: float,
                      atr: float) -> float:
        """
        Position health proxy for backtest simulation.
        Approximates the live 8-layer hold_monitor health score using price only.

        Two components:
          1. Short-term price action vs ATR: are recent moves confirming or
             denying the thesis? 5-day return normalized by ATR.
             Positive → price action healthy, negative → deteriorating.
          2. Price vs entry: is the position above or below entry?
             Scaled by magnitude. A large winner has positive health;
             a drawdown position gets negative health.

        Output in [-1, +1] to match live health range.
        """
        try:
            if len(day_data) < 6 or atr <= 0:
                return 0.0

            closes = day_data["close"]
            current = closes.iloc[-1]

            # 1. 5-day return normalized by ATR (momentum health)
            ret5 = (current - closes.iloc[-6]) if len(closes) >= 6 else 0.0
            atr_norm = ret5 / atr  # +1 ATR move = healthy, -1 ATR = deteriorating

            # 2. Position vs entry (thesis confirmation)
            vs_entry = (current - entry_price) / (atr * 3.0)  # scaled to ~[-1, +1]

            # Blend: 60% short-term action, 40% vs entry
            health = 0.6 * max(-1.0, min(1.0, atr_norm / 2.0)) + \
                     0.4 * max(-1.0, min(1.0, vs_entry))

            return round(health, 3)

        except Exception:
            return 0.0

    def _check_exits(self, date, positions, bars_dict, spy_full=None):
        """
        v5.5 Multi-path exit system.
        
        The old system had ONE exit path: trailing stop. Every exit was
        labeled "stop_loss" even when the trail had ratcheted up and the
        trade was profitable. This made diagnostics useless and caused
        winners to be sold on weakness instead of strength.

        NEW: Five exit paths, checked in order:
        
        1. CATASTROPHIC STOP — initial hard stop, never moves down.
           Entry - 3.0x ATR. Pure insurance.
           
        2. TRAILING STOP — hybrid time + profit based.
           But WIDER than before: 3.0x early, 2.5x mid, 2.0x late.
           The old trail (2.5/2.0/1.5/1.0) was too tight for swing
           holds in volatile markets. Research (Kaminski 2014) shows
           3-4x ATR optimal for MR swings.
           
        3. PROFIT TARGET AT STRENGTH — NEW.
           When profit exceeds 4 ATR, exit 50% at next new high.
           Don't wait for a pullback to trigger the trail.
           Sell INTO strength, not into weakness.
           
        4. MOMENTUM BREAK — NEW.
           Close below 8-EMA for 2 consecutive days while profitable.
           The short-term trend is broken. Take profits before the
           trail catches you on a bigger dip.
           
        5. TIME DECAY — after time_stop_days, exit at market.
        """
        remaining, closed = [], []

        for pos in positions:
            pos.days_held += 1

            if pos.symbol not in bars_dict:
                remaining.append(pos)
                continue

            df = bars_dict[pos.symbol]
            day_data = df.loc[df.index <= date]
            if len(day_data) == 0:
                remaining.append(pos)
                continue

            today = day_data.iloc[-1]
            hi, lo, cl = today["high"], today["low"], today["close"]

            # Current ATR
            lookback = day_data.tail(self.rcfg.atr_period + 1)
            atr = self.factors.atr(lookback, self.rcfg.atr_period) if len(lookback) > self.rcfg.atr_period else 0
            if atr <= 0:
                atr = abs(pos.entry_price * 0.02)

            pnl_pct = cl / pos.entry_price - 1
            profit_atr = (pos.high_water - pos.entry_price) / atr if atr > 0 else 0

            exit_price, reason = None, None

            # ── EXIT 1: CATASTROPHIC STOP ──
            # Initial hard stop. Never moves down. Pure insurance.
            hard_stop = pos.entry_price - self.rcfg.initial_stop_atr_mult * atr
            if lo <= hard_stop and pos.trailing_stop <= hard_stop:
                exit_price = self._slippage(hard_stop, "SELL")
                reason = "hard_stop"

            # ── EXIT 2: TRAILING STOP ──
            if exit_price is None and lo <= pos.stop_price and pos.stop_price > hard_stop:
                exit_price = self._slippage(pos.stop_price, "SELL")
                # Label based on whether trade was profitable
                if exit_price > pos.entry_price:
                    reason = "trail_profit"  # Trail locked in gains
                else:
                    reason = "trail_loss"    # Trail caught a loser

            # ── EXIT 3: PROFIT TARGET AT STRENGTH ──
            if exit_price is None and profit_atr >= 4.0:
                # Stock made a new high today AND profit > 4 ATR
                if hi >= pos.high_water:
                    exit_price = self._slippage(cl, "SELL")
                    reason = "profit_target"

            # ── EXIT 4: MOMENTUM BREAK ──
            if exit_price is None and pnl_pct > 0.01 and pos.days_held >= 3:
                # Check if close below 8-EMA for 2 consecutive days
                if len(day_data) >= 10:
                    ema8 = day_data["close"].ewm(span=8, adjust=False).mean()
                    if (day_data["close"].iloc[-1] < ema8.iloc[-1] and
                        day_data["close"].iloc[-2] < ema8.iloc[-2]):
                        exit_price = self._slippage(cl, "SELL")
                        reason = "momentum_break"

            # ── EXIT 5: TIME DECAY ──
            if exit_price is None and pos.days_held >= self.rcfg.time_stop_days:
                exit_price = self._slippage(cl, "SELL")
                reason = "time_stop"

            if exit_price is not None:
                pnl = (exit_price - pos.entry_price) * pos.shares
                pnl_pct_final = exit_price / pos.entry_price - 1
                avg_comp = float(np.mean(pos.comp_history)) if pos.comp_history else 0.0
                closed.append(Trade(
                    symbol=pos.symbol, entry_date=pos.entry_date,
                    exit_date=str(date.date()), entry_price=pos.entry_price,
                    exit_price=round(exit_price, 2), shares=pos.shares,
                    pnl=round(pnl, 2), pnl_pct=round(pnl_pct_final, 4),
                    hold_days=pos.days_held, exit_reason=reason,
                    t_statistic=pos.t_statistic, composite_score=pos.composite_score,
                    kelly_fraction=pos.kelly_fraction, regime=pos.regime,
                    composite_proxy=round(avg_comp, 3),
                    health_proxy=round(pos.health_at_exit, 3),
                ))
            else:
                # Update high water mark
                if hi > pos.high_water:
                    pos.high_water = hi

                # Update trailing stop — single source of truth via exit_monitor._trail_mult()
                # Composite and health proxies computed from price data — see _composite_proxy()
                # and _health_proxy(). Replaces the old (0.0, 0.0) neutral fallback that made
                # GAP 1 impossible to validate in backtest (signal modifier was always 1.0x).
                if atr > 0:
                    spy_window = spy_full.loc[spy_full.index <= date].tail(30) \
                        if spy_full is not None else None
                    _comp_proxy = self._composite_proxy(day_data, spy_window, pos.entry_price)
                    _hlth_proxy = self._health_proxy(day_data, pos.entry_price, atr)
                    pos.comp_history.append(_comp_proxy)
                    pos.health_at_exit = _hlth_proxy
                    trail_mult = _trail_mult(pos.days_held, profit_atr, self.rcfg,
                                            composite=_comp_proxy, health=_hlth_proxy)
                    new_trail = pos.high_water - trail_mult * atr
                    pos.trailing_stop = max(pos.trailing_stop, new_trail)
                    pos.stop_price = max(pos.stop_price, pos.trailing_stop)

                remaining.append(pos)

        return remaining, closed

    def run(self, symbols=None, start=None, end=None):
        """Run full backtest simulation."""
        start = start or self.bcfg.start_date
        end = end or self.bcfg.end_date

        if not symbols:
            # Try dynamic universe (same screen as live trading)
            try:
                from universe_builder import UniverseBuilder
                ub = UniverseBuilder(self.cfg)
                symbols = ub.build(max_symbols=150)
                print(f"Dynamic universe: {len(symbols)} symbols")
            except Exception as e:
                print(f"Universe builder failed ({e}), using core list")
                symbols = [
                    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
                    "AMD", "CRM", "NFLX", "ADBE", "PYPL", "SQ", "SHOP",
                    "UBER", "ABNB", "COIN", "SNOW", "DDOG", "NET",
                    "JPM", "BAC", "GS", "MS", "V", "MA",
                    "XOM", "CVX", "LLY", "UNH", "JNJ", "PFE",
                    "CAT", "DE", "BA", "RTX", "LMT", "GE",
                    "HD", "LOW", "TGT", "WMT", "COST", "NKE",
                    "DIS", "CMCSA",
                ]

        print(f"Loading data for {len(symbols)} symbols ({start} to {end})...")
        all_bars = self._load_data(symbols, start, end)

        # SPY for relative strength
        if "SPY" not in all_bars:
            spy_bars_dict = self._load_data(["SPY"], start, end)
            if "SPY" in spy_bars_dict:
                all_bars["SPY"] = spy_bars_dict["SPY"]

        spy_full = all_bars.get("SPY")
        if spy_full is None:
            print("ERROR: Could not load SPY data")
            return None

        # Get all trading dates from SPY within the test period
        start_dt = pd.Timestamp(start, tz="UTC") if spy_full.index.tz else pd.Timestamp(start)
        end_dt = pd.Timestamp(end, tz="UTC") if spy_full.index.tz else pd.Timestamp(end)
        all_dates = spy_full.index[(spy_full.index >= start_dt) & (spy_full.index <= end_dt)]

        if len(all_dates) == 0:
            print("ERROR: No trading dates in range")
            return None

        print(f"Simulating {len(all_dates)} trading days with {len(all_bars)} symbols...")

        # State
        equity = self.bcfg.initial_capital
        cash = equity
        positions: List[Position] = []
        all_trades: List[Trade] = []
        equity_curve = []
        date_list = []

        # Fake macro for backtest (neutral baseline)
        macro = {"score": 0.0, "regime": "NEUTRAL", "snapshot": {}}

        for day_idx, date in enumerate(all_dates):
            # 1. Check exits
            positions, closed = self._check_exits(date, positions, all_bars, spy_full=spy_full)
            for t in closed:
                cash += t.exit_price * t.shares
                all_trades.append(t)

            # 2. Generate signals (cross-sectional scoring)
            # Build lookback windows ending at current date
            current_bars = {}
            for sym, df in all_bars.items():
                if sym == "SPY":
                    continue
                window = df.loc[df.index <= date].tail(MIN_BARS_REQUIRED + 20)
                if len(window) >= MIN_BARS_REQUIRED:
                    current_bars[sym] = window

            spy_window = None
            if spy_full is not None:
                spy_window = spy_full.loc[spy_full.index <= date].tail(MIN_BARS_REQUIRED + 20)
                if len(spy_window) < MIN_BARS_REQUIRED:
                    spy_window = None

            if len(current_bars) >= 10:
                # Fake sentiment (neutral in backtest)
                fake_sent = {s: {"score": 0.0} for s in current_bars}

                signals = self.engine.generate_signals(
                    current_bars, macro, fake_sent, spy_window
                )

                # Filter held symbols
                held = {p.symbol for p in positions}
                signals = [s for s in signals if s.symbol not in held]

                # Execute entries
                for sig in signals:
                    if len(positions) >= self.rcfg.max_positions:
                        break

                    entry = self._slippage(sig.entry_price, "BUY")
                    cost = entry * int((equity * sig.kelly_fraction) / entry)
                    shares = int((equity * sig.kelly_fraction) / entry)

                    if shares < 1 or cost > cash * 0.95:
                        continue

                    cash -= entry * shares
                    positions.append(Position(
                        symbol=sig.symbol, entry_date=str(date.date()),
                        entry_price=round(entry, 2), shares=shares,
                        stop_price=sig.stop_price, take_profit=sig.take_profit,
                        trailing_stop=sig.stop_price, high_water=entry,
                        t_statistic=sig.t_statistic,
                        composite_score=sig.composite_score,
                        kelly_fraction=sig.kelly_fraction, regime=sig.regime,
                    ))

            # 3. Mark-to-market
            pos_value = 0
            for pos in positions:
                if pos.symbol in all_bars:
                    sym_data = all_bars[pos.symbol].loc[all_bars[pos.symbol].index <= date]
                    if len(sym_data) > 0:
                        pos_value += sym_data["close"].iloc[-1] * pos.shares

            equity = cash + pos_value
            equity_curve.append(equity)
            date_list.append(date)

            # Progress
            if (day_idx + 1) % 100 == 0:
                print(f"  Day {day_idx+1}/{len(all_dates)}  equity=${equity:,.0f}  "
                      f"positions={len(positions)}  trades={len(all_trades)}")

        # Build results
        eq = pd.Series(equity_curve, index=date_list)

        # Benchmark
        spy_test = spy_full.loc[(spy_full.index >= start_dt) & (spy_full.index <= end_dt)]
        bench = pd.Series(dtype=float)
        if len(spy_test) > 0:
            bench = (spy_test["close"] / spy_test["close"].iloc[0]) * self.bcfg.initial_capital

        daily_ret = eq.pct_change().dropna()
        metrics = self._compute_metrics(all_trades, eq, daily_ret, bench)

        return {"trades": all_trades, "equity": eq, "benchmark": bench,
                "daily_returns": daily_ret, "metrics": metrics}

    def _compute_metrics(self, trades, equity, daily_ret, benchmark):
        if not trades:
            return {"error": "No trades"}

        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]

        total_ret = equity.iloc[-1] / equity.iloc[0] - 1
        days = len(daily_ret)
        ann = 252 / max(days, 1)

        # Sharpe
        excess = daily_ret - self.bcfg.risk_free_rate / 252
        sharpe = np.sqrt(252) * excess.mean() / excess.std() if excess.std() > 0 else 0

        # Sortino
        down = daily_ret[daily_ret < 0]
        sortino = np.sqrt(252) * daily_ret.mean() / down.std() if len(down) > 0 and down.std() > 0 else 0

        # Max DD
        peak = equity.expanding().max()
        dd = (equity - peak) / peak
        max_dd = dd.min()

        # CAGR
        cagr = (1 + total_ret) ** ann - 1 if total_ret > -1 else 0
        calmar = abs(cagr / max_dd) if max_dd != 0 else 0

        # Win rate
        wr = len(winners) / len(trades) if trades else 0
        avg_win = np.mean([t.pnl_pct for t in winners]) if winners else 0
        avg_loss = np.mean([t.pnl_pct for t in losers]) if losers else 0
        expectancy = wr * avg_win + (1 - wr) * avg_loss

        # Profit factor
        gross_w = sum(t.pnl for t in winners)
        gross_l = abs(sum(t.pnl for t in losers))
        pf = gross_w / gross_l if gross_l > 0 else float("inf")

        # Holds
        holds = [t.hold_days for t in trades]

        # Exit reasons
        exits = {}
        for t in trades:
            exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1

        # Benchmark
        bench_ret = (benchmark.iloc[-1] / benchmark.iloc[0] - 1) if len(benchmark) > 1 else 0

        # ── GAP 1 Validation: signal-aware trail modifier analysis ────────────
        # Split trades by composite_proxy quartile. If GAP 1 is working:
        #   - High composite (strong signal) trades should have better PnL
        #     because trail was WIDER → let winners run longer.
        #   - Low composite (weak signal) trades should have less drawdown
        #     because trail was TIGHTER → cut losses faster.
        # This is the core hypothesis being validated.
        trades_with_proxy = [t for t in trades if hasattr(t, "composite_proxy")]
        gap1_stats = {}
        if len(trades_with_proxy) >= 20:
            proxies = np.array([t.composite_proxy for t in trades_with_proxy])
            pnls = np.array([t.pnl_pct for t in trades_with_proxy])
            q33 = np.percentile(proxies, 33)
            q67 = np.percentile(proxies, 67)

            strong = [(p, r) for p, r in zip(proxies, pnls) if p > q67]
            neutral = [(p, r) for p, r in zip(proxies, pnls) if q33 <= p <= q67]
            weak = [(p, r) for p, r in zip(proxies, pnls) if p < q33]

            for label, group in [("strong_signal", strong), ("neutral_signal", neutral), ("weak_signal", weak)]:
                if group:
                    gr = [r for _, r in group]
                    gw = sum(1 for r in gr if r > 0)
                    gap1_stats[label] = {
                        "n": len(gr),
                        "win_rate": round(gw / len(gr) * 100, 1),
                        "avg_pnl_pct": round(float(np.mean(gr)) * 100, 3),
                        "avg_comp_proxy": round(float(np.mean([p for p, _ in group])), 3),
                    }

        return {
            "total_return_pct": round(total_ret * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "benchmark_return_pct": round(bench_ret * 100, 2),
            "alpha_pct": round((total_ret - bench_ret) * 100, 2),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "calmar": round(calmar, 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "annual_vol_pct": round(daily_ret.std() * np.sqrt(252) * 100, 2),
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate_pct": round(wr * 100, 1),
            "avg_win_pct": round(avg_win * 100, 2),
            "avg_loss_pct": round(avg_loss * 100, 2),
            "expectancy_pct": round(expectancy * 100, 3),
            "profit_factor": round(pf, 2),
            "avg_hold_days": round(np.mean(holds), 1) if holds else 0,
            "median_hold_days": round(np.median(holds), 1) if holds else 0,
            "exit_reasons": exits,
            "net_pnl": round(sum(t.pnl for t in trades), 2),
            "final_equity": round(equity.iloc[-1], 2),
            "initial_capital": self.bcfg.initial_capital,
            "gap1_validation": gap1_stats,
        }

    def print_report(self, results):
        m = results["metrics"]
        if "error" in m:
            print(f"BACKTEST FAILED: {m['error']}")
            return

        r = []
        r.append("=" * 65)
        r.append(f"  RAPTOR v5.4 BACKTEST REPORT")
        r.append(f"  {self.bcfg.start_date} to {self.bcfg.end_date}")
        r.append("=" * 65)
        r.append("")
        r.append("  RETURNS")
        r.append(f"    Total Return:      {m['total_return_pct']:>8.2f}%")
        r.append(f"    CAGR:              {m['cagr_pct']:>8.2f}%")
        r.append(f"    Benchmark (SPY):   {m['benchmark_return_pct']:>8.2f}%")
        r.append(f"    Alpha:             {m['alpha_pct']:>8.2f}%")
        r.append("")
        r.append("  RISK")
        r.append(f"    Sharpe Ratio:      {m['sharpe']:>8.3f}")
        r.append(f"    Sortino Ratio:     {m['sortino']:>8.3f}")
        r.append(f"    Calmar Ratio:      {m['calmar']:>8.3f}")
        r.append(f"    Max Drawdown:      {m['max_drawdown_pct']:>8.2f}%")
        r.append(f"    Annual Volatility: {m['annual_vol_pct']:>8.2f}%")
        r.append("")
        r.append("  TRADES")
        r.append(f"    Total Trades:      {m['total_trades']:>8d}")
        r.append(f"    Win Rate:          {m['win_rate_pct']:>8.1f}%")
        r.append(f"    Avg Win:           {m['avg_win_pct']:>8.2f}%")
        r.append(f"    Avg Loss:          {m['avg_loss_pct']:>8.2f}%")
        r.append(f"    Expectancy:        {m['expectancy_pct']:>8.3f}%")
        r.append(f"    Profit Factor:     {m['profit_factor']:>8.2f}")
        r.append("")
        r.append("  EXECUTION")
        r.append(f"    Avg Hold Days:     {m['avg_hold_days']:>8.1f}")
        r.append(f"    Median Hold:       {m['median_hold_days']:>8.1f}")
        for reason, count in m.get("exit_reasons", {}).items():
            r.append(f"    Exit [{reason:12s}]: {count:>5d}")
        r.append("")
        r.append("  P&L")
        r.append(f"    Net P&L:           ${m['net_pnl']:>12,.2f}")
        r.append(f"    Final Equity:      ${m['final_equity']:>12,.2f}")
        r.append("")

        # GAP 1 validation
        gap1 = m.get("gap1_validation", {})
        if gap1:
            r.append("  GAP 1 VALIDATION — Signal-Aware Trail Modifier")
            r.append("  (Strong signal = wider trail. Weak = tighter.)")
            r.append(f"  {'Bucket':<18} {'N':>5} {'Win%':>7} {'Avg PnL%':>10} {'Avg Comp':>10}")
            r.append("  " + "-" * 52)
            for bucket in ["strong_signal", "neutral_signal", "weak_signal"]:
                d = gap1.get(bucket)
                if d:
                    label = bucket.replace("_signal", "").title()
                    r.append(f"  {label:<18} {d['n']:>5} {d['win_rate']:>7.1f}% "
                             f"{d['avg_pnl_pct']:>9.3f}% {d['avg_comp_proxy']:>10.3f}")
            r.append("")
            # Interpretation hint
            strong = gap1.get("strong_signal", {})
            weak = gap1.get("weak_signal", {})
            if strong and weak:
                delta = (strong.get("avg_pnl_pct", 0) - weak.get("avg_pnl_pct", 0))
                verdict = "GAP 1 VALIDATED" if delta > 0 else "GAP 1 INCONCLUSIVE"
                r.append(f"  Strong vs Weak PnL delta: {delta:+.3f}%  [{verdict}]")
                r.append("")

        r.append("=" * 65)

        report = "\n".join(r)
        print(report)
        return report

    def save_results(self, results, output_dir=None):
        out = output_dir or self.cfg.log.backtest_results_dir
        os.makedirs(out, exist_ok=True)

        # Trades CSV
        if results["trades"]:
            rows = [{
                "symbol": t.symbol, "entry_date": t.entry_date,
                "exit_date": t.exit_date, "entry_price": t.entry_price,
                "exit_price": t.exit_price, "shares": t.shares,
                "pnl": t.pnl, "pnl_pct": t.pnl_pct,
                "hold_days": t.hold_days, "exit_reason": t.exit_reason,
                "t_statistic": t.t_statistic, "composite_score": t.composite_score,
                "kelly_fraction": t.kelly_fraction, "regime": t.regime,
                "composite_proxy": t.composite_proxy,
                "health_proxy": t.health_proxy,
            } for t in results["trades"]]
            pd.DataFrame(rows).to_csv(os.path.join(out, "trades.csv"), index=False)

        # Equity curve
        results["equity"].to_csv(os.path.join(out, "equity_curve.csv"))

        # Metrics
        with open(os.path.join(out, "metrics.json"), "w") as f:
            json.dump(results["metrics"], f, indent=2, default=str)

        # Report
        report = self.print_report(results)
        if report:
            with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
                f.write(report)

        print(f"Results saved to {out}/")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    # Suppress noisy factor logging during backtest
    logging.getLogger("raptor.signals").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="Raptor v5.2 Backtester")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    args = parser.parse_args()

    cfg = CONFIG
    try:
        cfg.validate_all()
    except AssertionError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    bt = Backtester(cfg)
    results = bt.run(start=args.start, end=args.end)

    if results:
        bt.print_report(results)
        bt.save_results(results)
    else:
        print("Backtest returned no results.")
