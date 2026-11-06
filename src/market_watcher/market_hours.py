"""US equity market sessions: NYSE regular hours, weekends, holidays and early closes.

Everything here is computed from the published NYSE rules, so no calendar data
needs updating from year to year. Unscheduled closures (for example a national
day of mourning) cannot be known in advance: pass them as ``extra_closures``
(the ``MARKET_HOLIDAYS`` setting).

All instants are timezone-aware ``datetime`` objects; callers may pass any zone.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

MONDAY = 0
THURSDAY = 3
SATURDAY = 5
SUNDAY = 6


@dataclass(frozen=True)
class Session:
    """One trading session, with timezone-aware open and close instants."""

    day: date
    open: datetime
    close: datetime


def easter_sunday(year):
    """Gregorian Easter Sunday (Meeus/Jones/Butcher algorithm)."""
    golden = year % 19
    century, year_in_century = divmod(year, 100)
    leap_centuries, century_rem = divmod(century, 4)
    correction = (century + 8) // 25
    lunar_shift = (century - correction + 1) // 3
    epact = (19 * golden + century - leap_centuries - lunar_shift + 15) % 30
    leap_years, year_rem = divmod(year_in_century, 4)
    weekday_shift = (32 + 2 * century_rem + 2 * leap_years - epact - year_rem) % 7
    adjustment = (golden + 11 * epact + 22 * weekday_shift) // 451
    offset = epact + weekday_shift - 7 * adjustment + 114
    return date(year, offset // 31, offset % 31 + 1)


def _nth_weekday(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(holiday):
    """Fixed-date holidays falling on a weekend are observed on the nearest weekday."""
    if holiday.weekday() == SATURDAY:
        return holiday - timedelta(days=1)
    if holiday.weekday() == SUNDAY:
        return holiday + timedelta(days=1)
    return holiday


def nyse_holidays(year):
    """Full-day NYSE closures in ``year``."""
    holidays = set()

    new_year = date(year, 1, 1)
    if new_year.weekday() == SUNDAY:
        holidays.add(new_year + timedelta(days=1))
    elif new_year.weekday() < SATURDAY:
        holidays.add(new_year)
    # A Saturday New Year's Day is not observed on the preceding Friday (NYSE rule), so nothing is added then.

    holidays.add(_nth_weekday(year, 1, MONDAY, 3))  # Martin Luther King Jr. Day
    holidays.add(_nth_weekday(year, 2, MONDAY, 3))  # Presidents' Day
    holidays.add(easter_sunday(year) - timedelta(days=2))  # Good Friday
    holidays.add(_last_weekday(year, 5, MONDAY))  # Memorial Day
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    holidays.add(_observed(date(year, 7, 4)))  # Independence Day
    holidays.add(_nth_weekday(year, 9, MONDAY, 1))  # Labor Day
    holidays.add(_nth_weekday(year, 11, THURSDAY, 4))  # Thanksgiving
    holidays.add(_observed(date(year, 12, 25)))  # Christmas
    return holidays


def nyse_early_closes(year):
    """Days on which the NYSE closes at 13:00 New York time."""
    early = {_nth_weekday(year, 11, THURSDAY, 4) + timedelta(days=1)}  # day after Thanksgiving
    for eve in (date(year, 7, 3), date(year, 12, 24)):
        # On a Friday these are observed holidays (the holiday itself falls on Saturday); weekends are closed anyway.
        if eve.weekday() <= THURSDAY:
            early.add(eve)
    return early


class USMarketHours:
    """NYSE regular session: 09:30-16:00 New York on weekdays, minus holidays, with 13:00 early closes."""

    def __init__(self, extra_closures: Iterable[date] = ()):
        self.extra_closures = frozenset(extra_closures)

    def is_trading_day(self, day):
        return day.weekday() < SATURDAY and day not in nyse_holidays(day.year) and day not in self.extra_closures

    def session_on(self, day):
        """The session held on ``day``, or None if the market is closed that day."""
        if not self.is_trading_day(day):
            return None
        close = EARLY_CLOSE if day in nyse_early_closes(day.year) else REGULAR_CLOSE
        return Session(
            day=day,
            open=datetime.combine(day, REGULAR_OPEN, tzinfo=NEW_YORK),
            close=datetime.combine(day, close, tzinfo=NEW_YORK),
        )

    def current_session(self, now):
        """The session in progress at ``now``, or None while the market is closed."""
        session = self.session_on(now.astimezone(NEW_YORK).date())
        if session is not None and session.open <= now < session.close:
            return session
        return None

    def next_open(self, now):
        """The open of the first session that starts after ``now``."""
        day = now.astimezone(NEW_YORK).date()
        for _ in range(60):
            session = self.session_on(day)
            if session is not None and session.open > now:
                return session.open
            day += timedelta(days=1)
        raise ValueError("No trading session within the next 60 days; check MARKET_HOLIDAYS")


class AlwaysOpen:
    """Disables market-hours gating: the market is always open and the post-open warm-up is always over.

    Sessions are keyed by the New York calendar day, so per-session state still
    resets once a day.
    """

    def current_session(self, now):
        return Session(day=now.astimezone(NEW_YORK).date(), open=now - timedelta(days=1), close=now + timedelta(days=1))

    def next_open(self, now):
        return now


def parse_closures(text):
    """Parses the MARKET_HOLIDAYS setting: comma-separated ISO dates (YYYY-MM-DD); blank means none."""
    closures = set()
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            closures.add(date.fromisoformat(item))
        except ValueError:
            raise ValueError(f"MARKET_HOLIDAYS entry {item!r} is not a date in YYYY-MM-DD format") from None
    return closures
