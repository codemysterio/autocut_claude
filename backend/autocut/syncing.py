"""Find the time offset (and slow drift) between a main recording and an extra recording by matching audio.

Convention used everywhere: a moment at time `tm` in the main file is found at time
`tl = rate * tm + offset` in the layer file. `rate` is 1.0 unless the two devices' clocks drift apart.
"""
import subprocess

import numpy as np

from .media import _NO_WINDOW

ENV_RATE = 100          # coarse search works on a 100 Hz loudness envelope
FINE_SR = 16000         # fine search works on 16 kHz audio
WINDOW_S = 20.0         # length of each fine-search window
MARGIN_S = 1.5          # fine search looks +-1.5 s around the coarse offset
MIN_CONF_USED = 0.25    # windows below this are ignored when fitting
MIN_DRIFT_SECONDS = 0.003   # accumulated drift smaller than this is treated as zero


def _pcm(path: str, sr: int, start: float | None = None, dur: float | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.4f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.4f}"]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("Could not read audio: " + r.stderr.decode(errors="replace")[-200:])
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _window(path: str, t0: float, dur: float, sr: int = FINE_SR) -> np.ndarray:
    """Audio of [t0, t0+dur] at sr, zero-padded where the file does not cover the range."""
    pre = max(0.0, -t0)
    x = _pcm(path, sr, max(0.0, t0), dur - pre) if dur - pre > 0 else np.zeros(0, np.float32)
    out = np.zeros(int(round(dur * sr)), np.float32)
    i = int(round(pre * sr))
    n = min(len(x), len(out) - i)
    if n > 0:
        out[i:i + n] = x[:n]
    return out


def _envelope(x: np.ndarray, sr: int) -> np.ndarray:
    step = sr // ENV_RATE
    n = len(x) // step
    e = np.log1p(30 * np.abs(x[:n * step]).reshape(n, step).mean(axis=1))
    k = ENV_RATE * 2  # remove slow level changes (different mic gains) with a 2 s moving average
    if len(e) > k:
        e = e - np.convolve(e, np.ones(k) / k, mode="same")
    return e - e.mean()


def _xcorr(a: np.ndarray, b: np.ndarray):
    """c[k] = sum_n a[n+k] * b[n] for k in [-(len(b)-1), len(a)-1]; returns (c, k0) with c[i] <-> lag i-k0."""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    k0 = len(b) - 1
    return np.concatenate([c[-k0:], c[:len(a)]]) if k0 else c[:len(a)], k0


def _peak_stats(c: np.ndarray, i: int, guard: int) -> float:
    """Peak-to-sidelobe ratio: how far the peak stands out from the rest of the correlation."""
    mask = np.ones(len(c), bool)
    mask[max(0, i - guard):i + guard + 1] = False
    rest = c[mask]
    if len(rest) < 10:
        return 0.0
    sd = float(np.std(rest))
    return float((c[i] - np.mean(rest)) / sd) if sd > 0 else 0.0


def _conf(psr: float) -> float:
    return float(np.clip((psr - 5.0) / 10.0, 0.0, 1.0))


def _phat_lag(main_w: np.ndarray, layer_w: np.ndarray, max_lag: int, sr: int):
    """GCC-PHAT between a main window and a longer layer window; returns (lag in samples, psr).

    lag k means layer_w[n + k] matches main_w[n], searched over 0..max_lag (layer_w starts max_lag/2 early).
    """
    n = 1 << int(np.ceil(np.log2(len(layer_w) + len(main_w))))
    R = np.fft.rfft(layer_w, n) * np.conj(np.fft.rfft(main_w, n))
    f = np.fft.rfftfreq(n, 1 / sr)
    R = R / (np.abs(R) + 1e-12)                       # phase transform: keep timing, drop level/colour
    R[(f < 150) | (f > 4000)] = 0                     # speech band
    cc = np.fft.irfft(R, n)[:max_lag + 1]
    i = int(np.argmax(cc))
    frac = 0.0
    if 0 < i < len(cc) - 1:                           # parabolic interpolation -> sub-sample accuracy
        y0, y1, y2 = cc[i - 1], cc[i], cc[i + 1]
        d = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / d if d != 0 else 0.0
    return i + frac, _peak_stats(cc, i, int(0.02 * sr))


