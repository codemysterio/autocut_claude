"""AutoCut web server: REST API + static frontend."""
import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cleanup, editing, exporters, media, sequences, store, syncing, transcribe
from .config import DATA_DIR, DEFAULT_MODEL, FRONTEND_DIR, PORT

app = FastAPI(title="AutoCut")
SETTINGS_FILE = DATA_DIR / "settings.json"
TEXT_EXPORTS = {"xml", "edl", "srt", "vtt", "txt"}


# ------------------------------------------------------------------ helpers

def load_settings() -> dict:
    s = {"claude_auto_apply": False}
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return s


def proj(pid: str) -> dict:
    try:
        return store.get(pid)
    except KeyError:
        raise HTTPException(404, "Project not found")


def payload(p: dict) -> dict:
    return {**p, "stats": editing.stats(p), "languages": transcribe.LANGUAGES}


def need_transcript(p: dict) -> None:
    if not p["words"]:
        raise HTTPException(409, "This project has no transcript yet")


# ------------------------------------------------------------------ misc

@app.get("/api/health")
def health():
    return {"ok": True, "model": DEFAULT_MODEL, "port": PORT}


@app.get("/api/config")
def config():
    return {"languages": transcribe.LANGUAGES, "default_model": DEFAULT_MODEL,
            "models": ["large-v3-turbo", "large-v3", "medium", "small", "base"],
            "settings": load_settings()}


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    s = load_settings()
    s.update({k: v for k, v in body.items() if k in ("claude_auto_apply",)})
    SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
    return s


# ------------------------------------------------------------------ projects

@app.get("/api/projects")
def list_projects():
    return store.list_all()


class FromPath(BaseModel):
    path: str
    language: str = "auto"
    model: str | None = None
    transcribe: bool = True


