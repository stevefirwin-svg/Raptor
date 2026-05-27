# RAPTOR_STARTUP.md — Session Startup
*Read first. Every session. No exceptions.*
*Last updated: 2026-05-27 | Version: 5.6*

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
Remove-Item -Recurse -Force __pycache__
```

---

## STEP 2 — READ FILES IN ORDER

1. **RAPTOR_STARTUP.md** — startup sequence and system state (this file)
2. **RAPTOR_MASTER_PLAN.md** — what is done, what is open, build order
3. **RAPTOR_SKILL.md** — rules, factor lifecycle, what must never be violated
4. **RAPTOR_ONTOLOGY.md** — full system logic and math

---

## STEP 3 — HEALTH CHECK

Run before writing any code. Paste output to Claude when starting.

```powershell
python outcome_tracker.py --summary
python kelly_engine.py
python factor_lab.py
```

**What to look for:**

`outcome_tracker.py --summary`:
- IC-valid count — goal: 60 (unlocks MATH-5, ARCH-1)
- math_trim win% should be > 60%
- trailing_stop win% should be > 40% (currently underperforming)

`kelly_engine.py`:
- mode = SHADOW until 100 trades. Sizing unchanged.
- f_recommended = bootstrap P25. Should be 3–5%.

`factor_lab.py`:
- ma_stack and adx_dir should stay IC > 0.05, t > 1.5
- vol_ratio IC=-0.11 — WATCH, remove if IC < 0.03 for 3+ weeks
- Any factor IC < 0.03, t < 1.0 for 3+ consecutive weeks → remove

---

## STEP 4 — VERIFY CRITICAL INVARIANTS

```bash
# 1. _last_full_signals stores ALL scored symbols
grep -n "_last_full_signals" signals.py | head -3

# 2. Default composite is 0.0 not -1.0
grep -n "0\.0.*Not scored\|scores\[sym\] = 0\.0\|default 0\.0" exit_monitor.py | head -3

# 3. No fabricated fallbacks
grep -n "price \* 0\.02\|entry_price \* 0\.92" exit_monitor.py hold_monitor.py

# 4. Atomic writes present
grep -n "os.replace" main.py outcome_tracker.py hold_monitor.py exit_monitor.py

# 5. Spearman IC in AdaptiveWeights
grep -n "spearmanr" signals.py | head -3

# 6. Velocity and cooldown gates wired
grep -n "_velocity_filter\|_cooldown_filter" main.py | head -4

# 7. Per-book adaptive weights
grep -n "adaptive_mom\|adaptive_mr" signals.py | head -3