def estimate(main_path: str, layer_path: str, main_dur: float, layer_dur: float) -> dict:
    """Return {state, confidence, offset, rate, windows, drift_ppm, message}."""
    out = {"state": "failed", "confidence": 0.0, "offset": 0.0, "rate": 1.0, "windows": [], "drift_ppm": 0.0,
           "message": ""}
    # ---- coarse: match loudness envelopes of the whole files
    ea = _envelope(_pcm(layer_path, 8000), 8000)
    eb = _envelope(_pcm(main_path, 8000), 8000)
    if len(ea) < 200 or len(eb) < 200:
        out["message"] = "One of the files is too short or silent to match."
        return out
    c, k0 = _xcorr(ea, eb)
    i = int(np.argmax(c))
    coarse = (i - k0) / ENV_RATE
    coarse_conf = _conf(_peak_stats(c, i, ENV_RATE))
    if coarse_conf < 0.05:
        out["message"] = "No matching audio found - these recordings may not share any sound (e.g. music). Position it manually."
        return out

    # ---- fine: GCC-PHAT on windows spread over the overlapping part
    lo, hi = max(0.0, -coarse), min(main_dur, layer_dur - coarse)   # main-time range present in both files
    if hi - lo < 4.0:
        out["message"] = "The recordings barely overlap."
        return out
    W = min(WINDOW_S, (hi - lo) / 2.5)
    energy = np.convolve(eb ** 2 + 1e-9, np.ones(int(W * ENV_RATE)), mode="valid") if len(eb) > W * ENV_RATE else None
    # More windows for longer recordings: the spread (first to last) decides how precisely drift is measured.
    n_win = int(np.clip((hi - lo) // 150, 3, 9))
    wins = []
    for frac in np.linspace(0.02, 0.98, n_win):
        centre = lo + (hi - lo) * frac
        t0 = float(np.clip(centre - W / 2, lo, hi - W))
        if energy is not None:  # nudge the window to the loudest nearby stretch (speech, not silence)
            reach = 0.5 * (hi - lo) / n_win
            a = int(max(lo, t0 - reach) * ENV_RATE)
            b = int(min(hi - W, t0 + reach) * ENV_RATE)
            if b > a and b <= len(energy):
                t0 = (a + int(np.argmax(energy[a:b]))) / ENV_RATE
        wins.append(t0)
    wins = sorted(set(round(w, 1) for w in wins))

    pts = []
    for t0 in wins:
        main_w = _window(main_path, t0, W)
        layer_w = _window(layer_path, t0 + coarse - MARGIN_S, W + 2 * MARGIN_S)
        lag, psr = _phat_lag(main_w, layer_w, int(2 * MARGIN_S * FINE_SR), FINE_SR)
        off = float(coarse - MARGIN_S + lag / FINE_SR)
        pts.append({"t": float(t0 + W / 2), "offset": off, "confidence": _conf(psr)})
    out["windows"] = [{k: round(float(v), 4) for k, v in p.items()} for p in pts]
    good = [p for p in pts if p["confidence"] >= MIN_CONF_USED]
    if not good:
        out["message"] = "The audio did not match clearly. Try nudging by ear."
        out["confidence"] = float(round(max(p["confidence"] for p in pts), 3))
        out["offset"] = float(round(coarse, 4))
        return out

    t = np.array([p["t"] for p in good])
    o = np.array([p["offset"] for p in good])
    w = np.array([p["confidence"] for p in good])
    offset, slope = float(np.average(o, weights=w)), 0.0
    if len(good) >= 2 and t.max() - t.min() >= 30:
        slope, intercept = np.polyfit(t, o, 1, w=w)
        resid = float(np.max(np.abs(o - (slope * t + intercept))))
        if abs(slope) * (t.max() - t.min()) >= MIN_DRIFT_SECONDS and resid < 0.012:
            offset = float(intercept)
        else:
            slope = 0.0
            offset = float(np.median(o))
    out["offset"] = float(round(offset, 5))
    out["rate"] = float(round(1.0 + float(slope), 9))
    out["drift_ppm"] = float(round(float(slope) * 1e6, 2))
    out["confidence"] = float(round(float(np.median([p["confidence"] for p in good])), 3))
    out["state"] = "ok" if out["confidence"] >= 0.5 else "low"
    out["message"] = ("" if out["state"] == "ok" else "Low confidence - check the sync by ear and nudge if needed.")
    return out
