import logging
import signal
import sys

import click
from click.utils import echo

from market_watcher.common import (
    MarketWatcherEngine,
    config_flag,
    get_email_config,
    get_slack_config,
    get_target_stocks,
)
from market_watcher.notifier import EmailNotifier, SlackNotifier
from market_watcher.version import VERSION

# Prefix of a Slack incoming-webhook URL; the remainder is the secret part.
SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/services/"

# Signals that stop the bot: Ctrl-C in a terminal, `docker stop` / `kill` from outside.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@click.group()
def cli():
    """MarketWatcher cli commands."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    echo(
        r"""
         ______              _              _  _  _                 _
        |  ___ \            | |         _  | || || |      _        | |
        | | _ | | ____  ____| |  _ ____| |_| || || | ____| |_  ____| | _   ____  ____
        | || || |/ _  |/ ___) | / ) _  )  _) ||_|| |/ _  |  _)/ ___) || \ / _  )/ ___)
        | || || ( ( | | |   | |< ( (/ /| |_| |___| ( ( | | |_( (___| | | ( (/ /| |
        |_||_||_|\_||_|_|   |_| \_)____)\___)______|\_||_|\___)____)_| |_|\____)_|

        MarketWatcher tool for finding investment opportunities on Interactive Brokers
        for volatility trading on equity market using long and short options strategy.
    """  # noqa
    )
    echo(f"version: v{VERSION}")
    echo("\n\n\n")


@cli.command()
def test_slack():
    """Sends dummy messages to test if Slack app has been configured properly."""

    try:
        echo("Testing Slack options-trading bot...")

        config = get_slack_config()
        slack_notifier = SlackNotifier(config)

        echo("Sending message to #options-long-straddle channel...")
        slack_notifier.send_message(config["long url"], "MarketWatcher: Test long!")

        echo("Sending message to #options-short-straddle channel...")
        slack_notifier.send_message(config["short url"], "MarketWatcher: Test short!")
    except Exception as e:
        raise click.ClickException(f"Slack testing failed: {e}") from e


def _display_value(key, value):
    """Masks secrets so the output of `config` can be shared safely."""
    if "password" in key:
        return "********"
    if "url" in key and value.startswith(SLACK_WEBHOOK_PREFIX):
        return SLACK_WEBHOOK_PREFIX + "..."
    return value


@cli.command()
@click.option(
    "--notifier",
    default="all",
    help="Available options: email, slack, all",
)
def config(notifier):
    """Lists configuration for slack and email notifiers (secrets are masked)."""

    if notifier in ("all", "slack"):
        config = get_slack_config()
        for key, value in config.items():
            echo(f"{key}: {_display_value(key, value)}")

    if notifier in ("all", "email"):
        config = get_email_config()
        for key, value in config.items():
            echo(f"{key}: {_display_value(key, value)}")


def stop_on_signals(engine):
    """Installs handlers so SIGINT (Ctrl-C) and SIGTERM (`docker stop`, `kill`) stop ``engine`` gracefully.

    The first signal lets the current polling cycle finish; a second one exits
    immediately. Returns the handler so it can also be invoked directly.
    """

    def handle(signum, frame):
        name = signal.Signals(signum).name
        if engine.stopping:
            echo(f"Received {name} again, exiting immediately.")
            sys.exit(128 + signum)
        echo(f"Received {name}, stopping MarketWatcher after the current cycle (send it again to exit immediately).")
        engine.stop()

    for stop_signal in STOP_SIGNALS:
        signal.signal(stop_signal, handle)
    return handle


@cli.command()
@click.option(
    "--stocks",
    default=r"src/market_watcher/research/target_stocks.yaml",
    help="YAML file with the target stocks for the long and short straddle option strategies.",
)
def start(stocks):
    """Starts the MarketWatcher in the foreground; stop it with Ctrl-C (or SIGTERM)."""
    echo("Starting MarketWatcher...")

    try:
        echo(f"Reading target stocks from file: {stocks}")
        target_stocks = get_target_stocks(stocks)

        notifiers = []

        if config_flag("EMAIL_NOTIFIER_ENABLED", default=True):
            echo("Instantiating email notifier...")
            notifiers.append(EmailNotifier(get_email_config()))

        if config_flag("SLACK_NOTIFIER_ENABLED", default=True):
            echo("Instantiating slack notifier...")
            notifiers.append(SlackNotifier(get_slack_config()))

        if not notifiers:
            echo("Warning: both notifiers are disabled; opportunities will only be logged.")

        market_watcher_engine = MarketWatcherEngine(target_stocks=target_stocks, notifiers=notifiers)
    except Exception as e:
        raise click.ClickException(f"MarketWatcher failed to start: {e}") from e

    stop_on_signals(market_watcher_engine)
    echo("MarketWatcher started. Press Ctrl-C to stop.")

    try:
        market_watcher_engine.search_for_investment_opportunities()
    except Exception as e:
        raise click.ClickException(f"MarketWatcher failed: {e}") from e

    echo("MarketWatcher stopped.")
