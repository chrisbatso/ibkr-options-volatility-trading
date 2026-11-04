import logging
import threading
from enum import Enum

import yaml
from yahoofinancials import YahooFinancials

from market_watcher.config import context

logger = logging.getLogger(__name__)


class STRATEGIES(Enum):
    LONG_STRADDLE = "long straddle"
    SHORT_STRADDLE = "short straddle"


VALID_STRATEGIES = {strategy.value for strategy in STRATEGIES}


def get_target_stocks(file_path):
    """Reads target stocks for the long/short straddle strategies from a YAML file.

    The file maps tickers to a mapping with a ``strategy`` key::

        AAPL:
          strategy: short straddle

    Raises ``OSError`` if the file cannot be read, ``yaml.YAMLError`` if it is not
    valid YAML and ``ValueError`` if its structure is not the one described above.
    """
    with open(file_path) as f:
        target_stocks = yaml.safe_load(f)

    if not isinstance(target_stocks, dict) or not target_stocks:
        raise ValueError(f"{file_path}: expected a non-empty mapping of ticker -> {{strategy: ...}}")

    for ticker, settings in target_stocks.items():
        strategy = settings.get("strategy") if isinstance(settings, dict) else None
        if strategy not in VALID_STRATEGIES:
            raise ValueError(
                f"{file_path}: ticker {ticker!r} has invalid strategy {strategy!r}; "
                f"expected one of {sorted(VALID_STRATEGIES)}"
            )

    return target_stocks


def _required_config(key):
    """Returns a configuration value from .env / the environment, with a clear error when it is missing."""
    try:
        return context.config[key]
    except KeyError:
        raise ValueError(f"Missing configuration value {key}; set it in .env or as an environment variable") from None


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def config_flag(key, default):
    """Returns a boolean configuration value (true/false, yes/no, on/off, 1/0), or ``default`` when it is unset."""
    value = context.config.get(key)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"Configuration value {key}={value!r} is not a boolean; use true or false")


def get_email_config():
    """Returns email notifier related configuration."""
    return {
        "hostname": _required_config("SMTP_HOSTNAME"),
        "port": _required_config("SMTP_PORT"),
        "username": _required_config("SMTP_USERNAME"),
        "password": _required_config("SMTP_PASSWORD"),
        "sender": _required_config("EMAIL_SENDER"),
        "recipients": _required_config("EMAIL_RECIPIENTS"),
    }


def get_pnl_threshold_config():
    """Returns strategy alert configuration."""
    return {
        "LONG_THRESHOLD": float(_required_config("LONG_THRESHOLD")),
        "SHORT_THRESHOLD": float(_required_config("SHORT_THRESHOLD")),
    }


def get_slack_config():
    """Returns Slack notifier related configuration."""
    return {
        "long url": _required_config("SLACK_LONG_WEBHOOK"),
        "short url": _required_config("SLACK_SHORT_WEBHOOK"),
    }


