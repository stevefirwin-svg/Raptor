"""
agent_layer.py — Ollama-based Entry and Hold reasoning agents
Advisory only. Core signal math and exit logic are never touched.
Uses urllib (stdlib only) — no aiohttp dependency, no proxy issues.
Agents run in a thread pool — failure always defaults to PASS/HOLD.
"""

import json
import os
import re
import logging
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_PING  = "http://localhost:11434/api/tags"   # health check endpoint
MODEL        = "llama3.2"  # 3.2B vs mistral 7.2B — 2-3x faster, sufficient for rule-based prompts
TIMEOUT      = 45   # seconds per call — reduced from 120; if Mistral needs more, it's stalled
PING_TIMEOUT = 5    # seconds for health check before aborting entire batch

logger = logging.getLogger("AgentLayer")

# ── Macro Context Loader ───────────────────────────────────────────────────────

def _load_macro_summary() -> str:
    """Load today's macro regime summary for agent prompt injection."""
    try:
        p = Path("macro_context.json")
        if not p.exists():
            return "MACRO REGIME: UNKNOWN — macro_context.json not found"
        ctx = json.loads(p.read_text())
        return ctx.get("agent_summary", "MACRO REGIME: UNKNOWN")
    except Exception:
        return "MACRO REGIME: UNKNOWN — failed to load"


# ── Ollama Health Check ────────────────────────────────────────────────────────

def _ollama_alive() -> bool:
    """
    Ping Ollama /api/tags endpoint. Returns True if responsive within PING_TIMEOUT.
    Called once before any batch — if dead, entire batch fast-fails to passthrough
    instead of burning TIMEOUT seconds per symbol.
    """
    try:
        req = urllib.request.Request(OLLAMA_PING, method="GET")
        with urllib.request.urlopen(req, timeout=PING_TIMEOUT):
            return True
    except Exception as e:
        logger.warning(f"Ollama health check failed: {e} — skipping agent batch (passthrough)")
        return False


# ── Prompt Templates ───────────────────────────────────────────────────────────

# FIX 2026-06-03: Tightened prompt to prevent LLM from substituting its own
# regime judgment for the provided macro_regime field value. Previously the
# model was hallucinating RISK_OFF when it saw high kelly_fraction values,
# triggering rule 6 vetoes on RISK_ON candidates (KRE, HPE, HOOD, NOW all
# blocked incorrectly on 2026-06-03). Added explicit instruction to use only
# the literal field values from the candidate JSON.
ENTRY_PROMPT = """You are a quantitative trade filter. Apply the rules below mechanically.
Use ONLY the exact field values from the candidate JSON. Do NOT infer, assume, or override any field with your own market judgment.

Macro context (for reference only — do not override macro_regime from candidate): {macro}

Candidate: {context}

VETO if ANY of these rules are explicitly met using the candidate's literal field values:
1. regime="MIXED" AND composite_score < 1.0
2. kelly_fraction > 0.10 AND atr_pct > 3.5
3. days_since_earnings < 5
4. vix_regime="SPIKE" AND market_momentum_scalar < 0.6
5. macro_regime="CRISIS"
6. macro_regime="RISK_OFF" AND kelly_fraction > 0.07

PASS if macro_regime is RISK_ON, NEUTRAL, or BULLISH — these are never veto conditions regardless of other fields.
Default is PASS. Only VETO when a numbered rule above is literally and explicitly met by the candidate values.
Do NOT infer the regime from kelly_fraction, atr_pct, or any other field. Use only the macro_regime value provided.

Respond ONLY with valid JSON, no other text. Use only double quotes. Do not put quotes inside string values:
{"decision": "PASS or VETO", "confidence": 0.0-1.0, "veto_reason": "rule number and reason or null", "flags": []}"""


HOLD_PROMPT = """You are a quantitative position analyst. Evaluate whether to HOLD, TRIM, or EXIT based strictly on the numbers provided.

Macro: {macro}

Position data: {context}

Decision logic (apply in order):
1. If days_history < 5 OR health_tier = "INSUFFICIENT_DATA": decision=HOLD, confidence=0.9, reasoning must say "insufficient history: N days"
2. If health_tier = "DECAYING" AND momentum_score < 0 AND volume_score < 0 AND composite_slope < 0 AND unrealized_pct < 0: decision=EXIT, confidence=0.9
3. If health_tier = "DECAYING" AND unrealized_pct > 5 AND at least 2 of (momentum_score<0, volume_score<0, composite_slope<0) are true: decision=TRIM, trim_pct=25
4. Otherwise: decision=HOLD

Reasoning must cite actual numbers from the position data. Example: "health=-0.32 DECAYING, momentum=-0.41, volume=-0.28, pnl=-8.8% — thesis deteriorating across all dimensions"
Never use generic phrases. Always reference specific field values.

Respond ONLY with valid JSON, no other text. Use only double quotes. Do not put quotes inside string values:
{"decision": "HOLD or TRIM or EXIT", "confidence": 0.0-1.0, "reasoning": "cite specific field values", "trim_pct": 25 or null}"""


