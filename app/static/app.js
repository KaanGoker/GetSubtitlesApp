"use strict";

const TOKEN = document.querySelector('meta[name="gs-token"]').content;
const $ = (id) => document.getElementById(id);

const ACTIVE = ["queued", "preparing", "loading_model", "downloading_model", "transcribing"];
const MODEL_ORDER = ["fast", "balanced", "turbo", "best"];
const MODELS = {   // name, what it is, the Whisper model behind it and its one-time download
  fast: ["Fast", "Shortest time, lower accuracy.", "Whisper small \u00b7 0.5 GB"],
  balanced: ["Balanced", "Longer time, better accuracy.", "Whisper medium \u00b7 1.5 GB"],
  turbo: ["Turbo", "Close to Best, much faster. Cannot translate.", "Whisper large-v3-turbo \u00b7 1.6 GB"],
  best: ["Best", "Highest accuracy. Slowest. On a GPU it needs about 8 GB of graphics memory.", "Whisper large-v2 \u00b7 3.1 GB"],
};
const LINE_LIMIT = { default: { chars: 42, lines: 2 }, vertical: { chars: 38, lines: 1 } };
const LOW_CONFIDENCE = 0.6;
const LANGUAGES = {
  af: "Afrikaans", am: "Amharic", ar: "Arabic", as: "Assamese", az: "Azerbaijani", ba: "Bashkir",
  be: "Belarusian", bg: "Bulgarian", bn: "Bengali", bo: "Tibetan", br: "Breton", bs: "Bosnian",
  ca: "Catalan", cs: "Czech", cy: "Welsh", da: "Danish", de: "German", el: "Greek", en: "English",
  es: "Spanish", et: "Estonian", eu: "Basque", fa: "Persian", fi: "Finnish", fo: "Faroese",
  fr: "French", gl: "Galician", gu: "Gujarati", ha: "Hausa", haw: "Hawaiian", he: "Hebrew",
  hi: "Hindi", hr: "Croatian", ht: "Haitian", hu: "Hungarian", hy: "Armenian", id: "Indonesian",
  is: "Icelandic", it: "Italian", ja: "Japanese", jw: "Javanese", ka: "Georgian", kk: "Kazakh",
  km: "Khmer", kn: "Kannada", ko: "Korean", la: "Latin", lb: "Luxembourgish", ln: "Lingala",
  lo: "Lao", lt: "Lithuanian", lv: "Latvian", mg: "Malagasy", mi: "Maori", mk: "Macedonian",
  ml: "Malayalam", mn: "Mongolian", mr: "Marathi", ms: "Malay", mt: "Maltese", my: "Burmese",
  ne: "Nepali", nl: "Dutch", nn: "Nynorsk", no: "Norwegian", oc: "Occitan", pa: "Punjabi",
  pl: "Polish", ps: "Pashto", pt: "Portuguese", ro: "Romanian", ru: "Russian", sa: "Sanskrit",
  sd: "Sindhi", si: "Sinhala", sk: "Slovak", sl: "Slovenian", sn: "Shona", so: "Somali",
  sq: "Albanian", sr: "Serbian", su: "Sundanese", sv: "Swedish", sw: "Swahili", ta: "Tamil",
  te: "Telugu", tg: "Tajik", th: "Thai", tk: "Turkmen", tl: "Tagalog", tr: "Turkish", tt: "Tatar",
  uk: "Ukrainian", ur: "Urdu", uz: "Uzbek", vi: "Vietnamese", yi: "Yiddish", yo: "Yoruba",
  zh: "Chinese",
};

// ---------- small helpers ----------

function h(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...kids);
  return node;
}

const pad = (n, width = 2) => String(n).padStart(width, "0");