def _create_from_file(path: Path, name: str, owns: bool, language: str, model: str | None, start: bool) -> dict:
    try:
        info = media.probe(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    p = store.create(name, info, owns)
    if start:
        transcribe.submit(p["id"], language, model)
    return payload(p)


def _register_upload(dest: Path, tmp_dir: Path, name: str, language: str, model: str | None) -> dict:
    try:
        info = media.probe(dest)
    except ValueError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(400, f"{name}: {e}")
    p = store.create(dest.stem, info, True)
    media_dir = store.project_dir(p["id"]) / "media"
    media_dir.mkdir(exist_ok=True)
    final = media_dir / dest.name
    shutil.move(str(dest), final)  # same drive: a rename, not a copy
    shutil.rmtree(tmp_dir, ignore_errors=True)
    p["media"]["path"] = str(final.resolve())
    store.save(p)
    transcribe.submit(p["id"], language, model)
    return p


async def _ingest_upload(file: UploadFile, language: str, model: str | None) -> dict:
    tmp_dir = DATA_DIR / "uploads" / store.new_id()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / Path(file.filename or "upload").name
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return _register_upload(dest, tmp_dir, file.filename or "upload", language, model)


@app.post("/api/projects")
async def upload_project(file: UploadFile = File(...), language: str = Form("auto"), model: str = Form("")):
    return payload(await _ingest_upload(file, language, model or None))


@app.post("/api/projects/from-path")
def project_from_path(body: FromPath):
    path = Path(body.path.strip('"'))
    if not path.is_file():
        raise HTTPException(400, f"File not found: {path}")
    return _create_from_file(path, path.stem, False, body.language, body.model, body.transcribe)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return payload(proj(pid))


@app.get("/api/projects/{pid}/version")
def get_version(pid: str):
    p = proj(pid)
    return {"version": p["version"], "state": p["status"]["state"]}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    proj(pid)
    store.delete(pid)
    sequences.remove_project(pid)
    return {"ok": True}


class DuplicateReq(BaseModel):
    keep_cuts: bool = False


@app.post("/api/projects/{pid}/duplicate")
def duplicate_project(pid: str, body: DuplicateReq):
    """A fresh copy of this media's transcript (without cuts unless asked), e.g. to start over."""
    proj(pid)
    return payload(store.duplicate(pid, body.keep_cuts))


class TranscribeReq(BaseModel):
    language: str = "auto"
    model: str | None = None


@app.post("/api/projects/{pid}/transcribe")
def retranscribe(pid: str, body: TranscribeReq):
    p = proj(pid)
    if p["status"]["state"] == "processing":
        raise HTTPException(409, "Already processing")
    transcribe.submit(pid, body.language, body.model)
    return {"ok": True}


@app.get("/api/projects/{pid}/media")
def get_media(pid: str):
    p = proj(pid)
    d = store.project_dir(pid)
    if not p["proxy"]:
        transcribe.ensure_proxy(pid)
    for name in ("proxy.mp4", "proxy.m4a"):
        f = d / name
        if p["proxy"] and f.exists():
            return FileResponse(f)
    return FileResponse(p["media"]["path"])


# ------------------------------------------------------------------ editing

class CutReq(BaseModel):
    w0: int | None = None
    w1: int | None = None
    start: float | None = None
    end: float | None = None
    kind: str = "manual"
    reason: str = ""
    status: str | None = None


def _make_cut(p: dict, c: CutReq) -> dict:
    status = c.status or ("proposed" if c.kind == "claude" and not load_settings()["claude_auto_apply"]
                          else "applied")
    try:
        if c.w0 is not None:
            need_transcript(p)
            return editing.add_word_cut(p, c.w0, c.w1 if c.w1 is not None else c.w0, c.kind, c.reason, status)
        if c.start is not None and c.end is not None:
            return editing.add_time_cut(p, c.start, c.end, c.kind, c.reason, status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    raise HTTPException(400, "Give w0/w1 (word indices) or start/end (seconds)")


@app.post("/api/projects/{pid}/cuts")
def add_cut(pid: str, body: CutReq):
    p = proj(pid)
    with store._lock:
        store.snapshot(p)
        cut = _make_cut(p, body)
        store.save(p)
    return {"cut": cut, "stats": editing.stats(p)}


class CutBatch(BaseModel):
    cuts: list[CutReq]


@app.post("/api/projects/{pid}/cuts/batch")
def add_cuts(pid: str, body: CutBatch):
    p = proj(pid)
    with store._lock:
        store.snapshot(p)
        made = [_make_cut(p, c) for c in body.cuts]
        store.save(p)
    return {"cuts": made, "stats": editing.stats(p)}


@app.post("/api/projects/{pid}/cuts/{cid}/accept")
def accept_cut(pid: str, cid: str):
    p = proj(pid)
    with store._lock:
        store.snapshot(p)
        for c in p["cuts"]:
            if c["id"] == cid:
                c["status"] = "applied"
                store.save(p)
                return editing.stats(p)
    raise HTTPException(404, "Cut not found")


@app.delete("/api/projects/{pid}/cuts/{cid}")
def remove_cut(pid: str, cid: str):
    p = proj(pid)
    with store._lock:
        store.snapshot(p)
        before = len(p["cuts"])
        p["cuts"] = [c for c in p["cuts"] if c["id"] != cid]
        if len(p["cuts"]) == before:
            raise HTTPException(404, "Cut not found")
        store.save(p)
    return editing.stats(p)


class BulkReq(BaseModel):
    action: str  # accept | reject | clear
    kind: str | None = None
    status: str | None = None
    ids: list[str] | None = None


@app.post("/api/projects/{pid}/cuts/bulk")
def bulk_cuts(pid: str, body: BulkReq):
    p = proj(pid)
    with store._lock:
        store.snapshot(p)

        def match(c):
            return ((body.kind is None or c["kind"] == body.kind)
                    and (body.status is None or c["status"] == body.status)
                    and (body.ids is None or c["id"] in body.ids))

        if body.action == "accept":
            for c in p["cuts"]:
                if match(c) and c["status"] == "proposed":
                    c["status"] = "applied"
        elif body.action in ("reject", "clear"):
            p["cuts"] = [c for c in p["cuts"] if not match(c)]
        else:
            raise HTTPException(400, "action must be accept, reject or clear")
        store.save(p)
    return editing.stats(p)


@app.post("/api/projects/{pid}/undo")
def undo(pid: str):
    p = proj(pid)
    return {"ok": store.undo(p), "stats": editing.stats(p)}


@app.post("/api/projects/{pid}/redo")
def redo(pid: str):
    p = proj(pid)
    return {"ok": store.redo(p), "stats": editing.stats(p)}


class WordEdit(BaseModel):
    w: str


@app.patch("/api/projects/{pid}/words/{i}")
def edit_word(pid: str, i: int, body: WordEdit):
    p = proj(pid)
    if not 0 <= i < len(p["words"]):
        raise HTTPException(404, "No such word")
    p["words"][i]["w"] = body.w.strip()
    store.save(p)
    return {"ok": True}


# ------------------------------------------------------------------ analysis

class AnalyzeReq(BaseModel):
    fillers: bool = True
    soft_fillers: bool = False
    repeats: bool = True
    takes: bool = True
    silences: bool = True
    min_silence: float = 0.8
    keep_silence: float = 0.3


def _dupe_word(p: dict, w0: int, w1: int) -> bool:
    return any(editing.covers(c, w0, w1) for c in p["cuts"])


def _dupe_time(p: dict, start: float, end: float) -> bool:
    for c in p["cuts"]:
        ov = min(c["end"], end) - max(c["start"], start)
        if ov > 0.5 * (end - start):
            return True
    return False


def run_analysis(p: dict, o: AnalyzeReq) -> dict:
    words, dur = p["words"], p["media"]["duration"]
    found = {"filler": 0, "repeat": 0, "take": 0, "silence": 0}
    with store._lock:
        store.snapshot(p)
        jobs = []
        if o.takes:
            jobs.append(("take", cleanup.find_repeated_takes(words)))
        if o.repeats:
            jobs.append(("repeat", cleanup.find_repeated_words(words)))
        if o.fillers:
            jobs.append(("filler", cleanup.find_fillers(words, p["language"], o.soft_fillers)))
        for kind, hits in jobs:
            for w0, w1, reason in hits:
                if not _dupe_word(p, w0, w1):
                    editing.add_word_cut(p, w0, w1, kind, reason, "proposed")
                    found[kind] += 1
        if o.silences:
            for s, e, idx, length in cleanup.find_silences(words, dur, o.min_silence, o.keep_silence):
                if not _dupe_time(p, s, e):
                    editing.add_time_cut(p, s, e, "silence", f"Pause of {length:.1f}s", "proposed", gap_after=idx)
                    found["silence"] += 1
        store.save(p)
    return {"found": found, "stats": editing.stats(p)}


@app.post("/api/projects/{pid}/analyze")
def analyze(pid: str, body: AnalyzeReq):
    p = proj(pid)
    need_transcript(p)
    return run_analysis(p, body)


# ------------------------------------------------------------------ export

class ExportReq(BaseModel):
    kind: str            # xml | edl | srt | vtt | txt | media
    format: str | None = None
    out_dir: str | None = None


def _render_job(pid: str, fmt: str | None, out_dir: str | None) -> None:
    p = store.get(pid)
    try:
        out = exporters.render_media(p, fmt, out_dir)
        p["export"] = {"state": "done", "progress": 1.0, "file": str(out), "message": ""}
    except Exception as e:
        p["export"] = {"state": "error", "progress": 0.0, "file": None, "message": str(e)[:500]}
    store.save(p)


@app.post("/api/projects/{pid}/export")
def export(pid: str, body: ExportReq):
    p = proj(pid)
    need_transcript(p)
    if body.kind in TEXT_EXPORTS:
        try:
            path = exporters.write_text_export(p, body.kind, body.out_dir)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"state": "done", "file": str(path)}
    if body.kind == "media":
        if p["export"]["state"] == "running":
            raise HTTPException(409, "An export is already running")
        p["export"] = {"state": "running", "progress": 0.0, "file": None, "message": ""}
        store.save(p)
        threading.Thread(target=_render_job, args=(pid, body.format, body.out_dir), daemon=True).start()
        return {"state": "running"}
    raise HTTPException(400, "kind must be xml, edl, srt, vtt, txt or media")


@app.get("/api/projects/{pid}/export-status")
def export_status(pid: str):
    return proj(pid)["export"]


@app.get("/api/projects/{pid}/download/{kind}")
def download(pid: str, kind: str):
    p = proj(pid)
    if kind == "media":
        f = p["export"].get("file")
        if not f or not Path(f).exists():
            raise HTTPException(404, "Render the media first")
        return FileResponse(f, filename=Path(f).name)
    need_transcript(p)
    try:
        path = exporters.write_text_export(p, kind)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return FileResponse(path, filename=path.name)


# ------------------------------------------------------------------ native file picker

_PICKER = r"""
import json, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
exts = " ".join("*" + e for e in sorted(%s))
paths = filedialog.askopenfilenames(parent=root, title="Choose video or audio files",
                                    filetypes=[("Video and audio", exts), ("All files", "*.*")])
print(json.dumps(list(paths)))
"""


@app.post("/api/pick-files")
def pick_files():
    """Open the Windows file dialog on this PC and return the chosen paths, so nothing has to be uploaded."""
    code = _PICKER % repr(sorted(media.VIDEO_EXT | media.AUDIO_EXT))
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=600,
                           creationflags=media._NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "No file was chosen")
    if r.returncode != 0:
        raise HTTPException(500, "Could not open the file dialog: " + r.stderr.strip()[-200:])
    return {"paths": json.loads(r.stdout.strip().splitlines()[-1])}


