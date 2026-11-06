# ruff: noqa: E501 - report and log lines read better whole
"""Step 1: copies what the backtest needs out of the Sharadar parquet files into plain CSV.

Run with an environment that has pyarrow (the force_model one). Reads only the raw SEP, ticker and action files.
"""

import glob
from pathlib import Path

import pandas as pd

SEP_DIR = "/Users/christos/Projects/force_model/data/raw"
START, END = "2023-02-01", "2026-09-10"  # prices from February, so that the first screen has its history by June 2023
DATA = Path(__file__).parent / "data"

tickers = pd.read_parquet(f"{SEP_DIR}/tickers.parquet")
tickers = tickers[tickers.table == "SEP"]
# The bot's "nasdaq" universe: common stock (ADRs included) on a US exchange; no preferreds, no OTC.
common = tickers[
    tickers.category.fillna("").str.contains("Common Stock") & tickers.exchange.isin(["NYSE", "NASDAQ", "NYSEMKT"])
]
columns = ["ticker", "date", "open", "high", "low", "close", "volume", "closeadj", "closeunadj"]
frames = [
    pd.read_parquet(f, columns=columns)
    for f in sorted(glob.glob(f"{SEP_DIR}/sep/sep_20*.parquet"))
    if int(f[-12:-8]) >= 2023
]
sep = pd.concat(frames)
sep = sep[(sep.date >= START) & (sep.date <= END) & sep.ticker.isin(set(common.ticker))]
sep = sep.sort_values(["ticker", "date"])
sep.to_csv(DATA / "prices.csv.gz", index=False)
print(
    f"prices: {len(sep):,} rows, {sep.ticker.nunique():,} tickers, {sep.date.min():%Y-%m-%d} to {sep.date.max():%Y-%m-%d}"
)

actions = pd.read_parquet(f"{SEP_DIR}/actions.parquet")
actions = actions[
    actions.action.isin(["tickerchangefrom", "tickerchangeto", "split", "delisted"]) & (actions.date >= START)
]
actions.to_csv(DATA / "actions.csv", index=False)
print(f"actions: {len(actions):,} rows")
print(actions[actions.ticker.isin(["XYZ", "SQ"])].sort_values("date").to_string())
print("XYZ first price:", sep[sep.ticker == "XYZ"].date.min(), "| rows under SQ:", (sep.ticker == "SQ").sum())
