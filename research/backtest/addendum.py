# ruff: noqa: E501 - report and log lines read better whole
"""The one test fixed in the addendum to PLAN_IMPROVEMENTS.md: does selling rich implied vol survive at the bid on the
short list, where spreads should be tighter?

addendum.py null      markets with no edge: must pass before anything else is read
addendum.py power     sample sizes and standard errors only, no means
addendum.py results   development and validation, with the tail next to the mean
"""

import math
import sys
from collections import Counter
from pathlib import Path

import evaluate as E
import improve as I
import numpy as np
import pandas as pd
import stress as S

DATA = Path(__file__).parent / "data"
TRADES = DATA / "addendum_trades.csv"
RICH = I.RICH
SPLIT = I.SPLIT


def collect(market, list_side="short"):
    """Every short-signal entry the download covers, sold at the bid and held to expiry, rich or not."""
    rows, counts, cache = [], Counter(), {}
    for day in market.calendar:
        week = market.week_of.get(day)
        if week is None or (week, list_side) not in market.lists:
            continue
        for ticker, root in market.lists[(week, list_side)]:
            if (week, ticker) not in market.fetched or day not in market.closes[ticker]:
                continue
            move = E.signal_on(market.closes[ticker], market, day)
            if math.isnan(move) or abs(move) >= E.SHORT_THRESHOLD:
                continue
            realized = I.trailing_vol(market, ticker, day, cache)
            if not realized:
                counts["no trailing vol"] += 1
                continue
            trade = E.enter(market, "short", ticker, root, day, counts)
            if trade is None:
                continue
            trade.fill = trade.bid  # the addendum sells at the bid, not at the bot's limit
            implied = E.implied_vol(trade.bid, trade.spot, trade.strike, (trade.expiry - day).days / 365)
            if implied is None:
                counts["no implied vol at the bid"] += 1
                continue
            settled = I.settle_at_expiry(market, trade, "short", counts)
            if settled is None:
                continue
            rows.append(
                {
                    "day": day,
                    "ticker": ticker,
                    "expiry": trade.expiry,
                    "implied_bid": implied,
                    "realized": realized,
                    "ratio_bid": implied / realized,
                    "half_spread": (trade.mid - trade.bid) / trade.mid,
                    "premium_usd": trade.bid * 100,
                    "notional_usd": trade.strike * 100,
                    "spot": trade.spot,
                    "strike": trade.strike,
                    "bid": trade.bid,
                    "mid": trade.mid,
                    **settled,
                }
            )
    return pd.DataFrame(rows), counts


def chosen(trades):
    return trades[trades.ratio_bid >= RICH]


def summarize_markets(label, markets):
    """Each fake market is one observation, because trades inside one market share its paths and overlap."""
    rows = []
    for market in markets:
        trades, _ = collect(market, list_side="long")
        selected = chosen(trades)
        rows.append(
            {
                "entries": len(trades),
                "selected": len(selected),
                "all gross": trades.gross.mean(),
                "selected gross": selected.gross.mean(),
                "selected net at bid": selected.net.mean(),
                "half spread": selected.half_spread.median(),
            }
        )
    frame = pd.DataFrame(rows)
    print(
        f"{label}: {len(frame)} markets, {frame.entries.mean():.0f} entries and {frame.selected.mean():.0f} selected a market, median half-spread {frame['half spread'].mean():.1%}"
    )
    for column in ("all gross", "selected gross", "selected net at bid"):
        values = frame[column].dropna()
        error = values.std(ddof=1) / math.sqrt(len(values)) if len(values) > 1 else math.nan
        print(f"  {column:20} mean {values.mean():+7.2%}   standard error {error:.2%}   markets {len(values)}")


def run_null():
    # Fake prices start at 1,000, far above the $200 strike the notional cap allows. Lifted, or almost nothing trades.
    E.MAX_NOTIONAL = math.inf
    seeds = range(401, 413)
    summarize_markets(
        "Constant vol, implied = true (gross must be 0)",
        (E.synthetic_market(vol_ratio=1.0, seed=seed, names=30) for seed in seeds),
    )
    summarize_markets(
        "Constant vol, true vol 60% of implied (selected gross must be clearly above 0)",
        (E.synthetic_market(vol_ratio=0.6, seed=seed, names=30) for seed in seeds),
    )
    summarize_markets(
        "Vol regimes change, fair quotes with 8% noise and 12% half-spread, as on thin options",
        (S.noisy_market(seed) for seed in seeds),
    )
    summarize_markets(
        "Vol regimes change, fair quotes with 3% noise and 3% half-spread, as on liquid options",
        (S.noisy_market(seed, quote_noise=0.03, half_spread=0.03) for seed in seeds),
    )


def run_power():
    trades, counts = collect(E.load_market("short"))
    trades.to_csv(TRADES, index=False)
    print("counts:", dict(counts))
    selected = chosen(trades)
    print(
        f"short-signal entries {len(trades)}; selected {len(selected)} ({len(selected) / len(trades):.1%}); ratio of bid implied to trailing vol, median {trades.ratio_bid.median():.2f}"
    )
    print(
        f"half-spread, median: all entries {trades.half_spread.median():.1%}, selected {selected.half_spread.median():.1%}"
    )
    development, validation = selected[selected.day < SPLIT], selected[selected.day >= SPLIT]
    series = I.per_day(validation, "net")
    error = series.std() / math.sqrt(len(series)) if len(series) > 1 else math.nan
    print(
        f"trades: development {len(development)}, validation {len(validation)} (at least {I.MIN_VALIDATION_TRADES} needed); validation days {len(series)}; per-day SE {error:.2%}, detectable about {2.8 * error:.1%}"
    )


