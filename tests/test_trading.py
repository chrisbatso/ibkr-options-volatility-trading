import csv
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from ib_async import IB, Bag, ComboLeg, LimitOrder, Option, OptionChain

from market_watcher.config import context
from market_watcher.market_hours import Session
from market_watcher.trading import (
    EXIT_REF,
    HALT_EXIT_REF,
    LONG,
    ORDER_REF,
    SHORT,
    IBKRStraddleTrader,
    OpenStraddle,
    StraddleQuote,
    StraddleSpec,
    TradingSettings,
    check_limits,
    choose_expiry,
    exit_limit,
    exit_reason,
    limit_price,
    option_price,
    parse_strategies,
    select_chain,
    straddle_quote,
    strikes_by_distance,
    unrealized_pnl,
)

TODAY = date(2026, 11, 4)
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)  # 10:00 New York
SESSION = Session(day=TODAY, open=NOW - timedelta(minutes=30), close=NOW + timedelta(hours=6))
NEAR_CLOSE = SESSION.close - timedelta(minutes=10)


@dataclass
class Snapshot:
    last: float = math.nan
    bid: float = math.nan
    ask: float = math.nan
    close: float = math.nan


class FakeTrade:
    def __init__(self, order, order_id, status="PendingSubmit", contract=None):
        order.orderId = order_id
        self.order = order
        self.contract = contract
        self.orderStatus = SimpleNamespace(status=status, avgFillPrice=0.0, filled=0.0)

    def fill(self, price):
        self.orderStatus.status = "Filled"
        self.orderStatus.avgFillPrice = price
        self.orderStatus.filled = self.order.totalQuantity

    def partial_fill(self, contracts, price):
        self.orderStatus.status = "Submitted"  # IB reports a partly filled order as still working
        self.orderStatus.avgFillPrice = price
        self.orderStatus.filled = contracts

    def cancel(self):
        self.orderStatus.status = "Cancelled"

    def reject(self):
        self.orderStatus.status = "Inactive"


class FakeIB:
    """TWS stand-in: a few stocks at 101.30, one option chain with strikes 95/100/105, quotes per leg, an order book."""

    instances = []
    accounts = ["DU1234567"]
    stocks = {"AAPL": 1, "MSFT": 2, "KO": 3}
    price = 101.3
    available_strikes = {95.0, 100.0, 105.0}
    quotes = {}
    chains = []
    executions = []
    held = []
    live_trades = []  # orders an earlier run or connection left at IB: every new connection sees them
    cancel_status = "Cancelled"  # what a cancel does at once; real IB goes through PendingCancel

    def __init__(self):
        self.connected = False
        self.calls = []
        self.orders = []
        self.trade_objects = []
        self.cancelled = []
        self.modifications = []
        FakeIB.instances.append(self)

    def connect(self, host, port, clientId, timeout, readonly):  # noqa: N803 - ib_async's keyword
        self.calls.append(("connect", host, port, clientId, timeout, readonly))
        self.connected = True

    def isConnected(self):  # noqa: N802 - ib_async's name
        return self.connected

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False

    def managedAccounts(self):  # noqa: N802
        return list(FakeIB.accounts)

    def reqMarketDataType(self, market_data_type):  # noqa: N802
        self.calls.append(("reqMarketDataType", market_data_type))

    def qualifyContracts(self, *contracts):  # noqa: N802
        for contract in contracts:
            if contract.secType == "STK":
                contract.conId = FakeIB.stocks.get(contract.symbol, 0)
            elif contract.secType == "OPT" and contract.strike in FakeIB.available_strikes:
                contract.conId = int(contract.strike * 10) + (1 if contract.right == "C" else 2)
            elif not contract.secType and contract.conId:  # looked up by conId alone: IB fills in the rest
                contract.secType = "OPT"
                contract.symbol = "AAPL"
                contract.lastTradeDateOrContractMonth = "20261120"
                contract.strike = float(contract.conId // 10)
                contract.right = "C" if contract.conId % 10 == 1 else "P"
                contract.multiplier = "100"
        return [contract for contract in contracts if contract.conId]

    def reqTickers(self, *contracts):  # noqa: N802
        return [
            Snapshot(last=FakeIB.price)
            if contract.secType == "STK"
            else FakeIB.quotes.get((contract.right, contract.strike), Snapshot())
            for contract in contracts
        ]

    def reqSecDefOptParams(self, symbol, fut_fop_exchange, sec_type, con_id):  # noqa: N802
        self.calls.append(("reqSecDefOptParams", symbol, sec_type, con_id))
        return list(FakeIB.chains)

    def placeOrder(self, contract, order):  # noqa: N802
        if order.orderId:  # like IB: an order that already has an id is a modification, not a second order
            self.modifications.append((order.orderId, order.lmtPrice))
            return next(trade for trade in self.trades() if trade.order.orderId == order.orderId)
        self.orders.append((contract, order))
        trade = FakeTrade(order, len(self.orders), contract=contract)
        self.trade_objects.append(trade)
        return trade

    def trades(self):
        return list(self.trade_objects) + list(FakeIB.live_trades)

    def cancelOrder(self, order):  # noqa: N802
        self.cancelled.append(order.orderId)
        for trade in self.trades():
            if trade.order.orderId == order.orderId:
                trade.orderStatus.status = FakeIB.cancel_status

    def positions(self):
        return list(FakeIB.held)

    def reqExecutions(self):  # noqa: N802
        return list(FakeIB.executions)


DEFAULT_QUOTES = {
    ("C", 100.0): Snapshot(bid=3.0, ask=3.2),
    ("P", 100.0): Snapshot(bid=2.8, ask=3.0),
    ("C", 105.0): Snapshot(bid=1.0, ask=1.2),
    ("P", 105.0): Snapshot(bid=5.0, ask=5.2),
}
DEFAULT_CHAINS = [OptionChain("SMART", 1, "AAPL", "100", ["20261106", "20261120", "20261218"], [95.0, 100.0, 105.0])]


def quote_atm(call_bid, call_ask, put_bid, put_ask):
    """Re-quotes the 100 strike; the straddle mid is (call + put mids)."""
    FakeIB.quotes[("C", 100.0)] = Snapshot(bid=call_bid, ask=call_ask)
    FakeIB.quotes[("P", 100.0)] = Snapshot(bid=put_bid, ask=put_ask)


@pytest.fixture
def fake_ib():
    FakeIB.instances = []
    FakeIB.accounts = ["DU1234567"]
    FakeIB.stocks = {"AAPL": 1, "MSFT": 2, "KO": 3}
    FakeIB.price = 101.3
    FakeIB.available_strikes = {95.0, 100.0, 105.0}
    FakeIB.quotes = dict(DEFAULT_QUOTES)
    FakeIB.chains = list(DEFAULT_CHAINS)
    FakeIB.executions = []
    FakeIB.held = []
    FakeIB.live_trades = []
    FakeIB.cancel_status = "Cancelled"
    return FakeIB


def make_trader(**overrides):
    settings = TradingSettings(**{"enabled": True, "max_spread_pct": 0.0, **overrides})
    return IBKRStraddleTrader(settings, ib_factory=FakeIB, today=lambda: TODAY)


def open_long(**overrides):
    """A trader with one long AAPL straddle placed (entry limit 6.12); returns it with its FakeIB."""
    trader = make_trader(**overrides)
    trader.place_straddle("AAPL", LONG)
    return trader, FakeIB.instances[-1]


# --- Entries ---------------------------------------------------------------------------------------------------------


def test_long_straddle_is_one_combo_limit_order_bought_at_the_money(fake_ib):
    spec = make_trader().place_straddle("AAPL", LONG)

    [ib] = fake_ib.instances
    assert ib.calls[0] == ("connect", "127.0.0.1", 7497, 2, 10, False)  # the trading client is not read-only
    assert ("reqMarketDataType", 3) in ib.calls
    assert spec.expiry == "20261120"  # nearest expiry at least 7 days after 2026-11-04
    assert spec.strike == 100.0  # at the money for 101.30
    assert (spec.call_price, spec.put_price) == (pytest.approx(3.1), pytest.approx(2.9))
    assert spec.limit_price == pytest.approx(6.12)  # (3.1 + 2.9) * 1.02
    assert spec.premium == pytest.approx(612.0)

    [(contract, order)] = ib.orders
    assert contract.secType == "BAG" and contract.symbol == "AAPL"
    assert [(leg.conId, leg.action, leg.ratio) for leg in contract.comboLegs] == [(1001, "BUY", 1), (1002, "BUY", 1)]
    assert (order.action, order.totalQuantity, order.lmtPrice) == ("BUY", 1, 6.12)
    assert (order.tif, order.transmit, order.orderRef) == ("DAY", False, ORDER_REF)


def test_short_straddle_sells_the_combo_below_mid(fake_ib):
    spec = make_trader(strategies=frozenset({SHORT})).place_straddle("AAPL", SHORT)

    assert spec.action == "SELL"
    assert spec.limit_price == pytest.approx(5.88)  # (3.1 + 2.9) * 0.98
    assert spec.notional == 10_000.0
    [(_, order)] = fake_ib.instances[0].orders
    assert order.action == "SELL"


def test_falls_back_to_the_next_closest_strike_when_the_atm_pair_does_not_qualify(fake_ib):
    fake_ib.available_strikes = {95.0, 105.0}

    spec = make_trader().place_straddle("AAPL", LONG)

    assert spec.strike == 105.0  # 3.70 away beats 95 at 6.30
    assert spec.limit_price == pytest.approx(round((1.1 + 5.1) * 1.02, 2))


def test_premium_cap_blocks_expensive_long_straddles(fake_ib):
    with pytest.raises(ValueError, match="exceeds STRADDLE_MAX_PREMIUM"):
        make_trader(max_premium=500).place_straddle("AAPL", LONG)

    assert fake_ib.instances[0].orders == []


def test_notional_cap_blocks_large_short_straddles(fake_ib):
    with pytest.raises(ValueError, match="exceeds STRADDLE_MAX_NOTIONAL"):
        make_trader(strategies=frozenset({SHORT}), max_notional=5_000).place_straddle("AAPL", SHORT)

    assert fake_ib.instances[0].orders == []


def test_risk_per_trade_sizes_long_straddles_by_premium(fake_ib):
    spec = make_trader(contracts=5, risk_per_trade=2_000, max_premium=10_000).place_straddle("AAPL", LONG)
    assert spec.quantity == 3  # 2,000 / 612 per contract, capped by STRADDLE_CONTRACTS = 5

    with pytest.raises(ValueError, match="buys no contract"):
        make_trader(risk_per_trade=500).place_straddle("MSFT", LONG)


def test_refuses_to_trade_on_a_live_account_unless_allowed(fake_ib):
    fake_ib.accounts = ["U7654321"]

    with pytest.raises(RuntimeError, match="not paper accounts"):
        make_trader().place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    assert ("disconnect",) in ib.calls
    assert ib.orders == []

    make_trader(allow_live=True).place_straddle("AAPL", LONG)
    assert len(fake_ib.instances[1].orders) == 1


def test_notify_trades_only_enabled_strategies_and_isolates_failures(fake_ib):
    trader = make_trader()  # long straddles only

    trader.notify({LONG: {"AAPL": 0.05, "ZZZZ": 0.06, "MSFT": 0.07}, SHORT: {"KO": 0.0}})

    [ib] = fake_ib.instances
    assert [contract.symbol for contract, _ in ib.orders] == ["AAPL", "MSFT"]  # ZZZZ is unknown, KO is short


def test_one_order_per_ticker_per_day_and_a_daily_cap(fake_ib):
    trader = make_trader(max_orders_per_day=2, max_open_positions=10)

    assert trader.place_straddle("AAPL", LONG) is not None
    assert trader.place_straddle("AAPL", LONG) is None  # same ticker again today
    assert trader.place_straddle("MSFT", LONG) is not None
    assert trader.place_straddle("KO", LONG) is None  # daily cap reached

    assert len(fake_ib.instances[0].orders) == 2


def test_max_open_positions_blocks_new_entries(fake_ib):
    trader = make_trader(max_open_positions=1)

    assert trader.place_straddle("AAPL", LONG) is not None
    assert trader.place_straddle("MSFT", LONG) is None
    assert len(fake_ib.instances[0].orders) == 1


def test_the_daily_memory_resets_on_a_new_day(fake_ib):
    days = iter([TODAY, TODAY, TODAY + timedelta(days=1)])
    settings = TradingSettings(enabled=True, max_spread_pct=0.0)
    trader = IBKRStraddleTrader(settings, ib_factory=FakeIB, today=lambda: next(days))

    assert trader.place_straddle("AAPL", LONG) is not None
    assert trader.place_straddle("AAPL", LONG) is None
    trader.positions.clear()  # the first straddle was closed meanwhile
    assert trader.place_straddle("AAPL", LONG) is not None


def test_dry_run_builds_the_order_but_sends_nothing(fake_ib):
    spec = make_trader(dry_run=True).place_straddle("AAPL", LONG)

    assert spec is not None and spec.limit_price == pytest.approx(6.12)
    [ib] = fake_ib.instances
    assert ib.orders == []
    assert ib.calls[0][-1] is True  # a dry run only needs a read-only connection


def test_transmit_flag_is_passed_to_the_order(fake_ib):
    make_trader(transmit=True).place_straddle("AAPL", LONG)

    [(_, order)] = fake_ib.instances[0].orders
    assert order.transmit is True


def test_connection_is_reused_and_close_disconnects(fake_ib):
    trader = make_trader()
    trader.place_straddle("AAPL", LONG)
    trader.place_straddle("MSFT", LONG)
    assert len(fake_ib.instances) == 1

    trader.close()
    trader.close()

    [ib] = fake_ib.instances
    assert not ib.connected
    assert ib.calls.count(("disconnect",)) == 1


def test_explains_the_missing_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "ib_async", None)  # makes `import ib_async` raise ImportError
    trader = IBKRStraddleTrader(TradingSettings(enabled=True), today=lambda: TODAY)

    with pytest.raises(RuntimeError, match=r'pip install "market_watcher\[ibkr\]"'):
        trader.place_straddle("AAPL", LONG)