function fmtClock(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const hours = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  return hours ? `${hours}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function fmtTime(seconds) {
  const total = Math.max(0, Math.round(seconds * 1000));
  const hours = Math.floor(total / 3600000), m = Math.floor((total % 3600000) / 60000);
  const s = Math.floor((total % 60000) / 1000), ms = total % 1000;
  return (hours ? `${hours}:${pad(m)}` : `${m}`) + `:${pad(s)}.${pad(ms, 3)}`;
}

/** "1:02:03.456", "2:03,4" or "63.5" to seconds; null when it is not a time. */
function parseTime(text) {
  const parts = text.trim().replace(",", ".").split(":");
  if (!parts.length || parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
  if (parts.slice(0, -1).some((p) => p.includes("."))) return null;
  return parts.reduce((sum, part) => sum * 60 + Number(part), 0);
}

function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

const langName = (code) => LANGUAGES[code] || (code || "unknown").toUpperCase();

/** Break one long line into two balanced lines when both fit the limit. */
function wrapTwo(text, limit) {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= limit) return flat;
  let best = null;
  for (let i = 0; i < flat.length; i++) {
    if (flat[i] !== " ") continue;
    const a = flat.slice(0, i), b = flat.slice(i + 1);
    if (a.length > limit || b.length > limit) continue;
    if (!best || Math.abs(a.length - b.length) < Math.abs(best[0].length - best[1].length)) best = [a, b];
  }
  return best ? best.join("\n") : flat;
}

function store(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ }
}
function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch (e) { return fallback; }
}

// ---------- state ----------

const state = {
  health: null,
  rows: [],          // one per file: its upload and its latest job
  open: null,        // the row shown in the editor
  cues: [],
  focused: null,     // the cue whose text field has the cursor
  dragDepth: 0,
  closed: false,
  saveTimer: null,
  saving: null,
  objectUrl: null,
  unplayable: false,
};
const settings = Object.assign({ language: "auto", task: "transcribe", style: "default", model: "fast" },
                               load("gs.settings", {}));
let nextKey = 1;

function saveRows() {
  store("gs.rows", state.rows.filter((r) => r.uploadId).map((r) => ({
    name: r.name, size: r.size, uploadId: r.uploadId, tracks: r.tracks, track: r.track, jobId: r.jobId,
  })));
}

const isActive = (row) => Boolean(row.job && ACTIVE.includes(row.job.status));
const isDone = (row) => Boolean(row.job && row.job.status === "done");

function rowState(row) {
  if (row.error) return "error";
  if (!row.uploadId) return "uploading";
  return row.job ? row.job.status : "ready";
}

// ---------- server ----------

function setOffline(offline) {
  const banner = $("offline");
  if (banner) banner.hidden = !offline;
}

async function call(method, url, body) {
  const headers = {};
  if (TOKEN) headers["X-Token"] = TOKEN;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    if (!state.closed) setOffline(true);
    throw new Error("The app is not answering.");
  }
  setOffline(false);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 403 && data.error === "wrong token") location.reload();  // the app was restarted
    const error = new Error(data.error || `Error ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function upload(row) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    row.xhr = xhr;
    row.uploadPct = 0;
    row.error = null;
    xhr.open("POST", `/uploads?filename=${encodeURIComponent(row.name)}`);
    if (TOKEN) xhr.setRequestHeader("X-Token", TOKEN);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) row.uploadPct = Math.round((e.loaded / e.total) * 100);
      updateRow(row);
      refresh();
    };
    xhr.onload = () => {
      row.xhr = null;
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* not json */ }
      if (xhr.status === 200) {
        row.uploadId = data.upload_id;
        row.tracks = data.audio_tracks;
        const preferred = row.tracks.findIndex((t) => t.default);
        row.track = Math.max(preferred, 0);
        saveRows();
      } else {
        row.error = data.error || `The file could not be sent (error ${xhr.status}).`;
      }
      updateRow(row);
      refresh();
      resolve();
    };
    xhr.onerror = () => {
      row.xhr = null;
      row.error = "The file could not be sent to the app.";
      updateRow(row);
      refresh();
      resolve();
    };
    xhr.onabort = () => resolve();
    xhr.send(row.file);
  });
}

// ---------- files ----------

function addFiles(fileList) {
  for (const file of fileList) {
    const row = { key: nextKey++, file, name: file.name, size: file.size, uploadId: null, tracks: [],
                  track: 0, uploadPct: 0, error: null, jobId: null, job: null };
    state.rows.push(row);
    const health = state.health;
    if (health && health.demo && file.size > health.max_upload_mb * 1024 * 1024) {
      // the demo refuses it anyway: say so now instead of after sending it
      row.error = `This file is ${fmtSize(file.size)}. The demo takes files up to ${health.max_upload_mb} MB. ` +
        "Send only the audio, or use the desktop app.";
      buildRow(row);
      continue;
    }
    buildRow(row);
    upload(row);
  }
  refresh();
}

function trackLabel(track) {
  const parts = [`Track ${track.index + 1}`, track.language || "language not tagged"];
  if (track.title) parts.push(track.title);
  if (track.default) parts.push("default");
  return parts.join(", ");
}

function buildRow(row) {
  const name = h("div", { class: "row-name", title: row.name }, row.name);
  const meta = h("div", { class: "row-meta" });
  const select = h("select", { "aria-label": `Audio track of ${row.name}`,
                               onchange: () => { row.track = Number(select.value); saveRows(); } });
  const track = h("div", { class: "row-track", hidden: true }, "Audio track", select);
  const edit = h("button", { class: "btn", type: "button", onclick: () => openRow(row) }, "Edit");
  const formats = ["srt", "vtt", "txt"].map((format) => h("button", {
    class: "btn btn-small" + (format === "srt" ? " btn-main" : ""), type: "button",
    title: `Download .${format}`,
    onclick: () => download(`/jobs/${row.jobId}/subtitles?format=${format}`),
  }, `.${format}`));
  const remove = h("button", { class: "btn btn-quiet btn-x", type: "button", "aria-label": `Remove ${row.name}`,
                               title: "Remove", onclick: () => removeRow(row) }, "×");
  const done = h("span", { class: "row-done", hidden: true }, edit, ...formats);
  done.style.display = "contents";
  const bar = h("i");
  const node = h("li", { class: "row" }, name, meta,
                 h("div", { class: "row-actions" }, done, remove), track, h("div", { class: "row-bar" }, bar));
  row.dom = { node, meta, select, track, done, bar, tracksFor: null };
  $("rows").append(node);
  updateRow(row);
}

