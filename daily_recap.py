"""
Raptor Daily Recap — Bloomberg-Style Email Report
===================================================
Sends a daily HTML email with:
  - Account summary (equity, cash, P&L)
  - Today's trades (entries + exits)
  - Current holdings with P&L + days held
  - Signal diagnostics (top candidates)
  - Cumulative P&L: 1-week, 1-month, 6-month, YTD vs SPY
  - SPY 20/50/200 MA status + price trend
  - Portfolio analytics: Sharpe, Sortino, Win Rate, Expectancy, Max DD
  - Capital utilization, beta exposure
  - Regime status, market scale, factor weights

Usage:
  python daily_recap.py              # Send recap email
  python daily_recap.py --preview    # Save HTML locally, don't email

Schedule at 4:30 PM ET via Task Scheduler.
"""

import logging
import os
import sys
import smtplib
import json
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import numpy as np
import pandas as pd

from config import CONFIG
from hold_monitor import build_health_html, run_monitor
from data_feeds import DataManager
from signals import QuantSignalEngine, FACTOR_NAMES

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("raptor.recap")

EMAIL_SENDER = "stevefirwin@gmail.com"
EMAIL_PASSWORD = "trhy qqzo kylt jker"
EMAIL_RECEIVER = "stevefirwin@gmail.com"

LEDGER_FILE = "position_ledger.json"
CRYPTO_LEDGER = "crypto_ledger.json"

# ─────────────────────────────────────────────────────────────────────────────
# DATA COLLECTION
# ─────────────────────────────────────────────────────────────────────────────

def get_account_data():
    dm = DataManager(CONFIG)
    account = dm.alpaca.get_account()
    positions = dm.alpaca.get_positions()
    return dm, account, positions


def get_spy_data(dm):
    """
    Fetch SPY bars with enough history for 200-day MA + YTD return.
    Returns (series_of_closes, dict_of_returns, dict_of_mas).
    
    FIX: Previously used lookback_days=max(periods)+10 which was too short
    for 252-day YTD. Now fetches 280 days and validates length before slicing.
    """
    try:
        spy_raw = dm.alpaca.get_daily_bars(["SPY"], lookback_days=320)

        # Handle both dict-of-dict and dict-of-DataFrame returns
        if "SPY" not in spy_raw:
            return None, {}, {}
        
        spy_entry = spy_raw["SPY"]
        if isinstance(spy_entry, pd.DataFrame):
            if "close" in spy_entry.columns:
                spy = spy_entry["close"].dropna()
            else:
                spy = spy_entry.iloc[:, 0].dropna()
        elif isinstance(spy_entry, dict):
            spy = pd.Series(spy_entry.get("close", [])).dropna()
        elif isinstance(spy_entry, pd.Series):
            spy = spy_entry.dropna()
        else:
            return None, {}, {}

        if len(spy) < 5:
            return None, {}, {}

        # ── Returns ──────────────────────────────────────────────────────────
        periods = {5: "1W", 21: "1M", 126: "6M", 252: "YTD"}
        returns = {}
        for days, label in periods.items():
            if len(spy) >= days + 1:
                ret = (spy.iloc[-1] / spy.iloc[-(days + 1)]) - 1
                returns[days] = round(ret * 100, 2)
            else:
                # Partial: use what we have
                returns[days] = round(((spy.iloc[-1] / spy.iloc[0]) - 1) * 100, 2) if len(spy) > 1 else 0.0

        # ── Moving averages ───────────────────────────────────────────────────
        mas = {}
        for window in [20, 50, 200]:
            if len(spy) >= window:
                mas[window] = round(float(spy.rolling(window).mean().iloc[-1]), 2)
            else:
                mas[window] = None

        current_price = round(float(spy.iloc[-1]), 2)
        return current_price, returns, mas

    except Exception as e:
        logger.warning(f"SPY data error: {e}")
        return None, {}, {}


def get_todays_trades():
    """Parse today's log for entries and exits."""
    today = datetime.now().strftime("%Y%m%d")
    entries, exits = [], []

    raptor_log = f"logs/raptor_{today}.log"
    if os.path.exists(raptor_log):
        with open(raptor_log) as f:
            for line in f:
                if "ORDER [v5.4]:" in line:
                    parts = line.strip().split("ORDER [v5.4]: ")[1]
                    entries.append(parts)

    exit_log = f"logs/exits_{today}.log"
    if os.path.exists(exit_log):
        seen_exits = set()  # deduplicate same symbol appearing in multiple runs
        with open(exit_log) as f:
            for line in f:
                # Only count real executions — "Order submitted" only fires on live orders,
                # not dry-runs. The EXIT 1/2/3/4/5 diagnostic lines fire on every run
                # including dry-runs so we deliberately ignore them here.
                if "SELL" in line and "Order submitted" in line:
                    parts = line.strip().split("Order submitted: ")[1] if "Order submitted: " in line else line.strip()
                    # Deduplicate: extract symbol from client_order_id pattern
                    sym = parts.split("_")[1] if "_" in parts else parts[:4]
                    if sym not in seen_exits:
                        seen_exits.add(sym)
                        exits.append(parts)

    return entries, exits


def get_ledger_data():
    """
    Load closed trades from position_ledger.json.
    Returns list of closed trade dicts with: symbol, pnl_pct, pnl_dollar,
    entry_date, exit_date, hold_days, exit_reason.
    Also returns open position metadata for days_held.
    """
    if not os.path.exists(LEDGER_FILE):
        return [], {}

    try:
        with open(LEDGER_FILE) as f:
            ledger = json.load(f)
    except Exception:
        return [], {}

    closed = ledger.get("closed", [])
    open_pos = ledger.get("positions", {})
    return closed, open_pos


