import signal

import pytest
from click.testing import CliRunner

from market_watcher import market_watcher_cli
from market_watcher.common import context
from market_watcher.market_hours import AlwaysOpen, USMarketHours


def test_help_lists_the_commands():
    result = CliRunner().invoke(market_watcher_cli.cli, ["--help"])

    assert result.exit_code == 0
    commands_section = result.output.split("Commands:")[1]
    command_names = {line.split()[0] for line in commands_section.splitlines() if line.strip()}
    assert command_names == {"start", "config", "test-slack", "screen"}


def test_config_masks_secrets(monkeypatch):
    monkeypatch.setattr(
        market_watcher_cli,
        "get_slack_config",
        lambda: {
            "long url": "https://hooks.slack.com/services/T000/B000/LONGSECRET",
            "short url": "https://hooks.slack.com/services/T000/B000/SHORTSECRET",
        },
    )
    monkeypatch.setattr(
        market_watcher_cli,
        "get_email_config",
        lambda: {
            "hostname": "smtp.example.com",
            "port": "465",
            "username": "user",
            "password": "s3cret",
            "sender": "bot@example.com",
            "recipients": "alice@example.com",
        },
    )

    result = CliRunner().invoke(market_watcher_cli.cli, ["config"])

    assert result.exit_code == 0
    assert "smtp.example.com" in result.output
    assert "https://hooks.slack.com/services/..." in result.output
    for secret in ("s3cret", "LONGSECRET", "SHORTSECRET"):
        assert secret not in result.output


# --- Signal handling -------------------------------------------------------------------------------------------------


class FakeEngine:
    def __init__(self):
        self.stopping = False

    def stop(self):
        self.stopping = True


@pytest.fixture
def installed_handlers(monkeypatch):
    """Captures signal handlers instead of installing them in the test process."""
    installed = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: installed.__setitem__(signum, handler))
    return installed


def test_stop_on_signals_handles_sigint_and_sigterm(installed_handlers):
    handler = market_watcher_cli.stop_on_signals(FakeEngine())

    assert set(installed_handlers) == {signal.SIGINT, signal.SIGTERM}
    assert all(installed is handler for installed in installed_handlers.values())


def test_first_signal_stops_gracefully_and_a_second_one_exits(installed_handlers):
    engine = FakeEngine()
    handler = market_watcher_cli.stop_on_signals(engine)

    handler(signal.SIGTERM, None)
    assert engine.stopping

    with pytest.raises(SystemExit) as exc_info:
        handler(signal.SIGTERM, None)
    assert exc_info.value.code == 128 + signal.SIGTERM


# --- start -----------------------------------------------------------------------------------------------------------


@pytest.fixture
def start_environment(monkeypatch, tmp_path):
    """Config and stubs that let `start` run without notifiers, signals or a real polling loop."""
    for key, value in {
        "MARKET_DATA_PROVIDER": "yahoo",
        "MARKET_HOURS_ENABLED": "true",
        "MARKET_HOLIDAYS": "",
        "EMAIL_NOTIFIER_ENABLED": "false",
        "SLACK_NOTIFIER_ENABLED": "false",
        "TRADING_ENABLED": "false",
        "LONG_THRESHOLD": "0.02",
        "SHORT_THRESHOLD": "0.005",
        "UPDATE_TIMEOUT": "60",
    }.items():
        monkeypatch.setitem(context.config, key, value)
    monkeypatch.setattr(market_watcher_cli, "stop_on_signals", lambda engine: None)

    engines_run = []
    monkeypatch.setattr(
        market_watcher_cli.MarketWatcherEngine,
        "search_for_investment_opportunities",
        lambda self: engines_run.append(self),
    )

    stocks = tmp_path / "stocks.yaml"
    stocks.write_text("AAPL:\n  strategy: short straddle\n")
    return stocks, engines_run


def test_start_runs_the_engine_and_exits_cleanly(start_environment):
    stocks, engines_run = start_environment

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(stocks)])

    assert result.exit_code == 0, result.output
    [engine] = engines_run
    assert engine.target_stocks == {"AAPL": {"strategy": "short straddle"}}
    assert engine.notifiers == []
    assert engine.data_provider.name == "yahoo"
    assert isinstance(engine.market_hours, USMarketHours)
    assert "Using market data provider: yahoo" in result.output
    assert "NYSE regular trading hours" in result.output
    assert "both notifiers are disabled" in result.output
    assert "MarketWatcher started." in result.output
    assert "MarketWatcher stopped." in result.output


