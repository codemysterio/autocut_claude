"""AutoCut web server: REST API + static frontend."""
import json
import shutil
import threading
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cleanup, editing, exporters, media, store, transcribe
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


@app.post("/api/projects")
async def upload_project(file: UploadFile = File(...), language: str = Form("auto"), model: str = Form("")):
    pid_hint = store.new_id()
    tmp_dir = DATA_DIR / "uploads" / pid_hint
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / Path(file.filename or "upload").name
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    try:
        info = media.probe(dest)
    except ValueError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(400, str(e))
    p = store.create(dest.stem, info, True)
    media_dir = store.project_dir(p["id"]) / "media"
    media_dir.mkdir(exist_ok=True)
    final = media_dir / dest.name
    shutil.move(str(dest), final)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    p["media"]["path"] = str(final.resolve())
    store.save(p)
    transcribe.submit(p["id"], language, model or None)
    return payload(p)


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
    return {"ok": True}


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


# ------------------------------------------------------------------ frontend

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
