"use strict";
// Drishti-AI case screens: Quick investigation, Cases, Timeline & search, Journey, Forensic Studio,
// Court bundle. Uses the helpers of app.js (el, clear, api, toast, waitJob, SEQ, showTab, TAB_HOOKS).
// All data is inserted with textContent (never innerHTML).

const C = { id: "", data: null, timeline: null, points: [], sightTitle: "", studio: null, chain: [], filters: null,
            colors: [], qFolders: [] };
const caseFile = (rel) => rel ? `/api/cases/${encodeURIComponent(C.id)}/file?p=${encodeURIComponent(rel)}` : "";
const caseApi = (path) => `/api/cases/${encodeURIComponent(C.id)}${path}`;
function needCase() { if (!C.id) { toast("Choose or create a case first (Cases).", true); return false; } return true; }
function ts(t) { return t || "time unknown"; }
function dur(s) { s = Math.round(s || 0); return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`; }
function bundleKey() { return "drishti-bundle:" + C.id; }
function bundle() { try { return JSON.parse(localStorage.getItem(bundleKey()) || "{}"); } catch (_) { return {}; } }
function saveBundle(b) { localStorage.setItem(bundleKey(), JSON.stringify(b)); }
function addToBundle(items) {
  const b = bundle(); b.items = b.items || [];
  let n = 0;
  for (const it of items) if (!b.items.find(x => x.source_id === it.source_id && x.event_id === it.event_id)) { b.items.push(it); n++; }
  saveBundle(b); toast(`${n} record(s) added – ${b.items.length} in the court bundle.`);
}

// ------------------------------------------------------------------ cases list / picker
async function loadCases() {
  try {
    const r = await api("GET", "/api/cases");
    $("cs-root").textContent = r.root;
    clear($("case-pick"), el("option", { value: "", text: "–" }),
      r.cases.map(c => el("option", { value: c.case_id, text: c.case_id + (c.fir_number ? " · FIR " + c.fir_number : ""), selected: c.case_id === C.id })));
    clear($("cs-list"), r.cases.length ? el("table", {}, el("tr", {}, ["Case", "FIR", "Police station", "Investigating officer", "Sources", "Created", ""].map(h => el("th", { text: h }))),
      r.cases.map(c => el("tr", {}, el("td", { class: "mono", text: c.case_id }), el("td", { text: c.fir_number }), el("td", { text: c.police_station }),
        el("td", { text: c.investigating_officer }), el("td", { text: String(c.sources) }), el("td", { class: "small", text: (c.created_at || "").slice(0, 16).replace("T", " ") }),
        el("td", {}, el("button", { type: "button", text: "Open", onclick: () => selectCase(c.case_id, "cases") }))))) :
      el("p", { class: "hint", text: "No cases yet – start with “Quick investigation” or create one below." }));
  } catch (e) { toast(e.message, true); }
}
async function selectCase(cid, tab) {
  C.id = cid; C.timeline = null; C.points = []; C.studio = null; C.chain = [];
  localStorage.setItem("drishti-case", cid);
  if (![...$("case-pick").options].some(o => o.value === cid)) $("case-pick").appendChild(el("option", { value: cid, text: cid }));
  $("case-pick").value = cid;
  await loadCase();
  if (tab) showTab(tab);
}
$("case-pick").addEventListener("change", (e) => { if (e.target.value) selectCase(e.target.value, "cases"); });

async function loadCase() {
  if (!C.id) return;
  try { C.data = (await api("GET", caseApi(""))).case; renderCase(); }
  catch (e) { toast(e.message, true); if (e.status === 404) { C.id = ""; localStorage.removeItem("drishti-case"); } }
}

// ------------------------------------------------------------------ case detail
const CASE_FIELDS = [["fir_number", "FIR number"], ["police_station", "Police station"], ["under_sections", "Under sections (BNS)"],
  ["investigating_officer", "Investigating officer"], ["district", "District"], ["state", "State"],
  ["incident_time", "Incident time (YYYY-MM-DD HH:MM)"], ["notes", "Notes"]];
const DEVICE_KINDS = ["DVR", "NVR", "Computer / Storage Media", "Mobile", "Flash Drive", "CD/DVD", "Server", "Cloud", "Other"];
const MEDIA = ["", "HDD", "SSD", "Pen Drive", "SD Card", "CD/DVD", "Cloud export", "Other"];
const DECL = [["name", "Name"], ["relation_name", "Son/daughter/spouse of"], ["address", "Residing/employed at"], ["place", "Place"]];

function renderCase() {
  const c = C.data;
  $("cs-detail").classList.remove("hidden");
  $("cs-title").textContent = `Case ${c.case_id}`;
  clear($("cs-fields"), CASE_FIELDS.map(([k, label]) => el("label", { class: k === "notes" ? "wide" : null }, label,
    el("input", { id: "cf-" + k, value: c[k] || "" }))));
  clear($("cs-sources"), c.sources.length ? c.sources.map(sourceCard) : el("p", { class: "hint", text: "No sources yet – add the seized folders." }));
  if ($("cs-facemode").value === "reference") loadReferences();
}
function sourceCard(s) {
  const st = s.status;
  const label = el("input", { value: s.label, class: "w-label" });
  const off = el("input", { value: s.clock_offset_text, class: "w-off mono", title: "-00:11:00 = the DVR runs 11 minutes fast" });
  const camOff = el("input", { class: "mono", placeholder: "per camera, e.g. ch03=+00:02:00, ch07=-30s",
    value: Object.entries(s.camera_offsets_text || {}).map(([k, v]) => `${k}=${v}`).join(", ") });
  const dev = s.device || {};
  const devIn = {};
  const devGrid = el("div", { class: "formgrid" },
    el("label", {}, "Device type", devIn.kind = el("select", {}, DEVICE_KINDS.map(k => el("option", { value: k, text: k, selected: k === dev.kind })))),
    ...[["make", "Make"], ["model", "Model"], ["serial", "Serial number"], ["color", "Colour"], ["identifier", "IMEI / UIN / UID / MAC / Cloud ID"],
      ["operating_system", "Operating system"], ["custodian", "Custodian (person in lawful control)"], ["notes", "Other relevant information"]]
      .map(([k, lab]) => el("label", {}, lab, devIn[k] = el("input", { value: dev[k] || "" }))),
    el("label", {}, "Storage medium", devIn.storage_medium = el("select", {}, MEDIA.map(k => el("option", { value: k, text: k || "–", selected: k === (dev.storage_medium || "") })))),
    el("div", { class: "wide" }, "The device is ", ["Owned", "Maintained", "Managed", "Operated"].map(k => el("label", { class: "inline" },
      devIn["c_" + k] = el("input", { type: "checkbox", checked: (dev.control || []).includes(k) }), " " + k)), " by the person in charge."));
  const decl = s.declarant || {};
  const declIn = {};
  const declGrid = el("div", { class: "formgrid" }, DECL.map(([k, lab]) => el("label", {}, lab, declIn[k] = el("input", { value: decl[k] || "" }))));
  const loc = s.location || {};
  const locIn = {};
  const locRow = el("div", { class: "row" }, ...[["lat", "Latitude"], ["lon", "Longitude"], ["x", "or plan x"], ["y", "plan y"]].map(([k, lab]) =>
    el("label", { class: "inline" }, lab + " ", locIn[k] = el("input", { value: loc[k] ?? "", size: "9", class: "w-num" }))));
  const save = async (ev) => {
    const cams = {};
    for (const part of camOff.value.split(",").map(x => x.trim()).filter(Boolean)) {
      const i = part.indexOf("="); if (i < 1) { toast(`per-camera correction “${part}”: use camera=offset`, true); return; }
      cams[part.slice(0, i).trim()] = part.slice(i + 1).trim();
    }
    const device = { kind: devIn.kind.value, storage_medium: devIn.storage_medium.value,
      control: ["Owned", "Maintained", "Managed", "Operated"].filter(k => devIn["c_" + k].checked) };
    for (const k of ["make", "model", "serial", "color", "identifier", "operating_system", "custodian", "notes"]) device[k] = devIn[k].value.trim();
    const declarant = {}; for (const [k] of DECL) declarant[k] = declIn[k].value.trim();
    const location = {}; for (const k of ["lat", "lon", "x", "y"]) if (locIn[k].value.trim() !== "") location[k] = locIn[k].value.trim();
    busy(ev.target, true);
    try {
      C.data = (await api("PUT", caseApi("/sources/" + encodeURIComponent(s.source_id)), { label: label.value.trim(), clock_offset: off.value.trim(),
        camera_offsets: cams, device, declarant, location })).case;
      toast(`Saved ${s.label}.`); renderCase();
    } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
  };
  const relink = () => openBrowser(null, false, async (p) => {
    try {
      const r = await api("POST", caseApi("/sources/" + encodeURIComponent(s.source_id) + "/relink"), { path: p });
      toast("Checking every recording against its acquisition SHA-256…");
      const j = await waitJob(r.job);
      if (j.status !== "done") throw new Error(j.error || j.status);
      toast(`${s.label} relinked: ${j.result.files} recording(s) verified (${j.result.verified}).`); loadCase();
    } catch (e) { toast(e.message, true); }
  });
  const remove = async () => {
    if (!confirm(`Remove ${s.label} from the case? Its analysis stays on disk and in the audit log.`)) return;
    try { C.data = (await api("DELETE", caseApi("/sources/" + encodeURIComponent(s.source_id)))).case; renderCase(); } catch (e) { toast(e.message, true); }
  };
  const stTxt = !st.available ? "FOLDER NOT FOUND – plug in the drive or relink" :
    st.analysed ? `${st.files} file(s) · ${st.events} event(s) · ${st.persons} person track(s) · cameras ${st.cameras.join(", ") || "-"}${st.playback_copies ? ` · ${st.playback_copies} playback cop${st.playback_copies > 1 ? "ies" : "y"}` : ""}${st.text_search ? " · describe-it search ✓" : ""}` :
    st.files ? `${st.files} file(s) hashed – not analysed yet` : "not analysed yet";
  return el("div", { class: "source" + (st.available ? "" : " missing"), "data-source": s.source_id },
    el("div", { class: "row" }, el("b", { class: "mono", text: s.source_id }), label, el("label", { class: "inline" }, "Clock correction ", off),
      el("button", { type: "button", class: "primary", text: "Save", onclick: save }),
      el("button", { type: "button", text: "Relink…", title: "The folder is on another drive letter now", onclick: relink }),
      el("button", { type: "button", class: "danger", text: "Remove", onclick: remove })),
    el("div", { class: "small mono", text: s.path }),
    el("div", { class: "small " + (st.available ? "" : "st-error"), text: stTxt }),
    st.merkle_root ? el("div", { class: "small mono", text: "Acquisition Merkle root " + st.merkle_root }) : null,
    el("details", {}, el("summary", { text: "Per-camera corrections, device particulars, Part A declarant, map position" }),
      el("label", {}, "Per-camera corrections", camOff), el("h3", { text: "Device (Section 63 certificate)" }), devGrid,
      el("h3", { text: "Part A – person in charge of this device" }), declGrid, el("h3", { text: "Map position (optional)" }), locRow));
}
$("cs-save").addEventListener("click", async (ev) => {
  if (!needCase()) return;
  const d = {}; for (const [k] of CASE_FIELDS) d[k] = $("cf-" + k).value.trim();
  busy(ev.target, true);
  try { C.data = (await api("PUT", caseApi(""), d)).case; toast("Case details saved."); renderCase(); }
  catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});
$("cs-create").addEventListener("click", async (ev) => {
  busy(ev.target, true);
  try {
    const r = await api("POST", "/api/cases", { case_id: $("cn-id").value.trim(), fir_number: $("cn-fir").value.trim(),
      police_station: $("cn-ps").value.trim(), investigating_officer: $("cn-io").value.trim() });
    $("cs-new").open = false; await loadCases(); await selectCase(r.case.case_id, "cases"); toast(`Case ${r.case.case_id} created.`);
  } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});
async function addSourcePath(p) {
  try { const r = await api("POST", caseApi("/sources"), { path: p }); C.data = r.case; renderCase(); toast("Source added: " + p); }
  catch (e) { toast(e.message, true); }
}
$("cs-add-src").addEventListener("click", () => { if (needCase()) openBrowser(null, false, addSourcePath); });
const csNat = $("cs-native-src");
if (csNat) csNat.addEventListener("click", () => { if (needCase()) pickNativeFolder(addSourcePath); });
$("cs-root-change").addEventListener("click", () => openBrowser(null, true, async (p) => {
  try { await api("PUT", "/api/settings", { cases_root: p }); C.id = ""; localStorage.removeItem("drishti-case"); $("cs-detail").classList.add("hidden"); loadCases(); toast("Cases folder: " + p); }
  catch (e) { toast(e.message, true); }
}));
function runOptions(prefix) {
  return { use_semantic: $(prefix + "-semantic").checked, use_plates: $(prefix + "-plates").checked };
}
// the face mode is chosen before a run and decides all face work of it (server: investigation.FACE_MODES)
$("q-facemode").addEventListener("change", (e) => $("q-refs-row").classList.toggle("hidden", e.target.value !== "reference"));
$("q-refs-pick").addEventListener("click", () => openBrowser($("q-refs"), false));
$("cs-facemode").addEventListener("change", (e) => { $("cs-refs-box").classList.toggle("hidden", e.target.value !== "reference"); if (e.target.value === "reference") loadReferences(); });
async function loadReferences() {
  if (!C.id) return;
  try {
    const r = await api("GET", caseApi("/references"));
    clear($("cs-refs"), r.references.length ? r.references.map(x => el("div", { class: "tile", "data-person": x.name },
      el("img", { src: caseFile(x.thumb), alt: "" }), el("b", { text: x.name }), el("div", { class: "small", text: `${x.photos} photo(s)` }),
      el("button", { type: "button", class: "danger", text: "Remove", onclick: async () => {
        if (!confirm(`Remove ${x.name} from the reference persons? (recorded in the audit log)`)) return;
        try { await api("DELETE", caseApi("/references/" + encodeURIComponent(x.name))); loadReferences(); } catch (e) { toast(e.message, true); } } }))) :
      el("p", { class: "hint", text: "No reference persons yet: add a folder of their photos." }));
  } catch (e) { toast(e.message, true); }
}
$("cs-refs-add").addEventListener("click", () => { if (needCase()) openBrowser(null, false, async (p) => {
  try { const r = await api("POST", caseApi("/references"), { path: p }); toast(`${r.added} photo(s) added – SHA-256 recorded in the audit log.`); loadReferences(); }
  catch (e) { toast(e.message, true); }
}); });
function watchRun(job) { S.job = job; showTab("progress"); pollJob(); }
$("cs-run").addEventListener("click", async (ev) => {
  if (!needCase()) return;
  busy(ev.target, true);
  try { const r = await api("POST", caseApi("/run"), { preset: $("cs-preset").value, options: { ...runOptions("cs"), reprocess: $("cs-reprocess").checked },
    faces: $("cs-facemode").value, request_id: rid() }); watchRun(r.job); }
  catch (e) { if (e.status === 409 && e.data.job) { watchRun(e.data.job); toast("An analysis is already running."); } else toast(e.message, true); }
  finally { busy(ev.target, false); }
});
$("cs-audit").addEventListener("click", async () => {
  if (!needCase()) return;
  try {
    const a = await api("GET", caseApi("/audit"));
    clear($("cs-audit-out"), el("p", { class: a.intact ? "st-ok" : "st-error", text: a.intact ? `Audit log intact (${a.entries_checked} entries verified).` : `Audit log has been altered at line ${a.first_broken_line}!` }),
      el("table", {}, a.entries.slice().reverse().map(x => el("tr", {}, el("td", { class: "small", text: x.ts }), el("td", { text: x.action }),
        el("td", { class: "small mono", text: JSON.stringify(x.details).slice(0, 240) })))));
  } catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------------ folders: picker, drag & drop
async function nativeDialogAvailable() {
  if (C.nativeDialog === undefined) {
    try { C.nativeDialog = (await api("GET", "/api/fs/roots")).native_dialog; } catch (_) { C.nativeDialog = false; }
  }
  return C.nativeDialog;
}
async function pickNativeFolder(cb, title) {
  if (!(await nativeDialogAvailable())) { openBrowser(null, false, cb); return; }
  try {
    const p = await systemFolderDialog(title || "Choose the seized recordings folder");
    if (p) cb(p); else toast("No folder chosen.");
  } catch (e) { toast(e.message, true); openBrowser(null, false, cb); }
}
function promptFolderLocation(name, add, zone) {
  const existing = document.querySelector(".choose-drop");
  if (existing) existing.remove();
  const inp = el("input", { type: "text", class: "drop-path", placeholder: "e.g. D:\\Seized\\" + name });
  const box = el("div", { class: "card choose choose-drop" },
    el("b", { text: `Dropped folder: “${name}”` }),
    el("p", { class: "small", text: "The browser does not tell the app where a dropped folder is, and it was not found by its files near the folders used before. Paste its path, or choose it:" }),
    el("div", { class: "row" }, inp,
      el("button", { type: "button", class: "primary", text: "Add this path", onclick: () => {
        const p = inp.value.trim();
        if (p) { box.remove(); add(p); } else toast("Paste the folder's path first.", true);
      } }),
      el("button", { type: "button", text: "Folder browser…", onclick: () => { box.remove(); openBrowser(null, false, add); } }),
      C.nativeDialog ? el("button", { type: "button", text: "System folder dialog…", onclick: () => { box.remove(); pickNativeFolder(add); } }) : null,
      el("button", { type: "button", text: "Cancel", onclick: () => box.remove() })));
  (zone || $("q-drop")).after(box);
  inp.focus();
}
function readAll(reader) {
  return new Promise((resolve) => { const out = []; const next = () => reader.readEntries(b => { if (!b.length) resolve(out); else { out.push(...b); next(); } }, () => resolve(out)); next(); });
}
async function sampleFiles(dir, max) {
  const out = [], queue = [[dir, 0]];
  while (queue.length && out.length < max) {
    const [d, depth] = queue.shift();
    for (const e of await readAll(d.createReader())) {
      if (e.isFile && out.length < max) {
        const f = await new Promise((res) => e.file(res, () => res(null)));
        if (f) out.push({ rel: e.fullPath.slice(dir.fullPath.length + 1), size: f.size });
      } else if (e.isDirectory && depth < 3) queue.push([e, depth + 1]);
    }
  }
  return out;
}
async function dropped(ev, add) {
  ev.preventDefault();
  const zone = ev.currentTarget; zone.classList.remove("over");
  const items = [...(ev.dataTransfer.items || [])];
  const entries = items.map(i => i.webkitGetAsEntry ? i.webkitGetAsEntry() : null).filter(Boolean);
  if (!entries.length) { toast("Drop folders (not single files) – or use “Choose folder…”.", true); return; }
  for (const ent of entries) {
    if (!ent.isDirectory) { toast(`${ent.name} is a file – drop the folder that holds the recordings.`, true); continue; }
    toast(`Looking for “${ent.name}” on this computer…`);
    const files = await sampleFiles(ent, 8);
    if (!files.length) { promptFolderLocation(ent.name, add, zone); continue; }   // empty: cannot be found by its files
    try {
      const r = await api("POST", "/api/fs/locate", { name: ent.name, files });
      if (r.candidates.length === 1) { add(r.candidates[0]); toast(`Found: ${r.candidates[0]}`); }
      else if (r.candidates.length > 1) choosePath(ent.name, r.candidates, add, zone);
      else promptFolderLocation(ent.name, add, zone);
    } catch (e) { toast(e.message, true); promptFolderLocation(ent.name, add, zone); }
  }
}
function choosePath(name, paths, add, zone) {
  const box = el("div", { class: "card choose" }, el("b", { text: `Several folders called “${name}” hold these files – which one?` }),
    paths.map(p => el("div", {}, el("button", { type: "button", class: "mono", text: p, onclick: () => { box.remove(); add(p); } }))));
  (zone || $("q-drop")).after(box);
}
for (const [zone, add] of [["q-drop", (p) => addQuickFolder(p)], ["cs-detail", (p) => addSourcePath(p)]]) {
  const z = $(zone);
  z.addEventListener("dragover", (e) => { e.preventDefault(); z.classList.add("over"); });
  z.addEventListener("dragleave", () => z.classList.remove("over"));
  z.addEventListener("drop", (e) => dropped(e, add));
}

// ------------------------------------------------------------------ quick investigation
function addQuickFolder(p) {
  if (C.qFolders.some(f => f.path === p)) { toast("Already in the list: " + p); return; }
  const name = p.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  C.qFolders.push({ path: p, label: name.replace(/_/g, " "), clock_offset: "0" }); renderQuick();
}
function renderQuick() {
  const t = $("q-folders"); t.classList.toggle("hidden", !C.qFolders.length);
  clear(t.querySelector("tbody"), C.qFolders.map((f, i) => el("tr", {},
    el("td", {}, el("input", { value: f.label, oninput: (e) => { f.label = e.target.value; } })),
    el("td", { class: "mono small", text: f.path }),
    el("td", {}, el("input", { value: f.clock_offset, class: "mono w-off", oninput: (e) => { f.clock_offset = e.target.value; } })),
    el("td", {}, el("button", { type: "button", text: "✕", title: "remove", onclick: () => { C.qFolders.splice(i, 1); renderQuick(); } })))));
}
$("q-add").addEventListener("click", () => openBrowser(null, false, addQuickFolder));
const qNat = $("q-native");
if (qNat) qNat.addEventListener("click", () => pickNativeFolder(addQuickFolder, "Choose a seized recordings folder"));
$("q-run").addEventListener("click", async (ev) => {
  if (!C.qFolders.length) { toast("Add at least one seized folder.", true); return; }
  busy(ev.target, true); $("q-msg").textContent = "";
  try {
    const r = await api("POST", "/api/cases/quick", { folders: C.qFolders.map(f => ({ path: f.path, label: f.label.trim(), clock_offset: f.clock_offset.trim() || "0" })),
      case_id: $("q-cid").value.trim(), fir_number: $("q-fir").value.trim(), police_station: $("q-ps").value.trim(),
      investigating_officer: $("q-io").value.trim(), preset: document.querySelector("input[name=q-preset]:checked").value,
      options: runOptions("q"), faces: $("q-facemode").value, references: $("q-refs").value.trim() || undefined, request_id: rid() });
    C.qFolders = []; renderQuick();
    await loadCases(); await selectCase(r.case.case_id);
    watchRun(r.job);
  } catch (e) {
    if (e.status === 409 && e.data.job) { watchRun(e.data.job); toast("An analysis is already running."); }
    else { $("q-msg").textContent = e.message; toast(e.message, true); }
  } finally { busy(ev.target, false); }
});

// ------------------------------------------------------------------ timeline
$("tl-go").addEventListener("click", loadTimeline);
$("tl-score").addEventListener("input", (e) => { $("tl-score-v").textContent = e.target.value; });
$("tl-score").addEventListener("change", loadTimeline);
$("tl-kind").addEventListener("change", loadTimeline);
// ticked events of the timeline: "source/event" keys; bulk actions work on them
C.tlTicked = new Set();
const evKey = (e) => `${e.source_id}/${e.event_id}`;
function tickedEvents() { return (C.timeline ? C.timeline.events : []).filter(e => C.tlTicked.has(evKey(e))); }
function showTicked() {
  const n = tickedEvents().length, shown = C.timeline ? C.timeline.events.length : 0;
  $("tl-ticked").textContent = `${n} of ${shown} shown event(s) ticked`;
  $("tl-all").checked = shown > 0 && n === shown;
  for (const id of ["tl-b-bundle", "tl-b-journey", "tl-b-reel"]) $(id).disabled = !n;
}
$("tl-all").addEventListener("change", (e) => {
  for (const ev of C.timeline ? C.timeline.events : []) { if (e.target.checked) C.tlTicked.add(evKey(ev)); else C.tlTicked.delete(evKey(ev)); }
  document.querySelectorAll("#tl-list .ev input.tick").forEach(b => { b.checked = e.target.checked; });
  showTicked();
});
function tickedItems(max) {
  const t = tickedEvents();
  if (t.length > max) { toast(`${t.length} events ticked – at most ${max}: raise the score filter or narrow the dates.`, true); return null; }
  return t.map(e => ({ source_id: e.source_id, event_id: e.event_id }));
}
$("tl-b-bundle").addEventListener("click", () => addToBundle(tickedEvents().map(e => ({ source_id: e.source_id, event_id: e.event_id, label: `${e.source_label}/${e.camera} ${e.calibrated_time || ""}` }))));
$("tl-b-journey").addEventListener("click", async () => {
  const items = tickedItems(200); if (!items) return;
  try { const r = await api("POST", caseApi("/sightings"), { kind: "list", query: { items } });
    C.points = r.points.map(p => ({ ...p, on: true })); C.sightTitle = `${items.length} ticked event(s)`; C.sources = r.sources; showTab("journey"); renderJourney(r); }
  catch (e) { toast(e.message, true); }
});
$("tl-b-reel").addEventListener("click", async (ev) => {
  const items = tickedItems(200); if (!items) return;
  busy(ev.target, true);
  try {
    const r = await api("POST", caseApi("/reel"), { kind: "list", query: { items } });
    const j = await waitJob(r.job, () => { $("tl-ticked").textContent = "making the highlight reel from the original recordings…"; });
    if (j.status !== "done") throw new Error(j.error || j.status);
    toast(`Highlight reel: ${j.result.sightings} event(s) in ${j.result.parts} piece(s), ${dur(j.result.seconds)} – Court bundle › Exports.`);
    showTicked();
  } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});
async function loadTimeline() {
  if (!needCase()) return;
  const q = new URLSearchParams({ day_from: $("tl-from").value, day_to: $("tl-to").value, limit: 2000,
    levels: [...document.querySelectorAll(".tl-lv:checked")].map(x => x.value).join(","), people: $("tl-people").checked ? "1" : "",
    min_score: $("tl-score").value, kinds: $("tl-kind").value });
  const n = nextSeq("timeline");
  try {
    const r = await api("GET", caseApi("/timeline?" + q));
    if (stale("timeline", n)) return;
    C.timeline = r;
    $("tl-count").textContent = !r.events.length ? "No events match." :
      `${r.events.length} event(s) on ${new Set(r.events.map(e => e.source_id)).size} source(s), in calibrated time order` +
      (r.total > r.events.length ? ` – the first ${r.events.length} of ${r.total}: narrow the dates or levels to see the rest` : "");
    renderLanes(r.events, r.sources);
    const shownKeys = new Set(r.events.map(evKey));
    for (const k of [...C.tlTicked]) if (!shownKeys.has(k)) C.tlTicked.delete(k);       // ticks apply to what is shown
    $("tl-bulk").classList.toggle("hidden", !r.events.length);
    showTicked();
    clear($("tl-list"), r.events.map(e => el("div", { class: "ev", "data-event": e.event_id, onclick: () => openCaseEvent(e.source_id, e.event_id) },
      el("input", { type: "checkbox", class: "tick", title: "tick for the bulk actions above", checked: C.tlTicked.has(evKey(e)),
        onclick: (ev) => ev.stopPropagation(), onchange: (ev) => { if (ev.target.checked) C.tlTicked.add(evKey(e)); else C.tlTicked.delete(evKey(e)); showTicked(); } }),
      e.snapshot ? el("img", { src: caseFile(e.snapshot), loading: "lazy", alt: "" }) : null,
      el("div", {}, el("span", { class: "lv lv-" + e.level, text: e.level + " " + Math.round(e.score) }), " ", el("b", { text: `${e.source_label} / ${e.camera || ""}` })),
      el("div", { class: "small" }, el("b", { text: ts(e.calibrated_time) }), ` · ${dur(e.duration)} · ${e.kind}${e.persons ? " · " + e.persons + " person(s)" : ""}`),
      el("div", { class: "small hint", text: `DVR clock ${ts(e.dvr_time)}${e.offset_seconds ? ` (${e.offset_seconds > 0 ? "+" : ""}${e.offset_seconds} s)` : ""}` }))));
  } catch (e) { toast(e.message, true); }
}
function renderLanes(events, sources) {
  const box = $("tl-lanes");
  const timed = events.filter(e => e.calibrated_time);
  if (!timed.length) { clear(box); return; }
  const t = (s) => Date.parse(s.replace(" ", "T"));
  const t0 = Math.min(...timed.map(e => t(e.calibrated_time))), t1 = Math.max(...timed.map(e => t(e.calibrated_end || e.calibrated_time)));
  const span = Math.max(60000, t1 - t0), W = 1000, LH = 26, L = 150;
  const used = sources.filter(s => timed.some(e => e.source_id === s.source_id));
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W + L + 10} ${used.length * LH + 22}`); svg.setAttribute("class", "lanesvg");
  const mk = (tag, attrs, text) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); if (text) n.textContent = text; return n; };
  used.forEach((s, i) => {
    svg.appendChild(mk("rect", { x: L, y: i * LH + 2, width: W, height: LH - 6, class: "lane" }));
    svg.appendChild(mk("text", { x: 4, y: i * LH + 17, class: "lanelabel" }, s.label));
  });
  for (const e of timed) {
    const i = used.findIndex(s => s.source_id === e.source_id);
    const x = L + (t(e.calibrated_time) - t0) / span * W;
    const w = Math.max(4, (e.duration * 1000) / span * W);
    const r = mk("rect", { x, y: i * LH + 4, width: w, height: LH - 10, class: "tick lv-" + e.level, "data-event": e.event_id });
    r.appendChild(mk("title", {}, `${e.source_label}/${e.camera} ${e.calibrated_time} (DVR ${e.dvr_time}) · ${e.kind}`));
    r.addEventListener("click", () => openCaseEvent(e.source_id, e.event_id));
    svg.appendChild(r);
  }
  const fmt = (ms) => { const d = new Date(ms); return [d.getHours(), d.getMinutes(), d.getSeconds()].map(x => String(x).padStart(2, "0")).join(":"); };
  svg.appendChild(mk("text", { x: L, y: used.length * LH + 16, class: "axis" }, fmt(t0)));
  svg.appendChild(mk("text", { x: L + W, y: used.length * LH + 16, class: "axis", "text-anchor": "end" }, fmt(t1)));
  clear(box, svg);
}

