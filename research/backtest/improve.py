# ruff: noqa: E501 - report and log lines read better whole
"""Tests the candidates in PLAN_IMPROVEMENTS.md on the quotes already downloaded for the long list.

improve.py null      the fake markets: must pass first
improve.py power     sample sizes and standard errors only, no means
improve.py results   development and validation
"""

import math
import random
import sys
from bisect import bisect_right
from collections import Counter
from pathlib import Path

import evaluate as E
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
SPLIT = pd.Timestamp("2025-01-01")
TIGHT_COST = 0.05
CHEAP, RICH = 1.0, 1.5
CONFIDENCE = 99  # Bonferroni for five candidates
BLOCK_WEEKS = 5
MIN_VALIDATION_TRADES = 200

CANDIDATES = {
    "V1 tight": ("long", "next", lambda t: t.cost <= TIGHT_COST),
    "V2 expiry": ("long", "expiry", lambda t: t.cost == t.cost),
    "V3 cheap": ("long", "expiry", lambda t: t.ratio < CHEAP),
    "V4 all": ("long", "expiry", lambda t: (t.ratio < CHEAP) & (t.cost <= TIGHT_COST)),
    "V5 sell rich": ("short", "expiry", lambda t: t.ratio >= RICH),
}


def trailing_vol(market, ticker, day, cache):
    """Annualized 21-day realized vol up to and including ``day``, without its largest move. Uses nothing later."""
    if ticker not in cache:
        history = market.closes[ticker]
        dates = sorted(history)
        cache[ticker] = ({d: i for i, d in enumerate(dates)}, np.array([history[d][0] for d in dates], float))
    position, closes = cache[ticker]
    at = position[day]
    window = closes[at - 21 : at + 1] if at >= 21 else None
    if window is None or (window <= 0).any():
        return None
    returns = np.diff(np.log(window))
    trimmed = np.delete(returns, np.abs(returns).argmax())
    return float(trimmed.std(ddof=1)) * math.sqrt(252)


def settle_at_expiry(market, trade, side, counts):
    """Intrinsic value at the last close on or before expiry, or None when it cannot be valued."""
    if trade.expiry > market.calendar[-1]:
        counts["expiry after the price data"] += 1
        return None
    history = market.closes[trade.ticker]
    settle_at = bisect_right(market.calendar, trade.expiry) - 1
    entry_at = market.position[trade.day]
    row = None
    for at in range(settle_at, entry_at - 1, -1):  # a stock delisted before expiry settles on its last close
        row = history.get(market.calendar[at])
        if row is not None:
            break
    entry_row = history[trade.day]
    if row is None or row[0] <= 0 or abs((row[1] / row[0]) / (entry_row[1] / entry_row[0]) - 1) > 0.01:
        counts["split before expiry"] += 1
        return None
    value = abs(row[1] - trade.strike)
    sign = 1 if side == "long" else -1
    usd = sign * (value - trade.fill) * 100 - E.COMMISSION
    return {
        "gross": sign * (value - trade.mid) / trade.mid,
        "net": usd / (trade.fill * 100),
        "usd": usd,
        "value": value,
    }


def collect(market, list_side="long"):
    """Every entry the download covers, valued both ways and on both sides, with what was knowable at entry."""
    rows, counts, cache = [], Counter(), {}
    for day in market.calendar:
        week = market.week_of.get(day)
        if week is None or (week, list_side) not in market.lists:
            continue
        for ticker, root in market.lists[(week, list_side)]:
            if (week, ticker) not in market.fetched or day not in market.closes[ticker]:
                continue
            move = E.signal_on(market.closes[ticker], market, day)
            if math.isnan(move) or abs(move) <= E.LONG_THRESHOLD:
                continue
            realized = trailing_vol(market, ticker, day, cache)
            if not realized:
                counts["no trailing vol"] += 1
                continue
            for side in ("long", "short"):
                trade = E.enter(market, side, ticker, root, day, Counter())
                if trade is None:
                    continue
                implied = E.implied_vol(trade.mid, trade.spot, trade.strike, (trade.expiry - day).days / 365)
                if implied is None:
                    counts["no implied vol"] += 1
                    continue
                base = {
                    "day": day,
                    "ticker": ticker,
                    "side": side,
                    "implied": implied,
                    "realized": realized,
                    "ratio": implied / realized,
                    "cost": (trade.fill - trade.bid) / trade.fill
                    if side == "long"
                    else (trade.ask - trade.fill) / trade.fill,
                    "premium_usd": trade.fill * 100,
                    "notional_usd": trade.strike * 100,
                    "mid": trade.mid,
                    "bid": trade.bid,
                    "ask": trade.ask,
                    "fill": trade.fill,
                    "strike": trade.strike,
                    "spot": trade.spot,
                    "expiry": trade.expiry,
                }
                following = E.settle_next_close(market, trade, side, Counter())
                if following is not None:
                    rows.append(
                        {
                            **base,
                            "hold": "next",
                            "gross": following["gross"],
                            "net": following["net"],
                            "usd": following["usd"],
                        }
                    )
                at_expiry = settle_at_expiry(market, trade, side, counts)
                if at_expiry is not None:
                    rows.append({**base, "hold": "expiry", **at_expiry})
    return pd.DataFrame(rows), counts


