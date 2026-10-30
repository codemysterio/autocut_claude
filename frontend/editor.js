"use strict";
// The timeline editor: media bin, program monitor, free-form timeline with waveforms, playback across clips and gaps.
const NLE = (() => {
  const DPR = Math.min(2, window.devicePixelRatio || 1);
  const MARKER_COLORS = { red: "#ff5a5a", blue: "#5aa9ff", green: "#4fbf84", yellow: "#ffd24a", purple: "#b07cff" };
  let trackH = 72, layerH = 52, scale = 1;
  let S = null, sel = null, selLayer = null, selMarker = null, pps = 40, pending = null, snapOn = true;
  let sc, content, trackV, trackA, ruler, rulerWrap, playhead, dropmark, stageMsg, vids, layerRows, tlHeads, nleEl, frameEl, ovlBox, vRowsEl;
  let FL = [], total = 0, layoutSig = "", layersSig = "", itemsSig = "";
  let audioCtx = null, rowOrder = [], vRowOrder = [];
  const clipEls = {}, mediaData = {}, layerEls = {}, players = {};   // layerEls: layer id -> [{el, piece | ghost, clip}]
  let cur = 0, segI = -1, seqT = 0, playing = false, token = 0, prepared = -1, lastPlayClip = null;
  let speed = 1, reverse = 0, revTimer = null;
  let rafId = null, pollTimer = null, stepTimer = null, lastStep = 0, drawQueued = false, binDragging = false;
  let ver = -1, keysBound = false;
  const hist = [], redoStack = [];   // what Ctrl+Z undoes: timeline edits and transcript cuts, newest last

  const mediaUrl = (pid) => `/api/projects/${pid}/media`;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const fpsOf = () => (S && S.settings.has_video ? S.settings.fps_num / S.settings.fps_den : 30);
  const clipById = (id) => S.clips.find((c) => c.id === id);
  const layerLabel = (l) => `A${l.row + 1}`;

  // ================================================================= open / close
  async function open(sid) {
    S = await api("/sequences/" + sid);
    ver = S.version;
    hist.length = redoStack.length = 0;
    snapOn = localStorage.getItem("autocut-snap") !== "0";
    build();
    $("#crumb").textContent = "/ " + S.name;
    applySequence(S, true);
    fit();
    rafId = requestAnimationFrame(tick);
    stepTimer = setInterval(() => { if (performance.now() - lastStep > 200) step(); }, 100);
    pollTimer = setInterval(poll, 900);
    if (pending) { const f = pending; pending = null; importFiles(f); }
  }

  function close() {
    clearInterval(pollTimer); clearInterval(stepTimer); clearInterval(revTimer); cancelAnimationFrame(rafId);
    pollTimer = stepTimer = revTimer = rafId = null;
    if (vids) vids.forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });
    S = null; sel = selLayer = selMarker = null; FL = []; playing = false; segI = -1; seqT = 0; speed = 1; reverse = 0;
    layoutSig = layersSig = itemsSig = ""; prepared = -1; lastPlayClip = null;
    for (const k of Object.keys(clipEls)) delete clipEls[k];
    for (const k of Object.keys(layerEls)) delete layerEls[k];
    for (const [k, p] of Object.entries(players)) { p.el.pause(); p.el.removeAttribute("src"); if (p.pic) p.el.remove(); delete players[k]; }
  }

  async function poll() {
    if (!S) return;
    try {
      const v = await api(`/sequences/${S.id}/version`);
      if (v.version !== ver || S.export.state === "running") await refresh();
    } catch {}
  }

  async function refresh() {
    if (!S) return;
    const s = await api(`/sequences/${S.id}`);
    ver = s.version;
    applySequence(s);
  }

  // A timeline edit that the server can undo: remember it for Ctrl+Z, then show the new state.
  function commit(s) {
    hist.push({ k: "seq" });
    redoStack.length = 0;
    applySequence(s);
    syncLayerAudio(true);
  }

  async function undo() {
    const e = hist.pop();
    if (!e) return toast("Nothing to undo");
    pause();
    const r = await api(e.k === "seq" ? `/sequences/${S.id}/undo` : `/projects/${e.pid}/undo`, "POST");
    if (!r.ok) return toast("Nothing to undo");
    redoStack.push(e);
    if (e.k === "seq") applySequence(r.sequence); else await refresh();
    syncLayerAudio(true);
  }

  async function redo() {
    const e = redoStack.pop();
    if (!e) return toast("Nothing to redo");
    pause();
    const r = await api(e.k === "seq" ? `/sequences/${S.id}/redo` : `/projects/${e.pid}/redo`, "POST");
    if (!r.ok) return toast("Nothing to redo");
    hist.push(e);
    if (e.k === "seq") applySequence(r.sequence); else await refresh();
    syncLayerAudio(true);
  }

  // ================================================================= DOM
  function build() {
    const lang = localStorage.getItem("autocut-lang") || "auto";
    const model = localStorage.getItem("autocut-model") || cfg.default_model;
    scale = parseFloat(localStorage.getItem("autocut-trackscale") || "1");
    trackH = Math.round(72 * scale); layerH = Math.round(52 * scale);
    app.innerHTML = `
    <div class="nle">
      <div class="nle-top">
        <section class="panel" id="bin">
          <div class="ph"><b>Media</b><span class="sp"></span>
            <select id="lang" title="Language of the speech">${Object.entries(cfg.languages).map(([k, v]) => `<option value="${k}" ${k === lang ? "selected" : ""}>${v}</option>`).join("")}</select>
            <select id="model" title="Whisper model">${cfg.models.map((m) => `<option ${m === model ? "selected" : ""}>${m}</option>`).join("")}</select></div>
          <div class="bin-actions">
            <button class="primary small" id="import" title="Pick files with the Windows file dialog - nothing is copied">Import files…</button>
            <button class="small" id="importPath" title="Link the original file (best for Premiere export)">Import by path…</button>
            <button class="small" id="addExisting">Add existing…</button>
            <input type="file" id="file" multiple hidden accept="video/*,audio/*,.mkv,.mov,.mts">
          </div>
          <div id="upStat" class="hint" style="padding:6px 10px;display:none"></div>
          <div class="bin-list" id="binList"></div>
        </section>
        <section class="panel">
          <div class="ph"><b>Program</b><span class="hint" id="monInfo"></span></div>
          <div class="stage" id="stage"><div class="frame" id="frame"><video id="vA" playsinline></video><video id="vB" playsinline></video><div class="ovl-box" id="ovlBox" hidden><i></i></div></div><div class="stage-msg" id="stageMsg"></div></div>
          <div class="transport">
            <button id="tStart" title="Go to start (Home)">⏮</button><button id="tPlay" title="Play / pause (Space)">▶</button><button id="tEnd" title="Go to end (End)">⏭</button>
            <span class="tcbig" id="tc">00:00:00.0</span><span class="hint" id="speedTag"></span><span class="sp"></span><span class="hint" id="tcTotal"></span>
          </div>
        </section>
        <section class="panel" id="tpanel">
          <div class="tabs"><button class="tab on" data-tab="tp">Transcript</button><button class="tab" data-tab="lp">Layers <span id="layerCount"></span></button></div>
          <div class="pane" id="tpane"></div><div class="pane" id="lpane" hidden></div>
        </section>
      </div>
      <div class="splitter" id="splitter" title="Drag to resize the timeline"></div>
      <section class="panel timeline">
        <div class="tl-bar">
          <span class="tl-title" id="seqTitle" title="Double-click to rename"></span>
          <span class="hint" id="tlInfo"></span>
          <span class="tools">
            <button class="small" id="bSplit" title="Cut the clip at the playhead (C)">✂ Split</button>
            <button class="small" id="bDelete" title="Delete the selected clip, leaving a gap (Delete)">Delete</button>
            <button class="small" id="bRipple" title="Delete and close the gap (Shift+Delete)">Ripple delete</button>
            <button class="small" id="bGaps" title="Slide all clips together, closing every gap">Close gaps</button>
            <button class="small" id="bMarker" title="Add a marker at the playhead (M)">⚑ Marker</button>
            <button class="small toggle" id="bSnap" title="Snap to clip edges, markers and the playhead (S)">🧲 Snap</button>
            <button class="small" id="bUndo" title="Undo (Ctrl+Z)">↶</button><button class="small" id="bRedo" title="Redo (Ctrl+Y)">↷</button>
          </span>
          <span class="sp"></span>
          <button class="small" id="bHeight" title="Track height">↕</button>
          <span class="hint">Zoom</span><input type="range" id="zoom" min="0" max="100" step="0.5">
          <button class="small" id="fit">Fit</button>
          <span style="width:8px"></span>
          <button class="primary small" data-x="xml">Premiere XML</button>
          <button class="small" data-x="edl">EDL</button><button class="small" data-x="srt">SRT</button>
          <button class="small" data-x="media">Render</button>
          <span class="hint" id="expStat"></span>
        </div>
        <div class="tl-main">
          <div class="tl-heads" id="tlHeads"></div>
          <div class="tl-scroll" id="tlScroll"><div class="tl-content" id="tlContent">
            <div class="ruler-wrap" id="rulerWrap"><canvas id="ruler"></canvas></div>
            <div id="vRows"></div><div class="track" id="trackV"></div><div class="track" id="trackA"></div><div id="layerRows"></div>
            <div class="tl-hint" id="tlHint">Drag media here from the Media panel, or import files.</div>
            <div class="playhead" id="playhead"></div><div class="dropmark" id="dropmark"></div>
          </div></div>
        </div>
      </section>
    </div>`;
    nleEl = $(".nle");
    sc = $("#tlScroll"); content = $("#tlContent"); trackV = $("#trackV"); trackA = $("#trackA");
    ruler = $("#ruler"); rulerWrap = $("#rulerWrap"); playhead = $("#playhead"); dropmark = $("#dropmark");
    stageMsg = $("#stageMsg"); vids = [$("#vA"), $("#vB")];
    layerRows = $("#layerRows"); tlHeads = $("#tlHeads"); vRowsEl = $("#vRows");
    frameEl = $("#frame"); ovlBox = $("#ovlBox");
    cur = 0; vids[1].style.visibility = "hidden";
    new ResizeObserver(() => fitFrame()).observe($("#stage"));
    ovlBox.addEventListener("mousedown", boxDown);
    applySizes();
    const tlh = parseInt(localStorage.getItem("autocut-tlh") || "300", 10);
    nleEl.style.setProperty("--tl-h", clamp(tlh, 150, innerHeight - 200) + "px");

    TP.init($("#tpane"), {
      onChange: (info) => { if (!(info && info.history === false)) { hist.push({ k: "proj", pid: TP.pid }); redoStack.length = 0; } return refresh(); },
      seek: seekSource, undo, redo,
    });
    LP.init($("#lpane"), {
      seq: () => S, clipId: () => sel, layerId: () => selLayer, time: () => seqT,
      apply: (s) => commit(s), refresh, selectLayer, local: localLayerPatch, layerTime: layerTimeAtPlayhead,
      setCount: (n) => ($("#layerCount").textContent = n ? `(${n})` : ""),
    });
    app.querySelectorAll(".tab").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

    $("#lang").onchange = (e) => localStorage.setItem("autocut-lang", e.target.value);
    $("#model").onchange = (e) => localStorage.setItem("autocut-model", e.target.value);
    $("#import").onclick = guard(pickFiles);
    $("#file").onchange = (e) => { importFiles([...e.target.files]); e.target.value = ""; };
    $("#importPath").onclick = importByPath;
    $("#addExisting").onclick = guard(addExisting);
    $("#tPlay").onclick = () => (playing ? pause() : play());
    $("#tStart").onclick = () => goto(0);
    $("#tEnd").onclick = () => goto(total);
    $("#zoom").oninput = (e) => setZoom(Math.pow(300, e.target.value / 100), sc.clientWidth / 2 + sc.getBoundingClientRect().left, true);
    $("#fit").onclick = fit;
    $("#bSplit").onclick = guard(splitAtPlayhead);
    $("#bDelete").onclick = guard(() => deleteSelected(false));
    $("#bRipple").onclick = guard(() => deleteSelected(true));
    $("#bGaps").onclick = guard(async () => { commit(await api(`/sequences/${S.id}/close-gaps`, "POST")); });
    $("#bMarker").onclick = guard(addMarker);
    $("#bSnap").onclick = toggleSnap;
    $("#bUndo").onclick = guard(undo);
    $("#bRedo").onclick = guard(redo);
    $("#bHeight").onclick = () => {
      scale = scale >= 1.4 ? 0.7 : scale >= 1 ? 1.4 : 1;
      localStorage.setItem("autocut-trackscale", scale);
      trackH = Math.round(72 * scale); layerH = Math.round(52 * scale);
      applySizes(); layoutPositions();
    };
    $("#seqTitle").ondblclick = guard(async () => {
      const n = prompt("Sequence name:", S.name);
      if (n && n.trim()) { await api("/sequences/" + S.id, "PATCH", { name: n }); $("#crumb").textContent = "/ " + n.trim(); await refresh(); }
    });
    app.querySelectorAll("[data-x]").forEach((b) => (b.onclick = guard(() => doExport(b.dataset.x))));
    tlHeads.addEventListener("click", guard(onHeadClick));
    setupSplitter();

    sc.addEventListener("scroll", () => { drawRuler(); scheduleDraw(); });
    sc.addEventListener("wheel", (e) => {
      if (e.ctrlKey) { e.preventDefault(); setZoom(pps * (e.deltaY < 0 ? 1.25 : 0.8), e.clientX); }
      else if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && !e.shiftKey) { e.preventDefault(); sc.scrollLeft += e.deltaY; }
    }, { passive: false });
    window.addEventListener("resize", onResize);
    rulerWrap.addEventListener("mousedown", rulerDown);
    rulerWrap.addEventListener("dblclick", guard(renameMarkerAt));
    content.addEventListener("mousedown", (e) => { if (e.target === content || e.target.classList.contains("track")) { select(null); selMarker = null; scrub(e); } });
    setupDnD();
    if (!keysBound) { document.addEventListener("keydown", onKey); keysBound = true; }
    updateSnapButton();
  }

  // The frame is the sequence picture (its aspect), centred in the monitor; pictures of layers are placed inside it.
  function fitFrame() {
    if (!S || !frameEl) return;
    const st = $("#stage"), ar = S.settings.width / S.settings.height;
    let w = st.clientWidth, h = w / ar;
    if (h > st.clientHeight) { h = st.clientHeight; w = h * ar; }
    frameEl.style.width = Math.floor(w) + "px"; frameEl.style.height = Math.floor(h) + "px";
    updatePicBox();
  }

  const picFrac = (l) => {  // a layer picture's width and height as fractions of the frame
    const W = S.settings.width, H = S.settings.height, lw = l.media.width || 16, lh = l.media.height || 9;
    return { w: l.v_scale, h: l.v_scale * (lh / lw) * (W / H) };
  };

  // The yellow box on the monitor that moves and resizes the selected layer's picture.
  function updatePicBox() {
    if (!ovlBox) return;
    const l = selLayer && layerById(selLayer);
    const show = !!l && l.media.has_video && l.v_on && videoRowVisible(l);
    ovlBox.hidden = !show;
    if (!show) return;
    const f = picFrac(l);
    ovlBox.style.left = l.v_x * 100 + "%"; ovlBox.style.top = l.v_y * 100 + "%";
    ovlBox.style.width = f.w * 100 + "%"; ovlBox.style.height = f.h * 100 + "%";
  }

  function boxDown(e) {
    const l = selLayer && layerById(selLayer);
    if (!l || e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    const resize = e.target.tagName === "I", r = frameEl.getBoundingClientRect();
    const x0 = e.clientX, y0 = e.clientY, start = { x: l.v_x, y: l.v_y, s: l.v_scale };
    const move = (ev) => {
      const dx = (ev.clientX - x0) / r.width, dy = (ev.clientY - y0) / r.height;
      if (resize) localLayerPatch(l.id, { v_scale: clamp(start.s + dx, 0.05, 1) });
      else localLayerPatch(l.id, { v_x: start.x + dx, v_y: start.y + dy });
    };
    const up = async () => {
      removeEventListener("mousemove", move); removeEventListener("mouseup", up);
      await guard(async () => {
        commit(await api(`/sequences/${S.id}/layers/${l.id}`, "PATCH", { v_x: l.v_x, v_y: l.v_y, v_scale: l.v_scale }));
      })();
    };
    addEventListener("mousemove", move); addEventListener("mouseup", up);
  }

  const videoRowVisible = (l) => !(S.tracks[`V${l.v_row + 1}`] && S.tracks[`V${l.v_row + 1}`].mute);

  function applySizes() {
    nleEl.style.setProperty("--track-h", trackH + "px");
    nleEl.style.setProperty("--layer-h", layerH + "px");
  }

  function setupSplitter() {
    const bar = $("#splitter");
    bar.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const move = (ev) => {
        const h = clamp(innerHeight - ev.clientY - 6, 150, innerHeight - 200);
        nleEl.style.setProperty("--tl-h", h + "px");
        localStorage.setItem("autocut-tlh", String(Math.round(h)));
        drawRuler(); scheduleDraw();
      };
      const up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); };
      addEventListener("mousemove", move); addEventListener("mouseup", up);
    });
  }

  const onResize = () => { if (S) { drawRuler(); scheduleDraw(); fitFrame(); } };

  // ================================================================= sequence → UI
  const layerSig = (l) => [l.id, l.name, l.mute, l.pieces, l.offset, l.rate, l.start, l.row, l.v_on, l.v_row, l.sync.state, Math.round(l.sync.confidence * 100), l.media.proxy];

  function applySequence(s, first = false) {
    const anchor = first ? null : anchorNow();
    const wasPlaying = playing;
    S = s;
    for (const c of S.clips) {
      let acc = 0;
      c.cum = c.segments.map(([a, b]) => { const x = acc; acc += b - a; return x; });
    }
    total = S.duration;
    const sig = JSON.stringify(S.clips.map((c) => [c.id, c.start, c.length, c.segments, c.in_t, c.out_t]));
    const layoutChanged = sig !== layoutSig;
    layoutSig = sig;
    const lsig = JSON.stringify([S.clips.map((c) => c.layers.map(layerSig)), S.free_layers.map(layerSig), S.tracks]);
    const layersChanged = lsig !== layersSig;
    layersSig = lsig;
    buildFlat();
    $("#seqTitle").textContent = S.name;
    $("#tlInfo").textContent = `${S.clips.length} clip${S.clips.length === 1 ? "" : "s"} · ${fmt(total)} · ${S.settings.has_video ? `${S.settings.width}×${S.settings.height} ${(S.settings.fps_num / S.settings.fps_den).toFixed(2).replace(/\.?0+$/, "")} fps` : "audio only"}`;
    $("#tcTotal").textContent = "of " + fmtTC(total);
    const isig = JSON.stringify(S.items.map((i) => [i.id, i.state, i.progress, i.step, i.proxy, i.proxy_progress, i.name]));
    if (isig !== itemsSig && !binDragging) { itemsSig = isig; renderBin(); }
    if (layoutChanged || layersChanged) renderClips();
    if (sel && !clipById(sel)) select(null);
    if (selLayer && !layerById(selLayer)) selLayer = null;
    if (selMarker && !S.markers.find((m) => m.id === selMarker)) selMarker = null;
    TP.sync(S.items);
    LP.render();
    renderExport();
    drawRuler();
    fitFrame();
    if (layoutChanged && anchor) goto(locate(anchor), wasPlaying, true);
    else if (first) { seqT = 0; goto(0); }
    updateUI();
  }

  function renderBin() {
    const el = $("#binList");
    if (!S.items.length) { el.innerHTML = `<div class="empty-note">No media yet.<br>Import video or audio files, or drop them here.</div>`; }
    else el.innerHTML = S.items.map((i) => {
      const thumb = i.has_video ? `style="background-image:url(/api/projects/${i.id}/sprite.jpg);background-size:${i.sprite_count * 100}% 100%;background-position:0 0"` : "";
      const busy = i.state === "processing" || i.state === "created";
      const meta = i.state === "error" ? `<span style="color:var(--k-manual)" title="${esc(i.message)}">Transcription failed</span>`
        : busy ? `${esc(i.step || "Working…")}<div class="mini"><i style="width:${Math.round(i.progress * 100)}%"></i></div>`
        : i.proxy ? `${fmt(i.duration)} · ${i.kind}`
        : `${fmt(i.duration)} · ${i.kind} · preview ${Math.round((i.proxy_progress || 0) * 100)}%<div class="mini"><i style="width:${Math.round((i.proxy_progress || 0) * 100)}%"></i></div>`;
      return `<div class="bitem" draggable="true" data-pid="${i.id}" title="Drag onto the timeline · double-click to append">
        <div class="bthumb" ${thumb}>${i.has_video ? "" : "🎧"}</div>
        <div style="min-width:0"><div class="bname">${esc(i.name)}</div><div class="bmeta">${meta}</div></div>
        <div class="bbtn"><button title="Add to end of timeline" data-add="${i.id}">＋</button><button title="Remove from this sequence" data-rm="${i.id}">✕</button></div></div>`;
    }).join("");
    el.querySelectorAll(".bitem").forEach((b) => {
      b.ondragstart = (e) => { binDragging = true; e.dataTransfer.setData("text/autocut-pid", b.dataset.pid); e.dataTransfer.effectAllowed = "copy"; };
      b.ondragend = () => { binDragging = false; dropmark.style.display = "none"; };
      b.ondblclick = guard(() => addClip(b.dataset.pid));
    });
    el.querySelectorAll("[data-add]").forEach((b) => (b.onclick = guard((e) => { e.stopPropagation(); return addClip(b.dataset.add); })));
    el.querySelectorAll("[data-rm]").forEach((b) => (b.onclick = guard(async (e) => {
      e.stopPropagation();
      const it = S.items.find((i) => i.id === b.dataset.rm);
      if (!confirm(`Remove "${it.name}" from this sequence? Its clips on the timeline are removed too (the media and transcript are kept).`)) return;
      pause();
      commit(await api(`/sequences/${S.id}/items/${b.dataset.rm}`, "DELETE"));
    })));
  }

  // ----------------------------------------------------------------- timeline clips
  function mainToSeq(c, tm) {  // a time in the main file → sequence time, carrying on past the clip's start and end
    const segs = c.segments;
    if (!segs.length) return c.start;
    if (tm <= segs[0][0]) return c.start - (segs[0][0] - tm);
    let acc = c.start;
    for (const [a, b] of segs) {
      if (tm < a) return acc;                      // inside a cut: collapse to the cut
      if (tm <= b) return acc + (tm - a);
      acc += b - a;
    }
    return acc + (tm - segs[segs.length - 1][1]);
  }

  // Whole extent of a synced layer's file on the timeline, even where it runs past the clip (not audible there).
  function layerExtent(l, c) {
    return { xs: mainToSeq(c, (0 - l.offset) / l.rate), xe: mainToSeq(c, (l.media.duration - l.offset) / l.rate) };
  }

  function layerById(lid) {
    for (const c of S.clips) for (const l of c.layers) if (l.id === lid) return l;
    return S.free_layers.find((l) => l.id === lid) || null;
  }

  function isLocked(label) { return !!(S.tracks[label] && S.tracks[label].lock); }

  function audible(label) {
    const solo = Object.entries(S.tracks).some(([k, v]) => k.startsWith("A") && v.solo);
    const f = S.tracks[label] || {};
    return solo ? !!f.solo && !f.mute : !f.mute;
  }

  const videoVisible = () => !(S.tracks.V1 && S.tracks.V1.mute);

  function renderClips() {
    trackV.innerHTML = trackA.innerHTML = layerRows.innerHTML = vRowsEl.innerHTML = "";
    for (const k of Object.keys(clipEls)) delete clipEls[k];
    for (const k of Object.keys(layerEls)) delete layerEls[k];
    for (const c of S.clips) {
      if (c.length <= 0) continue;
      const e = (clipEls[c.id] = {});
      if (c.has_video) trackV.appendChild((e.v = mkClip(c, "v")));
      if (c.has_audio) trackA.appendChild((e.a = mkClip(c, "a")));
      ensureMedia(c);
    }
    // one row per audio row in use; clips' layers and music layers can share a row anywhere on the timeline
    const rows = new Set();
    for (const c of S.clips) for (const l of c.layers) if (l.media.has_audio) rows.add(l.row);   // sound rows only
    for (const l of S.free_layers) if (l.media.has_audio) rows.add(l.row);
    rowOrder = [...rows].sort((a, b) => a - b);
    const vrows = new Set();
    for (const c of S.clips) for (const l of c.layers) if (l.media.has_video) vrows.add(l.v_row);
    for (const l of S.free_layers) if (l.media.has_video) vrows.add(l.v_row);
    vRowOrder = [...vrows].sort((a, b) => a - b);
    const rowEl = {}, vRowEl = {};
    const head = (label, kind, buttons) => `<div class="head ${kind}" data-track="${label}"><b>${label}</b><span class="tb">${buttons}</span></div>`;
    const btn = (label, key, glyph, tip) => {
      const f = S.tracks[label] || {};
      return `<button data-tb="${key}" class="${f[key] ? "on" : ""}" title="${tip}">${glyph}</button>`;
    };
    let heads = `<div class="rh"></div>`;
    for (const r of [...vRowOrder].reverse()) {          // V3 above V2 above V1
      const label = `V${r + 1}`;
      heads += head(label, "l pv", btn(label, "mute", "👁", "Hide / show these pictures") + btn(label, "lock", "🔒", "Lock this track"));
      const row = document.createElement("div");
      row.className = "track l pv" + (!videoRowVisible({ v_row: r }) ? " muted" : "") + (isLocked(label) ? " locked" : "");
      vRowsEl.appendChild(row);
      vRowEl[r] = row;
    }
    heads += head("V1", "v", btn("V1", "mute", "👁", "Hide / show the picture") + btn("V1", "lock", "🔒", "Lock this track"));
    heads += head("A1", "a", btn("A1", "mute", "M", "Mute") + btn("A1", "solo", "S", "Solo") + btn("A1", "lock", "🔒", "Lock this track"));
    for (const r of rowOrder) {
      const label = `A${r + 1}`;
      heads += head(label, "l", btn(label, "mute", "M", "Mute") + btn(label, "solo", "S", "Solo") + btn(label, "lock", "🔒", "Lock this track"));
      const row = document.createElement("div");
      row.className = "track l" + (!audible(label) ? " muted" : "") + (isLocked(label) ? " locked" : "");
      row.dataset.row = r;
      layerRows.appendChild(row);
      rowEl[r] = row;
    }
    trackV.classList.toggle("muted", !videoVisible()); trackV.classList.toggle("locked", isLocked("V1"));
    trackA.classList.toggle("muted", !audible("A1")); trackA.classList.toggle("locked", isLocked("V1"));
    const addLayer = (l, clipId) => {
      layerEls[l.id] = [];
      const row = rowEl[l.row];
      if (l.media.has_video && vRowEl[l.v_row]) {         // the picture of the layer, on its own V row
        for (const p of l.pieces) {
          const el = mkLayerEl(l, clipId, "v");
          vRowEl[l.v_row].appendChild(el);
          layerEls[l.id].push({ el, piece: p, clip: clipId, pic: true });
        }
      }
      if (!l.media.has_audio) { ensureMedia({ pid: l.pid, has_audio: false, has_video: true }); return; }
      if (l.mode === "sync") {  // faint outline of the whole file, underneath the audible pieces
        const g = mkLayerEl(l, clipId);
        g.classList.add("ghost");
        g.firstChild.remove();
        row.appendChild(g);
        layerEls[l.id].push({ el: g, ghost: layerExtent(l, clipById(clipId)), clip: clipId });
      }
      for (const p of l.pieces) {
        const el = mkLayerEl(l, clipId);
        row.appendChild(el);
        layerEls[l.id].push({ el, piece: p, clip: clipId });
      }
      ensureMedia({ pid: l.pid, has_audio: l.media.has_audio, has_video: l.media.has_video });
    };
    for (const c of S.clips) for (const l of c.layers) addLayer(l, c.id);
    for (const l of S.free_layers) addLayer(l, null);
    tlHeads.innerHTML = heads;
    $("#tlHint").hidden = S.clips.length > 0;
    layoutPositions();
  }

  async function onHeadClick(e) {
    const b = e.target.closest("[data-tb]");
    if (!b) return;
    const label = b.closest("[data-track]").dataset.track, key = b.dataset.tb, f = S.tracks[label] || {};
    commit(await api(`/sequences/${S.id}/tracks/${label}`, "PATCH", { [key]: !f[key] }));
    syncLayerAudio(true);
  }

  function mkClip(c, type) {
    const d = document.createElement("div");
    d.className = `clip ${type}` + (c.id === sel ? " sel" : "");
    d.dataset.id = c.id;
    d.innerHTML = `<canvas></canvas><span class="cl-name">${esc(c.name)}</span><i class="trim l" data-edge="in"></i><i class="trim r" data-edge="out"></i>`;
    d.addEventListener("mousedown", (e) => (e.target.dataset.edge ? trimDown(e, c.id, e.target.dataset.edge) : clipDown(e, c.id)));
    return d;
  }

  function mkLayerEl(l, clipId, kind = "a") {
    const d = document.createElement("div");
    const st = l.sync.state;
    d.className = "clip l" + (kind === "v" ? " pv" : "") + (l.id === selLayer ? " sel" : "") + (l.mute ? " muted" : "") + (st === "low" || st === "failed" ? " warn" : "");
    d.dataset.lid = l.id;
    const tag = l.mode === "sync" ? (st === "ok" || st === "low" ? ` · ${Math.round(l.sync.confidence * 100)}%` : st === "failed" ? " · not synced" : " · matching…") : "";
    d.innerHTML = `<canvas></canvas><span class="cl-name">${esc(l.name)}${tag}</span>`;
    d.addEventListener("mousedown", (e) => layerDown(e, l.id, clipId, kind));
    return d;
  }

  function layoutPositions() {
    for (const c of S.clips) {
      const e = clipEls[c.id];
      if (!e) continue;
      for (const el of [e.v, e.a]) if (el) { el.style.left = c.start * pps + "px"; el.style.width = Math.max(2, c.length * pps) + "px"; }
    }
    for (const list of Object.values(layerEls)) {
      for (const { el, piece, ghost } of list) {
        if (ghost) {
          const a = Math.max(0, ghost.xs), b = Math.min(ghost.xe, total + 10);
          el.style.display = b - a > 0.02 ? "" : "none";
          el.style.left = a * pps + "px"; el.style.width = Math.max(2, (b - a) * pps) + "px";
        } else { el.style.left = piece.seq0 * pps + "px"; el.style.width = Math.max(2, (piece.seq1 - piece.seq0) * pps) + "px"; }
      }
    }
    content.style.width = Math.max(sc.clientWidth, total * pps + 160) + "px";
    $("#zoom").value = (Math.log(clamp(pps, 1, 300)) / Math.log(300)) * 100;
    drawRuler(); scheduleDraw(); updateUI();
  }

  function ensureMedia(c) {
    const m = mediaData[c.pid] || (mediaData[c.pid] = { tries: 0 });
    if (c.has_audio && !m.peaks && !m.loadingPeaks && m.tries < 4) {
      m.loadingPeaks = true;
      fetch(`/api/projects/${c.pid}/peaks`).then((r) => (r.ok ? r.json() : Promise.reject())).then((j) => {
        m.peaks = j.peaks; m.rate = j.rate; m.loadingPeaks = false; scheduleDraw();
      }).catch(() => { m.loadingPeaks = false; m.tries++; setTimeout(() => S && ensureMedia(c), 4000); });
    }
    if (c.has_video && !m.sprite && !m.loadingSprite && m.tries < 4) {
      m.loadingSprite = true;
      const img = new Image();
      img.onload = () => { m.sprite = img; m.loadingSprite = false; scheduleDraw(); };
      img.onerror = () => { m.loadingSprite = false; m.tries++; setTimeout(() => S && ensureMedia(c), 4000); };
      img.src = `/api/projects/${c.pid}/sprite.jpg`;
    }
  }

  // ----------------------------------------------------------------- drawing
  const scheduleDraw = () => { if (!drawQueued) { drawQueued = true; requestAnimationFrame(() => { drawQueued = false; if (S) { drawVisible(); drawLayers(); } }); } };

  function srcAt(c, t) {  // clip-relative seconds → source seconds
    let i = 0;
    while (i < c.segments.length - 1 && t >= c.cum[i] + (c.segments[i][1] - c.segments[i][0])) i++;
    const [s] = c.segments[i];
    return s + (t - c.cum[i]);
  }

  function drawVisible() {
    const left = sc.scrollLeft, vw = sc.clientWidth;
    for (const c of S.clips) {
      const e = clipEls[c.id];
      if (!e) continue;
      const x0 = c.start * pps, x1 = x0 + Math.max(2, c.length * pps);
      const v0 = Math.max(x0, left - 150), v1 = Math.min(x1, left + vw + 150);
      for (const [el, kind] of [[e.v, "v"], [e.a, "a"]]) {
        if (!el) continue;
        const cv = el.firstChild;
        if (v1 <= v0) { cv.style.display = "none"; continue; }
        const w = Math.ceil(v1 - v0), h = trackH - 8, off = Math.round(v0 - x0);
        cv.style.display = ""; cv.style.left = off + "px"; cv.style.width = w + "px";
        cv.width = w * DPR; cv.height = h * DPR;
        const ctx = cv.getContext("2d");
        ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
        if (kind === "v") drawThumbs(ctx, c, off, w, h); else drawWave(ctx, c, off, w, h);
        ctx.fillStyle = "rgba(255,255,255,.7)";  // markers where transcript cuts were removed
        for (let k = 1; k < c.cum.length; k++) { const x = c.cum[k] * pps - off; if (x >= 0 && x < w) ctx.fillRect(Math.floor(x), 0, 1, h); }
      }
    }
  }

  function drawLayers() {
    const left = sc.scrollLeft, vw = sc.clientWidth;
    for (const [lid, list] of Object.entries(layerEls)) {
      const l = layerById(lid);
      if (!l) continue;
      for (const { el, piece, pic } of list) {
        if (!piece) continue;                                            // outlines have no waveform
        const x0 = piece.seq0 * pps, x1 = x0 + Math.max(2, (piece.seq1 - piece.seq0) * pps);
        const v0 = Math.max(x0, left - 150), v1 = Math.min(x1, left + vw + 150), cv = el.firstChild;
        if (v1 <= v0) { cv.style.display = "none"; continue; }
        const w = Math.ceil(v1 - v0), h = layerH - 8, off = Math.round(v0 - x0);
        cv.style.display = ""; cv.style.left = off + "px"; cv.style.width = w + "px";
        cv.width = w * DPR; cv.height = h * DPR;
        const ctx = cv.getContext("2d");
        ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
        const m = mediaData[l.pid], pk = m && m.peaks;
        if (pic) {                                                       // filmstrip of the layer's picture
          if (!m || !m.sprite) continue;
          const count = l.media.sprite_count || 2, tw = Math.round((h * 16) / 9), k0 = (piece.tl1 - piece.tl0) / (piece.seq1 - piece.seq0);
          for (let k = Math.floor(off / tw); k * tw < off + w; k++) {
            const tl = piece.tl0 + ((k * tw + tw / 2) / pps) * k0;
            const idx = clamp(Math.floor((tl / (l.media.duration || 1)) * count), 0, count - 1);
            ctx.drawImage(m.sprite, idx * 160, 0, 160, 90, k * tw - off, 0, tw, h);
          }
          continue;
        }
        if (!pk) { ctx.fillStyle = "rgba(255,255,255,.5)"; ctx.font = "11px sans-serif"; ctx.fillText("loading waveform…", 8, h / 2 + 4); continue; }
        const rate = m.rate || 100, k = (piece.tl1 - piece.tl0) / (piece.seq1 - piece.seq0), mid = h / 2;
        ctx.fillStyle = l.mute ? "#7d8a85" : "#8fe7ff";
        for (let x = 0; x < w; x++) {
          const tl = piece.tl0 + ((off + x) / pps) * k;
          const i0 = Math.floor(tl * rate), i1 = Math.max(i0 + 1, Math.ceil((tl + (k / pps)) * rate));
          let mx = 0;
          for (let i = i0; i < i1 && i < pk.length; i++) if (pk[i] > mx) mx = pk[i];
          const hh = Math.max(1, (mx / 255) * (h - 6));
          ctx.fillRect(x, mid - hh / 2, 1, hh);
        }
      }
    }
  }

  function drawWave(ctx, c, off, w, h) {
    const m = mediaData[c.pid];
    if (!m || !m.peaks) { ctx.fillStyle = "rgba(255,255,255,.5)"; ctx.font = "11px sans-serif"; ctx.fillText("loading waveform…", 8, h / 2 + 4); return; }
    const pk = m.peaks, rate = m.rate || 100, mid = h / 2, segs = c.segments;
    ctx.fillStyle = "#5fe0b2";
    let si = 0;
    for (let x = 0; x < w; x++) {
      const t = (off + x) / pps;
      while (si < segs.length - 1 && t >= c.cum[si] + (segs[si][1] - segs[si][0])) si++;
      const src = segs[si][0] + (t - c.cum[si]);
      const i0 = Math.floor(src * rate), i1 = Math.max(i0 + 1, Math.ceil((src + 1 / pps) * rate));
      let mx = 0;
      for (let i = i0; i < i1 && i < pk.length; i++) if (pk[i] > mx) mx = pk[i];
      const hh = Math.max(1, (mx / 255) * (h - 6));
      ctx.fillRect(x, mid - hh / 2, 1, hh);
    }
  }

  function drawThumbs(ctx, c, off, w, h) {
    const m = mediaData[c.pid];
    if (!m || !m.sprite) return;
    const item = S.items.find((i) => i.id === c.pid);
    const count = item ? item.sprite_count : 2, tw = Math.round((h * 16) / 9);
    for (let k = Math.floor(off / tw); k * tw < off + w; k++) {
      const t = clamp((k * tw + tw / 2) / pps, 0, Math.max(0, c.length - 0.001));
      const idx = clamp(Math.floor((srcAt(c, t) / c.source_duration) * count), 0, count - 1);
      ctx.drawImage(m.sprite, idx * 160, 0, 160, 90, k * tw - off, 0, tw, h);
    }
  }

  function drawRuler() {
    if (!ruler || !S) return;
    const vw = sc.clientWidth, h = 26, left = sc.scrollLeft;
    rulerWrap.style.width = vw + "px";
    ruler.width = vw * DPR; ruler.height = h * DPR; ruler.style.width = vw + "px"; ruler.style.height = h + "px";
    const ctx = ruler.getContext("2d");
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.fillStyle = "#8b8b93"; ctx.strokeStyle = "#55555c"; ctx.font = "10px sans-serif"; ctx.textBaseline = "top";
    const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    const major = steps.find((s) => s * pps >= 90) || 3600, minor = major / 5;
    for (let t = Math.floor(left / pps / minor) * minor; t * pps - left < vw + 50; t += minor) {
      const x = Math.round(t * pps - left) + 0.5, isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
      ctx.beginPath(); ctx.moveTo(x, isMajor ? 12 : 19); ctx.lineTo(x, h); ctx.stroke();
      if (isMajor) { ctx.fillStyle = "#8b8b93"; ctx.fillText(major < 1 ? t.toFixed(1) + "s" : fmt(t), x + 3, 2); }
    }
    for (const m of S.markers) {            // flags
      const x = Math.round(m.t * pps - left) + 0.5;
      if (x < -100 || x > vw + 10) continue;
      ctx.fillStyle = MARKER_COLORS[m.color] || MARKER_COLORS.red;
      ctx.beginPath(); ctx.moveTo(x, h - 1); ctx.lineTo(x - 5, h - 11); ctx.lineTo(x - 5, 1); ctx.lineTo(x + 5, 1); ctx.lineTo(x + 5, h - 11); ctx.closePath(); ctx.fill();
      if (m.id === selMarker) { ctx.strokeStyle = "#fff"; ctx.stroke(); ctx.strokeStyle = "#55555c"; }
      if (m.name) { ctx.fillStyle = "#fff"; ctx.fillText(m.name, x + 8, 2); }
    }
  }

  // ================================================================= selection
  function showTab(name) {
    app.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    $("#tpane").hidden = name !== "tp";
    $("#lpane").hidden = name !== "lp";
    if (name === "lp") LP.render(true);
  }

  function selectLayer(lid, fromPanel = false) {
    for (const c of S.clips) if (c.layers.some((l) => l.id === lid) && c.id !== sel) select(c.id);
    selLayer = lid;                      // after select(), which clears the layer selection
    document.querySelectorAll(".clip.sel").forEach((e) => e.classList.remove("sel"));
    for (const { el } of layerEls[lid] || []) el.classList.add("sel");
    if (!fromPanel) showTab("lp");
    updatePicBox();
    // A press inside the Layers tab must not rebuild it: the buttons would be replaced before the click lands.
    if (fromPanel) LP.mark(lid); else LP.render(true);
  }

  function select(id, fromPlayhead = false) {
    if (id !== sel) selLayer = null;
    sel = id;
    updatePicBox();
    document.querySelectorAll(".clip.sel").forEach((e) => e.classList.remove("sel"));
    if (id && clipEls[id]) for (const el of [clipEls[id].v, clipEls[id].a]) if (el) el.classList.add("sel");
    const c = id && clipById(id);
    TP.show(c ? c.pid : null).catch(() => {});
    $("#monInfo").textContent = c ? c.name : "";
    LP.render();
  }

  // ================================================================= layer audio (external mics, music)
  const dbToLin = (db) => Math.pow(10, db / 20);

  function layerHit(l, t) {  // where in the layer's own file sequence time t falls, if it is audible there
    for (const p of l.pieces) {
      if (t >= p.seq0 && t < p.seq1) return p.tl0 + ((t - p.seq0) * (p.tl1 - p.tl0)) / (p.seq1 - p.seq0);
    }
    return null;
  }

  function fadeFactor(l, t) {
    if (!l.pieces.length) return 0;
    const a = l.pieces[0].seq0, b = l.pieces[l.pieces.length - 1].seq1;
    let g = 1;
    if (l.fade_in > 0) g = Math.min(g, (t - a) / l.fade_in);
    if (l.fade_out > 0) g = Math.min(g, (b - t) / l.fade_out);
    return clamp(g, 0, 1);
  }

  function playerFor(l) {
    let p = players[l.id];
    if (p) return p;
    const pic = !!l.media.has_video;
    const el = pic ? document.createElement("video") : new Audio();
    if (pic) { el.className = "ovl"; el.playsInline = true; frameEl.insertBefore(el, ovlBox); }
    el.preload = "auto"; el.src = mediaUrl(l.pid);
    p = players[l.id] = { el, gain: null, pic };
    if (audioCtx) {  // Web Audio so volume can go above 0 dB like the render does
      p.gain = audioCtx.createGain();
      audioCtx.createMediaElementSource(el).connect(p.gain).connect(audioCtx.destination);
    }
    return p;
  }

  function setGain(p, g) {
    if (p.gain) p.gain.gain.value = g; else p.el.volume = clamp(g, 0, 1);
  }

  function syncLayerAudio(forceSeek = false) {
    if (!S) return;
    const all = [...S.clips.flatMap((c) => c.layers), ...S.free_layers];
    for (const l of all) {
      const hasA = l.media.has_audio, hasV = l.media.has_video;
      if (!hasA && !hasV) continue;
      const sound = hasA && !l.mute && audible(layerLabel(l));
      const picture = hasV && l.v_on && videoRowVisible(l);
      const tl = (sound || picture) && l.pieces.length ? layerHit(l, seqT) : null;
      const p = tl == null ? players[l.id] : playerFor(l);
      if (!p) continue;
      if (tl == null) { if (!p.el.paused) p.el.pause(); if (p.pic) p.el.style.display = "none"; continue; }
      const err = p.el.currentTime - tl, base = (l.mode === "sync" ? clamp(l.rate, 0.5, 2) : 1) * speed;
      if (forceSeek || Math.abs(err) > (playing ? 0.25 : 0.03)) p.el.currentTime = tl;
      // small errors are pulled in by speeding up / slowing down a touch (no audible jump); big ones seek
      p.el.playbackRate = playing && Math.abs(err) > 0.012 ? base * (1 - clamp(err * 0.6, -0.05, 0.05)) : base;
      if (playing && !reverse && p.el.paused) p.el.play().catch(() => {});
      else if ((!playing || reverse) && !p.el.paused) p.el.pause();
      const fade = fadeFactor(l, seqT);
      setGain(p, sound ? dbToLin(l.gain_db) * fade : 0);
      if (p.pic) {  // the picture: where it sits in the frame, how see-through it is, faded with the sound
        p.el.style.display = picture ? "" : "none";
        p.el.style.left = l.v_x * 100 + "%"; p.el.style.top = l.v_y * 100 + "%";
        p.el.style.width = l.v_scale * 100 + "%"; p.el.style.opacity = String(l.v_opacity * fade);
        p.el.style.zIndex = String(2 + l.v_row);
      }
    }
    // a layer set to replace the camera's audio, or a muted A1, silences the main player for its clip
    const seg = FL[segI], clip = seg && clipById(seg.clip);
    const replaced = !!clip && clip.layers.some((l) => l.replace_main_audio && !l.mute && l.media.has_audio);
    vids.forEach((v) => (v.muted = replaced || !audible("A1") || reverse > 0));
  }

  function pauseLayers() { for (const p of Object.values(players)) p.el.pause(); }

  function localLayerPatch(lid, patch) {  // instant feedback while a slider is dragged; saved on release
    const l = layerById(lid);
    if (l) Object.assign(l, patch);
    syncLayerAudio();
    updatePicBox();
  }

  // The layer's own media time at the playhead (for "trim here" buttons).
  function layerTimeAtPlayhead(lid) {
    const l = layerById(lid);
    if (!l) return null;
    if (l.mode === "free") return l.trim_in + (seqT - l.start);
    const seg = FL[segI];
    if (!seg) return null;
    const owner = S.clips.find((c) => c.layers.some((x) => x.id === lid));
    if (!owner || owner.id !== seg.clip) return null;
    return l.rate * (seg.s0 + (seqT - seg.t0)) + l.offset;
  }

  // ================================================================= playback engine
  function buildFlat() {
    FL = [];
    for (const c of S.clips) {
      let acc = c.start;
      for (const [s, e] of c.segments) { FL.push({ clip: c.id, pid: c.pid, video: c.has_video, t0: acc, t1: acc + (e - s), s0: s, s1: e }); acc += e - s; }
    }
    FL.sort((a, b) => a.t0 - b.t0);
  }

  function segIndexAt(t) {  // the segment playing at t, or -1 in a gap / past the end
    let lo = 0, hi = FL.length - 1, r = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (FL[m].t0 <= t + 1e-9) { r = m; lo = m + 1; } else hi = m - 1; }
    return r >= 0 && t < FL[r].t1 - 1e-6 ? r : -1;
  }

  function nextSegAfter(t) {
    for (let i = 0; i < FL.length; i++) if (FL[i].t0 > t + 1e-6) return i;
    return -1;
  }

  const anchorNow = () => { const seg = FL[segI]; return seg ? { clip: seg.clip, src: seg.s0 + (seqT - seg.t0) } : null; };

  function locate(a) {  // (clip, source time) → sequence time, snapping out of removed parts
    const c = clipById(a.clip);
    if (!c) return clamp(seqT, 0, total);
    let acc = c.start;
    for (const [s, e] of c.segments) {
      if (a.src < s) return acc;
      if (a.src <= e) return acc + (a.src - s);
      acc += e - s;
    }
    return acc;
  }

  function loadAt(el, seg, t) {
    return new Promise((res) => {
      const req = (el._req = (el._req || 0) + 1);
      const done = () => { clearTimeout(timer); res(); };
      const timer = setTimeout(res, 2500);
      const apply = () => {
        if (el._req !== req) return done();
        if (Math.abs(el.currentTime - t) < 0.02) return done();
        el.addEventListener("seeked", done, { once: true });
        el.currentTime = t;
      };
      if (el.dataset.pid !== seg.pid) {
        el.dataset.pid = seg.pid;
        el.src = mediaUrl(seg.pid);
        el.addEventListener("loadedmetadata", apply, { once: true });
        el.addEventListener("error", done, { once: true });
      } else if (el.readyState >= 1) apply();
      else el.addEventListener("loadedmetadata", apply, { once: true });
    });
  }

  function show(i) {
    const seg = FL[segI];
    const hide = segI < 0 || (seg && !seg.video) || !videoVisible();
    vids.forEach((v, k) => (v.style.visibility = !hide && k === i ? "visible" : "hidden"));
    stageMsg.textContent = !FL.length ? "Import media and place it on the timeline." : seg && !seg.video ? "♪ audio" : "";
  }

  function stopReverse() { clearInterval(revTimer); revTimer = null; reverse = 0; }

  async function goto(t, play = false, keepShuttle = false) {
    const my = ++token;
    if (!keepShuttle) { stopReverse(); }
    t = clamp(t, 0, Math.max(total, 0));
    seqT = t; playing = false; vids.forEach((v) => v.pause()); pauseLayers();
    const i = FL.length ? segIndexAt(t) : -1;
    if (i < 0) {                       // an empty stretch (or nothing at all): black picture, the clock still runs
      segI = -1; show(cur);
      if (play && FL.length && t < total) playing = true;
      syncLayerAudio(true); updateUI();
      return;
    }
    segI = i;
    const seg = FL[i], el = vids[cur];
    await loadAt(el, seg, seg.s0 + clamp(t - seg.t0, 0, seg.s1 - seg.s0));
    if (my !== token) return;
    show(cur);
    el.playbackRate = speed;
    if (play) { playing = true; el.play().catch(() => (playing = false)); }
    syncLayerAudio(true);
    preloadNext();
    updateUI();
  }

  const play = () => {
    if (!FL.length) return;
    if (!audioCtx && (window.AudioContext || window.webkitAudioContext)) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    stopReverse();
    return goto(seqT >= total - 0.05 ? 0 : seqT, true);
  };
  function pause() { stopReverse(); speed = 1; playing = false; token++; vids.forEach((v) => { v.pause(); v.playbackRate = 1; }); pauseLayers(); updateUI(); }

  function preloadNext() {
    const n = segI + 1;
    if (segI >= 0 && n < FL.length && FL[n].pid !== FL[segI].pid && FL[n].t0 - FL[segI].t1 < 0.05) { loadAt(vids[1 - cur], FL[n], FL[n].s0); prepared = n; } else prepared = -1;
  }

  function advance() {
    const n = segI + 1, seg = FL[segI], el = vids[cur];
    if (n >= FL.length) { seqT = seg.t1; pause(); seqT = total; return; }
    const nx = FL[n];
    if (nx.t0 - seg.t1 > 0.03) {        // an empty stretch before the next clip
      el.pause(); segI = -1; seqT = seg.t1; show(cur); return;
    }
    if (nx.pid === seg.pid) { el.currentTime = nx.s0; segI = n; show(cur); preloadNext(); }
    else if (prepared === n) {
      const o = vids[1 - cur];
      o.playbackRate = speed;
      o.play().catch(() => {}); el.pause(); cur = 1 - cur; segI = n; show(cur); preloadNext();
    } else goto(nx.t0, true);
  }

  function tick() {
    rafId = requestAnimationFrame(tick);
    step();
  }

  // Driven by requestAnimationFrame; a timer takes over when the browser stops sending frames (hidden or covered window).
  function step() {
    const now = performance.now(), dt = Math.min(0.25, (now - lastStep) / 1000);
    lastStep = now;
    if (!S) return;
    if (playing && !reverse) {
      if (segI >= 0) {
        const seg = FL[segI], el = vids[cur], st = el.currentTime;
        seqT = clamp(seg.t0 + (st - seg.s0), 0, total);
        if (st >= seg.s1 - 0.03 || el.ended) advance();
      } else {                           // gap: nothing to show, the playhead just runs on
        seqT += dt * speed;
        const n = nextSegAfter(seqT - 0.001);
        if (n >= 0 && seqT >= FL[n].t0 - 0.005) goto(FL[n].t0, true, true);
        else if (seqT >= total) { seqT = total; pause(); }
      }
      syncLayerAudio();
    }
    updateUI();
  }

  function updateUI() {
    if (!S) return;
    $("#tc").textContent = fmtTC(seqT);
    $("#tPlay").textContent = playing || reverse ? "⏸" : "▶";
    $("#speedTag").textContent = reverse ? `◀◀ ${reverse}×` : playing && speed !== 1 ? `${speed}×` : "";
    const x = seqT * pps;
    playhead.style.transform = `translateX(${x}px)`;
    if (playing || reverse) {
      if (x > sc.scrollLeft + sc.clientWidth - 40 || x < sc.scrollLeft) sc.scrollLeft = Math.max(0, x - 80);
    }
    const seg = FL[segI];
    if (seg) {
      if (seg.clip !== lastPlayClip) { lastPlayClip = seg.clip; if (seg.clip !== sel) select(seg.clip, true); }
      TP.tick(seg.pid, seg.s0 + (seqT - seg.t0), playing);
    }
  }

  // ---- J / K / L shuttle: L plays forward (again = faster), J plays backwards by stepping, K stops
  function shuttleForward() {
    if (!FL.length) return;
    if (reverse) { stopReverse(); }
    if (playing) speed = Math.min(8, speed * 2);
    else speed = 1;
    if (!audioCtx && (window.AudioContext || window.webkitAudioContext)) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    const wasPlaying = playing;
    if (wasPlaying) { vids.forEach((v) => (v.playbackRate = speed)); syncLayerAudio(true); } else goto(seqT >= total - 0.05 ? 0 : seqT, true, true);
  }

  function shuttleBack() {
    if (!FL.length) return;
    const r = reverse ? Math.min(8, reverse * 2) : 1;
    pause();
    reverse = r;
    revTimer = setInterval(() => {
      if (seqT <= 0) { stopReverse(); return; }
      goto(Math.max(0, seqT - reverse * 0.12), false, true);
    }, 120);
  }

  function shuttleStop() { pause(); }

  function stepBy(sec) { pause(); goto(seqT + sec); }

  function editPoints() {
    const pts = new Set([0, total]);
    for (const c of S.clips) { pts.add(c.start); pts.add(c.start + c.length); }
    for (const m of S.markers) pts.add(m.t);
    return [...pts].sort((a, b) => a - b);
  }

  function jumpEdit(dir) {
    const pts = editPoints();
    const t = dir > 0 ? pts.find((p) => p > seqT + 0.01) : [...pts].reverse().find((p) => p < seqT - 0.01);
    if (t != null) { pause(); goto(t); }
  }

  // Jump to a time inside a media file (used by the transcript panel).
  function seekSource(pid, srcT) {
    const c = S.clips.find((x) => x.id === sel && x.pid === pid) || S.clips.find((x) => x.pid === pid);
    if (!c) return;
    if (sel !== c.id) select(c.id);
    goto(locate({ clip: c.id, src: srcT }), playing);
  }

  // ================================================================= snapping
  function snapPoints(excludeClip = null, excludeLayer = null) {
    const pts = [0, seqT];
    for (const m of S.markers) pts.push(m.t);
    for (const c of S.clips) {
      if (c.id !== excludeClip) { pts.push(c.start, c.start + c.length); }
      for (const l of c.layers) if (l.id !== excludeLayer) for (const p of l.pieces) pts.push(p.seq0, p.seq1);
    }
    for (const l of S.free_layers) if (l.id !== excludeLayer) for (const p of l.pieces) pts.push(p.seq0, p.seq1);
    return pts;
  }

  function snapNear(t, pts) {
    if (!snapOn) return { t, hit: false, d: Infinity };
    let best = { t, hit: false, d: 8 / pps };            // 8 pixels
    for (const p of pts) { const d = Math.abs(p - t); if (d < best.d) best = { t: p, hit: true, d }; }
    return best;
  }

  function toggleSnap() {
    snapOn = !snapOn;
    localStorage.setItem("autocut-snap", snapOn ? "1" : "0");
    updateSnapButton();
    toast(snapOn ? "Snapping on" : "Snapping off");
  }
  const updateSnapButton = () => $("#bSnap").classList.toggle("on", snapOn);

  function showSnapLine(t) {
    if (t == null) { dropmark.style.display = "none"; return; }
    dropmark.style.display = "block"; dropmark.style.left = t * pps + "px";
  }

  // ================================================================= timeline interaction
  const xToTime = (clientX) => (clientX - sc.getBoundingClientRect().left + sc.scrollLeft) / pps;

  function scrub(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    pause();
    const go = (ev) => { const sn = snapNear(xToTime(ev.clientX), snapPoints()); goto(sn.t, false); showSnapLine(sn.hit ? sn.t : null); };
    go(e);
    const move = (ev) => go(ev);
    const up = () => { showSnapLine(null); removeEventListener("mousemove", move); removeEventListener("mouseup", up); };
    addEventListener("mousemove", move); addEventListener("mouseup", up);
  }

  function markerNear(clientX) {
    const x = clientX - sc.getBoundingClientRect().left + sc.scrollLeft;
    return S.markers.find((m) => Math.abs(m.t * pps - x) < 7) || null;
  }

  function rulerDown(e) {
    if (e.button !== 0) return;
    const m = markerNear(e.clientX);
    if (m) { e.preventDefault(); selMarker = m.id; select(null); pause(); goto(m.t); drawRuler(); return; }
    selMarker = null;
    scrub(e);
  }

  async function renameMarkerAt(e) {
    const m = markerNear(e.clientX);
    if (!m) return;
    const name = prompt("Marker name:", m.name || "");
    if (name == null) return;
    commit(await api(`/sequences/${S.id}/markers/${m.id}`, "PATCH", { name }));
  }

  async function addMarker() {
    commit(await api(`/sequences/${S.id}/markers`, "POST", { time: seqT, name: "" }));
    toast(`Marker at ${fmtTC(seqT)}`);
  }

  function dragTip() {
    let t = $("#dragTip");
    if (!t) { t = document.createElement("div"); t.id = "dragTip"; t.className = "drag-tip"; t.hidden = true; document.body.appendChild(t); }
    return t;
  }

  const signed = (v) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(3)} s (${v >= 0 ? "+" : "−"}${Math.abs(v * fpsOf()).toFixed(1)} f)`;

  function layerDown(e, lid, clipId, kind = "a") {
    if (e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    const l = layerById(lid), startX = e.clientX, startY = e.clientY, fps = fpsOf();
    const els = (layerEls[lid] || []).map((x) => x.el);
    const pts = snapPoints(null, lid);
    const ext = (layerEls[lid] || []).filter((x) => x.piece).map((x) => x.piece);
    const first = ext.length ? ext[0].seq0 : 0, last = ext.length ? ext[ext.length - 1].seq1 : 0;
    const pv = kind === "v", vDesc = [...vRowOrder].reverse();     // picture rows are listed from the top: V3, V2
    const startIdx = pv ? Math.max(0, vDesc.indexOf(l.v_row)) : Math.max(0, rowOrder.indexOf(l.row));
    let dragging = false, dt = 0, newRow = pv ? l.v_row : l.row;
    const tip = dragTip();
    const move = (ev) => {
      const dx = ev.clientX - startX, dy = ev.clientY - startY;
      if (!dragging && Math.abs(dx) < 5 && Math.abs(dy) < 5) return;
      dragging = true;
      dt = ev.altKey ? dx / pps : Math.round((dx / pps) * fps) / fps;   // Alt: free, sub-frame moves
      let hit = null;
      if (!ev.altKey) {                                                  // snap the layer's start or end to a point
        const a = snapNear(first + dt, pts), b = snapNear(last + dt, pts);
        if (a.hit && (!b.hit || a.d <= b.d)) { dt = a.t - first; hit = a.t; } else if (b.hit) { dt = b.t - last; hit = b.t; }
      }
      showSnapLine(hit);
      let idx;
      if (pv) {                                                          // one row above the top = a new picture row
        idx = clamp(startIdx + Math.round(dy / layerH), -1, vDesc.length - 1);
        newRow = idx < 0 ? vDesc[0] + 1 : vDesc[idx];
      } else {                                                           // one row below the last = a new audio row
        idx = clamp(startIdx + Math.round(dy / layerH), 0, rowOrder.length);
        newRow = idx < rowOrder.length ? rowOrder[idx] : (rowOrder.length ? rowOrder[rowOrder.length - 1] + 1 : 1);
      }
      els.forEach((el) => { el.classList.add("dragging"); el.style.transform = `translate(${dt * pps}px, ${(idx - startIdx) * layerH}px)`; });
      tip.hidden = false;
      tip.style.left = ev.clientX + 14 + "px"; tip.style.top = ev.clientY - 28 + "px";
      const moved = newRow !== (pv ? l.v_row : l.row);
      tip.textContent = `${signed(dt)}${moved ? ` · track ${pv ? "V" : "A"}${newRow + 1}` : ""}${ev.altKey ? "" : " · Alt = fine"}`;
    };
    const up = async (ev) => {
      removeEventListener("mousemove", move); removeEventListener("mouseup", up);
      tip.hidden = true; showSnapLine(null);
      els.forEach((el) => { el.classList.remove("dragging"); el.style.transform = ""; });
      if (!dragging) { selectLayer(lid); pause(); goto(xToTime(ev.clientX), false); return; }
      if (isLocked(layerLabel(l))) return toast("That track is locked", true);
      const body = {};
      if (Math.abs(dt) > 1e-9) { if (l.mode === "sync") body.offset_delta = dt; else body.start = Math.max(0, l.start + dt); }
      if (pv) { if (newRow !== l.v_row) body.v_row = newRow; } else if (newRow !== l.row) body.row = newRow;
      if (!Object.keys(body).length) return;
      await guard(async () => { commit(await api(`/sequences/${S.id}/layers/${lid}`, "PATCH", body)); })();
    };
    addEventListener("mousemove", move); addEventListener("mouseup", up);
  }

  function clipDown(e, id) {
    if (e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    const c = clipById(id), startX = e.clientX, fps = fpsOf();
    const els = [clipEls[id].v, clipEls[id].a].filter(Boolean);
    for (const list of Object.values(layerEls)) for (const x of list) if (x.clip === id) els.push(x.el);
    const pts = snapPoints(id);
    let dragging = false, newStart = c.start;
    const tip = dragTip();
    const move = (ev) => {
      const dx = ev.clientX - startX;
      if (!dragging && Math.abs(dx) < 5) return;
      dragging = true;
      let want = Math.max(0, c.start + (ev.altKey ? dx / pps : Math.round((dx / pps) * fps) / fps)), hit = null;
      if (!ev.altKey) {
        const a = snapNear(want, pts), b = snapNear(want + c.length, pts);
        if (a.hit && (!b.hit || a.d <= b.d)) { want = a.t; hit = a.t; } else if (b.hit) { want = Math.max(0, b.t - c.length); hit = b.t; }
      }
      newStart = want;
      showSnapLine(hit);
      els.forEach((el) => { el.classList.add("dragging"); el.style.transform = `translateX(${(want - c.start) * pps}px)`; });
      tip.hidden = false;
      tip.style.left = ev.clientX + 14 + "px"; tip.style.top = ev.clientY - 28 + "px";
      tip.textContent = `starts at ${fmtTC(want)} (${signed(want - c.start)})`;
    };
    const up = async (ev) => {
      removeEventListener("mousemove", move); removeEventListener("mouseup", up);
      tip.hidden = true; showSnapLine(null);
      els.forEach((el) => { el.classList.remove("dragging"); el.style.transform = ""; });
      if (!dragging) { select(id); pause(); goto(xToTime(ev.clientX), false); return; }
      if (isLocked("V1")) return toast("Track V1 is locked", true);
      if (Math.abs(newStart - c.start) < 1e-9) return;
      await guard(async () => { commit(await api(`/sequences/${S.id}/clips/${id}/move`, "POST", { start: newStart })); })();
    };
    addEventListener("mousemove", move); addEventListener("mouseup", up);
  }

  function trimDown(e, id, edge) {
    if (e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    if (isLocked("V1")) return toast("Track V1 is locked", true);
    const c = clipById(id), startX = e.clientX, els = [clipEls[id].v, clipEls[id].a].filter(Boolean);
    const startT = edge === "in" ? c.start : c.start + c.length, endT = c.start + c.length;
    const pts = snapPoints(id);
    let want = startT;
    const tip = dragTip();
    select(id);
    const move = (ev) => {
      want = startT + (ev.clientX - startX) / pps;
      let hit = null;
      if (!ev.altKey) { const s = snapNear(want, pts); if (s.hit) { want = s.t; hit = s.t; } }
      want = edge === "in" ? Math.min(want, endT - 0.1) : Math.max(want, c.start + 0.1);
      showSnapLine(hit);
      els.forEach((el) => {          // preview: the dragged edge follows the mouse
        el.classList.add("trimming");
        if (edge === "in") { el.style.left = want * pps + "px"; el.style.width = Math.max(2, (endT - want) * pps) + "px"; }
        else el.style.width = Math.max(2, (want - c.start) * pps) + "px";
      });
      tip.hidden = false;
      tip.style.left = ev.clientX + 14 + "px"; tip.style.top = ev.clientY - 28 + "px";
      tip.textContent = `${edge === "in" ? "starts" : "ends"} ${fmtTC(want)} (${signed(want - startT)})`;
    };
    const up = async () => {
      removeEventListener("mousemove", move); removeEventListener("mouseup", up);
      tip.hidden = true; showSnapLine(null);
      els.forEach((el) => el.classList.remove("trimming"));
      if (Math.abs(want - startT) < 1e-3) { layoutPositions(); return; }
      await guard(async () => { commit(await api(`/sequences/${S.id}/clips/${id}/trim`, "POST", { edge, time: want })); })();
      layoutPositions();
    };
    addEventListener("mousemove", move); addEventListener("mouseup", up);
  }

  // ---- razor / delete
  function clipAtPlayhead() {
    const seg = FL[segI];
    if (seg) return clipById(seg.clip);
    return S.clips.find((c) => seqT > c.start && seqT < c.start + c.length) || null;
  }

  async function splitAtPlayhead() {
    const c = (sel && clipById(sel) && seqT > clipById(sel).start + 0.05 && seqT < clipById(sel).start + clipById(sel).length - 0.05)
      ? clipById(sel) : clipAtPlayhead();
    if (!c) return toast("Put the playhead on a clip to split it", true);
    if (isLocked("V1")) return toast("Track V1 is locked", true);
    pause();
    const r = await api(`/sequences/${S.id}/clips/${c.id}/split`, "POST", { time: seqT });
    commit(r.sequence);
    goto(seqT);
  }

  async function deleteSelected(ripple) {
    if (selMarker) { commit(await api(`/sequences/${S.id}/markers/${selMarker}`, "DELETE")); selMarker = null; return; }
    if (selLayer) {
      if (isLocked(layerLabel(layerById(selLayer)))) return toast("That track is locked", true);
      commit(await api(`/sequences/${S.id}/layers/${selLayer}`, "DELETE"));
      toast("Layer removed (the file is untouched)");
      return;
    }
    if (!sel) return toast("Select a clip first", true);
    if (isLocked("V1")) return toast("Track V1 is locked", true);
    pause();
    commit(await api(`/sequences/${S.id}/clips/${sel}${ripple ? "?ripple=true" : ""}`, "DELETE"));
    toast(ripple ? "Removed, and the gap closed" : "Removed (still in the Media panel)");
  }

  function setupDnD() {
    const over = (el, e) => {
      const types = [...e.dataTransfer.types];
      if (!types.includes("text/autocut-pid") && !types.includes("Files")) return false;
      e.preventDefault(); el.classList.add("over");
      return true;
    };
    sc.addEventListener("dragover", (e) => {
      if (!over(sc, e)) return;
      showSnapLine(snapNear(xToTime(e.clientX), snapPoints()).t);
    });
    sc.addEventListener("dragleave", () => { sc.classList.remove("over"); showSnapLine(null); });
    sc.addEventListener("drop", guard(async (e) => {
      e.preventDefault(); sc.classList.remove("over"); showSnapLine(null); binDragging = false;
      const pid = e.dataTransfer.getData("text/autocut-pid");
      if (pid) await addClip(pid, null, Math.max(0, snapNear(xToTime(e.clientX), snapPoints()).t));
      else if (e.dataTransfer.files.length) await importFiles([...e.dataTransfer.files]);
    }));
    const bin = $("#binList");
    bin.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); bin.classList.add("over"); } });
    bin.addEventListener("dragleave", () => bin.classList.remove("over"));
    bin.addEventListener("drop", guard(async (e) => {
      if (!e.dataTransfer.files.length) return;
      e.preventDefault(); bin.classList.remove("over");
      await importFiles([...e.dataTransfer.files]);
    }));
  }

  function onKey(e) {
    if (!S || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    const k = e.key, lower = k.toLowerCase(), fps = fpsOf();
    if (e.ctrlKey || e.metaKey) {
      if (lower === "z" && !e.shiftKey) { e.preventDefault(); guard(undo)(); }
      else if (lower === "y" || (lower === "z" && e.shiftKey)) { e.preventDefault(); guard(redo)(); }
      return;
    }
    if (k === " ") { e.preventDefault(); playing || reverse ? pause() : play(); }
    else if (k === "Home") { e.preventDefault(); pause(); goto(0); }
    else if (k === "End") { e.preventDefault(); pause(); goto(total); }
    else if (k === "ArrowLeft") { e.preventDefault(); stepBy(e.shiftKey ? -1 : -1 / fps); }
    else if (k === "ArrowRight") { e.preventDefault(); stepBy(e.shiftKey ? 1 : 1 / fps); }
    else if (k === "ArrowUp") { e.preventDefault(); jumpEdit(-1); }
    else if (k === "ArrowDown") { e.preventDefault(); jumpEdit(1); }
    else if (lower === "l") { e.preventDefault(); shuttleForward(); }
    else if (lower === "j") { e.preventDefault(); shuttleBack(); }
    else if (lower === "k") { e.preventDefault(); shuttleStop(); }
    else if (lower === "c") { e.preventDefault(); guard(splitAtPlayhead)(); }
    else if (lower === "m") { e.preventDefault(); guard(addMarker)(); }
    else if (lower === "s") { e.preventDefault(); toggleSnap(); }
    else if ((k === "Delete" || k === "Backspace") && !TP.hasFloating()) { e.preventDefault(); guard(() => deleteSelected(e.shiftKey))(); }
  }

  // ----------------------------------------------------------------- zoom
  function setZoom(next, anchorClientX) {
    const rect = sc.getBoundingClientRect(), t = (anchorClientX - rect.left + sc.scrollLeft) / pps;
    pps = clamp(next, 1, 300);
    layoutPositions();
    sc.scrollLeft = Math.max(0, t * pps - (anchorClientX - rect.left));
    drawRuler(); scheduleDraw();
  }
  function fit() {
    pps = total > 0 ? clamp((sc.clientWidth - 80) / total, 1, 300) : 40;
    layoutPositions(); sc.scrollLeft = 0; drawRuler(); scheduleDraw();
  }

  // ================================================================= import / bin actions
  function uploadOne(f, onProgress) {  // raw PUT: the browser streams the file from disk, the server writes it once
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      const q = new URLSearchParams({ filename: f.name, language: $("#lang").value, model: $("#model").value, add_to_timeline: "true" });
      x.open("PUT", `/api/sequences/${S.id}/upload?${q}`);
      x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded, e.total);
      x.onload = () => {
        let body = {};
        try { body = JSON.parse(x.responseText); } catch {}
        x.status < 300 ? resolve(body) : reject(new Error(body.detail || x.statusText));
      };
      x.onerror = () => reject(new Error("Upload failed"));
      x.send(f);
    });
  }

  async function importFiles(files) {
    files = files.filter((f) => f && (f.type.startsWith("video/") || f.type.startsWith("audio/") || /\.(mkv|mov|mts|m4a|aac|flac|opus|wma|mxf|mp4)$/i.test(f.name)));
    if (!files.length) return toast("No video or audio files found", true);
    const st = $("#upStat"), wasEmpty = !S.clips.length;
    st.style.display = "";
    try {
      for (let i = 0; i < files.length; i++) {
        const f = files[i];
        const show = (loaded, tot) => {
          const pct = tot ? Math.round((loaded / tot) * 100) : 0;
          st.innerHTML = `Uploading ${i + 1}/${files.length}: <b>${esc(f.name)}</b><br>${fmtSize(loaded)} of ${fmtSize(tot)} (${pct}%)<div class="mini"><i style="width:${pct}%"></i></div>`
            + (tot > 1e9 ? `<div style="margin-top:4px">Tip: use <b>Import files…</b> for big files - it links them instead of copying.</div>` : "");
        };
        show(0, f.size);
        applySequence(await uploadOne(f, show));
        if (wasEmpty && i === 0) fit();
      }
      toast("Imported");
    } finally { st.style.display = "none"; st.innerHTML = ""; }
  }

  // Native file dialog (opened by the server on this PC): the files are linked, never copied or uploaded.
  async function pickFiles() {
    toast("Choose your files in the window that just opened…");
    let r;
    try { r = await api("/pick-files", "POST"); }
    catch (e) { toast(e.message + " - using upload instead", true); return $("#file").click(); }
    if (!r.paths.length) return;
    const wasEmpty = !S.clips.length;
    applySequence(await api(`/sequences/${S.id}/import-path`, "POST", { paths: r.paths, language: $("#lang").value, model: $("#model").value, add_to_timeline: true }));
    if (wasEmpty) fit();
    toast(`Added ${r.paths.length} file${r.paths.length > 1 ? "s" : ""}`);
  }

  function importByPath() {
    const m = modal(`<h3>Import by path</h3>
      <p class="hint">One full file path per line. The original files are linked (not copied), so Premiere finds them.</p>
      <textarea id="paths" rows="5" style="width:100%;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px" placeholder="D:\\videos\\clip1.mp4&#10;D:\\videos\\clip2.mp4"></textarea>
      <div class="row" style="margin-top:10px;justify-content:flex-end"><button id="mc">Cancel</button><button class="primary" id="mok">Import</button></div>`);
    $("#mc", m).onclick = () => m.remove();
    $("#mok", m).onclick = guard(async () => {
      const paths = $("#paths", m).value.split("\n").map((x) => x.trim()).filter(Boolean);
      if (!paths.length) return;
      m.remove();
      const wasEmpty = !S.clips.length;
      applySequence(await api(`/sequences/${S.id}/import-path`, "POST", { paths, language: $("#lang").value, model: $("#model").value, add_to_timeline: true }));
      if (wasEmpty) fit();
    });
    $("#paths", m).focus();
  }

  async function addExisting() {
    const all = await api("/projects"), inBin = new Set(S.items.map((i) => i.id)), free = all.filter((p) => !inBin.has(p.id));
    const m = modal(`<h3>Add existing media</h3>${free.length ? free.map((p) => `<div class="pitem" style="margin-bottom:6px;cursor:pointer" data-id="${p.id}">
      <span>${p.kind === "video" ? "🎬" : "🎧"}</span><span class="name">${esc(p.name)}</span><span class="hint">${fmt(p.duration)}</span></div>`).join("") : `<p class="hint">Everything you have imported is already in this sequence.</p>`}`);
    m.querySelectorAll("[data-id]").forEach((el) => (el.onclick = guard(async () => { m.remove(); await addClip(el.dataset.id); })));
  }

  async function addClip(pid, index, start) {
    const wasEmpty = !S.clips.length;
    const r = await api(`/sequences/${S.id}/clips`, "POST", { project_id: pid, index: index ?? null, start: start ?? null });
    commit(r.sequence);
    if (wasEmpty) fit();
  }

  // ================================================================= export
  function renderExport() {
    const ex = S.export;
    $("#expStat").innerHTML = ex.state === "running" ? `Rendering… ${Math.round(ex.progress * 100)}%`
      : ex.state === "done" ? `<a href="/api/sequences/${S.id}/download/media" title="${esc(ex.file)}">Download render</a>`
      : ex.state === "error" ? `<span style="color:var(--k-manual)" title="${esc(ex.message)}">Render failed</span>` : "";
    app.querySelectorAll("[data-x=media]").forEach((b) => (b.disabled = ex.state === "running"));
  }

  async function doExport(kind) {
    if (!S.clips.length) return toast("The timeline is empty", true);
    if (kind === "media") {
      await api(`/sequences/${S.id}/export`, "POST", { kind });
      toast("Rendering started");
      return refresh();
    }
    const r = await api(`/sequences/${S.id}/export`, "POST", { kind });
    const a = document.createElement("a");
    a.href = `/api/sequences/${S.id}/download/${kind}`; a.download = ""; a.click();
    toast("Saved: " + r.file);
  }

  return {
    open, close, refresh,
    set pendingFiles(v) { pending = v; },
    // read-only view of the layer audio players, handy for checking sync from the console
    get debug() { return { t: seqT, playing, speed, reverse, seg: segI, history: hist.length, redo: redoStack.length, players: Object.fromEntries(Object.entries(players).map(([k, p]) => [k, { t: p.el.currentTime, paused: p.el.paused, muted: p.el.muted, gain: p.gain ? p.gain.gain.value : p.el.volume }])) }; },
  };
})();
