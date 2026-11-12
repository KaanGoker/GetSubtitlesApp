import base64
import io
import json
import urllib.error

import pytest

from app import visits as v

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"


@pytest.mark.parametrize("method,headers,ok", [
    ("GET", {"user-agent": BROWSER}, True),
    ("GET", {"user-agent": BROWSER, "sec-fetch-dest": "iframe"}, True),
    ("HEAD", {"user-agent": BROWSER}, False),
    ("GET", {"user-agent": BROWSER, "sec-fetch-dest": "image"}, False),
    ("GET", {"user-agent": BROWSER, "sec-purpose": "prefetch"}, False),
    ("GET", {"user-agent": "python-requests/2.32"}, False),
    ("GET", {"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}, False),
    ("GET", {"user-agent": "Mozilla/5.0 HeadlessChrome/131.0"}, False),
    ("GET", {}, False),
])
def test_is_visit(method, headers, ok):
    assert v.is_visit(method, headers) is ok


def test_off_without_token():
    c = v.Visits("x")
    c.count("GET", {"user-agent": BROWSER})
    assert not c.enabled and c.snapshot()["total"] == 0 and c.flush() is False


class FakeHub:
    def __init__(self, stored=None, fail_get=None):
        self.stored, self.fail_get, self.commits = stored, fail_get, []

    def __call__(self, req, timeout=None):
        assert req.get_header("Authorization") == "Bearer t"
        if req.get_method() == "GET":
            if self.fail_get:
                raise urllib.error.HTTPError(req.full_url, self.fail_get, "x", {}, None)
            if self.stored is None:
                raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)
            return io.BytesIO(json.dumps(self.stored).encode())
        lines = [json.loads(line) for line in req.data.decode().splitlines()]
        assert req.full_url.endswith("/api/datasets/me/stats/commit/main")
        self.commits.append(json.loads(base64.b64decode(lines[1]["value"]["content"])))
        return io.BytesIO(b"{}")


def test_load_adds_and_flush_writes(monkeypatch):
    hub = FakeHub({"days": {"2026-11-12": 5}})
    monkeypatch.setattr(v.urllib.request, "urlopen", hub)
    c = v.Visits("app", "t", "me/stats")
    c.count("GET", {"user-agent": BROWSER})
    assert c.flush() is False          # not before the stored counts are loaded
    assert c.load() is True
    assert c.flush() is True and c.flush() is False
    saved = hub.commits[-1]
    assert saved["since"] == v.SINCE and saved["total"] == 6


def test_failed_load_never_overwrites(monkeypatch):
    monkeypatch.setattr(v.urllib.request, "urlopen", FakeHub(fail_get=500))
    c = v.Visits("app", "t", "me/stats")
    c.count("GET", {"user-agent": BROWSER})
    assert c.load() is False and c.flush() is False


def test_first_run_creates_file(monkeypatch):
    hub = FakeHub(None)
    monkeypatch.setattr(v.urllib.request, "urlopen", hub)
    c = v.Visits("app", "t", "me/stats")
    assert c.load() is True and c.flush() is True
    assert hub.commits[-1]["total"] == 0


def test_desktop_never_counts(monkeypatch, make_app):
    monkeypatch.setenv("STATS_TOKEN", "t")
    monkeypatch.setenv("STATS_REPO", "me/stats")
    assert not make_app(demo=False).state.visits.enabled
    assert make_app(demo=True).state.visits.enabled


def test_page_load_is_counted(make_client):
    c = make_client()
    seen = []
    c.app.state.visits.count = lambda method, headers: seen.append(method)
    assert c.get("/", headers={"user-agent": "Mozilla/5.0"}).status_code == 200
    assert seen == ["GET"]
