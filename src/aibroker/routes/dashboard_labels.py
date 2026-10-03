"""Display-layer vocabulary for the admin UI: friendly error labels (EN/RU)
and the derived status of an API key. Pure functions over plain values — no
HTML, no DB — so the templates and the tests share one source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Known failure signatures → a short actionable label instead of raw
# exception text. 2026-07-05: "мёртв" + a raw litellm dump didn't tell the
# operator what to actually DO — "credit balance is too low" buried in JSON
# reads as generic breakage, not "go add money". Order matters (first match
# wins); nothing here duplicates classify_provider_error's signs — this is
# purely a display-layer translation, not a routing decision.
# Billing-exhaustion signs — a VALID key that's just out of money (not a broken
# credential). Kept as their own group so the status can render "нет средств"
# (needs top-up, recovers on top-up) instead of the alarming "мёртв".
_TOP_UP_SIGNS: tuple[str, ...] = (
    "credit balance is too low",
    "prepayment credits are depleted",
    "credits are depleted",
    "insufficient",
    "payment required",
    "no funds",
)
# Rate-limit / quota signs — a healthy key that's just throttled. Every
# provider phrases it differently and litellm often prepends its own class name,
# so raw last_error ranges from a tidy "rate limit" to a multi-line
# "litellm.RateLimitError: geminiException - {..json..}" dump. Collapse them all
# to one clean label. "monthly quota" is listed FIRST (before the generic
# "quota") so Mistral's monthly-ceiling reads as its own thing, not a transient
# throttle. First match wins.
_RATE_LIMIT_DISPLAY_SIGNS: tuple[str, ...] = (
    "rate limit", "ratelimit", "too many requests", "429",
    "resource_exhausted", "resource has been exhausted",
)

# Shared (en, ru) label vocabulary — one home for the strings both the raw-text
# key-status column (_friendly_reason) and the error-kind log cell
# (_friendly_call_error) speak, so the two never drift.
_LBL_RATE = ("rate limited", "лимит запросов")
_LBL_TIMEOUT = ("timeout", "таймаут")
_LBL_CAP = ("budget cap", "лимит бюджета")
_LBL_AUTH = ("auth failed", "ошибка авторизации")
_LBL_EMPTY = ("empty response", "пустой ответ")
_LBL_BADJSON = ("bad JSON", "плохой JSON")
_LBL_CONN = ("connection error", "ошибка соединения")

_FRIENDLY_REASONS: tuple[tuple[str, str, str], ...] = (
    *((s, "top up balance", "пополнить баланс") for s in _TOP_UP_SIGNS),
    ("monthly quota", "monthly quota", "месячная квота"),
    # cloudflare free tier: 10k neurons/day, resets 00:00 UTC — a daily quota,
    # not a dead key (2026-07-12).
    ("daily free allocation", "daily free quota — resets 00:00 UTC",
     "дневная free-квота — сброс в 00:00 UTC"),
    *((s, *_LBL_RATE) for s in _RATE_LIMIT_DISPLAY_SIGNS),
    ("auth failed", *_LBL_AUTH),
    ("unauthorized", *_LBL_AUTH),
    ("quota", "quota exceeded", "квота исчерпана"),
    ("timeout", "provider timeout", "таймаут провайдера"),
    ("response_format type is unavailable", "provider feature outage",
     "сбой фичи у провайдера"),
)

# Transient (recoverable) vs dead (needs intervention) → the log cell's colour:
# warn (yellow) for rate/timeout/cap/empty/json/connection, bad (red) for auth.
_ERR_TRANSIENT = "warn"
_ERR_DEAD = "bad"



def _is_top_up(raw: str | None) -> bool:
    """True if the error is a billing-exhaustion (out of money) — a valid key
    that recovers on top-up, not a dead credential."""
    low = (raw or "").lower()
    return any(s in low for s in _TOP_UP_SIGNS)


def _friendly_reason(raw: str) -> tuple[str, str] | None:
    """(en, ru) short actionable label for a known raw error, else None —
    caller falls back to showing (a truncated slice of) the raw text."""
    low = raw.lower()
    for sign, en, ru in _FRIENDLY_REASONS:
        if sign in low:
            return en, ru
    return None


def _friendly_call_error(
    http_status: int | None, error_kind: str | None
) -> tuple[str, str, str] | None:
    """(en, ru, css_class) for a usage_log error row's http/kind pair, else None
    (caller shows the raw `{http_status} {error_kind}`). Shares the _LBL_*
    vocabulary with _friendly_reason so the recent-calls table and the
    key-status column read the same. Colours by meaning: transient=warn,
    dead(auth)=bad.

    error_kind is matched BEFORE http_status: a timeout and a cap block are
    booked under 429/402 respectively, so a raw status read would mislabel a
    '429 TimeoutError' as a plain rate limit."""
    kind = (error_kind or "").lower()
    if not kind and http_status is None:
        return None
    if "capblock" in kind or http_status == 402:
        return (*_LBL_CAP, _ERR_TRANSIENT)
    if "timeout" in kind:
        return (*_LBL_TIMEOUT, _ERR_TRANSIENT)
    if http_status == 401 or "auth" in kind:
        return (*_LBL_AUTH, _ERR_DEAD)
    if http_status == 429:
        return (*_LBL_RATE, _ERR_TRANSIENT)
    if kind == "emptybody":
        return (*_LBL_EMPTY, _ERR_TRANSIENT)
    if kind == "invalidjson":
        return (*_LBL_BADJSON, _ERR_TRANSIENT)
    if "connection" in kind:
        return (*_LBL_CONN, _ERR_TRANSIENT)
    return None



@dataclass(frozen=True, slots=True)
class KeyStatus:
    """Derived state of one api_keys row, as the UI shows it."""
    code: str          # alive|capped|cooldown|no_credits|dead|disabled
    cls: str           # ok|warn|bad|off  (chip colour)
    en: str
    ru: str


_STATUS: dict[str, tuple[str, str, str]] = {
    "alive": ("ok", "alive", "жив"),
    "capped": ("warn", "day cap", "лимит дня"),
    "cooldown": ("warn", "cooldown", "пауза"),
    "no_credits": ("warn", "no credits", "нет средств"),
    "dead": ("bad", "dead", "мёртв"),
    "disabled": ("off", "disabled", "отключён"),
}


def key_status(k: Any, now: datetime) -> KeyStatus:
    """Mirror of what the selector will actually do with the key.

    - owner-disabled wins over everything (the selector skips it whatever
      is_alive says);
    - a hard-capped key (day cost cap or day request limit spent) is alive and
      not cooling but skipped until midnight UTC, so it reads "day cap". The
      freshness check mirrors FRESH_DAILY_*_SQL: a daily_reset_at from a
      previous day means the counter is stale and reads 0, i.e. NOT capped;
    - is_alive=False only because the BALANCE ran out is "no credits"
      (recovers on top-up), not the alarming "dead".
    """
    in_cd = bool(k.cooldown_until and k.cooldown_until > now)
    no_credits = not k.is_alive and not in_cd and _is_top_up(k.last_error)
    day_capped = bool(
        k.is_alive and not in_cd and k.daily_reset_at == now.date() and (
            (k.daily_cost_cap_usd is not None
             and float(k.daily_cost_used_usd or 0) >= float(k.daily_cost_cap_usd))
            or ((k.daily_limit or 0) > 0 and (k.daily_used or 0) >= k.daily_limit)
        )
    )
    code = (
        "disabled" if not k.is_active
        else "capped" if day_capped
        else "alive" if (k.is_alive and not in_cd)
        else "cooldown" if in_cd
        else "no_credits" if no_credits
        else "dead"
    )
    cls, en, ru = _STATUS[code]
    return KeyStatus(code, cls, en, ru)


def reason_labels(raw: str | None, limit: int = 60) -> tuple[str, str] | None:
    """(en, ru) for a key's last_error: a friendly label when the signature is
    known, else a truncated slice of the raw text (same string in both)."""
    if not raw:
        return None
    friendly = _friendly_reason(raw)
    if friendly:
        return friendly
    short = raw[:limit] + ("…" if len(raw) > limit else "")
    return short, short