# ------------------------------------------------------------------ timeline media: waveform + filmstrip

PEAKS_PER_SEC = 100
_prep_lock = threading.Lock()


@app.get("/api/projects/{pid}/peaks")
def get_peaks(pid: str):
    """Waveform data: 100 peak values (0-255) per second of audio."""
    p = proj(pid)
    f = store.project_dir(pid) / "peaks.json"
    if not f.exists():
        if not p["media"]["has_audio"]:
            return {"rate": PEAKS_PER_SEC, "peaks": []}
        import numpy as np

        with _prep_lock:
            if not f.exists():
                r = subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", p["media"]["path"], "-vn", "-ac", "1", "-ar", "8000",
                     "-f", "s16le", "-"], capture_output=True, creationflags=media._NO_WINDOW)
                if r.returncode != 0:
                    raise HTTPException(500, "Could not read audio for the waveform")
                a = np.abs(np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)) / 32768.0
                step = 8000 // PEAKS_PER_SEC
                n = len(a) // step
                peaks = a[:n * step].reshape(n, step).max(axis=1) if n else np.zeros(0)
                data = np.minimum(255, (peaks ** 0.6 * 255)).astype(int).tolist()
                f.write_text(json.dumps({"rate": PEAKS_PER_SEC, "peaks": data}), encoding="utf-8")
    return FileResponse(f, media_type="application/json")


