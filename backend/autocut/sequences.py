"""Sequences: a free-form timeline. Clips (transcribed media) sit anywhere on a track with gaps allowed,
carry trim points, and own audio layers that follow their cuts. Markers, track switches and undo live here too."""
import copy
import json
import os
import threading
import time
import uuid

from . import editing, media as media_tools, store
from .config import DATA_DIR

SEQ_DIR = DATA_DIR / "sequences"
SEQ_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()
_cache: dict[str, dict] = {}
_hist: dict[str, dict] = {}   # sequence id -> {"undo": [...], "redo": [...]} (memory only)
HISTORY_LIMIT = 80
STATE_KEYS = ("items", "clips", "layers", "markers", "tracks")
EPS = 1e-6


def _file(sid: str):
    return SEQ_DIR / f"{sid}.json"


def _id() -> str:
    return uuid.uuid4().hex[:10]


def create(name: str) -> dict:
    s = {"id": _id(), "name": name, "created": time.time(), "version": 0,
         "items": [],     # project ids shown in the media bin
         "clips": [],     # [{id, pid, start, in_t, out_t, track, last_len, layers: [...]}]
         "layers": [],    # free layers (music etc.) placed by sequence time
         "markers": [],   # [{id, t, name, color}]
         "tracks": {},    # {"V1": {lock, mute, solo}, "A1": ..., "A2": ...}
         "export": {"state": "idle", "progress": 0.0, "file": None, "message": ""}}
    with _lock:
        _cache[s["id"]] = s
        save(s)
    return s


def _project(c: dict):
    try:
        return store.get(c["pid"])
    except KeyError:
        return None


def clip_segments(c: dict, p: dict) -> list[tuple[float, float]]:
    """The parts of the media this clip plays: the transcript's kept ranges inside the clip's in/out trim."""
    lo = c.get("in_t") or 0.0
    hi = c["out_t"] if c.get("out_t") is not None else p["media"]["duration"]
    out = []
    for a, b in editing.keep_segments(p):
        a2, b2 = max(a, lo), min(b, hi)
        if b2 - a2 >= editing.MIN_SEGMENT:
            out.append((a2, b2))
    return out


def clip_length(c: dict, p: dict | None = None) -> float:
    p = p or _project(c)
    return sum(b - a for a, b in clip_segments(c, p)) if p else 0.0


def pack(s: dict, track: int = 0) -> None:
    """Lay the clips of a track out back to back from 0, in their current order."""
    cursor = 0.0
    for c in sorted((c for c in s["clips"] if c["track"] == track), key=lambda c: c["start"]):
        c["start"] = cursor
        c["last_len"] = clip_length(c)
        cursor += c["last_len"]


PICTURE_DEFAULTS = {"v_on": True, "v_x": 0.65, "v_y": 0.65, "v_scale": 0.32, "v_opacity": 1.0, "v_row": 1}


def _assign_rows(s: dict) -> None:
    """Every layer sits on an audio row (row 1 is track A2). Old sequences get the rows they used to show."""
    for l in [l for c in s["clips"] for l in c["layers"]] + s["layers"]:
        for k, v in PICTURE_DEFAULTS.items():
            l.setdefault(k, v)
    top = 0
    for c in s["clips"]:
        for k, l in enumerate(c["layers"]):
            l.setdefault("row", k + 1)
            top = max(top, l["row"])
    for l in s["layers"]:
        if "row" not in l:
            top += 1
            l["row"] = top


def get(sid: str) -> dict:
    with _lock:
        if sid in _cache:
            return _cache[sid]
        f = _file(sid)
        if not f.exists():
            raise KeyError(sid)
        s = json.loads(f.read_text(encoding="utf-8"))
        s.setdefault("layers", [])
        s.setdefault("markers", [])
        s.setdefault("tracks", {})
        old_order = any("start" not in c for c in s["clips"])
        for c in s["clips"]:
            c.setdefault("layers", [])
            c.setdefault("in_t", 0.0)
            c.setdefault("out_t", None)
            c.setdefault("track", 0)
            c.setdefault("start", 0.0)
        if old_order:  # sequences made before free placement were played back to back, in list order
            cursor = 0.0
            for c in s["clips"]:
                p = _project(c)
                c["start"], c["last_len"] = cursor, (clip_length(c, p) if p else 0.0)
                cursor += c["last_len"]
        _assign_rows(s)
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


