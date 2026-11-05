"""Automated straddle trading on Interactive Brokers (experimental; off by default).

For every new opportunity the engine reports, :class:`IBKRStraddleTrader` builds an
at-the-money straddle on the nearest expiry at least ``min_days_to_expiry`` away and
places it as one combo (BAG) limit order: bought for a long straddle, sold for a short
straddle. The trader is a notifier, so it receives exactly what Slack and email
receive: opportunities at the moment they cross their threshold.

Open straddles are then managed on every polling cycle (``manage_positions`` is an
engine cycle hook): entries are tracked until IB reports them filled, positions are
valued at the legs' bid/ask midpoints and closed with the opposite combo order on a
take-profit, a stop-loss, shortly before the session close (unless held overnight),
or when the day's loss limit is hit. After a restart or a lost connection the book
is reconciled with IB's positions, open orders and the day's executions.

Safety rails, all configurable (see TradingSettings):

* nothing happens unless ``TRADING_ENABLED=true``;
* non-paper accounts are refused (IB paper account ids start with ``DU``) unless
  ``TRADING_ALLOW_LIVE=true``;
* orders are staged in TWS without being transmitted - you click Transmit - unless
  ``IBKR_TRANSMIT_ORDERS=true``;
* long straddles only by default: short straddles are naked options with unlimited
  risk and need margin approval;
* caps on the premium paid, the notional sold, the contracts per order, the open
  positions, the new positions per trading day (one per ticker) and the daily loss;
* ``TRADING_DRY_RUN=true`` rehearses entries and exits over a read-only connection
  without sending a single order.

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
ORDER_REF = "MarketWatcher"  # tags entry orders, so the book can be rebuilt from executions
EXIT_REF = "MarketWatcher-exit"
STRIKE_ATTEMPTS = 3  # how many strikes around the money to try before giving up on a ticker
FILLED = "Filled"
DEAD_ORDER_STATUSES = {"Cancelled", "ApiCancelled"}


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
    max_notional: float = 20_000.0  # USD: most underlying notional (strike x multiplier x contracts) a short may carry
    min_days_to_expiry: int = 7
    limit_buffer_pct: float = 2.0  # pay up to / receive at least the net mid +/- this percentage
    max_orders_per_day: int = 3
    take_profit_pct: float = 50.0  # close once the straddle's value moved this far in our favour (0 disables)
    stop_loss_pct: float = 50.0  # ... or this far against us (0 disables)
    exit_minutes_before_close: int = 15
    hold_overnight: bool = False
    max_open_positions: int = 3
    max_daily_loss: float = 500.0  # USD of realized + unrealized loss that closes everything for the day (0 disables)
    risk_per_trade: float = 0.0  # USD: size long straddles by premium at risk (0 = always `contracts`)
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
            take_profit_pct=config_float("TRADING_TAKE_PROFIT_PCT", default=50.0),
            stop_loss_pct=config_float("TRADING_STOP_LOSS_PCT", default=50.0),
            exit_minutes_before_close=config_int("TRADING_EXIT_MINUTES_BEFORE_CLOSE", default=15),
            hold_overnight=config_flag("TRADING_HOLD_OVERNIGHT", default=False),
            max_open_positions=config_int("TRADING_MAX_OPEN_POSITIONS", default=3),
            max_daily_loss=config_float("TRADING_MAX_DAILY_LOSS", default=500.0),
            risk_per_trade=config_float("STRADDLE_RISK_PER_TRADE", default=0.0),
            host=context.config.get("IBKR_HOST") or "127.0.0.1",
            port=config_int("IBKR_PORT", default=7497),
            client_id=config_int("IBKR_TRADING_CLIENT_ID", default=2),
            market_data_type=config_int("IBKR_MARKET_DATA_TYPE", default=3),
            timeout=config_int("IBKR_TIMEOUT", default=10),
        )
        for key, value in (
            ("STRADDLE_CONTRACTS", settings.contracts),
            ("TRADING_MAX_ORDERS_PER_DAY", settings.max_orders_per_day),
            ("TRADING_MAX_OPEN_POSITIONS", settings.max_open_positions),
        ):
            if value < 1:
                raise ValueError(f"{key} must be at least 1")
        return settings

    def describe(self):
        mode = (
            "DRY RUN (nothing sent to TWS)" if self.dry_run else "auto-transmit" if self.transmit else "staged in TWS"
        )
        account = "live accounts allowed" if self.allow_live else "paper accounts only"
        sizing = f" (long straddles sized to {self.risk_per_trade:,.0f} USD at risk)" if self.risk_per_trade > 0 else ""
        take_profit = f"take profit {self.take_profit_pct:g}%" if self.take_profit_pct > 0 else "no take profit"
        stop_loss = f"stop loss {self.stop_loss_pct:g}%" if self.stop_loss_pct > 0 else "no stop loss"
        close = (
            "hold overnight" if self.hold_overnight else f"close {self.exit_minutes_before_close} min before the close"
        )
        return (
            f"{mode}; {account}; {', '.join(sorted(self.strategies))}; {self.contracts} contract(s) per order{sizing}; "
            f"max premium {self.max_premium:,.0f} USD, max notional {self.max_notional:,.0f} USD; "
            f"max {self.max_orders_per_day} order(s) per day, {self.max_open_positions} open; "
            f"daily loss limit {self.max_daily_loss:,.0f} USD; {take_profit}, {stop_loss}, {close}; "
            f"expiry >= {self.min_days_to_expiry} days"
        )


@dataclass(frozen=True)
class StraddleSpec:
    """A fully specified straddle order."""

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
    call: object = None  # the qualified ib_async Option legs
    put: object = None

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


@dataclass
class OpenStraddle:
    """A straddle in the trader's book: pending until the entry fills, then managed until an exit fills."""

    spec: StraddleSpec
    call: object
    put: object
    entry_order_id: int | None  # None for dry runs and positions recovered from executions
    entry_price: float  # the limit until filled, then the average fill
    filled: bool
    opened_on: date
    exit_order_id: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None