def select(trades, candidate):
    side, hold, rule = CANDIDATES[candidate]
    chosen = trades[(trades.side == side) & (trades.hold == hold)]
    return chosen[rule(chosen)]


def per_day(frame, column):
    return frame.groupby("day")[column].mean()


def interval(series, level=CONFIDENCE, draws=5000, seed=20261105):
    """The interval of the mean, resampling blocks of 5 calendar weeks, because holds to expiry overlap."""
    if len(series) < 2:
        return math.nan, math.nan
    first = series.index[0]
    blocks = {}
    for day, value in series.items():
        blocks.setdefault((day - first).days // (7 * BLOCK_WEEKS), []).append(value)
    blocks = [np.array(values, float) for values in blocks.values()]
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        means.append(sum(values.sum() for values in sample) / sum(len(values) for values in sample))
    tail = (100 - level) / 2
    return tuple(np.percentile(means, [tail, 100 - tail]))


def run_null():
    E.MAX_PREMIUM = math.inf  # fake prices are high on purpose: the cap would remove every trade
    for label, ratio, expectation in (
        ("implied = true vol", 1.0, "gross indistinguishable from 0"),
        ("true vol 25% above implied", 1.25, "long gross clearly above 0"),
    ):
        frames, counts = [], Counter()
        for seed in (7, 8):
            trades, seed_counts = collect(E.synthetic_market(vol_ratio=ratio, seed=seed, names=30))
            frames.append(trades)
            counts.update(seed_counts)
        trades = pd.concat(frames)
        print(f"{label} ({expectation}); counts {dict(counts)}")
        print(
            f"  ratio of implied to trailing vol: median {trades.ratio.median():.2f}; share below {CHEAP}: {(trades.ratio < CHEAP).mean():.1%}; at least {RICH}: {(trades.ratio >= RICH).mean():.1%}"
        )
        for candidate in CANDIDATES:
            chosen = select(trades, candidate)
            if len(chosen) < 30:
                print(f"  {candidate:13} too few trades ({len(chosen)})")
                continue
            series = per_day(chosen, "gross")
            low, high = interval(series, level=95)
            print(
                f"  {candidate:13} gross {series.mean():+7.2%}   95% interval {low:+7.2%} to {high:+7.2%}   trades {len(chosen)}"
            )


def load():
    return collect(E.load_market("long"))


def run_power():
    trades, counts = load()
    trades.to_csv(DATA / "improve_trades.csv", index=False)
    print("counts:", dict(counts))
    print(f"ratio of implied to trailing vol, long entries: median {trades[trades.side == 'long'].ratio.median():.2f}")
    for candidate in CANDIDATES:
        chosen = select(trades, candidate)
        development, validation = chosen[chosen.day < SPLIT], chosen[chosen.day >= SPLIT]
        series = per_day(validation, "net")
        error = series.std() / math.sqrt(len(series)) if len(series) > 1 else math.nan
        print(
            f"{candidate:13} trades: development {len(development):5}, validation {len(validation):5}; validation days {len(series):4}; "
            f"per-day SE {error:.2%}, detectable about {2.8 * error:.1%}; median cost {chosen.cost.median():.1%}"
        )


def run_results():
    trades = pd.read_csv(DATA / "improve_trades.csv", parse_dates=["day"])
    print(f"99% intervals, 5-week blocks. Development to {SPLIT:%Y-%m-%d}, validation after.\n")
    for candidate in CANDIDATES:
        chosen = select(trades, candidate)
        development, validation = chosen[chosen.day < SPLIT], chosen[chosen.day >= SPLIT]
        print(f"{candidate}: trades {len(development)} / {len(validation)}")
        for label, part in (("development", development), ("validation", validation)):
            if len(part) < 30:
                print(f"  {label:12} too few trades")
                continue
            net, gross = per_day(part, "net"), per_day(part, "gross")
            low, high = interval(net)
            months = part.groupby(part.day.dt.to_period("M")).usd.sum()
            print(
                f"  {label:12} net {net.mean():+7.2%} ({low:+7.2%} to {high:+7.2%})   gross {gross.mean():+7.2%}   won {(part.usd > 0).mean():5.1%}   months up {(months > 0).sum()}/{len(months)}   $ per trade {part.usd.mean():+7.0f}"
            )
        if len(validation) >= 30 and len(development) >= 30:
            low, _ = interval(per_day(validation, "net"))
            passed = per_day(development, "net").mean() > 0 and low > 0 and len(validation) >= MIN_VALIDATION_TRADES
            print(f"  passes the pre-set rule (null control checked separately): {'YES' if passed else 'no'}")
        if CANDIDATES[candidate][0] == "short":
            day_usd = chosen.groupby("day").usd.sum()
            print(
                f"  tail: worst trade {chosen.usd.min():+,.0f} USD, {(chosen.usd / chosen.notional_usd).min():.1%} of notional, {(chosen.usd / chosen.premium_usd).min():.0%} of premium; "
                f"worst day {day_usd.min():+,.0f}; mean of worst 5% of trades {chosen.usd.nsmallest(max(1, len(chosen) // 20)).mean():+,.0f} USD; losers beyond 3x premium {(chosen.usd / chosen.premium_usd < -3).mean():.1%}"
            )
        print()


if __name__ == "__main__":
    {"null": run_null, "power": run_power, "results": run_results}[sys.argv[1]]()
