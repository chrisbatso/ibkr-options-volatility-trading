# ruff: noqa: E501 - report and log lines read better whole
"""Evaluates the frozen rules, as specified in PLAN.md. Nothing here is tuned.

evaluate.py null      the synthetic controls: must pass before anything else is read
evaluate.py power     dispersion, sample size and cost only, no means
evaluate.py results   the results
(add short for the short side: evaluate.py results short)
"""

import glob
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
LONG_THRESHOLD, SHORT_THRESHOLD = 0.02, 0.005
MAX_PER_DAY = 3
MIN_DAYS_TO_EXPIRY = 7
BUFFER = 0.02
MAX_PREMIUM, MAX_NOTIONAL = 1_000.0, 20_000.0
COMMISSION = 2.60  # USD per straddle, round trip: four legs at 0.65
PARITY_LIMIT = 0.15
RATE = 0.04
TAKE_PROFIT = 0.50
OPEN_TO_CLOSE = (6.25 / 24) / 365  # the calendar-day convention: the least decay that can be charged, as PLAN.md tilts


# --- Market data -----------------------------------------------------------------------------------------------------


@dataclass
class Market:
    closes: dict  # ticker -> {date: (close, closeunadj, open, high, low)}
    calendar: list
    lists: dict  # (week_start, side) -> [(ticker, root)] alphabetical
    fetched: set  # (week_start, ticker) the download covers
    week_of: dict  # date -> week_start
    expiries: dict  # root -> sorted expiries
    chains: dict  # (root, expiry, date) -> {strike: (call_bid, call_ask, put_bid, put_ask)}

    def __post_init__(self):
        self.position = {day: at for at, day in enumerate(self.calendar)}


def load_market(side):
    prices = pd.read_csv(DATA / "prices.csv.gz", parse_dates=["date"])
    lists_frame = pd.read_csv(DATA / "lists.csv", parse_dates=["week_start"])
    plan = pd.read_csv(DATA / "fetch_plan.csv", parse_dates=["week_start"])
    lists_frame = lists_frame[lists_frame.side == side]
    plan = plan[plan.side == side]
    prices = prices[prices.ticker.isin(set(lists_frame.ticker))]
    closes = defaultdict(dict)
    for row in prices.itertuples():
        closes[row.ticker][row.date] = (row.close, row.closeunadj, row.open, row.high, row.low)
    calendar = sorted(pd.to_datetime(pd.read_csv(DATA / "prices.csv.gz", usecols=["date"]).date.unique()))
    weeks = sorted(pd.read_csv(DATA / "universe.csv", parse_dates=["week_start"]).week_start.unique())
    week_of, position = {}, 0
    for day in calendar:
        while position + 1 < len(weeks) and weeks[position + 1] <= day:
            position += 1
        if weeks[position] <= day:
            week_of[day] = weeks[position]
    lists = {}
    for (week, list_side), group in lists_frame.groupby(["week_start", "side"]):
        lists[(week, list_side)] = sorted(zip(group.ticker, group.root, strict=True))
    fetched = set(zip(plan.week_start, plan.ticker, strict=True))
    expiries = {}
    for path in glob.glob(str(DATA / "expirations/*.csv")):
        expiries[Path(path).stem] = sorted(pd.to_datetime(pd.read_csv(path).expiration))
    chains = defaultdict(dict)
    for path in glob.glob(str(DATA / "quotes/*.csv.gz")):
        frame = pd.read_csv(path, parse_dates=["expiration", "date"])
        if frame.empty:
            continue
        root = Path(path).name.split("_")[0]
        calls = frame[frame.right == "CALL"].set_index(["expiration", "date", "strike"])[["bid", "ask"]]
        puts = frame[frame.right == "PUT"].set_index(["expiration", "date", "strike"])[["bid", "ask"]]
        both = calls.add_suffix("_c").join(puts.add_suffix("_p"), how="inner")
        for (expiry, day, strike), row in zip(both.index, both.itertuples(index=False), strict=True):
            chains[(root, expiry, day)][float(strike)] = (row.bid_c, row.ask_c, row.bid_p, row.ask_p)
    return Market(closes, calendar, lists, fetched, week_of, expiries, chains)


