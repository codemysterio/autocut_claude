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
    return f"project_id={pid}\n" + project_status(pid) + f"\nBrowser UI: {BASE}/#/p/{pid}"


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
