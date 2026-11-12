import threading
import time

import pytest

from tests.fakes import FakeWhisper
from tests.helpers import RecordingWhisper, data_files, run_job, start, upload, wait_done


# ---------- health ----------

def test_health_says_which_app_answers(client):
    body = client.get("/health").json()
    assert body["app"] == "getsubtitles"
    assert body["models"] == ["fast", "balanced", "best", "turbo"]
    assert body["active_jobs"] == 0


# ---------- uploads ----------

def test_upload_returns_an_id_and_the_audio_tracks(client, media):
    r = upload(client, media["two"])
    assert r.status_code == 200
    body = r.json()
    assert body["upload_id"]
    assert [t["language"] for t in body["audio_tracks"]] == ["jpn", "rus"]


def test_upload_of_a_non_media_file_is_refused_and_not_kept(client, media, tmp_path):
    r = upload(client, media["broken"])
    assert r.status_code == 400
    assert "could not be read" in r.json()["error"]
    assert data_files(tmp_path, "") == []


def test_upload_of_a_video_without_sound_is_refused(client, media):
    r = upload(client, media["silent"])
    assert r.status_code == 400
    assert "no audio" in r.json()["error"]


def test_upload_name_never_becomes_a_path(client, media, tmp_path):
    r = upload(client, media["one"], filename="../../evil.wav")
    assert r.status_code == 200
    assert not (tmp_path / "evil.wav").exists()
    assert all(p.parent == tmp_path / "data" for p in (tmp_path / "data").iterdir())


def test_upload_over_the_limit_is_refused(make_client, media, tmp_path):
    client = make_client(max_upload_bytes=1000)
    r = upload(client, media["one"])
    assert r.status_code == 413
    assert data_files(tmp_path, "") == []


def test_old_uploads_are_removed_when_a_new_one_arrives(make_client, media, tmp_path):
    client = make_client(keep_seconds=0)
    upload(client, media["one"])
    second = upload(client, media["one"]).json()["upload_id"]
    assert data_files(tmp_path, ".src") == [f"{second}.src"]


# ---------- jobs ----------

def test_job_runs_to_done_and_gives_srt(client, media):
    job_id, info = run_job(client, media["one"])
    assert info["status"] == "done"
    assert info["progress"] == 1.0
    assert info["language"] == "en"
    assert info["model_choice"] == "fast"
    srt = client.get(f"/jobs/{job_id}/subtitles").text
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,000\nHello there.\n\n2\n")


def test_subtitles_come_in_vtt_and_txt_too(client, media):
    job_id, _ = run_job(client, media["one"])
    assert client.get(f"/jobs/{job_id}/subtitles", params={"format": "vtt"}).text.startswith("WEBVTT")
    assert client.get(f"/jobs/{job_id}/subtitles", params={"format": "txt"}).text == \
        "Hello there.\nThis is a test.\n"


def test_download_is_named_after_the_uploaded_file(client, media):
    upload_id = upload(client, media["one"], filename="Bölüm 5 Reel.mp4").json()["upload_id"]
    job_id = start(client, upload_id).json()["job_id"]
    wait_done(client, job_id)
    r = client.get(f"/jobs/{job_id}/subtitles")
    assert client.get(f"/jobs/{job_id}").json()["filename"] == "Bölüm 5 Reel.mp4"
    assert "B%C3%B6l%C3%BCm%205%20Reel.srt" in r.headers["content-disposition"]


def test_vertical_style_builds_cues_from_words(client, media):
    FakeWhisper.segments = [("one two three four five six", 0.0, 7.2)]
    job_id, _ = run_job(client, media["one"], style="vertical")
    txt = client.get(f"/jobs/{job_id}/subtitles", params={"format": "txt"}).text
    assert txt == "one two\nthree four\nfive six\n"


def test_auto_language_reaches_the_model_as_none(client, media):
    _, info = run_job(client, media["one"])
    assert info["language_auto"] is True
    assert FakeWhisper.calls[0]["language"] is None
    assert FakeWhisper.calls[0]["task"] == "transcribe"


def test_chosen_language_and_task_reach_the_model(client, media):
    _, info = run_job(client, media["one"], language="ja", task="translate")
    assert FakeWhisper.calls[0]["language"] == "ja"
    assert FakeWhisper.calls[0]["task"] == "translate"
    assert info["language"] == "ja"
    assert info["language_auto"] is False