// ------------------------------------------------------------------ one event of the case
async function openCaseEvent(sid, eid) {
  let e;
  try { e = (await api("GET", caseApi(`/events/${encodeURIComponent(sid)}/${encodeURIComponent(eid)}`))).event; }
  catch (err) { toast(err.message, true); return; }
  const d = $("ce-detail"); d.classList.remove("hidden");
  const video = el("video", { controls: "", preload: "metadata", src: caseApi(`/video?source=${encodeURIComponent(sid)}&rel=${encodeURIComponent(e.video)}`) });
  video.addEventListener("loadedmetadata", () => { video.currentTime = e.offset_start || 0; }, { once: true });
  video.addEventListener("error", () => { const n = d.querySelector(".novideo"); if (n) n.classList.remove("hidden"); }, { once: true });
  const people = e.tracks.filter(t => t.person || t.face);
  clear(d,
    el("div", { class: "row spread" }, el("h2", { text: `${e.source_label} / ${e.camera || ""} · ${e.event_id}` }), el("button", { type: "button", text: "✕ Close", onclick: () => d.classList.add("hidden") })),
    el("p", {}, el("span", { class: "lv lv-" + e.level, text: e.level + " " + Math.round(e.score) }), " ", el("b", { text: ts(e.calibrated_start) }),
      ` → ${(e.calibrated_end || "").slice(11)} (calibrated) · DVR clock ${ts(e.start)} · ${dur(e.duration)} · ${e.kind}`),
    el("p", { class: "small" }, "Recording ", el("span", { class: "mono", text: e.video }), ` at ${clock(e.offset_start)}–${clock(e.offset_end)} · time from: ${e.time_source || "?"}`),
    video, el("p", { class: "hint novideo hidden", text: "The browser cannot play this recording. Use “Open in Forensic Studio” to see its frames, or open the original in VLC." }),
    el("div", { class: "row" },
      el("button", { type: "button", class: "primary", text: "Open in Forensic Studio at this moment", onclick: () => openStudio(e, video.currentTime || e.offset_start) }),
      el("button", { type: "button", text: "Add to court bundle", onclick: () => addToBundle([{ source_id: sid, event_id: eid, label: `${e.source_label}/${e.camera} ${e.calibrated_start || ""}` }]) })),
    e.flags.length ? el("ul", {}, e.flags.map(f => el("li", {}, el("b", { text: f.flag }), ` (+${f.points}) ${f.reason}`))) : null,
    people.length ? el("div", {}, el("h3", { text: "People and vehicles in this event" }), el("div", { class: "grid" }, people.map(t => el("div", { class: "tile", "data-track": String(t.track) },
      t.person ? el("img", { src: caseFile(t.person), alt: "" }) : null, t.face ? el("img", { src: caseFile(t.face), alt: "" }) : null,
      el("div", { text: `${t.cls} #${t.track}${t.identity ? " · " + t.identity : ""}${t.watchlist ? " · matches reference " + t.watchlist : ""}` }),
      el("div", { class: "row center" }, t.cls === "person" ? [
        el("button", { type: "button", class: "fx-open", text: "Enhance face", title: "Forensic Studio: the head region, super-resolution from several frames", onclick: () => openStudioAt(e, "face", t.track) }),
        el("button", { type: "button", class: "fx-open", text: "Enhance person", title: "Forensic Studio: this person's region", onclick: () => openStudioAt(e, "person", t.track) })] :
        el("button", { type: "button", class: "fx-open", text: `Enhance ${t.cls}`, title: "Forensic Studio: this vehicle's region", onclick: () => openStudioAt(e, "vehicle", t.track) })),
      t.person && t.cls === "person" ? el("button", { type: "button", class: "primary", text: "Find this person on all cameras",
        onclick: () => runSightings({ kind: "appearance", min_score: 0.7, query: { source_id: sid, event_id: eid, track: t.track } }, `Person #${t.track} of ${e.source_label}/${e.camera}`) }) : null,
      t.watchlist ? el("button", { type: "button", class: "primary", text: `Find ${t.watchlist} on all cameras`, title: "reference person: every track whose face matched the reference photos",
        onclick: () => runSightings({ kind: "face", value: t.watchlist }, `Reference person ${t.watchlist}`) }) : null,
      t.identity_id ? el("button", { type: "button", text: `Follow face ${t.identity}`,
        onclick: () => runSightings(/^P\d{4,}$/.test(t.identity) ? { kind: "face", value: t.identity_id, query: { source_id: sid } } : { kind: "face", value: t.identity }, `Face ${t.identity}`) }) : null)))) : null,
    e.plates.length ? el("div", {}, el("h3", { text: "Number plates" }), el("div", { class: "grid" }, e.plates.map(p =>
      plateTile(p, caseFile, casePlateSaver(sid, () => openCaseEvent(sid, eid)),
        [el("button", { type: "button", class: "primary fx-open", text: "Enhance plate in Forensic Studio", onclick: () => openStudioAt(e, "plate", p.track) }),
         p.text ? el("button", { type: "button", text: "Find this vehicle", onclick: () => runSightings({ kind: "plate", value: p.text, fuzzy: true }, `Vehicle ${p.text}`) }) : null])))) : null,
    el("div", { class: "gallery" }, e.snapshots.map(s => el("a", { href: caseFile(s.annotated || s.raw), target: "_blank", rel: "noopener" },
      el("img", { src: caseFile(s.annotated || s.raw), title: s.time || "", loading: "lazy" })))));
  d.scrollIntoView({ behavior: "smooth" });
}

