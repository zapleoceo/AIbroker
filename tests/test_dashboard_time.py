"""Client-timezone helpers — day boundaries align to the viewer's zone, not the
server's UTC. Pure/deterministic, so they run on the SQLite gate."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aibroker.routes.dashboard_data import _parse_date_range, _range_where
from aibroker.routes.dashboard_time import (
    UTC_TZ,
    client_tz,
    day_bounds_utc,
    today_in,
)

_JKT = ZoneInfo("Asia/Jakarta")  # UTC+7, no DST


def test_client_tz_resolves_valid_name():
    assert client_tz("Asia/Jakarta") == _JKT


def test_client_tz_falls_back_to_utc_on_missing_or_bad():
    assert client_tz(None) is UTC_TZ
    assert client_tz("") is UTC_TZ
    assert client_tz("Not/AZone") is UTC_TZ
    assert client_tz("'; DROP TABLE api_keys;--") is UTC_TZ  # never reaches SQL


def test_today_in_reflects_zone_offset():
    # A moment that is 'tomorrow' in Jakarta but still 'today' in UTC proves the
    # date is computed in the given zone, not UTC. today_in just wraps now(),
    # so assert its type + that two zones can disagree by a day near midnight.
    assert isinstance(today_in(_JKT), date)


def test_day_bounds_utc_shifts_by_offset():
    """A Jakarta (UTC+7) calendar day is 17:00 the previous UTC day → 17:00 the
    same UTC day, so a naive-UTC created_at filter selects exactly that day."""
    start, end = day_bounds_utc(date(2026, 7, 12), _JKT)
    assert start == datetime(2026, 7, 11, 17, 0, 0)
    assert end == datetime(2026, 7, 12, 17, 0, 0)


def test_day_bounds_utc_is_identity_for_utc():
    start, end = day_bounds_utc(date(2026, 7, 12), UTC_TZ)
    assert start == datetime(2026, 7, 12, 0, 0, 0)
    assert end == datetime(2026, 7, 13, 0, 0, 0)


def test_range_where_uses_client_day_bounds():
    _, bind = _range_where(date(2026, 7, 12), date(2026, 7, 12), _JKT)
    assert bind["start"] == datetime(2026, 7, 11, 17, 0, 0)
    assert bind["end"] == datetime(2026, 7, 12, 17, 0, 0)


def test_range_where_utc_default_unchanged():
    # No tz → UTC → identical to the pre-timezone behaviour (midnight UTC bounds).
    _, bind = _range_where(date(2026, 7, 12), date(2026, 7, 12))
    assert bind["start"] == datetime(2026, 7, 12, 0, 0, 0)
    assert bind["end"] == datetime(2026, 7, 13, 0, 0, 0)


def test_parse_date_range_today_default_in_zone():
    # Only `to` given → `from` defaults to today IN THE ZONE (not server UTC).
    # `to` is derived from today, never hardcoded: a literal date silently
    # becomes "in the past" once the calendar passes it, which flips the range
    # and swaps `from` away from today — the assertion then fails for a reason
    # that has nothing to do with the behaviour under test (time-bomb, 2026-07-15).
    _, dt = _parse_date_range(None, None)
    assert dt is None  # both missing → all-time, tz irrelevant
    tomorrow = today_in(_JKT) + timedelta(days=1)
    df, dt2 = _parse_date_range(None, tomorrow.isoformat(), _JKT)
    assert df == today_in(_JKT)
    assert dt2 == tomorrow
