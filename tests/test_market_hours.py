from datetime import date, datetime, timedelta, timezone

import pytest

from market_watcher.market_hours import (
    NEW_YORK,
    AlwaysOpen,
    USMarketHours,
    easter_sunday,
    nyse_early_closes,
    nyse_holidays,
    parse_closures,
)


def ny(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=NEW_YORK)


# --- Calendar rules --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28))],
)
def test_easter_sunday(year, expected):
    assert easter_sunday(year) == expected


def test_nyse_holidays_2025_match_the_published_schedule():
    assert nyse_holidays(2025) == {
        date(2025, 1, 1),  # New Year's Day
        date(2025, 1, 20),  # Martin Luther King Jr. Day
        date(2025, 2, 17),  # Presidents' Day
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 26),  # Memorial Day
        date(2025, 6, 19),  # Juneteenth
        date(2025, 7, 4),  # Independence Day
        date(2025, 9, 1),  # Labor Day
        date(2025, 11, 27),  # Thanksgiving
        date(2025, 12, 25),  # Christmas
    }


def test_nyse_holidays_2026_match_the_published_schedule():
    assert nyse_holidays(2026) == {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),  # Independence Day falls on a Saturday
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }


def test_weekend_holidays_are_observed_on_weekdays_except_a_saturday_new_year():
    holidays_2022 = nyse_holidays(2022)
    assert date(2021, 12, 31) not in holidays_2022 and date(2022, 1, 1) not in holidays_2022  # Saturday: not observed
    assert date(2022, 6, 20) in holidays_2022  # Juneteenth on a Sunday -> Monday
    assert date(2022, 12, 26) in holidays_2022  # Christmas on a Sunday -> Monday

    holidays_2021 = nyse_holidays(2021)
    assert date(2021, 7, 5) in holidays_2021  # Independence Day on a Sunday -> Monday
    assert date(2021, 12, 24) in holidays_2021  # Christmas on a Saturday -> Friday
    assert date(2021, 6, 18) not in holidays_2021  # Juneteenth was first observed in 2022


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2021, {date(2021, 11, 26)}),  # 3 July is a Saturday, 24 December an observed holiday
        (2023, {date(2023, 7, 3), date(2023, 11, 24)}),  # 3 July on a Monday; 24 December a Sunday
        (2025, {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}),
        (2026, {date(2026, 11, 27), date(2026, 12, 24)}),  # 3 July is the observed Independence Day
    ],
)
def test_nyse_early_closes(year, expected):
    assert nyse_early_closes(year) == expected


# --- Sessions --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "is_open"),
    [
        (ny(2026, 11, 2, 9, 29), False),  # Monday, pre-market
        (ny(2026, 11, 2, 9, 30), True),
        (ny(2026, 11, 2, 15, 59), True),
        (ny(2026, 11, 2, 16, 0), False),
        (ny(2026, 11, 7, 12, 0), False),  # Saturday
        (ny(2026, 11, 26, 12, 0), False),  # Thanksgiving
        (ny(2026, 11, 27, 12, 59), True),  # day after Thanksgiving: early close
        (ny(2026, 11, 27, 13, 0), False),
    ],
)
def test_current_session(moment, is_open):
    assert (USMarketHours().current_session(moment) is not None) is is_open


def test_current_session_accepts_instants_in_any_time_zone():
    hours = USMarketHours()
    assert hours.current_session(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)) is not None  # 09:30 EST
    assert hours.current_session(datetime(2026, 11, 2, 14, 29, tzinfo=timezone.utc)) is None
    assert hours.current_session(datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)) is not None  # 09:30 EDT
    assert hours.current_session(datetime(2026, 7, 1, 13, 29, tzinfo=timezone.utc)) is None


def test_session_carries_its_open_and_close_instants():
    session = USMarketHours().current_session(ny(2026, 11, 27, 10, 0))

    assert session.day == date(2026, 11, 27)
    assert session.open == ny(2026, 11, 27, 9, 30)
    assert session.close == ny(2026, 11, 27, 13, 0)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (ny(2026, 11, 2, 8, 0), ny(2026, 11, 2, 9, 30)),  # pre-market: today's open
        (ny(2026, 11, 2, 16, 0), ny(2026, 11, 3, 9, 30)),  # after the close: tomorrow
        (ny(2026, 11, 27, 14, 0), ny(2026, 11, 30, 9, 30)),  # Friday afternoon: Monday
        (ny(2026, 11, 25, 17, 0), ny(2026, 11, 27, 9, 30)),  # Wednesday evening: skips Thanksgiving
    ],
)
def test_next_open(moment, expected):
    assert USMarketHours().next_open(moment) == expected


def test_extra_closures_close_the_market():
    hours = USMarketHours(extra_closures={date(2026, 11, 2)})

    assert hours.current_session(ny(2026, 11, 2, 12, 0)) is None
    assert hours.next_open(ny(2026, 11, 2, 8, 0)) == ny(2026, 11, 3, 9, 30)


def test_next_open_gives_up_when_everything_is_closed():
    closures = {date(2026, 11, 2) + timedelta(days=offset) for offset in range(90)}

    with pytest.raises(ValueError, match="MARKET_HOLIDAYS"):
        USMarketHours(extra_closures=closures).next_open(ny(2026, 11, 2, 8, 0))


def test_parse_closures():
    assert parse_closures(None) == set()
    assert parse_closures("  ") == set()
    assert parse_closures("2026-01-09, 2026-12-24,") == {date(2026, 1, 9), date(2026, 12, 24)}
    with pytest.raises(ValueError, match="'09/01/2026'"):
        parse_closures("09/01/2026")


def test_always_open_is_open_with_the_warm_up_long_over():
    now = ny(2026, 11, 7, 3, 0)  # Saturday night

    session = AlwaysOpen().current_session(now)

    assert session is not None
    assert now - session.open >= timedelta(hours=24)
    assert session.day == date(2026, 11, 7)
