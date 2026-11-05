"""`market_watcher_cli doctor`: checks the configuration and every external dependency before `start`.

Each check returns a :class:`Check` with a status of ``ok``, ``warn`` or ``fail`` and a
one-line detail. Nothing here changes state: Slack and email are only exercised when
the caller asks for test messages, and the TWS connection is read-only.
"""

import importlib.metadata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from market_watcher.common import (
    get_email_config,
    get_pnl_threshold_config,
    get_slack_config,
    get_target_stocks,
    get_update_timeout,
)
from market_watcher.config import config_flag, context
from market_watcher.market_data import DEFAULT_PROVIDER, PROVIDERS, create_provider
from market_watcher.market_hours import USMarketHours, parse_closures
from market_watcher.notifier import EmailNotifier, SlackNotifier, smtp_connection
from market_watcher.trading import PAPER_ACCOUNT_PREFIX, TradingSettings, select_chain

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _error(exception):
    return f"{type(exception).__name__}: {exception}"


def check_dependencies():
    parts = []
    for package, needed_for in (
        ("ib_async", "the ibkr provider and trading"),
        ("yfinance", "the default market data provider"),
    ):
        try:
            parts.append(f"{package} {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            parts.append(f"{package} not installed (needed for {needed_for})")
    return Check("optional dependencies", OK, "; ".join(parts))


def check_settings():
    """Parses every setting `start` would read, so a typo fails here rather than minutes into a session."""
    try:
        thresholds = get_pnl_threshold_config()
        update_timeout = get_update_timeout()
        trading = TradingSettings.from_config()
        gated = config_flag("MARKET_HOURS_ENABLED", default=True)
        parse_closures(context.config.get("MARKET_HOLIDAYS"))
        provider = (context.config.get("MARKET_DATA_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown market data provider {provider!r}; available: {', '.join(sorted(PROVIDERS))}")
        for key in ("EMAIL_NOTIFIER_ENABLED", "SLACK_NOTIFIER_ENABLED"):
            config_flag(key, default=True)
    except ValueError as e:
        return Check("settings", FAIL, str(e))
    resolved = PROVIDERS[provider].name
    shown_provider = provider if provider == resolved else f"{provider} ({resolved})"
    detail = (
        f"provider {shown_provider}; poll every {update_timeout}s; long straddle > {thresholds['LONG_THRESHOLD']:.2%}, "
        f"short straddle < {thresholds['SHORT_THRESHOLD']:.2%}; "
        f"{'NYSE sessions only' if gated else 'around the clock'}; "
        f"trading {'ENABLED' if trading.enabled else 'disabled'}"
    )
    return Check("settings", OK, detail)


def check_target_stocks(path):
    try:
        stocks = get_target_stocks(path)
    except Exception as e:
        return Check("target stocks", FAIL, f"{path}: {_error(e)}")
    counts = Counter(settings["strategy"] for settings in stocks.values())
    breakdown = ", ".join(f"{count} {strategy}" for strategy, count in sorted(counts.items()))
    return Check("target stocks", OK, f"{len(stocks)} tickers in {path} ({breakdown})")


def check_market_hours(now):
    if not config_flag("MARKET_HOURS_ENABLED", default=True):
        return Check("market hours", OK, "gating disabled: polling around the clock")
    try:
        hours = USMarketHours(extra_closures=parse_closures(context.config.get("MARKET_HOLIDAYS")))
        session = hours.current_session(now)
        if session is not None:
            return Check("market hours", OK, f"market open until {session.close:%H:%M %Z}")
        return Check("market hours", OK, f"market closed; next session opens {hours.next_open(now):%Y-%m-%d %H:%M %Z}")
    except ValueError as e:
        return Check("market hours", FAIL, str(e))


def check_market_data(tickers):
    """Fetches a few tickers through the configured provider, the same way the engine will."""
    sample = list(tickers)[:3]
    provider = None
    try:
        provider = create_provider()
        try:
            result = provider.get_daily_pnls(sample)
        finally:
            provider.close()
    except Exception as e:
        hint = ""
        if provider is not None and provider.name == "ibkr":
            hint = " - is TWS / IB Gateway running with the API enabled on IBKR_HOST:IBKR_PORT?"
        elif provider is not None:
            hint = " - is the machine online, and is Yahoo Finance reachable (not rate-limited)?"
        return Check("market data", FAIL, f"{_error(e)}{hint}")
    values = {ticker: value for ticker, value in result.items() if value is not None}
    if not values:
        return Check(
            "market data",
            WARN,
            f"{provider.name}: no values for {', '.join(sample)} "
            "(market closed, delisted tickers or missing data permissions)",
        )
    shown = ", ".join(f"{ticker} {value:+.2%}" for ticker, value in values.items())
    return Check("market data", OK, f"{provider.name}: {shown}")


def check_email(send_test_message=False, connect=smtp_connection):
    if not config_flag("EMAIL_NOTIFIER_ENABLED", default=True):
        return Check("email", OK, "disabled")
    try:
        config = get_email_config()
        notifier = EmailNotifier(config)
        if send_test_message:
            notifier.announce("Doctor", "MarketWatcher doctor: email notifications work.")
            return Check("email", OK, f"test email sent to {config['recipients']}")
        with connect(config["hostname"], int(config["port"])) as server:
            if config["username"]:
                server.login(config["username"], config["password"])
            server.noop()
    except Exception as e:
        return Check(
            "email",
            FAIL,
            f"{_error(e)} - SMTP_PORT 465 means implicit TLS, 587 means STARTTLS; check host, port and credentials",
        )
    return Check(
        "email",
        OK,
        f"SMTP login to {config['hostname']}:{config['port']} works (use --send-test-messages to send a test email)",
    )


def check_slack(send_test_messages=False):
    if not config_flag("SLACK_NOTIFIER_ENABLED", default=True):
        return Check("slack", OK, "disabled")
    try:
        config = get_slack_config()
        notifier = SlackNotifier(config)
        if not send_test_messages:
            return Check("slack", OK, "webhooks configured (use --send-test-messages to post a test message)")
        notifier.send_message(config["long url"], "MarketWatcher doctor: the long straddle channel works.")
        notifier.send_message(config["short url"], "MarketWatcher doctor: the short straddle channel works.")
        channels = 2
        if config.get("trading url"):
            notifier.send_message(config["trading url"], "MarketWatcher doctor: the trading channel works.")
            channels = 3
    except Exception as e:
        return Check("slack", FAIL, _error(e))
    return Check("slack", OK, f"test messages posted to {channels} channel(s)")


def check_trading(tickers, ib_factory=None):
    """Connects read-only with the trading client id, checks the account type and looks up one option chain."""
    try:
        settings = TradingSettings.from_config()
    except ValueError as e:
        return Check("trading", FAIL, str(e))
    if not settings.enabled:
        return Check("trading", OK, "disabled")

    ticker = list(tickers)[0] if tickers else "AAPL"
    try:
        if ib_factory is None:
            from ib_async import IB

            ib_factory = IB
        from ib_async import Stock

        ib = ib_factory()
        ib.RequestTimeout = settings.timeout
        ib.connect(settings.host, settings.port, clientId=settings.client_id, timeout=settings.timeout, readonly=True)
        try:
            accounts = list(ib.managedAccounts())
            stock = Stock(ticker, "SMART", "USD")
            ib.qualifyContracts(stock)
            if stock.conId:
                chain = select_chain(ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId), ticker)
                chain_detail = f"{ticker} option chain: {len(chain.expirations)} expiries, {len(chain.strikes)} strikes"
            else:
                chain_detail = f"{ticker} could not be resolved as a US stock"
        finally:
            ib.disconnect()
    except Exception as e:
        return Check(
            "trading",
            FAIL,
            f"{_error(e)} - is TWS / IB Gateway running with the API enabled on {settings.host}:{settings.port}?",
        )

    paper = bool(accounts) and all(account.startswith(PAPER_ACCOUNT_PREFIX) for account in accounts)
    account_detail = f"account(s) {', '.join(accounts) or 'none'} ({'paper' if paper else 'LIVE'})"
    note = "placing orders also needs the TWS Read-Only API setting off, which cannot be checked from here"
    if not paper and not settings.allow_live:
        return Check(
            "trading", WARN, f"{account_detail}: orders would be refused (TRADING_ALLOW_LIVE=false); {chain_detail}"
        )
    if not paper:
        return Check("trading", WARN, f"{account_detail}: LIVE TRADING IS ALLOWED; {chain_detail}; {note}")
    return Check("trading", OK, f"{account_detail}; {chain_detail}; {settings.describe()}; {note}")


def run_checks(stocks_path, send_test_messages=False, ib_factory=None, now=None):
    """Runs every check and returns the results in display order."""
    now = now or datetime.now(timezone.utc)
    checks = [check_dependencies(), check_settings()]
    stocks_check = check_target_stocks(stocks_path)
    checks.append(stocks_check)
    tickers = list(get_target_stocks(stocks_path)) if stocks_check.status == OK else ["AAPL"]
    checks.append(check_market_hours(now))
    checks.append(check_market_data(tickers))
    checks.append(check_email(send_test_message=send_test_messages))
    checks.append(check_slack(send_test_messages=send_test_messages))
    checks.append(check_trading(tickers, ib_factory=ib_factory))
    return checks
