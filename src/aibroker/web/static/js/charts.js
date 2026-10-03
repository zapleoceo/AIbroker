/* Responsive uPlot charts. A chart is a <div class="chart" data-chart="stacked">
   next to a <script type="application/json"> holding its data; this file reads
   it, draws it with theme colours taken from the CSS tokens, resizes it with
   its container and redraws when the OS colour scheme flips. */
(function () {
  "use strict";
  if (typeof uPlot === "undefined") return;

  var live = [];   // { u, el, ro, rebuild }

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function palette(n) {
    var out = [];
    for (var i = 1; i <= 6; i++) out.push(css("--c" + i));
    return { named: out, other: css("--c7") };
  }
  function colorFor(pal, name, i) { return name === "other" ? pal.other : pal.named[i % pal.named.length]; }

  function fmtValue(metric, v) {
    if (v == null) return "—";
    if (metric === "spend") return "$" + (v >= 1 ? v.toFixed(2) : v.toFixed(4));
    return Math.round(v).toLocaleString();
  }
  function tickValue(metric, v) {
    if (metric === "spend") return "$" + (v >= 1 ? +v.toFixed(2) : +v.toFixed(4));
    return v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + "k" : String(Math.round(v * 10) / 10);
  }

  function stacked(el, payload, metric) {
    var m = payload[metric];
    var xs = payload.buckets.map(function (s) { return Date.parse(s) / 1000; });
    var pal = palette();
    var raw = m.series, names = m.names, n = xs.length;
    // cumulative stack, drawn back-to-front so each band shows on top of the one below
    var acc = new Array(n).fill(0), cum = [];
    raw.forEach(function (s) { acc = acc.map(function (v, i) { return v + s[i]; }); cum.push(acc.slice()); });
    var order = names.map(function (_, i) { return i; }).reverse();
    var data = [xs].concat(order.map(function (i) { return cum[i]; }));
    var dayMode = n > 1 && (xs[1] - xs[0]) >= 86400;
    var bars = uPlot.paths.bars({ size: [0.72, 48], radius: 0.18, align: 0 });
    var axisStroke = css("--muted"), grid = css("--line");
    var tip = el.querySelector(".chart-tip");
    var opts = {
      width: Math.max(el.clientWidth, 200), height: el.clientHeight || 260,
      padding: [8, 4, 0, 0], legend: { show: false }, cursor: { points: { show: false }, drag: { x: false, y: false } },
      tzDate: dayMode ? function (ts) { return uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"); } : undefined,
      scales: { x: { time: true, range: function (u, min, max) { var st = n > 1 ? (xs[1] - xs[0]) : 3600; return [min - st * 0.6, max + st * 0.6]; } }, y: { range: function (u, min, max) { return [0, max > 0 ? max * 1.08 : 1]; } } },
      axes: [
        Object.assign({ stroke: axisStroke, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font: "11px system-ui", size: 28 },
          dayMode ? { incrs: [86400], values: function (u, ticks) {
            return ticks.map(function (t) { return new Date(t * 1000).toLocaleDateString([], { timeZone: "UTC", month: "short", day: "numeric" }); });
          } } : {}),
        { stroke: axisStroke, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font: "11px system-ui", size: 52,
          values: function (u, ticks) { return ticks.map(function (t) { return tickValue(metric, t); }); } }
      ],
      series: [{}].concat(order.map(function (i) {
        var c = colorFor(pal, names[i], i);
        return { label: names[i], stroke: c, fill: c, width: 0, paths: bars, points: { show: false } };
      })),
      hooks: {
        setCursor: [function (u) {
          if (!tip) return;
          var idx = u.cursor.idx;
          if (idx == null || u.cursor.left < 0) { tip.style.display = "none"; return; }
          var when = new Date(xs[idx] * 1000);
          var head = dayMode ? when.toLocaleDateString([], { timeZone: "UTC", month: "short", day: "numeric" })
            : when.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
          var rows = names.map(function (nm, i) {
            return { nm: nm, v: raw[i][idx], c: colorFor(pal, nm, i) };
          }).filter(function (r) { return r.v > 0; }).reverse();
          var total = rows.reduce(function (a, r) { return a + r.v; }, 0);
          tip.innerHTML = "";
          var b = document.createElement("b"); b.textContent = head + " · " + fmtValue(metric, total); tip.appendChild(b);
          rows.forEach(function (r) {
            var d = document.createElement("div"), k = document.createElement("span"), i = document.createElement("i");
            k.className = "k"; i.style.cssText = "width:8px;height:8px;border-radius:2px;flex:none;background:" + r.c;
            k.appendChild(i); k.appendChild(document.createTextNode(r.nm));
            var v = document.createElement("span"); v.textContent = fmtValue(metric, r.v);
            d.appendChild(k); d.appendChild(v); tip.appendChild(d);
          });
          if (!rows.length) { var e = document.createElement("div"); e.textContent = "—"; tip.appendChild(e); }
          tip.style.display = "block";
          var left = u.cursor.left + 14, w = tip.offsetWidth;
          tip.style.left = (left + w > el.clientWidth ? u.cursor.left - w - 14 : left) + "px";
          tip.style.top = "8px";
        }]
      }
    };
    var host = el.querySelector(".chart-canvas") || el;
    var u = new uPlot(opts, data, host);
    return { u: u, legend: names.map(function (nm, i) { return { name: nm, color: colorFor(pal, nm, i) }; }) };
  }

  function renderLegend(el, items) {
    var box = el.parentElement.querySelector(".legend");
    if (!box) return;
    box.innerHTML = "";
    items.forEach(function (it) {
      var s = document.createElement("span"), i = document.createElement("i"), e = document.createElement("em");
      i.style.background = it.color; e.textContent = it.name; e.title = it.name;
      s.appendChild(i); s.appendChild(e); box.appendChild(s);
    });
  }

  function build(el) {
    var script = document.getElementById(el.dataset.src);
    if (!script) return;
    var payload;
    try { payload = JSON.parse(script.textContent); } catch (e) { return; }
    var metric = el.dataset.metric || "spend";
    destroy(el);
    var host = el.querySelector(".chart-canvas");
    if (host) host.innerHTML = "";
    var m = payload[metric];
    if (!m || !m.series.length) { el.classList.add("is-empty"); return; }
    el.classList.remove("is-empty");
    var res = stacked(el, payload, metric);
    renderLegend(el, res.legend);
    var ro = new ResizeObserver(function () {
      res.u.setSize({ width: Math.max(el.clientWidth, 200), height: el.clientHeight || 260 });
    });
    ro.observe(el);
    live.push({ el: el, u: res.u, ro: ro });
  }

  function destroy(el) {
    live = live.filter(function (c) {
      if (c.el !== el) return true;
      c.ro.disconnect(); c.u.destroy(); return false;
    });
  }

  function init(root) {
    (root || document).querySelectorAll("[data-chart]").forEach(build);
  }

  // metric toggle: <button data-chart-metric="calls" data-target="#id">
  document.addEventListener("click", function (e) {
    var b = e.target instanceof Element && e.target.closest("[data-chart-metric]");
    if (!b) return;
    var el = document.querySelector(b.dataset.target);
    if (!el) return;
    el.dataset.metric = b.dataset.chartMetric;
    b.parentElement.querySelectorAll("[data-chart-metric]").forEach(function (x) {
      x.setAttribute("aria-pressed", x === b ? "true" : "false");
    });
    build(el);
  });

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    document.querySelectorAll("[data-chart]").forEach(build);
  });
  document.addEventListener("DOMContentLoaded", function () { init(document); });
  window.AIBCharts = { init: init };
})();
