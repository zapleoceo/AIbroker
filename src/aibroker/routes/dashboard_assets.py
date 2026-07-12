"""Static presentation assets for the admin dashboard — login page HTML,
and the dashboard CSS/JS served as long-cached versioned assets.

Pure strings, no logic and no per-request data: split out of `dashboard.py`
so the logic file is not buried under ~450 lines of markup. `_LOGIN_HTML` is
`.replace()`d (not `.format()`d) and the CSS/JS are served verbatim, so no
brace escaping is needed here.
"""
from __future__ import annotations

import hashlib

# Authenticated, always-fresh admin pages must never be cached — without this
# Chrome heuristic-caches the HTML (CF already serves it DYNAMIC) and shows a
# stale key list. Applied to every dashboard/login HTMLResponse.
_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

_LOGIN_HTML = """<!doctype html><html><head>
<meta charset="utf-8"><title>AIbroker — login</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="alternate icon" href="/favicon.ico">
<style>
  body { font-family:-apple-system, sans-serif; background:#0f1115; color:#e4e6eb;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
  .box { background:#1a1d24; padding:48px 40px; border-radius:14px; max-width:420px;
          text-align:center; border:1px solid #2a2d34; position:relative; }
  h1 { font-weight:500; font-size:28px; margin:0 0 8px; }
  p { color:#888; margin:6px 0 24px; font-size:14px; }
  .err { color:#f44336; margin-top:18px; font-size:13px; }
  .lang-toggle { position:absolute; top:14px; right:14px;
                display:inline-flex; background:#0f1115; border:1px solid #2a2d34;
                border-radius:6px; overflow:hidden;
                font-family:ui-monospace,monospace; font-size:11px; }
  .lang-toggle button { background:none; border:none; color:#888;
                       padding:5px 10px; cursor:pointer;
                       font-family:ui-monospace,monospace; font-size:11px; }
  .lang-toggle button.active { background:rgba(77,171,247,.12); color:#4dabf7; }
</style></head><body>
<div class="box">
  <span class="lang-toggle">
    <button type="button" data-lang="en" class="active">EN</button>
    <button type="button" data-lang="ru">RU</button>
  </span>
  <h1>AIbroker</h1>
  <p data-i18n
     data-en="Sign in with Telegram to administer"
     data-ru="Войти через Telegram, чтобы администрировать">Sign in with Telegram to administer</p>
  <script async src="https://telegram.org/js/telegram-widget.js?22"
          data-telegram-login="__BOT__"
          data-size="large"
          data-radius="8"
          data-auth-url="https://__HOST__/api/tg_login"
          data-request-access="write"></script>
  __ERR__
</div>
<script>
(function() {
  const KEY = "aib_lang";
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("lang");
  const fromStore = localStorage.getItem(KEY);
  let lang = (fromQuery === "ru" || fromQuery === "en") ? fromQuery
            : (fromStore === "ru" || fromStore === "en") ? fromStore
            : "en";
  function apply(l) {
    document.documentElement.lang = l;
    document.querySelectorAll("[data-i18n]").forEach(el => {
      const txt = el.getAttribute("data-" + l);
      if (txt !== null) el.textContent = txt;
    });
    document.querySelectorAll(".lang-toggle button").forEach(b => {
      b.classList.toggle("active", b.dataset.lang === l);
    });
    localStorage.setItem(KEY, l);
  }
  document.querySelectorAll(".lang-toggle button").forEach(b => {
    b.addEventListener("click", () => apply(b.dataset.lang));
  });
  apply(lang);
})();
</script>
</body></html>"""