// ------------------------------------------------------------------ searches
$("ts-go").addEventListener("click", textSearch);
$("ts-q").addEventListener("keydown", (e) => { if (e.key === "Enter") textSearch(); });
async function textSearch() {
  if (!needCase()) return;
  const q = $("ts-q").value.trim();
  const n = nextSeq("search");
  clear($("srch-out"), el("p", { class: "hint", text: "Searching every camera… (the first search loads the model, about half a minute)" }));
  try {
    const r = await api("POST", caseApi("/text-search"), { query: q, limit: 40 });
    if (stale("search", n)) return;
    clear($("srch-out"), el("p", { class: "hint", text: `Best matches for “${q}” on ${r.searched_sources.length} source(s), most similar first. Similarity ranks the pictures; it is not proof – look at them.` }),
      el("div", { class: "grid" }, r.results.map(h => el("div", { class: "tile", onclick: () => openCaseEvent(h.source_id, h.event_id) },
        h.thumb ? el("img", { src: caseFile(h.thumb), alt: "", loading: "lazy" }) : null,
        el("b", { text: `${h.source_label} / ${h.camera || ""}` }), el("div", { class: "small", text: ts(h.calibrated_time) }),
        el("div", { class: "small hint", text: `DVR ${ts(h.dvr_time)} · similarity ${h.score}` })))));
  } catch (e) { if (!stale("search", n)) { clear($("srch-out"), el("p", { class: "st-error", text: e.message })); } }
}
$("pp-go").addEventListener("click", async () => {
  if (!needCase()) return;
  const n = nextSeq("search");
  try {
    const r = await api("GET", caseApi(`/people?upper=${encodeURIComponent($("pp-upper").value)}&lower=${encodeURIComponent($("pp-lower").value)}&limit=300`));
    if (stale("search", n)) return;
    clear($("srch-out"), el("p", { class: "hint", text: `${r.total} person track(s) with these colours${r.total > r.people.length ? ` (the first ${r.people.length} shown)` : ""} – colour names are approximate under night lighting.` }),
      el("div", { class: "grid" }, r.people.map(p => el("div", { class: "tile" },
        p.thumb ? el("img", { src: caseFile(p.thumb), alt: "", loading: "lazy", onclick: () => openCaseEvent(p.source_id, p.event_id) }) : null,
        el("b", { text: `${p.source_label} / ${p.camera || ""}` }), el("div", { class: "small", text: ts(p.calibrated_time) }),
        el("div", { class: "small hint", text: `top ${p.upper || "?"} · bottom ${p.lower || "?"}${p.identity ? " · " + p.identity : ""}` }),
        el("button", { type: "button", class: "primary", text: "Find on all cameras",
          onclick: () => runSightings({ kind: "appearance", min_score: 0.7, query: { source_id: p.source_id, event_id: p.event_id, track: p.track } }, `Person in ${p.upper} / ${p.lower}`) })))));
  } catch (e) { toast(e.message, true); }
});
function casePlateSaver(sid, after) {
  return async (p, text, note) => {
    await api("PUT", caseApi(`/plates/${encodeURIComponent(sid)}/${encodeURIComponent(p.event_id)}/${p.track}`), { text, note });
    after();
  };
}
$("pu-go").addEventListener("click", unreadPlates);
async function unreadPlates() {
  if (!needCase()) return;
  const n = nextSeq("search");
  try {
    const r = await api("GET", caseApi("/plates?unread=1&limit=300"));
    if (stale("search", n)) return;
    clear($("srch-out"), el("p", { class: "hint", text: r.plates.length ? `${r.plates.length} vehicle(s) whose plate neither the machine (confidently) nor an examiner has read, in calibrated time order. Type what you can read, or enhance the plate in the Forensic Studio first.` : "Every plate of the case was read (by the machine or an examiner)." }),
      el("div", { class: "grid" }, r.plates.map(p => plateTile(p, caseFile, casePlateSaver(p.source_id, unreadPlates),
        [el("div", { class: "small", text: p.source_label }),
         el("button", { type: "button", text: "Open event", onclick: () => openCaseEvent(p.source_id, p.event_id) }),
         el("button", { type: "button", class: "primary fx-open", text: "Enhance plate", onclick: async () => {
           try { const ev = (await api("GET", caseApi(`/events/${encodeURIComponent(p.source_id)}/${encodeURIComponent(p.event_id)}`))).event; openStudioAt(ev, "plate", p.track); }
           catch (e) { toast(e.message, true); } } })]))));
  } catch (e) { toast(e.message, true); }
}
$("ps-go").addEventListener("click", () => { if (needCase()) runSightings({ kind: "plate", value: $("ps-q").value.trim(), fuzzy: $("ps-fuzzy").checked }, `Vehicle ${$("ps-q").value.trim().toUpperCase()}`); });
$("ps-q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("ps-go").click(); });

// ------------------------------------------------------------------ sightings & journey
async function runSightings(body, title) {
  const n = nextSeq("sight");
  try {
    const r = await api("POST", caseApi("/sightings"), body);
    if (stale("sight", n)) return;
    C.points = r.points.map(p => ({ ...p, on: true })); C.sightTitle = title; C.sources = r.sources;
    $("jr-score").value = "0"; $("jr-score-v").textContent = "0.00";
    showTab("journey"); renderJourney(r);
  } catch (e) { toast(e.message, true); }
}
function ticked() { return C.points.filter(p => p.on).map(p => ({ source_id: p.source_id, event_id: p.event_id, track: p.track })); }
function showJourneyTicked() {
  $("jr-ticked").textContent = `${C.points.filter(p => p.on).length} of ${C.points.length} sighting(s) ticked`;
  document.querySelectorAll("#jr-points tr[data-i]").forEach(tr => {
    const p = C.points[+tr.dataset.i]; tr.classList.toggle("off", !p.on); tr.querySelector("input").checked = p.on;
  });
}
$("jr-score").addEventListener("input", (e) => {
  const min = parseFloat(e.target.value); $("jr-score-v").textContent = min.toFixed(2);
  for (const p of C.points) p.on = p.score === undefined || p.score === null || p.score >= min;   // sightings without a score (exact plate / name) stay
  showJourneyTicked();
});
$("jr-all").addEventListener("click", () => { C.points.forEach(p => { p.on = true; }); showJourneyTicked(); });
$("jr-none").addEventListener("click", () => { C.points.forEach(p => { p.on = false; }); showJourneyTicked(); });
function renderJourney(r) {
  $("jr-title").textContent = C.sightTitle ? `Journey · ${C.sightTitle}` : "Journey";
  $("jr-hint").classList.toggle("hidden", !!C.points.length);
  $("jr-bulk").classList.toggle("hidden", !C.points.length);
  clear($("jr-points"), C.points.length ? el("table", {}, el("tr", {}, ["", "", "Source / camera", "Calibrated time", "DVR clock", "Score", ""].map(h => el("th", { text: h }))),
    C.points.map((p, i) => el("tr", { class: p.on ? "" : "off", "data-i": String(i) },
      el("td", {}, el("input", { type: "checkbox", checked: p.on, onchange: (e) => { p.on = e.target.checked; showJourneyTicked(); } })),
      el("td", {}, p.thumb ? el("img", { src: caseFile(p.thumb), class: "thumb", alt: "" }) : null),
      el("td", { text: `${p.source_label} / ${p.camera || ""}${p.plate ? " · " + p.plate : ""}` }), el("td", {}, el("b", { text: ts(p.calibrated_time) })),
      el("td", { class: "small", text: ts(p.dvr_time) }), el("td", { text: p.score === undefined ? "" : String(p.score) }),
      el("td", {}, el("button", { type: "button", text: "Open", onclick: () => { showTab("investigate"); openCaseEvent(p.source_id, p.event_id); } }))))) :
    el("p", { class: "hint", text: r ? "No sightings found." : "" }));
  if (C.points.length) showJourneyTicked();
  if (!r) return;
  const j = r.journey;
  $("jr-summary").textContent = j.stops.length ? `${j.stops.length} stop(s): ${j.summary}${j.untimed ? ` · ${j.untimed} sighting(s) without a time are not in the route` : ""}` : "";
  clear($("jr-flow"), j.stops.map((s, i) => [
    el("div", { class: "stop" }, el("div", { class: "n", text: String(s.n) }), s.thumb ? el("img", { src: caseFile(s.thumb), alt: "" }) : null,
      el("b", { text: `${s.source_label} / ${s.camera || ""}` }), el("div", { class: "small", text: `${(s.arrival || "").slice(11)} → ${(s.departure || "").slice(11)}` }),
      el("div", { class: "small hint", text: `${s.arrival.slice(0, 10)} · ${s.sightings} sighting(s)` })),
    i < j.stops.length - 1 ? el("div", { class: "arrow", text: `→ ${dur(s.to_next_seconds)}` }) : null]).flat());
  renderMap(j.stops);
}
function renderMap(stops) {
  const box = $("jr-map");
  const geo = stops.filter(s => s.location && s.location.lat !== undefined && s.location.lon !== undefined);
  const plan = stops.filter(s => s.location && s.location.x !== undefined && s.location.y !== undefined);
  const pts = geo.length >= plan.length ? geo.map(s => [s.location.lon, -s.location.lat, s]) : plan.map(s => [s.location.x, s.location.y, s]);
  if (!pts.length) { clear(box, el("p", { class: "hint", text: "Add the map position of each camera (Cases › source › Map position) to see the route on a map. Nothing is fetched from the internet." })); return; }
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const W = 700, Hh = 360, pad = 40;
  const sx = (Math.max(...xs) - Math.min(...xs)) || 1, sy = (Math.max(...ys) - Math.min(...ys)) || 1, sc = Math.min((W - 2 * pad) / sx, (Hh - 2 * pad) / sy);
  const X = (v) => pad + (v - Math.min(...xs)) * sc, Y = (v) => pad + (v - Math.min(...ys)) * sc;
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg"); svg.setAttribute("viewBox", `0 0 ${W} ${Hh}`); svg.setAttribute("class", "mapsvg");
  const mk = (tag, attrs, text) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); if (text !== undefined) n.textContent = text; return n; };
  svg.appendChild(mk("polyline", { points: pts.map(p => `${X(p[0])},${Y(p[1])}`).join(" "), class: "route" }));
  pts.forEach(([x, y, s]) => {
    svg.appendChild(mk("circle", { cx: X(x), cy: Y(y), r: 13, class: "stopdot" }));
    svg.appendChild(mk("text", { x: X(x), y: Y(y) + 5, "text-anchor": "middle", class: "stopn" }, String(s.n)));
    const right = X(x) > W * 0.6;              // labels of points near the right edge go to the left of the dot
    svg.appendChild(mk("text", { x: right ? X(x) - 17 : X(x) + 17, y: Y(y) - 8, class: "stoplabel", "text-anchor": right ? "end" : "start" },
      `${s.source_label} ${(s.arrival || "").slice(11, 16)}`));
  });
  clear(box, el("p", { class: "hint", text: geo.length >= plan.length ? "Route on the cameras' latitude/longitude (north up, not to a road map)." : "Route on the site plan positions." }), svg);
}
$("jr-rebuild").addEventListener("click", async () => {
  const items = ticked(); if (!items.length) { toast("Tick at least one sighting.", true); return; }
  try {
    const r = await api("POST", caseApi("/sightings"), { kind: "list", query: { items } });
    C.points = r.points.map(p => ({ ...p, on: true })); renderJourney(r); toast("Journey rebuilt from the ticked sightings.");
  } catch (e) { toast(e.message, true); }
});
$("jr-reel").addEventListener("click", async (ev) => {
  const items = ticked(); if (!items.length) { toast("Tick at least one sighting.", true); return; }
  busy(ev.target, true);
  try {
    const r = await api("POST", caseApi("/reel"), { kind: "list", query: { items } });
    const j = await waitJob(r.job, () => clear($("jr-reel-out"), el("p", { class: "hint", text: "Making the highlight reel from the original recordings…" })));
    if (j.status !== "done") throw new Error(j.error || j.status);
    const x = j.result;
    clear($("jr-reel-out"), el("h3", { text: `Highlight reel · ${x.sightings} sighting(s) in ${x.parts} piece(s) of footage · ${dur(x.seconds)}` }), el("video", { controls: "", preload: "metadata", src: caseApi("/exports/" + encodeURIComponent(x.name)) }),
      el("p", { class: "small" }, el("a", { href: caseApi("/exports/" + encodeURIComponent(x.name)), download: x.name, text: "Download " + x.name }), el("span", { class: "mono", text: "  SHA-256 " + x.sha256 })),
      el("p", { class: "hint", text: "Every frame carries the source, camera, calibrated time and the DVR's own clock. The reel is a viewing aid; the court bundle holds the original segments." }));
  } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});
