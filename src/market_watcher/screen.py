"""Screens a stock universe by historical volatility to produce the target stocks file.

Reproduces the research step described in the README: for every stock in the
universe (the Russell 3000 by default) compute the historical volatility over the
last week, two weeks and month of trading days, combine them as

    weighted volatility = 0.5 * vol(5 days) + 0.3 * vol(10 days) + 0.2 * vol(21 days)

and pick the N most volatile stocks as long straddle candidates and the N least
volatile as short straddle candidates.

Volatility is the sample standard deviation of daily log returns. It is not
annualised: that is a constant factor and does not change the ranking. Fetching
prices needs the optional ``research`` extra (``pip install "market_watcher[research]"``,
which brings yfinance); everything else is standard library.
"""

import csv
import logging
import math
import re
import statistics
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from market_watcher.common import STRATEGIES
from market_watcher.market_data import quiet_yfinance_logging
from market_watcher.version import VERSION

logger = logging.getLogger(__name__)

WINDOWS = (5, 10, 21)  # trading days: one week, two weeks, one month
WEIGHTS = (0.5, 0.3, 0.2)
REQUIRED_CLOSES = max(WINDOWS) + 1
DOLLAR_VOLUME_DAYS = max(WINDOWS)

DEFAULT_OUTPUT = "src/market_watcher/research/target_stocks.yaml"
IWV_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/1467271812596.ajax"
    "?fileType=csv&fileName=IWV_holdings&dataType=fund"
)

# Yahoo-style tickers: AAPL, BRK-B, RDS-A; share classes are separated by '-'.
TICKER_PATTERN = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,3})?$")


@dataclass(frozen=True)
class History:
    """Daily closes (split and dividend adjusted) and volumes of one stock, oldest first."""

    closes: list[float]
    volumes: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class Ranked:
    ticker: str
    weighted_volatility: float
    volatilities: tuple[float, ...]  # one per entry of WINDOWS
    last_close: float
    avg_dollar_volume: float


@dataclass
class ScreenResult:
    ranked: list[Ranked]  # every eligible stock, most volatile first
    skipped: Counter
    top: int
    universe_size: int = 0
    fetched: int = 0

    @property
    def long_straddle(self):
        """The ``top`` most volatile stocks, most volatile first."""
        return self.ranked[: self.top]

    @property
    def short_straddle(self):
        """The ``top`` least volatile stocks (never overlapping the long list), least volatile first."""
        start = max(self.top, len(self.ranked) - self.top)
        return list(reversed(self.ranked[start:]))


# --- Volatility ------------------------------------------------------------------------------------------------------


def log_returns(closes):
    return [math.log(current / previous) for previous, current in zip(closes, closes[1:], strict=False)]


def _valid_closes(closes):
    return all(close is not None and not math.isnan(close) and close > 0 for close in closes)


def volatilities(closes, windows=WINDOWS):
    """Sample standard deviation of the daily log returns over the last ``window`` returns, for each window.

    Returns None when fewer than ``REQUIRED_CLOSES`` valid closes are available.
    """
    recent = list(closes)[-REQUIRED_CLOSES:]
    if len(recent) < REQUIRED_CLOSES or not _valid_closes(recent):
        return None
    returns = log_returns(recent)
    return tuple(statistics.stdev(returns[-window:]) for window in windows)


def weighted_volatility(vols, weights=WEIGHTS):
    return sum(weight * vol for weight, vol in zip(weights, vols, strict=True))


def average_dollar_volume(history, days=DOLLAR_VOLUME_DAYS):
    """Mean of close * volume over the last ``days`` sessions; 0 when no volume data is available."""
    pairs = [
        (close, volume)
        for close, volume in zip(history.closes[-days:], history.volumes[-days:], strict=False)
        if close is not None and volume is not None and not math.isnan(close) and not math.isnan(volume)
    ]
    if not pairs:
        return 0.0
    return statistics.fmean(close * volume for close, volume in pairs)


