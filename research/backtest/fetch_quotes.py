# ruff: noqa: E501 - report and log lines read better whole
"""Downloads end-of-day quotes for the planned (stock, week) pairs. Resumable: finished files are skipped.

Each file holds every strike of one expiry, from the trading day before the week to the first trading day after it, so a
contract cannot fall out of the data by moving far from the money, which would quietly drop the largest winners. Weeks
run in a fixed shuffled order, so a partial download is still spread across the whole period.

Usage: fetch_quotes.py long|short [max_weeks]
"""

import gzip
import random
import sys
from pathlib import Path

import pandas as pd
import theta

DATA = Path(__file__).parent / "data"
QUOTES = DATA / "quotes"
EXPIRIES = DATA / "expirations"
KEEP = ["expiration", "strike", "right", "date", "bid", "ask", "close", "volume"]
MIN_DAYS_TO_EXPIRY = 7


def expirations(root):
    """Every expiry the symbol ever had, cached per symbol."""
    path = EXPIRIES / f"{root}.csv"
    if not path.exists():
        frame = theta.get_csv("option/list/expirations", symbol=root)
        (frame if frame is not None else pd.DataFrame(columns=["symbol", "expiration"])).to_csv(path, index=False)
    return sorted(pd.to_datetime(pd.read_csv(path).expiration))


def main():
    side = sys.argv[1]
    max_weeks = int(sys.argv[2]) if len(sys.argv) > 2 else None
    QUOTES.mkdir(exist_ok=True)
    EXPIRIES.mkdir(exist_ok=True)
    plan = pd.read_csv(DATA / "fetch_plan.csv", parse_dates=["week_start"])
    plan = plan[plan.side == side]
    calendar = sorted(pd.to_datetime(pd.read_csv(DATA / "prices.csv.gz", usecols=["date"]).date.unique()))
    weeks = sorted(plan.week_start.unique())
    random.Random(20261105).shuffle(weeks)
    if max_weeks:
        weeks = weeks[:max_weeks]
    all_weeks = sorted(pd.read_csv(DATA / "universe.csv", parse_dates=["week_start"]).week_start.unique())

    fetched = skipped = empty = no_listing = 0
    for done, week in enumerate(weeks, 1):
        later = [w for w in all_weeks if w > week]
        days = [d for d in calendar if week <= d and (not later or d < later[0])]
        before = [d for d in calendar if d < days[0]][-1]
        after = [d for d in calendar if d > days[-1]]
        start = before.strftime("%Y%m%d")
        end = (after[0] if after else days[-1]).strftime("%Y%m%d")
        for row in plan[plan.week_start == week].itertuples():
            listed = expirations(row.root)
            needed = {
                min((e for e in listed if e >= d + pd.Timedelta(days=MIN_DAYS_TO_EXPIRY)), default=None) for d in days
            }
            needed.discard(None)
            if not needed:
                no_listing += 1
                continue
            for expiry in sorted(needed):
                path = QUOTES / f"{row.root}_{expiry:%Y%m%d}_{week:%Y%m%d}.csv.gz"
                if path.exists():
                    skipped += 1
                    continue
                frame = theta.option_eod(row.root, start, end, expiration=expiry.strftime("%Y%m%d"))
                if frame is None:
                    empty += 1
                    frame = pd.DataFrame(columns=KEEP)
                else:
                    frame = frame[KEEP]
                    fetched += 1
                temporary = path.with_suffix(".tmp")
                with gzip.open(temporary, "wt") as f:
                    frame.to_csv(f, index=False)
                temporary.rename(path)
        print(
            f"week {done}/{len(weeks)} {week:%Y-%m-%d}: fetched {fetched}, already had {skipped}, empty {empty}, no listing {no_listing}",
            flush=True,
        )
    print("finished", flush=True)


if __name__ == "__main__":
    main()
