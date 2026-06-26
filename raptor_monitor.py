"""
raptor_monitor.py — Raptor End-of-Day Monitoring Agent
========================================================
Deterministic multi-layer health check that mimics LLM agent pattern
recognition without any API cost. Runs all 6 layers, collects flagged
items, and sends a single HTML summary email after daily_recap.py.

Layers:
    L1 — Infrastructure health  (logs, file freshness, silent fail detection)
    L2 — Position reconciliation (Alpaca vs ledger vs hold_health)
    L3 — Position risk flags    (confluence deterioration, winner-at-risk, etc.)
    L4 — Operational checks     (cooldown audit, outcome_pending age, gate progress)
    L5 — Macro / regime         (VIX change, regime drift per position, STANDBY)
    L6 — Opportunity awareness  (cooldown expiry, capital utilization)

Usage:
    python raptor_monitor.py              # full run, sends email
    python raptor_monitor.py --preview    # saves HTML to logs/monitor_preview.html
    python raptor_monitor.py --dry-run    # prints findings, no email

Scheduling (Task Scheduler):
    4:30 PM ET  →  python raptor_monitor.py
    (runs after daily_recap.py which fires at 4:15 PM)

Output:
    logs/monitor_YYYYMMDD.log   — machine-readable findings (JSON per line)
    One HTML email to EMAIL_RECEIVER
"""

import ast
import json
import logging
import os
import re
import smtplib
import sys
import argparse
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────

EMAIL_SENDER   = "stevefirwin@gmail.com"
EMAIL_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", "stevefirwin+raptor@gmail.com")

LOG_DIR        = Path("logs")
TODAY          = date.today()
TODAY_STR      = TODAY.strftime("%Y%m%d")
TODAY_ISO      = TODAY.isoformat()

# File paths
LEDGER_PATH          = Path("position_ledger.json")
HOLD_HEALTH_PATH     = Path("hold_health.json")
HOLD_HISTORY_PATH    = Path("hold_history.json")
MACRO_PATH           = Path("macro_context.json")
MARKET_DECISION_PATH = Path("market_decision.json")
COMPOSITE_CACHE_PATH = Path("composite_cache.json")
COOLDOWN_PATH        = Path("cooldown_log.json")
OUTCOME_PENDING_PATH = Path("outcome_pending.json")
OUTCOME_LOG_PATH     = Path("outcome_log.json")
POSITION_OUTCOMES_PATH = Path("position_outcomes.json")
KELLY_PATH           = Path("kelly_estimates.json")
SLIPPAGE_PATH        = Path("slippage_log.json")
DATA_FEEDS_PATH      = Path("data_feeds.py")

# Severity levels — determines sort order and badge color in email
SEV_CRITICAL = "CRITICAL"
SEV_ALERT    = "ALERT"
SEV_WARN     = "WARN"
SEV_INFO     = "INFO"
SEV_OK       = "OK"

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(exist_ok=True)

# Windows' default console/redirect encoding is cp1252, which cannot encode
# emoji used in log messages (e.g. the severity emoji in subject lines).
# Under Task Scheduler's S4U session this surfaces as UnicodeEncodeError on
# the StreamHandler even though the run itself completes successfully.
# reconfigure() (Python 3.7+) forces stdout/stderr to UTF-8 with replacement
# for anything that still can't be encoded, so logging never crashes the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # older Python or non-reconfigurable stream — StreamHandler below still degrades gracefully

# IMPORTANT: this filename is DIFFERENT from the bat file's own >> redirect
# target (logs\monitor_bat_YYYYMMDD.log). Two writers on the same filename
# caused PermissionError [Errno 13] when both the bat redirect and this
# FileHandler tried to hold the file open simultaneously on Windows.
# delay=True also defers opening until the first actual log call, reducing
# lock contention further.
INTERNAL_LOG_PATH = LOG_DIR / f"monitor_run_{TODAY_STR}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [monitor] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(INTERNAL_LOG_PATH, encoding="utf-8", delay=True),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("raptor.monitor")

# ── Finding dataclass (plain dict for simplicity) ─────────────────────────────

def finding(severity: str, layer: str, code: str, message: str,
            detail: str = "", action: str = "") -> dict:
    return {
        "severity":  severity,
        "layer":     layer,
        "code":      code,
        "message":   message,
        "detail":    detail,
        "action":    action,
        "timestamp": datetime.now().isoformat(),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path: Path) -> Optional[dict]:
    """Load JSON safely. Returns None on any failure."""
    try:
        return json.loads(path.read_bytes().decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning("Could not load %s: %s", path, e)
        return None


def file_age_hours(path: Path) -> Optional[float]:
    """Return hours since file was last modified, or None if missing."""
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return (datetime.now() - mtime).total_seconds() / 3600


def parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO timestamp string to datetime. Returns None on failure."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00").replace("+00:00", ""))
    except Exception:
        return None


def days_since(ts_str: Optional[str]) -> Optional[float]:
    """Return days since an ISO timestamp string."""
    dt = parse_ts(ts_str)
    if dt is None:
        return None
    return (datetime.now() - dt).total_seconds() / 86400


def read_log(filename: str) -> str:
    """Read a log file from LOG_DIR with encoding safety."""
    p = LOG_DIR / filename
    if not p.exists():
        return ""
    return p.read_bytes().decode("utf-8", errors="replace")


def get_alpaca_positions() -> Tuple[List[dict], dict]:
    """Connect to Alpaca and return (positions, account). Returns ([], {}) on failure."""
    try:
        from config import CONFIG
        from data_feeds import AlpacaDataFeed
        feed = AlpacaDataFeed(CONFIG)
        positions = feed.get_positions()
        account   = feed.get_account()
        return positions, account
    except Exception as e:
        logger.error("Alpaca connection failed: %s", e)
        return [], {}


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — Infrastructure Health
# ═══════════════════════════════════════════════════════════════════════════════

