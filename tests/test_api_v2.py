"""What the web page needs on top of upload, job and download: the page itself, editing,
cancelling, a zip of several results, and the count of open browser tabs."""
import io
import socket
import threading
import time
import zipfile

import httpx
import pytest

from tests.fakes import FakeWhisper
from tests.helpers import RecordingWhisper, run_job, start, upload, wait_done, wait_status


# ---------- the page ----------

def test_root_serves_the_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<title>GetSubtitles</title>" in r.text


def test_page_files_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_page_carries_the_token_only_when_there_is_one(make_client):
    assert 'name="gs-token" content=""' in make_client().get("/").text
    assert 'name="gs-token" content="secret"' in make_client(token="secret").get("/").text


def test_page_is_never_cached(client):
    assert "no-store" in client.get("/").headers["cache-control"]


# ---------- token: other websites cannot drive a desktop app ----------

def test_with_a_token_every_change_needs_it(make_client, media):
    client = make_client(token="secret")
    with open(media["one"], "rb") as f:
        assert client.post("/uploads", params={"filename": "a.wav"}, content=f).status_code == 403
    assert client.post("/cleanup").status_code == 403
    assert client.post("/cleanup", headers={"X-Token": "secret"}).status_code == 200


def test_with_a_token_reading_stays_open(make_client):
    assert make_client(token="secret").get("/health").status_code == 200


# ---------- health ----------

def test_health_says_which_device_will_be_used(make_client):
    assert make_client().get("/health").json()["device"] == "cpu"
    assert make_client(device="cuda").get("/health").json()["device"] == "cuda"


# ---------- uploads and job info ----------

def test_an_upload_can_be_looked_up_again(client, media):
    upload_id = upload(client, media["two"]).json()["upload_id"]
    body = client.get(f"/uploads/{upload_id}").json()
    assert body["filename"] == "two tracks.mkv"
    assert len(body["audio_tracks"]) == 2
    assert client.get("/uploads/nope").status_code == 404


def test_job_info_names_its_upload_and_options(client, media):
    upload_id = upload(client, media["one"]).json()["upload_id"]
    job_id = start(client, upload_id, style="vertical", language="ja").json()["job_id"]
    info = wait_done(client, job_id)
    assert info["job_id"] == job_id
    assert info["upload_id"] == upload_id
    assert info["style"] == "vertical"
    assert info["edited"] is False


# ---------- editing ----------

def test_cues_come_as_plain_numbers_and_text(client, media):
    job_id, _ = run_job(client, media["one"])
    cues = client.get(f"/jobs/{job_id}/cues").json()
    assert cues == [
        {"start": 0.0, "end": 1.0, "text": "Hello there."},
        {"start": 1.0, "end": 2.0, "text": "This is a test."},
    ]


def test_edited_cues_are_what_gets_downloaded(client, media):
    job_id, _ = run_job(client, media["one"])
    edited = [{"start": 0.5, "end": 1.5, "text": "Hello, Kaan."}]
    assert client.put(f"/jobs/{job_id}/cues", json=edited).status_code == 200
    assert client.get(f"/jobs/{job_id}/subtitles").text == "1\n00:00:00,500 --> 00:00:01,500\nHello, Kaan.\n\n"
    assert client.get(f"/jobs/{job_id}/subtitles", params={"format": "txt"}).text == "Hello, Kaan.\n"
    assert client.get(f"/jobs/{job_id}").json()["edited"] is True


def test_edited_cues_are_kept_in_time_order(client, media):
    job_id, _ = run_job(client, media["one"])
    client.put(f"/jobs/{job_id}/cues", json=[
        {"start": 5, "end": 6, "text": "second"},
        {"start": 1, "end": 2, "text": "first"},
    ])
    assert [c["text"] for c in client.get(f"/jobs/{job_id}/cues").json()] == ["first", "second"]


def test_edits_can_be_undone(client, media):
    job_id, _ = run_job(client, media["one"])
    client.put(f"/jobs/{job_id}/cues", json=[{"start": 0, "end": 1, "text": "changed"}])
    assert client.delete(f"/jobs/{job_id}/cues").status_code == 200
    assert client.get(f"/jobs/{job_id}/cues").json()[0]["text"] == "Hello there."
    assert client.get(f"/jobs/{job_id}").json()["edited"] is False


@pytest.mark.parametrize("bad", [
    [{"start": 2, "end": 1, "text": "ends before it starts"}],
    [{"start": -1, "end": 1, "text": "negative"}],
    [{"start": 0, "end": 1}],
    {"start": 0, "end": 1, "text": "not a list"},
])
def test_invalid_cues_are_refused_and_change_nothing(client, media, bad):
    job_id, _ = run_job(client, media["one"])
    assert client.put(f"/jobs/{job_id}/cues", json=bad).status_code == 422
    assert client.get(f"/jobs/{job_id}/cues").json()[0]["text"] == "Hello there."