$("jr-bundle").addEventListener("click", () => addToBundle(C.points.filter(p => p.on).map(p => ({ source_id: p.source_id, event_id: p.event_id, label: `${p.source_label}/${p.camera} ${p.calibrated_time || ""}` }))));

// ------------------------------------------------------------------ Forensic Studio
// Two zoomable viewers (the original frame, the enhanced picture): the mouse wheel zooms at the
// pointer, dragging moves, the zoom bar sets Fit / 1:1 / 2× … 16×. "Show real pixels" draws every
// pixel as a square (no smoothing) – an 8× view then shows what the recording holds, not the
// browser's interpolation. Coordinates are always in pixels of the picture (natural size).
function makeViewer(box, img) {
  const v = { z: 1, tx: 0, ty: 0, size: "", onchange: null, mode: "pan", onpick: null };
  const apply = () => { img.style.transform = `translate(${v.tx}px, ${v.ty}px) scale(${v.z})`; if (v.onchange) v.onchange(); };
  v.fit = () => {
    const w = img.naturalWidth, h = img.naturalHeight;
    if (!w || !h || !box.clientWidth || !box.clientHeight) return;
    v.z = Math.min(box.clientWidth / w, box.clientHeight / h); v.tx = (box.clientWidth - w * v.z) / 2; v.ty = (box.clientHeight - h * v.z) / 2; apply();
  };
  v.zoomAt = (z, cx, cy) => { z = Math.max(0.02, Math.min(64, z)); v.tx = cx - (cx - v.tx) * z / v.z; v.ty = cy - (cy - v.ty) * z / v.z; v.z = z; apply(); };
  v.zoomTo = (z) => v.zoomAt(z, box.clientWidth / 2, box.clientHeight / 2);
  v.toImage = (clientX, clientY) => { const r = box.getBoundingClientRect(); return [(clientX - r.left - v.tx) / v.z, (clientY - r.top - v.ty) / v.z]; };
  v.toScreen = (x, y) => [v.tx + x * v.z, v.ty + y * v.z];
  v.size2 = () => [img.naturalWidth, img.naturalHeight];
  box.addEventListener("wheel", (e) => { e.preventDefault(); const r = box.getBoundingClientRect(); v.zoomAt(v.z * (e.deltaY < 0 ? 1.25 : 0.8), e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  let drag = null;
  box.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !img.naturalWidth) return;
    box.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, tx: v.tx, ty: v.ty, moved: false };
    if (v.mode === "pan") box.classList.add("panning");
    if (v.mode === "region" && v.onpick) v.onpick("start", v.toImage(e.clientX, e.clientY));
  });
  box.addEventListener("pointermove", (e) => {
    if (!drag) return;
    drag.moved = drag.moved || Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3;
    if (v.mode === "pan" || (v.mode === "point" && drag.moved)) { v.tx = drag.tx + e.clientX - drag.x; v.ty = drag.ty + e.clientY - drag.y; apply(); }
    else if (v.mode === "region" && v.onpick) v.onpick("move", v.toImage(e.clientX, e.clientY));
  });
  const end = (e) => {
    if (!drag) return;
    const d = drag; drag = null; box.classList.remove("panning");
    if (v.mode === "region" && v.onpick) v.onpick("end", v.toImage(e.clientX, e.clientY));
    else if (v.mode === "point" && v.onpick && !d.moved) v.onpick("point", v.toImage(e.clientX, e.clientY));
  };
  box.addEventListener("pointerup", end); box.addEventListener("pointercancel", () => { drag = null; box.classList.remove("panning"); });
  img.addEventListener("load", () => { const s = `${img.naturalWidth}x${img.naturalHeight}`; if (s !== v.size) { v.size = s; v.fit(); } else apply(); });
  new ResizeObserver(() => { if (img.naturalWidth) v.fit(); }).observe(box);
  return v;
}
const V1 = makeViewer($("fx-v1"), $("fx-orig")), V2 = makeViewer($("fx-v2"), $("fx-out"));
function showZoom() { $("fx-zoom").textContent = `original ${Math.round(V1.z * 100)} % · enhanced ${Math.round(V2.z * 100)} %`; }
V1.onchange = () => { drawRoi(); showZoom(); };
V2.onchange = () => { drawCorners(); showZoom(); };
document.querySelectorAll(".zoombar [data-zoom]").forEach(b => b.addEventListener("click", () => {
  for (const v of [V1, V2]) { if (b.dataset.zoom === "fit") v.fit(); else v.zoomTo(parseFloat(b.dataset.zoom)); }
}));
$("fx-pixels").addEventListener("change", (e) => { for (const id of ["fx-v1", "fx-v2"]) $(id).classList.toggle("pixels", e.target.checked); });
for (const id of ["fx-v1", "fx-v2"]) $(id).classList.toggle("pixels", $("fx-pixels").checked);

