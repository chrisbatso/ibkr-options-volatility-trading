# Backtest plan, written before any option outcome was seen

Fixed on 2026-11-05. Everything measured so far is prices and quote alignment, no straddle P&L. The author already
expected this strategy to fail (see the review of 5 November), which is why the rules below are written first and why
the controls can contradict that expectation.

## The question

Would the bot's frozen rules have made money after costs, June 2023 to September 2026? Nothing is tuned. A rule that
fails is reported as failing, not adjusted and rerun on the same data.

## Data

- **Universe:** the bot's own `screen()` on Sharadar prices, which keep delisted stocks, refreshed every Friday close
  for the following week. Refresh day is an assumption: the bot leaves it to the user.
- **Options:** Theta Data end-of-day bid and ask, free tier, which starts in June 2023. Audited against Sharadar closes:
  parity-implied spot is 0.16% from that day's close on moves over 8%, against 22.6% from the day before.
- **Not available:** intraday option quotes (paid plan). The bot's real trade, in and out on the same day, therefore
  cannot be replayed from quotes. Tests A and B below are what can be done instead.
- `has_options` is "the symbol has ever had options", and a stock with no chain that week is then dropped and counted.

## Frozen rules (`.env.template` defaults)

Signal above 2% for long, below 0.5% for short. Three new positions a day, in file order, which is alphabetical. One per
ticker. Nearest expiry at least 7 days out, at-the-money strike, lower strike on a tie. One contract. Premium cap
$1,000 for long, notional cap $20,000 for short: a capped stock uses no slot and the next one is tried. Commission
$2.60 per round trip (four legs at $0.65).

## Test A: next-close hold, real quotes at both ends (primary)

Signal from close-to-close move on day t. Buy at the close of t, sell at the close of t+1.

- **Entry:** `min(ask, mid x 1.02)`, the bot's limit, assumed filled. Both legs need a bid and an ask, or the stock is
  skipped without using a slot.
- **Exit:** the straddle's bid, as the bot does at the close.
- **Gross:** mid to mid, no costs.
- **Guards, all counted:** skip if a split falls between entry and exit, or if parity spot is more than 15% from
  Sharadar's close (a converted or adjusted contract). If the exit quote is missing, value at intrinsic on the next
  Sharadar close, so that halts and delistings are not silently dropped.

This differs from the bot, which would exit on day t. A one-day hold gives the straddle more movement to earn from
against the same spread, so it is a fair reading of the economics, not a replay. Take-profit and stop-loss cannot be
seen with closes only.

## Test B: same-day, modelled entry, deliberately tilted in the bot's favour

The bot's first poll is at the open. Candidates are stocks opening more than 2% from the previous close, first three
alphabetically, strike nearest the open. **Exit is the real quote** at the close of the same day. **Entry is modelled:**
Black-Scholes at the open price, using the implied vol fitted to that strike's closing mid, plus the part of a calendar
day between open and close.

Tilts in the bot's favour, on purpose: no loss from implied vol falling during the day, closing spreads instead of the
wider ones at the open, fills assumed at the limit, and take-profit credited whenever the day's high or low would have
reached it, regardless of path. A second version, B2, uses the previous close's implied vol for entry and so includes
the overnight repricing.

Test B can only reject. If it loses even so, the same-day trade has no room. If it does not, that proves nothing and
intraday data would be needed.

## Controls

- **C1, does the signal add anything?** Same stocks, same weeks, days with no signal, same hold, mid to mid. The signal
  should beat this. Compared on matched weeks.
- **C2, synthetic null.** Fake quotes with implied vol equal to true vol, run through the same evaluator. Gross P&L must
  be indistinguishable from zero. This runs before results are read: a look-ahead or sign bug would show here.
- **C3, the alphabet.** All signals in the downloaded names against the alphabetical three. Descriptive.

## Inference

Unit: return on premium per trade, averaged per day. 95% interval from a weekly block bootstrap (5,000 draws), with a
Newey-West t alongside. Also dollars per day at one contract each.

**Power first.** Minimum detectable effect, 2.8 x standard error of the daily series, is computed and printed before
any mean, and compared with the measured round-trip cost. If the test cannot see an effect the size of its own costs,
the result is reported as uninformative whatever it says.

## Decision rules

| Result on test A, long side, net | Conclusion |
|---|---|
| Upper end of the interval below 0 | Loses money in this form |
| Lower end above 0, at least 3 of 4 calendar years positive, and it beats C1 | Worth paying for intraday data to test the real trade |
| Anything else | Inconclusive, stated as such |

- **Test B:** interval upper end below 0 on B1 means the same-day trade has no room even when tilted its way. Otherwise,
  no conclusion.
- **Short side** is secondary because it is off by default. Same rule, plus worst trade, worst day and the mean of the
  worst 5% of days, because a good average on naked options can hide the risk that matters.

Only one primary test is run on this window, so no multiple-testing correction is applied to it. Everything else is
labelled descriptive. There is no holdout, because nothing is fitted. A failed rule cannot be rescued on this data.
