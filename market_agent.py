"""
market_agent.py — Layer 4: Market Agent
Raptor Autonomous Agent Roadmap

Reads macro_context.json and decides whether to run the daily scan at all.
Outputs: SCAN / REDUCE / STANDBY + risk_scalar (0.5-1.0)
Writes: market_decision.json

Run:
    python market_agent.py            # evaluate and write market_decision.json
    python market_agent.py --summary  # print today's decision

Called automatically at the top of main.py before any scan logic.
Scheduled via Task Scheduler at 9:15 AM ET (after macro_context.py at 9:00 AM,
before main.py at 9:35 AM).
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OLLAMA_URL         = "http://localhost:11434/api/generate"
MODEL              = "llama3.2"  # faster than mistral 7B, sufficient for rule-based gate
TIMEOUT            = 45
MACRO_CONTEXT_PATH = "macro_context.json"
MARKET_DECISION_PATH = "market_decision.json"

logger = logging.getLogger("MarketAgent")

# ── Prompt ────────────────────────────────────────────────────────────────────

MARKET_PROMPT = """You are a session-level risk gatekeeper for a quantitative swing trading system.
Your job is to decide whether the system should scan for new entries today based on macro conditions.
You are NOT evaluating individual stocks. You are evaluating the overall market environment.

Current macro context:
{macro}

Decision rules:
- STANDBY: extreme risk — do not scan for new entries at all.
  Trigger when: macro_regime is CRISIS, OR (VIX > 35 AND credit spread STRESS)
- REDUCE: elevated risk — scan but cut position sizing.
  Trigger when: macro_regime is RISK_OFF, OR (VIX > 25 AND sector breadth WEAKENING or BROAD_WEAKNESS)
  Set risk_scalar between 0.5 and 0.75 based on severity.
- SCAN: normal operation — proceed with full sizing.
  Trigger when: macro_regime is NEUTRAL or RISK_ON and no extreme signals present.
  Set risk_scalar to 1.0.

Be conservative. If signals conflict, lean toward REDUCE over SCAN.
STANDBY should be rare — only extreme conditions.