def run_layer1() -> List[dict]:
    findings = []
    layer = "L1-Infrastructure"

    # ── 1a. Silent fail detection — SELL logged but no OK ────────────────────
    exit_log_name = f"exits_{TODAY_STR}.log"
    exit_content  = read_log(exit_log_name)

    if not exit_content:
        findings.append(finding(
            SEV_WARN, layer, "NO_EXIT_LOG",
            f"No exit log found for today ({exit_log_name})",
            "exit_monitor.py may not have run, or ran before midnight",
            "Check Task Scheduler — confirm Start_AfterClose.bat fired"
        ))
    else:
        sell_count = len(re.findall(r"INFO: SELL ", exit_content))
        ok_count   = len(re.findall(r"INFO:   OK:", exit_content))
        fail_count = len(re.findall(r"ERROR:   FAILED:", exit_content))
        insuf      = len(re.findall(r"insufficient qty", exit_content))

        if sell_count > 0 and ok_count == 0 and fail_count == 0:
            findings.append(finding(
                SEV_CRITICAL, layer, "SILENT_FAIL",
                f"Exit monitor silent failure — {sell_count} SELL logged, 0 OK, 0 FAILED",
                "Orders logged but submit_order never confirmed. Positions may not have exited.",
                "Check data_feeds.py submit_order method. Run: python check_ledger_vs_alpaca.py"
            ))
        else:
            findings.append(finding(
                SEV_OK, layer, "EXIT_LOG_OK",
                f"Exit log healthy — {sell_count} SELL, {ok_count} OK, {fail_count} FAILED",
            ))

        if insuf > 0:
            findings.append(finding(
                SEV_WARN, layer, "INSUF_QTY",
                f"{insuf} 'insufficient qty' rejection(s) in exit log",
                "Double-trim guard may have failed, or ledger share count is stale",
                "Check position_ledger.json share counts vs Alpaca"
            ))

        # Error lines in exit log
        error_lines = [l for l in exit_content.split("\n")
                       if " ERROR" in l or "Traceback" in l]
        if error_lines:
            findings.append(finding(
                SEV_ALERT, layer, "EXIT_LOG_ERRORS",
                f"{len(error_lines)} ERROR/Traceback line(s) in exit log",
                "\n".join(error_lines[:3]),
                "Read full log: logs/" + exit_log_name
            ))

    # ── 1b. Main entry log ────────────────────────────────────────────────────
    # main.py logs to raptor_YYYYMMDD.log or raptor_v6_YYYYMMDD.log
    main_log_patterns = [f"raptor_{TODAY_STR}.log", f"raptor_v6_{TODAY_STR}.log"]
    main_content = ""
    main_log_found = None
    for pattern in main_log_patterns:
        c = read_log(pattern)
        if c:
            main_content = c
            main_log_found = pattern
            break

    if not main_content:
        findings.append(finding(
            SEV_WARN, layer, "NO_MAIN_LOG",
            "No main.py entry scan log found for today",
            f"Checked: {', '.join(main_log_patterns)}",
            "Confirm Task Scheduler fired Start_Entry.bat at 9:35 AM"
        ))
    else:
        err_lines = [l for l in main_content.split("\n")
                     if " ERROR" in l or "Traceback" in l]
        if err_lines:
            findings.append(finding(
                SEV_ALERT, layer, "MAIN_LOG_ERRORS",
                f"{len(err_lines)} ERROR/Traceback line(s) in entry scan log",
                "\n".join(err_lines[:3]),
                "Read full log: logs/" + main_log_found
            ))
        else:
            findings.append(finding(SEV_OK, layer, "MAIN_LOG_OK",
                                    "Entry scan log has no errors"))

    # ── 1c. Critical JSON file freshness (must be today) ─────────────────────
    staleness_checks = [
        (HOLD_HEALTH_PATH,     "hold_health.json",     20),   # hours threshold
        (MACRO_PATH,           "macro_context.json",   30),
        (COMPOSITE_CACHE_PATH, "composite_cache.json", 30),
        (MARKET_DECISION_PATH, "market_decision.json", 30),
    ]
    for path, label, max_hours in staleness_checks:
        age = file_age_hours(path)
        if age is None:
            findings.append(finding(
                SEV_WARN, layer, f"MISSING_{label.upper().replace('.','_')}",
                f"{label} does not exist",
                action=f"Check if {label.split('.')[0].replace('_','')} ran today"
            ))
        elif age > max_hours:
            findings.append(finding(
                SEV_WARN, layer, f"STALE_{label.upper().replace('.','_')}",
                f"{label} is {age:.1f}h old (threshold: {max_hours}h)",
                f"Last modified: {datetime.fromtimestamp(path.stat().st_mtime).strftime('%H:%M')}",
                f"Verify the script that writes {label} ran today"
            ))
        else:
            findings.append(finding(SEV_OK, layer, f"{label}_FRESH",
                                    f"{label} is current ({age:.1f}h old)"))

    # ── 1d. AST check — submit_order present on AlpacaDataFeed ───────────────
    if DATA_FEEDS_PATH.exists():
        try:
            src  = DATA_FEEDS_PATH.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
            found = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "AlpacaDataFeed":
                    methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                    if "submit_order" in methods:
                        found = True
            if found:
                findings.append(finding(SEV_OK, layer, "SUBMIT_ORDER_PRESENT",
                                        "AlpacaDataFeed.submit_order confirmed present (AST)"))
            else:
                findings.append(finding(
                    SEV_CRITICAL, layer, "SUBMIT_ORDER_MISSING",
                    "AlpacaDataFeed.submit_order METHOD IS MISSING from data_feeds.py",
                    "This caused 11 days of silent execution failure (2026-05-25 to 2026-06-05)",
                    "Restore submit_order def immediately — do not run exit_monitor until fixed"
                ))
        except SyntaxError as e:
            findings.append(finding(
                SEV_CRITICAL, layer, "DATA_FEEDS_SYNTAX_ERROR",
                f"data_feeds.py has a syntax error: {e}",
                action="Fix syntax before next trading day"
            ))
    else:
        findings.append(finding(SEV_CRITICAL, layer, "DATA_FEEDS_MISSING",
                                "data_feeds.py not found in working directory"))

    return findings


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — Position Reconciliation
# ═══════════════════════════════════════════════════════════════════════════════

