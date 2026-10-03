"""Pure helpers behind tests/conftest.py's prod-isolation guards (unit-tested
in test_isolation_guards.py). No aibroker imports: conftest runs this before
the app's settings can be read."""
from __future__ import annotations

from collections.abc import Callable, MutableMapping

from sqlalchemy.engine import make_url

PROD_CWD = "/var/www/aibroker"
FAKE_OWNER_TELEGRAM_ID = "999000111"

# Forced (not setdefault) so a prod/dev shell env can never reach a real
# Redis, Telegram, local ASR/vision box or the backup dir during tests.
_BLANKED = (
    "REDIS_URL", "TELEGRAM_BOT_TOKEN", "ASR_LOCAL_URL", "VISION_LOCAL_URL",
    "MONITOR_BACKUP_DIR",
)


def sandbox_violation(env: MutableMapping[str, str] | dict, *, cwd: str,
                      exists: Callable[[str], bool]) -> str | None:
    """Reason pytest must not run here (looks like a prod container), else None."""
    if env.get("CI") == "true" or env.get("AIB_TEST_SANDBOX") == "1":
        return None
    if exists("/.dockerenv"):
        return "running inside a container (/.dockerenv exists)"
    if cwd.rstrip("/") == PROD_CWD:
        return f"cwd is the prod checkout {PROD_CWD}"
    return None


def isolate_env(env: MutableMapping[str, str], alert_state_dir: str) -> None:
    """Force every externally-reaching setting to an inert value."""
    for name in _BLANKED:
        env[name] = ""
    env["OWNER_TELEGRAM_ID"] = FAKE_OWNER_TELEGRAM_ID
    env["ALERT_STATE_DIR"] = alert_state_dir


def assert_safe_test_db_url(url: str) -> None:
    """Raise ValueError unless `url` is SQLite or a Postgres db named *_test."""
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    if backend == "sqlite":
        return
    if backend == "postgresql":
        name = parsed.database or ""
        if name.endswith("_test"):
            return
        raise ValueError(
            f"refusing to run tests against Postgres database {name!r}: "
            "TEST_DATABASE_URL must name a database ending in '_test'"
        )
    raise ValueError(f"unsupported TEST_DATABASE_URL backend: {backend!r}")


def assert_connected_db_is_test(name: str) -> None:
    """Runtime check on `SELECT current_database()` before the first drop_all."""
    if not name.endswith("_test"):
        raise RuntimeError(
            f"connected Postgres database is {name!r}, not *_test: refusing drop_all"
        )