# 8. Soft z shrinkage (not hard threshold)
grep -n "z_soft\|z\[fn\]\*(abs" signals.py | head -3
```

If any check fails: restore from GitHub before proceeding.

---

## STEP 5 — CONTEXT CHECK

1. What positions are held? Run `python check_account.py` or paste portfolio.
2. What did hold monitor say last run? Check `hold_health.json`.
3. What is current macro regime? Check `macro_context.json`.
4. What is today's build target? See MASTER_PLAN open items.

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

## SYSTEM STATE (2026-05-27)

### Live and working

| Component | Status |
|-----------|--------|
| Dual-book signal engine (MOMENTUM live, MR suspended) | ✅ LIVE |
| Ledoit-Wolf SNR entry ranking | ✅ LIVE 2026-05-25 |
| Soft z-score shrinkage (replaces hard \|z\|>0.10 threshold) | ✅ LIVE 2026-05-27 |
| accum_dist: r² quality weight (was abs(r)) | ✅ LIVE 2026-05-27 |
| Spearman IC inline derivation comments | ✅ LIVE 2026-05-27 |
| Regime probability blend (Gaussian, continuous macro_score) | ✅ LIVE 2026-05-25 |
| Velocity gate (_velocity_filter) | ✅ LIVE |
| Cooldown gate (_cooldown_filter) | ✅ LIVE |
| Vol-regime hard stop (2.5/3.0/3.5x ATR) | ✅ LIVE |
| Signal-aware trail (composite + health modifier) | ✅ LIVE |
| Regime-relative thesis threshold | ✅ LIVE |
| Multi-MA breadth (50/150/200) | ✅ LIVE |
| Hold monitor 10-layer health scoring | ✅ LIVE |
| Math trim from hold_health.json | ✅ LIVE |
| Portfolio heat proportional 25% trim | ✅ LIVE 2026-05-25 |
| Bootstrap Kelly — SHADOW mode | ✅ LIVE |
| Spearman IC + WLS + exponential decay (λ=0.005) | ✅ LIVE |
| Per-book AdaptiveWeights (MOMENTUM + MR files) | ✅ LIVE |
| Outcome tracker --backfill --relabel | ✅ LIVE |
| Atomic JSON writes (os.replace) | ✅ LIVE |
| exit_monitor: ATR fallback → hold_health.json, skip if missing | ✅ LIVE 2026-05-27 |
| exit_monitor: days_held fallback 1 (was 7) | ✅ LIVE 2026-05-27 |
| All scored symbols in _last_full_signals | ✅ LIVE |
| daily_recap regime_score, Sharpe, stop dist fixed | ✅ LIVE |

### NOT live (claimed but not in code)

| Claim | Reality |
|-------|---------|
| P1-1 Kalman macro | macro_context.py is vote-count; replaced by Gaussian blend in signals.py |
| P1-5 OU hold target | hardcoded 15 days MOM; dist_to_mean formula for MR |

### Open (next to build)

| Priority | Item | Gate |
|----------|------|------|
| Next session | MATH-3 Full Hurst DFA | No gate |
| IC-valid >= 60 | MATH-5: n_prior 50→20 | Have 42 |
| IC-valid >= 60 | ARCH-1: IC layer weights hold monitor | Have 42 |
| IC-valid >= 60 | MATH-1: Regime-conditional IC | Have 42 |
| 100 equity trades | Kelly ACTIVE mode | Have 42 |
| After MR resumes | P1-5: OU hold target per stock | MR needs outcomes |

---

## KEY FILES

```
RAPTOR_STARTUP.md         This file — read first
RAPTOR_MASTER_PLAN.md     Priority queue + verified status
RAPTOR_SKILL.md           Rules + factor lifecycle + what to never do
RAPTOR_ONTOLOGY.md        Full system logic — no code
signals.py                Dual-book engine + SNR + AdaptiveWeights + FACTOR_NAMES
main.py                   Entry + velocity + cooldown gates
exit_monitor.py           All exit and trim logic
hold_monitor.py           10-layer health scoring
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
adaptive_weights_MOMENTUM.json  Per-book ridge weights
```

---

## END OF SESSION CHECKLIST

```bash
# Syntax check all changed files
for f in main.py signals.py exit_monitor.py hold_monitor.py outcome_tracker.py kelly_engine.py daily_recap.py; do
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" 2>/dev/null
done

git add -A
git commit -m "Description: what changed and why (2026-MM-DD)"
git log --oneline -3
```

Steve's push:
```powershell
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" pull origin main
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" add -A
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" commit -m "same message"
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" push origin main
```

---

## RULES CLAUDE MUST FOLLOW

1. Clone fresh from GitHub first. Never work from project knowledge alone.
2. Read all four MD files before any technical work.
3. Run health check before writing code.
4. Verify invariants (Step 4). If any fail, restore before proceeding.
5. Real data or skip. Never invent a default.
6. comp=0.0 for unscored positions. Never -1.0.
7. Momentum clustering is alpha. No correlation gates.
8. Kelly is SHADOW until 100 trades.
9. Syntax check before committing.
10. Commit message must describe what changed and why.
11. Every session ends with git push.
12. Update ONTOLOGY same session as architecture changes.
13. Run outcome_tracker.py after any session touching trade outcomes.
14. Never change code intraday (9:35–3:50 ET) without explicit intent.
15. No factor is permanent. IC > 0.05, ICIR > 0.5 over 60-day rolling window to stay.
16. Every constant needs a derivation or `# TODO:DERIVE` comment with method noted.
17. When evaluation is impossible → log warning, skip. Never substitute.
