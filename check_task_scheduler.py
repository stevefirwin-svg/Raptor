"""
RAPTOR Task Scheduler Diagnostics
===================================
Checks all RAPTOR-related scheduled tasks: last run time, last result,
next run time, trigger config, and flags common failure causes.

Usage:
    python check_task_scheduler.py

Requires Windows Task Scheduler (runs via PowerShell subprocess).
No admin rights needed to READ task status.
"""

import subprocess
import json
import sys
from datetime import datetime, timedelta

# ── Tasks to check ────────────────────────────────────────────────────────────
# If your task names differ, update these. Script also auto-discovers any task
# with "raptor" or "recap" in the name (case-insensitive).
KNOWN_TASKS = [
    "Start_Daily_Recap",
    "Start_Raptor",
    "Start_Entry",
    "Start_PreMarket",
    "Start_Morning_Monitor",
    "Start_Afternoon_Monitor",
    "Start_Recap",
    "Start_Raptor_Recap",
    "Start_Watchdog",
    "Start_Viper",
    "Start_Crypto",
]

# Exit codes that mean success
SUCCESS_CODES = {0, 267009}  # 267009 = task is currently running

# Known error code meanings
ERROR_MAP = {
    0x1:        "Generic error — script crashed (non-zero exit code)",
    0x2:        "File not found — check working directory or script path",
    0x41300:    "Task is ready (never run yet)",
    0x41301:    "Task is running right now",
    0x41302:    "Task disabled",
    0x41303:    "Task has not run yet",
    0x41304:    "No more scheduled runs",
    0x41306:    "Task terminated (exceeded time limit)",
    0xC000013A: "Script terminated by Ctrl+C or killed",
    0xC0000142: "DLL init failed — Python environment issue",
    0x80070002: "File not found at launch path",
    0x80070005: "Access denied — run Task Scheduler as the correct user",
}


def hex_result(code):
    try:
        c = int(code)
        return hex(c) if c != 0 else "0x0 (Success)"
    except Exception:
        return str(code)


def friendly_error(code):
    try:
        c = int(code)
        return ERROR_MAP.get(c, ERROR_MAP.get(c & 0xFFFFFFFF, "Unknown error code"))
    except Exception:
        return ""


