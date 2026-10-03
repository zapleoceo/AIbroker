/* AIbroker admin UI behaviour. Plain JS + Alpine components + HTMX hooks.
   Loaded (deferred) BEFORE Alpine so the alpine:init listener below fires. */
(function () {
  "use strict";

  // ── Timezone cookie ─────────────────────────────────────────────────────
  // Tell the server the viewer's zone so day-bucketed figures ('today', the
  // per-key quota bars) follow the viewer's calendar day. Reload once on the
  // very first visit so the server can use it; a later change updates silently.
  try {
    var tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "";
    if (tz) {
      var m = document.cookie.match(/(?:^|; )aib_tz=([^;]+)/);
      var cur = m ? decodeURIComponent(m[1]) : null;
      if (cur !== tz) {
        document.cookie = "aib_tz=" + encodeURIComponent(tz) + "; path=/; max-age=31536000; SameSite=Lax";
        if (cur === null && document.body && document.body.dataset.reloadOnTz !== "off") location.reload();
      }
    }
  } catch (e) { /* cookies blocked: UTC days */ }

  // ── Language (EN/RU) ────────────────────────────────────────────────────
  var LANG_KEY = "aib_lang";
  function storedLang() {
    var q = new URLSearchParams(location.search).get("lang");
    if (q === "ru" || q === "en") return q;
    try { var s = localStorage.getItem(LANG_KEY); if (s === "ru" || s === "en") return s; } catch (e) {}
    return "en";
  }
  function applyLang(lang, root) {
    root = root || document;
    document.documentElement.lang = lang;
    root.querySelectorAll("[data-i18n]").forEach(function (el) {
      var t = el.getAttribute("data-" + lang);
      if (t !== null) el.textContent = t;
    });
    root.querySelectorAll("[data-en-placeholder]").forEach(function (el) {
      var t = el.getAttribute("data-" + lang + "-placeholder");
      if (t !== null) el.placeholder = t;
    });
    root.querySelectorAll("[data-en-title]").forEach(function (el) {
      var t = el.getAttribute("data-" + lang + "-title");
      if (t !== null) { el.title = t; el.setAttribute("aria-label", t); }
    });
    document.querySelectorAll(".lang button").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.lang === lang ? "true" : "false");
    });
  }
  function setLang(lang) {
    try { localStorage.setItem(LANG_KEY, lang); } catch (e) {}
    applyLang(lang);
  }

  // ── Timestamps → viewer's timezone ──────────────────────────────────────
  var TF = {
    hm: { hour: "2-digit", minute: "2-digit", hour12: false },
    mdhm: { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false },
    mdhms: { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false },
    full: { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }
  };
  function localizeTimes(root) {
    (root || document).querySelectorAll("time.ts[datetime]").forEach(function (el) {
      var d = new Date(el.getAttribute("datetime"));
      if (isNaN(d.getTime())) return;
      el.textContent = d.toLocaleString([], TF[el.dataset.tf] || TF.mdhms);
    });
  }

  function refresh(root) {
    applyLang(storedLang(), root);
    localizeTimes(root);
  }

  // ── Drawer / modal (native <dialog>: focus trap + Esc come for free) ────
  function drawer() { return document.getElementById("drawer"); }
  function openDrawer() { var d = drawer(); if (d && !d.open) d.showModal(); }
  function closeDrawer() { var d = drawer(); if (d && d.open) d.close(); }

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!(t instanceof Element)) return;
    // backdrop click closes a drawer/modal
    if (t.tagName === "DIALOG" && t.open) { t.close(); return; }
    if (t.closest("[data-close]")) { var dlg = t.closest("dialog"); if (dlg) dlg.close(); return; }
    var lb = t.closest(".lang button");
    if (lb) { setLang(lb.dataset.lang); return; }
    var cp = t.closest("[data-copy]");
    if (cp) { copyText(cp.getAttribute("data-copy"), cp); }
  });

  // Confirm dialog for destructive forms: <form data-confirm="Delete x?">.
  // The text is read from the attribute as a plain string, never executed.
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!(f instanceof HTMLFormElement) || !f.dataset.confirm || f.dataset.confirmed) return;
    e.preventDefault();
    var dlg = document.getElementById("confirm");
    if (!dlg) { if (window.confirm(f.dataset.confirm)) { f.dataset.confirmed = "1"; f.submit(); } return; }
    dlg.querySelector("[data-confirm-text]").textContent = f.dataset.confirm;
    var ok = dlg.querySelector("[data-confirm-ok]");
    var handler = function () {
      ok.removeEventListener("click", handler);
      dlg.close();
      f.dataset.confirmed = "1";
      f.submit();
    };
    ok.addEventListener("click", handler);
    dlg.addEventListener("close", function () { ok.removeEventListener("click", handler); }, { once: true });
    dlg.showModal();
  }, true);

  function toast(msg) {
    var el = document.createElement("div");
    el.className = "toast"; el.setAttribute("role", "status"); el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 1800);
  }
  function copyText(text, btn) {
    var done = function () { toast((document.documentElement.lang === "ru") ? "Скопировано" : "Copied"); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    } else { fallbackCopy(text, done); }
  }
  function fallbackCopy(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (e) {}
    ta.remove();
  }

  // ── HTMX hooks ──────────────────────────────────────────────────────────
  // Re-translate the whole document after EVERY swap: an outerHTML swap (the queue
  // strip's poll) detaches the old target, and out-of-band swaps (#req-more) land
  // outside it, so refreshing only e.detail.target would leave EN text behind.
  document.addEventListener("htmx:afterSwap", function (e) {
    refresh(document);
    if (e.detail.target && e.detail.target.id === "drawer-body") openDrawer();
    if (window.AIBCharts) window.AIBCharts.init(e.detail.target);
  });
  document.addEventListener("htmx:afterSettle", function (e) { refresh(document); });
  document.addEventListener("htmx:responseError", function () {
    toast((document.documentElement.lang === "ru") ? "Ошибка запроса" : "Request failed");
  });
  // A form posted from inside the drawer navigates normally; close first so a
  // back-navigation never restores an open modal.
  document.addEventListener("htmx:beforeHistorySave", closeDrawer);
  window.addEventListener("pageshow", function (e) { if (e.persisted) closeDrawer(); });

  // The sticky queue strip sits right under the (possibly wrapped) top bar.
  function syncTopbarHeight() {
    var tb = document.querySelector(".topbar");
    if (tb) document.documentElement.style.setProperty("--topbar-h", tb.offsetHeight + "px");
  }
  window.addEventListener("resize", syncTopbarHeight);

  document.addEventListener("DOMContentLoaded", function () {
    syncTopbarHeight();
    refresh(document);
    // Deep link (?open=<id>): the server rendered the drawer content hidden.
    var pre = document.getElementById("drawer-preload"), body = document.getElementById("drawer-body");
    if (pre && body) { body.innerHTML = pre.innerHTML; pre.remove(); refresh(body); openDrawer(); }
  });

  // ── Alpine components ───────────────────────────────────────────────────
  document.addEventListener("alpine:init", function () {
    // Live countdown to a UTC instant ("cooldown ends in 4m 12s").
    Alpine.data("countdown", function (iso) {
      return {
        left: "", timer: null,
        init: function () {
          var self = this, end = new Date(iso).getTime();
          function tick() {
            var s = Math.max(0, Math.round((end - Date.now()) / 1000));
            self.left = s >= 3600 ? Math.floor(s / 3600) + "h " + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + "m"
              : Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
            if (s === 0) { clearInterval(self.timer); self.left = "0s"; }
          }
          tick(); this.timer = setInterval(tick, 1000);
        },
        destroy: function () { clearInterval(this.timer); }
      };
    });

    // Add-key form: provider → default scope, greyed-out scopes, model hint.
    Alpine.data("keyForm", function () {
      return {
        meta: {}, provider: "",
        init: function () {
          var s = this.$root.querySelector('script[type="application/json"]');
          if (s) { try { this.meta = JSON.parse(s.textContent); } catch (e) {} }
        },
        get info() { return this.meta[this.provider] || null; },
        usable: function (scope) { return !this.info || this.info.scopes.indexOf(scope) !== -1; },
        pick: function () {
          var m = this.info; if (!m) return;
          var boxes = this.$root.querySelectorAll('input[name="scopes"]');
          var wantEdit = m.capabilities.indexOf("chat:edit") !== -1;
          boxes.forEach(function (cb) {
            var ok = m.scopes.indexOf(cb.value) !== -1;
            cb.disabled = !ok;
            cb.closest("label").classList.toggle("scope-na", !ok);
            cb.checked = ok && (cb.value === m.default_scope || (wantEdit && cb.value === "llm:edit"));
          });
        }
      };
    });

    // Providers page: search / capability / "live keys only" filters. Model rows
    // and provider cards read these (child scopes see the parent's state).
    Alpine.data("providersPage", function (init) {
      init = init || {};
      return {
        q: init.q || "", cap: init.cap || "", live: !!init.live,
        get filtering() { return !!(this.q.trim() || this.cap); },
        match: function (d) {
          var q = this.q.trim().toLowerCase();
          if (this.cap && (" " + d.caps + " ").indexOf(" " + this.cap + " ") === -1) return false;
          return !q || (d.model + " " + d.prov).toLowerCase().indexOf(q) !== -1;
        },
        rowShow: function (el) { return !this.filtering || this.match(el.dataset); },
        anyMatch: function (el) {
          var rows = el.querySelectorAll("[data-model-row]");
          for (var i = 0; i < rows.length; i++) if (this.match(rows[i].dataset)) return true;
          return false;
        },
        cardVisible: function (el) {
          if (this.live && el.dataset.live !== "1") return false;
          return !this.filtering || this.anyMatch(el);
        },
        noMatch: function () {
          var cards = document.querySelectorAll("[data-pcard]");
          for (var i = 0; i < cards.length; i++) if (this.cardVisible(cards[i])) return false;
          return cards.length > 0;
        }
      };
    });

    // One provider card: collapsed state and last tab, remembered per provider.
    Alpine.data("provCard", function (name, openDefault, tabDefault) {
      var kOpen = "aib.prov.open." + name, kTab = "aib.prov.tab." + name;
      function read(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
      function write(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
      var o = read(kOpen), t = read(kTab);
      return {
        open: o === null ? openDefault : o === "1",
        tab: t === "models" || t === "keys" ? t : tabDefault,
        toggle: function () { this.open = !this.open; write(kOpen, this.open ? "1" : "0"); },
        setTab: function (v) { this.tab = v; write(kTab, v); }
      };
    });

    // Header range picker: custom-range popover.
    Alpine.data("rangePicker", function () { return { open: false }; });
  });

  window.AIB = { applyLang: applyLang, localizeTimes: localizeTimes, toast: toast, openDrawer: openDrawer };
})();
