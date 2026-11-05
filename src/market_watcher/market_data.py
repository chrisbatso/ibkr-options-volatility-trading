"""Market data sources for the MarketWatcher engine.

A provider answers one question: what is the daily percent change of each
target stock right now? To add a source, subclass :class:`MarketDataProvider`,
give it a ``name``, implement ``get_daily_pnls`` and register the class in
:data:`PROVIDERS`.
"""

import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime, timezone

from yahoofinancials import YahooFinancials

from market_watcher.config import config_int, context
from market_watcher.market_hours import USMarketHours

logger = logging.getLogger(__name__)


class MarketDataProvider(ABC):
    """Source of daily P&L figures for a set of tickers."""

    #: Short identifier used in the MARKET_DATA_PROVIDER setting.
    name: str

    @abstractmethod
    def get_daily_pnls(self, tickers: Iterable[str]) -> dict[str, float | None]:
        """Returns the daily percent change of each ticker as a fraction (``0.02`` == 2%).

        The result has an entry for every requested ticker; the value is ``None``
        when no figure is available (unknown or delisted ticker, data outage, ...).
        Transient failures may be raised: the engine logs them and retries on its
        next polling cycle.
        """

    def close(self):  # noqa: B027 - an intentional no-op default, not a hook every provider must implement
        """Releases resources held by the provider (connections, sessions). The default does nothing."""


class YahooFinanceProvider(MarketDataProvider):
    """Daily P&L from Yahoo Finance through the ``yahoofinancials`` package: one HTTP request per ticker.

    Kept as a fallback; :class:`YFinanceProvider` fetches the whole universe in one batched call.
    """

    name = "yahoofinancials"

    def __init__(self, client_factory=YahooFinancials):
        self._client_factory = client_factory

    def get_daily_pnls(self, tickers):
        tickers = list(tickers)
        if not tickers:
            return {}
        raw = self._client_factory(tickers).get_current_percent_change() or {}
        return {ticker: _as_fraction(raw.get(ticker)) for ticker in tickers}


def _as_fraction(value):
    """Keeps numeric values (as float); anything else Yahoo may return (None, 'N/A', ...) becomes None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class YFinanceProvider(MarketDataProvider):
    """Daily P&L from Yahoo Finance through ``yfinance``: the whole universe in one batched download per cycle.

    The change is the last close over the previous close of the split/dividend-adjusted
    daily bars. During a session the last bar is today's, still forming, so this is the
    live move; a ticker with no bar for today yet (the first poll runs at the open) has no
    figure, because the bar it does have is yesterday's move. Outside sessions the newest
    bar is reported, like Yahoo's own quote.
    """

    name = "yfinance"

    def __init__(self, download=None, market_hours=None, clock=None):
        self._download = download
        self._market_hours = market_hours if market_hours is not None else USMarketHours()
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        quiet_yfinance_logging()

    def get_daily_pnls(self, tickers):
        tickers = list(tickers)
        if not tickers:
            return {}
        if self._download is None:
            try:
                import yfinance
            except ImportError:
                raise RuntimeError(
                    "The yfinance market data provider needs the yfinance package: pip install yfinance"
                ) from None
            self._download = yfinance.download
        data = self._download(
            tickers, period="5d", interval="1d", group_by="ticker", auto_adjust=True, threads=True, progress=False
        )
        closes = {ticker: _closes(data, ticker) for ticker in tickers}
        if all(len(series) < 2 for series in closes.values()):
            # yfinance swallows network errors and returns an empty frame; make that a loud, retried failure.
            raise RuntimeError(
                f"yfinance returned no data for any of {len(tickers)} ticker(s): network problem, Yahoo rate limit, "
                "or every ticker unknown"
            )
        session = self._market_hours.current_session(self._clock())
        session_day = session.day if session is not None else None
        return {ticker: _daily_change(series, session_day) for ticker, series in closes.items()}


def quiet_yfinance_logging():
    """Silences yfinance's own ERROR lines (one per delisted ticker, every download); we warn once per ticker."""
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def _closes(data, ticker):
    """Valid closes of ``ticker`` as a dated series (oldest first), from a download grouped by ticker or not."""
    grouped = hasattr(data.columns, "levels")  # a MultiIndex: (ticker, field)
    try:
        frame = data[ticker] if grouped else data
        series = frame["Close"]
    except KeyError:
        return []
    return series.dropna()


def _daily_change(closes, session_day=None):
    """The newest close over the one before it; None when, during session ``session_day``, the newest is not today's."""
    if len(closes) < 2:
        return None
    if session_day is not None and closes.index[-1].date() != session_day:
        return None
    previous, latest = float(closes.iloc[-2]), float(closes.iloc[-1])
    if previous <= 0:
        return None
    return (latest - previous) / previous


