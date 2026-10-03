"""Client-timezone helpers for the dashboard. The operator views the dashboard
in their own timezone (sent via the `aib_tz` cookie the dashboard JS sets from
`Intl…timeZone`); these turn that into the UTC datetime bounds every day-based
query needs, so 'today', the selected range, and the per-key quota bars align to
the VIEWER's calendar day, not the server's UTC one.

Pure + DB-portable: the SQL still filters the naive-UTC `created_at` column — only
the computed bounds shift — so there's no `AT TIME ZONE` and the SQLite tests run
unchanged. Single source of truth (DRY) for every day boundary in the dashboard.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC_TZ = ZoneInfo("UTC")


def client_tz(name: str | None) -> ZoneInfo:
    """Resolve an IANA tz name (from the aib_tz cookie) to a ZoneInfo, falling
    back to UTC for a missing / invalid / spoofed value. The name is validated
    by ZoneInfo and never reaches SQL, so a hostile cookie can at most pick a
    real timezone (or get UTC)."""
    if not name:
        return UTC_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC_TZ


def today_in(tz: ZoneInfo) -> date:
    """Today's calendar date in `tz`."""
    return datetime.now(tz).date()


def day_bounds_utc(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) as naive-UTC datetimes spanning the local calendar `day` in
    `tz` — that day's local midnight to the next, expressed in UTC, so a
    naive-UTC `created_at` filter selects exactly the viewer's day. The end is
    the next calendar date's midnight (not start + 24h) to stay correct across a
    DST transition."""
    nxt = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return (start.astimezone(UTC_TZ).replace(tzinfo=None),
            end.astimezone(UTC_TZ).replace(tzinfo=None))
