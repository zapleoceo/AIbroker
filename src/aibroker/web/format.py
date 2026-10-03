"""Pure value formatters shared by the Jinja filters (and unit-testable on
their own). Nothing here touches the DB or the request."""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

from markupsafe import Markup

_DASH = "—"


def money(v: Any, digits: int | None = None) -> str:
    """$-string. Sub-cent amounts keep 4 decimals so a $0.0004 call is not
    shown as $0.00; larger amounts are rounded to cents."""
    if v is None:
        return _DASH
    x = float(v)
    if digits is None:
        digits = 2 if abs(x) >= 1 else 4
    return f"${x:,.{digits}f}"


def num(v: Any) -> str:
    return _DASH if v is None else f"{int(v):,}"


def compact(v: Any) -> str:
    """1.2k / 3.4M — for token counts that would not fit a KPI tile."""
    if v is None:
        return _DASH
    x = float(v)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= limit:
            s = f"{x / limit:.1f}".rstrip("0").rstrip(".")
            return f"{s}{suffix}"
    return f"{int(x)}"


def pct(v: Any, digits: int = 0) -> str:
    return _DASH if v is None else f"{float(v):.{digits}f}%"


def ms(v: Any) -> str:
    """Latency: 340 ms, 1.2 s, 2m 05s."""
    if v is None:
        return _DASH
    x = float(v)
    if x < 1000:
        return f"{int(x)} ms"
    if x < 60_000:
        return f"{x / 1000:.1f} s"
    m, s = divmod(int(x / 1000), 60)
    return f"{m}m {s:02d}s"


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    """'5m ago' for a naive-UTC datetime."""
    if dt is None:
        return _DASH
    now = now or datetime.now(UTC).replace(tzinfo=None)
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return "in " + _span(-secs)
    if secs < 5:
        return "just now"
    return _span(secs) + " ago"


def _span(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def iso_utc(dt: datetime) -> str:
    return dt.replace(microsecond=0, tzinfo=None).isoformat() + "Z"


_TIME_FMT = {"hm": "%H:%M", "mdhm": "%m-%d %H:%M", "mdhms": "%m-%d %H:%M:%S",
             "full": "%Y-%m-%d %H:%M:%S"}


def time_tag(dt: datetime | None, fmt: str = "mdhms") -> Markup:
    """A <time> element the page JS rewrites into the viewer's timezone. The
    server-rendered text is the UTC fallback (JS off); the tooltip keeps UTC."""
    if dt is None:
        return Markup(_DASH)
    return Markup(
        f'<time class="ts" datetime="{iso_utc(dt)}" data-tf="{fmt}" '
        f'title="{iso_utc(dt)}">{escape(dt.strftime(_TIME_FMT[fmt]))}</time>'
    )


def spark(values: list[float] | None, *, w: int = 100, h: int = 28,
          cls: str = "spark") -> Markup:
    """Tiny responsive area+line sparkline. The viewBox is unitless and the
    svg is stretched by CSS (preserveAspectRatio none), so it never overflows
    its tile. A flat/empty series renders a muted baseline."""
    vals = [float(v) for v in (values or [])]
    if len(vals) < 2:
        return Markup(
            f'<svg class="{cls} flat" viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'aria-hidden="true"><line x1="0" y1="{h - 1}" x2="{w}" y2="{h - 1}"/></svg>'
        )
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 2
    step = w / (len(vals) - 1)
    pts = [
        (round(i * step, 2),
         round(h - pad - ((v - lo) / span) * (h - 2 * pad), 2) if hi != lo else h / 2)
        for i, v in enumerate(vals)
    ]
    line = " ".join(f"{x},{y}" for x, y in pts)
    area = f"0,{h} {line} {w},{h}"
    return Markup(
        f'<svg class="{cls}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
        f'<polygon class="area" points="{area}"/>'
        f'<polyline class="line" points="{line}" vector-effect="non-scaling-stroke"/></svg>'
    )


def plural_en(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian plural: 1 / 2-4 / 5-20 (11-14 are 'many')."""
    n_abs = abs(int(n))
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        form = one
    elif 2 <= n_abs % 10 <= 4 and not 12 <= n_abs % 100 <= 14:
        form = few
    else:
        form = many
    return f"{n} {form}"
