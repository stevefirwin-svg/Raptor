"""
premarket_scanner.py — Raptor Pre-Market Initialization
=========================================================
Runs the two pre-market preparation steps in sequence:
  1. macro_context.py  — fetch FRED + SPY + VIX → macro_context.json
  2. market_agent.py   — deterministic SCAN/REDUCE/STANDBY → market_decision.json

Called by Start_PreMarket.bat at 9:00 AM ET via Task Scheduler.
Replaces the missing premarket_scanner.py reference that caused silent crashes.

Usage:
  python premarket_scanner.py          # run both steps
  python premarket_scanner.py --dry    # print macro summary only, no writes
"""

import logging
import os
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [premarket] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join("logs", f"premarket_{datetime.now():%Y%m%d}.log"),
            encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("raptor.premarket")


def run(dry: bool = False) -> bool:
    """Returns True if both steps succeeded; False if either raised.

    LOGIC FIX 2026-07-01 (Tier 1/2 audit): previously every exception was
    caught, logged, and swallowed, and the function always fell through to
    "Pre-market init complete." with no way for the Task Scheduler job (or
    any monitoring) to detect a FATAL failure — the process always exited 0.
    Since this script runs unattended at 9:00 AM ET, a silent macro_context
    or market_agent failure could go unnoticed for days. Now tracks failures
    and __main__ exits non-zero when either step failed, consistent with the
    fail-closed philosophy already established in margin_guard.py.
    """
    os.makedirs("logs", exist_ok=True)
    logger.info("=" * 60)
    logger.info("PRE-MARKET INIT — %s", datetime.now().strftime("%Y-%m-%d %H:%M ET"))
    logger.info("=" * 60)

    ok = True

    # ── Step 1: Macro context ─────────────────────────────────────────────────
    logger.info("Step 1/2: Building macro context (FRED + SPY + VIX)...")
    try:
        from macro_context import build_macro_context
        mc = build_macro_context()
        regime = mc.get("macro_regime", "UNKNOWN")
        score  = mc.get("macro_score", 0)
        vix    = (mc.get("signals", {}).get("vix") or {}).get("value", "?")
        logger.info(
            "  macro_context.json written: regime=%s  score=%.3f  VIX=%s",
            regime, score, vix
        )
        if dry:
            import json
            print("\n--- Macro Context Summary ---")
            print(json.dumps({
                "macro_regime": regime,
                "macro_score": score,
                "vix": vix,
                "signals": {k: v.get("regime") if isinstance(v, dict) else v
                            for k, v in mc.get("signals", {}).items()},
            }, indent=2))
    except Exception as e:
        logger.exception("FATAL: macro_context.py failed — market_agent will use cached file: %s", e)
        ok = False

    # ── Step 2: Market agent ──────────────────────────────────────────────────
    if dry:
        logger.info("Step 2/2: DRY RUN — skipping market_decision.json write")
        return ok

    logger.info("Step 2/2: Running market agent (SCAN/REDUCE/STANDBY)...")
    try:
        from market_agent import evaluate_session
        decision = evaluate_session()
        logger.info(
            "  market_decision.json written: decision=%s  scalar=%.2f  source=%s",
            decision.get("decision"), decision.get("risk_scalar", 1.0),
            decision.get("source", "?")
        )
        logger.info("  Reasoning: %s", decision.get("reasoning", ""))
    except Exception as e:
        logger.exception("FATAL: market_agent.py failed — main.py will default to SCAN: %s", e)
        ok = False

    if ok:
        logger.info("Pre-market init complete.")
    else:
        logger.error("Pre-market init completed WITH FAILURES — see FATAL entries above.")
    return ok


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Raptor pre-market initialization")
    parser.add_argument("--dry", action="store_true", help="Print macro summary only, no writes")
    args = parser.parse_args()
    success = run(dry=args.dry)
    sys.exit(0 if success else 1)
