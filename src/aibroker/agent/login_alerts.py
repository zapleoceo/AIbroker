"""One Telegram message when a subscription login drops, one when it is back.

Run by the monitor every tick. The notifier's state file per account is the memory: alert()
sends only when no file exists (the "throttle" is longer than any outage, so it never repeats),
recover() sends only when one does and deletes it - exactly one message per transition.
A silent worker (stale) or a never-reported login (unknown) sends nothing either way.
"""
from __future__ import annotations

import html
from datetime import datetime

from aibroker.agent.login_status import BROKEN, DOWN_STATES, OK, AccountLogin, account_logins
from aibroker.config import get_settings
from aibroker.telemetry.notifier import alert, recover

ONCE_MIN = 10 * 365 * 24 * 60
_REASON_RU = {
    "logged_out": "сессия не залогинена",
    BROKEN: "залогинена, но прогоны падают: вход не может работать (обновление токена или доступ)",
}


def _card(acc: AccountLogin) -> str:
    host = get_settings().PUBLIC_HOST
    return (f"Приложение: {html.escape(acc.app)} ({html.escape(acc.key)})\n"
            f"Ящик: {html.escape(acc.email or 'неизвестен')}\n"
            f"Админка: https://{html.escape(host)}/dashboard/subscriptions")


def down_message(acc: AccountLogin) -> str:
    return (f"🔌 Подписка отвалилась: <b>{html.escape(acc.name)}</b>\n{_card(acc)}\n"
            f"Причина: {_REASON_RU.get(acc.state, html.escape(acc.reason))}")


def up_message(acc: AccountLogin) -> str:
    return f"Подписка снова работает: <b>{html.escape(acc.name)}</b>\n{_card(acc)}"


async def check_subscription_logins(now: datetime | None = None) -> None:
    for acc in await account_logins(now):
        key = f"sub_login:{acc.key}"
        if acc.state in DOWN_STATES:
            await alert(key, down_message(acc), throttle_min=ONCE_MIN)
        elif acc.state == OK:
            await recover(key, up_message(acc))
