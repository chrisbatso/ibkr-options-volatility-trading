# Interactive Brokers: Volatility trading with options

[![MarketWatcher Python package](https://github.com/mcf-long-short/ibkr-options-volatility-trading/actions/workflows/python-package.yaml/badge.svg)](https://github.com/mcf-long-short/ibkr-options-volatility-trading/actions/workflows/python-package.yaml)

## Introduction

`Volatility trading` using `equity options` and `long/short straddle` option strategies combined with a `momentum strategy` to profit from a high/low volatility on a daily level in the US equities.

For testing that strategy Interactive Brokers Trader Workstation was used with paper trading account. In order to quickly react on the market moves, simple `market watcher`/`trading bot` was implemented. `MarketWatcher bot` uses data from [Yahoo Finance](https://github.com/JECSand/yahoofinancials) and [Python TWS API](https://tradersacademy.online/trading-courses/python-tws-api).

This repository represents group project work for implementing option trading strategies (course in Financial Derivatives for advanced degree [Masters in Computational Finance, Union University](http://mcf.raf.edu.rs/)).

## Trading strategy

Our trading strategy is based on `volatility trading`. To exploit and profit from both the high and low volatility, we day traded - getting in and out of the positions in the same trading session. Some positions were held overnight as well.

In order to profit on a high or low volatility in the equities we traded `options` using `long straddle` and `short stradle` strategy respectively. For intuition about these strategies we highly recommend watching following videos:

- [Long Straddle](https://www.youtube.com/watch?v=4UlIMmXhjsc)
- [Short Straddle](https://www.youtube.com/watch?v=Lsk9ppb8ffs)

<p align="center">
<img center src="img/option-strategy.png" width="700" >
</p>

## Trading Workflow

Firstly, we started with doing basic market research and finding `historically the most and least volatile stocks` in the past `1 week`, `2 week` and a `1 month` period in the `Russel 3000 index` (in order to capture both the large-cap and the small-cap companies). From those stocks we chose the top 50 stocks with highest and the top 50 lowest historical weighted volatility:
`Weighted volatility = 0.5 * Vol_1_week + 0.3 * Vol_2_week + 0.2 * Vol_1_month`.

That screen is reproducible with `market_watcher_cli screen` (see *Regenerating the target stocks* below). The committed `target_stocks.yaml` is the 2021 output of this research and is stale - several of its tickers have since been delisted - so regenerate it before use.

Using naive expecation for the future volatility based on the historical volatility, combined with the momentum detected from the daily P&L for the target companies, we enter into `long straddle` or `short straddle` positions.

Our MarketWatcher bot was alerting us for all potential investment opportunities. From the provided list of `target stocks`, trading bot was sending us `alerts via Email and Slack`, when target stocks have `reached configured daily P&L threshold`:

- Long straddle - stocks whose absolute daily P&L is `greater` than `LONG_THRESHOLD` (a fraction, e.g. `0.02` = 2%)
- Short straddle - stocks whose absolute daily P&L is `lower` than `SHORT_THRESHOLD` (e.g. `0.005` = 0.5%)

Upon getting alerted by the MarketWatcher bot, we `hand picked` into which long and short straddle positions to enter.

<p align="center">
<img center src="img/trading-workflow.png" width="700" >
</p>

### Future improvements

Daily P&L can now be read from IBKR TWS as well as from Yahoo Finance (see *Market data providers* below). Order placement on TWS is available as an experimental, off-by-default feature (see *Automated order placement* below); the remaining step towards a fully automated strategy is position management (exits, sizing, risk).

## How to use MarketWatcher trading bot?

After cloning the repo, copy `.env.template` to `.env` and populate it with your settings: `polling interval`, `market hours`, `market data provider`, `daily P&L threshold values`, `which notifiers to enable`, `email settings`, `slack channel webhook links`, ...

Create `.yaml file with target stocks` (ticker and long/short straddle strategy indicator for that ticker). MarketWatcher engine listens to real-time ticker data feed and notifies you when there is a `potential opportunity for entering into long or short straddle options position`, with that stock as an underlying.

You can control MarketWatcher trading bot using simple `CLI`. After bot is started it will send us an email/slack messages for each investment opportunity it finds.

Each ticker is reported once, when its daily P&L crosses the configured threshold; it is reported again only after it has moved back inside the threshold and crossed it again. If a polling cycle fails (for example Yahoo Finance is unreachable) the error is logged and the bot retries on the next cycle. The bot runs in the foreground: stop it with `Ctrl-C` (or `SIGTERM`, which is what `docker stop` sends). It finishes the current polling cycle and exits cleanly; a second `Ctrl-C` exits immediately.

The bot polls only during the NYSE regular session (09:30-16:00 New York time, Monday to Friday), skipping NYSE holidays and the 13:00 early closes (the day after Thanksgiving, and 3 July / 24 December when they fall on a Monday-Thursday); outside the session it sleeps until the next open. Because every stock shows a ~0% move right after the open, short straddle alerts are suppressed for the first `SHORT_STRADDLE_WARMUP_MINUTES` (default 30) of each session - long straddle alerts are not. The alert memory resets at each open, so a stock that qualified yesterday is reported again today. The built-in calendar is computed from the NYSE holiday rules; unscheduled closures (for example a national day of mourning) must be listed in `MARKET_HOLIDAYS` (comma-separated `YYYY-MM-DD`). Set `MARKET_HOURS_ENABLED=false` to poll around the clock.

CLI commands:

```bash
market_watcher_cli --help
market_watcher_cli start
market_watcher_cli doctor
market_watcher_cli config
market_watcher_cli test-slack
market_watcher_cli screen
```

Run `market_watcher_cli doctor` first: it parses every setting, loads the target stocks, reports whether the market is open, fetches a few quotes through the configured provider, logs in to the SMTP server, and - with `TRADING_ENABLED=true` - connects to TWS read-only to check the account type and look up an option chain. `--send-test-messages` also posts to Slack and sends a test email. `market_watcher_cli config` prints the whole effective configuration with secrets masked. Email uses implicit TLS on `SMTP_PORT=465` and STARTTLS on any other port (587). Set `LOG_FILE` to keep a rotating log for unattended runs.

Example CLI output upon starting MarketWatcher engine:

```
         ______              _              _  _  _                 _
        |  ___ \            | |         _  | || || |      _        | |
        | | _ | | ____  ____| |  _ ____| |_| || || | ____| |_  ____| | _   ____  ____
        | || || |/ _  |/ ___) | / ) _  )  _) ||_|| |/ _  |  _)/ ___) || \ / _  )/ ___)
        | || || ( ( | | |   | |< ( (/ /| |_| |___| ( ( | | |_( (___| | | ( (/ /| |
        |_||_||_|\_||_|_|   |_| \_)____)\___)______|\_||_|\___)____)_| |_|\____)_|

        MarketWatcher tool for finding investiments opportunities on Interacive Brokers
        for volatility trading on equity market using long and short options strategy.

        version: v0.1.0




        Starting MarketWatcher...
        Reading target stocks from file: src/market_watcher/research/target_stocks.yaml
        Instantiating email notifier...
        Instantiating slack notifier...
        MarketWatcher started. Press Ctrl-C to stop.
```

You can run cli tool from:

- Docker container (using docker-compose)
- Python virtual environment

### 1. Running in Docker

The container reads its configuration from your local `.env` file (passed in through `env_file` in `docker-compose.yaml`), so create that file first.

Build the image and run the CLI:

```bash
docker compose build
docker compose run --rm mw market_watcher_cli start
```

Stop it with `Ctrl-C`. If you run it detached, `docker stop` sends `SIGTERM` and the bot shuts down cleanly after its current cycle.

To use the `ibkr` provider from the container, set `IBKR_HOST=host.docker.internal` (`docker-compose.yaml` maps that name to the host) and allow that address in the TWS API settings instead of "localhost only".

### 2. Running in a virtual environment

Requires Python 3.10 or newer (CI runs 3.10 to 3.14). Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install MarketWatcher in editable mode (the `dev` extra adds `pytest` and `ruff`):

```bash
pip install -e ".[dev]"

market_watcher_cli --help
```

Run the linter and the tests:

```bash
ruff check . && ruff format --check .
pytest
```

### Regenerating the target stocks

`market_watcher_cli screen` reproduces the research step. It downloads the iShares Russell 3000 ETF (IWV) holdings as the universe, fetches three months of daily split-adjusted prices from Yahoo Finance, computes the volatility (standard deviation of daily log returns) over the last 5, 10 and 21 trading days, combines them as `0.5 * vol_5d + 0.3 * vol_10d + 0.2 * vol_21d`, and writes the 50 most volatile stocks as long straddle candidates and the 50 least volatile as short straddle candidates to `src/market_watcher/research/target_stocks.yaml`. Stocks below $5 or with less than $5M average daily dollar volume are skipped (penny stocks have noisy volatility and rarely have tradable options); tune that with `--min-price` / `--min-dollar-volume`, and the list size with `--top`.

Two more filters guard the ends of the ranking, where the artifacts live. Stocks whose weighted volatility is below 0.5% are skipped: a stock that flat is almost always pinned to a pending cash takeover, which leaves no premium to sell and a gap if the deal breaks (`--min-volatility 0` disables this). And only stocks with listed options are selected, since a straddle cannot be opened otherwise: candidates are checked from either end of the ranking, one Yahoo Finance request each, until both lists are full (`--no-require-options` skips the check). The summary reports how many stocks each filter removed.

```bash
market_watcher_cli screen --report screen_report.csv
```

`--report` writes every screened stock with its volatilities for inspection.

The universe is the iShares Russell 3000 ETF (IWV) holdings by default. iShares does not always serve that file to scripts: the download comes back as an HTML page, which the screen reports as such instead of treating it as a universe. When that happens, either save the holdings CSV from the [IWV product page](https://www.ishares.com/us/products/239714/ishares-russell-3000-etf) in your browser and pass it with `--universe path/to/IWV_holdings.csv`, or run `--universe nasdaq`, which screens every common stock listed on a US exchange from the [Nasdaq Trader symbol directory](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs) (ETFs, warrants, rights, units, preferreds, notes and funds are excluded; about twice the size of the Russell 3000, so the price download takes longer). Any file with one ticker per line, or a CSV with a `Ticker` or `Symbol` column, also works as a universe.

### Market data providers

The engine reads daily P&L through a small `MarketDataProvider` interface (`src/market_watcher/market_data.py`): `get_daily_pnls(tickers)` returns each ticker's daily percent change as a fraction (`0.02` = 2%), or `None` when no figure is available. The provider is selected with `MARKET_DATA_PROVIDER` in `.env`:

| Name | Source |
|---|---|
| `yfinance` (default) | Yahoo Finance through [`yfinance`](https://github.com/ranaroussi/yfinance): the whole universe in one batched download per cycle. During a session a ticker has no figure until it has a bar dated today, so yesterday's move is not mistaken for today's at the open |
| `yahoofinancials` | Yahoo Finance through [`yahoofinancials`](https://github.com/JECSand/yahoofinancials): one request per ticker (kept as a fallback). Its quotes carry no date, so it cannot make that check |
| `ibkr` | Interactive Brokers TWS / IB Gateway, through the [`ib_async`](https://github.com/ib-api-reloaded/ib_async) package (`pip install -e ".[ibkr]"`; see *IBKR requirements* below) |

To add a source (for example IBKR TWS), subclass `MarketDataProvider`, give it a `name`, implement `get_daily_pnls`, and register the class in `PROVIDERS`.

### Slack requirements

In order to receive the investment opportunities from MarketWatcher bot as [Slack](https://slack.com/intl/en-rs/) messages, you must first create a new Slack app in your workspace and enable [Incoming-Webhooks](https://api.slack.com/messaging/webhooks). We expect to have two separate channels - one for receiving opportunities for a long straddle and the other for the short straddle strategies (see image bellow). Links for Slack webhooks need to be put in the `.env` file.

<p align="center">
<img center src="img/slack-example.png" width="400" >
</p>

### IBKR requirements

To read the daily P&L from Interactive Brokers instead of Yahoo Finance:

1. Install the optional dependency: `pip install -e ".[ibkr]"` (the Docker image includes it). It uses [`ib_async`](https://github.com/ib-api-reloaded/ib_async), which speaks the TWS protocol itself, so IB's `ibapi` package is not needed.
2. Run [Trader Workstation](https://www.interactivebrokers.com/en/index.php?f=14099#tws-software) or [IB Gateway](https://www.interactivebrokers.com/en/index.php?f=16457) and enable the API (`File -> Global Configuration -> API -> Settings`):
   - Enable ActiveX and Socket Clients
   - Read-Only API: keep it enabled unless you turn on *Automated order placement* (below), which needs it off
   - Socket port: 7497 (TWS paper) / 7496 (TWS live) / 4002 (IB Gateway paper) / 4001 (IB Gateway live)
   - Allow connections from localhost only, unless MarketWatcher runs in Docker (see *Running in Docker*)
3. Set `MARKET_DATA_PROVIDER=ibkr` and the `IBKR_*` settings in `.env`.

The bot takes one market data snapshot per target stock and computes the daily change as `(price - previous close) / previous close`, using the last trade or, before the first trade of the day, the bid/ask midpoint. Without a market data subscription IB serves delayed (15-20 minute) data: `IBKR_MARKET_DATA_TYPE=3` (the default) accepts that, `1` insists on live data. Tickers IB cannot resolve as US stocks are skipped with a warning.

### Automated order placement (experimental)

With `TRADING_ENABLED=true`, every new opportunity also becomes an order on Interactive Brokers: an at-the-money straddle on the nearest expiry at least `STRADDLE_MIN_DAYS_TO_EXPIRY` days out, placed as a single combo limit order (so both legs fill together) at the net mid price plus or minus `STRADDLE_LIMIT_BUFFER_PCT` - bought for a long straddle, sold for a short straddle. The trader receives exactly what Slack and email receive, so it acts on threshold crossings only.

The defaults are deliberately cautious, and every one of them is a setting in `.env`:

- orders are **staged in TWS, not transmitted** (`IBKR_TRANSMIT_ORDERS=false`): they appear in TWS and you click Transmit, which keeps the hand-picking of the original workflow;
- **paper accounts only** (`TRADING_ALLOW_LIVE=false`): every account id must start with `DU`;
- **long straddles only** (`TRADING_STRATEGIES=long straddle`): short straddles are naked options with unlimited risk and need margin approval;
- one contract per order; at most `STRADDLE_MAX_PREMIUM` (default $1,000) paid for a long straddle and `STRADDLE_MAX_NOTIONAL` (default $20,000) of underlying for a short one; at most `TRADING_MAX_ORDERS_PER_DAY` (default 3) new positions a day and one per ticker;
- **wide spreads are refused** (`STRADDLE_MAX_SPREAD_PCT`, default 6% of mid): a refused stock uses no slot, so the next one is tried;
- `TRADING_DRY_RUN=true` builds and logs the orders without sending anything - start there.

**A backtest found these rules lose money.** Buying these straddles lost about 20% of premium per trade after costs from June 2023 to September 2026, with no edge before costs and no profitable month. Selling them on the short rules, which are off by default, lost about 33%, also with no profitable month. The spread guard reduces the long side's loss but does not remove it. The method, the limits (intraday quotes were not available) and the changes tested are in [research/backtest/RESULTS.md](research/backtest/RESULTS.md). Set `TRADING_JOURNAL` to record every order, fill and close with its quotes, so that a run can be measured afterwards.

Once a straddle is on, the bot manages it on every polling cycle. It tracks whether the entry filled (a staged order counts only after you transmit it), values the position at the legs' bid/ask midpoints and closes it with the opposite combo order on a take-profit (`TRADING_TAKE_PROFIT_PCT`, default 50%), a stop-loss (`TRADING_STOP_LOSS_PCT`, default 50%), or `TRADING_EXIT_MINUTES_BEFORE_CLOSE` (default 15) minutes before the session close unless `TRADING_HOLD_OVERNIGHT=true`; unfilled entries are cancelled at that point. Take-profit exits are priced at mid minus the buffer; stop-loss and end-of-day exits cross the spread so they fill. An exit that does not fill keeps working: a take-profit waits for its price until another rule fires, and every other exit follows the market on each cycle, modified in place so there is never a second live exit (in staged mode each new price needs another click in TWS). A straddle whose exit still expired at the close is closed in the next session, unless `TRADING_HOLD_OVERNIGHT=true`. An entry still partly filled at the next cycle has the rest cancelled, and what it bought is managed as a smaller position. Rejected orders are announced: a rejected entry is dropped, a rejected exit retried. Portfolio rails: at most `TRADING_MAX_OPEN_POSITIONS` straddles at once, and once the day's realized plus unrealized loss reaches `TRADING_MAX_DAILY_LOSS` (default $500), counting positions whose exit is still working, everything is closed and nothing new is opened that day. `STRADDLE_RISK_PER_TRADE` sizes long straddles by premium at risk instead of a fixed contract count.

After a restart or a lost connection, before it places or manages anything, the bot reconciles its book with IB: fills, cancellations and closes that happened meanwhile are applied from the day's executions (tagged `MarketWatcher` / `MarketWatcher-exit`), open orders and positions, and the straddles, working entries and working exits of an earlier run are adopted. The day's limits are rebuilt from the same source, so a restart cannot reset them: the order cap counts today's filled and working entries (entries that never filled and are gone leave no trace), realized P&L comes from today's executions, and the loss halt survives through that P&L and through its exits, tagged `MarketWatcher-exit-halt`. Straddles opened on earlier days (overnight holds) are the only ones that have to be managed by hand after a restart. In dry-run mode the exits are rehearsed too, over a read-only connection. Every trade event - order placed, entry filled, exit placed, straddle closed with its P&L, daily loss limit hit - and an end-of-day summary are sent through the enabled Slack / email notifiers; `SLACK_TRADING_WEBHOOK` routes them to their own channel.

Requirements: the `ibkr` extra (`pip install -e ".[ibkr]"`), options trading permissions on the account, the TWS **Read-Only API** setting turned off (the trader uses its own connection, `IBKR_TRADING_CLIENT_ID`), and option market data (delayed is fine for paper trading). This feature has never run against a real TWS, only against a simulated one and `ib_async`'s own order handling with the socket cut: run it in dry-run mode, then staged mode, on a paper account before anything else.
