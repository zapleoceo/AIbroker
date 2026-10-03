# Admin dashboard (UI)

Server-rendered: Jinja2 templates (`src/aibroker/web/templates`), HTMX + Alpine.js and
uPlot **vendored** under `src/aibroker/web/static/vendor` (htmx 2.0.4, alpinejs 3.14.9,
uplot 1.6.32 — no third-party CDN at runtime; only the Telegram login widget loads from
telegram.org, on `/login`). One hand-written stylesheet (`css/app.css`) on design tokens
(`css/tokens.css`, light/dark via `prefers-color-scheme`); the landing and health pages
use the same tokens (`css/public.css`). No Node build.

Static files are served at `/dashboard/static/<path>?v=<ASSETS_VERSION>` (long-cached,
content-hashed, `dashboard_static`). `render` / `render_html` (web/render.py) render
templates; `build_env` builds the Jinja environment; formatters live in web/format.py
(`num`, `pct`, `ms`, `ms_ru`, `ago`, `age`, `spark`, `time_tag`, `iso_utc`; the `ms_t` / `age_t` filters emit both languages).

## Pages (left rail on desktop, bottom tab bar + "More" sheet on phones)

| Page | Route | Notes |
|---|---|---|
| Overview | `/dashboard` (`overview`) | KPI strip with sparklines and delta vs previous period, stacked usage-by-model chart (spend/calls), "needs attention" list, provider health grid, top projects, job queue summary whose tiles link to the filtered Requests page |
| Requests | `/dashboard/requests` (`requests_page`) | **one row per client request**, queued jobs included (the former Jobs page). Slim sticky live queue strip (`requests_queue_strip`, HTMX poll every 10 s: one line of chips for pending, running, failed 24 h, oldest pending; each chip is a filter link). The filter panel stays collapsed on phones; active filters show as removable chips under it. Columns: time, project · workflow, capability, final status, "N tries → provider/model", queue wait + response time (jobs wait; direct calls only respond), cost and tokens summed over the attempts. Filters: range picker, project, workflow, capability, provider and model (any attempt), status (`ok`/`failed`/`pending`/`running`), type (`job`/`direct`), request-id search (`q`), "Only failed". Server-side sort and "load more"; CSV `/dashboard/requests.csv` (`requests_csv`, one row per request, capped, formula-injection safe) |
| Request drawer | `/dashboard/requests/{ref}` (`request_drawer`) | `ref` is a usage_log id or `job-<id>`. Status, copyable request id, job block (queued at, waited, retries, final error), the attempt trail (provider, key, model served, latency, cost, tokens, cache) and totals; `?open=<ref>` deep-links |
| Projects | `/dashboard/projects` (`projects_page`) | cards: today's spend vs daily cap, lifetime-request meter (`request_cap_view`, only when a cap is set), cache-hit badge, sparkline, a **self-signup** badge for projects created via `POST /v1/signup` and a `?signup=1` filter chip; "New project" drawer (`project_new_form`) |
| Project detail | `/dashboard/projects/{id}?tab=usage\|models\|keys\|settings` (`project_detail`, `render_project_detail`; `render_projects` re-renders the list after create) | settings: edit (incl. **lifetime request cap**, blank = unlimited, with a used/limit progress bar), rotate token, delete |
| Providers | `/dashboard/providers` (`providers_page`) | **Keys and Models merged into one page.** Toolbar: search (model id or provider), capability filter chips (`capability_filters`, derived from the registry: Chat, Structured, Vision, Voice, Embeddings, Decisions), "Only with live keys", "Add key". Then one card per provider (`build_providers`; live keys first, then registry rank): free/paid badge, status dot, "N of M keys alive", paused / dead / disabled counts, aggregate quota-burn bar (`_quota_burn`), errors in the last hour. Two Alpine tabs per card, last tab (and collapsed state) remembered per provider in localStorage: **Models** (capability chips, copy-able pin id, price per token / per minute / free from `catalog.price_info`, 7-day p50 and success, `rotation` / `unrouted` hints, "N keys cooling") and **Keys** (status chip, cooldown countdown, a `model cooling` line per running `api_key_model_cooldowns` row, quota meters, last success/error; Test / Edit / Enable-Disable / Delete). Search or a capability filter shows only matching models and opens the Models tab; filters seed from `?q=&cap=&live=1`. Providers with no keys and no routed model fold under "Inactive providers". `/dashboard/keys` (`keys_page_moved`) and `/dashboard/models` (`models_page_moved`) answer 301 to it (query string kept, the old `provider` filter becomes `q`) |
| Add/edit key drawer | `/dashboard/keys/new` (`key_new_form`), `/dashboard/keys/{id}/edit` (`key_edit_form`) | advanced quota overrides collapsed |
| Audit log | `/dashboard/audit` | `audit_log`, actor/action filters, keyset paging |
| Settings | `/dashboard/settings` (`settings_page`) | version, caps, links to `/docs` and `/v1/health`, language, logout |

The header range picker (today / 7d / 30d / all / custom from-to) is one `DateRange`
(`resolve_range`, `range_links`) carried in the query string on every page; default 7d.
Bounds are the viewer's calendar days (aib_tz cookie).

