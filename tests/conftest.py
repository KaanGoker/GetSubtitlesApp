import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.engine import Engine
from app.main import create_app
from tests.fakes import FakeWhisper
from tests.helpers import RecordingWhisper

FFMPEG = shutil.which("ffmpeg")


def _ffmpeg(*args):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """Small generated media files: tones, no speech."""
    if not FFMPEG:
        pytest.skip("ffmpeg is not installed")
    d = tmp_path_factory.mktemp("media")

    one = d / "one track.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(one))

    two = d / "two tracks.mkv"
    _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
        "-map", "0:a", "-map", "1:a",
        "-metadata:s:a:0", "language=jpn",
        "-metadata:s:a:1", "language=rus", "-metadata:s:a:1", "title=Dub",
        "-c:a", "aac", str(two),
    )

    silent = d / "no audio.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-an", str(silent))

    broken = d / "broken.mp4"
    broken.write_bytes(b"this is not a media file")

    unplayable = d / "old format.au"     # ffmpeg reads it, browsers do not play it
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "au", str(unplayable))

    return {"one": one, "two": two, "silent": silent, "broken": broken, "unplayable": unplayable}


@pytest.fixture(autouse=True)
def reset_fake():
    FakeWhisper.reset()
    RecordingWhisper.gate = None
    yield
    if RecordingWhisper.gate is not None:
        RecordingWhisper.gate.set()


@pytest.fixture
def make_app(tmp_path):
    """Build the backend with the fake model; keyword arguments go to create_app."""
    def _make(device="cpu", download=lambda size, on_progress: size, **kwargs):
        engine = Engine(model_factory=RecordingWhisper, detect_device=lambda: device,
                        models_dir=tmp_path / "models", download=download)
        kwargs.setdefault("data_dir", tmp_path / "data")
        return create_app(engine=engine, **kwargs)
    return _make


@pytest.fixture
def make_client(make_app):
    def _make(**kwargs):
        return TestClient(make_app(**kwargs), base_url="http://127.0.0.1")
    return _make


@pytest.fixture
def client(make_client):
    return make_client()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(make_app):
    """Start the app on a real port. Returns a starter that gives back the base URL."""
    servers = []

    def _start(**kwargs):
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(make_app(**kwargs), host="127.0.0.1", port=port,
                                               log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.time() + 10
        while not server.started:
            assert time.time() < deadline
            time.sleep(0.01)
        servers.append(server)
        return f"http://127.0.0.1:{port}"

    _start.servers = servers
    yield _start
    for server in servers:
        server.should_exit = True
