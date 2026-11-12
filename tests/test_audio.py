import wave

import pytest

from app import audio
from app.audio import AudioError, extract_wav, parse_audio_tracks, probe_audio_tracks

FFMPEG_INFO = """\
Input #0, matroska,webm, from 'film.mkv':
  Metadata:
    ENCODER         : Lavf60.16.100
  Duration: 00:42:10.52, start: 0.000000, bitrate: 2203 kb/s
  Stream #0:0: Video: h264 (High), yuv420p(progressive), 1920x1080, 23.98 fps (default)
  Stream #0:1(jpn): Audio: aac (LC), 48000 Hz, stereo, fltp (default)
    Metadata:
      title           : Original
  Stream #0:2(rus): Audio: ac3, 48000 Hz, 5.1(side), fltp, 384 kb/s
  Stream #0:3(eng): Subtitle: subrip
At least one output file must be specified
"""


def test_parse_lists_only_audio_streams_in_order():
    tracks = parse_audio_tracks(FFMPEG_INFO)
    assert [t["index"] for t in tracks] == [0, 1]
    assert [t["language"] for t in tracks] == ["jpn", "rus"]


def test_parse_reads_title_codec_and_default_flag():
    first, second = parse_audio_tracks(FFMPEG_INFO)
    assert first["title"] == "Original" and first["default"] is True
    assert second["title"] is None and second["default"] is False
    assert second["codec"] == "ac3"


def test_parse_handles_stream_without_language_tag():
    info = "  Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 44100 Hz, mono, s16, 705 kb/s\n"
    assert parse_audio_tracks(info)[0]["language"] is None


def test_probe_finds_both_tracks_of_a_real_file(media):
    tracks = probe_audio_tracks(media["two"])
    assert [(t["index"], t["language"]) for t in tracks] == [(0, "jpn"), (1, "rus")]
    assert tracks[1]["title"] == "Dub"


def test_probe_reports_a_file_without_audio(media):
    assert probe_audio_tracks(media["silent"]) == []


def test_probe_rejects_a_file_that_is_not_media(media):
    with pytest.raises(AudioError):
        probe_audio_tracks(media["broken"])


def _wav_info(path):
    with wave.open(str(path)) as f:
        return f.getframerate(), f.getnchannels(), f.getnframes() / f.getframerate()


def test_extract_writes_16k_mono_wav(media, tmp_path):
    out = tmp_path / "a.wav"
    extract_wav(media["one"], out)
    rate, channels, seconds = _wav_info(out)
    assert (rate, channels) == (16000, 1)
    assert seconds == pytest.approx(2.0, abs=0.1)


def test_extract_stops_at_the_length_limit_whatever_the_file_claims(media, tmp_path):
    out = tmp_path / "short.wav"
    extract_wav(media["one"], out, max_seconds=0.5)
    assert _wav_info(out)[2] == pytest.approx(0.5, abs=0.05)


def test_extract_takes_the_chosen_track(media, tmp_path):
    out = tmp_path / "b.wav"
    extract_wav(media["two"], out, track=1)
    assert _wav_info(out)[2] == pytest.approx(3.0, abs=0.15)


def test_extract_error_names_the_problem_not_the_ffmpeg_command(media, tmp_path):
    with pytest.raises(AudioError) as err:
        extract_wav(media["broken"], tmp_path / "c.wav")
    assert "ffmpeg" not in str(err.value).split(":")[0].lower()
    assert "-ar" not in str(err.value)


def test_faster_whisper_can_read_audio_with_installed_pyav(media):
    """faster-whisper 1.2.1 passes metadata_errors to av.open, which newer PyAV removed."""
    from faster_whisper import decode_audio
    samples = decode_audio(str(media["one"]))
    assert len(samples) > 16000


def test_parse_duration_reads_the_length_in_seconds():
    from app.audio import parse_duration
    assert parse_duration(FFMPEG_INFO) == pytest.approx(42 * 60 + 10.52)


def test_parse_duration_is_none_when_ffmpeg_does_not_know_it():
    from app.audio import parse_duration
    assert parse_duration("Input #0, wav, from 'x':\n  Duration: N/A, bitrate: N/A\n") is None
    assert parse_duration("") is None


def test_probe_gives_tracks_and_length(media):
    from app.audio import probe
    tracks, seconds = probe(media["two"])
    assert len(tracks) == 2
    assert seconds == pytest.approx(3.0, abs=0.1)


def test_input_formats():
    assert audio.input_formats("Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'a.mp4':") == \
        ["mov", "mp4", "m4a", "3gp", "3g2", "mj2"]
    assert audio.input_formats("nothing") == []


def test_ffmpeg_reads_only_the_file(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0
        stderr = b"Input #0, wav, from 'a.wav':\n  Duration: 00:00:01.00\n  Stream #0:0: Audio: pcm_s16le"

    monkeypatch.setattr(audio.subprocess, "run", lambda args, **kw: calls.append(args) or Done())
    audio.probe(tmp_path / "a.wav")
    audio.extract_wav(tmp_path / "a.wav", tmp_path / "b.wav")
    for args in calls:
        i = args.index("-i")
        assert args[i - 2:i] == ["-protocol_whitelist", "file"]


@pytest.mark.parametrize("fmt", ["hls", "concat", "dash"])
def test_playlists_are_refused(monkeypatch, tmp_path, fmt):
    class Done:
        returncode = 0
        stderr = f"Input #0, {fmt}, from 'a.m3u8':\n  Duration: 00:00:10.00\n  Stream #0:0: Audio: aac".encode()

    monkeypatch.setattr(audio.subprocess, "run", lambda args, **kw: Done())
    with pytest.raises(audio.AudioError):
        audio.probe(tmp_path / "a.m3u8")
