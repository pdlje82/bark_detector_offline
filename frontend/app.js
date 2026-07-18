(function () {
  "use strict";

  /* Bark-detector evidence viewer — single-page app, no build step.
   * Reads results.json; uses the label API when served by `barkdetect serve`
   * (labeling mode), else runs read-only from a static bundle.
   * Major sections are marked with "// ====" banners, in this order:
   *   1. boot
   *   2. time helpers (offset-aware wall clock in the file's timezone)
   *   3. formatting
   *   4. derived data
   *   5. labels: label API (authoritative), in-memory cache
   *   6. render: summary band
   *   7. render: calendar
   *   8. render: day detail + zoom timeline
   *   9. window playback (real-time transport across the day)
   *   10. audio
   *   11. burst playback (sequential clips)
   *   12. render: table
   *   13. render: provenance
   *   14. render: dog identification reliability
   *   15. actions
   *   16. boot
   */
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var pad2 = function (n) { return String(n).padStart(2, "0"); };
  var DAY = 86400000;
  var STEPS = [86400000, 21600000, 3600000, 900000, 300000];
  var MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  var MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  var WD = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];

  var R = null;        // the results object
  var offMin = 0;      // file timezone offset in minutes
  var classes = [];
  var audio = null;    // shared Audio element
  var state = {
    selDate: null,
    roiCenter: null,
    roiZoom: 2,
    selId: null,
    sortKey: "time",
    sortDir: 1,
    mode: "readonly",
    labels: {},
    pending: {},
    filters: { night: false, dayOnly: false, cls: "all", minConf: 0, minInt: 0, status: "all", dog: "all", burstGap: 2.0, minBurst: 1, groupBurst: false },
    clipPad: 0.5,       // seconds of context around a single-bark clip (user-adjustable)
    playCursorMs: null
  };

  // ==========================================================
  // BOOT
  // ==========================================================
  function fail(detail) {
    $("#loading").hidden = true;
    $("#root").hidden = true;
    var e = $("#err"); e.hidden = false;
    $("#errDetail").textContent = detail || "";
  }

  function load() {
    $("#err").hidden = true;
    $("#loading").hidden = false;
    fetch("./results.json", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status + " " + r.statusText);
        return r.json();
      })
      .then(function (data) {
        if (!data || !Array.isArray(data.events) || !Array.isArray(data.recordings)) {
          throw new Error("results.json is missing required fields (events / recordings).");
        }
        // Probe the label API. Reachable -> labeling mode (API is authoritative);
        // unreachable (hosted static copy) -> read-only presentation mode.
        return apiProbe().then(function (labels) {
          state.mode = "label";
          state.apiLabels = (labels && typeof labels === "object") ? labels : {};
          boot(data);
        }, function () {
          state.mode = "readonly";
          state.apiLabels = null;
          boot(data);
        });
      })
      .catch(function (err) { fail(String(err && err.message || err)); });
  }

  $("#retry").addEventListener("click", load);

  // ==========================================================
  // TIME HELPERS (OFFSET-AWARE WALL CLOCK IN THE FILE'S TIMEZONE)
  // ==========================================================
  function detectOff() {
    var cands = [];
    if (R.recordings[0]) cands.push(R.recordings[0].start_local);
    if (R.events[0]) cands.push(R.events[0].abs_start_local);
    for (var i = 0; i < cands.length; i++) {
      var m = cands[i] && cands[i].match(/([+-])(\d{2}):(\d{2})$/);
      if (m) return (m[1] === "-" ? -1 : 1) * (parseInt(m[2], 10) * 60 + parseInt(m[3], 10));
    }
    return 0;
  }
  function wall(iso) {
    var t = Date.parse(iso) + offMin * 60000;
    var d = new Date(t);
    return {
      t: t,
      date: d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate()),
      time: pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes()) + ":" + pad2(d.getUTCSeconds())
    };
  }
  function dayMid(dateStr) { var p = dateStr.split("-").map(Number); return Date.UTC(p[0], p[1] - 1, p[2]); }
  function dayLabel(dateStr) {
    var p = dateStr.split("-").map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    return WD[d.getUTCDay()] + ", " + MON[d.getUTCMonth()] + " " + p[2];
  }
  function msToHM(ms) { ms = Math.max(0, Math.min(DAY, ms)); return pad2(Math.floor(ms / 3600000)) + ":" + pad2(Math.floor((ms % 3600000) / 60000)); }
  function spanLabel(ms) { return { 86400000: "24 h", 21600000: "6 h", 3600000: "1 h", 900000: "15 min", 300000: "5 min" }[ms] || ""; }

  // ==========================================================
  // FORMATTING
  // ==========================================================
  function fmtDur(s) { return (s == null) ? "\u2014" : s.toFixed(1) + " s"; }
  function fmtConf(c) { return (c == null) ? "\u2014" : Math.round(c * 100) + "%"; }
  function fmtDbfs(d) { return (d == null) ? "\u2014" : d.toFixed(1) + " dBFS"; }
  function fmtRel(r) { return (r == null) ? "\u2014" : r.toFixed(2); }
  function intensityColor(rel) {
    if (rel == null) return "#9a9a8f";
    if (rel >= 0.8) return "#a83a29";
    if (rel >= 0.5) return "#c07a34";
    if (rel >= 0.25) return "#b09a54";
    return "#9a9a8f";
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }

  // ==========================================================
  // DERIVED DATA
  // ==========================================================
  function covOverlapsDay(dateStr) {
    var mid = dayMid(dateStr);
    return R.coverage.some(function (c) { var a = wall(c.start).t - mid, b = wall(c.end).t - mid; return b > 0 && a < DAY; });
  }
  function daySummary(dateStr) {
    for (var i = 0; i < R.daily_summary.length; i++) if (R.daily_summary[i].date === dateStr) return R.daily_summary[i];
    return null;
  }
  function eventsOnDay(dateStr) {
    return R.events.map(function (e) { return { e: e, w: wall(e.abs_start_local) }; }).filter(function (o) { return o.w.date === dateStr; });
  }
  function firstEventIdOn(dateStr) { var o = eventsOnDay(dateStr)[0]; return o ? o.e.id : null; }

  function monthRange() {
    var insts = [];
    R.coverage.forEach(function (c) { insts.push(Date.parse(c.start), Date.parse(c.end)); });
    R.gaps.forEach(function (g) { insts.push(Date.parse(g.start), Date.parse(g.end)); });
    R.recordings.forEach(function (r) { if (r.start_local) { var t = Date.parse(r.start_local); insts.push(t, t + (r.duration_sec || 0) * 1000); } });
    R.events.forEach(function (e) { if (e.abs_start_local) insts.push(Date.parse(e.abs_start_local)); });
    R.daily_summary.forEach(function (d) { insts.push(Date.parse(d.date + "T12:00:00Z")); });
    if (!insts.length) insts.push(Date.parse(R.generated_at) || Date.now());
    var lo = Math.min.apply(null, insts), hi = Math.max.apply(null, insts);
    var wlo = wall(new Date(lo).toISOString()), whi = wall(new Date(hi).toISOString());
    var a = wlo.date.split("-").map(Number), b = whi.date.split("-").map(Number);
    var months = [], y = a[0], m = a[1];
    for (var guard = 0; guard < 120; guard++) {
      months.push({ year: y, month: m });
      if (y === b[0] && m === b[1]) break;
      m++; if (m > 12) { m = 1; y++; }
    }
    return months;
  }

  // ==========================================================
  // LABELS: LABEL API (AUTHORITATIVE), IN-MEMORY CACHE
  // ==========================================================
  var API = "api/labels"; // relative to this page's directory; same origin as static assets
  function apiProbe() {
    return fetch(API, { method: "GET", cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  function apiPut(key, label) {
    return fetch(API + "/" + encodeURIComponent(key), {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: label })
    }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r; });
  }
  function apiDelete(key) {
    return fetch(API + "/" + encodeURIComponent(key), { method: "DELETE" })
      .then(function (r) { if (!r.ok && r.status !== 404) throw new Error("HTTP " + r.status); return r; });
  }
  function showSaveError(retryFn) {
    var el = $("#savenote"); if (!el) return;
    el.innerHTML = "Couldn\u2019t save to the database. Your change is still shown here. <button type=\"button\" id=\"saveRetry\">Retry</button>";
    el.classList.add("on");
    var b = $("#saveRetry"); if (b) b.onclick = function () { clearSaveError(); if (retryFn) retryFn(); };
  }
  function clearSaveError() { var el = $("#savenote"); if (el) { el.classList.remove("on"); el.innerHTML = ""; } }
  // optimistic write: state already updated; sync value to the API, surface errors
  function persist(key) {
    if (state.mode !== "label") return;
    clearSaveError();
    var val = state.labels[key];
    var op = (val == null) ? function () { return apiDelete(key); } : function () { return apiPut(key, val); };
    op().catch(function () { showSaveError(function () { persist(key); }); });
  }
  function evKey(e) { return e.key != null ? String(e.key) : ("id:" + e.id); }
  function eventByKey(key) { for (var i = 0; i < R.events.length; i++) if (evKey(R.events[i]) === key) return R.events[i]; return null; }
  function isSpecial(id) { return id === "unsure" || id === "multiple" || id === "not_a_dog"; }
  function labelToArray(v) { if (v == null || v === "") return []; return Array.isArray(v) ? v.slice() : [v]; }
  function labelDisplay(arr) { return arr.map(function (id) { return labelName(id); }).join(", "); }
  function eventDogs(e) { if (Array.isArray(e.dog_labels) && e.dog_labels.length) return e.dog_labels.slice(); if (e.dog_label != null && e.dog_label !== "") return [e.dog_label]; return []; }
  function effectiveDogs(e) { var human = labelToArray(state.labels[evKey(e)]); return human.length ? human : eventDogs(e); }
  function sameSet(a, b) { if (a.length !== b.length) return false; var sa = a.slice().sort(), sb = b.slice().sort(); for (var i = 0; i < sa.length; i++) if (String(sa[i]) !== String(sb[i])) return false; return true; }
  function evStatus(e) {
    var cur = labelToArray(state.labels[evKey(e)]);
    if (!cur.length) return "unlabeled";
    var model = eventDogs(e);
    if (model.length && sameSet(cur, model)) return "confirmed";
    return "relabeled";
  }
  function statusMeta(st) {
    if (st === "confirmed") return { c: "#2f8a5b", t: "Confirmed \u2014 matches suggestion" };
    if (st === "relabeled") return { c: "#c98a3e", t: "Relabeled \u2014 differs from suggestion" };
    return { c: "", t: "Unlabeled" };
  }
  function statusDotStyle(st) {
    if (st === "unlabeled") return "background:transparent;border:1.5px dashed var(--faint)";
    return "background:" + statusMeta(st).c + ";border:1.5px solid " + statusMeta(st).c;
  }
  function statusDot(e) {
    var st = evStatus(e);
    return '<span class="stdot" data-statusfor="' + esc(evKey(e)) + '" title="' + esc(statusMeta(st).t) + '" style="' + statusDotStyle(st) + '"></span>';
  }
  function updateStatusDom(key) {
    var e = eventByKey(key); if (!e) return; var st = evStatus(e), m = statusMeta(st);
    document.querySelectorAll(".stdot[data-statusfor]").forEach(function (el) {
      if (el.getAttribute("data-statusfor") !== key) return;
      el.setAttribute("style", statusDotStyle(st)); el.title = m.t;
    });
  }
  function dayLabelState(dateStr) {
    var evs = eventsOnDay(dateStr); if (!evs.length) return null;
    var labeled = evs.filter(function (o) { return state.labels[evKey(o.e)]; }).length;
    if (labeled === 0) return "none";
    return labeled === evs.length ? "all" : "some";
  }
  function rosterDogs() {
    return (R.dogs || []).map(function (d) {
      if (typeof d === "string") return { id: d, name: d };
      return { id: (d.id != null ? d.id : d.name), name: (d.name != null ? d.name : d.id) };
    });
  }
  function labelOptions() {
    return rosterDogs().concat([
      { id: "unsure", name: "Unsure" },
      { id: "multiple", name: "Multiple dogs" },
      { id: "not_a_dog", name: "Not a dog (false positive)" }
    ]);
  }
  function labelName(id) {
    if (id == null || id === "") return "";
    var o = labelOptions().filter(function (x) { return String(x.id) === String(id); })[0];
    return o ? o.name : String(id);
  }
  function predHtml(e) {
    var dogs = eventDogs(e);
    if (!dogs.length) return "";
    var human = e.dog_label_source === "human";
    var conf = e.dog_confidence != null ? Math.round(e.dog_confidence * 100) + "%" : "";
    var title = human ? "Confirmed by a listener." : "Suggested by the dog model. Per-event confidence is a raw, over-confident score \u2014 see the Dog identification reliability panel for the cross-validated accuracy, which is the reliable figure.";
    return dogs.map(function (d, i) {
      var showConf = !human && conf && i === 0;
      return '<span class="pred ' + (human ? "confirmed" : "suggested") + '" title="' + esc(title) + '">' +
        esc(labelName(d)) + (showConf ? " \u00b7 " + conf : "") + (human ? "" : '<span class="ptag">suggested</span>') + '</span>';
    }).join(" ");
  }
  function labelControl(e) {
    var key = evKey(e);
    var cur = labelToArray(state.labels[key]);
    var dogChips = rosterDogs().map(function (o) {
      var on = cur.indexOf(o.id) >= 0;
      return '<button type="button" class="lchip' + (on ? " on" : "") + '" data-role="labeltoggle" data-key="' + esc(key) + '" data-id="' + esc(o.id) + '">' + esc(o.name) + '</button>';
    }).join("");
    var specials = [{ id: "unsure", name: "Unsure" }, { id: "multiple", name: "Multiple dogs" }, { id: "not_a_dog", name: "Not a dog" }];
    var spChips = specials.map(function (o) {
      var on = cur.length === 1 && cur[0] === o.id;
      return '<button type="button" class="lchip special' + (on ? " on" : "") + '" data-role="labeltoggle" data-key="' + esc(key) + '" data-id="' + esc(o.id) + '">' + esc(o.name) + '</button>';
    }).join("");
    return '<div class="lctrl" data-lc="' + esc(key) + '"><div class="lcgroup">' + dogChips + '</div><div class="lcgroup sp">' + spChips + '</div></div>';
  }
  function labelBlock(e) {
    var cur = labelToArray(state.labels[evKey(e)]);
    return '<div class="labelrow"><span class="lk">Your label</span>' + labelControl(e) + '</div>' +
      '<div class="labelrow"><span class="lk">Current</span><span class="mylabel ' + (cur.length ? "set" : "none") + '" data-mylabel="' + esc(evKey(e)) + '">' + (cur.length ? esc(labelDisplay(cur)) : "unlabeled") + '</span></div>' +
      '<div class="shortcuts">1\u20139 toggle dog / set special \u00b7 0 clear \u00b7 Space play/pause \u00b7 J / K (or \u2193 / \u2191) next / prev \u00b7 N next unlabeled</div>';
  }
  function toggleLabel(key, id) {
    var cur = labelToArray(state.labels[key]);
    if (isSpecial(id)) {
      if (cur.length === 1 && cur[0] === id) delete state.labels[key];
      else state.labels[key] = id;
    } else {
      var dogs = cur.filter(function (x) { return !isSpecial(x); });
      var idx = dogs.indexOf(id);
      if (idx >= 0) dogs.splice(idx, 1); else dogs.push(id);
      if (dogs.length === 0) delete state.labels[key];
      else state.labels[key] = dogs.length === 1 ? dogs[0] : dogs;
    }
    persist(key);
    applyLabelDom(key);
  }
  function clearCurrent() {
    if (state.selId == null) return;
    var e = null; for (var i = 0; i < R.events.length; i++) if (R.events[i].id === state.selId) { e = R.events[i]; break; }
    if (!e) return; var key = evKey(e); delete state.labels[key]; persist(key); applyLabelDom(key);
  }
  function setLabel(key, val, inPlace) {
    if (val === "" || val == null) delete state.labels[key]; else state.labels[key] = val;
    persist(key);
    if (inPlace) applyLabelDom(key); else { renderTable(); renderDay(); updateLabelStats(); }
  }
  function applyLabelDom(key) {
    var cur = labelToArray(state.labels[key]);
    document.querySelectorAll(".lctrl").forEach(function (ctrl) {
      if (ctrl.getAttribute("data-lc") !== key) return;
      ctrl.querySelectorAll(".lchip").forEach(function (ch) {
        var id = ch.getAttribute("data-id"), sp = ch.classList.contains("special");
        var on = sp ? (cur.length === 1 && cur[0] === id) : (cur.indexOf(id) >= 0);
        ch.classList.toggle("on", on);
      });
    });
    document.querySelectorAll(".mylabel[data-mylabel]").forEach(function (el) {
      if (el.getAttribute("data-mylabel") !== key) return;
      el.className = "mylabel " + (cur.length ? "set" : "none"); el.textContent = cur.length ? labelDisplay(cur) : "unlabeled";
    });
    updateStatusDom(key);
    updateLabelStats();
    renderCalendar();
    if (state.filters.status !== "all" || state.filters.dog !== "all") renderTable();
  }
  function updateLabelStats() {
    var el = $("#labelstats"); if (!el) return;
    var c = 0, r = 0, u = 0;
    R.events.forEach(function (e) { var s = evStatus(e); if (s === "confirmed") c++; else if (s === "relabeled") r++; else u++; });
    el.textContent = c + " confirmed \u00b7 " + r + " relabeled \u00b7 " + u + " unlabeled";
  }
  function toggleTraining() {}
  function exportLabels() {}
  function step(dir) {
    var rows = filteredRows(); if (!rows.length) return;
    var idx = -1; for (var i = 0; i < rows.length; i++) if (rows[i].e.id === state.selId) { idx = i; break; }
    var ni = idx < 0 ? (dir > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, idx + dir));
    selectEvent(rows[ni].e.id, false);
  }
  function nextUnlabeled() {
    var rows = filteredRows(); if (!rows.length) return;
    var idx = -1; for (var i = 0; i < rows.length; i++) if (rows[i].e.id === state.selId) { idx = i; break; }
    for (var j = 1; j <= rows.length; j++) { var r = rows[(idx + j) % rows.length]; if (!state.labels[evKey(r.e)]) { selectEvent(r.e.id, false); return; } }
  }
  function assignCurrent(id) {
    if (state.selId == null) return;
    var e = null; for (var i = 0; i < R.events.length; i++) if (R.events[i].id === state.selId) { e = R.events[i]; break; }
    if (!e) return; toggleLabel(evKey(e), id);
  }
  function togglePlay() { if (audio) { if (audio.paused) { var p = audio.play(); if (p && p.catch) p.catch(function () {}); } else audio.pause(); } }
  function onKey(ev) {
    if (state.mode !== "label") return;
    var t = ev.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
    var opts = labelOptions();
    var k = ev.key;
    if (k === "ArrowDown" || k === "j" || k === "J") { step(1); ev.preventDefault(); }
    else if (k === "ArrowUp" || k === "k" || k === "K") { step(-1); ev.preventDefault(); }
    else if (k === "n" || k === "N") { nextUnlabeled(); ev.preventDefault(); }
    else if (k === " " || k === "Spacebar") { togglePlay(); ev.preventDefault(); }
    else if (k >= "1" && k <= "9") { var i2 = parseInt(k, 10) - 1; if (i2 < opts.length) { assignCurrent(opts[i2].id); ev.preventDefault(); } }
    else if (k === "0") { clearCurrent(); ev.preventDefault(); }
  }

  // ==========================================================
  // RENDER: SUMMARY BAND
  // ==========================================================
  function renderSummary() {
    var ds = R.daily_summary;
    var recSec = R.coverage.reduce(function (a, c) { return a + (c.duration_sec || 0); }, 0);
    var gapSec = R.gaps.reduce(function (a, g) { return a + (g.duration_sec || 0); }, 0);
    var nightEvents = R.events.filter(function (e) { return e.night; }).length;
    var cov = (recSec + gapSec) > 0 ? Math.round(recSec / (recSec + gapSec) * 100) : 100;
    var range;
    if (ds.length) {
      var f = ds[0].date.split("-").map(Number), l = ds[ds.length - 1].date.split("-").map(Number);
      range = MON[f[1] - 1] + " " + f[2] + " \u2013 " + MON[l[1] - 1] + " " + l[2] + ", " + f[0];
    } else { range = "no events"; }

    $("#summary").innerHTML =
      '<div class="title">' +
        '<div class="eyebrow">Acoustic detection report</div>' +
        '<div class="h1">Dog-barking evidence</div>' +
        '<div class="sub">' + esc(range) + ' \u00b7 ' + esc(R.timezone || "") + '</div>' +
        (state.mode === "label"
          ? '<div class="modeind label" title="Labels are saved to the database as you go"><span class="md"></span>Labeling \u2014 saved to database</div>'
          : '<div class="modeind readonly" title="No label database reachable; showing stored results only"><span class="md"></span>Read-only</div>') +
        '<div class="savenote" id="savenote"></div>' +
      '</div>' +
      '<div class="stats">' +
        stat(R.event_count != null ? R.event_count : R.events.length, "Detections", "") +
        stat(nightEvents, "At night", "night") +
        stat(R.recording_count != null ? R.recording_count : R.recordings.length, "Recordings", "") +
        stat(cov + "%", (recSec / 3600).toFixed(1) + "h rec / " + (gapSec / 3600).toFixed(1) + "h gap", "") +
      '</div>';

    function stat(num, lbl, cls) {
      return '<div class="stat"><span class="num ' + cls + '">' + esc(num) + '</span><span class="lbl">' + esc(lbl) + '</span></div>';
    }

    if ([1, 2].indexOf(R.schema_version) === -1) {
      $("#warnSlot").innerHTML = '<div class="warn">This file reports schema version <b>' + esc(R.schema_version) + '</b>; this viewer supports versions 1\u20132. Some fields may not display correctly.</div>';
    } else {
      $("#warnSlot").innerHTML = "";
    }
  }

  // ==========================================================
  // RENDER: CALENDAR
  // ==========================================================
  // Per-day {count, night} computed from events passing the content filters
  // (night/class/status/dog/conf/intensity/burst) — but NOT "selected day only",
  // so the calendar keeps showing every day's filtered total.
  function filteredDayCounts() {
    var m = {};
    R.events.forEach(function (e) {
      if (!eventMatches(e)) return;
      var d = wall(e.abs_start_local).date;
      var o = m[d] || (m[d] = { count: 0, night: 0 });
      o.count++; if (e.night) o.night++;
    });
    return m;
  }
  // Re-render everything the content filters affect (calendar counts included).
  function renderFiltered() { renderCalendar(); renderDay(); renderTable(); }

  function renderCalendar() {
    var months = monthRange();
    var dayCounts = filteredDayCounts();
    var maxCount = Math.max.apply(null, [1].concat(Object.keys(dayCounts).map(function (k) { return dayCounts[k].count; })));
    var html = '<div class="cal-head"><h2 class="sec">Calendar</h2><span class="lead">tap a recorded day</span></div>';
    months.forEach(function (mo) {
      var first = new Date(Date.UTC(mo.year, mo.month - 1, 1));
      var dim = new Date(Date.UTC(mo.year, mo.month, 0)).getUTCDate();
      var lead = (first.getUTCDay() + 6) % 7;
      html += '<div class="cal-month"><div class="mlabel">' + MONTHS[mo.month - 1] + " " + mo.year + '</div>';
      html += '<div class="dow">' + ["Mo","Tu","We","Th","Fr","Sa","Su"].map(function (w) { return "<div>" + w + "</div>"; }).join("") + '</div>';
      html += '<div class="grid">';
      for (var i = 0; i < lead; i++) html += '<div class="cell blank"></div>';
      for (var d = 1; d <= dim; d++) {
        var dateStr = mo.year + "-" + pad2(mo.month) + "-" + pad2(d);
        var rec = covOverlapsDay(dateStr);
        var fc = dayCounts[dateStr];
        var count = fc ? fc.count : 0;
        var night = fc ? fc.night : 0;
        var sel = dateStr === state.selDate;
        var cls = "cell" + (rec ? " rec" : "") + (sel ? " sel" : "");
        var bg = rec ? countColor(count, maxCount) : "transparent";
        var hatch = rec ? "" : " hatch";
        var numColor = rec ? "var(--ink)" : "var(--faint)";
        var title = rec ? dayLabel(dateStr) + ": " + count + " detection" + (count === 1 ? "" : "s") + (night ? " (" + night + " night)" : "") : dayLabel(dateStr) + ": no recording";
        var lst = rec ? dayLabelState(dateStr) : null;
        var calst = "";
        if (lst) {
          var cc = lst === "all" ? "#2f8a5b" : lst === "some" ? "#c98a3e" : "";
          calst = '<span class="calst" title="' + (lst === "all" ? "all reviewed" : lst === "some" ? "partly reviewed" : "not reviewed") + '" style="' + (lst === "none" ? "background:transparent;border:1.5px dashed var(--faint)" : "background:" + cc) + '"></span>';
        }
        html += '<div class="' + cls + hatch + '" style="background:' + bg + '"' +
          (rec ? ' data-action="selectDay" data-date="' + dateStr + '"' : "") +
          ' title="' + esc(title) + '">' +
          '<span class="dnum" style="color:' + numColor + '">' + d + '</span>' +
          '<span class="cnt">' + (rec && count > 0 ? count : "") + '</span>' +
          '<span class="ndot" style="background:' + (night > 0 ? "var(--night)" : "transparent") + '"></span>' +
          calst +
        '</div>';
      }
      html += '</div></div>';
    });
    html +=
      '<div class="legend">' +
        '<span><i class="sw" style="background:#eadfce"></i>events (darker = more)</span>' +
        '<span><i class="sw line hatch"></i>no recording</span>' +
        '<span><i class="sw dot" style="background:var(--night)"></i>night</span>' +
        '<span><i class="sw dot" style="background:#2f8a5b"></i>all reviewed</span>' +
        '<span><i class="sw dot" style="background:#c98a3e"></i>partly reviewed</span>' +
      '</div>';
    $("#calWrap").innerHTML = html;
  }
  function countColor(c, max) {
    if (c <= 0) return "var(--field)";
    if (max <= 1) return "#c98a3e";
    var f = c / max;
    if (f >= 0.8) return "#c98a3e";
    if (f >= 0.4) return "#dcc09a";
    return "#eadfce";
  }

  // ==========================================================
  // RENDER: DAY DETAIL + ZOOM TIMELINE
  // ==========================================================
  function renderDay() {
    var dateStr = state.selDate;
    var wrap = $("#dayWrap");
    var prevScroll = 0; var _ol = wrap.querySelector(".evlist"); if (_ol) prevScroll = _ol.scrollTop;
    if (!dateStr) { wrap.innerHTML = '<div class="empty">Select a day in the calendar.</div>'; return; }
    var mid = dayMid(dateStr);
    var sum = daySummary(dateStr);
    var dayEvents = eventsOnDay(dateStr).filter(function (o) { return eventMatches(o.e); });

    var pct = function (ms) { return Math.max(0, Math.min(DAY, ms)) / DAY * 100; };
    var ovSegs = segsFor(mid, 0, DAY, pct);
    var ovTicks = dayEvents.map(function (o) { var st = evStatus(o.e); var ring = st !== "unlabeled" ? ";box-shadow:0 0 0 1.5px " + statusMeta(st).c : ""; return '<div class="tick" style="left:' + pct(o.w.t - mid) + '%;background:' + intensityColor(o.e.intensity_relative) + ring + '"></div>'; }).join("");

    var span = STEPS[state.roiZoom];
    var center = state.roiCenter;
    if (center == null) center = dayEvents.length ? (dayEvents[0].w.t - mid) : DAY / 2;
    var lo, hi;
    if (span >= DAY) { lo = 0; hi = DAY; } else { lo = Math.max(0, Math.min(DAY - span, center - span / 2)); hi = lo + span; }
    var rp = function (ms) { return (ms - lo) / (hi - lo) * 100; };
    state._win = { date: dateStr, lo: lo, hi: hi, mid: mid };
    var roiSegs = segsFor(mid, lo, hi, rp);
    var roiRaw = dayEvents.filter(function (o) { var r = o.w.t - mid; return r >= lo && r <= hi; });
    var phHtml = (state.playCursorMs != null && state.playCursorMs >= lo && state.playCursorMs <= hi)
      ? '<div class="playhead" id="roiPlayhead" style="left:' + rp(state.playCursorMs) + '%"></div>' : "";
    var roiTicks = roiRaw.map(function (o) { var st = evStatus(o.e); var ring = st !== "unlabeled" ? ";box-shadow:0 0 0 1.5px " + statusMeta(st).c : ""; var wPct = ((o.e.duration_sec || 0) * 1000) / (hi - lo) * 100; return '<div class="tick bar" data-id="' + o.e.id + '" title="' + esc(o.e.class + " \u00b7 " + fmtDur(o.e.duration_sec)) + '" style="left:' + rp(o.w.t - mid) + '%;width:' + wPct + '%;background:' + intensityColor(o.e.intensity_relative) + ring + '"></div>'; }).join("");

    var summary = sum ? (sum.count + " detection" + (sum.count === 1 ? "" : "s") + " \u00b7 " + sum.night_count + " at night") : "No recording this day";

    // selected event panel
    var selEv = null;
    for (var i = 0; i < R.events.length; i++) if (R.events[i].id === state.selId) { selEv = R.events[i]; break; }
    var selHtml = "";
    if (selEv && wall(selEv.abs_start_local).date === dateStr) {
      var sw = wall(selEv.abs_start_local);
      selHtml =
        '<div class="selpanel">' +
          '<div class="r1">' + statusDot(selEv) + '<span class="evdot" style="background:' + intensityColor(selEv.intensity_relative) + '"></span>' +
            '<span class="cls">' + esc(selEv.class) + '</span>' +
            '<span class="mono" style="font-size:14px">' + sw.time + '</span>' +
            (selEv.night ? '<span class="evnight">night</span>' : "") + predHtml(selEv) + '</div>' +
          '<div class="grid4">' +
            kv("Duration", fmtDur(selEv.duration_sec)) +
            kv("Peak conf.", fmtConf(selEv.peak_conf)) +
            kv("Loudness", fmtDbfs(selEv.intensity_dbfs)) +
            kv("Rel. intensity", fmtRel(selEv.intensity_relative)) +
          '</div>' +
          '<div class="caps" style="margin-top:14px">Selected clip</div>' +
          audioHtml(selEv) +
          (state.mode === "label" ? labelBlock(selEv) : "") +
        '</div>';
    }

    wrap.innerHTML =
      '<div class="daytitle">' + esc(dayLabel(dateStr)) + '</div>' +
      '<div class="daysub">' + esc(summary) + '</div>' +
      '<div class="caps">Full day \u2014 tap to move window</div>' +
      '<div class="strip ov" data-action="moveRoi">' + ovSegs + ovTicks +
        '<div class="roi-box" style="left:' + (lo / DAY * 100) + '%;width:' + ((hi - lo) / DAY * 100) + '%"></div>' +
      '</div>' +
      '<div class="axis"><span style="left:0">00</span><span style="left:25%">06</span><span style="left:50%">12</span><span style="left:75%">18</span><span style="right:0">24</span></div>' +
      '<div class="zoom">' +
        '<button data-action="zoomOut"' + (state.roiZoom <= 0 ? " disabled" : "") + '>\u2212</button>' +
        '<button data-action="zoomIn"' + (state.roiZoom >= STEPS.length - 1 ? " disabled" : "") + '>+</button>' +
        '<div><span class="zl">' + spanLabel(span) + '</span> <span class="zr">' + msToHM(lo) + " \u2013 " + msToHM(hi) + '</span></div>' +
      '</div>' +
      '<div class="strip roi" id="roiStrip">' + roiSegs + roiTicks + phHtml + '</div>' +
      '<div class="axis"><span style="left:0">' + msToHM(lo) + '</span><span style="right:0">' + msToHM(hi) + '</span></div>' +
      '<div class="wintransport"><button id="winPlayBtn" data-action="winPlay" title="Play the window in real time">' + (TP.playing ? "\u23f8" : "\u25b6") + '</button><span class="wt" id="winCursor"></span></div>' +
      '<div class="caps">Events in window</div>' +
      (roiRaw.length ? '<div class="evlist">' + sortRows(roiRaw.slice()).map(function (o) { return evCard(o.e, o.w); }).join("") + '</div>'
                     : '<div class="empty">No events match here \u2014 adjust filters, zoom out, or move the window.</div>') +
      selHtml;

    // wire audio for the selected event
    var _nl = wrap.querySelector(".evlist"); if (_nl) _nl.scrollTop = prevScroll;
    wireRoiStrip();
    updateWinTransport();
    setupAudio(selEv);
  }

  function kv(k, v) { return '<div><div class="k">' + k + '</div><div class="v">' + v + '</div></div>'; }
  function segsFor(mid, lo, hi, proj) {
    var out = "";
    R.coverage.forEach(function (c) {
      var a = wall(c.start).t - mid, b = wall(c.end).t - mid, ca = Math.max(a, lo), cb = Math.min(b, hi);
      if (cb <= ca) return;
      out += '<div class="seg" style="left:' + proj(ca) + '%;width:' + (proj(cb) - proj(ca)) + '%;background:var(--rec)"></div>';
    });
    R.gaps.forEach(function (g) {
      var a = wall(g.start).t - mid, b = wall(g.end).t - mid, ca = Math.max(a, lo), cb = Math.min(b, hi);
      if (cb <= ca) return;
      out += '<div class="seg hatch" style="left:' + proj(ca) + '%;width:' + (proj(cb) - proj(ca)) + '%"></div>';
    });
    return out;
  }
  function evCard(e, w) {
    var sel = e.id === state.selId;
    var meta = fmtDur(e.duration_sec) + " \u00b7 " + fmtConf(e.peak_conf) + " conf \u00b7 " + fmtDbfs(e.intensity_dbfs);
    return '<div class="evcard' + (sel ? " sel" : "") + '" data-action="selectEvent" data-id="' + e.id + '">' +
      '<span class="evdot" style="background:' + intensityColor(e.intensity_relative) + '"></span>' +
      '<div class="evbody"><div class="evtop"><span class="cls">' + esc(e.class) + '</span><span class="t">' + w.time + '</span>' +
        (e.night ? '<span class="evnight">night</span>' : "") + '</div>' +
        '<div class="evmeta">' + meta + '</div>' +
        (predHtml(e) ? '<div style="margin-top:6px">' + predHtml(e) + '</div>' : "") + '</div>' +
      statusDot(e) +
      '<button class="play" data-action="playEvent" data-id="' + e.id + '" title="Play clip" aria-label="Play clip">\u25b6</button>' +
    '</div>';
  }

  // ==========================================================
  // WINDOW PLAYBACK (REAL-TIME TRANSPORT ACROSS THE DAY)
  // ==========================================================
  var TP = { ctx: null, sources: [], _audio: null, raf: 0, startPerf: 0, startMs: 0, playing: false, endMs: 0 };
  var bufCache = {};
  function ensureCtx() {
    if (!TP.ctx) { var AC = window.AudioContext || window.webkitAudioContext; if (AC) TP.ctx = new AC(); }
    return TP.ctx;
  }
  function loadBuf(url) {
    if (bufCache[url]) return bufCache[url];
    var p = fetch(url).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.arrayBuffer(); })
      .then(function (ab) { return new Promise(function (res, rej) { TP.ctx.decodeAudioData(ab, res, rej); }); })
      .catch(function () { return null; }); // missing/undecodable clip -> silent
    bufCache[url] = p; return p;
  }
  function winEvents(fromMs) {
    if (!state.selDate) return [];
    var mid = dayMid(state.selDate);
    // Only events inside the visible ROI window [fromMs, win.hi] — never the whole
    // rest of the day (that floods the server and Web Audio with 1000s of clips).
    var hi = state._win ? state._win.hi : DAY;
    return eventsOnDay(state.selDate).filter(function (o) { return eventMatches(o.e); })
      .map(function (o) { return { e: o.e, ms: o.w.t - mid }; })
      .filter(function (o) { return o.ms >= fromMs - 1 && o.ms <= hi; })
      .sort(function (a, b) { return a.ms - b.ms; });
  }
  // Map the in-window events to a single continuous recording region anchored at
  // startMs (day-ms). Returns null if they parse cross-recording -> caller falls back.
  function windowRegion(evs, startMs) {
    var parts = evs.map(function (o) {
      var k = String(o.e.key || ""), i = k.indexOf("_");
      if (i < 0) return null;
      var off = parseInt(k.slice(i + 1), 10);
      return isNaN(off) ? null : { sha: k.slice(0, i), off: off / 1000, dayMs: o.ms };
    });
    if (parts.some(function (p) { return !p; })) return null;
    var sha = parts[0].sha;
    if (parts.some(function (p) { return p.sha !== sha; })) return null;
    var a = parts[0];
    var startOff = Math.max(0, a.off + (startMs - a.dayMs) / 1000);   // offset at day-time startMs
    var endMs = Math.min(TP.endMs, state._win ? state._win.hi : DAY);
    return { sha: sha, start: startOff, dur: Math.max(0.2, (endMs - startMs) / 1000) };
  }
  function stopWindowPlayback(keepCursor) {
    if (TP.raf) { cancelAnimationFrame(TP.raf); TP.raf = 0; }
    TP.sources.forEach(function (s) { try { s.stop(); } catch (e) {} });
    TP.sources = [];
    if (TP._audio) { try { TP._audio.pause(); } catch (e) {} TP._audio = null; }
    TP.playing = false;
    if (!keepCursor) { /* keep cursor where it is */ }
    updateWinTransport();
  }
  function playWindow() {
    var ctx = ensureCtx();
    if (ctx && ctx.state === "suspended") ctx.resume();
    var startMs = state.playCursorMs != null ? state.playCursorMs : (state._win ? state._win.lo : 0);
    var evs = winEvents(startMs);
    if (!evs.length) { state.playCursorMs = startMs; updateWinTransport(); return; }
    TP.endMs = Math.max.apply(null, evs.map(function (o) { return o.ms + (o.e.duration_sec || 0) * 1000; }));
    // master clock is wall-time (runs even if audio can't); decode clips best-effort and schedule sound
    TP.startPerf = performance.now();
    TP.startMs = startMs;
    TP.playing = true;
    updateWinTransport();
    loopWindow();

    // Preferred: one continuous RAW region for the window (real timing, no overlap
    // /flood). Playhead is driven by loopWindow's wall clock.
    if (state.mode === "label") {
      var reg = windowRegion(evs, startMs);
      if (reg) {
        var url = "api/audio/" + encodeURIComponent(reg.sha) + "?source=raw&start=" + reg.start.toFixed(3) + "&dur=" + reg.dur.toFixed(3);
        TP._audio = new Audio(url);
        var pr = TP._audio.play(); if (pr && pr.catch) pr.catch(function () {});
        return;
      }
    }
    // Fallback (read-only / cross-recording): schedule the per-bark clips.
    if (ctx) {
      var clipped = evs.filter(function (o) { return o.e.snippet_url; });
      Promise.all(clipped.map(function (o) { return loadBuf(o.e.snippet_url); })).then(function (bufs) {
        if (!TP.playing) return;
        var anchorCtx = ctx.currentTime + 0.05;
        var elapsed = (performance.now() - TP.startPerf) / 1000; // account for decode time
        TP.sources = [];
        clipped.forEach(function (o, i) {
          var buf = bufs[i]; if (!buf) return; // silent gap for missing/undecodable clip
          var when = anchorCtx + Math.max(0, (o.ms - startMs) / 1000 - elapsed);
          var src = ctx.createBufferSource(); src.buffer = buf; src.connect(ctx.destination);
          try { src.start(when); TP.sources.push(src); } catch (e) {}
        });
      });
    }
  }
  function loopWindow() {
    if (!TP.playing) return;
    var cursor = TP.startMs + (performance.now() - TP.startPerf);
    if (cursor >= TP.endMs + 400 || cursor >= DAY) {
      state.playCursorMs = Math.min(TP.endMs, DAY);
      stopWindowPlayback(true); renderDay(); return;
    }
    state.playCursorMs = cursor;
    var w = state._win;
    if (w && cursor > w.hi) {
      var span = STEPS[state.roiZoom];
      state.roiCenter = Math.min(DAY - span / 2, Math.max(span / 2, cursor + span / 2));
      renderDay();
    } else {
      var ph = $("#roiPlayhead");
      if (ph && w) ph.style.left = ((cursor - w.lo) / (w.hi - w.lo) * 100) + "%";
      updateWinTransport();
    }
    TP.raf = requestAnimationFrame(loopWindow);
  }
  function updateWinTransport() {
    var b = $("#winPlayBtn"); if (b) b.textContent = TP.playing ? "\u23f8" : "\u25b6";
    var t = $("#winCursor");
    if (t) t.innerHTML = state.playCursorMs != null ? ("cursor <b>" + msToHMS(state.playCursorMs) + "</b>") : "tap the strip to set the play cursor";
  }
  function msToHMS(ms) { ms = Math.max(0, Math.min(DAY, ms)); var s = Math.floor(ms / 1000); return pad2(Math.floor(s / 3600)) + ":" + pad2(Math.floor((s % 3600) / 60)) + ":" + pad2(s % 60); }
  function toggleWindowPlay() { if (TP.playing) stopWindowPlayback(true); else playWindow(); }
  function setPlayCursor(ms) {
    state.playCursorMs = Math.max(0, Math.min(DAY, ms));
    if (TP.playing) { stopWindowPlayback(true); playWindow(); } // re-anchor transport at new cursor
    else { var w = state._win, ph = $("#roiPlayhead");
      if (w && state.playCursorMs >= w.lo && state.playCursorMs <= w.hi) {
        if (!ph) { renderDay(); } else { ph.style.left = ((state.playCursorMs - w.lo) / (w.hi - w.lo) * 100) + "%"; }
      } else { renderDay(); }
      updateWinTransport();
    }
  }
  // pan the ROI window + tap-to-select / tap-to-set-cursor (mouse + touch)
  function wireRoiStrip() {
    var strip = $("#roiStrip"); if (!strip) return;
    strip.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      var rect = strip.getBoundingClientRect();
      var w = state._win, span = w.hi - w.lo;
      var center0 = (w.lo + w.hi) / 2;
      var tickEl = e.target.closest ? e.target.closest(".tick[data-id]") : null;
      var drag = { x0: e.clientX, center0: center0, span: span, moved: false, tickId: tickEl ? tickEl.getAttribute("data-id") : null };
      function move(ev) {
        var rect2 = (($("#roiStrip") || strip).getBoundingClientRect());
        var dx = ev.clientX - drag.x0;
        if (!drag.moved && Math.abs(dx) > 4) { drag.moved = true; var s = $("#roiStrip"); if (s) s.classList.add("dragging"); }
        if (drag.moved) {
          var deltaMs = (dx / rect2.width) * drag.span;
          state.roiCenter = Math.min(DAY - drag.span / 2, Math.max(drag.span / 2, drag.center0 - deltaMs));
          renderDay();
        }
      }
      function up(ev) {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        var s = $("#roiStrip"); if (s) s.classList.remove("dragging");
        if (!drag.moved) { // a tap
          if (drag.tickId != null) { selectEvent(drag.tickId, false); }
          else {
            var r = (($("#roiStrip") || strip).getBoundingClientRect());
            var frac = (ev.clientX - r.left) / r.width;
            var wnow = state._win;
            setPlayCursor(wnow.lo + frac * (wnow.hi - wnow.lo));
          }
        }
      }
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    });
  }

  // ==========================================================
  // AUDIO
  // ==========================================================
  // URL for a single event's clip: ENHANCED region on demand in labeling mode;
  // the pre-cut snippet file as a fallback (read-only / unparseable key).
  // Context padding is user-adjustable (state.clipPad), seeded from config.
  function clipUrl(e) {
    if (state.mode === "label") {
      var o = eventOffset(e);
      if (o) {
        var pad = state.clipPad != null ? state.clipPad : 0.5;
        var start = Math.max(0, o.off - pad);
        var dur = (o.dur || 0) + 2 * pad;
        return "api/audio/" + encodeURIComponent(o.sha) +
          "?source=enhanced&start=" + start.toFixed(3) + "&dur=" + dur.toFixed(3);
      }
    }
    return e.snippet_url || null;
  }
  function audioHtml(e) {
    if (!clipUrl(e)) return '<div class="clipnote">No audio for this event.</div>';
    return '<audio id="player" controls preload="none"></audio>' +
      '<div class="clipnote" id="clipnote"></div>';
  }
  function setupAudio(e) {
    audio = $("#player");
    var url = clipUrl(e);
    if (!audio || !url) return;
    audio.src = url; // relative to this page
    var note = $("#clipnote");
    audio.addEventListener("error", function () {
      if (note) { note.textContent = "Clip unavailable"; note.className = "clipnote bad"; }
    });
    if (state._play) {
      state._play = false;
      var p = audio.play();
      if (p && p.catch) p.catch(function () { if (note) { note.textContent = "Clip unavailable"; note.className = "clipnote bad"; } });
    }
  }

  // ==========================================================
  // BURST PLAYBACK (SEQUENTIAL CLIPS)
  // ==========================================================
  function stopBurstPlayback() {
    if (state._burstAudio) { try { state._burstAudio.pause(); } catch (e) {} state._burstAudio = null; }
  }
  // Offset (s) + recording sha from the stable event_key "<sha12>_<offsetms>".
  function eventOffset(e) {
    var k = String(e.key || ""), i = k.indexOf("_");
    if (i < 0) return null;
    var off = parseInt(k.slice(i + 1), 10);
    return isNaN(off) ? null : { sha: k.slice(0, i), off: off / 1000, dur: e.duration_sec || 0 };
  }
  // The continuous span [start, dur] covering a burst, if all members share a
  // recording and have parseable offsets. Returns null -> caller falls back.
  function burstRegion(mem) {
    var parts = mem.map(function (o) { return eventOffset(o.e); });
    if (parts.some(function (p) { return !p; })) return null;
    var sha = parts[0].sha;
    if (parts.some(function (p) { return p.sha !== sha; })) return null;   // cross-recording
    var pad = 0.15;
    var start = Math.max(0, Math.min.apply(null, parts.map(function (p) { return p.off; })) - pad);
    var end = Math.max.apply(null, parts.map(function (p) { return p.off + p.dur; })) + pad;
    return { sha: sha, start: start, dur: Math.max(0.2, end - start) };
  }
  function playBurst(burstId) {
    if (typeof stopWindowPlayback === "function") { try { stopWindowPlayback(true); } catch (e) {} }
    stopBurstPlayback();
    var mem = filteredRows()
      .filter(function (o) { return o.e._burstId === burstId; })
      .sort(function (a, b) { return eventStartMs(a.e) - eventStartMs(b.e); });
    if (!mem.length) return;
    // Preferred: play the whole burst as ONE continuous region from the original
    // recording (real timing, no repeated padding). Needs the label server.
    if (state.mode === "label") {
      var r = burstRegion(mem);
      if (r) {
        var url = "api/audio/" + encodeURIComponent(r.sha) + "?source=raw&start=" + r.start.toFixed(3) + "&dur=" + r.dur.toFixed(3);
        var a = state._burstAudio = new Audio(url);
        var p = a.play(); if (p && p.catch) p.catch(function () {});
        return;
      }
    }
    // Fallback (read-only, or cross-recording burst): sequence the padded clips.
    var clips = mem.filter(function (o) { return o.e.snippet_url; }).map(function (o) { return o.e.snippet_url; });
    if (!clips.length) return;
    var i = 0, aud = state._burstAudio = new Audio();
    function next() {
      if (aud !== state._burstAudio) return;
      if (i >= clips.length) { state._burstAudio = null; return; }
      aud.src = clips[i++];
      var pp = aud.play(); if (pp && pp.catch) pp.catch(next);
    }
    aud.addEventListener("ended", next);
    aud.addEventListener("error", next);
    next();
  }

  // ==========================================================
  // RENDER: TABLE
  // ==========================================================
  // Group events into "bursts" by time gap (client-side, adjustable). Annotates
  // each event with _burstSize so the filter/UI can use it. Recomputed whenever
  // the burst-gap slider changes; membership is over ALL events (time-ordered),
  // so a burst is a real acoustic group independent of other filters.
  function eventStartMs(e) { return Date.parse(e.abs_start_utc); }
  function computeBursts() {
    if (!R || !R.events) return;
    var evs = R.events.slice().sort(function (a, b) { return eventStartMs(a) - eventStartMs(b); });
    var gapMs = (state.filters.burstGap || 0) * 1000;
    var bid = 0, lastEnd = null;
    evs.forEach(function (e) {
      var s = eventStartMs(e), end = s + (e.duration_sec || 0) * 1000;
      if (lastEnd === null || (s - lastEnd) > gapMs) { bid++; lastEnd = end; }
      else { lastEnd = Math.max(lastEnd, end); }
      e._burstId = bid;
    });
    var counts = {};
    evs.forEach(function (e) { counts[e._burstId] = (counts[e._burstId] || 0) + 1; });
    evs.forEach(function (e) { e._burstSize = counts[e._burstId]; });
  }

  function eventMatches(e) {
    var f = state.filters;
    if (f.night && !e.night) return false;
    if (f.cls !== "all" && e.class !== f.cls) return false;
    if (f.minConf > 0 && !(e.peak_conf != null && e.peak_conf >= f.minConf)) return false;
    if (f.minInt > 0 && !(e.intensity_relative != null && e.intensity_relative >= f.minInt)) return false;
    if (f.minBurst > 1 && (e._burstSize || 1) < f.minBurst) return false;
    if (f.status !== "all") { var st = evStatus(e); if (f.status === "labeled" ? st === "unlabeled" : st !== f.status) return false; }
    if (f.dog !== "all" && effectiveDogs(e).indexOf(f.dog) < 0) return false;
    return true;
  }
  function sortRows(rows) {
    var k = state.sortKey, dir = state.sortDir;
    rows.sort(function (A, B) {
      var a, b;
      if (k === "class") return dir * String(A.e.class || "").localeCompare(String(B.e.class || ""));
      if (k === "dur") { a = A.e.duration_sec || 0; b = B.e.duration_sec || 0; }
      else if (k === "conf") { a = A.e.peak_conf || 0; b = B.e.peak_conf || 0; }
      else if (k === "dbfs") { a = A.e.intensity_dbfs == null ? -999 : A.e.intensity_dbfs; b = B.e.intensity_dbfs == null ? -999 : B.e.intensity_dbfs; }
      else { a = A.w.t; b = B.w.t; }
      return dir * (a < b ? -1 : a > b ? 1 : 0);
    });
    return rows;
  }
  function syncSortUI() {
    var s = $("#f-sort"); if (s) s.value = state.sortKey;
    var b = $("#f-sortdir"); if (b) b.textContent = state.sortDir > 0 ? "\u2191" : "\u2193";
  }
  function applySort() { renderTable(); renderDay(); syncSortUI(); }
  function filteredRows() {
    var f = state.filters;
    var rows = R.events.filter(function (e) {
      if (!eventMatches(e)) return false;
      if (f.dayOnly && state.selDate && wall(e.abs_start_local).date !== state.selDate) return false;
      return true;
    }).map(function (e) { return { e: e, w: wall(e.abs_start_local) }; });

    return sortRows(rows);
  }
  function arr(key) { return state.sortKey === key ? (state.sortDir > 0 ? " \u25b2" : " \u25bc") : ""; }
  function rowHtml(o) {
    var e = o.e, on = e.id === state.selId;
    return '<tr class="' + (on ? "on" : "") + '">' +
      '<td data-label="Status">' + statusDot(e) + '</td>' +
      '<td class="mono" data-label="Time">' + o.w.time + '</td>' +
      '<td class="mono" data-label="Date" style="color:var(--muted)">' + MON[parseInt(o.w.date.slice(5,7),10)-1] + " " + parseInt(o.w.date.slice(8,10),10) + '</td>' +
      '<td class="cls-cell" data-label="Class"><span class="clsdot" style="background:' + intensityColor(e.intensity_relative) + '"></span>' + esc(e.class) + (e.night ? '<span class="tag">night</span>' : "") + '</td>' +
      '<td class="mono r" data-label="Dur">' + fmtDur(e.duration_sec) + '</td>' +
      '<td class="mono r" data-label="Conf">' + fmtConf(e.peak_conf) + '</td>' +
      '<td class="mono r" data-label="Loudness">' + fmtDbfs(e.intensity_dbfs) + '</td>' +
      '<td class="mono r" data-label="Rel">' + fmtRel(e.intensity_relative) + '</td>' +
      '<td data-label="Dog">' + (predHtml(e) || "—") + '</td>' +
      '<td class="train-only" data-label="Your label">' + labelControl(e) + '</td>' +
      '<td class="r" data-label="Clip"><button class="playbtn" data-action="playEvent" data-id="' + e.id + '"' + (clipUrl(e) ? "" : " disabled title=\"no audio\"") + ' aria-label="Play clip">▶</button></td>' +
    '</tr>';
  }
  function burstHeaderHtml(burstId, mem) {
    var tally = {};
    mem.forEach(function (o) { effectiveDogs(o.e).forEach(function (d) { if (!isSpecial(d)) tally[d] = (tally[d] || 0) + 1; }); });
    var dogs = Object.keys(tally).sort(function (a, b) { return tally[b] - tally[a]; })
      .map(function (d) { return esc(labelName(d)) + " ×" + tally[d]; }).join(", ");
    var canPlay = mem.some(function (o) { return clipUrl(o.e); });
    return '<tr class="bursthdr"><td colspan="11"><div class="bh">' +
      '<button class="playbtn" data-action="playBurst" data-burst="' + burstId + '"' + (canPlay ? "" : " disabled") + ' title="Play every clip in this burst" aria-label="Play burst">▶</button>' +
      '<span class="cnt">' + mem.length + ' barks</span>' +
      '<span class="rng">' + esc(mem[0].w.time) + " – " + esc(mem[mem.length - 1].w.time) + '</span>' +
      (dogs ? '<span>' + dogs + '</span>' : "") +
      '</div></td></tr>';
  }
  function groupedRowsHtml(rows) {
    // bucket the filtered rows by burst, order bursts (and members) chronologically
    var buckets = {}, order = [];
    rows.forEach(function (o) { var b = o.e._burstId; if (!(b in buckets)) { buckets[b] = []; order.push(b); } buckets[b].push(o); });
    var minT = function (arr) { return Math.min.apply(null, arr.map(function (o) { return eventStartMs(o.e); })); };
    order.sort(function (b1, b2) { return minT(buckets[b1]) - minT(buckets[b2]); });
    return order.map(function (b) {
      var mem = buckets[b].slice().sort(function (a, c) { return eventStartMs(a.e) - eventStartMs(c.e); });
      var header = mem.length >= 2 ? burstHeaderHtml(b, mem) : "";   // no header for lone barks
      return header + mem.map(rowHtml).join("");
    }).join("");
  }

  function renderTable() {
    var _pl = $("#tableWrap") ? $("#tableWrap").querySelector(".logscroll") : null;
    var prevLogScroll = _pl ? _pl.scrollTop : 0;
    var rows = filteredRows();
    $("#shown").textContent = rows.length + " of " + R.events.length + " shown";
    var head =
      '<thead><tr>' +
        '<th></th>' +
        '<th data-sort="time">Time<span class="arr">' + arr("time") + '</span></th>' +
        '<th>Date</th>' +
        '<th data-sort="class">Class<span class="arr">' + arr("class") + '</span></th>' +
        '<th class="r" data-sort="dur">Dur<span class="arr">' + arr("dur") + '</span></th>' +
        '<th class="r" data-sort="conf">Conf<span class="arr">' + arr("conf") + '</span></th>' +
        '<th class="r" data-sort="dbfs">Loudness<span class="arr">' + arr("dbfs") + '</span></th>' +
        '<th class="r">Rel</th>' +
        '<th>Dog</th>' +
        '<th class="train-only">Your label</th>' +
        '<th></th>' +
      '</tr></thead>';
    var body = state.filters.groupBurst ? groupedRowsHtml(rows) : rows.map(rowHtml).join("");
    $("#tableWrap").innerHTML = '<div class="logscroll"><table class="log">' + head + '<tbody>' + (body || '<tr><td colspan="11" style="color:var(--faint);padding:18px 0">No events match the current filters.</td></tr>') + '</tbody></table></div>';
    var _nl = $("#tableWrap").querySelector(".logscroll"); if (_nl) _nl.scrollTop = prevLogScroll;
  }

  // ==========================================================
  // RENDER: PROVENANCE
  // ==========================================================
  function renderProv() {
    var p = R.parameters || {};
    var groups = [];
    if (p.model) groups.push(["Model", [["Name", p.model.name], ["Version", p.model.version], ["Device", p.model.device]]]);
    if (p.detection) groups.push(["Detection", [["Threshold", p.detection.threshold], ["Min event", p.detection.min_event_seconds + " s"], ["Merge gap", p.detection.merge_gap_seconds + " s"], ["Classes", (p.detection.dog_classes || []).length]]]);
    if (p.normalization) groups.push(["Normalization", [["Enabled", p.normalization.enabled ? "yes" : "no"], ["Target peak", p.normalization.target_peak], ["Max gain", p.normalization.max_gain + " dB"], ["Noise floor", p.normalization.noise_floor]]]);
    if (p.audio) groups.push(["Audio", [["Sample rate", (p.audio.sample_rate / 1000) + " kHz"], ["Window", p.audio.window_seconds + " s"]]]);
    if (p.intensity) groups.push(["Intensity", [["Metric", String(p.intensity.metric || "").toUpperCase()], ["Scope", p.intensity.scope + (p.intensity.scope === "per_file" ? " (resets per file)" : "")]]]);
    if (p.snippets) groups.push(["Snippets", [["Normalized", p.snippets.normalized ? "yes" : "no"], ["Target", p.snippets.target_lufs + " LUFS"]]]);
    groups.push(["Recordings", R.recordings.map(function (r) { return [r.original_filename, (r.sha256 || "").slice(0, 10) + (r.sha256 ? "\u2026" : "")]; })]);
    groups.push(["Timestamps", [["Source", (R.recordings[0] || {}).timestamp_source || "\u2014"], ["Timezone", R.timezone]]]);

    var html =
      '<details class="prov"><summary><span class="mono">+</span> Provenance &amp; method</summary>' +
        '<div class="provgrid">' +
          groups.map(function (g) {
            return '<div class="g"><div class="gh">' + esc(g[0]) + '</div>' +
              g[1].map(function (kv) { return '<div class="kv"><span class="pk">' + esc(kv[0]) + '</span><span class="pv">' + esc(kv[1]) + '</span></div>'; }).join("") +
            '</div>';
          }).join("") +
        '</div>' +
      '</details>';
    $("#provWrap").innerHTML = html;
    $("#foot").textContent = "Generated " + (R.generated_at ? wall(R.generated_at).date + " " + wall(R.generated_at).time : "\u2014") + " \u00b7 schema v" + R.schema_version + " \u00b7 " + (R.timezone || "");
  }

  // ==========================================================
  // RENDER: DOG IDENTIFICATION RELIABILITY
  // ==========================================================
  function renderIdentification() {
    var wrap = $("#idWrap"); if (!wrap) return;
    var m = R.identification_metrics;
    var H = '<h2 class="sec">Dog identification reliability</h2>';
    if (m == null) { wrap.innerHTML = H + '<p class="idnote">No dog model trained yet \u2014 label more clips to enable it.</p>'; return; }
    if (!m.trained) {
      var reason = m.reason ? '<p class="idnote">' + esc(m.reason) + '</p>' : "";
      var lc = m.label_counts || {};
      var lcRows = Object.keys(lc).map(function (name) {
        var v = lc[name], have, needed;
        if (typeof v === "number") { have = v; needed = null; }
        else { have = v.have != null ? v.have : (v.count != null ? v.count : (v.n != null ? v.n : 0)); needed = v.needed != null ? v.needed : (v.required != null ? v.required : (v.target != null ? v.target : null)); }
        var pct = needed ? Math.min(100, Math.round(have / needed * 100)) : (have > 0 ? 100 : 0);
        var ok = needed != null && have >= needed;
        var txt = needed != null ? (have + " / " + needed + " needed") : (have + " labeled");
        return '<div class="lcrow"><span class="lcname">' + esc(name) + '</span>' +
          '<span class="lcbar"><span class="lcfill" style="width:' + pct + '%;background:' + (ok ? "#2f8a5b" : "#c98a3e") + '"></span></span>' +
          '<span class="lcval">' + esc(txt) + '</span></div>';
      }).join("");
      wrap.innerHTML = H + '<p class="idnote">Not enough labels to train the dog model yet.</p>' + reason +
        (lcRows ? '<div class="lclist">' + lcRows + '</div>' : "");
      return;
    }
    var acc = m.accuracy != null ? Math.round(m.accuracy * 100) : null;
    var folds = m.cv_folds != null ? m.cv_folds : null;
    var labels = (m.labels && m.labels.length) ? m.labels : (R.dogs || []);
    var cm = m.confusion_matrix || [];
    var mx = 0; cm.forEach(function (r) { (r || []).forEach(function (v) { if (v > mx) mx = v; }); });
    var cmHead = '<tr><th class="cmcorner">true \\ pred</th>' + labels.map(function (l) { return '<th class="cmh">' + esc(l) + '</th>'; }).join("") + '</tr>';
    var cmBody = cm.map(function (row, i) {
      return '<tr><th class="cmrow">' + esc(labels[i]) + '</th>' + (row || []).map(function (v, j) {
        var diag = i === j;
        var a = mx > 0 ? v / mx : 0;
        var bg = v === 0 ? "transparent" : (diag ? "rgba(47,138,91," + (0.15 + 0.75 * a).toFixed(3) + ")" : "rgba(168,58,41," + (0.15 + 0.75 * a).toFixed(3) + ")");
        return '<td class="cmcell' + (diag ? " diag" : "") + '" style="background:' + bg + '" title="' + esc(labels[i] + " \u2192 " + labels[j] + ": " + v) + '">' + v + '</td>';
      }).join("") + '</tr>';
    }).join("");
    var matrix = '<div><div class="caps" style="margin-top:0">Confusion matrix</div><div class="cmwrap"><table class="cm"><thead>' + cmHead + '</thead><tbody>' + cmBody + '</tbody></table></div><div class="cmcap">Rows = true dog, columns = predicted. Diagonal (green) = correct; off-diagonal (red) = confusions.</div></div>';
    var pd = m.per_dog || {};
    var pdList = Array.isArray(pd) ? pd.map(function (o) { return { name: o.label != null ? o.label : (o.dog != null ? o.dog : o.name), o: o }; })
                                   : Object.keys(pd).map(function (k) { return { name: k, o: pd[k] }; });
    var pct1 = function (v) { return v == null ? "\u2014" : Math.round(v * 100) + "%"; };
    var pdRows = pdList.map(function (r) {
      var o = r.o || {};
      var f1 = o.f1 != null ? o.f1 : o.f_score;
      return '<tr><td>' + esc(r.name) + '</td><td>' + pct1(o.precision) + '</td><td>' + pct1(o.recall) + '</td><td>' + pct1(f1) + '</td><td>' + (o.support != null ? o.support : "\u2014") + '</td></tr>';
    }).join("");
    var perdog = '<div><div class="caps" style="margin-top:0">Per-dog performance</div><table class="pd"><thead><tr><th>Dog</th><th>Precision</th><th>Recall</th><th>F1</th><th>Support</th></tr></thead><tbody>' + pdRows + '</tbody></table></div>';
    wrap.innerHTML = H +
      '<div class="idhead"><span class="idacc">' + (acc != null ? acc + "%" : "\u2014") + '</span><span class="idacclbl">accurate on held-out labels' + (folds != null ? ", " + folds + "-fold cross-validation" : "") + '</span></div>' +
      '<p class="idnote">An estimate for the model\u2019s <b>suggested</b> predictions only \u2014 human-confirmed labels are taken as given, not scored.</p>' +
      '<div class="idcols">' + matrix + perdog + '</div>';
  }

  // ==========================================================
  // ACTIONS
  // ==========================================================
  function selectDay(dateStr) {
    if (TP.playing) stopWindowPlayback(true);
    state.playCursorMs = null;
    state.selDate = dateStr;
    state.roiCenter = null;
    state.roiZoom = 2;
    state.selId = firstEventIdOn(dateStr);
    renderCalendar(); renderDay(); renderTable();
  }
  function selectEvent(id, play) {
    id = parseInt(id, 10);
    var e = null; for (var i = 0; i < R.events.length; i++) if (R.events[i].id === id) { e = R.events[i]; break; }
    if (!e) return;
    var d = wall(e.abs_start_local).date;
    var t = wall(e.abs_start_local).t - dayMid(d);
    var inWin = state._win && state._win.date === d && t >= state._win.lo && t <= state._win.hi;
    state.selDate = d; state.selId = id;
    if (!inWin) state.roiCenter = t; // only recenter when the event isn't already in view
    state._play = !!play;
    renderCalendar(); renderDay(); renderTable();
    prefetchNext(e);
  }
  // Warm the browser cache with the next event's clip so the next click is instant.
  function prefetchNext(e) {
    if (state.mode !== "label") return;
    try {
      var rows = filteredRows();
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].e.id === e.id) {
          var n = rows[i + 1]; var u = n && clipUrl(n.e);
          if (u) fetch(u).catch(function () {});
          return;
        }
      }
    } catch (x) { /* prefetch is best-effort */ }
  }

  function bindControls() {
    if (state._bound) return; // idempotent: never double-bind if load()/boot() re-runs
    state._bound = true;
    // populate class select
    var sel = $("#f-cls");
    classes.forEach(function (c) { var o = document.createElement("option"); o.value = c; o.textContent = c; sel.appendChild(o); });
    var dogSel = $("#f-dog");
    labelOptions().forEach(function (o) { var op = document.createElement("option"); op.value = o.id; op.textContent = o.name; dogSel.appendChild(op); });

    $("#f-night").addEventListener("change", function (e) { state.filters.night = e.target.checked; renderFiltered(); });
    $("#f-day").addEventListener("change", function (e) { state.filters.dayOnly = e.target.checked; renderTable(); });
    sel.addEventListener("change", function (e) { state.filters.cls = e.target.value; renderFiltered(); });
    $("#f-conf").addEventListener("input", function (e) { state.filters.minConf = parseFloat(e.target.value); $("#f-conf-v").textContent = Math.round(state.filters.minConf * 100) + "%"; renderFiltered(); });
    $("#f-int").addEventListener("input", function (e) { state.filters.minInt = parseFloat(e.target.value); $("#f-int-v").textContent = state.filters.minInt.toFixed(2); renderFiltered(); });
    $("#f-burstgap").addEventListener("input", function (e) { state.filters.burstGap = parseFloat(e.target.value); $("#f-burstgap-v").textContent = state.filters.burstGap.toFixed(1) + "s"; computeBursts(); renderFiltered(); });
    $("#f-minburst").addEventListener("input", function (e) { state.filters.minBurst = parseInt(e.target.value, 10); $("#f-minburst-v").textContent = String(state.filters.minBurst); renderFiltered(); });
    $("#f-groupburst").addEventListener("change", function (e) { state.filters.groupBurst = e.target.checked; renderTable(); });
    var cpad = $("#f-clippad");
    if (cpad) {
      cpad.value = state.clipPad;
      $("#f-clippad-v").textContent = state.clipPad.toFixed(1) + "s";
      cpad.addEventListener("input", function (e) {
        state.clipPad = parseFloat(e.target.value);
        $("#f-clippad-v").textContent = state.clipPad.toFixed(1) + "s";
        try { localStorage.setItem("barkdetect.clipPad", String(state.clipPad)); } catch (x) {}
      });
    }
    $("#f-status").addEventListener("change", function (e) { state.filters.status = e.target.value; renderFiltered(); });
    $("#f-dog").addEventListener("change", function (e) { state.filters.dog = e.target.value; renderFiltered(); });
    $("#f-sort").addEventListener("change", function (e) { state.sortKey = e.target.value; applySort(); });

    $("#root").addEventListener("change", function (ev) {
      var s = ev.target.closest && ev.target.closest("nonexistent-labelsel");
      if (s) setLabel(s.getAttribute("data-key"), s.value, true);
    });
    document.addEventListener("keydown", onKey);

    // delegated clicks
    $("#root").addEventListener("click", function (ev) {
      var el = ev.target.closest("[data-action]");
      var th = ev.target.closest("th[data-sort]");
      if (th) {
        var k = th.getAttribute("data-sort");
        if (state.sortKey === k) state.sortDir *= -1; else { state.sortKey = k; state.sortDir = 1; }
        applySort(); return;
      }
      var lt = ev.target.closest("[data-role=labeltoggle]");
      if (lt) { toggleLabel(lt.getAttribute("data-key"), lt.getAttribute("data-id")); return; }
      if (!el) return;
      var a = el.getAttribute("data-action");
      if (a === "toggleSortDir") { state.sortDir *= -1; applySort(); return; }
      else if (a === "selectDay") selectDay(el.getAttribute("data-date"));
      else if (a === "selectEvent") selectEvent(el.getAttribute("data-id"), false);
      else if (a === "playEvent") { ev.stopPropagation(); selectEvent(el.getAttribute("data-id"), true); }
      else if (a === "playBurst") { ev.stopPropagation(); playBurst(parseInt(el.getAttribute("data-burst"), 10)); return; }
      else if (a === "winPlay") { toggleWindowPlay(); return; }
      else if (a === "zoomIn") { if (state.playCursorMs != null) state.roiCenter = state.playCursorMs; state.roiZoom = Math.min(STEPS.length - 1, state.roiZoom + 1); renderDay(); }
      else if (a === "zoomOut") { if (state.playCursorMs != null) state.roiCenter = state.playCursorMs; state.roiZoom = Math.max(0, state.roiZoom - 1); renderDay(); }
      else if (a === "moveRoi") {
        var box = el.getBoundingClientRect();
        var frac = (ev.clientX - box.left) / box.width;
        state.roiCenter = Math.max(0, Math.min(DAY, frac * DAY));
        renderDay();
      }
    });
  }

  // ==========================================================
  // BOOT
  // ==========================================================
  function boot(data) {
    R = data;
    R.coverage = R.coverage || [];
    R.gaps = R.gaps || [];
    R.daily_summary = R.daily_summary || [];
    R.events = R.events || [];
    R.recordings = R.recordings || [];
    offMin = detectOff();
    classes = R.events.map(function (e) { return e.class; }).filter(function (v, i, a) { return v && a.indexOf(v) === i; }).sort();
    // labels come from the API (authoritative) when reachable; otherwise read-only
    state.labels = (state.mode === "label" && state.apiLabels) ? state.apiLabels : {};
    // clip padding: user override (localStorage) wins, else the config default from results.json
    var cfgPad = (((R.parameters || {}).snippets || {}).padding_seconds);
    var saved = null; try { saved = localStorage.getItem("barkdetect.clipPad"); } catch (e) {}
    state.clipPad = saved != null ? parseFloat(saved) : (cfgPad != null ? cfgPad : 0.5);
    computeBursts();   // annotate events with _burstSize for the burst filter

    // default selected day: first day with events, else first recorded day, else first month day
    if (R.daily_summary.length) state.selDate = R.daily_summary[0].date;
    else {
      var m = monthRange()[0];
      for (var d = 1; d <= 31 && !state.selDate; d++) { var ds = m.year + "-" + pad2(m.month) + "-" + pad2(d); if (covOverlapsDay(ds)) state.selDate = ds; }
      if (!state.selDate) state.selDate = m.year + "-" + pad2(m.month) + "-01";
    }
    state.selId = firstEventIdOn(state.selDate);

    renderSummary();
    renderCalendar();
    renderDay();
    bindControls();
    renderTable();
    renderProv();
    renderIdentification();
    updateLabelStats();
    syncSortUI();
    if (state.mode === "label") $("#root").classList.add("labeling");

    $("#loading").hidden = true;
    $("#root").hidden = false;
  }

  load();
})();
