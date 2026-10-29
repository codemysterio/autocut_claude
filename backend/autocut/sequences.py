"""Sequences: an ordered list of clips (each a transcribed media project) played back to back."""
import json
import os
import threading
import time
import uuid

from . import editing, store
from .config import DATA_DIR

SEQ_DIR = DATA_DIR / "sequences"
SEQ_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()
_cache: dict[str, dict] = {}


def _file(sid: str):
    return SEQ_DIR / f"{sid}.json"


def _id() -> str:
    return uuid.uuid4().hex[:10]


def create(name: str) -> dict:
    s = {"id": _id(), "name": name, "created": time.time(), "version": 0,
         "items": [],   # project ids shown in the media bin
         "clips": [],   # [{"id", "pid"}] in timeline order
         "export": {"state": "idle", "progress": 0.0, "file": None, "message": ""}}
    with _lock:
        _cache[s["id"]] = s
        save(s)
    return s


def get(sid: str) -> dict:
    with _lock:
        if sid in _cache:
            return _cache[sid]
        f = _file(sid)
        if not f.exists():
            raise KeyError(sid)
        s = json.loads(f.read_text(encoding="utf-8"))
        if s.get("export", {}).get("state") == "running":
            s["export"] = {"state": "idle", "progress": 0.0, "file": None, "message": ""}
        _cache[sid] = s
        return s


def save(s: dict) -> None:
    with _lock:
        s["version"] = s.get("version", 0) + 1
        tmp = _file(s["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(s), encoding="utf-8")
        os.replace(tmp, _file(s["id"]))


def list_all() -> list[dict]:
    out = []
    for f in sorted(SEQ_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            s = get(f.stem)
        except Exception:
            continue
        L = layout(s)
        out.append({"id": s["id"], "name": s["name"], "clips": len(s["clips"]), "duration": L["duration"]})
    return out


def delete(sid: str) -> None:
    with _lock:
        _cache.pop(sid, None)
        _file(sid).unlink(missing_ok=True)


def remove_project(pid: str) -> None:
    """Drop a deleted media project from every sequence."""
    for f in list(SEQ_DIR.glob("*.json")):
        try:
            s = get(f.stem)
        except Exception:
            continue
        if pid in s["items"] or any(c["pid"] == pid for c in s["clips"]):
            s["items"] = [x for x in s["items"] if x != pid]
            s["clips"] = [c for c in s["clips"] if c["pid"] != pid]
            save(s)


def find_for_project(pid: str) -> dict | None:
    for f in SEQ_DIR.glob("*.json"):
        try:
            s = get(f.stem)
        except Exception:
            continue
        if pid in s["items"]:
            return s
    return None


def add_item(s: dict, pid: str) -> None:
    if pid not in s["items"]:
        s["items"].append(pid)


def add_clip(s: dict, pid: str, index: int | None = None) -> dict:
    add_item(s, pid)
    clip = {"id": _id(), "pid": pid}
    if index is None or index >= len(s["clips"]):
        s["clips"].append(clip)
    else:
        s["clips"].insert(max(0, index), clip)
    return clip


def combined_version(s: dict) -> int:
    """Changes whenever the sequence or any media project in it changes."""
    v = s["version"]
    for pid in s["items"]:
        try:
            v += store.get(pid)["version"]
        except KeyError:
            pass
    return v


def projects_in_order(s: dict) -> list[dict]:
    out = []
    for c in s["clips"]:
        try:
            out.append(store.get(c["pid"]))
        except KeyError:
            pass
    return out


def layout(s: dict) -> dict:
    """Timeline positions of every clip after the transcript cuts have been applied."""
    clips, cursor = [], 0.0
    for c in s["clips"]:
        try:
            p = store.get(c["pid"])
        except KeyError:
            continue
        segs = editing.keep_segments(p)
        length = sum(e - b for b, e in segs)
        m = p["media"]
        clips.append({"id": c["id"], "pid": p["id"], "name": p["name"], "kind": m["kind"],
                      "has_video": m["has_video"], "has_audio": m["has_audio"],
                      "start": cursor, "length": length, "segments": segs, "source_duration": m["duration"]})
        cursor += length
    return {"clips": clips, "duration": cursor}