class MarketWatcherEngine:
    """MarketWatcher core engine: polls the daily P&L of the target stocks and
    notifies about long/short straddle opportunities."""

    def __init__(self, target_stocks=None, notifiers=None, long_threshold=None, short_threshold=None):
        self.target_stocks = target_stocks or {}
        self.notifiers = notifiers or []

        if long_threshold is None or short_threshold is None:
            pnl_threshold_config = get_pnl_threshold_config()
            if long_threshold is None:
                long_threshold = pnl_threshold_config["LONG_THRESHOLD"]
            if short_threshold is None:
                short_threshold = pnl_threshold_config["SHORT_THRESHOLD"]
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold

        self.daily_pnls = {}
        # Tickers reported in the previous cycle, per strategy: alerts are sent on threshold crossings only.
        self._reported = {strategy.value: set() for strategy in STRATEGIES}
        # Tickers the data source returned no value for, so the warning is logged once rather than every cycle.
        self._unavailable = set()
        # Set by stop(); the polling loop waits on it so a stop request is honoured without waiting out the interval.
        self._stop_event = threading.Event()

    def stop(self):
        """Asks a running :meth:`search_for_investment_opportunities` loop to return once the current cycle is done.

        Safe to call from a signal handler or another thread. Calling it before the
        loop starts makes the loop return immediately, so a stop request that arrives
        during start-up is not lost.
        """
        self._stop_event.set()

    @property
    def stopping(self):
        """Whether :meth:`stop` has been called."""
        return self._stop_event.is_set()

    def search_for_investment_opportunities(self):
        """Polls the market every ``UPDATE_TIMEOUT`` seconds until :meth:`stop` is called.

        The first cycle runs immediately. A failing cycle (for example the data
        source being unreachable) is logged and retried on the next cycle instead
        of terminating the bot.
        """
        update_timeout = int(_required_config("UPDATE_TIMEOUT"))

        while not self._stop_event.is_set():
            try:
                self.process_latest_market_movements()
            except Exception:
                logger.exception("Polling cycle failed; retrying in %d seconds", update_timeout)
            # Returns as soon as stop() is called, so shutdown does not wait out the interval.
            self._stop_event.wait(update_timeout)

    def process_latest_market_movements(self):
        """Checks every target stock against the threshold of its strategy.

        Returns all current opportunities per strategy and notifies the subscribed
        notifiers about the ones that are new since the previous cycle, so a stock
        that stays beyond its threshold is reported once, when it crosses it,
        rather than on every cycle.
        """
        self.daily_pnls = self.get_daily_pnls()

        investment_opportunities = []
        for ticker, settings in self.target_stocks.items():
            daily_pnl = self.daily_pnls.get(ticker)
            if daily_pnl is None:
                if ticker not in self._unavailable:
                    logger.warning("No daily P&L available for %s; skipping it until data is available", ticker)
                    self._unavailable.add(ticker)
                continue
            self._unavailable.discard(ticker)

            if self.is_investment_opportunity(settings["strategy"], abs(daily_pnl)):
                investment_opportunities.append(ticker)

        investment_data = self.get_investment_data(investment_opportunities)
        new_investment_data = self._new_opportunities(investment_data)

        if any(new_investment_data.values()):
            logger.info("New opportunities: %s", new_investment_data)
            self.notify(new_investment_data)

        return investment_data

    def _new_opportunities(self, investment_data):
        """Returns the subset of ``investment_data`` that was not reported in the previous cycle."""
        new_investment_data = {}
        for strategy, opportunities in investment_data.items():
            previously_reported = self._reported.get(strategy, set())
            new_investment_data[strategy] = {
                ticker: pnl for ticker, pnl in opportunities.items() if ticker not in previously_reported
            }
            self._reported[strategy] = set(opportunities)
        return new_investment_data

    def notify(self, investment_data):
        """Sends investment updates to the subscribed notifiers.

        A failing notifier is logged and does not prevent the others from being notified.
        """
        for notifier in self.notifiers:
            try:
                notifier.notify(investment_data)
            except Exception:
                logger.exception("Notifier %s failed", type(notifier).__name__)

    def get_daily_pnls(self):
        """Returns the daily percent change of each target stock, as a fraction (0.02 == 2%)."""
        target_stocks = list(self.target_stocks.keys())
        return YahooFinancials(target_stocks).get_current_percent_change()

    def get_investment_data(self, investment_opportunities):
        """Returns the opportunities with their daily P&L, grouped by strategy."""
        long_straddle = {}
        short_straddle = {}

        for ticker in investment_opportunities:
            strategy = self.target_stocks[ticker]["strategy"]
            if STRATEGIES.LONG_STRADDLE.value == strategy:
                long_straddle[ticker] = self.daily_pnls[ticker]
            elif STRATEGIES.SHORT_STRADDLE.value == strategy:
                short_straddle[ticker] = self.daily_pnls[ticker]

        return {
            STRATEGIES.LONG_STRADDLE.value: long_straddle,
            STRATEGIES.SHORT_STRADDLE.value: short_straddle,
        }

    def is_investment_opportunity(self, strategy, abs_daily_pnl):
        """Checks whether a stock with this absolute daily P&L is an opportunity for the given strategy."""
        if STRATEGIES.LONG_STRADDLE.value == strategy:
            return abs_daily_pnl > self.long_threshold
        if STRATEGIES.SHORT_STRADDLE.value == strategy:
            return abs_daily_pnl < self.short_threshold
        return False
