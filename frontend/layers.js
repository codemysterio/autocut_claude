"use strict";
// Layers tab: extra recordings (external mic, second camera, music) synced to the selected clip or placed on the sequence.
const LP = (() => {
  let root, h = {};

  function init(container, hooks) {
    root = container;
    h = hooks;
    root.innerHTML = `
      <div class="ph"><b>Layers</b><span class="hint" id="lpCtx"></span></div>
      <div class="tp-bar">
        <button class="primary small" id="lpAddSync" title="External mic, second camera, screen recording… matched to the clip by its audio">＋ Synced layer…</button>
        <button class="small" id="lpAddFree" title="Music or other sound placed by time on the whole sequence">＋ Music / free layer…</button>
      </div>
      <div class="tp-body" id="lpBody"></div>`;
    $("#lpAddSync", root).onclick = guard(() => add("sync"));
    $("#lpAddFree", root).onclick = guard(() => add("free"));
    // drop files from Explorer onto either section (uploads a copy - for big files use the buttons, which link the original)
    const body = $("#lpBody", root);
    body.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); body.classList.add("over"); } });
    body.addEventListener("dragleave", () => body.classList.remove("over"));
    body.addEventListener("drop", guard(async (e) => {
      if (!e.dataTransfer.files.length) return;
      e.preventDefault(); body.classList.remove("over");
      const mode = (e.target.closest(".lsec") || {}).dataset ? e.target.closest(".lsec").dataset.mode : "sync";
      if (mode === "sync" && !h.clipId()) return toast("Select a clip on the timeline first - the layer syncs to it", true);
      for (const f of e.dataTransfer.files) {
        toast(`Uploading ${f.name}…`);
        h.apply(await uploadLayer(f, mode));
      }
      toast("Added");
    }));
  }

  function uploadLayer(file, mode) {
    return new Promise((resolve, reject) => {
      const S = h.seq(), x = new XMLHttpRequest();
      const q = new URLSearchParams({ filename: file.name, mode, clip_id: h.clipId() || "" });
      x.open("PUT", `/api/sequences/${S.id}/layer-upload?${q}`);
      x.onload = () => {
        let b = {};
        try { b = JSON.parse(x.responseText); } catch {}
        x.status < 300 ? resolve(b) : reject(new Error(b.detail || x.statusText));
      };
      x.onerror = () => reject(new Error("Upload failed"));
      x.send(file);
    });
  }

  async function add(mode) {
    const S = h.seq(), cid = h.clipId();
    if (mode === "sync" && !cid) return toast("Select a clip on the timeline first - the layer syncs to it", true);
    toast("Choose the file in the window that just opened…");
    const r = await api("/pick-files", "POST");
    if (!r.paths.length) return;
    const url = mode === "sync" ? `/sequences/${S.id}/clips/${cid}/layers` : `/sequences/${S.id}/layers`;
    h.apply(await api(url, "POST", { paths: r.paths }));
    toast(mode === "sync" ? "Added - matching the audio…" : "Added");
  }

  const fmtOff = (s, fps) => `${s >= 0 ? "+" : "−"}${Math.abs(s).toFixed(3)} s (${s >= 0 ? "+" : "−"}${Math.abs(Math.round(s * fps))} f)`;

  function badge(l) {
    const st = l.sync.state, c = Math.round((l.sync.confidence || 0) * 100);
    return { ok: [`Synced · ${c}%`, "ok"], low: [`Check sync · ${c}%`, "warn"], failed: ["Not synced", "bad"],
      syncing: ["Matching audio…", "busy"], pending: ["Waiting…", "busy"], manual: ["Manual", ""] }[st] || ["", ""];
  }

  function card(l, clip, selected, fps, seqDur) {
    const [btxt, bcls] = badge(l), free = l.mode === "free", m = l.media;
    const dur = m.duration || 0, to = l.trim_out == null ? dur : l.trim_out;
    const driftFrames = (l.sync.drift_ppm || 0) * 1e-6 * (clip ? clip.source_duration : seqDur) * fps;
    const info = free ? `Plays ${fmt(Math.max(0, to - l.trim_in))} from ${fmt(l.trim_in)}`
      : l.sync.state === "syncing" || l.sync.state === "pending" ? "Matching the audio of this file against the clip…"
      : `Offset ${fmtOff(l.offset, fps)}` + (Math.abs(l.sync.drift_ppm || 0) > 0.5 ? ` · drift ${l.sync.drift_ppm.toFixed(1)} ppm (${driftFrames.toFixed(1)} f over the clip, corrected)` : "")
        + (l.sync.message ? `<br><span style="color:var(--k-repeat)">${esc(l.sync.message)}</span>` : "");
    const media = !m.has_audio && !m.has_video ? `<div class="hint" style="color:var(--k-manual)">This file has no sound or picture.</div>` : "";
    const pct = (v) => Math.round(v * 100);
    const pic = !m.has_video ? "" : `
      <div class="lrow pic"><label class="chk"><input type="checkbox" data-f="v_on" ${l.v_on ? "checked" : ""}> Show picture</label>
        <span class="presets hint">Place
          <button class="small" data-preset="tl" title="Top left">↖</button><button class="small" data-preset="tr" title="Top right">↗</button>
          <button class="small" data-preset="bl" title="Bottom left">↙</button><button class="small" data-preset="br" title="Bottom right">↘</button>
          <button class="small" data-preset="full" title="Full screen">⛶</button></span></div>
      <div class="lrow">Size <input type="range" min="5" max="100" data-f="v_scale" data-k="100" value="${pct(l.v_scale)}">
        <input type="number" min="5" max="100" data-f="v_scale" data-k="100" value="${pct(l.v_scale)}" style="width:58px">%</div>
      <div class="lrow">Opacity <input type="range" min="0" max="100" data-f="v_opacity" data-k="100" value="${pct(l.v_opacity)}">
        <input type="number" min="0" max="100" data-f="v_opacity" data-k="100" value="${pct(l.v_opacity)}" style="width:58px">%</div>
      <div class="lrow">Position X <input type="number" step="1" data-f="v_x" data-k="100" value="${pct(l.v_x)}" style="width:58px">%
        Y <input type="number" step="1" data-f="v_y" data-k="100" value="${pct(l.v_y)}" style="width:58px">%
        <span class="hint">or drag the yellow box on the monitor</span></div>
      <div class="hint">The fades below also fade the picture.</div>`;
    return `<div class="lcard ${selected ? "sel" : ""}" data-lid="${l.id}">
      <div class="lhead"><b title="${esc(l.name)}">${esc(l.name)}</b><span class="badge ${bcls}">${btxt}</span><span class="sp"></span>
        <button class="small danger" data-act="remove">Remove</button></div>
      <div class="hint">${info}${m.has_video ? " · picture layer" : ""}${m.proxy ? "" : " · preparing preview…"}</div>${media}
      ${free ? `<div class="lrow">Start at <input type="number" step="0.1" data-f="start" value="${l.start.toFixed(2)}"> s
          <button class="small" data-act="start-here">Playhead</button></div>`
        : `<div class="lrow nudge"><span class="hint">Move layer</span>
          <button class="small" data-nudge="-1">◀ 1 s</button><button class="small" data-nudge="${-10 / fps}">◀ 10 f</button><button class="small" data-nudge="${-1 / fps}">◀ 1 f</button>
          <button class="small" data-nudge="${1 / fps}">1 f ▶</button><button class="small" data-nudge="${10 / fps}">10 f ▶</button><button class="small" data-nudge="1">1 s ▶</button>
          <button class="small" data-act="resync" title="Match the audio again">Auto-sync</button>
          <button class="small" data-act="unlink" title="Make this a free layer you can place anywhere. It stops following the clip's cuts">Unlink</button></div>`}
      ${pic}
      <div class="lrow">Volume <input type="range" min="-40" max="12" step="0.5" data-f="gain_db" value="${l.gain_db}">
        <input type="number" step="0.5" data-f="gain_db" value="${l.gain_db}" style="width:62px"> dB</div>
      <div class="lrow">Fade in <input type="number" min="0" step="0.1" data-f="fade_in" value="${l.fade_in}" style="width:62px"> s
        Fade out <input type="number" min="0" step="0.1" data-f="fade_out" value="${l.fade_out}" style="width:62px"> s</div>
      <div class="lrow">Trim in <input type="number" min="0" step="0.1" data-f="trim_in" value="${l.trim_in.toFixed(2)}" style="width:70px"><button class="small" data-act="in-here" title="Use the playhead position">⇥</button>
        out <input type="number" min="0" step="0.1" data-f="trim_out" value="${to.toFixed(2)}" style="width:70px"><button class="small" data-act="out-here" title="Use the playhead position">⇤</button>
        <button class="small" data-act="trim-reset">Reset</button></div>
      <div class="lrow"><label class="chk"><input type="checkbox" data-f="mute" ${l.mute ? "checked" : ""}> Mute</label>
        ${free ? "" : `<label class="chk" title="Silences the camera's own audio for this clip"><input type="checkbox" data-f="replace_main_audio" ${l.replace_main_audio ? "checked" : ""}> Use instead of the main audio</label>`}</div>
    </div>`;
  }

  function render(force = false) {
    if (!root) return;
    const body = $("#lpBody", root);
    if (!force && body.contains(document.activeElement) && /INPUT/.test(document.activeElement.tagName)) return;  // do not yank an input while typing
    const S = h.seq();
    if (!S) return;
    const fps = S.settings.has_video ? S.settings.fps_num / S.settings.fps_den : 30;
    const clip = S.clips.find((c) => c.id === h.clipId());
    $("#lpCtx", root).textContent = clip ? "— synced to " + clip.name : "";
    const sel = h.layerId();
    let html = `<div class="lsec" data-mode="sync">`;
    html += `<h4 class="lh">Synced to ${clip ? esc(clip.name) : "the selected clip"}</h4>`;
    html += clip && clip.layers.length ? clip.layers.map((l) => card(l, clip, l.id === sel, fps, S.duration)).join("")
      : `<div class="hint" style="padding:6px 0 12px">${clip ? "No layers yet. Add an external mic, a second camera or a screen recording - AutoCut matches its audio to this clip automatically."
        : "Select a clip on the timeline to see or add its layers."}</div>`;
    html += `</div><div class="lsec" data-mode="free"><h4 class="lh">Music &amp; free layers (whole sequence)</h4>`;
    html += S.free_layers.length ? S.free_layers.map((l) => card(l, null, l.id === sel, fps, S.duration)).join("")
      : `<div class="hint" style="padding:6px 0">None. Music goes here: it is placed by time and is not matched to the video.</div>`;
    html += `</div>`;
    const keep = body.scrollTop;
    body.innerHTML = html;
    body.scrollTop = keep;
    const n = S.clips.reduce((a, c) => a + c.layers.length, 0) + S.free_layers.length;
    h.setCount(n);
    wire(body, fps);
  }

  function wire(body, fps) {
    body.querySelectorAll(".lcard").forEach((card) => {
      const lid = card.dataset.lid;
      card.addEventListener("mousedown", () => h.selectLayer(lid, true));
      const patch = guard(async (p) => h.apply(await api(`/sequences/${h.seq().id}/layers/${lid}`, "PATCH", p)));
      card.querySelectorAll("[data-nudge]").forEach((b) => (b.onclick = () => patch({ offset_delta: +b.dataset.nudge })));
      card.querySelectorAll("input[data-f]").forEach((inp) => {
        const f = inp.dataset.f;
        const k = +(inp.dataset.k || 1);                       // some fields show percent but are stored as fractions
        const val = () => (inp.type === "checkbox" ? inp.checked : parseFloat(inp.value) / k);
        if (inp.type === "range") {  // live preview while dragging, save when released
          inp.addEventListener("input", () => {
            h.local(lid, { [f]: val() });
            const num = card.querySelector(`input[type=number][data-f="${f}"]`); if (num) num.value = inp.value;
          });
        }
        inp.addEventListener("change", () => {
          const v = val();
          if (typeof v === "number" && Number.isNaN(v)) return;
          patch({ [f]: v });
        });
      });
      card.querySelectorAll("[data-preset]").forEach((b) => (b.onclick = () => {
        const S = h.seq(), l = [...S.clips.flatMap((c) => c.layers), ...S.free_layers].find((x) => x.id === lid);
        const W = S.settings.width, H = S.settings.height, lw = l.media.width || 16, lh = l.media.height || 9, margin = 0.03;
        const full = b.dataset.preset === "full";
        const sc = full ? Math.min(1, (H / W) / (lh / lw)) : (l.v_scale < 0.9 ? l.v_scale : 0.32);   // keep the size for corners
        const hf = sc * (lh / lw) * (W / H);
        const pos = { tl: [margin, margin], tr: [1 - margin - sc, margin], bl: [margin, 1 - margin - hf], br: [1 - margin - sc, 1 - margin - hf],
                      full: [(1 - sc) / 2, (1 - hf) / 2] }[b.dataset.preset];
        patch({ v_x: pos[0], v_y: pos[1], v_scale: sc });
      }));
      card.querySelectorAll("[data-act]").forEach((b) => (b.onclick = guard(async () => {
        const act = b.dataset.act, tl = h.layerTime(lid);
        if (act === "remove") { if (confirm("Remove this layer? The file itself is not touched.")) h.apply(await api(`/sequences/${h.seq().id}/layers/${lid}`, "DELETE")); }
        else if (act === "resync") { await api(`/sequences/${h.seq().id}/layers/${lid}/sync`, "POST"); toast("Matching the audio again…"); await h.refresh(); }
        else if (act === "unlink") { if (confirm("Unlink this layer from its clip? It stays where it is, but will no longer follow the clip's cuts or be kept in sync.")) h.apply(await api(`/sequences/${h.seq().id}/layers/${lid}/unlink`, "POST")); }
        else if (act === "in-here" && tl != null) patch({ trim_in: Math.max(0, tl) });
        else if (act === "out-here" && tl != null) patch({ trim_out: Math.max(0, tl) });
        else if (act === "trim-reset") patch({ trim_in: 0, trim_out: null });
        else if (act === "start-here") patch({ start: Math.max(0, h.time()) });
      })));
    });
  }

  // Highlight a card without rebuilding the panel.
  function mark(lid) {
    root.querySelectorAll(".lcard").forEach((c) => c.classList.toggle("sel", c.dataset.lid === lid));
  }

  return { init, render, mark };
})();
