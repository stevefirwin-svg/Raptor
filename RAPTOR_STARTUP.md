# RAPTOR_STARTUP.md — Session Startup Skill
*Every Claude session working on Raptor must follow this file.*
*Last updated: 2026-05-24 | Version: 5.5*

---

## PURPOSE

This file is the mandatory entry point for every Claude session on Raptor.
It defines what to pull from GitHub, what to read, what to check, and what
the current system state is before touching any code.

The loop that killed productivity: Sessions kept patching code without knowing
what was already built, what was claimed vs actually in the code, or what
the live data was showing. This file breaks that loop.

---

## STEP 1 — PULL FRESH FROM GITHUB (mandatory, do not skip)

Claude must clone fresh at the start of every session.
Project knowledge files are snapshots. GitHub is the source of truth.

```bash
git clone https://github.com/stevefirwin-svg/Raptor /home/claude/raptor
cd /home/claude/raptor
git log --oneline -5
```

On Steve's machine before starting any session:
```powershell
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" pull origin main
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" log --oneline -3
Remove-Item -Recurse -Force __pycache__
```

---

## STEP 2 — READ THESE FILES IN THIS ORDER

Read all four. Do not skip any. Each answers a different question.

### 1. RAPTOR_STARTUP.md (this file)
Answers: What is the startup sequence? What is the system state?

### 2. RAPTOR_MASTER_PLAN.md
Answers: What is done, what is open, what is the build order?
- Check CRIT/MATH/ARCH status table
- Check P1 verified status table (code-verified, not claimed)
- Check live data snapshot for latest Kelly, IC, trade count
- Check Action required items from last session

### 3. RAPTOR_SKILL.md
Answers: What are the rules, what must never be violated?
- Section 9 CRITICAL RULES
- Section 10 IC VALIDATION — current factor validity
- Section 13 MATH GAPS — what is open and what is done
- Section 14 SESSION PROTOCOLS

### 4. RAPTOR_ONTOLOGY.md
Answers: How does the system actually work right now?
- Section 4 Signal Engine — dual-book, factor gates
- Section 5 Entry Filters — velocity gate, cooldown gate
- Section 6 Position Sizing — Kelly implementation status
- Section 8 Hold Monitor — 10 layers, trim logic
- Section 9 Exit Monitor — exit ruleset by trade type
- Section 11 Adaptive Weights — Spearman IC, WLS, per-book files

---

## STEP 3 — RUN THE HEALTH CHECK

Run these before writing any code. Paste output to Claude when starting a session.

```powershell
python outcome_tracker.py --summary
python kelly_engine.py
python reconcile_positions.py
python factor_lab.py
```

### What to look for

outcome_tracker.py --summary:
- exit_unknown should decrease over time. If high, run outcome_tracker.py to tag new trades.
- math_trim win% should be above 60%. This is the core exit mechanism.
- trailing_stop win% should be above 40%. If below, trail fires too late.

kelly_engine.py:
- mode = SHADOW until 100 trades. Sizing unchanged. This is correct.
- f_recommended = bootstrap P25 of final-f. Should be 3-5%.
- If mode = ACTIVE_ELIGIBLE, requires explicit config flag to enable.

reconcile_positions.py:
- Any symbol in Alpaca but NOT in ledger: run python backfill_ledger.py --write
- CVE, KDP, PLTD were missing as of 2026-05-24. Verify fixed.

factor_lab.py:
- ma_stack and adx_dir should remain significant (IC above 0.05, t above 1.5).
- vol_ratio IC is marginal (-0.11). Watch, candidate for removal.
- Condition number above 200 means high collinearity. Orthogonalization is critical.
- MR IC numbers are only meaningful once MR trades exist in outcome_log.

---

## STEP 4 — VERIFY CRITICAL INVARIANTS IN CODE

Spot-check before writing any code. These must never be violated.