@app.get("/api/projects/{pid}/sprite.jpg")
def get_sprite(pid: str):
    p = proj(pid)
    if not p["media"]["has_video"]:
        raise HTTPException(404, "Audio file has no picture")
    d = store.project_dir(pid)
    f = d / "sprite.jpg"
    if not f.exists():
        with _prep_lock:
            if not f.exists():
                proxy = d / "proxy.mp4"
                src = str(proxy) if p["proxy"] and proxy.exists() else p["media"]["path"]
                if not media.make_sprite(src, f, p["media"]["duration"]):
                    raise HTTPException(500, "Could not make thumbnails")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# ------------------------------------------------------------------ sequences (the timeline)

def seq_or_404(sid: str) -> dict:
    try:
        return sequences.get(sid)
    except KeyError:
        raise HTTPException(404, "Sequence not found")


def item_summary(pid: str) -> dict | None:
    try:
        p = store.get(pid)
    except KeyError:
        return None
    m, s = p["media"], p["status"]
    return {"id": pid, "name": p["name"], "kind": m["kind"], "duration": m["duration"],
            "has_video": m["has_video"], "has_audio": m["has_audio"], "state": s["state"],
            "progress": s["progress"], "step": s["step"], "message": s["message"], "proxy": p["proxy"], "proxy_progress": p.get("proxy_progress", 0.0),
            "language": p["language"], "words": len(p["words"]), "version": p["version"], "width": m["width"], "height": m["height"],
            "sprite_count": media.sprite_count(m["duration"]), "path": m["path"]}


def seq_payload(s: dict) -> dict:
    L = sequences.layout(s)
    return {"id": s["id"], "name": s["name"], "version": sequences.combined_version(s),
            "items": [i for i in (item_summary(pid) for pid in s["items"]) if i],
            "clips": L["clips"], "duration": L["duration"], "free_layers": L["free_layers"],
            "markers": L["markers"], "tracks": L["tracks"],
            "settings": exporters.seq_settings(sequences.projects_in_order(s)), "export": s["export"]}


@app.get("/api/sequences")
def list_sequences():
    return sequences.list_all()


class NewSeq(BaseModel):
    name: str = ""


@app.post("/api/sequences")
def new_sequence(body: NewSeq):
    name = body.name.strip() or f"Sequence {len(sequences.list_all()) + 1}"
    return seq_payload(sequences.create(name))


@app.post("/api/sequences/for-project/{pid}")
def sequence_for_project(pid: str):
    """The sequence that contains this media (made on demand), so old project links keep working."""
    p = proj(pid)
    s = sequences.find_for_project(pid)
    if not s:
        s = sequences.create(p["name"])
        sequences.add_clip(s, pid)
        sequences.save(s)
    return seq_payload(s)


@app.get("/api/sequences/{sid}")
def get_sequence(sid: str):
    return seq_payload(seq_or_404(sid))


@app.get("/api/sequences/{sid}/version")
def sequence_version(sid: str):
    return {"version": sequences.combined_version(seq_or_404(sid))}


@app.patch("/api/sequences/{sid}")
def rename_sequence(sid: str, body: NewSeq):
    s = seq_or_404(sid)
    if body.name.strip():
        s["name"] = body.name.strip()
        sequences.save(s)
    return {"ok": True}


@app.delete("/api/sequences/{sid}")
def delete_sequence(sid: str):
    seq_or_404(sid)
    sequences.delete(sid)
    return {"ok": True}


