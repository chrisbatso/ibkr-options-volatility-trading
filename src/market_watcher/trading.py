"""Automated straddle order placement on Interactive Brokers (experimental; off by default).

For every new opportunity the engine reports, :class:`IBKRStraddleTrader` builds an
at-the-money straddle on the nearest expiry at least ``min_days_to_expiry`` away and
places it as one combo (BAG) limit order: bought for a long straddle, sold for a short
straddle. The trader is a notifier, so it receives exactly what Slack and email
receive: opportunities at the moment they cross their threshold.

Safety rails, all configurable (see TradingSettings):

* nothing happens unless ``TRADING_ENABLED=true``;
* non-paper accounts are refused (IB paper account ids start with ``DU``) unless
  ``TRADING_ALLOW_LIVE=true``;
* orders are staged in TWS without being transmitted - you click Transmit - unless
  ``IBKR_TRANSMIT_ORDERS=true``;
* long straddles only by default: short straddles are naked options with unlimited
  risk and need margin approval;
* caps on the premium paid, the notional sold, the contracts per order and the new
  positions per trading day, plus one order per ticker per day;
* ``TRADING_DRY_RUN=true`` builds and logs the orders without touching TWS.

Requires the optional dependency: ``pip install "market_watcher[ibkr]"``.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from market_watcher.common import STRATEGIES, VALID_STRATEGIES
from market_watcher.config import config_flag, config_float, config_int, context
from market_watcher.market_data import current_price, valid_price

logger = logging.getLogger(__name__)

LONG = STRATEGIES.LONG_STRADDLE.value
SHORT = STRATEGIES.SHORT_STRADDLE.value
PAPER_ACCOUNT_PREFIX = "DU"
ORDER_REF = "MarketWatcher"
STRIKE_ATTEMPTS = 3  # how many strikes around the money to try before giving up on a ticker


def parse_strategies(text):
    """Parses TRADING_STRATEGIES (comma-separated); long straddle only when unset."""
    if text is None or not text.strip():
        return frozenset({LONG})
    names = {part.strip().lower() for part in text.split(",") if part.strip()}
    unknown = names - VALID_STRATEGIES
    if unknown:
        raise ValueError(
            f"TRADING_STRATEGIES has unknown strategies {sorted(unknown)}; valid: {sorted(VALID_STRATEGIES)}"
        )
    return frozenset(names)


@dataclass(frozen=True)
class TradingSettings:
    enabled: bool = False
    dry_run: bool = False
    transmit: bool = False
    allow_live: bool = False
    strategies: frozenset = frozenset({LONG})
    contracts: int = 1
    max_premium: float = 1_000.0  # USD: most a long straddle order may cost
    max_notional: float = (
        20_000.0  # USD: most underlying notional (strike x 100 x contracts) a short straddle may carry
    )
    min_days_to_expiry: int = 7
    limit_buffer_pct: float = 2.0  # pay up to / receive at least the net mid +/- this percentage
    max_orders_per_day: int = 3
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 2
    market_data_type: int = 3
    timeout: int = 10

    @classmethod
    def from_config(cls):
        settings = cls(
            enabled=config_flag("TRADING_ENABLED", default=False),
            dry_run=config_flag("TRADING_DRY_RUN", default=False),
            transmit=config_flag("IBKR_TRANSMIT_ORDERS", default=False),
            allow_live=config_flag("TRADING_ALLOW_LIVE", default=False),
            strategies=parse_strategies(context.config.get("TRADING_STRATEGIES")),
            contracts=config_int("STRADDLE_CONTRACTS", default=1),
            max_premium=config_float("STRADDLE_MAX_PREMIUM", default=1_000.0),
            max_notional=config_float("STRADDLE_MAX_NOTIONAL", default=20_000.0),
            min_days_to_expiry=config_int("STRADDLE_MIN_DAYS_TO_EXPIRY", default=7),
            limit_buffer_pct=config_float("STRADDLE_LIMIT_BUFFER_PCT", default=2.0),
            max_orders_per_day=config_int("TRADING_MAX_ORDERS_PER_DAY", default=3),
            host=context.config.get("IBKR_HOST") or "127.0.0.1",
            port=config_int("IBKR_PORT", default=7497),
            client_id=config_int("IBKR_TRADING_CLIENT_ID", default=2),
            market_data_type=config_int("IBKR_MARKET_DATA_TYPE", default=3),
            timeout=config_int("IBKR_TIMEOUT", default=10),
        )
        if settings.contracts < 1:
            raise ValueError("STRADDLE_CONTRACTS must be at least 1")
        if settings.max_orders_per_day < 1:
            raise ValueError("TRADING_MAX_ORDERS_PER_DAY must be at least 1")
        return settings

    def describe(self):
        mode = (
            "DRY RUN (nothing sent to TWS)" if self.dry_run else ("auto-transmit" if self.transmit else "staged in TWS")
        )
        account = "live accounts allowed" if self.allow_live else "paper accounts only"
        return (
            f"{mode}; {account}; {', '.join(sorted(self.strategies))}; {self.contracts} contract(s) per order; "
            f"max premium {self.max_premium:,.0f} USD, max notional {self.max_notional:,.0f} USD; "
            f"max {self.max_orders_per_day} order(s) per day; expiry >= {self.min_days_to_expiry} days"
        )


@dataclass(frozen=True)
class StraddleSpec:
    """A fully specified straddle order, before or after placement."""

    ticker: str
    strategy: str
    action: str  # BUY (long straddle) or SELL (short straddle) of the combo
    expiry: str  # YYYYMMDD
    strike: float
    underlying_price: float
    call_price: float
    put_price: float
    limit_price: float  # net price per combo unit; one contract is `multiplier` units
    quantity: int
    multiplier: int
    contract: object  # the ib_async Bag contract

    @property
    def premium(self):
        """USD paid (BUY) or received (SELL) at the limit price."""
        return self.limit_price * self.multiplier * self.quantity

    @property
    def notional(self):
        """USD of underlying the straddle covers."""
        return self.strike * self.multiplier * self.quantity

    def describe(self):
        return (
            f"{self.action} {self.quantity} x {self.ticker} {self.expiry} {self.strike:g} straddle ({self.strategy}): "
            f"underlying {self.underlying_price:.2f}, call {self.call_price:.2f} + put {self.put_price:.2f}, "
            f"limit {self.limit_price:.2f} = {self.premium:,.0f} USD"
        )


# --- Pure helpers ----------------------------------------------------------------------------------------------------


def select_chain(chains, symbol):
    """The SMART option chain for ``symbol`` (its own trading class if there are several)."""
    smart = [chain for chain in chains if chain.exchange == "SMART"]
    preferred = [chain for chain in smart if chain.tradingClass == symbol] or smart or list(chains)
    if not preferred:
        raise ValueError(f"IBKR has no option chain for {symbol}")
    return preferred[0]


def choose_expiry(expirations, today, min_days):
    """The earliest expiry (YYYYMMDD) at least ``min_days`` after ``today``."""
    earliest = today + timedelta(days=min_days)
    candidates = sorted(e for e in expirations if datetime.strptime(e, "%Y%m%d").date() >= earliest)
    if not candidates:
        raise ValueError(f"No option expiry at least {min_days} days out (available: {sorted(expirations)})")
    return candidates[0]


def strikes_by_distance(strikes, price):
    """Strikes ordered from the closest to ``price`` outwards; the lower strike wins a tie."""
    return sorted(strikes, key=lambda strike: (abs(strike - price), strike))


def option_price(snapshot):
    """Bid/ask midpoint when both sides are quoted (option last trades go stale), else the last trade, else None."""
    if valid_price(snapshot.bid) and valid_price(snapshot.ask):
        return (snapshot.bid + snapshot.ask) / 2
    if valid_price(snapshot.last):
        return snapshot.last
    return None


def limit_price(net_price, action, buffer_pct):
    """Net limit: pay up to mid + buffer when buying, accept down to mid - buffer when selling."""
    factor = 1 + buffer_pct / 100 if action == "BUY" else 1 - buffer_pct / 100
    return round(net_price * factor, 2)


def check_limits(spec, settings):
    """Raises ValueError when the order would exceed the configured premium or notional caps."""
    if spec.action == "BUY" and spec.premium > settings.max_premium:
        raise ValueError(f"premium {spec.premium:,.0f} USD exceeds STRADDLE_MAX_PREMIUM {settings.max_premium:,.0f}")
    if spec.action == "SELL" and spec.notional > settings.max_notional:
        raise ValueError(
            f"notional {spec.notional:,.0f} USD exceeds STRADDLE_MAX_NOTIONAL {settings.max_notional:,.0f}"
        )


# --- Trader ----------------------------------------------------------------------------------------------------------


class IBKRStraddleTrader:
    """Places ATM straddle combo orders on IBKR for new opportunities; usable as an engine notifier."""

    def __init__(self, settings, ib_factory=None, today=None):
        self.settings = settings
        self._ib_factory = ib_factory
        self._ib = None
        self._today = today or date.today
        self._day = None
        self._placed_today = {}

    # Notifier interface ---------------------------------------------------------------------------------------------

    def notify(self, investment_data):
        for strategy, opportunities in investment_data.items():
            if strategy not in self.settings.strategies:
                continue
            for ticker in opportunities:
                try:
                    self.place_straddle(ticker, strategy)
                except Exception:
                    logger.exception("Could not place a %s on %s", strategy, ticker)

    def close(self):
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
            logger.info("Disconnected the trading client from IBKR")
        self._ib = None

    # Orders ---------------------------------------------------------------------------------------------------------

    def place_straddle(self, ticker, strategy):
        """Builds and places (or stages, or dry-runs) one straddle; returns its spec, or None when skipped."""
        today = self._today()
        if today != self._day:
            self._day = today
            self._placed_today = {}
        if ticker in self._placed_today:
            logger.info("Already placed a straddle on %s today; skipping", ticker)
            return None
        if len(self._placed_today) >= self.settings.max_orders_per_day:
            logger.warning(
                "Daily limit of %d new positions reached; skipping %s", self.settings.max_orders_per_day, ticker
            )
            return None

        ib = self._connect()
        spec = self.build_straddle(ib, ticker, strategy, today)
        check_limits(spec, self.settings)

        if self.settings.dry_run:
            logger.info("DRY RUN - would place %s", spec.describe())
        else:
            order = self._order(spec)
            trade = ib.placeOrder(spec.contract, order)
            placement = "transmitted" if self.settings.transmit else "staged in TWS (transmit it manually)"
            logger.info(
                "Placed %s - order id %s, status %s, %s",
                spec.describe(),
                trade.order.orderId,
                trade.orderStatus.status,
                placement,
            )
        self._placed_today[ticker] = spec
        return spec

    def build_straddle(self, ib, ticker, strategy, today):
        """Resolves the underlying, picks expiry and ATM strike, prices the legs and assembles the combo contract."""
        from ib_async import Bag, ComboLeg, Stock

        stock = Stock(ticker, "SMART", "USD")
        ib.qualifyContracts(stock)
        if not stock.conId:
            raise ValueError(f"IBKR could not resolve {ticker} as a US stock")
        [snapshot] = ib.reqTickers(stock)
        underlying_price = current_price(snapshot)
        if underlying_price is None:
            raise ValueError(f"No current price for {ticker}")

        chain = select_chain(ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId), ticker)
        expiry = choose_expiry(chain.expirations, today, self.settings.min_days_to_expiry)
        call, put, strike = self._qualify_legs(ib, ticker, expiry, chain.strikes, underlying_price)

        call_snapshot, put_snapshot = ib.reqTickers(call, put)
        call_price, put_price = option_price(call_snapshot), option_price(put_snapshot)
        if call_price is None or put_price is None:
            raise ValueError(f"No quotes for the {ticker} {expiry} {strike:g} call/put")

        action = "BUY" if strategy == LONG else "SELL"
        # Legs are always BUY/BUY; selling the combo (SELL action) sells both legs.
        combo = Bag(
            symbol=ticker,
            currency="USD",
            exchange="SMART",
            comboLegs=[
                ComboLeg(conId=call.conId, ratio=1, action="BUY", exchange="SMART"),
                ComboLeg(conId=put.conId, ratio=1, action="BUY", exchange="SMART"),
            ],
        )
        return StraddleSpec(
            ticker=ticker,
            strategy=strategy,
            action=action,
            expiry=expiry,
            strike=strike,
            underlying_price=underlying_price,
            call_price=call_price,
            put_price=put_price,
            limit_price=limit_price(call_price + put_price, action, self.settings.limit_buffer_pct),
            quantity=self.settings.contracts,
            multiplier=int(chain.multiplier or 100),
            contract=combo,
        )

    def _qualify_legs(self, ib, ticker, expiry, strikes, price):
        """Call and put at the strike closest to ``price`` that IBKR can resolve for ``expiry``."""
        from ib_async import Option

        for strike in strikes_by_distance(strikes, price)[:STRIKE_ATTEMPTS]:
            call = Option(ticker, expiry, strike, "C", "SMART", currency="USD")
            put = Option(ticker, expiry, strike, "P", "SMART", currency="USD")
            ib.qualifyContracts(call, put)
            if call.conId and put.conId:
                return call, put, strike
        raise ValueError(f"No tradable {ticker} {expiry} straddle near {price:.2f}")

    def _order(self, spec):
        from ib_async import LimitOrder

        return LimitOrder(
            spec.action,
            spec.quantity,
            spec.limit_price,
            tif="DAY",
            transmit=self.settings.transmit,
            orderRef=ORDER_REF,
        )

    # Connection -----------------------------------------------------------------------------------------------------

    def _connect(self):
        if self._ib is not None and self._ib.isConnected():
            return self._ib
        if self._ib_factory is None:
            try:
                from ib_async import IB
            except ImportError:
                raise RuntimeError(
                    'Order placement needs the ib_async package: pip install "market_watcher[ibkr]"'
                ) from None
            self._ib_factory = IB

        settings = self.settings
        ib = self._ib_factory()
        ib.RequestTimeout = settings.timeout
        ib.connect(settings.host, settings.port, clientId=settings.client_id, timeout=settings.timeout, readonly=False)
        accounts = list(ib.managedAccounts())
        if not accounts:
            ib.disconnect()
            raise RuntimeError("IBKR reported no managed accounts for the trading connection")
        if not settings.allow_live and not all(account.startswith(PAPER_ACCOUNT_PREFIX) for account in accounts):
            ib.disconnect()
            raise RuntimeError(
                f"Account(s) {accounts} are not paper accounts; refusing to trade "
                "(set TRADING_ALLOW_LIVE=true to override)"
            )
        ib.reqMarketDataType(settings.market_data_type)
        logger.info(
            "Trading client connected to IBKR at %s:%s (client id %s, account(s) %s)",
            settings.host,
            settings.port,
            settings.client_id,
            ", ".join(accounts),
        )
        self._ib = ib
        return ib