def screen(histories, top=50, min_price=5.0, min_dollar_volume=5_000_000.0):
    """Ranks ``histories`` (ticker -> History) by weighted volatility after applying the liquidity filters."""
    ranked = []
    skipped = Counter()
    for ticker, history in histories.items():
        vols = volatilities(history.closes)
        if vols is None:
            skipped[f"fewer than {REQUIRED_CLOSES} valid closes"] += 1
            continue
        last_close = history.closes[-1]
        if last_close < min_price:
            skipped[f"last close below {min_price:g}"] += 1
            continue
        dollar_volume = average_dollar_volume(history)
        if dollar_volume < min_dollar_volume:
            skipped[f"average dollar volume below {min_dollar_volume:,.0f}"] += 1
            continue
        ranked.append(Ranked(ticker, weighted_volatility(vols), vols, last_close, dollar_volume))

    ranked.sort(key=lambda item: (-item.weighted_volatility, item.ticker))
    return ScreenResult(ranked=ranked, skipped=skipped, top=top)


# --- Universe --------------------------------------------------------------------------------------------------------


def normalize_ticker(raw):
    """Yahoo-style ticker (``BRK.B`` / ``BRK/B`` -> ``BRK-B``), or None for anything that is not a ticker."""
    ticker = (raw or "").strip().strip('"').upper().replace(".", "-").replace("/", "-")
    return ticker if TICKER_PATTERN.match(ticker) else None


def _first_field(line):
    return next(csv.reader([line]), [""])[0].strip() if line else ""


def parse_universe(text):
    """Tickers from a plain list (one per line), a CSV with a Ticker or Symbol column, or an iShares holdings export.

    iShares exports start with a few preamble lines and end with a disclaimer; both are
    ignored, and only rows whose ``Asset Class`` is ``Equity`` are kept.
    """
    lines = [line.strip() for line in text.splitlines()]
    header = next((index for index, line in enumerate(lines) if _first_field(line) in ("Ticker", "Symbol")), None)
    if header is None:
        raw_tickers = [_first_field(line) for line in lines]
    else:
        reader = csv.DictReader(lines[header:])
        column = "Ticker" if "Ticker" in reader.fieldnames else "Symbol"
        rows = [row for row in reader if row.get(column)]
        if "Asset Class" in reader.fieldnames:
            rows = [row for row in rows if (row.get("Asset Class") or "").strip() == "Equity"]
        raw_tickers = [row[column] for row in rows]

    tickers = []
    for raw in raw_tickers:
        ticker = normalize_ticker(raw)
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return tickers


def load_universe(source="iwv"):
    """Tickers from the iShares Russell 3000 ETF holdings (``iwv``) or from a local file."""
    if source.strip().lower() == "iwv":
        logger.info("Downloading the iShares Russell 3000 ETF (IWV) holdings")
        request = urllib.request.Request(IWV_HOLDINGS_URL, headers={"User-Agent": f"market_watcher/{VERSION}"})
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8-sig")
    else:
        text = Path(source).read_text(encoding="utf-8-sig")
    tickers = parse_universe(text)
    if not tickers:
        raise ValueError(f"No tickers found in universe {source!r}")
    return tickers


# --- Prices ----------------------------------------------------------------------------------------------------------


def histories_from_frame(data, tickers):
    """Converts a yfinance download (``group_by="ticker"``) into History objects, dropping days without a close."""
    histories = {}
    grouped = hasattr(data.columns, "levels")  # a MultiIndex: (ticker, field)
    for ticker in tickers:
        if grouped:
            if ticker not in data.columns.get_level_values(0):
                continue
            frame = data[ticker]
        else:
            frame = data
        if "Close" not in frame:
            continue
        frame = frame.dropna(subset=["Close"])
        if frame.empty:
            continue
        closes = [float(close) for close in frame["Close"]]
        volumes = (
            [float(volume) if volume == volume else 0.0 for volume in frame["Volume"]] if "Volume" in frame else []
        )
        histories[ticker] = History(closes=closes, volumes=volumes)
    return histories


