import math
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from ib_async import OptionChain

from market_watcher import doctor, market_data
from market_watcher.config import context
from market_watcher.doctor import (
    FAIL,
    OK,
    WARN,
    check_email,
    check_market_data,
    check_market_hours,
    check_settings,
    check_slack,
    check_target_stocks,
    check_trading,
    run_checks,
)
from market_watcher.market_data import MarketDataProvider

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)  # 10:00 New York on a trading day

BASE_CONFIG = {
    "MARKET_DATA_PROVIDER": "constant",
    "UPDATE_TIMEOUT": "60",
    "LONG_THRESHOLD": "0.02",
    "SHORT_THRESHOLD": "0.005",
    "MARKET_HOURS_ENABLED": "true",
    "MARKET_HOLIDAYS": "",
    "EMAIL_NOTIFIER_ENABLED": "false",
    "SLACK_NOTIFIER_ENABLED": "false",
    "TRADING_ENABLED": "false",
    "SMTP_HOSTNAME": "smtp.example.com",
    "SMTP_PORT": "465",
    "SMTP_USERNAME": "user",
    "SMTP_PASSWORD": "secret",
    "EMAIL_SENDER": "bot@example.com",
    "EMAIL_RECIPIENTS": "alice@example.com",
    "SLACK_LONG_WEBHOOK": "https://hooks.example/long",
    "SLACK_SHORT_WEBHOOK": "https://hooks.example/short",
}


class ConstantProvider(MarketDataProvider):
    name = "constant"
    values = {}
    error = None
    closed = 0

    def get_daily_pnls(self, tickers):
        if ConstantProvider.error:
            raise ConstantProvider.error
        return {ticker: ConstantProvider.values.get(ticker) for ticker in tickers}

    def close(self):
        ConstantProvider.closed += 1


@pytest.fixture
def configured(monkeypatch):
    for key, value in BASE_CONFIG.items():
        monkeypatch.setitem(context.config, key, value)
    monkeypatch.setitem(market_data.PROVIDERS, "constant", ConstantProvider)
    ConstantProvider.values = {"AAPL": 0.012, "KO": None}
    ConstantProvider.error = None
    ConstantProvider.closed = 0
    return monkeypatch


@pytest.fixture
def stocks(tmp_path):
    path = tmp_path / "stocks.yaml"
    path.write_text("AAPL:\n  strategy: long straddle\nKO:\n  strategy: short straddle\n")
    return path


# --- Individual checks -----------------------------------------------------------------------------------------------


def test_settings_check_summarises_the_configuration(configured):
    check = check_settings()

    assert check.status == OK
    assert "provider constant" in check.detail and "poll every 60s" in check.detail
    assert (
        "long straddle > 2.00%" in check.detail
        and "NYSE sessions only" in check.detail
        and "trading disabled" in check.detail
    )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("LONG_THRESHOLD", "two percent", "could not convert"),
        ("MARKET_DATA_PROVIDER", "bloomberg", "Unknown market data provider 'bloomberg'"),
        ("MARKET_HOLIDAYS", "soon", "MARKET_HOLIDAYS entry 'soon'"),
        ("TRADING_STRATEGIES", "iron condor", "iron condor"),
    ],
)
def test_settings_check_fails_on_a_bad_value(configured, key, value, message):
    configured.setitem(context.config, key, value)

    check = check_settings()

    assert check.status == FAIL and message in check.detail


def test_target_stocks_check(stocks, tmp_path):
    ok = check_target_stocks(stocks)
    assert ok.status == OK and ok.detail.startswith("2 tickers") and "1 long straddle, 1 short straddle" in ok.detail

    missing = check_target_stocks(tmp_path / "missing.yaml")
    assert missing.status == FAIL and "FileNotFoundError" in missing.detail


def test_market_hours_check(configured):
    assert check_market_hours(NOW).detail == "market open until 16:00 EST"
    saturday = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    assert check_market_hours(saturday).detail == "market closed; next session opens 2026-11-09 09:30 EST"

    configured.setitem(context.config, "MARKET_HOURS_ENABLED", "false")
    assert check_market_hours(NOW).detail.startswith("gating disabled")