def get_portfolio_analytics(closed_trades, equity):
    """
    Compute Sharpe, Sortino, win rate, avg win, avg loss, expectancy,
    max drawdown, profit factor from closed trade list.
    Also computes: exit reason breakdown, avg hold by exit reason,
    rolling 10-trade win rate, consecutive loss streak, capital efficiency.
    Uses per-trade P&L % as the return series.
    """
    if not closed_trades:
        return {}

    returns = []
    for t in closed_trades:
        pnl = t.get("pnl_pct", t.get("return_pct", None))
        if pnl is not None:
            returns.append(float(pnl) / 100.0)

    if len(returns) < 3:
        return {}

    r = np.array(returns)
    wins = r[r > 0]
    losses = r[r < 0]

    win_rate = len(wins) / len(r) * 100
    avg_win = float(np.mean(wins) * 100) if len(wins) > 0 else 0.0
    avg_loss = float(np.mean(losses) * 100) if len(losses) > 0 else 0.0
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    gross_profit = float(np.sum(wins))
    gross_loss = abs(float(np.sum(losses))) if len(losses) > 0 else 1e-9
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    # Sharpe / Sortino — per-trade return series annualized correctly.
    # Each observation is one trade, not one day, so the annualization factor
    # is sqrt(252 / avg_hold_days) — i.e. how many trades fit in a trading year.
    hold_days_list = [float(t.get("hold_days") or 1) for t in closed_trades if t.get("hold_days")]
    avg_hold_days = float(np.mean(hold_days_list)) if hold_days_list else 1.0
    avg_hold_days = max(avg_hold_days, 1.0)
    ann_factor = np.sqrt(252.0 / avg_hold_days)

    mean_r = np.mean(r)
    std_r = np.std(r, ddof=1)
    downside = r[r < 0]
    downside_std = np.std(downside, ddof=1) if len(downside) > 1 else 1e-9

    sharpe = (mean_r / std_r) * ann_factor if std_r > 0 else 0.0
    sortino = (mean_r / downside_std) * ann_factor if downside_std > 0 else 0.0

    # Max drawdown on cumulative equity curve
    cum = np.cumprod(1 + r)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    max_dd = float(np.min(dd) * 100)

    # ── NEW METRIC 1: Exit reason breakdown ──────────────────────────────────
    # Count exits and win rate per exit path. Groups: hard_stop, trail (trail_profit
    # + trail_loss), profit_target, thesis_invalid, time_decay, portfolio_heat, other.
    exit_groups = {
        "hard_stop":    ["hard_stop"],
        "trail":        ["trail_profit", "trail_loss", "trailing_stop"],
        "profit_target":["profit_target"],
        "thesis":       ["thesis_invalid", "momentum_break"],
        "time_decay":   ["time_decay"],
        "port_heat":    ["portfolio_heat"],
    }
    exit_breakdown = {}
    for label, paths in exit_groups.items():
        group = [t for t in closed_trades
                 if any(p in str(t.get("exit_reason", "") or t.get("actual_exit_path", ""))
                        for p in paths)
                 and t.get("pnl_pct") is not None]
        if not group:
            continue
        gpnls = [float(t["pnl_pct"]) for t in group]
        gwins = sum(1 for p in gpnls if p > 0)
        ghold = [float(t["hold_days"]) for t in group if t.get("hold_days")]
        exit_breakdown[label] = {
            "n":        len(group),
            "pct":      round(len(group) / len(closed_trades) * 100, 1),
            "win_rate": round(gwins / len(group) * 100, 1),
            "avg_pnl":  round(float(np.mean(gpnls)), 2),
            "avg_hold": round(float(np.mean(ghold)), 1) if ghold else None,
        }

    # ── NEW METRIC 2: Rolling 10-trade win rate ──────────────────────────────
    recent_10 = [t for t in closed_trades[-10:] if t.get("pnl_pct") is not None]
    if recent_10:
        r10_wins = sum(1 for t in recent_10 if float(t["pnl_pct"]) > 0)
        rolling_10_wr = round(r10_wins / len(recent_10) * 100, 1)
    else:
        rolling_10_wr = None

    # ── NEW METRIC 3: Consecutive loss streak ────────────────────────────────
    streak = 0
    max_streak = 0
    cur_streak = 0
    for t in reversed(closed_trades):
        pnl = t.get("pnl_pct")
        if pnl is None:
            break
        if float(pnl) < 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
            if streak == 0:
                streak = cur_streak  # current active streak
        else:
            if streak == 0:
                streak = 0  # streak broken before we counted anything
            cur_streak = 0
            break
    # streak = current active loss streak (0 if last trade was a win)
    # max_streak = worst streak in history

    # ── NEW METRIC 4: Capital efficiency ─────────────────────────────────────
    # Realized PnL ($) / max capital deployed ($) across closed trades.
    # Uses entry_price * shares (qty) when available, else skips.
    total_realized_pnl = 0.0
    total_max_deployed = 0.0
    for t in closed_trades:
        ep = t.get("entry_price") or t.get("avg_entry")
        qty = t.get("shares") or t.get("qty")
        pnl_d = t.get("pnl")
        if ep and qty and pnl_d is not None:
            try:
                deployed = float(ep) * float(qty)
                total_max_deployed += deployed
                total_realized_pnl += float(pnl_d)
            except Exception:
                pass
    cap_efficiency = round((total_realized_pnl / total_max_deployed) * 100, 2) \
        if total_max_deployed > 0 else None

    return {
        "n_trades":        len(r),
        "win_rate":        round(win_rate, 1),
        "avg_win":         round(avg_win, 2),
        "avg_loss":        round(avg_loss, 2),
        "expectancy":      round(expectancy * 100, 3),
        "profit_factor":   round(profit_factor, 2),
        "sharpe":          round(sharpe, 2),
        "sortino":         round(sortino, 2),
        "max_dd":          round(max_dd, 2),
        "avg_hold_days":   round(avg_hold_days, 1),
        "exit_breakdown":  exit_breakdown,
        "rolling_10_wr":   rolling_10_wr,
        "loss_streak":     streak,
        "max_loss_streak": max_streak,
        "cap_efficiency":  cap_efficiency,
    }


