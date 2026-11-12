"""Start a built app, run one real job with the Fast model, close it. Used by the release workflow.

    python tools/smoke_app.py dist/GetSubtitles
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SAMPLE_URL = "https://github.com/SYSTRAN/faster-whisper/raw/master/tests/data/jfk.flac"
PORTS = [8000, 8001, 8002, 8010, 8501]


def get(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def find_app():
    for _ in range(240):
        for port in PORTS:
            try:
                health = json.loads(get(f"http://127.0.0.1:{port}/health"))
                if health.get("app") == "getsubtitles":
                    return f"http://127.0.0.1:{port}", health
            except OSError:
                pass
        time.sleep(0.5)
    sys.exit("The app did not start.")


def main():
    binary = Path(sys.argv[1]).resolve()
    sample = Path("jfk.flac")
    if not sample.exists():
        urllib.request.urlretrieve(SAMPLE_URL, sample)
    env = dict(os.environ, GETSUBTITLES_NO_BROWSER="1")
    proc = subprocess.Popen([str(binary)], env=env)
    base, health = find_app()
    print("health", health)
    token = re.search(r'gs-token" content="([^"]+)', get(base + "/").decode()).group(1)
    try:
        auth = {"X-Token": token}
        upload = json.loads(get(f"{base}/uploads?filename=jfk.flac", sample.read_bytes(),
                                dict(auth, **{"Content-Type": "application/octet-stream"})))
        print("upload", upload)
        body = json.dumps({"upload_id": upload["upload_id"], "language": "auto", "task": "transcribe",
                           "model_choice": "fast", "style": "default"}).encode()
        job = json.loads(get(f"{base}/jobs", body, dict(auth, **{"Content-Type": "application/json"})))
        deadline = time.time() + 1200
        while time.time() < deadline:
            state = json.loads(get(f"{base}/jobs/{job['job_id']}"))
            if state["status"] in ("done", "error"):
                break
            time.sleep(2)
        print("job", state["status"], state.get("error"), state.get("device"), state.get("elapsed_sec"))
        srt = get(f"{base}/jobs/{job['job_id']}/subtitles?format=srt").decode()
        print(srt)
        if state["status"] != "done" or "country" not in srt.lower():
            sys.exit("The job did not give the expected subtitles.")
    finally:
        try:
            get(f"{base}/shutdown", b"", {"X-Token": token}, method="POST")
        except OSError:
            pass
    proc.wait(timeout=60)
    print("closed with", proc.returncode)


if __name__ == "__main__":
    main()