@app.post("/api/sequences/{sid}/import")
async def import_files(sid: str, files: list[UploadFile] = File(...), language: str = Form("auto"),
                       model: str = Form(""), add_to_timeline: bool = Form(True)):
    s = seq_or_404(sid)
    for f in files:
        p = await _ingest_upload(f, language, model or None)
        with sequences._lock:
            if add_to_timeline:
                sequences.add_clip(s, p["id"])
            else:
                sequences.add_item(s, p["id"])
            sequences.save(s)
    return seq_payload(s)


@app.put("/api/sequences/{sid}/upload")
async def upload_raw(sid: str, request: Request, filename: str, language: str = "auto", model: str = "",
                     add_to_timeline: bool = True):
    """Upload one file as the raw request body: written straight to disk once (no multipart spooling)."""
    s = seq_or_404(sid)
    tmp_dir = DATA_DIR / "uploads" / store.new_id()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / Path(filename).name
    with dest.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    p = _register_upload(dest, tmp_dir, filename, language, model or None)
    with sequences._lock:
        if add_to_timeline:
            sequences.add_clip(s, p["id"])
        else:
            sequences.add_item(s, p["id"])
        sequences.save(s)
    return seq_payload(s)


class ImportPaths(BaseModel):
    paths: list[str]
    language: str = "auto"
    model: str | None = None
    add_to_timeline: bool = True


@app.post("/api/sequences/{sid}/import-path")
def import_paths(sid: str, body: ImportPaths):
    s = seq_or_404(sid)
    for raw in body.paths:
        path = Path(raw.strip().strip('"'))
        if not path.is_file():
            raise HTTPException(400, f"File not found: {path}")
        existing = next((x for x in (store.get(i["id"]) for i in store.list_all())
                         if x["media"]["path"] == str(path.resolve()) and x["words"] and not x["owns_media"]), None)
        if existing:  # already transcribed: copy the transcript (but not the old cuts) instead of redoing it
            p = store.duplicate(existing["id"])
        else:
            try:
                info = media.probe(path)
            except ValueError as e:
                raise HTTPException(400, f"{path.name}: {e}")
            p = store.create(path.stem, info, False)
            transcribe.submit(p["id"], body.language, body.model)
        with sequences._lock:
            if body.add_to_timeline:
                sequences.add_clip(s, p["id"])
            else:
                sequences.add_item(s, p["id"])
            sequences.save(s)
    return seq_payload(s)


class ItemReq(BaseModel):
    project_id: str


@app.post("/api/sequences/{sid}/items")
def add_item(sid: str, body: ItemReq):
    s = seq_or_404(sid)
    proj(body.project_id)
    sequences.add_item(s, body.project_id)
    sequences.save(s)
    return seq_payload(s)


@app.delete("/api/sequences/{sid}/items/{pid}")
def remove_item(sid: str, pid: str):
    s = seq_or_404(sid)
    sequences.snapshot(s)
    s["items"] = [x for x in s["items"] if x != pid]
    s["clips"] = [c for c in s["clips"] if c["pid"] != pid]
    sequences.save(s)
    return seq_payload(s)


class ClipReq(BaseModel):
    project_id: str
    index: int | None = None
    start: float | None = None


@app.post("/api/sequences/{sid}/clips")
def add_clip(sid: str, body: ClipReq):
    """Put media on the timeline at `start` seconds, at list position `index`, or after the last clip."""
    s = seq_or_404(sid)
    proj(body.project_id)
    with sequences._lock:
        sequences.snapshot(s)
        clip = sequences.add_clip(s, body.project_id, body.index, body.start)
        sequences.save(s)
    return {"clip": clip, "sequence": seq_payload(s)}


def _clip_or_404(s: dict, cid: str) -> dict:
    try:
        c = sequences.clip_by_id(s, cid)
    except KeyError:
        raise HTTPException(404, "Clip not found")
    if sequences.is_locked(s, "V1"):
        raise HTTPException(409, "Track V1 is locked")
    return c


class MoveReq(BaseModel):
    start: float | None = None
    index: int | None = None


@app.post("/api/sequences/{sid}/clips/{cid}/move")
def move_clip(sid: str, cid: str, body: MoveReq):
    """Move a clip to `start` seconds (it slides to the nearest free spot) or to list position `index`."""
    s = seq_or_404(sid)
    with sequences._lock:
        _clip_or_404(s, cid)
        sequences.snapshot(s)
        sequences.move_clip(s, cid, body.start, body.index)
        sequences.save(s)
    return seq_payload(s)


class TrimReq(BaseModel):
    edge: str      # "in" (left edge) or "out" (right edge)
    time: float    # sequence time the edge was dragged to


