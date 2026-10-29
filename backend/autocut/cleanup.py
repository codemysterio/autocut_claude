"""Detect filler words, silences, repeated words and repeated takes."""
import re
from difflib import SequenceMatcher

# "hard" fillers are almost never real words; "soft" ones are legitimate words that are
# often used as verbal padding, so they are opt-in.
FILLERS = {
    "en": {
        "hard": ["um", "umm", "uhm", "uh", "uhh", "er", "erm", "ah", "hmm", "hm", "mm", "mmm", "mhm"],
        "soft": ["like", "you know", "i mean", "basically", "literally", "kind of", "sort of", "actually"],
    },
    "it": {
        "hard": ["ehm", "eh", "ehh", "uhm", "uh", "mmm", "mm", "ah", "hmm", "eeh", "ee"],
        "soft": ["cioè", "diciamo", "praticamente", "insomma", "tipo", "ecco", "come dire", "per così dire"],
    },
    "hi": {
        "hard": ["umm", "um", "uh", "hmm", "aa", "aaa", "ah", "हम्म", "उम्म", "अं", "आ"],
        "soft": ["matlab", "मतलब", "yaani", "yani", "यानी", "basically", "you know", "like", "achha", "अच्छा"],
    },
}


def norm(w: str) -> str:
    return re.sub(r"[^\w']+", "", w.lower(), flags=re.UNICODE)


def _langs(language: str | None) -> list[str]:
    if language in FILLERS:
        return [language]
    return list(FILLERS)  # unknown / auto: use every list


def find_fillers(words: list[dict], language: str | None, soft: bool = False) -> list[tuple[int, int, str]]:
    toks = [norm(w["w"]) for w in words]
    phrases: dict[str, str] = {}
    for lang in _langs(language):
        for f in FILLERS[lang]["hard"]:
            phrases[f] = "Filler word"
        if soft:
            for f in FILLERS[lang]["soft"]:
                phrases.setdefault(f, "Verbal padding")
    split = {tuple(p.split()): r for p, r in phrases.items()}
    longest = max(len(k) for k in split)
    out, i, n = [], 0, len(toks)
    while i < n:
        for L in range(min(longest, n - i), 0, -1):
            r = split.get(tuple(toks[i:i + L]))
            if r:
                out.append((i, i + L - 1, f'{r}: "{" ".join(words[j]["w"].strip(",.…") for j in range(i, i + L))}"'))
                i += L
                break
        else:
            i += 1
    return out


def find_silences(words: list[dict], duration: float, min_gap: float = 0.8, keep: float = 0.3
                  ) -> list[tuple[float, float, int, float]]:
    """Return (start, end, gap_after_word_index, gap_length) for pauses longer than min_gap.

    `keep` seconds of the pause are left in, split between both sides.
    """
    if not words:
        return []
    out = []
    half = keep / 2
    bounds = [(-1, 0.0, words[0]["s"])]
    bounds += [(i, words[i]["e"], words[i + 1]["s"]) for i in range(len(words) - 1)]
    bounds.append((len(words) - 1, words[-1]["e"], duration))
    for idx, a, b in bounds:
        if b - a >= min_gap and b - a > keep:
            lead = idx == -1
            trail = idx == len(words) - 1
            start = a if lead else a + half
            end = b if trail else b - half
            if lead:
                end = b - half
            if end - start > 0.05:
                out.append((start, end, idx, b - a))
    return out


def find_repeated_words(words: list[dict], max_n: int = 4) -> list[tuple[int, int, str]]:
    """'I I think', 'we should we should go' -> cut the earlier copies, keep the last one."""
    toks = [norm(w["w"]) for w in words]
    ends_sentence = [bool(re.search(r"[.?!।…]$", w["w"].strip())) for w in words]
    out, i, n = [], 0, len(toks)
    while i < n:
        hit = False
        for L in range(min(max_n, (n - i) // 2), 0, -1):
            chunk = toks[i:i + L]
            if not all(chunk) or any(ends_sentence[i:i + L - 1]):
                continue
            copies = 1
            while i + (copies + 1) * L <= n and toks[i + copies * L:i + (copies + 1) * L] == chunk:
                copies += 1
            if copies > 1 and not ends_sentence[i + L - 1]:
                last = i + (copies - 1) * L - 1
                text = " ".join(w["w"].strip(",.…") for w in words[i + (copies - 1) * L:i + copies * L])
                out.append((i, last, f'Repeated "{text}"' + (f" ×{copies}" if copies > 2 else "")))
                i += copies * L
                hit = True
                break
        if not hit:
            i += 1
    return out


def _sentences(words: list[dict], gap: float = 0.7) -> list[tuple[int, int]]:
    out, start = [], 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        end = last or bool(re.search(r"[.?!।…]$", w["w"].strip())) or words[i + 1]["s"] - w["e"] > gap
        if end:
            out.append((start, i))
            start = i + 1
    return out


def find_repeated_takes(words: list[dict], lookahead: int = 6, max_gap: float = 90.0
                        ) -> list[tuple[int, int, str]]:
    """Earlier attempts at a sentence that is said again soon after; keeps the last attempt.

    Also catches false starts: a short fragment that is the beginning of the next sentence.
    """
    sents = _sentences(words)
    toks = [[t for t in (norm(w["w"]) for w in words[a:b + 1]) if t] for a, b in sents]
    out: list[tuple[int, int, str]] = []
    for si, (a, b) in enumerate(sents):
        ta = toks[si]
        if len(ta) < 2:
            continue
        for sj in range(si + 1, min(si + 1 + lookahead, len(sents))):
            ja, jb = sents[sj]
            if words[ja]["s"] - words[b]["e"] > max_gap:
                break
            tb = toks[sj]
            if len(tb) < 2:
                continue
            full = SequenceMatcher(None, ta, tb).ratio()
            thr = 0.85 if min(len(ta), len(tb)) < 5 else 0.78
            if full >= thr and len(ta) >= 3:
                out.append((a, b, f'Repeated take (said again later): "{" ".join(w["w"] for w in words[a:min(b, a + 5) + 1])}…"'))
                break
            incomplete = not re.search(r"[.?!।…]$", words[b]["w"].strip())
            if (incomplete and len(ta) < len(tb)
                    and SequenceMatcher(None, ta, tb[:len(ta)]).ratio() >= 0.85):
                out.append((a, b, f'False start: "{" ".join(w["w"] for w in words[a:b + 1])}"'))
                break
    return out
