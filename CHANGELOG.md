# Changelog

All notable changes to MarketWatcher are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - Unreleased

A ground-up overhaul: the 2021 course project brought back to a buildable, tested
state, then extended with the Interactive Brokers integration it always promised.

### Added
- `MarketDataProvider` abstraction with two Yahoo Finance providers (`yfinance`, the
  default, fetches the whole universe in one batched download and never reads an earlier
  session's bar as today's move; `yahoofinancials` is kept as a fallback) and an
  **Interactive Brokers provider** (`MARKET_DATA_PROVIDER=ibkr`, built on `ib_async`, optional
  `ibkr` extra).
- **Market-hours gating**: polling only during NYSE sessions (holidays and early
  closes computed from the published rules), with a configurable warm-up before
  short-straddle alerts and `MARKET_HOLIDAYS` for unscheduled closures.
- `market_watcher_cli screen`: regenerates `target_stocks.yaml` from the
  weighted-volatility screen described in the README, over the Russell 3000 (iShares IWV
  holdings), every US-listed stock (Nasdaq Trader symbol directory) or a local ticker list,
  with liquidity filters, a volatility floor against takeover-pinned stocks and a
  listed-options check.
- **Experimental, off-by-default order placement** on IBKR: at-the-money straddle
  combo orders staged in TWS for manual transmit, paper accounts only, long
  straddles only, with premium / notional / per-day caps and a dry-run mode.
- **Position management** for the trader: fill tracking including partial fills and
  rejections, take-profit / stop-loss / end-of-day exits that follow the market until
  they fill, daily loss limit, risk-based sizing, and a book reconciled with IB after a
  restart or a lost connection, before anything acts on it: an earlier run's straddles
  and working orders are adopted, and the day's order cap, realized P&L and loss halt
  are rebuilt from IB.
- `market_watcher_cli doctor`: checks settings, target stocks, market hours, the data
  source, Slack, email and the TWS connection before `start`.
- Trade events and an end-of-day summary are sent through the Slack / email
  notifiers (`SLACK_TRADING_WEBHOOK` optionally routes them to their own channel).
- Optional rotating log file (`LOG_FILE`); `config` prints the whole effective
  configuration with secrets masked.
- A backtest of the frozen rules on real option quotes, with its method and limits, in
  `research/backtest/`. Result: buying or selling these straddles on the bot's rules loses money.
- `STRADDLE_MAX_SPREAD_PCT` refuses straddles with wide spreads (default 6% of mid), and
  `TRADING_JOURNAL` records every order, fill and close with its quotes as CSV.
- CI builds the Docker image; tests grew from 1 to over 340.

### Changed
- Packaging moved to `pyproject.toml`; Python 3.10+ required; `ruff` replaces
  flake8/black; Docker image on `python:3.12-slim` with a `.dockerignore`.
- Alerts fire once per threshold crossing instead of every polling cycle, and the
  alert memory resets at each session open.
- The bot runs in the foreground and stops on Ctrl-C / SIGTERM; notifier toggles are
  `.env` keys (`EMAIL_NOTIFIER_ENABLED`, `SLACK_NOTIFIER_ENABLED`).
- `.env.template` polling interval raised from 5 s to 60 s.

### Fixed
- A 0.0 % daily move (the strongest short-straddle signal) was skipped as "no data";
  tickers missing from the data source raised `KeyError`.
- Multi-recipient emails were sent to a single malformed address; the `From` header
  was missing; empty reports were emailed every cycle; SMTP servers on port 587 (or
  any port other than 465) were unreachable because every port was treated as
  implicit TLS instead of STARTTLS.
- Slack HTTP errors were handled with `assert`; YAML loading errors were swallowed;
  missing `.env` values surfaced as bare `KeyError`s; `config` printed the SMTP
  password.
- A network error killed the polling loop and left the bot marked as running.

### Removed
- `state.json` and the `stop` command (replaced by signal handling).
- The vendored copy of IB's `ibapi` client, which nothing imported.

## [0.1.0] - 2021

Original course project: Yahoo Finance polling with Slack and email alerts.
