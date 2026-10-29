"""Exports: Premiere Pro XML (FCP7 xmeml), EDL, subtitles, transcript text, rendered media."""
import re
import subprocess
from pathlib import Path
from urllib.parse import quote
from xml.dom import minidom
from xml.etree import ElementTree as ET

from . import editing, store
from .media import _NO_WINDOW


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "autocut"


def export_dir(p: dict, out_dir: str | None = None) -> Path:
    d = Path(out_dir) if out_dir else store.project_dir(p["id"]) / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rate(m: dict) -> tuple[int, bool, float]:
    """(timebase, ntsc, exact fps). Audio-only media uses a 30 fps timeline."""
    if not m["has_video"]:
        return 30, False, 30.0
    fps = m["fps_num"] / m["fps_den"]
    return round(fps), m["fps_den"] == 1001, fps


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


def premiere_xml(p: dict) -> str:
    m = p["media"]
    base, ntsc, fps = _rate(m)
    segs = editing.keep_segments(p)
    frames = []
    cursor = 0
    for s, e in segs:
        a, b = round(s * fps), round(e * fps)
        if b > a:
            frames.append((a, b, cursor, cursor + (b - a)))
            cursor += b - a
    if not frames:
        raise ValueError("Everything has been cut - nothing to export")
    total = cursor
    file_frames = round(m["duration"] * fps)
    has_v = m["has_video"]
    n_ch = max(1, min(2, m["channels"])) if m["has_audio"] else 0
    n = len(frames)

    def vid_id(k): return f"clipitem-{k}"
    def aud_id(ch, k): return f"clipitem-{n * (ch if has_v else ch - 1) + k}"

    def file_el(parent):
        f = _el(parent, "file", id="file-1")
        _el(f, "name", m["name"])
        _el(f, "pathurl", _pathurl(m["path"]))
        _rate_el(f, base, ntsc)
        _el(f, "duration", file_frames)
        tc = _el(f, "timecode")
        _rate_el(tc, base, ntsc)
        _el(tc, "string", "00:00:00:00")
        _el(tc, "frame", 0)
        _el(tc, "displayformat", "NDF")
        media = _el(f, "media")
        if has_v:
            v = _el(_el(media, "video"), "samplecharacteristics")
            _rate_el(v, base, ntsc)
            _el(v, "width", m["width"])
            _el(v, "height", m["height"])
            _el(v, "anamorphic", "FALSE")
            _el(v, "pixelaspectratio", "square")
            _el(v, "fielddominance", "none")
        if n_ch:
            a = _el(media, "audio")
            sc = _el(a, "samplecharacteristics")
            _el(sc, "depth", 16)
            _el(sc, "samplerate", m["sample_rate"])
            _el(a, "channelcount", m["channels"])

    def links(parent, k):
        refs = []
        if has_v:
            refs.append((vid_id(k), "video", 1))
        refs += [(aud_id(ch, k), "audio", ch) for ch in range(1, n_ch + 1)]
        for ref, mt, ti in refs:
            lk = _el(parent, "link")
            _el(lk, "linkclipref", ref)
            _el(lk, "mediatype", mt)
            _el(lk, "trackindex", ti)
            _el(lk, "clipindex", k)

    def clip(parent, cid, k, media_type, track_index, first_file):
        a, b, st, en = frames[k - 1]
        c = _el(parent, "clipitem", id=cid)
        _el(c, "name", Path(m["name"]).stem)
        _el(c, "enabled", "TRUE")
        _el(c, "duration", file_frames)
        _rate_el(c, base, ntsc)
        _el(c, "start", st)
        _el(c, "end", en)
        _el(c, "in", a)
        _el(c, "out", b)
        if first_file:
            file_el(c)
        else:
            _el(c, "file", id="file-1")
        src = _el(c, "sourcetrack")
        _el(src, "mediatype", media_type)
        _el(src, "trackindex", track_index)
        links(c, k)

    root = ET.Element("xmeml", version="4")
    seq = _el(root, "sequence", id="sequence-1")
    _el(seq, "name", f"{Path(m['name']).stem} - AutoCut")
    _el(seq, "duration", total)
    _rate_el(seq, base, ntsc)
    tc = _el(seq, "timecode")
    _rate_el(tc, base, ntsc)
    _el(tc, "string", "00:00:00:00")
    _el(tc, "frame", 0)
    _el(tc, "displayformat", "NDF")
    media = _el(seq, "media")

    first = True
    if has_v:
        video = _el(media, "video")
        fmt = _el(_el(video, "format"), "samplecharacteristics")
        _rate_el(fmt, base, ntsc)
        _el(fmt, "width", m["width"])
        _el(fmt, "height", m["height"])
        _el(fmt, "anamorphic", "FALSE")
        _el(fmt, "pixelaspectratio", "square")
        _el(fmt, "fielddominance", "none")
        track = _el(video, "track")
        for k in range(1, n + 1):
            clip(track, vid_id(k), k, "video", 1, first)
            first = False
    if n_ch:
        audio = _el(media, "audio")
        _el(audio, "numOutputChannels", n_ch)
        fmt = _el(_el(audio, "format"), "samplecharacteristics")
        _el(fmt, "depth", 16)
        _el(fmt, "samplerate", m["sample_rate"])
        for ch in range(1, n_ch + 1):
            track = _el(audio, "track")
            for k in range(1, n + 1):
                clip(track, aud_id(ch, k), k, "audio", ch, first)
                first = False

    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    body = pretty.split("\n", 1)[1]  # drop minidom's own XML declaration
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body


