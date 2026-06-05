# RAPTOR_STARTUP.md — Session Startup
*Read first. Every session. No exceptions.*
*Last updated: 2026-06-05 | Version: 6.0*

---

## STEP 1 — PULL FRESH FROM GITHUB

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

**Note the top commit hash from `log --oneline`.** Paste it to Claude at session start.

---

## STEP 2 — READ FILES IN ORDER

1. **RAPTOR_STARTUP.md** — startup sequence and system state (this file)
2. **RAPTOR_MASTER_PLAN.md** — what is done, what is open, build order
3. **RAPTOR_SKILL.md** — rules, factor lifecycle, what must never be violated
4. **RAPTOR_ONTOLOGY.md** — full system logic and math

---

## STEP 3 — INFRASTRUCTURE INTEGRITY CHECK

**Run this first, before health check, before any code work. These are not optional.**

```bash
# 1. AlpacaDataFeed has submit_order — the method that actually submits trades
python3 -c "
from data_feeds import AlpacaDataFeed
methods = [m for m in dir(AlpacaDataFeed) if not m.startswith('_')]
assert 'submit_order' in methods, 'CRITICAL: submit_order MISSING from AlpacaDataFeed'
print('OK: submit_order present')
print('All methods:', methods)
"

# 2. Syntax check all critical files
for f in main.py signals.py exit_monitor.py hold_monitor.py data_feeds.py outcome_tracker.py; do
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')"
done

# 3. No fabricated fallbacks
grep -n "price \* 0\.02\|entry_price \* 0\.92" exit_monitor.py hold_monitor.py
# Expected: no output. Any match = fabricated fallback = fix before proceeding.

# 4. Default composite is 0.0 not -1.0
grep -n "scores\[sym\] = 0\.0\|default 0\.0" exit_monitor.py | head -3

# 5. Atomic writes present
grep -n "os.replace" main.py outcome_tracker.py hold_monitor.py exit_monitor.py

# 6. _last_full_signals stores ALL scored symbols
grep -n "_last_full_signals" signals.py | head -3

# 7. Velocity and cooldown gates wired
grep -n "_velocity_filter\|_cooldown_filter" main.py | head -4

# 8. Spearman IC in AdaptiveWeights
grep -n "spearmanr" signals.py | head -3
```

**If submit_order check fails: STOP. Do not run exit_monitor live. Fix data_feeds.py first.**
This check exists because submit_order was missing its `def` line from ~2026-05-25 to 2026-06-05,
causing every order submission to fail silently with no log output. Eleven days of exits and trims
never reached Alpaca.

---

## STEP 4 — HEALTH CHECK

```powershell
python outcome_tracker.py --summary
python kelly_engine.py
python factor_lab.py
```

**What to look for:**

`outcome_tracker.py --summary`:
- IC-valid count — goal: 60 (unlocks MATH-5, ARCH-1). Currently 8
- math_trim win% should be > 60%
- trailing_stop win% should be > 40%
- entry_decision field: populating from next exit onwards (P0-1 fixed 2026-05-29)

`kelly_engine.py`:
- mode = SHADOW until 100 trades. Sizing unchanged.
- f_recommended = bootstrap P25. Currently 1% (win_rate 27.9% on terminal exits).

`factor_lab.py`:
- ma_stack and adx_dir should stay IC > 0.05, t > 1.5
- vol_ratio IC=-0.11 — WATCH, remove if IC < 0.03 for 3+ weeks
- Any factor IC < 0.03, t < 1.0 for 3+ consecutive weeks → remove

---

## STEP 5 — CONTEXT CHECK

1. What positions are held? Run `python check_account.py` or paste portfolio.
2. What did hold monitor say last run? Check `hold_health.json`.
3. Are hold_health timestamps TODAY? If all timestamps are yesterday → hold_monitor did not run this morning. Investigate before trusting trim recommendations.
4. What is current macro regime? Check `macro_context.json`.
5. What is today's build target? See MASTER_PLAN open items.

---

## DAILY SCHEDULE

| Time ET | Script | What it does |
|---------|--------|--------------|
| 9:00 AM | macro_context.py | FRED + SPY regime → macro_score |
| 9:15 AM | market_agent.py | SCAN / REDUCE / STANDBY |
| 9:28 AM | hold_monitor.py --pre | Pre-entry health check |
| 9:35 AM | main.py | Signal engine + gates + BUY orders |
| 9:35–3:50 | exit_monitor.py + hold_monitor.py | Loop every 30 min |
| 3:50 PM | exit_monitor.py + hold_monitor.py + daily_recap.py | Final exits + recap |
| 4:30 PM | daily_recap.py | Recap at closing prices |
| 5:00 PM | factor_lab.py + kelly_engine.py | IC + Kelly update |
| After close | outcome_tracker.py | Tag new closed trades |
| End of day | Daily_GitHub_Push.bat | git push |

---

## SYSTEM STATE (2026-06-05)

### Live and working

