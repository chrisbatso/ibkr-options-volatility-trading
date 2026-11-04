import pytest

from market_watcher.common import STRATEGIES, MarketWatcherEngine

LONG = STRATEGIES.LONG_STRADDLE.value
SHORT = STRATEGIES.SHORT_STRADDLE.value

TARGET_STOCKS = {
    "ABEO": {"strategy": LONG},
    "ACRX": {"strategy": LONG},
    "TPCO": {"strategy": SHORT},
    "TTMI": {"strategy": SHORT},
}


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, investment_data):
        self.calls.append(investment_data)


class FailingNotifier:
    def notify(self, investment_data):
        raise RuntimeError("notifier is down")


def make_engine(monkeypatch, pnls, notifiers=None):
    """Engine with thresholds 5 / 0.5 whose data source returns ``pnls``.

    ``pnls`` is a dict, or a list with one dict (or exception to raise) per
    cycle; the last entry is repeated for any further cycles.
    """
    cycles = list(pnls) if isinstance(pnls, list) else [pnls]

    def fake_get_daily_pnls(self):
        result = cycles.pop(0) if len(cycles) > 1 else cycles[0]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(MarketWatcherEngine, "get_daily_pnls", fake_get_daily_pnls)
    return MarketWatcherEngine(TARGET_STOCKS, notifiers=notifiers, long_threshold=5, short_threshold=0.5)


def test_reports_stocks_beyond_their_thresholds(monkeypatch):
    engine = make_engine(monkeypatch, {"ABEO": 7.59, "ACRX": 4, "TPCO": 0.3, "TTMI": 0.6})

    assert engine.process_latest_market_movements() == {LONG: {"ABEO": 7.59}, SHORT: {"TPCO": 0.3}}


def test_zero_move_is_a_short_straddle_opportunity(monkeypatch):
    engine = make_engine(monkeypatch, {"ABEO": 7.59, "ACRX": 4, "TPCO": 0.0, "TTMI": 0.6})

    assert engine.process_latest_market_movements()[SHORT] == {"TPCO": 0.0}


def test_tickers_without_data_are_skipped(monkeypatch):
    # ACRX and TTMI are missing from the result altogether, TPCO has no value.
    engine = make_engine(monkeypatch, {"ABEO": 7.59, "TPCO": None})

    assert engine.process_latest_market_movements() == {LONG: {"ABEO": 7.59}, SHORT: {}}


@pytest.mark.parametrize(
    ("strategy", "abs_daily_pnl", "expected"),
    [
        (LONG, 5.01, True),
        (LONG, 5, False),
        (LONG, 4.99, False),
        (SHORT, 0.49, True),
        (SHORT, 0.5, False),
        (SHORT, 0.51, False),
        ("iron condor", 0.0, False),
    ],
)
def test_thresholds_are_strict(monkeypatch, strategy, abs_daily_pnl, expected):
    engine = make_engine(monkeypatch, {})

    assert engine.is_investment_opportunity(strategy, abs_daily_pnl) is expected


def test_notifies_only_when_a_stock_crosses_its_threshold(monkeypatch):
    notifier = RecordingNotifier()
    engine = make_engine(
        monkeypatch,
        [
            {"ABEO": 7.59, "ACRX": 4, "TPCO": 0.3, "TTMI": 0.6},  # ABEO and TPCO cross
            {"ABEO": 7.80, "ACRX": 4, "TPCO": 0.2, "TTMI": 0.6},  # same set: nothing new
            {"ABEO": 4.00, "ACRX": 6, "TPCO": 0.2, "TTMI": 0.6},  # ABEO drops out, ACRX crosses
            {"ABEO": 8.00, "ACRX": 6, "TPCO": 0.2, "TTMI": 0.6},  # ABEO re-enters
        ],
        notifiers=[notifier],
    )

    for _ in range(3):
        engine.process_latest_market_movements()
    current = engine.process_latest_market_movements()

    assert notifier.calls == [
        {LONG: {"ABEO": 7.59}, SHORT: {"TPCO": 0.3}},
        {LONG: {"ACRX": 6}, SHORT: {}},
        {LONG: {"ABEO": 8.00}, SHORT: {}},
    ]
    # The return value still describes every current opportunity, not only the new ones.
    assert current == {LONG: {"ABEO": 8.00, "ACRX": 6}, SHORT: {"TPCO": 0.2}}


def test_a_failing_notifier_does_not_block_the_others(monkeypatch):
    recorder = RecordingNotifier()
    engine = make_engine(
        monkeypatch, {"ABEO": 7.59, "ACRX": 4, "TPCO": 0.3, "TTMI": 0.6}, notifiers=[FailingNotifier(), recorder]
    )

    engine.process_latest_market_movements()

    assert len(recorder.calls) == 1


class FakeContext:
    """Stands in for ``market_watcher.config.context``: runs for a fixed number of cycles without sleeping."""

    def __init__(self, cycles):
        self.config = {"UPDATE_TIMEOUT": "0"}
        self._cycles = cycles

    @property
    def running(self):
        self._cycles -= 1
        return self._cycles >= 0


def test_polling_loop_survives_a_failing_cycle(monkeypatch):
    monkeypatch.setattr("market_watcher.common.context", FakeContext(cycles=2))
    notifier = RecordingNotifier()
    engine = make_engine(
        monkeypatch,
        [RuntimeError("Yahoo Finance is unreachable"), {"ABEO": 7.59, "ACRX": 4, "TPCO": 0.3, "TTMI": 0.6}],
        notifiers=[notifier],
    )

    engine.search_for_investment_opportunities()

    assert notifier.calls == [{LONG: {"ABEO": 7.59}, SHORT: {"TPCO": 0.3}}]