# --- Pricing ---------------------------------------------------------------------------------------------------------


def _norm_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


def straddle_value(spot, strike, years, vol, rate=RATE):
    """Black-Scholes straddle: European, no dividends. Close enough at the money over a few weeks."""
    if years <= 0 or vol <= 0:
        return abs(spot - strike)
    root = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / root
    d2 = d1 - root
    discount = strike * math.exp(-rate * years)
    call = spot * _norm_cdf(d1) - discount * _norm_cdf(d2)
    return call + (call - spot + discount)


def implied_vol(value, spot, strike, years):
    """The volatility at which the straddle is worth ``value``, by bisection; None when no such volatility exists."""
    low, high = 1e-3, 20.0
    if not straddle_value(spot, strike, years, low) <= value <= straddle_value(spot, strike, years, high):
        return None
    for _ in range(70):
        mid = (low + high) / 2
        if straddle_value(spot, strike, years, mid) < value:
            low = mid
        else:
            high = mid
    return (low + high) / 2


# --- Entries and exits -----------------------------------------------------------------------------------------------


def nearest_expiry(listed, day):
    earliest = day + timedelta(days=MIN_DAYS_TO_EXPIRY)
    return next((e for e in listed if e >= earliest), None)


def nearest_strike(strikes, price):
    return min(strikes, key=lambda strike: (abs(strike - price), strike))


def two_sided(quote):
    return min(quote) > 0


@dataclass
class Trade:
    day: pd.Timestamp
    ticker: str
    root: str
    expiry: pd.Timestamp
    strike: float
    mid: float
    bid: float
    ask: float
    limit: float
    fill: float
    spot: float
    signal: float


def enter(market, side, ticker, root, day, counts):
    """The straddle the bot would open at this close, or None with the reason counted. Uses nothing after ``day``."""
    close, raw, *_ = market.closes[ticker][day]
    expiry = nearest_expiry(market.expiries.get(root, []), day)
    chain = market.chains.get((root, expiry, day)) if expiry is not None else None
    if not chain:
        counts["no chain"] += 1
        return None
    strike = nearest_strike(chain, raw)
    quote = chain[strike]
    if not two_sided(quote):
        counts["no two-sided quote"] += 1
        return None
    call_mid, put_mid = (quote[0] + quote[1]) / 2, (quote[2] + quote[3]) / 2
    if abs((strike + call_mid - put_mid) / raw - 1) > PARITY_LIMIT:
        counts["parity guard"] += 1
        return None
    mid = call_mid + put_mid
    bid, ask = quote[0] + quote[2], quote[1] + quote[3]
    if side == "long":
        limit = round(mid * (1 + BUFFER), 2)
        if limit * 100 > MAX_PREMIUM:
            counts["premium cap"] += 1
            return None
        fill = min(limit, ask)
    else:
        limit = round(mid * (1 - BUFFER), 2)
        if strike * 100 > MAX_NOTIONAL:
            counts["notional cap"] += 1
            return None
        fill = max(limit, bid)
    previous = market.closes[ticker]
    return Trade(
        day, ticker, root, expiry, strike, mid, bid, ask, limit, fill, raw, abs(signal_on(previous, market, day))
    )


def signal_on(history, market, day):
    """The close-to-close move on ``day``, from split-adjusted closes."""
    at = market.position[day]
    before = history.get(market.calendar[at - 1]) if at else None
    today = history.get(day)
    if before is None or today is None or before[0] <= 0:
        return math.nan
    return today[0] / before[0] - 1