function metaText(row) {
  const size = fmtSize(row.size);
  const job = row.job;
  switch (rowState(row)) {
    case "error": return row.error;
    case "uploading": return row.uploadPct >= 100 ? `${size} · reading the file…` : `${size} · sending ${row.uploadPct}%`;
    case "ready": return `${size} · ready`;
    case "queued": return job.queue_position ? `waiting in line (${job.queue_position} ahead)` : "starting…";
    case "preparing": return "extracting the audio…";
    case "loading_model": return "loading the model\u2026";
    case "downloading_model":
      return `downloading the model: ${Math.round((job.download_progress || 0) * 100)}%. This happens once per model.`;
    case "transcribing": {
      const left = job.eta_sec != null ? ` · about ${fmtClock(job.eta_sec)} left` : "";
      if (job.progress < 0.01) return "listening\u2026 the first part takes longest";
      return `${Math.floor(job.progress * 100)}%${left}`;
    }
    case "done": {
      let language = langName(job.language);
      if (job.language_auto) language = `Detected: ${language} (${Math.round(job.language_probability * 100)}%)`;
      const parts = [language, MODELS[job.model_choice] ? MODELS[job.model_choice][0] : job.model_choice,
                     job.device === "cuda" ? "GPU" : "CPU", `done in ${fmtClock(job.elapsed_sec)}`];
      if (job.edited) parts.push("edited");
      let text = parts.join(" · ");
      if (job.language_auto && job.language_probability < LOW_CONFIDENCE) {
        text += ". Not sure about the language: if it is wrong, choose it in the settings and create again.";
      }
      return text;
    }
    default: return job && job.error ? job.error : "";
  }
}

function updateRow(row) {
  const dom = row.dom;
  if (!dom) return;
  const status = rowState(row);
  const busy = status === "uploading" || isActive(row);
  dom.node.className = "row" + (busy ? " is-busy" : "") + (status === "done" ? " is-done" : "") +
    (status === "error" ? " is-error" : "") + (state.open === row ? " is-open" : "") +
    (["queued", "preparing", "loading_model"].includes(status) ? " is-waiting" : "");
  const barWidth = { uploading: row.uploadPct, transcribing: row.job && row.job.progress * 100,
                     downloading_model: row.job && (row.job.download_progress || 0) * 100 }[status] || 0;
  dom.meta.textContent = status === "error" ? (row.error || (row.job && row.job.error) || "Failed.") : metaText(row);
  dom.done.hidden = status !== "done";
  dom.bar.style.width = `${barWidth}%`;

  const showTracks = row.tracks.length > 1 && !busy;
  dom.track.hidden = !showTracks;
  if (showTracks && dom.tracksFor !== row.uploadId) {
    dom.select.replaceChildren(...row.tracks.map((t) => h("option", { value: t.index }, trackLabel(t))));
    dom.tracksFor = row.uploadId;
  }
  if (showTracks) dom.select.value = String(row.track);
}

async function removeRow(row) {
  if (state.open === row) await closeEditor();
  if (row.xhr) row.xhr.abort();
  if (row.jobId) call("DELETE", `/jobs/${row.jobId}`).catch(() => {});
  state.rows = state.rows.filter((r) => r !== row);
  row.dom.node.remove();
  saveRows();
  refresh();
}

async function startJob(row, retried = false) {
  row.error = null;
  if (row.jobId) {
    call("DELETE", `/jobs/${row.jobId}`).catch(() => {});
    row.jobId = null;
    row.job = null;
  }
  try {
    const { job_id } = await call("POST", "/jobs", {
      upload_id: row.uploadId, language: settings.language, task: settings.task,
      model_choice: settings.model, style: settings.style, audio_track: row.track,
    });
    row.jobId = job_id;
    row.job = { status: "queued", queue_position: null, progress: 0 };
  } catch (e) {
    if (e.status === 404 && row.file && !retried) {      // the app no longer has the file: send it again
      row.uploadId = null;
      updateRow(row);
      await upload(row);
      if (row.uploadId) return startJob(row, true);
    } else {
      row.error = e.status === 404 ? "This file is no longer in the app. Add it again." : e.message;
    }
  }
  saveRows();
  updateRow(row);
}

