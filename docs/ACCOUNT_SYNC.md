# Coinbase account sync (read-only)

MERIDIAN reads your Coinbase futures account every minute and keeps its records in sync: positions opened, added to,
partly closed, closed or flipped; your real stop and take-profit (from resting orders); exact exit prices and fees
(from fills). Each new position is linked to the matching recommendation from the last 24 hours.

## Safety
1. The API key has **View** permission only, so Coinbase refuses any order.
2. Every sync calls `GET /key_permissions` and stops if the key can trade or transfer.
3. MERIDIAN's account client sends only GET requests to five read endpoints
   (key_permissions, cfm/positions, orders/historical/batch, orders/historical/fills).

## Setup
1. Coinbase Developer Platform -> API keys -> Create API key. Permissions: **View** only. Signature algorithm: **ECDSA**
   (recommended for Advanced Trade; Ed25519 also works).
2. In Railway -> MERIDIAN service -> Variables, add:
   - `COINBASE_API_KEY_NAME` = the key name, e.g. `organizations/…/apiKeys/…`
   - `COINBASE_API_KEY_SECRET` = the private key (paste the whole `-----BEGIN EC PRIVATE KEY----- … -----END …` block)
3. Railway redeploys. The dashboard's "Open positions" card shows "Synced from your Coinbase account" within a minute.

If there's no stop order on Coinbase, the stop is assumed at the per-trade loss limit and flagged.
Manual recording still works as a backup.