| Component | Status |
|-----------|--------|
| Dual-book signal engine (MOMENTUM live, MR suspended) | ✅ LIVE |
| submit_order method on AlpacaDataFeed | ✅ FIXED 2026-06-05 (was missing def line) |
| submit_order try/except in execute loop | ✅ FIXED 2026-06-05 (loop no longer aborts on first error) |
| Ledoit-Wolf SNR entry ranking | ✅ LIVE |
| Soft z-score shrinkage (replaces hard threshold) | ✅ LIVE |
| accum_dist: r² quality weight | ✅ LIVE |
| Regime probability blend (Gaussian, continuous) | ✅ LIVE |
| Velocity gate (_velocity_filter) | ✅ LIVE |
| Cooldown gate (_cooldown_filter) | ✅ LIVE |
| Vol-regime hard stop | ✅ LIVE |
| Signal-aware trail (composite + health modifier) | ✅ LIVE |
| Regime-relative thesis threshold | ✅ LIVE |
| Multi-MA breadth (50/150/200) | ✅ LIVE |
| Hold monitor 8-layer health scoring | ✅ LIVE |
| Math trim from hold_health.json | ✅ LIVE |
| Portfolio heat proportional 25% trim | ✅ LIVE |
| Bootstrap Kelly — SHADOW mode | ✅ LIVE |
| Spearman IC + WLS + exponential decay (λ=0.005) | ✅ LIVE |
| Per-book AdaptiveWeights (MOMENTUM + MR files) | ✅ LIVE |
| Outcome tracker --backfill --relabel | ✅ LIVE |
| Atomic JSON writes (os.replace) | ✅ LIVE |
| P0-1: outcome_pending sidecar | ✅ LIVE 2026-05-29 |
| P0-8: regime override from macro_context.json | ✅ LIVE 2026-05-29 |
| DFA-1 Hurst (replaces R/S) | ✅ LIVE 2026-05-29 |

### NOT live (claimed but not in code)

| Claim | Reality |
|-------|---------|
| P1-1 Kalman macro | macro_context.py is vote-count; replaced by Gaussian blend in signals.py |
| P1-5 OU hold target | hardcoded 15 days MOM; dist_to_mean formula for MR |

### Known open issues

| Issue | Impact | Status |
|-------|--------|--------|
| Trail multiplier uses round numbers (profit_atr≥4.0→1.0×) | Stops too tight on winning positions — EXIT 1 fires on broad pullback days | GAP-B, needs backtest derivation |
| INTC ledger stop=112 (stale backfill) above current price | EXIT 1 fires every run | Verify/fix ledger stop manually |
| logs/ was gitignored | Diagnostic logs unavailable for analysis | Fixed 2026-06-05 — now tracked |
| UnicodeEncodeError on → character in log output | Logging error printed but execution continues | Low priority cosmetic fix |

---

## KEY FILES

```
RAPTOR_STARTUP.md         This file — read first
RAPTOR_MASTER_PLAN.md     Priority queue + verified status
RAPTOR_SKILL.md           Rules + factor lifecycle + what to never do
RAPTOR_ONTOLOGY.md        Full system logic — no code
data_feeds.py             AlpacaDataFeed — submit_order, get_positions, get_account
signals.py                Dual-book engine + SNR + AdaptiveWeights + FACTOR_NAMES
main.py                   Entry + velocity + cooldown gates
exit_monitor.py           All exit and trim logic
hold_monitor.py           8-layer health scoring
outcome_tracker.py        Trade labeling
kelly_engine.py           Bootstrap Kelly (shadow)
factor_lab.py             IC validation
macro_context.py          Regime classifier
config.py                 All parameters
outcome_log.json          Labeled trades
kelly_estimates.json      Bootstrap Kelly output
factor_ic_report.json     IC validation results
hold_health.json          Position health scores
composite_cache.json      Today's composites (velocity gate input)
cooldown_log.json         Active re-entry blocks
```

---

## END OF SESSION CHECKLIST

### Claude side (every session that touches code)

```bash
# 1. Infrastructure integrity check (Step 3 above) — re-run after any data_feeds.py change
python3 -c "
from data_feeds import AlpacaDataFeed
assert 'submit_order' in dir(AlpacaDataFeed), 'CRITICAL: submit_order missing'
print('OK: submit_order present')
"

# 2. Syntax check all changed files
for f in main.py signals.py exit_monitor.py hold_monitor.py data_feeds.py outcome_tracker.py; do
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')"
done

# 3. Commit
git add -A
git commit -m "Description: what changed and why (2026-MM-DD)"
git log --oneline -3
```

### Steve's side (pull and push)

```powershell
cd "C:\Users\steve\OneDrive\Desktop\Raptor"
git pull origin main
git log --oneline -4
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

---

## RULES CLAUDE MUST FOLLOW

1. Clone fresh from GitHub first. Never work from project knowledge alone.
2. Read all four MD files before any technical work.
3. Run infrastructure integrity check (Step 3) before any code work.
4. Run health check before writing code.
5. Real data or skip. Never invent a default.
6. comp=0.0 for unscored positions. Never -1.0.
7. Momentum clustering is alpha. No correlation gates.
8. Kelly is SHADOW until 100 trades.
9. Syntax check before committing.
10. Commit message must describe what changed and why.
11. Update ONTOLOGY same session as architecture changes.
12. Run outcome_tracker.py after any session touching trade outcomes.
13. Never change code intraday (9:35–3:50 ET) without explicit intent.
14. No factor is permanent. IC > 0.03, |t| > 1.0, ICIR > 0.3 over 60-day rolling window to stay.
15. Every constant needs a derivation or `# TODO:DERIVE` comment with method noted.
16. When evaluation is impossible → log warning, skip. Never substitute.
17. After any edit to data_feeds.py: re-run the submit_order existence check. Non-negotiable.
