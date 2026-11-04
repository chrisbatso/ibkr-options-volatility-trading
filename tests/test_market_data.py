import math
import sys
from dataclasses import dataclass

import pytest

from market_watcher import market_data
from market_watcher.config import context
from market_watcher.market_data import IBKRProvider, MarketDataProvider, YahooFinanceProvider, create_provider

# --- Yahoo Finance ---------------------------------------------------------------------------------------------------


class FakeYahooClient:
    created_with = []
    response = {}

    def __init__(self, tickers):
        FakeYahooClient.created_with.append(tickers)

    def get_current_percent_change(self):
        return FakeYahooClient.response


@pytest.fixture
def yahoo():
    FakeYahooClient.created_with = []
    FakeYahooClient.response = {}
    return FakeYahooClient


def test_yahoo_provider_returns_a_fraction_or_none_for_every_requested_ticker(yahoo):
    yahoo.response = {"AAPL": 0.0123, "KO": -0.002, "TPCO": None, "ZZZZ": "N/A"}
    provider = YahooFinanceProvider(client_factory=yahoo)

    result = provider.get_daily_pnls(["AAPL", "KO", "TPCO", "ZZZZ", "MISSING"])

    assert yahoo.created_with == [["AAPL", "KO", "TPCO", "ZZZZ", "MISSING"]]
    assert result == {"AAPL": 0.0123, "KO": -0.002, "TPCO": None, "ZZZZ": None, "MISSING": None}
    assert all(value is None or isinstance(value, float) for value in result.values())


def test_yahoo_provider_does_not_call_yahoo_without_tickers(yahoo):
    assert YahooFinanceProvider(client_factory=yahoo).get_daily_pnls([]) == {}
    assert yahoo.created_with == []


# --- Interactive Brokers ---------------------------------------------------------------------------------------------


@dataclass
class Snapshot:
    """The fields of an ib_async Ticker that the provider reads; nan means 'no value', as in ib_async."""

    last: float = math.nan
    bid: float = math.nan
    ask: float = math.nan
    close: float = math.nan


class FakeIB:
    """Stands in for ib_async.IB: resolves the symbols in ``known`` and serves ``snapshots`` by symbol."""

    instances = []
    known = {"AAPL", "KO", "TPCO"}
    snapshots = {}

    def __init__(self):
        self.connected = False
        self.calls = []
        FakeIB.instances.append(self)

    def connect(self, host, port, clientId, timeout, readonly):  # noqa: N803 - ib_async's keyword
        self.calls.append(("connect", host, port, clientId, timeout, readonly))
        self.connected = True

    def isConnected(self):  # noqa: N802 - ib_async's name
        return self.connected

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False

    def reqMarketDataType(self, market_data_type):  # noqa: N802
        self.calls.append(("reqMarketDataType", market_data_type))

    def qualifyContracts(self, *contracts):  # noqa: N802
        self.calls.append(("qualifyContracts", [contract.symbol for contract in contracts]))
        for number, contract in enumerate(contracts, start=1):
            if contract.symbol in FakeIB.known:
                contract.conId = 1000 + number
        return [contract for contract in contracts if contract.conId]

    def reqTickers(self, *contracts):  # noqa: N802
        self.calls.append(("reqTickers", [contract.symbol for contract in contracts]))
        return [FakeIB.snapshots.get(contract.symbol, Snapshot()) for contract in contracts]


@pytest.fixture
def fake_ib():
    FakeIB.instances = []
    FakeIB.snapshots = {}
    return FakeIB


def make_ibkr():
    return IBKRProvider(host="gateway", port=4002, client_id=7, market_data_type=1, timeout=5, ib_factory=FakeIB)


def test_ibkr_connects_lazily_read_only_and_selects_the_market_data_type(fake_ib):
    provider = make_ibkr()
    assert fake_ib.instances == []

    provider.get_daily_pnls(["AAPL"])

    [ib] = fake_ib.instances
    assert ib.calls[:2] == [("connect", "gateway", 4002, 7, 5, True), ("reqMarketDataType", 1)]
    assert ib.RequestTimeout == 5


def test_ibkr_computes_the_daily_change_from_the_previous_close(fake_ib):
    fake_ib.snapshots = {
        "AAPL": Snapshot(last=102.0, close=100.0),  # traded today: last vs previous close
        "KO": Snapshot(bid=49.0, ask=51.0, close=50.0),  # no trade yet: bid/ask midpoint
        "TPCO": Snapshot(close=10.0),  # no price at all: never fall back to the close (a fake 0% move)
    }

    result = make_ibkr().get_daily_pnls(["AAPL", "KO", "TPCO"])

    assert result["AAPL"] == pytest.approx(0.02)
    assert result["KO"] == pytest.approx(0.0)
    assert result["TPCO"] is None