def settle_next_close(market, trade, side, counts):
    """Exits at the next trading day's close. Returns a result dict, or None when the trade cannot be valued."""
    at = market.position[trade.day]
    if at + 1 >= len(market.calendar):
        return None
    day = market.calendar[at + 1]
    history = market.closes[trade.ticker]
    entry_row, exit_row = history[trade.day], history.get(day)
    if (
        exit_row is not None
        and exit_row[0] > 0
        and abs((exit_row[1] / exit_row[0]) / (entry_row[1] / entry_row[0]) - 1) > 0.01
    ):
        counts["split during the hold"] += 1
        return None
    quote = (market.chains.get((trade.root, trade.expiry, day)) or {}).get(trade.strike)
    if quote is None or max(quote) <= 0:
        # No quote: halted, delisted or merged. Value at intrinsic, not dropped: these may be the extreme outcomes.
        counts["exit at intrinsic"] += 1
        last = exit_row or next(history[d] for d in reversed(market.calendar[: at + 1]) if d in history)
        value = abs(last[1] - trade.strike)
        exit_mid = exit_bid = exit_ask = value
    else:
        exit_mid = (quote[0] + quote[1] + quote[2] + quote[3]) / 2
        exit_bid, exit_ask = quote[0] + quote[2], quote[1] + quote[3]
    sign = 1 if side == "long" else -1
    usd = sign * ((exit_bid if side == "long" else exit_ask) - trade.fill) * 100 - COMMISSION
    moved = None
    if exit_row is not None:
        moved = abs(exit_row[1] / trade.spot - 1)
    return {
        "day": trade.day,
        "ticker": trade.ticker,
        "premium_usd": trade.fill * 100,
        "notional_usd": trade.strike * 100,
        "gross": sign * (exit_mid - trade.mid) / trade.mid,
        "net": usd / (trade.fill * 100),
        "usd": usd,
        "cost": (trade.fill - trade.bid) / trade.fill if side == "long" else (trade.ask - trade.fill) / trade.fill,
        "straddle_pct_of_spot": trade.mid / trade.spot,
        "next_move": moved,
        "days_to_expiry": (trade.expiry - trade.day).days,
    }


def run_next_close(market, side):
    """Test A: every signal the download covers, with the bot's own three a day marked; and the no-signal days (C1)."""
    fires = (lambda x: x > LONG_THRESHOLD) if side == "long" else (lambda x: x < SHORT_THRESHOLD)
    rows, counts = [], Counter()
    for day in market.calendar:
        week = market.week_of.get(day)
        if week is None or (week, side) not in market.lists:
            continue
        taken = 0
        ran_out = False
        for ticker, root in market.lists[(week, side)]:
            if (week, ticker) not in market.fetched:
                ran_out = True
                continue
            history = market.closes[ticker]
            if day not in history:
                continue
            move = signal_on(history, market, day)
            if math.isnan(move):
                continue
            signal = fires(abs(move))
            trade = enter(market, side, ticker, root, day, counts)
            if trade is None:
                continue
            is_pick = bool(signal and taken < MAX_PER_DAY)
            if is_pick:
                taken += 1  # the slot is used whatever happens next: skipping on later information would be look-ahead
            result = settle_next_close(market, trade, side, counts)
            if result is not None:
                rows.append({**result, "week": week, "signal": signal, "pick": is_pick})
        if taken < MAX_PER_DAY and ran_out:
            counts["days the download ran out of names"] += 1
    return pd.DataFrame(rows), counts