def get_signals(dm):
    """Run signal engine for today's diagnostics."""
    try:
        from universe_builder import UniverseBuilder
        ub = UniverseBuilder(CONFIG)
        universe = ub.build(max_symbols=150)
    except Exception:
        universe = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "SPY"]

    if "SPY" not in universe:
        universe.append("SPY")

    dataset = dm.get_full_dataset(universe, lookback_days=CONFIG.signals.lookback_days)
    engine = QuantSignalEngine(CONFIG)
    signals = engine.generate_signals(dataset["bars"], dataset["macro"],
                                       dataset["sentiment"], dataset["bars"].get("SPY"))
    macro = dataset["macro"]
    scale = engine._market_scale(dataset["bars"].get("SPY"))
    return signals, macro, scale, len(universe)


def get_capital_utilization(positions, equity):
    """Market value of all positions as % of total equity."""
    if not equity or equity == 0:
        return 0.0
    total_mv = sum(abs(float(p.get("market_value", 0))) for p in positions)
    return round((total_mv / float(equity)) * 100, 1)


def get_position_beta(positions, dm):
    """
    Approximate portfolio beta vs SPY using 60-day price correlation.
    Returns weighted average beta.
    """
    if not positions:
        return None
    try:
        symbols = [p["symbol"] for p in positions]
        all_syms = list(set(symbols + ["SPY"]))
        bars = dm.alpaca.get_daily_bars(all_syms, lookback_days=65)

        def to_returns(entry):
            if isinstance(entry, pd.DataFrame):
                s = entry["close"] if "close" in entry.columns else entry.iloc[:, 0]
            elif isinstance(entry, dict):
                s = pd.Series(entry.get("close", []))
            elif isinstance(entry, pd.Series):
                s = entry
            else:
                return None
            return s.dropna().pct_change().dropna()

        spy_ret = to_returns(bars.get("SPY"))
        if spy_ret is None or len(spy_ret) < 20:
            return None

        spy_var = spy_ret.var()
        if spy_var == 0:
            return None

        total_mv = sum(abs(float(p.get("market_value", 1))) for p in positions)
        weighted_beta = 0.0
        for p in positions:
            sym = p["symbol"]
            weight = abs(float(p.get("market_value", 0))) / total_mv if total_mv > 0 else 0
            stock_ret = to_returns(bars.get(sym))
            if stock_ret is None or len(stock_ret) < 20:
                beta = 1.0  # fallback
            else:
                aligned = pd.concat([stock_ret, spy_ret], axis=1).dropna()
                if len(aligned) < 10:
                    beta = 1.0
                else:
                    cov = aligned.iloc[:, 0].cov(aligned.iloc[:, 1])
                    beta = cov / spy_var
            weighted_beta += weight * beta

        return round(weighted_beta, 2)
    except Exception as e:
        logger.warning(f"Beta calc error: {e}")
        return None


def get_days_held(open_ledger, positions):
    """
    Map symbol → (days_held, stop_price, regime, t_stat) from ledger.
    Ledger keys are "v5.4:SYM" — strip prefix before lookup.
    Falls back to ATR-based stop estimate and "N/A" regime when ledger
    is missing a symbol (e.g. after a ledger reset or manual Alpaca entry).
    """
    today = date.today()
    # Build a clean symbol→record map regardless of "model:SYM" key format
    sym_map = {}
    for key, val in open_ledger.items():
        sym = val.get("symbol", key.split(":")[-1])
        sym_map[sym] = val

    # Load hold_health.json once for stop_dist_atr fallback
    _hold_health = {}
    if os.path.exists("hold_health.json"):
        try:
            with open("hold_health.json") as _hf:
                _hold_health = json.load(_hf)
        except Exception:
            _hold_health = {}

    result = {}
    for p in positions:
        sym = p["symbol"]
        meta = sym_map.get(sym, {})
        entry_date_str = meta.get("entry_date", meta.get("date", None))
        if entry_date_str:
            try:
                ed = datetime.strptime(entry_date_str[:10], "%Y-%m-%d").date()
                days = (today - ed).days
            except Exception:
                days = "?"
        else:
            days = "?"

        m = meta.get("metadata", {})
        ledger_stop = m.get("stop", None)

        # Fallback chain for stop distance:
        # 1. Ledger stop price (most accurate — set at entry)
        # 2. hold_health.json stop_dist_atr (updated each monitor run)
        # 3. None — display "—" rather than show a wrong number
        health_stop_dist = None
        if ledger_stop is None:
            sym_health = _hold_health.get(sym, {})
            raw_dist = sym_health.get("stop_dist_atr")
            if raw_dist is not None:
                try:
                    health_stop_dist = float(raw_dist)
                except Exception:
                    pass

        regime = m.get("regime", "")
        if not regime:
            regime = "N/A"  # Show N/A instead of blank when ledger missing

        result[sym] = {
            "days": days,
            "stop": ledger_stop,
            "health_stop_dist": health_stop_dist,  # % from hold_health.json when ledger stop unavailable
            "regime": regime,
            "t_stat": m.get("t_stat", None),
        }
    return result