def test_ibkr_skips_unresolvable_tickers_and_does_not_ask_about_them_again(fake_ib):
    fake_ib.snapshots = {"AAPL": Snapshot(last=101.0, close=100.0)}
    provider = make_ibkr()

    first = provider.get_daily_pnls(["AAPL", "ZZZZ"])
    second = provider.get_daily_pnls(["AAPL", "ZZZZ"])

    assert first == second == {"AAPL": pytest.approx(0.01), "ZZZZ": None}
    [ib] = fake_ib.instances
    assert [call for call in ib.calls if call[0] == "qualifyContracts"] == [("qualifyContracts", ["AAPL", "ZZZZ"])]
    assert [call for call in ib.calls if call[0] == "reqTickers"] == [("reqTickers", ["AAPL"])] * 2


def test_ibkr_reuses_the_connection_and_reconnects_when_it_drops(fake_ib):
    provider = make_ibkr()

    provider.get_daily_pnls(["AAPL"])
    provider.get_daily_pnls(["AAPL"])
    assert len(fake_ib.instances) == 1

    fake_ib.instances[0].connected = False
    provider.get_daily_pnls(["AAPL"])
    assert len(fake_ib.instances) == 2


def test_ibkr_does_not_connect_without_tickers(fake_ib):
    assert make_ibkr().get_daily_pnls([]) == {}
    assert fake_ib.instances == []


def test_ibkr_close_disconnects_and_is_idempotent(fake_ib):
    provider = make_ibkr()
    provider.get_daily_pnls(["AAPL"])

    provider.close()
    provider.close()

    [ib] = fake_ib.instances
    assert not ib.connected
    assert ib.calls.count(("disconnect",)) == 1


def test_ibkr_reads_its_settings_from_the_configuration(monkeypatch):
    settings = {
        "IBKR_HOST": "gateway.local",
        "IBKR_PORT": "4001",
        "IBKR_CLIENT_ID": "9",
        "IBKR_MARKET_DATA_TYPE": "1",
        "IBKR_TIMEOUT": "20",
    }
    for key, value in settings.items():
        monkeypatch.setitem(context.config, key, value)

    provider = IBKRProvider(ib_factory=FakeIB)

    assert (provider.host, provider.port, provider.client_id, provider.market_data_type, provider.timeout) == (
        "gateway.local",
        4001,
        9,
        1,
        20,
    )


def test_ibkr_defaults_to_a_local_paper_tws_with_delayed_data(monkeypatch):
    for key in ("IBKR_HOST", "IBKR_PORT", "IBKR_CLIENT_ID", "IBKR_MARKET_DATA_TYPE", "IBKR_TIMEOUT"):
        monkeypatch.delitem(context.config, key, raising=False)

    provider = IBKRProvider(ib_factory=FakeIB)

    assert (provider.host, provider.port, provider.client_id, provider.market_data_type, provider.timeout) == (
        "127.0.0.1",
        7497,
        1,
        3,
        10,
    )


def test_ibkr_explains_the_missing_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "ib_async", None)  # makes `import ib_async` raise ImportError
    provider = IBKRProvider(host="h", port=1, client_id=1, market_data_type=3, timeout=1)

    with pytest.raises(RuntimeError, match=r'pip install "market_watcher\[ibkr\]"'):
        provider.get_daily_pnls(["AAPL"])


# --- Provider selection ----------------------------------------------------------------------------------------------


class FakeContext:
    def __init__(self, **config):
        self.config = config


def test_create_provider_defaults_to_yahoo(monkeypatch):
    monkeypatch.setattr(market_data, "context", FakeContext())

    assert isinstance(create_provider(), YahooFinanceProvider)


@pytest.mark.parametrize("value", ["yahoo", "Yahoo", " YAHOO "])
def test_create_provider_reads_the_setting_case_insensitively(monkeypatch, value):
    monkeypatch.setattr(market_data, "context", FakeContext(MARKET_DATA_PROVIDER=value))

    assert create_provider().name == "yahoo"


def test_create_provider_knows_ibkr(monkeypatch):
    monkeypatch.setattr(market_data, "context", FakeContext(MARKET_DATA_PROVIDER="ibkr"))

    assert isinstance(create_provider(), IBKRProvider)


def test_create_provider_rejects_unknown_names(monkeypatch):
    monkeypatch.setattr(market_data, "context", FakeContext(MARKET_DATA_PROVIDER="bloomberg"))

    with pytest.raises(ValueError, match="'bloomberg'.*available: ibkr, yahoo"):
        create_provider()


def test_new_providers_can_be_registered(monkeypatch):
    class ConstantProvider(MarketDataProvider):
        name = "constant"

        def get_daily_pnls(self, tickers):
            return dict.fromkeys(tickers, 0.0)

    monkeypatch.setitem(market_data.PROVIDERS, "constant", ConstantProvider)

    provider = create_provider("constant")

    assert isinstance(provider, ConstantProvider)
    assert provider.get_daily_pnls(["AAPL"]) == {"AAPL": 0.0}
    provider.close()  # the default close() is a no-op