# ---- undo / redo of timeline edits ---------------------------------------------------------

def snapshot(s: dict) -> None:
    """Call before changing the timeline so the change can be undone."""
    h = _hist.setdefault(s["id"], {"undo": [], "redo": []})
    h["undo"].append(json.loads(json.dumps({k: s[k] for k in STATE_KEYS})))
    del h["undo"][:-HISTORY_LIMIT]
    h["redo"].clear()


def _swap(s: dict, src: list, dst: list) -> bool:
    if not src:
        return False
    dst.append(json.loads(json.dumps({k: s[k] for k in STATE_KEYS})))
    s.update(src.pop())
    save(s)
    return True


def undo(s: dict) -> bool:
    h = _hist.get(s["id"])
    return bool(h) and _swap(s, h["undo"], h["redo"])


def redo(s: dict) -> bool:
    h = _hist.get(s["id"])
    return bool(h) and _swap(s, h["redo"], h["undo"])


# ---- ripple: clips after a clip move when transcript cuts change its length ---------------------

def settle(s: dict) -> bool:
    """Apply automatic ripple. Cutting words shortens a clip; the clips after it on the track slide left."""
    changed = False
    tracks: dict[int, list] = {}
    for c in s["clips"]:
        tracks.setdefault(c["track"], []).append(c)
    for clips in tracks.values():
        clips.sort(key=lambda c: c["start"])
        for c in clips:
            p = _project(c)
            if not p:
                continue
            length = clip_length(c, p)
            last = c.get("last_len")
            if last is None:
                c["last_len"], changed = length, True
                continue
            d = length - last
            if abs(d) > EPS:
                end_old = c["start"] + last
                for o in clips:
                    if o is not c and o["start"] >= end_old - 1e-4:
                        o["start"] += d
                c["last_len"], changed = length, True
    return changed


# ---- placing clips ---------------------------------------------------------------------------

def _others(s: dict, c: dict) -> list[dict]:
    return [o for o in s["clips"] if o is not c and o["track"] == c["track"]]


def _overlaps(s: dict, c: dict) -> bool:
    a, b = c["start"], c["start"] + (c.get("last_len") or 0.0)
    return any(o["start"] < b - 1e-4 and o["start"] + (o.get("last_len") or 0.0) > a + 1e-4 for o in _others(s, c))


def fit_start(s: dict, c: dict, want: float, length: float) -> float:
    """The free position on the clip's track nearest to `want` where a clip of this length fits without overlap."""
    others = _others(s, c)
    cands = [max(0.0, want), 0.0]
    for o in others:
        cands += [o["start"] + (o.get("last_len") or 0.0), o["start"] - length]
    best, best_d = None, None
    for t in cands:
        if t < -EPS:
            continue
        if any(o["start"] < t + length - 1e-4 and o["start"] + (o.get("last_len") or 0.0) > t + 1e-4 for o in others):
            continue
        d = abs(t - want)
        if best is None or d < best_d:
            best, best_d = t, d
    return max(0.0, best if best is not None else want)


def track_end(s: dict, track: int = 0) -> float:
    return max([c["start"] + (c.get("last_len") or 0.0) for c in s["clips"] if c["track"] == track] or [0.0])


def add_item(s: dict, pid: str) -> None:
    if pid not in s["items"]:
        s["items"].append(pid)


def add_clip(s: dict, pid: str, index: int | None = None, start: float | None = None, track: int = 0) -> dict:
    """Put media on the timeline: at `start`, at list position `index` (re-packs the track), or after the last clip."""
    add_item(s, pid)
    clip = {"id": _id(), "pid": pid, "layers": [], "in_t": 0.0, "out_t": None, "track": track, "start": 0.0}
    clip["last_len"] = clip_length(clip)
    if index is not None:
        order = sorted((c for c in s["clips"] if c["track"] == track), key=lambda c: c["start"])
        order.insert(max(0, min(index, len(order))), clip)
        s["clips"].append(clip)
        cursor = 0.0
        for c in order:
            c["start"] = cursor
            cursor += c.get("last_len") or 0.0
    else:
        want = track_end(s, track) if start is None else start   # measured before the new clip is in the list
        s["clips"].append(clip)
        clip["start"] = fit_start(s, clip, want, clip["last_len"])
    return clip