@dataclass(frozen=True)
class StraddleQuote:
    """Current value of a straddle: sum of the legs' mids, and of their bids/asks when both legs quote them."""

    mid: float
    bid: float | None
    ask: float | None


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


def straddle_quote(call_snapshot, put_snapshot):
    """The straddle's quote from its legs' snapshots, or None when a leg has no usable price."""
    call_mid, put_mid = option_price(call_snapshot), option_price(put_snapshot)
    if call_mid is None or put_mid is None:
        return None
    bids = (call_snapshot.bid, put_snapshot.bid)
    asks = (call_snapshot.ask, put_snapshot.ask)
    return StraddleQuote(
        mid=call_mid + put_mid,
        bid=sum(bids) if all(valid_price(bid) for bid in bids) else None,
        ask=sum(asks) if all(valid_price(ask) for ask in asks) else None,
    )


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


def unrealized_pnl(position, value):
    """USD P&L of ``position`` if closed at ``value`` (net per unit): long gains when the straddle rises."""
    sign = 1 if position.spec.action == "BUY" else -1
    return (value - position.entry_price) * position.spec.multiplier * position.spec.quantity * sign


def exit_reason(position, value, now, session, settings):
    """Why ``position`` should be closed now, or None to keep it."""
    long = position.spec.action == "BUY"
    entry = position.entry_price
    if settings.take_profit_pct > 0:
        target = entry * (1 + settings.take_profit_pct / 100) if long else entry * (1 - settings.take_profit_pct / 100)
        if (long and value >= target) or (not long and value <= target):
            return f"take profit ({value:.2f} vs entry {entry:.2f})"
    if settings.stop_loss_pct > 0:
        stop = entry * (1 - settings.stop_loss_pct / 100) if long else entry * (1 + settings.stop_loss_pct / 100)
        if (long and value <= stop) or (not long and value >= stop):
            return f"stop loss ({value:.2f} vs entry {entry:.2f})"
    if not settings.hold_overnight and now >= session.close - timedelta(minutes=settings.exit_minutes_before_close):
        return "session close"
    return None


