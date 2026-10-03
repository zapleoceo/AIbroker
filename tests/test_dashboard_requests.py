"""The unified Requests page: one row per client request (usage_log attempts
grouped by request_id, queued jobs joined in), the live queue strip, filters,
the drawer for a job and for a direct call, CSV, and the retired Jobs page."""
from __future__ import annotations

import csv
import io
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from aibroker.main import app
from aibroker.routes import dashboard_requests as rq
from tests import dash_seed as ds

client = TestClient(app)


def _get(path: str, **kw):
    return ds.get(client, path, **kw)


def _rows(**params):
    f = rq.RequestFilter.from_params(params, None, None)
    return ds.run(rq.query_requests(f))[0]


def _by_id(rows):
    return {r["request_id"]: r for r in rows}


def _seed_mixed() -> None:
    """A 3-attempt direct request, a plain call, two legacy NULL rows, a done job
    with 2 attempts, a failed job, and a pending + running job with no attempts."""
    ds.seed(ds.project(1, "stepan"), ds.project(2, "vera"), ds.key(1, "gemini", "k1"),
            ds.key(2, "groq", "k2"))
    ds.seed(
        ds.usage(1, minutes_ago=30, status="error", provider="groq", api_key_id=2, cost=0.001,
                 request_id="rq-walk", error_kind="TimeoutError", http_status=429, latency_ms=900),
        ds.usage(2, minutes_ago=29, status="error", provider="cerebras", api_key_id=None,
                 request_id="rq-walk", error_kind="RateLimitError", http_status=429,
                 latency_ms=100, cost=0.002),
        ds.usage(3, minutes_ago=28, provider="gemini", api_key_id=1, request_id="rq-walk",
                 model_served="gemini-3.5-flash", cost=0.004, latency_ms=1000, tokens_in=10,
                 tokens_out=5),
        ds.usage(4, minutes_ago=20, project_id=2, workflow="describe", capability="vision",
                 request_id="rq-plain"),
        ds.usage(5, minutes_ago=15, status="error", provider="groq", api_key_id=2,
                 error_kind="X", http_status=500),                       # legacy, NULL request_id
        ds.usage(6, minutes_ago=14, provider="gemini", api_key_id=1),     # legacy, NULL request_id
        ds.usage(7, minutes_ago=9, status="error", provider="groq", api_key_id=2,
                 request_id="job-11", error_kind="TimeoutError", http_status=429),
        ds.usage(8, minutes_ago=8, provider="gemini", api_key_id=1, request_id="job-11",
                 capability="chat:deep", latency_ms=2000),
        ds.usage(9, minutes_ago=6, status="error", provider="groq", api_key_id=2,
                 request_id="job-12", capability="chat:deep"),
    )
    now = ds.now()
    ds.seed(
        ds.job(10, "pending", minutes_ago=5),
        ds.job(13, "running", minutes_ago=4, started_at=now - timedelta(minutes=3)),
        ds.job(11, "done", minutes_ago=10, started_at=now - timedelta(minutes=9),
               completed_at=now - timedelta(minutes=8), retry_count=1),
        ds.job(12, "error", minutes_ago=7, started_at=now - timedelta(minutes=6),
               completed_at=now - timedelta(minutes=5), error_message="all providers failed"))


# ─── grouping ───────────────────────────────────────────────────────────────


def test_attempts_of_one_request_collapse_into_one_row_with_summed_totals():
    _seed_mixed()
    walk = _by_id(_rows())["rq-walk"]
    assert (walk["tries"], walk["state"], walk["provider"]) == (3, "ok", "gemini")
    assert walk["model_served"] == "gemini-3.5-flash" and walk["is_job"] is False
    assert round(walk["cost_usd"], 6) == 0.007 and walk["latency_ms"] == 2000
    assert walk["tokens_in"] == 100 + 100 + 10 and walk["ref"] == "1"


def test_legacy_null_request_id_rows_are_their_own_requests():
    _seed_mixed()
    ids = _by_id(_rows())
    assert ids["u-5"]["tries"] == 1 and ids["u-5"]["state"] == "failed"
    assert ids["u-6"]["state"] == "ok" and ids["u-6"]["ref"] == "6"