async function createAll() {
  const targets = state.rows.filter((r) => r.uploadId && !isActive(r) && rowState(r) !== "uploading");
  if (!targets.length) return;
  if (targets.some((r) => r.job && r.job.edited) &&
      !confirm("This replaces the subtitles you edited. Continue?")) return;
  if (state.open) await closeEditor();
  for (const row of targets) await startJob(row);
  refresh();
  poll();
}

let polling = false;
let pollCount = 0;
async function poll() {
  if (polling || state.closed) return;
  polling = true;
  try {
    if (++pollCount % 7 === 0) {            // every few seconds: is the app still there, and on which device
      try { showDevice(await call("GET", "/health")); } catch (e) { return; }
    }
    for (const row of state.rows) {
      if (!row.jobId || !isActive(row)) continue;
      try {
        row.job = await call("GET", `/jobs/${row.jobId}`);
        if (isDone(row)) showStorage();          // a model may have been downloaded for this job
      } catch (e) {
        if (e.status !== 404) continue;
        row.jobId = null;
        row.job = null;
        row.error = "This job is gone. Create the subtitles again.";
        saveRows();
      }
      updateRow(row);
      if (isDone(row) && state.rows.length === 1 && !state.open) openRow(row);
    }
    refresh();
  } finally {
    polling = false;
  }
}

/** After a page refresh or a cleanup: keep only what the app still has. */
async function validateRows(saved) {
  for (const item of saved) {
    const row = { key: nextKey++, file: null, uploadPct: 100, error: null, job: null, ...item };
    if (row.jobId) {
      try { row.job = await call("GET", `/jobs/${row.jobId}`); } catch (e) { row.jobId = null; }
    }
    let uploadThere = true;
    try { await call("GET", `/uploads/${row.uploadId}`); } catch (e) { uploadThere = false; }
    if (!uploadThere && !row.jobId) continue;
    state.rows.push(row);
    buildRow(row);
  }
  saveRows();
  refresh();
}

// ---------- the screen and its caption ----------

function setCaption(text, animate) {
  const caption = $("caption");
  if (caption.textContent === text) return;
  caption.textContent = text;
  if (animate) {
    caption.classList.remove("swap");
    void caption.offsetWidth;
    caption.classList.add("swap");
  }
}

function setupCaption() {
  if (state.dragDepth > 0) return "Let go.";
  const rows = state.rows;
  const running = rows.find((r) => ["preparing", "loading_model", "downloading_model", "transcribing"].includes(rowState(r)));
  if (running) {
    const status = rowState(running);
    if (status === "preparing") return "Extracting the audio…";
    if (status === "loading_model") return "Loading the model…";
    if (status === "downloading_model") return `Downloading the model\u2026 ${Math.round((running.job.download_progress || 0) * 100)}%`;
    if (running.job.progress < 0.01) return "Listening\u2026";
    return `Listening\u2026 ${Math.floor(running.job.progress * 100)}%`;
  }
  const queued = rows.find((r) => rowState(r) === "queued");
  if (queued && state.health && state.health.demo && queued.job.queue_position) {
    const ahead = queued.job.queue_position;
    return `Waiting in line.\n${ahead} ${ahead === 1 ? "file is" : "files are"} ahead of yours.`;
  }
  if (queued) return "Starting…";
  const sending = rows.find((r) => rowState(r) === "uploading");
  if (sending) return `Taking in ${sending.name}…`;
  if (rows.some((r) => rowState(r) === "ready")) return "Ready.\nChoose the settings, then create.";
  const done = rows.filter(isDone);
  if (done.length) return done.length === 1 ? "Done.\nOpen the file to check the subtitles." : `Done. ${done.length} files have subtitles.`;
  return "Drop a video or audio file here,\nor click to choose one.";
}

function refresh() {
  if (state.closed) return;
  const startable = state.rows.filter((r) => r.uploadId && !isActive(r) && rowState(r) !== "uploading");
  const create = $("create");
  create.disabled = !startable.length;
  create.textContent = startable.length && startable.every(isDone) ? "Create again with these settings" : "Create subtitles";
  $("rows-empty").hidden = state.rows.length > 0;
  $("zip").hidden = state.rows.filter(isDone).length < 2;
  $("turbo-note").hidden = !(settings.model === "turbo" && settings.task === "translate");
  if (!state.open) setCaption(setupCaption(), true);
}

// ---------- editor ----------

const player = $("player");

function cueAt(time) {
  return state.cues.find((c) => time >= c.start && time < c.end) || null;
}

function limitOf(row) {
  return LINE_LIMIT[(row.job && row.job.style) || "default"] || LINE_LIMIT.default;
}

