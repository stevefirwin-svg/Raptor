"""
ledger_lock.py — Cross-process mutex for position_ledger.json
================================================================
FOUND 2026-07-01 (senior-dev pipeline audit): watchdog.py (every 15 min,
fresh process each cycle) and exit_monitor.py (every 30 min, one long-lived
self-looping process) both independently do:

    ledger = Ledger()                  # loads a full in-memory snapshot
    ... decide to exit/trim/ratchet ...
    ledger.data["positions"][...] = ... # mutate the in-memory snapshot
    ledger._save()                      # atomically overwrite the WHOLE file

`_save()`'s tmp-file + os.replace() is atomic at the OS level — it prevents
the file from being left corrupted/half-written if a process dies mid-write
(the original OneDrive-era failure mode). It does NOT prevent a classic
lost-update race: if watchdog loads its snapshot, then exit_monitor loads
ITS OWN (now slightly newer) snapshot, then watchdog writes back its
snapshot — that write silently erases whatever exit_monitor already wrote,
because the whole file gets replaced, not just the field that changed.

Concretely: if watchdog executes a hard-stop SELL and calls
`ledger.record_exit(...)` while exit_monitor is mid-cycle (loaded slightly
earlier, hasn't saved yet), exit_monitor's next `_save()` — which still
shows that position as OPEN in its stale in-memory copy — will overwrite
watchdog's exit and put the position right back in the ledger as ACTIVE,
even though it's genuinely flat on Alpaca. That reproduces exactly the
GHOST_IN_LEDGER / MISSING_FROM_LEDGER symptom class this system keeps
alerting on, from a source with no exception to log — nothing crashes,
the file is valid JSON, it's just wrong.

Watchdog and exit_monitor's schedules are offset by design (5+ min apart)
and each cycle is short (~10-20s), so a true collision is uncommon rather
than routine — but there's no coordination between them at all, so the
probability isn't zero, and a stop-loss silently reverting to "still open"
in the ledger is exactly the kind of thing this system can't afford to
get wrong silently.

Fix: a simple cross-process file lock, held for the *entire* span from
Ledger() load through the final _save() of a cycle, in both scripts.
Fails open (proceeds without the lock, with a warning) if the lock can't
be acquired within LOCK_TIMEOUT_SECONDS — consistent with this codebase's
existing "never block trading over an infra issue" convention (see
main.py's raptor_scan.lock, exit_monitor.py's exit_monitor.lock).

Usage:
    from ledger_lock import ledger_lock
    with ledger_lock("watchdog"):
        ledger = Ledger()
        ... read/modify/save ...

GENERALIZED 2026-07-01 (Tier 1/2 audit): slippage_tracker.py's record_fill()/
backfill_slippage() had the exact same read-modify-write race on
slippage_log.json (watchdog.py's 15-min cycle and exit_monitor.py's 30-min
cycle can each independently load, append, and atomically rewrite the whole
file, silently dropping the other's concurrent record). Rather than duplicate
this primitive, the lock logic below is now generic (`_file_lock`); `ledger_lock`
and the new `slippage_lock` are both thin wrappers over it with their own
mutex file, so a collision on one never blocks the other.
"""

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger("raptor.ledger_lock")

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LEDGER_LOCK_FILE    = os.path.join(_LOG_DIR, "position_ledger.mutex")
SLIPPAGE_LOCK_FILE  = os.path.join(_LOG_DIR, "slippage_log.mutex")
HOLD_HISTORY_LOCK_FILE = os.path.join(_LOG_DIR, "hold_history.mutex")
LOCK_TIMEOUT_SECONDS = 20      # max time to wait for the lock before proceeding anyway
LOCK_POLL_SECONDS    = 0.25
STALE_LOCK_SECONDS   = 120     # a lock older than this means its holder almost certainly died


@contextmanager
def _file_lock(lock_path: str, owner: str, label: str):
    """
    Held for the full duration of a read-modify-write cycle, across
    processes. Uses exclusive file creation (O_CREAT|O_EXCL) as the mutex
    primitive — atomic on both Windows and POSIX.
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    acquired = False
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(f"{owner} {datetime.now().isoformat()}")
            acquired = True
            break
        except FileExistsError:
            # Someone else holds it — but if it's ancient, its holder almost
            # certainly crashed/died without cleaning up. Clear it rather than
            # waiting out the full timeout for a lock nobody's coming back for.
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > STALE_LOCK_SECONDS:
                    logger.warning("%s: stale lock (age %.0fs) held by another process — clearing.", label, age)
                    os.remove(lock_path)
                    continue
            except OSError:
                pass  # lock file vanished between the check and now (released concurrently) — just retry
            time.sleep(LOCK_POLL_SECONDS)
        except OSError as e:
            logger.warning("%s: could not create lock file (%s) — proceeding without lock.", label, e)
            acquired = False
            break

    if not acquired and time.monotonic() >= deadline:
        logger.warning(
            "%s: could not acquire within %ds (held by another process) — "
            "proceeding WITHOUT the lock rather than blocking trading. "
            "A concurrent write may be lost this cycle.",
            label, LOCK_TIMEOUT_SECONDS,
        )

    try:
        yield acquired
    finally:
        if acquired:
            try:
                os.remove(lock_path)
            except OSError as e:
                logger.warning("%s: could not release lock file (%s).", label, e)


@contextmanager
def ledger_lock(owner: str = "unknown"):
    """Cross-process mutex for position_ledger.json read-modify-write cycles."""
    with _file_lock(LEDGER_LOCK_FILE, owner, "ledger_lock") as acquired:
        yield acquired


@contextmanager
def slippage_lock(owner: str = "unknown"):
    """Cross-process mutex for slippage_log.json read-modify-write cycles."""
    with _file_lock(SLIPPAGE_LOCK_FILE, owner, "slippage_lock") as acquired:
        yield acquired


@contextmanager
def hold_history_lock(owner: str = "unknown"):
    """Cross-process mutex for hold_history.json read-modify-write cycles.

    ADDED 2026-07-06 (recap crash follow-up audit): hold_monitor.py's
    save_history() (tmp-write + os.replace()) had no lock at all, unlike
    position_ledger.json and slippage_log.json which got this same treatment
    in the 2026-07-01 audit (S12-1, S12-9). This was NOT a hypothetical risk —
    it already crashed live on 2026-06-11 (recap_errors.log:
    "run_monitor() failed: [WinError 32] The process cannot access the file
    because it is being used by another process: 'hold_history.json.tmp' ->
    'hold_history.json'"), when run_monitor() is invoked from more than one
    of its three call sites (Raptor MidDay Monitor 12:30 PM, Start_Afternoon_
    Monitor 3:50 PM, daily_recap.py's inline call ~4:15 PM) close enough
    together for a collision. That crash didn't take down the recap email
    itself (daily_recap.py already isolates run_monitor() in its own
    try/except) but it did silently skip that day's health-history write.
    Same generic `_file_lock()` primitive, own mutex file so it never
    contends with the ledger or slippage locks.
    """
    with _file_lock(HOLD_HISTORY_LOCK_FILE, owner, "hold_history_lock") as acquired:
        yield acquired
