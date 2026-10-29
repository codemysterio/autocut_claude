"use strict";
const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const KINDS = {
  manual: "Manual", filler: "Filler words", repeat: "Repeated words",
  take: "Repeated takes", silence: "Pauses", claude: "Claude",
};
let cfg = { languages: {}, models: [], default_model: "", settings: {} };
let P = null;            // current project
let pollTimer = null, rafId = null;
let appliedIv = [];      // merged applied cut intervals [start,end]
let skipCuts = true;
let focusCut = null;

// ---------------------------------------------------------------- helpers
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (t) => {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
};
const fmt1 = (t) => `${fmt(t)}.${Math.floor((t % 1) * 10)}`;

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.hidden = true), err ? 6000 : 2200);
}

async function api(path, method = "GET", body) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api" + path, opt);
  if (!r.ok) {
    let d = r.statusText;
    try { d = (await r.json()).detail || d; } catch {}
    throw new Error(d);
  }
  return r.json();
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

function stop() { clearInterval(pollTimer); cancelAnimationFrame(rafId); pollTimer = rafId = null; P = null; }

// ---------------------------------------------------------------- routing
window.addEventListener("hashchange", route);
async function route() {
  stop();
  const m = location.hash.match(/^#\/p\/(\w+)/);
  if (m) await openProject(m[1]); else await home();
}

// ---------------------------------------------------------------- home
async function home() {
  $("#crumb").textContent = "";
  const opts = (obj, sel) => Object.entries(obj).map(([k, v]) => `<option value="${k}" ${k === sel ? "selected" : ""}>${v}</option>`).join("");
  app.innerHTML = `
  <div class="home">
    <div class="drop" id="drop">
      <h2>Drop a video or audio file</h2>
      <div class="hint">mp4, mov, mkv, webm, mp3, wav, m4a… It stays on this computer.</div>
      <div class="row center">
        <label>Language <select id="lang">${opts(cfg.languages, "auto")}</select></label>
        <label>Model <select id="model">${cfg.models.map((m) => `<option ${m === cfg.default_model ? "selected" : ""}>${m}</option>`).join("")}</select></label>
        <button class="primary" id="pick">Choose file…</button>
        <input type="file" id="file" hidden accept="video/*,audio/*,.mkv,.mov">
      </div>
      <div class="pathbox"><input id="path" placeholder="…or paste a full file path, e.g. D:\\videos\\interview.mp4"><button id="usepath">Open path</button></div>
      <p class="hint" style="margin-bottom:0">Tip: for Hindi use <b>large-v3</b>. Opening by path links to the original file, which is best for Premiere export.</p>
    </div>
    <div class="plist" id="plist"></div>
  </div>`;
  const drop = $("#drop"), file = $("#file");
  const start = guard(async (f) => {
    const fd = new FormData();
    fd.append("file", f); fd.append("language", $("#lang").value); fd.append("model", $("#model").value);
    drop.innerHTML = `<h2>Uploading ${esc(f.name)}…</h2><div class="progress"><i style="width:60%"></i></div>`;
    const p = await api("/projects", "POST", fd);
    location.hash = "#/p/" + p.id;
  });
  $("#pick").onclick = () => file.click();
  file.onchange = () => file.files[0] && start(file.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); e.dataTransfer.files[0] && start(e.dataTransfer.files[0]); };
  $("#usepath").onclick = guard(async () => {
    const path = $("#path").value.trim();
    if (!path) return;
    const p = await api("/projects/from-path", "POST", { path, language: $("#lang").value, model: $("#model").value });
    location.hash = "#/p/" + p.id;
  });
  const list = await api("/projects");
  $("#plist").innerHTML = list.map((p) => `
    <a class="pitem" href="#/p/${p.id}">
      <span>${p.kind === "video" ? "🎬" : "🎧"}</span>
      <span class="name">${esc(p.name)}</span>
      <span class="hint">${fmt(p.duration)}</span>
      <span class="pill ${p.state === "error" ? "error" : ""}">${p.state === "ready" ? p.cuts + " cuts" : p.state}</span>
      <button class="small danger" data-del="${p.id}">Delete</button>
    </a>`).join("");
  app.querySelectorAll("[data-del]").forEach((b) => (b.onclick = guard(async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!confirm("Delete this project and its files? Your original file is not touched when it was opened by path.")) return;
    await api("/projects/" + b.dataset.del, "DELETE");
    home();
  })));
}

