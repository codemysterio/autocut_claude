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

from . import editing, store
from .config import DATA_DIR
from .media import _NO_WINDOW

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


def sequence_xml(name: str, projects: list[dict]) -> str:
    st = seq_settings(projects)
    base, ntsc, fps = _seq_rate(st)

    # One event per kept segment, placed back to back.
    events, cursor = [], 0
    for p in projects:
        m = p["media"]
        ffps = _rate(m)[2]
        for s, e in editing.keep_segments(p):
            n = round((e - s) * fps)
            if n < 1:
                continue
            a = round(s * ffps)
            events.append({"m": m, "in": a, "out": a + max(1, round((e - s) * ffps)),
                           "start": cursor, "end": cursor + n,
                           "n_ch": max(1, min(2, m["channels"])) if m["has_audio"] else 0, "ids": {}, "idx": {}})
            cursor += n
    if not events:
        raise ValueError("Everything has been cut - nothing to export")

    counter, per_track = 0, {}
    for ev in events:
        keys = ([("video", 1)] if ev["m"]["has_video"] else []) + [("audio", c) for c in range(1, ev["n_ch"] + 1)]
        for key in keys:
            counter += 1
            ev["ids"][key] = f"clipitem-{counter}"
            per_track[key] = per_track.get(key, 0) + 1
            ev["idx"][key] = per_track[key]

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
        _el(c, "enabled", "TRUE")
        _el(c, "duration", round(m["duration"] * ffps))
        _rate_el(c, fbase, fntsc)
        _el(c, "start", ev["start"])
        _el(c, "end", ev["end"])
        _el(c, "in", ev["in"])
        _el(c, "out", ev["out"])
        file_el(c, m)
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
    media = _el(seq, "media")

    if st["has_video"]:
        video = _el(media, "video")
        _video_chars(_el(video, "format"), base, ntsc, st["width"], st["height"])
        track = _el(video, "track")
        for ev in events:
            if ("video", 1) in ev["ids"]:
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

    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    body = pretty.split("\n", 1)[1]  # drop minidom's own XML declaration
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body


def premiere_xml(p) -> str:
    ps = _plist(p)
    return sequence_xml(f"{Path(ps[0]['media']['name']).stem} - AutoCut" if len(ps) == 1 else "AutoCut sequence", ps)


# --------------------------------------------------------------------------- EDL

def edl(p) -> str:
    ps = _plist(p)
    st = seq_settings(ps)
    base, _ntsc, fps = _seq_rate(st)
    track = "AA/V" if st["has_video"] else "AA"
    lines = [f"TITLE: {Path(ps[0]['media']['name']).stem if len(ps) == 1 else 'AutoCut sequence'} - AutoCut",
             "FCM: NON-DROP FRAME", ""]
    rec, n = 0, 0
    for proj in ps:
        m = proj["media"]
        for s, e in editing.keep_segments(proj):
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

def _cues(p, max_chars=84, max_dur=6.0, gap=0.8):
    cues, offset = [], 0.0
    for proj in _plist(p):
        cur = []
        for w in editing.kept_words(proj):
            w = {**w, "s": w["s"] + offset, "e": w["e"] + offset}
            if cur:
                text_len = sum(len(x["w"]) + 1 for x in cur)
                if (w["s"] - cur[-1]["e"] > gap or text_len + len(w["w"]) > max_chars
                        or w["e"] - cur[0]["s"] > max_dur or re.search(r"[.?!।…]$", cur[-1]["w"])):
                    cues.append(cur)
                    cur = []
            cur.append(w)
        if cur:
            cues.append(cur)
        offset += sum(e - s for s, e in editing.keep_segments(proj))
    return cues


def _ts(t: float, sep: str) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


def srt(p) -> str:
    out = []
    for i, c in enumerate(_cues(p), 1):
        out.append(f"{i}\n{_ts(c[0]['s'], ',')} --> {_ts(c[-1]['e'], ',')}\n{' '.join(w['w'] for w in c)}\n")
    return "\n".join(out)


def vtt(p) -> str:
    out = ["WEBVTT\n"]
    for c in _cues(p):
        out.append(f"{_ts(c[0]['s'], '.')} --> {_ts(c[-1]['e'], '.')}\n{' '.join(w['w'] for w in c)}\n")
    return "\n".join(out)