```bash
# 1. _last_full_signals stores ALL scored symbols (not just gate-passers)
grep -n "hold_monitor_proxy" signals.py | head -3

# 2. Default composite is 0.0 not -1.0
grep -n "0\.0.*Not scored" exit_monitor.py | head -3

# 3. No fabricated fallbacks
grep -n "price \* 0\.02\|entry_price \* 0\.92\|days_held = 7" exit_monitor.py hold_monitor.py

# 4. Atomic writes present
grep -n "os.replace" main.py outcome_tracker.py hold_monitor.py exit_monitor.py

# 5. Spearman IC in AdaptiveWeights
grep -n "spearmanr" signals.py | head -3

# 6. Velocity and cooldown gates wired into main.py
grep -n "_velocity_filter\|_cooldown_filter" main.py | head -4

# 7. Per-book adaptive weights
grep -n "adaptive_mom\|adaptive_mr" signals.py | head -3
```

If any check fails: restore from GitHub before proceeding.

---

## STEP 5 — UNDERSTAND TODAY'S CONTEXT

1. What positions are currently held? Run python check_account.py or paste portfolio.
2. What did the hold monitor say last run? Check hold_health.json.
3. What is the current macro regime? Check macro_context.json.
4. What is today's build target? Check RAPTOR_MASTER_PLAN.md build order section.

---

## DAILY OPERATING SCHEDULE

| Time ET | Bat File | Scripts | What it does |
|---------|----------|---------|--------------|
| 9:00 AM | — | macro_context.py | FRED + SPY regime |
| 9:15 AM | — | market_agent.py | SCAN / REDUCE / STANDBY |
| 9:28 AM | Start_PreMarket.bat | hold_monitor.py --pre | Pre-entry health check |
| 9:35 AM | Start_Entry.bat | main.py | Signal engine + gates + BUY orders |
| 9:35–3:50 | Start_Intraday_Monitor.bat | exit_monitor.py + hold_monitor.py | Loop every 30 min |
| 3:50 PM | Start_Afternoon_Monitor.bat | exit_monitor.py + hold_monitor.py + daily_recap.py | Final exits + recap |
| 4:30 PM | Start_Recap.bat | daily_recap.py | Recap at closing prices |
| 5:00 PM | Start_Analysis_Lab.bat | factor_lab.py + kelly_engine.py | IC + Kelly update |
| After close | — | outcome_tracker.py | Tag new closed trades |
| End of day | Daily_GitHub_Push.bat | git add/commit/push | Sync state to GitHub |

---

## SYSTEM STATE REFERENCE (2026-05-25)

### What is live and working

| Component | Status |
|-----------|--------|
| Dual-book signal engine (MOMENTUM only, MR suspended) | LIVE |
| Velocity gate wired to main.py | LIVE |
| Cooldown gate wired to main.py | LIVE |
| Vol-regime hard stop (2.5/3.0/3.5x ATR) | LIVE |
| Signal-aware trail — composite + health modifier | LIVE |
| Regime-relative thesis threshold | LIVE |
| Multi-MA breadth (50/150/200) | LIVE |
| Hold monitor 10-layer health scoring | LIVE |
| Math trim executing from hold_health.json | LIVE |
| Bootstrap Kelly — SHADOW mode (73/100 trades) | LIVE |
| Spearman IC + WLS + exponential decay | LIVE |
| Per-book AdaptiveWeights (MOMENTUM + MR files) | LIVE |
| Outcome tracker with backfill | LIVE — 98 trades |
| Factor IC lab | LIVE |
| Atomic JSON writes | LIVE — all critical files |
| All scored symbols in _last_full_signals | LIVE |
| Composite default 0.0 not -1.0 | LIVE |
| Ledoit-Wolf SNR ranking — replaces heuristic t-stat | LIVE |
| Gaussian regime blend via continuous macro_score | LIVE |
| No fabricated fallbacks | LIVE |

### What is NOT live (claimed but not in code)

| Claim | Reality |
|-------|---------|
| P1-1 Kalman macro | macro_context.py is vote-count — no Kalman. macro_score written and now consumed by Gaussian blend |
| P1-5 OU hold target | dist_to_mean formula for MR, hardcoded 15 for MOM |

### What is open (next to build)