// the region (plate, face, vehicle): dragged on the original, in pixels of the recording's frame
function placeBox(node, x, y, w, h, v) {
  const [sx, sy] = v.toScreen(x, y);
  node.style.left = sx + "px"; node.style.top = sy + "px"; node.style.width = (w * v.z) + "px"; node.style.height = (h * v.z) + "px";
}
function drawRoi() {
  const r = C.studio && C.studio.roi, box = $("fx-roibox");
  box.classList.toggle("hidden", !r);
  if (r) placeBox(box, r[0], r[1], r[2], r[3], V1);
  $("fx-roi-text").textContent = r ? `x ${r[0]}, y ${r[1]}, ${r[2]} × ${r[3]} px` : "whole frame";
}
let roiStart = null;
V1.onpick = (phase, [x, y]) => {
  const [W, H] = V1.size2();
  x = Math.max(0, Math.min(W, x)); y = Math.max(0, Math.min(H, y));
  if (phase === "start") { roiStart = [x, y]; $("fx-drag").classList.remove("hidden"); }
  const x0 = Math.min(roiStart[0], x), y0 = Math.min(roiStart[1], y), w = Math.abs(x - roiStart[0]), h = Math.abs(y - roiStart[1]);
  placeBox($("fx-drag"), x0, y0, w, h, V1);
  if (phase === "end") {
    $("fx-drag").classList.add("hidden"); V1.mode = "pan"; $("fx-v1").classList.remove("picking");
    if (w < 4 || h < 4) { toast("Drag a larger region (at least 4 × 4 pixels).", true); return; }
    setRoi([Math.round(x0), Math.round(y0), Math.round(w), Math.round(h)]);
  }
};
function setRoi(r) {
  C.studio.roi = r; drawRoi();
  if (!r && $("fx-mode").value === "sr") $("fx-mode").value = "frame";
  modeUi(); preview();
}
$("fx-roi-pick").addEventListener("click", () => {
  if (!C.studio) { toast("Open an event first.", true); return; }
  V1.mode = "region"; $("fx-v1").classList.add("picking"); toast("Drag a rectangle around the plate, face or vehicle on the original.");
});
$("fx-roi-clear").addEventListener("click", () => { if (C.studio) setRoi(null); });

