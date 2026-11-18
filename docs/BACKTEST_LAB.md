# Backtest Lab

Three workers (department "Backtest Lab"):

| Worker | Every | Does |
|---|---|---|
| History Loader (`bt.historian`) | 6h | Hourly charts, up to 730 days: BTC-USD, ETH-USD, XRP-USD spot; PAXG-USD for gold; each silver/oil futures contract on its own. |
| Strategy Lab (`bt.strategy_lab`) | 6h | Replays the live trend strategy exactly (same code path for signals and filters), tests ~45 small changes, files proposals. |
| Trade Tester (`bt.trade_tester`) | 15 min | Paper-tests every committee memo (RECOMMEND, BLOCKED, REJECT, DEFER) as if placed. |

## Replay rules
Signal on a closed hourly bar; entry at the next open; live stop/target levels; skip if the open is already past
either; one position per asset; stop counted first if a bar touches both; 10-day time exit; 0.1 ATR cost per trade.
Results in R (1R = $70).

## When is a change proposed?
Ranked on the older 70% of history, then checked on the newest 30%. All must hold:
>= 60 trades older / >= 30 newer, >= 3 months newer; better total and average R on both; worst losing run no more
than 25% deeper (+1R); bootstrap over months says >= 90% chance the improvement is real.
Accepting (password) changes `trend.signal_analyst` config (versioned, audited, revertible); other open proposals
are superseded and re-tested against the new live version.

## Recommendation paper-test
Filled if price trades at the entry within 12 hours. Reports winners/losers, how often stopped trades later reached
the target, how far losers ran first, how far winners ran past the target, a stop x target grid (each cell sized to
the same $70), and a move-stop-to-breakeven-at-+1R alternative. Turned-down ideas are tested too.
