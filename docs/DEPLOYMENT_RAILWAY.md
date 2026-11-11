# Deploying to Railway

One Railway **service** (this repo, built from the `Dockerfile`), plus Railway's **PostgreSQL** and **Redis** databases.

1. **Push this repository to GitHub.**
2. **In Railway, create a new project and deploy from the GitHub repo.** Railway detects `railway.json` and builds the Dockerfile.
3. **Add databases** to the project: **PostgreSQL** and **Redis**.
4. **Set these service variables** on the MERIDIAN service:
   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference variable; `postgresql://` is converted to the psycopg driver automatically) |
   | `REDIS_URL` | `${{Redis.REDIS_URL}}` |
   | `MERIDIAN_ENV` | `production` |
   | `JWT_SECRET` | 48+ random chars: `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
   | `CIO_USERNAME` / `CIO_PASSWORD` | your login (account is created on first boot only) |
   | `MARKET_DATA_MODE` | `simulated` first; switch to `coinbase` after the checks below |
   | `STARTING_CAPITAL` | `700` |
5. **Networking:** generate a public domain for the service. Railway sets `PORT`; the app listens on it.
6. **Deploy.** On boot the app runs migrations, seeds the registry and the default risk config, records $700 starting capital and creates the CIO user. The health check is at `/api/health`.

### Going live with Coinbase market data
1. Open a Railway shell (or run locally) and run `python -m meridian.cli check-coinbase`. Confirm GOLD, SILVER, OIL, BTC, ETH and XRP each resolve to the contract you actually trade, with sensible contract size, tick and expiry.
2. Pin any wrong ones with `MERIDIAN_PRODUCT_OVERRIDES`.
3. Set `MARKET_DATA_MODE=coinbase` and redeploy.
4. In Risk Command, enter `margin_per_contract` per asset from your Coinbase futures screen so ideas are not left as RESTRICT: MARGIN_UNVERIFIED.

### Notes
* Keep **numReplicas = 1**. Workers run in-process; multiple replicas would run duplicate schedulers. Consumers are idempotent, but scheduled ingestion would still be doubled.
* Railway's Postgres image is plain PostgreSQL. TimescaleDB is optional and is skipped automatically.
* Rollback: redeploy a previous build in Railway. Migrations are additive; `alembic downgrade` exists but drops data, so never run it in production without a backup.
* Backups: enable Railway Postgres backups. Postgres is the system of record (Redis can be rebuilt from the outbox via replay).
