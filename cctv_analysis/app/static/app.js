"use strict";
// Drishti-AI desktop UI (single-folder screens and shared helpers; case screens are in case.js).
// All data is inserted with textContent (never innerHTML).
const TOKEN = document.querySelector('meta[name="cctv-token"]').content;
const $ = (id) => document.getElementById(id);
const S = { features: null, job: null, poll: null, resOut: "", offset: 0, limit: 48, detail: null, fbTarget: null };

// ------------------------------------------------------------------ helpers
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "selected" || k === "disabled" || k === "value") e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false)
    e.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  return e;
}
function clear(node, ...kids) { node.replaceChildren(...kids.flat().filter(Boolean)); }
function toast(msg, err) {
  const t = $("toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), err ? 7000 : 3500);
}
async function api(method, path, body) {
  const opt = { method, headers: { "X-CCTV-Token": TOKEN } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok) {
    const e = new Error(data.error || ("HTTP " + r.status)); e.status = r.status; e.data = data; throw e;
  }
  return data;
}
function fileUrl(out, rel) { return rel ? "/api/file?out=" + encodeURIComponent(out) + "&p=" + encodeURIComponent(rel) : ""; }
function hms(s) { s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return (h ? h + " h " : "") + m + " min" + (h ? "" : " " + (s % 60) + " s"); }
function clock(s) { s = Math.max(0, Math.floor(s || 0)); return [s / 3600, s % 3600 / 60, s % 60].map(x => String(Math.floor(x)).padStart(2, "0")).join(":"); }
function rid() { return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, "").slice(0, 32); }
async function waitJob(job, onTick) {
  for (;;) {
    const j = (await api("GET", "/api/jobs/" + job.id + "?log=0")).job;
    if (onTick) onTick(j);
    if (!["queued", "running"].includes(j.status)) return j;
    await new Promise(r => setTimeout(r, 1000));
  }
}
function busy(btn, on) { btn.disabled = on; }
// "latest request wins": a slow, older response must never overwrite a newer one
const SEQ = {};
function nextSeq(k) { SEQ[k] = (SEQ[k] || 0) + 1; return SEQ[k]; }
function stale(k, n) { return SEQ[k] !== n; }

