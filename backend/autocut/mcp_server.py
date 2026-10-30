"""AutoCut MCP server: lets Claude transcribe, review and cut media in AutoCut.

It is a thin client of the AutoCut web server, so every edit Claude makes shows up
live in the browser UI. The web server is started automatically if it is not running.
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from . import cleanup
from .config import DATA_DIR, HOST, PORT, ROOT

BASE = f"http://{HOST}:{PORT}"
mcp = FastMCP(
    "autocut",
    instructions=(
        "AutoCut edits video/audio by editing its transcript. Typical flow: open_media -> (wait for "
        "project_status to say ready) -> get_transcript -> analyze and/or cut_words -> review with "
        "list_cuts -> export_premiere_xml or render_media. Cuts you make are PROPOSALS shown highlighted in "
        "the user's browser unless they enabled 'Claude cuts apply instantly'; the user (or you via "
        "accept_cuts) applies them. Word indices are stable; use get_words/find_text to get them."
    ),
)


# ------------------------------------------------------------------ plumbing

def _ensure_backend() -> None:
    try:
        if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
            return
    except httpx.HTTPError:
        pass
    log = open(DATA_DIR / "server.log", "ab")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "autocut.server:app", "--host", HOST, "--port", str(PORT)],
        cwd=str(ROOT / "backend"), stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags,
    )
    for _ in range(60):
        time.sleep(0.5)
        try:
            if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
    raise RuntimeError(f"Could not start the AutoCut server on {BASE}. See {DATA_DIR / 'server.log'}")


def _call(method: str, path: str, **kw):
    _ensure_backend()
    r = httpx.request(method, f"{BASE}/api{path}", timeout=120, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(str(detail))
    return r.json()


def _t(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def _stats_line(s: dict) -> str:
    return (f"original {_t(s['original'])}, edited {_t(s['edited'])} (removed {_t(s['removed'])}); "
            f"{s['applied']} cuts applied, {s['proposed']} waiting for approval")


def _cut_line(c: dict, words: list[dict]) -> str:
    where = (f"words {c['w0']}-{c['w1']}" if c["w0"] is not None else f"pause after word {c['gap']}")
    text = ""
    if c["w0"] is not None:
        text = ' "' + " ".join(w["w"] for w in words[c["w0"]:c["w1"] + 1])[:70] + '"'
    return (f"{c['id']} [{c['status']}/{c['kind']}] {_t(c['start'])}-{_t(c['end'])} {where}{text}"
            + (f" — {c['reason']}" if c["reason"] else ""))


# ------------------------------------------------------------------ projects

@mcp.tool()
def open_media(path: str, language: str = "auto", model: str = "") -> str:
    """Add a video/audio file (absolute path) to AutoCut and start transcribing it locally.

    language: auto, en, it, hi (Devanagari), or hinglish (Hindi in Latin letters).
    model: Whisper model; empty = default (large-v3-turbo). Use large-v3 for Hindi.
    Waits up to ~40s; if transcription is still running call project_status until it says ready.
    """
    p = _call("POST", "/projects/from-path", json={"path": path, "language": language, "model": model or None})
    pid = p["id"]
    for _ in range(40):
        time.sleep(1)
        st = _call("GET", f"/projects/{pid}")["status"]
        if st["state"] != "processing":
            break
    return f"project_id={pid}\n" + project_status(pid) + f"\nBrowser UI: {BASE}/#/p/{pid} (opens it on a timeline)"


@mcp.tool()
def list_projects() -> str:
    """List all AutoCut projects."""
    ps = _call("GET", "/projects")
    if not ps:
        return "No projects yet. Use open_media."
    return "\n".join(f"{p['id']}  {p['name']}  {p['kind']} {_t(p['duration'])}  {p['state']}  "
                     f"{p['words']} words, {p['cuts']} cuts" for p in ps)


@mcp.tool()
def project_status(project_id: str) -> str:
    """Transcription progress and edit statistics for a project."""
    p = _call("GET", f"/projects/{project_id}")
    s = p["status"]
    if s["state"] == "processing":
        return f"processing: {s['step']} {int(s['progress'] * 100)}% {s['message']} (call again in a few seconds)"
    if s["state"] == "error":
        return f"error: {s['message']}"
    return (f"ready: {p['media']['name']} ({p['media']['kind']}), language {p['language']}, "
            f"{len(p['words'])} words. {_stats_line(p['stats'])}")


@mcp.tool()
def delete_project(project_id: str) -> str:
    """Delete a project and its working files (an original opened by path is not touched)."""
    _call("DELETE", f"/projects/{project_id}")
    return "deleted"


# ------------------------------------------------------------------ reading

@mcp.tool()
def get_transcript(project_id: str, start_word: int = 0, max_words: int = 600) -> str:
    """Read the transcript as sentences, each prefixed with its word-index range and time.

    Format: `[first-last] m:ss text`. Cut text is wrapped in ~~strike~~ (applied) or ??maybe?? (proposed).
    Use get_words for exact indices inside a sentence. Page with start_word.
    """
    p = _call("GET", f"/projects/{project_id}")
    words = p["words"]
    state = [None] * len(words)
    for c in p["cuts"]:
        if c["w0"] is not None:
            for i in range(c["w0"], c["w1"] + 1):
                if state[i] != "applied":
                    state[i] = c["status"]
    out, n, i = [], 0, start_word
    sents = [s for s in cleanup._sentences(words) if s[1] >= start_word]
    for a, b in sents:
        a = max(a, start_word)
        parts, cur = [], None
        for j in range(a, b + 1):
            if state[j] != cur:
                if cur: parts.append("~~" if cur == "applied" else "??")
                if state[j]: parts.append("~~" if state[j] == "applied" else "??")
                cur = state[j]
            parts.append(words[j]["w"] + " ")
        if cur: parts.append("~~" if cur == "applied" else "??")
        out.append(f"[{a}-{b}] {_t(words[a]['s'])} {''.join(parts).strip()}")
        n += b - a + 1
        if n >= max_words:
            out.append(f"... more: call again with start_word={b + 1} (of {len(words)} words)")
            break
    return "\n".join(out) or "(empty transcript)"


@mcp.tool()
def get_words(project_id: str, start_word: int, end_word: int) -> str:
    """Exact word indices and timestamps: `index:word@seconds`, for choosing precise cut boundaries."""
    words = _call("GET", f"/projects/{project_id}")["words"]
    end_word = min(end_word, len(words) - 1, start_word + 400)
    return " ".join(f"{i}:{words[i]['w']}@{words[i]['s']:.2f}" for i in range(max(0, start_word), end_word + 1))


@mcp.tool()
def find_text(project_id: str, query: str, max_results: int = 20) -> str:
    """Find a word or phrase in the transcript (case/punctuation-insensitive); returns word index ranges."""
    words = _call("GET", f"/projects/{project_id}")["words"]
    q = [cleanup.norm(x) for x in query.split() if cleanup.norm(x)]
    toks = [cleanup.norm(w["w"]) for w in words]
    hits = []
    for i in range(len(toks) - len(q) + 1):
        if toks[i:i + len(q)] == q:
            ctx = " ".join(w["w"] for w in words[max(0, i - 4):i + len(q) + 4])
            hits.append(f"words {i}-{i + len(q) - 1} @ {_t(words[i]['s'])}: …{ctx}…")
            if len(hits) >= max_results:
                break
    return "\n".join(hits) or "no matches"


# ------------------------------------------------------------------ analysis + cuts

@mcp.tool()
def analyze(project_id: str, fillers: bool = True, soft_fillers: bool = False, repeats: bool = True,
            takes: bool = True, silences: bool = True, min_silence_seconds: float = 0.8) -> str:
    """Run the built-in detectors (filler words, stutters, repeated takes/false starts, pauses).

    Results are added as proposals (nothing is removed until accepted). Review them with list_cuts.
    soft_fillers also flags words like 'like', 'you know', 'cioè', 'matlab'.
    """
    r = _call("POST", f"/projects/{project_id}/analyze", json={
        "fillers": fillers, "soft_fillers": soft_fillers, "repeats": repeats, "takes": takes,
        "silences": silences, "min_silence": min_silence_seconds})
    f = r["found"]
    return (f"Added proposals: {f['filler']} fillers, {f['repeat']} repeated words, {f['take']} repeated takes/false "
            f"starts, {f['silence']} pauses. {_stats_line(r['stats'])}")


@mcp.tool()
def list_cuts(project_id: str, status: str = "", kind: str = "", limit: int = 150) -> str:
    """List cuts. status: applied | proposed | '' (all). kind: manual, filler, repeat, take, silence, claude."""
    p = _call("GET", f"/projects/{project_id}")
    cuts = sorted(p["cuts"], key=lambda c: c["start"])
    cuts = [c for c in cuts if (not status or c["status"] == status) and (not kind or c["kind"] == kind)]
    lines = [_cut_line(c, p["words"]) for c in cuts[:limit]]
    if len(cuts) > limit:
        lines.append(f"... {len(cuts) - limit} more")
    return "\n".join(lines) or "no cuts"


class WordRange(BaseModel):
    start_word: int
    end_word: int
    reason: str = ""


@mcp.tool()
def cut_words(project_id: str, ranges: list[WordRange]) -> str:
    """Cut one or more inclusive word-index ranges. Each should carry a short reason the user will see.

    The cut includes the words only; surrounding pauses are trimmed slightly. Cuts appear as proposals
    unless the user turned on instant apply.
    """
    r = _call("POST", f"/projects/{project_id}/cuts/batch", json={"cuts": [
        {"w0": x.start_word, "w1": x.end_word, "kind": "claude", "reason": x.reason} for x in ranges]})
    status = r["cuts"][0]["status"] if r["cuts"] else "applied"
    return f"{len(r['cuts'])} cut(s) added as {status}. {_stats_line(r['stats'])}"


class TimeRange(BaseModel):
    start: float
    end: float
    reason: str = ""


@mcp.tool()
def cut_time_ranges(project_id: str, ranges: list[TimeRange]) -> str:
    """Cut raw time ranges (seconds in the original media), e.g. a silent or off-screen section."""
    r = _call("POST", f"/projects/{project_id}/cuts/batch", json={"cuts": [
        {"start": x.start, "end": x.end, "kind": "claude", "reason": x.reason} for x in ranges]})
    status = r["cuts"][0]["status"] if r["cuts"] else "applied"
    return f"{len(r['cuts'])} cut(s) added as {status}. {_stats_line(r['stats'])}"


@mcp.tool()
def accept_cuts(project_id: str, cut_ids: list[str] | None = None, kind: str = "") -> str:
    """Apply proposed cuts: by ids, by kind, or all proposals if neither is given."""
    s = _call("POST", f"/projects/{project_id}/cuts/bulk",
              json={"action": "accept", "ids": cut_ids or None, "kind": kind or None, "status": "proposed"})
    return _stats_line(s)


@mcp.tool()
def remove_cuts(project_id: str, cut_ids: list[str] | None = None, kind: str = "", status: str = "") -> str:
    """Delete cuts (restoring that media): by ids, or by kind/status. Needs at least one filter."""
    if not (cut_ids or kind or status):
        return "Give cut_ids, kind or status so you do not clear everything by accident."
    s = _call("POST", f"/projects/{project_id}/cuts/bulk",
              json={"action": "clear", "ids": cut_ids or None, "kind": kind or None, "status": status or None})
    return _stats_line(s)


@mcp.tool()
def undo(project_id: str) -> str:
    """Undo the last change to the cut list."""
    r = _call("POST", f"/projects/{project_id}/undo")
    return ("undone. " if r["ok"] else "nothing to undo. ") + _stats_line(r["stats"])


@mcp.tool()
def fix_word(project_id: str, word_index: int, text: str) -> str:
    """Correct a mis-transcribed word without changing timing."""
    _call("PATCH", f"/projects/{project_id}/words/{word_index}", json={"w": text})
    return "ok"


# ------------------------------------------------------------------ export

def _export(project_id: str, kind: str, out_dir: str) -> str:
    r = _call("POST", f"/projects/{project_id}/export", json={"kind": kind, "out_dir": out_dir or None})
    return r["file"]


@mcp.tool()
def export_premiere_xml(project_id: str, output_folder: str = "") -> str:
    """Write a Premiere Pro XML (Final Cut Pro 7 xmeml) of the edit. In Premiere: File > Import.

    The XML links to the original media file by path, so keep that file where it is.
    Only applied cuts are included, not proposals.
    """
    return "Saved " + _export(project_id, "xml", output_folder)


@mcp.tool()
def export_edl(project_id: str, output_folder: str = "") -> str:
    """Write a CMX3600 EDL of the edit."""
    return "Saved " + _export(project_id, "edl", output_folder)


@mcp.tool()
def export_subtitles(project_id: str, format: str = "srt", output_folder: str = "") -> str:
    """Write subtitles (srt or vtt) or the plain transcript (txt) for the edited timeline."""
    if format not in ("srt", "vtt", "txt"):
        return "format must be srt, vtt or txt"
    return "Saved " + _export(project_id, format, output_folder)


@mcp.tool()
def render_media(project_id: str, format: str = "", output_folder: str = "") -> str:
    """Render the edited video/audio with ffmpeg. format: mp4 (video) or m4a/mp3/wav (audio).

    Waits up to ~40s; for long files call export_status until done.
    """
    _call("POST", f"/projects/{project_id}/export",
          json={"kind": "media", "format": format or None, "out_dir": output_folder or None})
    return export_status(project_id, wait=40)


@mcp.tool()
def export_status(project_id: str, wait: int = 0) -> str:
    """Progress of a render_media job."""
    for _ in range(max(1, wait)):
        e = _call("GET", f"/projects/{project_id}/export-status")
        if e["state"] != "running":
            break
        time.sleep(1)
    if e["state"] == "done":
        return f"Rendered: {e['file']}"
    if e["state"] == "error":
        return f"Render failed: {e['message']}"
    return f"{e['state']} {int(e['progress'] * 100)}%"


# ------------------------------------------------------------------ sequences (timeline)

def _layer_text(l: dict, indent: str) -> str:
    sy = l["sync"]
    if l["mode"] == "free":
        where = f"starts at {_t(l['start'])}"
    else:
        where = f"{sy['state']} {int((sy['confidence'] or 0) * 100)}%, offset {l['offset']:+.3f}s"
        if abs(sy.get("drift_ppm") or 0) > 0.5:
            where += f", drift {sy['drift_ppm']:.1f}ppm corrected"
    flags = ("muted " if l["mute"] else "") + ("replaces main audio " if l["replace_main_audio"] else "")
    if l["media"].get("has_video"):
        flags += (f"picture {'on' if l['v_on'] else 'off'} on V{l['v_row'] + 1} at {l['v_x'] * 100:.0f}%,{l['v_y'] * 100:.0f}% "
                  f"size {l['v_scale'] * 100:.0f}% opacity {l['v_opacity'] * 100:.0f}% ")
    return (f"{indent}layer_id={l['id']} \"{l['name']}\" {where}; gain {l['gain_db']:+.1f}dB, fades {l['fade_in']}/{l['fade_out']}s "
            f"{flags}" + (f"- {sy['message']}" if sy.get("message") else "")).rstrip()


def _seq_text(s: dict) -> str:
    st = s["settings"]
    head = (f"sequence_id={s['id']} \"{s['name']}\" {_t(s['duration'])}, "
            + (f"{st['width']}x{st['height']} {st['fps_num'] / st['fps_den']:.2f} fps" if st["has_video"] else "audio only"))
    clips = []
    for i, c in enumerate(s["clips"]):
        clips.append(f"  {i}. clip_id={c['id']} {c['name']} ({c['kind']}) at {_t(c['start'])}, length {_t(c['length'])}")
        clips += [_layer_text(l, "       ") for l in c["layers"]]
    if s.get("free_layers"):
        clips.append("  Music / free layers:")
        clips += [_layer_text(l, "       ") for l in s["free_layers"]]
    bin_only = [i["name"] + f" (project_id={i['id']}, {i['state']})" for i in s["items"]
                if not any(c["pid"] == i["id"] for c in s["clips"])]
    marks = [f"  marker {m['name'] or '-'} at {_t(m['t'])} ({m['color']})" for m in s.get("markers", [])]
    return "\n".join([head, *clips, *marks] + ([f"In bin, not on timeline: {', '.join(bin_only)}"] if bin_only else [])
                     + [f"Browser UI: {BASE}/#/s/{s['id']}"])


@mcp.tool()
def create_sequence(name: str = "") -> str:
    """Create an empty timeline (sequence). Import media into it, then arrange the clips in order."""
    return _seq_text(_call("POST", "/sequences", json={"name": name}))


@mcp.tool()
def list_sequences() -> str:
    """List timelines."""
    ss = _call("GET", "/sequences")
    return "\n".join(f"{s['id']}  {s['name']}  {s['clips']} clips, {_t(s['duration'])}" for s in ss) or "No sequences yet."


@mcp.tool()
def sequence_status(sequence_id: str) -> str:
    """Show the clip order, positions and lengths of a timeline (cuts already applied)."""
    return _seq_text(_call("GET", f"/sequences/{sequence_id}"))


@mcp.tool()
def import_to_sequence(sequence_id: str, paths: list[str], language: str = "auto", model: str = "") -> str:
    """Import video/audio files (absolute paths) into a timeline; they are appended in the order given.

    Each file is transcribed in the background (watch project_status / sequence_status).
    """
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/import-path", json={
        "paths": paths, "language": language, "model": model or None, "add_to_timeline": True}))


@mcp.tool()
def add_clip(sequence_id: str, project_id: str, position: int = -1) -> str:
    """Place media (a project_id) on the timeline at a 0-based position; -1 appends at the end."""
    r = _call("POST", f"/sequences/{sequence_id}/clips", json={"project_id": project_id,
                                                               "index": None if position < 0 else position})
    return _seq_text(r["sequence"])


@mcp.tool()
def move_clip(sequence_id: str, clip_id: str, position: int) -> str:
    """Move a timeline clip to a new 0-based position in the order."""
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/clips/{clip_id}/move", json={"index": position}))


@mcp.tool()
def remove_clip(sequence_id: str, clip_id: str, ripple: bool = False) -> str:
    """Remove a clip from the timeline (the media and its transcript are kept).

    ripple=False leaves a gap where it was; ripple=True slides the clips after it left to close the gap."""
    return _seq_text(_call("DELETE", f"/sequences/{sequence_id}/clips/{clip_id}" + ("?ripple=true" if ripple else "")))


@mcp.tool()
def place_clip(sequence_id: str, clip_id: str, start_seconds: float) -> str:
    """Move a clip to a start time on the timeline. Gaps are allowed; if the spot is taken it slides to the nearest free one."""
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/clips/{clip_id}/move", json={"start": start_seconds}))


@mcp.tool()
def split_clip(sequence_id: str, clip_id: str, at_seconds: float) -> str:
    """Razor: cut a clip in two at a timeline time inside it. Its layers are copied to the second half."""
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/clips/{clip_id}/split", json={"time": at_seconds})["sequence"])


@mcp.tool()
def trim_clip(sequence_id: str, clip_id: str, edge: str, at_seconds: float) -> str:
    """Trim a clip: edge 'in' moves its start to the timeline time, 'out' moves its end. The other edge stays put."""
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/clips/{clip_id}/trim", json={"edge": edge, "time": at_seconds}))


@mcp.tool()
def close_gaps(sequence_id: str) -> str:
    """Slide all clips together from time 0, closing every gap."""
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/close-gaps"))


@mcp.tool()
def add_marker(sequence_id: str, at_seconds: float, name: str = "", color: str = "red") -> str:
    """Drop a marker on the timeline (colors: red, blue, green, yellow, purple). Markers are included in the Premiere XML."""
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/markers", json={"time": at_seconds, "name": name, "color": color}))


@mcp.tool()
def set_track(sequence_id: str, track: str, mute: bool | None = None, solo: bool | None = None, lock: bool | None = None) -> str:
    """Mute / solo / lock a track by label: V1 (mute = hide the picture), A1 (the clips' own sound), A2, A3… (layers)."""
    body = {k: v for k, v in {"mute": mute, "solo": solo, "lock": lock}.items() if v is not None}
    return _seq_text(_call("PATCH", f"/sequences/{sequence_id}/tracks/{track}", json=body))


@mcp.tool()
def undo_timeline(sequence_id: str) -> str:
    """Undo the last timeline edit (move, trim, split, delete, marker, track switch, layer change)."""
    r = _call("POST", f"/sequences/{sequence_id}/undo")
    return ("undone.\n" if r["ok"] else "nothing to undo.\n") + _seq_text(r["sequence"])


@mcp.tool()
def unlink_layer(sequence_id: str, layer_id: str) -> str:
    """Turn a synced layer into a free layer that stays where it is but no longer follows its clip's cuts."""
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/layers/{layer_id}/unlink"))


@mcp.tool()
def add_layer(sequence_id: str, clip_id: str, paths: list[str]) -> str:
    """Attach extra recordings (external mic, second camera, screen recording) to a clip and auto-sync them.

    AutoCut matches each file's audio to the clip's audio and reports a confidence score, the time offset
    and any clock drift (which it corrects). Check sequence_status after a few seconds; if the state is
    'low' or 'failed', use nudge_layer. The layer follows every cut made on the clip.
    """
    return _seq_text(_call("POST", f"/sequences/{sequence_id}/clips/{clip_id}/layers", json={"paths": paths}))


@mcp.tool()
def add_music_layer(sequence_id: str, paths: list[str], start_seconds: float = 0.0) -> str:
    """Add music or other sound to the whole sequence, placed by time (not synced to the video)."""
    s = _call("POST", f"/sequences/{sequence_id}/layers", json={"paths": paths})
    for l in s["free_layers"][-len(paths):]:
        if start_seconds:
            s = _call("PATCH", f"/sequences/{sequence_id}/layers/{l['id']}", json={"start": start_seconds})
    return _seq_text(s)


@mcp.tool()
def set_layer(sequence_id: str, layer_id: str, gain_db: float | None = None, mute: bool | None = None,
              fade_in_seconds: float | None = None, fade_out_seconds: float | None = None,
              trim_in_seconds: float | None = None, trim_out_seconds: float | None = None,
              replace_main_audio: bool | None = None, start_seconds: float | None = None,
              show_picture: bool | None = None, picture_corner: str = "", picture_size_percent: float | None = None,
              picture_x_percent: float | None = None, picture_y_percent: float | None = None,
              picture_opacity_percent: float | None = None) -> str:
    """Change a layer: volume in dB, mute, fades, trim (in the layer's own file time), replace the camera audio,
    or (music layers) the start position on the sequence. Only the values you pass are changed.

    For a layer that has a picture (second camera, screen recording, b-roll): show_picture, picture_corner
    ('top-left', 'top-right', 'bottom-left', 'bottom-right' or 'full'), picture_size_percent (width as a % of the frame),
    picture_x_percent / picture_y_percent (top-left corner as a % of the frame) and picture_opacity_percent.
    Fades also fade the picture."""
    body = {k: v for k, v in {"gain_db": gain_db, "mute": mute, "fade_in": fade_in_seconds, "fade_out": fade_out_seconds,
                              "trim_in": trim_in_seconds, "trim_out": trim_out_seconds,
                              "replace_main_audio": replace_main_audio, "start": start_seconds, "v_on": show_picture,
                              "v_scale": None if picture_size_percent is None else picture_size_percent / 100,
                              "v_x": None if picture_x_percent is None else picture_x_percent / 100,
                              "v_y": None if picture_y_percent is None else picture_y_percent / 100,
                              "v_opacity": None if picture_opacity_percent is None else picture_opacity_percent / 100}.items()
            if v is not None}
    if picture_corner:
        seq = _call("GET", f"/sequences/{sequence_id}")
        layer = next((l for c in seq["clips"] for l in c["layers"] if l["id"] == layer_id), None) or             next((l for l in seq["free_layers"] if l["id"] == layer_id), None)
        if not layer or not layer["media"].get("has_video"):
            return "That layer has no picture."
        W, H = seq["settings"]["width"], seq["settings"]["height"]
        lw, lh = layer["media"]["width"] or 16, layer["media"]["height"] or 9
        full = picture_corner == "full"
        sc = body.get("v_scale") or (min(1.0, (H / W) / (lh / lw)) if full else (layer["v_scale"] if layer["v_scale"] < 0.9 else 0.32))
        hf, m = sc * (lh / lw) * (W / H), 0.03
        pos = {"top-left": (m, m), "top-right": (1 - m - sc, m), "bottom-left": (m, 1 - m - hf),
               "bottom-right": (1 - m - sc, 1 - m - hf), "full": ((1 - sc) / 2, (1 - hf) / 2)}.get(picture_corner)
        if not pos:
            return "picture_corner must be top-left, top-right, bottom-left, bottom-right or full."
        body.update(v_x=pos[0], v_y=pos[1], v_scale=sc)
    return _seq_text(_call("PATCH", f"/sequences/{sequence_id}/layers/{layer_id}", json=body))


@mcp.tool()
def nudge_layer(sequence_id: str, layer_id: str, frames: float = 0.0, seconds: float = 0.0) -> str:
    """Move a synced layer later (positive) or earlier (negative) relative to the video, by frames and/or seconds."""
    s = _call("GET", f"/sequences/{sequence_id}")
    fps = s["settings"]["fps_num"] / s["settings"]["fps_den"] if s["settings"]["has_video"] else 30.0
    return _seq_text(_call("PATCH", f"/sequences/{sequence_id}/layers/{layer_id}",
                           json={"offset_delta": seconds + frames / fps}))


@mcp.tool()
def sync_layer(sequence_id: str, layer_id: str) -> str:
    """Run the automatic audio sync again for a layer (after trimming the clip, or to retry a failed sync)."""
    _call("POST", f"/sequences/{sequence_id}/layers/{layer_id}/sync")
    return "Matching audio… call sequence_status in a few seconds for the result."


@mcp.tool()
def remove_layer(sequence_id: str, layer_id: str) -> str:
    """Remove a layer from the timeline (the file itself is not touched)."""
    return _seq_text(_call("DELETE", f"/sequences/{sequence_id}/layers/{layer_id}"))


@mcp.tool()
def export_sequence(sequence_id: str, kind: str = "xml", output_folder: str = "") -> str:
    """Export the whole timeline: kind = xml (Premiere Pro), edl, srt, vtt or txt."""
    r = _call("POST", f"/sequences/{sequence_id}/export", json={"kind": kind, "out_dir": output_folder or None})
    return "Saved " + r["file"]


@mcp.tool()
def render_sequence(sequence_id: str, format: str = "", output_folder: str = "") -> str:
    """Render the whole timeline to one video/audio file with ffmpeg (waits up to ~40s, then poll)."""
    _call("POST", f"/sequences/{sequence_id}/export",
          json={"kind": "media", "format": format or None, "out_dir": output_folder or None})
    for _ in range(40):
        e = _call("GET", f"/sequences/{sequence_id}/export-status")
        if e["state"] != "running":
            break
        time.sleep(1)
    if e["state"] == "done":
        return f"Rendered: {e['file']}"
    if e["state"] == "error":
        return f"Render failed: {e['message']}"
    return f"still rendering ({int(e['progress'] * 100)}%); call render_sequence_status"


@mcp.tool()
def render_sequence_status(sequence_id: str) -> str:
    """Progress of a render_sequence job."""
    e = _call("GET", f"/sequences/{sequence_id}/export-status")
    return f"Rendered: {e['file']}" if e["state"] == "done" else (
        f"Render failed: {e['message']}" if e["state"] == "error" else f"{e['state']} {int(e['progress'] * 100)}%")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