def run_same_day(market):
    """Test B: gaps at the open, modelled entry, real exit quote at the same day's close."""
    rows, counts = [], Counter()
    for at, day in enumerate(market.calendar):
        week = market.week_of.get(day)
        if week is None or (week, "long") not in market.lists or at == 0:
            continue
        previous_day = market.calendar[at - 1]
        taken = 0
        for ticker, root in market.lists[(week, "long")]:
            if taken == MAX_PER_DAY or (week, ticker) not in market.fetched:
                continue
            history = market.closes[ticker]
            if day not in history or previous_day not in history:
                continue
            close, raw, open_, high, low = history[day]
            previous_close = history[previous_day][0]
            if previous_close <= 0 or close <= 0 or abs(open_ / previous_close - 1) <= LONG_THRESHOLD:
                continue
            scale = raw / close  # Sharadar's open, high and low are split-adjusted: back to the prices of the day
            spot_open = open_ * scale
            expiry = nearest_expiry(market.expiries.get(root, []), day)
            chain = market.chains.get((root, expiry, day)) if expiry is not None else None
            if not chain:
                counts["no chain"] += 1
                continue
            strike = nearest_strike(chain, spot_open)
            quote = chain[strike]
            if not two_sided(quote):
                counts["no two-sided quote"] += 1
                continue
            exit_mid = (quote[0] + quote[1] + quote[2] + quote[3]) / 2
            exit_bid = quote[0] + quote[2]
            years = (expiry - day).days / 365
            vol_close = implied_vol(exit_mid, raw, strike, years)
            if vol_close is None:
                counts["no implied vol"] += 1
                continue
            entry_value = straddle_value(spot_open, strike, years + OPEN_TO_CLOSE, vol_close)
            limit = round(entry_value * (1 + BUFFER), 2)
            if limit <= 0 or limit * 100 > MAX_PREMIUM:
                counts["premium cap"] += 1
                continue
            taken += 1
            target = limit * (1 + TAKE_PROFIT)
            extreme = max(
                straddle_value(high * scale, strike, years + OPEN_TO_CLOSE / 2, vol_close),
                straddle_value(low * scale, strike, years + OPEN_TO_CLOSE / 2, vol_close),
            )
            take_profit = extreme >= target
            sold = target * (1 - BUFFER) if take_profit else exit_bid
            marked = target if take_profit else exit_mid
            usd = (sold - limit) * 100 - COMMISSION

            # B2: the same trade, entered at the implied vol of the previous close.
            previous_quote = (market.chains.get((root, expiry, previous_day)) or {}).get(strike)
            usd_b2 = gross_b2 = None
            if previous_quote is not None and two_sided(previous_quote):
                previous_mid = sum(previous_quote) / 2
                previous_raw = history[previous_day][1]
                vol_previous = implied_vol(previous_mid, previous_raw, strike, (expiry - previous_day).days / 365)
                if vol_previous is not None:
                    entry_b2 = straddle_value(spot_open, strike, years + OPEN_TO_CLOSE, vol_previous)
                    limit_b2 = round(entry_b2 * (1 + BUFFER), 2)
                    if limit_b2 > 0:
                        gross_b2 = (exit_mid - entry_b2) / entry_b2
                        usd_b2 = (exit_bid - limit_b2) * 100 - COMMISSION
                        net_b2 = usd_b2 / (limit_b2 * 100)
            rows.append(
                {
                    "day": day,
                    "week": week,
                    "ticker": ticker,
                    "take_profit": take_profit,
                    "premium_usd": limit * 100,
                    "gross": (marked - entry_value) / entry_value,
                    "net": usd / (limit * 100),
                    "usd": usd,
                    "gross_no_tp": (exit_mid - entry_value) / entry_value,
                    "gross_b2": gross_b2,
                    "net_b2": net_b2 if usd_b2 is not None else None,
                    "usd_b2": usd_b2,
                }
            )
    return pd.DataFrame(rows), counts


# --- Inference -------------------------------------------------------------------------------------------------------


def daily_series(trades, column):
    return trades.dropna(subset=[column]).groupby("day")[column].mean()


def newey_west_t(series, lags=5):
    values = series.to_numpy(float)
    values = values - values.mean()
    n = len(values)
    variance = float(np.dot(values, values)) / n
    for lag in range(1, lags + 1):
        variance += 2 * (1 - lag / (lags + 1)) * float(np.dot(values[lag:], values[:-lag])) / n
    return series.mean() / math.sqrt(variance / n) if variance > 0 else math.nan