_DASHBOARD_CSS = """
body { font-family:-apple-system, sans-serif; background:#0f1115; color:#e4e6eb;
       margin:0; padding:24px; max-width:1280px; margin-inline:auto; }
nav { display:flex; gap:24px; align-items:center; margin-bottom:24px; }
nav h1 { margin:0; font-weight:500; font-size:22px; }
nav .right { margin-left:auto; }
nav a { color:#4dabf7; text-decoration:none; font-size:13px; margin-left:14px; }
h2 { font-weight:500; font-size:18px; margin:32px 0 12px; color:#aaa; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
         gap:12px; margin:14px 0; }
.card { background:#1a1d24; padding:16px; border-radius:10px; border:1px solid #2a2d34; }
.card-label { font-size:10px; color:#888; text-transform:uppercase; letter-spacing:.05em; }
.card-value { font-size:26px; font-weight:600; margin-top:4px; }
.card-sub { font-size:12px; color:#888; margin-top:2px; }
table { width:100%; border-collapse:collapse; background:#1a1d24; border-radius:10px;
         overflow:hidden; margin:12px 0; }
th, td { padding:8px 12px; text-align:left; border-bottom:1px solid #2a2d34; font-size:13px; }
th { background:#0f1115; color:#888; text-transform:uppercase; font-size:11px; font-weight:500; }
td.mono, code { font-family:ui-monospace, monospace; color:#4dabf7; font-size:12px; }
.ok { color:#4caf50; } .bad { color:#f44336; } .warn { color:#ffd84a; }
/* Cost column: dim free $0.0000 rows, brighten rows that actually spent money. */
.cost-zero { color:#565b66; } .cost-pos { color:#f0f2f5; font-weight:600; }
/* Per-key scope pills: every known scope shown; enabled bright, disabled dim —
   the key's toggles are readable in the table without opening the edit form. */
.sc { display:inline-block; padding:1px 5px; border-radius:6px; font-size:10px;
      margin:0 2px 1px 0; white-space:nowrap; }
.sc-on  { background:rgba(77,171,247,.15); color:#4dabf7; }
.sc-off { color:#3a3f4a; border:1px solid #23262e; }
.sc-rsv { color:#ffd84a; border:1px solid #4a4326; }
.status-detail { font-size:10px; color:#888; margin-top:2px; white-space:nowrap;
                 max-width:160px; overflow:hidden; text-overflow:ellipsis; }
.pill { display:inline-block; padding:2px 8px; border-radius:8px; font-size:11px;
         background:#0f1115; border:1px solid #2a2d34; }
form.inline { display:inline; }
button, input, select { font:inherit; }
button { background:#1a1d24; color:#e4e6eb; border:1px solid #2a2d34; border-radius:6px;
          padding:5px 10px; font-size:12px; cursor:pointer; }
button:hover { background:#2a2d34; }
button.danger { color:#f44336; }
input, select { background:#0f1115; color:#e4e6eb; border:1px solid #2a2d34;
                 border-radius:6px; padding:6px 10px; font-size:13px; }
.flash { background:#1a3d1a; border:1px solid #2a5d2a; color:#4caf50; padding:10px 14px;
          border-radius:8px; margin-bottom:18px; font-size:13px; }
.flash.err { background:#3d1a1a; border-color:#5d2a2a; color:#f44336; }
fieldset { background:#1a1d24; border:1px solid #2a2d34; border-radius:10px;
            padding:14px 18px; margin:12px 0; }
legend { color:#aaa; padding:0 8px; font-size:12px; }
.row-form { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:6px; }
.row-form input, .row-form select { min-width:120px; }
.provider { display:inline-block; margin:4px 6px 4px 0; padding:5px 10px;
             background:#1a1d24; border:1px solid #2a2d34; border-radius:6px; font-size:12px; }
/* Sortable headers */
th.sortable { cursor:pointer; user-select:none; position:relative; padding-right:18px; }
th.sortable:hover { color:#e4e6eb; background:#13161c; }
th.sortable::after { content:"↕"; position:absolute; right:6px; opacity:.3; font-size:10px; }
th.sortable.asc::after  { content:"↑"; opacity:1; color:#4dabf7; }
th.sortable.desc::after { content:"↓"; opacity:1; color:#4dabf7; }
/* Inline edit row */
tr.editing td { background:#13161c; }
tr.edit-row { display:none; }
tr.edit-row.active { display:table-row; background:#13161c; }
tr.edit-row td { padding:14px 12px; }
tr.edit-row input, tr.edit-row select { min-width:90px; }
/* Scope checkboxes */
.scope-group { display:inline-flex; gap:8px; flex-wrap:wrap;
              padding:4px 8px; border:1px solid #2a2d34; border-radius:6px;
              background:#0f1115; }
.scope-group .scope-cb { display:inline-flex; align-items:center; gap:4px;
                         font-family:ui-monospace,monospace; font-size:11px;
                         color:#888; cursor:pointer; }
.scope-group .scope-cb input { margin:0; min-width:0; padding:0; }
.scope-group .scope-cb input:checked + * { color:#4dabf7; }
/* Manual quota override cluster (4 narrow number inputs) */
.quota-override { display:inline-flex; gap:4px; align-items:center;
                 padding:3px 6px; border:1px dashed #2a2d34; border-radius:6px; }
.quota-override input { width:78px; min-width:0; font-size:11px;
                       font-family:ui-monospace,monospace; padding:4px 6px; }
/* Cap bar */
.cap-bar { display:inline-block; width:80px; height:6px; background:#0f1115;
            border-radius:3px; vertical-align:middle; margin-left:6px; overflow:hidden; }
.cap-bar .fill { display:block; height:100%; background:#4dabf7; }
.cap-bar .fill.warn { background:#ffd84a; }
.cap-bar .fill.bad  { background:#f44336; }
/* Project drill-down */
.proj-link { color:#e4e6eb; text-decoration:none; border-bottom:1px dotted #4dabf7; }
.proj-link:hover { color:#4dabf7; }
.breadcrumb { font-size:12px; color:#888; margin:6px 0 18px; }
.breadcrumb a { color:#4dabf7; }
.breakdown { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
            gap:12px; margin:8px 0 20px; }
.brk-card { background:#1a1d24; border:1px solid #2a2d34; border-radius:10px;
           padding:14px 16px; }
.brk-card h3 { margin:0 0 10px; font-size:11px; color:#888;
              text-transform:uppercase; letter-spacing:.05em; font-weight:500; }
.brk-card table { margin:0; background:none; border-radius:0; }
.brk-card td { padding:4px 8px 4px 0; border:none; font-size:12px; color:#aaa; }
.brk-card td.num { color:#e4e6eb; text-align:right; font-family:ui-monospace,monospace; }
.brk-card td.k { color:#4dabf7; font-family:ui-monospace,monospace; }
.brk-card .total-row { border-top:1px solid #2a2d34; }
.brk-card .total-row td { padding-top:6px; color:#e4e6eb; font-weight:600; }
.brk-card-split { display:flex; flex-direction:column; }
.brk-card-split .brk-section + .brk-section {
  margin-top:12px; padding-top:12px; border-top:1px solid #2a2d34;
}
.spark { display:block; vertical-align:middle; }
.range-pills { display:inline-flex; gap:6px; margin-left:10px; vertical-align:middle; }
.range-pills a { font-size:11px; padding:3px 9px; border-radius:4px;
               background:#1a1d24; border:1px solid #2a2d34;
               color:#888; text-decoration:none; font-family:ui-monospace,monospace; }
.range-pills a.active { background:rgba(77,171,247,.12); color:#4dabf7;
                       border-color:#4dabf7; }
.recent-table td.status-ok { color:#4caf50; }
.recent-table td.status-error { color:#f44336; }
.recent-table td.status-rate_limit { color:#ffd84a; }
.recent-table td.status-auth_fail { color:#ff8a00; }
/* Date-range picker on main dashboard */
.range-form { display:flex; align-items:center; gap:8px; margin:0 0 14px;
              flex-wrap:wrap; font-size:13px; color:#888; }
.range-form label { font-family:ui-monospace,monospace; font-size:11px;
                    text-transform:uppercase; letter-spacing:.05em; color:#5a6171; }
.range-form input[type="date"] { font-family:ui-monospace,monospace;
                                 font-size:12px; padding:5px 8px; min-width:130px; }
.range-form .range-reset { color:#888; font-size:12px; text-decoration:none;
                           font-family:ui-monospace,monospace; padding:5px 10px;
                           border:1px solid #2a2d34; border-radius:6px; }
.range-form .range-quick { margin-left:10px; display:inline-flex; gap:6px; }
.range-form .range-quick a { font-size:11px; padding:3px 9px; border-radius:4px;
                             background:#1a1d24; border:1px solid #2a2d34;
                             color:#888; text-decoration:none;
                             font-family:ui-monospace,monospace; }
.range-form .range-quick a:hover { color:#4dabf7; border-color:#4dabf7; }
/* Selected-range indicator: 'all time' and the quick today/7d/30d links share
   the same active look (regression: 'all time' used to render permanently
   blue regardless of state, and today/7d/30d never got any indicator). */
.range-form .range-reset.active,
.range-form .range-quick a.active { background:rgba(77,171,247,.12);
                                    color:#4dabf7; border-color:#4dabf7; }
/* Totals row */
tfoot td { background:#0f1115; font-weight:600; color:#e4e6eb;
           border-top:2px solid #2a2d34; padding:10px 12px; font-size:12px; }
tfoot td.k { color:#888; text-transform:uppercase; letter-spacing:.05em;
             font-size:11px; }
tfoot td.num { font-family:ui-monospace,monospace; color:#4dabf7; }
/* Row number — CSS counter so it stays correct after client-side re-sort.
   id is the DB identifier (has gaps from deletions); # is the visible count. */
tbody { counter-reset: rownum; }
tr.data-row { counter-increment: rownum; }
td.rownum { width:30px; text-align:right; color:#5a6171;
           font-family:ui-monospace,monospace; font-size:11px; }
td.rownum::before { content: counter(rownum); }
/* Provider hint under add-key form */
.provider-hint { margin-top:10px; padding:10px 12px; border-radius:6px;
                background:#0f1115; border:1px dashed #2a2d34;
                color:#888; font-size:12px; line-height:1.55; }
.provider-hint b { color:#4dabf7; font-family:ui-monospace,monospace; font-weight:500; }
.provider-hint .cap-tag { display:inline-block; margin:2px 4px 2px 0; padding:1px 7px;
                         font-family:ui-monospace,monospace; font-size:11px;
                         background:#1a1d24; color:#4dabf7; border-radius:3px; }
.provider-hint table { width:auto; background:none; margin:6px 0 0; border-radius:0; overflow:visible; }
.provider-hint td { padding:2px 12px 2px 0; border:none; font-size:12px; color:#aaa; }
.provider-hint td.mono { font-family:ui-monospace,monospace; color:#4dabf7; }
/* Lang toggle */
.lang-toggle { display:inline-flex; background:#1a1d24; border:1px solid #2a2d34;
              border-radius:6px; overflow:hidden; font-family:ui-monospace,monospace;
              font-size:11px; margin-right:10px; vertical-align:middle; }
.lang-toggle button { background:none; border:none; color:#888;
              padding:5px 10px; cursor:pointer; font-family:ui-monospace,monospace;
              font-size:11px; }
.lang-toggle button.active { background:rgba(77,171,247,.12); color:#4dabf7; }
[data-i18n].lang-hidden { display:none !important; }
"""

