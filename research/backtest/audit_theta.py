"""Before downloading anything large: do Theta's end-of-day quotes belong to that day's close?

For a spread of dates and stocks, including volatile ones and big-move days, compares the spot implied by put-call
parity with Sharadar's raw closes one day either side. A quote taken at the close should sit on lag 0 by a wide margin,
and the check is sharpest when the stock moved a lot, because the neighbouring closes are then far apart.
"""

from pathlib import Path

import pandas as pd
import theta

DATA = Path(__file__).parent / "data"
prices = pd.read_csv(DATA / "prices.csv.gz", parse_dates=["date"], usecols=["ticker", "date", "closeunadj", "close"])
prices = prices.sort_values(["ticker", "date"])
prices["ret"] = prices.groupby("ticker").close.pct_change()
names = ["AAPL", "TSLA", "XOM", "ALNY", "AMC", "MARA", "NVDA", "BA", "SMCI", "PLTR"]
sample = prices[prices.ticker.isin(names) & (prices.date >= "2023-06-05")]
# For each stock: its four largest moves, plus four ordinary days spread through the window.
picks = []
for _ticker, g in sample.groupby("ticker"):
    picks.append(g.loc[g.ret.abs().nlargest(4).index])
    picks.append(g.iloc[[len(g) // 5, 2 * len(g) // 5, 3 * len(g) // 5, 4 * len(g) // 5]])
picks = pd.concat(picks).drop_duplicates(["ticker", "date"])

results = []
for row in picks.itertuples():
    day = row.date.strftime("%Y%m%d")
    chain = theta.option_eod(row.ticker, day, day, strike_range=4)
    if chain is None:
        results.append((row.ticker, row.date, row.ret, None, None, None, None))
        continue
    chain["expiration"] = pd.to_datetime(chain["expiration"])
    chain = chain[chain.expiration >= row.date + pd.Timedelta(days=5)]
    chain = chain[chain.expiration == chain.expiration.min()]
    chain["mid"] = (chain.bid + chain.ask) / 2
    wide = chain.pivot_table(index="strike", columns="right", values="mid").dropna()
    if wide.empty or not {"CALL", "PUT"} <= set(wide.columns):
        results.append((row.ticker, row.date, row.ret, None, None, None, None))
        continue
    strike = (wide.CALL - wide.PUT).abs().idxmin()
    spot = strike + wide.CALL[strike] - wide.PUT[strike]
    series = prices[prices.ticker == row.ticker].reset_index(drop=True)
    at = series.index[series.date == row.date][0]
    closes = {lag: series.closeunadj.iloc[at + lag] for lag in (-1, 0, 1) if 0 <= at + lag < len(series)}
    results.append((row.ticker, row.date, row.ret, spot, closes.get(-1), closes.get(0), closes.get(1)))

out = pd.DataFrame(results, columns=["ticker", "date", "ret", "spot", "close_m1", "close_0", "close_p1"])
print(f"requests {len(out)}; with a usable chain {out.spot.notna().sum()}")
ok = out.dropna(subset=["spot", "close_0"]).copy()
for lag in ("close_m1", "close_0", "close_p1"):
    ok[f"err_{lag}"] = (ok.spot / ok[lag] - 1).abs()
print(
    "median |parity spot / close - 1| at lag -1, 0, +1:",
    ok[["err_close_m1", "err_close_0", "err_close_p1"]].median().round(5).tolist(),
)
big = ok[ok.ret.abs() > 0.08]
print(
    f"on moves over 8% (n={len(big)}):", big[["err_close_m1", "err_close_0", "err_close_p1"]].median().round(5).tolist()
)
print(
    "share nearest to lag 0, all:",
    round((ok.err_close_0 <= ok[["err_close_m1", "err_close_p1"]].min(axis=1)).mean(), 3),
    "| on big moves:",
    round((big.err_close_0 <= big[["err_close_m1", "err_close_p1"]].min(axis=1)).mean(), 3),
)
print("worst lag-0 errors:")
print(
    ok.nlargest(5, "err_close_0")[["ticker", "date", "ret", "spot", "close_m1", "close_0", "close_p1"]]
    .round(3)
    .to_string(index=False)
)
print(out[out.spot.isna()][["ticker", "date"]].head(8).to_string(index=False))