def test_a_job_with_attempts_is_one_row_and_a_job_without_any_still_appears():
    _seed_mixed()
    ids = _by_id(_rows())
    done = ids["job-11"]
    assert (done["is_job"], done["tries"], done["state"], done["ref"]) == (True, 2, "ok", "job-11")
    assert abs(done["wait_ms"] - 60_000) < 2_000 and done["job_retries"] == 1
    failed = ids["job-12"]
    assert failed["state"] == "failed" and failed["job_error"] == "all providers failed"
    pending, running = ids["job-10"], ids["job-13"]
    assert (pending["state"], pending["tries"], pending["provider"]) == ("pending", 0, None)
    assert running["state"] == "running" and abs(running["wait_ms"] - 60_000) < 2_000
    assert pending["wait_ms"] >= 5 * 60_000 - 5_000                    # still waiting: grows
    assert sum(1 for r in _rows() if r["request_id"].startswith("job-")) == 4   # no duplicates


def test_result_is_newest_first_and_counts_requests_not_attempts():
    _seed_mixed()
    rows = _rows()
    assert len(rows) == 8                                   # 9 attempt rows + 2 orphan jobs -> 8 requests
    times = [r["created_at"] for r in rows]
    assert times == sorted(times, reverse=True)


# ─── filters ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("params,expected", [
    ({"type": "job"}, {"job-10", "job-11", "job-12", "job-13"}),
    ({"type": "direct"}, {"rq-walk", "rq-plain", "u-5", "u-6"}),
    ({"status": "pending"}, {"job-10"}),
    ({"status": "running"}, {"job-13"}),
    ({"status": "failed"}, {"u-5", "job-12"}),
    ({"status": "error"}, {"u-5", "job-12"}),                # legacy alias
    ({"status": "failed", "type": "direct"}, {"u-5"}),
    ({"project": "2"}, {"rq-plain"}),
    ({"capability": "vision"}, {"rq-plain"}),
    ({"workflow": "describe"}, {"rq-plain"}),
    ({"provider": "cerebras"}, {"rq-walk"}),                 # ANY attempt touched it
    ({"provider": "groq", "status": "ok"}, {"rq-walk", "job-11"}),
    ({"model": "gemini/gemini-3.5-flash-lite", "provider": "cerebras"}, {"rq-walk"}),
    ({"model": "no/such"}, set()),
    ({"q": "job-11"}, {"job-11"}),
    ({"q": "10"}, {"job-10"}),
    ({"q": "u-6"}, {"u-6"}),
    ({"q": "rq-walk"}, {"rq-walk"}),
    ({"q": "nope"}, set()),
])
def test_filters(params, expected):
    _seed_mixed()
    assert {r["request_id"] for r in _rows(**params)} == expected


def test_sort_by_cost_and_pagination_flag():
    _seed_mixed()
    top = _rows(sort="cost")[0]
    assert top["request_id"] == "rq-walk"
    for page, more_expected in (("0", True), ("1", False)):
        f = rq.RequestFilter.from_params({"page": page}, None, None)
        rows, more = ds.run(rq.query_requests(f, page_size=4))
        assert len(rows) == 4 and more is more_expected


def test_newest_page_is_cut_from_a_recent_slice_but_older_pages_still_follow():
    ds.seed(ds.project(1), ds.key(1))
    ds.seed(*[ds.usage(100 + i, minutes_ago=i + 1, request_id=f"rq-new{i:03d}") for i in range(53)])
    f = rq.RequestFilter()
    rows, more = ds.run(rq.query_requests(f))
    assert len(rows) == 50 and more and rows[0]["request_id"] == "rq-new000"
    rows, more = ds.run(rq.query_requests(rq.RequestFilter(page=1)))
    assert len(rows) == 3 and not more


def test_slices_widen_until_the_page_is_full_and_straddling_requests_stay_whole():
    ds.seed(ds.project(1), ds.key(1))
    ds.seed(*[ds.usage(200 + i, minutes_ago=60 * 24 * 40 + i, request_id=f"rq-old{i:03d}")
              for i in range(60)])
    ds.seed(ds.usage(300, minutes_ago=60 * 25, status="error", request_id="rq-straddle"),
            ds.usage(301, minutes_ago=60 * 23, request_id="rq-straddle", cost=0.5),
            ds.usage(302, minutes_ago=5, request_id="rq-fresh"))
    rows, more = ds.run(rq.query_requests(rq.RequestFilter()))
    assert [r["request_id"] for r in rows[:3]] == ["rq-fresh", "rq-straddle", "rq-old000"]
    assert rows[1]["tries"] == 2 and rows[1]["cost_usd"] == 0.5 and len(rows) == 50 and more
    rows, more = ds.run(rq.query_requests(rq.RequestFilter(page=1)))
    assert len(rows) == 12 and not more