# --- Position management ---------------------------------------------------------------------------------------------


def test_entry_is_pending_until_ib_reports_the_fill(fake_ib):
    trader, ib = open_long()

    trader.manage_positions(NOW, SESSION)
    [position] = trader.positions.values()
    assert not position.filled and position.entry_price == pytest.approx(6.12)
    assert len(ib.orders) == 1  # a pending entry is never "exited"

    ib.trade_objects[0].fill(6.05)
    trader.manage_positions(NOW, SESSION)
    assert position.filled and position.entry_price == 6.05


def test_cancelled_or_rejected_entry_is_forgotten(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].cancel()

    trader.manage_positions(NOW, SESSION)

    assert trader.positions == {}


def test_no_exit_while_the_value_stays_inside_the_band(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)

    trader.manage_positions(NOW, SESSION)  # still quoted at a 6.00 mid

    assert len(ib.orders) == 1
    assert trader.positions["AAPL"].exit_order_id is None


def test_take_profit_sells_the_same_combo_a_little_below_mid(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)  # straddle mid 9.20 >= 6.00 * 1.5

    trader.manage_positions(NOW, SESSION)

    [(entry_contract, _), (exit_contract, exit_order)] = ib.orders
    assert exit_contract is entry_contract
    assert (exit_order.action, exit_order.totalQuantity, exit_order.orderRef, exit_order.transmit) == (
        "SELL",
        1,
        EXIT_REF,
        False,
    )
    assert exit_order.lmtPrice == pytest.approx(round(9.2 * 0.98, 2))
    position = trader.positions["AAPL"]
    assert position.exit_order_id == 2 and position.exit_reason.startswith("take profit")


def test_stop_loss_crosses_the_spread(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)
    quote_atm(1.4, 1.5, 1.4, 1.5)  # mid 2.90 <= 3.00; straddle bid 2.80

    trader.manage_positions(NOW, SESSION)

    [_, (_, exit_order)] = ib.orders
    assert exit_order.action == "SELL" and exit_order.lmtPrice == pytest.approx(2.8)
    assert trader.positions["AAPL"].exit_reason.startswith("stop loss")


def test_positions_are_closed_before_the_session_close_unless_held_overnight(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)

    trader.manage_positions(NEAR_CLOSE, SESSION)

    [_, (_, exit_order)] = ib.orders
    assert exit_order.lmtPrice == pytest.approx(5.8)  # straddle bid 3.0 + 2.8: marketable
    assert trader.positions["AAPL"].exit_reason == "session close"

    overnight, ib2 = open_long(hold_overnight=True)
    ib2.trade_objects[0].fill(6.0)
    overnight.manage_positions(NEAR_CLOSE, SESSION)
    assert len(ib2.orders) == 1


def test_unfilled_entries_are_cancelled_at_the_session_close(fake_ib):
    trader, ib = open_long()

    trader.manage_positions(NEAR_CLOSE, SESSION)

    assert ib.cancelled == [1]
    assert trader.positions == {}


def test_exit_fill_realizes_the_pnl_and_frees_the_slot(fake_ib):
    trader, ib = open_long(max_open_positions=1)
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)  # places the take-profit exit
    ib.trade_objects[1].fill(9.1)

    trader.manage_positions(NOW, SESSION)

    assert trader.positions == {}
    assert trader.realized_today(TODAY) == pytest.approx((9.1 - 6.0) * 100)
    assert trader.place_straddle("MSFT", LONG) is not None  # the slot is free again


