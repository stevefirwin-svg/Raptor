# RAPTOR_STARTUP.md — Session Startup Protocol
*Read first. Every session. No exceptions.*
*Last updated: 2026-06-12 | Version: 7.0*

---

## STEP 1 — PULL FRESH FROM GITHUB

Claude (container):
```bash
git clone https://github.com/stevefirwin-svg/Raptor /home/claude/raptor
cd /home/claude/raptor
git log --oneline -5
```

Steve's machine before any session:
```powershell
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" pull origin main
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" log --oneline -3
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

**Paste the top commit hash to Claude at session start. Claude verifies it matches before touching any code.**

---

## STEP 2 — READ FILES IN ORDER

1. **RAPTOR_STARTUP.md** — this file
2. **RAPTOR_MASTER_PLAN.md** — system state, open items, gates, build order
3. **RAPTOR_SKILL.md** — immutable rules, factor lifecycle
4. **RAPTOR_ONTOLOGY.md** — full system logic and math

---

## STEP 3 — LOG ANALYSIS (mandatory before any code work)

**Logs are ground truth. Read before reading code. Every session.**

```python
import os, re
log_dir = "logs"
exit_logs = sorted([f for f in os.listdir(log_dir)
                    if f.startswith("exits_") and f.endswith(".log")])[-10:]
print(f"{'DATE':<12} {'SELL':>5} {'OK':>5} {'FAIL':>7} {'INSUF':>6} REASONS")
print("-"*75)
for lf in exit_logs:
    content = open(os.path.join(log_dir, lf), errors="replace").read()
    date = lf.replace("exits_","").replace(".log","")
    date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    sell  = len(re.findall(r"INFO: SELL ", content))
    ok    = len(re.findall(r"INFO:   OK:", content))
    fail  = len(re.findall(r"ERROR:   FAILED:", content))
    insuf = len(re.findall(r"insufficient qty", content))
    reasons = ", ".join(sorted(set(re.findall(r"SELL \S+ \S+ \[(\S+)\]", content))))[:35]
    flag = " *** SILENT FAIL" if sell > 0 and ok == 0 and fail == 0 else ""
    print(f"{date_fmt:<12} {sell:>5} {ok:>5} {fail:>7} {insuf:>6}  {reasons}{flag}")
```

**Red flags — investigate before anything else:**
- `SELL > 0` and `OK == 0` and `FAIL == 0` → silent execution failure (crash between SELL log and submit_order)
- `INSUF > 0` → double-trim firing, stale ledger snapshot
- `SELL == 0` multiple days while positions held → exit_monitor not running
- `FATAL: uncaught exception` in any log → read the full traceback immediately below
- Log ends mid-run with NO FATAL line → external kill (Task Scheduler timeout, reboot, OneDrive lock)
- `AGENT_OVERRIDE` lines → LLM disagreed with deterministic entry rules; math governed (expected occasionally)

---

## STEP 4 — INFRASTRUCTURE INTEGRITY CHECKS

**Run after log analysis. Not optional.**

```powershell
# Save as _startup_check.py and run: python _startup_check.py

import ast, glob, sys

# 1. submit_order AST-verified on AlpacaDataFeed
src = open('data_feeds.py').read()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == 'AlpacaDataFeed':
        methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
        assert 'submit_order' in methods, 'CRITICAL: submit_order MISSING'
        print('OK: submit_order on AlpacaDataFeed (AST-verified)')

# 2. Syntax check all critical files
for f in ['main.py','signals.py','exit_monitor.py','hold_monitor.py',
          'data_feeds.py','outcome_tracker.py','slippage_tracker.py','dsr.py']:
    ast.parse(open(f).read())
    print(f'OK syntax: {f}')

# 3. Deterministic entry gate
from agent_layer import _eval_entry_rules
r = _eval_entry_rules({'regime':'TRENDING','composite_score':1.5,'kelly_fraction':0.05,
    'atr_pct':2.0,'days_since_earnings':30,'vix_regime':'NORMAL',
    'market_momentum_scalar':1.0,'macro_regime':'RISK_ON'})
assert r == (False, None, None), f'Gate failed: {r}'
r = _eval_entry_rules({'regime':'TRENDING','composite_score':1.5,'kelly_fraction':0.08,
    'atr_pct':2.0,'days_since_earnings':30,'vix_regime':'NORMAL',
    'market_momentum_scalar':1.0,'macro_regime':'RISK_OFF'})
assert r[0] and r[1] == 6, f'Rule 6 failed: {r}'
print('OK: deterministic entry rules (6/6 rules)')

# 4. No hardcoded credentials
hits = [f for f in glob.glob('*.py')
        if 'EMAIL_PASSWORD = "' in open(f,errors='ignore').read()
        and 'getenv' not in open(f,errors='ignore').read().split('EMAIL_PASSWORD')[1][:50]]
