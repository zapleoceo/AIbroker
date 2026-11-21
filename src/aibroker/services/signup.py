"""Public self-signup: POST /v1/signup mints a restricted project + key.

A signed-up project can only spend nothing: `daily_cost_cap_usd = 0` (cost_guard
reads 0 as "free providers only" - a paid call's estimate always exceeds it; it
is NULL that means unlimited) and a lifetime `total_request_cap`. The owner
raises either limit in the dashboard. Abuse limits (all settings): the
SIGNUP_ENABLED kill switch, SIGNUP_PER_IP_PER_DAY and SIGNUP_PER_DAY over a
rolling 24h, counted from the `projects` rows themselves (`self_signup`,
`signup_ip`, `created_at`) so no extra table can drift.

Anti-bot (2026-11-21, services/signup_guard.py): every signup must carry a solved
proof-of-work challenge; a contact email on a throwaway domain is refused and each
email domain is limited per day. A project starts UNVERIFIED on
SIGNUP_UNVERIFIED_REQUEST_CAP requests; proving a Telegram account (Telegram Login
Widget payload, signed with the bot token) at signup or later via
POST /v1/signup/verify lifts it to SIGNUP_REQUEST_CAP. One Telegram account may
verify at most SIGNUP_PER_TELEGRAM projects. Existing projects are untouched.

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
from aibroker.auth_session import verify_telegram_widget
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.services.signup_guard import (
    PowInvalid,
    check_pow,
    email_domain,
    is_disposable,
)
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


class SignupContactRefused(ValueError):
    """The contact email is on a disposable-mail domain."""


class SignupTelegramInvalid(ValueError):
    """The Telegram Login Widget payload is missing its signature, forged or stale."""


class SignupNotEligible(ValueError):
    """Verification asked for a project that is not an unverified self-signup."""


__all__ = ["PowInvalid", "SignupContactRefused", "SignupDisabled", "SignupNameInvalid",
           "SignupNotEligible", "SignupRateLimited", "SignupResult", "SignupTelegramInvalid",
           "self_signup", "slugify_name", "verify_signup"]


@dataclass
class SignupResult:
    project_id: int
    name: str
    project_key: str
    scopes: list[str]
    daily_cost_cap_usd: float
    total_request_cap: int
    verified: bool = False


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


def _telegram_id(telegram: dict[str, object] | None) -> int | None:
    """The verified Telegram user id of a Login Widget payload; None when absent."""
    if not telegram:
        return None
    tg_id = verify_telegram_widget({k: str(v) for k, v in telegram.items() if v is not None})
    if tg_id is None:
        raise SignupTelegramInvalid("telegram login payload is invalid or older than a day")
    return tg_id


async def _check_telegram_limit(tg_id: int | None) -> None:
    if tg_id is None:
        return
    s = get_settings()
    async with get_session() as sess:
        used = (await sess.execute(select(func.count()).select_from(ProjectRow).where(
            ProjectRow.signup_telegram_id == tg_id))).scalar_one()
    if used >= s.SIGNUP_PER_TELEGRAM:
        raise SignupRateLimited("telegram", s.SIGNUP_PER_TELEGRAM)


async def _check_limits(ip: str, domain: str | None = None) -> None:
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
        from_domain = 0
        if domain:
            from_domain = (await sess.execute(base.where(
                func.lower(ProjectRow.owner_email).like(f"%@{domain}")))).scalar_one()
    if total >= s.SIGNUP_PER_DAY:
        raise SignupRateLimited("global", s.SIGNUP_PER_DAY)
    if ip and from_ip >= s.SIGNUP_PER_IP_PER_DAY:
        raise SignupRateLimited("ip", s.SIGNUP_PER_IP_PER_DAY)
    if domain and from_domain >= s.SIGNUP_PER_EMAIL_DOMAIN_PER_DAY:
        raise SignupRateLimited("email_domain", s.SIGNUP_PER_EMAIL_DOMAIN_PER_DAY)


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
                      ip: str, user_agent: str, pow_challenge: str | None = None,
                      pow_nonce: str | None = None,
                      telegram: dict[str, object] | None = None) -> SignupResult:
    """Validate, check the proof of work and limits, create the project (verified when a
    valid Telegram payload came along), audit and notify the owner."""
    slug = slugify_name(name)
    s = get_settings()
    if not s.SIGNUP_ENABLED:
        raise SignupDisabled()
    contact_c, purpose_c = _clean(contact), _clean(purpose)
    domain = email_domain(contact_c)
    if domain and is_disposable(domain):
        raise SignupContactRefused("disposable email domains are not accepted")
    tg_id = _telegram_id(telegram)
    await check_pow(pow_challenge, pow_nonce)
    await _check_limits(ip, domain)
    await _check_telegram_limit(tg_id)
    plain = generate_project_key()
    scopes = s.signup_scopes
    verified = tg_id is not None
    cap = s.SIGNUP_REQUEST_CAP if verified else s.SIGNUP_UNVERIFIED_REQUEST_CAP

    def make_row(final_name: str) -> ProjectRow:
        return ProjectRow(
            name=final_name, owner_email=contact_c if "@" in contact_c else None,
            project_key_hash=hash_project_key(plain), project_key_prefix=plain[:12],
            allowed_scopes=scopes, daily_cost_cap_usd=0.0,
            total_request_cap=cap, total_requests_used=0,
            self_signup=True, signup_ip=ip or None,
            signup_telegram_id=tg_id,
            signup_verified_at=datetime.now(UTC).replace(tzinfo=None) if verified else None,
            notes=_notes(contact_c, purpose_c, ip, user_agent),
        )

    row = await _insert_unique(slug, make_row)
    await audit(actor="signup", action="project.self_signup", target=row.name,
                metadata={"scopes": scopes, "contact": contact_c, "purpose": purpose_c,
                          "requested_name": name[:100], "cap": cap, "verified": verified,
                          "telegram_id": tg_id},
                ip=ip or None)
    try:
        await alert(
            "self_signup",
            f"New self-signup: <b>{html.escape(row.name)}</b>\n"
            f"contact: {html.escape(contact_c or '-')}\n"
            f"purpose: {html.escape(purpose_c or '-')}\nip: {html.escape(ip or '-')}\n"
            f"verified: {'telegram ' + str(tg_id) if verified else 'no'}",
            throttle_min=5,
        )
    except Exception as e:  # noqa: BLE001 - a notifier hiccup must not fail a signup
        log.warning("self-signup notify failed: %s", e)
    return SignupResult(
        project_id=row.id, name=row.name, project_key=plain, scopes=scopes,
        daily_cost_cap_usd=0.0, total_request_cap=cap, verified=verified,
    )


async def verify_signup(project_id: int, telegram: dict[str, object] | None,
                        ip: str) -> SignupResult:
    """POST /v1/signup/verify: an UNVERIFIED self-signup proves a Telegram account and
    its lifetime cap rises to SIGNUP_REQUEST_CAP (never lowered). The key is unchanged
    (project_key is empty in the result)."""
    tg_id = _telegram_id(telegram)
    if tg_id is None:
        raise SignupTelegramInvalid("telegram login payload required")
    s = get_settings()
    async with get_session() as sess:
        row = await sess.get(ProjectRow, project_id)
        if row is None or not row.self_signup or row.signup_verified_at is not None:
            raise SignupNotEligible("only an unverified self-signup project can be verified")
    await _check_telegram_limit(tg_id)
    async with get_session() as sess:
        row = await sess.get(ProjectRow, project_id)
        row.signup_telegram_id = tg_id
        row.signup_verified_at = datetime.now(UTC).replace(tzinfo=None)
        row.total_request_cap = max(row.total_request_cap or 0, s.SIGNUP_REQUEST_CAP)
        result = SignupResult(project_id=row.id, name=row.name, project_key="",
                              scopes=list(row.allowed_scopes),
                              daily_cost_cap_usd=row.daily_cost_cap_usd or 0.0,
                              total_request_cap=row.total_request_cap, verified=True)
    await audit(actor="signup", action="project.signup_verified", target=result.name,
                metadata={"telegram_id": tg_id, "cap": result.total_request_cap}, ip=ip or None)
    return result