Respond ONLY with this exact JSON structure, no other text:
{{"decision": "SCAN or REDUCE or STANDBY", "risk_scalar": 0.5-1.0, "confidence": 0.0-1.0, "reasoning": "one sentence"}}"""

# ── Ollama call ───────────────────────────────────────────────────────────────

def _call_ollama(prompt: str) -> str:
    body = json.dumps({
        "model":   MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0.1}
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
        return raw.get("response", "{}").strip()

# ── Rule-based fallback (no Mistral needed) ───────────────────────────────────

def _rule_based_decision(macro: dict) -> dict:
    """
    Deterministic fallback when Mistral is unavailable.
    Uses the same logic as the prompt but in pure Python.
    Always runs first — Mistral is used to add reasoning, not to make the call.
    """
    # compute_regime_score() returns key "regime" not "macro_regime" — match data_feeds.py
    regime  = macro.get("regime", macro.get("macro_regime", "UNKNOWN"))
    signals = macro.get("signals", {})
    vix     = signals.get("vix", {})
    credit  = signals.get("credit_spread", {})
    breadth = signals.get("sector_breadth", {})

    vix_val    = vix.get("value") or 0
    vix_regime = vix.get("regime", "UNKNOWN")
    cr_regime  = credit.get("regime", "UNKNOWN")
    br_regime  = breadth.get("regime", "UNKNOWN")

    # STANDBY conditions
    if regime == "CRISIS":
        return {"decision": "STANDBY", "risk_scalar": 0.0,
                "reasoning": "Macro regime is CRISIS — no new entries."}
    if vix_val > 35 and cr_regime == "STRESS":
        return {"decision": "STANDBY", "risk_scalar": 0.0,
                "reasoning": f"VIX {vix_val:.1f} + credit STRESS — halting scan."}

    # REDUCE conditions
    if regime == "RISK_OFF":
        scalar = 0.6 if br_regime in ("WEAKENING", "BROAD_WEAKNESS") else 0.75
        return {"decision": "REDUCE", "risk_scalar": scalar,
                "reasoning": f"Macro RISK_OFF — reducing sizing to {scalar:.0%}."}
    if vix_val > 25 and br_regime in ("WEAKENING", "BROAD_WEAKNESS"):
        return {"decision": "REDUCE", "risk_scalar": 0.65,
                "reasoning": f"VIX {vix_val:.1f} + breadth {br_regime} — reducing sizing to 65%."}

    # SCAN
    return {"decision": "SCAN", "risk_scalar": 1.0,
            "reasoning": f"Macro {regime} — full scan, normal sizing."}

# ── Main ──────────────────────────────────────────────────────────────────────

def evaluate_session() -> dict:
    """
    Evaluate today's macro and decide session mode.
    Returns the decision dict and writes market_decision.json.
    Rule-based logic runs first (deterministic). Mistral adds reasoning if available.
    """
    # Load macro context
    macro_path = Path(MACRO_CONTEXT_PATH)
    if not macro_path.exists():
        logger.warning("[MarketAgent] macro_context.json not found — defaulting to SCAN")
        decision = {
            "decision":    "SCAN",
            "risk_scalar": 1.0,
            "confidence":  0.5,
            "reasoning":   "macro_context.json missing — passthrough default",
            "source":      "fallback",
        }
        _write(decision)
        return decision

    try:
        macro = json.loads(macro_path.read_text())
    except Exception as e:
        logger.warning(f"[MarketAgent] Failed to read macro_context.json: {e} — defaulting to SCAN")
        decision = {
            "decision":    "SCAN",
            "risk_scalar": 1.0,
            "confidence":  0.5,
            "reasoning":   "macro_context.json unreadable — passthrough default",
            "source":      "fallback",
        }
        _write(decision)
        return decision

    # Step 1: Rule-based decision (always runs, deterministic)
    rule_decision = _rule_based_decision(macro)
    logger.info(f"[MarketAgent] Rule-based: {rule_decision['decision']} "
                f"(scalar={rule_decision['risk_scalar']}) — {rule_decision['reasoning']}")

    # Step 2: Ask Mistral for reasoning (non-blocking — rule decision is authoritative)
    agent_summary = macro.get("agent_summary", str(macro))
    prompt = MARKET_PROMPT.format(macro=agent_summary)
    mistral_decision = None
    try:
        raw = _call_ollama(prompt)
        logger.info(f"[MarketAgent] Mistral RAW: {repr(raw[:300])}")
        start = raw.find("{")
        if start != -1:
            depth, end = 0, start
            for i, ch in enumerate(raw[start:], start):
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            mistral_decision = json.loads(raw[start:end])
    except Exception as e:
        logger.warning(f"[MarketAgent] Mistral unavailable: {e} — using rule-based only")

    # Rule-based is authoritative for STANDBY/REDUCE/SCAN.
    # Mistral can only provide reasoning string and confidence.
    # It CANNOT override a STANDBY or change the risk_scalar by more than 0.1.
    final = {
        "decision":    rule_decision["decision"],
        "risk_scalar": rule_decision["risk_scalar"],
        "confidence":  mistral_decision.get("confidence", 0.9) if mistral_decision else 0.9,
        "reasoning":   mistral_decision.get("reasoning", rule_decision["reasoning"]) if mistral_decision else rule_decision["reasoning"],
        "source":      "rule+llama3.2" if mistral_decision else "rule_only",
        "macro_regime": macro.get("macro_regime", "UNKNOWN"),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }

    _write(final)

    logger.info(f"[MarketAgent] FINAL: {final['decision']} | "
                f"scalar={final['risk_scalar']} | {final['reasoning']}")
    return final


def _write(decision: dict):
    Path(MARKET_DECISION_PATH).write_text(json.dumps(decision, indent=2))
    logger.info(f"[MarketAgent] Written → {MARKET_DECISION_PATH}")


def load_market_decision() -> dict:
    """
    Load today's market decision. Called by main.py at session start.
    Falls back to SCAN if file missing or stale (>12 hours old).
    """
    path = Path(MARKET_DECISION_PATH)
    if not path.exists():
        return {"decision": "SCAN", "risk_scalar": 1.0, "reasoning": "No market_decision.json — defaulting to SCAN"}

    try:
        decision = json.loads(path.read_text())
        # Staleness check — if file is older than 12 hours, don't trust it
        ts_str = decision.get("timestamp")
        if ts_str:
            from datetime import timezone
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if age_hours > 12:
                logger.warning(f"[MarketAgent] market_decision.json is {age_hours:.1f}h old — defaulting to SCAN")
                return {"decision": "SCAN", "risk_scalar": 1.0, "reasoning": "Stale decision file — defaulting to SCAN"}
        return decision
    except Exception as e:
        logger.warning(f"[MarketAgent] Failed to load market_decision.json: {e} — defaulting to SCAN")
        return {"decision": "SCAN", "risk_scalar": 1.0, "reasoning": "Load error — defaulting to SCAN"}


def print_summary():
    path = Path(MARKET_DECISION_PATH)
    if not path.exists():
        print("[MarketAgent] market_decision.json not found. Run without --summary first.")
        return
    d = json.loads(path.read_text())
    print(f"\n{'='*62}")
    print(f"  MARKET AGENT DECISION — {d.get('timestamp','N/A')[:19]}")
    print(f"{'='*62}")
    print(f"  Decision    : {d.get('decision')}")
    print(f"  Risk scalar : {d.get('risk_scalar')}")
    print(f"  Confidence  : {d.get('confidence')}")
    print(f"  Macro regime: {d.get('macro_regime')}")
    print(f"  Reasoning   : {d.get('reasoning')}")
    print(f"  Source      : {d.get('source')}")
    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
    parser = argparse.ArgumentParser(description="Raptor Market Agent — Layer 4")
    parser.add_argument("--summary", action="store_true", help="Print today's market decision")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        evaluate_session()
        print_summary()
