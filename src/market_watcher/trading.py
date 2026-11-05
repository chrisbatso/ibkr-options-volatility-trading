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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from market_watcher.common import STRATEGIES, VALID_STRATEGIES
from market_watcher.config import config_flag, config_float, config_int, context
from market_watcher.journal import TradeJournal
from market_watcher.market_data import current_price, valid_price

logger = logging.getLogger(__name__)

LONG = STRATEGIES.LONG_STRADDLE.value
SHORT = STRATEGIES.SHORT_STRADDLE.value
PAPER_ACCOUNT_PREFIX = "DU"
ORDER_REF = "MarketWatcher"  # tags entry orders, so the book can be rebuilt from executions
EXIT_REF = "MarketWatcher-exit"
HALT_EXIT_REF = "MarketWatcher-exit-halt"  # an exit forced by the daily loss limit: IB remembers the halt for a restart
EXIT_REFS = frozenset({EXIT_REF, HALT_EXIT_REF})
STRIKE_ATTEMPTS = 3  # how many strikes around the money to try before giving up on a ticker
FILLED = "Filled"
REJECTED = "Inactive"
DEAD_ORDER_STATUSES = {"Cancelled", "ApiCancelled", REJECTED}
DAILY_LOSS_REASON = "daily loss limit"
RECOVERED_EXIT_REASON = "exit order from an earlier run"


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
    max_spread_pct: float = (
        6.0  # refuse a straddle whose bid-ask spread is wider than this share of its mid (0 disables)
    )
    journal_path: str = ""  # CSV of every order, fill and close ("" = no journal)
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
            max_spread_pct=config_float("STRADDLE_MAX_SPREAD_PCT", default=6.0),
            journal_path=context.config.get("TRADING_JOURNAL") or "",
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
            f"{f'max spread {self.max_spread_pct:g}% of mid' if self.max_spread_pct > 0 else 'no spread limit'}; "
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
    bid: float | None = None  # the straddle's quote when it was priced, when both legs quote that side
    ask: float | None = None

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
    exit_contracts_booked: int = 0  # contracts the working exit had closed before this run: already off the quantity
    cancel_requested: bool = False  # the entry is being cancelled; it stays in the book until IB confirms
    exit_rejection_announced: bool = False

    @property
    def leg_ids(self):
        return self.call.conId, self.put.conId

    def forget_exit(self):
        """The exit order is gone: the position is open again, for the next exit to close."""
        self.exit_order_id = self.exit_price = self.exit_reason = None
        self.exit_contracts_booked = 0


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


def check_spread(spec, settings):
    """Raises ValueError when the straddle's bid-ask spread is too wide to be worth trading, or cannot be measured.

    Crossing the spread is what decides this strategy's result (research/backtest/RESULTS.md), so an unknown spread is
    not given the benefit of the doubt.
    """
    if settings.max_spread_pct <= 0:
        return
    if spec.bid is None or spec.ask is None:
        raise ValueError("the straddle has no two-sided quote, so its spread cannot be checked")
    spread = (spec.ask - spec.bid) / ((spec.ask + spec.bid) / 2) * 100
    if spread > settings.max_spread_pct:
        raise ValueError(f"spread {spread:.1f}% of mid exceeds STRADDLE_MAX_SPREAD_PCT {settings.max_spread_pct:g}")


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
    if not settings.hold_overnight:
        if now >= session.close - timedelta(minutes=settings.exit_minutes_before_close):
            return "session close"
        if position.opened_on < session.day:  # its exit expired unfilled at an earlier close
            return "left open from an earlier session"
    return None


def is_patient(reason):
    """A take-profit can wait for its price; any other exit has to get out, so its order follows the market."""
    return reason.startswith("take profit") or reason == RECOVERED_EXIT_REASON


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


def _leg_fills(fills):
    """The executions of option legs: IB also reports a combo as a whole, which would count everything twice."""
    return [fill for fill in fills if fill.contract.secType != "BAG"]


def _order_ref(fill):
    return getattr(fill.execution, "orderRef", "")


def _is_working(trade):
    return trade.orderStatus.status != FILLED and trade.orderStatus.status not in DEAD_ORDER_STATUSES


