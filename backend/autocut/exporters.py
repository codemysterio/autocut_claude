"""Exports: Premiere Pro XML (FCP7 xmeml), EDL, subtitles, transcript text, rendered media.

Every export works on a list of media projects played back to back (a sequence); a single
project is just a sequence of one.
"""
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote
from xml.dom import minidom
from xml.etree import ElementTree as ET

from . import editing, sequences, store
from .config import DATA_DIR
from .media import _NO_WINDOW, probe

VIDEO_CONTAINERS = ("mp4", "mov", "mkv")


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "autocut"


def _plist(x) -> list[dict]:
    return x if isinstance(x, list) else [x]


def export_dir(owner: dict, out_dir: str | None = None, sub: str = "projects") -> Path:
    d = Path(out_dir) if out_dir else (store.project_dir(owner["id"]) / "exports" if sub == "projects"
                                       else DATA_DIR / "sequence_exports" / owner["id"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def seq_settings(projects: list[dict]) -> dict:
    """Frame size / rate of a sequence: taken from the first video clip."""
    v = next((p["media"] for p in projects if p["media"]["has_video"]), None)
    a = next((p["media"] for p in projects if p["media"]["has_audio"]), None)
    sr = a["sample_rate"] if a and a["sample_rate"] in (44100, 48000) else 48000
    return {
        "width": v["width"] if v else 1920, "height": v["height"] if v else 1080,
        "fps_num": v["fps_num"] if v else 30, "fps_den": v["fps_den"] if v else 1,
        "has_video": v is not None, "has_audio": a is not None, "sample_rate": sr,
    }


def _rate(m: dict) -> tuple[int, bool, float]:
    """(timebase, ntsc, exact fps) of a media file. Audio-only media uses a 30 fps timeline."""
    if not m["has_video"]:
        return 30, False, 30.0
    fps = m["fps_num"] / m["fps_den"]
    return round(fps), m["fps_den"] == 1001, fps


def _seq_rate(st: dict) -> tuple[int, bool, float]:
    if not st["has_video"]:
        return 30, False, 30.0
    fps = st["fps_num"] / st["fps_den"]
    return round(fps), st["fps_den"] == 1001, fps


def _tc(frames: int, base: int) -> str:
    f = frames % base
    s = frames // base
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


# --------------------------------------------------------------------------- XML

def _el(parent, tag, text=None, **attrs):
    e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        e.text = str(text)
    return e


def _rate_el(parent, base, ntsc):
    r = _el(parent, "rate")
    _el(r, "timebase", base)
    _el(r, "ntsc", "TRUE" if ntsc else "FALSE")
    return r


def _pathurl(path: str) -> str:
    return "file://localhost/" + quote(Path(path).resolve().as_posix(), safe="/")


def _timecode(parent, base, ntsc):
    tc = _el(parent, "timecode")
    _rate_el(tc, base, ntsc)
    _el(tc, "string", "00:00:00:00")
    _el(tc, "frame", 0)
    _el(tc, "displayformat", "NDF")


def _video_chars(parent, base, ntsc, w, h):
    v = _el(parent, "samplecharacteristics")
    _rate_el(v, base, ntsc)
    _el(v, "width", w)
    _el(v, "height", h)
    _el(v, "anamorphic", "FALSE")
    _el(v, "pixelaspectratio", "square")
    _el(v, "fielddominance", "none")


def _level_gain(db: float) -> float:
    return round(10 ** (db / 20), 5)


def _corrected_media(l: dict, lm: dict) -> dict:
    """Media info of the file to link for a layer: the original, or a speed-corrected copy if the layer drifts.

    The copy's clock matches the main recording, so Premiere stays in sync to the end: a WAV for sound-only
    layers, an H.264 mp4 when the layer has a picture."""
    if abs(l["rate"] - 1.0) < 1e-7:
        return lm
    ext = "mp4" if lm["has_video"] else "wav"
    dst = DATA_DIR / "layer_cache" / f"{l['id']}_{l['rate']:.9f}.{ext}"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if lm["has_video"]:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", lm["path"], "-vf", f"setpts=PTS/{l['rate']:.9f}",
                   *(["-af", f"atempo={l['rate']:.9f}"] if lm["has_audio"] else ["-an"]),
                   "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p",
                   *(["-c:a", "aac", "-b:a", "256k"] if lm["has_audio"] else []), str(dst)]
        else:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", lm["path"], "-vn", "-af", f"atempo={l['rate']:.9f}",
                   "-c:a", "pcm_s16le", str(dst)]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError("Could not make the drift-corrected copy: " + r.stderr.strip()[-200:])
    return probe(dst)