def test_cancelled_exit_is_placed_again(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    ib.trade_objects[1].cancel()

    trader.manage_positions(NOW, SESSION)

    assert len(ib.orders) == 3
    assert trader.positions["AAPL"].exit_order_id == 3


def test_daily_loss_limit_closes_everything_and_halts_new_entries(fake_ib):
    trader = make_trader(max_daily_loss=100)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    quote_atm(2.4, 2.5, 2.4, 2.5)  # mid 4.90: -110 USD unrealized, inside the stop-loss band but past the daily limit

    trader.manage_positions(NOW, SESSION)

    [_, (_, exit_order)] = ib.orders
    assert exit_order.action == "SELL" and exit_order.lmtPrice == pytest.approx(4.8)  # crosses the spread
    assert trader.positions["AAPL"].exit_reason == "daily loss limit"
    assert trader.place_straddle("MSFT", LONG) is None
    assert len(ib.orders) == 2


def test_short_straddle_is_bought_back_on_take_profit(fake_ib):
    trader = make_trader(strategies=frozenset({SHORT}))
    trader.place_straddle("AAPL", SHORT)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(5.9)
    quote_atm(1.4, 1.5, 1.4, 1.5)  # mid 2.90 <= 5.90 * 0.5

    trader.manage_positions(NOW, SESSION)

    [_, (_, exit_order)] = ib.orders
    assert exit_order.action == "BUY" and exit_order.lmtPrice == pytest.approx(round(2.9 * 1.02, 2))


def test_dry_run_rehearses_exits_without_sending_orders(fake_ib):
    trader = make_trader(dry_run=True)
    trader.place_straddle("AAPL", LONG)  # recorded as filled at the limit, 6.12
    quote_atm(5.0, 5.2, 4.0, 4.2)

    trader.manage_positions(NOW, SESSION)

    [ib] = fake_ib.instances
    assert ib.orders == []
    assert trader.positions == {}
    assert trader.realized_today(TODAY) == pytest.approx((round(9.2 * 0.98, 2) - 6.12) * 100)


def execution(con_id, right, order_id, price, side="BOT", shares=1, ref=ORDER_REF):
    contract = Option("AAPL", "20261120", 100.0, right, "SMART", multiplier="100", currency="USD")
    contract.conId = con_id
    return SimpleNamespace(
        contract=contract,
        execution=SimpleNamespace(orderId=order_id, orderRef=ref, side=side, shares=shares, price=price),
    )


def test_the_book_is_rebuilt_from_todays_executions_after_a_restart(fake_ib):
    fake_ib.executions = [
        execution(1001, "C", 7, 3.05),
        execution(1002, "P", 7, 2.95),
        execution(1051, "C", 8, 1.0, ref="something else"),  # not ours
    ]
    fake_ib.held = [
        SimpleNamespace(contract=SimpleNamespace(conId=1001), position=1),
        SimpleNamespace(contract=SimpleNamespace(conId=1002), position=1),
    ]
    trader = make_trader()

    trader.manage_positions(NOW, SESSION)

    [position] = trader.positions.values()
    assert position.filled and position.entry_order_id is None
    assert position.entry_price == pytest.approx(6.0)
    assert (position.spec.ticker, position.spec.action, position.spec.strategy) == ("AAPL", "BUY", LONG)
    assert [leg.conId for leg in position.spec.contract.comboLegs] == [1001, 1002]

    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    [(exit_contract, exit_order)] = fake_ib.instances[0].orders
    assert exit_order.action == "SELL" and exit_contract.comboLegs[0].conId == 1001


def test_rebuild_skips_straddles_no_longer_held(fake_ib):
    fake_ib.executions = [execution(1001, "C", 7, 3.05), execution(1002, "P", 7, 2.95)]
    fake_ib.held = []

    trader = make_trader()
    trader.manage_positions(NOW, SESSION)

    assert trader.positions == {}


# --- Pure helpers ----------------------------------------------------------------------------------------------------


def test_choose_expiry_takes_the_nearest_expiry_far_enough_out():
    expirations = ["20261218", "20261106", "20261120"]

    assert choose_expiry(expirations, TODAY, 7) == "20261120"
    assert choose_expiry(expirations, TODAY, 0) == "20261106"
    with pytest.raises(ValueError, match="at least 60 days out"):
        choose_expiry(expirations, TODAY, 60)


def test_strikes_by_distance_prefers_the_closer_then_the_lower_strike():
    assert strikes_by_distance([95.0, 100.0, 105.0], 101.3) == [100.0, 105.0, 95.0]
    assert strikes_by_distance([110.0, 100.0], 105.0) == [100.0, 110.0]


def test_select_chain_prefers_smart_in_the_symbols_own_trading_class():
    other = OptionChain("CBOE", 1, "AAPL", "100", [], [])
    smart_other_class = OptionChain("SMART", 1, "AAPL7", "100", [], [])
    smart = OptionChain("SMART", 1, "AAPL", "100", [], [])

    assert select_chain([other, smart_other_class, smart], "AAPL") is smart
    assert select_chain([other, smart_other_class], "AAPL") is smart_other_class
    assert select_chain([other], "AAPL") is other
    with pytest.raises(ValueError, match="no option chain"):
        select_chain([], "AAPL")


def test_option_price_prefers_the_midpoint():
    assert option_price(Snapshot(bid=1.0, ask=1.2, last=5.0)) == pytest.approx(1.1)
    assert option_price(Snapshot(last=5.0)) == 5.0
    assert option_price(Snapshot(bid=1.0)) is None


def test_straddle_quote_sums_the_legs():
    quote = straddle_quote(Snapshot(bid=3.0, ask=3.2), Snapshot(bid=2.8, ask=3.0))
    assert (quote.mid, quote.bid, quote.ask) == (pytest.approx(6.0), pytest.approx(5.8), pytest.approx(6.2))

    one_sided = straddle_quote(Snapshot(last=3.0), Snapshot(bid=2.8, ask=3.0))
    assert one_sided.mid == pytest.approx(5.9) and one_sided.bid is None and one_sided.ask is None
    assert straddle_quote(Snapshot(), Snapshot(bid=2.8, ask=3.0)) is None


def test_limit_price_applies_the_buffer_in_the_right_direction():
    assert limit_price(6.0, "BUY", 2.0) == 6.12
    assert limit_price(6.0, "SELL", 2.0) == 5.88


def spec(action, limit, strike=100.0, quantity=1):
    strategy = LONG if action == "BUY" else SHORT
    return StraddleSpec("AAPL", strategy, action, "20261120", strike, 101.3, 3.1, 2.9, limit, quantity, 100, None)


def position(action, entry):
    return OpenStraddle(
        spec=spec(action, entry), call=None, put=None, entry_order_id=1, entry_price=entry, filled=True, opened_on=TODAY
    )


def test_check_limits():
    settings = TradingSettings(max_premium=1_000.0, max_notional=20_000.0)

    check_limits(spec("BUY", 9.99), settings)  # 999 USD
    check_limits(spec("SELL", 6.0, strike=200.0), settings)  # 20,000 USD notional
    with pytest.raises(ValueError, match="premium"):
        check_limits(spec("BUY", 10.01), settings)
    with pytest.raises(ValueError, match="notional"):
        check_limits(spec("SELL", 6.0, strike=200.5), settings)


def test_unrealized_pnl_has_the_right_sign_for_each_side():
    assert unrealized_pnl(position("BUY", 6.0), 6.5) == pytest.approx(50.0)
    assert unrealized_pnl(position("SELL", 6.0), 6.5) == pytest.approx(-50.0)


@pytest.mark.parametrize(
    ("action", "entry", "value", "now", "expected"),
    [
        ("BUY", 6.0, 9.0, NOW, "take profit"),
        ("BUY", 6.0, 8.99, NOW, None),
        ("BUY", 6.0, 3.0, NOW, "stop loss"),
        ("BUY", 6.0, 3.01, NOW, None),
        ("SELL", 6.0, 3.0, NOW, "take profit"),
        ("SELL", 6.0, 9.0, NOW, "stop loss"),
        ("BUY", 6.0, 6.0, SESSION.close - timedelta(minutes=15), "session close"),
        ("BUY", 6.0, 6.0, SESSION.close - timedelta(minutes=16), None),
    ],
)
def test_exit_reason(action, entry, value, now, expected):
    reason = exit_reason(position(action, entry), value, now, SESSION, TradingSettings())

    assert (reason or "").startswith(expected or "") if expected else reason is None


def test_exit_rules_can_be_disabled():
    settings = TradingSettings(take_profit_pct=0, stop_loss_pct=0, hold_overnight=True)

    assert exit_reason(position("BUY", 6.0), 60.0, NEAR_CLOSE, SESSION, settings) is None
    assert exit_reason(position("BUY", 6.0), 0.01, NEAR_CLOSE, SESSION, settings) is None


def test_exit_limit_is_gentle_on_profits_and_marketable_otherwise():
    quote = StraddleQuote(mid=9.0, bid=8.8, ask=9.2)
    long = position("BUY", 6.0)
    short = position("SELL", 6.0)

    assert exit_limit(long, quote, "take profit", 2.0) == pytest.approx(8.82)
    assert exit_limit(long, quote, "stop loss", 2.0) == pytest.approx(8.8)
    assert exit_limit(short, quote, "take profit", 2.0) == pytest.approx(9.18)
    assert exit_limit(short, quote, "session close", 2.0) == pytest.approx(9.2)
    assert exit_limit(long, StraddleQuote(mid=9.0, bid=None, ask=None), "stop loss", 2.0) == pytest.approx(8.82)


def test_spec_describe_reads_like_an_order_ticket():
    text = spec("BUY", 6.12).describe()

    assert text.startswith("BUY 1 x AAPL 20261120 100 straddle (long straddle)")
    assert "limit 6.12 = 612 USD" in text


# --- Settings --------------------------------------------------------------------------------------------------------


def test_parse_strategies():
    assert parse_strategies(None) == frozenset({LONG})
    assert parse_strategies(" long straddle, Short Straddle ") == frozenset({LONG, SHORT})
    with pytest.raises(ValueError, match="iron condor"):
        parse_strategies("long straddle, iron condor")


TRADING_KEYS = (
    "TRADING_ENABLED",
    "TRADING_DRY_RUN",
    "IBKR_TRANSMIT_ORDERS",
    "TRADING_ALLOW_LIVE",
    "TRADING_STRATEGIES",
    "STRADDLE_CONTRACTS",
    "STRADDLE_MAX_PREMIUM",
    "STRADDLE_MAX_NOTIONAL",
    "STRADDLE_MIN_DAYS_TO_EXPIRY",
    "STRADDLE_LIMIT_BUFFER_PCT",
    "TRADING_MAX_ORDERS_PER_DAY",
    "TRADING_TAKE_PROFIT_PCT",
    "TRADING_STOP_LOSS_PCT",
    "TRADING_EXIT_MINUTES_BEFORE_CLOSE",
    "TRADING_HOLD_OVERNIGHT",
    "TRADING_MAX_OPEN_POSITIONS",
    "TRADING_MAX_DAILY_LOSS",
    "STRADDLE_RISK_PER_TRADE",
    "STRADDLE_MAX_SPREAD_PCT",
    "TRADING_JOURNAL",
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_TRADING_CLIENT_ID",
    "IBKR_MARKET_DATA_TYPE",
    "IBKR_TIMEOUT",
)


def test_settings_are_cautious_by_default(monkeypatch):
    for key in TRADING_KEYS:
        monkeypatch.delitem(context.config, key, raising=False)

    settings = TradingSettings.from_config()

    assert settings == TradingSettings()
    assert (settings.enabled, settings.transmit, settings.allow_live, settings.dry_run) == (False, False, False, False)
    assert settings.strategies == frozenset({LONG})
    assert (settings.take_profit_pct, settings.stop_loss_pct, settings.exit_minutes_before_close) == (50.0, 50.0, 15)
    assert (settings.hold_overnight, settings.max_open_positions, settings.max_daily_loss, settings.risk_per_trade) == (
        False,
        3,
        500.0,
        0.0,
    )
    assert (settings.max_spread_pct, settings.journal_path) == (6.0, "")
    description = settings.describe()
    assert "staged in TWS" in description and "paper accounts only" in description
    assert "max spread 6% of mid" in description
    assert (
        "take profit 50%" in description and "stop loss 50%" in description and "15 min before the close" in description
    )


def test_settings_are_read_from_the_configuration(monkeypatch):
    values = {
        "TRADING_ENABLED": "true",
        "TRADING_DRY_RUN": "yes",
        "IBKR_TRANSMIT_ORDERS": "true",
        "TRADING_ALLOW_LIVE": "true",
        "TRADING_STRATEGIES": "long straddle,short straddle",
        "STRADDLE_CONTRACTS": "2",
        "STRADDLE_MAX_PREMIUM": "2500",
        "STRADDLE_MAX_NOTIONAL": "50000.5",
        "STRADDLE_MIN_DAYS_TO_EXPIRY": "14",
        "STRADDLE_LIMIT_BUFFER_PCT": "1.5",
        "TRADING_MAX_ORDERS_PER_DAY": "5",
        "TRADING_TAKE_PROFIT_PCT": "40",
        "TRADING_STOP_LOSS_PCT": "30",
        "TRADING_EXIT_MINUTES_BEFORE_CLOSE": "20",
        "TRADING_HOLD_OVERNIGHT": "true",
        "TRADING_MAX_OPEN_POSITIONS": "4",
        "TRADING_MAX_DAILY_LOSS": "750",
        "STRADDLE_RISK_PER_TRADE": "1200",
        "STRADDLE_MAX_SPREAD_PCT": "8.5",
        "TRADING_JOURNAL": "journal.csv",
        "IBKR_HOST": "gateway",
        "IBKR_PORT": "4002",
        "IBKR_TRADING_CLIENT_ID": "9",
        "IBKR_MARKET_DATA_TYPE": "1",
        "IBKR_TIMEOUT": "20",
    }
    for key, value in values.items():
        monkeypatch.setitem(context.config, key, value)

    settings = TradingSettings.from_config()

    assert settings == TradingSettings(
        enabled=True,
        dry_run=True,
        transmit=True,
        allow_live=True,
        strategies=frozenset({LONG, SHORT}),
        contracts=2,
        max_premium=2500.0,
        max_notional=50000.5,
        min_days_to_expiry=14,
        limit_buffer_pct=1.5,
        max_orders_per_day=5,
        take_profit_pct=40.0,
        stop_loss_pct=30.0,
        exit_minutes_before_close=20,
        hold_overnight=True,
        max_open_positions=4,
        max_daily_loss=750.0,
        risk_per_trade=1200.0,
        max_spread_pct=8.5,
        journal_path="journal.csv",
        host="gateway",
        port=4002,
        client_id=9,
        market_data_type=1,
        timeout=20,
    )
    assert "DRY RUN" in settings.describe() and "live accounts allowed" in settings.describe()


@pytest.mark.parametrize(
    ("key", "value"),
    [("STRADDLE_CONTRACTS", "0"), ("TRADING_MAX_ORDERS_PER_DAY", "0"), ("TRADING_MAX_OPEN_POSITIONS", "0")],
)
def test_settings_reject_zero_contracts_orders_or_positions(monkeypatch, key, value):
    monkeypatch.setitem(context.config, key, value)

    with pytest.raises(ValueError, match=key):
        TradingSettings.from_config()


# --- Announcements and the daily summary -----------------------------------------------------------------------------


class RecordingAnnouncer:
    def __init__(self):
        self.messages = []

    def announce(self, subject, text):
        self.messages.append((subject, text))


class FailingAnnouncer:
    def announce(self, subject, text):
        raise RuntimeError("webhook down")


def announcing_trader(announcer, **overrides):
    settings = TradingSettings(**{"enabled": True, "max_spread_pct": 0.0, **overrides})
    return IBKRStraddleTrader(
        settings, ib_factory=FakeIB, today=lambda: TODAY, announcers=[FailingAnnouncer(), announcer]
    )


def test_trade_events_are_announced_in_order_and_a_failing_announcer_is_isolated(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer)

    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    trader.manage_positions(NOW, SESSION)  # entry filled
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)  # take-profit exit placed
    ib.trade_objects[1].fill(9.1)
    trader.manage_positions(NOW, SESSION)  # exit filled

    subjects = [subject for subject, _ in announcer.messages]
    assert subjects == ["Straddle order placed", "Straddle entry filled", "Closing straddle", "Straddle closed"]
    assert "Order id 1: staged in TWS" in announcer.messages[0][1]
    assert "Filled at 6.00" in announcer.messages[1][1]
    assert "(take profit" in announcer.messages[2][1]
    assert "+310 USD" in announcer.messages[3][1]