def test_chosen_audio_track_is_the_one_transcribed(client, media):
    run_job(client, media["two"], audio_track=1)
    assert FakeWhisper.calls[0]["wav_seconds"] == pytest.approx(3.0, abs=0.15)


def test_audio_track_that_does_not_exist_is_refused(client, media):
    upload_id = upload(client, media["two"]).json()["upload_id"]
    assert start(client, upload_id, audio_track=5).status_code == 400


def test_two_uploads_with_the_same_name_keep_separate_results(client, media):
    FakeWhisper.segments = [("First file.", 0.0, 1.0)]
    job_a, _ = run_job(client, media["one"])
    FakeWhisper.segments = [("Second file.", 0.0, 1.0)]
    job_b, _ = run_job(client, media["one"])
    assert "First file." in client.get(f"/jobs/{job_a}/subtitles").text
    assert "Second file." in client.get(f"/jobs/{job_b}/subtitles").text


def test_same_upload_can_be_run_again_with_another_language(client, media):
    upload_id = upload(client, media["one"]).json()["upload_id"]
    wait_done(client, start(client, upload_id).json()["job_id"])
    second = wait_done(client, start(client, upload_id, language="ja").json()["job_id"])
    assert second["status"] == "done" and second["language"] == "ja"


def test_wav_is_deleted_when_the_job_ends(client, media, tmp_path):
    run_job(client, media["one"])
    assert data_files(tmp_path, ".wav") == []


def test_model_failure_becomes_a_job_error_with_the_message(client, media, tmp_path):
    FakeWhisper.fail_on_device = {"cpu"}
    _, info = run_job(client, media["one"])
    assert info["status"] == "error"
    assert "cudnn" in info["error"]
    assert data_files(tmp_path, ".wav") == []


def test_jobs_run_one_at_a_time_and_the_next_one_waits_in_line(client, media):
    RecordingWhisper.gate = threading.Event()
    upload_id = upload(client, media["one"]).json()["upload_id"]
    first = start(client, upload_id).json()["job_id"]
    second = start(client, upload_id).json()["job_id"]
    deadline = time.time() + 5
    while client.get(f"/jobs/{first}").json()["status"] != "transcribing":
        assert time.time() < deadline
        time.sleep(0.02)
    waiting = client.get(f"/jobs/{second}").json()
    assert waiting["status"] == "queued" and waiting["queue_position"] == 1
    assert client.get("/health").json()["active_jobs"] == 2
    RecordingWhisper.gate.set()
    assert wait_done(client, second)["status"] == "done"


def test_subtitles_of_an_unfinished_job_are_not_served(client, media):
    RecordingWhisper.gate = threading.Event()
    upload_id = upload(client, media["one"]).json()["upload_id"]
    job_id = start(client, upload_id).json()["job_id"]
    assert client.get(f"/jobs/{job_id}/subtitles").status_code == 409


# ---------- refusals ----------

def test_unknown_job_and_unknown_upload_are_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/subtitles").status_code == 404
    assert start(client, "nope").status_code == 404


@pytest.mark.parametrize("field,value", [
    ("model_choice", "huge"),
    ("style", "diagonal"),
    ("task", "summarize"),
    ("language", "Japanese!"),
])
def test_invalid_options_are_refused(client, media, field, value):
    upload_id = upload(client, media["one"]).json()["upload_id"]
    assert start(client, upload_id, **{field: value}).status_code == 422


def test_unknown_subtitle_format_is_refused(client, media):
    job_id, _ = run_job(client, media["one"])
    assert client.get(f"/jobs/{job_id}/subtitles", params={"format": "docx"}).status_code == 422


# ---------- demo mode (the public Space) ----------

def test_demo_offers_only_the_fast_model(make_client, media):
    client = make_client(demo=True)
    assert client.get("/health").json()["models"] == ["fast"]
    upload_id = upload(client, media["one"]).json()["upload_id"]
    assert start(client, upload_id, model_choice="best").status_code == 403


def test_demo_does_not_let_one_visitor_clean_up_for_all(make_client):
    assert make_client(demo=True).post("/cleanup").status_code == 403