async function openRow(row) {
  if (state.open === row) return;
  await flushSave();
  let cues;
  try {
    cues = await call("GET", `/jobs/${row.jobId}/cues`);
  } catch (e) {
    row.error = e.message;
    updateRow(row);
    return;
  }
  const previous = state.open;
  state.open = row;
  state.cues = cues;
  state.focused = null;
  if (previous) updateRow(previous);
  updateRow(row);

  $("settings").hidden = true;
  $("editor").hidden = false;
  $("editor-title").textContent = row.name;
  $("save-state").textContent = "";
  $("tool-note").textContent = "";
  $("drop").hidden = true;
  $("screen").classList.add("is-editing");
  $("caption").setAttribute("aria-live", "off");   // a screen reader should not read every line while it plays

  state.unplayable = false;
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  state.objectUrl = row.file ? URL.createObjectURL(row.file) : null;
  $("screen").classList.remove("has-video");
  $("screen").style.aspectRatio = "";
  $("screen").style.maxWidth = "";
  player.hidden = false;
  player.src = state.objectUrl || `/uploads/${row.uploadId}/media`;
  renderCues();
  tick();
}

async function closeEditor() {
  await flushSave();
  const row = state.open;
  state.open = null;
  state.cues = [];
  player.pause();
  player.removeAttribute("src");
  player.load();
  player.hidden = true;
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  state.objectUrl = null;
  $("screen").classList.remove("has-video");
  $("screen").style.aspectRatio = "";
  $("screen").style.maxWidth = "";
  $("transport").hidden = true;
  $("screen").classList.remove("is-editing");
  $("caption").setAttribute("aria-live", "polite");
  $("drop").hidden = false;
  $("editor").hidden = true;
  $("settings").hidden = false;
  if (row) updateRow(row);
  refresh();
}

function markDirty() {
  $("save-state").textContent = "Saving…";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => { state.saveTimer = null; save(); }, 500);
}

function save() {
  const row = state.open;
  if (!row) return Promise.resolve();
  const body = state.cues.map((c) => ({ start: c.start, end: c.end, text: c.text }));
  state.saving = call("PUT", `/jobs/${row.jobId}/cues`, body).then(() => {
    row.job.edited = true;
    updateRow(row);
    if (!state.saveTimer) $("save-state").textContent = "Saved";
  }).catch((e) => {
    $("save-state").textContent = `Not saved: ${e.message}`;
  });
  return state.saving;
}

async function flushSave() {
  if (state.saveTimer) {
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
    await save();
  } else if (state.saving) {
    await state.saving;
  }
}

async function download(url) {
  await flushSave();
  const link = h("a", { href: url, download: "" });
  document.body.append(link);
  link.click();
  link.remove();
}

function autosize(area) {
  area.style.height = "auto";
  area.style.height = `${area.scrollHeight + 2}px`;
}

function showCount(cue) {
  const limit = limitOf(state.open);
  const lines = cue.text.split("\n");
  const longest = Math.max(...lines.map((l) => l.length));
  cue.dom.count.textContent = `${longest}/${limit.chars}`;
  cue.dom.count.classList.toggle("is-over", longest > limit.chars || lines.length > limit.lines);
  cue.dom.count.title = `Longest line: ${longest} characters. Advice: at most ${limit.chars} per line, ${limit.lines} line${limit.lines > 1 ? "s" : ""}.`;
}

function renderCues(focusIndex = -1) {
  const list = $("cues");
  const scroll = list.scrollTop;
  list.replaceChildren(...state.cues.map((cue, index) => buildCue(cue, index)));
  for (const cue of state.cues) autosize(cue.dom.area);
  list.scrollTop = scroll;
  if (focusIndex >= 0 && state.cues[focusIndex]) state.cues[focusIndex].dom.area.focus();
}

function buildCue(cue, index) {
  const timeInput = (key, label) => {
    const input = h("input", { type: "text", value: fmtTime(cue[key]), "aria-label": `${label} of line ${index + 1}`, spellcheck: "false" });
    input.addEventListener("change", () => {
      const value = parseTime(input.value);
      const start = key === "start" ? value : cue.start, end = key === "end" ? value : cue.end;
      if (value == null || end <= start) {
        input.classList.add("is-bad");
        return;
      }
      input.classList.remove("is-bad");
      cue[key] = value;
      input.value = fmtTime(value);
      markDirty();
      if (key === "start") {
        state.cues.sort((a, b) => a.start - b.start);
        renderCues();
      }
      tick();
    });
    return input;
  };

  const area = h("textarea", { rows: "1", "aria-label": `Text of line ${index + 1}` });
  area.value = cue.text;
  area.addEventListener("input", () => {
    cue.text = area.value;
    autosize(area);
    showCount(cue);
    markDirty();
    tick();
  });
  area.addEventListener("focus", () => {
    state.focused = cue;
    if (player.paused && !state.unplayable && Number.isFinite(player.duration)) player.currentTime = cue.start + 0.01;
    tick();
  });
  area.addEventListener("blur", () => { if (state.focused === cue) state.focused = null; });

  const count = h("span", { class: "cue-count" });
  const tools = h("span", { class: "cue-tools" },
    h("button", { type: "button", title: "Split this line at the cursor", onclick: () => splitCue(index) }, "Split"),
    index < state.cues.length - 1
      ? h("button", { type: "button", title: "Join with the next line", onclick: () => mergeCue(index) }, "Join") : "",
    h("button", { type: "button", title: "Delete this line", onclick: () => deleteCue(index) }, "Delete"));
  const number = h("button", { class: "cue-n", type: "button", title: "Play from here",
                               onclick: () => playFrom(cue) }, String(index + 1));
  const node = h("li", { class: "cue" }, number,
    h("div", { class: "cue-times" }, timeInput("start", "Start"), "›", timeInput("end", "End"), tools, count),
    area);
  cue.dom = { node, area, count };
  showCount(cue);
  return node;
}

