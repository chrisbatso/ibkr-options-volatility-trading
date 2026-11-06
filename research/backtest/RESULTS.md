# Backtest results

Rules and pass/fail criteria are in `PLAN.md`, fixed before any option P&L was seen. Window: 5 June 2023 to 9 September
2026. Reproduce with `evaluate.py null`, `evaluate.py power` and `evaluate.py results`. The raw output is in
`data/results_long.txt`, which stays out of git. The short side, which also loses money, is in its own section at the end.

## Verdict, long side (the bot's default)

**It loses money, and costs are not the only problem: before costs there is no edge either.**

| Test | Net of costs, per trade | Before costs, per trade | Rule in `PLAN.md` |
|---|---|---|---|
| **A: next-close hold, real quotes** | **-20.2%** (95% interval -21.6% to -18.9%) | +0.33% (-0.54% to +1.28%) | Loses money in this form |
| **B: same-day, modelled entry, tilted to the bot** | **-14.3%** (-15.6% to -12.9%) | +3.58%, see below | No room even when tilted its way |

Test A in dollars, at one contract per trade: 2,443 trades, -$141,186 in total, about -$43,000 a year.
**0 of 40 months made money.** The best month lost $1,132. 6.8% of days made money. The loss is a steady bleed, not a
crash: the worst single day was -$874, and the $500 daily loss limit would have cut some days short without changing
the picture.

| Year | Net, per trade | Before costs, per trade | Median round-trip cost |
|---|---|---|---|
| 2023 (from June) | -15.7% | +1.37% | 10.1% |
| 2024 | -18.7% | +0.09% | 11.4% |
| 2025 | -20.4% | +0.12% | 12.7% |
| 2026 (to September) | -26.0% | +0.13% | 18.0% |

## Why

- **Options on these stocks are priced about right.** Before costs the straddle is worth about what was paid a day
  later. The median trade loses 3.1% and 37.8% of trades are up, so the small positive mean comes from a few large
  winners, as it should for a fairly priced long option.
- **The signal adds nothing (C1).** Signal days minus no-signal days, same stock and week, before costs: -0.96%
  (-2.22% to +0.29%). A big move today does not make the straddle a better buy tomorrow.
- **The spread decides the result.** Median round-trip cost is 13.0% of premium, because the screen picks volatile,
  mostly small stocks with thin options. The median premium is $226, so the $2.60 commission is another 1%.
- **The alphabet is arbitrary but harmless (C3).** Every signal in the downloaded names nets -20.4%, the same as the
  bot's three (-20.2%). 79% of the bot's trades were in stocks starting with A. That is a poor way to choose, but
  choosing better among these signals would not have helped.

## How far to trust this

- **Power:** the test can detect 1.43% of premium per trade (2.8 standard errors), far less than the costs it measures.
- **Null control, Test A:** on fake quotes with implied vol equal to true vol, gross is -0.09% (t -1.48), so it finds
  nothing where there is nothing. With true vol 25% above implied, gross is +3.95% (t +50.7), so it finds an edge where
  there is one.
- **Null control, Test B:** see below. This one matters.
- **Cost estimate:** Theta's end-of-day spreads may be wider than during the day, which would overstate costs. The
  verdict does not rest on that. Test A's gross is +1.28% at the top of its interval, and only 0.12% of trades cost less
  than that. Halving every spread would still leave a median cost of about 7.5%.
- **Data:** parity-implied spot is 0.16% from that day's close on moves over 8%, and 22.6% from the day before. None of
  the 8,023 entries checked lacked an exit quote, so nothing was valued by the intrinsic fallback. The universe comes
  from the bot's own `screen()` on prices that include delisted stocks.

### Test B's +3.58% before costs is not an edge

Test B is deliberately tilted, and the null control measures by how much. Run through a market with **no edge at all**,
it reports +3.49% before costs if 30% of daily variance arrives overnight, and +1.44% if 60% does. Real data gives
+3.58%. So the tilts alone produce between 40% and nearly all of it. Stocks that gap probably do get more of their variance
overnight, so a real same-day residual of up to about 2% cannot be ruled out, but it would still be far below the 13%
cost. Only 1.84% of trades cost under 3.58%.