def block_bootstrap_interval(series, draws=5000, seed=20261105):
    """95% interval of the mean, resampling calendar weeks with replacement."""
    frame = pd.DataFrame({"value": series.to_numpy(float), "week": [d.to_period("W") for d in series.index]})
    weeks = [group.value.to_numpy(float) for _, group in frame.groupby("week")]
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [weeks[rng.randrange(len(weeks))] for _ in weeks]
        total = sum(values.sum() for values in sample)
        means.append(total / sum(len(values) for values in sample))
    return tuple(np.percentile(means, [2.5, 97.5]))


def describe(label, series):
    low, high = block_bootstrap_interval(series)
    print(
        f"  {label:38} mean {series.mean():+7.2%}   95% interval {low:+7.2%} to {high:+7.2%}   NW t {newey_west_t(series):+6.2f}   days {len(series)}"
    )
    return low, high


# --- Synthetic control ---------------------------------------------------------------------------------------------


def synthetic_market(vol_ratio=1.0, days=500, names=40, seed=7, overnight_share=0.3):
    """Random-walk stocks with no drift, quoted at Black-Scholes values with a fixed implied vol.

    The clock is trading days at zero rates, so a straddle's mid is a martingale when ``vol_ratio`` is 1: the honest
    answer for gross P&L is zero. A ratio above 1 means stocks move more than options imply, and gross must be positive.
    Strikes come from one fixed grid, so that a contract still exists the next day, as a real one does. Prices start
    high enough for cent rounding not to matter, and the spread is 1% either side of each leg.
    """
    rng = np.random.default_rng(seed)
    business_days = list(pd.bdate_range("2023-06-05", periods=days + 40))
    calendar = business_days[:days]
    position = {day: at for at, day in enumerate(business_days)}
    fridays = [day for day in business_days if day.weekday() == 4]
    grid = [round(100 * 1.03**j, 2) for j in range(-120, 240)]
    closes, chains, expiries, lists, fetched, week_of = defaultdict(dict), defaultdict(dict), {}, {}, set(), {}
    tickers = [f"T{i:03d}" for i in range(names)]
    for ticker in tickers:
        implied_daily = rng.uniform(0.02, 0.06)
        realized_daily = implied_daily * vol_ratio
        expiries[ticker] = fridays
        spot = 1000.0
        for day in calendar:
            # A share of the day's variance arrives overnight, the rest in half-hour steps through the session.
            night = realized_daily**2 * overnight_share
            opening = spot * math.exp(rng.normal(-night / 2, math.sqrt(night)))
            step = realized_daily**2 * (1 - overnight_share) / 13
            spot = high = low = opening
            for _ in range(13):
                spot *= math.exp(rng.normal(-step / 2, math.sqrt(step)))
                high, low = max(high, spot), min(low, spot)
            closes[ticker][day] = (spot, spot, opening, high, low)
            for expiry in [friday for friday in fridays if timedelta(days=0) <= friday - day <= timedelta(days=21)]:
                left = position[expiry] - position[day]
                for strike in [k for k in grid if 0.6 * spot <= k <= 1.6 * spot]:
                    total = straddle_value(spot, strike, left, implied_daily, rate=0.0)
                    call = (total + spot - strike) / 2
                    put = total - call
                    chains[(ticker, expiry, day)][strike] = (
                        round(call * 0.99, 2),
                        round(call * 1.01, 2),
                        round(put * 0.99, 2),
                        round(put * 1.01, 2),
                    )
    for day in calendar:
        week = day - timedelta(days=day.weekday())
        week_of[day] = week
        lists[(week, "long")] = lists[(week, "short")] = [(ticker, ticker) for ticker in tickers]
        fetched.update((week, ticker) for ticker in tickers)
    return Market(closes, calendar, lists, fetched, week_of, expiries, chains)


