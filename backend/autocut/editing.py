"""Cut list logic: turning word ranges into time ranges and kept segments."""
import uuid

# Pauses next to a cut word are trimmed by at most this much on each side.
WORD_PAD = 0.12
MIN_SEGMENT = 0.04


def _cid() -> str:
    return uuid.uuid4().hex[:8]


def word_range_times(p: dict, w0: int, w1: int) -> tuple[float, float]:
    words = p["words"]
    n = len(words)
    dur = p["media"]["duration"]
    prev_e = words[w0 - 1]["e"] if w0 > 0 else 0.0
    next_s = words[w1 + 1]["s"] if w1 + 1 < n else dur
    start = words[w0]["s"] - min(max(words[w0]["s"] - prev_e, 0) / 2, WORD_PAD)
    end = words[w1]["e"] + min(max(next_s - words[w1]["e"], 0) / 2, WORD_PAD)
    return max(0.0, start), min(dur, end)


def add_word_cut(p: dict, w0: int, w1: int, kind="manual", reason="", status="applied") -> dict:
    n = len(p["words"])
    if n == 0:
        raise ValueError("Project has no transcript yet")
    w0, w1 = max(0, min(w0, w1)), min(n - 1, max(w0, w1))
    start, end = word_range_times(p, w0, w1)
    cut = {"id": _cid(), "w0": w0, "w1": w1, "gap": None, "start": start, "end": end,
           "kind": kind, "reason": reason, "status": status}
    p["cuts"].append(cut)
    return cut


def add_time_cut(p: dict, start: float, end: float, kind="manual", reason="", status="applied",
                 gap_after: int | None = None) -> dict:
    dur = p["media"]["duration"]
    start, end = max(0.0, start), min(dur, end)
    if end - start < 0.01:
        raise ValueError("Cut is too short")
    cut = {"id": _cid(), "w0": None, "w1": None, "gap": gap_after, "start": start, "end": end,
           "kind": kind, "reason": reason, "status": status}
    p["cuts"].append(cut)
    return cut


def covers(existing: dict, w0: int, w1: int) -> bool:
    return existing["w0"] is not None and existing["w0"] <= w0 and existing["w1"] >= w1


def merged_intervals(cuts: list[dict], only_applied=True) -> list[tuple[float, float]]:
    iv = sorted((c["start"], c["end"]) for c in cuts if not only_applied or c["status"] == "applied")
    out: list[list[float]] = []
    for s, e in iv:
        if out and s <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def keep_segments(p: dict) -> list[tuple[float, float]]:
    """Time ranges of the source that survive the applied cuts."""
    dur = p["media"]["duration"]
    segs, pos = [], 0.0
    for s, e in merged_intervals(p["cuts"]):
        if s - pos >= MIN_SEGMENT:
            segs.append((pos, s))
        pos = max(pos, e)
    if dur - pos >= MIN_SEGMENT:
        segs.append((pos, dur))
    return segs


def stats(p: dict) -> dict:
    dur = p["media"]["duration"]
    kept = sum(e - s for s, e in keep_segments(p))
    return {
        "original": dur,
        "edited": kept,
        "removed": max(0.0, dur - kept),
        "applied": sum(1 for c in p["cuts"] if c["status"] == "applied"),
        "proposed": sum(1 for c in p["cuts"] if c["status"] == "proposed"),
    }


def edited_time(segs: list[tuple[float, float]], t: float) -> float:
    """Map a source time to the edited timeline."""
    acc = 0.0
    for s, e in segs:
        if t < s:
            return acc
        if t <= e:
            return acc + (t - s)
        acc += e - s
    return acc


def kept_words(p: dict) -> list[dict]:
    """Words whose midpoint is inside a kept segment, with edited-timeline times."""
    segs = keep_segments(p)
    out = []
    for i, w in enumerate(p["words"]):
        mid = (w["s"] + w["e"]) / 2
        if any(s <= mid <= e for s, e in segs):
            out.append({"i": i, "w": w["w"], "s": edited_time(segs, w["s"]), "e": edited_time(segs, w["e"])})
    return out
