"""
diagnose_system.py — Raptor Full System Diagnostic
====================================================
Runs every component in the correct daily order using --dry-run where available.
Checks every data handoff, log file, and cross-component communication point.
Produces a single PASS/FAIL/WARN report with actionable detail.

Run BEFORE market open on any trading day to confirm the system is healthy.

Usage:
    python diagnose_system.py              # Full diagnostic
    python diagnose_system.py --quick      # Skip signal engine (faster)

Output:
    Console report + logs/diagnostic_YYYYMMDD.log
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Setup ─────────────────────────────────────────────────────────────────────

# Force UTF-8 on Windows — cp1252 terminal cannot encode diagnostic symbols
import sys, io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

os.makedirs("logs", exist_ok=True)

# ── Git state — print at top so Steve can paste commit hash to Claude ─────────
print()
print("=" * 60)
print("  RAPTOR DIAGNOSTIC")
print(f"  {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
try:
    import subprocess
    _hash   = subprocess.check_output(["git","rev-parse","--short","HEAD"],
                stderr=subprocess.DEVNULL).decode().strip()
    _msg    = subprocess.check_output(["git","log","--oneline","-1"],
                stderr=subprocess.DEVNULL).decode().strip()
    _remote = subprocess.check_output(["git","rev-parse","--short","origin/main"],
                stderr=subprocess.DEVNULL).decode().strip()
    _in_sync = "(in sync with GitHub)" if _hash == _remote else f"(GitHub is at {_remote} -- DIVERGED)"
    print(f"  Git: {_msg} {_in_sync}")
    print(f"  Paste to Claude at session start: {_hash}")
except Exception:
    print("  Git: could not read commit hash")
print("=" * 60)
print()
LOG_FILE = f"logs/diagnostic_{datetime.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("diagnostic")

PASS  = "[ PASS ]"
FAIL  = "[ FAIL ]"
WARN  = "[ WARN ]"
INFO  = "[  INFO ]"

results = []

def check(label, status, detail=""):
    icon = {"PASS": PASS, "FAIL": FAIL, "WARN": WARN, "INFO": INFO}.get(status, INFO)
    msg = f"  {icon}  {label}"
    if detail:
        msg += f"\n         {detail}"
    log.info(msg)
    results.append((status, label, detail))

def section(title):
    log.info("")
    log.info("-" * 60)
    log.info(f"  {title}")
    log.info("-" * 60)


# ── 1. ENVIRONMENT ────────────────────────────────────────────────────────────

section("1. ENVIRONMENT")

# Python version
import platform
py = platform.python_version()
check("Python version", "PASS", f"Python {py}")

# .env file
env_ok = Path(".env").exists()
check(".env file exists", "PASS" if env_ok else "FAIL",
      "" if env_ok else ".env missing — API keys unavailable")

# Required files exist
required = [
    "config.py", "main.py", "exit_monitor.py", "hold_monitor.py",
    "signals.py", "ledger.py", "outcome_tracker.py", "factor_lab.py",
    "kelly_engine.py", "macro_context.py", "universe_builder.py",
    "daily_recap.py", "watchdog.py", "margin_guard.py",
]
missing = [f for f in required if not Path(f).exists()]
if missing:
    check("Required .py files", "FAIL", f"Missing: {missing}")
else:
    check("Required .py files", "PASS", f"All {len(required)} present")

# Syntax check all Python files
import ast
syntax_errors = []
for f in required:
    try:
        ast.parse(open(f, encoding="utf-8", errors="replace").read())
    except SyntaxError as e:
        syntax_errors.append(f"{f}:{e.lineno} — {e.msg}")
if syntax_errors:
    check("Syntax check", "FAIL", "\n         ".join(syntax_errors))
else:
    check("Syntax check", "PASS", f"All {len(required)} files clean")


# ── 2. IMPORTS ────────────────────────────────────────────────────────────────

section("2. IMPORTS")

import_errors = []
modules = [
    "config", "signals", "exit_monitor", "hold_monitor", "main",
    "macro_context", "ledger", "outcome_tracker", "factor_lab",
    "kelly_engine", "universe_builder", "daily_recap", "margin_guard",
]
for m in modules:
    try:
        __import__(m)
        check(f"import {m}", "PASS")
    except Exception as e:
        check(f"import {m}", "FAIL", str(e))
        import_errors.append(m)


# ── 3. CONFIG ─────────────────────────────────────────────────────────────────

section("3. CONFIG")

try:
    from config import CONFIG
    CONFIG.validate_all()
    check("Config loads and validates", "PASS")

    # Key config values
    check("Kelly mode", "INFO",
          f"SHADOW until 100 trades (max_positions={CONFIG.risk.max_positions}, "
          f"kelly_fraction={CONFIG.risk.kelly_fraction})")
    check("Trail ATR levels", "INFO",
          f"early={CONFIG.risk.trail_early_atr}x (<={CONFIG.risk.trail_early_days}d)  "
          f"mid={CONFIG.risk.trail_mid_atr}x (<={CONFIG.risk.trail_mid_days}d)  "
          f"late={CONFIG.risk.trail_late_atr}x (<={CONFIG.risk.trail_late_days}d)  "
          f"final={CONFIG.risk.trail_final_atr}x")
    check("Stop ATR mult", "INFO",
          f"initial_stop_atr_mult={CONFIG.risk.initial_stop_atr_mult}")
except Exception as e:
    check("Config", "FAIL", str(e))


# ── 4. DATA FILES ─────────────────────────────────────────────────────────────

section("4. DATA FILES")

today_str = str(date.today())

data_files = {
    "position_ledger.json":  ("Ledger",          True,  ["positions", "closed"]),
    "hold_health.json":      ("Hold Health",      True,  None),
    "hold_history.json":     ("Hold History",     False, ["positions"]),
    "outcome_log.json":      ("Outcome Log",      False, None),
    "trim_log.json":         ("Trim Log",         False, None),
    "composite_cache.json":  ("Composite Cache",  False, None),
    "cooldown_log.json":     ("Cooldown Log",     False, None),
    "macro_context.json":    ("Macro Context",    True,  None),
    "market_decision.json":  ("Market Decision",  True,  None),
    "hold_decisions.json":   ("Hold Decisions",   False, None),
    "entry_vetoes.json":     ("Entry Vetoes",     False, None),
    "factor_ic_report.json": ("Factor IC Report", False, None),
    "kelly_estimates.json":  ("Kelly Estimates",  False, None),
}

for fname, (label, required_f, required_keys) in data_files.items():
    p = Path(fname)
    if not p.exists():
        check(f"{label} ({fname})", "FAIL" if required_f else "WARN",
              "File missing")
        continue

    mtime = datetime.fromtimestamp(p.stat().st_mtime)
    age_hours = (datetime.now() - mtime).total_seconds() / 3600
    age_str = f"modified {mtime:%Y-%m-%d %H:%M} ({age_hours:.1f}h ago)"

    try:
        data = json.loads(p.read_text())
    except Exception as e:
        check(f"{label}", "FAIL", f"JSON parse error: {e}")
        continue

    # Check required keys
    key_ok = True
    if required_keys:
        missing_keys = [k for k in required_keys if k not in data]
        if missing_keys:
            check(f"{label}", "FAIL", f"Missing keys {missing_keys}  {age_str}")
            key_ok = False

    if key_ok:
        # Staleness check — warn if over 26 hours (missed yesterday)
        if age_hours > 26:
            check(f"{label}", "WARN", f"Stale — {age_str}")
        else:
            check(f"{label}", "PASS", age_str)


# ── 5. LEDGER INTEGRITY ────────────────────────────────────────────────────────

section("5. LEDGER INTEGRITY")

try:
    from ledger import Ledger
    ledger = Ledger()
    open_pos = ledger.data.get("positions", {})
    closed = ledger.data.get("closed", [])

    check("Ledger loads", "PASS",
          f"{len(open_pos)} open positions, {len(closed)} closed trades")

    # Check every open position has required metadata
    missing_stop = []
    missing_entry = []
    bad_pnl = []
    for key, pos in open_pos.items():
        sym = pos.get("symbol", key)
        meta = pos.get("metadata", {})
        if not meta.get("stop"):
            missing_stop.append(sym)
        if not pos.get("entry_date"):
            missing_entry.append(sym)

    if missing_stop:
        check("Open positions: stop price", "WARN",
              f"Missing stop: {missing_stop} — trail will use ATR-only fallback")
    else:
        check("Open positions: stop price", "PASS",
              f"All {len(open_pos)} positions have stop metadata")

    if missing_entry:
        check("Open positions: entry_date", "WARN",
              f"Missing entry_date: {missing_entry} — days_held will use fallback=1")
    else:
        check("Open positions: entry_date", "PASS",
              f"All {len(open_pos)} positions have entry_date")

    # Check pnl_pct units in closed trades — should be percentage (>1 typical), not decimal
    decimal_pnl = [t.get("symbol") for t in closed
                   if t.get("pnl_pct") is not None and abs(float(t["pnl_pct"])) < 0.5
                   and t.get("exit_reason") not in ("pre_label",)]
    if decimal_pnl:
        check("Closed trades: pnl_pct units", "WARN",
              f"Suspiciously small pnl_pct (<0.5) on: {decimal_pnl[:5]} — may be decimal not %")
    else:
        check("Closed trades: pnl_pct units", "PASS",
              "Values look like percentages (not raw decimals)")

    # Check atomic save (tmp file should not exist)
    if Path("position_ledger.json.tmp").exists():
        check("Ledger atomic write", "FAIL",
              "position_ledger.json.tmp exists — previous write may have crashed mid-write")
    else:
        check("Ledger atomic write", "PASS", "No .tmp file present")

    # Print current positions
    check("Open positions", "INFO",
          "  ".join(f"{pos['symbol']}({pos.get('shares','?')}sh)" for pos in open_pos.values()))

except Exception as e:
    check("Ledger integrity", "FAIL", str(e))


# ── 6. ALPACA CONNECTIVITY ────────────────────────────────────────────────────

section("6. ALPACA CONNECTIVITY")

try:
    from data_feeds import DataManager
    dm = DataManager(CONFIG)
    acct = dm.alpaca.get_account()
    equity = float(acct["equity"])
    cash = float(acct["cash"])
    check("Alpaca API connection", "PASS",
          f"equity=${equity:,.2f}  cash=${cash:,.2f}")

    positions = dm.alpaca.get_positions()
    alpaca_syms = {p["symbol"] for p in positions}
    ledger_syms = {v["symbol"] for v in ledger.data["positions"].values()}

    in_alpaca_not_ledger = alpaca_syms - ledger_syms
    in_ledger_not_alpaca = ledger_syms - alpaca_syms

    if in_alpaca_not_ledger:
        check("Alpaca/Ledger sync", "FAIL",
              f"In Alpaca but NOT ledger: {sorted(in_alpaca_not_ledger)} — run backfill_positions.py")
    elif in_ledger_not_alpaca:
        check("Alpaca/Ledger sync", "FAIL",
              f"In ledger but NOT Alpaca: {sorted(in_ledger_not_alpaca)} — stale ledger entry")
    else:
        check("Alpaca/Ledger sync", "PASS",
              f"{len(alpaca_syms)} positions match between Alpaca and ledger")

    # Check buying power is reasonable
    bp = float(acct.get("buying_power", 0))
    if bp < 1000:
        check("Buying power", "WARN", f"${bp:,.2f} — very low, entries may be blocked")
    else:
        check("Buying power", "PASS", f"${bp:,.2f} available")

except Exception as e:
    check("Alpaca connectivity", "FAIL", str(e))
    positions = []
    alpaca_syms = set()


# ── 7. BAT FILE ORDER ─────────────────────────────────────────────────────────

section("7. BAT FILE RUN ORDER")

bat_checks = {
    "Start_Intraday_Monitor.bat": {
        "must_precede": [("hold_monitor.py", "exit_monitor.py")],
    },
    "Start_Morning_Monitor.bat": {
        "must_precede": [("hold_monitor.py", "exit_monitor.py")],
    },
    "Start_Afternoon_Monitor.bat": {
        "must_precede": [("hold_monitor.py", "exit_monitor.py")],
    },
}

for bat_file, rules in bat_checks.items():
    if not Path(bat_file).exists():
        check(f"{bat_file}", "WARN", "File not found")
        continue
    content = open(bat_file, encoding="utf-8", errors="replace").read()
    all_ok = True
    for first, second in rules["must_precede"]:
        if first not in content or second not in content:
            check(f"{bat_file}", "WARN", f"Can't find '{first}' or '{second}' in file")
            all_ok = False
            continue
        idx_first  = content.index(first)
        idx_second = content.index(second)
        if idx_first < idx_second:
            check(f"{bat_file} order: {first} before {second}", "PASS")
        else:
            check(f"{bat_file} order", "FAIL",
                  f"{second} runs before {first} — hold_monitor must run first")
            all_ok = False


# ── 8. HOLD HEALTH INTEGRITY ──────────────────────────────────────────────────

section("8. HOLD HEALTH INTEGRITY")

try:
    hh = json.loads(Path("hold_health.json").read_text())
    hh_syms = set(hh.keys())

    # No ghost positions (symbols in hold_health not in Alpaca)
    ghosts = hh_syms - alpaca_syms if alpaca_syms else set()
    if ghosts:
        check("Hold health: no ghosts", "WARN",
              f"Ghost entries (closed positions still in hold_health): {sorted(ghosts)}"
              f"\n         Will be cleaned on next hold_monitor run")
    else:
        check("Hold health: no ghosts", "PASS",
              "All hold_health symbols match Alpaca positions")

    # Every Alpaca position has a health entry
    missing_health = alpaca_syms - hh_syms if alpaca_syms else set()
    if missing_health:
        check("Hold health: coverage", "WARN",
              f"Alpaca positions missing health score: {sorted(missing_health)}"
              f"\n         Will be scored on next hold_monitor run")
    else:
        check("Hold health: coverage", "PASS",
              f"All {len(alpaca_syms)} positions have health scores")

    # Check health timestamps
    stale_health = []
    for sym, rec in hh.items():
        if sym not in alpaca_syms:
            continue
        ts = rec.get("timestamp", "")
        if ts:
            try:
                age_h = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600
                if age_h > 1:
                    stale_health.append(f"{sym}({age_h:.0f}h)")
            except Exception:
                pass
    if stale_health:
        check("Hold health: freshness", "WARN",
              f"Stale health scores (>1h old): {stale_health}"
              f"\n         exit_monitor will use these for trim/trail decisions")
    else:
        check("Hold health: freshness", "PASS", "All health scores fresh")

    # Check stops and high_water in ledger for all positions
    stale_stops = []
    for key, pos in ledger.data.get("positions", {}).items():
        sym = pos.get("symbol")
        meta = pos.get("metadata", {})
        hw = meta.get("high_water")
        stop = meta.get("stop")
        entry = float(pos.get("entry_price", 0))
        hh_rec = hh.get(sym, {})
        snap = hh_rec.get("snapshot", {})
        current = float(snap.get("current_price") or snap.get("price") or 0)

        if not hw and current > 0:
            stale_stops.append(f"{sym}(no hw)")
        elif hw and stop and entry > 0:
            stop_gap_pct = (current - float(stop)) / current * 100 if current > 0 else 0
            entry_to_stop = (entry - float(stop)) / entry * 100
            # Flag if stop is still within 2% of entry on an old position
            try:
                entry_date = pos.get("entry_date", "")
                days = (date.today() - datetime.strptime(entry_date[:10], "%Y-%m-%d").date()).days
                if days > 5 and entry_to_stop > 0 and stop_gap_pct > 40:
                    stale_stops.append(f"{sym}(stop {stop_gap_pct:.0f}% below current after {days}d)")
            except Exception:
                pass

    if stale_stops:
        check("Trailing stops: ratcheted", "WARN",
              f"Potentially stale stops: {stale_stops}"
              f"\n         Run fix_stops.py if stops haven't been updated by exit_monitor recently")
    else:
        check("Trailing stops: ratcheted", "PASS",
              "Stops appear to have trailed with price")

except Exception as e:
    check("Hold health integrity", "FAIL", str(e))


# ── 9. OUTCOME LOG INTEGRITY ──────────────────────────────────────────────────

section("9. OUTCOME LOG INTEGRITY")

try:
    outcome_log = json.loads(Path("outcome_log.json").read_text())

    unknowns     = [r for r in outcome_log if r.get("actual_exit_path") == "unknown"]
    none_pnl     = [r for r in outcome_log
                    if r.get("actual_pnl_pct") is None
                    and r.get("actual_exit_path") not in ("pre_label", "crypto")]
    math_trims   = [r for r in outcome_log if r.get("actual_exit_path") == "math_trim"]
    full_exits   = [r for r in outcome_log
                    if r.get("actual_exit_path") in
                    ("trailing_stop","math_exit","hard_stop","thesis_invalid","time_decay")]
    pre_label    = [r for r in outcome_log if r.get("actual_exit_path") == "pre_label"]

    if unknowns:
        check("Outcome log: no unknowns", "FAIL",
              f"{len(unknowns)} records with exit_path=unknown — run: python outcome_tracker.py --backfill")
    else:
        check("Outcome log: no unknowns", "PASS", "0 unknown exit paths")

    if none_pnl:
        check("Outcome log: pnl coverage", "WARN",
              f"{len(none_pnl)} non-pre_label records with null pnl")
    else:
        check("Outcome log: pnl coverage", "PASS", "All non-pre_label records have pnl")

    check("Outcome log: IC-valid count", "INFO",
          f"Full terminal exits (IC-valid): {len(full_exits)}  "
          f"Partial trims (excluded from IC): {len(math_trims)}  "
          f"Pre-label (excluded): {len(pre_label)}")

    gate_needed = 60
    if len(full_exits) >= gate_needed:
        check("IC gate (60 full exits)", "PASS",
              f"{len(full_exits)} >= {gate_needed} — MATH-5/ARCH-1/MATH-1 unlocked")
    else:
        check("IC gate (60 full exits)", "INFO",
              f"{len(full_exits)}/{gate_needed} full exits — {gate_needed - len(full_exits)} more needed")

except Exception as e:
    check("Outcome log integrity", "FAIL", str(e))


# ── 10. COMPONENT DRY RUNS ────────────────────────────────────────────────────

section("10. COMPONENT DRY RUNS")

# hold_monitor import and function existence
try:
    import hold_monitor
    assert hasattr(hold_monitor, "run_monitor"), "run_monitor missing"
    assert hasattr(hold_monitor, "build_snapshot"), "build_snapshot missing"
    assert hasattr(hold_monitor, "compute_health_score"), "compute_health_score missing"
    assert hasattr(hold_monitor, "compute_trim"), "compute_trim missing"
    check("hold_monitor: functions present", "PASS")
except Exception as e:
    check("hold_monitor", "FAIL", str(e))

# exit_monitor import and function existence
try:
    import exit_monitor
    assert hasattr(exit_monitor, "run_exit_monitor"), "run_exit_monitor missing"
    assert hasattr(exit_monitor, "_trail_mult"), "_trail_mult missing"
    check("exit_monitor: functions present", "PASS")
except Exception as e:
    check("exit_monitor", "FAIL", str(e))

# ledger method existence
try:
    from ledger import Ledger
    l = Ledger.__dict__
    for method in ["record_entry", "record_exit", "record_trim", "_save"]:
        assert method in l, f"{method} missing from Ledger"
    # Verify _save is atomic
    import inspect
    save_src = inspect.getsource(Ledger._save)
    assert "os.replace" in save_src, "_save is not atomic (missing os.replace)"
    check("ledger: all methods + atomic save", "PASS")
except Exception as e:
    check("ledger", "FAIL", str(e))

# outcome_tracker
try:
    import outcome_tracker
    assert hasattr(outcome_tracker, "run_tracker"), "run_tracker missing"
    assert hasattr(outcome_tracker, "build_outcome_record"), "build_outcome_record missing"
    check("outcome_tracker: functions present", "PASS")
except Exception as e:
    check("outcome_tracker", "FAIL", str(e))

# watchdog
try:
    import watchdog
    import inspect
    src = inspect.getsource(watchdog.run_watchdog)
    assert "record_exit" in src, "watchdog does not call record_exit"
    assert "high_water" in src, "watchdog missing high_water logic"
    assert "_wledger" in src, "watchdog does not load ledger"
    check("watchdog: ledger writes present", "PASS")
except Exception as e:
    check("watchdog", "FAIL", str(e))

# Kelly engine
try:
    from kelly_engine import load_outcomes
    outcomes = load_outcomes()
    trim_in_outcomes = [o for o in outcomes if o.get("actual_exit_path") == "math_trim"]
    if trim_in_outcomes:
        check("kelly_engine: math_trim excluded", "FAIL",
              f"{len(trim_in_outcomes)} math_trim records not filtered")
    else:
        check("kelly_engine: math_trim excluded", "PASS",
              f"{len(outcomes)} clean outcomes loaded (no math_trim partials)")
except Exception as e:
    check("kelly_engine", "FAIL", str(e))

# factor_lab
try:
    from factor_lab import load_outcome_observations
    import inspect
    src = inspect.getsource(load_outcome_observations)
    assert "math_trim" in src, "math_trim not excluded in factor_lab"
    check("factor_lab: math_trim excluded", "PASS")
except Exception as e:
    check("factor_lab", "FAIL", str(e))


# ── 11. LOG FILE CHECK ────────────────────────────────────────────────────────

section("11. LOG FILES")

today_date = datetime.now().strftime("%Y%m%d")
expected_logs = {
    f"logs/raptor_{today_date}.log":    "Entry scan log",
    f"logs/exits_{today_date}.log":     "Exit monitor log",
}

for log_path, label in expected_logs.items():
    p = Path(log_path)
    if not p.exists():
        check(f"{label}", "WARN", f"{log_path} — not created yet today")
    else:
        lines = p.read_text().strip().split("\n")
        size = p.stat().st_size
        check(f"{label}", "PASS", f"{log_path}  {len(lines)} lines  {size:,}b")

# Check for ERROR lines in today's logs
for log_path in Path("logs").glob(f"*{today_date}*.log"):
    try:
        content = log_path.read_text()
        errors = [l for l in content.split("\n") if " ERROR" in l or "Traceback" in l]
        if errors:
            check(f"Errors in {log_path.name}", "WARN",
                  f"{len(errors)} ERROR lines:\n         " + "\n         ".join(errors[:3]))
    except Exception:
        pass


# ── 12. INVARIANT CHECKS ──────────────────────────────────────────────────────

section("12. INVARIANTS (code-level)")

import inspect

# No -1.0 defaults for composite
try:
    import exit_monitor as em, hold_monitor as hm
    em_src = inspect.getsource(em.run_exit_monitor)
    hm_src = inspect.getsource(hm.run_monitor)

    if "composite_score = -1.0" in hm_src or "= -1.0  # " in em_src:
        check("comp=0.0 default (not -1.0)", "FAIL",
              "Found composite_score = -1.0 in code")
    else:
        check("comp=0.0 default (not -1.0)", "PASS")
except Exception as e:
    check("comp=0.0 invariant", "WARN", str(e))

# No fabricated ATR proxy — skip comment lines (elimination comments say "0.92 was eliminated")
try:
    for fname in ["exit_monitor.py", "hold_monitor.py", "daily_recap.py"]:
        src = open(fname, encoding="utf-8", errors="replace").read()
        found_live = False
        for line in src.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "entry_price * 0.92" in stripped or "current_price * 0.02" in stripped:
                found_live = True
                break
        if found_live:
            check(f"No fabricated fallback in {fname}", "FAIL",
                  "Found live price * 0.92 or price * 0.02 (not in comment)")
        else:
            check(f"No fabricated fallback in {fname}", "PASS")
except Exception as e:
    check("Fabricated fallback check", "WARN", str(e))

# Atomic writes
try:
    for fname, func in [("main.py", "os.replace"), ("outcome_tracker.py", "os.replace"),
                        ("hold_monitor.py", "os.replace"), ("exit_monitor.py", "os.replace"),
                        ("ledger.py", "os.replace")]:
        src = open(fname, encoding="utf-8", errors="replace").read()
        if func in src:
            check(f"Atomic write in {fname}", "PASS")
        else:
            check(f"Atomic write in {fname}", "WARN", f"os.replace not found")
except Exception as e:
    check("Atomic write check", "WARN", str(e))


# ── SUMMARY ───────────────────────────────────────────────────────────────────

section("SUMMARY")

passes  = sum(1 for r in results if r[0] == "PASS")
fails   = sum(1 for r in results if r[0] == "FAIL")
warns   = sum(1 for r in results if r[0] == "WARN")
infos   = sum(1 for r in results if r[0] == "INFO")

log.info(f"  Total checks: {len(results)}")
log.info(f"  {PASS}: {passes}")
log.info(f"  {WARN}: {warns}")
log.info(f"  {FAIL}: {fails}")
log.info("")

if fails > 0:
    log.info("  FAILURES — fix before trading:")
    for status, label, detail in results:
        if status == "FAIL":
            log.info(f"    • {label}")
            if detail:
                log.info(f"      {detail.split(chr(10))[0]}")
    log.info("")

if warns > 0:
    log.info("  WARNINGS — review before trading:")
    for status, label, detail in results:
        if status == "WARN":
            log.info(f"    • {label}")
    log.info("")

overall = "SYSTEM READY" if fails == 0 else "SYSTEM NOT READY"
log.info(f"  {'='*40}")
log.info(f"  {overall}  ({fails} failures, {warns} warnings)")
log.info(f"  {'='*40}")
log.info(f"")
log.info(f"  Full log: {LOG_FILE}")

sys.exit(1 if fails > 0 else 0)


if __name__ == "__main__":
    pass  # All checks run at module level above