def run_null_short():
    global MAX_NOTIONAL
    MAX_NOTIONAL = math.inf  # synthetic prices are high on purpose: the cap would remove most trades
    print("Short side, synthetic controls through the same evaluator (gross, per-day means):")
    for label, ratio, expectation in (
        ("implied = true vol", 1.0, "must be indistinguishable from 0"),
        ("true vol 25% above implied", 1.25, "must be clearly negative for a seller"),
    ):
        results = [run_next_close(synthetic_market(vol_ratio=ratio, seed=seed), "short")[0] for seed in (7, 8, 9)]
        trades = pd.concat(results)
        print(f" {label} ({expectation}); signal trades {trades.signal.sum()}:")
        describe("every signal, gross", daily_series(trades[trades.signal], "gross"))
        describe("no-signal days, gross", daily_series(trades[~trades.signal], "gross"))
        print(f"  median round-trip cost {trades.cost.median():.1%}")
        if ratio == 1.0:
            describe("every signal, net (must be below 0)", daily_series(trades[trades.signal], "net"))


def run_null():
    global MAX_PREMIUM
    MAX_PREMIUM = math.inf  # synthetic prices are high on purpose: the cap would remove every trade
    print("C2, synthetic controls through the same evaluator (gross, per-day means):")
    for label, ratio, expectation in (
        ("implied = true vol", 1.0, "must be indistinguishable from 0"),
        ("true vol 25% above implied", 1.25, "must be clearly positive"),
    ):
        results, counts = [], Counter()
        for seed in (7, 8, 9):
            trades, seed_counts = run_next_close(synthetic_market(vol_ratio=ratio, seed=seed), "long")
            results.append(trades)
            counts.update(seed_counts)
        trades = pd.concat(results)
        print(f" {label} ({expectation}); signal trades {trades.signal.sum()}; counts {dict(counts)}:")
        describe("every signal, gross", daily_series(trades[trades.signal], "gross"))
        describe("no-signal days, gross", daily_series(trades[~trades.signal], "gross"))
        print(f"  median round-trip cost {trades.cost.median():.1%}")
        if ratio == 1.0:
            describe("every signal, net (must be below 0)", daily_series(trades[trades.signal], "net"))

    print("\nTest B through a market with no edge: any positive gross here is made by the tilts, not by the market.")
    for share in (0.3, 0.6):
        same_day = pd.concat(
            [run_same_day(synthetic_market(vol_ratio=1.0, seed=seed, overnight_share=share))[0] for seed in (7, 8, 9)]
        )
        print(
            f" {share:.0%} of daily variance overnight; trades {len(same_day)}; take-profit credited on {same_day.take_profit.mean():.1%}"
        )
        describe("B1 gross", daily_series(same_day, "gross"))
        describe("B1 gross, without the take-profit credit", daily_series(same_day, "gross_no_tp"))
        describe("B2 gross", daily_series(same_day, "gross_b2"))


# --- Reports -----------------------------------------------------------------------------------------------------------


def run_power(side):
    market = load_market(side)
    trades, counts = run_next_close(market, side)
    picks = trades[trades.pick]
    print(
        f"{side}: weeks with quotes {picks.week.nunique()} of {len({w for w, s in market.lists if s == side})}; bot's trades {len(picks)}; days {picks.day.nunique()}"
    )
    print("counts:", dict(counts))
    series = daily_series(picks, "net")
    trade_sd, day_sd = picks.net.std(), series.std()
    se = day_sd / math.sqrt(len(series))
    print(f"standard deviation per trade {trade_sd:.1%}; of the daily portfolio {day_sd:.1%}; standard error {se:.2%}")
    print(f"minimum detectable effect, 2.8 x SE: {2.8 * se:.2%} of premium per trade")
    print(
        f"round-trip cost, median {picks.cost.median():.1%}, mean {picks.cost.mean():.1%} of premium; premium median ${picks.premium_usd.median():,.0f}"
    )
    print(
        f"straddle as a share of spot, median {picks.straddle_pct_of_spot.median():.1%}; days to expiry, median {picks.days_to_expiry.median():.0f}"
    )


