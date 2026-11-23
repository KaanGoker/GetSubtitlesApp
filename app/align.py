"""Move subtitle starts to where the speech begins.

Whisper tends to place the first word of a phrase a little early, inside the silence before it.
Each word start that falls in a quiet part is moved forward to the first moment the sound rises
above the quiet level: at most MAX_SHIFT_S later and never past the end of the word. A start
that already sits in speech stays where it is.
"""
import wave
from pathlib import Path
from typing import List, Optional

import numpy as np

FRAME_S = 0.01        # one loudness value per 10 ms
MAX_SHIFT_S = 0.5     # a start moves at most this much
RISE = 0.08           # speech level: floor + RISE * (peak - floor)
HOLD_FRAMES = 3       # the rise must last 30 ms, so a click does not count
MIN_WORD_S = 0.06     # a moved start keeps at least this much of the word
LEAD_S = 0.03         # a moved start sits this much before the rise, so soft first sounds stay in


def envelope(wav_path: Path, frame_s: float = FRAME_S) -> np.ndarray:
    """The loudness (RMS) of a 16-bit wav, one value per frame. Read in chunks, so long files fit."""
    with wave.open(str(wav_path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit wav is supported")
        channels, rate = w.getnchannels(), w.getframerate()
        frame = max(1, int(round(rate * frame_s)))
        chunk = frame * 1000
        out, rest = [], np.zeros(0, dtype=np.float32)
        while True:
            raw = w.readframes(chunk)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            samples = np.concatenate([rest, samples])
            n = len(samples) // frame
            if n:
                blocks = samples[:n * frame].reshape(n, frame)
                out.append(np.sqrt((blocks ** 2).mean(axis=1)))
            rest = samples[n * frame:]
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


def speech_level(env: np.ndarray) -> Optional[float]:
    """The loudness above which a frame counts as speech; None when the file has no clear quiet part."""
    if len(env) < HOLD_FRAMES:
        return None
    floor, peak = np.percentile(env, 10), np.percentile(env, 99)
    if peak <= 0 or peak - floor < 0.002 or floor > 0.5 * peak:
        return None
    return float(floor + RISE * (peak - floor))


def _loud(env: np.ndarray, level: float) -> np.ndarray:
    """True where this frame and the next HOLD_FRAMES - 1 are all above the level."""
    above = env >= level
    held = above.copy()
    for k in range(1, HOLD_FRAMES):
        held[:-k] &= above[k:]
        held[-k:] = False
    return held


def _snap(start: float, end: float, loud: np.ndarray, frame_s: float) -> float:
    i = int(round(start / frame_s))
    if i >= len(loud) or loud[i]:
        return start
    last = min(int((start + MAX_SHIFT_S) / frame_s), int((end - MIN_WORD_S) / frame_s), len(loud) - 1)
    hits = np.flatnonzero(loud[i:last + 1])
    return max(start, (i + hits[0]) * frame_s - LEAD_S) if len(hits) else start


def snap_starts(segments: List[dict], env: np.ndarray, frame_s: float = FRAME_S) -> List[dict]:
    """The segments with every word start, and each segment start, moved to where speech begins."""
    level = speech_level(env)
    if level is None:
        return segments
    loud = _loud(env, level)
    out = []
    for seg in segments:
        words = [dict(w, start=_snap(w["start"], w["end"], loud, frame_s)) for w in seg["words"]]
        start = seg["start"]
        if words:
            start = max(start, min(words[0]["start"], words[0]["end"]))
        else:
            start = _snap(start, seg["end"], loud, frame_s)
        out.append(dict(seg, start=start, words=words))
    return out


def snap_to_speech(segments: List[dict], wav_path: Path) -> List[dict]:
    """snap_starts on a wav file; the segments stay as they are when the file cannot be read."""
    try:
        return snap_starts(segments, envelope(wav_path))
    except Exception as e:  # the times are still usable without this step
        print(f"[GETSUBTITLES] Start alignment skipped: {e}")
        return segments