# ── Core HTTP call (blocking, runs in thread) ──────────────────────────────────

def _call_ollama(prompt: str) -> str:
    """Blocking urllib call to Ollama. Returns raw response text."""
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


def _sanitize_llm_json(raw: str) -> str:
    """
    Sanitize common LLM JSON formatting errors before parsing.

    FIX 2026-06-03: LLM (llama3.2) was producing unescaped double quotes
    inside JSON string values, e.g.:
        "veto_reason": "macro_regime="RISK_OFF" AND kelly_fraction > 0.07"
    This is invalid JSON and caused JSONDecodeError, silently dropping the
    agent decision and leaving the symbol absent from the decisions dict.
    entry_passes() defaults to PASS on missing symbol, but the decision was
    not written to entry_vetoes.json, making it invisible for debugging.

    Strategy: extract each string value (between outer quotes) and replace
    any interior double quotes with single quotes. Handles the most common
    LLM output pattern without risking damage to the JSON structure itself.
    """
    def _fix_value(m):
        key = m.group(1)
        val = m.group(2)
        # Replace any interior double quotes with single quotes
        val_clean = val.replace('"', "'")
        return f'"{key}": "{val_clean}"'

    # Match "key": "value" patterns and sanitize interior quotes in value
    sanitized = re.sub(
        r'"([^"]+)":\s*"(.*?)"(?=\s*[,}])',
        _fix_value,
        raw,
        flags=re.DOTALL
    )
    return sanitized


# ── Agent Class ────────────────────────────────────────────────────────────────

class OllamaAgent:
    def __init__(self, name: str, prompt_template: str, output_path: str):
        self.name            = name
        self.prompt_template = prompt_template
        self.output_path     = Path(output_path)
        self._decisions      = []

    def evaluate(self, context: dict) -> dict:
        """Synchronous evaluation of one context dict."""
        macro = _load_macro_summary()
        prompt = self.prompt_template.format(
            context=json.dumps(context, indent=2),
            macro=macro,
        )
        try:
            inner = _call_ollama(prompt)
            logger.info(f"{self.name} | RAW: {repr(inner[:300])}")
            # Extract only the first complete JSON object — LLM sometimes outputs two
            start = inner.find("{")
            if start == -1:
                inner = "{}"
            else:
                depth, end = 0, start
                for i, ch in enumerate(inner[start:], start):
                    if ch == "{": depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                inner = inner[start:end]
            # Sanitize before parsing — LLM may embed unescaped quotes in string values
            inner = _sanitize_llm_json(inner)
            result = json.loads(inner)
            return self._stamp(result, context)
        except json.JSONDecodeError as e:
            logger.warning(f"{self.name} | JSON parse error on {context.get('symbol')}: {e} | raw: {repr(inner[:200])}")
            return self._default(context)
        except Exception as e:
            logger.warning(f"{self.name} | Error on {context.get('symbol')}: {e}")
            return self._default(context)

    def evaluate_batch(self, contexts: list) -> dict:
        """
        Evaluate contexts sequentially — Mistral is single-threaded, parallelism causes timeouts.
        Health-checks Ollama first — if unresponsive, entire batch fast-fails to passthrough
        defaults instead of timing out once per symbol.
        """
        _snapshot_prompts()  # lazy — only runs once per process, no-op on subsequent calls
        if not _ollama_alive():
            logger.warning(f"{self.name} | Ollama unreachable — returning passthrough defaults for all {len(contexts)} symbol(s)")
            results = {}
            for ctx in contexts:
                result = self._default(ctx)
                results[result["symbol"]] = result
            self.flush()
            return results

        results = {}
        for ctx in contexts:
            # Short-circuit HoldAgent when history is too thin to reason meaningfully.
            # Saves ~40s per position — Ollama call skipped, default HOLD written directly.
            if self.name == "HoldAgent":
                days = ctx.get("days_history", 0)
                if days < 5:
                    result = self._stamp({
                        "decision":   "HOLD",
                        "confidence": 0.9,
                        "reasoning":  f"insufficient history: {days} days"
                    }, ctx)
                    results[result["symbol"]] = result
                    continue
            result = self.evaluate(ctx)
            results[result["symbol"]] = result
        self.flush()
        return results

    def _stamp(self, result: dict, context: dict) -> dict:
        result["symbol"]    = context.get("symbol", "UNKNOWN")
        result["timestamp"] = datetime.now().isoformat()
        result["agent"]     = self.name
        self._decisions.append(result)
        return result

    def _default(self, context: dict) -> dict:
        default = "PASS" if self.name == "EntryAgent" else "HOLD"
        return self._stamp({
            "decision":    default,
            "confidence":  0.5,
            "veto_reason": None,
            "reasoning":   "Agent unavailable — passthrough default",
            "flags":       ["agent_error"]
        }, context)

    def flush(self):
        if not self._decisions:
            return
        existing = []
        if self.output_path.exists():
            try:
                existing = json.loads(self.output_path.read_text())
            except Exception:
                existing = []
        self.output_path.write_text(json.dumps(existing + self._decisions, indent=2))
        logger.info(f"{self.name} | Flushed {len(self._decisions)} decisions -> {self.output_path}")
        self._decisions = []