# --------------------------------------------------------------------------- EDL

def edl(p: dict) -> str:
    m = p["media"]
    base, _ntsc, fps = _rate(m)
    track = "AA/V" if m["has_video"] else "AA"
    lines = [f"TITLE: {Path(m['name']).stem} - AutoCut", "FCM: NON-DROP FRAME", ""]
    rec = 0
    n = 0
    for s, e in editing.keep_segments(p):
        a, b = round(s * fps), round(e * fps)
        if b <= a:
            continue
        n += 1
        lines.append(f"{n:03d}  AX       {track:<5} C        {_tc(a, base)} {_tc(b, base)} "
                     f"{_tc(rec, base)} {_tc(rec + b - a, base)}")
        lines.append(f"* FROM CLIP NAME: {m['name']}")
        lines.append("")
        rec += b - a
    return "\n".join(lines)


# --------------------------------------------------------------- transcript / subs

def _cues(p: dict, max_chars=84, max_dur=6.0, gap=0.8):
    words = editing.kept_words(p)
    cues, cur = [], []
    for w in words:
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


def srt(p: dict) -> str:
    out = []
    for i, c in enumerate(_cues(p), 1):
        out.append(f"{i}\n{_ts(c[0]['s'], ',')} --> {_ts(c[-1]['e'], ',')}\n{' '.join(w['w'] for w in c)}\n")
    return "\n".join(out)


def vtt(p: dict) -> str:
    out = ["WEBVTT\n"]
    for c in _cues(p):
        out.append(f"{_ts(c[0]['s'], '.')} --> {_ts(c[-1]['e'], '.')}\n{' '.join(w['w'] for w in c)}\n")
    return "\n".join(out)


def txt(p: dict) -> str:
    return "\n".join(" ".join(w["w"] for w in c) for c in _cues(p, max_chars=400, max_dur=60, gap=1.5)) + "\n"


# --------------------------------------------------------------------- rendered media

def _expr(segs):
    return "+".join(f"gte(t,{s:.4f})*lt(t,{e:.4f})" for s, e in segs)


def render_media(p: dict, fmt: str | None = None, out_dir: str | None = None) -> Path:
    """Render the edited media with ffmpeg. Updates p['export'] with progress."""
    m = p["media"]
    segs = editing.keep_segments(p)
    if not segs:
        raise ValueError("Everything has been cut - nothing to export")
    total = sum(e - s for s, e in segs)
    d = export_dir(p, out_dir)
    ext = (fmt or ("mp4" if m["has_video"] else "m4a")).lstrip(".").lower()
    if not m["has_video"] and ext in ("mp4", "mov", "mkv"):
        ext = "m4a"
    out = d / f"{_safe_name(Path(m['name']).stem)}_autocut.{ext}"
    script = d / "filter.txt"

    parts, maps = [], []
    e = _expr(segs)
    if m["has_video"]:
        parts.append(f"[0:v]select='{e}',setpts=N/FRAME_RATE/TB[v]")
        maps += ["-map", "[v]"]
    if m["has_audio"]:
        parts.append(f"[0:a]asetnsamples=n=256:p=0,aselect='{e}',asetpts=N/SR/TB[a]")
        maps += ["-map", "[a]"]
    script.write_text(";".join(parts), encoding="utf-8")

    if m["has_video"]:
        venc = [["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"],
                ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]]
    else:
        venc = [[]]
    aenc = {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], "wav": ["-c:a", "pcm_s16le"],
            "flac": ["-c:a", "flac"]}.get(ext, ["-c:a", "aac", "-b:a", "192k"])

    # ffmpeg 7+ replaced -filter_complex_script with -/filter_complex; try the new spelling first.
    last_err = ""
    for script_opt in (["-/filter_complex", str(script)], ["-filter_complex_script", str(script)]):
        for ve in venc:
            cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", "-i", m["path"],
                   *script_opt, *maps, *ve, *aenc]
            if ext == "mp4":
                cmd += ["-movflags", "+faststart"]
            cmd.append(str(out))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    creationflags=_NO_WINDOW)
            for line in proc.stdout:  # type: ignore[union-attr]
                if line.startswith("out_time_us="):
                    try:
                        p["export"]["progress"] = min(0.99, int(line.split("=")[1]) / 1e6 / total)
                    except ValueError:
                        pass
            err = proc.stderr.read()  # type: ignore[union-attr]
            if proc.wait() == 0 and out.exists():
                script.unlink(missing_ok=True)
                return out
            last_err = err
            if "Unrecognized option" in err or "Option not found" in err:
                break  # wrong spelling for this ffmpeg; try the other one
    raise RuntimeError(f"ffmpeg failed: {last_err.strip()[-500:]}")


def write_text_export(p: dict, kind: str, out_dir: str | None = None) -> Path:
    stem = _safe_name(Path(p["media"]["name"]).stem)
    makers = {"xml": (premiere_xml, "_autocut.xml"), "edl": (edl, "_autocut.edl"),
              "srt": (srt, "_autocut.srt"), "vtt": (vtt, "_autocut.vtt"), "txt": (txt, "_autocut.txt")}
    if kind not in makers:
        raise ValueError(f"Unknown export type: {kind}")
    fn, suffix = makers[kind]
    path = export_dir(p, out_dir) / f"{stem}{suffix}"
    path.write_text(fn(p), encoding="utf-8")
    return path