def _motion_filters(clipitem, ev: dict, W: int, H: int) -> None:
    """FCP7 'Basic Motion' (scale, centre) and 'Opacity' for a picture layer; the opacity has keyframes for fades."""
    m, mo = ev["m"], ev["motion"]
    h_frac = mo["scale"] * (m["height"] / max(1, m["width"])) * W / H
    cx, cy = (mo["x"] + mo["scale"] / 2) - 0.5, (mo["y"] + h_frac / 2) - 0.5
    eff = _el(_el(clipitem, "filter"), "effect")
    _el(eff, "name", "Basic Motion")
    _el(eff, "effectid", "basic")
    _el(eff, "effectcategory", "motion")
    _el(eff, "effecttype", "motion")
    _el(eff, "mediatype", "video")
    p = _el(eff, "parameter", authoringApp="PremierePro")
    _el(p, "parameterid", "scale")
    _el(p, "name", "Scale")
    _el(p, "valuemin", 0)
    _el(p, "valuemax", 1000)
    _el(p, "value", round(100.0 * mo["scale"] * W / max(1, m["width"]), 3))
    p = _el(eff, "parameter", authoringApp="PremierePro")
    _el(p, "parameterid", "center")
    _el(p, "name", "Center")
    v = _el(p, "value")
    _el(v, "horiz", round(cx, 5))
    _el(v, "vert", round(cy, 5))
    if mo["opacity"] < 0.999 or ev.get("fade_in") or ev.get("fade_out"):
        eff = _el(_el(clipitem, "filter"), "effect")
        _el(eff, "name", "Opacity")
        _el(eff, "effectid", "opacity")
        _el(eff, "effectcategory", "motion")
        _el(eff, "effecttype", "motion")
        _el(eff, "mediatype", "video")
        p = _el(eff, "parameter", authoringApp="PremierePro")
        _el(p, "parameterid", "opacity")
        _el(p, "name", "opacity")
        _el(p, "valuemin", 0)
        _el(p, "valuemax", 100)
        level = round(mo["opacity"] * 100, 2)
        _el(p, "value", level)
        length, pts = ev["end"] - ev["start"], []
        if ev.get("fade_in"):
            pts += [(0, 0.0), (ev["fade_in"], level)]
        if ev.get("fade_out"):
            pts += [(length - ev["fade_out"], level), (length, 0.0)]
        for when, val in sorted(set(pts)):
            k = _el(p, "keyframe")
            _el(k, "when", when)
            _el(k, "value", val)