def test_dry_run_announcements_are_labelled(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer, dry_run=True)

    trader.place_straddle("AAPL", LONG)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)

    assert [subject for subject, _ in announcer.messages] == ["DRY RUN - Straddle order", "DRY RUN - Straddle closed"]
    assert announcer.messages[0][1].startswith("Would place BUY 1 x AAPL")


def test_daily_loss_limit_is_announced(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer, max_daily_loss=100)
    trader.place_straddle("AAPL", LONG)
    fake_ib.instances[0].trade_objects[0].fill(6.0)
    quote_atm(2.4, 2.5, 2.4, 2.5)

    trader.manage_positions(NOW, SESSION)

    subjects = [subject for subject, _ in announcer.messages]
    assert subjects == ["Straddle order placed", "Straddle entry filled", "Closing straddle", "Daily loss limit hit"]
    assert "limit 100 USD" in announcer.messages[-1][1]


def test_end_of_day_summary_is_announced_once_near_the_close(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    ib.trade_objects[1].fill(9.1)
    trader.manage_positions(NOW, SESSION)
    trader.place_straddle("MSFT", LONG)  # a second straddle whose entry never fills

    summary_before_close = trader.day_summary(TODAY)
    assert "Realized P&L: +310 USD over 1 closed straddle(s)" in summary_before_close
    assert "  AAPL: +310 USD" in summary_before_close
    assert "  MSFT: long straddle, entry not filled" in summary_before_close

    trader.manage_positions(NEAR_CLOSE, SESSION)
    trader.manage_positions(NEAR_CLOSE, SESSION)  # not repeated

    summaries = [text for subject, text in announcer.messages if subject == "End of day summary"]
    assert len(summaries) == 1
    assert summaries[0].startswith("Realized P&L: +310 USD over 1 closed straddle(s)")
    assert ib.cancelled == [3]  # the unfilled MSFT entry was cancelled at the close


# --- Reconnect reconciliation ----------------------------------------------------------------------------------------


def disconnect(ib):
    ib.connected = False


def leg_fill(con_id, right, price, ref, side="SLD", order_id=99):
    return execution(con_id, right, order_id, price, side=side, ref=ref)


def held_legs(*con_ids):
    return [SimpleNamespace(contract=SimpleNamespace(conId=con_id), position=1) for con_id in con_ids]


def test_exit_filled_while_disconnected_is_realized_after_the_reconnect(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)  # take-profit exit placed
    disconnect(ib)
    fake_ib.held = []  # the legs are gone: the exit filled meanwhile
    fake_ib.executions = [leg_fill(1001, "C", 5.0, EXIT_REF), leg_fill(1002, "P", 4.1, EXIT_REF)]

    trader.manage_positions(NOW, SESSION)

    assert len(fake_ib.instances) == 2  # reconnected
    assert trader.positions == {}
    assert trader.realized_today(TODAY) == pytest.approx((9.1 - 6.0) * 100)
    closed = [text for subject, text in announcer.messages if subject == "Straddle closed"]
    assert len(closed) == 1 and "fill detected after reconnect" in closed[0]


def test_exit_order_lost_in_the_reconnect_is_placed_again(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    disconnect(ib)
    fake_ib.held = held_legs(1001, 1002)  # still open, but the new connection knows no such order

    trader.manage_positions(NOW, SESSION)

    [(_, exit_order)] = fake_ib.instances[1].orders
    assert exit_order.action == "SELL" and exit_order.orderRef == EXIT_REF
    assert trader.positions["AAPL"].exit_order_id == exit_order.orderId


def test_entry_filled_while_disconnected_is_picked_up(fake_ib):
    trader, ib = open_long()  # entry pending
    disconnect(ib)
    fake_ib.held = held_legs(1001, 1002)
    fake_ib.executions = [
        leg_fill(1001, "C", 3.05, ORDER_REF, side="BOT", order_id=1),
        leg_fill(1002, "P", 2.95, ORDER_REF, side="BOT", order_id=1),
    ]

    trader.manage_positions(NOW, SESSION)

    [position] = trader.positions.values()
    assert position.filled and position.entry_price == pytest.approx(6.0)


def test_position_closed_by_hand_while_disconnected_is_dropped(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)
    trader.manage_positions(NOW, SESSION)
    disconnect(ib)
    fake_ib.held = []  # closed manually in TWS: no MarketWatcher exit fill

    trader.manage_positions(NOW, SESSION)

    assert trader.positions == {}
    assert trader.realized_today(TODAY) == 0


def test_entry_cancelled_while_disconnected_is_forgotten(fake_ib):
    trader, ib = open_long()
    disconnect(ib)
    fake_ib.held = []

    trader.manage_positions(NOW, SESSION)

    assert trader.positions == {}


def test_untouched_positions_survive_a_reconnect(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)
    trader.manage_positions(NOW, SESSION)
    disconnect(ib)
    fake_ib.held = held_legs(1001, 1002)

    trader.manage_positions(NOW, SESSION)

    position = trader.positions["AAPL"]
    assert position.filled and position.exit_order_id is None and position.entry_price == 6.0


def test_dry_run_positions_survive_a_reconnect(fake_ib):
    trader = make_trader(dry_run=True)
    trader.place_straddle("AAPL", LONG)
    disconnect(fake_ib.instances[0])
    fake_ib.held = []

    trader.manage_positions(NOW, SESSION)

    assert "AAPL" in trader.positions


def test_a_pending_entry_is_not_marked_filled_by_fills_of_another_order(fake_ib):
    trader, ib = open_long()  # entry pending as order 1
    disconnect(ib)
    fake_ib.live_trades = list(ib.trade_objects)  # still working at IB
    fake_ib.held = held_legs(1001, 1002)  # the same legs, held because of a different order
    fake_ib.executions = todays_entry(order_id=7)

    trader.manage_positions(NOW, SESSION)

    position = trader.positions["AAPL"]
    assert not position.filled and position.entry_order_id == 1


# --- Restarts: what an earlier run left at IB ------------------------------------------------------------------------


def todays_entry(call=3.05, put=2.95, order_id=7, shares=1, side="BOT"):
    return [
        execution(1001, "C", order_id, call, side=side, shares=shares),
        execution(1002, "P", order_id, put, side=side, shares=shares),
    ]


def todays_exit(call, put, order_id=8, shares=1, side="SLD", ref=EXIT_REF):
    return [
        execution(1001, "C", order_id, call, side=side, shares=shares, ref=ref),
        execution(1002, "P", order_id, put, side=side, shares=shares, ref=ref),
    ]


def combo_fill(order_id, price, ref=ORDER_REF, side="BOT"):
    """IB also reports a combo as a whole, alongside the fills of its legs."""
    contract = Bag(symbol="AAPL", currency="USD", exchange="SMART")
    contract.conId = 28812380
    return SimpleNamespace(
        contract=contract,
        execution=SimpleNamespace(orderId=order_id, orderRef=ref, side=side, shares=1, price=price),
    )


def live_order(ref, action, limit, order_id=7, quantity=1):
    """A working combo order, the way a new connection sees it."""
    contract = Bag(
        symbol="AAPL",
        currency="USD",
        exchange="SMART",
        comboLegs=[
            ComboLeg(conId=1001, ratio=1, action="BUY", exchange="SMART"),
            ComboLeg(conId=1002, ratio=1, action="BUY", exchange="SMART"),
        ],
    )
    order = LimitOrder(action, quantity, limit, orderRef=ref)
    return FakeTrade(order, order_id, status="Submitted", contract=contract)


def test_restart_reconciles_the_book_before_the_first_order(fake_ib):
    fake_ib.executions = todays_entry()
    fake_ib.held = held_legs(1001, 1002)
    trader = make_trader()

    trader.notify({LONG: {"AAPL": 0.03}})  # the engine notifies before it runs the cycle hooks

    [ib] = fake_ib.instances
    assert ib.orders == []  # IB already holds this straddle
    position = trader.positions["AAPL"]
    assert position.filled and position.entry_order_id is None and position.entry_price == pytest.approx(6.0)


def test_restart_counts_what_ib_holds_against_the_open_position_limit(fake_ib):
    fake_ib.executions = todays_entry()
    fake_ib.held = held_legs(1001, 1002)
    trader = make_trader(max_open_positions=1)

    assert trader.place_straddle("MSFT", LONG) is None
    assert fake_ib.instances[0].orders == []


def test_restart_recovers_a_working_entry_and_manages_its_fill(fake_ib):
    trade = live_order(ORDER_REF, "BUY", 6.12)
    fake_ib.live_trades = [trade]
    trader = make_trader()

    assert trader.place_straddle("AAPL", LONG) is None
    assert fake_ib.instances[0].orders == []
    position = trader.positions["AAPL"]
    assert not position.filled and position.entry_order_id == 7
    assert (position.spec.action, position.spec.strike, position.spec.expiry) == ("BUY", 100.0, "20261120")
    assert (position.call.right, position.put.right) == ("C", "P")

    trade.fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)

    assert position.filled and position.entry_price == 6.0
    assert position.exit_reason.startswith("take profit")  # managed like any other straddle


def test_restart_adopts_a_working_exit_instead_of_placing_a_second_one(fake_ib):
    fake_ib.executions = todays_entry()
    fake_ib.held = held_legs(1001, 1002)
    fake_ib.live_trades = [live_order(EXIT_REF, "SELL", 9.0, order_id=8)]
    trader = make_trader()
    quote_atm(1.4, 1.5, 1.4, 1.5)  # past the stop loss

    trader.manage_positions(NOW, SESSION)

    [ib] = fake_ib.instances
    assert ib.orders == []  # a second exit would leave the straddle short once both filled
    assert trader.positions["AAPL"].exit_order_id == 8
    assert ib.modifications == [(8, 2.8)]  # the working exit follows the market instead
    assert fake_ib.live_trades[0].order.transmit is False  # and a staged order stays staged


def test_restart_with_a_partly_filled_working_exit_counts_those_contracts_once(fake_ib):
    fake_ib.executions = todays_entry(shares=3) + todays_exit(3.1, 3.0, shares=1)
    fake_ib.held = held_legs(1001, 1002)
    exit_trade = live_order(EXIT_REF, "SELL", 9.0, order_id=8, quantity=3)
    exit_trade.partial_fill(1, 6.1)
    fake_ib.live_trades = [exit_trade]
    trader = make_trader()

    trader.manage_positions(NOW, SESSION)

    position = trader.positions["AAPL"]
    assert position.spec.quantity == 2 and position.exit_order_id == 8
    assert trader.realized_today(TODAY) == pytest.approx(10)

    exit_trade.cancel()
    trader.manage_positions(NOW, SESSION)

    assert position.spec.quantity == 2 and position.exit_order_id is None
    assert trader.realized_today(TODAY) == pytest.approx(10)

    trader.manage_positions(NEAR_CLOSE, SESSION)

    [(_, new_exit)] = fake_ib.instances[0].orders
    assert new_exit.totalQuantity == 2


def test_a_reconnect_with_a_partly_filled_working_exit_counts_those_contracts_once(fake_ib):
    trader, ib = open_long(contracts=3, max_premium=100_000)
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    exit_trade = ib.trade_objects[1]
    exit_trade.partial_fill(1, 9.1)
    disconnect(ib)
    fake_ib.live_trades = [exit_trade]
    fake_ib.held = held_legs(1001, 1002)
    fake_ib.executions = todays_entry(order_id=1, shares=3) + todays_exit(5.0, 4.1, order_id=2, shares=1)

    trader.manage_positions(NOW, SESSION)

    assert trader.realized_today(TODAY) == 0  # still one working order: it is booked when it finishes
    assert trader.positions["AAPL"].spec.quantity == 3

    exit_trade.fill(9.1)
    trader.manage_positions(NOW, SESSION)

    assert trader.realized_today(TODAY) == pytest.approx((9.1 - 6.0) * 100 * 3)


def test_restart_keeps_tickers_already_traded_today_off_limits(fake_ib):
    fake_ib.executions = todays_entry() + todays_exit(3.1, 3.0)  # opened and closed by an earlier run
    trader = make_trader()

    assert trader.place_straddle("AAPL", LONG) is None
    assert fake_ib.instances[0].orders == []


def test_restart_restores_the_daily_order_cap(fake_ib):
    fake_ib.executions = todays_entry() + todays_exit(3.1, 3.0)
    trader = make_trader(max_orders_per_day=1)

    assert trader.place_straddle("MSFT", LONG) is None
    assert fake_ib.instances[0].orders == []


@pytest.mark.parametrize(
    ("entry_side", "exit_side", "expected"),
    [("BOT", "SLD", (6.1 - 6.0) * 100), ("SLD", "BOT", (6.0 - 6.1) * 100)],
)
def test_restart_restores_the_realized_pnl_of_both_directions(fake_ib, entry_side, exit_side, expected):
    fake_ib.executions = todays_entry(side=entry_side) + todays_exit(3.1, 3.0, side=exit_side)
    trader = make_trader()

    trader.manage_positions(NOW, SESSION)

    assert trader.realized_today(TODAY) == pytest.approx(expected)


def test_restart_restores_the_halt_after_a_realized_loss(fake_ib):
    fake_ib.executions = todays_entry() + todays_exit(1.0, 1.0)  # -400 USD
    trader = make_trader(max_daily_loss=100)

    assert trader.place_straddle("MSFT", LONG) is None
    assert fake_ib.instances[0].orders == []


def test_restart_restores_a_halt_that_realized_less_than_the_limit(fake_ib):
    # The limit was hit on unrealized losses, then the exits filled at better prices: only the tag remembers it.
    fake_ib.executions = todays_entry() + todays_exit(2.9, 2.9, ref=HALT_EXIT_REF)  # -20 USD
    trader = make_trader(max_daily_loss=500)

    assert trader.place_straddle("MSFT", LONG) is None
    assert fake_ib.instances[0].orders == []


def test_a_restored_halt_closes_what_is_still_open(fake_ib):
    fake_ib.executions = todays_entry(shares=2) + todays_exit(2.9, 2.9, shares=1, ref=HALT_EXIT_REF)
    fake_ib.held = held_legs(1001, 1002)
    trader = make_trader(max_daily_loss=500)

    trader.manage_positions(NOW, SESSION)

    [ib] = fake_ib.instances
    [(_, exit_order)] = ib.orders
    assert (exit_order.action, exit_order.totalQuantity, exit_order.orderRef) == ("SELL", 1, HALT_EXIT_REF)
    assert trader.positions["AAPL"].exit_reason == "daily loss limit"


def test_exits_forced_by_the_daily_loss_limit_are_tagged(fake_ib):
    trader = make_trader(max_daily_loss=100)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    quote_atm(2.4, 2.5, 2.4, 2.5)

    trader.manage_positions(NOW, SESSION)

    assert ib.orders[1][1].orderRef == HALT_EXIT_REF


def test_a_reconnect_does_not_count_a_close_twice(fake_ib):
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    ib.trade_objects[1].fill(9.1)
    trader.manage_positions(NOW, SESSION)
    assert trader.realized_today(TODAY) == pytest.approx(310)
    disconnect(ib)
    fake_ib.executions = todays_entry(order_id=1) + todays_exit(5.0, 4.1, order_id=2)

    trader.manage_positions(NOW, SESSION)

    assert trader.realized_today(TODAY) == pytest.approx(310)


def test_recovery_ignores_the_combo_level_fill_ib_reports_with_the_legs(fake_ib):
    fake_ib.executions = [combo_fill(7, 6.0), *todays_entry()]
    fake_ib.held = held_legs(1001, 1002)
    trader = make_trader()

    trader.manage_positions(NOW, SESSION)

    position = trader.positions["AAPL"]
    assert position.entry_price == pytest.approx(6.0) and position.spec.quantity == 1


def test_realized_pnl_ignores_the_combo_level_fills(fake_ib):
    fake_ib.executions = [
        combo_fill(7, 6.0),
        *todays_entry(),
        combo_fill(8, 6.1, ref=EXIT_REF, side="SLD"),
        *todays_exit(3.1, 3.0),
    ]
    trader = make_trader()

    trader.manage_positions(NOW, SESSION)

    assert trader.realized_today(TODAY) == pytest.approx(10)
    assert trader.positions == {}


def test_recovered_legs_are_quoted_on_smart_not_on_the_venue_that_filled_them(fake_ib):
    fills = todays_entry()
    for fill in fills:
        fill.contract.exchange = "BOX"
    fake_ib.executions = fills
    fake_ib.held = held_legs(1001, 1002)
    trader = make_trader()

    trader.manage_positions(NOW, SESSION)

    position = trader.positions["AAPL"]
    assert (position.call.exchange, position.put.exchange) == ("SMART", "SMART")


def test_recovery_nets_off_the_contracts_already_closed(fake_ib):
    fake_ib.executions = todays_entry(shares=3) + todays_exit(3.1, 3.0, shares=1)
    fake_ib.held = held_legs(1001, 1002)
    trader = make_trader()

    trader.manage_positions(NOW, SESSION)

    assert trader.positions["AAPL"].spec.quantity == 2
    assert trader.realized_today(TODAY) == pytest.approx(10)


# --- Exits that do not fill ------------------------------------------------------------------------------------------


def stopped_out(**overrides):
    """A long straddle filled at 6.00 with a stop-loss exit resting at the 2.80 bid (order 2)."""
    trader, ib = open_long(**overrides)
    ib.trade_objects[0].fill(6.0)
    quote_atm(1.4, 1.5, 1.4, 1.5)
    trader.manage_positions(NOW, SESSION)
    assert trader.positions["AAPL"].exit_price == pytest.approx(2.8)
    return trader, ib


def test_unfilled_stop_loss_follows_the_market_down(fake_ib):
    trader, ib = stopped_out()
    quote_atm(0.5, 0.6, 0.5, 0.6)  # straddle bid 1.00

    trader.manage_positions(NOW, SESSION)

    assert len(ib.orders) == 2  # modified in place: there are never two live exits
    assert ib.modifications == [(2, pytest.approx(1.0))]
    position = trader.positions["AAPL"]
    assert position.exit_price == pytest.approx(1.0) and position.exit_reason.startswith("stop loss")


def test_stop_loss_keeps_following_the_market_after_the_value_bounces_inside_the_band(fake_ib):
    trader, ib = stopped_out()
    quote_atm(1.3, 1.9, 1.3, 1.9)  # mid 3.20 is back above the stop, but the bid 2.60 is below the resting limit

    trader.manage_positions(NOW, SESSION)

    assert ib.modifications == [(2, pytest.approx(2.6))]


def test_an_exit_that_finishes_while_quotes_are_fetched_is_not_modified(fake_ib):
    trader, ib = stopped_out()
    quote_atm(0.5, 0.6, 0.5, 0.6)
    request_quotes = ib.reqTickers

    def fill_during_the_request(*contracts):
        ib.trade_objects[1].fill(2.8)
        return request_quotes(*contracts)

    ib.reqTickers = fill_during_the_request

    trader.manage_positions(NOW, SESSION)

    assert ib.modifications == []  # ib_async refuses to modify a finished order

    ib.reqTickers = request_quotes
    trader.manage_positions(NOW, SESSION)

    assert trader.positions == {}
    assert trader.realized_today(TODAY) == pytest.approx((2.8 - 6.0) * 100)


def test_exit_is_not_repriced_when_the_market_moves_its_way(fake_ib):
    trader, ib = stopped_out()
    quote_atm(1.5, 1.6, 1.5, 1.6)  # bid 3.00, above the resting 2.80: it will fill as it stands

    trader.manage_positions(NOW, SESSION)

    assert ib.modifications == []
    assert trader.positions["AAPL"].exit_price == pytest.approx(2.8)


def test_short_straddle_exit_follows_the_ask_up(fake_ib):
    trader = make_trader(strategies=frozenset({SHORT}))
    trader.place_straddle("AAPL", SHORT)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(5.9)
    quote_atm(4.5, 4.7, 4.5, 4.7)  # mid 9.20 is past the stop at 8.85: buy back at the ask, 9.40
    trader.manage_positions(NOW, SESSION)
    assert trader.positions["AAPL"].exit_price == pytest.approx(9.4)
    quote_atm(5.5, 5.7, 5.5, 5.7)  # ask 11.40

    trader.manage_positions(NOW, SESSION)

    assert ib.modifications == [(2, pytest.approx(11.4))]


def test_a_position_with_a_resting_exit_still_counts_against_the_daily_loss_limit(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer, max_daily_loss=500)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    quote_atm(1.4, 1.5, 1.4, 1.5)
    trader.manage_positions(NOW, SESSION)  # stop-loss exit rests at 2.80; -310 USD is inside the limit
    quote_atm(0.2, 0.3, 0.2, 0.3)  # mid 0.50: -550 USD

    trader.manage_positions(NOW, SESSION)

    assert "Daily loss limit hit" in [subject for subject, _ in announcer.messages]
    assert trader.place_straddle("MSFT", LONG) is None


def test_a_halted_day_keeps_closing_what_could_not_be_closed_at_first(fake_ib):
    trader = make_trader(max_daily_loss=100)
    trader.place_straddle("AAPL", LONG)
    trader.place_straddle("MSFT", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    ib.trade_objects[1].fill(6.0)
    quote_atm(2.4, 2.5, 2.4, 2.5)
    trader.manage_positions(NOW, SESSION)
    ib.trade_objects[2].cancel()  # the first exit was cancelled by someone or something

    trader.manage_positions(NOW, SESSION)

    assert trader.positions["AAPL"].exit_reason == "daily loss limit"
    assert trader.positions["AAPL"].exit_order_id == 5  # placed again under the same halt


def take_profit_resting():
    """A long straddle filled at 6.00 with a take-profit exit resting at 9.02 (order 2)."""
    trader, ib = open_long()
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    assert trader.positions["AAPL"].exit_price == pytest.approx(9.02)
    return trader, ib


def test_take_profit_waits_for_its_price_when_no_rule_fires(fake_ib):
    trader, ib = take_profit_resting()
    quote_atm(3.0, 3.2, 3.0, 3.2)  # mid 6.20: no rule fires

    trader.manage_positions(NOW, SESSION)

    assert ib.modifications == []
    assert trader.positions["AAPL"].exit_price == pytest.approx(9.02)


def test_take_profit_crosses_the_spread_at_the_session_close(fake_ib):
    trader, ib = take_profit_resting()
    quote_atm(3.0, 3.2, 3.0, 3.2)  # straddle bid 6.00

    trader.manage_positions(NEAR_CLOSE, SESSION)

    assert len(ib.orders) == 2
    assert ib.modifications == [(2, pytest.approx(6.0))]
    assert trader.positions["AAPL"].exit_reason == "session close"


def test_take_profit_becomes_a_stop_loss_when_the_market_collapses(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    quote_atm(1.4, 1.5, 1.4, 1.5)

    trader.manage_positions(NOW, SESSION)

    assert ib.modifications == [(2, pytest.approx(2.8))]
    assert trader.positions["AAPL"].exit_reason.startswith("stop loss")
    assert "Closing straddle" in announcer.messages[-1][0] and "2.80" in announcer.messages[-1][1]


def test_straddle_left_over_from_an_earlier_session_is_closed_unless_held_overnight():
    leftover = position("BUY", 6.0)
    leftover.opened_on = TODAY - timedelta(days=1)

    assert exit_reason(leftover, 6.0, NOW, SESSION, TradingSettings()) == "left open from an earlier session"
    assert exit_reason(leftover, 6.0, NOW, SESSION, TradingSettings(hold_overnight=True)) is None


# --- Partial fills and rejections ------------------------------------------------------------------------------------


def test_partly_filled_entry_cancels_the_rest_and_manages_what_filled(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer, contracts=5, max_premium=100_000)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].partial_fill(2, 6.05)

    trader.manage_positions(NOW, SESSION)

    assert ib.cancelled == [1]
    position = trader.positions["AAPL"]
    assert position.filled and position.spec.quantity == 2 and position.entry_price == 6.05
    assert "2 of 5" in announcer.messages[-1][1]

    trader.manage_positions(NEAR_CLOSE, SESSION)

    [_, (_, exit_order)] = ib.orders
    assert exit_order.totalQuantity == 2  # exactly what is held


def test_entry_partly_filled_at_the_close_is_closed_rather_than_dropped(fake_ib):
    trader, ib = open_long(contracts=5, max_premium=100_000)
    ib.trade_objects[0].partial_fill(2, 6.05)

    trader.manage_positions(NEAR_CLOSE, SESSION)

    assert ib.cancelled == [1]
    [_, (_, exit_order)] = ib.orders
    assert (exit_order.action, exit_order.totalQuantity) == ("SELL", 2)


def test_fills_that_arrive_before_ib_confirms_the_cancel_are_kept(fake_ib):
    fake_ib.cancel_status = "PendingCancel"
    trader, ib = open_long(contracts=5, max_premium=100_000)
    trade = ib.trade_objects[0]
    trade.partial_fill(2, 6.05)

    trader.manage_positions(NOW, SESSION)
    assert ib.cancelled == [1]
    assert not trader.positions["AAPL"].filled  # the final quantity is not known yet

    trader.manage_positions(NOW, SESSION)
    assert ib.cancelled == [1]  # asked once

    trade.orderStatus.status = "Cancelled"
    trade.orderStatus.filled = 3

    trader.manage_positions(NOW, SESSION)
    assert trader.positions["AAPL"].filled and trader.positions["AAPL"].spec.quantity == 3


def test_unfilled_entry_stays_in_the_book_until_ib_confirms_the_cancel(fake_ib):
    fake_ib.cancel_status = "PendingCancel"
    trader, ib = open_long()

    trader.manage_positions(NEAR_CLOSE, SESSION)
    trader.manage_positions(NEAR_CLOSE, SESSION)

    assert ib.cancelled == [1]
    assert "AAPL" in trader.positions

    ib.trade_objects[0].cancel()
    trader.manage_positions(NEAR_CLOSE, SESSION)

    assert trader.positions == {}


def test_partly_filled_exit_is_booked_and_replaced_only_for_what_is_left(fake_ib):
    trader, ib = open_long(contracts=3, max_premium=100_000)
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    exit_trade = ib.trade_objects[1]
    exit_trade.partial_fill(1, 9.1)
    exit_trade.cancel()

    trader.manage_positions(NOW, SESSION)

    assert trader.realized_today(TODAY) == pytest.approx((9.1 - 6.0) * 100)
    assert trader.positions["AAPL"].spec.quantity == 2
    [_, _, (_, new_exit)] = ib.orders
    assert new_exit.totalQuantity == 2  # closing all 3 again would leave the straddle short


def test_exit_partly_filled_while_disconnected_is_replaced_only_for_what_is_left(fake_ib):
    trader, ib = open_long(contracts=3, max_premium=100_000)
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)
    disconnect(ib)
    fake_ib.held = held_legs(1001, 1002)
    fake_ib.executions = todays_entry(order_id=1, shares=3) + todays_exit(5.0, 4.1, order_id=2, shares=1)

    trader.manage_positions(NOW, SESSION)

    assert trader.realized_today(TODAY) == pytest.approx((9.1 - 6.0) * 100)
    [(_, new_exit)] = fake_ib.instances[1].orders
    assert new_exit.totalQuantity == 2


def test_rejected_entry_frees_its_slot_and_is_announced(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer, max_open_positions=1)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].reject()

    trader.manage_positions(NOW, SESSION)

    assert trader.positions == {}
    assert announcer.messages[-1][0] == "Straddle entry rejected"
    assert trader.place_straddle("MSFT", LONG) is not None  # the open slot is free
    assert trader.place_straddle("AAPL", LONG) is None  # but the ticker is not retried today


