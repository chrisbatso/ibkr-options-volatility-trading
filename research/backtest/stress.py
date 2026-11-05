# ruff: noqa: E501 - report and log lines read better whole
"""Stress tests of V5, which passed PLAN_IMPROVEMENTS.md's rule. They can only weaken it, and they run before it is believed.

stress.py null     a no-edge market with noisy, wide quotes and vol that really changes: what does V5 report there?
stress.py real     realistic fills, the worst trades, how the effect scales, and what it would ask of an account
"""

import math
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import evaluate as E
import improve as I
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
RICH = I.RICH


def noisy_market(seed, quote_noise=0.08, half_spread=0.12, days=500, names=30):
    """No edge, on purpose: every option is fair at its true future vol. But vol regimes change, so implied can be high
    relative to trailing realized for a good reason, and each leg's quote is noisy and wide, as on thin real options.
    Selling what looks rich can only make money here by selling quote errors at prices nobody would really pay."""
    rng = np.random.default_rng(seed)
    business_days = list(pd.bdate_range("2023-06-05", periods=days + 40))
    calendar = business_days[:days]
    position = {day: at for at, day in enumerate(business_days)}
    fridays = [day for day in business_days if day.weekday() == 4]
    grid = [round(100 * 1.03**j, 2) for j in range(-120, 240)]
    closes, chains, expiries, lists, fetched, week_of = defaultdict(dict), defaultdict(dict), {}, {}, set(), {}
    tickers = [f"T{i:03d}" for i in range(names)]
    for ticker in tickers:
        expiries[ticker] = fridays
        level, path = rng.uniform(0.02, 0.05), []
        for _ in business_days:  # a new vol level every 30 days or so
            if rng.random() < 1 / 30:
                level = float(np.clip(level * math.exp(rng.normal(0, 0.6)), 0.01, 0.15))
            path.append(level)
        path = np.array(path)
        spot = 1000.0
        for day in calendar:
            at = position[day]
            spot *= math.exp(rng.normal(-(path[at] ** 2) / 2, path[at]))
            closes[ticker][day] = (spot, spot, spot, spot, spot)
            for expiry in [friday for friday in fridays if timedelta(days=0) <= friday - day <= timedelta(days=21)]:
                left = position[expiry] - at
                fair_vol = math.sqrt(float(np.mean(path[at + 1 : at + left + 1] ** 2))) if left > 0 else path[at]
                for strike in [k for k in grid if 0.6 * spot <= k <= 1.6 * spot]:
                    total = E.straddle_value(spot, strike, left, fair_vol, rate=0.0)
                    call = (total + spot - strike) / 2
                    put = total - call
                    call_mid = max(call * (1 + rng.normal(0, quote_noise)), 0.02)
                    put_mid = max(put * (1 + rng.normal(0, quote_noise)), 0.02)
                    chains[(ticker, expiry, day)][strike] = (
                        round(call_mid * (1 - half_spread), 2),
                        round(call_mid * (1 + half_spread), 2),
                        round(put_mid * (1 - half_spread), 2),
                        round(put_mid * (1 + half_spread), 2),
                    )
    for day in calendar:
        week = day - timedelta(days=day.weekday())
        week_of[day] = week
        lists[(week, "long")] = [(ticker, ticker) for ticker in tickers]
        fetched.update((week, ticker) for ticker in tickers)
    return E.Market(closes, calendar, lists, fetched, week_of, expiries, chains)


def with_bid_columns(short):
    short = short.copy()
    short["net_bid"] = ((short.bid - short.value) * 100 - E.COMMISSION) / (short.bid * 100)
    short["usd_bid"] = (short.bid - short.value) * 100 - E.COMMISSION
    years = (pd.to_datetime(short.expiry) - pd.to_datetime(short.day)).dt.days / 365
    vols = [E.implied_vol(b, s, k, y) for b, s, k, y in zip(short.bid, short.spot, short.strike, years, strict=True)]
    short["ratio_bid"] = np.array([v if v is not None else np.nan for v in vols], float) / short.realized
    return short


def run_null():
    E.MAX_PREMIUM = math.inf
    rows = []
    for seed in range(301, 313):
        trades, _ = I.collect(noisy_market(seed))
        short = with_bid_columns(trades[(trades.side == "short") & (trades.hold == "expiry")])
        rich = short[short.ratio >= RICH]
        rich_bid = short[short.ratio_bid >= RICH]
        rows.append(
            {
                "n": len(rich),
                "share": len(rich) / len(short),
                "cost": rich.cost.median(),
                "gross mid": rich.gross.mean(),
                "net at mid-2%": rich.net.mean(),
                "net at bid": rich.net_bid.mean(),
                "n bid": len(rich_bid),
                "bid-selected gross": rich_bid.gross.mean(),
                "bid-selected net at bid": rich_bid.net_bid.mean(),
            }
        )
    r = pd.DataFrame(rows)
    print(
        f"No-edge market with noisy wide quotes, {len(r)} independent markets; V5 selects {r.share.mean():.1%} of entries, {r.n.mean():.0f} trades a market, median cost {r.cost.mean():.1%}"
    )
    for column in ("gross mid", "net at mid-2%", "net at bid", "bid-selected gross", "bid-selected net at bid"):
        x = r[column].dropna()
        print(f"  {column:26} mean {x.mean():+7.2%}   standard error {x.std(ddof=1) / math.sqrt(len(x)):.2%}")