def sequence_xml(name: str, projects: list[dict], clips: list[dict] | None = None,
                 free: list[dict] | None = None, seq_data: dict | None = None) -> str:
    """`clips` (aligned with `projects`) place each project on the timeline; `free` and the clips' layers are
    the audio layers; `seq_data` supplies track switches and markers. All optional (a lone project plays from 0)."""
    st = seq_settings(projects)
    if clips is None:
        clips, cum = [], 0.0
        for p in projects:
            c = {"start": cum, "in_t": 0.0, "out_t": None, "layers": []}
            clips.append(c)
            cum += sequences.clip_length(c, p)

    def layer_on(l):
        return not l["mute"] and (seq_data is None or sequences.audible(seq_data, f"A{l.get('row', 1) + 1}"))

    free = [l for l in (free or []) if layer_on(l)]
    if free or any(l["mode"] == "sync" and layer_on(l) for c in clips for l in c["layers"]):
        st["has_audio"] = True
    a1_on = seq_data is None or sequences.audible(seq_data, "A1")
    v_on = seq_data is None or sequences.video_visible(seq_data)
    base, ntsc, fps = _seq_rate(st)

    # One event per kept segment, at the clip's position (gaps stay empty).
    events = []
    for ci, p in enumerate(projects):
        m, c = p["media"], clips[ci]
        ffps = _rate(m)[2]
        replaced = any(l["replace_main_audio"] and layer_on(l) for l in c["layers"])
        acc = 0.0
        for a_s, a_e in sequences.clip_segments(c, p):
            s_f, e_f = round((c["start"] + acc) * fps), round((c["start"] + acc + (a_e - a_s)) * fps)
            acc += a_e - a_s
            if e_f - s_f < 1:
                continue
            a = round(a_s * ffps)
            events.append({"m": m, "in": a, "out": a + max(1, round((a_e - a_s) * ffps)),
                           "start": s_f, "end": e_f, "ci": ci, "s": a_s, "e": a_e,
                           "audio_off": replaced or not a1_on, "video_off": not v_on,
                           "n_ch": max(1, min(2, m["channels"])) if m["has_audio"] else 0, "ids": {}, "idx": {}})
    if not events:
        raise ValueError("Everything has been cut - nothing to export")
    cursor = max(ev["end"] for ev in events)

    counter, per_track = 0, {}
    for ev in events:
        keys = ([("video", 1)] if ev["m"]["has_video"] else []) + [("audio", c) for c in range(1, ev["n_ch"] + 1)]
        for key in keys:
            counter += 1
            ev["ids"][key] = f"clipitem-{counter}"
            per_track[key] = per_track.get(key, 0) + 1
            ev["idx"][key] = per_track[key]

    # ---- layers: one extra audio track slot per layer, laid out under the main A1/A2
    slots: list[dict] = []   # {"events": [...], "width": 1|2}

    def new_event(lm, key_ch, start, end, tin, tout, level, fi, fo, enabled=True):
        nonlocal counter
        counter += 1
        return {"m": lm, "in": tin, "out": tout, "start": start, "end": end, "n_ch": key_ch, "audio_off": not enabled,
                "ids": {("audio", key_ch): f"clipitem-{counter}"}, "idx": {("audio", key_ch): 0},
                "level": level, "fade_in": fi, "fade_out": fo}

    def fades(ev_list, fi_s, fo_s):
        """Attach fade lengths (frames) to the first / last piece of a layer."""
        for i, evs in enumerate(ev_list):
            evs["fade_in"] = min(round(fi_s * fps), evs["end"] - evs["start"]) if i == 0 else 0
            evs["fade_out"] = min(round(fo_s * fps), evs["end"] - evs["start"]) if i == len(ev_list) - 1 else 0

    def add_slot(per_channel_events: dict[int, list[dict]], width: int):
        slots.append({"events": per_channel_events, "width": width})

    vslots: dict[int, list[dict]] = {}   # picture tracks: V2 = row 1, V3 = row 2 ...

    def new_video_event(lm, start, end, tin, tout, l):
        nonlocal counter
        counter += 1
        return {"m": lm, "in": tin, "out": tout, "start": start, "end": end, "n_ch": 0, "audio_off": False,
                "video_off": False, "ids": {("video", 1): f"clipitem-{counter}"}, "idx": {("video", 1): 0},
                "motion": {"x": l["v_x"], "y": l["v_y"], "scale": l["v_scale"], "opacity": l["v_opacity"]},
                "fade_in": 0, "fade_out": 0}

    def picture_on(l, has_video):
        hidden = bool(seq_data and seq_data["tracks"].get(f"V{l['v_row'] + 1}", {}).get("mute"))
        return bool(has_video and l.get("v_on") and not hidden)

    def layer_events(l, clip_events):
        """Sound events (A tracks) and picture events (V tracks) of one layer; clip_events = (start_f, end_f, in_s, out_s) pieces."""
        try:
            lm0 = store.get(l["pid"])["media"]
        except KeyError:
            return
        sound, pic = layer_on(l) and lm0["has_audio"], picture_on(l, lm0["has_video"])
        if not sound and not pic:
            return
        lm = _corrected_media(l, lm0)
        lfps = _rate(lm)[2]
        chans = max(1, min(2, lm["channels"])) if sound else 0
        per, vevs = {ch: [] for ch in range(1, chans + 1)}, []
        for st_f, n, tin_s, len_s in clip_events:
            tin = max(0, round(tin_s * lfps))
            out_f = tin + max(1, round(len_s * lfps))
            for ch in per:
                per[ch].append(new_event(lm, ch, st_f, st_f + n, tin, out_f, _level_gain(l["gain_db"]), 0, 0))
            if pic:
                vevs.append(new_video_event(lm, st_f, st_f + n, tin, out_f, l))
        for ch in per:
            fades(per[ch], l["fade_in"], l["fade_out"])
        fades(vevs, l["fade_in"], l["fade_out"])
        if any(per.values()):
            add_slot(per, chans)
        if vevs:
            vslots.setdefault(l["v_row"], []).extend(vevs)

    for ci, c in enumerate(clips):
        for l in c["layers"]:
            try:
                lm0 = store.get(l["pid"])["media"]
            except KeyError:
                continue
            ti = l["trim_in"]
            to = min(l["trim_out"] if l["trim_out"] is not None else lm0["duration"], lm0["duration"])
            lo, hi = (ti - l["offset"]) / l["rate"], (to - l["offset"]) / l["rate"]
            pieces = []
            for ev in (e for e in events if e["ci"] == ci):
                ms, me = max(ev["s"], lo), min(ev["e"], hi)
                n = round((me - ms) * fps)
                if me - ms < 1e-3 or n < 1:
                    continue
                # (timeline start frame, length in frames, position in the layer file (s, drift-corrected), length (s))
                pieces.append((ev["start"] + round((ms - ev["s"]) * fps), n, ms + l["offset"] / l["rate"], me - ms))
            layer_events(l, pieces)
    for l in free:
        try:
            lm0 = store.get(l["pid"])["media"]
        except KeyError:
            continue
        to = min(l["trim_out"] if l["trim_out"] is not None else lm0["duration"], lm0["duration"])
        start_f = round(l["start"] * fps)
        n = min(round((to - l["trim_in"]) * fps), cursor - start_f)
        if n >= 1:
            layer_events(l, [(start_f, n, l["trim_in"], n / fps)])

    files: dict[str, str] = {}

    def file_el(parent, m):
        if m["path"] in files:
            _el(parent, "file", id=files[m["path"]])
            return
        fid = files[m["path"]] = f"file-{len(files) + 1}"
        fbase, fntsc, ffps = _rate(m)
        f = _el(parent, "file", id=fid)
        _el(f, "name", m["name"])
        _el(f, "pathurl", _pathurl(m["path"]))
        _rate_el(f, fbase, fntsc)
        _el(f, "duration", round(m["duration"] * ffps))
        _timecode(f, fbase, fntsc)
        media = _el(f, "media")
        if m["has_video"]:
            _video_chars(_el(media, "video"), fbase, fntsc, m["width"], m["height"])
        if m["has_audio"]:
            a = _el(media, "audio")
            sc = _el(a, "samplecharacteristics")
            _el(sc, "depth", 16)
            _el(sc, "samplerate", m["sample_rate"])
            _el(a, "channelcount", m["channels"])

    def clip(parent, ev, key):
        m = ev["m"]
        fbase, fntsc, ffps = _rate(m)
        c = _el(parent, "clipitem", id=ev["ids"][key])
        _el(c, "name", Path(m["name"]).stem)
        off = (key[0] == "audio" and ev.get("audio_off")) or (key[0] == "video" and ev.get("video_off"))
        _el(c, "enabled", "FALSE" if off else "TRUE")
        _el(c, "duration", round(m["duration"] * ffps))
        _rate_el(c, fbase, fntsc)
        _el(c, "start", ev["start"])
        _el(c, "end", ev["end"])
        _el(c, "in", ev["in"])
        _el(c, "out", ev["out"])
        file_el(c, m)
        if ev.get("level") is not None and (abs(ev["level"] - 1.0) > 1e-4 or ev.get("fade_in") or ev.get("fade_out")):
            _audio_levels(c, ev, ev["level"])
        if ev.get("motion"):
            _motion_filters(c, ev, st["width"], st["height"])
        src = _el(c, "sourcetrack")
        _el(src, "mediatype", key[0])
        _el(src, "trackindex", key[1])
        for lk_key, ref in ev["ids"].items():
            lk = _el(c, "link")
            _el(lk, "linkclipref", ref)
            _el(lk, "mediatype", lk_key[0])
            _el(lk, "trackindex", lk_key[1])
            _el(lk, "clipindex", ev["idx"][lk_key])

    root = ET.Element("xmeml", version="4")
    seq = _el(root, "sequence", id="sequence-1")
    _el(seq, "name", name)
    _el(seq, "duration", cursor)
    _rate_el(seq, base, ntsc)
    _timecode(seq, base, ntsc)
    for mk in (seq_data or {}).get("markers", []):
        m_el = _el(seq, "marker")
        _el(m_el, "name", mk["name"] or "Marker")
        _el(m_el, "comment", "")
        _el(m_el, "in", round(mk["t"] * fps))
        _el(m_el, "out", -1)
    media = _el(seq, "media")

    if st["has_video"]:
        video = _el(media, "video")
        _video_chars(_el(video, "format"), base, ntsc, st["width"], st["height"])
        track = _el(video, "track")
        for ev in events:
            if ("video", 1) in ev["ids"]:
                clip(track, ev, ("video", 1))
        for row in sorted(vslots):                # picture layers: V2, V3 ...
            track = _el(video, "track")
            for ev in vslots[row]:
                clip(track, ev, ("video", 1))
    if st["has_audio"]:
        audio = _el(media, "audio")
        _el(audio, "numOutputChannels", 2)
        fmt = _el(_el(audio, "format"), "samplecharacteristics")
        _el(fmt, "depth", 16)
        _el(fmt, "samplerate", st["sample_rate"])
        for ch in (1, 2):
            track = _el(audio, "track")
            for ev in events:
                if ("audio", ch) in ev["ids"]:
                    clip(track, ev, ("audio", ch))
        for slot in slots:                       # layer tracks (A3, A4, ...)
            for ch, evs in slot["events"].items():
                track = _el(audio, "track")
                for ev in evs:
                    clip(track, ev, ("audio", ch))

    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    body = pretty.split("\n", 1)[1]  # drop minidom's own XML declaration
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body


