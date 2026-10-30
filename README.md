# AutoCut

Arrange several videos and audio files on a Premiere-style timeline, then trim them by editing the transcript.
Local Whisper transcription (runs on your GPU), filler/repeat/pause detection, Premiere Pro XML export, and an MCP server
so Claude can do the editing.

## Run it

```
start.bat            (or: .venv\Scripts\python.exe run_server.py)
```

Opens http://127.0.0.1:8765. Setup from scratch on another PC: `setup.bat` (needs Python 3.10+ and ffmpeg on PATH).

## The editor

```
┌ Media ────────┬ Program (preview) ─┬ Transcript (selected clip) ┐
│ import, bin   │ plays the timeline │ cut words, suggestions     │
├───────────────┴────────────────────┴────────────────────────────┤
│ Timeline   ruler · V1 (thumbnails) · A1 (waveforms) · playhead   │
└─────────────────────────────────────────────────────────────────┘
```

1. **New sequence**, then **Import files…**: the Windows file dialog opens and the files you pick are *linked*, not copied, so
   even huge files import instantly (and the Premiere XML points at your real files). Each file is transcribed in the
   background (pick the language and model first). **Import by path…** does the same from pasted paths. Dragging files from
   Explorer onto the page still works but has to upload a copy, because browsers hide real file paths.
2. **Arrange**: the timeline is free-form like Premiere's. Drag a clip anywhere (gaps are fine; if the spot is taken it slides to
   the nearest free one). Drag items from the Media panel onto the timeline at the spot you want, or press **＋** to append.
   Video and its audio are linked: V1 holds the picture, A1 the waveform. Audio-only files appear on A1 only.
3. **Edit** with the buttons above the timeline or the keys:
   - **Split** (**C**) cuts the clip at the playhead; **drag a clip's left or right edge** to trim it.
   - **Delete** removes a clip and leaves a gap; **Ripple delete** (**Shift+Delete**) closes the gap; **Close gaps** packs every clip together.
   - **Snap** (**S**) snaps clips, layers, trims and the playhead to edges, markers and each other (hold **Alt** to move freely).
   - **Marker** (**M**) drops a flag on the ruler (double-click to name it, Delete to remove). Markers go into the Premiere XML.
   - Each track has buttons: **M** mute, **S** solo, **🔒** lock (V1 has 👁 to hide the picture). They apply to preview, render and XML.
   - **Ctrl+Z / Ctrl+Y** undo and redo timeline edits *and* transcript cuts in the order you made them.
   - The bar between the panels and the timeline can be dragged to resize it; **↕** changes the track height.
4. **Preview**: **Space** plays across all clips (empty gaps play as black and silence). **J / K / L** shuttle (L again = faster, J plays
   backwards, K stops), **← / →** step one frame (Shift = one second), **↑ / ↓** jump to the previous / next edit or marker,
   click or drag the ruler to scrub, Ctrl+wheel (or the slider) zooms, **Fit** shows everything.
5. **Trim by transcript**: the playhead's clip is shown in the Transcript panel. Drag across words and press **Delete** to cut them;
   the clip shortens on the timeline right away (thin white lines mark where text was removed) and **the clips after it slide left
   automatically** (and back again if you restore the words). Click a struck-out word to restore it. **Find suggestions** proposes
   fillers, stutters, repeated takes and long pauses; nothing is cut until you accept.
6. **Export** from the bar above the timeline.

Preview plays 720p copies, so it is smooth; exports always use your original files at full quality.

### Languages and models
`auto`, `en`, `it`, `hi` (Devanagari) and `hinglish` (Hindi in Latin letters). `large-v3-turbo` is the default and is fast;
choose `large-v3` for Hindi/Hinglish. Whisper tends to drop "um/uh", so AutoCut primes it to keep them; some may still be missed.

## Extra audio layers (external mic, second camera sound, music)

Select a clip, open the **Layers** tab and press **＋ Synced layer…** (or drop files on the tab). AutoCut finds how far the
new recording is offset from the clip by matching their audio, and shows a **confidence score** (below 50% it asks you to check).

- **Auto-sync:** a coarse match over the whole files, then a precise match (GCC-PHAT, sub-millisecond) at several points - start,
  middle and end for long recordings. If the offsets slowly diverge (two devices' clocks running at slightly different speeds) the
  drift is measured in ppm and **corrected** automatically in preview, render and XML.
- **Nudge by hand:** ◀ 1 s / 10 f / 1 f and 1 f / 10 f / 1 s ▶ move the layer while the clip plays. You can also drag the layer on
  the timeline (it snaps to whole frames), or press **Auto-sync** again.
- **Per layer:** volume (dB), mute, fade in/out, trim in/out (or ⇥ ⇤ to use the playhead), and **Use instead of the main audio** to
  silence the camera's own sound for that clip.
- **Cuts apply to every layer:** layers follow the clip's source time, so every cut you make in the transcript (words, silences,
  repeated takes) is made on the layer too and they stay in sync. This also holds in the render and the XML.