// --------------------------------------------------------------------- tabs
document.querySelectorAll("#tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
const TAB_HOOKS = {};             // tab name -> function run when the tab is shown (case.js adds the case tabs)
function showTab(t) {
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  document.querySelectorAll(".tab").forEach(s => s.classList.toggle("on", s.id === "tab-" + t));
  if (t === "highlights") loadHighlights();
  if (t === "faces") loadFaces();
  if (t === "export") renderExport();
  if (t === "settings") loadSettings();
  if (TAB_HOOKS[t]) TAB_HOOKS[t]();
}

// -------------------------------------------------------------- setup form
async function initSetup() {
  S.features = await api("GET", "/api/features");
  const st = await api("GET", "/api/settings");
  const last = st.last_config || {};
  clear($("preset"), Object.entries(S.features.presets).map(([k, v]) => el("option", { value: k, text: v, selected: k === "balanced" })));
  const form = $("feature-form");
  const groups = {};
  S.features.groups.forEach((g, i) => {
    const d = el("details", { class: "group", open: i < 2 ? "" : null }, el("summary", { text: g }));
    groups[g] = d; form.appendChild(d);
  });
  for (const f of S.features.fields) {
    const v = f.key in last ? last[f.key] : f.default;
    groups[f.group].appendChild(fieldRow(f, v));
  }
  if (st.recent_inputs && st.recent_inputs[0]) $("in-dir").value = st.recent_inputs[0];
  if (st.recent_outputs && st.recent_outputs[0]) $("out-dir").value = st.recent_outputs[0];
  fillResultFolders(st.recent_outputs || []);
  $("op-name").value = st.operator || "";
  $("ex-operator").value = st.operator || "";
}
function fieldRow(f, v) {
  const id = "cfg-" + f.key;
  let input;
  if (f.type === "bool") input = el("input", { type: "checkbox", id, checked: !!v });
  else if (f.type.startsWith("choice:")) input = el("select", { id }, f.type.slice(7).split(",").map(o => el("option", { value: o, text: o, selected: o === v })));
  else if (f.type.startsWith("range:")) {
    const [, mn, mx, stp] = f.type.split(":");
    const out = el("span", { class: "small", text: String(v) });
    input = el("span", {}, el("input", { type: "range", id, min: mn, max: mx, step: stp, value: String(v),
      oninput: (e) => { out.textContent = e.target.value; } }), " ", out);
  } else if (f.type === "list") input = el("input", { type: "text", id, value: (v || []).join(", ") });
  else if (f.type === "number") input = el("input", { type: "number", id, value: String(v ?? 0), min: "0" });
  else if (f.type === "folder") input = el("span", { class: "row" }, el("input", { type: "text", id, value: v || "" }),
    el("button", { type: "button", text: "Browse…", onclick: () => openBrowser($(id)) }));
  else input = el("input", { type: "text", id, value: v ?? "" });
  const cost = f.cost && f.cost !== "none" ? el("span", { class: "cost cost-" + f.cost, text: f.cost === "high" ? "slow" : f.cost === "medium" ? "+time" : "fast" }) : null;
  return el("div", { class: "field" }, el("label", { for: id }, f.label, cost), el("div", {}, input, f.help ? el("div", { class: "help", text: f.help }) : null));
}
function collectConfig() {
  const c = { input_dir: $("in-dir").value.trim(), output_dir: $("out-dir").value.trim() };
  for (const f of S.features.fields) {
    const n = $("cfg-" + f.key) || document.querySelector("#cfg-" + f.key);
    if (!n) continue;
    if (f.type === "bool") c[f.key] = n.checked;
    else if (f.type === "list") c[f.key] = n.value.split(",").map(s => s.trim()).filter(Boolean);
    else if (f.type.startsWith("range:") || f.type === "number") c[f.key] = parseFloat(n.value) || 0;
    else c[f.key] = n.value.trim();
  }
  return c;
}
function payload() { return { config: collectConfig(), preset: $("preset").value }; }
function showProblems(e) {
  if (e.data && e.data.problems) toast(e.message + ": " + e.data.problems.join("; "), true); else toast(e.message, true);
}

$("in-browse").addEventListener("click", () => openBrowser($("in-dir")));
$("out-browse").addEventListener("click", () => openBrowser($("out-dir"), true));

$("btn-check").addEventListener("click", async (ev) => {
  busy(ev.target, true);
  try {
    const s = await api("GET", "/api/system?out=" + encodeURIComponent($("out-dir").value.trim()));
    const head = el("p", {}, `${s.os} · Python ${s.python} · ${s.cpu.cores} CPU cores · ${s.ram_gb} GB RAM (${s.ram_free_gb} free) · `,
      s.gpu.available ? "GPU: " + s.gpu.name + (s.gpu.memory_gb ? ` (${s.gpu.memory_gb} GB)` : "") : "no GPU (CPU only)",
      ` · ${s.disk_free_gb} GB free disk`);
    const tbl = el("table", {}, el("tr", {}, el("th", { text: "Check" }), el("th", { text: "Status" }), el("th", { text: "Details" })),
      s.checks.map(c => el("tr", {}, el("td", { text: c.item }), el("td", { class: "st-" + c.level, text: c.ok ? "OK" : c.level.toUpperCase() }), el("td", { text: c.detail }))));
    if (s.gpu.available) { const d = $("cfg-device"); if (d && d.value === "cpu") d.value = "auto"; }
    clear($("check-out"), head, tbl);
  } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});

$("btn-scan").addEventListener("click", async (ev) => {
  busy(ev.target, true);
  try {
    const r = await api("POST", "/api/scan", payload());
    const res = await waitJob(r.job, j => clear($("scan-out"), el("p", { class: "hint", text: `Scanning… ${j.progress.index || 0}/${j.progress.total || "?"}` })));
    if (res.status !== "done") throw new Error(res.error || "scan " + res.status);
    const x = res.result;
    clear($("scan-out"), el("p", {}, el("b", { text: `${x.videos} video(s)` }), ` · ${hms(x.footage_seconds)} of footage · ${x.size_gb} GB · cameras: ${Object.entries(x.cameras).map(([k, v]) => k + " (" + v + ")").join(", ") || "-"}`),
      x.untimed ? el("p", { class: "hint", text: `${x.untimed} file(s) have no time in their name – their time is read from the clock printed on the video.` }) : null,
      el("table", {}, el("tr", {}, ["File", "Length", "Resolution", "Time from name"].map(h => el("th", { text: h }))),
        x.sample.map(s => el("tr", {}, el("td", { class: "mono small", text: s.file }), el("td", { text: clock(s.seconds) }), el("td", { text: s.resolution }), el("td", { text: s.time_in_name || "– (read from video clock)" })))));
  } catch (e) { showProblems(e); } finally { busy(ev.target, false); }
});

$("btn-bench").addEventListener("click", async (ev) => {
  busy(ev.target, true);
  try {
    const r = await api("POST", "/api/benchmark", payload());
    const res = await waitJob(r.job, () => clear($("est-out"), el("p", { class: "hint", text: "Measuring speed on one of your videos…" })));
    if (res.status !== "done") throw new Error(res.error || "benchmark " + res.status);
    const b = res.result.rates;
    clear($("est-out"), el("p", { text: `Measured: motion scan ${b.motion_x_realtime}× real time, object detection ${b.detect_fps} frames/s, clock reading ${b.ocr_s_per_read} s, video encoding ${b.reel_x_realtime}× real time. Click “Estimate time”.` }));
  } catch (e) { showProblems(e); } finally { busy(ev.target, false); }
});

$("btn-estimate").addEventListener("click", async (ev) => {
  busy(ev.target, true);
  try {
    const e = await api("POST", "/api/estimate", payload());
    clear($("est-out"), el("p", {}, el("b", { text: "Estimated time: " + hms(e.total_seconds) }), ` for ${hms(e.footage_seconds)} of footage in ${e.videos} file(s) (projection).`),
      el("table", {}, Object.entries(e.breakdown).map(([k, v]) => el("tr", {}, el("td", { text: k }), el("td", { text: hms(v) })))),
      el("ul", { class: "hint" }, e.assumptions.map(a => el("li", { text: "Assumes " + a }))),
      e.trial_note ? el("p", { class: "st-warning", text: e.trial_note }) : null);
  } catch (e) { if (e.status === 409) toast("Scan the recordings folder first.", true); else showProblems(e); } finally { busy(ev.target, false); }
});

$("btn-start").addEventListener("click", async (ev) => {
  busy(ev.target, true);
  try {
    const r = await api("POST", "/api/jobs", { ...payload(), request_id: rid() });
    S.job = r.job; S.resOut = $("out-dir").value.trim();
    $("start-msg").textContent = "";
    showTab("progress"); pollJob();
  } catch (e) {
    if (e.status === 409 && e.data.job) { S.job = e.data.job; showTab("progress"); pollJob(); toast("An analysis is already running."); }
    else showProblems(e);
  } finally { busy(ev.target, false); }
});

// ------------------------------------------------------------------ progress
async function pollJob() {
  clearTimeout(S.poll);
  if (!S.job) return;
  try {
    const j = (await api("GET", "/api/jobs/" + S.job.id + "?log=120")).job;
    S.job = j;
    $("job-none").classList.add("hidden"); $("job-box").classList.remove("hidden");
    const p = j.progress || {};
    $("job-bar").style.width = (p.percent || 0) + "%";
    const stage = { planning: "Reading recording times", video: "Analysing video", motion: "Scanning for movement",
      event: "Analysing movement", reports: "Building reports and highlight videos", finished: "Finished", discovering: "Finding videos",
      ingest: "Starting", hashing: "Computing SHA-256 of the seized files", remuxing: "Making lossless playback copies",
      "appearance index": "Indexing people's appearance", "scene search index": "Indexing keyframes for describe-it search" }[p.stage] || p.stage || j.status;
    const src = p.sources ? `Source ${p.source_index} of ${p.sources} (${p.source_label}) · ` : "";
    const unit = ["hashing", "remuxing"].includes(p.stage) ? "file" : "video";
    $("job-stage").textContent = `${src}${stage}${p.total ? ` · ${unit} ${p.index || 0} of ${p.total}` : ""}${p.events ? ` · movement ${p.event_index} of ${p.events}` : ""} · ${Math.round(p.percent || 0)}%`;
    $("job-eta").textContent = j.status === "running" && p.eta_seconds ? "about " + hms(p.eta_seconds) + " left" : j.status;
    $("job-file").textContent = p.path || "";
    $("job-log").textContent = (j.log || []).join("\n");
    $("job-log").scrollTop = 1e9;
    $("btn-cancel").disabled = !["running", "queued"].includes(j.status);
    if (["running", "queued"].includes(j.status)) S.poll = setTimeout(pollJob, 1500);
    $("btn-open-results").textContent = j.result && j.result.case_id ? "Open the case timeline" : "Open results";
    if (j.status === "done" && j.result && j.result.case_id) {
      const bad = Object.entries(j.result.sources || {}).filter(([, v]) => v.error || v.skipped);
      toast(bad.length ? `Finished. ${bad.map(([k, v]) => k + ": " + (v.error || v.skipped)).join("; ")}` : "Investigation finished – open the case timeline.", bad.length > 0);
    }
    else if (j.status === "done") {
      const st = (j.result && j.result.stats) || {};
      if (st.videos_failed || st.events_failed || st.aborted)
        toast(`Finished with problems: ${st.videos_failed || 0} video(s) and ${st.events_failed || 0} event(s) failed${st.aborted ? " – stopped early" : ""}. See the log.`, true);
      else toast("Analysis finished.");
    }
    else if (j.status === "failed") toast("Analysis failed: " + j.error, true);
  } catch (e) { S.poll = setTimeout(pollJob, 4000); }
}
$("btn-cancel").addEventListener("click", async () => {
  if (!S.job) return;
  await api("POST", "/api/jobs/" + S.job.id + "/cancel", {});
  toast("Stopping after the current step… finished videos are kept; the rest continues next time.");
});
$("btn-open-results").addEventListener("click", () => {
  if (S.job && S.job.result && S.job.result.case_id) { selectCase(S.job.result.case_id, "investigate"); return; }
  addResultFolder(S.resOut || $("out-dir").value.trim()); showTab("results"); loadResults();
});

// ------------------------------------------------------------------- results
function fillResultFolders(list) {
  clear($("res-out"), list.map(p => el("option", { value: p, text: p })));
  if (list[0]) S.resOut = list[0];
}
function addResultFolder(p) {
  if (!p) return;
  const opts = [...$("res-out").options].map(o => o.value);
  if (!opts.includes(p)) $("res-out").prepend(el("option", { value: p, text: p }));
  $("res-out").value = p; S.resOut = p;
}
$("res-out").addEventListener("change", (e) => { S.resOut = e.target.value; loadResults(); });
$("res-browse").addEventListener("click", () => openBrowser(null, false, (p) => { addResultFolder(p); loadResults(); }));
$("res-load").addEventListener("click", loadResults);
$("res-retime").addEventListener("click", async (ev) => {
  if (!S.resOut) { toast("Choose a results folder.", true); return; }
  busy(ev.target, true);
  try {
    const r = await api("POST", "/api/retime", { out: S.resOut });
    const j = await waitJob(r.job, x => { $("ev-count").textContent = `Fixing recording times… ${Math.round(x.progress.percent || 0)}%`; });
    if (j.status !== "done") throw new Error(j.error || j.status);
    toast(`Times re-read: ${j.result.videos_changed} video(s) and ${j.result.events_changed} event(s) corrected.`);
    loadResults();
  } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});
$("f-go").addEventListener("click", () => { S.offset = 0; loadEvents(); });
$("ev-prev").addEventListener("click", () => { S.offset = Math.max(0, S.offset - S.limit); loadEvents(); });
$("ev-next").addEventListener("click", () => { S.offset += S.limit; loadEvents(); });
$("f-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { S.offset = 0; loadEvents(); } });

async function loadResults() {
  S.resOut = $("res-out").value || S.resOut;
  if (!S.resOut) { toast("Choose a results folder.", true); return; }
  try {
    const s = await api("GET", "/api/results/summary?out=" + encodeURIComponent(S.resOut));
    const k = (v, t) => el("div", { class: "kpi" }, el("b", { text: String(v) }), t);
    clear($("kpis"), k(s.videos, "videos"), k(s.footage_hours + " h", "footage"), k(s.events, "events"), k(s.levels.HIGH, "HIGH"),
      k(s.levels.REVIEW, "REVIEW"), k(s.distinct_plates, "plates"), k(s.face_identities, "people (faces)"),
      k(s.watchlist_alerts, "watchlist alerts"), k(s.highlight_days, "highlight days"),
      s.videos_with_approx_time ? k(s.videos_with_approx_time, "videos with approx. time") : null,
      s.videos_failed ? k(s.videos_failed, "failed videos") : null);
    clear($("f-cam"), el("option", { value: "", text: "all" }), s.cameras.map(c => el("option", { value: c, text: c })));
    if (s.first_day) { $("f-from").value = s.first_day; $("f-to").value = s.last_day; }
    S.offset = 0; loadEvents();
  } catch (e) { toast(e.message, true); clear($("kpis")); clear($("ev-list")); }
}
async function loadEvents() {
  const q = new URLSearchParams({ out: S.resOut, day_from: $("f-from").value, day_to: $("f-to").value, camera: $("f-cam").value,
    levels: [...document.querySelectorAll(".f-lv:checked")].map(x => x.value).join(","), q: $("f-q").value.trim(),
    person: $("f-person").value.trim(), plate: $("f-plate").value.trim(), people: $("f-people").checked ? "1" : "",
    review: $("f-review").value, sort: $("f-sort").value, limit: S.limit, offset: S.offset });
  const n = nextSeq("events");
  try {
    const r = await api("GET", "/api/results/events?" + q);
    if (stale("events", n)) return;
    $("ev-count").textContent = r.total ? `${r.offset + 1}–${Math.min(r.total, r.offset + r.items.length)} of ${r.total} event(s)` : "No events match.";
    $("ev-prev").disabled = r.offset === 0; $("ev-next").disabled = r.offset + r.items.length >= r.total;
    clear($("ev-list"), r.items.map(e => el("div", { class: "ev", onclick: () => openEvent(e.event_id) },
      e.thumb ? el("img", { src: fileUrl(S.resOut, e.thumb), loading: "lazy", alt: "" }) : null,
      el("div", {}, el("span", { class: "lv lv-" + e.level, text: e.level + " " + e.score }), " ", el("b", { text: e.camera || "" })),
      el("div", { class: "small", text: (e.start || "time unknown") + (e.approx_time ? " (approx.)" : "") + " · " + Math.round(e.duration) + " s · " + e.kind }),
      el("div", {}, e.plates.map(p => el("span", { class: "tag", text: "🚗 " + p })), e.identities.map(p => el("span", { class: "tag", text: "👤 " + p })),
        e.flags.map(f => el("span", { class: "tag warn", text: f })), e.review !== "Not reviewed" ? el("span", { class: "tag", text: "✔ " + e.review }) : null))));
  } catch (e) { toast(e.message, true); }
}
async function openEvent(id) {
  try {
    const e = await api("GET", "/api/results/events/" + encodeURIComponent(id) + "?out=" + encodeURIComponent(S.resOut));
    S.detail = e;
    const out = S.resOut;
    const reviewSel = el("select", {}, ["Not reviewed", "OK / normal", "Suspicious", "Theft confirmed", "Reported", "False alarm"].map(s => el("option", { value: s, text: s, selected: e.review && e.review.status === s })));
    const notes = el("textarea", { rows: "2", placeholder: "notes" }); notes.value = (e.review && e.review.notes) || "";
    const d = $("ev-detail"); d.classList.remove("hidden");
    clear(d,
      el("div", { class: "row spread" }, el("h2", { text: e.event_id }), el("button", { text: "✕ Close", onclick: () => d.classList.add("hidden") })),
      el("p", {}, el("span", { class: "lv lv-" + e.level, text: e.level + " " + e.score }), `  ${e.camera || ""} · ${e.start || "time unknown"} → ${(e.end || "").slice(11)} · ${Math.round(e.duration)} s`),
      el("p", { class: "small" }, "Source: ", el("span", { class: "mono", text: e.video }), ` at ${clock(e.offset_start)}–${clock(e.offset_end)} · time from: ${e.time_source || "?"}`),
      e.ai && e.ai.description ? el("p", { class: "card" }, "🤖 ", e.ai.description) : null,
      e.flags.length ? el("ul", {}, e.flags.map(f => el("li", {}, el("b", { text: f.flag }), ` (+${f.points}) ${f.reason}`))) : el("p", { class: "hint", text: "No suspicious signs." }),
      e.clip ? el("video", { controls: "", preload: "metadata", src: fileUrl(out, e.clip) }) : null,
      el("div", { class: "gallery" }, e.snapshots.map(s => el("a", { href: fileUrl(out, s.annotated || s.raw), target: "_blank", rel: "noopener" }, el("img", { src: fileUrl(out, s.annotated || s.raw), title: s.time || "", loading: "lazy" })))),
      e.plates.length ? el("div", {}, el("h3", { text: "Number plates" }), el("div", { class: "grid" }, e.plates.map(p =>
        plateTile(p, (rel) => fileUrl(out, rel), resultsPlateSaver(out, () => openEvent(e.event_id)))))) : null,
      e.tracks.length ? el("div", {}, el("h3", { text: "People & objects" }), el("div", { class: "grid" }, e.tracks.filter(t => t.face || t.person).map(t => el("div", { class: "tile" },
        t.person ? el("img", { src: fileUrl(out, t.person) }) : null, t.face ? el("img", { src: fileUrl(out, t.face) }) : null,
        el("div", { text: `${t.cls} #${t.track} ${t.identity || ""}${t.watchlist ? " WATCHLIST " + t.watchlist : ""}` }))))) : null,
      el("h3", { text: "Review" }), el("div", { class: "row" }, reviewSel, el("button", { class: "primary", text: "Save review", onclick: async () => {
        try { await api("PUT", "/api/results/events/" + encodeURIComponent(e.event_id) + "/review?out=" + encodeURIComponent(out), { status: reviewSel.value, notes: notes.value }); toast("Review saved."); }
        catch (err) { toast(err.message, true); } } }),
        el("button", { text: "Add to evidence", onclick: () => { addToExport(e); } })), notes);
    d.scrollIntoView({ behavior: "smooth" });
  } catch (err) { toast(err.message, true); }
}

// ---------------------------------------------------------------- highlights
async function loadHighlights() {
  if (!S.resOut) { toast("Open a results folder first (Results tab).", true); return; }
  try {
    const r = await api("GET", "/api/results/highlights?out=" + encodeURIComponent(S.resOut));
    S.days = r.days;
    clear($("hl-day"), r.days.map((d, i) => el("option", { value: String(i), text: `${d.day} · ${d.movements} movement(s) · ${clock(d.seconds)}` })));
    if (r.days.length) showDay(0); else { clear($("hl-index"), el("p", { class: "hint", text: "No highlight videos yet." })); $("hl-video").removeAttribute("src"); }
  } catch (e) { toast(e.message, true); }
}
$("hl-day").addEventListener("change", (e) => showDay(parseInt(e.target.value, 10)));
async function showDay(i) {
  let d;
  try { d = await api("GET", "/api/results/highlights/" + encodeURIComponent(S.days[i].day) + "?out=" + encodeURIComponent(S.resOut)); }
  catch (e) { toast(e.message, true); return; }
  const v = $("hl-video");
  v.src = fileUrl(S.resOut, d.reel);
  clear($("hl-index"), d.index.map(s => el("div", { onclick: () => { v.currentTime = s.at + 0.05; v.play(); } },
    el("b", { text: clock(s.at) }), `  ${(s.time || "").slice(11) || "?"}  ${s.camera || ""}  `, el("span", { class: "lv lv-" + s.level, text: s.level }),
    el("span", { class: "small mono", text: `  ${s.video} @ ${clock(s.offset)}` }))));
}

// -------------------------------------------------------------- plates/faces
$("pl-go").addEventListener("click", loadPlates);
$("pl-q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadPlates(); });
$("pl-unread").addEventListener("change", loadPlates);
async function loadPlates() {
  if (!S.resOut) { toast("Open a results folder first (Results tab).", true); return; }
  const n = nextSeq("plates"), out = S.resOut;
  try {
    const q = new URLSearchParams({ out, q: $("pl-q").value, edits: $("pl-edits").value, unread: $("pl-unread").checked ? "1" : "" });
    const r = await api("GET", "/api/results/plates?" + q);
    if (stale("plates", n)) return;
    clear($("pl-list"), r.plates.length ? r.plates.map(p => plateTile(p, (rel) => fileUrl(out, rel), resultsPlateSaver(out, loadPlates),
      [el("button", { type: "button", text: "Open event", onclick: () => { showTab("results"); openEvent(p.event_id); } })])) :
      [el("p", { class: "hint", text: $("pl-unread").checked ? "Every plate was read (by the machine or an examiner)." : "No plates found." })]);
  } catch (e) { toast(e.message, true); }
}
// ------------------------------------------------------------ one plate: what counts, the machine's read, the examiner's
// A machine read below plate_min_confidence is never shown as the plate: the tile says "not read" and shows the
// machine's guess next to it; an examiner types the plate (or marks it not readable) and that reading counts.
function plateTile(p, imgUrl, save, extra) {
  const shown = p.text || (p.reading ? "not readable" : "not read");
  const who = p.reading ? `read by examiner ${p.reading.reviewer || ""} · ${(p.reading.updated || "").slice(0, 16).replace("T", " ")}` +
      (p.machine_text ? ` · machine read ${p.machine_text}` : "") :
    p.text ? `machine read · confidence ${p.confidence}${p.valid_format ? "" : " · unusual format"}` : "";
  const guess = !p.machine_text && p.ocr_text ? `machine guess “${p.ocr_text}” (confidence ${p.confidence}) – too uncertain to report` : "";
  const inp = el("input", { class: "mono plate-in", maxlength: "15", placeholder: p.ocr_text || "type the plate", value: p.reading ? p.reading.text : (p.text || "") });
  const note = el("input", { class: "plate-note", maxlength: "300", placeholder: "how it was read (optional)", value: (p.reading && p.reading.note) || "" });
  const run = async (btn, text) => {
    busy(btn, true);
    try { await save(p, text, note.value.trim()); toast(text === null ? "Reading removed." : text ? `Plate ${text.toUpperCase()} saved.` : "Marked as not readable."); }
    catch (e) { toast(e.message, true); } finally { busy(btn, false); }
  };
  return el("div", { class: "tile plate", "data-plate": `${p.event_id}/${p.track}` },
    p.crop ? el("img", { src: imgUrl(p.crop), alt: "plate" }) : null, p.enhanced ? el("img", { src: imgUrl(p.enhanced), alt: "enhanced plate", title: "enhanced" }) : null,
    el("div", { class: "plate-text" + (p.text ? "" : " unread"), text: shown }),
    who ? el("div", { class: "small", text: who }) : null, guess ? el("div", { class: "small hint plate-guess", text: guess }) : null,
    el("div", { class: "small", text: [p.vehicle, p.camera, p.calibrated_time || p.time, p.watchlist ? "WATCHLIST " + p.watchlist : ""].filter(Boolean).join(" · ") }),
    ...(extra || []),
    el("details", { class: "plate-edit" }, el("summary", { text: p.reading ? "Change the reading" : "Type the plate" }), inp, note,
      el("div", { class: "row" },
        guess ? el("button", { type: "button", text: "Start from the guess", title: "copies the machine's guess into the box – check every character before saving", onclick: () => { inp.value = p.ocr_text; inp.focus(); } }) : null,
        el("button", { type: "button", class: "primary", text: "Save", onclick: (ev) => { const t = inp.value.trim(); if (!t) { toast("Type the plate, or choose “Not readable”.", true); return; } run(ev.target, t); } }),
        el("button", { type: "button", text: "Not readable", onclick: (ev) => run(ev.target, "") }),
        p.reading ? el("button", { type: "button", text: "Remove reading", onclick: (ev) => run(ev.target, null) }) : null)));
}
function resultsPlateSaver(out, after) {
  return async (p, text, note) => {
    await api("PUT", `/api/results/plates/${encodeURIComponent(p.event_id)}/${p.track}?out=${encodeURIComponent(out)}`, { text, note });
    after();
  };
}
async function loadFaces() {
  if (!S.resOut) return;
  try {
    const r = await api("GET", "/api/results/faces?out=" + encodeURIComponent(S.resOut));
    clear($("fa-list"), r.faces.length ? r.faces.map(f => el("div", { class: "tile" + (f.new ? " new" : ""), onclick: () => { showTab("results"); $("f-person").value = f.identity; document.querySelectorAll(".f-lv").forEach(x => x.checked = true); S.offset = 0; loadEvents(); } },
      f.sample ? el("img", { src: fileUrl(S.resOut, f.sample) }) : null, el("b", { text: f.identity }), " ", f.name,
      el("div", { class: "small", text: `${f.events} event(s) · ${f.cameras.join(", ")}` }), el("div", { class: "small", text: `${(f.first || "").slice(0, 16)} → ${(f.last || "").slice(0, 16)}` }))) : [el("p", { class: "hint", text: "No faces yet." })]);
  } catch (e) { toast(e.message, true); }
}

$("fs-go").addEventListener("click", async (ev) => {
  const f = $("fs-photo").files[0];
  if (!f) { toast("Choose a photo first.", true); return; }
  if (!S.resOut) { toast("Open a results folder first (Results tab).", true); return; }
  busy(ev.target, true);
  try {
    const fd = new FormData(); fd.append("photo", f);
    const r = await fetch("/api/results/face-search?out=" + encodeURIComponent(S.resOut), { method: "POST", headers: { "X-CCTV-Token": TOKEN }, body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    clear($("fs-out"), d.matches.length ? d.matches.map(m => el("div", { class: "card" },
      el("div", { class: "row" }, m.sample ? el("img", { src: fileUrl(S.resOut, m.sample), width: "80" }) : null,
        el("b", { text: m.identity }), ` similarity ${m.similarity} · ${m.appearances.length} appearance(s)`),
      el("table", {}, m.appearances.map(a => el("tr", { onclick: () => { showTab("results"); openEvent(a.event_id); } },
        el("td", { text: a.time || "" }), el("td", { text: a.camera || "" }), el("td", { class: "mono small", text: `${a.video} @ ${clock(a.offset)}` })))))) :
      [el("p", { class: "hint", text: "No matching person found." })]);
  } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});

// -------------------------------------------------------------------- export
function exportList() { try { return JSON.parse(localStorage.getItem("cctv-export:" + S.resOut) || "[]"); } catch (_) { return []; } }
function addToExport(e) {
  const l = exportList(); if (!l.find(x => x.id === e.event_id)) l.push({ id: e.event_id, label: `${e.start || ""} ${e.camera || ""} ${e.level}` });
  localStorage.setItem("cctv-export:" + S.resOut, JSON.stringify(l)); toast(`Added – ${l.length} event(s) in the evidence list.`);
}
function renderExport() {
  const l = exportList();
  clear($("ex-list"), l.length ? el("table", {}, l.map(x => el("tr", {}, el("td", { class: "mono", text: x.id }), el("td", { text: x.label }),
    el("td", {}, el("button", { text: "remove", onclick: () => { localStorage.setItem("cctv-export:" + S.resOut, JSON.stringify(exportList().filter(y => y.id !== x.id))); renderExport(); } }))))) :
    el("p", { class: "hint", text: "No events selected yet." }));
}
$("ex-clear").addEventListener("click", () => { localStorage.removeItem("cctv-export:" + S.resOut); renderExport(); });
$("ex-go").addEventListener("click", async (ev) => {
  const ids = exportList().map(x => x.id);
  if (!ids.length) { toast("Add events first (open an event → Add to evidence).", true); return; }
  busy(ev.target, true);
  try {
    const r = await api("POST", "/api/export", { out: S.resOut, event_ids: ids, case_id: $("ex-case").value, operator: $("ex-operator").value,
      notes: $("ex-notes").value, redact: $("ex-redact").checked, redact_plates: $("ex-redact-plates").checked });
    const j = await waitJob(r.job, x => clear($("ex-out"), el("p", { class: "hint", text: `Creating package… ${Math.round(x.progress.percent || 0)}%` })));
    if (j.status !== "done") throw new Error(j.error || "export " + j.status);
    const x = j.result;
    clear($("ex-out"), el("p", {}, "✔ ", el("a", { href: "/api/exports/" + encodeURIComponent(x.name) + "?out=" + encodeURIComponent(S.resOut), text: "Download " + x.name })),
      el("p", { class: "small mono", text: "ZIP SHA-256: " + x.zip_sha256 }), el("p", { class: "small mono", text: "Manifest SHA-256: " + x.manifest_sha256 }),
      el("p", { class: "hint", text: `${x.events} event(s), ${x.files} files${x.redacted ? ", faces blurred" : ""}. Saved in the results folder under exports/.` }));
  } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});
$("audit-load").addEventListener("click", async () => {
  try {
    const a = await api("GET", "/api/audit?out=" + encodeURIComponent(S.resOut));
    clear($("audit-out"), el("p", { class: a.intact ? "st-ok" : "st-error", text: a.intact ? `Audit log intact (${a.entries_checked} entries verified).` : `Audit log has been altered at line ${a.first_broken_line}!` }),
      el("table", {}, a.entries.slice().reverse().map(x => el("tr", {}, el("td", { class: "small", text: x.ts }), el("td", { text: x.action }), el("td", { class: "small mono", text: JSON.stringify(x.details).slice(0, 200) })))));
  } catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------------ settings
async function loadLicence() {
  const l = await api("GET", "/api/license");
  const st = l.license;
  $("licbadge").textContent = st.valid ? `Licensed · ${st.days_left} days left` : (st.state === "TRIAL" || st.state === "NOT_CONFIGURED" ? "Trial" : st.state);
  $("licbadge").className = "badge" + (st.valid ? " ok" : "");
  clear($("lic-out"), el("p", { class: st.valid ? "st-ok" : "st-warning", text: st.reason }),
    el("p", { class: "small" }, "This computer's ID (for a computer-bound licence): ", el("span", { class: "mono", text: l.machine_id })));
}
async function loadSettings() {
  try {
    await loadLicence();
    const s = await api("GET", "/api/settings");
    await loadProviders();
    const m = await api("GET", "/api/models");
    clear($("models-out"), el("table", {}, m.models.map(x => el("tr", {}, el("td", { text: x.name }), el("td", { class: x.installed ? "st-ok" : "st-info", text: x.installed ? "installed" : "not downloaded" }),
      el("td", {}, x.installed ? null : el("button", { text: "Download", onclick: async (ev) => {
        busy(ev.target, true);
        try { const r = await api("POST", "/api/models/download", { id: x.id }); const j = await waitJob(r.job); toast(j.status === "done" ? x.name + " downloaded." : "Download failed: " + j.error, j.status !== "done"); loadSettings(); }
        catch (e) { toast(e.message, true); busy(ev.target, false); } } }))))));
  } catch (e) { toast(e.message, true); }
}
$("lic-go").addEventListener("click", async () => {
  try { const r = await api("POST", "/api/license", { key: $("lic-key").value }); toast(r.license.reason); $("lic-key").value = ""; loadLicence(); }
  catch (e) { toast(e.message, true); }
});
// AI model providers (ordered, fallback): Settings › AI model providers
const AI = { items: [], kinds: [] };
async function loadProviders() {
  const r = await api("GET", "/api/ai/providers");
  AI.items = r.providers.map(x => ({ ...x, api_key: "" })); AI.kinds = r.kinds; AI.nvidia = r.nvidia_base_url;
  renderProviders();
}
function renderProviders() {
  clear($("ai-list"), AI.items.length ? AI.items.map((x, i) => el("div", { class: "source", "data-provider": x.id },
    el("div", { class: "row" }, el("b", { text: `${i + 1}.` }),
      el("label", { class: "inline" }, el("input", { type: "checkbox", checked: x.enabled !== false, onchange: (e) => { x.enabled = e.target.checked; } }), " use"),
      el("select", { class: "ai-kind", onchange: (e) => { x.provider = e.target.value; renderProviders(); } },
        AI.kinds.map(k => el("option", { value: k, text: k, selected: k === x.provider }))),
      el("input", { class: "w-label ai-label", placeholder: "name (optional)", value: x.label || "", oninput: (e) => { x.label = e.target.value; } }),
      el("button", { type: "button", text: "↑", disabled: i === 0, onclick: () => { [AI.items[i - 1], AI.items[i]] = [AI.items[i], AI.items[i - 1]]; renderProviders(); } }),
      el("button", { type: "button", text: "Test", onclick: (ev) => testProvider(x, ev.target) }),
      el("button", { type: "button", class: "danger", text: "Remove", onclick: () => { AI.items.splice(i, 1); renderProviders(); } })),
    el("div", { class: "formgrid" },
      x.provider === "transformers" ? null : el("label", {}, "Server address", el("input", { class: "ai-url", value: x.base_url || "",
        placeholder: x.provider === "nvidia" ? AI.nvidia : "http://127.0.0.1:11434/v1", oninput: (e) => { x.base_url = e.target.value; } })),
      el("label", {}, "Model name", el("input", { class: "ai-model", value: x.model || "",
        placeholder: x.provider === "transformers" ? "Qwen/Qwen3-VL-2B-Instruct" : x.provider === "nvidia" ? "meta/llama-3.2-90b-vision-instruct" : "qwen2.5vl:7b",
        oninput: (e) => { x.model = e.target.value; } })),
      x.provider === "transformers" ? null : el("label", {}, "API key", el("input", { class: "ai-key", type: "password", autocomplete: "off",
        placeholder: x.key_set ? "(saved – type to replace)" : "(if the server needs one)", oninput: (e) => { x.api_key = e.target.value; } }))),
    el("div", { class: "small ai-result" }))) : el("p", { class: "hint", text: "No provider: the built-in model runs on this PC." }));
}
$("ai-add").addEventListener("click", () => { AI.items.push({ id: "", provider: "openai", base_url: "", model: "", enabled: true, api_key: "" }); renderProviders(); });
async function saveProviders() {
  const used = new Set(AI.items.map(x => x.id).filter(Boolean));
  let n = 1;
  for (const x of AI.items) if (!x.id) { while (used.has("provider-" + n)) n++; x.id = "provider-" + n; used.add(x.id); }
  const r = await api("PUT", "/api/ai/providers", { providers: AI.items.map(x => ({ id: x.id, label: x.label, provider: x.provider,
    base_url: x.base_url, model: x.model, enabled: x.enabled !== false, api_key: x.api_key || "" })) });
  AI.items = r.providers.map(x => ({ ...x, api_key: "" })); renderProviders();
}
$("ai-save").addEventListener("click", async (ev) => {
  busy(ev.target, true);
  try { await saveProviders(); clear($("ai-out"), el("p", { class: "st-ok", text: "Saved. New analyses use these providers in this order." })); }
  catch (e) { clear($("ai-out"), el("p", { class: "st-error", text: e.message })); } finally { busy(ev.target, false); }
});
async function testProvider(x, btn) {
  busy(btn, true);
  const card = () => document.querySelector(`[data-provider="${x.id}"] .ai-result`);
  try {
    await saveProviders();
    const out = card(); if (out) out.textContent = "Testing – sending a picture with a number…";
    const r = await api("POST", `/api/ai/providers/${encodeURIComponent(x.id)}/test`, {});
    const c = card();
    if (c) { c.className = "small ai-result " + (r.vision_ok ? "st-ok" : "st-error");
      c.textContent = (r.vision_ok ? `Working: it read the test picture (“${r.answer}”).` : `Not working: ${r.error || r.answer}`) +
        (r.models.length ? `  Models on the server: ${r.models.slice(0, 30).join(", ")}${r.models.length > 30 ? " …" : ""}` : ""); }
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}
$("op-save").addEventListener("click", async () => { try { await api("PUT", "/api/settings", { operator: $("op-name").value.trim() }); $("ex-operator").value = $("op-name").value.trim(); toast("Saved."); } catch (e) { toast(e.message, true); } });

// ------------------------------------------------------------ folder browser
function openBrowser(target, allowCreate, cb) {
  S.fbTarget = target; S.fbCb = cb || null;
  $("fb").classList.remove("hidden");
  $("fb-new").parentElement.querySelector("#fb-mk").disabled = false;
  loadDir((target && target.value.trim()) || "");
}
async function loadDir(p) {
  const n = nextSeq("dir");
  try {
    const r = await api("GET", "/api/fs/list?path=" + encodeURIComponent(p));
    if (stale("dir", n)) return;
    $("fb-path").value = r.path; S.fbParent = r.parent;
    $("fb-info").textContent = r.videos_here ? `${r.videos_here} video file(s) directly in this folder` : "";
    clear($("fb-list"), r.dirs.map(d => el("div", { text: "📁 " + d, ondblclick: () => loadDir(r.path.replace(/[\\/]+$/, "") + (r.path.includes("\\") ? "\\" : "/") + d),
      onclick: () => loadDir(r.path.replace(/[\\/]+$/, "") + (r.path.includes("\\") ? "\\" : "/") + d) })));
    const roots = await api("GET", "/api/fs/roots");
    if (stale("dir", n)) return;
    clear($("fb-roots"), roots.roots.map(x => el("button", { text: x, onclick: () => loadDir(x) })));
    $("fb-native").classList.toggle("hidden", !roots.native_dialog);
  } catch (e) { if (!stale("dir", n)) toast(e.message, true); }
}
$("fb-close").addEventListener("click", () => $("fb").classList.add("hidden"));
$("fb-up").addEventListener("click", () => { if (S.fbParent) loadDir(S.fbParent); });
$("fb-path").addEventListener("keydown", (e) => { if (e.key === "Enter") loadDir(e.target.value); });
$("fb-mk").addEventListener("click", async () => {
  const n = $("fb-new").value.trim(); if (!n) return;
  try { const base = $("fb-path").value.replace(/[\\/]+$/, ""); const r = await api("POST", "/api/fs/mkdir", { path: base + (base.includes("\\") ? "\\" : "/") + n }); $("fb-new").value = ""; loadDir(r.path); }
  catch (e) { toast(e.message, true); }
});
$("fb-ok").addEventListener("click", () => {
  const p = $("fb-path").value; $("fb").classList.add("hidden");
  if (S.fbTarget) S.fbTarget.value = p; if (S.fbCb) S.fbCb(p);
});
// the system folder dialog (on Windows the Windows folder dialog), opened by the app on this PC:
// the chosen folder, or "" when cancelled. Throws (status 501) where no dialog can be shown.
async function systemFolderDialog(title, start) {
  toast("The folder dialog is open – if you do not see it, it is behind the browser window.");
  const r = await api("POST", "/api/fs/pick", { title: title || "Choose a folder", start: start || "" });
  return r.path || "";
}
$("fb-native").addEventListener("click", async () => {
  try {
    const p = await systemFolderDialog("Choose a folder", $("fb-path").value);
    if (p) { $("fb").classList.add("hidden"); if (S.fbTarget) S.fbTarget.value = p; if (S.fbCb) S.fbCb(p); }
  } catch (e) { toast(e.message, true); if (e.status === 501) $("fb-native").classList.add("hidden"); }
});

// ---------------------------------------------------------------------- boot
(async function boot() {
  try {
    await initSetup();
    clear($("f-review"), el("option", { value: "", text: "any" }), ["Not reviewed", "OK / normal", "Suspicious", "Theft confirmed", "Reported", "False alarm"].map(s => el("option", { value: s, text: s })));
    await loadLicence();
    const jobs = (await api("GET", "/api/jobs")).jobs;
    const running = jobs.find(j => j.kind === "analysis" && ["running", "queued"].includes(j.status));
    if (running) { S.job = running; pollJob(); }
  } catch (e) { toast("Could not start: " + e.message, true); }
})();
