"""Visit counter for the public demo: how many times the page was opened, per day.

Only a number per day is kept. No address, browser string, text or file is kept.
The counts live in one JSON file in a private Hugging Face dataset, so they survive
restarts of the Space. The counter is off unless STATS_TOKEN and STATS_REPO are set,
so the desktop and local runs never count and never connect anywhere."""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Mapping, Optional

log = logging.getLogger("visits")

SINCE = "2026-11-12"
HUB = "https://huggingface.co"
# Browsers say "Mozilla"; these words mark scripts, crawlers and link previews.
NOT_A_VISITOR = ("bot", "crawl", "spider", "slurp", "python", "curl", "wget", "httpx", "go-http",
                 "headless", "preview", "monitor", "uptime", "lighthouse")


def is_visit(method: str, headers: Mapping[str, str]) -> bool:
    """A page load by a person: a GET from a browser, as a page or inside the Space frame."""
    if method != "GET":
        return False
    agent = (headers.get("user-agent") or "").lower()
    if "mozilla" not in agent or any(word in agent for word in NOT_A_VISITOR):
        return False
    if (headers.get("sec-fetch-dest") or "document") not in ("document", "iframe"):
        return False
    purpose = (headers.get("sec-purpose") or headers.get("purpose") or "").lower()
    return "prefetch" not in purpose and "prerender" not in purpose


class Visits:
    def __init__(self, name: str, token: Optional[str] = None, repo: Optional[str] = None,
                 interval: float = 600.0):
        self.path = f"{name}.json"
        self.token = token
        self.repo = repo
        self.interval = interval
        self.days: dict[str, int] = {}
        self.loaded = False
        self.dirty = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def from_env(cls, name: str) -> "Visits":
        return cls(name, (os.environ.get("STATS_TOKEN") or "").strip() or None,
                   (os.environ.get("STATS_REPO") or "").strip() or None)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repo)

    def count(self, method: str, headers: Mapping[str, str]) -> None:
        if not self.enabled or not is_visit(method, headers):
            return
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        with self._lock:
            self.days[day] = self.days.get(day, 0) + 1
            self.dirty = True

    def snapshot(self) -> dict:
        with self._lock:
            days = dict(sorted(self.days.items()))
        return {"since": SINCE, "total": sum(days.values()), "days": days}

    # ---------- the dataset ----------

    def _request(self, url: str, data: Optional[bytes] = None, content_type: Optional[str] = None):
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        if content_type:
            req.add_header("Content-Type", content_type)
        return urllib.request.urlopen(req, timeout=60)

    def load(self) -> bool:
        """Adds the stored counts to the ones counted since start. False if the store did not answer."""
        url = f"{HUB}/datasets/{self.repo}/resolve/main/{self.path}"
        try:
            with self._request(url) as resp:
                stored = json.loads(resp.read().decode("utf-8")).get("days", {})
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                log.warning("visit counts not loaded: HTTP %s", exc.code)
                return False
            stored = {}
        except Exception as exc:
            log.warning("visit counts not loaded: %s", type(exc).__name__)
            return False
        with self._lock:
            for day, n in stored.items():
                if isinstance(n, int) and n > 0:
                    self.days[day] = self.days.get(day, 0) + n
            self.loaded = True
            self.dirty = self.dirty or not stored
        return True

    def flush(self) -> bool:
        """Writes the counts to the dataset when they changed. Never before the stored ones were loaded,
        so a failed load cannot overwrite the total."""
        if not self.enabled or not self.loaded or not self.dirty:
            return False
        with self._lock:
            self.dirty = False
        body = json.dumps(self.snapshot(), indent=1).encode("utf-8")
        lines = [
            {"key": "header", "value": {"summary": f"Visits {self.path}", "description": ""}},
            {"key": "file", "value": {"path": self.path, "encoding": "base64",
                                      "content": base64.b64encode(body).decode("ascii")}},
        ]
        payload = "\n".join(json.dumps(line) for line in lines).encode("utf-8")
        try:
            with self._request(f"{HUB}/api/datasets/{self.repo}/commit/main", payload, "application/x-ndjson"):
                pass
            return True
        except Exception as exc:
            code = getattr(exc, "code", None)
            log.warning("visit counts not saved: %s", code or type(exc).__name__)
            with self._lock:
                self.dirty = True
            return False

    # ---------- the background loop ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.loaded:
                self.load()
            self.flush()
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="visits", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.flush()
