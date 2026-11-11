# Architecture

## Shape
A **modular monolith**: one Python process runs the FastAPI API, serves the static Next.js command center, and runs workers in background threads (`meridian/runtime.py`). PostgreSQL is the system of record; Redis Streams carries events. Any consumer group can later be moved to its own process without code changes because workers only talk through events and the database.

```
Coinbase public API ─┐                      ┌─> trend.signal_analyst ──signal.detected──> exec.chief_of_staff (case + macro)
  (or SIMULATED)     ├─> data.market_ingest ─┤                                            │ case.researched
Fair Economy feed ───┴─> macro.calendar_analyst                                          v
                                             └─> risk.position_monitor          exec.research_officer (challenge)
                                                       │ HOLD/EXIT                        │ case.challenged
                                                       v                                  v
                                                  CIO (dashboard)  <── committee memo ── risk.chief_risk_officer ── risk.decision.recorded ──> exec.committee_coordinator
```

## Event bus (`meridian/bus`)
* **Outbox:** producers call `publish(session, envelope)` inside the same DB transaction as their state change. The relay moves rows to the Redis stream `meridian:events`. If Redis is down, events wait in Postgres.
* **Envelope:** `event_id`, `event_type`, `schema_version`, `correlation_id`, `causation_id`, `producer` (worker@version), `occurred_at` (UTC), `expires_at`, `input_refs[]` (kind, ref, hash), `payload`. Payloads are validated against registered Pydantic models at publish time (`python -m meridian.cli export-schemas` writes them to `docs/event-schemas.json`).
* **Consumers:** one Redis consumer group per worker. Processing happens in a DB transaction that also writes `(consumer, event_id)` into `event_consumptions`, so duplicate deliveries are no-ops (idempotent).
* **Retries and DLQ:** failures stay pending and are re-claimed after a backoff (`XAUTOCLAIM`). After `max_attempts` they are written to `dead_letters` plus the `meridian:dlq` stream, then acknowledged.
* **Expiry:** expired events are acknowledged and recorded as `EXPIRED`, not processed.
* **Replay:** `EventBus.replay()` re-runs stored events from Postgres through a consumer (idempotent by default, `force=True` to reprocess). Exposed at `POST /api/events/replay` (CIO).

## Workers (`meridian/workers`)
`WorkerSpec` declares id, department, responsibility, version, input/output schemas, schedule, subscriptions, escalation path, retry policy and default config. `execute()` wraps every run with a `worker_runs` record (versions, inputs, outputs, metrics, errors), heartbeats, retries with exponential backoff, structured JSON logs and, on final failure, a `worker.failed` event plus an audit entry naming the escalation target. Deactivated workers are not run; their events wait durably until reactivation.

| Worker | Trigger | Does |
|---|---|---|
| data.instrument_discovery | every 6h | Resolves the 6 assets to products (futures preferred, perpetual-style first for crypto, spot fallback) |
| data.market_ingest | every 5 min | Fetches closed candles + book, validates, stores; emits on a new closed bar |
| data.freshness_monitor | every 60s | FRESH / STALE / UNAVAILABLE for every dataset |
| macro.calendar_analyst | hourly | Looks up the weekly economic calendar; never guesses time zones |
| trend.signal_analyst | candles event | EMA(20/50) + 20-bar breakout; stop 2×ATR(14), target 3×ATR, rounded to tick |
| exec.chief_of_staff | signal event, 5-min sweep | Creates/reconfirms/invalidates cases, attaches macro context, expires stale cases |
| exec.research_officer | case.researched | Independent opposing analysis |
| risk.chief_risk_officer | case.challenged | Runs the risk engine; decision is final |
| exec.committee_coordinator | risk decision | Memo, ranking score, dissent, uncertainty |
| risk.position_monitor | every 60s + candles/position events | HOLD / EXIT / REVIEW for CIO-recorded positions |

## Research case lifecycle
`DETECTED → RESEARCHING → CHALLENGED → RISK_REVIEW → COMMITTEE_REVIEW → RECOMMENDED | DEFERRED | REJECTED | BLOCKED → EXPIRED | INVALIDATED | CLOSED`, with back-transitions to `RESEARCHING` from CHALLENGED, RISK_REVIEW, COMMITTEE_REVIEW, RECOMMENDED, DEFERRED and BLOCKED when new evidence appears (for example a portfolio change). The original hypothesis is immutable (DB trigger); revisions, evidence, transitions, risk decisions, memos and CIO decisions are all kept.

## Risk veto: three layers
1. `risk/engine.py` is a pure function; missing essential data returns BLOCK and missing non-essential data returns RESTRICT. It never returns ALLOW with gaps.
2. `cases/lifecycle.transition()` refuses RECOMMENDED unless the latest risk decision exists and is not BLOCK.
3. PostgreSQL triggers refuse the same thing on direct SQL, refuse a memo that RECOMMENDs against BLOCK, and make `risk_decisions`, `audit_log` and risk config contents immutable.

The committee is deterministic: every number in a memo is copied from the signal calculation or the risk engine's sizing. No language model is involved in Milestone 1.

## Reconstruction
`GET /api/memos/{id}/reconstruct` re-reads the candle window by time range and checks its hash. It then recomputes the trend signal, re-runs the risk engine on the stored inputs and re-runs the committee on its stored inputs, comparing each layer.

## Database schema (key tables)
| Table | Purpose |
|---|---|
| departments, worker_definitions, worker_config_versions, worker_heartbeats, worker_runs | Registry, versioned config, health, execution log |
| events, event_consumptions, dead_letters | Outbox / durable log, idempotency, DLQ |
| instruments, candles (Timescale hypertable when available), book_snapshots, data_freshness | Market data |
| macro_events | Economic calendar |
| research_cases, case_revisions, case_evidence, case_transitions | Research cases |
| risk_configs (versioned, immutable), risk_decisions (append-only) | Risk |
| committee_memos, cio_decisions | Committee and human decisions |
| positions, position_advisories, capital_events | CIO-recorded trades, advisories, capital |
| users, audit_log (hash-chained, append-only) | Security and audit |

The full DDL is in `backend/migrations/versions/`.

## Security
* Secrets only from environment variables. Production refuses to start with a weak `JWT_SECRET`.
* bcrypt password hashes; JWT in an HttpOnly, SameSite=Strict cookie (Secure in production); login throttling.
* Roles: **CIO** (all state changes) and **VIEWER** (read-only). Risk-config changes need password re-entry and a reason.
* Security headers (CSP, X-Frame-Options DENY, nosniff). No credentials for any exchange are requested, stored or sent.