# ── Prompt Version Control ────────────────────────────────────────────────────
# P2-12 fix: was called at module level (_snapshot_prompts() on line 250) which
# ran a filesystem glob on every import of agent_layer — slow I/O on every
# main.py, exit_monitor.py, and hold_monitor.py startup.
# Fixed: called lazily on first agent use, not at import time.

_prompts_snapshotted = False

def _snapshot_prompts():
    """
    Save current prompt text to prompt_versions/ if changed.
    Uses content hash — only writes when prompt has actually changed.
    Called once per process on first agent evaluate_batch() call.
    """
    global _prompts_snapshotted
    if _prompts_snapshotted:
        return
    _prompts_snapshotted = True

    import hashlib
    from pathlib import Path as _P
    vdir = _P("prompt_versions")
    vdir.mkdir(exist_ok=True)

    for name, text in [("entry_prompt", ENTRY_PROMPT), ("hold_prompt", HOLD_PROMPT)]:
        h = hashlib.md5(text.encode()).hexdigest()[:8]
        existing = list(vdir.glob(f"{name}_*.txt"))
        already_saved = any(h in f.name for f in existing)
        if not already_saved:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = vdir / f"{name}_{ts}_{h}.txt"
            fname.write_text(text)
            logger.info("Prompt snapshot saved: %s", fname.name)


# ── Agent Instances ────────────────────────────────────────────────────────────

entry_agent = OllamaAgent("EntryAgent", ENTRY_PROMPT, "entry_vetoes.json")
hold_agent  = OllamaAgent("HoldAgent",  HOLD_PROMPT,  "hold_decisions.json")


# ── Public API ─────────────────────────────────────────────────────────────────

# ── Deterministic Entry Rules (2026-06-10) ────────────────────────────────────
# All six entry veto rules are exact boolean predicates over the candidate's
# literal field values. Delegating their evaluation to an LLM violated the
# system's first principle (math and logic only): despite two prompt-tightening
# passes, llama3.2 was still emitting fabricated vetoes — e.g. 2026-06-01..03 it
# vetoed candidates with reason macro_regime="RISK_OFF" while the logged macro
# regime was RISK_ON, and those vetoes were BINDING (signals removed in main.py).
#
# Design: math governs, agent advises.
#   - The rules below are evaluated exactly in Python.
#   - The LLM is still called and its decision logged to entry_vetoes.json for
#     calibration (agent-vs-math disagreement rate), but it cannot veto a
#     candidate the math passes, and it cannot pass a candidate the math vetoes.

def _eval_entry_rules(c: dict):
    """Evaluate the six deterministic veto rules. Returns (vetoed, rule, reason)."""
    regime       = c.get("regime", "")
    comp         = float(c.get("composite_score", 0.0))
    kelly        = float(c.get("kelly_fraction", 0.0))
    atr_pct      = float(c.get("atr_pct", 0.0))
    dse          = int(c.get("days_since_earnings", 999))
    vix_regime   = c.get("vix_regime", "NORMAL")
    mms          = float(c.get("market_momentum_scalar", 1.0))
    macro_regime = c.get("macro_regime", "NEUTRAL")

    if regime == "MIXED" and comp < 1.0:
        return True, 1, f'regime="MIXED" AND composite_score {comp:.4f} < 1.0'
    if kelly > 0.10 and atr_pct > 3.5:
        return True, 2, f"kelly_fraction {kelly:.4f} > 0.10 AND atr_pct {atr_pct:.2f} > 3.5"
    if dse < 5:
        return True, 3, f"days_since_earnings {dse} < 5"
    if vix_regime == "SPIKE" and mms < 0.6:
        return True, 4, f'vix_regime="SPIKE" AND market_momentum_scalar {mms:.2f} < 0.6'
    if macro_regime == "CRISIS":
        return True, 5, 'macro_regime="CRISIS"'
    if macro_regime == "RISK_OFF" and kelly > 0.07:
        return True, 6, f'macro_regime="RISK_OFF" AND kelly_fraction {kelly:.4f} > 0.07'
    return False, None, None