_DASHBOARD_JS = """
// Timezone cookie — tell the server the viewer's zone so DAY-bucketed figures
// ('today', per-range spend, per-key quota bars) align to the viewer's calendar
// day, not UTC. Set before anything else; on the very first visit (no cookie
// yet) reload once so the server can use it. Point-in-time labels are localised
// client-side below and need no reload. A changed zone updates silently.
(function() {
  const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "";
  if (!tz) return;
  const m = document.cookie.match(/(?:^|; )aib_tz=([^;]+)/);
  const cur = m ? decodeURIComponent(m[1]) : null;
  if (cur === tz) return;
  document.cookie = "aib_tz=" + encodeURIComponent(tz) + "; path=/; max-age=31536000; SameSite=Lax";
  if (cur === null) location.reload();
})();
// Lang toggle — same pattern as landing page. Default EN, persisted in localStorage.
(function() {
  const KEY = "aib_lang";
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("lang");
  const fromStore = localStorage.getItem(KEY);
  let lang = (fromQuery === "ru" || fromQuery === "en") ? fromQuery
            : (fromStore === "ru" || fromStore === "en") ? fromStore
            : "en";

  function apply(l) {
    document.documentElement.lang = l;
    document.querySelectorAll("[data-i18n]").forEach(el => {
      const txt = el.getAttribute("data-" + l);
      if (txt !== null) el.textContent = txt;
    });
    document.querySelectorAll("input[data-en], input[data-ru]").forEach(el => {
      const ph = el.getAttribute("data-" + l + "-placeholder");
      if (ph !== null) el.placeholder = ph;
    });
    document.querySelectorAll("[data-en-placeholder], [data-ru-placeholder]").forEach(el => {
      const ph = el.getAttribute("data-" + l + "-placeholder");
      if (ph !== null) el.placeholder = ph;
    });
    document.querySelectorAll(".lang-toggle button").forEach(b => {
      b.classList.toggle("active", b.dataset.lang === l);
    });
    localStorage.setItem(KEY, l);
  }
  document.querySelectorAll(".lang-toggle button").forEach(b => {
    b.addEventListener("click", () => apply(b.dataset.lang));
  });
  apply(lang);
})();
// Timezone — server renders every timestamp as UTC in <span class="ts" data-utc>;
// rewrite each into the VIEWER's local timezone so the operator reads times in
// their own zone, not the server's. data-tf picks which fields to show. No-JS
// falls back to the server-rendered UTC text.
(function() {
  const F = {
    hm:    { hour: "2-digit", minute: "2-digit", hour12: false },
    mdhm:  { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false },
    mdhms: { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false },
  };
  function localize(root) {
    (root || document).querySelectorAll("span.ts[data-utc]").forEach(el => {
      const d = new Date(el.getAttribute("data-utc"));
      if (isNaN(d.getTime())) return;
      el.title = el.getAttribute("data-utc");            // keep UTC on hover
      el.textContent = d.toLocaleString([], F[el.getAttribute("data-tf")] || F.mdhms);
    });
  }
  localize(document);
})();
// Provider → scope + models hint, driven by JSON inlined into the page.
(function() {
  const metaEl = document.getElementById("provider-meta");
  const sel    = document.getElementById("add-key-provider");
  const scope  = document.getElementById("add-key-scope");
  const hint   = document.getElementById("provider-hint");
  if (!metaEl || !sel || !scope || !hint) return;
  const META = JSON.parse(metaEl.textContent);

  // Stash original hint so we can restore it
  const originalHint = hint.cloneNode(true);

  function langStrings() {
    const l = document.documentElement.lang || "en";
    return l === "ru"
      ? { chosen: "Выбран:", scope: "scope:", models: "модели, которые будет вызывать брокер:", capabilities: "способности:" }
      : { chosen: "Picked:",  scope: "scope:", models: "models the broker will route through this key:", capabilities: "capabilities:" };
  }

  function render(provider) {
    const m = META[provider];
    if (!m) {
      hint.replaceWith(originalHint.cloneNode(true));
      return;
    }
    // Auto-set scope checkboxes: tick default; tick llm:edit too if this
    // provider is in the chat:edit chain so operator notices the option.
    const wantEdit = m.capabilities.includes("chat:edit");
    scope.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.checked = (cb.value === m.default_scope) || (wantEdit && cb.value === "llm:edit");
    });

    const t = langStrings();
    const capChips = m.capabilities.map(c =>
      '<span class="cap-tag">' + c + '</span>').join("");
    const modelRows = Object.entries(m.models).map(([cap, model]) =>
      '<tr><td>' + cap + '</td><td class="mono">' + model + '</td></tr>').join("");

    hint.innerHTML =
      '<div>' + t.chosen + ' <b>' + provider + '</b> · ' +
      t.scope + ' <code>' + m.default_scope + '</code></div>' +
      '<div style="margin-top:6px">' + t.capabilities + ' ' + capChips + '</div>' +
      '<div style="margin-top:8px">' + t.models + '</div>' +
      '<table>' + modelRows + '</table>';
  }

  sel.addEventListener("change", () => render(sel.value));
  // Re-render on lang toggle so labels follow EN/RU
  document.querySelectorAll(".lang-toggle button").forEach(b => {
    b.addEventListener("click", () => {
      if (sel.value) setTimeout(() => render(sel.value), 0);
    });
  });
})();
// Click-to-sort tables. <th class="sortable" data-type="num|text|date"> opt-in.
(function() {
  function cellValue(tr, idx, kind) {
    const td = tr.children[idx];
    const raw = (td.dataset.sort !== undefined) ? td.dataset.sort : td.textContent.trim();
    if (kind === "num") return parseFloat(raw.replace(/[$,]/g,"")) || 0;
    return raw.toLowerCase();
  }
  document.querySelectorAll("th.sortable").forEach((th, idx) => {
    const colIdx = Array.from(th.parentNode.children).indexOf(th);
    th.addEventListener("click", () => {
      const table = th.closest("table");
      const tbody = table.tBodies[0];
      // Only sort .data-row tbody rows (skip inline edit-row)
      const rows = Array.from(tbody.querySelectorAll("tr.data-row"));
      const kind = th.dataset.type || "text";
      const asc = !th.classList.contains("asc");
      table.querySelectorAll("th.sortable").forEach(o => o.classList.remove("asc","desc"));
      th.classList.add(asc ? "asc" : "desc");
      rows.sort((a, b) => {
        const va = cellValue(a, colIdx, kind);
        const vb = cellValue(b, colIdx, kind);
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ?  1 : -1;
        return 0;
      });
      rows.forEach(r => {
        // Move data row + its edit-row partner together
        const partner = tbody.querySelector('tr.edit-row[data-edit-for="' + r.dataset.rowId + '"]');
        tbody.appendChild(r);
        if (partner) tbody.appendChild(partner);
      });
    });
  });

  // Inline edit toggle
  document.querySelectorAll("button[data-edit-toggle]").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.editToggle;
      const editRow = document.querySelector('tr.edit-row[data-edit-for="' + id + '"]');
      const dataRow = document.querySelector('tr.data-row[data-row-id="' + id + '"]');
      if (!editRow) return;
      const wasActive = editRow.classList.contains("active");
      // Close any other open editors first
      document.querySelectorAll("tr.edit-row.active").forEach(r => {
        r.classList.remove("active");
        const peer = document.querySelector('tr.data-row[data-row-id="' + r.dataset.editFor + '"]');
        if (peer) peer.classList.remove("editing");
      });
      if (!wasActive) {
        editRow.classList.add("active");
        dataRow.classList.add("editing");
      }
    });
  });
})();
// Delegated confirm for destructive forms. The prompt text lives in a
// data-confirm attribute (HTML-attribute-escaped server-side) and is read as a
// plain string via dataset — never interpolated into inline JS, so a key label
// containing a quote can't break out of / inject into the handler.
(function() {
  document.querySelectorAll("form[data-confirm]").forEach(f => {
    f.addEventListener("submit", e => {
      if (!window.confirm(f.dataset.confirm)) e.preventDefault();
    });
  });
})();
"""


# Cache-bust the long-cached (immutable, 1y) CSS/JS by a hash of their OWN
# content, not the package __version__ — an asset edit without a version bump
# used to ship to the server but never reach browsers (they kept the immutable
# ?v=<version> copy). Content-addressed, so any CSS/JS change auto-invalidates.
ASSETS_VERSION = hashlib.sha256(
    (_DASHBOARD_CSS + _DASHBOARD_JS).encode()
).hexdigest()[:12]
