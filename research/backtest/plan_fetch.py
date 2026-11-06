# ruff: noqa: E501 - report and log lines read better whole
"""Works out which (stock, week) chains the backtest needs, from prices alone: no option data or outcomes are seen here.

Applies the screen's has_options step as "the symbol has ever had options" (Theta's symbol list); a stock whose chain then
turns out to be empty that week is dropped when the quotes are read, and that is counted. Writes data/lists.csv (the
weekly target lists) and data/fetch_plan.csv.
"""

from pathlib import Path

import pandas as pd
import theta

DATA = Path(__file__).parent / "data"
TOP = 50
LONG_THRESHOLD, SHORT_THRESHOLD = 0.02, 0.005
MAX_PER_DAY = 3
EXTRA_NAMES = {
    "long": 8,
    "short": 2,
}  # beyond the names the bot would pick: premium caps can push it down the alphabet; long also gets a no-signal control

universe = pd.read_csv(DATA / "universe.csv", parse_dates=["week_start"])
actions = pd.read_csv(DATA / "actions.csv", parse_dates=["date"])
symbols = set(theta.get_csv("option/list/symbols").symbol)
changes = actions[actions.action == "tickerchangefrom"][["ticker", "contraticker", "date"]].sort_values(
    "date", ascending=False
)


def root_on(ticker, day):
    """The symbol the stock's options traded under on ``day``: Sharadar files all history under the latest ticker."""
    root = ticker
    for row in changes[changes.ticker == ticker].itertuples():
        if day < row.date:
            root = row.contraticker
    return root


lists = []
for week_start, group in universe.groupby("week_start"):
    for side, rank in (("long", "rank_top"), ("short", "rank_bottom")):
        taken = 0
        for row in group.sort_values(rank).itertuples():
            if side == "long" and row.rank_top >= 90 or side == "short" and row.rank_bottom >= 90:
                break
            root = root_on(row.ticker, week_start)
            if root in symbols:
                lists.append((week_start, side, row.ticker, root, getattr(row, rank)))
                taken += 1
                if taken == TOP:
                    break
lists = pd.DataFrame(lists, columns=["week_start", "side", "ticker", "root", "rank"])
lists.to_csv(DATA / "lists.csv", index=False)
print("names per weekly list:", lists.groupby(["week_start", "side"]).size().describe()[["min", "max"]].to_dict())

prices = pd.read_csv(DATA / "prices.csv.gz", parse_dates=["date"], usecols=["ticker", "date", "close"])
prices = prices[prices.ticker.isin(set(lists.ticker))].sort_values(["ticker", "date"])
prices["ret"] = prices.groupby("ticker").close.pct_change()
calendar = sorted(prices.date.unique())
week_of = {}
weeks = sorted(lists.week_start.unique())
for day in calendar:
    started = [w for w in weeks if w <= day]
    if started:
        week_of[day] = started[-1]
prices["week_start"] = prices.date.map(week_of)
prices = prices.dropna(subset=["week_start", "ret"])

plan, summary = [], {}
for side in ("long", "short"):
    members = lists[lists.side == side].merge(prices, on=["week_start", "ticker"])
    fires = members.ret.abs() > LONG_THRESHOLD if side == "long" else members.ret.abs() < SHORT_THRESHOLD
    signals = members[fires].sort_values(["date", "ticker"])
    picks = signals.groupby("date").head(MAX_PER_DAY)
    per_day = signals.groupby("date").size()
    print(
        f"{side}: signal days {per_day.size}, signals per day median {per_day.median():.0f}; "
        f"picks {len(picks)}, days with fewer than 3 signals {(per_day < MAX_PER_DAY).sum()}; share of member-days firing {fires.mean():.1%}"
    )
    # For each week: the names the bot would pick, then the next EXTRA_NAMES alphabetically.
    for week_start, group in lists[lists.side == side].groupby("week_start"):
        ordered = sorted(group.ticker)
        chosen = set(picks[picks.week_start == week_start].ticker)
        last = max((ordered.index(t) for t in chosen), default=-1)
        for ticker in ordered[: last + 1 + EXTRA_NAMES[side]]:
            plan.append((week_start, side, ticker, group[group.ticker == ticker].root.iloc[0]))
    print(f"  name-weeks to fetch so far: {len(plan)}")
plan = pd.DataFrame(plan, columns=["week_start", "side", "ticker", "root"]).drop_duplicates(["week_start", "ticker"])
plan.to_csv(DATA / "fetch_plan.csv", index=False)
print(f"fetch plan: {len(plan)} requests ({plan.side.value_counts().to_dict()}); unique names {plan.ticker.nunique()}")
print(f"at 5 seconds a request: {len(plan) * 5 / 3600:.1f} hours")
