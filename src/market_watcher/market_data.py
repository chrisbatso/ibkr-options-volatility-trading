"""Market data sources for the MarketWatcher engine.

A provider answers one question: what is the daily percent change of each
target stock right now? To add a source (for example IBKR TWS), subclass
:class:`MarketDataProvider`, give it a ``name``, implement ``get_daily_pnls``
and register the class in :data:`PROVIDERS`.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable

from yahoofinancials import YahooFinancials

from market_watcher.config import context


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


class YahooFinanceProvider(MarketDataProvider):
    """Daily P&L from Yahoo Finance through the ``yahoofinancials`` package."""

    name = "yahoo"

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


PROVIDERS: dict[str, type[MarketDataProvider]] = {YahooFinanceProvider.name: YahooFinanceProvider}
DEFAULT_PROVIDER = YahooFinanceProvider.name


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