@app.post("/api/sequences/{sid}/clips/{cid}/trim")
def trim_clip(sid: str, cid: str, body: TrimReq):
    s = seq_or_404(sid)
    with sequences._lock:
        _clip_or_404(s, cid)
        sequences.snapshot(s)
        try:
            sequences.trim_clip(s, cid, body.edge, body.time)
        except ValueError as e:
            raise HTTPException(400, str(e))
        sequences.save(s)
    return seq_payload(s)


class SplitReq(BaseModel):
    time: float


@app.post("/api/sequences/{sid}/clips/{cid}/split")
def split_clip(sid: str, cid: str, body: SplitReq):
    """Razor: cut the clip in two at `time` (sequence seconds)."""
    s = seq_or_404(sid)
    with sequences._lock:
        _clip_or_404(s, cid)
        sequences.snapshot(s)
        try:
            second = sequences.split_clip(s, cid, body.time)
        except ValueError as e:
            raise HTTPException(400, str(e))
        sequences.save(s)
    return {"clip": second, "sequence": seq_payload(s)}


@app.delete("/api/sequences/{sid}/clips/{cid}")
def remove_clip(sid: str, cid: str, ripple: bool = False):
    """Remove a clip. With ripple=true the clips after it slide left to close the gap."""
    s = seq_or_404(sid)
    with sequences._lock:
        _clip_or_404(s, cid)
        sequences.snapshot(s)
        sequences.delete_clip(s, cid, ripple)
        sequences.save(s)
    return seq_payload(s)


@app.post("/api/sequences/{sid}/close-gaps")
def close_gaps(sid: str):
    s = seq_or_404(sid)
    with sequences._lock:
        sequences.snapshot(s)
        sequences.close_gaps(s, 0)
        sequences.save(s)
    return seq_payload(s)


@app.post("/api/sequences/{sid}/undo")
def undo_sequence(sid: str):
    s = seq_or_404(sid)
    with sequences._lock:
        ok = sequences.undo(s)
    return {"ok": ok, "sequence": seq_payload(s)}


@app.post("/api/sequences/{sid}/redo")
def redo_sequence(sid: str):
    s = seq_or_404(sid)
    with sequences._lock:
        ok = sequences.redo(s)
    return {"ok": ok, "sequence": seq_payload(s)}


# ---- markers and track switches

class MarkerReq(BaseModel):
    time: float | None = None
    name: str | None = None
    color: str | None = None


@app.post("/api/sequences/{sid}/markers")
def add_marker(sid: str, body: MarkerReq):
    s = seq_or_404(sid)
    with sequences._lock:
        sequences.snapshot(s)
        sequences.add_marker(s, body.time or 0.0, body.name or "", body.color or "red")
        sequences.save(s)
    return seq_payload(s)


@app.patch("/api/sequences/{sid}/markers/{mid}")
def patch_marker(sid: str, mid: str, body: MarkerReq):
    s = seq_or_404(sid)
    m = next((m for m in s["markers"] if m["id"] == mid), None)
    if not m:
        raise HTTPException(404, "Marker not found")
    with sequences._lock:
        sequences.snapshot(s)
        if body.time is not None:
            m["t"] = max(0.0, body.time)
        if body.name is not None:
            m["name"] = body.name
        if body.color is not None:
            m["color"] = body.color
        s["markers"].sort(key=lambda x: x["t"])
        sequences.save(s)
    return seq_payload(s)


@app.delete("/api/sequences/{sid}/markers/{mid}")
def remove_marker(sid: str, mid: str):
    s = seq_or_404(sid)
    with sequences._lock:
        sequences.snapshot(s)
        s["markers"] = [m for m in s["markers"] if m["id"] != mid]
        sequences.save(s)
    return seq_payload(s)


class TrackReq(BaseModel):
    lock: bool | None = None
    mute: bool | None = None
    solo: bool | None = None


@app.patch("/api/sequences/{sid}/tracks/{label}")
def patch_track(sid: str, label: str, body: TrackReq):
    """Lock / mute / solo a track by its label (V1, A1, A2…)."""
    s = seq_or_404(sid)
    with sequences._lock:
        sequences.snapshot(s)
        s["tracks"].setdefault(label, {}).update(body.model_dump(exclude_unset=True))
        sequences.save(s)
    return seq_payload(s)


def _seq_render_job(sid: str, fmt: str | None, out_dir: str | None) -> None:
    s = sequences.get(sid)
    try:
        out = exporters.render_sequence(s, sequences.projects_in_order(s), fmt, out_dir)
        s["export"] = {"state": "done", "progress": 1.0, "file": str(out), "message": ""}
    except Exception as e:
        s["export"] = {"state": "error", "progress": 0.0, "file": None, "message": str(e)[:500]}
    sequences.save(s)


