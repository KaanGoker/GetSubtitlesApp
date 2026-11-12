"""Shared helpers for the API tests: a recording fake model and small request wrappers."""
import time
import wave

from tests.fakes import FakeWhisper


class RecordingWhisper(FakeWhisper):
    """Also notes how long the wav it was given is, and can be held at a gate."""
    gate = None

    def transcribe(self, path, **kwargs):
        with wave.open(str(path)) as f:
            seconds = f.getnframes() / f.getframerate()
        result = super().transcribe(path, **kwargs)
        FakeWhisper.calls[-1]["wav_seconds"] = seconds
        FakeWhisper.calls[-1]["wav_path"] = str(path)
        if RecordingWhisper.gate is not None:
            RecordingWhisper.gate.wait(timeout=10)
        return result


def upload(client, path, filename=None):
    with open(path, "rb") as f:
        return client.post("/uploads", params={"filename": filename or path.name}, content=f)


def start(client, upload_id, **options):
    return client.post("/jobs", json={"upload_id": upload_id, **options})


def wait_status(client, job_id, statuses, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = client.get(f"/jobs/{job_id}").json()
        if info.get("status") in statuses:
            return info
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {statuses}: {info}")


def wait_done(client, job_id, timeout=10):
    return wait_status(client, job_id, ("done", "error"), timeout)


def run_job(client, path, **options):
    upload_id = upload(client, path).json()["upload_id"]
    job_id = start(client, upload_id, **options).json()["job_id"]
    return job_id, wait_done(client, job_id)


def data_files(tmp_path, suffix):
    return sorted(p.name for p in (tmp_path / "data").glob(f"*{suffix}"))
