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
    trade_type: str = "MOMENTUM"
    pattern_signal: str = ""
    hold_target_days: int = 15
    days_held: int = 0


class Backtester:
    """
    Daily walk-through backtester.
    Each day: check exits on open positions → generate new signals → execute entries.
    """

    def __init__(self, cfg: RaptorConfig, gap1_enabled: bool = True):
        self.cfg = cfg
        self.rcfg = cfg.risk
        self.bcfg = cfg.backtest
        self.engine = QuantSignalEngine(cfg)
        self.factors = Factors()
        self.gap1_enabled = gap1_enabled  # --no-gap1 flag disables signal-quality trail modifier

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

    def _check_exits(self, date, positions, bars_dict):
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
                closed.append(Trade(
                    symbol=pos.symbol, entry_date=pos.entry_date,
                    exit_date=str(date.date()), entry_price=pos.entry_price,
                    exit_price=round(exit_price, 2), shares=pos.shares,
                    pnl=round(pnl, 2), pnl_pct=round(pnl_pct_final, 4),
                    hold_days=pos.days_held, exit_reason=reason,
                    t_statistic=pos.t_statistic, composite_score=pos.composite_score,
                    kelly_fraction=pos.kelly_fraction, regime=pos.regime,
                ))
            else:
                # Update high water mark
                if hi > pos.high_water:
                    pos.high_water = hi

                # Update trailing stop — single source of truth via exit_monitor._trail_mult()
                # composite and health not available in backtest simulation (no hold_health.json)
                # so signal-quality modifier defaults to neutral (0.0, 0.0) — same as pre-GAP1 behavior.
                # When hold_history is integrated into backtest, pass real composite/health here.
                if atr > 0:
                    # GAP 1: use composite_score as signal-quality proxy when enabled.
                    # --no-gap1 forces neutral modifier (0.0) — reproduces pre-GAP1 baseline.
                    _comp = pos.composite_score if self.gap1_enabled else 0.0
                    trail_mult = _trail_mult(pos.days_held, profit_atr, self.rcfg,
                                            composite=_comp, health=0.0)
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
            # ── PRIORITY 1: locked backtest universe file ─────────────────────
            # backtest_universe.txt is a fixed, date-stamped symbol list that
            # does not change with live market conditions. This ensures
            # reproducible results across sessions.
            bt_universe_file = os.path.join(os.path.dirname(__file__), "backtest_universe.txt")
            if os.path.exists(bt_universe_file):
                with open(bt_universe_file) as f:
                    symbols = [s.strip() for s in f if s.strip() and not s.startswith("#")]
                print(f"Locked backtest universe: {len(symbols)} symbols (backtest_universe.txt)")

            else:
                # ── PRIORITY 2: live universe builder (non-reproducible — avoid for analysis) ──
                print("WARNING: backtest_universe.txt not found — using live universe screen.")
                print("WARNING: Results will vary by run date. Run generate_backtest_universe.py to fix.")
                try:
                    from universe_builder import UniverseBuilder
                    ub = UniverseBuilder(self.cfg)
                    symbols = ub.build(max_symbols=150)
                    print(f"Live universe: {len(symbols)} symbols")
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
            positions, closed = self._check_exits(date, positions, all_bars)
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
                        trade_type=getattr(sig, "trade_type", "MOMENTUM"),
                        pattern_signal=getattr(sig, "pattern_signal", ""),
                        hold_target_days=getattr(sig, "hold_target_days", 15),
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