def test_rejected_exit_is_placed_again_and_announced_once(fake_ib):
    announcer = RecordingAnnouncer()
    trader = announcing_trader(announcer)
    trader.place_straddle("AAPL", LONG)
    [ib] = fake_ib.instances
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)

    for attempt in (1, 2):
        ib.trade_objects[attempt].reject()
        trader.manage_positions(NOW, SESSION)

    assert len(ib.orders) == 4  # the entry and three exit attempts
    subjects = [subject for subject, _ in announcer.messages]
    assert subjects.count("Straddle exit rejected") == 1


# --- Against ib_async's own order bookkeeping -------------------------------------------------------------------------


class WiredIB(IB):
    """A real ib_async client with only the socket cut: placing, modifying and cancelling orders and applying status
    updates all run through ib_async's own code, so these tests also check what the FakeIB tests assume about it."""

    connect = FakeIB.connect
    isConnected = FakeIB.isConnected  # noqa: N815
    disconnect = FakeIB.disconnect
    managedAccounts = FakeIB.managedAccounts  # noqa: N815
    reqMarketDataType = FakeIB.reqMarketDataType  # noqa: N815
    qualifyContracts = FakeIB.qualifyContracts  # noqa: N815
    reqTickers = FakeIB.reqTickers  # noqa: N815
    reqSecDefOptParams = FakeIB.reqSecDefOptParams  # noqa: N815
    positions = FakeIB.positions
    reqExecutions = FakeIB.reqExecutions  # noqa: N815

    instances = []

    def __init__(self):
        super().__init__()
        self.connected = False
        self.calls = []
        self.sent = []
        self.wrapper.clientId = 2
        order_ids = iter(range(1, 100))
        self.client.getReqId = lambda: next(order_ids)
        self.client.placeOrder = lambda order_id, contract, order: self.sent.append(
            ("place", order_id, order.action, order.totalQuantity, order.lmtPrice)
        )
        self.client.cancelOrder = lambda order_id, manual_time="": self.sent.append(("cancel", order_id))
        WiredIB.instances.append(self)

    def report(self, order_id, status, filled=0.0, price=0.0):
        """What TWS sends back about an order."""
        self.wrapper.orderStatus(order_id, status, filled, 0.0, price, order_id + 1000, 0, price, 2, "")


