"""
RAPTOR Live Dashboard Server
=============================
Run: python raptor_dashboard.py
Access: http://<your-tailscale-ip>:7443  (or http://localhost:7443)

Serves a Bloomberg-style mobile dashboard reading all RAPTOR JSON files live.
Refreshes every 30s automatically. No Alpaca calls — reads local JSON only.

Files read (all from the same directory as this script):
  hold_health.json       — positions, health tiers, factors, trim signals
  macro_context.json     — regime, VIX, SPY trend, sector breadth
  outcome_log.json       — closed trades (win rate, Sharpe, etc.)
  hold_decisions.json    — agent advisory log
  raptor_state.json      — session stats, factor weights
  trim_log.json          — trim history
  position_ledger.json   — entry metadata
  market_decision.json   — market open/close decision
  entry_vetoes.json      — today's veto log

Put this file in your Raptor folder (same dir as hold_health.json etc).
"""

import json
import os
import glob
import math
from datetime import datetime, date, timedelta
from pathlib import Path
from flask import Flask, jsonify, send_file
from io import BytesIO

BASE = Path(__file__).parent
app = Flask(__name__)

# ─── Data loaders ────────────────────────────────────────────────────────────

def load(fname, default=None):
    p = BASE / fname
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_log_lines(pattern, max_lines=200):
    """Read most recent matching log file, return last N lines."""
    today = datetime.now().strftime("%Y%m%d")
    candidates = sorted(BASE.glob(f"logs/{pattern}*{today}*"), reverse=True)
    if not candidates:
        candidates = sorted(BASE.glob(f"logs/{pattern}*"), reverse=True)
    if not candidates:
        return []
    try:
        lines = candidates[0].read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except Exception:
        return []


# ─── Analytics ───────────────────────────────────────────────────────────────

