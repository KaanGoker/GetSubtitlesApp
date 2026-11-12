"""The desktop app, one process: starts the server on a free port, opens the browser,
and closes when the browser tab has been gone for a while."""
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PORTS = [8000, 8001, 8002, 8010, 8501]
WATCH_EVERY_S = 5
IDLE_CHECKS_BEFORE_EXIT = 12   # about a minute with the tab closed


def find_free_port(candidates):
    for p in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name != "nt":     # a port the app just left is free again at once
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_our_app(port: int) -> bool:
    """True only when GetSubtitles answers on the port, not any other program."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "getsubtitles"
    except Exception:
        return False


class IdleWatch:
    """Says when to close the app: a browser tab was open once and has been gone for a while.

    It counts checks, not seconds, so a laptop that slept does not look like a long idle time.
    """

    def __init__(self, checks_needed: int = IDLE_CHECKS_BEFORE_EXIT):
        self.checks_needed = checks_needed
        self.seen_tab = False
        self.idle_checks = 0

    def tick(self, open_tabs: int, active_jobs: int) -> bool:
        if open_tabs > 0:
            self.seen_tab = True
            self.idle_checks = 0
            return False
        if not self.seen_tab or active_jobs > 0:
            self.idle_checks = 0
            return False
        self.idle_checks += 1
        return self.idle_checks >= self.checks_needed


def _open_browser(url: str) -> None:
    if os.environ.get("GETSUBTITLES_NO_BROWSER") != "1":
        webbrowser.open(url)


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def storage_folder() -> Path:
    """Where the app keeps what it downloads.

    The packaged app (and every run on Windows) has its own folder. A run from source on Linux or
    macOS uses the Hugging Face cache, so it shares the models other programs downloaded.
    """
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "GetSubtitles"
    if _frozen() and sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GetSubtitles"
    if _frozen():
        return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "GetSubtitles"
    return Path(os.getenv("HF_HOME", Path.home() / ".cache" / "huggingface"))


def offers_gpu_download() -> bool:
    """The page offers GPU support where PyPI has the CUDA libraries and the app has its own folder."""
    return os.name == "nt" or (_frozen() and sys.platform.startswith("linux"))


def main() -> None:
    storage = storage_folder()
    if os.name == "nt" or _frozen():
        os.environ.setdefault("HF_HOME", str(storage / "hf"))
        os.environ.setdefault("CTRANSLATE2_HOME", str(storage / "ct2"))
        os.environ.setdefault("GETSUBTITLES_CUDA_DIR", str(storage / "cuda"))

    for port in PORTS:                      # already running: show that one
        if is_our_app(port):
            _open_browser(f"http://127.0.0.1:{port}")
            return

    if getattr(sys, "frozen", False):      # the windowed exe has no console to print to
        storage.mkdir(parents=True, exist_ok=True)
        log = storage / "getsubtitles.log"
        sys.stdout = sys.stderr = log.open("w", buffering=1, encoding="utf-8")

    import uvicorn
    from app.main import create_app

    port = find_free_port(PORTS)
    gpu_install = None
    if offers_gpu_download():               # from source, the libraries come with pip, see the README
        from app.gpu import install as gpu_install
    app = create_app(token=secrets.token_hex(16), storage_dir=storage, gpu_install=gpu_install)

    def open_when_ready():
        deadline = time.time() + 60
        while time.time() < deadline and not is_our_app(port):
            time.sleep(0.2)
        _open_browser(f"http://127.0.0.1:{port}")

    def watch():
        idle = IdleWatch()
        while True:
            time.sleep(WATCH_EVERY_S)
            if idle.tick(app.state.open_tabs, app.state.active_jobs()):
                print("No browser tab for a while: closing.", flush=True)
                app.state.close()

    threading.Thread(target=open_when_ready, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    print(f"GetSubtitles runs at http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