def txt(p) -> str:
    return "\n".join(" ".join(w["w"] for w in c) for c in _cues(p, max_chars=400, max_dur=60, gap=1.5)) + "\n"


# --------------------------------------------------------------------- rendered media

def _expr(segs):
    return "+".join(f"gte(t,{s:.4f})*lt(t,{e:.4f})" for s, e in segs)


VENC = [["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"],
        ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]]


def _render_segments(m: dict, out: Path, segs, target: dict | None, venc: list, aenc: list, progress) -> int:
    """Render the kept segments of one media file with ffmpeg; returns the index of the encoder used.

    `target` (sequences) forces frame size, rate and sample rate and fills in a missing picture or
    sound with black / silence so every clip comes out with identical streams.
    """
    total = sum(e - s for s, e in segs)
    script = out.with_suffix(".filter.txt")
    want_v = target["has_video"] if target else m["has_video"]
    want_a = target["has_audio"] if target else m["has_audio"]
    expr = _expr(segs)
    inputs, parts, maps, n_in = ["-i", m["path"]], [], [], 1

    if want_v:
        if m["has_video"]:
            chain = f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB"
            if target:
                w, h = target["width"], target["height"]
                chain += (f",scale={w}:{h}:force_original_aspect_ratio=decrease,"
                          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target['fps_num']}/{target['fps_den']}")
            parts.append(chain + "[v]")
            maps += ["-map", "[v]"]
        else:
            inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i",
                       f"color=c=black:s={target['width']}x{target['height']}:r={target['fps_num']}/{target['fps_den']}"]
            maps += ["-map", f"{n_in}:v"]
            n_in += 1
    if want_a:
        if m["has_audio"]:
            chain = f"[0:a]asetnsamples=n=256:p=0,aselect='{expr}',asetpts=N/SR/TB"
            if target:
                chain += f",aresample={target['sample_rate']},aformat=channel_layouts=stereo"
            parts.append(chain + "[a]")
            maps += ["-map", "[a]"]
        else:
            inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", f"anullsrc=r={target['sample_rate']}:cl=stereo"]
            maps += ["-map", f"{n_in}:a"]
            n_in += 1
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


def render_sequence(seq: dict, projects: list[dict], fmt: str | None = None, out_dir: str | None = None) -> Path:
    """Render every clip to a uniform intermediate file, then join them without re-encoding."""
    st = seq_settings(projects)
    parts = [(p, editing.keep_segments(p)) for p in projects]
    parts = [(p, s) for p, s in parts if s]
    if not parts:
        raise ValueError("Everything has been cut - nothing to export")
    total = sum(e - s for _p, segs in parts for s, e in segs)
    d = export_dir(seq, out_dir, sub="sequences")
    ext = (fmt or ("mp4" if st["has_video"] else "m4a")).lstrip(".").lower()
    if not st["has_video"] and ext in VIDEO_CONTAINERS:
        ext = "m4a"
    out = d / f"{_safe_name(seq['name'])}_autocut.{ext}"
    tmp = d / "_clips"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    inter_ext = "mp4" if st["has_video"] else "m4a"

    done, used, files = 0.0, None, []
    try:
        for i, (p, segs) in enumerate(parts):
            length = sum(e - s for s, e in segs)
            clip_out = tmp / f"clip{i:03d}.{inter_ext}"
            base = done
            used = _render_segments(
                p["media"], clip_out, segs, st, [VENC[used]] if used is not None else VENC,
                ["-c:a", "aac", "-b:a", "192k", "-ar", str(st["sample_rate"]), "-ac", "2"],
                lambda f, base=base, length=length: seq["export"].update(progress=min(0.97, (base + f * length) / total)))
            files.append(clip_out)
            done += length
        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
        copy_audio = ext in ("mp4", "m4a", "mov", "mkv")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
               *(["-c", "copy"] if copy_audio else ["-vn", *_final_audio_codec(ext)])]
        if ext == "mp4":
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(out))
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg join failed: {r.stderr.strip()[-400:]}")
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
    path.write_text(fn(ps), encoding="utf-8")
    return path