def run_layer2(alpaca_positions: List[dict]) -> List[dict]:
    findings = []
    layer = "L2-Reconciliation"

    ledger_data   = load_json(LEDGER_PATH)
    hold_health   = load_json(HOLD_HEALTH_PATH) or {}

    alpaca_syms = {p["symbol"] for p in alpaca_positions}

    # Build active ledger map
    ledger_active_syms = set()
    ledger_by_sym: Dict[str, dict] = {}
    if ledger_data:
        for key, entry in ledger_data.get("positions", {}).items():
            if entry.get("status", "ACTIVE") == "ACTIVE":
                sym = entry["symbol"]
                ledger_active_syms.add(sym)
                ledger_by_sym[sym] = entry

    health_syms = set(hold_health.keys())

    # ── 2a. Missing from ledger ───────────────────────────────────────────────
    missing_from_ledger = alpaca_syms - ledger_active_syms
    if missing_from_ledger:
        findings.append(finding(
            SEV_ALERT, layer, "MISSING_FROM_LEDGER",
            f"{len(missing_from_ledger)} Alpaca position(s) not in ledger: {sorted(missing_from_ledger)}",
            "Ledger is out of sync — these positions have no metadata (stop, entry, Kelly)",
            "Run: python backfill_ledger.py --write"
        ))

    # ── 2b. Ghost in ledger ───────────────────────────────────────────────────
    ghost_in_ledger = ledger_active_syms - alpaca_syms
    if ghost_in_ledger:
        findings.append(finding(
            SEV_ALERT, layer, "GHOST_IN_LEDGER",
            f"{len(ghost_in_ledger)} ledger position(s) not on Alpaca: {sorted(ghost_in_ledger)}",
            "These may have been exited but ledger not updated, or manual close on Alpaca",
            "Run: python backfill_ledger.py --write  then  python outcome_tracker.py"
        ))

    # ── 2c. Share count mismatch ──────────────────────────────────────────────
    for sym in alpaca_syms & ledger_active_syms:
        alpaca_qty  = float(next(p["qty"] for p in alpaca_positions if p["symbol"] == sym))
        ledger_qty  = float(ledger_by_sym[sym].get("shares", 0))
        if abs(alpaca_qty - ledger_qty) > 0.5:
            findings.append(finding(
                SEV_ALERT, layer, f"QTY_MISMATCH_{sym}",
                f"{sym}: Alpaca qty={alpaca_qty:.0f}, ledger qty={ledger_qty:.0f}",
                "Share count drift — a trim may not have been recorded in ledger",
                f"Manually update position_ledger.json entry for {sym}"
            ))

    # ── 2d. Stop above current price ─────────────────────────────────────────
    for sym in alpaca_syms & ledger_active_syms:
        entry    = ledger_by_sym[sym]
        stop     = entry.get("metadata", {}).get("stop")
        price    = float(next(p["current_price"] for p in alpaca_positions if p["symbol"] == sym))
        if stop and float(stop) > price:
            findings.append(finding(
                SEV_CRITICAL, layer, f"STOP_ABOVE_PRICE_{sym}",
                f"{sym}: stop=${float(stop):.3f} > current price=${price:.2f} — EXIT 1 fires immediately",
                "Hard stop is above current market price — exit_monitor will trigger on next run",
                f"Check ledger stop for {sym}. If this is intentional after a gap-down, acknowledge manually."
            ))

    # ── 2e. Stop dangerously close ────────────────────────────────────────────
    for sym in alpaca_syms & ledger_active_syms:
        entry = ledger_by_sym[sym]
        stop  = entry.get("metadata", {}).get("stop")
        price = float(next(p["current_price"] for p in alpaca_positions if p["symbol"] == sym))
        if stop and float(stop) <= price:
            pct_from_stop = (price - float(stop)) / price * 100
            if pct_from_stop < 1.5:
                findings.append(finding(
                    SEV_WARN, layer, f"STOP_NEAR_{sym}",
                    f"{sym}: stop is {pct_from_stop:.2f}% below current price (${float(stop):.3f} vs ${price:.2f})",
                    "Normal if trail has tightened. Worth being aware of.",
                ))

    # ── 2f. Ghost health records ──────────────────────────────────────────────
    phantom_health = health_syms - alpaca_syms
    if phantom_health:
        findings.append(finding(
            SEV_WARN, layer, "PHANTOM_HEALTH",
            f"hold_health.json has records for exited positions: {sorted(phantom_health)}",
            "Stale health scores from positions no longer held — harmless but noisy",
            "Will auto-clear on next hold_monitor.py run"
        ))

    # ── 2g. No health record for held position ────────────────────────────────
    no_health = alpaca_syms - health_syms
    if no_health:
        findings.append(finding(
            SEV_WARN, layer, "NO_HEALTH_RECORD",
            f"{len(no_health)} held position(s) missing from hold_health.json: {sorted(no_health)}",
            "hold_monitor may not have run, or these positions are very new",
            "Run: python hold_monitor.py"
        ))

    if not missing_from_ledger and not ghost_in_ledger and not phantom_health and not no_health:
        findings.append(finding(SEV_OK, layer, "RECONCILIATION_CLEAN",
                                f"All {len(alpaca_syms)} positions reconciled — Alpaca, ledger, health all aligned"))

    return findings


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 3 — Position Risk Flags
# ═══════════════════════════════════════════════════════════════════════════════