def exit_limit(position, quote, reason, buffer_pct):
    """Limit for the closing order: gentle (mid -/+ buffer) for a take-profit, crossing the spread otherwise."""
    selling = position.spec.action == "BUY"  # closing a long straddle sells it
    if reason.startswith("take profit"):
        return limit_price(quote.mid, "SELL" if selling else "BUY", buffer_pct)
    if selling and quote.bid is not None:
        return round(quote.bid, 2)
    if not selling and quote.ask is not None:
        return round(quote.ask, 2)
    return limit_price(quote.mid, "SELL" if selling else "BUY", buffer_pct)


# --- Trader ----------------------------------------------------------------------------------------------------------


def _fills_by_leg(fills, order_ref):
    """Shares and cost per option conId among today's executions tagged with ``order_ref``."""
    by_leg = {}
    for fill in fills:
        if getattr(fill.execution, "orderRef", "") != order_ref:
            continue
        shares, cost = by_leg.get(fill.contract.conId, (0.0, 0.0))
        by_leg[fill.contract.conId] = (
            shares + fill.execution.shares,
            cost + fill.execution.shares * fill.execution.price,
        )
    return by_leg


def _fill_price(by_leg, position):
    """Average net price per unit of ``position``'s two legs from ``by_leg``, or None without fills for both."""
    total = 0.0
    for leg in (position.call, position.put):
        shares, cost = by_leg.get(leg.conId, (0.0, 0.0))
        if not shares:
            return None
        total += cost / shares
    return total


