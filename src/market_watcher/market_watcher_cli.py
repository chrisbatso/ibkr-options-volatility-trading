import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

import click
from click.utils import echo

from market_watcher import doctor as doctor_module
from market_watcher import screen as screen_module
from market_watcher.common import (
    MarketWatcherEngine,
    get_email_config,
    get_pnl_threshold_config,
    get_slack_config,
    get_target_stocks,
    get_update_timeout,
)
from market_watcher.config import config_flag, config_int, context
from market_watcher.market_data import DEFAULT_PROVIDER, create_provider
from market_watcher.market_hours import AlwaysOpen, USMarketHours, parse_closures
from market_watcher.notifier import EmailNotifier, SlackNotifier
from market_watcher.trading import IBKRStraddleTrader, TradingSettings
from market_watcher.version import VERSION

logger = logging.getLogger(__name__)

# Prefix of a Slack incoming-webhook URL; the remainder is the secret part.
SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/services/"

# Signals that stop the bot: Ctrl-C in a terminal, `docker stop` / `kill` from outside.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def configure_logging(log_file=None):
    """Logs to stderr and, when LOG_FILE is set, to a rotating file (10 MB x 5) for unattended runs."""
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True
    )


@click.group()
def cli():
    """MarketWatcher cli commands."""
    configure_logging(context.config.get("LOG_FILE"))
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
    if value is None:
        return "(not set)"
    if "password" in key:
        return "********"
    if "url" in key and isinstance(value, str) and value.startswith(SLACK_WEBHOOK_PREFIX):
        return SLACK_WEBHOOK_PREFIX + "..."
    return value


def _section(title, loader):
    """Rows of one configuration section, or a single error row when a setting is missing or malformed."""
    try:
        return title, list(loader())
    except Exception as e:
        return title, [("error", str(e))]