def test_cues_of_an_unfinished_or_unknown_job(client, media):
    RecordingWhisper.gate = threading.Event()
    upload_id = upload(client, media["one"]).json()["upload_id"]
    job_id = start(client, upload_id).json()["job_id"]
    assert client.get(f"/jobs/{job_id}/cues").status_code == 409
    assert client.put(f"/jobs/{job_id}/cues", json=[]).status_code == 409
    assert client.get("/jobs/nope/cues").status_code == 404


# ---------- zip ----------

def test_zip_holds_one_file_per_job_named_after_the_upload(client, media):
    upload_a = upload(client, media["one"], filename="Bölüm 1.mp4").json()["upload_id"]
    upload_b = upload(client, media["one"], filename="Bölüm 2.mp4").json()["upload_id"]
    job_a = start(client, upload_a).json()["job_id"]
    job_b = start(client, upload_b).json()["job_id"]
    wait_done(client, job_a), wait_done(client, job_b)
    r = client.get("/zip", params={"jobs": f"{job_a},{job_b}", "format": "vtt"})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert z.namelist() == ["Bölüm 1.vtt", "Bölüm 2.vtt"]
        assert z.read("Bölüm 1.vtt").decode("utf-8").startswith("WEBVTT")


def test_zip_keeps_two_files_with_the_same_name_apart(client, media):
    job_a, _ = run_job(client, media["one"])
    job_b, _ = run_job(client, media["one"])
    r = client.get("/zip", params={"jobs": f"{job_a},{job_b}"})
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert z.namelist() == ["one track.srt", "one track (2).srt"]


def test_zip_with_an_unknown_or_unfinished_job_is_refused(client, media):
    job_a, _ = run_job(client, media["one"])
    assert client.get("/zip", params={"jobs": f"{job_a},nope"}).status_code == 404
    assert client.get("/zip", params={"jobs": ""}).status_code == 404


# ---------- cancelling ----------

def test_a_finished_job_can_be_removed(client, media):
    job_id, _ = run_job(client, media["one"])
    assert client.delete(f"/jobs/{job_id}").status_code == 200
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert client.delete(f"/jobs/{job_id}").status_code == 404


def test_a_waiting_job_that_is_removed_never_runs(client, media):
    RecordingWhisper.gate = threading.Event()
    upload_id = upload(client, media["one"]).json()["upload_id"]
    first = start(client, upload_id).json()["job_id"]
    second = start(client, upload_id).json()["job_id"]
    wait_status(client, first, ("transcribing",))
    assert client.delete(f"/jobs/{second}").status_code == 200
    RecordingWhisper.gate.set()
    wait_done(client, first)
    time.sleep(0.2)
    assert len(FakeWhisper.calls) == 1


def test_a_running_job_stops_and_the_next_one_runs(client, media, tmp_path):
    RecordingWhisper.gate = threading.Event()
    upload_id = upload(client, media["one"]).json()["upload_id"]
    first = start(client, upload_id).json()["job_id"]
    second = start(client, upload_id).json()["job_id"]
    wait_status(client, first, ("transcribing",))
    assert client.delete(f"/jobs/{first}").status_code == 200
    assert client.get(f"/jobs/{first}").status_code == 404
    RecordingWhisper.gate.set()
    assert wait_done(client, second)["status"] == "done"
    assert client.get("/health").json()["active_jobs"] == 0
    assert sorted(p.suffix for p in (tmp_path / "data").iterdir()) == [".src"]


# ---------- playing the uploaded file ----------

def test_uploaded_file_can_be_played_back(client, media):
    upload_id = upload(client, media["one"], filename="talk.wav").json()["upload_id"]
    r = client.get(f"/uploads/{upload_id}/media")
    assert r.status_code == 200
    assert r.content == media["one"].read_bytes()
    assert r.headers["content-type"].startswith("audio/")


