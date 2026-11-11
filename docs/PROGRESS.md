# Overnight build log: 2026-11-10 → 11

Instructions from Mason: fix spelling; add a Trading Psychology & Philosophy research department (learn + recommend only, visible journal);
deeper worker detail in Operations; activate the remaining workers. Work unattended; make judgment calls.

Hard stops (left for Mason): risk-limit changes (need his password), new paid services/keys, deleting history, loosening his rules.

| Stage | Status |
|---|---|
| 1 Spelling (American English) | done |
| 2 Worker detail view | done |
| 3 Research department + Learning Journal | done (deploying) |
| 4 Activate remaining workers | done: 54 of 55 roles working (deploying) |
| 5 Verify on Railway + report | done (research notes re-verified after the 00:00 UTC budget reset) |

## Decisions made without Mason
- American spelling everywhere.
- New analysts attach evidence to cases (can move rankings); visible per case. Department leads / consensus views are shown
  but not counted again as evidence. Committee now defers only when opposing evidence outnumbers supporting evidence.
- Language model: claude-sonnet-5-5, $3/day total cap (researchers $1.10 each, director $0.25, each macro analyst $0.15).
  A single call can overshoot the cap by at most ~$0.40 (reserved before each research call).
- Research reads with Anthropic web search + web fetch (max 4 searches, 2 full reads per session); falls back to arXiv
  abstracts if web tools are disabled on the account. Only sources actually retrieved are kept as citations.
- Schedules persist across deploys (no re-run burst on each redeploy). LLM workers run on their own scheduler thread.
- stat_arb.cross_venue stays PLANNED: needs other exchanges' price feeds (not available without new paid services).

## Incidents
- 2026-11-11 05:56-11:56 UTC: research sessions read real sources (27-28 per session) but answers hit the output-length limit
  before the JSON note, so 6 sessions (about $2) produced no notes; each researcher then hit its $1.10 daily cap.
  Fixed: tool-free repair call (~2 cents) turns any cut-off answer into the note; longer output limit; 1 full read per session;
  failed topics re-queued (migration 0004). Regression test added.
- Demo scenario was time-of-day dependent (flaky test); now has a decisive breakout bar and is checked across 48 hours.

## Verified live (2026-11-11 17:55 UTC)
- All 5 AI macro analysts: web search working (27-29 sources each), views parsed, ~$0.11 per run.
- Freshness 13/13 after retiring demo datasets; 0 failed runs; compliance self-check PASS; system HEALTHY.

## Needs Mason in the morning
- Raise max_total_open_risk_pct (Risk Command, needs password) if you want several positions open at once.
