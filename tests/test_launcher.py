import http.server
import socket
import threading

from launcher import IdleWatch, find_free_port, is_our_app
from tests.conftest import free_port


def test_idle_watch_never_fires_before_a_browser_tab_was_seen():
    watch = IdleWatch(checks_needed=3)
    assert not any(watch.tick(open_tabs=0, active_jobs=0) for _ in range(10))


def test_idle_watch_fires_after_the_tab_stayed_closed_for_enough_checks():
    watch = IdleWatch(checks_needed=3)
    watch.tick(open_tabs=1, active_jobs=0)
    assert [watch.tick(open_tabs=0, active_jobs=0) for _ in range(3)] == [False, False, True]


def test_idle_watch_starts_over_when_the_tab_comes_back():
    watch = IdleWatch(checks_needed=3)
    watch.tick(open_tabs=1, active_jobs=0)
    watch.tick(open_tabs=0, active_jobs=0)
    watch.tick(open_tabs=0, active_jobs=0)
    watch.tick(open_tabs=1, active_jobs=0)   # a page refresh reconnects
    assert [watch.tick(open_tabs=0, active_jobs=0) for _ in range(3)] == [False, False, True]


def test_idle_watch_waits_while_a_job_is_running():
    watch = IdleWatch(checks_needed=2)
    watch.tick(open_tabs=1, active_jobs=0)
    assert not any(watch.tick(open_tabs=0, active_jobs=1) for _ in range(10))
    assert [watch.tick(open_tabs=0, active_jobs=0) for _ in range(2)] == [False, True]


def test_free_port_prefers_the_first_candidate_that_is_free():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        busy = taken.getsockname()[1]
        free = free_port()
        assert find_free_port([busy, free]) == free


def test_our_app_is_recognised(live_server):
    port = int(live_server().rsplit(":", 1)[1])
    assert is_our_app(port) is True


def test_another_program_on_the_port_is_not_taken_for_the_app():
    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>some other app</html>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert is_our_app(server.server_address[1]) is False
    finally:
        server.shutdown()


def test_nothing_on_the_port_is_not_the_app():
    assert is_our_app(free_port()) is False


def test_storage_folder_is_the_hugging_face_home_off_windows(monkeypatch, tmp_path):
    import launcher
    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert launcher.storage_folder() == tmp_path / "hf"


def test_packaged_app_has_its_own_folder_on_linux_and_macos(monkeypatch, tmp_path):
    import launcher
    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    assert launcher.storage_folder() == tmp_path / ".local" / "share" / "GetSubtitles"
    assert launcher.offers_gpu_download()
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    assert launcher.storage_folder() == tmp_path / "Library" / "Application Support" / "GetSubtitles"
    assert not launcher.offers_gpu_download()


def test_run_from_source_on_linux_offers_no_gpu_download(monkeypatch):
    import launcher
    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.delattr(launcher.sys, "frozen", raising=False)
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    assert not launcher.offers_gpu_download()
