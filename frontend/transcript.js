"use strict";
// Transcript panel: shows the transcript of the selected clip's media and turns edits into cuts.
const TP = (() => {
  let root, P = null, focusCut = null, hooks = {}, lastNow = -2, bound = false;

  function init(container, h) {
    hooks = h;
    root = container;
    root.innerHTML = `
      <div class="ph"><b>Transcript</b><span class="hint" id="tpTitle"></span><span class="sp"></span>
        <button class="small" id="tpUndo" title="Ctrl+Z">↶ Undo</button><button class="small" id="tpRedo" title="Ctrl+Y">↷ Redo</button></div>
      <div class="tp-bar">
        <button class="primary small" id="tpAnalyze">Find suggestions</button>
        <details><summary class="hint" style="cursor:pointer">Options</summary><div class="opts">
          <label class="chk"><input type="checkbox" id="o-fillers" checked> Filler words (um, uh, ehm…)</label>
          <label class="chk"><input type="checkbox" id="o-soft"> Verbal padding (like, you know, cioè…)</label>
          <label class="chk"><input type="checkbox" id="o-repeats" checked> Stutters &amp; repeated words</label>
          <label class="chk"><input type="checkbox" id="o-takes" checked> Repeated takes &amp; false starts</label>
          <label class="chk"><input type="checkbox" id="o-sil" checked> Pauses longer than <input type="number" id="o-gap" value="0.8" step="0.1" min="0.3"> s</label>
        </div></details>
        <span class="hint">Drag to select words → <b>Delete</b> cuts them</span>
      </div>
      <div class="tp-sug" id="tpSug" hidden></div>
      <div class="tp-body" id="tpBody"><div class="tp-empty">Select a clip on the timeline to edit its transcript.</div></div>`;
    $("#tpUndo", root).onclick = guard(() => step("undo"));
    $("#tpRedo", root).onclick = guard(() => step("redo"));
    $("#tpAnalyze", root).onclick = guard(analyze);
    const body = $("#tpBody", root);
    body.addEventListener("mouseup", onSelect);
    body.addEventListener("click", guard(onClick));
    body.addEventListener("dblclick", guard(onWordEdit));
    if (!bound) {  // document-level listeners are added once; they act on whatever panel is current
      document.addEventListener("mousedown", (e) => { if (!e.target.closest(".floating")) killFloating(); });
      document.addEventListener("keydown", guard(onKey));
      bound = true;
    }
  }

  async function show(pid) {
    if (!pid) { P = null; paint(); return; }
    if (P && P.id === pid) return;
    P = await api("/projects/" + pid);
    focusCut = null;
    paint();
  }

  // Called after every sequence refresh with the media summaries; reloads if the transcript changed.
  async function sync(items) {
    if (!P) return;
    const it = items.find((i) => i.id === P.id);
    if (!it) { P = null; paint(); return; }
    if (it.version !== P.version) { P = await api("/projects/" + P.id); paint(); }
  }

  async function step(what) {
    if (!P) return;
    await api(`/projects/${P.id}/${what}`, "POST");
    await hooks.onChange();
  }

  async function analyze() {
    if (!P || !P.words.length) return toast("This media has no transcript yet", true);
    const r = await api(`/projects/${P.id}/analyze`, "POST", {
      fillers: $("#o-fillers", root).checked, soft_fillers: $("#o-soft", root).checked,
      repeats: $("#o-repeats", root).checked, takes: $("#o-takes", root).checked,
      silences: $("#o-sil", root).checked, min_silence: parseFloat($("#o-gap", root).value) || 0.8,
    });
    const n = Object.values(r.found).reduce((a, b) => a + b, 0);
    toast(n ? `${n} suggestions found` : "Nothing new found");
    await hooks.onChange();
  }

  function cutIndex() {
    const st = new Array(P.words.length).fill(null), gaps = {};
    for (const c of P.cuts) {
      if (c.w0 == null) { if (c.gap != null) gaps[c.gap] = c; continue; }
      for (let i = c.w0; i <= c.w1; i++) if (!st[i] || (st[i].status === "proposed" && c.status === "applied")) st[i] = c;
    }
    return { st, gaps };
  }

  function paint() {
    const body = $("#tpBody", root);
    $("#tpTitle", root).textContent = P ? "— " + P.name : "";
    lastNow = -2;
    if (!P) { body.innerHTML = `<div class="tp-empty">Select a clip on the timeline to edit its transcript.</div>`; paintSuggestions(); return; }
    const s = P.status;
    if (s.state === "processing" || s.state === "created") {
      body.innerHTML = `<div class="tp-empty"><b>${esc(s.step || "Working…")}</b>
        <div class="progress"><i style="width:${Math.round((s.progress || 0) * 100)}%"></i></div>
        <div class="hint">${esc(s.message || "")}</div></div>`;
      paintSuggestions(); return;
    }
    if (s.state === "error" && !P.words.length) {
      body.innerHTML = `<div class="tp-empty"><b>Transcription failed</b><p>${esc(s.message)}</p>
        <button class="primary" id="tpRetry">Try again</button></div>`;
      $("#tpRetry", root).onclick = guard(async () => { await api(`/projects/${P.id}/transcribe`, "POST", { language: "auto" }); await hooks.onChange(); });
      paintSuggestions(); return;
    }
    const { st, gaps } = cutIndex();
    const W = P.words;
    const chip = (c) => `<span class="gap ${c.status === "applied" ? "cut" : "prop"}" data-cut="${c.id}" title="${c.status === "applied" ? "Click to restore" : "Suggested: " + esc(c.reason)}">⏸ ${(c.end - c.start).toFixed(1)}s</span> `;
    let html = `<div class="legend" style="margin-bottom:8px">${Object.entries(KINDS).map(([k, v]) => `<span style="--kc:var(--k-${k})"><i></i>${v}</span>`).join("")}</div>`, open = false;
    const openPara = (t) => { html += `<div class="para"><span class="ts" data-t="${t}">${fmt(t)}</span><span class="txt">`; open = true; };
    if (gaps[-1]) { openPara(0); html += chip(gaps[-1]); }
    for (let i = 0; i < W.length; i++) {
      const w = W[i], prev = W[i - 1];
      if (!prev || (w.s - prev.e > 1.2) || (/[.?!।…]$/.test(prev.w) && w.s - prev.e > 0.6)) { if (open) html += "</span></div>"; openPara(w.s); }
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
    const keep = body.scrollTop;
    body.innerHTML = html + (W.length ? "" : `<p class="hint">No words were found. Try another language or model.</p>`);
    body.scrollTop = keep;
    paintSuggestions();
  }

  function paintSuggestions() {
    const box = $("#tpSug", root);
    const props = P ? P.cuts.filter((c) => c.status === "proposed").sort((a, b) => a.start - b.start) : [];
    box.hidden = !props.length;
    if (!props.length) { box.innerHTML = ""; return; }
    const groups = {};
    props.forEach((c) => (groups[c.kind] = groups[c.kind] || []).push(c));
    box.innerHTML = `<div class="btns"><b>${props.length} suggestions</b>
      <button class="primary small" data-bulk="accept">Cut all</button><button class="small" data-bulk="reject">Dismiss all</button></div>
      <div class="sug">${Object.entries(groups).map(([k, list]) => `
        <div class="group" style="--kc:var(--k-${k})"><span class="dot"></span><b>${KINDS[k] || k} (${list.length})</b>
          <button class="small" data-bulk="accept" data-kind="${k}">Cut</button><button class="small" data-bulk="reject" data-kind="${k}">Dismiss</button></div>
        ${list.map((c) => `<div class="sugitem ${focusCut === c.id ? "active" : ""}" style="--kc:var(--k-${k})" data-sug="${c.id}">
          <span class="t">${fmt(c.start)}</span><span class="why" title="${esc(c.reason)}">${esc(c.reason || KINDS[k])}</span>
          <button class="small primary" data-acc="${c.id}">Cut</button><button class="small" data-rej="${c.id}">Keep</button></div>`).join("")}`).join("")}</div>`;
    const done = guard(async (fn) => { await fn(); await hooks.onChange(); });
    box.querySelectorAll("[data-bulk]").forEach((b) => (b.onclick = () => done(() =>
      api(`/projects/${P.id}/cuts/bulk`, "POST", { action: b.dataset.bulk, kind: b.dataset.kind || null, status: "proposed" }))));
    box.querySelectorAll("[data-acc]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); done(() => api(`/projects/${P.id}/cuts/${b.dataset.acc}/accept`, "POST")); }));
    box.querySelectorAll("[data-rej]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); done(() => api(`/projects/${P.id}/cuts/${b.dataset.rej}`, "DELETE")); }));
    box.querySelectorAll("[data-sug]").forEach((el) => (el.onclick = () => {
      focusCut = el.dataset.sug;
      const c = P.cuts.find((x) => x.id === focusCut);
      if (c) hooks.seek(P.id, Math.max(0, c.start - 1.5));
      paint();
      const t = $(`#tpBody [data-cut="${focusCut}"]`, root);
      if (t) t.scrollIntoView({ block: "center", behavior: "smooth" });
    }));
  }

  // ------------------------------------------------------------ interaction
  const wordOf = (n) => { const el = n && (n.nodeType === 3 ? n.parentElement : n); return el && el.closest ? el.closest("[data-i]") : null; };
  const killFloating = () => { const f = $("#floating"); if (f) f.remove(); };

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

  async function cutRange(w0, w1) {
    killFloating(); getSelection().removeAllRanges();
    await api(`/projects/${P.id}/cuts`, "POST", { w0, w1, kind: "manual" });
    await hooks.onChange();
  }

  async function onClick(e) {
    const sel = getSelection();
    if (sel && !sel.isCollapsed) return;
    killFloating();
    const gap = e.target.closest(".gap");
    if (gap) {
      const c = P.cuts.find((x) => x.id === gap.dataset.cut);
      if (c.status === "applied") { await api(`/projects/${P.id}/cuts/${c.id}`, "DELETE"); await hooks.onChange(); }
      else hooks.seek(P.id, Math.max(0, c.start - 1));
      return;
    }
    const ts = e.target.closest(".ts");
    if (ts) { hooks.seek(P.id, +ts.dataset.t); return; }
    const w = wordOf(e.target);
    if (!w) return;
    const c = w.dataset.cut && P.cuts.find((x) => x.id === w.dataset.cut);
    if (c && c.status === "applied") { await api(`/projects/${P.id}/cuts/${c.id}`, "DELETE"); await hooks.onChange(); return; }
    if (c) { focusCut = c.id; paint(); }
    hooks.seek(P.id, P.words[+w.dataset.i].s);
  }

  async function onWordEdit(e) {
    const w = wordOf(e.target);
    if (!w) return;
    const i = +w.dataset.i;
    const nv = prompt("Fix this word:", P.words[i].w);
    if (nv && nv.trim() && nv !== P.words[i].w) { await api(`/projects/${P.id}/words/${i}`, "PATCH", { w: nv }); await hooks.onChange(); }
  }

  async function onKey(e) {
    if (!P || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    const f = $("#floating");
    if ((e.key === "Delete" || e.key === "Backspace") && f) { e.preventDefault(); await cutRange(...f._range); }
    else if (e.ctrlKey && e.key.toLowerCase() === "z") { e.preventDefault(); await step("undo"); }
    else if (e.ctrlKey && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z"))) { e.preventDefault(); await step("redo"); }
  }

  // Highlight the word being played (srcTime = seconds in the media file).
  function tick(pid, srcTime, playing) {
    if (!P || P.id !== pid || !P.words.length) return;
    const W = P.words;
    let lo = 0, hi = W.length - 1, idx = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (W[m].s <= srcTime) { idx = m; lo = m + 1; } else hi = m - 1; }
    if (idx === lastNow) return;
    lastNow = idx;
    const old = $("#tpBody .w.now", root); if (old) old.classList.remove("now");
    const el = idx >= 0 && $(`#tpBody [data-i="${idx}"]`, root);
    if (el) {
      el.classList.add("now");
      if (playing) {
        const body = $("#tpBody", root), r = el.getBoundingClientRect(), b = body.getBoundingClientRect();
        if (r.top < b.top + 30 || r.bottom > b.bottom - 30) el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }
  }

  return { init, show, sync, tick, hasFloating: () => !!$("#floating"), get pid() { return P && P.id; } };
})();