def _audio_levels(clipitem, ev, level: float) -> None:
    """FCP7 'Audio Levels' effect: constant gain, or keyframes when the layer fades in/out."""
    eff = _el(_el(clipitem, "filter"), "effect")
    _el(eff, "name", "Audio Levels")
    _el(eff, "effectid", "audiolevels")
    _el(eff, "effectcategory", "audiolevels")
    _el(eff, "effecttype", "audiolevels")
    _el(eff, "mediatype", "audio")
    par = _el(eff, "parameter", authoringApp="PremierePro")
    _el(par, "parameterid", "level")
    _el(par, "name", "Level")
    _el(par, "valuemin", 0)
    _el(par, "valuemax", 3.98109)
    _el(par, "value", level)
    length = ev["end"] - ev["start"]
    pts = []
    if ev.get("fade_in"):
        pts += [(0, 0.0), (ev["fade_in"], level)]
    if ev.get("fade_out"):
        pts += [(length - ev["fade_out"], level), (length, 0.0)]
    for when, val in sorted(set(pts)):
        k = _el(par, "keyframe")
        _el(k, "when", when)
        _el(k, "value", val)


def premiere_xml(p, seq: dict | None = None) -> str:
    """`seq` (a sequence) adds each clip's synced layers and the music layers as extra audio tracks."""
    ps = _plist(p)
    clips = free = None
    if seq is not None:
        pairs = sequences.clip_projects(seq)
        ps, clips, free = [p for _c, p in pairs], [c for c, _p in pairs], seq.get("layers", [])
    return sequence_xml(f"{Path(ps[0]['media']['name']).stem} - AutoCut" if len(ps) == 1 else "AutoCut sequence",
                        ps, clips, free, seq)


# --------------------------------------------------------------------------- EDL

def _placed(p, seq: dict | None) -> list[tuple[dict, dict]]:
    """(clip, project) pairs: from the sequence timeline, or a lone project laid out from 0."""
    if seq is not None:
        return sequences.clip_projects(seq)
    out, cum = [], 0.0
    for proj in _plist(p):
        c = {"start": cum, "in_t": 0.0, "out_t": None, "layers": []}
        out.append((c, proj))
        cum += sequences.clip_length(c, proj)
    return out


def edl(p, seq: dict | None = None) -> str:
    pairs = _placed(p, seq)
    st = seq_settings([pp for _c, pp in pairs])
    base, _ntsc, fps = _seq_rate(st)
    track = "AA/V" if st["has_video"] else "AA"
    title = Path(pairs[0][1]["media"]["name"]).stem if len(pairs) == 1 else (seq["name"] if seq else "AutoCut sequence")
    lines = [f"TITLE: {title} - AutoCut", "FCM: NON-DROP FRAME", ""]
    n = 0
    for c, proj in pairs:
        acc = 0.0
        for s_, e_ in sequences.clip_segments(c, proj):
            a, b = round(s_ * fps), round(e_ * fps)
            rec0, rec1 = round((c["start"] + acc) * fps), round((c["start"] + acc + (e_ - s_)) * fps)
            acc += e_ - s_
            if b <= a or rec1 <= rec0:
                continue
            n += 1
            lines.append(f"{n:03d}  AX       {track:<5} C        {_tc(a, base)} {_tc(b, base)} "
                         f"{_tc(rec0, base)} {_tc(rec1, base)}")
            lines.append(f"* FROM CLIP NAME: {proj['media']['name']}")
            lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------- transcript / subs

def _clip_words(c: dict, p: dict) -> list[dict]:
    """Words of a clip that survive its trim and the transcript cuts, with sequence-time stamps."""
    segs = sequences.clip_segments(c, p)
    out = []
    for w in p["words"]:
        mid, acc = (w["s"] + w["e"]) / 2, 0.0
        for a, b in segs:
            if a <= mid <= b:
                out.append({"w": w["w"], "s": c["start"] + acc + max(w["s"], a) - a, "e": c["start"] + acc + min(w["e"], b) - a})
                break
            acc += b - a
    return out


