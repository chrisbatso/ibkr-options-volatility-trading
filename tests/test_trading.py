import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from ib_async import Option, OptionChain

from market_watcher.config import context
from market_watcher.market_hours import Session
from market_watcher.trading import (
    EXIT_REF,
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
    def __init__(self, order, order_id, status="PendingSubmit"):
        order.orderId = order_id
        self.order = order
        self.orderStatus = SimpleNamespace(status=status, avgFillPrice=0.0)

    def fill(self, price):
        self.orderStatus.status = "Filled"
        self.orderStatus.avgFillPrice = price

    def cancel(self):
        self.orderStatus.status = "Cancelled"


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

    def __init__(self):
        self.connected = False
        self.calls = []
        self.orders = []
        self.trade_objects = []
        self.cancelled = []
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
        self.orders.append((contract, order))
        trade = FakeTrade(order, len(self.orders))
        self.trade_objects.append(trade)
        return trade

    def trades(self):
        return list(self.trade_objects)

    def cancelOrder(self, order):  # noqa: N802
        self.cancelled.append(order.orderId)
        for trade in self.trade_objects:
            if trade.order.orderId == order.orderId:
                trade.cancel()

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
    return FakeIB


def make_trader(**overrides):
    settings = TradingSettings(**{"enabled": True, **overrides})
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
    trader = IBKRStraddleTrader(TradingSettings(enabled=True), ib_factory=FakeIB, today=lambda: next(days))

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
    description = settings.describe()
    assert "staged in TWS" in description and "paper accounts only" in description
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
    settings = TradingSettings(**{"enabled": True, **overrides})
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
