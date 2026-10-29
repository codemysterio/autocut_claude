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
2. **Arrange**: drag clips left/right on the timeline to change their order (they always sit back to back). Drag items from
   the Media panel onto the timeline, or press **＋**. **Delete** removes the selected clip from the timeline (the media stays
   in the bin). Video and its audio are linked: V1 holds the picture, A1 the waveform. Audio-only files appear on A1 only.
3. **Preview**: Space plays across all clips, click or drag the ruler to scrub, Ctrl+wheel (or the slider) zooms, **Fit** shows everything.
4. **Trim by transcript**: the playhead's clip is shown in the Transcript panel. Drag across words and press **Delete** to cut them;
   the clip shortens on the timeline right away (thin white lines mark where text was removed). Click a struck-out word to
   restore it. **Find suggestions** proposes fillers, stutters, repeated takes and long pauses; nothing is cut until you accept.
5. **Export** from the bar above the timeline.

Preview plays 720p copies, so it is smooth; exports always use your original files at full quality.

### Languages and models
`auto`, `en`, `it`, `hi` (Devanagari) and `hinglish` (Hindi in Latin letters). `large-v3-turbo` is the default and is fast;
choose `large-v3` for Hindi/Hinglish. Whisper tends to drop "um/uh", so AutoCut primes it to keep them; some may still be missed.

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
- Timeline: `create_sequence`, `list_sequences`, `sequence_status`, `import_to_sequence`, `add_clip`, `move_clip`, `remove_clip`,
  `export_sequence`, `render_sequence`, `render_sequence_status`
- Per media file: `open_media`, `project_status`, `get_transcript`, `get_words`, `find_text`, `analyze`, `list_cuts`, `cut_words`,
  `cut_time_ranges`, `accept_cuts`, `remove_cuts`, `undo`, `fix_word`, `list_projects`, `delete_project`
- Single-file exports: `export_premiere_xml`, `export_edl`, `export_subtitles`, `render_media`, `export_status`

## Layout

```
backend/autocut/   server.py (API) · sequences.py · transcribe.py · cleanup.py · editing.py · exporters.py · mcp_server.py
frontend/          index.html · app.js (home) · editor.js (timeline) · transcript.js · boot.js · style.css  (no build step)
data/              projects, sequences and exports (not in git)
```