def get_trim_efficiency():
    """
    Trim efficiency from trim_log.json.
    A trim is 'efficient' when the position continued to decline after the trim
    (agent said HOLD but math trimmed and was right) or when composite < 0 at trim time.
    Returns: n_trims, pct_negative_composite, avg_pnl_at_trim, agent_vs_math_disagree_rate.
    """
    if not os.path.exists("trim_log.json"):
        return {}
    try:
        with open("trim_log.json") as f:
            trims = json.load(f)
    except Exception:
        return {}
    if not trims:
        return {}

    n = len(trims)
    neg_comp = sum(1 for t in trims if (t.get("composite") or 0) < 0)
    pnl_vals = [float(t["pnl_pct"]) for t in trims if t.get("pnl_pct") is not None]
    avg_pnl = round(float(np.mean(pnl_vals)), 2) if pnl_vals else None

    # Disagreement: agent said HOLD but math trimmed
    disagree = sum(1 for t in trims
                   if str(t.get("agent_decision", "")).upper() == "HOLD"
                   and "trim" in str(t.get("reason", "")).lower())
    disagree_rate = round(disagree / n * 100, 1) if n > 0 else 0.0

    return {
        "n_trims":       n,
        "pct_neg_comp":  round(neg_comp / n * 100, 1),
        "avg_pnl":       avg_pnl,
        "disagree_rate": disagree_rate,
    }


def get_agent_disagreement(closed_trades):
    """
    Agent vs math disagreement rate on full exits.
    Disagreement = HoldAgent said HOLD but exit_monitor fired a mechanical exit.
    Tracks whether agent overconfidence correlates with losses.
    Source: outcome_log.json hold_decision field.
    """
    if not os.path.exists("outcome_log.json"):
        return None
    try:
        with open("outcome_log.json") as f:
            records = json.load(f)
    except Exception:
        return None

    mechanical_exits = [
        "hard_stop", "trail_loss", "trail_profit", "trailing_stop",
        "profit_target", "time_decay", "portfolio_heat", "thesis_invalid",
        "momentum_break"
    ]
    eligible = [r for r in records
                if r.get("hold_decision") == "HOLD"
                and r.get("actual_exit_path") in mechanical_exits
                and r.get("actual_pnl_pct") is not None]
    if not eligible:
        return None

    total_eligible = len([r for r in records
                          if r.get("hold_decision") is not None
                          and r.get("actual_exit_path") is not None])
    n_disagree = len(eligible)
    n_disagree_loss = sum(1 for r in eligible if float(r["actual_pnl_pct"]) < 0)

    return {
        "n_disagree":      n_disagree,
        "total":           total_eligible,
        "disagree_rate":   round(n_disagree / max(total_eligible, 1) * 100, 1),
        "loss_rate_when_disagree": round(n_disagree_loss / max(n_disagree, 1) * 100, 1),
    }


