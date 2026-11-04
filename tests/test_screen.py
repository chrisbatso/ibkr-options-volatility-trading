import math
import sys
from collections import Counter

import pytest
import yaml

from market_watcher import screen as screen_module
from market_watcher.common import STRATEGIES, get_target_stocks
from market_watcher.screen import (
    REQUIRED_CLOSES,
    History,
    Ranked,
    ScreenResult,
    average_dollar_volume,
    fetch_history,
    histories_from_frame,
    normalize_ticker,
    parse_universe,
    run,
    screen,
    volatilities,
    weighted_volatility,
    write_report,
    write_target_stocks,
)

LONG = STRATEGIES.LONG_STRADDLE.value
SHORT = STRATEGIES.SHORT_STRADDLE.value


def alternating(amplitude, days=30, base=100.0):
    """Closes that go up and down by ``amplitude`` every day: the bigger the amplitude, the higher the volatility."""
    return [base * (1 + amplitude * (1 if day % 2 else -1)) for day in range(days)]


def history(amplitude, days=30, base=100.0, volume=1_000_000.0):
    return History(closes=alternating(amplitude, days, base), volumes=[volume] * days)


# --- Volatility ------------------------------------------------------------------------------------------------------


def test_constant_prices_have_zero_volatility():
    assert volatilities([100.0] * REQUIRED_CLOSES) == (0.0, 0.0, 0.0)


def test_a_single_recent_shock_weighs_more_in_shorter_windows():
    closes = [100.0] * (REQUIRED_CLOSES - 1) + [110.0]

    vol_5, vol_10, vol_21 = volatilities(closes)

    assert vol_5 > vol_10 > vol_21 > 0


def test_larger_daily_swings_mean_higher_volatility():
    small, large = volatilities(alternating(0.01)), volatilities(alternating(0.05))

    assert all(s < lg for s, lg in zip(small, large, strict=True))


@pytest.mark.parametrize(
    "closes",
    [
        [100.0] * (REQUIRED_CLOSES - 1),  # too short
        [100.0] * (REQUIRED_CLOSES - 1) + [math.nan],  # a missing close
        [100.0] * (REQUIRED_CLOSES - 1) + [0.0],  # a non-positive close
    ],
)
def test_insufficient_or_invalid_history_gives_no_volatility(closes):
    assert volatilities(closes) is None


def test_weighted_volatility_applies_the_readme_weights():
    assert weighted_volatility((0.01, 0.02, 0.03)) == pytest.approx(0.5 * 0.01 + 0.3 * 0.02 + 0.2 * 0.03)


def test_average_dollar_volume_uses_the_last_month_and_tolerates_missing_data():
    closes = [1.0] * 10 + [10.0] * 21
    volumes = [999.0] * 10 + [1_000.0] * 20 + [math.nan]

    assert average_dollar_volume(History(closes, volumes)) == pytest.approx(10_000.0)
    assert average_dollar_volume(History(closes, [])) == 0.0


# --- Screen ----------------------------------------------------------------------------------------------------------


def test_screen_ranks_by_weighted_volatility_and_splits_into_long_and_short():
    histories = {f"T{amplitude}": history(amplitude / 100) for amplitude in (1, 2, 3, 4, 5, 6)}

    result = screen(histories, top=2)

    assert [item.ticker for item in result.ranked] == ["T6", "T5", "T4", "T3", "T2", "T1"]
    assert [item.ticker for item in result.long_straddle] == ["T6", "T5"]
    assert [item.ticker for item in result.short_straddle] == ["T1", "T2"]  # least volatile first
    assert result.skipped == Counter()


def test_screen_applies_the_liquidity_filters_and_counts_what_it_skips():
    histories = {
        "GOOD": history(0.02),
        "PENNY": history(0.02, base=2.0),
        "ILLIQUID": history(0.02, volume=100.0),
        "YOUNG": history(0.02, days=REQUIRED_CLOSES - 1),
        "DEAD": History(closes=[math.nan] * 30, volumes=[0.0] * 30),
    }

    result = screen(histories, top=5, min_price=5.0, min_dollar_volume=5_000_000.0)

    assert [item.ticker for item in result.ranked] == ["GOOD"]
    assert result.skipped == Counter(
        {
            "last close below 5": 1,
            "average dollar volume below 5,000,000": 1,
            f"fewer than {REQUIRED_CLOSES} valid closes": 2,
        }
    )


def test_short_list_never_overlaps_the_long_list_when_few_stocks_qualify():
    result = screen({f"T{a}": history(a / 100) for a in (1, 2, 3)}, top=2, min_dollar_volume=0)

    assert [item.ticker for item in result.long_straddle] == ["T3", "T2"]
    assert [item.ticker for item in result.short_straddle] == ["T1"]


def test_ties_are_broken_alphabetically():
    result = screen({"B": history(0.02), "A": history(0.02)}, top=1, min_dollar_volume=0)

    assert [item.ticker for item in result.ranked] == ["A", "B"]


# --- Universe --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aapl", "AAPL"),
        (" BRK.B ", "BRK-B"),
        ("BRK/B", "BRK-B"),
        ('"KO"', "KO"),
        ("-", None),
        ("", None),
        ("Cash USD", None),
    ],
)
def test_normalize_ticker(raw, expected):
    assert normalize_ticker(raw) == expected