def test_demo_takes_up_to_30_minutes_of_audio_and_the_app_any_length(make_client):
    assert make_client(demo=True).get("/health").json()["max_audio_minutes"] == 30
    assert make_client().get("/health").json()["max_audio_minutes"] is None


def test_demo_refuses_a_file_longer_than_the_limit(make_client, media, tmp_path):
    client = make_client(demo=True, max_audio_seconds=2.5)
    r = upload(client, media["two"])                 # 3 seconds
    assert r.status_code == 413
    assert "0:03 long" in r.json()["error"] and "desktop app" in r.json()["error"]
    assert data_files(tmp_path, ".src") == []
    assert upload(client, media["one"]).status_code == 200     # 2 seconds


def test_demo_turns_new_jobs_away_when_the_line_is_full(make_client, media):
    client = make_client(demo=True, max_waiting_jobs=2)
    RecordingWhisper.gate = threading.Event()
    upload_id = upload(client, media["one"]).json()["upload_id"]
    first = start(client, upload_id).json()["job_id"]
    assert start(client, upload_id).status_code == 200
    busy = start(client, upload_id)
    assert busy.status_code == 503 and "busy" in busy.json()["error"]
    RecordingWhisper.gate.set()
    wait_done(client, first)
    deadline = time.time() + 10
    while client.get("/health").json()["active_jobs"]:
        assert time.time() < deadline
        time.sleep(0.02)
    assert start(client, upload_id).status_code == 200


def test_the_app_has_no_line_limit(client, media):
    RecordingWhisper.gate = threading.Event()
    upload_id = upload(client, media["one"]).json()["upload_id"]
    assert all(start(client, upload_id).status_code == 200 for _ in range(12))
    RecordingWhisper.gate.set()


def test_demo_visitors_cannot_list_or_guess_other_visitors_files(make_client, media):
    client = make_client(demo=True)
    job_id, _ = run_job(client, media["one"])
    upload_id = client.get(f"/jobs/{job_id}").json()["upload_id"]
    assert len(job_id) == 32 and len(upload_id) == 32      # random 128 bit ids
    assert client.get("/jobs").status_code in (404, 405)
    assert client.get("/uploads").status_code in (404, 405)
    other = "0" * 32
    assert client.get(f"/jobs/{other}").status_code == 404
    assert client.get(f"/uploads/{other}/media").status_code == 404
    assert client.get("/zip", params={"jobs": other}).status_code == 404
    assert client.get("/zip", params={"jobs": f"{job_id},{other}"}).status_code == 404


SPACE = "kaangoker-getsubtitlesapp.hf.space"


def test_demo_page_behind_the_spaces_proxy_can_send_requests(make_client):
    # The proxy ends TLS and passes the public Host on; the page and the embedded frame on
    # huggingface.co/spaces both load from the hf.space address, so the Origin matches the Host.
    client = make_client(demo=True)
    r = client.post("/jobs", json={"upload_id": "missing"},
                    headers={"Host": SPACE, "Origin": f"https://{SPACE}", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 404                     # past the origin check, the upload is unknown
    r = client.post("/jobs", json={"upload_id": "missing"},
                    headers={"Host": SPACE, "Origin": "https://huggingface.co", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


# ---------- cleanup and shutdown ----------

def test_cleanup_removes_uploads_finished_jobs_and_loaded_models(client, media, tmp_path):
    job_id, _ = run_job(client, media["one"])
    assert client.post("/cleanup").status_code == 200
    assert data_files(tmp_path, "") == []
    assert client.get(f"/jobs/{job_id}").status_code == 404
    run_job(client, media["one"])
    assert len(FakeWhisper.built) == 2


def test_shutdown_does_not_exist_without_a_token(make_client):
    exits = []
    client = make_client(exit_fn=lambda: exits.append(1))
    assert client.post("/shutdown").status_code == 404
    assert exits == []


def test_shutdown_needs_the_right_token(make_client):
    exits = []
    client = make_client(token="secret", exit_fn=lambda: exits.append(1))
    assert client.post("/shutdown", headers={"X-Token": "wrong"}).status_code == 403
    assert exits == []
    assert client.post("/shutdown", headers={"X-Token": "secret"}).status_code == 200
    assert exits == [1]