class IBKRStraddleTrader:
    """Places ATM straddle combo orders on IBKR for new opportunities and manages them until they are closed.

    ``notify`` makes it an engine notifier; ``manage_positions`` an engine cycle hook.
    """

    def __init__(self, settings, ib_factory=None, today=None, announcers=()):
        self.settings = settings
        self.announcers = list(announcers)  # notifiers with announce(subject, text) for trade events
        self.positions = {}  # ticker -> OpenStraddle
        self._ib_factory = ib_factory
        self._ib = None
        self._today = today or date.today
        self._day = None
        self._placed_today = {}
        self._halted_on = None  # the day the daily loss limit was hit
        self._realized = []  # (day, ticker, USD)
        self._needs_sync = False  # set by _connect() on every new connection: reconcile the book with IB first
        self._summarized_on = None

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

    def _announce(self, subject, text):
        """Sends a trade event to every announcer; a failing announcer is logged and never affects trading."""
        if self.settings.dry_run:
            subject = f"DRY RUN - {subject}"
        for announcer in self.announcers:
            try:
                announcer.announce(subject, text)
            except Exception:
                logger.exception("Announcer %s failed", type(announcer).__name__)

    def day_summary(self, today):
        """One paragraph on the day: closed trades with their P&L and what is still open."""
        closed = [(ticker, pnl) for day, ticker, pnl in self._realized if day == today]
        lines = [f"Realized P&L: {self.realized_today(today):+,.0f} USD over {len(closed)} closed straddle(s)"]
        lines += [f"  {ticker}: {pnl:+,.0f} USD" for ticker, pnl in closed]
        for ticker, position in sorted(self.positions.items()):
            if position.exit_order_id is not None:
                state = f"exit pending ({position.exit_reason})"
            elif position.filled:
                state = f"open since {position.entry_price:.2f}"
            else:
                state = "entry not filled"
            lines.append(f"  {ticker}: {position.spec.strategy}, {state}")
        if self._halted_on == today:
            lines.append("Daily loss limit was hit; no new positions were opened afterwards")
        return "\n".join(lines)

    def _maybe_summarize(self, today, near_close):
        if near_close and self._summarized_on != today:
            self._summarized_on = today
            self._announce("End of day summary", self.day_summary(today))

    def realized_today(self, day):
        return sum(pnl for realized_on, _, pnl in self._realized if realized_on == day)

    # Entries --------------------------------------------------------------------------------------------------------

    def place_straddle(self, ticker, strategy):
        """Builds and places (or stages, or dry-runs) one straddle; returns its spec, or None when skipped."""
        today = self._today()
        self._roll_day(today)
        settings = self.settings
        if self._halted_on == today:
            logger.warning("Daily loss limit was hit today; not opening %s", ticker)
            return None
        if ticker in self._placed_today or ticker in self.positions:
            logger.info("Already have a straddle on %s today; skipping", ticker)
            return None
        if len(self._placed_today) >= settings.max_orders_per_day:
            logger.warning("Daily limit of %d new positions reached; skipping %s", settings.max_orders_per_day, ticker)
            return None
        if len(self.positions) >= settings.max_open_positions:
            logger.warning("Already %d open position(s); not opening %s", len(self.positions), ticker)
            return None

        ib = self._connect()
        spec = self.build_straddle(ib, ticker, strategy, today)
        check_limits(spec, settings)

        if settings.dry_run:
            logger.info("DRY RUN - would place %s", spec.describe())
            self._announce("Straddle order", f"Would place {spec.describe()}")
            position = OpenStraddle(spec, spec.call, spec.put, None, spec.limit_price, True, today)
        else:
            trade = ib.placeOrder(spec.contract, self._order(spec.action, spec.quantity, spec.limit_price, ORDER_REF))
            placement = "transmitted" if settings.transmit else "staged in TWS (transmit it manually)"
            logger.info(
                "Placed %s - order id %s, status %s, %s",
                spec.describe(),
                trade.order.orderId,
                trade.orderStatus.status,
                placement,
            )
            position = OpenStraddle(spec, spec.call, spec.put, trade.order.orderId, spec.limit_price, False, today)
            self._announce("Straddle order placed", f"{spec.describe()}\nOrder id {trade.order.orderId}: {placement}")
        self.positions[ticker] = position
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
        multiplier = int(chain.multiplier or 100)
        limit = limit_price(call_price + put_price, action, self.settings.limit_buffer_pct)
        return StraddleSpec(
            ticker=ticker,
            strategy=strategy,
            action=action,
            expiry=expiry,
            strike=strike,
            underlying_price=underlying_price,
            call_price=call_price,
            put_price=put_price,
            limit_price=limit,
            quantity=self._quantity(limit, multiplier, action),
            multiplier=multiplier,
            contract=self._combo(ticker, call.conId, put.conId, Bag, ComboLeg),
            call=call,
            put=put,
        )

    def _quantity(self, limit, multiplier, action):
        """Contracts per order: fixed, or for long straddles the number the premium budget allows, if smaller."""
        contracts = self.settings.contracts
        if action == "BUY" and self.settings.risk_per_trade > 0:
            per_contract = limit * multiplier
            affordable = int(self.settings.risk_per_trade // per_contract)
            if affordable < 1:
                raise ValueError(
                    f"STRADDLE_RISK_PER_TRADE {self.settings.risk_per_trade:,.0f} USD buys no contract "
                    f"at {per_contract:,.0f} USD each"
                )
            contracts = min(contracts, affordable)
        return contracts

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

    @staticmethod
    def _combo(ticker, call_con_id, put_con_id, bag_class, leg_class):
        # Legs are always BUY/BUY; selling the combo (SELL action) sells both legs.
        return bag_class(
            symbol=ticker,
            currency="USD",
            exchange="SMART",
            comboLegs=[
                leg_class(conId=call_con_id, ratio=1, action="BUY", exchange="SMART"),
                leg_class(conId=put_con_id, ratio=1, action="BUY", exchange="SMART"),
            ],
        )

    def _order(self, action, quantity, limit, reference):
        from ib_async import LimitOrder

        return LimitOrder(action, quantity, limit, tif="DAY", transmit=self.settings.transmit, orderRef=reference)

    # Position management --------------------------------------------------------------------------------------------

    def manage_positions(self, now, session):
        """Engine cycle hook: tracks fills, values open straddles and places exits when a rule says so."""
        today = self._today()
        self._roll_day(today)
        settings = self.settings

        ib = self._connect()
        if self._needs_sync:
            if not settings.dry_run:  # dry-run positions are virtual: IB knows nothing about them
                self._sync_book(ib, today)
            self._needs_sync = False
        self._refresh_orders(ib, today)
        near_close = now >= session.close - timedelta(minutes=settings.exit_minutes_before_close)
        if not self.positions:
            self._maybe_summarize(today, near_close)
            return

        closing_time = not settings.hold_overnight and near_close
        open_positions = [p for p in self.positions.values() if p.filled and p.exit_order_id is None]
        pending_entries = [p for p in self.positions.values() if not p.filled and p.entry_order_id is not None]
        quotes = self._quotes(ib, open_positions)

        unrealized = sum(
            unrealized_pnl(p, quotes[p.spec.ticker].mid) for p in open_positions if p.spec.ticker in quotes
        )
        day_pnl = self.realized_today(today) + unrealized
        if settings.max_daily_loss > 0 and day_pnl <= -settings.max_daily_loss and self._halted_on != today:
            self._halted_on = today
            logger.error(
                "Daily loss limit hit (realized %+.0f, unrealized %+.0f, limit %.0f USD): closing everything, "
                "no new positions today",
                self.realized_today(today),
                unrealized,
                settings.max_daily_loss,
            )
            for position in open_positions:
                if position.spec.ticker in quotes:
                    self._close(ib, position, quotes[position.spec.ticker], "daily loss limit", today)
            for position in pending_entries:
                self._cancel_entry(ib, position)
            self._announce(
                "Daily loss limit hit",
                f"Realized {self.realized_today(today):+,.0f} USD, unrealized {unrealized:+,.0f} USD, "
                f"limit {settings.max_daily_loss:,.0f} USD: closing everything; no new positions today",
            )
            return

        for position in open_positions:
            quote = quotes.get(position.spec.ticker)
            if quote is None:
                continue
            reason = exit_reason(position, quote.mid, now, session, settings)
            if reason:
                self._close(ib, position, quote, reason, today)

        if closing_time:
            for position in pending_entries:
                self._cancel_entry(ib, position)
        self._maybe_summarize(today, near_close)

    def _roll_day(self, today):
        if today != self._day:
            self._day = today
            self._placed_today = {}

    def _quotes(self, ib, positions):
        quotes = {}
        for position in positions:
            call_snapshot, put_snapshot = ib.reqTickers(position.call, position.put)
            quote = straddle_quote(call_snapshot, put_snapshot)
            if quote is None:
                logger.warning("No quote for the %s straddle; leaving it alone this cycle", position.spec.ticker)
                continue
            quotes[position.spec.ticker] = quote
        return quotes

    def _refresh_orders(self, ib, today):
        """Applies fills and cancellations reported by IB to the book."""
        trades = {trade.order.orderId: trade for trade in ib.trades()}
        for ticker, position in list(self.positions.items()):
            if position.exit_order_id is not None:
                trade = trades.get(position.exit_order_id)
                if trade is None:
                    continue
                status = trade.orderStatus.status
                if status == FILLED:
                    price = trade.orderStatus.avgFillPrice or position.exit_price
                    self._record_close(position, price, today, position.exit_reason)
                elif status in DEAD_ORDER_STATUSES:
                    logger.warning(
                        "Exit order %s for %s was cancelled; it will be placed again", trade.order.orderId, ticker
                    )
                    position.exit_order_id = position.exit_price = position.exit_reason = None
                continue
            if not position.filled and position.entry_order_id is not None:
                trade = trades.get(position.entry_order_id)
                if trade is None:
                    continue
                status = trade.orderStatus.status
                if status == FILLED:
                    position.filled = True
                    position.entry_price = trade.orderStatus.avgFillPrice or position.entry_price
                    self._announce(
                        "Straddle entry filled", f"{position.spec.describe()}\nFilled at {position.entry_price:.2f}"
                    )
                    logger.info("Entry filled: %s at %.2f", position.spec.describe(), position.entry_price)
                elif status in DEAD_ORDER_STATUSES:
                    logger.warning("Entry order %s for %s was cancelled; forgetting it", trade.order.orderId, ticker)
                    del self.positions[ticker]

    def _close(self, ib, position, quote, reason, today):
        ticker = position.spec.ticker
        action = "SELL" if position.spec.action == "BUY" else "BUY"
        limit = exit_limit(position, quote, reason, self.settings.limit_buffer_pct)
        if self.settings.dry_run:
            pnl = unrealized_pnl(position, limit)
            self._realized.append((today, ticker, pnl))
            del self.positions[ticker]
            logger.info("DRY RUN - would close the %s straddle at %.2f (%s): %+.0f USD", ticker, limit, reason, pnl)
            self._announce("Straddle closed", f"Would close {ticker} at {limit:.2f} ({reason}): {pnl:+,.0f} USD")
            return
        trade = ib.placeOrder(position.spec.contract, self._order(action, position.spec.quantity, limit, EXIT_REF))
        position.exit_order_id = trade.order.orderId
        position.exit_price = limit
        position.exit_reason = reason
        logger.info(
            "Closing the %s straddle: %s %d at %.2f (%s) - order id %s, %s",
            ticker,
            action,
            position.spec.quantity,
            limit,
            reason,
            trade.order.orderId,
            "transmitted" if self.settings.transmit else "staged in TWS (transmit it manually)",
        )
        self._announce(
            "Closing straddle",
            f"{ticker}: {action} {position.spec.quantity} at {limit:.2f} ({reason}) - order id {trade.order.orderId}",
        )

    def _record_close(self, position, price, today, how):
        """Books the realized P&L of a closed straddle, drops it from the book and announces it."""
        ticker = position.spec.ticker
        pnl = unrealized_pnl(position, price)
        self._realized.append((today, ticker, pnl))
        self.positions.pop(ticker, None)
        self._announce(
            "Straddle closed",
            f"{ticker} closed at {price:.2f} ({how}): {pnl:+,.0f} USD; "
            f"realized today {self.realized_today(today):+,.0f} USD",
        )
        logger.info("Closed the %s straddle at %.2f (%s): %+.0f USD", ticker, price, how, pnl)

    def _sync_book(self, ib, today):
        """Reconciles the book with IB after a (re)connection.

        Applies the fills, cancellations and closes that happened while disconnected, using
        IB's positions, open orders and the day's executions, then recovers straddles opened
        by an earlier run (see _rebuild_book).
        """
        fills = list(ib.reqExecutions())
        held = {position.contract.conId for position in ib.positions() if position.position}
        open_order_ids = {trade.order.orderId for trade in ib.trades()}
        entry_fills = _fills_by_leg(fills, ORDER_REF)
        exit_fills = _fills_by_leg(fills, EXIT_REF)

        for ticker, position in list(self.positions.items()):
            legs_held = position.call.conId in held and position.put.conId in held
            if position.exit_order_id is not None:
                if not legs_held:
                    price = _fill_price(exit_fills, position) or position.exit_price
                    self._record_close(position, price, today, f"{position.exit_reason}; fill detected after reconnect")
                elif position.exit_order_id not in open_order_ids:
                    logger.warning(
                        "Exit order %s for %s is gone after the reconnect; it will be placed again",
                        position.exit_order_id,
                        ticker,
                    )
                    position.exit_order_id = position.exit_price = position.exit_reason = None
            elif position.filled:
                if not legs_held:
                    price = _fill_price(exit_fills, position)
                    if price is None:
                        logger.warning(
                            "%s is no longer held and no exit fill was found; dropping it from the book", ticker
                        )
                        del self.positions[ticker]
                    else:
                        self._record_close(position, price, today, "closed while disconnected")
            elif legs_held:
                position.filled = True
                position.entry_price = _fill_price(entry_fills, position) or position.entry_price
                logger.info(
                    "Entry filled while disconnected: %s at %.2f", position.spec.describe(), position.entry_price
                )
                self._announce(
                    "Straddle entry filled", f"{position.spec.describe()}\nFilled at {position.entry_price:.2f}"
                )
            elif position.entry_order_id not in open_order_ids:
                logger.warning(
                    "Entry order %s for %s is gone after the reconnect; forgetting it", position.entry_order_id, ticker
                )
                del self.positions[ticker]

        self._rebuild_book(ib, today, fills=fills, held=held)

    def _cancel_entry(self, ib, position):
        ticker = position.spec.ticker
        for trade in ib.trades():
            if trade.order.orderId == position.entry_order_id:
                ib.cancelOrder(trade.order)
        del self.positions[ticker]
        logger.info("Cancelled the unfilled entry for %s (order id %s)", ticker, position.entry_order_id)

    def _rebuild_book(self, ib, today, fills=None, held=None):
        """Recovers straddles opened today by an earlier run from IB's executions tagged with our order reference."""
        from ib_async import Bag, ComboLeg

        if fills is None:
            fills = list(ib.reqExecutions())
        fills = [fill for fill in fills if getattr(fill.execution, "orderRef", "") == ORDER_REF]
        if not fills:
            return
        if held is None:
            held = {position.contract.conId for position in ib.positions() if position.position}
        by_order = {}
        for fill in fills:
            by_order.setdefault(fill.execution.orderId, []).append(fill)

        for order_id, group in sorted(by_order.items()):
            legs = {}
            for fill in group:
                leg = legs.setdefault(
                    fill.contract.conId,
                    {"contract": fill.contract, "shares": 0.0, "cost": 0.0, "side": fill.execution.side},
                )
                leg["shares"] += fill.execution.shares
                leg["cost"] += fill.execution.shares * fill.execution.price
            call = next((leg for leg in legs.values() if leg["contract"].right == "C"), None)
            put = next((leg for leg in legs.values() if leg["contract"].right == "P"), None)
            if len(legs) != 2 or call is None or put is None:
                continue
            if not all(con_id in held for con_id in legs):
                continue  # closed since (or never fully opened)
            ticker = call["contract"].symbol
            if ticker in self.positions:
                continue

            action = "BUY" if call["side"] == "BOT" else "SELL"
            call_price = call["cost"] / call["shares"]
            put_price = put["cost"] / put["shares"]
            spec = StraddleSpec(
                ticker=ticker,
                strategy=LONG if action == "BUY" else SHORT,
                action=action,
                expiry=call["contract"].lastTradeDateOrContractMonth,
                strike=call["contract"].strike,
                underlying_price=0.0,  # unknown for a recovered position
                call_price=call_price,
                put_price=put_price,
                limit_price=call_price + put_price,
                quantity=int(min(call["shares"], put["shares"])),
                multiplier=int(call["contract"].multiplier or 100),
                contract=self._combo(ticker, call["contract"].conId, put["contract"].conId, Bag, ComboLeg),
                call=call["contract"],
                put=put["contract"],
            )
            self.positions[ticker] = OpenStraddle(spec, spec.call, spec.put, None, spec.limit_price, True, today)
            logger.info(
                "Recovered the open %s on %s from today's executions (order id %s, entry %.2f, %d contract(s))",
                spec.strategy,
                ticker,
                order_id,
                spec.limit_price,
                spec.quantity,
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
        # A dry run never sends orders, so a read-only connection suffices (and works with TWS's Read-Only API on).
        ib.connect(
            settings.host,
            settings.port,
            clientId=settings.client_id,
            timeout=settings.timeout,
            readonly=settings.dry_run,
        )
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
        self._needs_sync = True  # a new connection: reconcile the book with it before acting on it
        return ib