def run_layer3(alpaca_positions: List[dict], account: dict) -> List[dict]:
    findings = []
    layer = "L3-PositionRisk"

    hold_health  = load_json(HOLD_HEALTH_PATH) or {}
    ledger_data  = load_json(LEDGER_PATH) or {}
    ledger_map   = {}
    for key, entry in ledger_data.get("positions", {}).items():
        if entry.get("status", "ACTIVE") == "ACTIVE":
            ledger_map[entry["symbol"]] = entry

    equity = float(account.get("equity", 0)) if account else 0.0
    alpaca_map = {p["symbol"]: p for p in alpaca_positions}

    triple_deterioration = []
    winner_at_risk       = []
    time_stop_candidates = []
    concentration_warns  = []

    for sym, pos in alpaca_map.items():
        price   = float(pos["current_price"])
        pnl_pct = float(pos["unrealized_pnl_pct"]) * 100
        pnl_usd = float(pos["unrealized_pnl"])
        qty     = float(pos["qty"])
        value   = price * qty

        health_rec  = hold_health.get(sym, {})
        health      = float(health_rec.get("health", 0.0))
        composite   = float(health_rec.get("composite", 0.0))
        tier        = health_rec.get("tier", "UNKNOWN")
        days_held   = int(health_rec.get("days_held", 0))
        stop_dist   = float(health_rec.get("stop_dist_atr", 999))

        ledger_entry = ledger_map.get(sym, {})
        entry_price  = float(ledger_entry.get("entry_price", price))
        high_water   = float(ledger_entry.get("metadata", {}).get("high_water", price))

        # ── Triple deterioration ──────────────────────────────────────────────
        # health < -0.15 AND composite < 0 AND pnl < -2%
        if health < -0.15 and composite < 0.0 and pnl_pct < -2.0:
            triple_deterioration.append({
                "sym": sym, "health": health, "composite": composite,
                "pnl_pct": pnl_pct, "tier": tier
            })

        # ── Winner at risk ────────────────────────────────────────────────────
        # pnl > 8% AND health < -0.15 (open gain + deteriorating thesis)
        if pnl_pct > 8.0 and health < -0.15:
            winner_at_risk.append({
                "sym": sym, "pnl_pct": pnl_pct, "health": health, "tier": tier
            })

        # ── Stop proximity + health deteriorating ─────────────────────────────
        if stop_dist < 2.0 and tier == "DECAYING":
            findings.append(finding(
                SEV_WARN, layer, f"STOP_PROXIMITY_{sym}",
                f"{sym}: {stop_dist:.1f} ATR from stop with DECAYING health (pnl={pnl_pct:+.1f}%)",
                f"health={health:.3f} composite={composite:.4f}",
                "Monitor for hard stop trigger — consider whether math trim is already queued"
            ))

        # ── Time stop candidate (EXIT 5 precursor) ────────────────────────────
        # Losing, held 12+ days, health and composite both < 0
        if days_held >= 12 and pnl_pct < -1.0 and health < 0.0 and composite < 0.0:
            time_stop_candidates.append({
                "sym": sym, "days_held": days_held,
                "pnl_pct": pnl_pct, "health": health
            })

        # ── Position concentration ────────────────────────────────────────────
        if equity > 0 and (value / equity) > 0.20:
            concentration_warns.append({
                "sym": sym, "pct": value / equity * 100, "value": value
            })

    # Emit triple deterioration findings
    if triple_deterioration:
        for td in triple_deterioration:
            findings.append(finding(
                SEV_ALERT, layer, f"TRIPLE_DETERIORATION_{td['sym']}",
                f"{td['sym']}: TRIPLE_DETERIORATION — health={td['health']:.3f}, "
                f"composite={td['composite']:.4f}, pnl={td['pnl_pct']:+.1f}%",
                f"Tier: {td['tier']}. All three deterioration signals active simultaneously.",
                "exit_monitor should handle this via math trim — verify it ran today and queued this symbol"
            ))
    else:
        findings.append(finding(SEV_OK, layer, "NO_TRIPLE_DETERIORATION",
                                "No positions in triple-deterioration state"))

    # Emit winner-at-risk findings
    if winner_at_risk:
        for w in winner_at_risk:
            findings.append(finding(
                SEV_WARN, layer, f"WINNER_AT_RISK_{w['sym']}",
                f"{w['sym']}: Open gain {w['pnl_pct']:+.1f}% but health={w['health']:.3f} ({w['tier']})",
                "Open profit may be at risk — thesis deteriorating while still in the money",
                "Trail stop will protect some gain. Verify trail has ratcheted appropriately."
            ))

    # Emit time-stop candidates
    if time_stop_candidates:
        for ts in time_stop_candidates:
            findings.append(finding(
                SEV_WARN, layer, f"TIME_STOP_CANDIDATE_{ts['sym']}",
                f"{ts['sym']}: EXIT 5 candidate — {ts['days_held']}d held, "
                f"pnl={ts['pnl_pct']:+.1f}%, health={ts['health']:.3f}",
                "Flat/losing position after 12+ days with deteriorating score",
                "exit_monitor EXIT 5 check requires composite<0 AND health<0 AND flat price — confirm all gates met"
            ))

    # Emit concentration warnings
    for c in concentration_warns:
        findings.append(finding(
            SEV_WARN, layer, f"CONCENTRATION_{c['sym']}",
            f"{c['sym']}: {c['pct']:.1f}% of portfolio (${c['value']:,.0f})",
            "Single position exceeds 20% of account equity",
            "Normal if position ran up from smaller entry. Verify Kelly sizing was respected at entry."
        ))

    # ── Capital utilization ───────────────────────────────────────────────────
    if equity > 0 and alpaca_positions:
        total_market_value = sum(float(p["current_price"]) * float(p["qty"])
                                 for p in alpaca_positions)
        utilization = total_market_value / equity * 100
        macro = load_json(MACRO_PATH) or {}
        regime = macro.get("macro_regime", "UNKNOWN")

        if utilization < 30 and regime in ("RISK_ON", "NEUTRAL"):
            findings.append(finding(
                SEV_INFO, layer, "LOW_CAPITAL_UTILIZATION",
                f"Capital utilization: {utilization:.1f}% in {regime} regime",
                f"${total_market_value:,.0f} deployed of ${equity:,.0f} equity",
                "Low deployment is valid if max_positions already at capacity or signals are weak"
            ))
        elif utilization > 85:
            findings.append(finding(
                SEV_WARN, layer, "HIGH_CAPITAL_UTILIZATION",
                f"Capital utilization: {utilization:.1f}% — near full deployment",
                f"${total_market_value:,.0f} deployed of ${equity:,.0f} equity",
                "Ensure no margin is being used if allow_margin=False"
            ))
        else:
            findings.append(finding(SEV_OK, layer, "CAPITAL_UTILIZATION_OK",
                                    f"Capital utilization: {utilization:.1f}% ({regime})"))

    return findings


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 4 — Operational Checks
# ═══════════════════════════════════════════════════════════════════════════════