// ---------------------------------------------------------------- project
async function openProject(id) {
  P = await api("/projects/" + id);
  $("#crumb").textContent = "/ " + P.name;
  render();
  let ver = P.version;
  pollTimer = setInterval(async () => {
    try {
      const v = await api(`/projects/${id}/version`);
      if (v.version !== ver) { ver = v.version; await refresh(); }
    } catch {}
  }, 900);
}

async function refresh() {
  const prev = P.status.state;
  P = await api("/projects/" + P.id);
  if (P.status.state === "processing" || prev !== P.status.state) return render();
  paintEditor();
}

function render() {
  cancelAnimationFrame(rafId);
  const s = P.status;
  if (s.state === "processing" || s.state === "created") {
    app.innerHTML = `<div class="card center-card"><h2>${esc(s.step || "Working…")}</h2>
      <div class="progress"><i style="width:${Math.round((s.progress || 0) * 100)}%"></i></div>
      <div class="hint">${esc(s.message || "")}</div>
      <p class="hint">First run loads the model onto the GPU, which can take a minute.</p></div>`;
    return;
  }
  if (s.state === "error" && !P.words.length) {
    app.innerHTML = `<div class="card center-card"><h2>Something went wrong</h2><p>${esc(s.message)}</p>
      <div class="row center"><select id="lang">${Object.entries(cfg.languages).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
      <button class="primary" id="retry">Try again</button></div></div>`;
    $("#retry").onclick = guard(async () => { await api(`/projects/${P.id}/transcribe`, "POST", { language: $("#lang").value }); await refresh(); });
    return;
  }
  buildEditor();
}

// ---------------------------------------------------------------- editor
function buildEditor() {
  const isVideo = P.media.kind === "video";
  app.innerHTML = `
  <div class="editor">
    <div class="side">
      <div class="card">
        ${isVideo ? `<video id="player" controls preload="metadata"></video>` : `<audio id="player" controls preload="metadata"></audio>`}
        <label class="chk small" style="margin-top:8px"><input type="checkbox" id="skip" checked> Skip cut parts when playing</label>
      </div>
      <div class="card"><h3>Length</h3><div class="stats" id="stats"></div></div>
      <div class="card">
        <h3>Find things to remove</h3>
        <div class="opts">
          <label class="chk"><input type="checkbox" id="o-fillers" checked> Filler words (um, uh, ehm…)</label>
          <label class="chk"><input type="checkbox" id="o-soft"> Verbal padding (like, you know, cioè, matlab…)</label>
          <label class="chk"><input type="checkbox" id="o-repeats" checked> Stutters &amp; repeated words</label>
          <label class="chk"><input type="checkbox" id="o-takes" checked> Repeated takes &amp; false starts</label>
          <label class="chk"><input type="checkbox" id="o-sil" checked> Pauses longer than <input type="number" id="o-gap" value="0.8" step="0.1" min="0.3"> s</label>
        </div>
        <button class="primary" id="analyze">Find suggestions</button>
        <div class="hint" style="margin-top:6px">Nothing is cut until you accept it. You can also ask Claude through MCP.</div>
      </div>
      <div class="card" id="sugcard"></div>
      <div class="card">
        <h3>Export</h3>
        <div class="btns">
          <button class="primary" data-x="xml">Premiere Pro XML</button>
          <button data-x="edl">EDL</button>
          <button data-x="srt">SRT</button><button data-x="vtt">VTT</button><button data-x="txt">Text</button>
        </div>
        <div class="btns" style="margin-top:8px"><button data-x="media">Render ${isVideo ? "video (mp4)" : "audio (m4a)"}</button>
          ${isVideo ? "" : `<select id="afmt"><option>m4a</option><option>mp3</option><option>wav</option></select>`}</div>
        <div id="expstat" class="hint" style="margin-top:8px"></div>
      </div>
      <div class="card"><div class="row"><button class="small" id="retx">Transcribe again…</button>
        <span class="hint">${esc(P.media.name)} · ${P.language || "?"} · ${P.device || ""} ${esc(P.model || "")}</span></div></div>
    </div>
    <div class="main">
      <div class="bar">
        <button id="undo">↶ Undo</button><button id="redo">↷ Redo</button>
        <span class="hint">Click a word to jump · drag to select, then press <b>Delete</b> · click a struck-out word to bring it back · double-click a word to fix the text</span>
      </div>
      <div class="legend">${Object.entries(KINDS).map(([k, v]) => `<span style="--kc:var(--k-${k})"><i></i>${v}</span>`).join("")}</div>
      <div class="script" id="script"></div>
    </div>
  </div>`;
  const pl = $("#player");
  pl.src = `/api/projects/${P.id}/media`;
  $("#skip").onchange = (e) => (skipCuts = e.target.checked);
  $("#analyze").onclick = guard(async () => {
    const r = await api(`/projects/${P.id}/analyze`, "POST", {
      fillers: $("#o-fillers").checked, soft_fillers: $("#o-soft").checked,
      repeats: $("#o-repeats").checked, takes: $("#o-takes").checked,
      silences: $("#o-sil").checked, min_silence: parseFloat($("#o-gap").value) || 0.8,
    });
    const n = Object.values(r.found).reduce((a, b) => a + b, 0);
    toast(n ? `${n} suggestions found` : "Nothing new found");
    await refresh();
  });
  $("#undo").onclick = guard(async () => { await api(`/projects/${P.id}/undo`, "POST"); await refresh(); });
  $("#redo").onclick = guard(async () => { await api(`/projects/${P.id}/redo`, "POST"); await refresh(); });
  $("#retx").onclick = guard(async () => {
    const code = prompt("Language code (auto, en, it, hi, hinglish). This replaces the transcript and clears all cuts.", P.language || "auto");
    if (!code) return;
    await api(`/projects/${P.id}/transcribe`, "POST", { language: code.trim() });
    await refresh();
  });
  app.querySelectorAll("[data-x]").forEach((b) => (b.onclick = guard(() => doExport(b.dataset.x))));
  $("#script").addEventListener("mouseup", onSelect);
  $("#script").addEventListener("click", onScriptClick);
  $("#script").addEventListener("dblclick", onWordEdit);
  paintEditor();
  loop();
}