def clip_by_id(s: dict, cid: str) -> dict:
    c = next((c for c in s["clips"] if c["id"] == cid), None)
    if not c:
        raise KeyError(cid)
    return c


def move_clip(s: dict, cid: str, start: float | None = None, index: int | None = None, track: int | None = None) -> None:
    c = clip_by_id(s, cid)
    if track is not None:
        c["track"] = track
    if index is not None:  # reorder, closing gaps (used by the old "position in the order" API)
        order = sorted((o for o in s["clips"] if o["track"] == c["track"] and o is not c), key=lambda o: o["start"])
        order.insert(max(0, min(index, len(order))), c)
        cursor = 0.0
        for o in order:
            o["start"] = cursor
            cursor += o.get("last_len") or 0.0
    elif start is not None:
        c["start"] = fit_start(s, c, start, c.get("last_len") or 0.0)


def seq_to_src(c: dict, p: dict, t: float) -> float:
    """Media time shown at sequence time t; extended linearly before the clip's start and after its end."""
    segs = clip_segments(c, p)
    if not segs:
        return c.get("in_t") or 0.0
    if t <= c["start"]:
        return segs[0][0] - (c["start"] - t)
    acc = c["start"]
    for a, b in segs:
        if t < acc + (b - a):
            return a + (t - acc)
        acc += b - a
    return segs[-1][1] + (t - acc)


def _bisect_trim(s: dict, c: dict, key: str, ok_val: float, bad_val: float, after) -> None:
    """The trim would overlap a neighbour: move the trim point as far as it can go without overlapping."""
    for _ in range(30):
        mid = (ok_val + bad_val) / 2
        c[key] = mid
        after()
        if _overlaps(s, c):
            bad_val = mid
        else:
            ok_val = mid
    c[key] = ok_val
    after()


def trim_clip(s: dict, cid: str, edge: str, t: float) -> None:
    """Drag a clip edge to sequence time t. The other edge stays where it is (no ripple)."""
    c = clip_by_id(s, cid)
    p = _project(c)
    if not p:
        raise ValueError("Its media is gone")
    dur = p["media"]["duration"]
    old_len, old_end = c.get("last_len") or clip_length(c, p), c["start"] + (c.get("last_len") or clip_length(c, p))
    old_in, old_out = c.get("in_t") or 0.0, c["out_t"] if c.get("out_t") is not None else dur
    src = seq_to_src(c, p, t)

    def refresh(end_fixed: bool):
        c["last_len"] = clip_length(c, p)
        if end_fixed:
            c["start"] = old_end - c["last_len"]

    if edge == "in":
        c["in_t"] = max(0.0, min(src, old_out - 0.1))
        refresh(True)
        if clip_length(c, p) < editing.MIN_SEGMENT:
            c["in_t"] = old_in
            refresh(True)
            raise ValueError("That would trim away the whole clip")
        if _overlaps(s, c):
            _bisect_trim(s, c, "in_t", old_in, c["in_t"], lambda: refresh(True))
    elif edge == "out":
        c["out_t"] = min(dur, max(src, old_in + 0.1))
        refresh(False)
        if clip_length(c, p) < editing.MIN_SEGMENT:
            c["out_t"] = old_out
            refresh(False)
            raise ValueError("That would trim away the whole clip")
        if _overlaps(s, c):
            _bisect_trim(s, c, "out_t", old_out, c["out_t"], lambda: refresh(False))
    else:
        raise ValueError("edge must be 'in' or 'out'")


