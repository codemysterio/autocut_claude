"""Local Whisper transcription (faster-whisper) and the import pipeline."""
import os
import site
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import media, store
from .config import DEFAULT_MODEL

_executor = ThreadPoolExecutor(max_workers=1)  # one transcription at a time (GPU)
_model_lock = threading.Lock()
_model = {"key": None, "obj": None, "device": None}

# Whisper tends to drop "um"/"uh". A prompt that contains them makes it keep them,
# which is what we need to be able to cut them.
PROMPTS = {
    "en": "Umm, let me think like, hmm... Okay, here's what I'm, like, thinking.",
    "it": "Ehm, allora, dunque, cioè... mmm, vediamo, diciamo che, ehm, ci siamo.",
    "hi": "हम्म, तो, मतलब, उम्म... अच्छा, ठीक है, यानी हाँ.",
    "hinglish": "Haan toh, umm, basically main ye bol raha tha, matlab, hmm, aap samajh rahe ho na.",
}

LANGUAGES = {
    "auto": "Auto-detect",
    "en": "English",
    "it": "Italian",
    "hi": "Hindi (Devanagari)",
    "hinglish": "Hinglish (Hindi in Latin letters)",
}


def _setup_cuda_dlls() -> None:
    """Make the pip-installed CUDA libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12) findable."""
    roots = set(site.getsitepackages())
    for root in roots:
        nv = Path(root) / "nvidia"
        if nv.is_dir():
            for b in nv.glob("*/bin"):
                os.environ["PATH"] = str(b) + os.pathsep + os.environ["PATH"]
                try:
                    os.add_dll_directory(str(b))
                except (OSError, AttributeError):
                    pass


_setup_cuda_dlls()


def get_model(name: str, force_cpu: bool = False):
    from faster_whisper import WhisperModel

    with _model_lock:
        key = (name, force_cpu)
        if _model["key"] == key:
            return _model["obj"], _model["device"]
        _model.update(key=None, obj=None, device=None)
        if not force_cpu:
            try:
                obj = WhisperModel(name, device="cuda", compute_type="float16")
                _model.update(key=key, obj=obj, device="cuda")
                return obj, "cuda"
            except Exception as e:  # CUDA libs missing / unsupported GPU
                print(f"[autocut] GPU unavailable ({e}); using CPU", flush=True)
        obj = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=max(4, (os.cpu_count() or 8) // 2))
        _model.update(key=key, obj=obj, device="cpu")
        return obj, "cpu"


def _set(p: dict, **status) -> None:
    p["status"].update(status)
    store.save(p)


def _load_wav(path: Path):
    """Read the 16 kHz mono PCM wav that media.extract_wav wrote (avoids faster-whisper's PyAV decoder)."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _transcribe(p: dict, wav: Path, language: str, model_name: str, force_cpu: bool) -> None:
    model, device = get_model(model_name, force_cpu)
    p["device"] = device
    audio = _load_wav(wav)

    code, prompt_key = language, language
    if language == "auto":
        code, prob, _ = model.detect_language(audio[:16000 * 30])
        prompt_key = code if code in PROMPTS else None
        p["language"] = code
    elif language == "hinglish":
        code = "hi"
    p["language"] = "hi" if language == "hinglish" else code

    dur = len(audio) / 16000
    segments, _info = model.transcribe(
        audio, language=code, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 250},
        condition_on_previous_text=False, beam_size=5,
        initial_prompt=PROMPTS.get(prompt_key) if prompt_key else None,
    )
    words, last_save = [], 0.0
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"w": text, "s": round(w.start, 3), "e": round(w.end, 3)})
        p["status"]["progress"] = min(0.99, seg.end / dur) if dur else 0.0
        if time.time() - last_save > 1.0:
            p["status"]["message"] = f"{len(words)} words"
            store.save(p)
            last_save = time.time()
    # Whisper occasionally returns overlapping/zero-length words; make timings monotonic.
    prev_end = 0.0
    for w in words:
        w["s"] = max(w["s"], prev_end)
        w["e"] = max(w["e"], w["s"] + 0.02)
        prev_end = w["e"]
    p["words"] = words
    p["cuts"] = []


def process(pid: str, language: str, model_name: str) -> None:
    p = store.get(pid)
    d = store.project_dir(pid)
    try:
        _set(p, state="processing", step="Preparing audio", progress=0.0, message="")
        wav = d / "audio16k.wav"
        media.extract_wav(p["media"]["path"], wav)

        if not p["proxy"] and not (d / "proxy.mp4").exists() and not (d / "proxy.m4a").exists():
            threading.Thread(target=_build_proxy, args=(pid,), daemon=True).start()

        _set(p, step=f"Transcribing ({model_name})", progress=0.0)
        p["model"] = model_name
        try:
            _transcribe(p, wav, language, model_name, force_cpu=False)
        except RuntimeError as e:  # e.g. cublas/cudnn DLL missing at inference time
            if p.get("device") == "cuda":
                print(f"[autocut] GPU inference failed ({e}); retrying on CPU", flush=True)
                _set(p, step=f"Transcribing on CPU ({model_name})", progress=0.0)
                _transcribe(p, wav, language, model_name, force_cpu=True)
            else:
                raise
        _set(p, state="ready", step="", progress=1.0, message=f"{len(p['words'])} words")
    except Exception as e:
        _set(p, state="error", step="", message=str(e)[:500])


def _build_proxy(pid: str) -> None:
    p = store.get(pid)
    d = store.project_dir(pid)
    dst = d / ("proxy.mp4" if p["media"]["has_video"] else "proxy.m4a")
    ok = media.make_proxy(p["media"]["path"], dst, p["media"]["has_video"])
    p["proxy"] = bool(ok)
    store.save(p)


def submit(pid: str, language: str = "auto", model_name: str | None = None) -> None:
    p = store.get(pid)
    p["status"] = {"state": "processing", "step": "Queued", "progress": 0.0, "message": ""}
    store.save(p)
    _executor.submit(process, pid, language, model_name or DEFAULT_MODEL)