def run_results():
    trades = pd.read_csv(TRADES, parse_dates=["day", "expiry"])
    selected = chosen(trades)
    development, validation = selected[selected.day < SPLIT], selected[selected.day >= SPLIT]
    print(f"Sold at the bid, held to expiry. 99% intervals, 5-week blocks. Split at {SPLIT:%Y-%m-%d}.")
    for label, part in (("development", development), ("validation", validation)):
        net, gross = I.per_day(part, "net"), I.per_day(part, "gross")
        low, high = I.interval(net)
        months = part.groupby(part.day.dt.to_period("M")).usd.sum()
        print(
            f"  {label:12} net {net.mean():+7.2%} ({low:+7.2%} to {high:+7.2%})   before costs {gross.mean():+7.2%}   won {(part.usd > 0).mean():5.1%}   months up {(months > 0).sum()}/{len(months)}   $ per trade {part.usd.mean():+7.0f}   n {len(part)}"
        )
    low, _ = I.interval(I.per_day(validation, "net"))
    passed = I.per_day(development, "net").mean() > 0 and low > 0 and len(validation) >= I.MIN_VALIDATION_TRADES
    print(f"  passes the rule fixed in the addendum: {'YES' if passed else 'no'}")

    day_usd = selected.groupby("day").usd.sum()
    ranked = selected.usd.sort_values(ascending=False)
    print(
        f"\nTail: worst trade {selected.usd.min():+,.0f} USD, {(selected.usd / selected.notional_usd).min():.1%} of notional, {(selected.usd / selected.premium_usd).min():.0%} of premium; worst day {day_usd.min():+,.0f}; mean of the worst 5% of days {day_usd.nsmallest(max(1, len(day_usd) // 20)).mean():+,.0f}; mean of the worst 5% of trades {selected.usd.nsmallest(max(1, len(selected) // 20)).mean():+,.0f}"
    )
    print(
        f"Loss beyond the premium {(selected.usd < -selected.premium_usd).mean():.1%} of trades, beyond 3x premium {(selected.usd < -3 * selected.premium_usd).mean():.1%}"
    )
    print(
        f"Concentration: total {selected.usd.sum():+,.0f} USD; 10 best {ranked.head(10).sum():+,.0f}; the other {len(ranked) - 10} {ranked.iloc[10:].sum():+,.0f}"
    )
    for year, part in selected.groupby(selected.day.dt.year):
        print(
            f"  {year}: net {part.net.mean():+7.2%}, before costs {part.gross.mean():+7.2%}, won {(part.usd > 0).mean():.1%}, n {len(part)}, total {part.usd.sum():+,.0f} USD"
        )

    bets = (
        selected.groupby(["ticker", "expiry"])
        .agg(day=("day", "min"), usd=("usd", "mean"), net=("net", "mean"))
        .reset_index()
    )
    print(
        f"\nSeparate bets: {len(selected)} trades are {len(bets)} ticker-and-expiry bets in {selected.ticker.nunique()} tickers; top 5 tickers' share of trades {selected.ticker.value_counts().head(5).sum() / len(selected):.1%}"
    )
    for label, part in (("development", bets[bets.day < SPLIT]), ("validation", bets[bets.day >= SPLIT])):
        print(f"  counted once each, {label}: net {I.per_day(part, 'net').mean():+7.2%}, n {len(part)}")

    print(
        "\nDoes it scale with richness? Descriptive, all short-signal entries, by ratio of bid implied to trailing vol"
    )
    buckets = pd.cut(trades.ratio_bid, [0, 0.75, 1.0, 1.25, 1.5, 2.0, 99], right=False)
    for bucket, part in trades.groupby(buckets, observed=True):
        early, late = part[part.day < SPLIT], part[part.day >= SPLIT]
        print(
            f"  {str(bucket):14} development before costs {early.gross.mean():+7.2%} net {early.net.mean():+7.2%} n {len(early):5} | validation before costs {late.gross.mean():+7.2%} net {late.net.mean():+7.2%} n {len(late):5}"
        )

    margin = 0.2 * selected.spot * 100 + selected.premium_usd
    print(
        f"\nApproximate margin per trade: median {margin.median():,.0f} USD; profit on margin: mean {(selected.usd / margin).mean():+.1%}, worst {(selected.usd / margin).min():+.0%}"
    )
    print(
        f"Median half-spread on these trades {selected.half_spread.median():.1%}; share of trades with it under 5% {(selected.half_spread < 0.05).mean():.1%}"
    )
    print(
        f"Sanity: no selected trade has a missing value: {not np.isnan(selected[['net', 'gross', 'usd']].to_numpy()).any()}"
    )


if __name__ == "__main__":
    {"null": run_null, "power": run_power, "results": run_results}[sys.argv[1]]()
