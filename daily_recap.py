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
    # Inject market_value (qty × current_price) — not returned by data_feeds.get_positions()
    for p in positions:
        if "market_value" not in p:
            p["market_value"] = p.get("qty", 0) * p.get("current_price", 0)
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

    # Avg hold days for annualization — derive from closed trade records
    hold_days_list = [float(t.get("hold_days", 0)) for t in closed_trades if t.get("hold_days")]
    avg_hold_days = float(np.mean(hold_days_list)) if hold_days_list else 1.0

    gross_profit = float(np.sum(wins))
    gross_loss = abs(float(np.sum(losses))) if len(losses) > 0 else 1e-9
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    # Sharpe / Sortino — annualize correctly by trade frequency.
    # Bug was: sqrt(252) treats each trade as one trading day.
    # Fix: sqrt(252 / avg_hold_days) — each trade covers avg_hold_days days,
    # so there are 252/avg_hold_days independent trade-periods per year.
    # Ref: Lo (2002) "The Statistics of Sharpe Ratios".
    mean_r = np.mean(r)
    std_r = np.std(r, ddof=1)
    downside = r[r < 0]
    downside_std = np.std(downside, ddof=1) if len(downside) > 1 else 1e-9
    ann_factor = np.sqrt(252.0 / max(avg_hold_days, 1.0))

    sharpe  = (mean_r / std_r)  * ann_factor if std_r > 0 else 0.0
    sortino = (mean_r / downside_std) * ann_factor if downside_std > 0 else 0.0

    # Max drawdown on cumulative equity curve
    cum = np.cumprod(1 + r)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    max_dd = float(np.min(dd) * 100)

    # ── H-4: Exit reason breakdown ───────────────────────────────────────────
    exit_reasons = {}
    hold_by_reason = {}
    for t in closed_trades:
        reason = (t.get("exit_reason") or t.get("exit_path") or "unknown").lower()
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        hd = float(t.get("hold_days", 0))
        hold_by_reason.setdefault(reason, []).append(hd)
    avg_hold_by_reason = {k: round(float(np.mean(v)), 1) for k, v in hold_by_reason.items()}
    n = len(closed_trades)
    exit_pct = {k: round(v / n * 100, 1) for k, v in exit_reasons.items()}

    # ── H-4: Rolling 10-trade win rate ────────────────────────────────────────
    last10 = r[-10:] if len(r) >= 10 else r
    roll10_wr = round(float(np.sum(last10 > 0) / len(last10) * 100), 1)

    # ── H-4: Trim efficiency from trim_log.json ───────────────────────────────
    # trim_efficiency = % of trims where position continued to decay after trim
    # (i.e. trim was correct). Loaded here from file; graceful if missing.
    trim_efficiency = None
    try:
        import json, os
        if os.path.exists("trim_log.json"):
            tl = json.load(open("trim_log.json"))
            if tl:
                # A trim is "efficient" if the position was eventually stopped or
                # thesis-exited after the trim (not a full re-add). Best proxy
                # available without cross-referencing: count trims followed by
                # a hard_stop/trail exit in outcome_log for the same symbol.
                # Simple version: ratio of TRIM_MAJOR+EXIT vs TRIM_MINOR actions
                # (more aggressive trims on decaying positions = higher confidence)
                major = sum(1 for x in tl if x.get("action","") in ("TRIM_MAJOR","EXIT"))
                trim_efficiency = round(major / len(tl) * 100, 1) if tl else None
    except Exception:
        pass

    # ── H-4: Consecutive loss streak ─────────────────────────────────────────
    max_consec_loss = 0
    cur_consec = 0
    for ret in r:
        if ret < 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0
    current_streak = cur_consec  # still ongoing if last trade was a loss

    # ── H-4: Capital efficiency (realized PnL / max capital deployed) ─────────
    # Approximated from closed trades: sum of abs(pnl_pct * position_size) where
    # position_size is available, else fallback to mean equity assumption.
    cap_eff = None
    realized_pnl_sum = sum(float(t.get("pnl_usd", t.get("pnl", 0) or 0)) for t in closed_trades)
    cap_deployed = sum(
        abs(float(t.get("entry_value", t.get("market_value", 0) or 0)))
        for t in closed_trades
    )
    if cap_deployed > 0:
        cap_eff = round(realized_pnl_sum / cap_deployed * 100, 2)

    return {
        "n_trades": len(r),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy * 100, 3),
        "profit_factor": round(profit_factor, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_dd": round(max_dd, 2),
        # H-4 additions
        "exit_pct": exit_pct,
        "avg_hold_by_reason": avg_hold_by_reason,
        "roll10_wr": roll10_wr,
        "trim_efficiency": trim_efficiency,
        "max_consec_loss": max_consec_loss,
        "current_streak": current_streak,
        "cap_efficiency": cap_eff,
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
    Falls back to hold_health.json days_held when ledger entry_date is missing.
    """
    today = date.today()

    # Load hold_health as a fallback days source
    health_days = {}
    try:
        with open("hold_health.json") as f:
            hh = json.load(f)
        for sym, rec in hh.items():
            dh = rec.get("days_held")
            if dh is not None:
                health_days[sym] = int(dh)
    except Exception:
        pass

    # Build a clean symbol→record map regardless of "model:SYM" key format
    sym_map = {}
    for key, val in open_ledger.items():
        sym = val.get("symbol", key.split(":")[-1])
        sym_map[sym] = val

    result = {}
    for p in positions:
        sym = p["symbol"]
        meta = sym_map.get(sym, {})
        entry_date_str = meta.get("entry_date", meta.get("date", None))
        days = None
        if entry_date_str:
            try:
                ed = datetime.strptime(entry_date_str[:10], "%Y-%m-%d").date()
                days = (today - ed).days
            except Exception:
                pass

        # Fallback 1: hold_health.json days_held
        if days is None and sym in health_days:
            days = health_days[sym]

        # Fallback 2: show "New" if truly unknown
        if days is None:
            days = "New"

        m = meta.get("metadata", {})
        ledger_stop = m.get("stop", None)

        # No stop in ledger — do not fabricate via price * 0.02.
        # An invented ATR proxy produces a fake stop distance with no signal value.
        # Leave ledger_stop as None; display layer handles missing stop gracefully.
        if ledger_stop is None:
            pass  # Skip — real data or nothing

        regime = m.get("regime", "")
        if not regime:
            regime = "N/A"

        result[sym] = {
            "days": days,
            "stop": ledger_stop,
            "regime": regime,
            "t_stat": m.get("t_stat", None),
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# HTML BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_html(account, positions, entries, exits, signals, macro, scale,
               spy_price, spy_returns, spy_mas, analytics, open_ledger, portfolio_beta,
               closed_trades=None, universe_size=None):

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

        # Stop distance: % from current to stop (negative = stop is below current)
        if stop and current_price > 0:
            stop_dist = ((current_price - stop) / current_price) * 100
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
    # ── Recent closed trades table ───────────────────────────────────────────
    closed_trades_html = ""
    if closed_trades:
        # Sort by exit_date descending, show last 20
        def _exit_sort(t):
            d = t.get("exit_date") or t.get("exit_date", "")
            return str(d)[:10] if d else ""
        recent = sorted(closed_trades, key=_exit_sort, reverse=True)[:30]

        ct_rows = ""
        for t in recent:
            sym       = t.get("symbol", "?")
            pnl       = t.get("pnl_pct")
            exit_d    = str(t.get("exit_date", "?"))[:10]
            entry_d   = str(t.get("entry_date", "?"))[:10]
            reason    = t.get("exit_reason") or t.get("exit_path") or "?"
            hold_days = t.get("hold_days")
            pnl_abs   = t.get("pnl") or t.get("pnl_dollar")

            # pnl_pct should now be in % (e.g. 5.2 = 5.2%), convert if still decimal
            if pnl is not None:
                pnl_f = float(pnl)
                # If abs < 1.5, was stored as decimal — convert
                if abs(pnl_f) < 1.5:
                    pnl_f = pnl_f * 100
                pnl_color_t = "#00d4aa" if pnl_f >= 0 else "#ff4757"
                pnl_str = f"{pnl_f:+.2f}%"
            else:
                pnl_color_t = "#a0a0b0"
                pnl_str = "—"

            # Hold days
            if hold_days:
                hold_str = f"{int(float(hold_days))}d"
            elif entry_d and entry_d != "?" and exit_d and exit_d != "?":
                try:
                    from datetime import date as _date
                    hold_str = f"{(_date.fromisoformat(exit_d) - _date.fromisoformat(entry_d)).days}d"
                except Exception:
                    hold_str = "—"
            else:
                hold_str = "—"

            # Clean up reason display
            reason_display = reason.replace("math_trim_", "trim ").replace("_", " ")

            # pnl_abs
            abs_str = f"${float(pnl_abs):+,.0f}" if pnl_abs is not None else "—"

            ct_rows += f"""<tr style="border-bottom:1px solid #1e1e34">
                <td style="padding:7px 10px;color:#e0e0e0;font-weight:500">{sym}</td>
                <td style="padding:7px 10px;text-align:right;color:{pnl_color_t};font-weight:600">{pnl_str}</td>
                <td style="padding:7px 10px;text-align:right;color:#e0e0e0">{abs_str}</td>
                <td style="padding:7px 10px;text-align:right;color:#a0a0b0;font-size:12px">{reason_display}</td>
                <td style="padding:7px 10px;text-align:right;color:#a0a0b0;font-size:12px">{hold_str}</td>
                <td style="padding:7px 10px;text-align:right;color:#6a6a8a;font-size:11px">{exit_d}</td>
            </tr>"""

        _th = '<th style="padding:7px 10px;text-align:right;color:#00d4aa;font-size:11px;text-transform:uppercase">'
        closed_trades_html = (
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<tr style="border-bottom:2px solid #2a2a3e">'
            '<th style="padding:7px 10px;text-align:left;color:#00d4aa;font-size:11px;text-transform:uppercase">Symbol</th>'
            + _th + 'P&L %</th>'
            + _th + 'P&L $</th>'
            + _th + 'Exit Type</th>'
            + _th + 'Hold</th>'
            + _th + 'Exit Date</th>'
            + '</tr>' + ct_rows + '</table>'
        )
    else:
        closed_trades_html = '<div style="color:#6a6a8a;font-size:12px;padding:8px 0">No closed trades in ledger yet.</div>'

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
        wr_color   = "#00d4aa" if analytics.get('win_rate', 0) >= 50 else "#ffa502"
        exp_color  = "#00d4aa" if analytics.get('expectancy', 0) > 0 else "#ff4757"
        pf_color   = "#00d4aa" if analytics.get('profit_factor', 0) >= 1.5 else "#ffa502"
        sh_color   = "#00d4aa" if analytics.get('sharpe', 0) >= 1.0 else "#ffa502"
        so_color   = "#00d4aa" if analytics.get('sortino', 0) >= 1.5 else "#ffa502"
        # H-4: exit reason breakdown string
        ep = analytics.get("exit_pct", {})
        ahr = analytics.get("avg_hold_by_reason", {})
        exit_reason_parts = []
        for reason, pct in sorted(ep.items(), key=lambda x: -x[1]):
            avg_h = ahr.get(reason, 0)
            exit_reason_parts.append(f"{reason}: {pct:.0f}% ({avg_h:.0f}d avg)")
        exit_breakdown_str = " | ".join(exit_reason_parts) if exit_reason_parts else "—"

        roll10_wr = analytics.get("roll10_wr", 0)
        roll10_color = "#00d4aa" if roll10_wr >= 50 else "#ffa502" if roll10_wr >= 35 else "#ff4757"
        streak = analytics.get("current_streak", 0)
        streak_color = "#ff4757" if streak >= 3 else "#ffa502" if streak >= 2 else "#e0e0e0"
        te = analytics.get("trim_efficiency")
        te_str = f"{te:.0f}%" if te is not None else "N/A"
        ce = analytics.get("cap_efficiency")
        ce_str = f"{ce:.2f}%" if ce is not None else "N/A"

        analytics_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #1e1e34">
          <tr>
            {_metric_tile("Trades",        str(analytics.get('n_trades', 0)),                     "#e0e0e0")}
            {_metric_tile("Win Rate",       f"{analytics.get('win_rate', 0):.1f}%",                wr_color)}
            {_metric_tile("Avg Win",        f"+{analytics.get('avg_win', 0):.2f}%",               "#00d4aa")}
            {_metric_tile("Avg Loss",       f"{analytics.get('avg_loss', 0):.2f}%",               "#ff4757")}
            {_metric_tile("Expectancy",     f"{analytics.get('expectancy', 0):.3f}%",              exp_color)}
            {_metric_tile("Profit Factor",  f"{analytics.get('profit_factor', 0):.2f}",            pf_color)}
          </tr>
          <tr>
            {_metric_tile("Sharpe",         f"{analytics.get('sharpe', 0):.2f}",                   sh_color)}
            {_metric_tile("Sortino",        f"{analytics.get('sortino', 0):.2f}",                  so_color)}
            {_metric_tile("Max DD",         f"{analytics.get('max_dd', 0):.1f}%",                 "#ff4757")}
            {_metric_tile("Capital Util",   f"{capital_util:.1f}%",                                util_color)}
            {_metric_tile("Port Beta",      beta_str,                                              "#a0a0b0")}
            <td bgcolor="#0e0e20" style="background-color:#0e0e20;border-bottom:1px solid #1e1e34"></td>
          </tr>
          <tr>
            {_metric_tile("Roll10 WR",      f"{roll10_wr:.1f}%",                                   roll10_color)}
            {_metric_tile("Loss Streak",    f"{streak} trades",                                    streak_color)}
            {_metric_tile("Trim Effic.",    te_str,                                                "#a0a0b0")}
            {_metric_tile("Cap Effic.",     ce_str,                                                "#a0a0b0")}
            <td colspan="2" bgcolor="#0e0e20" style="background-color:#0e0e20;border-bottom:1px solid #1e1e34"></td>
          </tr>
        </table>
        <div style="padding:6px 0 2px 2px;font-size:11px;color:#6a6a8a">
          Exit breakdown: {exit_breakdown_str}
        </div>"""
    else:
        analytics_html = '<div style="color:#6a6a8a;font-size:12px;padding:8px 0">Insufficient trade history for analytics (need ≥ 3 closed trades)</div>'

    # ── Regime ────────────────────────────────────────────────────────────────
    regime = macro.get("regime", "NEUTRAL")
    regime_score = macro.get("macro_score", 0)  # key is macro_score, not score
    regime_colors = {"EXPANSION": "#00d4aa", "BULLISH": "#00d4aa", "NEUTRAL": "#ffa502",
                     "BEARISH": "#ff4757", "CRISIS": "#ff0000"}
    regime_color = regime_colors.get(regime, "#ffa502")

    spy_price_str = f"${spy_price:,.2f}" if spy_price else "N/A"
    universe_size_str = str(universe_size) if universe_size else "~150"

    html = f"""
    <html>
    <head><meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark"></head>
    <body style="margin:0;padding:0;background-color:#0a0a1a;font-family:'Segoe UI',Arial,sans-serif" bgcolor="#0a0a1a">
    <table width="100%" cellpadding="0" cellspacing="0" bgcolor="#0a0a1a" style="background-color:#0a0a1a">
    <tr><td align="center" style="padding:20px 0">
    <table width="720" cellpadding="0" cellspacing="0" bgcolor="#12122a" style="max-width:720px;background-color:#12122a;border:1px solid #2a2a3e">

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
                <span style="color:#e0e0e0;font-weight:700;font-size:14px">{universe_size_str} symbols</span>
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

        <!-- Recent Closed Trades -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Recent Closed Trades (Last 30)</div>
            {closed_trades_html}
        </div>

        <!-- Portfolio Analytics -->
        <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
            <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:10px">Portfolio Analytics (All Closed Trades)</div>
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

    </table>
    </td></tr>
    </table>
    </body>
    </html>
    """
    return html


def _metric_tile(label, value, color):
    """Reusable stat tile — renders as a <td> for email-safe table layout."""
    return f"""<td bgcolor="#0e0e20" style="background-color:#0e0e20;padding:12px 8px;text-align:center;border-bottom:1px solid #1e1e34;border-right:1px solid #1e1e34">
        <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase;letter-spacing:1px;white-space:nowrap">{label}</div>
        <div style="color:{color};font-size:16px;font-weight:700;margin-top:5px">{value}</div>
    </td>"""


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
        os.makedirs("logs", exist_ok=True)
        with open("logs/recap_errors.log", "a", encoding="utf-8") as lf:
            lf.write(f"[{datetime.now().isoformat()}] Recap email sent OK.\n")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Email failed: {e}")
        os.makedirs("logs", exist_ok=True)
        with open("logs/recap_errors.log", "a", encoding="utf-8") as lf:
            lf.write(f"[{datetime.now().isoformat()}] send_email() FAILED: {e}\n{tb}\n")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def send_error_email(error_text):
    """Send a plain-text fallback email if main() crashes before building the HTML."""
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER
        msg["Subject"] = f"RAPTOR Recap FAILED ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
        msg.attach(MIMEText(f"daily_recap.py crashed before sending the recap email.\n\nError:\n{error_text}", "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.send_message(msg)
        print("Error notification email sent.")
    except Exception as e2:
        print(f"Could not send error email either: {e2}")


def main(preview=False):
    os.makedirs("logs", exist_ok=True)
    log_path = os.path.join("logs", "recap_errors.log")

    try:
        dm, account, positions = get_account_data()
        entries, exits = get_todays_trades()
        signals, macro, scale, universe_size = get_signals(dm)

        # run_monitor is isolated — a failure here must not kill the email
        try:
            run_monitor()
        except Exception as e:
            import traceback
            msg = f"[{datetime.now().isoformat()}] run_monitor() failed: {e}\n{traceback.format_exc()}\n"
            print(msg)
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(msg)

        spy_price, spy_returns, spy_mas = get_spy_data(dm)
        closed_trades, open_ledger = get_ledger_data()
        analytics = get_portfolio_analytics(closed_trades, float(account["equity"]))
        portfolio_beta = get_position_beta(positions, dm)

        html = build_html(
            account, positions, entries, exits, signals, macro, scale,
            spy_price, spy_returns, spy_mas, analytics, open_ledger, portfolio_beta,
            closed_trades=closed_trades, universe_size=universe_size
        )

        if preview:
            with open("logs/recap_preview.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("Preview saved to logs/recap_preview.html")
        else:
            send_email(html, positions)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        msg = f"[{datetime.now().isoformat()}] FATAL: daily_recap.py crashed\n{tb}\n"
        print(msg)
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(msg)
        if not preview:
            send_error_email(tb)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="Save HTML locally instead of emailing")
    args = parser.parse_args()
    main(preview=args.preview)










