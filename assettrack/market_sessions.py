"""Deterministic US equity market-session calendar.

The calendar models full-day NYSE closures needed for forecast horizons and
daily snapshot keys.  Early closes remain valid sessions and therefore do not
affect session shifting.
"""

from __future__ import annotations

from datetime import date, timedelta


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    current += timedelta(days=(weekday - current.weekday()) % 7)
    return current + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    current -= timedelta(days=(current.weekday() - weekday) % 7)
    return current


def _easter_sunday(year: int) -> date:
    """Gregorian Easter using the Anonymous Gregorian computus."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


_SPECIAL_CLOSURES = frozenset(
    {
        date(2001, 9, 11),
        date(2001, 9, 12),
        date(2001, 9, 13),
        date(2001, 9, 14),
        date(2004, 6, 11),
        date(2007, 1, 2),
        date(2012, 10, 29),
        date(2012, 10, 30),
        date(2018, 12, 5),
    }
)


class NYSESessionCalendar:
    """NYSE full-session calendar for ordinary scheduling and settlement."""

    def holidays(self, year: int) -> frozenset[date]:
        holidays = {
            _observed_fixed_holiday(year, 1, 1),
            _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
            _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
            _easter_sunday(year) - timedelta(days=2),  # Good Friday
            _last_weekday(year, 5, 0),  # Memorial Day
            _observed_fixed_holiday(year, 7, 4),
            _nth_weekday(year, 9, 0, 1),  # Labor Day
            _nth_weekday(year, 11, 3, 4),  # Thanksgiving
            _observed_fixed_holiday(year, 12, 25),
        }
        if year >= 2022:
            holidays.add(_observed_fixed_holiday(year, 6, 19))

        # When next New Year's Day falls on Saturday, the observed closure is
        # Friday Dec 31 of the current year.
        next_new_year = _observed_fixed_holiday(year + 1, 1, 1)
        if next_new_year.year == year:
            holidays.add(next_new_year)
        holidays.update(d for d in _SPECIAL_CLOSURES if d.year == year)
        return frozenset(holidays)

    def is_session(self, session: date) -> bool:
        return session.weekday() < 5 and session not in self.holidays(session.year)

    def shift(self, session: date, sessions: int) -> date:
        if sessions <= 0:
            raise ValueError("sessions must be positive")
        if not self.is_session(session):
            raise ValueError("session must be an NYSE trading session")
        current = session
        remaining = sessions
        while remaining:
            current += timedelta(days=1)
            if self.is_session(current):
                remaining -= 1
        return current

    def latest_session_on_or_before(self, day: date) -> date:
        current = day
        while not self.is_session(current):
            current -= timedelta(days=1)
        return current