def wired_trader(**overrides):
    WiredIB.instances = []
    settings = TradingSettings(**{"enabled": True, "transmit": True, "max_spread_pct": 0.0, **overrides})
    return IBKRStraddleTrader(settings, ib_factory=WiredIB, today=lambda: TODAY)


def test_ib_async_moving_an_exit_modifies_the_order_instead_of_adding_one(fake_ib):
    trader = wired_trader()
    trader.place_straddle("AAPL", LONG)
    [ib] = WiredIB.instances
    ib.report(1, "Filled", filled=1.0, price=6.0)
    quote_atm(1.4, 1.5, 1.4, 1.5)
    trader.manage_positions(NOW, SESSION)  # stop-loss exit at 2.80
    ib.report(2, "Submitted")
    quote_atm(0.5, 0.6, 0.5, 0.6)

    trader.manage_positions(NOW, SESSION)

    assert ib.sent[-2:] == [("place", 2, "SELL", 1, pytest.approx(2.8)), ("place", 2, "SELL", 1, pytest.approx(1.0))]
    assert len(ib.trades()) == 2  # the entry and one exit
    exit_trade = next(trade for trade in ib.trades() if trade.order.orderId == 2)
    assert exit_trade.order.lmtPrice == pytest.approx(1.0)
    assert exit_trade.log[-1].message == "Modify"

    ib.report(2, "Filled", filled=1.0, price=1.0)
    trader.manage_positions(NOW, SESSION)

    assert trader.positions == {}
    assert trader.realized_today(TODAY) == pytest.approx((1.0 - 6.0) * 100)