def test_market_data_check_reports_values_and_closes_the_provider(configured):
    check = check_market_data(["AAPL", "KO", "MSFT", "TSLA"])

    assert check.status == OK and check.detail == "constant: AAPL +1.20%"
    assert ConstantProvider.closed == 1


def test_market_data_check_warns_when_nothing_comes_back(configured):
    ConstantProvider.values = {}

    check = check_market_data(["AAPL", "KO"])

    assert check.status == WARN and "no values for AAPL, KO" in check.detail


def test_market_data_check_fails_when_the_provider_raises(configured):
    ConstantProvider.error = ConnectionRefusedError("Connect call failed")

    check = check_market_data(["AAPL"])

    assert check.status == FAIL and check.detail.startswith("ConnectionRefusedError: Connect call failed")


class FakeSMTP:
    instances = []
    fail_login = False

    def __init__(self, hostname, port, context=None):
        self.hostname, self.port = hostname, port
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def login(self, username, password):
        if FakeSMTP.fail_login:
            raise RuntimeError("535 authentication failed")
        self.calls.append(("login", username))

    def noop(self):
        self.calls.append(("noop",))

    def sendmail(self, sender, recipients, message):
        self.calls.append(("sendmail", recipients))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False
    monkeypatch.setattr("market_watcher.notifier.smtplib.SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_email_check_logs_in_without_sending_by_default(configured, smtp):
    configured.setitem(context.config, "EMAIL_NOTIFIER_ENABLED", "true")

    check = check_email(connect=FakeSMTP)

    assert check.status == OK and check.detail.startswith("SMTP login to smtp.example.com:465 works")
    [server] = smtp.instances
    assert server.calls == [("login", "user"), ("noop",)]


def test_email_check_can_send_a_test_email(configured, smtp):
    configured.setitem(context.config, "EMAIL_NOTIFIER_ENABLED", "true")

    check = check_email(send_test_message=True, connect=FakeSMTP)

    assert check.status == OK and check.detail == "test email sent to alice@example.com"
    [server] = smtp.instances
    assert ("sendmail", ["alice@example.com"]) in server.calls


def test_email_check_fails_on_bad_credentials(configured, smtp):
    configured.setitem(context.config, "EMAIL_NOTIFIER_ENABLED", "true")
    smtp.fail_login = True

    check = check_email(connect=FakeSMTP)

    assert check.status == FAIL and "535 authentication failed" in check.detail


def test_email_check_reports_a_disabled_notifier(configured):
    assert check_email() == doctor.Check("email", OK, "disabled")


class FakeWebhookClient:
    calls = []

    def __init__(self, url):
        self.url = url

    def send(self, text):
        FakeWebhookClient.calls.append((self.url, text))
        return type("Response", (), {"status_code": 200, "body": "ok"})()


def test_slack_check_posts_only_when_asked(configured, monkeypatch):
    configured.setitem(context.config, "SLACK_NOTIFIER_ENABLED", "true")
    configured.setitem(context.config, "SLACK_TRADING_WEBHOOK", "https://hooks.example/trading")
    FakeWebhookClient.calls = []
    monkeypatch.setattr("market_watcher.notifier.WebhookClient", FakeWebhookClient)

    quiet = check_slack()
    assert quiet.status == OK and FakeWebhookClient.calls == []

    posted = check_slack(send_test_messages=True)
    assert posted.status == OK and posted.detail == "test messages posted to 3 channel(s)"
    assert [url for url, _ in FakeWebhookClient.calls] == [
        "https://hooks.example/long",
        "https://hooks.example/short",
        "https://hooks.example/trading",
    ]


@dataclass
class Snapshot:
    last: float = math.nan
    bid: float = math.nan
    ask: float = math.nan
    close: float = math.nan


class FakeIB:
    instances = []
    accounts = ["DU1234567"]

    def __init__(self):
        self.calls = []
        self.connected = False
        FakeIB.instances.append(self)

    def connect(self, host, port, clientId, timeout, readonly):  # noqa: N803 - ib_async's keyword
        self.calls.append(("connect", host, port, clientId, readonly))
        self.connected = True

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False

    def managedAccounts(self):  # noqa: N802 - ib_async's name
        return list(FakeIB.accounts)

    def qualifyContracts(self, *contracts):  # noqa: N802
        for contract in contracts:
            contract.conId = 1 if contract.symbol == "AAPL" else 0
        return [contract for contract in contracts if contract.conId]

    def reqSecDefOptParams(self, symbol, fut_fop_exchange, sec_type, con_id):  # noqa: N802
        return [OptionChain("SMART", 1, "AAPL", "100", ["20261120", "20261218"], [95.0, 100.0, 105.0])]


@pytest.fixture
def fake_ib():
    FakeIB.instances = []
    FakeIB.accounts = ["DU1234567"]
    return FakeIB


def test_trading_check_connects_read_only_and_reports_the_chain(configured, fake_ib):
    configured.setitem(context.config, "TRADING_ENABLED", "true")

    check = check_trading(["AAPL"], ib_factory=FakeIB)

    assert check.status == OK
    assert "account(s) DU1234567 (paper)" in check.detail
    assert "AAPL option chain: 2 expiries, 3 strikes" in check.detail
    [ib] = fake_ib.instances
    assert ib.calls == [("connect", "127.0.0.1", 7497, 2, True), ("disconnect",)]


def test_trading_check_warns_on_a_live_account(configured, fake_ib):
    configured.setitem(context.config, "TRADING_ENABLED", "true")
    fake_ib.accounts = ["U7654321"]

    check = check_trading(["AAPL"], ib_factory=FakeIB)

    assert check.status == WARN and "orders would be refused" in check.detail

    configured.setitem(context.config, "TRADING_ALLOW_LIVE", "true")
    assert "LIVE TRADING IS ALLOWED" in check_trading(["AAPL"], ib_factory=FakeIB).detail


def test_trading_check_fails_when_tws_is_unreachable(configured):
    configured.setitem(context.config, "TRADING_ENABLED", "true")

    class RefusingIB(FakeIB):
        def connect(self, *args, **kwargs):
            raise ConnectionRefusedError("Connect call failed")

    check = check_trading(["AAPL"], ib_factory=RefusingIB)

    assert check.status == FAIL and "is TWS / IB Gateway running" in check.detail


def test_trading_check_reports_a_disabled_trader(configured):
    assert check_trading(["AAPL"], ib_factory=FakeIB) == doctor.Check("trading", OK, "disabled")


# --- run_checks ------------------------------------------------------------------------------------------------------


def test_run_checks_returns_every_check_in_order(configured, stocks, fake_ib):
    checks = run_checks(stocks, ib_factory=FakeIB, now=NOW)

    assert [check.name for check in checks] == [
        "optional dependencies",
        "settings",
        "target stocks",
        "market hours",
        "market data",
        "email",
        "slack",
        "trading",
    ]
    assert all(check.status == OK for check in checks)


def test_run_checks_falls_back_to_aapl_when_the_stocks_file_is_broken(configured, tmp_path, fake_ib):
    ConstantProvider.values = {"AAPL": 0.03}

    checks = {check.name: check for check in run_checks(tmp_path / "missing.yaml", ib_factory=FakeIB, now=NOW)}

    assert checks["target stocks"].status == FAIL
    assert checks["market data"].detail == "constant: AAPL +3.00%"


def test_settings_check_shows_the_resolved_provider_behind_an_alias(configured):
    configured.setitem(context.config, "MARKET_DATA_PROVIDER", "yahoo")

    assert "provider yahoo (yfinance)" in check_settings().detail


def test_email_check_hints_at_the_tls_mode_on_a_disconnect(configured):
    configured.setitem(context.config, "EMAIL_NOTIFIER_ENABLED", "true")

    class Disconnecting:
        def __init__(self, hostname, port):
            raise ConnectionError("Connection unexpectedly closed")

    check = check_email(connect=Disconnecting)

    assert check.status == FAIL
    assert (
        "Connection unexpectedly closed" in check.detail
        and "465 means implicit TLS, 587 means STARTTLS" in check.detail
    )
