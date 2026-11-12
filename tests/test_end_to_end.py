"""The whole pipeline with a real (tiny) Whisper model on CPU. Run with: pytest -m slow"""
import re
import subprocess
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app.engine import Engine
from app.main import create_app
from tests.conftest import FFMPEG

pytestmark = pytest.mark.slow

SAMPLE_URL = "https://github.com/SYSTRAN/faster-whisper/raw/master/tests/data/jfk.flac"
TIMING = re.compile(r"^(\d\d:\d\d:\d\d,\d\d\d) --> (\d\d:\d\d:\d\d,\d\d\d)$", re.M)


@pytest.fixture(scope="session")
def speech(request):
    """11 seconds of English speech (a public domain recording of John F. Kennedy)."""
    path = request.config.cache.mkdir("media") / "jfk.flac"
    if not path.exists():
        try:
            urllib.request.urlretrieve(SAMPLE_URL, path)
        except OSError as e:
            pytest.skip(f"speech sample could not be downloaded: {e}")
    return path


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    from faster_whisper import WhisperModel
    engine = Engine(model_factory=lambda model_id, device, compute: WhisperModel(
        "tiny", device="cpu", compute_type="int8"), detect_device=lambda: "cpu",
        download=lambda size, on_progress: size)
    return TestClient(create_app(engine=engine, data_dir=tmp_path_factory.mktemp("data")), base_url="http://127.0.0.1")


def transcribe(client, path, **options):
    with open(path, "rb") as f:
        upload_id = client.post("/uploads", params={"filename": path.name}, content=f).json()["upload_id"]
    job_id = client.post("/jobs", json={"upload_id": upload_id, **options}).json()["job_id"]
    deadline = time.time() + 300
    while time.time() < deadline:
        info = client.get(f"/jobs/{job_id}").json()
        if info["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert info["status"] == "done", info
    return info, client.get(f"/jobs/{job_id}/subtitles").text


def plain(srt):
    text = " ".join(line for line in srt.splitlines() if line and not line.isdigit() and "-->" not in line)
    return re.sub(r"[^a-z ]", "", text.lower())


def assert_valid_srt(srt):
    blocks = srt.strip().split("\n\n")
    assert [b.split("\n")[0] for b in blocks] == [str(i) for i in range(1, len(blocks) + 1)]
    times = TIMING.findall(srt)
    assert len(times) == len(blocks)
    assert all(start < end for start, end in times)
    assert all(times[i][1] <= times[i + 1][0] for i in range(len(times) - 1))


def test_english_speech_becomes_valid_srt(client, speech):
    info, srt = transcribe(client, speech)
    assert info["language"] == "en"
    assert "ask not what your country can do for you" in plain(srt)
    assert_valid_srt(srt)


def test_vertical_style_gives_short_single_lines(client, speech):
    _, srt = transcribe(client, speech, style="vertical")
    assert_valid_srt(srt)
    lines = [l for l in srt.splitlines() if l and not l.isdigit() and "-->" not in l]
    assert len(lines) >= 4 and all(len(l) <= 38 for l in lines)


def test_a_long_intro_without_speech_does_not_break_language_or_timing(client, speech, tmp_path):
    """The language is detected on speech, and the cues start after the 40 second intro."""
    with_intro = tmp_path / "intro.mkv"
    subprocess.run([
        FFMPEG, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=40",
        "-i", str(speech),
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1", str(with_intro),
    ], check=True)
    info, srt = transcribe(client, with_intro)
    assert info["language"] == "en"
    assert "what your country can do for you" in plain(srt)  # the tiny model drops words at the cut
    first_start = TIMING.search(srt).group(1)
    assert first_start >= "00:00:39,000"