def get_position_composite_regimes(positions, signals, open_ledger):
    """
    Per-position: composite score at entry (from ledger metadata) vs current
    (from today's signal run), and macro regime at entry vs current.
    Returns dict: symbol -> {entry_comp, current_comp, entry_regime, current_regime}
    """
    # Build signal lookup from today's run
    signal_map = {s.symbol: getattr(s, "composite_score", getattr(s, "t_statistic", None))
                  for s in signals}

    # Build regime from macro_context.json (current)
    current_regime = "N/A"
    if os.path.exists("macro_context.json"):
        try:
            with open("macro_context.json") as f:
                mc = json.load(f)
            current_regime = mc.get("macro_regime", mc.get("regime", "N/A"))
        except Exception:
            pass

    # Build ledger symbol map
    sym_map = {}
    for key, val in open_ledger.items():
        sym = val.get("symbol", key.split(":")[-1])
        sym_map[sym] = val

    result = {}
    for p in positions:
        sym = p["symbol"]
        meta = sym_map.get(sym, {}).get("metadata", {})
        entry_comp = meta.get("composite_score")
        entry_regime = meta.get("regime", "N/A") or "N/A"
        current_comp = signal_map.get(sym)
        result[sym] = {
            "entry_comp":    round(float(entry_comp), 3) if entry_comp is not None else None,
            "current_comp":  round(float(current_comp), 3) if current_comp is not None else None,
            "entry_regime":  entry_regime,
            "current_regime": current_regime,
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# HTML BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_html(account, positions, entries, exits, signals, macro, scale,
               spy_price, spy_returns, spy_mas, analytics, open_ledger, portfolio_beta,
               universe_size=0, trim_stats=None, agent_disagree=None, pos_comp_regimes=None):

    equity = float(account["equity"])
    cash = float(account["cash"])
    date_str = datetime.now().strftime("%B %d, %Y")
    time_str = datetime.now().strftime("%I:%M %p ET")

    # Portfolio P&L
    total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    pnl_color = "#00d4aa" if total_pnl >= 0 else "#ff4757"

    capital_util = get_capital_utilization(positions, equity)

    # ── Holdings table ────────────────────────────────────────────────────────
    ledger_map = get_days_held(open_ledger, positions)
    holdings_rows = ""
    sorted_pos = sorted(positions, key=lambda p: float(p.get("unrealized_pnl_pct", 0)), reverse=True)
    for p in sorted_pos:
        pnl_pct = float(p.get("unrealized_pnl_pct", 0)) * 100
        pnl_val = float(p.get("unrealized_pnl", 0))
        color = "#00d4aa" if pnl_pct >= 0 else "#ff4757"
        sym = p["symbol"]
        lmeta = ledger_map.get(sym, {})
        days = lmeta.get("days", "?")
        stop = lmeta.get("stop", None)
        regime = lmeta.get("regime", "")
        current_price = float(p.get("current_price", 0))

        # Stop distance: prefer ledger stop price → compute % dist.
        # Fallback: use stop_dist_atr from hold_health.json directly (already a %).
        if stop and current_price > 0:
            stop_dist = ((current_price - stop) / current_price) * 100
            stop_source = "ledger"
        elif lmeta.get("health_stop_dist") is not None:
            stop_dist = lmeta["health_stop_dist"]
            stop_source = "health"
        else:
            stop_dist = None
            stop_source = None

        if stop_dist is not None:
            # Red if within 5% of stop, orange if within 10%
            if stop_dist < 5:
                stop_color = "#ff4757"
            elif stop_dist < 10:
                stop_color = "#ffa502"
            else:
                stop_color = "#6a6a8a"
            stop_str = f"{stop_dist:.1f}%"
        else:
            stop_color = "#6a6a8a"
            stop_str = "—"

        # Regime badge — compact (format can be "TREND/BULL" or plain "TRENDING" or "N/A")
        regime_short = regime.split("/")[1] if "/" in regime else regime
        if not regime_short:
            regime_short = "N/A"
        regime_color = "#00d4aa" if "TREND" in regime.upper() else "#ffa502" if "MIXED" in regime.upper() else "#a0a0b0"

        holdings_rows += f"""
        <tr>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#e0e0e0;font-weight:600">{sym}</td>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#a0a0b0;text-align:right">{float(p.get('qty', 0)):.0f}</td>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#a0a0b0;text-align:right">${float(p.get('avg_entry', 0)):.2f}</td>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#a0a0b0;text-align:right">${current_price:.2f}</td>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:{color};text-align:right;font-weight:600">{pnl_pct:+.1f}%</td>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:{color};text-align:right">${pnl_val:+,.2f}</td>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:{stop_color};text-align:right">{stop_str}</td>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:{regime_color};text-align:right;font-size:11px">{regime_short}</td>
            <td style="padding:7px 10px;border-bottom:1px solid #2a2a3e;color:#6a6a8a;text-align:right">{days}d</td>
        </tr>"""

    if not holdings_rows:
        holdings_rows = '<tr><td colspan="9" style="padding:12px;color:#a0a0b0;text-align:center">No open positions</td></tr>'

    # ── Today's activity ──────────────────────────────────────────────────────
    activity_html = ""
    if entries:
        for e in entries:
            activity_html += f'<div style="padding:4px 0;color:#00d4aa">▲ {e}</div>'
    if exits:
        for e in exits:
            activity_html += f'<div style="padding:4px 0;color:#ff4757">▼ {e}</div>'
    if not entries and not exits:
        activity_html = '<div style="color:#a0a0b0">No trades today</div>'

    # ── Top signals ────────────────────────────────────────────────────────────
    signals_html = ""
    for s in signals[:5]:
        signals_html += f"""
        <div style="padding:6px 0;border-bottom:1px solid #1e1e34">
            <span style="color:#e0e0e0;font-weight:600;width:60px;display:inline-block">{s.symbol}</span>
            <span style="color:#a0a0b0">score={getattr(s, 'composite_score', getattr(s, 't_statistic', 0)):.3f}</span>
            <span style="color:#a0a0b0;margin-left:12px">kelly={s.kelly_fraction:.3f}</span>
            <span style="color:#a0a0b0;margin-left:12px">[{s.regime}]</span>
        </div>"""
    if not signals_html:
        signals_html = '<div style="color:#a0a0b0">No signals today. Patience.</div>'

    # ── SPY benchmark returns ─────────────────────────────────────────────────
    perf_html = ""
    labels = {5: "1 Week", 21: "1 Month", 126: "6 Month", 252: "YTD"}
    for days, label in labels.items():
        spy_ret = spy_returns.get(days, 0.0)
        ret_color = "#00d4aa" if spy_ret >= 0 else "#ff4757"
        perf_html += f"""
        <div style="display:inline-block;width:23%;text-align:center;padding:12px 0">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:1px">{label}</div>
            <div style="color:{ret_color};font-size:18px;font-weight:700;margin-top:4px">{spy_ret:+.1f}%</div>
        </div>"""

    # ── SPY moving averages ───────────────────────────────────────────────────
    spy_ma_html = ""
    if spy_price:
        for window, ma_val in spy_mas.items():
            if ma_val is None:
                status = "N/A"
                dot_color = "#6a6a8a"
                rel = ""
            else:
                above = spy_price >= ma_val
                dot_color = "#00d4aa" if above else "#ff4757"
                status = "ABOVE" if above else "BELOW"
                pct_diff = ((spy_price - ma_val) / ma_val) * 100
                rel = f"{pct_diff:+.1f}%"
            spy_ma_html += f"""
            <div style="display:inline-block;width:31%;text-align:center;padding:10px 4px">
                <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:1px">{window}-Day MA</div>
                <div style="color:#e0e0e0;font-size:15px;font-weight:700;margin-top:4px">${f"{ma_val:,.2f}" if ma_val else "N/A"}</div>
                <div style="color:{dot_color};font-size:11px;margin-top:2px">● {status} &nbsp; {rel}</div>
            </div>"""
    else:
        spy_ma_html = '<span style="color:#a0a0b0">SPY MA data unavailable</span>'

    # ── Portfolio analytics ───────────────────────────────────────────────────
    if analytics:
        beta_str = f"{portfolio_beta:.2f}" if portfolio_beta is not None else "N/A"
        util_color = "#ffa502" if capital_util > 80 else "#e0e0e0"

        # Rolling 10-trade win rate
        r10 = analytics.get("rolling_10_wr")
        r10_str = f"{r10:.1f}%" if r10 is not None else "N/A"
        r10_color = "#00d4aa" if (r10 or 0) >= 50 else "#ff4757"

        # Loss streak
        streak = analytics.get("loss_streak", 0)
        streak_color = "#ff4757" if streak >= 3 else "#ffa502" if streak >= 2 else "#6a6a8a"

        # Capital efficiency
        cap_eff = analytics.get("cap_efficiency")
        cap_eff_str = f"{cap_eff:+.2f}%" if cap_eff is not None else "N/A"
        cap_eff_color = "#00d4aa" if (cap_eff or 0) > 0 else "#ff4757"

        avg_hold = analytics.get("avg_hold_days", 0)

        analytics_html = f"""
        <div style="display:flex;flex-wrap:wrap;gap:0">
            {_metric_tile("Trades", str(analytics.get('n_trades', 0)), "#e0e0e0")}
            {_metric_tile("Win Rate", f"{analytics.get('win_rate', 0):.1f}%", "#00d4aa" if analytics.get('win_rate', 0) >= 50 else "#ffa502")}
            {_metric_tile("Last 10 WR", r10_str, r10_color)}
            {_metric_tile("Avg Win", f"+{analytics.get('avg_win', 0):.2f}%", "#00d4aa")}
            {_metric_tile("Avg Loss", f"{analytics.get('avg_loss', 0):.2f}%", "#ff4757")}
            {_metric_tile("Expectancy", f"{analytics.get('expectancy', 0):.3f}%", "#00d4aa" if analytics.get('expectancy', 0) > 0 else "#ff4757")}
            {_metric_tile("Profit Factor", f"{analytics.get('profit_factor', 0):.2f}", "#00d4aa" if analytics.get('profit_factor', 0) >= 1.5 else "#ffa502")}
            {_metric_tile("Sharpe", f"{analytics.get('sharpe', 0):.2f}", "#00d4aa" if analytics.get('sharpe', 0) >= 1.0 else "#ffa502")}
            {_metric_tile("Sortino", f"{analytics.get('sortino', 0):.2f}", "#00d4aa" if analytics.get('sortino', 0) >= 1.5 else "#ffa502")}
            {_metric_tile("Max DD", f"{analytics.get('max_dd', 0):.1f}%", "#ff4757")}
            {_metric_tile("Avg Hold", f"{avg_hold:.1f}d", "#a0a0b0")}
            {_metric_tile("Capital Util", f"{capital_util:.1f}%", util_color)}
            {_metric_tile("Cap Efficiency", cap_eff_str, cap_eff_color)}
            {_metric_tile("Port Beta", beta_str, "#a0a0b0")}
            {_metric_tile("Loss Streak", str(streak), streak_color)}
        </div>"""

        # ── Exit reason breakdown table ───────────────────────────────────────
        breakdown = analytics.get("exit_breakdown", {})
        if breakdown:
            exit_rows = ""
            label_map = {
                "hard_stop": "Hard Stop", "trail": "Trail Stop",
                "profit_target": "Profit Target", "thesis": "Thesis Invalid",
                "time_decay": "Time Decay", "port_heat": "Portfolio Heat",
            }
            for key, data in sorted(breakdown.items(), key=lambda x: -x[1]["n"]):
                wr_color = "#00d4aa" if data["win_rate"] >= 50 else "#ff4757"
                pnl_color_e = "#00d4aa" if data["avg_pnl"] >= 0 else "#ff4757"
                hold_str = f"{data['avg_hold']:.1f}d" if data["avg_hold"] is not None else "—"
                exit_rows += f"""
                <tr style="border-bottom:1px solid #1e1e34">
                    <td style="padding:5px 8px;color:#e0e0e0;font-size:12px">{label_map.get(key, key)}</td>
                    <td style="padding:5px 8px;color:#a0a0b0;text-align:right;font-size:12px">{data['n']} ({data['pct']}%)</td>
                    <td style="padding:5px 8px;text-align:right;font-size:12px;color:{wr_color}">{data['win_rate']}%</td>
                    <td style="padding:5px 8px;text-align:right;font-size:12px;color:{pnl_color_e}">{data['avg_pnl']:+.2f}%</td>
                    <td style="padding:5px 8px;color:#6a6a8a;text-align:right;font-size:12px">{hold_str}</td>
                </tr>"""
            analytics_html += f"""
        <div style="margin-top:12px">
            <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Exit Path Breakdown</div>
            <table style="width:100%;border-collapse:collapse">
                <tr style="border-bottom:1px solid #2a2a3e">
                    <th style="padding:4px 8px;text-align:left;color:#00d4aa;font-size:10px;text-transform:uppercase">Path</th>
                    <th style="padding:4px 8px;text-align:right;color:#00d4aa;font-size:10px;text-transform:uppercase">Count</th>
                    <th style="padding:4px 8px;text-align:right;color:#00d4aa;font-size:10px;text-transform:uppercase">Win%</th>
                    <th style="padding:4px 8px;text-align:right;color:#00d4aa;font-size:10px;text-transform:uppercase">Avg P&L</th>
                    <th style="padding:4px 8px;text-align:right;color:#00d4aa;font-size:10px;text-transform:uppercase">Avg Hold</th>
                </tr>
                {exit_rows}
            </table>
        </div>"""

        # ── Trim efficiency ───────────────────────────────────────────────────
        if trim_stats:
            trim_disagree_color = "#ffa502" if trim_stats.get("disagree_rate", 0) > 50 else "#00d4aa"
            analytics_html += f"""
        <div style="margin-top:12px">
            <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Trim Efficiency</div>
            <div style="display:flex;gap:0;flex-wrap:wrap">
                {_metric_tile("Trims", str(trim_stats.get('n_trims', 0)), "#e0e0e0")}
                {_metric_tile("Neg Comp at Trim", f"{trim_stats.get('pct_neg_comp', 0):.1f}%", "#ffa502")}
                {_metric_tile("Avg PnL at Trim", f"{trim_stats.get('avg_pnl', 0):+.2f}%" if trim_stats.get('avg_pnl') is not None else "N/A", "#a0a0b0")}
                {_metric_tile("Agent Disagree", f"{trim_stats.get('disagree_rate', 0):.1f}%", trim_disagree_color)}
            </div>
        </div>"""

        # ── Agent vs math disagreement (full exits) ───────────────────────────
        if agent_disagree:
            ad_color = "#ffa502" if agent_disagree.get("disagree_rate", 0) > 40 else "#00d4aa"
            loss_when_color = "#ff4757" if agent_disagree.get("loss_rate_when_disagree", 0) > 60 else "#ffa502"
            analytics_html += f"""
        <div style="margin-top:12px">
            <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Agent vs Math (Full Exits)</div>
            <div style="display:flex;gap:0;flex-wrap:wrap">
                {_metric_tile("Disagreements", str(agent_disagree.get('n_disagree', 0)), "#e0e0e0")}
                {_metric_tile("Disagree Rate", f"{agent_disagree.get('disagree_rate', 0):.1f}%", ad_color)}
                {_metric_tile("Loss When Disagree", f"{agent_disagree.get('loss_rate_when_disagree', 0):.1f}%", loss_when_color)}
            </div>
        </div>"""

    else:
        analytics_html = '<div style="color:#6a6a8a;font-size:12px;padding:8px 0">Insufficient trade history for analytics (need ≥ 3 closed trades)</div>'

    # ── Composite + regime per position ──────────────────────────────────────
    pos_intel_html = ""
    if pos_comp_regimes:
        pos_intel_rows = ""
        for sym, data in sorted(pos_comp_regimes.items()):
            ec = data.get("entry_comp")
            cc = data.get("current_comp")
            ec_str = f"{ec:+.3f}" if ec is not None else "—"
            cc_str = f"{cc:+.3f}" if cc is not None else "—"
            # Arrow direction: improving, declining, flat
            if ec is not None and cc is not None:
                delta = cc - ec
                arrow = "▲" if delta > 0.1 else "▼" if delta < -0.1 else "→"
                arr_color = "#00d4aa" if delta > 0.1 else "#ff4757" if delta < -0.1 else "#6a6a8a"
            else:
                arrow, arr_color = "?", "#6a6a8a"

            er = data.get("entry_regime", "N/A")
            cr = data.get("current_regime", "N/A")
            regime_match = er == cr
            regime_color_r = "#00d4aa" if regime_match else "#ffa502"

            pos_intel_rows += f"""
            <tr style="border-bottom:1px solid #1e1e34">
                <td style="padding:5px 8px;color:#e0e0e0;font-size:12px;font-weight:600">{sym}</td>
                <td style="padding:5px 8px;color:#a0a0b0;text-align:right;font-size:12px">{ec_str}</td>
                <td style="padding:5px 8px;color:#a0a0b0;text-align:right;font-size:12px">{cc_str}</td>
                <td style="padding:5px 8px;text-align:center;font-size:12px;color:{arr_color}">{arrow}</td>
                <td style="padding:5px 8px;color:{regime_color_r};text-align:right;font-size:11px">{er} → {cr}</td>
            </tr>"""

        if pos_intel_rows:
            pos_intel_html = f"""
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Position Intelligence (Composite Δ &amp; Regime)</div>
            <table style="width:100%;border-collapse:collapse">
                <tr style="border-bottom:1px solid #2a2a3e">
                    <th style="padding:4px 8px;text-align:left;color:#00d4aa;font-size:10px;text-transform:uppercase">Symbol</th>
                    <th style="padding:4px 8px;text-align:right;color:#00d4aa;font-size:10px;text-transform:uppercase">Entry Comp</th>
                    <th style="padding:4px 8px;text-align:right;color:#00d4aa;font-size:10px;text-transform:uppercase">Current Comp</th>
                    <th style="padding:4px 8px;text-align:center;color:#00d4aa;font-size:10px;text-transform:uppercase">Δ</th>
                    <th style="padding:4px 8px;text-align:right;color:#00d4aa;font-size:10px;text-transform:uppercase">Regime (Entry→Now)</th>
                </tr>
                {pos_intel_rows}
            </table>
        </div>"""

    # ── Regime ────────────────────────────────────────────────────────────────
    regime = macro.get("regime", macro.get("macro_regime", "NEUTRAL"))
    # Prefer FRED-computed score from data_feeds; fall back to vote-based score
    # computed from macro_context.json signals sub-dict (used when macro_context.py
    # ran instead of FRED — the key was "score" in FRED output, absent in macro_context.json).
    if "score" in macro and macro["score"] != 0:
        regime_score = round(float(macro["score"]), 3)
    else:
        _sig_map = {
            "BULLISH": 1, "BROAD_STRENGTH": 1, "TIGHT": 1, "RISK_ON": 1,
            "NEUTRAL": 0, "UNKNOWN": 0,
            "BEARISH": -1, "INVERTED": -1, "WIDE": -1, "CRISIS": -1,
        }
        _macro_signals = macro.get("signals", {})
        _votes = [_sig_map.get(v.get("regime", ""), 0)
                  for v in _macro_signals.values() if isinstance(v, dict)]
        regime_score = round(sum(_votes) / max(len(_votes), 1), 3) if _votes else 0.0
    regime_colors = {"EXPANSION": "#00d4aa", "BULLISH": "#00d4aa", "NEUTRAL": "#ffa502",
                     "BEARISH": "#ff4757", "CRISIS": "#ff0000"}
    regime_color = regime_colors.get(regime, "#ffa502")

    spy_price_str = f"${spy_price:,.2f}" if spy_price else "N/A"

    html = f"""
    <html>
    <body style="margin:0;padding:0;background:#0a0a1a;font-family:'Segoe UI',Arial,sans-serif">
    <div style="max-width:720px;margin:0 auto;background:#12122a;border:1px solid #2a2a3e">

        <!-- Header -->
        <div style="background:linear-gradient(135deg,#1a1a3e,#0d0d2b);padding:24px 28px;border-bottom:2px solid #00d4aa">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <div>
                    <div style="color:#00d4aa;font-size:13px;letter-spacing:3px;text-transform:uppercase;font-weight:700">RAPTOR v5.4</div>
                    <div style="color:#e0e0e0;font-size:22px;font-weight:700;margin-top:4px">Daily Intelligence Report</div>
                </div>
                <div style="text-align:right">
                    <div style="color:#a0a0b0;font-size:12px">{date_str}</div>
                    <div style="color:#a0a0b0;font-size:12px">{time_str}</div>
                </div>
            </div>
        </div>

        <!-- Account Summary -->
        <div style="padding:20px 28px;border-bottom:1px solid #2a2a3e">
            <div style="display:flex;justify-content:space-between">
                <div style="text-align:center;flex:1">
                    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:1px">Equity</div>
                    <div style="color:#e0e0e0;font-size:24px;font-weight:700">${equity:,.2f}</div>
                </div>
                <div style="text-align:center;flex:1">
                    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:1px">Cash</div>
                    <div style="color:#e0e0e0;font-size:24px;font-weight:700">${cash:,.2f}</div>
                </div>
                <div style="text-align:center;flex:1">
                    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:1px">Unrealized P&L</div>
                    <div style="color:{pnl_color};font-size:24px;font-weight:700">${total_pnl:+,.2f}</div>
                </div>
                <div style="text-align:center;flex:1">
                    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:1px">Positions</div>
                    <div style="color:#e0e0e0;font-size:24px;font-weight:700">{len(positions)}</div>
                </div>
            </div>
        </div>

        <!-- Regime & Scale -->
        <div style="padding:14px 28px;border-bottom:1px solid #2a2a3e;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px">
            <div>
                <span style="color:#a0a0b0;font-size:12px">MACRO REGIME: </span>
                <span style="color:{regime_color};font-weight:700;font-size:14px">{regime}</span>
                <span style="color:#a0a0b0;font-size:12px"> ({regime_score:.3f})</span>
            </div>
            <div>
                <span style="color:#a0a0b0;font-size:12px">MARKET SCALE: </span>
                <span style="color:#e0e0e0;font-weight:700;font-size:14px">{scale:.2f}x</span>
            </div>
            <div>
                <span style="color:#a0a0b0;font-size:12px">SPY: </span>
                <span style="color:#e0e0e0;font-weight:700;font-size:14px">{spy_price_str}</span>
            </div>
            <div>
                <span style="color:#a0a0b0;font-size:12px">UNIVERSE: </span>
                <span style="color:#e0e0e0;font-weight:700;font-size:14px">{universe_size} symbols</span>
            </div>
        </div>

        <!-- SPY Moving Averages -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">SPY Trend Structure</div>
            <div style="display:flex;justify-content:space-between">{spy_ma_html}</div>
        </div>

        <!-- SPY Benchmark Returns -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">SPY Benchmark Returns</div>
            <div style="display:flex;justify-content:space-between">{perf_html}</div>
        </div>

        <!-- Portfolio Analytics -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:10px">Portfolio Analytics (Closed Trades)</div>
            {analytics_html}
        </div>

        <!-- Today's Activity -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Today's Activity</div>
            <div style="font-size:13px;font-family:'Courier New',monospace">{activity_html}</div>
        </div>

        <!-- Holdings -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Current Holdings</div>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <tr style="border-bottom:2px solid #2a2a3e">
                    <th style="padding:8px 10px;text-align:left;color:#00d4aa;font-size:11px;text-transform:uppercase">Symbol</th>
                    <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Qty</th>
                    <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Entry</th>
                    <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Current</th>
                    <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">P&L %</th>
                    <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">P&L $</th>
                    <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Stop Dist</th>
                    <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Regime</th>
                    <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">Days</th>
                </tr>
                {holdings_rows}
            </table>
        </div>

        <!-- Position Health Monitor -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            {build_health_html()}
        </div>

        <!-- Position Intelligence: Composite Δ + Regime -->
        {pos_intel_html}

        <!-- Top Signals -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Signal Pipeline (Top 5)</div>
            <div style="font-size:13px;font-family:'Courier New',monospace">{signals_html}</div>
        </div>

        <!-- Footer -->
        <div style="padding:16px 28px;text-align:center">
            <div style="color:#3a3a5e;font-size:11px">RAPTOR v5.4 | 16 Factors | Inverse-Vol Weighted | Adaptive Ridge</div>
            <div style="color:#3a3a5e;font-size:10px;margin-top:4px">Paper Trading — Not Financial Advice</div>
        </div>

    </div>
    </body>
    </html>
    """
    return html


