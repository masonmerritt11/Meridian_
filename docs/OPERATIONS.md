# Operations runbook

## Daily use (CIO)
1. **Executive Overview:** new memos, open-risk meter, EXIT advisories, alerts, data freshness.
2. **Research Laboratory:** open a memo, read evidence for and against and the risk checks, then record **ACCEPT / DEFER / DECLINE**.
3. **If you trade it:** use *Record the trade you executed* with your real fills and stop. This starts HOLD/EXIT monitoring and re-reviews the other open recommendations against the new open risk.
4. **Risk Command:** the advisory for each position. When you exit, click **I exited** with your fill; when you move your stop at the broker, click **Stop moved**.

Advisory meanings: **HOLD** means the trend is intact, the stop has not been hit and no exit trigger is present. **EXIT** comes with reasons: STOP_BREACHED, LOSS_LIMIT_REACHED, TARGET_REACHED, TREND_INVALIDATED, CONTRACT_EXPIRING. **REVIEW** means MERIDIAN cannot vouch for HOLD (stale or missing data, or a high-impact event is imminent).

## Health signals
| Signal | Where | Action |
|---|---|---|
| Worker FAILING / STALE | Department Operations | Open the worker, read the last run error; failures also emit `worker.failed` and an audit entry |
| Dataset STALE / UNAVAILABLE | Overview → freshness | Risk blocks new ideas on stale candles; check provider reachability / `check-coinbase` |
| macro_calendar UNAVAILABLE | Radar → Macro calendar | Ideas are RESTRICTed (EVENT_DATA_UNAVAILABLE); check the calendar manually before acting |
| DLQ depth > 0 | Operations → Dead-letter queue | Fix the cause, then `POST /api/events/replay {"consumer": "<worker id>", "force": false}` |
| Audit chain broken | Decision Archive | Treat as a security incident; `python -m meridian.cli verify-audit` |

## Common procedures
* **Pause a worker:** Operations → worker → Deactivate. Its events queue durably in Redis.
* **Change risk limits:** Risk Command → edit JSON → reason + password. Creates a new immutable version.
* **Add or withdraw capital:** `POST /api/capital {"kind":"DEPOSIT","amount":"300","note":"..."}` (CIO).
* **Add a read-only user:** `MERIDIAN_NEW_PASSWORD=... python -m meridian.cli create-user alice VIEWER`.
* **Reconstruct a recommendation:** Research Laboratory → Reconstruct from inputs (or `GET /api/memos/{id}/reconstruct`).
* **Recover after Redis loss:** restart the app. Unpublished events relay from the outbox; replay consumers from Postgres if needed.

## Logs
Structured JSON on stdout (Railway → Deployments → Logs). Useful fields: `worker_id`, `run_id`, `event_id`, `consumer`, `status`. Optional OpenTelemetry traces: set `OTEL_EXPORTER_OTLP_ENDPOINT` and install `.[otel]`.