assert not hits, f'CRITICAL: hardcoded password in {hits}'
print('OK: no hardcoded credentials')

# 5. Crash handlers present
for f in ['main.py','exit_monitor.py','hold_monitor.py']:
    assert 'logger.exception' in open(f).read() or 'logging.getLogger' in open(f).read()
    print(f'OK crash handler: {f}')

# 6. position_outcomes.json exists and has correct schema
import json
pos = json.load(open('position_outcomes.json'))
assert all('position_pnl_pct' in p for p in pos), 'position_outcomes schema broken'
assert all('flags' in p for p in pos), 'position_outcomes schema broken'
clean = [p for p in pos if 'leveraged_or_inverse_etp' not in p.get('flags',[])]
print(f'OK: position_outcomes.json — {len(pos)} positions, {len(clean)} clean')

print('\nALL STARTUP CHECKS PASSED')
```

**If submit_order check fails: STOP. Do not run exit_monitor. Fix data_feeds.py first.**

---

## STEP 5 — DATA STATE CHECK

```python
import json
from collections import Counter

# Position-level outcomes (the authoritative gate counter)
pos = json.load(open('position_outcomes.json'))
clean = [p for p in pos if 'leveraged_or_inverse_etp' not in p.get('flags',[])]
print(f"Independent positions: {len(pos)}")
print(f"Clean (gate counter):  {len(clean)}")
print(f"DATA-40 gate: {len(clean)}/40  {'OPEN' if len(clean) < 40 else 'MET'}")
print(f"DATA-60 gate: {len(clean)}/60  {'OPEN' if len(clean) < 60 else 'MET'}")

# DSR
from dsr import compute_dsr, print_report
print_report(compute_dsr(n_trials=10))

# Kelly
k = json.load(open('kelly_estimates.json'))
print(f"Kelly mode: {k.get('mode','?')}  trades: {k.get('n_trades','?')}/100")
print(f"  f_recommended: {k.get('f_recommended','?')}")

# Macro
m = json.load(open('macro_context.json'))
print(f"Macro regime: {m.get('regime','?')}  score: {m.get('macro_score','?')}")
```

---

## STEP 6 — CONTEXT CHECK

1. What positions are held? Run `python check_account.py` or paste portfolio.
2. Is hold_health.json timestamped today? If not → hold_monitor did not run this morning.
3. Any open SQQQ/leveraged ETP positions? Exit manually if still held (pre-filter era).
4. Check `slippage_log.json` — any pending fills (fill_price=None) from yesterday?
5. Today's build target: see MASTER_PLAN open priority queue.

---

## DAILY SCHEDULE

| Time ET | Script | What it does |
|---------|--------|--------------|
| 9:00 AM | macro_context.py | FRED + SPY regime → macro_context.json |
| 9:15 AM | market_agent.py | SCAN / REDUCE / STANDBY decision |
| 9:28 AM | hold_monitor.py --pre | Pre-entry health check |
| 9:35 AM | main.py | Signal engine + deterministic entry gate + BUY orders |
| 9:35–3:50 | exit_monitor.py loop | Exits + trims every 30 min |
| 3:50 PM | exit_monitor.py + hold_monitor.py + daily_recap.py | Final cycle + recap |
| 5:00 PM | outcome_tracker.py | Tag new closed trades → outcome_log.json |
| 5:00 PM | factor_lab.py + kelly_engine.py | IC + Kelly update |
| 5:00 PM | dsr.py | Deflated Sharpe (position-level) |
| End of day | Daily_GitHub_Push.bat | git add -A + push |

**Note:** position_outcomes.json is NOT auto-updated yet. Rebuild manually when needed:
see Start_AfterClose.bat TODO below.

---

## SYSTEM STATE (2026-06-12)

### Live and working

| Component | Status |
|-----------|--------|
| Dual-book engine (MOMENTUM live, MR suspended) | ✅ LIVE |
| submit_order on AlpacaDataFeed | ✅ FIXED 2026-06-05 |
| Crash-visibility handlers (3 entry points) | ✅ LIVE 2026-06-10 |
| Deterministic entry gate (_eval_entry_rules) | ✅ LIVE 2026-06-10 |
| Cross-sectional sector neutralization | ✅ LIVE 2026-06-10 |
| Implementation shortfall tracker | ✅ LIVE 2026-06-10 |
| Deflated Sharpe Ratio (position-level) | ✅ LIVE 2026-06-12 |
| OU hold target (ln(2)/θ) | ✅ LIVE 2026-06-11 |
| exit_regime in outcome records | ✅ LIVE 2026-06-11 |
| Survivorship bias warning in backtest | ✅ LIVE 2026-06-11 |
| position_outcomes.json | ✅ LIVE 2026-06-12 |
| Leveraged/inverse ETP exclusion | ✅ LIVE 2026-06-10 |
| Gmail credentials (env var) | ✅ FIXED 2026-06-10 |
| Bat log isolation | ✅ FIXED 2026-06-10 |
| Bootstrap Kelly SHADOW | ✅ LIVE |
| Spearman IC + WLS + decay | ✅ LIVE |
| DFA-1 Hurst | ✅ LIVE 2026-05-29 |
| P0-1 outcome sidecar | ✅ LIVE 2026-05-29 |
| P0-8 regime override | ✅ LIVE 2026-05-29 |

### NOT live (claimed but not in code)

| Claim | Reality |
|-------|---------|
| P1-1 Kalman macro | macro_context.py is vote-count; replaced by Gaussian blend in signals.py |
| P1-9 Watchdog intraday | fetches 5 daily bars, not intraday — misleading |

### Open Steve actions (cannot be done by Claude)

| # | Action | Priority |
|---|--------|----------|
| 1 | Task Scheduler: confirm "Stop task if runs longer than" is OFF for entry task (Jun-08 kill) | HIGH |
| 2 | `git rm raptor_s5b_positions.zip raptor_session4_fixes.zip` if present in repo | MEDIUM |
| 3 | Add `python outcome_tracker.py` + `python -c "from rebuild_positions import rebuild"` to Start_AfterClose.bat once rebuild_positions.py is built | FUTURE |

---

## KEY FILES

```
RAPTOR_STARTUP.md          This file
RAPTOR_MASTER_PLAN.md      Priority queue, gates, verified state
RAPTOR_SKILL.md            Immutable rules, factor lifecycle
RAPTOR_ONTOLOGY.md         Full system logic — no code

