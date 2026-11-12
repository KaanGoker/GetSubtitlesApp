import re

import pytest

from app.subtitles import (
    Cue,
    build_default_cues,
    build_vertical_cues,
    fmt_timestamp,
    to_srt,
    to_txt,
    to_vtt,
    wrap_two_lines,
)


def w(text, start, end):
    return {"text": text, "start": start, "end": end}


def seg(text, start, end, words=None):
    return {"text": text, "start": start, "end": end, "words": words or []}


def spoken(sentence, start=0.0, per_word=0.4):
    """Words the way Whisper returns them: a leading space on each word."""
    out, t = [], start
    for token in sentence.split():
        out.append(w(" " + token, t, t + per_word))
        t += per_word
    return out


# ---------- timestamps ----------

@pytest.mark.parametrize("seconds,expected", [
    (0, "00:00:00,000"),
    (1.5, "00:00:01,500"),
    (1.9996, "00:00:02,000"),
    (59.9997, "00:01:00,000"),
    (3599.9996, "01:00:00,000"),
    (3661.25, "01:01:01,250"),
    (-0.2, "00:00:00,000"),
])
def test_timestamp_never_has_four_digit_milliseconds(seconds, expected):
    assert fmt_timestamp(seconds) == expected


def test_vtt_timestamp_uses_a_dot():
    assert fmt_timestamp(1.5, ms_sep=".") == "00:00:01.500"


# ---------- writers ----------

def test_srt_blocks_are_numbered_and_separated_by_a_blank_line():
    srt = to_srt([Cue(0.0, 1.0, "Hello"), Cue(1.0, 2.5, "World")])
    assert srt == (
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\nWorld\n\n"
    )


def test_srt_skips_empty_cues_and_keeps_numbering_continuous():
    srt = to_srt([Cue(0, 1, "A"), Cue(1, 2, "   "), Cue(2, 3, "B")])
    assert re.findall(r"^\d+$", srt, flags=re.M) == ["1", "2"]


def test_srt_cues_never_overlap_the_next_one():
    srt = to_srt([Cue(0.0, 2.0, "A"), Cue(1.5, 3.0, "B")])
    assert "00:00:00,000 --> 00:00:01,500" in srt


def test_srt_cue_always_ends_after_it_starts():
    srt = to_srt([Cue(1.0, 1.0, "A")])
    start, end = re.search(r"(\S+) --> (\S+)", srt).groups()
    assert end > start


def test_vtt_has_header_and_dot_timestamps():
    vtt = to_vtt([Cue(0.0, 1.0, "Hello")])
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.000\nHello\n" in vtt


def test_txt_is_one_line_per_cue_without_line_breaks_inside():
    assert to_txt([Cue(0, 1, "Hello\nthere"), Cue(1, 2, "World")]) == "Hello there\nWorld\n"


# ---------- vertical style ----------

def test_vertical_joins_spaced_words_with_single_spaces():
    cues = build_vertical_cues(spoken("Hello big world."))
    assert [c.text for c in cues] == ["Hello big world."]


def test_vertical_does_not_add_spaces_between_japanese_tokens():
    words = [w("こん", 0.0, 0.3), w("にちは", 0.3, 0.6), w("世界", 0.6, 0.9)]
    cues = build_vertical_cues(words)
    assert [c.text for c in cues] == ["こんにちは世界"]


def test_vertical_lines_stay_within_38_characters():
    cues = build_vertical_cues(spoken("word " * 60, per_word=0.05))
    assert cues and all(len(c.text) <= 38 for c in cues)


def test_vertical_blocks_hold_at_most_10_words():
    cues = build_vertical_cues(spoken("a " * 45, per_word=0.1))
    assert all(len(c.text.split()) <= 10 for c in cues)


def test_vertical_breaks_once_a_block_lasts_2_2_seconds():
    cues = build_vertical_cues(spoken("one two three four five six", per_word=1.2))
    assert [c.text for c in cues] == ["one two", "three four", "five six"]


def test_vertical_keeps_every_word_in_order():
    sentence = "The quick brown fox jumps over the lazy dog, again and again and again."
    cues = build_vertical_cues(spoken(sentence))
    assert " ".join(c.text for c in cues) == sentence