- **Layers can be dragged freely**: sideways (a synced layer shows its whole file as a faint outline and the drag changes its sync; hold Alt for sub-frame moves) and up or down between audio tracks, even on top of other layers. **Unlink** turns a synced layer into a free one that stays put but stops following the clip's cuts.
- **Music / free layers** are placed by time on the whole sequence (they are not matched to the video).
  Music has nothing in common with the video's audio, so it cannot be auto-synced - set its start and fades by hand.
- **Picture layers:** a layer file with a picture (second camera angle, screen recording, b-roll) also shows on its own picture track
  (V2, V3…, above V1) and is drawn on top of the main picture in preview, render and XML. Per layer: **Show picture**, one-click
  corner presets (↖ ↗ ↙ ↘, ⛶ full screen), size, position, opacity, or drag the yellow box on the Program monitor. The layer's fades
  also fade the picture. Screen recordings without sound can't be auto-synced, so nudge them by eye. Drag a picture up or down to
  change its V track (higher tracks are drawn on top); the V buttons hide or lock a whole picture track.
- Layer files are linked, not copied (use the buttons, not drag and drop, for big files).

Accuracy note: matching the sound is far finer than one frame, but a camera or recorder can have its own fixed audio delay, so
check one or two spots by ear, especially if the confidence is below 100%.

In the **Premiere XML** each layer gets its own audio track (A3, A4… and the music tracks) with volume and fade keyframes, and its picture its own video track (V2, V3…) with Basic Motion (scale, position) and Opacity. A layer that
drifts is linked as a speed-corrected copy (WAV, or mp4 when it has a picture) kept in `data\layer_cache\`, so Premiere stays in sync to the end. Premiere handling of
the level/fade keyframes has not been checked on a real Premiere install.

## Export

| Button | Result |
|---|---|
| Premiere XML | Final Cut Pro 7 XML (xmeml). In Premiere: **File > Import**. Every clip, in order, with your transcript cuts as separate clips on V1/A1, linked to the original files. |
| EDL | CMX3600 edit list |
| SRT | Subtitles of the whole edited timeline |
| Render | One mp4 (or m4a for audio-only) made with ffmpeg |

The sequence takes its size and frame rate from the first video clip. In the **render**, other clips are scaled to fit (with black bars)
and converted to that frame rate; audio-only clips get a black picture. In the **XML**, each clip keeps its own frame rate and size
and Premiere places it at its native size, so resize in Premiere if your clips differ.

Files are written to `data\sequence_exports\<id>\`. The XML points at the original media by path - do not move or rename those
files, or Premiere will ask you to relink them.

## Edit with Claude (MCP)

The MCP server lets Claude import files, arrange the timeline, read transcripts, cut, and export. It talks to the same server as the
browser UI (and starts it if needed), so Claude's changes appear live. Claude's transcript cuts arrive as **proposals** unless you tick
"Claude cuts apply instantly" (top right).

**Claude Code** - this folder has a `.mcp.json`, so a session opened in `D:\autocut` offers the server automatically. For other folders:

```
claude mcp add autocut -- D:\autocut\.venv\Scripts\python.exe D:\autocut\run_mcp.py
```

**Claude Desktop** - add to `%APPDATA%\Claude\claude_desktop_config.json`, then restart it:

```json
{
  "mcpServers": {
    "autocut": {
      "command": "D:\\autocut\\.venv\\Scripts\\python.exe",
      "args": ["D:\\autocut\\run_mcp.py"]
    }
  }
}
```

Example prompts:

- "Create a sequence, import D:\shoot\a.mp4, b.mp4 and c.mp4 in that order, remove fillers and repeated takes from all of them, then export a Premiere XML."
- "Put the interview clip first, then the B-roll, and render it."
- "Read the transcript of the second clip and propose cuts for anything off-topic, with a reason for each."

Tools:
- Timeline: `create_sequence`, `list_sequences`, `sequence_status`, `import_to_sequence`, `add_clip`, `move_clip` (reorder), `place_clip` (start time),
  `split_clip`, `trim_clip`, `remove_clip` (optionally ripple), `close_gaps`, `add_marker`, `set_track`, `undo_timeline`,
  `export_sequence`, `render_sequence`, `render_sequence_status`
- Layers: `add_layer` (auto-syncs), `add_music_layer`, `set_layer`, `nudge_layer`, `sync_layer`, `unlink_layer`, `remove_layer`
- Per media file: `open_media`, `project_status`, `get_transcript`, `get_words`, `find_text`, `analyze`, `list_cuts`, `cut_words`,
  `cut_time_ranges`, `accept_cuts`, `remove_cuts`, `undo`, `fix_word`, `list_projects`, `delete_project`
- Single-file exports: `export_premiere_xml`, `export_edl`, `export_subtitles`, `render_media`, `export_status`

## Layout

```
backend/autocut/   server.py (API) · sequences.py · syncing.py · transcribe.py · cleanup.py · editing.py · exporters.py · mcp_server.py
frontend/          index.html · app.js (home) · editor.js (timeline) · transcript.js · layers.js · boot.js · style.css  (no build step)
data/              projects, sequences and exports (not in git)
```