@app.post("/api/sequences/{sid}/export")
def export_sequence(sid: str, body: ExportReq):
    s = seq_or_404(sid)
    ps = sequences.projects_in_order(s)
    if not ps:
        raise HTTPException(409, "The timeline is empty")
    if body.kind in TEXT_EXPORTS:
        try:
            path = exporters.write_text_export(s, body.kind, body.out_dir, projects=ps, sub="sequences")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"state": "done", "file": str(path)}
    if body.kind == "media":
        if s["export"]["state"] == "running":
            raise HTTPException(409, "An export is already running")
        s["export"] = {"state": "running", "progress": 0.0, "file": None, "message": ""}
        sequences.save(s)
        threading.Thread(target=_seq_render_job, args=(sid, body.format, body.out_dir), daemon=True).start()
        return {"state": "running"}
    raise HTTPException(400, "kind must be xml, edl, srt, vtt, txt or media")


@app.get("/api/sequences/{sid}/export-status")
def sequence_export_status(sid: str):
    return seq_or_404(sid)["export"]


@app.get("/api/sequences/{sid}/download/{kind}")
def download_sequence(sid: str, kind: str):
    s = seq_or_404(sid)
    if kind == "media":
        f = s["export"].get("file")
        if not f or not Path(f).exists():
            raise HTTPException(404, "Render the media first")
        return FileResponse(f, filename=Path(f).name)
    try:
        path = exporters.write_text_export(s, kind, None, projects=sequences.projects_in_order(s), sub="sequences")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return FileResponse(path, filename=path.name)


# ------------------------------------------------------------------ layers (extra mics, cameras, music)

_sync_pool = ThreadPoolExecutor(max_workers=2)


def _create_layer_project(path_str: str) -> dict:
    path = Path(path_str.strip().strip('"'))
    if not path.is_file():
        raise HTTPException(400, f"File not found: {path}")
    try:
        info = media.probe(path)
    except ValueError as e:
        raise HTTPException(400, f"{path.name}: {e}")
    p = store.create(path.stem, info, False)
    p["is_layer"] = True
    p["status"] = {"state": "ready", "step": "", "progress": 1.0, "message": ""}
    store.save(p)
    transcribe.ensure_proxy(p["id"])  # browser-playable copy for the preview
    return p


def _run_sync(sid: str, lid: str) -> None:
    s = sequences.get(sid)
    layer, clip = sequences.find_layer(s, lid)
    if not layer or not clip:
        return
    with sequences._lock:
        layer["sync"].update(state="syncing", message="Matching audio…")
        sequences.save(s)
    try:
        main, lp = store.get(clip["pid"]), store.get(layer["pid"])
        if not main["media"]["has_audio"] or not lp["media"]["has_audio"]:
            raise RuntimeError("Both files need an audio track to sync automatically.")
        r = syncing.estimate(main["media"]["path"], lp["media"]["path"], main["media"]["duration"],
                             lp["media"]["duration"])
    except Exception as e:
        r = {"state": "failed", "confidence": 0.0, "windows": [], "drift_ppm": 0.0, "message": str(e)[:300]}
    with sequences._lock:
        layer["sync"] = {k: r.get(k) for k in ("state", "confidence", "windows", "drift_ppm", "message")}
        if r["state"] in ("ok", "low"):
            layer["offset"], layer["rate"] = r["offset"], r["rate"]
        sequences.save(s)


class LayerAdd(BaseModel):
    paths: list[str]