def test_ib_async_part_filled_entry_waits_for_the_cancel_to_be_confirmed(fake_ib):
    trader = wired_trader(contracts=5, max_premium=100_000)
    trader.place_straddle("AAPL", LONG)
    [ib] = WiredIB.instances
    ib.report(1, "Submitted", filled=2.0, price=6.05)

    trader.manage_positions(NOW, SESSION)

    assert ("cancel", 1) in ib.sent
    assert ib.trades()[0].orderStatus.status == "PendingCancel"
    assert not trader.positions["AAPL"].filled

    trader.manage_positions(NOW, SESSION)
    assert ib.sent.count(("cancel", 1)) == 1

    ib.report(1, "Cancelled", filled=3.0, price=6.05)
    trader.manage_positions(NOW, SESSION)

    position = trader.positions["AAPL"]
    assert position.filled and position.spec.quantity == 3 and position.entry_price == 6.05


def test_ib_async_a_staged_entry_is_cancelled_at_once(fake_ib):
    trader = wired_trader(transmit=False)
    trader.place_straddle("AAPL", LONG)

    trader.manage_positions(NEAR_CLOSE, SESSION)

    assert trader.positions == {}  # ib_async cancels an order that was never transmitted without asking TWS


def test_ib_async_rejected_entry_is_forgotten_and_a_rejected_exit_retried(fake_ib):
    trader = wired_trader()
    trader.place_straddle("AAPL", LONG)
    trader.place_straddle("MSFT", LONG)
    [ib] = WiredIB.instances
    ib.report(1, "Inactive")
    ib.report(2, "Filled", filled=1.0, price=6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)

    trader.manage_positions(NOW, SESSION)

    assert list(trader.positions) == ["MSFT"]
    assert trader.positions["MSFT"].exit_order_id == 3

    ib.report(3, "Inactive")
    trader.manage_positions(NOW, SESSION)

    assert trader.positions["MSFT"].exit_order_id == 4  # a new order: ib_async refuses to modify a finished one


