# Raptor Trading System — Development Skill
*Last updated: 2026-06-19 | Version: 8.0*

---

## System Overview

Raptor v5.4 is a quantitative swing trading system on Alpaca paper (~$107K equity).
US equities only, 0–10 positions, typical hold 5–20 trading days.
Math governs every decision. LLM is advisory only.
**Installed at:** `C:\Raptor` (moved from OneDrive 2026-06-19 — zero OneDrive paths remain)

---

## Architecture

```
data_feeds.py        AlpacaDataFeed — submit_order, get_positions, get_account, get_daily_bars
signals.py           Dual-book engine — sector neutralization, OU hold target, SNR ranking
config.py            All parameters
main.py              Entry scanner — 9:35 AM, deterministic gate + velocity + cooldown
exit_monitor.py      Position manager — 5 exit paths + math trim
hold_monitor.py      8-layer health scoring
outcome_tracker.py   Trade labeling — --backfill, --relabel flags
slippage_tracker.py  Implementation shortfall recording (Perold 1988)
dsr.py               Deflated Sharpe Ratio (Bailey & López de Prado 2014)
kelly_engine.py      Bootstrap Kelly (shadow until DATA-100)
factor_lab.py        Spearman IC validation per factor
macro_context.py     Vote-count regime classifier → continuous macro_score [-1,1]
backtest.py          Walk-forward backtester (survivorship bias warning in output)
universe_builder.py  Screens 6800 assets → ~75–150 symbols (excl. leveraged ETPs)
agent_layer.py       _eval_entry_rules() deterministic gate + LLM advisory
position_outcomes.json  27 independent positions — authoritative gate counter (post-repair 2026-06-19)
```

Dead files removed 2026-05-29: raptor_state.json, diagnose.py, diagnose_regime.py, Start_Raptor_Recap.bat

---

## Critical Infrastructure — Verify Every Session

**1. AlpacaDataFeed.submit_order**
Missing its `def` line from ~2026-05-25 to 2026-06-05 — 11 days of silent execution failure.
```bash
python3 -c "
import ast
src = open('data_feeds.py').read()
tree = ast.parse(src)
for n in ast.walk(tree):
    if isinstance(n, ast.ClassDef) and n.name == 'AlpacaDataFeed':
        assert 'submit_order' in [m.name for m in n.body if isinstance(m, ast.FunctionDef)]
        print('OK: submit_order present')
"
```
After any edit to data_feeds.py: re-run. Non-negotiable.

**2. Deterministic entry gate**
All six entry veto rules are exact boolean predicates in Python.
LLM (Ollama) is advisory — it cannot bind a veto or pass any candidate.
Disagreements logged as `AGENT_OVERRIDE` in entry_vetoes.json.
```bash
python3 -c "
from agent_layer import _eval_entry_rules
r = _eval_entry_rules({'regime':'TRENDING','composite_score':1.5,'kelly_fraction':0.05,
    'atr_pct':2.0,'days_since_earnings':30,'vix_regime':'NORMAL',
    'market_momentum_scalar':1.0,'macro_regime':'RISK_ON'})
assert r == (False, None, None)
print('OK: deterministic entry rules')
"
```

**3. Data independence rule**
`outcome_log.json` contains multiple trim events per position. These are NOT independent.
All IC, DSR, and gate calculations MUST use `position_outcomes.json`.
position_outcomes.json: 27 records = 27 independent position entries.
Data integrity reminder: PLTD had 9 trim events from 1 entry. AMD had 4. 49/76 were non-independent.

---

## Factor System

Factors registered in two places in signals.py. To add:
1. Implement static method in `class Factors` — takes DataFrame, returns float
2. Register in `FACTOR_NAMES` list
3. Assign cluster in `FACTOR_CLUSTERS` — one of: `mr, trend, vol, volat, rev`
4. Gate for production: IC > 0.03, |t| > 1.0, ICIR > 0.3 over 60-day rolling window

To remove: delete from FACTOR_NAMES and FACTOR_CLUSTERS. Downstream auto-adjusts.

**Current factors (16):**

| Cluster | Factors |
|---------|---------|
| mr | rsi_mr, bollinger_z, crowd_panic, ma_distance, hurst |
| trend | ma_stack, macd_accel, adx_dir, price_cloud |
| vol | vol_ratio, obv_r2, accum_dist |
| volat | atr_pctile, bb_squeeze, rel_strength |
| rev | rev_momentum |

**Factor status:**
- `vol_ratio`: IC = -0.11 — WATCH. Remove if IC < 0.03 and |t| < 1.0 for 3+ consecutive weeks.
- `hurst`: DFA-1 (Kantelhardt et al. 2002). Requires ≥ 60 bars.
- `sentiment_score`: always 0.0 — dead path. Remove or fix (P1-15).

**Signal lifecycle:**
PROPOSED → SHADOW (IC provisional) → LIVE (IC > 0.03, |t| > 1.0, ICIR > 0.3 over 30 trades) → DEPRECATED

---

## Execution Chain