class IBKRProvider(MarketDataProvider):
    """Daily P&L from Interactive Brokers TWS / IB Gateway through the ``ib_async`` package.

    One market data snapshot per ticker; the daily percent change is
    ``(current price - previous close) / previous close`` where the current price
    is the last trade or, before the first trade, the bid/ask midpoint. Connects
    lazily (read-only) on the first request and reconnects if the link drops.
    Requires the optional dependency: ``pip install "market_watcher[ibkr]"``.
    """

    name = "ibkr"

    def __init__(self, host=None, port=None, client_id=None, market_data_type=None, timeout=None, ib_factory=None):
        self.host = host if host is not None else (context.config.get("IBKR_HOST") or "127.0.0.1")
        self.port = port if port is not None else config_int("IBKR_PORT", default=7497)
        self.client_id = client_id if client_id is not None else config_int("IBKR_CLIENT_ID", default=1)
        self.market_data_type = (
            market_data_type if market_data_type is not None else config_int("IBKR_MARKET_DATA_TYPE", default=3)
        )
        self.timeout = timeout if timeout is not None else config_int("IBKR_TIMEOUT", default=10)
        self._ib_factory = ib_factory
        self._ib = None
        self._contracts = {}  # ticker -> qualified contract, per connection
        self._unresolved = set()  # tickers IB could not resolve, per connection

    def get_daily_pnls(self, tickers):
        tickers = list(tickers)
        if not tickers:
            return {}
        ib = self._connect()
        contracts = self._qualify(ib, tickers)
        result = dict.fromkeys(tickers)
        if contracts:
            snapshots = ib.reqTickers(*contracts.values())
            for ticker, snapshot in zip(contracts, snapshots, strict=True):
                result[ticker] = _percent_change(current_price(snapshot), snapshot.close)
        return result

    def close(self):
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
            logger.info("Disconnected from IBKR")
        self._ib = None

    def _connect(self):
        if self._ib is not None and self._ib.isConnected():
            return self._ib
        if self._ib_factory is None:
            try:
                from ib_async import IB
            except ImportError:
                raise RuntimeError(
                    'The ibkr market data provider needs the ib_async package: pip install "market_watcher[ibkr]"'
                ) from None
            self._ib_factory = IB
        ib = self._ib_factory()
        ib.RequestTimeout = self.timeout
        ib.connect(self.host, self.port, clientId=self.client_id, timeout=self.timeout, readonly=True)
        ib.reqMarketDataType(self.market_data_type)
        logger.info(
            "Connected to IBKR at %s:%s (client id %s, market data type %s)",
            self.host,
            self.port,
            self.client_id,
            self.market_data_type,
        )
        self._ib = ib
        self._contracts = {}
        self._unresolved = set()
        return ib

    def _qualify(self, ib, tickers):
        """Resolves tickers to IB stock contracts (SMART / USD), remembering the outcome for this connection."""
        from ib_async import Stock

        missing = [ticker for ticker in tickers if ticker not in self._contracts and ticker not in self._unresolved]
        if missing:
            candidates = [Stock(ticker, "SMART", "USD") for ticker in missing]
            ib.qualifyContracts(*candidates)
            for ticker, contract in zip(missing, candidates, strict=True):
                if contract.conId:
                    self._contracts[ticker] = contract
                else:
                    self._unresolved.add(ticker)
                    logger.warning("IBKR could not resolve %s as a US stock; it will be skipped", ticker)
        return {ticker: self._contracts[ticker] for ticker in tickers if ticker in self._contracts}


def valid_price(value):
    return value is not None and not math.isnan(value) and value > 0


def current_price(snapshot):
    """Last trade price, or the bid/ask midpoint before the first trade; None when neither is available.

    Deliberately never falls back to the previous close, which would read as a 0% move.
    """
    if valid_price(snapshot.last):
        return snapshot.last
    if valid_price(snapshot.bid) and valid_price(snapshot.ask):
        return (snapshot.bid + snapshot.ask) / 2
    return None


def _percent_change(price, previous_close):
    if price is None or not valid_price(previous_close):
        return None
    return (price - previous_close) / previous_close


PROVIDERS: dict[str, type[MarketDataProvider]] = {
    YFinanceProvider.name: YFinanceProvider,
    "yahoo": YFinanceProvider,  # alias for the default, kept for older .env files
    YahooFinanceProvider.name: YahooFinanceProvider,
    IBKRProvider.name: IBKRProvider,
}
DEFAULT_PROVIDER = YFinanceProvider.name


def create_provider(name=None):
    """Instantiates the provider called ``name``, or the one set by ``MARKET_DATA_PROVIDER`` (default: yahoo)."""
    if name is None:
        name = context.config.get("MARKET_DATA_PROVIDER") or DEFAULT_PROVIDER
    key = name.strip().lower()
    try:
        provider_class = PROVIDERS[key]
    except KeyError:
        available = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unknown market data provider {name!r}; available: {available}") from None
    return provider_class()