def test_time_window_applies_to_attempts_and_to_orphan_jobs():
    _seed_mixed()
    ds.seed(ds.job(20, "pending", minutes_ago=60 * 24 * 3))
    now = ds.now()
    f = rq.RequestFilter(start=now - timedelta(hours=1), end=None)
    assert "job-20" not in {r["request_id"] for r in ds.run(rq.query_requests(f))[0]}
    f = rq.RequestFilter(start=now - timedelta(days=5), end=None)
    assert "job-20" in {r["request_id"] for r in ds.run(rq.query_requests(f))[0]}


def test_junk_filters_never_500():
    assert _get("/dashboard/requests", params={
        "project": "x", "status": "zzz", "type": "q", "q": "' OR 1=1 --", "sort": "; drop table",
        "page": "99999999"}).status_code == 200


# ─── page ───────────────────────────────────────────────────────────────────


def test_page_lists_requests_with_outcome_status_and_drawer_hooks():
    _seed_mixed()
    body = _get("/dashboard/requests", params={"range": "all"}).text
    assert body.count('class="clickable"') == 8
    assert 'hx-get="/dashboard/requests/job-11"' in body and 'hx-get="/dashboard/requests/1"' in body
    assert "3 tries" in body and "gemini-3.5-flash" in body and "no attempt yet" in body
    assert 'data-ru="3 попытки"' in body and 'data-ru="2 попытки"' in body
    assert "wait" in body and 'data-ru="ждала"' in body
    assert 'class="chip warn"' in body and 'class="chip info"' in body    # pending, running
    assert 'class="chip bad"' in body                                     # failed


def test_empty_page_still_shows_the_strip_and_the_empty_state():
    body = _get("/dashboard/requests").text
    assert "No requests match" in body and 'id="qstrip"' in body


def test_one_attempt_shows_no_tries_prefix():
    ds.seed(ds.project(1), ds.key(1))
    ds.seed(ds.usage(1, request_id="rq-one"))
    body = _get("/dashboard/requests", params={"range": "all"}).text
    assert "tries" not in body and 'data-ru="1 попытка"' not in body


def test_pagination_offers_load_more_then_stops():
    ds.seed(ds.project(1), ds.key(1))
    ds.seed(*[ds.usage(100 + i, minutes_ago=i) for i in range(53)])
    first = _get("/dashboard/requests", params={"range": "today"}).text
    assert first.count('class="clickable"') == 50
    assert 'hx-select-oob="#req-more"' in first and "page=1" in first
    second = _get("/dashboard/requests", params={"range": "today", "page": "1"}).text
    assert second.count('class="clickable"') == 3 and "Load more" not in second


def test_filter_form_has_every_control_and_keeps_state():
    ds.seed(ds.project(1, "stepan"))
    body = _get("/dashboard/requests", params={"type": "job", "status": "failed", "q": "job-3"}).text
    for name in ("q", "type", "status", "project", "workflow", "capability", "provider"):
        assert f'name="{name}"' in body
    assert '<option value="job" selected>' in body and '<option value="failed" selected>' in body
    assert 'value="job-3"' in body and "Only failed" in body


# ─── queue strip ────────────────────────────────────────────────────────────


def test_queue_strip_counts_links_and_oldest_pending():
    _seed_mixed()
    html = _get("/dashboard/requests/queue", headers={"HX-Request": "true"}).text
    assert 'id="qstrip"' in html and 'hx-trigger="every 10s"' in html
    assert "status=pending" in html and "status=running" in html and "status=failed" in html
    tiles = {lbl: n for n, lbl in re.findall(r"<b>(\d+)</b> <span><span[^>]*>([^<]+)</span>", html)}
    assert tiles == {"pending": "1", "running": "1", "failed 24h": "1"}
    assert 'data-ru="ждут"' in html and 'data-ru="сбои 24ч"' in html
    assert "oldest" in html and 'data-en="5m" data-ru="5 мин"' in html


def test_queue_strip_marks_the_active_filter_and_keeps_it_across_polls():
    _seed_mixed()
    page = _get("/dashboard/requests", params={"type": "job", "status": "pending"}).text
    assert page.count('aria-current="true"') == 1 + 1         # active tile + active range preset
    assert "queue?status=pending&amp;type=job" in page or "queue?type=job&amp;status=pending" in page
    quiet = _get("/dashboard/requests/queue").text
    assert 'aria-current="true"' not in quiet