def _cues(p, seq: dict | None = None, max_chars=84, max_dur=6.0, gap=0.8):
    cues = []
    for c, proj in _placed(p, seq):
        cur = []
        for w in _clip_words(c, proj):
            if cur:
                text_len = sum(len(x["w"]) + 1 for x in cur)
                if (w["s"] - cur[-1]["e"] > gap or text_len + len(w["w"]) > max_chars
                        or w["e"] - cur[0]["s"] > max_dur or re.search(r"[.?!।…]$", cur[-1]["w"])):
                    cues.append(cur)
                    cur = []
            cur.append(w)
        if cur:
            cues.append(cur)
    return cues


def _ts(t: float, sep: str) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


def srt(p, seq: dict | None = None) -> str:
    out = []
    for i, c in enumerate(_cues(p, seq), 1):
        out.append(f"{i}\n{_ts(c[0]['s'], ',')} --> {_ts(c[-1]['e'], ',')}\n{' '.join(w['w'] for w in c)}\n")
    return "\n".join(out)


def vtt(p, seq: dict | None = None) -> str:
    out = ["WEBVTT\n"]
    for c in _cues(p, seq):
        out.append(f"{_ts(c[0]['s'], '.')} --> {_ts(c[-1]['e'], '.')}\n{' '.join(w['w'] for w in c)}\n")
    return "\n".join(out)


def txt(p, seq: dict | None = None) -> str:
    return "\n".join(" ".join(w["w"] for w in c) for c in _cues(p, seq, max_chars=400, max_dur=60, gap=1.5)) + "\n"


# --------------------------------------------------------------------- rendered media

def _expr(segs):
    return "+".join(f"gte(t,{s:.4f})*lt(t,{e:.4f})" for s, e in segs)


VENC = [["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"],
        ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]]


def _picture_spec(seq: dict, l: dict, lm: dict) -> dict | None:
    """Where and how a video layer's picture is drawn (fractions of the frame), or None when it is not shown."""
    if not lm["has_video"] or not sequences.picture_visible(seq, l):
        return None
    return {"x": l["v_x"], "y": l["v_y"], "scale": l["v_scale"], "opacity": l["v_opacity"], "row": l["v_row"]}


def _clip_layer_specs(seq: dict, clip: dict, p: dict) -> tuple[list[dict], bool]:
    """Layers attached to a clip as render specs (sound and/or picture), and whether one replaces the main audio."""
    segs = sequences.clip_segments(clip, p)
    specs, replace = [], False
    for l in clip.get("layers", []):
        try:
            lm = store.get(l["pid"])["media"]
        except KeyError:
            continue
        sound = lm["has_audio"] and not l["mute"] and sequences.audible(seq, f"A{l['row'] + 1}")
        picture = _picture_spec(seq, l, lm)
        if not sound and not picture:
            continue
        pieces = sequences.sync_pieces(l, 0.0, segs, lm["duration"])
        if not pieces:
            continue
        ti = l["trim_in"]
        to = min(l["trim_out"] if l["trim_out"] is not None else lm["duration"], lm["duration"])
        specs.append({"path": lm["path"], "offset": l["offset"], "rate": l["rate"], "gain_db": l["gain_db"],
                      "fade_in": l["fade_in"], "fade_out": l["fade_out"],
                      "lo": max(0.0, (ti - l["offset"]) / l["rate"]), "hi": (to - l["offset"]) / l["rate"],
                      "edit0": pieces[0]["seq0"], "edit1": pieces[-1]["seq1"], "audio": sound, "picture": picture})
        replace = replace or (l["replace_main_audio"] and sound)
    return specs, replace


def _free_layer_specs(seq: dict) -> list[dict]:
    specs = []
    for l in seq.get("layers", []):
        try:
            lm = store.get(l["pid"])["media"]
        except KeyError:
            continue
        sound = lm["has_audio"] and not l["mute"] and sequences.audible(seq, f"A{l['row'] + 1}")
        picture = _picture_spec(seq, l, lm)
        if sound or picture:
            to = min(l["trim_out"] if l["trim_out"] is not None else lm["duration"], lm["duration"])
            specs.append({"path": lm["path"], "start": l["start"], "trim_in": l["trim_in"], "trim_out": to,
                          "gain_db": l["gain_db"], "fade_in": l["fade_in"], "fade_out": l["fade_out"],
                          "audio": sound, "picture": picture})
    return specs


def _layer_chain(k: int, i: int, l: dict, sr: int, main_dur: float, expr: str) -> str:
    """Filters that turn layer file k into a stream on the main clip's timeline, cut like the main clip."""
    o = l["offset"] / l["rate"]
    c = f"[{k}:a]aresample={sr},aformat=channel_layouts=stereo"
    if abs(l["rate"] - 1.0) > 1e-9:
        c += f",atempo={l['rate']:.9f}"                       # remove clock drift
    c += f",atrim=start={o:.6f},asetpts=PTS-STARTPTS" if o >= 0 else f",adelay={int(round(-o * sr))}S:all=1"
    c += f",apad=whole_dur={main_dur:.3f}"
    c += f",volume=0:enable='not(between(t,{l['lo']:.6f},{l['hi']:.6f}))'"      # trim
    c += f",asetnsamples=n=64:p=0,aselect='{expr}',asetpts=N/SR/TB"             # same cuts as the main clip
    if l["gain_db"]:
        c += f",volume={l['gain_db']}dB"
    if l["fade_in"] > 0:
        c += f",afade=t=in:st={l['edit0']:.4f}:d={l['fade_in']:.4f}"
    if l["fade_out"] > 0:
        c += f",afade=t=out:st={max(l['edit0'], l['edit1'] - l['fade_out']):.4f}:d={l['fade_out']:.4f}"
    return c + f"[lay{i}]"


