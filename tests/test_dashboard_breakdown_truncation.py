"""Long labels truncate (full name kept in a tooltip) instead of overflowing a tile.

REGRESSION (2026-09-26): long workflow names pushed sparklines past a narrow card.
The redesigned UI keeps the guarantee: cells truncate via CSS and carry the full
name in `title`; grids use min-width:0 so nothing forces horizontal page scroll.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from aibroker.main import app
from tests import dash_seed as ds

client = TestClient(app)
LONG_WF = "sinhrm.candidate_screening_with_a_very_long_suffix"
LONG_MODEL = "openrouter/google/gemma-4-31b-it:free"


def test_css_has_truncation_and_no_overflow_hooks():
    css = client.get("/dashboard/static/css/app.css").text
    assert ".cell-trunc" in css and "text-overflow: ellipsis" in css
    assert ".trunc" in css and "minmax(0, 1fr)" in css and "min-width: 0" in css


def test_project_detail_keeps_full_names_in_tooltips():
    ds.seed(ds.project(1), ds.key(1))
    ds.seed(ds.usage(1, workflow=LONG_WF, model=LONG_MODEL, provider="openrouter"))
    body = ds.get(client, "/dashboard/projects/1", params={"range": "all"}).text
    assert f'title="{LONG_WF}"' in body
    assert f'title="{LONG_MODEL}"' in body
    models = ds.get(client, "/dashboard/projects/1", params={"range": "all", "tab": "models"}).text
    assert f'title="{LONG_MODEL}"' in models
