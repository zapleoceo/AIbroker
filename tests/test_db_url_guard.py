"""The test-DB guard must refuse anything that could be a real database."""
from __future__ import annotations

import pytest
from conftest import _assert_safe_test_db_url


@pytest.mark.parametrize("url", [
    "sqlite+aiosqlite:///:memory:",
    "postgresql+asyncpg://u:p@localhost:5432/aibroker_test",
])
def test_safe_urls_pass(url):
    _assert_safe_test_db_url(url)


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://u:p@pgbouncer:6432/aibroker",
    "postgres://u:p@postgres:5432/aibroker",
    "postgresql+asyncpg://u:p@localhost/test_aibroker",
    "postgresql+asyncpg://u:p@localhost/",
    "mysql://u:p@h/x_test",
])
def test_unsafe_urls_refused(url):
    with pytest.raises(ValueError):
        _assert_safe_test_db_url(url)
