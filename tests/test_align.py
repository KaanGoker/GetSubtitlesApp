import wave

import numpy as np
import pytest

from app.align import LEAD_S, MAX_SHIFT_S, envelope, snap_starts, snap_to_speech

RATE = 16000


def write_wav(path, parts):
    """parts: (seconds, amplitude) pieces of a 300 Hz tone; amplitude 0 is near silence."""
    chunks = []
    for seconds, amp in parts:
        t = np.arange(int(seconds * RATE)) / RATE
        chunks.append(amp * np.sin(2 * np.pi * 300 * t) + 0.001 * np.random.default_rng(0).standard_normal(len(t)))
    samples = (np.clip(np.concatenate(chunks), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return path


def seg(start, end, words):
    return {"text": " ".join(t for t, _, _ in words), "start": start, "end": end,
            "words": [{"text": " " + t, "start": s, "end": e} for t, s, e in words]}


@pytest.fixture
def speech_at_one_second(tmp_path):
    # 1 s quiet, 1.5 s speech, 1 s quiet, 1 s speech
    return write_wav(tmp_path / "a.wav", [(1.0, 0), (1.5, 0.5), (1.0, 0), (1.0, 0.5)])


def test_envelope_has_one_value_per_10_ms(speech_at_one_second):
    env = envelope(speech_at_one_second)
    assert len(env) == 450
    assert env[:90].max() < 0.01 < env[110:240].min()


def test_an_early_start_moves_to_where_speech_begins(speech_at_one_second):
    env = envelope(speech_at_one_second)
    out = snap_starts([seg(0.7, 2.5, [("Hello", 0.7, 1.4), ("there", 1.5, 2.5)])], env)
    assert out[0]["words"][0]["start"] == pytest.approx(1.0 - LEAD_S, abs=0.02)
    assert out[0]["start"] == out[0]["words"][0]["start"]


def test_a_start_inside_speech_stays(speech_at_one_second):
    env = envelope(speech_at_one_second)
    out = snap_starts([seg(0.7, 2.5, [("Hello", 0.7, 1.4), ("there", 1.5, 2.5)])], env)
    assert out[0]["words"][1]["start"] == 1.5


def test_a_start_moves_at_most_the_limit(speech_at_one_second):
    env = envelope(speech_at_one_second)
    out = snap_starts([seg(2.6, 4.5, [("Again", 2.6, 4.5)])], env)   # speech is 0.9 s later
    assert out[0]["words"][0]["start"] == 2.6
    out = snap_starts([seg(3.2, 4.5, [("Again", 3.2, 4.5)])], env)
    assert out[0]["words"][0]["start"] == pytest.approx(3.5 - LEAD_S, abs=0.02)
    assert 3.5 - 3.2 <= MAX_SHIFT_S


def test_a_start_never_passes_the_end_of_its_word(speech_at_one_second):
    env = envelope(speech_at_one_second)
    out = snap_starts([seg(0.7, 0.9, [("Hm", 0.7, 0.9)])], env)    # the word ends before speech
    assert out[0]["words"][0]["start"] == 0.7


def test_starts_never_move_earlier_and_text_and_ends_stay(speech_at_one_second):
    env = envelope(speech_at_one_second)
    before = [seg(0.7, 2.5, [("Hello", 0.7, 1.4), ("there", 1.5, 2.5)]),
              seg(3.3, 4.5, [("Again", 3.3, 4.5)])]
    after = snap_starts(before, env)
    for a, b in zip(before, after):
        assert b["text"] == a["text"] and b["end"] == a["end"] and b["start"] >= a["start"]
        for wa, wb in zip(a["words"], b["words"]):
            assert wb["text"] == wa["text"] and wb["end"] == wa["end"]
            assert wa["start"] <= wb["start"] < wb["end"]


def test_a_segment_without_words_is_snapped_on_its_own(speech_at_one_second):
    env = envelope(speech_at_one_second)
    out = snap_starts([{"text": "Hi", "start": 0.8, "end": 2.0, "words": []}], env)
    assert out[0]["start"] == pytest.approx(1.0 - LEAD_S, abs=0.02)


def test_sound_without_a_quiet_part_is_left_alone(tmp_path):
    env = envelope(write_wav(tmp_path / "b.wav", [(3.0, 0.5)]))
    before = [seg(0.5, 2.0, [("Music", 0.5, 2.0)])]
    assert snap_starts(before, env) == before


def test_an_unreadable_file_keeps_the_times(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    before = [seg(0.7, 2.5, [("Hello", 0.7, 2.5)])]
    assert snap_to_speech(before, bad) == before