def split_clip(s: dict, cid: str, t: float) -> dict:
    """Razor: cut a clip in two at sequence time t. Layers are copied to the second half."""
    c = clip_by_id(s, cid)
    p = _project(c)
    length = c.get("last_len") or clip_length(c, p)
    if not p or t <= c["start"] + 0.05 or t >= c["start"] + length - 0.05:
        raise ValueError("Put the playhead inside the clip to split it")
    src = seq_to_src(c, p, t)
    b = copy.deepcopy(c)
    b["id"], b["in_t"] = _id(), src
    for l in b["layers"]:
        l["id"] = _id()
    c["out_t"] = src
    c["last_len"], b["last_len"] = clip_length(c, p), clip_length(b, p)
    b["start"] = c["start"] + c["last_len"]
    s["clips"].insert(s["clips"].index(c) + 1, b)
    return b


def delete_clip(s: dict, cid: str, ripple: bool = False) -> None:
    c = clip_by_id(s, cid)
    length = c.get("last_len") or 0.0
    end = c["start"] + length
    s["clips"].remove(c)
    if ripple:
        for o in s["clips"]:
            if o["track"] == c["track"] and o["start"] >= end - 1e-4:
                o["start"] -= length


def close_gaps(s: dict, track: int = 0) -> None:
    pack(s, track)


# ---- layers --------------------------------------------------------------------------------

LAYER_FIELDS = {"name": str, "offset": float, "rate": float, "start": float, "trim_in": float, "trim_out": float,
                "gain_db": float, "mute": bool, "fade_in": float, "fade_out": float, "replace_main_audio": bool,
                "row": int, "v_on": bool, "v_x": float, "v_y": float, "v_scale": float, "v_opacity": float, "v_row": int}


def new_layer(pid: str, name: str, mode: str) -> dict:
    """mode 'sync': follows its clip (tl = rate * main_time + offset); 'free': placed at `start` on the sequence."""
    return {"id": _id(), "pid": pid, "name": name, "mode": mode, "row": 1,
            "offset": 0.0, "rate": 1.0, "start": 0.0, "trim_in": 0.0, "trim_out": None,
            "gain_db": 0.0, "mute": False, "fade_in": 0.0, "fade_out": 0.0, "replace_main_audio": False,
            **PICTURE_DEFAULTS,   # picture of a video layer: shown?, top-left corner and width as fractions of the frame
            "sync": {"state": "pending" if mode == "sync" else "manual", "confidence": 0.0, "windows": [],
                     "drift_ppm": 0.0, "message": ""}}


def _extent(s: dict, l: dict, c: dict | None) -> tuple[float, float]:
    if c is not None:
        return c["start"], c["start"] + (c.get("last_len") or 0.0)
    to = l["trim_out"] if l["trim_out"] is not None else _media_duration(l["pid"])
    return l["start"], l["start"] + max(0.0, to - l["trim_in"])


def pick_row(s: dict, t0: float, t1: float, skip: str | None = None) -> int:
    """The lowest audio row with nothing else audible in [t0, t1]."""
    busy = set()
    for c in s["clips"]:
        for l in c["layers"]:
            a, b = _extent(s, l, c)
            if l["id"] != skip and a < t1 and b > t0:
                busy.add(l["row"])
    for l in s["layers"]:
        a, b = _extent(s, l, None)
        if l["id"] != skip and a < t1 and b > t0:
            busy.add(l["row"])
    row = 1
    while row in busy:
        row += 1
    return row


def has_picture(l: dict) -> bool:
    return bool(_layer_info(l).get("has_video"))


def pick_vrow(s: dict, t0: float, t1: float, skip: str | None = None) -> int:
    """The lowest picture row (1 = track V2) with no other picture layer in [t0, t1]."""
    busy = set()
    for c in s["clips"]:
        for l in c["layers"]:
            a, b = _extent(s, l, c)
            if l["id"] != skip and has_picture(l) and a < t1 and b > t0:
                busy.add(l["v_row"])
    for l in s["layers"]:
        a, b = _extent(s, l, None)
        if l["id"] != skip and has_picture(l) and a < t1 and b > t0:
            busy.add(l["v_row"])
    row = 1
    while row in busy:
        row += 1
    return row


def add_sync_layer(s: dict, clip: dict, layer: dict) -> None:
    t0, t1 = clip["start"], clip["start"] + (clip.get("last_len") or 0.0)
    layer["row"] = pick_row(s, t0, t1)
    if has_picture(layer):
        layer["v_row"] = pick_vrow(s, t0, t1)
    clip["layers"].append(layer)