// where the picture comes from: one frame, frames aligned and combined, or super-resolution of the region
const FRAME_CHOICES = { stack: [3, 5, 7, 9, 13, 15], sr: [5, 9, 13, 17, 25] };
function modeUi() {
  const mode = $("fx-mode").value;
  document.querySelectorAll(".fx-multi").forEach(n => n.classList.toggle("hidden", mode === "frame"));
  document.querySelectorAll(".fx-sr").forEach(n => n.classList.toggle("hidden", mode !== "sr"));
  if (mode !== "frame") {
    const keep = parseInt($("fx-n").value, 10), opts = FRAME_CHOICES[mode];
    const pick = opts.includes(keep) ? keep : opts[Math.min(3, opts.length - 1)];
    clear($("fx-n"), opts.map(n => el("option", { value: String(n), text: String(n), selected: n === pick })));
  }
}
$("fx-mode").addEventListener("change", () => {
  if ($("fx-mode").value === "sr" && !(C.studio && C.studio.roi)) toast("Super-resolution works on a region: select the plate or face first.", true);
  modeUi(); preview();
});
for (const id of ["fx-n", "fx-factor", "fx-motion"]) $(id).addEventListener("change", () => preview());

async function loadFilters() {
  if (C.filters) return;
  C.filters = (await api("GET", "/api/enhance/filters")).filters;
  clear($("fx-op"), Object.keys(C.filters).map(k => el("option", { value: k, text: k })));
}
// open the Studio on an event at a time; `focus` = {roi, kind, track} from "Enhance in Forensic Studio"
function openStudio(e, t, focus) {
  C.studio = { source_id: e.source_id, event_id: e.event_id, label: `${e.source_label} / ${e.camera}`, video: e.video,
               fps: e.fps || 25, roi: focus ? focus.roi : null, kind: focus ? focus.kind : "", track: focus ? focus.track : null };
  $("fx-t").value = (Math.round((t || 0) * 1000) / 1000).toFixed(3);
  $("fx-mode").value = focus && (focus.kind === "plate" || focus.kind === "face") ? "sr" : "frame";
  $("fx-prompt").value = focus && focus.kind === "plate" ? $("fx-prompt-preset").options[0].value :
    focus && (focus.kind === "person" || focus.kind === "face") ? $("fx-prompt-preset").options[1].value :
    focus && focus.kind === "vehicle" ? $("fx-prompt-preset").options[2].value : $("fx-prompt").value;
  clear($("fx-read-out")); clear($("fx-ask-out"));
  modeUi(); drawRoi(); showTab("studio"); loadFrame();
}
async function openStudioAt(e, kind, track) {
  try {
    const q = new URLSearchParams({ source: e.source_id, event: e.event_id, track: String(track), kind });
    const r = await api("GET", caseApi("/roi?" + q));
    openStudio(e, r.t, { roi: r.roi, kind, track });
  } catch (err) { toast(err.message + " – opening the event's first frame; select the region yourself.", true); openStudio(e, e.offset_start, { roi: null, kind, track }); }
}
function pendingCorners() { return C.chain.findIndex(s => s.op === "Perspective" && !s.points); }
function cleanChain() {      // while the corners of a Perspective step are picked, the picture shows the steps before it
  const p = pendingCorners();
  return (p >= 0 ? C.chain.slice(0, p) : C.chain).map(({ _pts, ...s }) => s);
}
function studioBody(extra) {
  const s = C.studio, mode = $("fx-mode").value, n = parseInt($("fx-n").value, 10);
  const b = { source_id: s.source_id, event_id: s.event_id, t: parseFloat($("fx-t").value) || 0, chain: cleanChain() };
  if (s.roi) b.roi = s.roi;
  if (mode === "stack") b.stack = { frames: n, method: "median", motion: $("fx-motion").value };
  if (mode === "sr") b.sr = { frames: n, factor: parseInt($("fx-factor").value, 10), motion: $("fx-motion").value };
  return { ...b, ...extra };
}
function loadFrame() {
  if (!C.studio) { toast("Open an event first and choose “Open in Forensic Studio”.", true); return; }
  $("fx-src").textContent = `${C.studio.label} · ${C.studio.video} · event ${C.studio.event_id}`;
  $("fx-orig").src = caseApi(`/frame?source=${encodeURIComponent(C.studio.source_id)}&event=${encodeURIComponent(C.studio.event_id)}&t=${encodeURIComponent($("fx-t").value || "0")}`);
  preview();
}
$("fx-load").addEventListener("click", loadFrame);
$("fx-t").addEventListener("change", loadFrame);
for (const [id, d] of [["fx-prev", -1], ["fx-next", 1]]) $(id).addEventListener("click", () => {
  if (!C.studio) return;
  $("fx-t").value = Math.max(0, (parseFloat($("fx-t").value) || 0) + d / C.studio.fps).toFixed(3); loadFrame();
});
let previewTimer = null;
function preview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    if (!C.studio) return;
    if ($("fx-mode").value === "sr" && !C.studio.roi) { $("fx-log").textContent = "select the region (Select region…) for super-resolution"; return; }
    const n = nextSeq("preview");
    $("fx-log").textContent = "working…";
    try {
      const r = await api("POST", caseApi("/enhance/preview"), studioBody());
      if (stale("preview", n)) return;
      $("fx-out").src = r.image;
      const p = pendingCorners();
      $("fx-log").textContent = r.log + (r.aligned ? ` · ${r.aligned} frame(s) aligned` : "") + ` · ${r.width}×${r.height}` +
        (p >= 0 ? ` · click the four corners of the plate / sign on the enhanced picture (${(C.chain[p]._pts || []).length} of 4)` : "");
    } catch (e) { if (!stale("preview", n)) $("fx-log").textContent = e.message; }
  }, 300);
}
// Perspective: the four corners are clicked on the enhanced picture (it shows the steps before Perspective)
function drawCorners() {
  const v2 = $("fx-v2");
  v2.querySelectorAll(".corner").forEach(n => n.remove());
  const p = pendingCorners();
  if (p < 0) return;
  for (const [x, y] of C.chain[p]._pts || []) {
    const [sx, sy] = V2.toScreen(x, y);
    const d = el("div", { class: "corner" }); d.style.left = sx + "px"; d.style.top = sy + "px"; v2.appendChild(d);
  }
}
V2.onpick = (phase, [x, y]) => {
  const p = pendingCorners();
  if (phase !== "point" || p < 0) return;
  const step = C.chain[p], [W, H] = V2.size2();
  step._pts = (step._pts || []).concat([[Math.round(Math.max(0, Math.min(W, x))), Math.round(Math.max(0, Math.min(H, y)))]]);
  if (step._pts.length === 4) { step.points = step._pts; delete step._pts; V2.mode = "pan"; $("fx-v2").classList.remove("picking"); renderChain(); preview(); }
  else { drawCorners(); $("fx-log").textContent = `corner ${step._pts.length} of 4 at (${step._pts.at(-1).join(", ")}) – click the next`; }
};
function cornersMode() { const on = pendingCorners() >= 0; V2.mode = on ? "point" : "pan"; $("fx-v2").classList.toggle("picking", on); drawCorners(); }
function renderChain() {
  clear($("fx-chain"), C.chain.map((step, i) => {
    const spec = C.filters[step.op];
    return el("div", { class: "fstep" }, el("b", { text: `${i + 1}. ${step.op}` }),
      Object.entries(spec).map(([k, p]) => {
        if (k === "points") return el("span", { class: "small" }, step.points ? ` corners ${step.points.map(q => `(${q[0]},${q[1]})`).join(" ")} ` : " – click the four corners on the enhanced picture ",
          step.points ? el("button", { type: "button", text: "pick again", onclick: () => { step.points = null; delete step._pts; renderChain(); preview(); } }) : null);
        if (p.type === "str") return el("label", { class: "inline" }, " " + k + " ", el("select", { onchange: (e) => { step[k] = e.target.value; preview(); } },
          ["motion", "defocus"].map(o => el("option", { value: o, text: o, selected: (step[k] ?? p.default) === o }))));
        return el("label", { class: "inline" }, " " + k + " ", el("input", { type: "number", class: "w-num", value: String(step[k] ?? p.default), min: String(p.min), max: String(p.max),
          step: p.type === "int" ? "1" : "any", oninput: (e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v)) { step[k] = p.type === "int" ? Math.round(v) : v; preview(); } } }));
      }),
      el("button", { type: "button", text: "↑", title: "earlier", disabled: i === 0, onclick: () => { [C.chain[i - 1], C.chain[i]] = [C.chain[i], C.chain[i - 1]]; renderChain(); preview(); } }),
      el("button", { type: "button", text: "✕", title: "remove", onclick: () => { C.chain.splice(i, 1); renderChain(); preview(); } }));
  }));
  cornersMode();
}
$("fx-add").addEventListener("click", async () => {
  await loadFilters();
  const op = $("fx-op").value; const step = { op };
  if (op === "Perspective") step.points = null;
  C.chain.push(step); renderChain(); preview();
});
$("fx-clear").addEventListener("click", () => { C.chain = []; renderChain(); preview(); });
// presets: starting points tuned on plates / night footage – every value stays editable
const PRESETS = {
  "fx-preset-plate": [{ op: "CLAHE", clip_limit: 2.5, tile_grid: 4 }, { op: "UnsharpMask", sigma: 1.2, amount: 1.5, threshold: 0 }],
  "fx-preset-night": [{ op: "Levels", black: 8, white: 235, gamma: 1.6 }, { op: "CLAHE", clip_limit: 2.0, tile_grid: 8 }, { op: "Denoise", strength: 6 }],
};
for (const [id, steps] of Object.entries(PRESETS)) $(id).addEventListener("click", async () => {
  await loadFilters(); C.chain = steps.map(s => ({ ...s })); renderChain(); preview();
});
$("fx-save").addEventListener("click", async (ev) => {
  if (!C.studio) { toast("Open an event first.", true); return; }
  if (pendingCorners() >= 0) { toast("Click the four corners for Perspective first.", true); return; }
  busy(ev.target, true);
  try {
    const r = await api("POST", caseApi("/enhance/save"), studioBody({ name: $("fx-name").value.trim() }));
    const x = r.enhancement;
    if (x.reproducible) toast(`Saved ${x.name} – decoding the recording again and re-running every step reproduced it bit for bit.`);
    else toast(`Saved ${x.name}, but decoding the recording again did NOT give the same picture – check before using it.`, true);
    const b = bundle(); b.enh = b.enh || []; if (!b.enh.includes(x.name)) b.enh.push(x.name); saveBundle(b);
    loadSaved();
  } catch (e) { toast(e.message, true); } finally { busy(ev.target, false); }
});
// machine reading of the picture: the plate reader and the text reader, with their confidence
$("fx-read").addEventListener("click", async (ev) => {
  if (!C.studio) { toast("Open an event first.", true); return; }
  busy(ev.target, true); clear($("fx-read-out"), "reading…");
  try {
    const r = await api("POST", caseApi("/enhance/read"), studioBody());
    const best = (r.plate_format.find(x => x.fit === 1) || {}).fits_as || r.plate_reader.text || r.text_reader.text || "";
    const parts = [el("span", { text: `plate reader: ${r.plate_reader.text || "–"} (${r.plate_reader.confidence}) · text reader: ${r.text_reader.text || "–"} (${r.text_reader.confidence})` +
      (r.plate_format.length ? ` · Indian format: ${r.plate_format.map(x => `${x.read} → ${x.fits_as || "no fit"}`).join(", ")}` : "") })];
    if (C.studio.kind === "plate" && C.studio.track !== null) {
      const inp = el("input", { class: "mono plate-in", value: best, maxlength: "15" });
      parts.push(el("div", { class: "row" }, "Plate as you read it ", inp, el("button", { type: "button", class: "primary", text: "Save as the plate reading", onclick: async (e2) => {
        busy(e2.target, true);
        try {
          await api("PUT", caseApi(`/plates/${encodeURIComponent(C.studio.source_id)}/${encodeURIComponent(C.studio.event_id)}/${C.studio.track}`),
            { text: inp.value.trim(), note: `read in the Forensic Studio: ${$("fx-log").textContent}`.slice(0, 900) });
          toast(`Plate ${inp.value.trim().toUpperCase()} saved for this vehicle.`);
        } catch (e) { toast(e.message, true); } finally { busy(e2.target, false); }
      } })));
    }
    clear($("fx-read-out"), parts);
  } catch (e) { clear($("fx-read-out"), el("span", { class: "st-error", text: e.message })); } finally { busy(ev.target, false); }
});
$("fx-prompt-preset").addEventListener("change", (e) => { $("fx-prompt").value = e.target.value; });
$("fx-ask").addEventListener("click", async (ev) => {
  if (!C.studio) { toast("Open an event first.", true); return; }
  const prompt = $("fx-prompt").value.trim() || $("fx-prompt-preset").value;
  busy(ev.target, true); clear($("fx-ask-out"), "asking the AI model…");
  try {
    const r = await api("POST", caseApi("/enhance/ask"), studioBody({ prompt }));
    clear($("fx-ask-out"), el("div", { class: "card" }, el("b", { text: "AI answer (advice, not evidence): " }), el("span", { class: "ai-answer", text: r.answer })),
      el("div", { class: "small mono", text: `${r.provider} · picture SHA-256 ${r.picture_sha256.slice(0, 16)}… · kept in the case audit log` }));
  } catch (e) { clear($("fx-ask-out"), el("span", { class: "st-error", text: e.message })); } finally { busy(ev.target, false); }
});
async function loadSaved() {
  if (!C.id) return;
  try {
    const r = await api("GET", caseApi("/enhancements"));
    clear($("fx-saved"), r.enhancements.length ? r.enhancements.slice().reverse().map(x => el("div", { class: "tile" },
      el("a", { href: caseFile(x.file), target: "_blank", rel: "noopener" }, el("img", { src: caseFile(x.file), alt: "", loading: "lazy" })),
      el("b", { text: x.name }), el("div", { class: "small", text: `${x.source.camera} · ${x.source.calibrated_time || ""}` }),
      el("div", { class: "small mono", text: x.log }), el("div", { class: "small mono", text: "SHA-256 " + x.output_sha256.slice(0, 16) + "…" }))) :
      [el("p", { class: "hint", text: "No enhanced stills yet." })]);
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ court bundle
const PARTB = [["name", "Name"], ["relation_name", "Son/daughter/spouse of"], ["address", "Residing/employed at"], ["designation", "Designation"], ["place", "Place"]];
async function renderBundle() {
  if (!C.id || !C.data) { clear($("bd-items"), el("p", { class: "hint", text: "Choose a case first." })); return; }
  const b = bundle(); const items = b.items || [];
  clear($("bd-items"), items.length ? el("table", {}, items.map((x, i) => el("tr", {}, el("td", { class: "mono small", text: `${x.source_id} / ${x.event_id}` }), el("td", { text: x.label || "" }),
    el("td", {}, el("button", { type: "button", text: "remove", onclick: () => { items.splice(i, 1); saveBundle({ ...b, items }); renderBundle(); } }))))) :
    el("p", { class: "hint", text: "No records yet: open an event → “Add to court bundle”, or tick sightings on the Journey." }));
  let enh = [];
  try { enh = (await api("GET", caseApi("/enhancements"))).enhancements; } catch (e) { toast(e.message, true); }
  const chosen = new Set(b.enh || []);
  clear($("bd-enh"), enh.length ? enh.map(x => el("label", { class: "inline enh" }, el("input", { type: "checkbox", checked: chosen.has(x.name),
    onchange: (e) => { const bb = bundle(); bb.enh = (bb.enh || []).filter(n => n !== x.name); if (e.target.checked) bb.enh.push(x.name); saveBundle(bb); } }),
    el("img", { src: caseFile(x.file), alt: "", class: "thumb" }), ` ${x.name} `)) : el("p", { class: "hint", text: "None saved (Forensic Studio)." }));
  const used = new Set(items.map(x => x.source_id).concat(enh.filter(x => chosen.has(x.name)).map(x => x.source.source_id)));
  clear($("bd-parta"), C.data.sources.filter(s => used.has(s.source_id)).map(s => el("div", { class: "source", "data-parta": s.source_id },
    el("div", { class: "row" }, el("b", { text: s.label }), el("span", { class: "small", text: `${s.device.kind}${s.device.make ? " · " + s.device.make : ""}${s.device.serial ? " · S/N " + s.device.serial : ""}` }),
      el("a", { href: caseApi(`/certificate?source=${encodeURIComponent(s.source_id)}`), target: "_blank", rel: "noopener", text: "Preview certificate" })),
    el("div", { class: "formgrid" }, DECL.map(([k, lab]) => el("label", {}, lab, el("input", { "data-k": k, value: (s.declarant || {})[k] || "" })))))));
  if (!used.size) clear($("bd-parta"), el("p", { class: "hint", text: "Add records first – one Part A per device they come from." }));
  const ex = C.data.expert || {};
  clear($("bd-partb"), PARTB.map(([k, lab]) => el("label", {}, lab, el("input", { id: "pb-" + k, value: ex[k] || "" }))));
  loadExports();
}
$("bd-go").addEventListener("click", async (ev) => {
  if (!needCase()) return;
  const b = bundle();
  const part_a = {};
  document.querySelectorAll("#bd-parta [data-parta]").forEach(card => {
    const d = {}; card.querySelectorAll("input[data-k]").forEach(i => { d[i.dataset.k] = i.value.trim(); }); part_a[card.dataset.parta] = d;
  });
  const part_b = {}; for (const [k] of PARTB) part_b[k] = $("pb-" + k).value.trim();
  busy(ev.target, true);
  try {
    const r = await api("POST", caseApi("/bundle"), { items: (b.items || []).map(x => ({ source_id: x.source_id, event_id: x.event_id })),
      enhancements: b.enh || [], part_a, part_b, notes: $("bd-notes").value });
    const j = await waitJob(r.job, x => clear($("bd-out"), el("p", { class: "hint", text: `Cutting original segments, hashing, writing certificates… ${Math.round(x.progress.percent || 0)}%` })));
    if (j.status !== "done") throw new Error(j.error || j.status);
    const x = j.result;
    clear($("bd-out"), el("p", {}, "✔ ", el("a", { href: caseApi("/exports/" + encodeURIComponent(x.name)), download: x.name, text: "Download " + x.name })),
      el("p", { class: "small mono", text: "ZIP SHA-256 " + x.zip_sha256 }),
      el("p", { class: "hint", text: `${x.exhibits} record(s) · ${x.files} files · ${Object.keys(x.certificates).length} certificate(s): ${Object.keys(x.certificates).join(", ")}. Print each certificate, have Part A and Part B signed, and keep the ZIP's SHA-256 with the case diary.` }));
    C.data = (await api("GET", caseApi(""))).case; loadExports();
  } catch (e) { toast(e.message, true); clear($("bd-out")); } finally { busy(ev.target, false); }
});
async function loadExports() {
  try {
    const r = await api("GET", caseApi("/exports"));
    clear($("bd-exports"), r.exports.length ? el("table", {}, el("tr", {}, ["File", "Kind", "Size", "Created", "SHA-256", ""].map(h => el("th", { text: h }))),
      r.exports.map(x => el("tr", {}, el("td", {}, el("a", { href: caseApi("/exports/" + encodeURIComponent(x.name)), download: x.name, text: x.name })),
        el("td", { text: x.kind }), el("td", { text: (x.bytes / 1e6).toFixed(1) + " MB" }), el("td", { class: "small", text: x.created.replace("T", " ") }),
        el("td", { class: "mono small", text: x.sha256 }),
        el("td", {}, x.kind === "bundle" ? el("button", { type: "button", text: "Verify", onclick: async (ev) => {
          try { const v = await api("POST", caseApi(`/exports/${encodeURIComponent(x.name)}/verify`));
            ev.target.replaceWith(el("span", { class: v.ok ? "st-ok" : "st-error", text: v.ok ? `intact · ${v.files} files` : `ALTERED: ${[...v.modified, ...v.missing, ...v.unlisted].join(", ") || "certificate root"}` })); }
          catch (e) { toast(e.message, true); } } }) : null)))) : el("p", { class: "hint", text: "Nothing exported yet." }));
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ tab hooks and boot
TAB_HOOKS.cases = () => { loadCases(); if (C.id) loadCase(); };
TAB_HOOKS.investigate = () => { if (C.id && !C.timeline) loadTimeline(); };
TAB_HOOKS.journey = () => { if (!C.points.length) renderJourney(null); };
TAB_HOOKS.studio = async () => { await loadFilters(); loadSaved(); };
TAB_HOOKS.bundle = async () => { if (C.id && !C.data) await loadCase(); renderBundle(); };
(async function bootCases() {
  try {
    const f = await api("GET", "/api/features");
    C.colors = f.clothing_colors || [];
    for (const id of ["pp-upper", "pp-lower"]) clear($(id), el("option", { value: "", text: "any" }), C.colors.map(c => el("option", { value: c, text: c })));
    const nat = await nativeDialogAvailable();
    const label = /Win/.test(navigator.platform || "") ? "Windows folder dialog…" : "System folder dialog…";
    for (const id of ["q-native", "cs-native-src"]) { const b = $(id); if (b) { b.textContent = label; b.classList.toggle("hidden", !nat); } }
    await loadCases();
    const last = localStorage.getItem("drishti-case");
    if (last && [...$("case-pick").options].some(o => o.value === last)) await selectCase(last);
  } catch (e) { toast("Could not load cases: " + e.message, true); }
})();
