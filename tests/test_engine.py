import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from market_watcher.common import STRATEGIES, MarketWatcherEngine
from market_watcher.market_data import MarketDataProvider
from market_watcher.market_hours import AlwaysOpen, Session

LONG = STRATEGIES.LONG_STRADDLE.value
SHORT = STRATEGIES.SHORT_STRADDLE.value

TARGET_STOCKS = {
    "ABEO": {"strategy": LONG},
    "ACRX": {"strategy": LONG},
    "TPCO": {"strategy": SHORT},
    "TTMI": {"strategy": SHORT},
}
PNLS = {"ABEO": 7.59, "ACRX": 4, "TPCO": 0.3, "TTMI": 0.6}

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)  # 10:00 New York on a trading day


def session_opened_at(opened):
    return Session(day=opened.date(), open=opened, close=opened + timedelta(hours=6, minutes=30))


class FakeProvider(MarketDataProvider):
    """Returns one entry of ``cycles`` per call (a dict, or an exception to raise); the last entry repeats."""

    name = "fake"

    def __init__(self, cycles):
        self.cycles = list(cycles) if isinstance(cycles, list) else [cycles]
        self.requested = []

    def get_daily_pnls(self, tickers):
        self.requested.append(list(tickers))
        result = self.cycles.pop(0) if len(self.cycles) > 1 else self.cycles[0]
        if isinstance(result, Exception):
            raise result
        return result


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, investment_data):
        self.calls.append(investment_data)


class FailingNotifier:
    def notify(self, investment_data):
        raise RuntimeError("notifier is down")


def make_engine(pnls, notifiers=None, market_hours=None):
    """Engine with thresholds 5 / 0.5 and a 30-minute warm-up, fed by a FakeProvider returning ``pnls``.

    Market hours default to AlwaysOpen so tests do not depend on the wall clock.
    """
    return MarketWatcherEngine(
        TARGET_STOCKS,
        notifiers=notifiers,
        long_threshold=5,
        short_threshold=0.5,
        data_provider=FakeProvider(pnls),
        market_hours=market_hours if market_hours is not None else AlwaysOpen(),
        short_warmup_minutes=30,
    )


# --- Opportunity detection -------------------------------------------------------------------------------------------


def test_requests_every_target_ticker_from_the_provider():
    engine = make_engine(PNLS)

    engine.process_latest_market_movements()

    assert engine.data_provider.requested == [["ABEO", "ACRX", "TPCO", "TTMI"]]


def test_reports_stocks_beyond_their_thresholds():
    engine = make_engine(PNLS)

    assert engine.process_latest_market_movements() == {LONG: {"ABEO": 7.59}, SHORT: {"TPCO": 0.3}}


def test_zero_move_is_a_short_straddle_opportunity():
    engine = make_engine({**PNLS, "TPCO": 0.0})

    assert engine.process_latest_market_movements()[SHORT] == {"TPCO": 0.0}


def test_tickers_without_data_are_skipped():
    # ACRX and TTMI are missing from the result altogether, TPCO has no value.
    engine = make_engine({"ABEO": 7.59, "TPCO": None})

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
def test_thresholds_are_strict(strategy, abs_daily_pnl, expected):
    engine = make_engine({})

    assert engine.is_investment_opportunity(strategy, abs_daily_pnl) is expected