def compute_analytics(closed_trades):
    if not closed_trades:
        return {}
    rets = [float(t.get("actual_pnl_pct", t.get("pnl_pct", 0))) / 100.0
            for t in closed_trades if t.get("actual_pnl_pct") is not None or t.get("pnl_pct") is not None]
    if len(rets) < 3:
        return {}
    import statistics
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    win_rate = len(wins) / len(rets) * 100
    avg_win = (sum(wins) / len(wins) * 100) if wins else 0
    avg_loss = (sum(losses) / len(losses) * 100) if losses else 0
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)
    profit_factor = (-sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999

    # Sharpe (simplified, annualised)
    mean_r = sum(rets) / len(rets)
    std_r = statistics.stdev(rets) if len(rets) > 1 else 1e-9
    avg_hold = sum(t.get("hold_days", 1) or 1 for t in closed_trades) / len(closed_trades)
    periods_per_year = 252 / max(avg_hold, 0.5)
    sharpe = (mean_r / std_r) * math.sqrt(periods_per_year)

    # Sortino
    down = [r for r in rets if r < 0]
    down_std = statistics.stdev(down) if len(down) > 1 else 1e-9
    sortino = (mean_r / down_std) * math.sqrt(periods_per_year)

    # Max drawdown (equity curve approximation)
    cum = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rets:
        cum *= (1 + r)
        if cum > peak:
            peak = cum
        dd = (peak - cum) / peak
        if dd > max_dd:
            max_dd = dd

    # Rolling 10-trade win rate
    last10 = rets[-10:]
    roll10_wr = len([r for r in last10 if r > 0]) / len(last10) * 100 if last10 else 0

    return {
        "total_trades": len(rets),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy * 100, 3),
        "profit_factor": round(profit_factor, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_dd": round(max_dd * 100, 2),
        "roll10_wr": round(roll10_wr, 1),
        "avg_hold_days": round(avg_hold, 1),
    }


# ─── API endpoints ────────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    health     = load("hold_health.json", {})
    macro      = load("macro_context.json", {})
    outcome    = load("outcome_log.json", [])
    decisions  = load("hold_decisions.json", [])
    state      = load("raptor_state.json", {})
    trim_log   = load("trim_log.json", [])
    ledger_raw = load("position_ledger.json", {})
    mkt_dec    = load("market_decision.json", {})
    vetoes     = load("entry_vetoes.json", [])

    # Closed trades from ledger
    closed_ledger = ledger_raw.get("closed", []) if isinstance(ledger_raw, dict) else []
    # Merge with outcome_log for most complete closed trade set
    closed_all = outcome + closed_ledger

    analytics = compute_analytics(outcome)

    # Latest agent decision per symbol
    agent_map = {}
    if isinstance(decisions, list):
        for d in decisions:
            sym = d.get("symbol")
            if sym:
                agent_map[sym] = d

    # Build positions list from hold_health
    positions = []
    for sym, rec in health.items():
        snap = rec.get("snapshot", {})
        layers = rec.get("layers", {})
        agent = agent_map.get(sym, {})
        positions.append({
            "symbol": sym,
            "tier": rec.get("tier", "UNKNOWN"),
            "health": round(rec.get("health", 0), 4),
            "pnl_pct": round(rec.get("pnl_pct", 0), 3),
            "days_held": rec.get("days_held", 0),
            "composite": round(rec.get("composite", 0), 4),
            "factors_positive": rec.get("factors_positive", 0),
            "stop_dist_atr": round(rec.get("stop_dist_atr", 0), 3),
            "decay_driver": rec.get("decay_driver", ""),
            "trim": rec.get("trim", {}),
            # Snapshot detail
            "entry_price": snap.get("entry_price"),
            "current_price": snap.get("current_price"),
            "qty": snap.get("qty"),
            "market_value": snap.get("market_value"),
            "stop_price": snap.get("stop_price"),
            "hold_target": snap.get("hold_target", 15),
            "hold_ratio": snap.get("hold_ratio", 0),
            "atr": snap.get("atr"),
            "roc_5d": snap.get("roc_5d"),
            "t_stat": snap.get("t_stat"),
            "regime": snap.get("regime", ""),
            "factor_scores": snap.get("factor_scores", {}),
            "factor_contributions": snap.get("factor_contributions", {}),
            "cluster_scores": snap.get("cluster_scores", {}),
            # Layers
            "layers": {k: {
                "score": round(v.get("score", 0), 4),
                "detail": v.get("detail", ""),
                "weight": v.get("weight", 0),
            } for k, v in layers.items()},
            # Agent
            "agent_decision": agent.get("decision", "—"),
            "agent_confidence": agent.get("confidence"),
            "agent_reasoning": agent.get("reasoning", ""),
            "agent_ts": agent.get("timestamp", ""),
        })

    # Sort: DECAYING first, then by health asc
    tier_order = {"DECAYING": 0, "STABLE": 1, "STRENGTHENING": 2, "INSUFFICIENT_DATA": 3}
    positions.sort(key=lambda p: (tier_order.get(p["tier"], 9), p["health"]))

    # Macro signals
    signals_raw = macro.get("signals", {})

    # Session stats from raptor_state
    session = state.get("session_stats", {})
    account = state.get("account", {})

    # Recent trims
    recent_trims = sorted(trim_log, key=lambda x: x.get("timestamp", ""), reverse=True)[:10] if trim_log else []

    # Recent closed trades
    recent_closed = sorted(
        [t for t in outcome if t.get("exit_date")],
        key=lambda x: x.get("exit_date", ""), reverse=True
    )[:15]

    # Log tail
    raptor_logs = load_log_lines("raptor_", 80)
    exit_logs   = load_log_lines("exits_",  40)

    # Totals
    total_pnl_pct = sum(p["pnl_pct"] for p in positions)
    total_mv = sum(p.get("market_value") or 0 for p in positions)

    return jsonify({
        "ts": datetime.now().isoformat(),
        "positions": positions,
        "macro": {
            "regime": macro.get("macro_regime", "UNKNOWN"),
            "summary": macro.get("agent_summary", ""),
            "vix": signals_raw.get("vix", {}),
            "spy_trend": signals_raw.get("spy_trend", {}),
            "sector_breadth": signals_raw.get("sector_breadth", {}),
            "yield_curve": signals_raw.get("yield_curve", {}),
            "credit_spread": signals_raw.get("credit_spread", {}),
            "macro_ts": macro.get("timestamp", ""),
        },
        "analytics": analytics,
        "session": session,
        "account": account,
        "total_pnl_pct": round(total_pnl_pct, 3),
        "total_mv": round(total_mv, 2),
        "recent_closed": recent_closed,
        "recent_trims": recent_trims,
        "market_decision": mkt_dec,
        "raptor_log": raptor_logs,
        "exit_log": exit_logs,
        "vetoes": vetoes[-20:] if vetoes else [],
    })


@app.route("/api/logs")
def api_logs():
    raptor = load_log_lines("raptor_", 150)
    exits  = load_log_lines("exits_",  60)
    return jsonify({"raptor": raptor, "exits": exits, "ts": datetime.now().isoformat()})


@app.route("/")
def index():
    return DASHBOARD_HTML


# ─── Dashboard HTML (single-file, mobile-first) ───────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>RAPTOR</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #050508;
    --bg2: #0a0a10;
    --bg3: #0f0f18;
    --border: #1a1a28;
    --border2: #252535;
    --text: #e2e4f0;
    --muted: #5a5a78;
    --muted2: #3a3a52;
    --accent: #00d4aa;
    --accent2: #0099cc;
    --red: #ff4466;
    --yellow: #ffc233;
    --green: #00d4aa;
    --purple: #9b6dff;
    --orange: #ff7733;
    --mono: 'IBM Plex Mono', monospace;
    --sans: 'IBM Plex Sans', sans-serif;
    --safe-bottom: env(safe-area-inset-bottom, 0px);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html { background: var(--bg); }
  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100dvh;
    overflow-x: hidden;
  }
  /* ── Header ── */
  .header {
    position: sticky; top: 0; z-index: 100;
    background: rgba(5,5,8,0.96);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
    padding: 10px 16px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .header-logo {
    font-family: var(--mono); font-size: 13px; font-weight: 600;
    letter-spacing: 3px; color: var(--accent);
  }
  .header-right { display: flex; align-items: center; gap: 10px; }
  .pulse-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--accent);
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.7); }
  }
  .ts-label { font-family: var(--mono); font-size: 10px; color: var(--muted); }
  /* ── Tabs ── */
  .tabs {
    display: flex; overflow-x: auto; gap: 0;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
    scrollbar-width: none;
  }
  .tabs::-webkit-scrollbar { display: none; }
  .tab {
    padding: 11px 16px; font-size: 11px; font-weight: 500;
    letter-spacing: 1.5px; text-transform: uppercase;
    white-space: nowrap; cursor: pointer;
    color: var(--muted); border-bottom: 2px solid transparent;
    transition: all 0.2s; flex-shrink: 0;
  }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  /* ── Layout ── */
  .page { display: none; padding: 12px 12px calc(12px + var(--safe-bottom)); }
  .page.active { display: block; }
  /* ── Cards ── */
  .card {
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 10px; margin-bottom: 10px; overflow: hidden;
  }
  .card-header {
    padding: 10px 14px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .card-title {
    font-family: var(--mono); font-size: 10px; font-weight: 500;
    letter-spacing: 2px; text-transform: uppercase; color: var(--muted);
  }
  .card-badge {
    font-family: var(--mono); font-size: 10px; padding: 2px 8px;
    border-radius: 4px; font-weight: 600;
  }
  .card-body { padding: 12px 14px; }
  /* ── Account Hero ── */
  .acct-grid {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 1px; background: var(--border);
    border-radius: 10px; overflow: hidden; margin-bottom: 10px;
  }
  .acct-cell {
    background: var(--bg2); padding: 14px 14px 12px;
  }
  .acct-label {
    font-family: var(--mono); font-size: 9px; letter-spacing: 2px;
    text-transform: uppercase; color: var(--muted); margin-bottom: 5px;
  }
  .acct-value {
    font-family: var(--mono); font-size: 22px; font-weight: 600; line-height: 1;
  }
  .acct-sub { font-size: 11px; color: var(--muted); margin-top: 3px; }
  /* ── Metrics row ── */
  .metrics-row {
    display: flex; gap: 8px; overflow-x: auto;
    scrollbar-width: none; padding-bottom: 2px; margin-bottom: 10px;
  }
  .metrics-row::-webkit-scrollbar { display: none; }
  .metric-chip {
    flex-shrink: 0; background: var(--bg3);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 9px 12px; min-width: 90px;
  }
  .metric-chip .label { font-family: var(--mono); font-size: 9px; letter-spacing: 1.5px; color: var(--muted); text-transform: uppercase; }
  .metric-chip .val { font-family: var(--mono); font-size: 16px; font-weight: 600; margin-top: 3px; }
  /* ── Position card ── */
  .pos-card {
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 10px; margin-bottom: 8px; overflow: hidden;
    transition: border-color 0.2s;
  }
  .pos-card.DECAYING { border-left: 3px solid var(--red); }
  .pos-card.STRENGTHENING { border-left: 3px solid var(--green); }
  .pos-card.STABLE { border-left: 3px solid var(--yellow); }
  .pos-header {
    padding: 12px 14px 10px; display: flex; align-items: flex-start;
    justify-content: space-between; cursor: pointer;
  }
  .pos-sym { font-family: var(--mono); font-size: 16px; font-weight: 600; }
  .pos-regime { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .pos-pnl {
    font-family: var(--mono); font-size: 18px; font-weight: 600;
    text-align: right;
  }
  .pos-meta { font-size: 10px; color: var(--muted); text-align: right; margin-top: 2px; }
  .pos-bar-row { padding: 0 14px 10px; }
  .pos-bars { display: flex; gap: 6px; }
  .bar-group { flex: 1; }
  .bar-label { font-size: 9px; color: var(--muted); margin-bottom: 3px; font-family: var(--mono); }
  .bar-track { height: 4px; background: var(--border2); border-radius: 2px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 2px; transition: width 0.5s; }
  .pos-detail { display: none; padding: 0 14px 14px; border-top: 1px solid var(--border); }
  .pos-detail.open { display: block; }
  /* ── Layers table ── */
  .layers-grid { display: grid; gap: 4px; }
  .layer-row {
    display: flex; align-items: center; gap: 8px;
    padding: 5px 0; border-bottom: 1px solid var(--border);
  }
  .layer-row:last-child { border-bottom: none; }
  .layer-name { font-family: var(--mono); font-size: 10px; color: var(--muted); width: 130px; flex-shrink: 0; }
  .layer-score-bar { flex: 1; height: 4px; background: var(--border2); border-radius: 2px; overflow: hidden; }
  .layer-score-fill { height: 100%; border-radius: 2px; }
  .layer-score-val { font-family: var(--mono); font-size: 11px; width: 50px; text-align: right; flex-shrink: 0; }
  .layer-detail { font-size: 10px; color: var(--muted); margin-top: 2px; font-family: var(--mono); }
  /* ── Factors grid ── */
  .factors-grid {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 4px; margin-top: 10px;
  }
  .factor-chip {
    background: var(--bg3); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 8px;
  }
  .factor-name { font-family: var(--mono); font-size: 9px; color: var(--muted); }
  .factor-val { font-family: var(--mono); font-size: 12px; font-weight: 600; margin-top: 2px; }
  /* ── Agent badge ── */
  .agent-row {
    margin-top: 10px; padding: 8px 10px;
    background: var(--bg3); border-radius: 8px;
    border: 1px solid var(--border2);
  }
  .agent-header { display: flex; align-items: center; gap: 8px; }
  .agent-badge {
    font-family: var(--mono); font-size: 10px; font-weight: 600;
    padding: 2px 8px; border-radius: 4px;
  }
  .agent-conf { font-family: var(--mono); font-size: 10px; color: var(--muted); }
  .agent-reason { font-size: 11px; color: var(--muted); margin-top: 5px; line-height: 1.5; }
  /* ── Macro panel ── */
  .macro-regime-banner {
    padding: 16px 14px; text-align: center;
  }
  .macro-regime-label {
    font-family: var(--mono); font-size: 9px; letter-spacing: 3px;
    text-transform: uppercase; color: var(--muted);
  }
  .macro-regime-val {
    font-family: var(--mono); font-size: 24px; font-weight: 600; margin-top: 6px;
  }
  .macro-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 1px;
    background: var(--border); border-top: 1px solid var(--border);
  }
  .macro-cell {
    background: var(--bg2); padding: 12px 14px;
  }
  .macro-cell-label { font-family: var(--mono); font-size: 9px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted); }
  .macro-cell-val { font-family: var(--mono); font-size: 16px; font-weight: 600; margin-top: 4px; }
  .macro-cell-sub { font-size: 10px; color: var(--muted); margin-top: 2px; }
  /* ── Analytics grid ── */
  .analytics-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px;
    background: var(--border);
  }
  .analytic-cell {
    background: var(--bg2); padding: 12px 10px; text-align: center;
  }
  .analytic-label { font-family: var(--mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
  .analytic-val { font-family: var(--mono); font-size: 18px; font-weight: 600; margin-top: 4px; }
  /* ── Trades table ── */
  .trade-row {
    padding: 10px 0; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .trade-row:last-child { border-bottom: none; }
  .trade-sym { font-family: var(--mono); font-size: 13px; font-weight: 600; }
  .trade-meta { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .trade-pnl { font-family: var(--mono); font-size: 14px; font-weight: 600; text-align: right; }
  /* ── Log viewer ── */
  .log-container {
    background: #030305; border-radius: 8px; padding: 10px;
    font-family: var(--mono); font-size: 10px; line-height: 1.6;
    max-height: 400px; overflow-y: auto; color: #8888aa;
  }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .log-line.entry { color: #00d4aa; }
  .log-line.exit { color: #ff4466; }
  .log-line.warn { color: #ffc233; }
  .log-line.agent { color: #9b6dff; }
  .log-line.trim { color: #ff7733; }
  /* ── Trim history ── */
  .trim-row {
    padding: 9px 0; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: flex-start;
  }
  .trim-row:last-child { border-bottom: none; }
  .trim-sym { font-family: var(--mono); font-size: 12px; font-weight: 600; }
  .trim-reason { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .trim-pnl { font-family: var(--mono); font-size: 12px; text-align: right; }
  .trim-ts { font-size: 9px; color: var(--muted2); text-align: right; margin-top: 2px; }
  /* ── Colors ── */
  .c-green { color: var(--green); }
  .c-red { color: var(--red); }
  .c-yellow { color: var(--yellow); }
  .c-purple { color: var(--purple); }
  .c-muted { color: var(--muted); }
  .c-orange { color: var(--orange); }
  /* ── Regime badge ── */
  .regime-pill {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-family: var(--mono); font-size: 10px; font-weight: 600;
    letter-spacing: 0.5px;
  }
  /* ── Section divider ── */
  .section-label {
    font-family: var(--mono); font-size: 9px; letter-spacing: 2.5px;
    text-transform: uppercase; color: var(--muted2);
    padding: 14px 2px 6px;
  }
  /* ── Loading ── */
  .loading {
    display: flex; align-items: center; justify-content: center;
    height: 60px; color: var(--muted); font-family: var(--mono); font-size: 12px;
  }
  .spin {
    display: inline-block; width: 16px; height: 16px;
    border: 2px solid var(--border2); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 10px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  /* ── Refresh btn ── */
  .refresh-btn {
    font-family: var(--mono); font-size: 10px; letter-spacing: 1px;
    color: var(--muted); background: none; border: 1px solid var(--border2);
    border-radius: 6px; padding: 5px 10px; cursor: pointer;
    transition: all 0.2s;
  }
  .refresh-btn:active { background: var(--border); color: var(--text); }
  /* ── Veto row ── */
  .veto-row { padding: 8px 0; border-bottom: 1px solid var(--border); }
  .veto-row:last-child { border-bottom: none; }
  .veto-sym { font-family: var(--mono); font-size: 12px; font-weight: 600; }
  .veto-reason { font-size: 10px; color: var(--muted); margin-top: 2px; }
</style>
</head>
<body>

<div class="header">
  <div class="header-logo">RAPTOR v5.4</div>
  <div class="header-right">
    <div class="pulse-dot" id="pulseDot"></div>
    <div class="ts-label" id="tsLabel">—</div>
    <button class="refresh-btn" onclick="fetchData()">↻</button>
  </div>
</div>

<div class="tabs" id="tabs">
  <div class="tab active" data-page="overview">Overview</div>
  <div class="tab" data-page="positions">Positions</div>
  <div class="tab" data-page="macro">Macro</div>
  <div class="tab" data-page="analytics">Analytics</div>
  <div class="tab" data-page="trades">Trades</div>
  <div class="tab" data-page="logs">Logs</div>
</div>

<!-- ═══════════════════════ OVERVIEW ═══════════════════════ -->
<div class="page active" id="page-overview">
  <div id="overview-loading" class="loading"><div class="spin"></div>Loading...</div>
  <div id="overview-content" style="display:none">

    <!-- Account hero -->
    <div class="acct-grid" id="acct-grid"></div>

    <!-- Key metrics scroll row -->
    <div class="metrics-row" id="metrics-row"></div>

    <!-- Macro regime summary -->
    <div class="card" id="macro-summary-card">
      <div class="card-header">
        <span class="card-title">Macro Regime</span>
        <span class="card-badge" id="macro-badge">—</span>
      </div>
      <div class="macro-grid" id="macro-mini-grid"></div>
    </div>

    <!-- Position health summary -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">Portfolio Health</span>
        <span id="pos-count-badge" class="card-badge" style="background:var(--border);color:var(--text)"></span>
      </div>
      <div class="card-body" id="health-summary"></div>
    </div>

    <!-- Today's session -->
    <div class="card">
      <div class="card-header"><span class="card-title">Session Stats</span></div>
      <div class="card-body" id="session-stats"></div>
    </div>

  </div>
</div>

<!-- ═══════════════════════ POSITIONS ═══════════════════════ -->
<div class="page" id="page-positions">
  <div id="positions-loading" class="loading"><div class="spin"></div>Loading...</div>
  <div id="positions-content" style="display:none" id="positions-list"></div>
</div>

<!-- ═══════════════════════ MACRO ═══════════════════════ -->
<div class="page" id="page-macro">
  <div id="macro-content">
    <div id="macro-loading" class="loading"><div class="spin"></div>Loading...</div>
    <div id="macro-detail" style="display:none"></div>
  </div>
</div>

<!-- ═══════════════════════ ANALYTICS ═══════════════════════ -->
<div class="page" id="page-analytics">
  <div id="analytics-loading" class="loading"><div class="spin"></div>Loading...</div>
  <div id="analytics-content" style="display:none"></div>
</div>

<!-- ═══════════════════════ TRADES ═══════════════════════ -->
<div class="page" id="page-trades">
  <div id="trades-loading" class="loading"><div class="spin"></div>Loading...</div>
  <div id="trades-content" style="display:none"></div>
</div>

<!-- ═══════════════════════ LOGS ═══════════════════════ -->
<div class="page" id="page-logs">
  <div class="section-label">Raptor Engine Log</div>
  <div class="log-container" id="raptor-log-container">
    <div class="loading"><div class="spin"></div>Loading...</div>
  </div>
  <div class="section-label">Exit Monitor Log</div>
  <div class="log-container" id="exit-log-container"></div>
</div>

<script>
let DATA = null;

// ─── Tab switching ────────────────────────────────────────────────────────
document.getElementById('tabs').addEventListener('click', e => {
  const tab = e.target.closest('.tab');
  if (!tab) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  tab.classList.add('active');
  document.getElementById('page-' + tab.dataset.page).classList.add('active');
});

// ─── Helpers ─────────────────────────────────────────────────────────────
const fmt$ = v => v == null ? '—' : '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const fmtPct = v => v == null ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%';
const fmtN = (v, d=2) => v == null ? '—' : Number(v).toFixed(d);
const clr = v => v == null ? '' : v >= 0 ? 'c-green' : 'c-red';
const tierClr = t => ({STRENGTHENING:'c-green', DECAYING:'c-red', STABLE:'c-yellow', INSUFFICIENT_DATA:'c-muted'}[t] || 'c-muted');
const tierBg = t => ({STRENGTHENING:'rgba(0,212,170,0.12)', DECAYING:'rgba(255,68,102,0.12)', STABLE:'rgba(255,194,51,0.12)', INSUFFICIENT_DATA:'rgba(90,90,120,0.12)'}[t] || '');
const regimeClr = r => {
  if (!r) return 'c-muted';
  if (r.includes('RISK_ON') || r.includes('BULL')) return 'c-green';
  if (r.includes('RISK_OFF') || r.includes('BEAR')) return 'c-red';
  return 'c-yellow';
};
const esc = s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function scoreBar(score, color) {
  // score -1..+1 → width 0..100%, center at 50%
  const clamped = Math.max(-1, Math.min(1, score || 0));
  const pct = ((clamped + 1) / 2) * 100;
  return `<div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${color}"></div></div>`;
}

function healthBar(h) {
  const clamped = Math.max(-1, Math.min(1, h || 0));
  const pct = ((clamped + 1) / 2) * 100;
  const clr = h >= 0.3 ? '#00d4aa' : h >= 0 ? '#ffc233' : '#ff4466';
  return `<div class="bar-track" style="height:6px"><div class="bar-fill" style="width:${pct}%;background:${clr}"></div></div>`;
}

function agentBadgeStyle(dec) {
  const m = {EXIT:'background:#ff4466;color:#000', TRIM:'background:#ff7733;color:#000', HOLD:'background:#1a2a20;color:#00d4aa'};
  return m[dec] || 'background:var(--border);color:var(--muted)';
}

function fmtTS(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'});
  } catch { return ts.slice(11,16); }
}

function fmtDate(ts) {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleDateString('en-US', {month:'short', day:'numeric'});
  } catch { return ts.slice(0,10); }
}

// ─── Render Overview ─────────────────────────────────────────────────────
function renderOverview(d) {
  const acct = d.account || {};
  const sess = d.session || {};
  const pos  = d.positions || [];
  const macro = d.macro || {};

  // Account grid
  const totalPnl = d.total_pnl_pct || 0;
  const mv = d.total_mv || 0;
  document.getElementById('acct-grid').innerHTML = `
    <div class="acct-cell">
      <div class="acct-label">Equity</div>
      <div class="acct-value">${acct.equity ? fmt$(acct.equity) : '—'}</div>
      <div class="acct-sub">Cash: ${acct.cash_pct != null ? acct.cash_pct + '%' : '—'}</div>
    </div>
    <div class="acct-cell">
      <div class="acct-label">Unrealized P&L</div>
      <div class="acct-value ${clr(totalPnl)}">${fmtPct(totalPnl)}</div>
      <div class="acct-sub">Exposure: ${acct.total_exposure_pct != null ? acct.total_exposure_pct + '%' : '—'}</div>
    </div>
    <div class="acct-cell">
      <div class="acct-label">Market Value</div>
      <div class="acct-value">${fmt$(mv)}</div>
      <div class="acct-sub">${pos.length} positions</div>
    </div>
    <div class="acct-cell">
      <div class="acct-label">Max Drawdown</div>
      <div class="acct-value ${acct.drawdown_from_peak_pct < 0 ? 'c-red' : 'c-muted'}">${acct.drawdown_from_peak_pct != null ? '-'+Math.abs(acct.drawdown_from_peak_pct)+'%' : '—'}</div>
      <div class="acct-sub">From peak</div>
    </div>`;

  // Metrics row
  const an = d.analytics || {};
  const chips = [
    {l:'Win Rate', v: an.win_rate != null ? an.win_rate+'%' : '—', c: an.win_rate >= 50 ? 'c-green' : 'c-red'},
    {l:'Sharpe', v: an.sharpe != null ? fmtN(an.sharpe,2) : '—', c: an.sharpe >= 1 ? 'c-green' : an.sharpe >= 0 ? 'c-yellow' : 'c-red'},
    {l:'Profit F', v: an.profit_factor != null ? fmtN(an.profit_factor,2) : '—', c: an.profit_factor >= 1.5 ? 'c-green' : 'c-yellow'},
    {l:'Expectancy', v: an.expectancy != null ? fmtN(an.expectancy,2)+'%' : '—', c: an.expectancy >= 0 ? 'c-green' : 'c-red'},
    {l:'Roll 10 WR', v: an.roll10_wr != null ? an.roll10_wr+'%' : '—', c: an.roll10_wr >= 50 ? 'c-green' : 'c-red'},
    {l:'Sortino', v: an.sortino != null ? fmtN(an.sortino,2) : '—', c: an.sortino >= 1.5 ? 'c-green' : 'c-yellow'},
    {l:'Avg Hold', v: an.avg_hold_days != null ? an.avg_hold_days+'d' : '—', c:''},
    {l:'Trades', v: an.total_trades || '—', c:''},
  ];
  document.getElementById('metrics-row').innerHTML = chips.map(c =>
    `<div class="metric-chip"><div class="label">${c.l}</div><div class="val ${c.c}">${c.v}</div></div>`
  ).join('');

  // Macro mini
  const r = macro.regime || 'UNKNOWN';
  const badge = document.getElementById('macro-badge');
  badge.textContent = r;
  badge.style.cssText = r.includes('RISK_ON') || r.includes('BULL') ?
    'background:rgba(0,212,170,0.15);color:#00d4aa' :
    r.includes('RISK_OFF') || r.includes('BEAR') ?
    'background:rgba(255,68,102,0.15);color:#ff4466' :
    'background:rgba(255,194,51,0.15);color:#ffc233';

  const vix = macro.vix || {};
  const spy = macro.spy_trend || {};
  const sb  = macro.sector_breadth || {};
  const cs  = macro.credit_spread || {};
  document.getElementById('macro-mini-grid').innerHTML = `
    <div class="macro-cell">
      <div class="macro-cell-label">VIX</div>
      <div class="macro-cell-val ${vix.value > 25 ? 'c-red' : vix.value > 18 ? 'c-yellow' : 'c-green'}">${fmtN(vix.value,2)}</div>
      <div class="macro-cell-sub">${vix.regime || '—'}</div>
    </div>
    <div class="macro-cell">
      <div class="macro-cell-label">SPY</div>
      <div class="macro-cell-val">${fmt$(spy.price)}</div>
      <div class="macro-cell-sub">${spy.trend_20d != null ? (spy.trend_20d >= 0 ? '+' : '') + spy.trend_20d + '% 20d' : '—'}</div>
    </div>
    <div class="macro-cell">
      <div class="macro-cell-label">Breadth</div>
      <div class="macro-cell-val ${sb.pct_above_50ma > 60 ? 'c-green' : sb.pct_above_50ma > 40 ? 'c-yellow' : 'c-red'}">${sb.pct_above_50ma != null ? sb.pct_above_50ma+'%' : '—'}</div>
      <div class="macro-cell-sub">Above 50MA</div>
    </div>
    <div class="macro-cell">
      <div class="macro-cell-label">Credit Spread</div>
      <div class="macro-cell-val">${cs.spread_pct != null ? cs.spread_pct+'%' : '—'}</div>
      <div class="macro-cell-sub">${cs.regime || '—'}</div>
    </div>`;

  // Health summary
  const tiers = {STRENGTHENING:0, STABLE:0, DECAYING:0, INSUFFICIENT_DATA:0};
  pos.forEach(p => { if (tiers[p.tier] != null) tiers[p.tier]++; });
  document.getElementById('pos-count-badge').textContent = pos.length + ' positions';
  document.getElementById('health-summary').innerHTML = `
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:8px;height:8px;border-radius:2px;background:var(--green)"></div>
        <span style="font-family:var(--mono);font-size:12px">${tiers.STRENGTHENING} <span class="c-muted">STR</span></span>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:8px;height:8px;border-radius:2px;background:var(--yellow)"></div>
        <span style="font-family:var(--mono);font-size:12px">${tiers.STABLE} <span class="c-muted">STA</span></span>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:8px;height:8px;border-radius:2px;background:var(--red)"></div>
        <span style="font-family:var(--mono);font-size:12px">${tiers.DECAYING} <span class="c-muted">DEC</span></span>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:8px;height:8px;border-radius:2px;background:var(--muted)"></div>
        <span style="font-family:var(--mono);font-size:12px">${tiers.INSUFFICIENT_DATA} <span class="c-muted">INS</span></span>
      </div>
    </div>
    <div style="margin-top:12px">
      ${pos.filter(p=>p.trim && p.trim.action !== 'HOLD').map(p => `
        <div style="padding:6px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
          <span style="font-family:var(--mono);font-size:12px;font-weight:600">${p.symbol}</span>
          <span class="regime-pill" style="background:rgba(255,119,51,0.15);color:var(--orange);font-size:10px">${p.trim.action}</span>
        </div>
      `).join('') || '<div class="c-muted" style="font-size:12px;padding:4px 0">No trim signals</div>'}
    </div>`;

  // Session stats
  document.getElementById('session-stats').innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      ${[
        ['Entries Today', sess.trades_entered_today ?? '—'],
        ['Exits Today', sess.trades_exited_today ?? '—'],
        ['Stop Exits', sess.exits_by_stop ?? '—'],
        ['Daily P&L', sess.daily_pnl_pct != null ? fmtPct(sess.daily_pnl_pct) : '—'],
        ['Bot Uptime', sess.bot_uptime_hours != null ? sess.bot_uptime_hours + 'h' : '—'],
        ['Errors', sess.errors_today ?? '—'],
      ].map(([l,v]) => `
        <div>
          <div style="font-family:var(--mono);font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted)">${l}</div>
          <div style="font-family:var(--mono);font-size:15px;font-weight:600;margin-top:3px">${v}</div>
        </div>
      `).join('')}
    </div>`;

  document.getElementById('overview-loading').style.display = 'none';
  document.getElementById('overview-content').style.display = 'block';
}

// ─── Render Positions ─────────────────────────────────────────────────────
function renderPositions(d) {
  const pos = d.positions || [];
  const container = document.getElementById('positions-content');

  if (!pos.length) {
    container.innerHTML = '<div class="loading c-muted">No positions</div>';
    document.getElementById('positions-loading').style.display = 'none';
    container.style.display = 'block';
    return;
  }

  container.innerHTML = pos.map((p, i) => {
    const pnlC = clr(p.pnl_pct);
    const hlth = p.health || 0;
    const hlthPct = ((Math.max(-1, Math.min(1, hlth)) + 1) / 2 * 100).toFixed(0);
    const hlthClr = hlth >= 0.3 ? '#00d4aa' : hlth >= 0 ? '#ffc233' : '#ff4466';

    // Factor scores bar mini
    const compPct = ((Math.max(-2, Math.min(2, p.composite || 0)) + 2) / 4 * 100).toFixed(0);
    const compClr = p.composite >= 0 ? '#00d4aa' : '#ff4466';

    // Layers detail
    const layerRows = Object.entries(p.layers || {}).map(([k, l]) => {
      const s = l.score || 0;
      const pct = ((Math.max(-1, Math.min(1, s)) + 1) / 2 * 100).toFixed(0);
      const lc = s >= 0.3 ? '#00d4aa' : s >= 0 ? '#ffc233' : '#ff4466';
      return `<div class="layer-row">
        <div class="layer-name">${k.replace(/_/g,' ')}</div>
        <div class="layer-score-bar"><div class="layer-score-fill" style="width:${pct}%;background:${lc}"></div></div>
        <div class="layer-score-val" style="color:${lc}">${s >= 0 ? '+' : ''}${fmtN(s,3)}</div>
      </div>
      <div class="layer-detail" style="padding:0 0 6px 0;color:var(--muted2)">${esc(l.detail)}</div>`;
    }).join('');

    // Factor scores grid (top contributors by abs value)
    const fcs = p.factor_contributions || {};
    const topFactors = Object.entries(fcs)
      .sort((a,b) => Math.abs(b[1]) - Math.abs(a[1]))
      .slice(0, 8);
    const factorGrid = topFactors.map(([k, v]) => {
      const fc = v >= 0 ? 'c-green' : 'c-red';
      return `<div class="factor-chip">
        <div class="factor-name">${k.replace(/_/g,' ')}</div>
        <div class="factor-val ${fc}">${v >= 0 ? '+' : ''}${fmtN(v,4)}</div>
      </div>`;
    }).join('');

    // Cluster scores
    const clusters = p.cluster_scores || {};
    const clusterRow = Object.entries(clusters).map(([k,v]) =>
      `<span style="font-family:var(--mono);font-size:10px;margin-right:10px"><span class="c-muted">${k}:</span> <span class="${clr(v)}">${v>=0?'+':''}${fmtN(v,2)}</span></span>`
    ).join('');

    // Trim signal
    const trim = p.trim || {};
    const trimHtml = trim.action && trim.action !== 'HOLD' ? `
      <div style="margin-top:10px;padding:8px 10px;background:rgba(255,119,51,0.1);border:1px solid rgba(255,119,51,0.3);border-radius:6px">
        <div style="font-family:var(--mono);font-size:10px;font-weight:600;color:var(--orange)">${trim.action} ${trim.trim_pct ? '(' + trim.trim_pct.toFixed(1) + '%)' : ''}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:3px">${esc(trim.reasoning)}</div>
      </div>` : '';

    // Agent
    const agentHtml = `<div class="agent-row">
      <div class="agent-header">
        <span class="agent-badge" style="${agentBadgeStyle(p.agent_decision)}">${p.agent_decision || '—'}</span>
        <span class="agent-conf">${p.agent_confidence != null ? (p.agent_confidence*100).toFixed(0)+'% conf' : ''}</span>
        <span class="c-muted" style="font-size:9px;margin-left:auto">${fmtTS(p.agent_ts)}</span>
      </div>
      <div class="agent-reason">${esc(p.agent_reasoning)}</div>
    </div>`;

    return `<div class="pos-card ${p.tier}">
      <div class="pos-header" onclick="togglePos(${i})">
        <div>
          <div class="pos-sym">${esc(p.symbol)}</div>
          <div class="pos-regime">${esc(p.regime)} · ${p.days_held}d held · FAR ${p.factors_positive}/16</div>
        </div>
        <div>
          <div class="pos-pnl ${pnlC}">${fmtPct(p.pnl_pct)}</div>
          <div class="pos-meta">${p.qty != null ? p.qty + ' shares' : ''} · ${fmt$(p.current_price)}</div>
        </div>
      </div>
      <div class="pos-bar-row">
        <div class="pos-bars">
          <div class="bar-group">
            <div class="bar-label">HEALTH ${fmtN(p.health,3)}</div>
            ${healthBar(p.health)}
          </div>
          <div class="bar-group">
            <div class="bar-label">COMPOSITE ${fmtN(p.composite,3)}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${compPct}%;background:${compClr}"></div></div>
          </div>
          <div class="bar-group">
            <div class="bar-label">STOP ${fmtN(p.stop_dist_atr,2)} ATR</div>
            <div class="bar-track"><div class="bar-fill" style="width:${Math.max(0,Math.min(100,(p.stop_dist_atr/5)*100)).toFixed(0)}%;background:${p.stop_dist_atr > 0 ? '#00d4aa' : '#ff4466'}"></div></div>
          </div>
        </div>
      </div>
      <div class="pos-detail" id="pos-detail-${i}">
        <div style="padding:10px 0 4px">
          <div style="font-family:var(--mono);font-size:9px;letter-spacing:2px;color:var(--muted2);text-transform:uppercase;margin-bottom:8px">Entry / Price / Stop</div>
          <div style="display:flex;gap:12px;font-family:var(--mono);font-size:12px">
            <span><span class="c-muted">Entry </span>${fmt$(p.entry_price)}</span>
            <span><span class="c-muted">Now </span>${fmt$(p.current_price)}</span>
            <span><span class="c-muted">Stop </span><span class="${p.stop_dist_atr < 0 ? 'c-red' : ''}">${fmt$(p.stop_price)}</span></span>
          </div>
          <div style="display:flex;gap:12px;font-family:var(--mono);font-size:12px;margin-top:6px">
            <span><span class="c-muted">MV </span>${fmt$(p.market_value)}</span>
            <span><span class="c-muted">ATR </span>${fmtN(p.atr,3)}</span>
            <span><span class="c-muted">ROC5 </span><span class="${clr(p.roc_5d)}">${p.roc_5d != null ? fmtPct(p.roc_5d*100) : '—'}</span></span>
          </div>
          <div style="display:flex;gap:12px;font-family:var(--mono);font-size:12px;margin-top:6px">
            <span><span class="c-muted">Hold </span>${p.days_held}/${p.hold_target}d</span>
            <span><span class="c-muted">T-stat </span>${fmtN(p.t_stat,3)}</span>
          </div>
        </div>
        ${clusterRow ? `<div style="padding:8px 0;border-top:1px solid var(--border)"><div style="font-family:var(--mono);font-size:9px;letter-spacing:2px;color:var(--muted2);text-transform:uppercase;margin-bottom:6px">Cluster Scores</div>${clusterRow}</div>` : ''}
        <div style="padding:10px 0;border-top:1px solid var(--border)">
          <div style="font-family:var(--mono);font-size:9px;letter-spacing:2px;color:var(--muted2);text-transform:uppercase;margin-bottom:8px">Health Layers</div>
          <div class="layers-grid">${layerRows}</div>
        </div>
        ${factorGrid ? `<div style="padding:10px 0;border-top:1px solid var(--border)">
          <div style="font-family:var(--mono);font-size:9px;letter-spacing:2px;color:var(--muted2);text-transform:uppercase;margin-bottom:8px">Top Factor Contributions</div>
          <div class="factors-grid">${factorGrid}</div>
        </div>` : ''}
        ${trimHtml}
        ${agentHtml}
      </div>
    </div>`;
  }).join('');

  document.getElementById('positions-loading').style.display = 'none';
  container.style.display = 'block';
}

function togglePos(i) {
  const el = document.getElementById('pos-detail-' + i);
  if (el) el.classList.toggle('open');
}

// ─── Render Macro ─────────────────────────────────────────────────────────
function renderMacro(d) {
  const m = d.macro || {};
  const spy = m.spy_trend || {};
  const vix = m.vix || {};
  const sb  = m.sector_breadth || {};
  const yc  = m.yield_curve || {};
  const cs  = m.credit_spread || {};
  const r = m.regime || 'UNKNOWN';

  const rClr = r.includes('RISK_ON') || r.includes('BULL') ? '#00d4aa' :
               r.includes('RISK_OFF') || r.includes('BEAR') ? '#ff4466' : '#ffc233';

  document.getElementById('macro-detail').innerHTML = `
    <div class="card">
      <div style="padding:20px 14px;text-align:center;border-bottom:1px solid var(--border)">
        <div style="font-family:var(--mono);font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--muted)">Macro Regime</div>
        <div style="font-family:var(--mono);font-size:26px;font-weight:600;margin-top:8px;color:${rClr}">${esc(r)}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:6px">${fmtTS(m.macro_ts)} UTC</div>
      </div>
      <div class="macro-grid" style="grid-template-columns:1fr 1fr">
        <div class="macro-cell">
          <div class="macro-cell-label">VIX</div>
          <div class="macro-cell-val ${vix.value > 25 ? 'c-red' : vix.value > 18 ? 'c-yellow' : 'c-green'}">${fmtN(vix.value,2)}</div>
          <div class="macro-cell-sub">${vix.regime || '—'}</div>
        </div>
        <div class="macro-cell">
          <div class="macro-cell-label">SPY Price</div>
          <div class="macro-cell-val">${fmt$(spy.price)}</div>
          <div class="macro-cell-sub">${spy.trend_20d != null ? (spy.trend_20d>=0?'+':'')+spy.trend_20d+'% 20d trend' : '—'}</div>
        </div>
        <div class="macro-cell">
          <div class="macro-cell-label">SPY vs 50MA</div>
          <div class="macro-cell-val ${spy.above_50ma ? 'c-green' : 'c-red'}">${spy.above_50ma ? 'ABOVE' : 'BELOW'}</div>
          <div class="macro-cell-sub">${fmt$(spy.ma50)}</div>
        </div>
        <div class="macro-cell">
          <div class="macro-cell-label">SPY vs 200MA</div>
          <div class="macro-cell-val ${spy.above_200ma ? 'c-green' : 'c-red'}">${spy.above_200ma ? 'ABOVE' : 'BELOW'}</div>
          <div class="macro-cell-sub">${fmt$(spy.ma200)}</div>
        </div>
        <div class="macro-cell">
          <div class="macro-cell-label">Sector Breadth</div>
          <div class="macro-cell-val ${sb.pct_above_50ma > 60 ? 'c-green' : sb.pct_above_50ma > 40 ? 'c-yellow' : 'c-red'}">${sb.pct_above_50ma != null ? sb.pct_above_50ma+'%' : '—'}</div>
          <div class="macro-cell-sub">Above 50MA · ${sb.regime || '—'}</div>
        </div>
        <div class="macro-cell">
          <div class="macro-cell-label">Credit Spread</div>
          <div class="macro-cell-val">${cs.spread_pct != null ? cs.spread_pct+'%' : '—'}</div>
          <div class="macro-cell-sub">${cs.regime || '—'}</div>
        </div>
        <div class="macro-cell">
          <div class="macro-cell-label">Yield Curve</div>
          <div class="macro-cell-val ${yc.inverted ? 'c-red' : 'c-green'}">${yc.inverted === true ? 'INVERTED' : yc.inverted === false ? 'NORMAL' : 'UNKNOWN'}</div>
          <div class="macro-cell-sub">${yc.spread_pct != null ? yc.spread_pct+'%' : '—'}</div>
        </div>
        <div class="macro-cell">
          <div class="macro-cell-label">SPY Regime</div>
          <div class="macro-cell-val" style="font-size:13px">${spy.regime || '—'}</div>
          <div class="macro-cell-sub">&nbsp;</div>
        </div>
      </div>
    </div>
    <div class="section-label">Agent Summary</div>
    <div class="card">
      <div style="padding:12px 14px;font-size:11px;color:var(--muted);line-height:1.7;font-family:var(--mono)">${esc(m.summary)}</div>
    </div>`;

  document.getElementById('macro-loading').style.display = 'none';
  document.getElementById('macro-detail').style.display = 'block';
}

// ─── Render Analytics ─────────────────────────────────────────────────────
function renderAnalytics(d) {
  const an = d.analytics || {};
  const trims = d.recent_trims || [];

  const cells = [
    {l:'Win Rate', v: an.win_rate != null ? an.win_rate+'%' : '—', c: an.win_rate >= 50 ? '#00d4aa' : '#ff4466'},
    {l:'Sharpe', v: an.sharpe != null ? fmtN(an.sharpe,2) : '—', c: an.sharpe >= 1 ? '#00d4aa' : an.sharpe >= 0 ? '#ffc233' : '#ff4466'},
    {l:'Sortino', v: an.sortino != null ? fmtN(an.sortino,2) : '—', c: an.sortino >= 1.5 ? '#00d4aa' : '#ffc233'},
    {l:'Expectancy', v: an.expectancy != null ? fmtN(an.expectancy,3)+'%' : '—', c: an.expectancy >= 0 ? '#00d4aa' : '#ff4466'},
    {l:'Profit Factor', v: an.profit_factor != null ? fmtN(an.profit_factor,2) : '—', c: an.profit_factor >= 1.5 ? '#00d4aa' : '#ffc233'},
    {l:'Max DD', v: an.max_dd != null ? '-'+an.max_dd+'%' : '—', c: '#ff4466'},
    {l:'Avg Win', v: an.avg_win != null ? '+'+fmtN(an.avg_win,2)+'%' : '—', c: '#00d4aa'},
    {l:'Avg Loss', v: an.avg_loss != null ? fmtN(an.avg_loss,2)+'%' : '—', c: '#ff4466'},
    {l:'Roll 10 WR', v: an.roll10_wr != null ? an.roll10_wr+'%' : '—', c: an.roll10_wr >= 50 ? '#00d4aa' : '#ff4466'},
    {l:'Avg Hold', v: an.avg_hold_days != null ? an.avg_hold_days+'d' : '—', c: '#e2e4f0'},
    {l:'Total Trades', v: an.total_trades || '—', c: '#e2e4f0'},
    {l:'', v:'', c:'#e2e4f0'},
  ];

  const trimRows = trims.length ? trims.map(t => `
    <div class="trim-row">
      <div>
        <div class="trim-sym">${esc(t.symbol)}</div>
        <div class="trim-reason">${esc(t.reason)} · ${esc(t.trim_detail || '')}</div>
      </div>
      <div>
        <div class="trim-pnl ${t.pnl_pct >= 0 ? 'c-green' : 'c-red'}">${fmtPct(t.pnl_pct)}</div>
        <div class="trim-ts">${fmtTS(t.timestamp)}</div>
      </div>
    </div>`).join('') : '<div class="c-muted" style="font-size:12px">No trim history</div>';

  document.getElementById('analytics-content').innerHTML = `
    <div class="card">
      <div class="card-header"><span class="card-title">Closed Trade Analytics</span></div>
      <div class="analytics-grid">
        ${cells.map(c => `<div class="analytic-cell">
          <div class="analytic-label">${c.l}</div>
          <div class="analytic-val" style="color:${c.c}">${c.v}</div>
        </div>`).join('')}
      </div>
    </div>
    <div class="section-label">Recent Trim History</div>
    <div class="card">
      <div class="card-body">${trimRows}</div>
    </div>`;

  document.getElementById('analytics-loading').style.display = 'none';
  document.getElementById('analytics-content').style.display = 'block';
}

// ─── Render Trades ─────────────────────────────────────────────────────────
function renderTrades(d) {
  const closed = d.recent_closed || [];
  const vetoes = d.vetoes || [];

  const tradeRows = closed.length ? closed.map(t => {
    const pnl = t.actual_pnl_pct ?? t.pnl_pct ?? 0;
    return `<div class="trade-row">
      <div>
        <div class="trade-sym">${esc(t.symbol)}</div>
        <div class="trade-meta">${fmtDate(t.entry_date)} → ${fmtDate(t.exit_date)} · ${t.hold_days || 0}d · ${t.qty ? Number(t.qty).toFixed(3) : '?'} shares</div>
        <div class="trade-meta">${fmt$(t.entry_price)} → ${fmt$(t.exit_price)}</div>
      </div>
      <div>
        <div class="trade-pnl ${clr(pnl)}">${fmtPct(pnl)}</div>
      </div>
    </div>`;
  }).join('') : '<div class="c-muted" style="font-size:12px;padding:8px 0">No closed trades</div>';

  const vetoRows = vetoes.length ? vetoes.map(v => `
    <div class="veto-row">
      <div class="veto-sym">${esc(v.symbol || '—')}</div>
      <div class="veto-reason">${esc(v.reason || JSON.stringify(v))}</div>
    </div>`).join('') : '<div class="c-muted" style="font-size:12px">No vetoes today</div>';

  document.getElementById('trades-content').innerHTML = `
    <div class="section-label">Recent Closed Trades (${closed.length})</div>
    <div class="card">
      <div class="card-body" style="padding:0 14px">${tradeRows}</div>
    </div>
    <div class="section-label">Entry Vetoes</div>
    <div class="card">
      <div class="card-body">${vetoRows}</div>
    </div>`;

  document.getElementById('trades-loading').style.display = 'none';
  document.getElementById('trades-content').style.display = 'block';
}

// ─── Render Logs ──────────────────────────────────────────────────────────
function renderLogs(d) {
  const colorLine = line => {
    let cls = 'log-line';
    if (line.includes('ORDER [v5.4]') || line.includes('BUY')) cls += ' entry';
    else if (line.includes('EXIT') || line.includes('SELL')) cls += ' exit';
    else if (line.includes('WARNING') || line.includes('WARN') || line.includes('warn')) cls += ' warn';
    else if (line.includes('AGENT') || line.includes('agent')) cls += ' agent';
    else if (line.includes('TRIM') || line.includes('trim')) cls += ' trim';
    return `<div class="${cls}">${esc(line)}</div>`;
  };

  const rl = d.raptor_log || [];
  const el = d.exit_log || [];

  const rc = document.getElementById('raptor-log-container');
  const ec = document.getElementById('exit-log-container');
  rc.innerHTML = rl.length ? rl.map(colorLine).join('') : '<div class="c-muted">No log data</div>';
  ec.innerHTML = el.length ? el.map(colorLine).join('') : '<div class="c-muted">No exit log data</div>';

  // Auto-scroll to bottom
  rc.scrollTop = rc.scrollHeight;
  ec.scrollTop = ec.scrollHeight;
}

// ─── Main fetch ───────────────────────────────────────────────────────────
async function fetchData() {
  try {
    const pulse = document.getElementById('pulseDot');
    pulse.style.background = '#ffc233';
    const res = await fetch('/api/data');
    DATA = await res.json();
    pulse.style.background = '#00d4aa';

    const ts = new Date(DATA.ts);
    document.getElementById('tsLabel').textContent =
      ts.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit'});

    renderOverview(DATA);
    renderPositions(DATA);
    renderMacro(DATA);
    renderAnalytics(DATA);
    renderTrades(DATA);
    renderLogs(DATA);

  } catch(e) {
    console.error('Fetch error', e);
    document.getElementById('pulseDot').style.background = '#ff4466';
    document.getElementById('tsLabel').textContent = 'ERR';
  }
}

// Initial load + auto-refresh every 30s
fetchData();
setInterval(fetchData, 30000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="RAPTOR Live Dashboard")
    parser.add_argument("--port", type=int, default=7443, help="Port (default: 7443)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0 — all interfaces)")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════╗
║            RAPTOR LIVE DASHBOARD                     ║
╠══════════════════════════════════════════════════════╣
║  Local:      http://localhost:{args.port}                ║
║  Tailscale:  http://<your-tailscale-ip>:{args.port}     ║
║                                                      ║
║  Data refreshes every 30 seconds automatically.      ║
║  All data read from local JSON files — no API calls. ║
╚══════════════════════════════════════════════════════╝
    """)
    app.run(host=args.host, port=args.port, debug=False)