def test_start_can_poll_around_the_clock(start_environment, monkeypatch):
    stocks, engines_run = start_environment
    monkeypatch.setitem(context.config, "MARKET_HOURS_ENABLED", "false")

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(stocks)])

    assert result.exit_code == 0, result.output
    [engine] = engines_run
    assert isinstance(engine.market_hours, AlwaysOpen)
    assert "polling around the clock" in result.output


def test_start_fails_clearly_on_a_malformed_market_holiday(start_environment, monkeypatch):
    stocks, engines_run = start_environment
    monkeypatch.setitem(context.config, "MARKET_HOLIDAYS", "2026-01-09, next tuesday")

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(stocks)])

    assert result.exit_code == 1
    assert "MARKET_HOLIDAYS entry 'next tuesday'" in result.output
    assert engines_run == []


def test_start_fails_clearly_when_the_stocks_file_is_missing(start_environment, tmp_path):
    _, engines_run = start_environment

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(tmp_path / "missing.yaml")])

    assert result.exit_code == 1
    assert "MarketWatcher failed to start" in result.output
    assert "missing.yaml" in result.output
    assert engines_run == []


class ClosableProvider:
    name = "closable"

    def __init__(self):
        self.closed = 0

    def get_daily_pnls(self, tickers):
        return dict.fromkeys(tickers)

    def close(self):
        self.closed += 1


def test_screen_writes_the_target_stocks_and_summarises(monkeypatch, tmp_path):
    from market_watcher import screen as screen_module
    from market_watcher.screen import History

    def closes(amplitude):
        return [100.0 * (1 + amplitude * (1 if day % 2 else -1)) for day in range(30)]

    monkeypatch.setattr(screen_module, "load_universe", lambda source: ["CALM", "WILD"])
    monkeypatch.setattr(
        screen_module,
        "fetch_history",
        lambda tickers: {
            "CALM": History(closes(0.005), [1e7] * 30),
            "WILD": History(closes(0.05), [1e7] * 30),
        },
    )
    output = tmp_path / "stocks.yaml"

    result = CliRunner().invoke(market_watcher_cli.cli, ["screen", "--top", "1", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert "Long straddle (most volatile): WILD" in result.output
    assert "Short straddle (least volatile): CALM" in result.output
    assert f"Wrote {output}" in result.output
    assert output.exists()


def test_screen_fails_clearly(monkeypatch, tmp_path):
    from market_watcher import screen as screen_module

    monkeypatch.setattr(screen_module, "load_universe", lambda source: (_ for _ in ()).throw(OSError("network down")))

    result = CliRunner().invoke(market_watcher_cli.cli, ["screen", "--output", str(tmp_path / "x.yaml")])

    assert result.exit_code == 1
    assert "Screen failed: network down" in result.output


def test_start_leaves_order_placement_off_by_default(start_environment):
    stocks, engines_run = start_environment

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(stocks)])

    assert result.exit_code == 0, result.output
    [engine] = engines_run
    assert engine.notifiers == []
    assert "Order placement disabled" in result.output


def test_start_adds_the_trader_when_order_placement_is_enabled(start_environment, monkeypatch):
    from market_watcher.trading import IBKRStraddleTrader

    stocks, engines_run = start_environment
    monkeypatch.setitem(context.config, "TRADING_ENABLED", "true")

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(stocks)])

    assert result.exit_code == 0, result.output
    [engine] = engines_run
    [trader] = engine.notifiers
    assert isinstance(trader, IBKRStraddleTrader)
    assert trader.settings.enabled and not trader.settings.transmit and not trader.settings.allow_live
    assert engine.cycle_hooks == [trader.manage_positions]
    assert "Order placement ENABLED: staged in TWS; paper accounts only; long straddle" in result.output


def test_start_closes_the_trader_on_exit(start_environment, monkeypatch):
    stocks, _ = start_environment
    monkeypatch.setitem(context.config, "TRADING_ENABLED", "true")
    closed = []
    monkeypatch.setattr(market_watcher_cli.IBKRStraddleTrader, "close", lambda self: closed.append(self))

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(stocks)])

    assert result.exit_code == 0, result.output
    assert len(closed) == 1


def test_start_closes_the_market_data_provider_on_exit(start_environment, monkeypatch):
    stocks, _ = start_environment
    provider = ClosableProvider()
    monkeypatch.setattr(market_watcher_cli, "create_provider", lambda: provider)

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(stocks)])

    assert result.exit_code == 0, result.output
    assert provider.closed == 1