def _layer_picture_chain(k: int, j: int, l: dict, target: dict, main_dur: float, expr: str) -> str:
    """Filters that turn layer file k's picture into a transparent-padded overlay on the main clip's timeline."""
    W = target["width"]
    pic, o, rate = l["picture"], l["offset"], l["rate"]
    w = max(2, int(round(pic["scale"] * W / 2)) * 2)
    c = f"[{k}:v]"
    if o > 0:
        c += f"trim=start={o:.6f},setpts=(T-{o:.6f})/{rate:.9f}/TB"
    else:
        c += f"setpts=T/{rate:.9f}/TB"
    c += f",fps={target['fps_num']}/{target['fps_den']},scale={w}:-2,format=yuva420p"
    if o < 0:
        c += f",tpad=start_duration={-o / rate:.6f}:color=black@0"      # the layer starts later than the clip
    c += f",tpad=stop_duration={main_dur:.3f}:stop_mode=add:color=black@0"
    c += f",select='{expr}',setpts=N/FRAME_RATE/TB"                     # same cuts as the main clip
    if pic["opacity"] < 0.999:
        c += f",colorchannelmixer=aa={pic['opacity']:.3f}"
    if l["fade_in"] > 0:
        c += f",fade=t=in:st={l['edit0']:.4f}:d={l['fade_in']:.4f}:alpha=1"
    if l["fade_out"] > 0:
        c += f",fade=t=out:st={max(l['edit0'], l['edit1'] - l['fade_out']):.4f}:d={l['fade_out']:.4f}:alpha=1"
    return c + f"[pic{j}]"


def _render_segments(m: dict, out: Path, segs, target: dict | None, venc: list, aenc: list, progress,
                     layers: list[dict] = (), replace_main: bool = False) -> int:
    """Render the kept segments of one media file with ffmpeg; returns the index of the encoder used.

    `target` (sequences) forces frame size, rate and sample rate and fills in a missing picture or
    sound with black / silence so every clip comes out with identical streams. `layers` are extra
    recordings mixed in (sound) and drawn on top (picture) with the same cuts.
    """
    total = sum(e - s for s, e in segs)
    script = out.with_suffix(".filter.txt")
    want_v = target["has_video"] if target else m["has_video"]
    want_a = (target["has_audio"] if target else m["has_audio"]) or any(l["audio"] for l in layers)
    expr = _expr(segs)
    inputs, parts, maps, n_in = ["-i", m["path"]], [], [], 1
    vlabel = None

    if want_v:
        if m["has_video"]:
            chain = f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB"
            if target:
                w, h = target["width"], target["height"]
                chain += (f",scale={w}:{h}:force_original_aspect_ratio=decrease,"
                          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target['fps_num']}/{target['fps_den']}")
            parts.append(chain + "[v0]")
        else:
            inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i",
                       f"color=c=black:s={target['width']}x{target['height']}:r={target['fps_num']}/{target['fps_den']}"]
            parts.append(f"[{n_in}:v]null[v0]")
            n_in += 1
        vlabel = "[v0]"
    if want_a:
        sr = target["sample_rate"] if target else m["sample_rate"]
        a_src = None
        if m["has_audio"]:
            if target or layers:  # same sample rate and tiny frames as the layers, so every cut lands identically
                chain = (f"[0:a]aresample={sr},aformat=channel_layouts=stereo,asetnsamples=n=64:p=0,"
                         f"aselect='{expr}',asetpts=N/SR/TB")
            else:
                chain = f"[0:a]asetnsamples=n=256:p=0,aselect='{expr}',asetpts=N/SR/TB"
            if replace_main:
                chain += ",volume=0"
            parts.append(chain + "[ma]")
            a_src = "[ma]"
        else:
            inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", f"anullsrc=r={sr}:cl=stereo"]
            a_src = f"[{n_in}:a]"
            n_in += 1
    lin = []                                 # input index of every layer file
    for l in layers:
        inputs += ["-i", l["path"]]
        lin.append(n_in)
        n_in += 1

    if want_a:
        sources = [a_src]
        for i, l in enumerate(layers):
            if l["audio"]:
                parts.append(_layer_chain(lin[i], i, l, sr, m["duration"], expr))
                sources.append(f"[lay{i}]")
        parts.append("".join(sources) + (f"amix=inputs={len(sources)}:duration=first:normalize=0,alimiter=limit=0.97[a]"
                                         if len(sources) > 1 else "anull[a]"))
        maps += ["-map", "[a]"]
    if want_v:
        pics = sorted([(i, l) for i, l in enumerate(layers) if l.get("picture") and target], key=lambda t: t[1]["picture"]["row"])
        for j, (i, l) in enumerate(pics):
            pic = l["picture"]
            parts.append(_layer_picture_chain(lin[i], j, l, target, m["duration"], expr))
            out_label = "[v]" if j == len(pics) - 1 else f"[vo{j}]"
            parts.append(f"{vlabel}[pic{j}]overlay=x={round(pic['x'] * target['width'])}:y={round(pic['y'] * target['height'])}"
                         f":eof_action=pass:enable='between(t,{l['edit0']:.4f},{l['edit1']:.4f})'{out_label}")
            vlabel = out_label
        if not pics:
            parts.append(f"{vlabel}null[v]")
        maps = ["-map", "[v]"] + maps
    if parts:
        script.write_text(";".join(parts), encoding="utf-8")

    last_err = ""
    # ffmpeg 7+ replaced -filter_complex_script with -/filter_complex; try the new spelling first.
    for script_opt in ((["-/filter_complex", str(script)], ["-filter_complex_script", str(script)]) if parts else ([],)):
        for vi, ve in enumerate(venc if want_v else [[]]):
            cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", *inputs, *script_opt,
                   *maps, *ve, *aenc]
            if out.suffix == ".mp4":
                cmd += ["-movflags", "+faststart"]
            cmd.append(str(out))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    creationflags=_NO_WINDOW)
            for line in proc.stdout:  # type: ignore[union-attr]
                if line.startswith("out_time_us="):
                    try:
                        progress(min(0.99, int(line.split("=")[1]) / 1e6 / total))
                    except ValueError:
                        pass
            err = proc.stderr.read()  # type: ignore[union-attr]
            if proc.wait() == 0 and out.exists():
                script.unlink(missing_ok=True)
                return vi
            last_err = err
            if "Unrecognized option" in err or "Option not found" in err:
                break  # wrong spelling for this ffmpeg; try the other one
    raise RuntimeError(f"ffmpeg failed: {last_err.strip()[-500:]}")


