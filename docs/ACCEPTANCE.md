# Milestone 1 acceptance tests

Run: `cd backend && pytest -q`, which gives **75 passed** (unit + integration on real PostgreSQL 16 + Redis 7; Anthropic API mocked). Frontend: `npm run typecheck && npm run build` passes, and all 6 sections were rendered in headless Chrome with zero console errors.

| # | Criterion | Evidence |
|---|---|---|
| AT1 | Full flow: market data → trend → case → macro → risk → memo → dashboard → CIO decision | `test_end_to_end_vertical_slice`; browser walkthrough (accept + record position) |
| AT2 | No execution capability | `test_no_order_execution_code_exists`, `test_client_rejects_non_public_or_non_get` |
| AT3 | BLOCK can never become RECOMMENDED (state machine + DB) | `test_database_enforces_risk_veto_and_immutability`, `test_committee_cannot_recommend_on_block`, `test_cannot_accept_blocked_case` |
| AT4 | Missing data never yields ALLOW | `test_missing_essential_information_blocks`, `test_missing_nonessential_information_never_allows`, `test_empty_config_blocks` |
| AT5 | Idempotency, retry → DLQ, expiry, replay | `test_bus_idempotency_retry_dlq_expiry_replay` |
| AT6 | Risk config needs CIO + re-auth; versioned and audited | `test_risk_config_change_requires_reauth_and_is_audited` |
| AT7 | Recommendation reconstructs from stored inputs | `test_end_to_end_vertical_slice` (6/6 layers match) |
| AT8 | Worker failure retried, recorded, escalated; inactive workers pause durably | `test_worker_failure_is_retried_recorded_and_escalated`, `test_inactive_worker_does_not_consume` |
| AT9 | Simulated data always labeled | SIM- ids, `source=SIMULATED` on rows/memos, UI banner, `test_simulated_provider_is_deterministic_and_labeled` |
| AT10 | 10% max loss per trade and across open positions | `test_clean_trade_allows_and_sizes_to_10pct`, `test_one_contract_over_10pct_blocks`, `test_total_open_risk_*`, e2e re-review assertion |
| AT11 | HOLD/EXIT advisories | `test_position_monitor`, `test_positions_and_monitor_exit_on_stop` |
| AT12 | Audit tampering detected | `test_audit_chain_detects_tampering` |
| AT13 | Research department learns from retrieved sources only, records spend, respects caps | `test_researcher_learns_records_spend_and_drops_fabricated_sources`, `test_budget_cap_blocks_calls_and_failures_still_count`, `test_no_key_is_explicitly_unavailable`, `test_fabricated_citations_are_dropped` |
| AT14 | Every analyst runs cleanly on market data and attaches evidence to cases | `test_all_analysts_run_and_attach_evidence` |
| AT15 | Portfolio constructor never exceeds capital, risk room or one-per-asset | `tests/unit/test_constructor.py` |