ISHARES_EXPORT = """\
iShares Russell 3000 ETF
Fund Holdings as of,"Oct 31, 2026"
Inception Date,"May 22, 2000"
\xa0
Ticker,Name,Sector,Asset Class,Market Value,Weight (%)
"AAPL","APPLE INC","Information Technology","Equity","1,000.00","5.00"
"BRK.B","BERKSHIRE HATHAWAY INC CLASS B","Financials","Equity","500.00","2.50"
"XTSLA","BLK CSH FND TREASURY SL AGENCY","Cash and/or Derivatives","Money Market","10.00","0.05"
"AAPL","APPLE INC","Information Technology","Equity","1.00","0.00"
\xa0
"The content contained herein is owned or licensed by BlackRock and/or its third-party information providers."
"""


def test_parse_universe_handles_an_ishares_holdings_export():
    assert parse_universe(ISHARES_EXPORT) == ["AAPL", "BRK-B"]


def test_parse_universe_handles_a_plain_list_and_a_symbol_column():
    assert parse_universe("aapl\nMSFT\n\nbrk.b\nMSFT\n") == ["AAPL", "MSFT", "BRK-B"]
    assert parse_universe("Symbol,Name\nKO,Coca-Cola\nPEP,PepsiCo\n") == ["KO", "PEP"]


# --- Prices ----------------------------------------------------------------------------------------------------------


def test_histories_from_a_grouped_yfinance_frame_drops_days_without_a_close():
    pd = pytest.importorskip("pandas")
    index = pd.date_range("2026-10-01", periods=3, freq="B")
    columns = pd.MultiIndex.from_product([["AAPL", "DEAD"], ["Close", "Volume"]])
    data = pd.DataFrame(
        [
            [100.0, 1_000.0, math.nan, math.nan],
            [math.nan, math.nan, math.nan, math.nan],
            [102.0, math.nan, math.nan, math.nan],
        ],
        index=index,
        columns=columns,
    )

    histories = histories_from_frame(data, ["AAPL", "DEAD", "MISSING"])

    assert histories == {"AAPL": History(closes=[100.0, 102.0], volumes=[1_000.0, 0.0])}


def test_fetch_history_explains_the_missing_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", None)  # makes `import yfinance` raise ImportError

    with pytest.raises(RuntimeError, match=r'pip install "market_watcher\[research\]"'):
        fetch_history(["AAPL"])


# --- Output ----------------------------------------------------------------------------------------------------------


def ranked(ticker, vol):
    return Ranked(ticker, vol, (vol, vol, vol), 50.0, 10_000_000.0)


def test_write_target_stocks_produces_a_loadable_file_with_quoted_tickers(tmp_path):
    result = ScreenResult(ranked=[ranked("ZZ", 0.05), ranked("ON", 0.04), ranked("AB", 0.01)], skipped=Counter(), top=1)
    path = tmp_path / "target_stocks.yaml"

    write_target_stocks(path, result, universe="test", parameters="top 1", generated_on="2026-11-04")

    text = path.read_text()
    assert text.startswith("# Generated by `market_watcher_cli screen` on 2026-11-04.")
    assert get_target_stocks(path) == {"ZZ": {"strategy": LONG}, "AB": {"strategy": SHORT}}


def test_write_target_stocks_keeps_the_ticker_on_a_string():
    assert yaml.safe_load(yaml.safe_dump({"ON": {"strategy": LONG}})) == {"ON": {"strategy": LONG}}


def test_write_report_lists_every_eligible_stock_with_its_selection(tmp_path):
    result = ScreenResult(
        ranked=[ranked("HI", 0.05), ranked("MID", 0.03), ranked("LO", 0.01)], skipped=Counter(), top=1
    )
    path = tmp_path / "report.csv"

    write_report(path, result)

    rows = [line.split(",") for line in path.read_text().splitlines()]
    assert rows[0] == [
        "ticker",
        "weighted_volatility",
        "vol_5d",
        "vol_10d",
        "vol_21d",
        "last_close",
        "avg_dollar_volume",
        "strategy",
    ]
    assert [(row[0], row[-1]) for row in rows[1:]] == [("HI", LONG), ("MID", ""), ("LO", SHORT)]


# --- End to end with fakes -------------------------------------------------------------------------------------------


def test_run_writes_the_target_stocks_and_the_report(tmp_path):
    universe = ["T1", "T2", "T3", "T4", "GONE"]
    histories = {f"T{a}": history(a / 100) for a in (1, 2, 3, 4)}  # GONE: no price history at all
    output, report = tmp_path / "stocks.yaml", tmp_path / "report.csv"

    result = run(
        universe="universe.txt",
        top=1,
        min_dollar_volume=0,
        output=output,
        report=report,
        load=lambda source: universe,
        fetch=lambda tickers: histories,
    )

    assert (result.universe_size, result.fetched, len(result.ranked)) == (5, 4, 4)
    assert get_target_stocks(output) == {"T4": {"strategy": LONG}, "T1": {"strategy": SHORT}}
    assert report.read_text().count("\n") == 5  # header + 4 stocks


def test_run_refuses_to_write_an_empty_file(tmp_path):
    with pytest.raises(ValueError, match="No stock passed"):
        run(universe="x", output=tmp_path / "out.yaml", load=lambda source: ["T1"], fetch=lambda tickers: {})


def test_run_uses_the_module_level_loader_and_fetcher_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(screen_module, "load_universe", lambda source: ["T1", "T2"])
    monkeypatch.setattr(screen_module, "fetch_history", lambda tickers: {"T1": history(0.01), "T2": history(0.03)})

    result = run(top=1, min_dollar_volume=0, output=tmp_path / "out.yaml")

    assert [item.ticker for item in result.long_straddle] == ["T2"]
