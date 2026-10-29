"""ffmpeg / ffprobe helpers: probing, browser proxies, audio extraction."""
import json
import subprocess
from fractions import Fraction
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".mxf", ".flv", ".wmv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aif", ".aiff"}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW, **kw)


def _frac(s: str | None) -> Fraction | None:
    try:
        f = Fraction(s)
        return f if f > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe(path: str | Path) -> dict:
    """Return duration, frame rate, size and audio format of a media file."""
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise ValueError(f"ffprobe could not read {path}: {r.stderr.strip()[:300]}")
    info = json.loads(r.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None and video is None:
        raise ValueError("No audio or video stream found")
    duration = float(info.get("format", {}).get("duration") or (video or audio).get("duration") or 0)

    out = {
        "path": str(Path(path).resolve()),
        "name": Path(path).name,
        "kind": "video" if video else "audio",
        "has_video": bool(video),
        "has_audio": bool(audio),
        "duration": duration,
        "fps_num": 30, "fps_den": 1, "width": 0, "height": 0,
        "sample_rate": 48000, "channels": 2,
    }
    if video:
        fps = _frac(video.get("avg_frame_rate")) or _frac(video.get("r_frame_rate")) or Fraction(30)
        # Snap near-NTSC rates to the exact broadcast fractions Premiere expects.
        for exact in (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001)):
            if abs(float(fps) - float(exact)) < 0.01:
                fps = exact
        out.update(fps_num=fps.numerator, fps_den=fps.denominator,
                   width=int(video.get("width", 0)), height=int(video.get("height", 0)))
    if audio:
        out.update(sample_rate=int(audio.get("sample_rate", 48000)), channels=int(audio.get("channels", 2)))
    return out


def extract_wav(src: str, dst: Path) -> None:
    r = run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(dst)])
    if r.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {r.stderr.strip()[:400]}")


def make_proxy(src: str, dst: Path, has_video: bool) -> bool:
    """Make a browser-playable copy (H.264/AAC) with the same timeline as the source."""
    if not has_video:
        cmds = [["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-c:a", "aac", "-b:a", "128k", str(dst)]]
    else:
        vf = "scale=-2:'min(720,ih)'"
        base = ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf]
        tail = ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
        cmds = [
            base + ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "30", "-pix_fmt", "yuv420p"] + tail,
            base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p"] + tail,
        ]
    for cmd in cmds:
        if run(cmd).returncode == 0 and dst.exists():
            return True
    return False
