"""
agent_layer.py — Ollama-based Entry and Hold reasoning agents
Advisory only. Core signal math and exit logic are never touched.
Uses urllib (stdlib only) — no aiohttp dependency, no proxy issues.
Agents run in a thread pool — failure always defaults to PASS/HOLD.
"""

import json
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

ENTRY_PROMPT = """Quantitative risk filter. Signal math already approved this entry. VETO only on structural risk.

Macro: {macro}

Candidate: {context}

VETO if ANY of these are true (otherwise PASS):
1. regime="MIXED" AND composite_score < 1.0
2. kelly_fraction > 0.10 AND atr_pct > 3.5
3. days_since_earnings < 5
4. vix_regime="SPIKE" AND market_momentum_scalar < 0.6
5. macro_regime="CRISIS"
6. macro_regime="RISK_OFF" AND kelly_fraction > 0.07

PASS if macro_regime is RISK_ON, NEUTRAL, or BULLISH — these are NOT veto conditions.
Default is PASS. Only VETO when a numbered rule above is explicitly met.

Respond ONLY with this JSON, no other text:
{{"decision": "PASS or VETO", "confidence": 0.0-1.0, "veto_reason": "rule number and reason or null", "flags": []}}"""


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

Respond ONLY with this JSON, no other text:
{{"decision": "HOLD or TRIM or EXIT", "confidence": 0.0-1.0, "reasoning": "cite specific field values", "trim_pct": 25 or null}}"""


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
            # Extract only the first complete JSON object — Mistral sometimes outputs two
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
            result = json.loads(inner)
            return self._stamp(result, context)
        except json.JSONDecodeError as e:
            logger.warning(f"{self.name} | JSON parse error on {context.get('symbol')}: {e}")
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

def run_entry_screening(candidates: list) -> dict:
    """Screen ranked entry candidates. Returns {symbol: decision_dict}."""
    return entry_agent.evaluate_batch(candidates)


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