```
hold_monitor.py
  compute_health_score() → 8 layers → tier + health score
  compute_trim()         → trim% → trim_shares, action
  save_health()          → hold_health.json

exit_monitor.py
  EXIT 1–5 checks        → hard stop, trail, thesis, lev cap, time decay
  EXIT 4 portfolio heat  → 25% trim if portfolio_dd < -12%
  Math trim block        → reads hold_health.json → exits list
  EXECUTE loop           → dm.alpaca.submit_order() per exit
    → slippage_tracker.record_fill() [IS logging, Perold 1988]
    → outcome_pending.json (keyed by order ID)
    → ledger.record_trim() or ledger.record_exit()
    → trim_log.json

outcome_tracker.py
  Reads outcome_pending.json → joins with buy order → outcome_log.json
  Backfills pending slippage fills via slippage_tracker.backfill_slippage()
```

**Known failure modes (historical):**
- submit_order missing def line → AttributeError → execute loop aborts silently (happened Jun 1-4, fixed 2026-06-05)
- hold_monitor crash → stale hold_health.json → exit_monitor uses old health scores
- Ledger stop above current price → EXIT 1 fires every run → position never trims
- **OneDrive sync conflict → ledger silently reverted to cloud version after rapid writes** (caused 3 corruptions May-Jun; FIXED 2026-06-19 by moving to C:\Raptor outside OneDrive scope)

---

## Trim and Exit: What each path means

| Path | Meaning | IC-valid? |
|------|---------|-----------|
| `math_trim` | hold_monitor partial trim — position still open | NO (partial, not terminal) |
| `trailing_stop` | Trail ratchet hit — full exit | YES |
| `hard_stop` | Fixed stop hit — full exit | YES |
| `math_exit` | Thesis fully decayed — full exit | YES |
| `pre_label` | Historical, no factor scores | NO |
| `crypto` | Crypto system — separate | NO |

**position_outcomes.json logic:** a position's `final_exit_path` is the LAST event.
A position with 4 math_trims followed by a trailing_stop → `final_exit_path = "trailing_stop"`.

---

## Immutable Rules

1. **Math-first.** Every constant needs derivation or `# TODO:DERIVE`. Round numbers without derivation are bugs.
2. **No fabricated fallbacks.** Missing data → skip with warning. Never substitute invented values.
3. **comp=0.0 for unscored held positions.** Never -1.0.
4. **Momentum clustering is intentional alpha.** Do not add correlation gates. CRIT-9 cancelled permanently.
5. **Kelly is SHADOW until DATA-100.** Do not override sizing.
6. **Syntax check every file before committing.**
7. **After every edit to data_feeds.py: AST-verify submit_order.**
8. **Update ONTOLOGY same session as any architecture change.**
9. **Never change code in intraday window 9:35–3:50 PM ET without explicit intent.**
10. **Clear __pycache__ before every test.**
11. **Real data or skip.** When a position cannot be evaluated → log warning, skip. Never substitute.
12. **Logs are tracked in git.** logs/ not gitignored. Every push includes today's logs.
13. **All gate counts use position_outcomes.json.** Never raw outcome_log.json.
14. **position_outcomes.json must be rebuilt after every new position closes.** (TODO: automate in AfterClose)
15. **Backtest Sharpe figures are upper bounds** until ARCH-5 point-in-time universe is implemented.
16. **Raptor lives at `C:\Raptor`.** Never reference or suggest the old OneDrive path. Any file, script, or task pointing to `C:\Users\steve\OneDrive\Desktop\Raptor` is wrong and must be patched immediately.

---

## What Must Never Be Violated

- Do NOT add factors without IC validation gate
- Do NOT propose HMM at current data scale (wait for ARCH-2 gate — exception: macro_context.py replacement is approved at any time as it requires no trade data)
- Do NOT propose online RandomForest retraining
- Do NOT redesign architecture — iterate on what works
- Do NOT use CMD syntax — use PowerShell
- Do NOT assume order execution worked without checking outcome_pending.json for the order ID
- Do NOT compute DSR, win rate, or IC from raw outcome_log.json — use position_outcomes.json

---

## Performance Reference (backtest, survivorship-biased upper bounds)

| Universe | Return | CAGR | Sortino | PF |
|----------|--------|------|---------|-----|
| 120-symbol | 1008% | 56% | 4.64 | 1.73 |
| 47-symbol | 201% | 22.6% | 2.17 | 1.43 |

**Live performance (2026-06-19, post-repair):**
- Open positions: 7 (KRE, WFC, MRVL, BAC, WULF, UBER, AAL) — Alpaca/ledger sync confirmed
- Independent positions: 27 (position_outcomes.json)
- Closed trades in ledger: 40
- Win rate: 59.1% (13W/9L, clean positions)
- Mean position PnL: 5.47%
- DSR: 59.8% WEAK (n=24, SR=1.42 vs SR*=1.22)
- Kelly: SHADOW mode, n_trades=53/100, f_recommended=1%
- Equity: $106,915.78
- Note: DSR rises toward STRONG as n grows if alpha is genuine. Current sample too small to distinguish from luck.

---

## Steve's Preferences

- Math-first, PhD-level rigor, research-backed decisions
- Direct critical feedback — no softening
- PowerShell commands on Windows
- Explicit step-by-step instructions
- Challenge any assumption that looks like a default or round number
- **Claude fixes bugs directly.** No manual edits, no diffs. Claude writes the fixed file, validates syntax, delivers via present_files. Steve downloads and pushes.
- **Claude Project must mirror GitHub.** Run sync_to_claude.py after every push and upload all listed files.
- **Claude prompts sync after any session where files changed.** Show only: `python sync_to_claude.py` — nothing else.