def run_results(side):
    market = load_market(side)
    trades, counts = run_next_close(market, side)
    picks = trades[trades.pick]
    print(
        f"=== {side} side, test A: next-close hold, real quotes ({picks.day.min():%Y-%m-%d} to {picks.day.max():%Y-%m-%d})"
    )
    print("counts:", dict(counts))
    print(
        f"bot's trades {len(picks)}, every signal in the download {trades.signal.sum()}, no-signal {(~trades.signal).sum()}"
    )
    net = daily_series(picks, "net")
    describe("bot's picks, net of costs", net)
    describe("bot's picks, gross (mid to mid)", daily_series(picks, "gross"))
    usd = picks.groupby("day").usd.sum()
    curve = usd.cumsum()
    print(
        f"  dollars at one contract each: total {usd.sum():+,.0f}, per year {usd.mean() * 252:+,.0f}, worst drawdown {(curve - curve.cummax()).min():+,.0f}"
    )
    print(
        f"  share of trades that made money: {(picks.usd > 0).mean():.1%}; median {picks.net.median():+.1%}; best {picks.net.max():+.0%}; worst {picks.net.min():+.0%}"
    )
    print("  by year (net, per-day mean):")
    for year, group in net.groupby(net.index.year):
        print(f"    {year}: {group.mean():+7.2%}  days {len(group)}  dollars {usd[usd.index.year == year].sum():+,.0f}")
    if side == "short":
        print(
            f"  tail: worst trade {picks.usd.min():+,.0f} USD; worst day {usd.min():+,.0f} USD; mean of the worst 5% of days {usd.nsmallest(max(1, len(usd) // 20)).mean():+,.0f} USD"
        )
        print(f"  worst trade as a share of notional: {(picks.usd / picks.notional_usd).min():.1%}")

    print("\nC1, does the signal add anything? Gross, same stock and week, signal days minus no-signal days:")
    matched = trades.groupby(["week", "ticker", "signal"]).gross.mean().unstack("signal").dropna()
    matched = matched.rename(columns={True: "signal", False: "no_signal"})
    by_week = matched.groupby("week").mean()
    by_week.index = pd.to_datetime(by_week.index)
    describe("signal days", by_week.signal)
    describe("no-signal days", by_week.no_signal)
    describe("signal minus no-signal", by_week.signal - by_week.no_signal)

    print("\nC3, the alphabet: every signal in the downloaded names, against the bot's three")
    describe("every signal, net", daily_series(trades[trades.signal], "net"))

    if side == "long":
        same_day, counts_b = run_same_day(market)
        print("\n=== Test B: same-day, modelled entry, tilted in the bot's favour")
        print("counts:", dict(counts_b))
        print(f"trades {len(same_day)}; take-profit credited on {same_day.take_profit.mean():.1%}")
        describe("B1 constant implied vol, net", daily_series(same_day, "net"))
        describe("B1 constant implied vol, gross", daily_series(same_day, "gross"))
        describe("B1 gross, without the take-profit credit", daily_series(same_day, "gross_no_tp"))
        describe("B2 previous close's implied vol, net", daily_series(same_day, "net_b2"))
        describe("B2 previous close's implied vol, gross", daily_series(same_day, "gross_b2"))
        print(
            f"  B1 dollars at one contract each: {same_day.usd.sum():+,.0f}, per year {same_day.groupby('day').usd.sum().mean() * 252:+,.0f}"
        )
        same_day.to_csv(DATA / "same_day_long.csv", index=False)
    trades.to_csv(DATA / f"trades_{side}.csv", index=False)


if __name__ == "__main__":
    command = sys.argv[1]
    chosen_side = sys.argv[2] if len(sys.argv) > 2 else "long"
    {
        "null": lambda: run_null_short() if chosen_side == "short" else run_null(),
        "power": lambda: run_power(chosen_side),
        "results": lambda: run_results(chosen_side),
    }[command]()
