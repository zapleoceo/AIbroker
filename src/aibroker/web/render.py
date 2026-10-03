"""Jinja environment + the one `render()` every dashboard page goes through."""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from aibroker import __version__
from aibroker.web import format as fmt

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# Authenticated, always-fresh admin pages must never be cached (a heuristic
# browser cache once showed a stale key list); static files are versioned.
NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


def _t(en: str, ru: str) -> Markup:
    """Bilingual inline text; the page JS swaps the visible language."""
    return Markup(
        f'<span data-i18n data-en="{escape(en)}" data-ru="{escape(ru)}">{escape(en)}</span>'
    )


def _ph(en: str, ru: str) -> Markup:
    """Bilingual placeholder attributes for an <input>."""
    return Markup(
        f'placeholder="{escape(en)}" data-en-placeholder="{escape(en)}" '
        f'data-ru-placeholder="{escape(ru)}"'
    )


def _ttl(en: str, ru: str) -> Markup:
    """Bilingual tooltip + accessible name."""
    return Markup(
        f'title="{escape(en)}" aria-label="{escape(en)}" data-en-title="{escape(en)}" '
        f'data-ru-title="{escape(ru)}"'
    )


def _tn(n: int, en1: str, enn: str, ru1: str, ru2: str, ru5: str) -> Markup:
    """Counted phrase in both languages with correct plurals: 1 error / 2 errors,
    1 ошибка / 2 ошибки / 5 ошибок."""
    return _t(fmt.plural_en(n, en1, enn), fmt.plural_ru(n, ru1, ru2, ru5))


def _ms_t(v: Any) -> Markup:
    """Duration in both languages (the page JS swaps it): 1.2 s / 1,2 с."""
    return _t(fmt.ms(v), fmt.ms_ru(v))


def _age_t(dt: Any) -> Markup:
    """Bare age in both languages: 36m / 36 мин."""
    return _t(fmt.age(dt), fmt.age(dt, ru=True))


def _asset(path: str) -> str:
    from aibroker.routes.dashboard_assets import ASSETS_VERSION
    return f"/dashboard/static/{path}?v={ASSETS_VERSION}"


def build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        money=fmt.money, num=fmt.num, compact=fmt.compact, pct=fmt.pct, ms=fmt.ms,
        ago=fmt.ago, time_tag=fmt.time_tag, spark=fmt.spark, ms_t=_ms_t, age_t=_age_t,
    )
    env.globals.update(t=_t, tn=_tn, ph=_ph, ttl=_ttl, asset=_asset, version=__version__)
    return env


env = build_env()


def render_html(name: str, **ctx: Any) -> str:
    return env.get_template(name).render(**ctx)


def render(name: str, *, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    return HTMLResponse(render_html(name, **ctx), status_code=status_code, headers=NO_STORE)