def test_queue_strip_flags_a_stuck_queue():
    ds.seed(ds.project(1))
    ds.seed(ds.job(1, "pending", minutes_ago=45))
    assert 'class="qchip bad"' in _get("/dashboard/requests/queue").text


def test_strip_requires_login():
    r = client.get("/dashboard/requests/queue", follow_redirects=False)
    assert r.status_code == 303


# ─── drawer ─────────────────────────────────────────────────────────────────


def test_drawer_for_a_job_shows_queue_block_and_the_attempt_trail():
    _seed_mixed()
    html = _get("/dashboard/requests/job-11", headers={"HX-Request": "true"}).text
    for marker in ("Queued job", "Queued at", "Waited", "Retries", "Attempt trail", "job-11",
                   'data-copy="job-11"', "2 attempts", "Totals", "cache read"):
        assert marker in html
    assert re.findall(r'<b class="trunc">(\w+)</b>', html) == ["groq", "gemini"]
    assert html.count('class="n"') == 2


def test_drawer_for_a_failed_job_shows_the_final_error():
    _seed_mixed()
    html = _get("/dashboard/requests/job-12", headers={"HX-Request": "true"}).text
    assert "Final error" in html and "all providers failed" in html


def test_drawer_for_a_job_without_attempts_says_it_is_waiting():
    _seed_mixed()
    html = _get("/dashboard/requests/job-10", headers={"HX-Request": "true"}).text
    assert "No attempt yet" in html and "Queued job" in html and 'class="chip warn"' in html


def test_drawer_for_a_direct_call_shows_the_trail_without_a_job_block():
    _seed_mixed()
    html = _get("/dashboard/requests/3", headers={"HX-Request": "true"}).text
    assert re.findall(r'<b class="trunc">(\w+)</b>', html) == ["groq", "cerebras", "gemini"]
    assert html.count('class="n"') == 3 and "Queued job" not in html
    assert "rq-walk" in html and "gemini-3.5-flash" in html and "$0.007000" in html


def test_drawer_for_a_legacy_row_is_that_single_attempt():
    _seed_mixed()
    html = _get("/dashboard/requests/5", headers={"HX-Request": "true"}).text
    assert html.count('class="n"') == 1 and "u-5" in html


@pytest.mark.parametrize("ref", ["999999", "job-999999", "nope"])
def test_drawer_for_unknown_ref_says_so(ref):
    assert "Request not found" in _get(f"/dashboard/requests/{ref}",
                                       headers={"HX-Request": "true"}).text


def test_drawer_url_without_htmx_deep_links_into_the_page():
    _seed_mixed()
    r = _get("/dashboard/requests/job-11")
    assert r.status_code == 303 and "open=job-11" in r.headers["location"]
    page = _get("/dashboard/requests", params={"open": "job-11", "range": "all"}).text
    assert 'id="drawer-preload"' in page and "Attempt trail" in page


# ─── CSV ────────────────────────────────────────────────────────────────────


def test_csv_has_one_row_per_client_request_with_job_columns():
    _seed_mixed()
    r = _get("/dashboard/requests.csv", params={"range": "all"})
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 8
    by = {x["request_id"]: x for x in rows}
    assert by["rq-walk"]["tries"] == "3" and by["rq-walk"]["type"] == "direct"
    assert by["job-11"]["type"] == "job" and by["job-11"]["retries"] == "1"
    assert by["job-12"]["error"] == "all providers failed" and by["job-10"]["status"] == "pending"
    assert float(by["rq-walk"]["cost_usd"]) == pytest.approx(0.007)
    only = list(csv.DictReader(io.StringIO(_get(
        "/dashboard/requests.csv", params={"range": "all", "type": "job", "status": "failed"}).text)))
    assert [x["request_id"] for x in only] == ["job-12"]


def test_csv_neutralises_formula_injection():
    ds.seed(ds.project(1), ds.key(1))
    ds.seed(ds.usage(1, workflow="=HYPERLINK(1)", request_id="rq-evil1234"))
    rows = list(csv.DictReader(io.StringIO(
        _get("/dashboard/requests.csv", params={"range": "all"}).text)))
    assert rows[0]["workflow"] == "'=HYPERLINK(1)"


# ─── the retired Jobs page ──────────────────────────────────────────────────


def test_jobs_page_redirects_permanently_to_requests_filtered_to_jobs():
    r = _get("/dashboard/jobs")
    assert r.status_code == 301 and r.headers["location"] == "/dashboard/requests?type=job"


