"""Why does "exit at intrinsic" fire? Looks only at data availability, never at P&L."""

from collections import Counter

import evaluate as E

market = E.load_market("long")
cases, reasons, total = [], Counter(), 0
for day in market.calendar:
    week = market.week_of.get(day)
    if week is None or (week, "long") not in market.lists:
        continue
    for ticker, root in market.lists[(week, "long")]:
        if (week, ticker) not in market.fetched or day not in market.closes[ticker]:
            continue
        trade = E.enter(market, "long", ticker, root, day, Counter())
        if trade is None:
            continue
        total += 1
        at = market.position[day]
        if at + 1 >= len(market.calendar):
            continue
        next_day = market.calendar[at + 1]
        chain = market.chains.get((root, trade.expiry, next_day))
        quote = (chain or {}).get(trade.strike)
        if quote is not None and max(quote) > 0:
            continue
        has_price = next_day in market.closes[ticker]
        if not has_price:
            why = "no stock price next day (delisted or halted)"
        elif chain is None:
            next_week = market.week_of.get(next_day)
            why = "expiry has no rows next day" + (" (next week)" if next_week != week else " (same week)")
        elif quote is None:
            why = "strike missing next day, expiry present"
        else:
            why = "quote all zeros next day"
        reasons[why] += 1
        cases.append(
            (
                str(day.date()),
                str(next_day.date()),
                ticker,
                root,
                str(trade.expiry.date()),
                trade.strike,
                why,
                len(chain or {}),
            )
        )
print("entries checked", total, "| exits without a usable quote", len(cases), f"({len(cases) / max(total, 1):.2%})")
print(reasons.most_common())
for case in cases[:14]:
    print(case)
