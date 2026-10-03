"""Public self-signup: POST /v1/signup mints a restricted project + key.

A signed-up project can only spend nothing: `daily_cost_cap_usd = 0` (cost_guard
reads 0 as "free providers only" - a paid call's estimate always exceeds it; it
is NULL that means unlimited) and a lifetime `total_request_cap`. The owner
raises either limit in the dashboard. Abuse limits (all settings): the
SIGNUP_ENABLED kill switch, SIGNUP_PER_IP_PER_DAY and SIGNUP_PER_DAY over a
rolling 24h, counted from the `projects` rows themselves (`self_signup`,
`signup_ip`, `created_at`) so no extra table can drift.

The count-then-insert is not serialised across workers: a burst can overshoot a
limit by a request or two. The per-project caps, not this, bound the spend.
"""
from __future__ import annotations

import html
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.telemetry import alert, audit

log = logging.getLogger(__name__)

_MAX_SLUG = 60
_NAME_RETRIES = 5
_FIELD_MAX = 300


class SignupDisabled(Exception):
    """SIGNUP_ENABLED is off."""


class SignupRateLimited(Exception):
    """A per-IP or global daily signup limit is reached; `scope` is "ip" or "global"."""

    def __init__(self, scope: str, limit: int):
        self.scope = scope
        self.limit = limit
        super().__init__(f"{scope} signup limit reached ({limit}/day)")


class SignupNameInvalid(ValueError):
    """The requested name has no usable characters (or no free name was found)."""


@dataclass
class SignupResult:
    project_id: int
    name: str
    project_key: str
    scopes: list[str]
    daily_cost_cap_usd: float
    total_request_cap: int


def slugify_name(raw: str) -> str:
    """Lowercase `[a-z][a-z0-9_-]*` slug (the admin API's project-name shape),
    at most 60 chars; SignupNameInvalid when nothing usable is left."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-_")
    slug = re.sub(r"^[^a-z]+", "", slug)[:_MAX_SLUG].rstrip("-_")
    if len(slug) < 2:
        raise SignupNameInvalid("name must contain at least 2 letters/digits (a-z, 0-9)")
    return slug


def _clean(v: str | None) -> str:
    return " ".join((v or "").split())[:_FIELD_MAX]


def _notes(contact: str, purpose: str, ip: str, user_agent: str) -> str:
    return (f"self-signup {datetime.now(UTC):%Y-%m-%d %H:%M}Z | contact: {contact or '-'} | "
            f"purpose: {purpose or '-'} | ip: {ip or '-'} | ua: {_clean(user_agent) or '-'}")


async def _check_limits(ip: str) -> None:
    s = get_settings()
    if not s.SIGNUP_ENABLED:
        raise SignupDisabled()
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    base = select(func.count()).select_from(ProjectRow).where(
        ProjectRow.self_signup.is_(True), ProjectRow.created_at >= since)
    async with get_session() as sess:
        total = (await sess.execute(base)).scalar_one()
        from_ip = 0
        if ip:
            from_ip = (await sess.execute(base.where(ProjectRow.signup_ip == ip))).scalar_one()
    if total >= s.SIGNUP_PER_DAY:
        raise SignupRateLimited("global", s.SIGNUP_PER_DAY)
    if ip and from_ip >= s.SIGNUP_PER_IP_PER_DAY:
        raise SignupRateLimited("ip", s.SIGNUP_PER_IP_PER_DAY)


async def _insert_unique(slug: str, make_row: Callable[[str], ProjectRow]) -> ProjectRow:
    """Insert under `slug`, appending `-<4 hex>` on a name clash (the unique index
    is the arbiter, so two racing signups cannot both win a name)."""
    name = slug
    for _ in range(_NAME_RETRIES):
        try:
            async with get_session() as sess:
                row = make_row(name)
                sess.add(row)
                await sess.flush()
            return row
        except IntegrityError:
            name = f"{slug[:_MAX_SLUG - 5]}-{secrets.token_hex(2)}"
    raise SignupNameInvalid("could not allocate a unique name, try another")


async def self_signup(*, name: str, contact: str | None, purpose: str | None,
                      ip: str, user_agent: str) -> SignupResult:
    """Validate, rate-limit, create the project, audit and notify the owner."""
    slug = slugify_name(name)
    await _check_limits(ip)
    s = get_settings()
    plain = generate_project_key()
    contact_c, purpose_c = _clean(contact), _clean(purpose)
    scopes = s.signup_scopes

    def make_row(final_name: str) -> ProjectRow:
        return ProjectRow(
            name=final_name, owner_email=contact_c if "@" in contact_c else None,
            project_key_hash=hash_project_key(plain), project_key_prefix=plain[:12],
            allowed_scopes=scopes, daily_cost_cap_usd=0.0,
            total_request_cap=s.SIGNUP_REQUEST_CAP, total_requests_used=0,
            self_signup=True, signup_ip=ip or None,
            notes=_notes(contact_c, purpose_c, ip, user_agent),
        )

    row = await _insert_unique(slug, make_row)
    await audit(actor="signup", action="project.self_signup", target=row.name,
                metadata={"scopes": scopes, "contact": contact_c, "purpose": purpose_c,
                          "requested_name": name[:100], "cap": s.SIGNUP_REQUEST_CAP},
                ip=ip or None)
    try:
        await alert(
            "self_signup",
            f"New self-signup: <b>{html.escape(row.name)}</b>\n"
            f"contact: {html.escape(contact_c or '-')}\n"
            f"purpose: {html.escape(purpose_c or '-')}\nip: {html.escape(ip or '-')}",
            throttle_min=5,
        )
    except Exception as e:  # noqa: BLE001 - a notifier hiccup must not fail a signup
        log.warning("self-signup notify failed: %s", e)
    return SignupResult(
        project_id=row.id, name=row.name, project_key=plain, scopes=scopes,
        daily_cost_cap_usd=0.0, total_request_cap=s.SIGNUP_REQUEST_CAP,
    )
