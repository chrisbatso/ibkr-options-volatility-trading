# Improvement candidates, written before any of them was tested

Fixed on 2026-11-05, after `RESULTS.md` showed the frozen rules losing 20% of premium per trade with about zero before
costs. These are new hypotheses on data whose overall result is already known, so they are exploratory, and any survivor
still needs a fresh test on data not used here. No subgroup or holding-period outcome has been looked at yet.

## Why these and no others

The result has two parts, so each candidate attacks one:

- **Costs are 13% per round trip**, charged per trade whatever the holding period. Paying once and holding longer, or
  trading only tight spreads, should keep more of whatever exists.
- **The bot never looks at implied vol.** Published work (Goyal and Saretto, 2009) finds straddle returns line up with
  the gap between historical and implied vol. That hypothesis comes from outside this data, and it is the gap the first
  review of this project pointed at.

## Candidates

All use the bot's signal (daily move above 2%), its stock list, and ATM straddles on the nearest expiry at least 7 days
out. `cheap` and `rich` compare implied vol, fitted to the straddle's mid, with trailing 21-day realized vol **excluding
its largest day**, because `RESULTS.md` found one-off jumps dominate the raw number.

| Name | Change | Attacks |
|---|---|---|
| **V1 tight** | Only round-trip cost up to 5% of premium; next-close hold | Costs |
| **V2 expiry** | Hold to expiry and settle at intrinsic, so the exit spread is never paid | Costs |
| **V3 cheap** | V2, and only when implied is below that realized vol | Implied vol |
| **V4 all** | V1 and V3 together | Both |
| **V5 sell rich** | Sell instead of buy, when implied is at least 1.5 times that realized vol; hold to expiry | Implied vol |

V5 is naked short options. It is included because long options on single stocks are known to pay a premium, and it can
only be offered with its tail risk reported next to its mean.

## Costs and fills

As in `PLAN.md`: limit at mid plus or minus 2%, assumed filled, which is generous on wide spreads. $2.60 commission per
straddle, kept for expiry holds as an allowance for exercise and closing the stock. Expiries after the price data ends
(10 September 2026) are skipped; a split before expiry is skipped and counted.

## Test

- **Null control first.** All candidates go through the fake market with no edge: gross must be indistinguishable from
  zero, including after the implied-vol filters, which would expose any look-ahead in them. With true vol 25% above
  implied, long candidates must show positive gross.
- **Power next**, printed before any mean: can each candidate detect an effect on the validation half?
- **Development:** June 2023 to December 2024. **Validation:** January 2025 to September 2026.
- Trades are averaged per day, every qualifying trade counted rather than three a day, for sensitivity. Holds to expiry
  overlap, so intervals come from a bootstrap of 5-week blocks.

## Rule for passing

With five candidates, intervals are 99% (Bonferroni, 0.05 / 5). A candidate passes only if **all** of these hold:

1. Development mean, net of costs, above 0.
2. Validation 99% interval, net of costs, entirely above 0.
3. Its null control passed.
4. Enough trades to matter: at least 200 in validation.

Thresholds (5%, 1.0, 1.5) are fixed here and will not be changed after seeing results. A candidate that passes earns a
paper trial, not live money. If none passes, the answer is that these changes do not rescue the strategy, and no further
variants get tried on this window.

## Addendum, written after V5's result and before any short-list quote was analysed

V5 passed the rule above at the assumed fill of mid -2%, and failed when sold at the bid (`RESULTS.md`). Spreads on the
long list are wide, so the natural next question is whether selling rich implied vol works **where spreads are tight**.
The short list is a fair place to ask: it is different stocks, more liquid, and its quotes have not been looked at.

**One test, fixed now.** On the short list, on the bot's short signal (daily move below 0.5%): sell the ATM straddle when
implied vol **fitted to the bid** is at least 1.5 times trailing 21-day realized vol excluding its largest day. Sold **at
the bid**, not at the mid, and held to expiry. Same costs. Same split at January 2025.

**Pass:** development mean above 0, validation 99% interval above 0, at least 200 validation trades. Tail risk is reported
next to the mean. Passing earns a paper trial only. The window is the same as before, so this is new evidence across
stocks, not across time.