def _final_audio_codec(ext: str) -> list[str]:
    return {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], "wav": ["-c:a", "pcm_s16le"],
            "flac": ["-c:a", "flac"]}.get(ext, ["-c:a", "aac", "-b:a", "192k"])


def render_media(p: dict, fmt: str | None = None, out_dir: str | None = None) -> Path:
    """Render one project's edited media. Updates p['export'] with progress."""
    m = p["media"]
    segs = editing.keep_segments(p)
    if not segs:
        raise ValueError("Everything has been cut - nothing to export")
    d = export_dir(p, out_dir)
    ext = (fmt or ("mp4" if m["has_video"] else "m4a")).lstrip(".").lower()
    if not m["has_video"] and ext in VIDEO_CONTAINERS:
        ext = "m4a"
    out = d / f"{_safe_name(Path(m['name']).stem)}_autocut.{ext}"

    def progress(f):
        p["export"]["progress"] = f

    _render_segments(m, out, segs, None, VENC, _final_audio_codec(ext), progress)
    return out


def _mix_free_layers(joined: Path, out: Path, specs: list[dict], st: dict, total: float, ext: str) -> None:
    """Mix music / free layers into the joined sequence: their sound is mixed and their picture drawn on top.

    The picture of the sequence is copied when no free layer has one, otherwise it is encoded again once."""
    sr, W, H = st["sample_rate"], st["width"], st["height"]
    inputs, parts = ["-i", str(joined)], []
    sources = []
    if st["has_audio"]:
        sources.append("[0:a]")
    elif any(l["audio"] for l in specs):
        inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", f"anullsrc=r={sr}:cl=stereo"]
        sources.append("[1:a]")
    k = len(inputs) // 2
    pics = []
    for i, l in enumerate(specs):
        inputs += ["-i", l["path"]]
        length = l["trim_out"] - l["trim_in"]
        if l["audio"]:
            c = (f"[{k}:a]atrim=start={l['trim_in']:.4f}:end={l['trim_out']:.4f},asetpts=PTS-STARTPTS,"
                 f"aresample={sr},aformat=channel_layouts=stereo")
            if l["gain_db"]:
                c += f",volume={l['gain_db']}dB"
            if l["fade_in"] > 0:
                c += f",afade=t=in:st=0:d={l['fade_in']:.4f}"
            if l["fade_out"] > 0:
                c += f",afade=t=out:st={max(0.0, length - l['fade_out']):.4f}:d={l['fade_out']:.4f}"
            c += f",adelay={int(round(l['start'] * sr))}S:all=1[f{i}]"
            parts.append(c)
            sources.append(f"[f{i}]")
        if l["picture"] and st["has_video"]:
            pic = l["picture"]
            w = max(2, int(round(pic["scale"] * W / 2)) * 2)
            c = (f"[{k}:v]trim=start={l['trim_in']:.4f}:end={l['trim_out']:.4f},setpts=PTS-STARTPTS+{l['start']:.4f}/TB,"
                 f"fps={st['fps_num']}/{st['fps_den']},scale={w}:-2,format=yuva420p")
            if pic["opacity"] < 0.999:
                c += f",colorchannelmixer=aa={pic['opacity']:.3f}"
            if l["fade_in"] > 0:
                c += f",fade=t=in:st={l['start']:.4f}:d={l['fade_in']:.4f}:alpha=1"
            if l["fade_out"] > 0:
                c += f",fade=t=out:st={l['start'] + max(0.0, length - l['fade_out']):.4f}:d={l['fade_out']:.4f}:alpha=1"
            parts.append(c + f"[fp{i}]")
            pics.append((i, l, length))
        k += 1
    maps = []
    if sources:
        parts.append("".join(sources) + (f"amix=inputs={len(sources)}:duration=first:normalize=0,alimiter=limit=0.97[a]"
                                         if len(sources) > 1 else "anull[a]"))
        maps += ["-map", "[a]"]
    if st["has_video"]:
        base = "[0:v]"
        for n, (i, l, length) in enumerate(sorted(pics, key=lambda t: t[1]["picture"]["row"])):
            pic = l["picture"]
            lab = "[v]" if n == len(pics) - 1 else f"[vm{n}]"
            parts.append(f"{base}[fp{i}]overlay=x={round(pic['x'] * W)}:y={round(pic['y'] * H)}:eof_action=pass:"
                         f"enable='between(t,{l['start']:.4f},{l['start'] + length:.4f})'{lab}")
            base = lab
        maps = ["-map", base if pics else "0:v"] + maps
    venc = VENC if pics else [["-c:v", "copy"]]
    script = out.with_suffix(".mix.txt")
    script.write_text(";".join(parts), encoding="utf-8")
    err = ""
    for opt in (["-/filter_complex", str(script)], ["-filter_complex_script", str(script)]):
        for ve in (venc if st["has_video"] else [[]]):
            cmd = ["ffmpeg", "-y", "-v", "error", *inputs, *opt, *maps, *ve, *(_final_audio_codec(ext) if sources else [])]
            if ext == "mp4":
                cmd += ["-movflags", "+faststart"]
            cmd.append(str(out))
            r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
            if r.returncode == 0:
                script.unlink(missing_ok=True)
                return
            err = r.stderr
        if "Unrecognized option" not in err and "Option not found" not in err:
            break
    raise RuntimeError(f"ffmpeg music/picture mix failed: {err.strip()[-400:]}")