function cutIndex() {
  // per word: the cut that covers it (applied wins over proposed)
  const st = new Array(P.words.length).fill(null);
  const gaps = {};
  for (const c of P.cuts) {
    if (c.w0 == null) { if (c.gap != null) gaps[c.gap] = c; continue; }
    for (let i = c.w0; i <= c.w1; i++) {
      if (!st[i] || (st[i].status === "proposed" && c.status === "applied")) st[i] = c;
    }
  }
  return { st, gaps };
}

function paintEditor() {
  if (!$("#script")) return buildEditor();
  const { st, gaps } = cutIndex();
  const W = P.words;
  const chip = (c) => `<span class="gap ${c.status === "applied" ? "cut" : "prop"}" data-cut="${c.id}" title="${c.status === "applied" ? "Click to restore" : "Suggested: " + esc(c.reason)}">⏸ ${(c.end - c.start).toFixed(1)}s</span> `;
  let html = "", open = false;
  const openPara = (t) => { html += `<div class="para"><span class="ts" data-t="${t}">${fmt(t)}</span><span class="txt">`; open = true; };
  if (gaps[-1]) { openPara(0); html += chip(gaps[-1]); }
  for (let i = 0; i < W.length; i++) {
    const w = W[i], prev = W[i - 1];
    const newPara = !prev || (w.s - prev.e > 1.2) || (/[.?!।…]$/.test(prev.w) && w.s - prev.e > 0.6);
    if (newPara) { if (open) html += "</span></div>"; openPara(w.s); }
    const c = st[i];
    let cls = "w", style = "", cid = "";
    if (c) {
      cid = ` data-cut="${c.id}"`;
      if (c.status === "applied") cls += " cut";
      else { cls += " prop" + (focusCut === c.id ? " focus" : ""); style = ` style="--kc:var(--k-${c.kind})"`; }
    }
    html += `<span class="${cls}" data-i="${i}"${cid}${style}>${esc(w.w)}</span> `;
    if (gaps[i]) html += chip(gaps[i]);
  }
  if (open) html += "</span></div>";
  $("#script").innerHTML = html || `<p class="hint">No words were found. Try a different language or model.</p>`;
  loop.last = -2; // DOM was rebuilt: force the playing-word highlight to repaint

  // intervals the player should skip
  const iv = P.cuts.filter((c) => c.status === "applied").map((c) => [c.start, c.end]).sort((a, b) => a[0] - b[0]);
  appliedIv = [];
  for (const [s, e] of iv) {
    if (appliedIv.length && s <= appliedIv[appliedIv.length - 1][1]) appliedIv[appliedIv.length - 1][1] = Math.max(appliedIv[appliedIv.length - 1][1], e);
    else appliedIv.push([s, e]);
  }

  const S = P.stats;
  $("#stats").innerHTML = `<div><b>${fmt(S.original)}</b>original</div><div><b>${fmt(S.edited)}</b>edited</div>
    <div><b>−${fmt(S.removed)}</b>removed</div>`;
  paintSuggestions();
  const ex = P.export;
  $("#expstat").innerHTML = ex.state === "running" ? `Rendering… ${Math.round(ex.progress * 100)}%`
    : ex.state === "done" ? `Rendered: <a href="/api/projects/${P.id}/download/media">download</a><br><span class="small">${esc(ex.file)}</span>`
    : ex.state === "error" ? `<span style="color:var(--k-manual)">${esc(ex.message)}</span>` : "";
  app.querySelectorAll("[data-x=media]").forEach((b) => (b.disabled = ex.state === "running"));
}

