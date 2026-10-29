"""Project storage: one JSON file per project, cached in memory."""
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from .config import PROJECTS_DIR

_lock = threading.RLock()
_cache: dict[str, dict] = {}
_history: dict[str, dict] = {}  # id -> {"undo": [...], "redo": [...]} (memory only)

HISTORY_LIMIT = 60


def project_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def _file(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def create(name: str, media: dict, owns_media: bool) -> dict:
    pid = new_id()
    project_dir(pid).mkdir(parents=True, exist_ok=True)
    p = {
        "id": pid,
        "name": name,
        "created": time.time(),
        "version": 0,
        "media": media,
        "owns_media": owns_media,
        "status": {"state": "created", "step": "", "progress": 0.0, "message": ""},
        "language": None,
        "model": None,
        "device": None,
        "proxy": False,
        "words": [],
        "cuts": [],
        "export": {"state": "idle", "progress": 0.0, "file": None, "message": ""},
    }
    with _lock:
        _cache[pid] = p
        save(p)
    return p


def get(pid: str) -> dict:
    with _lock:
        if pid in _cache:
            return _cache[pid]
        f = _file(pid)
        if not f.exists():
            raise KeyError(pid)
        p = json.loads(f.read_text(encoding="utf-8"))
        # A restart interrupts any running job.
        if p["status"]["state"] == "processing":
            p["status"] = {"state": "error", "step": "", "progress": 0.0, "message": "Interrupted by restart"}
        if p.get("export", {}).get("state") == "running":
            p["export"] = {"state": "idle", "progress": 0.0, "file": None, "message": ""}
        _cache[pid] = p
        return p


def save(p: dict) -> None:
    with _lock:
        p["version"] = p.get("version", 0) + 1
        f = _file(p["id"])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)


def list_all() -> list[dict]:
    out = []
    for d in sorted(PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else [], key=lambda x: x.stat().st_mtime, reverse=True):
        if (d / "project.json").exists():
            try:
                p = get(d.name)
            except Exception:
                continue
            out.append({
                "id": p["id"], "name": p["name"], "created": p["created"],
                "kind": p["media"]["kind"], "duration": p["media"]["duration"],
                "state": p["status"]["state"], "language": p["language"],
                "cuts": len(p["cuts"]), "words": len(p["words"]),
            })
    return out


def delete(pid: str) -> None:
    with _lock:
        _cache.pop(pid, None)
        _history.pop(pid, None)
        shutil.rmtree(project_dir(pid), ignore_errors=True)


# ---- undo / redo of the cut list ------------------------------------------------

def snapshot(p: dict) -> None:
    """Call before mutating p['cuts'] so the change can be undone."""
    h = _history.setdefault(p["id"], {"undo": [], "redo": []})
    h["undo"].append(json.loads(json.dumps(p["cuts"])))
    del h["undo"][:-HISTORY_LIMIT]
    h["redo"].clear()


def undo(p: dict) -> bool:
    h = _history.get(p["id"])
    if not h or not h["undo"]:
        return False
    h["redo"].append(json.loads(json.dumps(p["cuts"])))
    p["cuts"] = h["undo"].pop()
    save(p)
    return True


def redo(p: dict) -> bool:
    h = _history.get(p["id"])
    if not h or not h["redo"]:
        return False
    h["undo"].append(json.loads(json.dumps(p["cuts"])))
    p["cuts"] = h["redo"].pop()
    save(p)
    return True
