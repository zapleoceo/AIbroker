"""Guards that keep pytest away from prod: sandbox refusal, forced-inert env,
runtime *_test database name check, and the 013 migration mirroring init.sql."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from _isolation import (
    FAKE_OWNER_TELEGRAM_ID,
    assert_connected_db_is_test,
    isolate_env,
    sandbox_violation,
)

SQL_DIR = Path(__file__).resolve().parent.parent / "infra" / "sql"


def _v(env, cwd="/home/ci", docker=False):
    return sandbox_violation(env, cwd=cwd, exists=lambda p: docker and p == "/.dockerenv")


def test_plain_checkout_allowed():
    assert _v({}) is None


def test_dockerenv_refused():
    assert "container" in _v({}, docker=True)


def test_prod_cwd_refused():
    assert "prod checkout" in _v({}, cwd="/var/www/aibroker")
    assert "prod checkout" in _v({}, cwd="/var/www/aibroker/")


@pytest.mark.parametrize("env", [{"CI": "true"}, {"AIB_TEST_SANDBOX": "1"}])
def test_opt_in_allows_container_and_prod_path(env):
    assert _v(env, cwd="/var/www/aibroker", docker=True) is None


def test_other_values_do_not_opt_in():
    assert _v({"CI": "1", "AIB_TEST_SANDBOX": "0"}, docker=True) is not None


def test_isolate_env_forces_over_existing_values():
    env = {"REDIS_URL": "redis://prod", "TELEGRAM_BOT_TOKEN": "tok", "OWNER_TELEGRAM_ID": "1",
           "VISION_LOCAL_URL": "http://y",
           "MONITOR_BACKUP_DIR": "/backups", "ALERT_STATE_DIR": "/var/lib/aibroker"}
    isolate_env(env, "/tmp/fresh")
    assert env["OWNER_TELEGRAM_ID"] == FAKE_OWNER_TELEGRAM_ID
    assert env["ALERT_STATE_DIR"] == "/tmp/fresh"
    for k in ("REDIS_URL", "TELEGRAM_BOT_TOKEN", "VISION_LOCAL_URL",
              "MONITOR_BACKUP_DIR"):
        assert env[k] == ""


def test_live_process_env_is_inert():
    assert os.environ["REDIS_URL"] == ""
    assert os.environ["TELEGRAM_BOT_TOKEN"] == ""
    assert os.environ["OWNER_TELEGRAM_ID"] == FAKE_OWNER_TELEGRAM_ID


def test_settings_do_not_read_cwd_dotenv():
    from aibroker.config import Settings

    assert Settings.model_config["env_file"] is None


def test_connected_db_name_check():
    assert_connected_db_is_test("aibroker_test")
    for bad in ("aibroker", "test_aibroker", ""):
        with pytest.raises(RuntimeError):
            assert_connected_db_is_test(bad)


def test_app_role_grants_in_migration_and_init_sql():
    mig = (SQL_DIR / "migrations" / "013_app_role_grants.sql").read_text(encoding="utf-8")
    init = (SQL_DIR / "init.sql").read_text(encoding="utf-8")
    block = mig[mig.index("DO $roles$"):]
    assert block in init
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" in block
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES" in block
    assert "ALTER DEFAULT PRIVILEGES" in block
    for forbidden in ("SUPERUSER", "GRANT ALL", "OWNER TO", "PASSWORD"):
        assert forbidden not in block.upper()