function paintSuggestions() {
  const props = P.cuts.filter((c) => c.status === "proposed").sort((a, b) => a.start - b.start);
  const box = $("#sugcard");
  if (!props.length) { box.innerHTML = `<h3>Suggestions</h3><div class="hint">None right now.</div>`; return; }
  const groups = {};
  props.forEach((c) => (groups[c.kind] = groups[c.kind] || []).push(c));
  box.innerHTML = `<h3>Suggestions (${props.length})</h3>
    <div class="btns"><button class="primary small" data-bulk="accept">Cut all</button><button class="small" data-bulk="reject">Dismiss all</button></div>
    <div class="sug">${Object.entries(groups).map(([k, list]) => `
      <div class="group" style="--kc:var(--k-${k})"><span class="dot"></span><b>${KINDS[k] || k} (${list.length})</b>
        <button class="small" data-bulk="accept" data-kind="${k}">Cut</button>
        <button class="small" data-bulk="reject" data-kind="${k}">Dismiss</button></div>
      ${list.map((c) => `<div class="sugitem ${focusCut === c.id ? "active" : ""}" style="--kc:var(--k-${k})" data-sug="${c.id}">
        <span class="t">${fmt(c.start)}</span><span class="why" title="${esc(c.reason)}">${esc(c.reason || KINDS[k])}</span>
        <button class="small primary" data-acc="${c.id}">Cut</button><button class="small" data-rej="${c.id}">Keep</button></div>`).join("")}`).join("")}</div>`;
  box.querySelectorAll("[data-bulk]").forEach((b) => (b.onclick = guard(async () => {
    await api(`/projects/${P.id}/cuts/bulk`, "POST", { action: b.dataset.bulk, kind: b.dataset.kind || null, status: "proposed" });
    await refresh();
  })));
  box.querySelectorAll("[data-acc]").forEach((b) => (b.onclick = guard(async (e) => {
    e.stopPropagation(); await api(`/projects/${P.id}/cuts/${b.dataset.acc}/accept`, "POST"); await refresh();
  })));
  box.querySelectorAll("[data-rej]").forEach((b) => (b.onclick = guard(async (e) => {
    e.stopPropagation(); await api(`/projects/${P.id}/cuts/${b.dataset.rej}`, "DELETE"); await refresh();
  })));
  box.querySelectorAll("[data-sug]").forEach((el) => (el.onclick = () => {
    focusCut = el.dataset.sug;
    const c = P.cuts.find((x) => x.id === focusCut);
    if (c) { $("#player").currentTime = Math.max(0, c.start - 1.5); }
    paintEditor();
    const target = $(`#script [data-cut="${focusCut}"]`);
    if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
  }));
}

// ---------------------------------------------------------------- interaction
function wordOf(node) {
  const el = node && (node.nodeType === 3 ? node.parentElement : node);
  return el && el.closest ? el.closest("[data-i]") : null;
}

function onSelect() {
  killFloating();
  const sel = getSelection();
  if (!sel || sel.isCollapsed) return;
  const a = wordOf(sel.anchorNode), b = wordOf(sel.focusNode);
  if (!a || !b) return;
  const w0 = Math.min(+a.dataset.i, +b.dataset.i), w1 = Math.max(+a.dataset.i, +b.dataset.i);
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  const f = document.createElement("div");
  f.className = "floating"; f.id = "floating";
  f.style.left = Math.max(8, rect.left) + "px"; f.style.top = Math.max(60, rect.top - 44) + "px";
  f.innerHTML = `<button>✂ Cut ${w1 - w0 + 1} word${w1 > w0 ? "s" : ""} (Del)</button>`;
  f.firstChild.onmousedown = (e) => e.preventDefault();
  f.firstChild.onclick = guard(() => cutRange(w0, w1));
  document.body.appendChild(f);
  f._range = [w0, w1];
}
const killFloating = () => { const f = $("#floating"); if (f) f.remove(); };