def _render_gap(out: Path, length: float, st: dict, venc: list, with_audio: bool) -> int:
    """Black picture and silence for an empty stretch of the timeline, encoded like the clips so the join is seamless."""
    err = ""
    for vi, ve in enumerate(venc if st["has_video"] else [[]]):
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if st["has_video"]:
            cmd += ["-f", "lavfi", "-t", f"{length:.3f}", "-i",
                    f"color=c=black:s={st['width']}x{st['height']}:r={st['fps_num']}/{st['fps_den']}"]
        if with_audio:  # same streams as the clips, or the join would not line up
            cmd += ["-f", "lavfi", "-t", f"{length:.3f}", "-i", f"anullsrc=r={st['sample_rate']}:cl=stereo"]
        cmd += [*ve, *(["-c:a", "aac", "-b:a", "192k", "-ar", str(st["sample_rate"]), "-ac", "2"] if with_audio else [])]
        if out.suffix == ".mp4":
            cmd += ["-movflags", "+faststart"]
        r = subprocess.run(cmd + [str(out)], capture_output=True, text=True, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            return vi
        err = r.stderr
    raise RuntimeError(f"ffmpeg gap failed: {err.strip()[-300:]}")


def render_sequence(seq: dict, projects: list[dict] | None = None, fmt: str | None = None,
                    out_dir: str | None = None) -> Path:
    """Render each clip (with its layers) to a uniform file, fill gaps with black/silence, join them, mix music layers."""
    pairs = [(c, p, sequences.clip_segments(c, p)) for c, p in sequences.clip_projects(seq)]
    pairs = [(c, p, segs) for c, p, segs in pairs if segs]
    if not pairs:
        raise ValueError("Everything has been cut - nothing to export")
    st = seq_settings([p for _c, p, _s in pairs])
    free = _free_layer_specs(seq)
    clip_specs = [_clip_layer_specs(seq, c, p) for c, p, _s in pairs]
    st["has_audio"] = (st["has_audio"] or any(l["audio"] for l in free)
                       or any(l["audio"] for sp, _r in clip_specs for l in sp))
    show_video = sequences.video_visible(seq)
    a1_on = sequences.audible(seq, "A1")
    end = max(c["start"] + sum(e - s for s, e in segs) for c, _p, segs in pairs)
    d = export_dir(seq, out_dir, sub="sequences")
    ext = (fmt or ("mp4" if st["has_video"] else "m4a")).lstrip(".").lower()
    if not st["has_video"] and ext in VIDEO_CONTAINERS:
        ext = "m4a"
    out = d / f"{_safe_name(seq['name'])}_autocut.{ext}"
    tmp = d / "_clips"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    inter_ext = "mp4" if st["has_video"] else "m4a"

    used, files, cursor, n = None, [], 0.0, 0
    try:
        for (c, p, segs), (layers, replace) in zip(pairs, clip_specs):
            length = sum(e - s for s, e in segs)
            gap = c["start"] - cursor
            if gap > 0.02:  # empty timeline before this clip
                gap_out = tmp / f"gap{n:03d}.{inter_ext}"
                used = _render_gap(gap_out, gap, st, [VENC[used]] if used is not None else VENC, st["has_audio"])
                files.append(gap_out)
                n += 1
            clip_out = tmp / f"clip{n:03d}.{inter_ext}"
            n += 1
            base = c["start"]
            media_m = p["media"] if show_video else {**p["media"], "has_video": False}
            used = _render_segments(
                media_m, clip_out, segs, st, [VENC[used]] if used is not None else VENC,
                ["-c:a", "aac", "-b:a", "192k", "-ar", str(st["sample_rate"]), "-ac", "2"],
                lambda f, base=base, length=length: seq["export"].update(progress=min(0.97, (base + f * length) / end)),
                layers, replace or not a1_on)
            files.append(clip_out)
            cursor = c["start"] + length
        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
        joined = tmp / f"joined.{inter_ext}"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                            "-c", "copy", str(joined)], capture_output=True, text=True, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg join failed: {r.stderr.strip()[-400:]}")
        if free:
            _mix_free_layers(joined, out, free, st, end, ext)
        else:
            copy = ext in ("mp4", "m4a", "mov", "mkv")
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(joined),
                   *(["-c", "copy"] if copy else ["-vn", *_final_audio_codec(ext)])]
            if ext == "mp4":
                cmd += ["-movflags", "+faststart"]
            r = subprocess.run(cmd + [str(out)], capture_output=True, text=True, creationflags=_NO_WINDOW)
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg finish failed: {r.stderr.strip()[-400:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def write_text_export(owner: dict, kind: str, out_dir: str | None = None, projects: list[dict] | None = None,
                      sub: str = "projects") -> Path:
    """Write xml/edl/srt/vtt/txt for a project (owner) or a sequence (owner + its projects)."""
    ps = projects if projects is not None else [owner]
    stem = _safe_name(owner["name"] if projects is not None else Path(owner["media"]["name"]).stem)
    makers = {"xml": (premiere_xml, "_autocut.xml"), "edl": (edl, "_autocut.edl"),
              "srt": (srt, "_autocut.srt"), "vtt": (vtt, "_autocut.vtt"), "txt": (txt, "_autocut.txt")}
    if kind not in makers:
        raise ValueError(f"Unknown export type: {kind}")
    fn, suffix = makers[kind]
    path = export_dir(owner, out_dir, sub) / f"{stem}{suffix}"
    path.write_text(fn(ps, owner if projects is not None else None), encoding="utf-8")
    return path