def run_layer4() -> List[dict]:
    findings = []
    layer = "L4-Operations"

    # ── 4a. Outcome pending sidecar age ──────────────────────────────────────
    pending = load_json(OUTCOME_PENDING_PATH) or {}
    stale_pending = []
    for order_id, rec in pending.items():
        submitted = rec.get("submitted_at") or rec.get("timestamp")
        age_days  = days_since(submitted)
        if age_days is not None and age_days > 5:
            stale_pending.append((rec.get("symbol", "?"), age_days, order_id[:8]))

    if stale_pending:
        detail = ", ".join(f"{s} ({d:.0f}d, order {oid})" for s, d, oid in stale_pending)
        findings.append(finding(
            SEV_WARN, layer, "STALE_OUTCOME_PENDING",
            f"{len(stale_pending)} outcome_pending sidecar(s) older than 5 days",
            detail,
            "Run: python outcome_tracker.py  to close out stale records"
        ))
    else:
        findings.append(finding(SEV_OK, layer, "OUTCOME_PENDING_OK",
                                f"outcome_pending.json: {len(pending)} records, none stale"))

    # ── 4b. Cooldown log audit — expired entries still present ────────────────
    cooldown = load_json(COOLDOWN_PATH) or {}
    expired_blocks = []
    for sym, expiry_str in cooldown.items():
        if isinstance(expiry_str, str):
            try:
                expiry = date.fromisoformat(expiry_str[:10])
                if expiry < TODAY:
                    expired_blocks.append((sym, expiry_str[:10]))
            except Exception:
                pass

    if expired_blocks:
        syms = [s for s, _ in expired_blocks]
        findings.append(finding(
            SEV_WARN, layer, "EXPIRED_COOLDOWNS",
            f"{len(expired_blocks)} expired cooldown block(s) still in cooldown_log.json: {syms}",
            "These symbols are blocked from entry even though cooldown has passed",
            "These should auto-clear on next main.py run. If not, manually remove from cooldown_log.json"
        ))
    else:
        findings.append(finding(SEV_OK, layer, "COOLDOWNS_CLEAN",
                                f"cooldown_log.json: {len(cooldown)} active block(s), none expired"))

    # ── 4c. Gate progress ─────────────────────────────────────────────────────
    pos_outcomes = load_json(POSITION_OUTCOMES_PATH) or []
    n_total = len(pos_outcomes)
    clean   = [p for p in pos_outcomes
               if "leveraged_or_inverse_etp" not in p.get("flags", [])]
    n_clean = len(clean)

    gate_data40 = "✅ MET" if n_clean >= 40 else f"⏳ {n_clean}/40 ({40 - n_clean} to go)"
    gate_data60 = "✅ MET" if n_clean >= 60 else f"⏳ {n_clean}/60 ({60 - n_clean} to go)"
    gate_data100 = "✅ MET" if n_clean >= 100 else f"⏳ {n_clean}/100 ({100 - n_clean} to go)"

    findings.append(finding(
        SEV_INFO, layer, "GATE_PROGRESS",
        f"Data gates: DATA-40 {gate_data40}  |  DATA-60 {gate_data60}  |  DATA-100 (Kelly active) {gate_data100}",
        f"{n_total} total positions in position_outcomes.json, {n_clean} clean (excl. leveraged ETPs)",
    ))

    # ── 4d. Kelly mode ────────────────────────────────────────────────────────
    kelly = load_json(KELLY_PATH) or {}
    books = kelly.get("books", {})
    momentum_book = books.get("MOMENTUM", {})
    kelly_mode    = momentum_book.get("mode", "UNKNOWN")
    n_trades      = momentum_book.get("n_trades", 0)
    f_rec         = momentum_book.get("f_recommended", None)

    findings.append(finding(
        SEV_INFO, layer, "KELLY_STATUS",
        f"Kelly: mode={kelly_mode}, n_trades={n_trades}/100, f_recommended={f_rec}",
        "Kelly becomes ACTIVE at 100 terminal exits. Currently in SHADOW mode (advisory only)."
        if kelly_mode == "SHADOW" else "",
    ))

    # ── 4e. Slippage pending fills ────────────────────────────────────────────
    slippage = load_json(SLIPPAGE_PATH)
    if isinstance(slippage, list):
        pending_fills = [r for r in slippage if r.get("fill_price") is None]
        if pending_fills:
            # Check if any are older than 1 trading day
            old_pending = []
            for r in pending_fills:
                age = days_since(r.get("ts") or r.get("timestamp"))
                if age is not None and age > 1.5:
                    old_pending.append(r.get("symbol", "?"))
            if old_pending:
                findings.append(finding(
                    SEV_WARN, layer, "STALE_PENDING_FILLS",
                    f"{len(old_pending)} slippage record(s) with no fill_price older than 1 day: {old_pending}",
                    "Fill confirmation was never received — IS tracking incomplete",
                    "These will skew implementation shortfall stats. Backfill manually if possible."
                ))
            else:
                findings.append(finding(SEV_OK, layer, "SLIPPAGE_OK",
                                        f"slippage_log.json: {len(pending_fills)} pending (all recent)"))
        else:
            findings.append(finding(SEV_OK, layer, "SLIPPAGE_OK",
                                    f"slippage_log.json: {len(slippage)} records, all fills confirmed"))

    # ── 4f. position_outcomes.json freshness ─────────────────────────────────
    pos_age = file_age_hours(POSITION_OUTCOMES_PATH)
    if pos_age is not None and pos_age > 168:  # 7 days
        findings.append(finding(
            SEV_WARN, layer, "POSITION_OUTCOMES_STALE",
            f"position_outcomes.json not updated in {pos_age / 24:.1f} days",
            "Manual rebuild step may be falling behind — gate counters could be understated",
            "Rebuild when outcome_tracker.py closes new positions: python outcome_tracker.py"
        ))

    return findings


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 5 — Macro / Regime Situational Awareness
# ═══════════════════════════════════════════════════════════════════════════════