## What this does not show

- **The bot's actual trade was not replayed.** Intraday option quotes need a paid plan. Test A holds a day longer than
  the bot, and Test B models the entry. To reverse this verdict, real intraday data would have to show a same-day gross
  edge large enough to cover a double-digit cost, which nothing here hints at.
- **It covers one stretch of market.** June 2023 to September 2026 has no 2020 or 2022. Long volatility can pay in a
  crash, and this window cannot speak to that.
- **Fills are assumed.** Entries fill at the limit, which is generous, because a limit at mid +2% may simply not fill
  when the spread is 20%.
- **The weekly refresh is an assumption.** The bot leaves re-screening to the user.

## Deviations from the plan

- **Test B's null control was added after its result was seen**, because the +3.58% could not be read without it. It can
  only weaken the result, and it did. No rule was changed.
- **Test A's null control was first run on a faulty fake market**: strikes changed daily, which pushed trades into the
  intrinsic fallback and gave -31%. That was fixed before any real result was read.
- A crash in the bootstrap helper interrupted the first results run. The fix changed no calculation, and the numbers
  above come from the complete rerun.
- **V5's null controls first ran with the $20,000 notional cap still on.** Fake prices start at $1,000, so the cap removed
  most trades (36 a market). It was found while writing the short-side test, and the controls were rerun with it lifted,
  giving about 1,500 trades a market in the noisy-quote control. Its result barely moved: +1.39% before costs became +1.45%,
  with the standard error down from 2.40 to 0.83. The real-data results did not use these controls and are unchanged.

## Improvements tested

Five changes were fixed in advance in `PLAN_IMPROVEMENTS.md`, chosen on 2023-24 and judged on 2025-26, with 99% intervals
because there are five. **None of them makes the strategy reliably profitable.** This is exploratory: the overall result
for this window was already known, so anything that survives still needs fresh data.

| Candidate | 2023-24, net per trade | 2025-26, net per trade (99% interval) | Before costs, both periods | Trades, both periods |
|---|---|---|---|---|
| V1: spread cost up to 5% only | -8.2% | -5.1% (-10.0% to -0.1%) | -2.2%, +1.8% | 255, 109 |
| V2: hold to expiry | -10.9% | -6.8% (-16.7% to +8.1%) | -7.7%, -3.7% | 2,647, 2,400 |
| V3: V2, only when implied looks cheap | -6.1% | -2.8% (-21.3% to +28.9%) | -2.8%, +0.4% | 1,015, 1,056 |
| V4: V1 and V3 together | -7.7% | +4.9% (-22.6% to +40.6%) | -5.0%, +7.9% | 117, 52 |
| **V5: sell when implied looks rich, at mid -2%** | **+17.3%** | **+21.6% (+8.4% to +35.9%)** | +20.1%, +24.1% | 364, 289 |

### V5 passes the rule as written, and should not be believed yet

**The rule assumed a fill at mid -2%, on options whose half-spread has a median of 13.7%.** That assumption carries it:

| Same trades, sold at | 2023-24, net per trade | 2025-26, net per trade (99% interval) |
|---|---|---|
| Mid -2%, as tested | +17.3% | +21.6% (+8.4% to +35.9%) |
| The bid | -12.3% | -16.7% (-34.1% to +3.0%) |
| The bid, and chosen only if rich at the bid (151 and 104 trades) | +2.9% | +2.9% (-25.3% to +35.2%) |

- **Break-even** is about a quarter of the way from the bid to the mid. Historical closing quotes cannot show whether real
  orders get that, which is why the bot now keeps a journal.
- **At the bid it rests on a handful of trades.** Total +$25,029, the 10 best trades +$30,630, the other 643 -$5,601.
  2026 alone is +$21,238.
- **There are fewer bets than trades.** 653 trades are 328 separate ticker-and-expiry bets. Counted once each, the net at
  the bid is -19.1% and -21.1%.
- **The tail is real.** `AAOI` went from 76.52 to 135.63 before expiry: one contract lost $4,176 on $1,690 of premium.
  `DAVE` doubled, from 40.28 to 80.06. 15.9% of trades lose more than their premium, and the worst 5% average -$1,246.
  This also ignores assignment and the cost of borrowing hard-to-borrow stock.