def report(label, frame, column, usd):
    for part_label, part in (("development", frame[frame.day < I.SPLIT]), ("validation", frame[frame.day >= I.SPLIT])):
        series = part.groupby("day")[column].mean()
        low, high = I.interval(series)
        months = part.groupby(part.day.dt.to_period("M"))[usd].sum()
        print(
            f"  {label:34} {part_label:12} net {series.mean():+7.2%} ({low:+7.2%} to {high:+7.2%})   won {(part[usd] > 0).mean():5.1%}   months up {(months > 0).sum()}/{len(months)}   $ per trade {part[usd].mean():+6.0f}   n {len(part)}"
        )


def run_real():
    trades, _ = I.load()
    trades.to_csv(DATA / "improve_trades.csv", index=False)
    all_short = trades[trades.side == "short"]
    short = with_bid_columns(all_short[all_short.hold == "expiry"])
    short = short[short.bid > 0]
    rich = short[short.ratio >= RICH]

    print("=== Fills: V5 as tested assumed selling at mid -2%")
    report("as tested, mid -2%", rich, "net", "usd")
    report("selected on mid, sold at the bid", rich, "net_bid", "usd_bid")
    report("selected on the bid, sold at the bid", short[short.ratio_bid >= RICH], "net_bid", "usd_bid")
    print(
        f"  half-spread on these trades: median {((rich.mid - rich.bid) / rich.mid).median():.1%}, mean {((rich.mid - rich.bid) / rich.mid).mean():.1%}"
    )

    print("\n=== Is the mid a price anyone could trade? One day later, mid to mid, sold side")
    following = all_short[all_short.hold == "next"]
    for label, part in (
        ("ratio below 1.5", following[following.ratio < RICH]),
        ("ratio 1.5 and up", following[following.ratio >= RICH]),
    ):
        print(
            f"  {label:20} gross after one day {part.gross.mean():+7.2%}, median {part.gross.median():+7.2%}, n {len(part)}, median days to expiry {(pd.to_datetime(part.expiry) - part.day).dt.days.median():.0f}"
        )

    print("\n=== Does the effect scale with how rich implied is? Sold, held to expiry, by ratio")
    edges = [0, 0.75, 1.0, 1.25, 1.5, 2.0, 99]
    short["bucket"] = pd.cut(short.ratio, edges, right=False)
    for bucket, part in short.groupby("bucket", observed=True):
        d, v = part[part.day < I.SPLIT], part[part.day >= I.SPLIT]
        print(
            f"  {str(bucket):14} development gross {d.gross.mean():+7.2%} at-bid {d.net_bid.mean():+7.2%} n {len(d):4} | validation gross {v.gross.mean():+7.2%} at-bid {v.net_bid.mean():+7.2%} n {len(v):4}"
        )

    print("\n=== How many separate bets? The same contract sold on several days counts once here")
    events = (
        rich.groupby(["ticker", "expiry"])
        .agg(day=("day", "min"), usd_bid=("usd_bid", "mean"), net_bid=("net_bid", "mean"), n=("usd_bid", "size"))
        .reset_index()
    )
    report("first-class: one per ticker and expiry", events, "net_bid", "usd_bid")
    print(
        f"  trades {len(rich)}, separate bets {len(events)}, tickers {rich.ticker.nunique()}; top 5 tickers' share of trades {rich.ticker.value_counts().head(5).sum() / len(rich):.1%}"
    )

    print("\n=== The worst trades, sold at the bid")
    columns = ["day", "ticker", "spot", "strike", "expiry", "value", "bid", "usd_bid"]
    print(rich.nsmallest(8, "usd_bid")[columns].round(2).to_string(index=False))
    prices = pd.read_csv(
        DATA / "prices.csv.gz", parse_dates=["date"], usecols=["ticker", "date", "close", "closeunadj"]
    )
    worst = rich.nsmallest(3, "usd_bid")
    for row in worst.itertuples():
        path = prices[
            (prices.ticker == row.ticker) & (prices.date >= row.day) & (prices.date <= pd.Timestamp(row.expiry))
        ]
        print(
            f"  {row.ticker} from {row.day:%Y-%m-%d}: raw close {path.closeunadj.iloc[0]:.2f} to {path.closeunadj.iloc[-1]:.2f}; adjusted {path.close.iloc[0]:.2f} to {path.close.iloc[-1]:.2f}; largest day {path.close.pct_change().abs().max():.0%}"
        )

    print("\n=== What it asks of an account, sold at the bid, one contract each")
    margin = 0.2 * rich.spot * 100 + rich.bid * 100
    print(
        f"  approximate margin per trade: median {margin.median():,.0f} USD; profit on margin per trade: mean {(rich.usd_bid / margin).mean():+.1%}, worst {(rich.usd_bid / margin).min():+.0%}"
    )
    print(
        f"  loss beyond the premium: {(rich.usd_bid < -rich.bid * 100).mean():.1%} of trades; beyond 3x premium: {(rich.usd_bid < -3 * rich.bid * 100).mean():.1%}; mean of the worst 5% of trades {rich.usd_bid.nsmallest(max(1, len(rich) // 20)).mean():+,.0f} USD"
    )
    months = rich.groupby(rich.day.dt.to_period("M")).usd_bid.sum()
    print(
        f"  by month: worst {months.min():+,.0f} ({months.idxmin()}), best {months.max():+,.0f}, total {months.sum():+,.0f}, up {(months > 0).sum()}/{len(months)}"
    )
    for year, part in rich.groupby(rich.day.dt.year):
        print(
            f"  {year}: net at the bid {part.net_bid.mean():+7.2%}, gross {part.gross.mean():+7.2%}, won {(part.usd_bid > 0).mean():.1%}, n {len(part)}, total {part.usd_bid.sum():+,.0f} USD"
        )


if __name__ == "__main__":
    {"null": run_null, "real": run_real}[sys.argv[1]]()