def effective_configuration():
    """The configuration `start` would run with, as (section, [(key, value)]) with secrets masked."""

    def polling():
        thresholds = get_pnl_threshold_config()
        yield "market data provider", (context.config.get("MARKET_DATA_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
        yield "polling interval", f"{get_update_timeout()} s"
        yield "long straddle threshold", f"{thresholds['LONG_THRESHOLD']:.2%}"
        yield "short straddle threshold", f"{thresholds['SHORT_THRESHOLD']:.2%}"
        yield "log file", context.config.get("LOG_FILE") or "(stderr only)"

    def market_hours():
        if config_flag("MARKET_HOURS_ENABLED", default=True):
            closures = sorted(parse_closures(context.config.get("MARKET_HOLIDAYS")))
            yield "polling", "NYSE regular sessions only"
            yield "extra closures", ", ".join(str(day) for day in closures) or "none"
            yield (
                "short straddle warm-up",
                f"{config_int('SHORT_STRADDLE_WARMUP_MINUTES', default=30)} min after the open",
            )
        else:
            yield "polling", "around the clock (MARKET_HOURS_ENABLED=false)"

    def notifier(name, flag, loader):
        def rows():
            if not config_flag(flag, default=True):
                yield name, "disabled"
                return
            for key, value in loader().items():
                yield f"{name} {key}", _display_value(key, value)

        return rows

    def trading():
        settings = TradingSettings.from_config()
        yield "order placement", settings.describe() if settings.enabled else "disabled (TRADING_ENABLED=false)"

    return [
        _section("Polling", polling),
        _section("Market hours", market_hours),
        _section("Email", notifier("email", "EMAIL_NOTIFIER_ENABLED", get_email_config)),
        _section("Slack", notifier("slack", "SLACK_NOTIFIER_ENABLED", get_slack_config)),
        _section("Trading", trading),
    ]


@cli.command()
def config():
    """Shows the effective configuration `start` would run with (secrets are masked)."""
    for title, rows in effective_configuration():
        echo(f"[{title}]")
        for key, value in rows:
            echo(f"  {key}: {value}")


@cli.command()
@click.option("--stocks", default=screen_module.DEFAULT_OUTPUT, show_default=True, help="Target stocks YAML to check.")
@click.option("--send-test-messages", is_flag=True, help="Also post a test message to Slack and send a test email.")
def doctor(stocks, send_test_messages):
    """Checks settings, target stocks, market hours, the data source, notifiers and TWS before you run `start`."""
    checks = doctor_module.run_checks(stocks, send_test_messages=send_test_messages)
    labels = {doctor_module.OK: " OK ", doctor_module.WARN: "WARN", doctor_module.FAIL: "FAIL"}
    for check in checks:
        echo(f"[{labels[check.status]}] {check.name}: {check.detail}")
    failed = [check for check in checks if check.status == doctor_module.FAIL]
    if failed:
        raise click.ClickException(f"{len(failed)} check(s) failed")
    warnings = [check for check in checks if check.status == doctor_module.WARN]
    echo("All checks passed." if not warnings else f"Checks passed with {len(warnings)} warning(s).")


@cli.command("screen")
@click.option(
    "--universe",
    default="iwv",
    show_default=True,
    help="'iwv' downloads the iShares Russell 3000 ETF holdings; otherwise a CSV/TXT file with tickers.",
)
@click.option("--top", default=50, show_default=True, help="Number of stocks per strategy.")
@click.option("--min-price", default=5.0, show_default=True, help="Skip stocks whose last close is below this (USD).")
@click.option(
    "--min-dollar-volume",
    default=5_000_000.0,
    show_default=True,
    help="Skip stocks whose 21-day average dollar volume is below this (USD).",
)
@click.option("--output", default=screen_module.DEFAULT_OUTPUT, show_default=True, help="Target stocks YAML to write.")
@click.option("--report", default=None, help="Also write a CSV with the volatility of every screened stock.")
def screen_command(universe, top, min_price, min_dollar_volume, output, report):
    """Regenerates the target stocks file from a historical-volatility screen (needs the `research` extra)."""
    echo(f"Screening universe {universe!r} for the {top} most and {top} least volatile stocks...")

    try:
        result = screen_module.run(
            universe=universe,
            top=top,
            min_price=min_price,
            min_dollar_volume=min_dollar_volume,
            output=output,
            report=report,
        )
    except Exception as e:
        raise click.ClickException(f"Screen failed: {e}") from e

    echo(
        f"Universe: {result.universe_size} tickers, price history for {result.fetched}, eligible: {len(result.ranked)}"
    )
    for reason, count in sorted(result.skipped.items()):
        echo(f"  skipped {count}: {reason}")
    echo(
        "Long straddle (most volatile): "
        + ", ".join(f"{r.ticker} {r.weighted_volatility:.1%}" for r in result.long_straddle)
    )
    echo(
        "Short straddle (least volatile): "
        + ", ".join(f"{r.ticker} {r.weighted_volatility:.1%}" for r in result.short_straddle)
    )
    echo(f"Wrote {output}" + (f" and {report}" if report else ""))


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

    closers = []  # providers and traders to disconnect on exit
    started = False
    try:
        echo(f"Reading target stocks from file: {stocks}")
        target_stocks = get_target_stocks(stocks)

        data_provider = create_provider()
        closers.append(data_provider)
        echo(f"Using market data provider: {data_provider.name}")

        if config_flag("MARKET_HOURS_ENABLED", default=True):
            market_hours = USMarketHours(extra_closures=parse_closures(context.config.get("MARKET_HOLIDAYS")))
            echo("Polling during NYSE regular trading hours only (MARKET_HOURS_ENABLED=false polls around the clock).")
        else:
            market_hours = AlwaysOpen()
            echo("Market-hours gating disabled: polling around the clock.")

        notifiers = []

        if config_flag("EMAIL_NOTIFIER_ENABLED", default=True):
            echo("Instantiating email notifier...")
            notifiers.append(EmailNotifier(get_email_config()))

        if config_flag("SLACK_NOTIFIER_ENABLED", default=True):
            echo("Instantiating slack notifier...")
            notifiers.append(SlackNotifier(get_slack_config()))

        if not notifiers:
            echo("Warning: both notifiers are disabled; opportunities will only be logged.")

        cycle_hooks = []
        trading = TradingSettings.from_config()
        if trading.enabled:
            trader = IBKRStraddleTrader(trading, announcers=list(notifiers))
            notifiers.append(trader)
            closers.append(trader)
            cycle_hooks.append(trader.manage_positions)
            echo(f"Order placement ENABLED: {trading.describe()}")
        else:
            echo("Order placement disabled (TRADING_ENABLED=false).")

        market_watcher_engine = MarketWatcherEngine(
            target_stocks=target_stocks,
            notifiers=notifiers,
            data_provider=data_provider,
            market_hours=market_hours,
            cycle_hooks=cycle_hooks,
        )

        stop_on_signals(market_watcher_engine)
        echo("MarketWatcher started. Press Ctrl-C to stop.")
        started = True
        market_watcher_engine.search_for_investment_opportunities()
    except Exception as e:
        phase = "failed" if started else "failed to start"
        raise click.ClickException(f"MarketWatcher {phase}: {e}") from e
    finally:
        for closer in closers:
            try:
                closer.close()
            except Exception:
                logger.exception("Could not close %s cleanly", type(closer).__name__)

    echo("MarketWatcher stopped.")