def fetch_history(tickers, period="3mo", batch_size=200):
    """Three months of daily adjusted closes and volumes from Yahoo Finance, through yfinance, in batches."""
    try:
        import yfinance
    except ImportError:
        raise RuntimeError('Screening needs the yfinance package: pip install "market_watcher[research]"') from None

    quiet_yfinance_logging()
    tickers = list(tickers)
    histories = {}
    for start in range(0, len(tickers), batch_size):
        batch = tickers[start : start + batch_size]
        logger.info("Downloading price history for tickers %d-%d of %d", start + 1, start + len(batch), len(tickers))
        data = yfinance.download(
            batch, period=period, interval="1d", group_by="ticker", auto_adjust=True, threads=True, progress=False
        )
        histories.update(histories_from_frame(data, batch))
    return histories


# --- Output ----------------------------------------------------------------------------------------------------------


def write_target_stocks(path, result, universe="", parameters="", generated_on=None):
    """Writes the long and short straddle candidates in the target stocks YAML format, with a provenance header."""
    long_straddle = sorted(result.long_straddle, key=lambda item: item.ticker)
    short_straddle = sorted(result.short_straddle, key=lambda item: item.ticker)
    header = [
        f"# Generated by `market_watcher_cli screen` on {generated_on or date.today()}.",
        f"# Universe: {universe}; {parameters}.",
        "# Weighted volatility = 0.5 * vol(5d) + 0.3 * vol(10d) + 0.2 * vol(21d) of daily log returns;",
        f"# {len(long_straddle)} most volatile -> long straddle, "
        f"{len(short_straddle)} least volatile -> short straddle.",
    ]
    mapping = {}
    for item in long_straddle:
        mapping[item.ticker] = {"strategy": STRATEGIES.LONG_STRADDLE.value}
    for item in short_straddle:
        mapping[item.ticker] = {"strategy": STRATEGIES.SHORT_STRADDLE.value}
    # safe_dump quotes keys that YAML would otherwise read as something else (the ticker ON would become `true`).
    body = yaml.safe_dump(mapping, sort_keys=False, default_flow_style=False)
    Path(path).write_text("\n".join(header) + "\n\n" + body)


def write_report(path, result):
    """Writes every eligible stock with its volatilities and selection as CSV, most volatile first."""
    long_tickers = {item.ticker for item in result.long_straddle}
    short_tickers = {item.ticker for item in result.short_straddle}
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "ticker",
                "weighted_volatility",
                *(f"vol_{window}d" for window in WINDOWS),
                "last_close",
                "avg_dollar_volume",
                "strategy",
            ]
        )
        for item in result.ranked:
            if item.ticker in long_tickers:
                strategy = STRATEGIES.LONG_STRADDLE.value
            elif item.ticker in short_tickers:
                strategy = STRATEGIES.SHORT_STRADDLE.value
            else:
                strategy = ""
            writer.writerow(
                [
                    item.ticker,
                    f"{item.weighted_volatility:.6f}",
                    *(f"{vol:.6f}" for vol in item.volatilities),
                    f"{item.last_close:.2f}",
                    f"{item.avg_dollar_volume:.0f}",
                    strategy,
                ]
            )


def run(
    universe="iwv",
    top=50,
    min_price=5.0,
    min_dollar_volume=5_000_000.0,
    output=DEFAULT_OUTPUT,
    report=None,
    load=None,
    fetch=None,
):
    """Loads the universe, fetches prices, screens, and writes the target stocks file (and optionally a report)."""
    load = load or load_universe
    fetch = fetch or fetch_history

    tickers = load(universe)
    histories = fetch(tickers)
    result = screen(histories, top=top, min_price=min_price, min_dollar_volume=min_dollar_volume)
    result.universe_size = len(tickers)
    result.fetched = len(histories)

    if not result.ranked:
        raise ValueError("No stock passed the filters; nothing was written")
    if len(result.ranked) < 2 * top:
        logger.warning("Only %d eligible stocks for 2 x %d slots; the lists will be shorter", len(result.ranked), top)

    parameters = f"top {top} per strategy, min price {min_price:g}, min average dollar volume {min_dollar_volume:,.0f}"
    write_target_stocks(output, result, universe=universe, parameters=parameters)
    if report:
        write_report(report, result)
    return result
