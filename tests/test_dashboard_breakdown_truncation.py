"""Breakdown cards truncate long labels instead of overflowing the tile.

REGRESSION (2026-09-26, owner screenshot): on the project page the
"By workflow" rows for SIN_HRM (`sinhrm.candidate_screening`, …) pushed their
sparklines past the right edge of a 311px card. A monospace label cannot wrap,
so under the auto table layout the table's minimum width grew with the longest
name and the table overflowed the card. The fix is CSS (fixed layout +
ellipsis, measured in a browser at 900-1400px viewports); these tests pin the
parts a refactor could silently drop: the rules, the class hooks they key on,
and the full name in a tooltip so truncation never hides information.
"""
from __future__ import annotations

from collections import namedtuple

from aibroker.routes.dashboard_assets import _DASHBOARD_CSS
from tests.test_routes_dashboard import _fake_proj_detail

LONG_WF = "sinhrm.candidate_screening"
LONG_MODEL = "openrouter/google/gemma-4-31b-it:free"


def _body() -> str:
    from aibroker.routes.dashboard_render import _render_project_detail

    d = _fake_proj_detail()
    BrkWf = namedtuple("BW", "wf n spend")
    BrkModel = namedtuple("BM", "model n spend toks tin cache_r")
    d["by_workflow"] = [BrkWf(LONG_WF, 472, 0.0)]
    d["by_model"] = [BrkModel(LONG_MODEL, 12, 0.0, 900, 800, 0)]
    return _render_project_detail(d).body.decode()


def test_css_truncates_breakdown_labels_with_a_fixed_layout():
    css = _DASHBOARD_CSS
    assert ".brk-card-split table, .brk-card-models table { width:100%; table-layout:fixed; }" in css
    assert "text-overflow:ellipsis" in css and "white-space:nowrap" in css
    # the sparkline no longer claims a third of a narrow card
    assert ".brk-card-split td.sp { width:24%; }" in css


def test_cards_carry_the_hooks_the_css_keys_on():
    body = _body()
    assert "brk-card brk-card-split" in body
    assert "brk-card brk-card-models" in body


def test_truncated_names_keep_the_full_name_in_a_tooltip():
    body = _body()
    assert f'title="{LONG_WF}"' in body
    assert f'title="{LONG_MODEL}"' in body