# --- The spread guard -------------------------------------------------------------------------------------------------


def test_straddle_wider_than_the_spread_limit_is_refused_without_using_a_slot(fake_ib):
    trader = make_trader(
        max_spread_pct=6.0, max_orders_per_day=1
    )  # the default quotes: bid 5.80, ask 6.20, 6.67% of mid

    trader.notify({LONG: {"AAPL": 0.05}})

    assert fake_ib.instances[0].orders == []
    assert trader.positions == {}

    quote_atm(3.05, 3.15, 2.85, 2.95)  # now 3.3% of mid: the slot was not used up
    trader.notify({LONG: {"AAPL": 0.05}})

    assert len(fake_ib.instances[0].orders) == 1


def test_spread_limit_applies_to_short_straddles_too(fake_ib):
    trader = make_trader(strategies=frozenset({SHORT}), max_spread_pct=6.0)

    with pytest.raises(ValueError, match="exceeds STRADDLE_MAX_SPREAD_PCT 6"):
        trader.place_straddle("AAPL", SHORT)


def test_an_unmeasurable_spread_is_refused_unless_the_limit_is_off(fake_ib):
    FakeIB.quotes[("C", 100.0)] = Snapshot(last=3.1)  # no bid or ask on the call

    with pytest.raises(ValueError, match="no two-sided quote"):
        make_trader(max_spread_pct=6.0).place_straddle("AAPL", LONG)

    assert make_trader(max_spread_pct=0.0).place_straddle("AAPL", LONG) is not None


def test_a_refusal_is_one_warning_not_a_traceback(fake_ib, caplog):
    trader = make_trader(max_spread_pct=6.0)

    with caplog.at_level("WARNING", logger="market_watcher.trading"):
        trader.notify({LONG: {"AAPL": 0.05}})

    [record] = caplog.records
    assert record.levelname == "WARNING" and "spread 6.7% of mid" in record.getMessage()
    assert record.exc_info is None


# --- The trade journal ------------------------------------------------------------------------------------------------


def read_journal(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_journal_records_a_trade_from_order_to_close_with_its_quotes(fake_ib, tmp_path):
    path = tmp_path / "journal.csv"
    trader, ib = open_long(journal_path=str(path))
    ib.trade_objects[0].fill(6.05)
    trader.manage_positions(NOW, SESSION)
    quote_atm(1.4, 1.5, 1.4, 1.5)
    trader.manage_positions(NOW, SESSION)  # stop-loss exit
    quote_atm(0.5, 0.6, 0.5, 0.6)
    trader.manage_positions(NOW, SESSION)  # the exit follows the market
    ib.trade_objects[1].fill(1.0)
    trader.manage_positions(NOW, SESSION)

    rows = read_journal(path)

    assert [row["event"] for row in rows] == ["order placed", "entry filled", "exit placed", "exit moved", "closed"]
    placed, filled, exited, moved, closed = rows
    assert (placed["ticker"], placed["action"], placed["quantity"], placed["expiry"]) == (
        "AAPL",
        "BUY",
        "1",
        "20261120",
    )
    assert (float(placed["bid"]), float(placed["ask"]), float(placed["mid"])) == pytest.approx((5.8, 6.2, 6.0))
    assert float(placed["limit"]) == pytest.approx(6.12) and placed["order_id"] == "1"
    assert float(filled["fill"]) == pytest.approx(6.05)  # fill against the mid is what the backtest could not measure
    assert exited["reason"].startswith("stop loss") and float(exited["limit"]) == pytest.approx(2.8)
    assert float(moved["limit"]) == pytest.approx(1.0) and float(moved["bid"]) == pytest.approx(1.0)
    assert float(closed["fill"]) == pytest.approx(1.0)
    assert float(closed["pnl_usd"]) == pytest.approx((1.0 - 6.05) * 100)
    assert placed["dry_run"] == "False"
    assert placed["time"].endswith("+00:00")


def test_journal_records_rejections_cancellations_and_partial_closes(fake_ib, tmp_path):
    path = tmp_path / "journal.csv"
    rejected, ib = open_long(journal_path=str(path))
    ib.trade_objects[0].reject()
    rejected.manage_positions(NOW, SESSION)
    cancelled, ib = open_long(journal_path=str(path))
    ib.trade_objects[0].cancel()
    cancelled.manage_positions(NOW, SESSION)
    partial, ib = open_long(journal_path=str(path), contracts=3, max_premium=100_000)
    ib.trade_objects[0].fill(6.0)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    partial.manage_positions(NOW, SESSION)
    ib.trade_objects[1].partial_fill(1, 9.1)
    ib.trade_objects[1].cancel()
    partial.manage_positions(NOW, SESSION)

    rows = read_journal(path)
    events = [row["event"] for row in rows]

    assert "entry rejected" in events and "entry cancelled" in events
    assert "partly closed" in events and "exit cancelled" in events
    closed = next(row for row in rows if row["event"] == "partly closed")
    assert closed["quantity"] == "1" and float(closed["pnl_usd"]) == pytest.approx((9.1 - 6.0) * 100)


def test_journal_labels_dry_runs(fake_ib, tmp_path):
    path = tmp_path / "journal.csv"
    trader = make_trader(dry_run=True, journal_path=str(path))
    trader.place_straddle("AAPL", LONG)
    quote_atm(5.0, 5.2, 4.0, 4.2)
    trader.manage_positions(NOW, SESSION)

    rows = read_journal(path)

    assert [row["event"] for row in rows] == ["order placed", "closed"]
    assert {row["dry_run"] for row in rows} == {"True"}
    assert rows[0]["order_id"] == ""


def test_a_journal_that_cannot_be_written_never_affects_trading(fake_ib, tmp_path, caplog):
    trader = make_trader(journal_path=str(tmp_path))  # a directory, so every write fails

    assert trader.place_straddle("AAPL", LONG) is not None

    assert len(fake_ib.instances[0].orders) == 1
    assert "Could not write to the trade journal" in caplog.text


def test_no_journal_is_written_unless_one_is_configured(fake_ib, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    make_trader().place_straddle("AAPL", LONG)

    assert list(tmp_path.iterdir()) == []