| Priority | Item | Gate |
|----------|------|------|
| Data gate | MATH-1: Regime-conditional IC | 10+ trades per regime |
| Data gate | MATH-5: n_prior 50 to 20 | 60+ agent-tagged trades |
| Data gate | ARCH-1: IC layer weights hold monitor | 60+ tagged trades |
| Data gate | Kelly ACTIVE mode | 100 trades (coded, needs config flag) |
| Future | IC shrinkage (Bayesian prior on factor weights) | ARCH-2 |
| Future | Kalman macro classifier | ARCH-2 |

---

## CURRENT LIVE METRICS (update each session)

```
Equity:          ~$106,000
Trades tagged:   98 (outcome_log.json)
Kelly f_rec:     3.89% bootstrap P25 (SHADOW mode, 73/100 trades)
Exit quality:    math_trim 70% win (+5.59%) | trailing_stop 20% win (-5.35%)
IC condition:    272 (high — orthogonalization active)
Top factors:     ma_stack +0.48 | adx_dir +0.38 | price_cloud +0.35
MR book:         SUSPENDED — need MR-only trade outcomes to validate IC
Ledger gap:      CVE, KDP, PLTD — run backfill_ledger.py --write
```

---

## RULES CLAUDE MUST FOLLOW EVERY SESSION

1. Clone fresh from GitHub first. Never work from project knowledge alone.
2. Read all four MD files before any technical work.
3. Run the health check before writing code.
4. Verify the invariants (Step 4). If any fail, restore before proceeding.
5. Real data or skip. Missing data means conservative neutral or skip with warning. Never invent.
6. Not scored today does not mean failing thesis. comp=0.0 not -1.0 for unscored positions.
7. Momentum clustering is intentional alpha. Do not add correlation gates.
8. Kelly is SHADOW until 100 trades. Do not override sizing.
9. Syntax check every file before committing.
10. Commit message must describe what changed and why, not just update.
11. Every session ends with git push. Steve must pull before next trading day.
12. Update MD files when architecture changes. ONTOLOGY sync is mandatory same session.
13. Run outcome_tracker.py after any session discussing trade outcomes.
14. Never change code in the intraday window 9:35-3:50 PM ET without explicit intent.

---

## END OF SESSION CHECKLIST

```bash
# Syntax check all changed files
for f in main.py signals.py exit_monitor.py hold_monitor.py outcome_tracker.py kelly_engine.py; do
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" 2>/dev/null
done

# Commit
git add -A
git commit -m "Description of what changed and why (2026-MM-DD)"
git log --oneline -3
```

Then post push command for Steve:
```powershell
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" pull origin main
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" add -A
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" commit -m "same message"
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" push origin main
```

---

## KEY FILE LOCATIONS

```
Raptor/
├── RAPTOR_STARTUP.md        THIS FILE — read first every session
├── RAPTOR_MASTER_PLAN.md    Priority queue + verified status table
├── RAPTOR_SKILL.md          Rules + IC findings + math gaps
├── RAPTOR_ONTOLOGY.md       Full system logic — no code
├── main.py                  Entry scanner + velocity + cooldown gates
├── signals.py               Dual-book engine + AdaptiveWeights
├── exit_monitor.py          All exit and trim logic
├── hold_monitor.py          10-layer health scoring
├── outcome_tracker.py       Trade labeling + backfill
├── kelly_engine.py          Bootstrap Kelly (shadow mode)
├── factor_lab.py            IC validation (Spearman, regime breakdown)
├── macro_context.py         Regime classifier (vote-count)
├── config.py                All parameters
├── position_ledger.json     Open and closed positions
├── outcome_log.json         Labeled trades (growing)
├── kelly_estimates.json     Bootstrap Kelly output
├── factor_ic_report.json    IC validation results
├── hold_health.json         Current position health scores
├── composite_cache.json     Yesterday composites (velocity gate)
├── cooldown_log.json        Active re-entry blocks
└── adaptive_weights_MOMENTUM.json   Per-book ridge weights
```

---

*This file is the first thing read in every session.*
*If this file is out of date, update it before anything else.*