def run_ps(cmd):
    """Run a PowerShell command and return stdout."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True
    )
    return result.stdout.strip(), result.stderr.strip()


def get_all_raptor_tasks():
    """Auto-discover all tasks with 'raptor', 'recap', 'viper', 'watchdog' in name."""
    ps = r"""
    Get-ScheduledTask | Where-Object {
        $_.TaskName -match 'raptor|recap|viper|watchdog|start_entry|start_pre|morning|afternoon|crypto'
    } | Select-Object -ExpandProperty TaskName
    """
    out, _ = run_ps(ps)
    discovered = [t.strip() for t in out.splitlines() if t.strip()]
    # Merge with known list, deduplicate case-insensitively
    all_names = list({t.lower(): t for t in KNOWN_TASKS + discovered}.values())
    return all_names


def get_task_info(task_name):
    """Pull full task info via PowerShell Get-ScheduledTaskInfo + Get-ScheduledTask."""
    ps = f"""
    try {{
        $t = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop
        $i = Get-ScheduledTaskInfo -TaskName '{task_name}' -ErrorAction Stop
        $action = $t.Actions[0]
        $trigger = $t.Triggers[0]
        [PSCustomObject]@{{
            TaskName      = $t.TaskName
            State         = $t.State.ToString()
            LastRunTime   = $i.LastRunTime.ToString('o')
            LastResult    = $i.LastTaskResult
            NextRunTime   = $i.NextRunTime.ToString('o')
            Execute       = $action.Execute
            Arguments     = $action.Arguments
            WorkingDir    = $action.WorkingDirectory
            TriggerType   = if ($trigger) {{ $trigger.GetType().Name }} else {{ 'None' }}
            TriggerEnabled = if ($trigger) {{ $trigger.Enabled.ToString() }} else {{ 'N/A' }}
        }}
    }} catch {{
        [PSCustomObject]@{{
            TaskName = '{task_name}'
            State    = 'NOT_FOUND'
            LastRunTime = ''
            LastResult = -1
            NextRunTime = ''
            Execute = ''
            Arguments = ''
            WorkingDir = ''
            TriggerType = ''
            TriggerEnabled = ''
        }}
    }} | ConvertTo-Json
    """
    out, err = run_ps(ps)
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def parse_dt(s):
    if not s:
        return None
    try:
        # Remove trailing Z or timezone offset for simple parse
        s = s.rstrip("Z").split("+")[0].split("-")[0] if "T" in s else s
        return datetime.fromisoformat(s[:19])
    except Exception:
        return None


def print_separator(char="─", width=70):
    print(char * width)


def check_task(name):
    info = get_task_info(name)
    if info is None or info.get("State") == "NOT_FOUND":
        return False  # skip — task doesn't exist

    state       = info.get("State", "?")
    last_run    = parse_dt(info.get("LastRunTime", ""))
    last_result = info.get("LastResult", -1)
    next_run    = parse_dt(info.get("NextRunTime", ""))
    execute     = info.get("Execute", "")
    arguments   = info.get("Arguments", "")
    working_dir = info.get("WorkingDir", "")
    trigger     = info.get("TriggerType", "")
    trig_en     = info.get("TriggerEnabled", "")

    now = datetime.now()

    # ── Determine status ──────────────────────────────────────────────────────
    result_code = int(last_result) if last_result is not None else -1
    success = result_code in SUCCESS_CODES or result_code == 0

    print_separator()
    status_icon = "✅" if success else ("⚠️ " if state == "Disabled" else "❌")
    print(f"  {status_icon}  {name}   [{state}]")
    print_separator()

    # Last run
    if last_run:
        ago = now - last_run
        ago_str = _ago(ago)
        run_ok = "✅" if success else "❌"
        print(f"  Last Run    : {last_run.strftime('%Y-%m-%d %H:%M:%S')}  ({ago_str} ago)")
        print(f"  Last Result : {hex_result(last_result)}  {run_ok}")
        if not success:
            msg = friendly_error(result_code)
            if msg:
                print(f"               → {msg}")
    else:
        print("  Last Run    : Never")

    # Next run
    if next_run and next_run > now:
        till = next_run - now
        print(f"  Next Run    : {next_run.strftime('%Y-%m-%d %H:%M:%S')}  (in {_ago(till)})")
    else:
        print(f"  Next Run    : {'Not scheduled / disabled' if not next_run else next_run.strftime('%Y-%m-%d %H:%M:%S')}")

    # Config
    print(f"  Trigger     : {trigger}  (enabled={trig_en})")
    print(f"  Execute     : {execute}")
    if arguments:
        print(f"  Arguments   : {arguments}")
    if working_dir:
        print(f"  Working Dir : {working_dir}")

    # ── Common failure checks ─────────────────────────────────────────────────
    warnings = []

    if state == "Disabled":
        warnings.append("TASK IS DISABLED — enable it in Task Scheduler")

    if trig_en == "False":
        warnings.append("Trigger is disabled — task will never auto-run")

    if last_run and (now - last_run) > timedelta(hours=26) and state != "Disabled":
        warnings.append(f"Last run was {_ago(now - last_run)} ago — may have missed today's schedule")

    if not working_dir:
        warnings.append("No Working Directory set — relative paths (logs/, .json) will fail")

    if execute and "pythonw" not in execute.lower() and "python" not in execute.lower():
        warnings.append(f"Executable doesn't look like Python: {execute}")

    if result_code == 0x1:
        warnings.append("Exit code 0x1 = script crashed — check logs/ folder for today's error")

    if result_code == 0x2:
        warnings.append("Exit code 0x2 = file not found — verify script path and working directory")

    if warnings:
        print()
        for w in warnings:
            print(f"  ⚠️  {w}")

    print()
    return True


def _ago(td):
    total = int(td.total_seconds())
    if total < 0:
        total = -total
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        h, m = divmod(total // 60, 60)
        return f"{h}h {m}m"
    d, rem = divmod(total, 86400)
    h = rem // 3600
    return f"{d}d {h}h"


def check_python_env():
    """Confirm Python version and key packages."""
    print_separator("═")
    print("  PYTHON ENVIRONMENT")
    print_separator("═")
    print(f"  Python      : {sys.version.split()[0]}  ({sys.executable})")
    for pkg in ["numpy", "pandas", "alpaca_trade_api", "requests"]:
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", "?")
            print(f"  {pkg:<22}: {ver} ✅")
        except ImportError:
            print(f"  {pkg:<22}: NOT INSTALLED ❌")
    print()


def main():
    print()
    print_separator("═")
    print("  RAPTOR TASK SCHEDULER DIAGNOSTICS")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print_separator("═")
    print()

    check_python_env()

    print_separator("═")
    print("  SCHEDULED TASKS")
    print_separator("═")
    print()

    tasks = get_all_raptor_tasks()
    found = 0
    for task in sorted(set(tasks)):
        if check_task(task):
            found += 1

    if found == 0:
        print("  No RAPTOR tasks found in Task Scheduler.")
        print("  Check that tasks were created with names matching:")
        for t in KNOWN_TASKS:
            print(f"    - {t}")
        print()

    print_separator("═")
    print("  HOW TO FIX COMMON ISSUES")
    print_separator("═")
    print("""
  1. Exit code 0x1 (script crashed)
       → Run the script manually: python daily_recap.py
       → Check today's log: logs\\raptor_YYYYMMDD.log

  2. Working directory missing
       → Task Scheduler > task > Actions > Edit
       → Set "Start in" to: C:\\Users\\steve\\OneDrive\\Desktop\\Raptor

  3. Task never runs / disabled
       → Task Scheduler > right-click task > Enable

  4. "Run only when user is logged on"
       → Task Scheduler > task > General
       → Change to "Run whether user is logged on or not"
       → Check "Run with highest privileges"

  5. Python not found by Task Scheduler
       → Use full path in Action: C:\\Users\\steve\\AppData\\Local\\Programs\\Python\\Python313\\python.exe
       → Or use the .bat launcher (preferred): Start_Daily_Recap.bat

  6. Task ran but no email arrived
       → Run: python daily_recap.py --preview
       → Check logs/recap_preview.html for errors
""")


if __name__ == "__main__":
    main()