**What does hold up** is the direction, before costs. Selling gets better as implied gets richer, in both periods, which
matches the published work (Goyal and Saretto, 2009):

| Implied / trailing vol | 2023-24, before costs | 2025-26, before costs |
|---|---|---|
| Below 0.75 | +4.3% | +10.8% |
| 0.75 to 1.0 | +4.3% | +1.1% |
| 1.0 to 1.25 | +8.5% | +0.8% |
| 1.25 to 1.5 | +4.7% | +5.0% |
| 1.5 to 2.0 | +16.9% | +19.6% |
| 2.0 and up | +21.5% | +29.1% |

Selling quote errors does not explain this: in a fake market with no edge and noisy, wide quotes, V5 reports +1.45%
before costs (standard error 0.83) and -12.2% at the bid. It remains a lead on the thin options where it is hardest to
collect. The next test, fixed in the addendum to `PLAN_IMPROVEMENTS.md`, asked whether it survives at the bid on the short
list. It does not: see the short side below.

### Null controls for the candidates

The first run reported +2.47% before costs for V2 in a no-edge market, with an interval excluding zero. It used two fake
markets. Across 24 fresh ones V2 averages +0.56% (standard error 0.34), and single markets range from -1.8% to +6.0%, so
that was chance. It also shows the bootstrap interval can run about 30% narrow on holds to expiry. V5's lower bound at
mid -2% stays positive even after widening it by that much. V1 to V4 are consistent with zero across those markets. V5
needed a rerun (see the deviations below) and reports -1.08% before costs (-9.05% to +6.22%) over 1,511 trades in two fake
markets, also consistent with zero. The noisy-quote control above is still the stronger test of it.

### What changed in the bot

- **Spread guard** (`STRADDLE_MAX_SPREAD_PCT`, default 6% of mid, about the 5% round-trip cost of V1). It takes the loss
  from about 20% per trade to 5-8%. It reduces losses and does not create a profit.
- **Trade journal** (`TRADING_JOURNAL`): every order, fill and close with the quotes around it, which is what can measure
  real fills against the mid.
- **Not added:** selling rich vol, holding to expiry, or an implied-vol filter. None has earned it.

## Short side

**The frozen short rule loses money too, and selling rich vol failed its one test at the bid.** Neither direction works.
Reproduce with `evaluate.py null short`, `evaluate.py power short`, `evaluate.py results short`, then `addendum.py null`,
`addendum.py power` and `addendum.py results`. The raw output is in `data/results_short.txt`.

### Frozen rule, Test A (next-close hold)

| | Net of costs, per trade | Before costs, per trade | Rule in `PLAN.md` |
|---|---|---|---|
| **The bot's three a day** | **-32.8%** (95% interval -35.3% to -30.4%) | -1.04% (-2.19% to +0.05%) | Loses money in this form |

In dollars, at one contract per trade: 2,057 trades, -$196,607 in total, about -$63,000 a year. **0 of 40 months made
money.** The best month lost $1,532. 5.6% of days and 11.2% of trades made money.

| Year | Net, per trade | Before costs, per trade | Median round-trip cost |
|---|---|---|---|
| 2023 (from June) | -26.8% | -1.36% | 12.7% |
| 2024 | -30.2% | -1.40% | 13.6% |
| 2025 | -32.0% | -0.12% | 19.6% |
| 2026 (to September) | -43.3% | -1.57% | 32.0% |

- **There is no premium to collect over one day.** Before costs the median trade makes 2.0% and 55.9% are up, the usual
  shape for a seller, but the mean is about -1%.
- **The signal adds nothing (C1).** Signal days minus no-signal days, same stock and week, before costs: -1.25% (-3.03% to
  +0.43%).
- **The spread decides the result.** Median round-trip cost is 17.4% of premium and the mean is 26.9%.
- **The alphabet is arbitrary but harmless (C3).** Every signal in the downloaded names nets -32.7%.
- **Tail:** worst trade -$1,359 (18.1% of notional), worst day -$1,416, mean of the worst 5% of days -$927. The window has
  no crash, so these understate what naked options can lose.