def test_notifies_only_when_a_stock_crosses_its_threshold():
    notifier = RecordingNotifier()
    engine = make_engine(
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


def test_a_failing_notifier_does_not_block_the_others():
    recorder = RecordingNotifier()
    engine = make_engine(PNLS, notifiers=[FailingNotifier(), recorder])

    engine.process_latest_market_movements()

    assert len(recorder.calls) == 1


# --- Market hours ----------------------------------------------------------------------------------------------------


def test_only_long_straddle_is_evaluated_during_the_warm_up():
    engine = make_engine({**PNLS, "TPCO": 0.0})
    session = session_opened_at(NOW - timedelta(minutes=10))

    strategies = engine.active_strategies(NOW, session)
    result = engine.process_latest_market_movements(strategies=strategies)

    assert strategies == {LONG}
    assert result == {LONG: {"ABEO": 7.59}, SHORT: {}}
    # Only the long straddle tickers are fetched during the warm-up.
    assert engine.data_provider.requested == [["ABEO", "ACRX"]]


def test_short_straddle_is_evaluated_once_the_warm_up_is_over():
    engine = make_engine(PNLS)

    assert engine.active_strategies(NOW, session_opened_at(NOW - timedelta(minutes=30))) == {LONG, SHORT}


def test_a_new_session_resets_the_alert_memory():
    notifier = RecordingNotifier()
    engine = make_engine(PNLS, notifiers=[notifier])

    engine.begin_session(session_opened_at(NOW))
    engine.process_latest_market_movements()
    engine.begin_session(session_opened_at(NOW))  # same day: no reset
    engine.process_latest_market_movements()
    engine.begin_session(session_opened_at(NOW + timedelta(days=1)))
    engine.process_latest_market_movements()

    assert len(notifier.calls) == 2


class ClosedMarket:
    """Market hours that are always closed, counting how often the engine asks for the next open."""

    def __init__(self):
        self.next_open_calls = 0

    def current_session(self, now):
        return None

    def next_open(self, now):
        self.next_open_calls += 1
        return now + timedelta(hours=1)


def test_polling_loop_sleeps_without_fetching_while_the_market_is_closed(monkeypatch):
    monkeypatch.setattr("market_watcher.common.context", FakeContext())
    market = ClosedMarket()
    engine = make_engine(PNLS, market_hours=market)
    worker = threading.Thread(target=engine.search_for_investment_opportunities, daemon=True)

    worker.start()
    deadline = time.time() + 5
    while market.next_open_calls == 0 and time.time() < deadline:
        time.sleep(0.01)
    engine.stop()
    worker.join(timeout=5)

    assert not worker.is_alive(), "stop() did not interrupt the sleep until the next open"
    assert market.next_open_calls == 1
    assert engine.data_provider.requested == []


# --- Polling loop ----------------------------------------------------------------------------------------------------


class FakeContext:
    """Stands in for ``market_watcher.config.context`` with the given polling interval."""

    def __init__(self, update_timeout="0"):
        self.config = {"UPDATE_TIMEOUT": update_timeout}


class StopOnNotify(RecordingNotifier):
    """Records the notification, then asks the engine to stop so the polling loop ends."""

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def notify(self, investment_data):
        super().notify(investment_data)
        self.engine.stop()


class EventNotifier:
    """Sets a threading event when notified."""

    def __init__(self, event):
        self.event = event

    def notify(self, investment_data):
        self.event.set()


def test_polling_loop_survives_a_failing_cycle(monkeypatch):
    monkeypatch.setattr("market_watcher.common.context", FakeContext())
    engine = make_engine([RuntimeError("Yahoo Finance is unreachable"), PNLS])
    notifier = StopOnNotify(engine)
    engine.notifiers.append(notifier)

    engine.search_for_investment_opportunities()

    assert notifier.calls == [{LONG: {"ABEO": 7.59}, SHORT: {"TPCO": 0.3}}]
    assert len(engine.data_provider.requested) == 2
    assert engine.stopping


def test_cycle_hooks_run_after_every_cycle_and_a_failing_hook_is_isolated(monkeypatch):
    monkeypatch.setattr("market_watcher.common.context", FakeContext())
    calls = []

    def failing_hook(now, session):
        raise RuntimeError("hook is down")

    def recording_hook(now, session):
        calls.append((now, session))

    engine = make_engine(PNLS)
    engine.cycle_hooks = [failing_hook, recording_hook]
    engine.notifiers.append(StopOnNotify(engine))

    engine.search_for_investment_opportunities()

    [(now, session)] = calls
    assert now.tzinfo is not None and session.day == now.astimezone(session.open.tzinfo).date()


def test_polling_loop_returns_at_once_when_stopped_before_it_starts(monkeypatch):
    """A stop request that arrives during start-up must not be lost."""
    monkeypatch.setattr("market_watcher.common.context", FakeContext())
    engine = make_engine(PNLS)

    engine.stop()
    engine.search_for_investment_opportunities()

    assert engine.data_provider.requested == []


def test_stop_interrupts_the_wait_between_cycles(monkeypatch):
    """stop() from another thread (or a signal handler) must not wait out a long polling interval."""
    monkeypatch.setattr("market_watcher.common.context", FakeContext(update_timeout="3600"))
    first_cycle_done = threading.Event()
    engine = make_engine(PNLS, notifiers=[EventNotifier(first_cycle_done)])
    worker = threading.Thread(target=engine.search_for_investment_opportunities, daemon=True)

    worker.start()
    assert first_cycle_done.wait(timeout=5), "first polling cycle did not run"
    engine.stop()
    worker.join(timeout=5)

    assert not worker.is_alive(), "polling loop did not return promptly after stop()"