Language is applied client-side: `data-i18n` elements (`t()`, `tn()`) carry `data-en`/`data-ru`, and the JS re-translates the whole document after every HTMX swap (drawers, load-more, the polled queue strip). A `<select>` option must use the `option` macro in `_macros.html` (i18n attributes on the option itself), since a span inside an option is dropped by the HTML parser.

## Actions (POST, owner session or X-Admin-Key; unchanged semantics)

`/dashboard/keys/create|{id}/edit|{id}/disable|{id}/delete`,
`/dashboard/projects/create|{id}/edit|{id}/delete`. New: `/dashboard/keys/{id}/test`
(`dash_test_key`: one probe, answers a status chip, audited as `key.test`) and
`/dashboard/projects/{id}/rotate-token` (`dash_rotate_project_token`: new key shown once,
audited as `project.rotate_token`). Forms may send a hidden `next` (a plain `/dashboard…`
path, else ignored) to land back on the page they came from.

## Data layer

`routes/dashboard_queries.py` — read-only, **portable** SQL (Postgres and SQLite):
`range_totals`, `latency_percentile`, `time_series`, `usage_by_model_series`,
`provider_activity`, `project_range_stats`, `project_breakdown`, `request_facets`,
`get_request`, `request_attempts`, `get_job`, `key_activity`, `model_cooldowns`, `gather` (parallel on Postgres, sequential on SQLite), `observed_model_stats`, `job_overview`, `audit_page`, `bucket_starts`,
`floor_bucket`. `routes/dashboard_views.py` shapes rows into view-models (pure functions:
`build_kpis`, `build_attention`, `build_key_rows`, `group_by_provider`,
`build_project_cards`, `build_providers`, `capability_filters`, `cap_group`, `pct_change`);
`routes/dashboard_labels.py` holds friendly error labels and `key_status` (`KeyStatus`,
`reason_labels`); `provider_catalogue` drives the add-key drawer.

### One row per client request

`routes/dashboard_requests.py` builds the Requests list (`query_requests`, `RequestFilter`,
`search_target`, `request_detail`). `usage_log` has one row per provider **attempt**; the
attempts of one request share `request_id` (a uuid for direct calls, `job-<id>` for queued
jobs, migration 015) and are aggregated in the database with `GROUP BY`. Rows with a NULL
`request_id` (older than the migration) are each their own request (`u-<usage id>`). Queued
jobs are LEFT JOINed on `'job-' || deep_jobs.id`, and a job with no attempt yet still appears
straight from `deep_jobs`, so `pending` / `running` work is visible. The final state is the
job's own state while it is live or once it ended, otherwise `ok` iff some attempt succeeded.

Cost control: only the requested page is joined to its served attempt, project and key, and
the newest-first page is cut from a recent slice (1 d, 7 d, 30 d, then the whole range) that
reaches one day further back than it keeps, so a request started just before the cut is still
aggregated whole. On 1M `usage_log` rows the default view answers in about 0.2 s (EXPLAIN
shows `ix_usage_created_at`, and `ix_usage_request_id` for the id search and job lookups);
a rare filter (e.g. one provider over the whole retention window) still scans the range.
`/dashboard/jobs` answers 301 to `/dashboard/requests?type=job` (`jobs_page_moved`).

## Landing and health

`/` is generated from the provider registry and routing tables: `wired_providers` (registered provider AND a seat in a
chain — mistral is absent), `client_endpoints` (read from the real `/v1` router) and
`llms_text` (`/llms.txt`). The provider list is deliberately not derived from which
providers hold active keys (the public pages must not reveal per-provider key state).

## Self-signup projects and the request cap (2026-10-03)

`projects.total_request_cap` (NULL = unlimited, all pre-existing projects) and
`total_requests_used` back the lifetime cap; `POST /v1/signup` creates projects with `$0/day` and
100 requests (see [api.md](api.md#quick-start-self-signup)). The project form has a "Lifetime request cap" field;
`dash_edit_project` only applies it when the form carries the `req_cap_present` marker (FastAPI
turns a blank form value into "absent", so a blank value alone cannot mean "unlimited" - this also
keeps an older client from silently lifting a self-signup cap). `_parse_request_cap` accepts a whole
number >= 0 (0 blocks every request). The daily cost cap stays required on create (`0` = free-only).
Raising the cap takes effect on the next request (`admit_request` re-reads the live row).

## Review fixes (2026-10-03)

- **Caps are never unlimited by default.** Creating a project requires `daily_cost_cap_usd`
  (prefilled 0.20). In `cost_guard` `NULL` = no cap, `0` = free-only (every paid call's cost
  estimate exceeds 0, free calls cost 0 and pass); the form says so. Server-side, a blank cap on
  create is refused.
- **One-time secrets are refresh-safe.** Project create and `rotate-token` answer
  POST -> 303; the new key travels in a 5-minute encrypted HttpOnly cookie (`set_once` /
  `take_once` in dashboard_pages.py, stateless so both workers work) and is shown once, then the
  cookie is deleted.
- **Plurals.** `plural_en` / `plural_ru` (web/format.py) and the `tn()` template global give
  "1 error / 2 errors" and "1 ошибка / 2 ошибки / 5 ошибок".
- **Request drawer.** `request_attempts` groups by `usage_log.request_id` (migration 015) and falls back to the timing inference only for rows with a NULL id.
