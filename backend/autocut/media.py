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


SPRITE_W, SPRITE_H = 160, 90


def sprite_count(duration: float) -> int:
    return int(min(60, max(2, -(-duration // 2))))


def make_sprite(src: str, dst: Path, duration: float) -> bool:
    """One JPEG strip of evenly spaced thumbnails (count x 1 tiles) for the timeline filmstrip."""
    count = sprite_count(duration)
    vf = (f"fps={count}/{max(duration, 0.1):.3f},scale={SPRITE_W}:{SPRITE_H}:force_original_aspect_ratio=decrease,"
          f"pad={SPRITE_W}:{SPRITE_H}:(ow-iw)/2:(oh-ih)/2,tile={count}x1")
    # Long files: decode keyframes only, thumbnails do not need to be exact.
    pre = ["-skip_frame", "nokey"] if duration > 120 else []
    r = run(["ffmpeg", "-y", "-v", "error", *pre, "-i", src, "-vf", vf, "-frames:v", "1", "-q:v", "5", str(dst)])
    return r.returncode == 0 and dst.exists()


def _run_with_progress(cmd: list[str], duration: float, progress) -> bool:
    """Run ffmpeg with -progress output; report the finished fraction through `progress`."""
    proc = subprocess.Popen(cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, creationflags=_NO_WINDOW)
    for line in proc.stdout:  # type: ignore[union-attr]
        if progress and duration and line.startswith("out_time_us="):
            try:
                progress(min(0.99, int(line.split("=")[1]) / 1e6 / duration))
            except ValueError:
                pass
    return proc.wait() == 0


def make_proxy(src: str, dst: Path, has_video: bool, duration: float = 0.0, progress=None) -> bool:
    """Make a browser-playable copy (H.264/AAC) with the same timeline as the source.

    Tries the fully GPU path first (NVDEC decode + NVENC encode), which is several times faster
    than CPU decoding for 4K / HEVC footage, then falls back to slower but more compatible paths.
    """
    if not has_video:
        cmds = [["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-c:a", "aac", "-b:a", "128k", str(dst)]]
    else:
        tail = ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
        cpu_in = ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", "scale=-2:'min(720,ih)'"]
        cmds = [
            ["ffmpeg", "-y", "-v", "error", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", src,
             "-vf", "scale_cuda=-2:720:format=nv12", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "30"] + tail,
            cpu_in + ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "30", "-pix_fmt", "yuv420p"] + tail,
            cpu_in + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p"] + tail,
        ]
    for cmd in cmds:
        dst.unlink(missing_ok=True)
        if _run_with_progress(cmd, duration, progress) and dst.exists():
            return True
    dst.unlink(missing_ok=True)
    return False