def _fills_by_leg(fills, order_refs, order_id=None):
    """Shares and cost per option conId among today's executions tagged with ``order_refs`` (of one order, if given)."""
    by_leg = {}
    for fill in fills:
        if _order_ref(fill) not in order_refs:
            continue
        if order_id is not None and fill.execution.orderId != order_id:
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
    for con_id in position.leg_ids:
        shares, cost = by_leg.get(con_id, (0.0, 0.0))
        if not shares:
            return None
        total += cost / shares
    return total


def _filled_contracts(by_leg, leg_ids):
    """Straddles complete in ``by_leg``: a contract counts once both its legs have filled."""
    return int(min(by_leg.get(con_id, (0.0, 0.0))[0] for con_id in leg_ids))


def _realized_by_ticker(fills):
    """USD realized per ticker by today's tagged executions of option legs.

    Only straddles whose entry and exit both executed today can be valued: IB reports executions
    per day, so the exit of an overnight hold has no entry here to compare it with.
    """
    realized = {}
    for ticker in {fill.contract.symbol for fill in fills}:
        own = [fill for fill in fills if fill.contract.symbol == ticker]
        entries, exits = _fills_by_leg(own, {ORDER_REF}), _fills_by_leg(own, EXIT_REFS)
        if len(entries) != 2 or set(entries) != set(exits):
            continue
        entry_fill = next(fill for fill in own if _order_ref(fill) == ORDER_REF)
        sign = 1 if entry_fill.execution.side == "BOT" else -1
        entry_net = sum(cost / shares for shares, cost in entries.values())
        exit_net = sum(cost / shares for shares, cost in exits.values())
        multiplier = int(entry_fill.contract.multiplier or 100)
        realized[ticker] = (exit_net - entry_net) * multiplier * _filled_contracts(exits, entries) * sign
    return realized


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
        self._placed_today = set()
        self._halted_on = None  # the day the daily loss limit was hit
        self._realized = []  # (day, ticker, USD)
        self._needs_sync = False  # set by _connect() on every new connection: reconcile the book with IB first
        self._summarized_on = None
        self._journal = TradeJournal(settings.journal_path, settings.dry_run) if settings.journal_path else None

    # Notifier interface ---------------------------------------------------------------------------------------------

    def notify(self, investment_data):
        for strategy, opportunities in investment_data.items():
            if strategy not in self.settings.strategies:
                continue
            for ticker in opportunities:
                try:
                    self.place_straddle(ticker, strategy)
                except ValueError as e:  # a rule said no (caps, spread, no quote): routine, and it uses no slot
                    logger.warning("Not placing a %s on %s: %s", strategy, ticker, e)
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

    def _record(self, event, spec, quote=None, **fields):
        """Adds a row to the journal, if there is one, with the quote that applied when ``quote`` is given."""
        if self._journal is None:
            return
        if quote is not None:
            fields = {"bid": quote.bid, "ask": quote.ask, "mid": quote.mid, **fields}
        self._journal.record(event, spec, **fields)

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
        ib = self._connect()
        self._ensure_synced(ib, today)  # what an earlier run left at IB counts against every check below
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

        spec = self.build_straddle(ib, ticker, strategy, today)
        check_limits(spec, settings)
        check_spread(spec, settings)
        entry_quote = StraddleQuote(mid=spec.call_price + spec.put_price, bid=spec.bid, ask=spec.ask)

        if settings.dry_run:
            logger.info("DRY RUN - would place %s", spec.describe())
            self._announce("Straddle order", f"Would place {spec.describe()}")
            self._record("order placed", spec, entry_quote, limit=spec.limit_price)
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
            self._record("order placed", spec, entry_quote, limit=spec.limit_price, order_id=trade.order.orderId)
        self.positions[ticker] = position
        self._placed_today.add(ticker)
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
        quote = straddle_quote(call_snapshot, put_snapshot)
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
            bid=quote.bid,
            ask=quote.ask,
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
        self._ensure_synced(ib, today)
        self._refresh_orders(ib, today)
        near_close = now >= session.close - timedelta(minutes=settings.exit_minutes_before_close)
        if not self.positions:
            self._maybe_summarize(today, near_close)
            return

        closing_time = not settings.hold_overnight and near_close
        held = [p for p in self.positions.values() if p.filled]  # an exit still working leaves the risk on the book
        pending_entries = [p for p in self.positions.values() if not p.filled and p.entry_order_id is not None]
        quotes = self._quotes(ib, held)

        unrealized = sum(unrealized_pnl(p, quotes[p.spec.ticker].mid) for p in held if p.spec.ticker in quotes)
        day_pnl = self.realized_today(today) + unrealized
        limit_just_hit = (
            settings.max_daily_loss > 0 and day_pnl <= -settings.max_daily_loss and self._halted_on != today
        )
        if limit_just_hit:
            self._halted_on = today
            logger.error(
                "Daily loss limit hit (realized %+.0f, unrealized %+.0f, limit %.0f USD): closing everything, "
                "no new positions today",
                self.realized_today(today),
                unrealized,
                settings.max_daily_loss,
            )
        halted = self._halted_on == today

        for position in held:
            quote = quotes.get(position.spec.ticker)
            if quote is None:
                continue
            reason = DAILY_LOSS_REASON if halted else exit_reason(position, quote.mid, now, session, settings)
            if position.exit_order_id is not None:
                self._follow_market(ib, position, quote, reason)
            elif reason:
                self._close(ib, position, quote, reason, today)

        if halted or closing_time:
            for position in pending_entries:
                self._cancel_entry(ib, position)
        if limit_just_hit:
            self._announce(
                "Daily loss limit hit",
                f"Realized {self.realized_today(today):+,.0f} USD, unrealized {unrealized:+,.0f} USD, "
                f"limit {settings.max_daily_loss:,.0f} USD: closing everything; no new positions today",
            )
        self._maybe_summarize(today, near_close)

    def _ensure_synced(self, ib, today):
        """Reconciles the book with a new connection before anything acts on it."""
        if self._needs_sync:
            if not self.settings.dry_run:  # dry-run positions are virtual: IB knows nothing about them
                self._sync_book(ib, today)
            self._needs_sync = False

    def _roll_day(self, today):
        if today != self._day:
            self._day = today
            self._placed_today = set()

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
        for position in list(self.positions.values()):
            if position.exit_order_id is not None:
                trade = trades.get(position.exit_order_id)
                if trade is None:
                    continue
                status = trade.orderStatus.status
                if status == FILLED:
                    price = trade.orderStatus.avgFillPrice or position.exit_price
                    self._record_close(position, price, today, position.exit_reason)
                elif status in DEAD_ORDER_STATUSES:
                    self._exit_order_died(position, trade, today)
                continue
            if not position.filled and position.entry_order_id is not None:
                trade = trades.get(position.entry_order_id)
                if trade is not None:
                    self._settle_entry(ib, position, trade)

    def _settle_entry(self, ib, position, trade):
        """Applies what IB reports about a working entry order to the book."""
        ticker = position.spec.ticker
        status = trade.orderStatus.status
        filled = int(trade.orderStatus.filled)
        price = trade.orderStatus.avgFillPrice
        if status == FILLED:
            self._mark_entry_filled(position, position.spec.quantity, price)
        elif status in DEAD_ORDER_STATUSES and filled:
            self._mark_entry_filled(position, filled, price)
        elif status == REJECTED:
            logger.error("Entry order %s for %s was rejected by IB; forgetting it", trade.order.orderId, ticker)
            del self.positions[ticker]
            self._record("entry rejected", position.spec, order_id=trade.order.orderId)
            self._announce(
                "Straddle entry rejected",
                f"{position.spec.describe()}\nOrder id {trade.order.orderId} was rejected by IB; see TWS for why",
            )
        elif status in DEAD_ORDER_STATUSES:
            logger.warning("Entry order %s for %s was cancelled; forgetting it", trade.order.orderId, ticker)
            del self.positions[ticker]
            self._record("entry cancelled", position.spec, order_id=trade.order.orderId)
        elif filled and not position.cancel_requested:
            # A limit order still part-filled at the next poll has gone stale: stop it and manage what it bought,
            # rather than leave open risk outside the stops until the order completes.
            self._cancel_entry(ib, position)

    def _mark_entry_filled(self, position, contracts, price):
        ordered = position.spec.quantity
        position.filled = True
        position.entry_price = price or position.entry_price
        note = ""
        if contracts < ordered:
            position.spec = replace(position.spec, quantity=contracts)
            note = f" ({contracts} of {ordered} contracts; the rest was cancelled)"
        self._record("entry filled", position.spec, fill=position.entry_price, order_id=position.entry_order_id)
        self._announce(
            "Straddle entry filled", f"{position.spec.describe()}\nFilled at {position.entry_price:.2f}{note}"
        )
        logger.info("Entry filled: %s at %.2f%s", position.spec.describe(), position.entry_price, note)

    def _exit_order_died(self, position, trade, today):
        """A cancelled or rejected exit: books whatever it did close and leaves the rest to be closed again."""
        ticker = position.spec.ticker
        order_id = trade.order.orderId
        filled = int(trade.orderStatus.filled) - position.exit_contracts_booked
        if filled:
            self._book_partial_exit(position, filled, trade.orderStatus.avgFillPrice or position.exit_price, today)
            if ticker not in self.positions:
                return
        self._record(
            "exit rejected" if trade.orderStatus.status == REJECTED else "exit cancelled",
            position.spec,
            limit=position.exit_price,
            reason=position.exit_reason,
            order_id=order_id,
        )
        if trade.orderStatus.status == REJECTED:
            logger.error("Exit order %s for %s was rejected by IB; it will be placed again", order_id, ticker)
            if not position.exit_rejection_announced:
                position.exit_rejection_announced = True
                self._announce(
                    "Straddle exit rejected",
                    f"{ticker}: exit order {order_id} was rejected by IB. The bot keeps trying while the position "
                    "is open; see TWS for the reason",
                )
        else:
            logger.warning("Exit order %s for %s was cancelled; it will be placed again", order_id, ticker)
        position.forget_exit()

    def _book_partial_exit(self, position, contracts, price, today):
        """Books the contracts a dead exit order closed: the next exit must cover only the rest, or it goes short."""
        ticker = position.spec.ticker
        if contracts >= position.spec.quantity:
            self._record_close(position, price, today, f"{position.exit_reason}; fully filled")
            return
        pnl = unrealized_pnl(position, price) * contracts / position.spec.quantity
        self._realized.append((today, ticker, pnl))
        position.spec = replace(position.spec, quantity=position.spec.quantity - contracts)
        self._record("partly closed", replace(position.spec, quantity=contracts), fill=price, pnl_usd=round(pnl, 2))
        self._announce(
            "Straddle partly closed",
            f"{ticker}: {contracts} contract(s) closed at {price:.2f}: {pnl:+,.0f} USD; "
            f"{position.spec.quantity} still open",
        )
        logger.info("Closed %d contract(s) of the %s straddle at %.2f: %+.0f USD", contracts, ticker, price, pnl)

    def _follow_market(self, ib, position, quote, reason):
        """Keeps a working exit able to fill.

        A take-profit waits for its price while no rule fires. Once one does, or for any exit that
        has to get out, the limit follows the market, in place so that there is never a second
        live exit that could flip the position. It only ever moves towards a fill. In staged mode
        each new price waits for another click in TWS, like the order itself.
        """
        ticker = position.spec.ticker
        escalated = False
        if is_patient(position.exit_reason):
            if reason is None:
                return
            escalated = not is_patient(reason)
            position.exit_reason = reason
        limit = exit_limit(position, quote, position.exit_reason, self.settings.limit_buffer_pct)
        selling = position.spec.action == "BUY"
        if (selling and limit >= position.exit_price) or (not selling and limit <= position.exit_price):
            return
        trade = self._trade(ib, position.exit_order_id)
        if trade is None or not _is_working(trade):  # it can finish while the quotes are being fetched
            return
        trade.order.lmtPrice = limit
        trade.order.transmit = self.settings.transmit  # an order recovered from IB defaults to transmit=True
        ib.placeOrder(trade.contract, trade.order)
        logger.info(
            "Exit order %s for %s moved from %.2f to %.2f (%s)",
            position.exit_order_id,
            ticker,
            position.exit_price,
            limit,
            position.exit_reason,
        )
        position.exit_price = limit
        self._record(
            "exit moved",
            position.spec,
            quote,
            limit=limit,
            reason=position.exit_reason,
            order_id=position.exit_order_id,
        )
        if escalated:
            self._announce(
                "Closing straddle",
                f"{ticker}: exit moved to {limit:.2f} ({position.exit_reason}) - order id {position.exit_order_id}",
            )

    @staticmethod
    def _trade(ib, order_id):
        return next((trade for trade in ib.trades() if trade.order.orderId == order_id), None)

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
            self._record("closed", position.spec, quote, limit=limit, fill=limit, pnl_usd=round(pnl, 2), reason=reason)
            return
        reference = HALT_EXIT_REF if reason == DAILY_LOSS_REASON else EXIT_REF
        trade = ib.placeOrder(position.spec.contract, self._order(action, position.spec.quantity, limit, reference))
        position.exit_order_id = trade.order.orderId
        position.exit_price = limit
        position.exit_reason = reason
        self._record("exit placed", position.spec, quote, limit=limit, reason=reason, order_id=trade.order.orderId)
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
        self._record("closed", position.spec, fill=price, pnl_usd=round(pnl, 2), reason=how)
        self._announce(
            "Straddle closed",
            f"{ticker} closed at {price:.2f} ({how}): {pnl:+,.0f} USD; "
            f"realized today {self.realized_today(today):+,.0f} USD",
        )
        logger.info("Closed the %s straddle at %.2f (%s): %+.0f USD", ticker, price, how, pnl)

    def _sync_book(self, ib, today):
        """Reconciles the book with IB after a (re)connection.

        Applies the fills, cancellations and closes that happened while disconnected, using
        IB's positions, open orders and the day's executions, recovers what an earlier run left
        at IB (see _rebuild_book) and restores the day's limits (see _restore_day).
        """
        fills = _leg_fills(ib.reqExecutions())
        held = {position.contract.conId for position in ib.positions() if position.position}
        trades = ib.trades()
        open_order_ids = {trade.order.orderId for trade in trades}
        exit_fills = _fills_by_leg(fills, EXIT_REFS)
        known = set(self.positions)  # what this run already tracks, each with its own accounting of partial exits

        for ticker, position in list(self.positions.items()):
            legs_held = position.call.conId in held and position.put.conId in held
            if position.exit_order_id is not None:
                if not legs_held:
                    price = _fill_price(exit_fills, position) or position.exit_price
                    self._record_close(position, price, today, f"{position.exit_reason}; fill detected after reconnect")
                elif position.exit_order_id not in open_order_ids:
                    own = _fills_by_leg(fills, EXIT_REFS, position.exit_order_id)
                    closed = _filled_contracts(own, position.leg_ids) - position.exit_contracts_booked
                    if closed:
                        self._book_partial_exit(position, closed, _fill_price(own, position), today)
                    if ticker in self.positions:
                        logger.warning(
                            "Exit order %s for %s is gone after the reconnect; it will be placed again",
                            position.exit_order_id,
                            ticker,
                        )
                        position.forget_exit()
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
            else:
                # Holding the legs proves nothing by itself: only this order's own fills say it filled.
                own = _fills_by_leg(fills, {ORDER_REF}, position.entry_order_id)
                filled = _filled_contracts(own, position.leg_ids)
                order_open = position.entry_order_id in open_order_ids
                if legs_held and filled and (filled >= position.spec.quantity or not order_open):
                    self._mark_entry_filled(position, filled, _fill_price(own, position))
                elif not order_open:
                    logger.warning(
                        "Entry order %s for %s is gone after the reconnect; forgetting it",
                        position.entry_order_id,
                        ticker,
                    )
                    del self.positions[ticker]

        self._rebuild_book(ib, today, fills, held, trades, known)
        self._restore_day(today, fills, trades, known)

    def _restore_day(self, today, fills, trades, known):
        """Rebuilds the day's limits from what IB remembers, so that restarting the bot cannot reset them.

        Entries that never filled and are no longer working leave no trace to count, so the order
        cap can only forget those. Realized P&L is restored only for what this run does not already
        account for (``known``): the bot trades a ticker once a day, so a ticker identifies its trade.
        """
        working = [trade for trade in trades if _is_working(trade)]
        self._placed_today |= {fill.contract.symbol for fill in fills if _order_ref(fill) == ORDER_REF}
        self._placed_today |= {trade.contract.symbol for trade in working if trade.order.orderRef == ORDER_REF}

        booked = known | {ticker for day, ticker, _ in self._realized if day == today}
        for ticker, pnl in _realized_by_ticker(fills).items():
            if ticker not in booked:
                self._realized.append((today, ticker, pnl))

        limit = self.settings.max_daily_loss
        if limit <= 0 or self._halted_on == today:
            return
        tagged = any(_order_ref(fill) == HALT_EXIT_REF for fill in fills) or any(
            trade.order.orderRef == HALT_EXIT_REF for trade in working
        )
        if tagged or self.realized_today(today) <= -limit:
            self._halted_on = today
            logger.error(
                "The daily loss limit was already hit today (realized %+.0f USD): closing what is open, "
                "no new positions today",
                self.realized_today(today),
            )

    def _cancel_entry(self, ib, position):
        """Cancels a working entry. It stays in the book until IB confirms: fills can still arrive until then."""
        ticker = position.spec.ticker
        trade = self._trade(ib, position.entry_order_id)
        if trade is None:
            logger.warning("Entry order %s for %s is unknown to IB; forgetting it", position.entry_order_id, ticker)
            del self.positions[ticker]
            return
        if not position.cancel_requested:
            position.cancel_requested = True
            ib.cancelOrder(trade.order)
            logger.info("Cancelling the entry for %s (order id %s)", ticker, position.entry_order_id)
        self._settle_entry(ib, position, trade)

    def _recover_working_entry(self, ib, trade, today):
        """Puts an entry order an earlier run left working back in the book, so that its fill gets managed."""
        from ib_async import Contract

        ticker = trade.contract.symbol
        order_id = trade.order.orderId
        legs = [Contract(conId=leg.conId, exchange="SMART") for leg in trade.contract.comboLegs]
        ib.qualifyContracts(*legs)
        call = next((leg for leg in legs if leg.right == "C"), None)
        put = next((leg for leg in legs if leg.right == "P"), None)
        if call is None or put is None:
            logger.error("Could not resolve the legs of working order %s on %s; check it in TWS", order_id, ticker)
            return
        action = trade.order.action
        limit = trade.order.lmtPrice
        spec = StraddleSpec(
            ticker=ticker,
            strategy=LONG if action == "BUY" else SHORT,
            action=action,
            expiry=call.lastTradeDateOrContractMonth,
            strike=call.strike,
            underlying_price=0.0,  # unknown for a recovered order
            call_price=0.0,
            put_price=0.0,
            limit_price=limit,
            quantity=int(trade.order.totalQuantity),
            multiplier=int(call.multiplier or 100),
            contract=trade.contract,
            call=call,
            put=put,
        )
        self.positions[ticker] = OpenStraddle(spec, call, put, order_id, limit, False, today)
        logger.info(
            "Recovered the working %s entry on %s (order id %s, limit %.2f)", spec.strategy, ticker, order_id, limit
        )

    def _rebuild_book(self, ib, today, fills, held, trades, known):
        """Recovers what an earlier run left at IB, found by our order references.

        Working entries first, so that a part-filled one is settled by its order status; then
        straddles opened today, from their executions, less what has been closed since; then
        working exits, which must be adopted: placing another would leave the position short
        once both filled.
        """
        working = [trade for trade in trades if _is_working(trade)]
        for trade in working:
            if trade.order.orderRef == ORDER_REF and trade.contract.symbol not in self.positions:
                self._recover_working_entry(ib, trade, today)

        self._recover_open_straddles(today, fills, held)

        for trade in working:
            if trade.order.orderRef not in EXIT_REFS:
                continue
            position = self.positions.get(trade.contract.symbol)
            if position is not None and position.filled and position.exit_order_id is None:
                position.exit_order_id = trade.order.orderId
                position.exit_price = trade.order.lmtPrice
                position.exit_reason = RECOVERED_EXIT_REASON
                if position.spec.ticker not in known:  # recovered net of what this exit has already closed
                    position.exit_contracts_booked = int(trade.orderStatus.filled)
                logger.info("Adopted the working exit order %s for %s", trade.order.orderId, position.spec.ticker)

    def _recover_open_straddles(self, today, fills, held):
        """Straddles opened today by an earlier run and still open, from the executions tagged with our reference."""
        from ib_async import Bag, ComboLeg

        exits = _fills_by_leg(fills, EXIT_REFS)
        by_order = {}
        for fill in fills:
            if _order_ref(fill) == ORDER_REF:
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
            quantity = int(min(call["shares"], put["shares"])) - _filled_contracts(exits, legs)
            if quantity <= 0:
                continue  # closed since, with the legs held for some other reason
            # An execution names the venue that filled it; quotes and orders must go through SMART.
            call["contract"].exchange = put["contract"].exchange = "SMART"

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
                quantity=quantity,
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