def run_entry_screening(candidates: list) -> dict:
    """Screen ranked entry candidates. Returns {symbol: decision_dict}.

    Deterministic rules are authoritative. LLM output is advisory and is
    reconciled against the math; any disagreement is logged and overridden.
    """
    llm_decisions = {}
    try:
        llm_decisions = entry_agent.evaluate_batch(candidates)
    except Exception as e:
        logger.warning("EntryAgent batch failed (%s) — deterministic rules only.", e)

    final = {}
    for c in candidates:
        sym = c.get("symbol")
        vetoed, rule, reason = _eval_entry_rules(c)
        llm = llm_decisions.get(sym, {})
        llm_dec = llm.get("decision", "PASS")

        math_dec = "VETO" if vetoed else "PASS"
        if llm_dec != math_dec:
            logger.warning(
                "AGENT_OVERRIDE %s: math=%s (rule %s: %s) vs agent=%s (%s) — math governs",
                sym, math_dec, rule, reason, llm_dec, llm.get("veto_reason"))

        final[sym] = {
            "symbol":           sym,
            "decision":         math_dec,
            "confidence":       1.0,
            "veto_reason":      f"rule {rule}: {reason}" if vetoed else None,
            "decision_source":  "deterministic",
            "agent_decision":   llm_dec,
            "agent_confidence": llm.get("confidence"),
            "agent_veto_reason": llm.get("veto_reason"),
            "agent_math_disagree": llm_dec != math_dec,
            "flags":            llm.get("flags", []),
            "timestamp":        datetime.now().isoformat(),
        }

    # Persist reconciled decisions so entry_vetoes.json reflects what actually
    # governed execution (with the raw agent view kept for calibration).
    try:
        existing = []
        p = Path("entry_vetoes.json")
        if p.exists():
            try:
                existing = json.loads(p.read_text())
            except Exception:
                existing = []
        existing.extend(final.values())
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2))
        os.replace(tmp, p)
    except Exception as e:
        logger.warning("entry_vetoes.json write failed: %s", e)

    return final


def run_hold_screening(positions: list) -> dict:
    """Evaluate open positions. Returns {symbol: decision_dict}."""
    return hold_agent.evaluate_batch(positions)


def entry_passes(symbol: str, decisions: dict) -> bool:
    """Returns True (proceed) or False (vetoed)."""
    return decisions.get(symbol, {}).get("decision", "PASS") != "VETO"


def hold_recommendation(symbol: str, decisions: dict) -> str:
    """Returns HOLD / TRIM / EXIT."""
    return decisions.get(symbol, {}).get("decision", "HOLD")


# ── Smoke Test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")

    test_entry = [{
        "symbol": "NVDA", "composite_score": 1.84, "score_rank": 2,
        "regime": "TRENDING", "kelly_fraction": 0.09,
        "market_momentum_scalar": 0.85, "atr_pct": 2.1,
        "days_since_earnings": 14, "vix_regime": "NORMAL",
        "macro_regime": "RISK_ON",
        "cluster_scores": {"MR": 0.42, "TREND": 1.21, "VOL": 0.88, "VOLAT": 0.31, "REV": 0.61}
    }]

    test_hold = [{
        "symbol": "NVDA", "hold_days": 4, "unrealized_pct": 3.2,
        "health_score": 6.5, "active_exit_path": "trail_profit",
        "health_layers": {
            "momentum": 0.8, "vol_regime": 0.6, "factor_coherence": 0.9,
            "exit_proximity": 0.4, "market_alignment": 0.7,
            "cluster_drift": 0.5, "regime_stability": 0.8, "drawdown_risk": 0.6
        }
    }]

    print("\n── Entry Agent Test ──")
    entry_results = run_entry_screening(test_entry)
    for sym, d in entry_results.items():
        print(f"  RAW: {json.dumps(d, indent=2)}")
        print(f"  {sym}: {d.get('decision','?')} | confidence={d.get('confidence','?')} | veto={d.get('veto_reason')}")

    print("\n── Hold Agent Test ──")
    hold_results = run_hold_screening(test_hold)
    for sym, d in hold_results.items():
        print(f"  RAW: {json.dumps(d, indent=2)}")
        print(f"  {sym}: {d.get('decision','?')} | confidence={d.get('confidence','?')} | reason={d.get('reasoning')}")

    print("\nDONE")