function playFrom(cue) {
  if (state.unplayable) return;
  player.currentTime = cue.start;
  player.play();
}

function splitCue(index) {
  const cue = state.cues[index];
  const area = cue.dom.area;
  let at = document.activeElement === area ? area.selectionStart : -1;
  if (at <= 0 || at >= cue.text.length) {
    const middle = Math.floor(cue.text.length / 2);
    const spaces = [...cue.text].map((ch, i) => (/\s/.test(ch) ? i : -1)).filter((i) => i > 0);
    if (!spaces.length) return;
    at = spaces.reduce((best, i) => (Math.abs(i - middle) < Math.abs(best - middle) ? i : best));
  }
  const first = cue.text.slice(0, at).trim(), second = cue.text.slice(at).trim();
  if (!first || !second) return;
  const cut = cue.start + (cue.end - cue.start) * (first.length / (first.length + second.length));
  state.cues.splice(index, 1, { start: cue.start, end: cut, text: first }, { start: cut, end: cue.end, text: second });
  renderCues(index + 1);
  markDirty();
}

function mergeCue(index) {
  const cue = state.cues[index], next = state.cues[index + 1];
  if (!next) return;
  const limit = limitOf(state.open);
  const joined = `${cue.text} ${next.text}`.replace(/\s+/g, " ").trim();
  const text = limit.lines > 1 ? wrapTwo(joined, limit.chars) : joined;
  state.cues.splice(index, 2, { start: cue.start, end: next.end, text });
  renderCues(index);
  markDirty();
}

function deleteCue(index) {
  state.cues.splice(index, 1);
  renderCues(Math.min(index, state.cues.length - 1));
  markDirty();
}

function replaceAll() {
  const find = $("find").value;
  if (!find) return;
  let count = 0;
  for (const cue of state.cues) {
    const pieces = cue.text.split(find);
    if (pieces.length > 1) {
      count += pieces.length - 1;
      cue.text = pieces.join($("replace").value);
    }
  }
  $("tool-note").textContent = count ? `Replaced ${count}.` : "Not found.";
  if (count) {
    renderCues();
    markDirty();
  }
}

function shiftAll() {
  const by = Number($("shift").value);
  if (!Number.isFinite(by) || by === 0) return;
  for (const cue of state.cues) {
    const length = cue.end - cue.start;
    cue.start = Math.max(0, cue.start + by);
    cue.end = cue.start + length;
  }
  $("tool-note").textContent = `Moved every line by ${by} s.`;
  renderCues();
  markDirty();
  tick();
}

async function resetEdits() {
  const row = state.open;
  if (!row || !confirm("Undo every edit in this file?")) return;
  clearTimeout(state.saveTimer);
  state.saveTimer = null;
  await call("DELETE", `/jobs/${row.jobId}/cues`);
  state.cues = await call("GET", `/jobs/${row.jobId}/cues`);
  row.job.edited = false;
  updateRow(row);
  $("save-state").textContent = "";
  $("tool-note").textContent = "Edits undone.";
  renderCues();
  tick();
}

// ---------- player ----------

let seeking = false;
let lastNow = null;