def test_jobs_is_gone_from_the_rail_and_the_more_sheet_and_overview_links_to_filters():
    ds.seed_demo()
    body = _get("/dashboard").text
    assert 'href="/dashboard/jobs"' not in body and "Jobs" not in body.split('class="sheet"')[1]
    assert "/dashboard/requests?type=job&amp;status=pending&amp;range=all" in body
    assert "/dashboard/requests?type=job&amp;status=failed&amp;range=7d" in body


def test_attention_items_link_to_the_filtered_requests_page():
    from aibroker.routes import dashboard_views as v
    hrefs = [i["href"] for i in v.build_attention(
        [], [], [], {"stuck_pending": True, "stuck_running": True})]
    assert all(h.startswith("/dashboard/requests?type=job") for h in hrefs) and len(hrefs) == 2


def test_responsive_css_hooks_for_the_unified_table_and_strip():
    css = client.get("/dashboard/static/css/app.css").text
    assert ".qstrip" in css and "position: sticky" in css and "td.cell-wrap" in css
    assert "@media (hover: hover) { .table tbody tr.clickable:hover td" in css   # no sticky grey on touch
    assert 'html[lang="ru"] .rtable td[data-ru-label]::before' in css


# ─── mobile review: collapsed filters, chips, RU, card cells ────────────────


def test_filters_stay_collapsed_on_phones_and_active_ones_become_removable_chips():
    ds.seed(ds.project(1, "stepan"))
    body = _get("/dashboard/requests", params={"type": "job", "status": "failed",
                                              "project": "1", "q": "job-3"}).text
    assert "open: window.innerWidth > 720 }" in body            # never forced open by a filter
    chips = body.split('class="fchips"')[1].split("</div>")[0]
    assert chips.count('class="fchip"') == 4
    assert 'data-ru="Тип"' in chips and 'data-ru="Очередь"' in chips and "stepan" in chips
    assert "type=job" not in chips.split('data-ru="Тип"')[0].rsplit("href=", 1)[1]   # its own link drops it
    assert 'class="fchips"' not in _get("/dashboard/requests").text


def test_select_labels_are_short_and_workflow_is_russian():
    body = _get("/dashboard/requests").text
    assert ">Queue</option>" in body and ">Direct</option>" in body
    assert ">Queued job</option>" not in body
    assert 'data-ru="Сценарий"' in body and 'data-ru-label="Время"' not in body.split("<tbody")[0]


def test_cards_are_translated_and_do_not_print_empty_placeholders():
    ds.seed(ds.project(1, "stepan"), ds.key(1))
    ds.seed(ds.usage(1, workflow=None, request_id="a" * 32), ds.job(5, "pending"))
    body = _get("/dashboard/requests", params={"range": "all"}).text
    assert 'data-ru-label="Время"' in body and 'data-ru-label="Цена"' in body
    assert 'data-ru-label="Токены"' in body
    assert "· —" not in body
    row = [r for r in body.split("<tr class=") if "no attempt yet" in r][0]
    assert "—</td>" not in row.split('data-label="Time"')[1].split("</td>")[0]   # no lone dash
    assert 'data-ru="ждала"' in row


def test_job_wait_uses_the_bilingual_duration_helper():
    ds.seed(ds.project(1))
    ds.seed(ds.job(5, "pending", minutes_ago=1))
    body = _get("/dashboard/requests", params={"range": "all"}).text
    assert 'data-ru="1 мин"' in body or 'data-ru="1 мин 0' in body
    assert "ждала 1m" not in body


def test_done_job_joined_by_a_prod_format_id_and_direct_calls_by_uuid_hex():
    ds.seed(ds.project(1), ds.key(1))
    uid = "0123456789abcdef0123456789abcdef"
    ds.seed(ds.usage(1, status="error", request_id="job-533735"),
            ds.usage(2, request_id="job-533735"), ds.usage(3, request_id=uid))
    ds.seed(ds.job(533735, "done", minutes_ago=3, started_at=ds.now() - timedelta(minutes=2),
                   completed_at=ds.now()))
    ids = _by_id(_rows())
    assert (ids["job-533735"]["tries"], ids["job-533735"]["state"], ids["job-533735"]["is_job"])         == (2, "ok", True)
    assert ids[uid]["is_job"] is False and len(ids) == 2
    assert [r["request_id"] for r in _rows(q=uid)] == [uid]
    assert [r["request_id"] for r in _rows(q="533735")] == ["job-533735"]
    html = _get("/dashboard/requests/job-533735", headers={"HX-Request": "true"}).text
    assert "Queued job" in html and html.count('class="n"') == 2