def _compute_book_stats(trades):
    """Per-book P&L breakdown for backtest report."""
    books = {"MOMENTUM": [], "MEAN_REVERSION": []}
    for t in trades:
        book = getattr(t, "trade_type", "MOMENTUM")
        if book in books:
            books[book].append(t)
    stats = {}
    for book, btrades in books.items():
        if not btrades:
            stats[book] = None
            continue
        wins   = [t for t in btrades if t.pnl > 0]
        losses = [t for t in btrades if t.pnl <= 0]
        exits  = {}
        for t in btrades:
            exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
        patterns = {}
        for t in btrades:
            p = getattr(t, "pattern_signal", "") or "none"
            patterns[p] = patterns.get(p, 0) + 1
        stats[book] = {
            "n":          len(btrades),
            "win_rate":   round(len(wins) / len(btrades) * 100, 1),
            "avg_win":    round(np.mean([t.pnl_pct*100 for t in wins]),   3) if wins   else 0,
            "avg_loss":   round(np.mean([t.pnl_pct*100 for t in losses]), 3) if losses else 0,
            "avg_pnl":    round(np.mean([t.pnl_pct*100 for t in btrades]), 3),
            "avg_hold":   round(np.mean([t.hold_days   for t in btrades]), 1),
            "pf":         round(sum(t.pnl for t in wins) / max(abs(sum(t.pnl for t in losses)), 1e-10), 2),
            "exits":      exits,
            "patterns":   dict(sorted(patterns.items(), key=lambda x: -x[1])[:5]),
        }
    return stats

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

        # ── GAP 1 STATISTICAL DIAGNOSTICS ─────────────────────────────────────────
        def _bucket(t):
            c = t.composite_score
            if c > 0.3:   return "Strong"
            elif c < -0.3: return "Weak"
            else:          return "Neutral"

        buckets = {"Strong": [], "Neutral": [], "Weak": []}
        for t in trades:
            buckets[_bucket(t)].append(t)

        gap1_stats = {}
        for bname, btrades in buckets.items():
            if not btrades:
                gap1_stats[bname] = {}
                continue
            bwin = [t for t in btrades if t.pnl > 0]
            blose = [t for t in btrades if t.pnl <= 0]
            b_pnl = [t.pnl_pct * 100 for t in btrades]
            b_hold = [t.hold_days for t in btrades]
            b_exits = {}
            for t in btrades:
                b_exits[t.exit_reason] = b_exits.get(t.exit_reason, 0) + 1
            # Trail width proxy: avg composite drives modifier
            avg_comp = np.mean([t.composite_score for t in btrades])
            trail_modifier = 1.3 if avg_comp > 0.3 else (0.75 if avg_comp < -0.3 else 1.0)
            gap1_stats[bname] = {
                "n":              len(btrades),
                "win_rate":       round(len(bwin) / len(btrades) * 100, 1),
                "avg_pnl":        round(np.mean(b_pnl), 3),
                "median_pnl":     round(np.median(b_pnl), 3),
                "std_pnl":        round(np.std(b_pnl), 3),
                "avg_win":        round(np.mean([t.pnl_pct*100 for t in bwin]), 3) if bwin else 0,
                "avg_loss":       round(np.mean([t.pnl_pct*100 for t in blose]), 3) if blose else 0,
                "avg_hold":       round(np.mean(b_hold), 1),
                "median_hold":    round(np.median(b_hold), 1),
                "avg_composite":  round(avg_comp, 3),
                "trail_modifier": trail_modifier,
                "exit_breakdown": b_exits,
            }

        # Strong vs Weak delta
        s_pnl = gap1_stats.get("Strong", {}).get("avg_pnl", 0)
        w_pnl = gap1_stats.get("Weak",   {}).get("avg_pnl", 0)
        gap1_delta = round(s_pnl - w_pnl, 3)

        # ── EXIT PATH QUALITY ───────────────────────────────────────────────────
        exit_quality = {}
        for reason in set(t.exit_reason for t in trades):
            group = [t for t in trades if t.exit_reason == reason]
            gwin = [t for t in group if t.pnl > 0]
            exit_quality[reason] = {
                "n":        len(group),
                "win_rate": round(len(gwin) / len(group) * 100, 1),
                "avg_pnl":  round(np.mean([t.pnl_pct * 100 for t in group]), 3),
                "avg_hold": round(np.mean([t.hold_days for t in group]), 1),
            }

        # ── HOLD-DAY DISTRIBUTION ───────────────────────────────────────────────
        hold_arr = np.array([t.hold_days for t in trades])
        hold_dist = {
            "p25": float(np.percentile(hold_arr, 25)),
            "p50": float(np.percentile(hold_arr, 50)),
            "p75": float(np.percentile(hold_arr, 75)),
            "p90": float(np.percentile(hold_arr, 90)),
            "max": float(hold_arr.max()),
        }

        # ── REGIME BREAKDOWN ────────────────────────────────────────────────────
        regimes = {}
        for t in trades:
            r = t.regime or "unknown"
            if r not in regimes:
                regimes[r] = []
            regimes[r].append(t)
        regime_stats = {}
        for rname, rtrades in regimes.items():
            rwin = [t for t in rtrades if t.pnl > 0]
            regime_stats[rname] = {
                "n":        len(rtrades),
                "win_rate": round(len(rwin) / len(rtrades) * 100, 1),
                "avg_pnl":  round(np.mean([t.pnl_pct * 100 for t in rtrades]), 3),
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
            "gap1_stats": gap1_stats,
            "gap1_delta": gap1_delta,
            "exit_quality": exit_quality,
            "hold_distribution": hold_dist,
            "regime_stats": regime_stats,
            "gap1_enabled": self.gap1_enabled,
        }

    def print_report(self, results):
        m = results["metrics"]
        if "error" in m:
            print(f"BACKTEST FAILED: {m['error']}")
            return

        r = []
        gap1_label = "GAP 1 ACTIVE" if m.get("gap1_enabled", True) else "GAP 1 DISABLED (baseline)"
        r.append("=" * 65)
        r.append(f"  RAPTOR v5.4 BACKTEST REPORT  [{gap1_label}]")
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

        # ── GAP 1 SIGNAL-QUALITY DIAGNOSTICS ───────────────────────────────────
        r.append("=" * 65)
        r.append("  GAP 1 — Signal-Aware Trail Modifier (LIVE in backtest)")
        r.append("  composite_score drives trail width: Strong=1.3x  Neutral=1.0x  Weak=0.75x")
        r.append("")
        r.append(f"  {'Bucket':<10} {'N':>5} {'Win%':>6} {'AvgPnL%':>8} {'MedPnL%':>8} "                 f"{'StdPnL%':>8} {'AvgHold':>8} {'TrailMod':>9} {'ExitMix'}")
        r.append("  " + "-" * 90)
        for bname in ["Strong", "Neutral", "Weak"]:
            b = m.get("gap1_stats", {}).get(bname, {})
            if not b:
                continue
            exits = b.get("exit_breakdown", {})
            exit_str = "  ".join(f"{k[:4]}:{v}" for k, v in sorted(exits.items(), key=lambda x: -x[1]))
            r.append(f"  {bname:<10} {b['n']:>5} {b['win_rate']:>5.1f}% {b['avg_pnl']:>8.3f} "                     f"{b['median_pnl']:>8.3f} {b['std_pnl']:>8.3f} {b['avg_hold']:>8.1f} "                     f"{b['trail_modifier']:>9.2f}x  {exit_str}")
        r.append("")
        r.append(f"  Strong vs Weak PnL delta: {m.get('gap1_delta', 0):+.3f}%  "                 f"{'[GAP 1 VALIDATED]' if m.get('gap1_delta', 0) > 5 else '[NEEDS CALIBRATION]'}")
        r.append("")

        # ── EXIT PATH QUALITY ───────────────────────────────────────────────────
        r.append("=" * 65)
        r.append("  EXIT PATH QUALITY")
        r.append("")
        r.append(f"  {'Exit Path':<18} {'N':>5} {'Win%':>6} {'AvgPnL%':>9} {'AvgHold':>8}")
        r.append("  " + "-" * 50)
        eq = m.get("exit_quality", {})
        for path in ["trail_profit", "profit_target", "momentum_break", "trail_loss", "hard_stop", "time_stop"]:
            if path in eq:
                e = eq[path]
                r.append(f"  {path:<18} {e['n']:>5} {e['win_rate']:>5.1f}% {e['avg_pnl']:>9.3f} {e['avg_hold']:>8.1f}")
        r.append("")

        # ── HOLD-DAY DISTRIBUTION ───────────────────────────────────────────────
        r.append("=" * 65)
        r.append("  HOLD-DAY DISTRIBUTION")
        r.append("")
        hd = m.get("hold_distribution", {})
        r.append(f"    P25: {hd.get('p25',0):.1f}d    P50: {hd.get('p50',0):.1f}d    "                 f"P75: {hd.get('p75',0):.1f}d    P90: {hd.get('p90',0):.1f}d    Max: {hd.get('max',0):.1f}d")
        r.append("")

        # ── REGIME BREAKDOWN ────────────────────────────────────────────────────
        rs = m.get("regime_stats", {})
        if rs:
            r.append("=" * 65)
            r.append("  REGIME BREAKDOWN")
            r.append("")
            r.append(f"  {'Regime':<20} {'N':>5} {'Win%':>6} {'AvgPnL%':>9}")
            r.append("  " + "-" * 45)
            for rname, rv in sorted(rs.items(), key=lambda x: -x[1].get('n', 0)):
                r.append(f"  {rname:<20} {rv['n']:>5} {rv['win_rate']:>5.1f}% {rv['avg_pnl']:>9.3f}")
            r.append("")

        # ── PER-BOOK BREAKDOWN ─────────────────────────────────────────────────
        bs = m.get("book_stats", {})
        if bs:
            r.append("=" * 65)
            r.append("  PER-BOOK BREAKDOWN")
            r.append("")
            for book in ["MOMENTUM", "MEAN_REVERSION"]:
                bdata = bs.get(book)
                if not bdata:
                    r.append(f"  {book}: no trades")
                    continue
                r.append(f"  {book}")
                r.append(f"    Trades: {bdata['n']}  Win%: {bdata['win_rate']:.1f}%  "                         f"AvgPnL: {bdata['avg_pnl']:+.3f}%  AvgHold: {bdata['avg_hold']:.1f}d  PF: {bdata['pf']:.2f}")
                r.append(f"    Avg Win: {bdata['avg_win']:+.3f}%  Avg Loss: {bdata['avg_loss']:+.3f}%")
                exits_str = "  ".join(f"{k[:6]}:{v}" for k,v in sorted(bdata['exits'].items(), key=lambda x:-x[1]))
                r.append(f"    Exits: {exits_str}")
                pats_str = "  ".join(f"{k}:{v}" for k,v in list(bdata['patterns'].items())[:4])
                r.append(f"    Patterns: {pats_str or 'none'}")
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

    parser = argparse.ArgumentParser(description="Raptor v5.4 Backtester")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--no-gap1", action="store_true",
                        help="Disable GAP 1 signal-quality trail modifier (reproduces pre-GAP1 baseline)")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbol list e.g. AAPL,MSFT,NVDA (overrides universe file)")
    args = parser.parse_args()

    cfg = CONFIG
    try:
        cfg.validate_all()
    except AssertionError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    gap1 = not args.no_gap1
    print(f"  GAP 1 modifier: {'ENABLED' if gap1 else 'DISABLED (baseline mode)'}\n")

    bt = Backtester(cfg, gap1_enabled=gap1)
    sym_override = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    results = bt.run(symbols=sym_override, start=args.start, end=args.end)

    if results:
        bt.print_report(results)
        bt.save_results(results)
    else:
        print("Backtest returned no results.")
