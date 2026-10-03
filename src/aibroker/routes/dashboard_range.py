"""The header range picker: today / 7d / 30d / all / custom.

One `DateRange` value drives every page (KPIs, charts, tables, exports), so
the nav links carry it as a query string and no page invents its own window.
Bounds are the VIEWER's calendar days (see dashboard_time), expressed as
naive-UTC half-open [start, end) on `usage_log.created_at`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aibroker.routes.dashboard_data import _parse_date_range
from aibroker.routes.dashboard_time import UTC_TZ, day_bounds_utc, today_in
from aibroker.services.job_queue import _USAGE_RETENTION_DAYS

PRESETS = ("today", "7d", "30d", "all")
DEFAULT_PRESET = "7d"
# Hourly buckets up to this span, daily beyond (a 30-day chart of 720 hourly
# bars is unreadable on a phone and 30 daily ones are).
_HOURLY_MAX_DAYS = 2


@dataclass(frozen=True)
class DateRange:
    key: str                      # today | 7d | 30d | all | custom
    date_from: date | None
    date_to: date | None
    start: datetime | None        # naive UTC, inclusive; None = all retained
    end: datetime | None          # naive UTC, exclusive
    tz: ZoneInfo = UTC_TZ

    @property
    def is_all(self) -> bool:
        return self.key == "all"

    @property
    def chart_start(self) -> datetime:
        """Left edge for time series — 'all' is the retained window."""
        if self.start is not None:
            return self.start
        return _utc_now() - timedelta(days=_USAGE_RETENTION_DAYS)

    @property
    def chart_end(self) -> datetime:
        return self.end if self.end is not None else _utc_now() + timedelta(hours=1)

    @property
    def bucket(self) -> str:
        days = (self.chart_end - self.chart_start).total_seconds() / 86400
        return "hour" if days <= _HOURLY_MAX_DAYS else "day"

    @property
    def query(self) -> dict[str, str]:
        if self.key in PRESETS:
            return {"range": self.key}
        return {"from": self.date_from.isoformat() if self.date_from else "",
                "to": self.date_to.isoformat() if self.date_to else ""}

    @property
    def qs(self) -> str:
        return urlencode(self.query)

    def previous(self) -> tuple[datetime, datetime] | None:
        """The equally long window right before this one (for KPI deltas);
        None for the open-ended 'all'."""
        if self.start is None or self.end is None:
            return None
        return self.start - (self.end - self.start), self.start

    @property
    def label(self) -> tuple[str, str]:
        if self.key == "today":
            return "today", "сегодня"
        if self.key == "7d":
            return "last 7 days", "последние 7 дней"
        if self.key == "30d":
            return "last 30 days", "последние 30 дней"
        if self.key == "all":
            return (f"all time ({_USAGE_RETENTION_DAYS} d retention)",
                    f"всё время (хранение {_USAGE_RETENTION_DAYS} дн.)")
        text = (f"{self.date_from or '…'} → {self.date_to or '…'}"
                if self.date_from != self.date_to else f"{self.date_from}")
        return text, text


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _bounds(df: date | None, dt: date | None, tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    start = day_bounds_utc(df, tz)[0] if df else None
    end = day_bounds_utc(dt, tz)[1] if dt else None
    return start, end


def _preset_dates(key: str, today: date) -> tuple[date | None, date | None]:
    if key == "today":
        return today, today
    if key == "7d":
        return today - timedelta(days=6), today
    if key == "30d":
        return today - timedelta(days=29), today
    return None, None


def resolve_range(params: Mapping[str, str], tz: ZoneInfo = UTC_TZ) -> DateRange:
    """`?range=today|7d|30d|all` or the legacy/custom `?from=&to=`.

    A custom from/to that happens to equal a preset normalises to that preset,
    so the picker highlights it. No params at all → the default preset."""
    today = today_in(tz)
    has_custom = bool(params.get("from") or params.get("to"))
    if has_custom:
        df, dt = _parse_date_range(params.get("from"), params.get("to"), tz)
        for key in ("today", "7d", "30d"):
            if (df, dt) == _preset_dates(key, today):
                return _make(key, df, dt, tz)
        return _make("custom", df, dt, tz)
    key = params.get("range") or DEFAULT_PRESET
    if key not in PRESETS:
        key = DEFAULT_PRESET
    df, dt = _preset_dates(key, today)
    return _make(key, df, dt, tz)


def _make(key: str, df: date | None, dt: date | None, tz: ZoneInfo) -> DateRange:
    start, end = _bounds(df, dt, tz)
    return DateRange(key, df, dt, start, end, tz)