position_outcomes.json     AUTHORITATIVE: 27 independent positions (use for all gating)
outcome_log.json           Raw trim events (135 records, 27 unique positions)
slippage_log.json          Implementation shortfall per fill (Perold 1988)
kelly_estimates.json       Bootstrap Kelly output
factor_ic_report.json      IC validation
hold_health.json           Position health scores
macro_context.json         Current regime
composite_cache.json       Today's composites (velocity gate)
cooldown_log.json          Active re-entry blocks
entry_vetoes.json          Deterministic gate decisions (decision_source="deterministic")

data_feeds.py              AlpacaDataFeed.submit_order — verify every session
signals.py                 Signal engine + sector neutralization + OU hold target
main.py                    Entry scanner
exit_monitor.py            Exits + trims
hold_monitor.py            8-layer health scoring
outcome_tracker.py         Trade labeling
slippage_tracker.py        IS recording + backfill + report
dsr.py                     Deflated Sharpe Ratio (Bailey & López de Prado 2014)
agent_layer.py             _eval_entry_rules() — deterministic gate
universe_builder.py        Dynamic universe (excl. leveraged ETPs)
```

---

## END OF SESSION CHECKLIST

```bash
# Claude side
for f in main.py signals.py exit_monitor.py hold_monitor.py data_feeds.py \
          outcome_tracker.py slippage_tracker.py dsr.py agent_layer.py; do
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK $f')"
done

python3 -c "
import ast
src = open('data_feeds.py').read()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == 'AlpacaDataFeed':
        assert 'submit_order' in [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
        print('OK: submit_order present')
"

git add -A
git commit -m 'Description of change and why (2026-MM-DD)'
git log --oneline -3
```

```powershell
# Steve side
cd "C:\Users\steve\OneDrive\Desktop\Raptor"
git pull origin main
git log --oneline -4
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

---

## RULES (immutable)

1. Clone fresh from GitHub first. Never work from project knowledge alone.
2. Read all four MD files before any technical work.
3. Run Step 3 (log analysis) before any code work.
4. Run Step 4 (integrity checks) before any code work.
5. Real data or skip. Never invent a default.
6. comp=0.0 for unscored positions. Never -1.0.
7. Momentum clustering is alpha. No correlation gates.
8. Kelly is SHADOW until DATA-100.
9. Syntax check every file before committing.
10. Commit message must describe what changed and why.
11. Update ONTOLOGY same session as architecture changes.
12. Run outcome_tracker.py after any session touching trade outcomes.
13. Never change code intraday (9:35–3:50 ET) without explicit intent.
14. No factor is permanent. IC > 0.03, |t| > 1.0, ICIR > 0.3 over 60-day rolling window to stay.
15. Every constant needs derivation or `# TODO:DERIVE` comment with method noted.
16. When evaluation is impossible → log warning, skip. Never substitute.
17. After any edit to data_feeds.py: re-run AST submit_order check. Non-negotiable.
18. All gate calculations use position_outcomes.json (independent positions), never raw outcome_log.json.