How far to trust it:

- **Power:** the test can detect 3.10% of premium per trade, far less than the costs it measures.
- **Null control:** on fake quotes with implied vol equal to true vol, gross is -0.12% (-0.43% to +0.19%). With true vol 25%
  above implied, a seller's gross is -4.25% (-4.74% to -3.76%).
- **Cost estimate:** end-of-day spreads may overstate real ones, and median cost nearly triples from 2023 to 2026, which
  deserves suspicion. The verdict does not rest on it. With every spread halved, median cost is still about 10%, against
  about zero before costs.
- **Coverage:** on 267 of the 790 days the download ran out of names before reaching three picks, so those days have fewer
  trades than the bot would have made. C3 suggests the missing names would not change the picture.
- The limits listed for the long side also apply: the real intraday trade was not replayed, the window has no crash,
  fills are assumed and the weekly refresh is an assumption. Test B is defined for the long side only.

### Selling rich vol at the bid, the test fixed in the addendum

| Period | Net at the bid, per trade (99% interval) | Before costs, per trade | Trades |
|---|---|---|---|
| 2023-24 | +5.0% (-7.3% to +19.3%) | +16.6% | 724 |
| 2025-26 | **-19.7%** (-43.6% to -0.7%) | +0.4% | 411 |

**It fails the rule**, which needs the 2025-26 interval entirely above 0. It does not earn a paper trial.

- **It is not only the spread.** Before costs, 2025-26 is +0.4%. Fake markets with no edge report +2.9% to +4.1% after the
  same selection on the bid, so this is no more than noise alone produces.
- **The pattern from the long list does not carry over.** There, selling improved as implied got richer in both periods.
  Here it does not, so the lead does not generalize across stocks:

| Bid implied / trailing vol | 2023-24, before costs | 2025-26, before costs |
|---|---|---|
| Below 0.75 | -5.1% | -0.3% |
| 0.75 to 1.0 | +5.1% | +10.5% |
| 1.0 to 1.25 | -4.9% | +7.0% |
| 1.25 to 1.5 | -6.2% | +11.4% |
| 1.5 to 2.0 | +18.1% | -1.7% |
| 2.0 and up | +9.7% | +14.3% |

- **The list is tighter, not tight, which the addendum got wrong.** Median half-spread on these trades is 8.1%, against 13.7%
  on the long list, and 27.2% of trades are under 5%. It rises from 6.4% in 2023-24 to 10.7% in 2025-26.
- **In dollars:** total -$12,766. The 10 best trades made +$7,642 and the other 1,125 lost $20,408.
- **There are fewer bets than trades.** 1,135 trades are 574 separate ticker-and-expiry bets. Counted once each, the net is
  -2.5% and -12.2%.
- **Tail:** worst trade -$2,063 (30.1% of notional, 4.5 times its premium), worst day -$2,186, mean of the worst 5% of days
  -$1,260. 10.4% of trades lose more than their premium.

How far to trust it:

- **Null control:** with no edge, constant vol gives -2.7% net at the bid (standard error 2.5), and fair quotes with changing
  vol give -9.2% (2.9) on thin options and -0.3% (2.0) on liquid ones. When selling really does pay (true vol 60% of implied),
  it reports +38.4% (0.4).
- **Hand check:** six random trades were rebuilt from the raw quote files and raw closes, and matched to the cent.
- **Power is limited.** The 2025-26 half can detect about 15% of premium, so this does not rule out a small edge. The
  bootstrap can also run about 30% narrow on holds to expiry, and widening by that much would take the interval across
  zero. So the test shows this does not pass, not that it is certain to lose.
- **Same window.** This is new evidence across stocks, not across time.

### Where this leaves the strategy

Both frozen rules lose money, with no edge before costs on either side. `PLAN_IMPROVEMENTS.md` says no further variants get
tried on this window, and none will be. Selling rich vol stays an unproven lead on the long list only, and it holds there
only at a fill of mid -2%, which real orders may not get. What could change this is new evidence: data after September
2026, and real fills from the bot's journal on a paper account. Order placement should stay off for real money.
