import signal

import pytest
from click.testing import CliRunner

from market_watcher import market_watcher_cli
from market_watcher.common import context


def test_help_lists_the_commands():
    result = CliRunner().invoke(market_watcher_cli.cli, ["--help"])

    assert result.exit_code == 0
    commands_section = result.output.split("Commands:")[1]
    command_names = {line.split()[0] for line in commands_section.splitlines() if line.strip()}
    assert command_names == {"start", "config", "test-slack"}


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
        "EMAIL_NOTIFIER_ENABLED": "false",
        "SLACK_NOTIFIER_ENABLED": "false",
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
    assert "Using market data provider: yahoo" in result.output
    assert "both notifiers are disabled" in result.output
    assert "MarketWatcher started." in result.output
    assert "MarketWatcher stopped." in result.output


def test_start_fails_clearly_when_the_stocks_file_is_missing(start_environment, tmp_path):
    _, engines_run = start_environment

    result = CliRunner().invoke(market_watcher_cli.cli, ["start", "--stocks", str(tmp_path / "missing.yaml")])

    assert result.exit_code == 1
    assert "MarketWatcher failed to start" in result.output
    assert "missing.yaml" in result.output
    assert engines_run == []
