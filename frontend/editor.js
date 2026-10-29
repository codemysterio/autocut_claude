"use strict";
// The timeline editor: media bin, program monitor, timeline with waveforms, playback across clips.
const NLE = (() => {
  const TRACK_H = 72, DPR = Math.min(2, window.devicePixelRatio || 1);
  let S = null, sel = null, pps = 40, pending = null;
  let sc, content, trackV, trackA, ruler, rulerWrap, playhead, dropmark, stageMsg, vids;
  let FL = [], total = 0, layoutSig = "", itemsSig = "";
  const clipEls = {}, mediaData = {};
  let cur = 0, segI = 0, seqT = 0, playing = false, token = 0, prepared = -1, lastPlayClip = null;
  let rafId = null, pollTimer = null, drawQueued = false, binDragging = false, ver = -1, keysBound = false;

  const mediaUrl = (pid) => `/api/projects/${pid}/media`;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  // ================================================================= open / close
  async function open(sid) {
    S = await api("/sequences/" + sid);
    ver = S.version;
    build();
    $("#crumb").textContent = "/ " + S.name;
    applySequence(S, true);
    fit();
    rafId = requestAnimationFrame(tick);
    pollTimer = setInterval(poll, 900);
    if (pending) { const f = pending; pending = null; importFiles(f); }
  }

  function close() {
    clearInterval(pollTimer); cancelAnimationFrame(rafId);
    pollTimer = rafId = null;
    if (vids) vids.forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });
    S = null; sel = null; FL = []; playing = false; segI = 0; seqT = 0; layoutSig = itemsSig = ""; prepared = -1; lastPlayClip = null;
    for (const k of Object.keys(clipEls)) delete clipEls[k];
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

  // ================================================================= DOM
  function build() {
    const lang = localStorage.getItem("autocut-lang") || "auto";
    const model = localStorage.getItem("autocut-model") || cfg.default_model;
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
          <div class="stage"><video id="vA" playsinline></video><video id="vB" playsinline></video><div class="stage-msg" id="stageMsg"></div></div>
          <div class="transport">
            <button id="tStart" title="Go to start (Home)">⏮</button><button id="tPlay" title="Play / pause (Space)">▶</button><button id="tEnd" title="Go to end (End)">⏭</button>
            <span class="tcbig" id="tc">00:00:00.0</span><span class="sp"></span><span class="hint" id="tcTotal"></span>
          </div>
        </section>
        <section class="panel" id="tpanel"></section>
      </div>
      <section class="panel timeline">
        <div class="tl-bar">
          <span class="tl-title" id="seqTitle" title="Double-click to rename"></span>
          <span class="hint" id="tlInfo"></span><span class="sp"></span>
          <span class="hint">Zoom</span><input type="range" id="zoom" min="0" max="100" step="0.5">
          <button class="small" id="fit">Fit</button>
          <span style="width:12px"></span>
          <button class="primary small" data-x="xml">Premiere XML</button>
          <button class="small" data-x="edl">EDL</button><button class="small" data-x="srt">SRT</button>
          <button class="small" data-x="media">Render</button>
          <span class="hint" id="expStat"></span>
        </div>
        <div class="tl-main">
          <div class="tl-heads"><div class="rh"></div><div class="head v">V1</div><div class="head a">A1</div></div>
          <div class="tl-scroll" id="tlScroll"><div class="tl-content" id="tlContent">
            <div class="ruler-wrap" id="rulerWrap"><canvas id="ruler"></canvas></div>
            <div class="track" id="trackV"></div><div class="track" id="trackA"></div>
            <div class="tl-hint" id="tlHint">Drag media here from the Media panel, or import files.</div>
            <div class="playhead" id="playhead"></div><div class="dropmark" id="dropmark"></div>
          </div></div>
        </div>
      </section>
    </div>`;
    sc = $("#tlScroll"); content = $("#tlContent"); trackV = $("#trackV"); trackA = $("#trackA");
    ruler = $("#ruler"); rulerWrap = $("#rulerWrap"); playhead = $("#playhead"); dropmark = $("#dropmark");
    stageMsg = $("#stageMsg"); vids = [$("#vA"), $("#vB")];
    cur = 0; vids[1].style.visibility = "hidden";

    TP.init($("#tpanel"), { onChange: refresh, seek: seekSource });

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
    $("#seqTitle").ondblclick = guard(async () => {
      const n = prompt("Sequence name:", S.name);
      if (n && n.trim()) { await api("/sequences/" + S.id, "PATCH", { name: n }); $("#crumb").textContent = "/ " + n.trim(); await refresh(); }
    });
    app.querySelectorAll("[data-x]").forEach((b) => (b.onclick = guard(() => doExport(b.dataset.x))));

    sc.addEventListener("scroll", () => { drawRuler(); scheduleDraw(); });
    sc.addEventListener("wheel", (e) => {
      if (e.ctrlKey) { e.preventDefault(); setZoom(pps * (e.deltaY < 0 ? 1.25 : 0.8), e.clientX); }
      else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); sc.scrollLeft += e.deltaY; }
    }, { passive: false });
    window.addEventListener("resize", onResize);
    rulerWrap.addEventListener("mousedown", (e) => scrub(e));
    content.addEventListener("mousedown", (e) => { if (e.target === content || e.target === trackV || e.target === trackA) { select(null); scrub(e); } });
    setupDnD();
    if (!keysBound) { document.addEventListener("keydown", onKey); keysBound = true; }
  }

  const onResize = () => { if (S) { drawRuler(); scheduleDraw(); } };

  // ================================================================= sequence → UI
  function applySequence(s, first = false) {
    const anchor = first ? null : anchorNow();
    const wasPlaying = playing;
    S = s;
    for (const c of S.clips) {
      let acc = 0;
      c.cum = c.segments.map(([a, b]) => { const x = acc; acc += b - a; return x; });
    }
    total = S.duration;
    const sig = JSON.stringify(S.clips.map((c) => [c.id, c.start, c.length, c.segments]));
    const layoutChanged = sig !== layoutSig;
    layoutSig = sig;
    buildFlat();
    $("#seqTitle").textContent = S.name;
    $("#tlInfo").textContent = `${S.clips.length} clip${S.clips.length === 1 ? "" : "s"} · ${fmt(total)} · ${S.settings.has_video ? `${S.settings.width}×${S.settings.height} ${(S.settings.fps_num / S.settings.fps_den).toFixed(2).replace(/\.?0+$/, "")} fps` : "audio only"}`;
    $("#tcTotal").textContent = "of " + fmtTC(total);
    const isig = JSON.stringify(S.items.map((i) => [i.id, i.state, i.progress, i.step, i.proxy, i.proxy_progress, i.name]));
    if (isig !== itemsSig && !binDragging) { itemsSig = isig; renderBin(); }
    if (layoutChanged) renderClips();
    if (sel && !S.clips.find((c) => c.id === sel)) select(null);
    TP.sync(S.items);
    renderExport();
    if (layoutChanged && anchor) goto(locate(anchor), wasPlaying);
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
      applySequence(await api(`/sequences/${S.id}/items/${b.dataset.rm}`, "DELETE"));
    })));
  }

  // ----------------------------------------------------------------- timeline clips
  function renderClips() {
    trackV.innerHTML = trackA.innerHTML = "";
    for (const k of Object.keys(clipEls)) delete clipEls[k];
    for (const c of S.clips) {
      if (c.length <= 0) continue;
      const e = (clipEls[c.id] = {});
      if (c.has_video) trackV.appendChild((e.v = mkClip(c, "v")));
      if (c.has_audio) trackA.appendChild((e.a = mkClip(c, "a")));
      ensureMedia(c);
    }
    $("#tlHint").hidden = S.clips.length > 0;
    layoutPositions();
  }

  function mkClip(c, type) {
    const d = document.createElement("div");
    d.className = `clip ${type}` + (c.id === sel ? " sel" : "");
    d.dataset.id = c.id;
    d.innerHTML = `<canvas></canvas><span class="cl-name">${esc(c.name)}</span>`;
    d.addEventListener("mousedown", (e) => clipDown(e, c.id));
    return d;
  }

  function layoutPositions() {
    for (const c of S.clips) {
      const e = clipEls[c.id];
      if (!e) continue;
      for (const el of [e.v, e.a]) if (el) { el.style.left = c.start * pps + "px"; el.style.width = Math.max(2, c.length * pps) + "px"; }
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
  const scheduleDraw = () => { if (!drawQueued) { drawQueued = true; requestAnimationFrame(() => { drawQueued = false; if (S) drawVisible(); }); } };

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
        const w = Math.ceil(v1 - v0), h = TRACK_H - 8, off = Math.round(v0 - x0);
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
    if (!ruler) return;
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
      if (isMajor) ctx.fillText(major < 1 ? t.toFixed(1) + "s" : fmt(t), x + 3, 2);
    }
  }

  // ================================================================= selection
  function select(id, fromPlayhead = false) {
    sel = id;
    document.querySelectorAll(".clip.sel").forEach((e) => e.classList.remove("sel"));
    if (id && clipEls[id]) for (const el of [clipEls[id].v, clipEls[id].a]) if (el) el.classList.add("sel");
    const c = id && S.clips.find((x) => x.id === id);
    TP.show(c ? c.pid : null).catch(() => {});
    $("#monInfo").textContent = c ? c.name : "";
  }

  // ================================================================= playback engine
  function buildFlat() {
    FL = [];
    for (const c of S.clips) {
      let acc = c.start;
      for (const [s, e] of c.segments) { FL.push({ clip: c.id, pid: c.pid, video: c.has_video, t0: acc, t1: acc + (e - s), s0: s, s1: e }); acc += e - s; }
    }
  }

  function segIndexAt(t) {
    if (!FL.length) return -1;
    let lo = 0, hi = FL.length - 1, r = 0;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (FL[m].t0 <= t) { r = m; lo = m + 1; } else hi = m - 1; }
    return r;
  }

  const anchorNow = () => { const seg = FL[segI]; return seg ? { clip: seg.clip, src: seg.s0 + (seqT - seg.t0) } : null; };

  function locate(a) {  // (clip, source time) → sequence time, snapping out of removed parts
    const c = S.clips.find((x) => x.id === a.clip);
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
    vids.forEach((v, k) => (v.style.visibility = k === i ? "visible" : "hidden"));
    const seg = FL[segI];
    const audioOnly = seg && !seg.video;
    if (audioOnly) vids.forEach((v) => (v.style.visibility = "hidden"));
    stageMsg.textContent = !FL.length ? "Import media and place it on the timeline." : audioOnly ? "♪ audio" : "";
  }

  async function goto(t, play = false) {
    const my = ++token;
    t = clamp(t, 0, total);
    seqT = t; playing = false; vids.forEach((v) => v.pause());
    if (!FL.length) { show(cur); updateUI(); return; }
    segI = segIndexAt(t);
    const seg = FL[segI], el = vids[cur];
    await loadAt(el, seg, seg.s0 + clamp(t - seg.t0, 0, seg.s1 - seg.s0));
    if (my !== token) return;
    show(cur);
    if (play) { playing = true; el.play().catch(() => (playing = false)); }
    preloadNext();
    updateUI();
  }

  const play = () => { if (!FL.length) return; return goto(seqT >= total - 0.05 ? 0 : seqT, true); };
  function pause() { playing = false; token++; vids.forEach((v) => v.pause()); updateUI(); }

  function preloadNext() {
    const n = segI + 1;
    if (n < FL.length && FL[n].pid !== FL[segI].pid) { loadAt(vids[1 - cur], FL[n], FL[n].s0); prepared = n; } else prepared = -1;
  }

  function advance() {
    const n = segI + 1, seg = FL[segI], el = vids[cur];
    if (n >= FL.length) { pause(); seqT = total; return; }
    const nx = FL[n];
    if (nx.pid === seg.pid) { el.currentTime = nx.s0; segI = n; show(cur); preloadNext(); }
    else if (prepared === n) {
      const o = vids[1 - cur];
      o.play().catch(() => {}); el.pause(); cur = 1 - cur; segI = n; show(cur); preloadNext();
    } else goto(nx.t0, true);
  }

  function tick() {
    rafId = requestAnimationFrame(tick);
    if (!S) return;
    if (playing) {
      const seg = FL[segI], el = vids[cur];
      if (seg) {
        const st = el.currentTime;
        seqT = clamp(seg.t0 + (st - seg.s0), 0, total);
        if (st >= seg.s1 - 0.03 || el.ended) advance();
      }
    }
    updateUI();
  }

  function updateUI() {
    if (!S) return;
    $("#tc").textContent = fmtTC(seqT);
    $("#tPlay").textContent = playing ? "⏸" : "▶";
    const x = seqT * pps;
    playhead.style.transform = `translateX(${x}px)`;
    if (playing) {
      if (x > sc.scrollLeft + sc.clientWidth - 40 || x < sc.scrollLeft) sc.scrollLeft = Math.max(0, x - 80);
    }
    const seg = FL[segI];
    if (seg) {
      if (seg.clip !== lastPlayClip) { lastPlayClip = seg.clip; if (seg.clip !== sel) select(seg.clip, true); }
      TP.tick(seg.pid, seg.s0 + (seqT - seg.t0), playing);
    }
  }

  // Jump to a time inside a media file (used by the transcript panel).
  function seekSource(pid, srcT) {
    const c = S.clips.find((x) => x.id === sel && x.pid === pid) || S.clips.find((x) => x.pid === pid);
    if (!c) return;
    if (sel !== c.id) select(c.id);
    goto(locate({ clip: c.id, src: srcT }), playing);
  }

  // ================================================================= timeline interaction
  const xToTime = (clientX) => (clientX - sc.getBoundingClientRect().left + sc.scrollLeft) / pps;

  function scrub(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    goto(xToTime(e.clientX), false);
    const move = (ev) => goto(xToTime(ev.clientX), false);
    const up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); };
    addEventListener("mousemove", move); addEventListener("mouseup", up);
  }

  function dropIndex(centerSec, skipId) {
    const others = S.clips.filter((c) => c.id !== skipId);
    let idx = 0;
    for (const o of others) if (o.start + o.length / 2 < centerSec) idx++;
    return { idx, x: (others[idx] ? others[idx].start : total) * pps };
  }

  function clipDown(e, id) {
    if (e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    const c = S.clips.find((x) => x.id === id), from = S.clips.indexOf(c), startX = e.clientX;
    const els = [clipEls[id].v, clipEls[id].a].filter(Boolean);
    let dragging = false, target = from;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      if (!dragging && Math.abs(dx) < 5) return;
      dragging = true;
      els.forEach((el) => { el.classList.add("dragging"); el.style.transform = `translateX(${dx}px)`; });
      const d = dropIndex(c.start + c.length / 2 + dx / pps, id);
      target = d.idx;
      dropmark.style.display = "block"; dropmark.style.left = d.x + "px";
    };
    const up = async (ev) => {
      removeEventListener("mousemove", move); removeEventListener("mouseup", up);
      dropmark.style.display = "none";
      els.forEach((el) => { el.classList.remove("dragging"); el.style.transform = ""; });
      if (!dragging) { select(id); goto(xToTime(ev.clientX), false); return; }
      if (target !== from) await guard(async () => { applySequence(await api(`/sequences/${S.id}/clips/${id}/move`, "POST", { index: target })); })();
    };
    addEventListener("mousemove", move); addEventListener("mouseup", up);
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
      const d = dropIndex(xToTime(e.clientX), null);
      dropmark.style.display = "block"; dropmark.style.left = d.x + "px";
    });
    sc.addEventListener("dragleave", () => { sc.classList.remove("over"); dropmark.style.display = "none"; });
    sc.addEventListener("drop", guard(async (e) => {
      e.preventDefault(); sc.classList.remove("over"); dropmark.style.display = "none"; binDragging = false;
      const pid = e.dataTransfer.getData("text/autocut-pid");
      if (pid) await addClip(pid, dropIndex(xToTime(e.clientX), null).idx);
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
    if (e.key === " ") { e.preventDefault(); playing ? pause() : play(); }
    else if (e.key === "Home") { e.preventDefault(); goto(0); }
    else if (e.key === "End") { e.preventDefault(); goto(total); }
    else if ((e.key === "Delete" || e.key === "Backspace") && sel && !TP.hasFloating()) {
      e.preventDefault();
      guard(async () => {
        pause();
        applySequence(await api(`/sequences/${S.id}/clips/${sel}`, "DELETE"));
        toast("Removed from the timeline (still in the Media panel)");
      })();
    }
  }

  // ----------------------------------------------------------------- zoom
  function setZoom(next, anchorClientX, fromSlider = false) {
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

  async function addClip(pid, index) {
    const wasEmpty = !S.clips.length;
    const r = await api(`/sequences/${S.id}/clips`, "POST", { project_id: pid, index: index ?? null });
    applySequence(r.sequence);
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
  };
})();
