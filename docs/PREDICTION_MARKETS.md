# Prediction Desk (Kalshi 15-minute BTC up/down)

Advisory only. MERIDIAN reads Kalshi's **public** market data (no key, GET-only allowlist, order and portfolio
paths refused) and Coinbase's public BTC-USD price. The CIO places every order.

## Department: Prediction Markets (4 roles)
| Role | What it does |
|---|---|
| Live Odds Desk (`pm.live_desk`) | Every `PM_POLL_SECONDS` (default 2s): reads the open window and live BTC, prices the window from BTC, raises and manages setups. |
| Odds Historian (`pm.historian`) | Every 30 min: backfills settled windows (1-minute bid/ask bars, outcome) for `PM_BACKFILL_DAYS` (30) plus BTC 1-minute closes. |
| Strategy Lab (`pm.strategy_lab`) | Hourly: tests ~3,000 entry/exit rules on older 70% of windows, verifies the best distinct ones on the newest 30%; studies calibration, last-minute flips, the underdog bounce and BTC-model vs market accuracy. |
| Prediction Risk Officer (`pm.risk_officer`) | Hard rules, enforced in code and by a database trigger. |

## Hard rules
- Worst-case loss per setup ≤ `PM_MAX_LOSS_USD` ($70): premium plus Kalshi taker fee.
- One setup of each kind per window; no new setups in the final 4 minutes.
- Every setup exits at least 2 minutes before close (DB trigger `pm_signal_exit_rule`). Never hold through close.

## Signals
- **Underdog underpriced vs real BTC** (`ALIGNED_UNDERDOG`): minutes 2-10, the underdog's ask is at least 3¢ below the
  BTC-implied fair price after fees. Sell target = fair price at entry; exit by minute 12 regardless.
- **Strategy setup** (`STRATEGY`): the top PROVEN rule from the lab; until one exists, Mason's playbook
  (underdog 25-40¢ in minutes 5-8, sell at 50¢, exit at minute 12), marked UNPROVEN.

## Fair value model
`P(UP) = Φ(ln(BTC/strike) / (σ·√V))`, σ = per-second vol from the last hour of 1-minute closes,
V = τ−40 s when τ > 60 s, else (τ/60)²·τ/3 (the settlement is a 60-second average, so the final minute only partly counts).
BTC comes from Coinbase spot, a proxy for the settlement index (CF Benchmarks BRTI), so small differences are expected.

## Backtest honesty
Entry at the ask + taker fee `0.07·P·(1−P)`; take-profit counted only if the bid trades 1¢ *through* the target;
time exits sell at the bid and pay the fee. PROVEN = profitable on never-seen newer windows, ≥30 trades, t ≥ 1.65.
On random data the lab finds zero proven rules.

## Settings
`PM_ENABLED` (true), `PM_SERIES` (KXBTC15M), `PM_POLL_SECONDS` (2), `PM_BACKFILL_DAYS` (30), `PM_MAX_LOSS_USD` (70).
Runs only with `MARKET_DATA_MODE=coinbase`.
