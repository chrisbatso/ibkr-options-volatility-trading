"""Screens a stock universe by historical volatility to produce the target stocks file.

Reproduces the research step described in the README: for every stock in the
universe (the Russell 3000 by default) compute the historical volatility over the
last week, two weeks and month of trading days, combine them as

    weighted volatility = 0.5 * vol(5 days) + 0.3 * vol(10 days) + 0.2 * vol(21 days)

and pick the N most volatile stocks as long straddle candidates and the N least
volatile as short straddle candidates.

Volatility is the sample standard deviation of daily log returns. It is not
annualised: that is a constant factor and does not change the ranking. Prices come
from Yahoo Finance through yfinance; everything else is standard library.
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
# A stock whose daily moves average well under half a percent is almost always pinned to a pending cash takeover:
# no premium to sell and a gap if the deal breaks. Normal low-volatility stocks (utilities, insurers) sit near 1%.
DEFAULT_MIN_VOLATILITY = 0.005
YFINANCE_MISSING = "Screening needs the yfinance package (a core dependency): pip install -e ."

DEFAULT_OUTPUT = "src/market_watcher/research/target_stocks.yaml"
IWV_PRODUCT_PAGE = "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf"
IWV_HOLDINGS_URL = f"{IWV_PRODUCT_PAGE}/1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund"
# Every security listed on a US exchange, maintained by Nasdaq: the Nasdaq-listed ones and the other exchanges.
NASDAQ_TRADER_URLS = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)
UNIVERSES = ("iwv", "nasdaq")
# Downloads identify themselves honestly. A site that wants a browser answers a script with an HTML bot check or
# consent page instead of the file; download_text() reports that rather than parsing it (or working around it).
DOWNLOAD_HEADERS = {"User-Agent": f"market_watcher/{VERSION}", "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.8"}

# Yahoo-style tickers: AAPL, BRK-B, RDS-A; share classes are separated by '-'.
TICKER_PATTERN = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,3})?$")
# Nasdaq Integrated symbology: letters, optionally '.X' for a share class. '-' (preferred), '+' (warrant),
# '=' (unit), '^' (right) and '#' (when issued) mark securities that are not common stock.
NASDAQ_STOCK_PATTERN = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
# Securities whose name says they are not common stock, whatever their symbol looks like.
NOT_A_STOCK = re.compile(
    r"\b(warrants?|rights?|units?|preferred|preference|notes?|debentures?|bonds?|funds?|etns?|when[- ]issued)\b",
    re.IGNORECASE,
)


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
    no_options: set = field(default_factory=set)  # tickers found to have no listed options
    universe_size: int = 0
    fetched: int = 0

    @property
    def long_straddle(self):
        """The ``top`` most volatile stocks with listed options, most volatile first."""
        return self._pick(self.ranked)

    @property
    def short_straddle(self):
        """The ``top`` least volatile stocks with options (never overlapping the long list), least volatile first."""
        long_tickers = {item.ticker for item in self.long_straddle}
        return self._pick(item for item in reversed(self.ranked) if item.ticker not in long_tickers)

    def _pick(self, candidates):
        picked = []
        for item in candidates:
            if len(picked) >= self.top:
                break
            if item.ticker not in self.no_options:
                picked.append(item)
        return picked


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


def screen(
    histories,
    top=50,
    min_price=5.0,
    min_dollar_volume=5_000_000.0,
    min_volatility=DEFAULT_MIN_VOLATILITY,
    has_options=None,
):
    """Ranks ``histories`` (ticker -> History) by weighted volatility after applying the filters.

    ``has_options(ticker)`` (True, False, or None when unknown) is asked only about the
    candidates at either end of the ranking, until ``top`` per side qualify: a straddle
    cannot be opened on a stock without listed options, so those are skipped.
    """
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
        volatility = weighted_volatility(vols)
        if volatility < min_volatility:
            skipped[f"weighted volatility below {min_volatility:.1%} (likely pinned to a takeover price)"] += 1
            continue
        ranked.append(Ranked(ticker, volatility, vols, last_close, dollar_volume))

    ranked.sort(key=lambda item: (-item.weighted_volatility, item.ticker))
    result = ScreenResult(ranked=ranked, skipped=skipped, top=top)
    if has_options is not None:
        _check_options(result, has_options)
    return result


def _check_options(result, has_options):
    """Asks ``has_options`` about the candidates at both ends of the ranking until ``top`` per side qualify.

    Walks the ranking the same way the ``long_straddle`` / ``short_straddle`` properties do,
    recording the tickers without options in ``result.no_options`` as it goes; an unknown
    answer (None) keeps the candidate.
    """

    def walk(candidates):
        picked = 0
        for item in candidates:
            if picked >= result.top:
                break
            if has_options(item.ticker) is False:
                result.no_options.add(item.ticker)
                result.skipped["no listed options"] += 1
            else:
                picked += 1

    walk(result.ranked)
    long_tickers = {item.ticker for item in result.long_straddle}
    walk(item for item in reversed(result.ranked) if item.ticker not in long_tickers)


# --- Universe --------------------------------------------------------------------------------------------------------


def normalize_ticker(raw):
    """Yahoo-style ticker (``BRK.B`` / ``BRK/B`` -> ``BRK-B``), or None for anything that is not a ticker."""
    ticker = (raw or "").strip().strip('"').upper().replace(".", "-").replace("/", "-")
    return ticker if TICKER_PATTERN.match(ticker) else None


def _first_field(line):
    return next(csv.reader([line]), [""])[0].strip() if line else ""


def looks_like_html(text):
    """True for an HTML document: what a download gets instead of the data when a site wants a browser."""
    head = text[:2048].lstrip().lower()
    return head.startswith("<") or "<html" in head or "<!doctype" in head


def parse_universe(text):
    """Tickers from a plain list (one per line), a CSV with a Ticker or Symbol column, or an iShares holdings export.

    iShares exports start with a few preamble lines and end with a disclaimer; both are
    ignored, and only rows whose ``Asset Class`` is ``Equity`` are kept.
    """
    if looks_like_html(text):
        raise ValueError("the universe is an HTML page, not a list of tickers")
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


def parse_nasdaq_directory(text):
    """Common-stock tickers (Yahoo style) from a Nasdaq Trader symbol directory file.

    ``nasdaqlisted.txt`` and ``otherlisted.txt`` are pipe-delimited, cover every security on
    a US exchange and end with a ``File Creation Time`` line. ETFs, test issues, bankrupt
    issuers and anything that is not common stock (warrants, rights, units, preferreds,
    notes, funds) are dropped.
    """
    if looks_like_html(text):
        raise ValueError("the symbol directory is an HTML page, not a Nasdaq Trader file")
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("File Creation Time")]
    reader = csv.DictReader(lines, delimiter="|")
    column = next((name for name in ("NASDAQ Symbol", "Symbol") if name in (reader.fieldnames or [])), None)
    if column is None:
        raise ValueError("not a Nasdaq Trader symbol directory: no Symbol column")

    tickers = []
    for row in reader:
        symbol = (row.get(column) or "").strip()
        if not NASDAQ_STOCK_PATTERN.match(symbol):
            continue
        if (row.get("ETF") or "").strip() == "Y" or (row.get("Test Issue") or "").strip() == "Y":
            continue
        if any(flag in (row.get("Financial Status") or "") for flag in "QGJK"):  # bankrupt, alone or combined
            continue
        if NOT_A_STOCK.search(row.get("Security Name") or ""):
            continue
        ticker = normalize_ticker(symbol)
        if ticker:
            tickers.append(ticker)
    return list(dict.fromkeys(tickers))


def download_text(url, timeout=60):
    """The text at ``url``; raises if the site answered with an HTML page instead of the data file."""
    request = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8-sig", errors="replace")
    if looks_like_html(text):
        raise ValueError(f"{url} answered with an HTML page instead of the data file")
    return text


def load_universe(source="iwv"):
    """Tickers from the iShares Russell 3000 ETF holdings (``iwv``), the Nasdaq Trader symbol directory of every
    US-listed stock (``nasdaq``), or a local file."""
    name = source.strip().lower()
    if name == "iwv":
        logger.info("Downloading the iShares Russell 3000 ETF (IWV) holdings")
        try:
            tickers = parse_universe(download_text(IWV_HOLDINGS_URL))
        except (ValueError, OSError) as e:
            raise ValueError(
                f"{e}. iShares does not always serve the file to scripts: save the holdings CSV from "
                f"{IWV_PRODUCT_PAGE} and pass it with --universe PATH, or screen every US-listed stock with "
                "--universe nasdaq"
            ) from e
    elif name == "nasdaq":
        logger.info("Downloading the Nasdaq Trader symbol directory (every US-listed stock)")
        tickers = []
        for url in NASDAQ_TRADER_URLS:
            tickers.extend(parse_nasdaq_directory(download_text(url)))
        tickers = list(dict.fromkeys(tickers))
    else:
        path = Path(source)
        if not path.is_file():
            choices = " or ".join(f"'{name}'" for name in UNIVERSES)
            raise ValueError(f"universe {source!r} is not a file; use {choices}, or the path of a saved ticker list")
        tickers = parse_universe(path.read_text(encoding="utf-8-sig"))
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
        raise RuntimeError(YFINANCE_MISSING) from None

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


def yfinance_has_options(ticker):
    """Whether Yahoo Finance lists option expirations for ``ticker`` (one request); None when it could not tell."""
    try:
        import yfinance
    except ImportError:
        raise RuntimeError(YFINANCE_MISSING) from None

    quiet_yfinance_logging()
    try:
        return bool(yfinance.Ticker(ticker).options)
    except Exception as e:  # a network hiccup must not drop a candidate
        logger.warning("Could not check whether %s has listed options (%s); keeping it", ticker, e)
        return None


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
    min_volatility=DEFAULT_MIN_VOLATILITY,
    require_options=True,
    output=DEFAULT_OUTPUT,
    report=None,
    load=None,
    fetch=None,
    has_options=None,
):
    """Loads the universe, fetches prices, screens, and writes the target stocks file (and optionally a report)."""
    load = load or load_universe
    fetch = fetch or fetch_history
    if has_options is None and require_options:
        has_options = yfinance_has_options

    tickers = load(universe)
    histories = fetch(tickers)
    if tickers and not histories:
        raise ValueError(
            f"No price history came back for any of the {len(tickers)} tickers in the universe; "
            "is Yahoo Finance reachable?"
        )
    result = screen(
        histories,
        top=top,
        min_price=min_price,
        min_dollar_volume=min_dollar_volume,
        min_volatility=min_volatility,
        has_options=has_options,
    )
    result.universe_size = len(tickers)
    result.fetched = len(histories)

    if not result.ranked:
        raise ValueError("No stock passed the filters; nothing was written")
    if len(result.ranked) < 2 * top:
        logger.warning("Only %d eligible stocks for 2 x %d slots; the lists will be shorter", len(result.ranked), top)

    options = "listed options required" if has_options else "options not checked"
    parameters = (
        f"top {top} per strategy, min price {min_price:g}, min average dollar volume {min_dollar_volume:,.0f}, "
        f"min volatility {min_volatility:.1%}, {options}"
    )
    write_target_stocks(output, result, universe=universe, parameters=parameters)
    if report:
        write_report(report, result)
    return result
