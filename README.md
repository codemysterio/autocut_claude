# AutoCut

Edit video and audio by editing its transcript. Local Whisper transcription (runs on your GPU),
filler/repeat/pause detection, Premiere Pro XML export, and an MCP server so Claude can do the editing.

## Run it

```
start.bat            (or: .venv\Scripts\python.exe run_server.py)
```

Opens http://127.0.0.1:8765. Drop a file (or paste its full path), pick the language, and wait for the transcript.
First transcription downloads/loads the Whisper model, so it is slower.

Setup from scratch on another PC: `setup.bat` (needs Python 3.10+ and ffmpeg on PATH).

## Using the editor

- Click a word to jump there. Drag to select words, press **Delete** to cut them. Click a struck-out word (or pause chip) to restore it.
- **Find suggestions** adds proposals (coloured underline): filler words, repeated words/stutters, repeated takes and false starts, long pauses. Nothing is cut until you press Cut on it or **Cut all**.
- Playback skips cut parts, so you can hear the edit before exporting. Ctrl+Z / Ctrl+Y undo and redo.
- Double-click a word to fix a transcription mistake.

### Languages and models
`auto`, `en`, `it`, `hi` (Devanagari) and `hinglish` (Hindi in Latin letters). `large-v3-turbo` is the default and is fast;
choose `large-v3` for Hindi/Hinglish. Whisper tends to drop "um/uh", so AutoCut primes it to keep them; some may still be missed.

## Export

| Button | Result |
|---|---|
| Premiere Pro XML | Final Cut Pro 7 XML (xmeml). In Premiere: **File > Import**. Contains your cuts as clips on one video + audio timeline, linked to the original media. |
| EDL | CMX3600 edit list |
| SRT / VTT / Text | Subtitles and transcript of the *edited* timeline |
| Render | Edited mp4 (video) or m4a/mp3/wav (audio) made with ffmpeg |

Files are written to `data\projects\<id>\exports\`. The XML points at the original media by path - do not move or rename
that file, or Premiere will ask you to relink it. Open files **by path** (not by upload) if you plan to use the XML,
so it links to your real file instead of AutoCut's working copy.

## Edit with Claude (MCP)

The MCP server lets Claude open files, read the transcript, cut, and export. It talks to the same server as the browser
UI (and starts it if needed), so Claude's cuts appear live in the page. They arrive as **proposals** unless you tick
"Claude cuts apply instantly" (top right).

**Claude Code**

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

- "Open D:\videos\interview.mp4 in AutoCut, language Italian, and remove the filler words and repeated takes."
- "Read the transcript and propose cuts for anything off-topic about pricing, with a reason for each."
- "Accept all the filler cuts, then export a Premiere XML."

Tools: `open_media`, `project_status`, `get_transcript`, `get_words`, `find_text`, `analyze`, `list_cuts`, `cut_words`,
`cut_time_ranges`, `accept_cuts`, `remove_cuts`, `undo`, `fix_word`, `export_premiere_xml`, `export_edl`,
`export_subtitles`, `render_media`, `export_status`, `list_projects`, `delete_project`.

## Layout

```
backend/autocut/   server.py (API) · transcribe.py · cleanup.py · editing.py · exporters.py · mcp_server.py
frontend/          index.html · app.js · style.css (no build step)
data/              projects, models cache is in your Hugging Face cache
```