function tick() {
  if (!state.open) return;
  const time = player.currentTime || 0;
  const duration = Number.isFinite(player.duration) ? player.duration : 0;
  $("clock").textContent = `${fmtClock(time)} / ${fmtClock(duration)}`;
  if (!seeking && duration) $("seek").value = String(Math.round((time / duration) * 1000));

  const now = cueAt(time);
  const shown = !player.paused ? now : (state.focused || now);
  if (state.unplayable) {
    setCaption(state.focused ? state.focused.text : "This file cannot be played here.\nThe text can still be edited.", false);
  } else {
    setCaption(shown ? shown.text : "", false);
  }
  if (now !== lastNow) {
    if (lastNow && lastNow.dom) lastNow.dom.node.classList.remove("is-now");
    if (now && now.dom) {
      now.dom.node.classList.add("is-now");
      if (!player.paused && !$("cues").contains(document.activeElement)) {
        now.dom.node.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }
    lastNow = now;
  }
}

function frameLoop() {
  tick();
  if (!player.paused && state.open) requestAnimationFrame(frameLoop);
}

function togglePlay() {
  if (!state.open || state.unplayable) return;
  if (player.paused) player.play(); else player.pause();
}

player.addEventListener("loadedmetadata", () => {
  $("transport").hidden = false;
  const isVideo = player.videoWidth > 0;
  const screen = $("screen");
  screen.classList.toggle("has-video", isVideo);
  screen.style.aspectRatio = isVideo ? `${player.videoWidth} / ${player.videoHeight}` : "";
  // A tall video gets a narrow screen, so it is not a small picture between black bars.
  screen.style.maxWidth = isVideo ? `calc(68vh * ${player.videoWidth / player.videoHeight})` : "";
  tick();
});
player.addEventListener("error", () => {
  if (!state.open) return;
  state.unplayable = true;
  player.hidden = true;
  $("transport").hidden = true;
  tick();
});
player.addEventListener("play", () => { $("play").classList.add("is-playing"); $("play").setAttribute("aria-label", "Pause"); frameLoop(); });
player.addEventListener("pause", () => { $("play").classList.remove("is-playing"); $("play").setAttribute("aria-label", "Play"); tick(); });
player.addEventListener("timeupdate", tick);
player.addEventListener("click", togglePlay);
$("play").addEventListener("click", togglePlay);
$("seek").addEventListener("input", () => {
  seeking = true;
  if (Number.isFinite(player.duration)) player.currentTime = (Number($("seek").value) / 1000) * player.duration;
});
$("seek").addEventListener("change", () => { seeking = false; });

// ---------- wiring ----------

function chooseFiles() { $("file-input").click(); }
$("drop").addEventListener("click", chooseFiles);
$("add-files").addEventListener("click", chooseFiles);
$("file-input").addEventListener("change", (e) => {
  addFiles(e.target.files);
  e.target.value = "";
});

const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
window.addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  state.dragDepth++;
  $("screen").classList.add("is-over");
  refresh();
});
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  state.dragDepth = Math.max(0, state.dragDepth - 1);
  if (!state.dragDepth) $("screen").classList.remove("is-over");
  refresh();
});
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  state.dragDepth = 0;
  $("screen").classList.remove("is-over");
  addFiles(e.dataTransfer.files);
});

window.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || !state.open) return;
  if (["INPUT", "TEXTAREA", "SELECT", "BUTTON", "SUMMARY"].includes(e.target.tagName)) return;
  e.preventDefault();
  togglePlay();
});

window.addEventListener("pagehide", () => {
  if (!state.saveTimer || !state.open) return;      // send the last edit while the page goes away
  const headers = { "Content-Type": "application/json" };
  if (TOKEN) headers["X-Token"] = TOKEN;
  fetch(`/jobs/${state.open.jobId}/cues`, { method: "PUT", headers, keepalive: true,
    body: JSON.stringify(state.cues.map((c) => ({ start: c.start, end: c.end, text: c.text }))) });
});

$("settings").addEventListener("submit", (e) => { e.preventDefault(); createAll(); });
$("settings").addEventListener("change", () => {
  const form = $("settings");
  settings.language = $("language").value;
  settings.task = form.elements.task.value;
  settings.style = form.elements.style.value;
  settings.model = form.elements.model.value;
  store("gs.settings", settings);
  refresh();
});

$("editor-back").addEventListener("click", closeEditor);
$("replace-all").addEventListener("click", replaceAll);
$("shift-apply").addEventListener("click", shiftAll);
$("reset-edits").addEventListener("click", resetEdits);
document.querySelector(".editor-foot").addEventListener("click", (e) => {
  const format = e.target.dataset.format;
  if (format && state.open) download(`/jobs/${state.open.jobId}/subtitles?format=${format}`);
});
$("zip").addEventListener("click", () => {
  const ids = state.rows.filter(isDone).map((r) => r.jobId).join(",");
  download(`/zip?jobs=${ids}&format=srt`);
});

$("clear-all").addEventListener("click", async () => {
  if (state.rows.length && !confirm("Delete the uploaded files and their subtitles from the app?")) return;
  if (state.open) await closeEditor();
  await call("POST", "/cleanup").catch(() => {});
  const saved = load("gs.rows", []);
  for (const row of state.rows) row.dom.node.remove();
  state.rows = [];
  await validateRows(saved);
});

$("close-app").addEventListener("click", async () => {
  await flushSave();
  state.closed = true;
  await call("POST", "/shutdown").catch(() => {});
  document.body.replaceChildren(h("div", { class: "closed" },
    h("h1", { class: "brand" }, "GetSubtitles", h("span", {}, "App")),
    h("p", { class: "muted" }, "Closed. You can close this tab.")));
});

