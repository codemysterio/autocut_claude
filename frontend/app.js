"use strict";
// Shared helpers, routing and the home screen. Loaded first; transcript.js / editor.js build on it.
const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const KINDS = {
  manual: "Manual", filler: "Filler words", repeat: "Repeated words",
  take: "Repeated takes", silence: "Pauses", claude: "Claude",
};
let cfg = { languages: {}, models: [], default_model: "", settings: {} };

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (t) => {
  t = Math.max(0, t || 0);
  const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = Math.floor(t % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
};
const fmtSize = (b) => (b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : (b / 1e6).toFixed(0) + " MB");
const fmtTC = (t) => {
  t = Math.max(0, t || 0);
  const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = Math.floor(t % 60), f = Math.floor((t % 1) * 10);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${f}`;
};

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

function modal(html) {
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal">${html}</div>`;
  bg.onmousedown = (e) => { if (e.target === bg) bg.remove(); };
  document.body.appendChild(bg);
  return bg;
}

// ---------------------------------------------------------------- routing
window.addEventListener("hashchange", route);
async function route() {
  NLE.close();
  document.body.classList.remove("nle-mode");
  const s = location.hash.match(/^#\/s\/(\w+)/);
  const p = location.hash.match(/^#\/p\/(\w+)/);
  try {
    if (s) { document.body.classList.add("nle-mode"); await NLE.open(s[1]); }
    else if (p) {  // old per-media links: open (or create) the sequence that contains it
      const seq = await api(`/sequences/for-project/${p[1]}`, "POST");
      location.replace("#/s/" + seq.id);
    } else await home();
  } catch (e) { toast(e.message, true); app.innerHTML = `<p class="hint" style="padding:30px">${esc(e.message)}</p>`; }
}

// ---------------------------------------------------------------- home
async function home() {
  $("#crumb").textContent = "";
  app.innerHTML = `
  <div class="home">
    <div class="drop" id="drop">
      <h2>Start a sequence</h2>
      <div class="hint">Import several videos or audio files, arrange them on a timeline, then trim by editing the transcript.</div>
      <div class="row center"><button class="primary" id="new">New empty sequence</button></div>
      <div class="hint" style="margin-top:10px">or drop video / audio files here to create a sequence from them</div>
    </div>
    <div class="plist" id="plist"></div>
  </div>`;
  $("#new").onclick = guard(async () => {
    const s = await api("/sequences", "POST", {});
    location.hash = "#/s/" + s.id;
  });
  const drop = $("#drop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = guard(async (e) => {
    e.preventDefault(); drop.classList.remove("over");
    if (!e.dataTransfer.files.length) return;
    const s = await api("/sequences", "POST", {});
    NLE.pendingFiles = [...e.dataTransfer.files];
    location.hash = "#/s/" + s.id;
  });
  const list = await api("/sequences");
  $("#plist").innerHTML = list.length ? list.map((s) => `
    <a class="pitem" href="#/s/${s.id}">
      <span>🎞️</span><span class="name">${esc(s.name)}</span>
      <span class="hint">${s.clips} clip${s.clips === 1 ? "" : "s"} · ${fmt(s.duration)}</span>
      <button class="small danger" data-del="${s.id}">Delete</button>
    </a>`).join("") : `<p class="hint" style="text-align:center">No sequences yet.</p>`;
  app.querySelectorAll("[data-del]").forEach((b) => (b.onclick = guard(async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!confirm("Delete this sequence? Your media files and their transcripts are kept.")) return;
    await api("/sequences/" + b.dataset.del, "DELETE");
    home();
  })));
}
