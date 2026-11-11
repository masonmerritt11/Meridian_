# Known limitations (Milestone 1)

This is not production-ready for real-money decisions until the items marked **must verify** are done.

## Data
* **Coinbase adapter not verified against the live API (must verify).** The build environment could not reach api.coinbase.com. The adapter follows Coinbase's published OpenAPI for the public `/market/*` endpoints and is tested against fixtures shaped from that spec. Run `python -m meridian.cli check-coinbase` from Railway or your machine before switching `MARKET_DATA_MODE=coinbase`.
* **Product discovery is heuristic (must verify).** Assets are matched on `contract_root_unit` and contract names. Confirm each of the 6 resolved products (especially oil and silver) and pin with `MERIDIAN_PRODUCT_OVERRIDES` if needed. Unmatched assets show as UNAVAILABLE rather than being guessed.
* **Futures margin is not available from the public API.** Until you enter `margin_per_contract`, futures ideas are RESTRICT: MARGIN_UNVERIFIED and capital-usage checks cannot be enforced (the 10× gross-notional ceiling still is).
* **Macro calendar** uses the free, unofficial Fair Economy (ForexFactory) weekly JSON feed. It covers the current week only and has no "actual" values. It was also unreachable from the build environment, so it has been tested only against parsed fixtures. When unavailable, the system shows UNAVAILABLE and risk RESTRICTs; nothing is invented. A second, official source (Fed/BLS/BEA calendars) is a planned improvement.
* **Simulated data** is a deterministic random walk with a scripted breakout scenario for the demo. Contract specs on SIM- instruments are illustrative round numbers, not exchange specifications.
* Candles are hourly only. The trend strategy is a single, unoptimized rule set that has **not been backtested**. Its signals are research inputs, not a validated edge.

## Risk and portfolio
* Equity = recorded capital + realized P&L from positions you record. MERIDIAN does not see your broker account, so unrecorded trades, fees and funding are invisible to it.
* Daily/weekly loss limits use realized P&L only (not unrealized).
* Total open risk is measured to your recorded stops. A stop moved at the broker but not recorded in MERIDIAN will make it wrong.
* Gaps through a stop can lose more than the planned $70; the engine sizes to the stop and cannot prevent slippage.
* Assumptions I chose (change in Risk Command): total open risk 10%, correlated risk 10%, daily 10%, weekly 20%, notional ceiling 10×, event blackout −60/+30 min.

## Analysts and research
* Analyst rules are reasonable textbook measures (EMA alignment, RSI, Bollinger squeeze, efficiency ratio, z-scores, EWMA vol), not
  backtested signals. Their votes move the committee score; the recommendation track record (Attribution worker) is how we will learn
  which ones help.
* The basis and carry analysts use the exchange index price captured at discovery (every 6 hours), so they lag.
* Trading fees are not in the public API; the trading-cost analyst counts the spread only.
* AI researchers and macro analysts can still misread a source. Their sources are listed so you can check them, invented URLs are
  dropped, and macro views without retrieved sources get almost no weight.
* The AI spend cap is checked before each call; a single long call can overshoot it by up to about $0.40.
* If web search is disabled on the Anthropic account, researchers fall back to arXiv abstracts (narrower reading).

## Platform
* Workers run in-process; deploy with exactly one replica.
* Docker Compose and the Railway build were not executed in the build environment (no Docker daemon). The same installed package was run in production mode against local Postgres/Redis and the static dashboard served correctly.
* No password reset UI; manage users via the CLI. No MFA.
* 1 of 55 roles is planned (cross-venue arbitrage: needs price feeds from other exchanges).
* No LLM components yet; when added, they must remain commentary-only within fixed schemas.