@app.post("/api/sequences/{sid}/clips/{cid}/layers")
def add_synced_layers(sid: str, cid: str, body: LayerAdd):
    """Attach extra recordings to a clip and sync them to it automatically."""
    s = seq_or_404(sid)
    clip = next((c for c in s["clips"] if c["id"] == cid), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    for path in body.paths:
        p = _create_layer_project(path)
        layer = sequences.new_layer(p["id"], p["name"], "sync")
        with sequences._lock:
            sequences.snapshot(s)
            sequences.add_sync_layer(s, clip, layer)
            sequences.save(s)
        _sync_pool.submit(_run_sync, sid, layer["id"])
    return seq_payload(s)


@app.post("/api/sequences/{sid}/layers")
def add_free_layers(sid: str, body: LayerAdd):
    """Add music or other layers placed by time on the whole sequence (no auto-sync)."""
    s = seq_or_404(sid)
    for path in body.paths:
        p = _create_layer_project(path)
        with sequences._lock:
            sequences.snapshot(s)
            sequences.add_free_layer(s, sequences.new_layer(p["id"], p["name"], "free"))
            sequences.save(s)
    return seq_payload(s)


@app.put("/api/sequences/{sid}/layer-upload")
async def upload_layer(sid: str, request: Request, filename: str, clip_id: str = "", mode: str = "sync"):
    """Upload one extra file (dropped on the Layers tab) as a synced layer of `clip_id`, or as a free layer."""
    s = seq_or_404(sid)
    clip = next((c for c in s["clips"] if c["id"] == clip_id), None)
    if mode == "sync" and not clip:
        raise HTTPException(400, "Select a clip to sync the layer to")
    dest = DATA_DIR / "layer_uploads" / store.new_id() / Path(filename).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    p = _create_layer_project(str(dest))
    layer = sequences.new_layer(p["id"], p["name"], "sync" if mode == "sync" else "free")
    with sequences._lock:
        sequences.snapshot(s)
        sequences.add_sync_layer(s, clip, layer) if mode == "sync" else sequences.add_free_layer(s, layer)
        sequences.save(s)
    if mode == "sync":
        _sync_pool.submit(_run_sync, sid, layer["id"])
    return seq_payload(s)


class LayerPatch(BaseModel):
    name: str | None = None
    offset: float | None = None
    offset_delta: float | None = None
    rate: float | None = None
    start: float | None = None
    trim_in: float | None = None
    trim_out: float | None = None
    gain_db: float | None = None
    mute: bool | None = None
    fade_in: float | None = None
    fade_out: float | None = None
    replace_main_audio: bool | None = None
    row: int | None = None
    v_on: bool | None = None
    v_x: float | None = None
    v_y: float | None = None
    v_scale: float | None = None
    v_opacity: float | None = None
    v_row: int | None = None


@app.patch("/api/sequences/{sid}/layers/{lid}")
def patch_layer(sid: str, lid: str, body: LayerPatch):
    s = seq_or_404(sid)
    layer, _clip = sequences.find_layer(s, lid)
    if not layer:
        raise HTTPException(404, "Layer not found")
    changes = body.model_dump(exclude_unset=True)
    delta = changes.pop("offset_delta", None)
    if sequences.is_locked(s, f"A{layer['row'] + 1}"):
        raise HTTPException(409, "That track is locked")
    with sequences._lock:
        sequences.snapshot(s)
        layer.update(changes)
        layer["row"] = max(1, int(layer.get("row") or 1))
        layer["v_row"] = max(1, int(layer.get("v_row") or 1))
        layer["v_scale"] = min(1.0, max(0.02, layer.get("v_scale") or 0.32))
        layer["v_opacity"] = min(1.0, max(0.0, layer.get("v_opacity") if layer.get("v_opacity") is not None else 1.0))
        layer["v_x"] = min(2.0, max(-1.0, layer.get("v_x") or 0.0))
        layer["v_y"] = min(2.0, max(-1.0, layer.get("v_y") or 0.0))
        if delta is not None:
            layer["offset"] = layer["offset"] - delta  # +delta moves the layer later on the timeline
        layer["rate"] = min(1.05, max(0.95, layer["rate"] or 1.0))
        layer["trim_in"] = max(0.0, layer["trim_in"] or 0.0)
        layer["fade_in"], layer["fade_out"] = max(0.0, layer["fade_in"]), max(0.0, layer["fade_out"])
        sequences.save(s)
    return seq_payload(s)


@app.post("/api/sequences/{sid}/layers/{lid}/sync")
def resync_layer(sid: str, lid: str):
    s = seq_or_404(sid)
    layer, clip = sequences.find_layer(s, lid)
    if not layer or not clip:
        raise HTTPException(404, "Only layers attached to a clip can be auto-synced")
    _sync_pool.submit(_run_sync, sid, lid)
    return {"ok": True}


@app.post("/api/sequences/{sid}/layers/{lid}/unlink")
def unlink_layer(sid: str, lid: str):
    """Make a synced layer free: it stays where it is but no longer follows the clip's cuts or its sync."""
    s = seq_or_404(sid)
    with sequences._lock:
        sequences.snapshot(s)
        try:
            sequences.unlink_layer(s, lid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        sequences.save(s)
    return seq_payload(s)


@app.delete("/api/sequences/{sid}/layers/{lid}")
def remove_layer(sid: str, lid: str):
    s = seq_or_404(sid)
    with sequences._lock:
        sequences.snapshot(s)
        for c in s["clips"]:
            c["layers"] = [l for l in c["layers"] if l["id"] != lid]
        s["layers"] = [l for l in s["layers"] if l["id"] != lid]
        sequences.save(s)
    return seq_payload(s)


# ------------------------------------------------------------------ frontend

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