async function cutRange(w0, w1) {
  killFloating(); getSelection().removeAllRanges();
  await api(`/projects/${P.id}/cuts`, "POST", { w0, w1, kind: "manual" });
  await refresh();
}

const onScriptClick = guard(async (e) => {
  const sel = getSelection();
  if (sel && !sel.isCollapsed) return;
  killFloating();
  const gap = e.target.closest(".gap");
  if (gap) {
    const c = P.cuts.find((x) => x.id === gap.dataset.cut);
    if (c.status === "applied") { await api(`/projects/${P.id}/cuts/${c.id}`, "DELETE"); await refresh(); }
    else { $("#player").currentTime = Math.max(0, c.start - 1); }
    return;
  }
  const ts = e.target.closest(".ts");
  if (ts) { $("#player").currentTime = +ts.dataset.t; return; }
  const w = wordOf(e.target);
  if (!w) return;
  const cid = w.dataset.cut && P.cuts.find((x) => x.id === w.dataset.cut);
  if (cid && cid.status === "applied") { await api(`/projects/${P.id}/cuts/${cid.id}`, "DELETE"); await refresh(); return; }
  if (cid) { focusCut = cid.id; paintEditor(); }
  const pl = $("#player");
  pl.currentTime = P.words[+w.dataset.i].s;
});

const onWordEdit = guard(async (e) => {
  const w = wordOf(e.target);
  if (!w) return;
  const i = +w.dataset.i;
  const nv = prompt("Fix this word:", P.words[i].w);
  if (nv && nv.trim() && nv !== P.words[i].w) { await api(`/projects/${P.id}/words/${i}`, "PATCH", { w: nv }); await refresh(); }
});

document.addEventListener("keydown", guard(async (e) => {
  if (!P || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
  const f = $("#floating");
  if ((e.key === "Delete" || e.key === "Backspace") && f) { e.preventDefault(); await cutRange(...f._range); }
  else if (e.ctrlKey && e.key.toLowerCase() === "z") { e.preventDefault(); await api(`/projects/${P.id}/undo`, "POST"); await refresh(); }
  else if (e.ctrlKey && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z"))) { e.preventDefault(); await api(`/projects/${P.id}/redo`, "POST"); await refresh(); }
  else if (e.key === " " && !f) { const pl = $("#player"); if (pl) { e.preventDefault(); pl.paused ? pl.play() : pl.pause(); } }
}));
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".floating")) killFloating(); });

async function doExport(kind) {
  if (kind === "media") {
    const fmtSel = $("#afmt");
    await api(`/projects/${P.id}/export`, "POST", { kind, format: fmtSel ? fmtSel.value : null });
    toast("Rendering started");
    return refresh();
  }
  if (P.stats.applied === 0 && kind === "xml" && !confirm("No cuts are applied yet, so the XML will be the uncut timeline. Export anyway?")) return;
  const r = await api(`/projects/${P.id}/export`, "POST", { kind });
  const a = document.createElement("a");
  a.href = `/api/projects/${P.id}/download/${kind}`; a.download = ""; a.click();
  toast("Saved: " + r.file);
}

// ---------------------------------------------------------------- playback loop
function loop() {
  const pl = $("#player");
  if (!pl) return;
  const t = pl.currentTime;
  if (skipCuts && !pl.paused) {
    for (const [s, e] of appliedIv) {
      if (t >= s && t < e - 0.02) { pl.currentTime = e; break; }
      if (s > t) break;
    }
  }
  // highlight current word
  const W = P.words;
  let lo = 0, hi = W.length - 1, idx = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (W[m].s <= t) { idx = m; lo = m + 1; } else hi = m - 1; }
  if (idx !== loop.last) {
    const old = $("#script .w.now"); if (old) old.classList.remove("now");
    const el = idx >= 0 && $(`#script [data-i="${idx}"]`);
    if (el) {
      el.classList.add("now");
      if (!pl.paused) {
        const r = el.getBoundingClientRect();
        if (r.top < 120 || r.bottom > innerHeight - 40) el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }
    loop.last = idx;
  }
  rafId = requestAnimationFrame(loop);
}

// ---------------------------------------------------------------- boot
(async function boot() {
  try { cfg = await api("/config"); } catch (e) { toast("Cannot reach the AutoCut server", true); }
  const chk = $("#claudeAuto");
  chk.checked = !!cfg.settings.claude_auto_apply;
  chk.onchange = guard(async () => { await api("/settings", "PUT", { claude_auto_apply: chk.checked }); toast(chk.checked ? "Claude's cuts now apply instantly" : "Claude's cuts now wait for your approval"); });
  route();
})();