def _metric_tile(label, value, color):
    """Reusable stat tile for portfolio analytics grid."""
    return f"""
    <div style="width:33%;box-sizing:border-box;padding:10px 8px;border-bottom:1px solid #1e1e34;text-align:center">
        <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase;letter-spacing:1px">{label}</div>
        <div style="color:{color};font-size:16px;font-weight:700;margin-top:4px">{value}</div>
    </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────────────────────

def send_email(html, positions):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    date_str = datetime.now().strftime("%Y-%m-%d")

    total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    pnl_indicator = f" | ${total_pnl:+,.0f}" if positions else ""

    msg["Subject"] = f"RAPTOR v5.4 Daily Recap{pnl_indicator} ({date_str})"
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.send_message(msg)
        print("Recap email sent.")
    except Exception as e:
        print(f"Email failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main(preview=False):
    dm, account, positions = get_account_data()
    entries, exits = get_todays_trades()
    signals, macro, scale, universe_size = get_signals(dm)

    run_monitor()
    spy_price, spy_returns, spy_mas = get_spy_data(dm)
    closed_trades, open_ledger = get_ledger_data()
    analytics = get_portfolio_analytics(closed_trades, float(account["equity"]))
    portfolio_beta = get_position_beta(positions, dm)
    trim_stats = get_trim_efficiency()
    agent_disagree = get_agent_disagreement(closed_trades)
    pos_comp_regimes = get_position_composite_regimes(positions, signals, open_ledger)

    html = build_html(
        account, positions, entries, exits, signals, macro, scale,
        spy_price, spy_returns, spy_mas, analytics, open_ledger, portfolio_beta,
        universe_size, trim_stats, agent_disagree, pos_comp_regimes
    )

    if preview:
        os.makedirs("logs", exist_ok=True)
        with open("logs/recap_preview.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Preview saved to logs/recap_preview.html")
    else:
        send_email(html, positions)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="Save HTML locally instead of emailing")
    args = parser.parse_args()
    main(preview=args.preview)