def add_free_layer(s: dict, layer: dict) -> None:
    a, b = _extent(s, layer, None)
    layer["row"] = pick_row(s, a, max(b, a + 1))
    if has_picture(layer):
        layer["v_row"] = pick_vrow(s, a, max(b, a + 1))
    s["layers"].append(layer)


def find_layer(s: dict, lid: str):
    """(layer, clip or None) for a layer id anywhere in the sequence."""
    for c in s["clips"]:
        for l in c["layers"]:
            if l["id"] == lid:
                return l, c
    for l in s["layers"]:
        if l["id"] == lid:
            return l, None
    return None, None


def unlink_layer(s: dict, lid: str) -> None:
    """Turn a synced layer into a free one that stays exactly where it is now but no longer follows the clip's cuts."""
    l, c = find_layer(s, lid)
    if not l or not c:
        raise ValueError("Only a layer attached to a clip can be unlinked")
    p = _project(c)
    dur = _media_duration(l["pid"])
    pieces = sync_pieces(l, c["start"], clip_segments(c, p), dur) if p else []
    if not pieces:
        raise ValueError("This layer is not audible inside its clip")
    p0 = pieces[0]
    start = p0["seq0"] - (p0["tl0"] - l["trim_in"])
    if start < 0:  # begins before the timeline does: trim the head instead
        l["trim_in"] += -start
        start = 0.0
    c["layers"].remove(l)
    l.update(mode="free", start=start, rate=1.0, offset=0.0, replace_main_audio=False)
    l["sync"] = {"state": "manual", "confidence": 0.0, "windows": [], "drift_ppm": 0.0, "message": ""}
    s["layers"].append(l)


# ---- markers, tracks ---------------------------------------------------------------------------

def add_marker(s: dict, t: float, name: str = "", color: str = "red") -> dict:
    m = {"id": _id(), "t": max(0.0, t), "name": name, "color": color}
    s["markers"].append(m)
    s["markers"].sort(key=lambda x: x["t"])
    return m


def track_labels(s: dict) -> list[str]:
    layers = [l for c in s["clips"] for l in c["layers"]] + s["layers"]
    rows = {l["row"] for l in layers}
    vrows = {l["v_row"] for l in layers if has_picture(l)}
    return ["V1", "A1"] + [f"V{r + 1}" for r in sorted(vrows)] + [f"A{r + 1}" for r in sorted(rows)]


def track_flags(s: dict) -> dict:
    """lock / mute / solo for every track that exists (defaults filled in)."""
    return {lab: {"lock": False, "mute": False, "solo": False, **s["tracks"].get(lab, {})} for lab in track_labels(s)}


def audible(s: dict, label: str) -> bool:
    """Whether an audio track is heard, taking solo into account."""
    flags = track_flags(s)
    f = flags.get(label, {"mute": False, "solo": False})
    if any(v["solo"] for k, v in flags.items() if k.startswith("A")):
        return f["solo"] and not f["mute"]
    return not f["mute"]


def video_visible(s: dict) -> bool:
    return not s["tracks"].get("V1", {}).get("mute", False)


def picture_visible(s: dict, layer: dict) -> bool:
    """Whether a video layer's picture is shown: its own switch and its V row's eye."""
    return bool(layer.get("v_on")) and not s["tracks"].get(f"V{layer['v_row'] + 1}", {}).get("mute", False)


def is_locked(s: dict, label: str) -> bool:
    return bool(s["tracks"].get(label, {}).get("lock"))


def combined_version(s: dict) -> int:
    """Changes whenever the sequence or any media project in it changes."""
    v = s["version"]
    layer_pids = [l["pid"] for c in s["clips"] for l in c["layers"]] + [l["pid"] for l in s["layers"]]
    for pid in s["items"] + layer_pids:
        try:
            v += store.get(pid)["version"]
        except KeyError:
            pass
    return v


