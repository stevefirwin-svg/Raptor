# RAPTOR — Verified Audit Archive
*Original audit: 2026-05-29 | Updated: 2026-06-12*

This document preserves the original 2026-05-29 audit findings and is updated
with what was subsequently confirmed vs corrected.

---

## Original Audit Summary (2026-05-29)

Key finding: planning docs were unreliable. At least 3 of 8 claimed P0 fixes
were absent from live code at audit time. submit_order was missing its def line.
"42 IC-valid trades" was actually 8.

All original findings from this audit are now captured in RAPTOR_MASTER_PLAN.md
under COMPLETED or OPEN sections.

---

## Data Audit Update (2026-06-12)

The 2026-05-29 audit found "8 IC-valid terminal exits" which was correct at the time.
A further data quality audit on 2026-06-12 found a more serious problem:

**Multi-trim inflation:** By 2026-06-12, outcome_log.json had 76 "IC-valid" records
but only 27 independent position entries. Multiple trim events from a single position
(e.g. PLTD: 9 records, AMD: 4 records) were being counted as independent trades.

Impact:
- DSR falsely reported at 99.9% (correct value: 59.8%)
- Win rate inflated
- Mean PnL inflated (7.19% → true 5.47%)
- All gate counts were wrong (76 → 27 independent positions)

Resolution: position_outcomes.json built 2026-06-12. All future gating uses
this file. Raw outcome_log.json is for record-keeping only.

---

## Rule 11 Compliance Record

Rule 11: A fix is DONE only when grep/test output is pasted in the same session.

| Fix | Session | Rule 11 status |
|-----|---------|----------------|
| submit_order def line | 2026-06-05 | Verified — grep output pasted |
| P0-1 outcome sidecar | 2026-05-29 | Verified — grep output pasted |
| P0-8 regime override | 2026-05-29 | Verified — grep output pasted |
| S4-1 crash visibility | 2026-06-10 | Verified — python output pasted |
| S4-2 deterministic gate | 2026-06-10 | Verified — 8/8 unit tests pasted |
| S4-3 Gmail credentials | 2026-06-10 | Verified — grep output pasted |
| S4-4 bat log isolation | 2026-06-10 | Verified — in commit 0fc61f0 |
| S4-5 ETP exclusion | 2026-06-10 | Verified — 10/10 pattern tests pasted |
| S4b slippage tracker | 2026-06-10 | Verified — commit be8f37b |
| S4c sector neutralization | 2026-06-10 | Verified — commit 3857259 |
| S4d DSR | 2026-06-10 | Verified — commit 3b647bd |
| S5-1 OU hold target | 2026-06-11 | Verified — commit 57c08d7 |
| S5-5 position_outcomes | 2026-06-12 | Verified — 27 positions, schema OK |
| S5-6 DSR corrected | 2026-06-12 | Verified — 59.8% from 24 positions |

---

## Previously Claimed But Incorrect (corrected record)

| Claim | Reality | Corrected |
|-------|---------|-----------|
| "42 IC-valid trades" (pre-2026-05-29) | Actually 8 terminal exits | 2026-05-29 |
| "P1-5 OU hold target live" (pre-2026-06-11) | Was hardcoded 15 days / dist_to_mean | 2026-06-11 |
| "DSR 99.9%" (2026-06-10) | Multi-trim inflation; true DSR 59.8% | 2026-06-12 |
| "76 IC-valid trades" (2026-06-12) | Actually 27 independent positions | 2026-06-12 |
