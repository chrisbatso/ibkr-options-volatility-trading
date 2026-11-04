import pytest

from market_watcher import market_data
from market_watcher.market_data import MarketDataProvider, YahooFinanceProvider, create_provider


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


def test_create_provider_rejects_unknown_names(monkeypatch):
    monkeypatch.setattr(market_data, "context", FakeContext(MARKET_DATA_PROVIDER="bloomberg"))

    with pytest.raises(ValueError, match="'bloomberg'.*available: yahoo"):
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
