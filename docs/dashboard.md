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
(`num`, `pct`, `ms`, `ago`, `spark`, `time_tag`, `iso_utc`).

## Pages (left rail on desktop, bottom tab bar + "More" sheet on phones)

| Page | Route | Notes |
|---|---|---|
| Overview | `/dashboard` (`overview`) | KPI strip with sparklines and delta vs previous period, stacked usage-by-model chart (spend/calls), "needs attention" list, provider health grid, top projects, job queue summary |
| Requests | `/dashboard/requests` (`requests_page`) | filters (project, workflow, capability, provider, model, status, min cost/latency, request id), server-side sort, "load more", CSV export `/dashboard/requests.csv` (`requests_csv`, capped, formula-injection safe) |
| Request drawer | `/dashboard/requests/{id}` (`request_drawer`) | tokens in/out/cache, cost, latency, error label, `model_served`, attempt trail; `?open=<id>` deep-links |
| Projects | `/dashboard/projects` (`projects_page`) | cards: today's spend vs daily cap, cache-hit badge, sparkline; "New project" drawer (`project_new_form`) |
| Project detail | `/dashboard/projects/{id}?tab=usage\|models\|keys\|settings` (`project_detail`, `render_project_detail`; `render_projects` re-renders the list after create) | settings: edit, rotate token, delete |
| Keys & providers | `/dashboard/keys` (`keys_page`) | grouped by provider; status chip, cooldown countdown, quota burn per axis, last success/error; Test / Edit / Enable-Disable / Delete |
| Add/edit key drawer | `/dashboard/keys/new` (`key_new_form`), `/dashboard/keys/{id}/edit` (`key_edit_form`) | advanced quota overrides collapsed |
| Models | `/dashboard/models` (`models_page`) | catalogue from `providers/catalog` (`price_info`) and the registry, with the capability chains: capability, provider, id (copy), price in/out per 1M, 7-day p50 latency and success; unrouted models flagged |
| Jobs | `/dashboard/jobs` (`jobs_page`) | `deep_jobs` states, stuck-queue warning |
| Audit log | `/dashboard/audit` | `audit_log`, actor/action filters, keyset paging |
| Settings | `/dashboard/settings` (`settings_page`) | version, caps, links to `/docs` and `/v1/health`, language, logout |

The header range picker (today / 7d / 30d / all / custom from-to) is one `DateRange`
(`resolve_range`, `range_links`) carried in the query string on every page; default 7d.
Bounds are the viewer's calendar days (aib_tz cookie).

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
`provider_activity`, `project_range_stats`, `project_breakdown`, `query_requests`
(`RequestFilter`), `request_facets`, `get_request`, `request_attempts` (`link_attempts`),
`key_activity`, `gather` (parallel on Postgres, sequential on SQLite), `observed_model_stats`, `job_overview`, `audit_page`, `bucket_starts`,
`floor_bucket`. `routes/dashboard_views.py` shapes rows into view-models (pure functions:
`build_kpis`, `build_attention`, `build_key_rows`, `group_by_provider`,
`build_project_cards`, `build_model_catalogue`, `pct_change`);
`routes/dashboard_labels.py` holds friendly error labels and `key_status` (`KeyStatus`,
`reason_labels`); `provider_catalogue` drives the add-key drawer.

### Attempt trail (honest limitation)

`usage_log` has no request/lease/job id linking the attempts of one request (`lease_id`
is always NULL). The drawer therefore **infers** the trail: same project, workflow and
capability, each attempt starting (created_at − latency) within 5 s of the previous
failure's end. Concurrent identical requests can in theory be mixed; the UI says so.

## Landing and health

`/` is generated from the provider registry and routing tables: `wired_providers` (registered provider AND a seat in a
chain — mistral is absent), `client_endpoints` (read from the real `/v1` router) and
`llms_text` (`/llms.txt`). The provider list is deliberately not derived from which
providers hold active keys (the public pages must not reveal per-provider key state).

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