function showDevice(health) {
  const device = $("device");
  device.hidden = false;
  device.textContent = health.device === "cuda" ? "GPU" : "CPU";
  device.classList.toggle("chip-gpu", health.device === "cuda");
  device.title = health.demo ? "The demo runs on a shared processor, one file at a time."
    : health.device === "cuda" ? "The models run on your graphics card."
    : "The models run on the processor. Fast and Balanced are the practical choices.";
}

let gpuTimer = null;
function gpuText(gpu) {
  if (gpu.running && gpu.progress >= 0.99) return "Downloading GPU support\u2026 99%, unpacking the files.";
  if (gpu.running) return `Downloading GPU support\u2026 ${Math.floor(gpu.progress * 100)}%`;
  if (gpu.error) return gpu.error;
  if (gpu.installed) return "GPU support is downloaded, but the graphics card could not be used. " +
    "The app runs on the processor.";
  return "This computer has an NVIDIA graphics card. With GPU support the models run several times " +
    `faster. It is a ${fmtSize(gpu.download_bytes)} download, once.`;
}

async function showGpu() {
  clearTimeout(gpuTimer);
  let gpu;
  if (!TOKEN) { $("gpu-offer").hidden = true; return; }      // the demo and Docker have no GPU download
  try { gpu = await call("GET", "/gpu"); } catch (e) { $("gpu-offer").hidden = true; return; }
  if (gpu.running) gpuTimer = setTimeout(showGpu, 1000);   // first, so a later error cannot stop the updates
  $("gpu-offer").hidden = !gpu.present || (gpu.ready && !gpu.running);
  $("gpu-text").textContent = gpuText(gpu);
  $("gpu-install").hidden = gpu.running || (gpu.installed && !gpu.error);
  $("gpu-install").textContent = gpu.error ? "Try again" : "Download GPU support";
  if (gpu.ready && !gpu.running) showDevice(await call("GET", "/health").catch(() => state.health));
}

$("gpu-install").addEventListener("click", async () => {
  $("gpu-install").hidden = true;
  await call("POST", "/gpu/install").catch(() => {});
  showGpu();
});

async function showStorage() {
  let storage;
  if (!TOKEN) { $("storage").hidden = true; return; }        // only the desktop app has a storage folder
  try { storage = await call("GET", "/storage"); } catch (e) { $("storage").hidden = true; return; }
  $("storage").hidden = false;
  $("storage-path").textContent = storage.path;
  const models = storage.models.map((m) => `${MODELS[m.model][0]} ${fmtSize(m.size_bytes)}`);
  $("storage-models").textContent = models.length ? ` (${models.join(", ")}).` : ". No model is downloaded yet.";
}

$("open-storage").addEventListener("click", () => call("POST", "/storage/open").catch(() => {}));

function applyHealth(health) {
  state.health = health;
  const allowed = MODEL_ORDER.filter((m) => health.models.includes(m));
  if (!allowed.includes(settings.model)) settings.model = allowed[0];
  $("models").replaceChildren(...allowed.map((m) => h("label", { class: "model" },
    h("input", { type: "radio", name: "model", value: m, checked: m === settings.model }),
    h("div", {}, h("b", {}, MODELS[m][0]),
      h("small", {}, MODELS[m][1], h("span", { class: "model-id" }, MODELS[m][2]))))));

  showDevice(health);
  $("demo").hidden = !health.demo;
  $("demo-limit").textContent = health.max_upload_mb;
  $("demo-minutes").textContent = health.max_audio_minutes ? ` and ${health.max_audio_minutes} minutes` : "";
  if (health.demo) {
    $("offline").textContent = "The demo is not answering. It may be restarting; this page continues by itself.";
  }
  $("privacy-demo").hidden = !health.demo;
  $("privacy-local").hidden = health.demo;
  $("clear-all").hidden = health.demo;
  $("close-app").hidden = !TOKEN;
}

async function init() {
  const language = $("language");
  language.append(h("option", { value: "auto" }, "Detect automatically"),
    ...Object.entries(LANGUAGES).sort((a, b) => a[1].localeCompare(b[1]))
      .map(([code, name]) => h("option", { value: code }, name)));
  language.value = settings.language;
  const form = $("settings");
  form.elements.task.value = settings.task;
  form.elements.style.value = settings.style;

  for (;;) {
    try {
      applyHealth(await call("GET", "/health"));
      break;
    } catch (e) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }
  showStorage();
  showGpu();
  await validateRows(load("gs.rows", []));
  setInterval(poll, 700);
  if (!state.health.demo) new EventSource("/presence");   // tells the desktop app that a tab is open
}

window.GS = { state, settings, fmtTime, parseTime, fmtClock, wrapTwo };
refresh();
init();