def test_vertical_single_overlong_word_gets_its_own_cue():
    long_word = "x" * 50
    cues = build_vertical_cues([w(" hi", 0, 1), w(" " + long_word, 1, 4), w(" bye", 4, 5)])
    assert long_word in [c.text for c in cues]


def test_vertical_empty_input_gives_no_cues():
    assert build_vertical_cues([]) == []


def test_vertical_cue_times_come_from_first_and_last_word():
    cues = build_vertical_cues([w(" a", 1.0, 1.2), w(" b", 1.2, 1.9)])
    assert (cues[0].start, cues[0].end) == (1.0, 1.9)


# ---------- default style ----------

def test_wrap_keeps_short_text_on_one_line():
    assert wrap_two_lines("Short line.") == "Short line."


def test_wrap_splits_long_text_into_two_balanced_lines_of_at_most_42():
    text = "This sentence is clearly longer than forty-two characters in total."
    lines = wrap_two_lines(text).split("\n")
    assert len(lines) == 2
    assert all(len(line) <= 42 for line in lines)
    assert " ".join(lines) == text
    assert abs(len(lines[0]) - len(lines[1])) <= 12


def test_wrap_breaks_text_without_spaces():
    lines = wrap_two_lines("あ" * 60).split("\n")
    assert len(lines) == 2 and all(len(line) <= 42 for line in lines)


def test_default_short_segment_stays_one_cue():
    s = seg(" Hello there.", 0.0, 1.5, spoken("Hello there."))
    cues = build_default_cues([s])
    assert cues == [Cue(0.0, 1.5, "Hello there.")]


def test_default_long_segment_is_split_into_cues_of_at_most_two_lines():
    sentence = ("This is a very long segment that Whisper sometimes returns as one block "
                "even though nobody can read that much text on a screen in one go, "
                "so it has to be cut into smaller pieces.")
    words = spoken(sentence, per_word=0.3)
    s = seg(" " + sentence, 0.0, words[-1]["end"], words)
    cues = build_default_cues([s])
    assert len(cues) > 1
    for c in cues:
        lines = c.text.split("\n")
        assert len(lines) <= 2 and all(len(line) <= 42 for line in lines)
    assert " ".join(c.text.replace("\n", " ") for c in cues) == sentence


def test_default_slow_segment_is_split_by_duration():
    words = spoken("one two three four five six", per_word=3.0)
    s = seg(" one two three four five six", 0.0, 18.0, words)
    cues = build_default_cues([s])
    assert len(cues) > 1
    assert all(c.end - c.start <= 7.0 for c in cues)


def test_default_long_segment_without_word_times_is_kept_and_wrapped():
    text = "word " * 30
    cues = build_default_cues([seg(text, 0.0, 5.0)])
    assert len(cues) == 1
    assert cues[0].text.replace("\n", " ") == text.strip()


def test_default_skips_empty_segments():
    assert build_default_cues([seg("   ", 0, 1)]) == []


def test_default_split_does_not_leave_a_few_words_alone_at_the_end():
    sentence = "And so my fellow Americans, ask not what your country can do for you."
    words = spoken(sentence, per_word=0.53)
    cues = build_default_cues([seg(" " + sentence, 0.0, words[-1]["end"], words)])
    assert len(cues) == 2
    assert all(len(c.text.split()) >= 4 for c in cues)
    assert cues[0].text.replace("\n", " ") == "And so my fellow Americans,"


def test_default_split_prefers_the_end_of_a_sentence_over_an_even_cut():
    text = ("And so my fellow Americans, ask not what your country can do for you. "
            "Ask what you can do for your country.")
    words = spoken(text, per_word=0.47)
    cues = build_default_cues([seg(" " + text, 0.0, words[-1]["end"], words)])
    assert [c.text.replace("\n", " ") for c in cues] == [
        "And so my fellow Americans, ask not what your country can do for you.",
        "Ask what you can do for your country.",
    ]


def test_default_split_prefers_a_pause_between_words():
    first = spoken("one two three four five six seven eight nine", per_word=0.5)
    second = spoken("ten eleven twelve thirteen fourteen fifteen sixteen", start=6.5, per_word=0.5)
    words = first + second
    text = " ".join(w["text"].strip() for w in words)
    cues = build_default_cues([seg(" " + text, 0.0, words[-1]["end"], words)])
    assert cues[0].text.replace("\n", " ").endswith("nine")
    assert cues[1].start == 6.5
