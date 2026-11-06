# ruff: noqa: E501 - report and log lines read better whole
"""Step 2: rebuilds the bot's target list for every week, with the bot's own screen() on point-in-time prices.

Sharadar keeps delisted stocks, so the universe has no survivorship bias. Writes data/universe.csv: each week's
ranking at either end, from the closes up to the previous Friday. The screen's options check needs Theta, so it is
applied when the quotes are fetched. Run with the repo's environment and PYTHONPATH=src.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from market_watcher.screen import History, screen

FIRST_WEEK = pd.Timestamp("2023-06-05")
HISTORY_DAYS = 63  # the screen downloads three months of daily prices
KEEP = 90  # names kept at either end, enough for those without listed options to be skipped
DATA = Path(__file__).parent / "data"

prices = pd.read_csv(DATA / "prices.csv.gz", parse_dates=["date"])
days = np.array(sorted(prices.date.unique()), dtype="datetime64[ns]")
series = {}
for ticker, g in prices.groupby("ticker", sort=False):
    series[ticker] = (
        g.date.to_numpy(dtype="datetime64[ns]"),
        g.closeadj.to_numpy(float),
        g.closeunadj.to_numpy(float),
        (g.close * g.volume).to_numpy(float),  # both split-adjusted, so this is the true dollar volume
    )

last_day_of_week = (
    pd.Series(days, index=days).groupby(pd.Grouper(freq="W-FRI")).max().dropna().to_numpy(dtype="datetime64[ns]")
)
rows = []
for last_day in last_day_of_week:
    later = days[days > last_day]
    if len(later) == 0 or later[0] < np.datetime64(FIRST_WEEK):
        continue
    histories = {}
    for ticker, (dates, adjusted, raw, dollars) in series.items():
        end = np.searchsorted(dates, last_day, side="right")
        if end < 22 or dates[end - 1] != last_day or adjusted[end - 1] <= 0:
            continue
        start = max(0, end - HISTORY_DAYS)
        # Scaled so that the last close is the price the screen would have seen that day; returns are unaffected.
        scale = raw[end - 1] / adjusted[end - 1]
        closes = adjusted[start:end] * scale
        histories[ticker] = History(closes=closes.tolist(), volumes=(dollars[start:end] / closes).tolist())
    result = screen(histories, has_options=None)
    total = len(result.ranked)
    for rank, item in enumerate(result.ranked):
        if rank < KEEP or rank >= total - KEEP:
            rows.append(
                (
                    later[0],
                    item.ticker,
                    rank,
                    total - 1 - rank,
                    item.weighted_volatility,
                    item.last_close,
                    item.avg_dollar_volume,
                )
            )

universe = pd.DataFrame(
    rows, columns=["week_start", "ticker", "rank_top", "rank_bottom", "weighted_vol", "last_close", "dollar_volume"]
)
universe.to_csv(DATA / "universe.csv", index=False)
print(
    f"weeks {universe.week_start.nunique()}, {universe.week_start.min():%Y-%m-%d} to {universe.week_start.max():%Y-%m-%d}"
)
print(f"eligible stocks per week, median: {universe.groupby('week_start').rank_top.max().median():.0f}")
print(
    f"unique names near the top: {universe[universe.rank_top < KEEP].ticker.nunique()}; near the bottom: {universe[universe.rank_bottom < KEEP].ticker.nunique()}"
)