def test_playback_can_seek_with_a_byte_range(client, media):
    upload_id = upload(client, media["one"]).json()["upload_id"]
    r = client.get(f"/uploads/{upload_id}/media", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == media["one"].read_bytes()[100:200]


def test_playback_of_an_unknown_upload_is_404(client):
    assert client.get("/uploads/nope/media").status_code == 404


# ---------- open browser tabs ----------

def test_open_tabs_follow_the_presence_connections(live_server):
    base = live_server(presence_ping_seconds=0.05)

    def open_tabs():
        return httpx.get(f"{base}/health").json()["open_tabs"]

    def wait_for(count):
        deadline = time.time() + 5
        while open_tabs() != count:
            assert time.time() < deadline, f"open_tabs stayed at {open_tabs()}, wanted {count}"
            time.sleep(0.05)

    assert open_tabs() == 0
    with httpx.stream("GET", f"{base}/presence", timeout=5) as tab:
        assert tab.headers["content-type"].startswith("text/event-stream")
        first_lines = tab.iter_lines()   # kept: closing this iterator closes the connection
        next(first_lines)
        wait_for(1)
        with httpx.stream("GET", f"{base}/presence", timeout=5) as second:
            second_lines = second.iter_lines()
            next(second_lines)
            wait_for(2)
    wait_for(0)


# ---------- an upload that stops halfway ----------

def test_an_upload_cut_off_halfway_leaves_nothing_on_disk(live_server, tmp_path):
    base = live_server()
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.sendall(b"POST /uploads?filename=big.mp4 HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\n\r\n")
        s.sendall(b"x" * 5000)
        time.sleep(0.3)        # the app has started to write the file
    deadline = time.time() + 5
    while list((tmp_path / "data").iterdir()):
        assert time.time() < deadline, "the half-written upload was left on disk"
        time.sleep(0.05)


# ---------- first run: the model is downloaded ----------

def test_job_reports_the_model_download(make_client, media):
    gate = threading.Event()

    def download(size, on_progress):
        on_progress(0.4)
        gate.wait(timeout=10)
        return size

    client = make_client(download=download)
    upload_id = upload(client, media["one"]).json()["upload_id"]
    job_id = start(client, upload_id).json()["job_id"]
    info = wait_status(client, job_id, ("downloading_model",))
    assert info["download_progress"] == 0.4
    assert client.get("/health").json()["active_jobs"] == 1
    gate.set()
    assert wait_done(client, job_id)["status"] == "done"


# ---------- files leave by themselves ----------

class Clock:
    """A clock the test moves by hand, so a slow machine cannot change the result."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def wait_until(check, message):
    deadline = time.time() + 5
    while not check():
        assert time.time() < deadline, message
        time.sleep(0.02)


def test_idle_files_are_removed_without_anyone_asking(make_client, media, tmp_path):
    clock = Clock()
    client = make_client(keep_seconds=60, cleanup_every_seconds=0.02, clock=clock)
    job_id, _ = run_job(client, media["one"])
    clock.now += 61                          # nobody touched the file or the result for a minute
    wait_until(lambda: list((tmp_path / "data").iterdir()) == [], "the idle upload was kept")
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_a_result_that_is_being_edited_is_kept(make_client, media, tmp_path):
    clock = Clock()
    client = make_client(keep_seconds=60, cleanup_every_seconds=0.02, clock=clock)
    job_id, _ = run_job(client, media["one"])
    for _ in range(5):                       # five minutes of editing, one edit every 50 seconds
        clock.now += 50
        time.sleep(0.1)                      # the cleanup runs several times in between
        assert client.put(f"/jobs/{job_id}/cues", json=[{"start": 0, "end": 1, "text": "still here"}]).status_code == 200
    assert len(list((tmp_path / "data").glob("*.src"))) == 1       # the file can still be played


# ---------- another website cannot reach the desktop app through its own domain name ----------

def test_desktop_app_answers_only_to_its_own_address(make_client):
    client = make_client(token="secret")
    assert client.get("/health", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/health", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.get("/health", headers={"Host": "evil.example:8000"}).status_code == 400


def test_demo_answers_to_any_address_docker_only_to_this_computer(make_client):
    assert make_client(demo=True).get("/health", headers={"Host": "subtitles.example"}).status_code == 200
    assert make_client().get("/health", headers={"Host": "subtitles.example"}).status_code == 400


# ---------- a played-back file can never run as a page ----------

@pytest.mark.parametrize("name", ["page.html", "page.svg", "page.htm", "no extension"])
def test_playback_never_serves_a_page(client, media, name):
    upload_id = upload(client, media["one"], filename=name).json()["upload_id"]
    r = client.get(f"/uploads/{upload_id}/media")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


def test_playback_of_real_media_keeps_its_type(client, media):
    upload_id = upload(client, media["two"], filename="film.mkv").json()["upload_id"]
    r = client.get(f"/uploads/{upload_id}/media")
    assert r.headers["content-type"].startswith("video/")
    assert r.headers["x-content-type-options"] == "nosniff"


# ---------- a page on another site cannot change anything, with or without a token ----------

@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},                     # what a sandboxed frame on another site sends
    {"Sec-Fetch-Site": "cross-site"},
])
def test_requests_from_another_site_are_refused(client, headers):
    assert client.post("/cleanup", headers=headers).status_code == 403


def test_requests_from_the_page_itself_are_allowed(client):
    assert client.post("/cleanup", headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_reading_from_another_site_is_not_blocked_by_this_check(client):
    assert client.get("/health", headers={"Origin": "https://evil.example"}).status_code == 200


def test_allowed_addresses_can_be_set_for_docker(make_client):
    client = make_client(allowed_hosts=["subtitles.lan"])
    assert client.get("/health", headers={"Host": "subtitles.lan"}).status_code == 200
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_docker_default_answers_only_to_this_computer(make_app, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("GETSUBTITLES_ALLOWED_HOSTS", raising=False)
    assert TestClient(make_app(), base_url="http://evil.example").get("/health").status_code == 400
    assert TestClient(make_app(), base_url="http://localhost:8000").get("/health").status_code == 200
    monkeypatch.setenv("GETSUBTITLES_ALLOWED_HOSTS", "*")
    assert TestClient(make_app(), base_url="http://subtitles.lan").get("/health").status_code == 200
