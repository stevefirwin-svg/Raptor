from agent_layer import _call_ollama, _sanitize_llm_json, ENTRY_PROMPT, _load_macro_summary
import json

candidate = {
    "symbol": "TEST",
    "regime": "TRENDING",
    "composite_score": 1.4,
    "kelly_fraction": 0.06,
    "atr_pct": 1.8,
    "days_since_earnings": 45,
    "vix_regime": "NORMAL",
    "market_momentum_scalar": 1.0,
    "macro_regime": "NEUTRAL"
}

macro = _load_macro_summary()
prompt = ENTRY_PROMPT.replace("{macro}", macro).replace("{context}", json.dumps(candidate, indent=2))
raw = _call_ollama(prompt)
print('RAW:', repr(raw))
clean = _sanitize_llm_json(raw)
print('CLEAN:', repr(clean))
try:
    parsed = json.loads(clean)
    print('PARSED:', parsed)
    print('confidence:', parsed.get('confidence'))
except Exception as e:
    print('PARSE FAILED:', e)