def run_layer5(alpaca_positions: List[dict]) -> List[dict]:
    findings = []
    layer = "L5-Macro"

    macro = load_json(MACRO_PATH) or {}
    market_dec = load_json(MARKET_DECISION_PATH) or {}

    current_regime = macro.get("macro_regime", "UNKNOWN")
    macro_score    = float(macro.get("macro_score", 0.0))
    vix_data       = macro.get("signals", {}).get("vix", {})
    vix_value      = float(vix_data.get("value", 0))

    # ── 5a. STANDBY alert ────────────────────────────────────────────────────
    decision = market_dec.get("decision", "")
    if decision == "STANDBY":
        findings.append(finding(
            SEV_ALERT, layer, "MARKET_STANDBY",
            f"MarketAgent is in STANDBY — no new entries permitted today",
            f"Reasoning: {market_dec.get('reasoning', 'N/A')}",
            "Verify this is correct given current macro regime"
        ))
    elif decision == "REDUCE":
        findings.append(finding(
            SEV_WARN, layer, "MARKET_REDUCE",
            f"MarketAgent in REDUCE mode (risk_scalar={market_dec.get('risk_scalar', '?')})",
            f"Reasoning: {market_dec.get('reasoning', 'N/A')}",
        ))
    else:
        findings.append(finding(SEV_OK, layer, "MARKET_SCAN",
                                f"MarketAgent: {decision} — normal operation"))

    # ── 5b. VIX elevated ─────────────────────────────────────────────────────
    if vix_value > 30:
        findings.append(finding(
            SEV_ALERT, layer, "VIX_ELEVATED",
            f"VIX is elevated: {vix_value:.1f}",
            "Watchdog trail multipliers shift in high-vol regime (vol_pctile > 0.75 → stop_mult=3.5)",
            "Review all positions for wider-than-expected stops"
        ))
    elif vix_value > 20:
        findings.append(finding(
            SEV_WARN, layer, "VIX_MODERATE",
            f"VIX moderately elevated: {vix_value:.1f}",
        ))
    else:
        findings.append(finding(SEV_OK, layer, "VIX_OK",
                                f"VIX: {vix_value:.1f} (normal range)"))

    # ── 5c. Regime drift per position ────────────────────────────────────────
    ledger_data = load_json(LEDGER_PATH) or {}
    drift_positions = []
    for key, entry in ledger_data.get("positions", {}).items():
        if entry.get("status", "ACTIVE") != "ACTIVE":
            continue
        entry_regime = entry.get("metadata", {}).get("regime")
        if not entry_regime or entry_regime == "BACKFILL":
            continue
        sym = entry["symbol"]
        # Flag if entered in RISK_ON but now in RISK_OFF or worse
        if entry_regime in ("RISK_ON",) and current_regime in ("RISK_OFF", "CRISIS"):
            drift_positions.append((sym, entry_regime, current_regime))
        # Also flag NEUTRAL entered, now CRISIS
        elif entry_regime == "NEUTRAL" and current_regime == "CRISIS":
            drift_positions.append((sym, entry_regime, current_regime))

    if drift_positions:
        detail = ", ".join(f"{s} (entered {er} → now {cr})"
                           for s, er, cr in drift_positions)
        findings.append(finding(
            SEV_WARN, layer, "REGIME_DRIFT",
            f"{len(drift_positions)} position(s) entered under different macro regime",
            detail,
            "Positions are still valid — this is awareness only. exit_monitor uses live composite, not entry regime."
        ))
    else:
        findings.append(finding(SEV_OK, layer, "NO_REGIME_DRIFT",
                                f"All positions entered under regime consistent with today ({current_regime})"))

    # ── 5d. Overall macro context summary ────────────────────────────────────
    spy  = macro.get("signals", {}).get("spy_trend", {})
    breadth = macro.get("signals", {}).get("sector_breadth", {})
    findings.append(finding(
        SEV_INFO, layer, "MACRO_SUMMARY",
        f"Macro: regime={current_regime}, score={macro_score:+.2f}, "
        f"VIX={vix_value:.1f}, SPY vs 50MA={'above' if spy.get('above_50ma') else 'below'}, "
        f"sector breadth={breadth.get('pct_above_50ma', '?')}%",
    ))

    return findings


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 6 — Opportunity Awareness
# ═══════════════════════════════════════════════════════════════════════════════

def run_layer6() -> List[dict]:
    findings = []
    layer = "L6-Opportunity"

    cooldown    = load_json(COOLDOWN_PATH) or {}
    composite   = load_json(COMPOSITE_CACHE_PATH) or {}
    ledger_data = load_json(LEDGER_PATH) or {}
    held_syms   = set()
    for key, entry in ledger_data.get("positions", {}).items():
        if entry.get("status", "ACTIVE") == "ACTIVE":
            held_syms.add(entry["symbol"])

    # ── 6a. Cooldown expiring in next 2 days with strong composite ────────────
    expiring_soon = []
    for sym, expiry_str in cooldown.items():
        if not isinstance(expiry_str, str):
            continue
        try:
            expiry = date.fromisoformat(expiry_str[:10])
            days_left = (expiry - TODAY).days
            if 0 <= days_left <= 2:
                comp_score = composite.get(sym)
                if comp_score is not None:
                    expiring_soon.append((sym, days_left, float(comp_score), expiry_str[:10]))
        except Exception:
            pass

    if expiring_soon:
        expiring_soon.sort(key=lambda x: -x[2])  # sort by composite desc
        items = ", ".join(
            f"{s} (expires {e}, comp={c:.2f}, {d}d left)"
            for s, d, c, e in expiring_soon
        )
        findings.append(finding(
            SEV_INFO, layer, "COOLDOWN_EXPIRING",
            f"{len(expiring_soon)} cooldown(s) expiring in ≤2 days with active composite scores",
            items,
            "These symbols become entry-eligible again soon — scanner will auto-pick up if signal persists"
        ))

    # ── 6b. High-composite symbols not currently held ─────────────────────────
    top_unowned = [(sym, float(score)) for sym, score in composite.items()
                   if sym not in held_syms and sym not in cooldown
                   and float(score) > 1.5]
    top_unowned.sort(key=lambda x: -x[1])
    if top_unowned[:5]:
        items = ", ".join(f"{s}={c:.2f}" for s, c in top_unowned[:5])
        findings.append(finding(
            SEV_INFO, layer, "HIGH_COMPOSITE_UNOWNED",
            f"Top unowned high-composite symbols (not on cooldown): {items}",
            "For awareness only — main.py applies full filter stack before any entry",
        ))

    return findings


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

SEV_ORDER = {SEV_CRITICAL: 0, SEV_ALERT: 1, SEV_WARN: 2, SEV_INFO: 3, SEV_OK: 4}
SEV_COLOR = {
    SEV_CRITICAL: ("#ff4444", "#2a0000"),
    SEV_ALERT:    ("#ff8c00", "#2a1500"),
    SEV_WARN:     ("#ffd700", "#2a2200"),
    SEV_INFO:     ("#00aaff", "#001a2a"),
    SEV_OK:       ("#00d4aa", "#001a15"),
}
SEV_EMOJI = {
    SEV_CRITICAL: "🔴",
    SEV_ALERT:    "🟠",
    SEV_WARN:     "🟡",
    SEV_INFO:     "🔵",
    SEV_OK:       "✅",
}


