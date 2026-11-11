# Local development

## Option A: Docker Compose (everything)
```bash
cp .env.example .env     # edit JWT_SECRET, CIO_USERNAME, CIO_PASSWORD
docker compose up --build
open http://localhost:8000
```

## Option B: bare metal (PostgreSQL 16 + Redis 7 installed locally)
```bash
createuser -s meridian && createdb -O meridian meridian && createdb -O meridian meridian_test
psql -c "ALTER USER meridian PASSWORD 'meridian'"

cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example .env       # adjust
python -m meridian.cli bootstrap   # migrations + seed + CIO user
python -m meridian.cli demo        # optional: run the simulated pipeline once from the CLI

cd ../frontend && npm install && npm run build   # produces frontend/out, served by the API
cd ../backend && python -m meridian.cli serve    # http://localhost:8000
```

Frontend hot reload: run the API on :8000, then `cd frontend && npm run dev` and open http://localhost:3000 (`/api` is proxied).

## Tests
```bash
cd backend && pytest -q          # unit + integration (integration auto-skips if Postgres/Redis are unreachable)
cd frontend && npm run typecheck && npm run build
```
Integration tests use the `meridian_test` database and Redis DB 15, and drop and recreate the schema for each test.

## Switching to live Coinbase market data
```bash
MARKET_DATA_MODE=coinbase python -m meridian.cli check-coinbase   # shows which products each asset resolved to, plus a candle/book fetch
```
If an asset resolves to the wrong contract, pin it with `MERIDIAN_PRODUCT_OVERRIDES='{"GOLD":"<product_id>"}'`.
Set `MARKET_DATA_MODE=coinbase` and restart. Simulated instruments are then marked UNAVAILABLE and no longer used.
