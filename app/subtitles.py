"""Subtitle building and writing. Pure functions, no model and no files.

Words and segments are plain dicts:
    word    = {"text": " Hello", "start": 0.0, "end": 0.4}   (text as Whisper returns it)
    segment = {"text": " Hello there.", "start": 0.0, "end": 1.5, "words": [word, ...]}
"""
import textwrap
from dataclasses import dataclass
from typing import List


@dataclass
class Cue:
    start: float
    end: float
    text: str


# ---------- writers ----------

def fmt_timestamp(seconds: float, ms_sep: str = ",") -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    h, rest = divmod(total_ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{ms_sep}{ms:03d}"


def _clean(cues: List[Cue]) -> List[Cue]:
    """Drop empty cues, stop each cue where the next one starts, keep end after start."""
    kept = [c for c in cues if c.text.strip()]
    out = []
    for i, c in enumerate(kept):
        end = c.end
        if i + 1 < len(kept) and kept[i + 1].start > c.start:
            end = min(end, kept[i + 1].start)
        if end <= c.start:
            end = c.start + 0.001
        out.append(Cue(c.start, end, c.text.strip()))
    return out


def to_srt(cues: List[Cue]) -> str:
    return "".join(
        f"{i}\n{fmt_timestamp(c.start)} --> {fmt_timestamp(c.end)}\n{c.text}\n\n"
        for i, c in enumerate(_clean(cues), start=1)
    )


def to_vtt(cues: List[Cue]) -> str:
    return "WEBVTT\n\n" + "".join(
        f"{fmt_timestamp(c.start, '.')} --> {fmt_timestamp(c.end, '.')}\n{c.text}\n\n"
        for c in _clean(cues)
    )


def to_txt(cues: List[Cue]) -> str:
    return "".join(c.text.replace("\n", " ") + "\n" for c in _clean(cues))


# ---------- vertical style (reels, shorts): short single lines ----------

VERT_MAX_CHARS_PER_LINE = 38
VERT_MAX_WORDS_PER_BLOCK = 10
VERT_MAX_DURATION_S = 2.2
VERT_MIN_DURATION_S = 0.7
PUNCT_BREAK = {".", ",", "!", "?", "\u2026", ":", ";", "\u2014", "\u2013"}
SENTENCE_END = {".", "!", "?", "…", "。", "！", "？"}


def _last_char(text: str) -> str:
    return text.strip()[-1:]


def build_vertical_cues(words: List[dict]) -> List[Cue]:
    """Pack words into short single-line cues."""
    words = [w for w in words if w["text"].strip()]
    cues = []
    i, n = 0, len(words)

    while i < n:
        line = ""
        start = words[i]["start"]
        end = words[i]["end"]
        count = 0
        j = i

        while j < n:
            candidate = (line + words[j]["text"]).strip()
            if count > 0 and len(candidate) > VERT_MAX_CHARS_PER_LINE:
                break
            if count >= VERT_MAX_WORDS_PER_BLOCK:
                break
            line = line + words[j]["text"]
            end = words[j]["end"]
            count += 1
            j += 1
            if end - start >= VERT_MAX_DURATION_S:
                break
            if (len(line.strip()) >= VERT_MAX_CHARS_PER_LINE - 3
                    and _last_char(line) in PUNCT_BREAK):
                break

        # A block that would only flash on screen takes more words, up to the line limit.
        while j < n and end - start < VERT_MIN_DURATION_S:
            if _last_char(line) in SENTENCE_END:
                break
            if len((line + words[j]["text"]).strip()) > VERT_MAX_CHARS_PER_LINE:
                break
            line = line + words[j]["text"]
            end = words[j]["end"]
            j += 1

        cues.append(Cue(start, end, line.strip()))
        i = j

    return cues


# ---------- default style: at most two lines of 42 characters ----------

MAX_LINE_CHARS = 42
MAX_CUE_DURATION_S = 7.0
SENTENCE_END_BONUS = 0.6
PUNCT_BONUS = 0.3
PAUSE_S = 0.5
PAUSE_BONUS = 0.3


def _split_in_two(text: str):
    """Best split into two lines of at most MAX_LINE_CHARS, or None."""
    if " " in text:
        options = [(text[:k], text[k + 1:]) for k, ch in enumerate(text) if ch == " "]
    else:
        mid = (len(text) + 1) // 2
        options = [(text[:mid], text[mid:])]
    options = [(a, b) for a, b in options if len(a) <= MAX_LINE_CHARS and len(b) <= MAX_LINE_CHARS]
    if not options:
        return None
    return min(options, key=lambda ab: abs(len(ab[0]) - len(ab[1])))


def _fits(text: str) -> bool:
    return len(text) <= MAX_LINE_CHARS or _split_in_two(text) is not None


def wrap_two_lines(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_LINE_CHARS:
        return text
    two = _split_in_two(text)
    if two:
        return "\n".join(two)
    return "\n".join(textwrap.wrap(text, MAX_LINE_CHARS))


def _text_of(words: List[dict]) -> str:
    return "".join(w["text"] for w in words).strip()


def _readable(words: List[dict]) -> bool:
    return _fits(_text_of(words)) and words[-1]["end"] - words[0]["start"] <= MAX_CUE_DURATION_S


def _best_cut(words: List[dict]) -> int:
    """Where to cut a run of words in two: near the middle, but rather at the end of a sentence,
    at a comma, or at a pause in the speech."""
    total = len(_text_of(words))
    best, best_score = 1, None
    for i in range(1, len(words)):
        left = len(_text_of(words[:i]))
        score = abs(2 * left - total) / max(total, 1)     # 0 in the middle, 1 at the edge
        last = _last_char(words[i - 1]["text"])
        if last in SENTENCE_END:
            score -= SENTENCE_END_BONUS
        elif last in PUNCT_BREAK:
            score -= PUNCT_BONUS
        if words[i]["start"] - words[i - 1]["end"] >= PAUSE_S:
            score -= PAUSE_BONUS
        if best_score is None or score < best_score:
            best, best_score = i, score
    return best


def _split_segment(words: List[dict]) -> List[Cue]:
    if len(words) == 1 or _readable(words):
        return [Cue(words[0]["start"], words[-1]["end"], wrap_two_lines(_text_of(words)))]
    cut = _best_cut(words)
    return _split_segment(words[:cut]) + _split_segment(words[cut:])


def build_default_cues(segments: List[dict]) -> List[Cue]:
    """One cue per Whisper segment; a segment too long to read is split on its word times."""
    cues = []
    for seg in segments:
        text = " ".join(seg["text"].split())
        if not text:
            continue
        words = [w for w in seg.get("words") or [] if w["text"].strip()]
        readable = _fits(text) and seg["end"] - seg["start"] <= MAX_CUE_DURATION_S
        if readable or not words:
            cues.append(Cue(seg["start"], seg["end"], wrap_two_lines(text)))
        else:
            cues.extend(_split_segment(words))
    return cues