def build_html(all_findings: List[dict], alpaca_positions: List[dict],
               account: dict, run_ts: str) -> str:

    # Exclude OK from the summary counts for the subject line
    critical = [f for f in all_findings if f["severity"] == SEV_CRITICAL]
    alerts   = [f for f in all_findings if f["severity"] == SEV_ALERT]
    warns    = [f for f in all_findings if f["severity"] == SEV_WARN]
    infos    = [f for f in all_findings if f["severity"] == SEV_INFO]
    oks      = [f for f in all_findings if f["severity"] == SEV_OK]

    # Sort: non-OK first by severity, then OK at bottom
    sorted_findings = sorted(
        [f for f in all_findings if f["severity"] != SEV_OK],
        key=lambda f: (SEV_ORDER[f["severity"]], f["layer"])
    ) + oks

    equity = float(account.get("equity", 0)) if account else 0
    cash   = float(account.get("cash", 0)) if account else 0
    n_pos  = len(alpaca_positions)
    total_pnl = sum(float(p["unrealized_pnl"]) for p in alpaca_positions)

    # Status badge
    if critical:
        status_color = "#ff4444"
        status_label = f"CRITICAL ({len(critical)})"
    elif alerts:
        status_color = "#ff8c00"
        status_label = f"ALERT ({len(alerts)})"
    elif warns:
        status_color = "#ffd700"
        status_label = f"WARN ({len(warns)})"
    else:
        status_color = "#00d4aa"
        status_label = "ALL CLEAR"

    def row(f: dict) -> str:
        sev     = f["severity"]
        color, bg = SEV_COLOR.get(sev, ("#ffffff", "#111111"))
        emoji   = SEV_EMOJI.get(sev, "•")
        detail  = f["detail"].replace("\n", "<br>") if f.get("detail") else ""
        action  = f.get("action", "")
        detail_html = (
            f'<div style="color:#a0a0b0;font-size:11px;margin-top:4px">{detail}</div>'
            if detail else ""
        )
        action_html = (
            f'<div style="color:#00d4aa;font-size:11px;margin-top:4px;font-style:italic">'
            f'→ {action}</div>'
            if action else ""
        )
        layer_short = f["layer"].split("-")[0]
        return f"""
<tr>
  <td style="padding:10px 14px;border-bottom:1px solid #1e1e34;background:{bg};vertical-align:top">
    <div style="display:flex;align-items:flex-start;gap:10px">
      <span style="font-size:14px;min-width:20px">{emoji}</span>
      <div style="flex:1">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <span style="background:{color};color:#000;font-size:10px;font-weight:700;
                        padding:2px 6px;border-radius:3px;white-space:nowrap">{sev}</span>
          <span style="color:#6a6a8a;font-size:10px">{layer_short}</span>
          <span style="color:#e0e0e0;font-size:13px;font-weight:600">{f['message']}</span>
        </div>
        {detail_html}
        {action_html}
      </div>
    </div>
  </td>
</tr>"""

    rows_html = "\n".join(row(f) for f in sorted_findings)

    # Position table
    pos_rows = ""
    for p in sorted(alpaca_positions, key=lambda x: float(x["unrealized_pnl_pct"]), reverse=True):
        sym     = p["symbol"]
        pnl_pct = float(p["unrealized_pnl_pct"]) * 100
        pnl_usd = float(p["unrealized_pnl"])
        price   = float(p["current_price"])
        qty     = float(p["qty"])
        color   = "#00d4aa" if pnl_pct >= 0 else "#ff4444"
        pos_rows += f"""
<tr>
  <td style="padding:6px 10px;border-bottom:1px solid #1e1e34;color:#e0e0e0;font-size:12px">{sym}</td>
  <td style="padding:6px 10px;border-bottom:1px solid #1e1e34;color:#a0a0b0;font-size:12px;text-align:right">{qty:.0f}</td>
  <td style="padding:6px 10px;border-bottom:1px solid #1e1e34;color:#a0a0b0;font-size:12px;text-align:right">${price:.2f}</td>
  <td style="padding:6px 10px;border-bottom:1px solid #1e1e34;color:{color};font-size:12px;text-align:right;font-weight:600">{pnl_pct:+.1f}%</td>
  <td style="padding:6px 10px;border-bottom:1px solid #1e1e34;color:{color};font-size:12px;text-align:right">${pnl_usd:+,.0f}</td>
</tr>"""

    pnl_color = "#00d4aa" if total_pnl >= 0 else "#ff4444"

    return f"""
<html>
<body style="margin:0;padding:0;background:#0a0a1a;font-family:'Segoe UI',Arial,sans-serif">
<div style="max-width:720px;margin:0 auto;background:#12122a;border:1px solid #2a2a3e">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1a1a3e,#0d0d2b);padding:20px 24px;border-bottom:2px solid {status_color}">
    <div style="color:{status_color};font-size:11px;letter-spacing:3px;text-transform:uppercase;font-weight:700">RAPTOR MONITOR</div>
    <div style="color:#e0e0e0;font-size:20px;font-weight:700;margin-top:4px">End-of-Day System Check</div>
    <div style="display:flex;gap:16px;margin-top:8px;flex-wrap:wrap">
      <span style="color:#a0a0b0;font-size:12px">{run_ts}</span>
      <span style="background:{status_color};color:#000;font-size:11px;font-weight:700;padding:2px 8px;border-radius:3px">{status_label}</span>
    </div>
  </div>

  <!-- Account summary -->
  <div style="background:#0e0e20;padding:14px 24px;border-bottom:1px solid #1e1e34">
    <table style="width:100%;border-collapse:collapse">
      <tr>
        <td style="text-align:center;padding:6px">
          <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Equity</div>
          <div style="color:#e0e0e0;font-size:16px;font-weight:700">${equity:,.0f}</div>
        </td>
        <td style="text-align:center;padding:6px">
          <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Cash</div>
          <div style="color:#e0e0e0;font-size:16px;font-weight:700">${cash:,.0f}</div>
        </td>
        <td style="text-align:center;padding:6px">
          <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Positions</div>
          <div style="color:#e0e0e0;font-size:16px;font-weight:700">{n_pos}</div>
        </td>
        <td style="text-align:center;padding:6px">
          <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Open P&L</div>
          <div style="color:{pnl_color};font-size:16px;font-weight:700">${total_pnl:+,.0f}</div>
        </td>
        <td style="text-align:center;padding:6px">
          <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Findings</div>
          <div style="color:#e0e0e0;font-size:16px;font-weight:700">
            <span style="color:#ff4444">{len(critical)}C</span>&nbsp;
            <span style="color:#ff8c00">{len(alerts)}A</span>&nbsp;
            <span style="color:#ffd700">{len(warns)}W</span>&nbsp;
            <span style="color:#00aaff">{len(infos)}I</span>
          </div>
        </td>
      </tr>
    </table>
  </div>

  <!-- Findings -->
  <div style="padding:0">
    <div style="padding:12px 24px 6px;color:#6a6a8a;font-size:10px;text-transform:uppercase;letter-spacing:2px;border-bottom:1px solid #1e1e34">
      Findings — {len(all_findings)} total
    </div>
    <table style="width:100%;border-collapse:collapse">
      {rows_html}
    </table>
  </div>

  <!-- Position table -->
  {'<div style="padding:12px 24px 6px;color:#6a6a8a;font-size:10px;text-transform:uppercase;letter-spacing:2px;border-bottom:1px solid #1e1e34">Open Positions</div>' if alpaca_positions else ''}
  {'<table style="width:100%;border-collapse:collapse"><tr><th style="padding:6px 10px;text-align:left;color:#6a6a8a;font-size:10px;text-transform:uppercase">Symbol</th><th style="padding:6px 10px;text-align:right;color:#6a6a8a;font-size:10px;text-transform:uppercase">Qty</th><th style="padding:6px 10px;text-align:right;color:#6a6a8a;font-size:10px;text-transform:uppercase">Price</th><th style="padding:6px 10px;text-align:right;color:#6a6a8a;font-size:10px;text-transform:uppercase">P&L%</th><th style="padding:6px 10px;text-align:right;color:#6a6a8a;font-size:10px;text-transform:uppercase">P&L$</th></tr>' + pos_rows + '</table>' if alpaca_positions else ''}

  <!-- Footer -->
  <div style="padding:14px 24px;border-top:1px solid #2a2a3e;text-align:center">
    <div style="color:#3a3a5e;font-size:10px">RAPTOR v5.4 Monitor | Paper Trading | Math-First</div>
    <div style="color:#3a3a5e;font-size:10px;margin-top:2px">Generated {run_ts}</div>
  </div>

</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL SEND
# ═══════════════════════════════════════════════════════════════════════════════

def send_email(html: str, n_critical: int, n_alerts: int, n_warns: int) -> None:
    if not EMAIL_PASSWORD:
        logger.error("EMAIL_APP_PASSWORD not set in .env — cannot send email")
        return

    if n_critical > 0:
        subject_tag = f"🔴 CRITICAL ({n_critical})"
    elif n_alerts > 0:
        subject_tag = f"🟠 ALERT ({n_alerts})"
    elif n_warns > 0:
        subject_tag = f"🟡 WARN ({n_warns})"
    else:
        subject_tag = "✅ ALL CLEAR"

    subject = f"RAPTOR Monitor {subject_tag} — {TODAY_ISO}"
    msg = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.send_message(msg)
        logger.info("Monitor email sent: %s", subject)
    except Exception as e:
        logger.error("Email send failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Raptor end-of-day monitor")
    parser.add_argument("--preview",  action="store_true", help="Save HTML to logs/monitor_preview.html, no email")
    parser.add_argument("--dry-run",  action="store_true", help="Print findings to stdout only, no email")
    args = parser.parse_args()

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    logger.info("=" * 60)
    logger.info("RAPTOR MONITOR — %s", run_ts)
    logger.info("=" * 60)

    # Connect to Alpaca once — shared across layers
    logger.info("Connecting to Alpaca...")
    alpaca_positions, account = get_alpaca_positions()
    if not alpaca_positions and not account:
        logger.warning("No Alpaca data available — L2/L3 will be limited")

    # Refresh macro_context.json inline — written at 9AM, now 7+ hours stale.
    # L5 regime drift analysis must use today's closing regime, not pre-market.
    # Falls back to cached file on any failure.
    try:
        from macro_context import build_macro_context as _bmc_mon
        _mc_fresh = _bmc_mon()
        if _mc_fresh.get("macro_regime") in ("RISK_ON", "NEUTRAL", "RISK_OFF", "CRISIS"):
            logger.info("[Monitor] macro_context refreshed inline: regime=%s score=%.3f",
                        _mc_fresh.get("macro_regime"), _mc_fresh.get("macro_score", 0))
        else:
            logger.warning("[Monitor] inline macro refresh returned unrecognised regime — using cached")
    except Exception as _mce_mon:
        logger.warning("[Monitor] inline macro refresh failed (%s) — using cached macro_context.json", _mce_mon)

    # Run all layers
    all_findings: List[dict] = []
    for layer_fn, label in [
        (run_layer1,                         "L1-Infrastructure"),
        (lambda: run_layer2(alpaca_positions), "L2-Reconciliation"),
        (lambda: run_layer3(alpaca_positions, account), "L3-PositionRisk"),
        (run_layer4,                         "L4-Operations"),
        (lambda: run_layer5(alpaca_positions), "L5-Macro"),
        (run_layer6,                         "L6-Opportunity"),
    ]:
        logger.info("Running %s...", label)
        try:
            layer_findings = layer_fn()
            all_findings.extend(layer_findings)
            non_ok = [f for f in layer_findings if f["severity"] != SEV_OK]
            logger.info("  %s: %d findings (%d non-OK)", label, len(layer_findings), len(non_ok))
        except Exception as e:
            logger.exception("Layer %s crashed: %s", label, e)
            all_findings.append(finding(
                SEV_ALERT, label, f"{label}_CRASHED",
                f"Monitor layer {label} threw an exception: {e}",
                f"Check {INTERNAL_LOG_PATH} for full traceback",
                "Fix the monitor, not Raptor — this is a monitor bug"
            ))

    # Counts
    critical = [f for f in all_findings if f["severity"] == SEV_CRITICAL]
    alerts   = [f for f in all_findings if f["severity"] == SEV_ALERT]
    warns    = [f for f in all_findings if f["severity"] == SEV_WARN]

    logger.info("=" * 60)
    logger.info("SUMMARY: %d CRITICAL  %d ALERT  %d WARN  %d total findings",
                len(critical), len(alerts), len(warns), len(all_findings))

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN — findings only, no email")
        print("=" * 60)
        for f in sorted(all_findings, key=lambda x: (SEV_ORDER[x["severity"]], x["layer"])):
            emoji = SEV_EMOJI.get(f["severity"], "•")
            print(f"{emoji} [{f['severity']:8s}] [{f['layer']}] {f['message']}")
            if f.get("detail"):
                print(f"         {f['detail'][:100]}")
            if f.get("action"):
                print(f"         → {f['action']}")
        return

    # Build HTML
    html = build_html(all_findings, alpaca_positions, account, run_ts)

    if args.preview:
        preview_path = LOG_DIR / "monitor_preview.html"
        preview_path.write_text(html, encoding="utf-8")
        logger.info("Preview saved to %s", preview_path)
        print(f"\nPreview saved: {preview_path}")
        return

    # Send email
    send_email(html, len(critical), len(alerts), len(warns))

    # Write machine-readable findings log
    findings_log = LOG_DIR / f"monitor_{TODAY_STR}.json"
    with findings_log.open("w", encoding="utf-8") as f_out:
        json.dump({
            "run_ts": run_ts,
            "summary": {
                "critical": len(critical),
                "alerts":   len(alerts),
                "warns":    len(warns),
                "total":    len(all_findings),
            },
            "findings": all_findings,
        }, f_out, indent=2)
    logger.info("Findings log written: %s", findings_log)


if __name__ == "__main__":
    main()