def list_all() -> list[dict]:
    out = []
    for f in sorted(SEQ_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            s = get(f.stem)
        except Exception:
            continue
        out.append({"id": s["id"], "name": s["name"], "clips": len(s["clips"]), "duration": layout(s)["duration"]})
    return out


def delete(sid: str) -> None:
    with _lock:
        _cache.pop(sid, None)
        _hist.pop(sid, None)
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


def projects_in_order(s: dict) -> list[dict]:
    out = []
    for c in sorted(s["clips"], key=lambda c: (c["track"], c["start"])):
        p = _project(c)
        if p:
            out.append(p)
    return out


def clip_projects(s: dict) -> list[tuple[dict, dict]]:
    """(timeline clip, media project) pairs in timeline order, skipping clips whose media was deleted."""
    out = []
    for c in sorted(s["clips"], key=lambda c: (c["track"], c["start"])):
        p = _project(c)
        if p:
            out.append((c, p))
    return out


# ---- layout (what the editor draws) --------------------------------------------------------------

def _media_duration(pid: str) -> float:
    try:
        return store.get(pid)["media"]["duration"]
    except KeyError:
        return 0.0


def sync_pieces(layer: dict, clip_start: float, segments: list, layer_dur: float) -> list[dict]:
    """Where a synced layer is audible, in sequence time, after the clip's cuts.

    Each piece maps a stretch of sequence time linearly onto layer media time (tl0..tl1).
    """
    ti = layer["trim_in"]
    to = min(layer["trim_out"] if layer["trim_out"] is not None else layer_dur, layer_dur)
    rate, off = layer["rate"], layer["offset"]
    lo, hi = (ti - off) / rate, (to - off) / rate        # main-clip time range in which the layer has sound
    pieces, t_edit = [], clip_start
    for s, e in segments:
        ms, me = max(s, lo), min(e, hi)
        if me - ms > 1e-4:
            pieces.append({"seq0": t_edit + (ms - s), "seq1": t_edit + (me - s), "tl0": rate * ms + off,
                           "tl1": rate * me + off, "tm0": ms, "tm1": me})
        t_edit += e - s
    return pieces


def free_pieces(layer: dict, layer_dur: float) -> list[dict]:
    ti = layer["trim_in"]
    to = min(layer["trim_out"] if layer["trim_out"] is not None else layer_dur, layer_dur)
    if to - ti <= 1e-4:
        return []
    seq0 = layer["start"]
    return [{"seq0": seq0, "seq1": seq0 + (to - ti), "tl0": ti, "tl1": to}]


def _layer_info(l: dict) -> dict:
    try:
        m = store.get(l["pid"])
    except KeyError:
        return {"missing": True, "duration": 0.0, "has_audio": False, "has_video": False, "proxy": False}
    mm = m["media"]
    return {"duration": mm["duration"], "has_audio": mm["has_audio"], "has_video": mm["has_video"],
            "channels": mm["channels"], "proxy": m["proxy"], "proxy_progress": m.get("proxy_progress", 0.0),
            "path": mm["path"], "width": mm["width"], "height": mm["height"],
            "sprite_count": media_tools.sprite_count(mm["duration"])}


def layout(s: dict) -> dict:
    """Everything the editor draws: clips with their cuts applied and layers, free layers, markers, track switches."""
    with _lock:
        if settle(s):
            save(s)
    clips, end = [], 0.0
    for c in sorted(s["clips"], key=lambda c: (c["track"], c["start"])):
        p = _project(c)
        if not p:
            continue
        segs = clip_segments(c, p)
        length = sum(e - b for b, e in segs)
        m = p["media"]
        layers = []
        for l in c["layers"]:
            info = _layer_info(l)
            layers.append({**l, "media": info, "pieces": sync_pieces(l, c["start"], segs, info["duration"])})
        clips.append({"id": c["id"], "pid": p["id"], "name": p["name"], "kind": m["kind"],
                      "has_video": m["has_video"], "has_audio": m["has_audio"], "track": c["track"],
                      "start": c["start"], "length": length, "segments": segs, "source_duration": m["duration"],
                      "in_t": c["in_t"] or 0.0, "out_t": c["out_t"], "layers": layers})
        end = max(end, c["start"] + length)
    free = []
    for l in s["layers"]:
        info = _layer_info(l)
        free.append({**l, "media": info, "pieces": free_pieces(l, info["duration"])})
    return {"clips": clips, "duration": end, "free_layers": free, "markers": s["markers"], "tracks": track_flags(s)}
