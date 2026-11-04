import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from ib_async import OptionChain

from market_watcher.config import context
from market_watcher.trading import (
    LONG,
    SHORT,
    IBKRStraddleTrader,
    StraddleSpec,
    TradingSettings,
    check_limits,
    choose_expiry,
    limit_price,
    option_price,
    parse_strategies,
    select_chain,
    strikes_by_distance,
)

TODAY = date(2026, 11, 4)


@dataclass
class Snapshot:
    last: float = math.nan
    bid: float = math.nan
    ask: float = math.nan
    close: float = math.nan


class FakeTrade:
    def __init__(self, order, order_id):
        order.orderId = order_id
        self.order = order
        self.orderStatus = SimpleNamespace(status="PreSubmitted")


class FakeIB:
    """TWS stand-in: a few stocks at 101.30, one option chain with strikes 95/100/105, quotes per leg."""

    instances = []
    accounts = ["DU1234567"]
    stocks = {"AAPL": 1, "MSFT": 2, "KO": 3}
    price = 101.3
    available_strikes = {95.0, 100.0, 105.0}
    quotes = {}
    chains = []

    def __init__(self):
        self.connected = False
        self.calls = []
        self.orders = []
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
        return FakeTrade(order, len(self.orders))


DEFAULT_QUOTES = {
    ("C", 100.0): Snapshot(bid=3.0, ask=3.2),
    ("P", 100.0): Snapshot(bid=2.8, ask=3.0),
    ("C", 105.0): Snapshot(bid=1.0, ask=1.2),
    ("P", 105.0): Snapshot(bid=5.0, ask=5.2),
}
DEFAULT_CHAINS = [OptionChain("SMART", 1, "AAPL", "100", ["20261106", "20261120", "20261218"], [95.0, 100.0, 105.0])]


@pytest.fixture
def fake_ib():
    FakeIB.instances = []
    FakeIB.accounts = ["DU1234567"]
    FakeIB.stocks = {"AAPL": 1, "MSFT": 2, "KO": 3}
    FakeIB.price = 101.3
    FakeIB.available_strikes = {95.0, 100.0, 105.0}
    FakeIB.quotes = dict(DEFAULT_QUOTES)
    FakeIB.chains = list(DEFAULT_CHAINS)
    return FakeIB


def make_trader(**overrides):
    settings = TradingSettings(**{"enabled": True, **overrides})
    return IBKRStraddleTrader(settings, ib_factory=FakeIB, today=lambda: TODAY)


# --- Orders ----------------------------------------------------------------------------------------------------------


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
    assert (order.tif, order.transmit, order.orderRef) == ("DAY", False, "MarketWatcher")


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
    trader = make_trader(max_orders_per_day=2)

    assert trader.place_straddle("AAPL", LONG) is not None
    assert trader.place_straddle("AAPL", LONG) is None  # same ticker again today
    assert trader.place_straddle("MSFT", LONG) is not None
    assert trader.place_straddle("KO", LONG) is None  # daily cap reached

    assert len(fake_ib.instances[0].orders) == 2


def test_the_daily_memory_resets_on_a_new_day(fake_ib):
    days = iter([TODAY, TODAY, TODAY + timedelta(days=1)])
    trader = IBKRStraddleTrader(TradingSettings(enabled=True), ib_factory=FakeIB, today=lambda: next(days))

    assert trader.place_straddle("AAPL", LONG) is not None
    assert trader.place_straddle("AAPL", LONG) is None
    assert trader.place_straddle("AAPL", LONG) is not None


def test_dry_run_builds_the_order_but_sends_nothing(fake_ib):
    spec = make_trader(dry_run=True).place_straddle("AAPL", LONG)

    assert spec is not None and spec.limit_price == pytest.approx(6.12)
    assert fake_ib.instances[0].orders == []


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


def test_limit_price_applies_the_buffer_in_the_right_direction():
    assert limit_price(6.0, "BUY", 2.0) == 6.12
    assert limit_price(6.0, "SELL", 2.0) == 5.88


def spec(action, limit, strike=100.0, quantity=1):
    return StraddleSpec("AAPL", LONG, action, "20261120", strike, 101.3, 3.1, 2.9, limit, quantity, 100, None)


def test_check_limits():
    settings = TradingSettings(max_premium=1_000.0, max_notional=20_000.0)

    check_limits(spec("BUY", 9.99), settings)  # 999 USD
    check_limits(spec("SELL", 6.0, strike=200.0), settings)  # 20,000 USD notional
    with pytest.raises(ValueError, match="premium"):
        check_limits(spec("BUY", 10.01), settings)
    with pytest.raises(ValueError, match="notional"):
        check_limits(spec("SELL", 6.0, strike=200.5), settings)


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
    assert "staged in TWS" in settings.describe() and "paper accounts only" in settings.describe()


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
        host="gateway",
        port=4002,
        client_id=9,
        market_data_type=1,
        timeout=20,
    )
    assert "DRY RUN" in settings.describe() and "live accounts allowed" in settings.describe()


@pytest.mark.parametrize(("key", "value"), [("STRADDLE_CONTRACTS", "0"), ("TRADING_MAX_ORDERS_PER_DAY", "0")])
def test_settings_reject_zero_contracts_or_orders(monkeypatch, key, value):
    monkeypatch.setitem(context.config, key, value)

    with pytest.raises(ValueError, match=key):
        TradingSettings.from_config()
