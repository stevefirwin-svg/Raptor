"""
RAPTOR WATCHDOG v4.0  —  watchdog.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Self-healing process manager. Restarts bot on crash.
Crash rate detection, heartbeat file, optional alerts.

Run: python watchdog.py
"""

import subprocess
import sys
import os
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

BOT_DIR = Path(__file__).parent
BOT_SCRIPT = BOT_DIR / "main.py"
LOG_DIR = BOT_DIR / "logs"
CRASH_LOG_DIR = LOG_DIR / "crashes"
HEARTBEAT_FILE = LOG_DIR / "heartbeat.json"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"

MAX_CRASHES_HOUR = 5
RESTART_DELAY = 15
RAPID_CRASH_SEC = 30

LOG_DIR.mkdir(exist_ok=True)
CRASH_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(WATCHDOG_LOG),
    ],
)
logger = logging.getLogger("RaptorWatchdog")

_start_time = datetime.now(timezone.utc)
_crash_history: list = []


def _uptime_seconds() -> int:
    return int((datetime.now(timezone.utc) - _start_time).total_seconds())


def write_heartbeat(status: str, pid: int = 0, extra: dict = None):
    data = {
        "status": status,
        "pid": pid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": _uptime_seconds(),
        "version": "4.0",
        **(extra or {}),
    }
    try:
        HEARTBEAT_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def record_crash(exit_code: int, runtime: float, output: str):
    crash = {
        "timestamp": datetime.now().isoformat(),
        "exit_code": exit_code,
        "runtime_sec": round(runtime, 1),
        "rapid": runtime < RAPID_CRASH_SEC,
    }
    _crash_history.append(crash)
    crash_file = CRASH_LOG_DIR / f"crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    crash_file.write_text(
        f"Exit code : {exit_code}\n"
        f"Runtime   : {runtime:.1f}s\n"
        f"Timestamp : {crash['timestamp']}\n"
        f"\n--- Last output ---\n{output}"
    )
    logger.error(f"Crash log: {crash_file.name}")


def crashes_in_last_hour() -> int:
    cutoff = datetime.now() - timedelta(hours=1)
    return sum(
        1 for c in _crash_history
        if datetime.fromisoformat(c["timestamp"]) > cutoff
    )


def run_bot(attempt: int):
    cmd = [sys.executable, str(BOT_SCRIPT)] + sys.argv[1:]
    logger.info(f"Starting Raptor v4.0 (attempt #{attempt})")
    write_heartbeat("starting", extra={"attempt": attempt})

    start = time.time()
    output = []

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(BOT_DIR),
        )
        write_heartbeat("running", pid=proc.pid, extra={"attempt": attempt})

        for line in proc.stdout:
            print(line, end="", flush=True)
            output.append(line.strip())
            if len(output) > 300:
                output.pop(0)

        proc.wait()
        runtime = time.time() - start
        return proc.returncode, runtime, "\n".join(output[-50:])

    except KeyboardInterrupt:
        try:
            proc.terminate()
        except Exception:
            pass
        return 0, time.time() - start, "User interrupt"
    except Exception as e:
        return 1, time.time() - start, str(e)


def main():
    logger.info("=" * 60)
    logger.info("RAPTOR WATCHDOG v4.0")
    logger.info("=" * 60)

    attempt = 0

    while True:
        attempt += 1
        exit_code, runtime, last_output = run_bot(attempt)

        if exit_code == 0:
            logger.info("Bot exited cleanly — watchdog stopping")
            write_heartbeat("stopped_clean")
            break

        rapid = runtime < RAPID_CRASH_SEC
        logger.error(f"Bot CRASHED after {runtime:.1f}s (code {exit_code})")
        record_crash(exit_code, runtime, last_output)
        write_heartbeat("crashed", extra={
            "exit_code": exit_code, "runtime": runtime,
        })

        recent = crashes_in_last_hour()
        if recent >= MAX_CRASHES_HOUR:
            logger.error(
                f"Too many crashes ({recent}/{MAX_CRASHES_HOUR}). Stopping."
            )
            write_heartbeat("stopped_too_many_crashes")
            break

        delay = RESTART_DELAY * (3 if rapid else 1)
        if rapid:
            logger.warning(f"RAPID CRASH — increasing delay to {delay}s")

        logger.info(f"Restarting in {delay}s ({recent}/{MAX_CRASHES_HOUR})")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            logger.info("Watchdog stopped by user")
            write_heartbeat("stopped_user")
            break

    uptime = _uptime_seconds()
    logger.info(f"Finished. Uptime: {uptime//3600}h {(uptime%3600)//60}m")


if __name__ == "__main__":
    main()
