"""ffmpeg calls: list the audio tracks of a file, extract one as 16 kHz mono wav."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


class AudioError(Exception):
    """A message that can be shown to the user as it is."""


def ffmpeg_path() -> str:
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / "bin" / name
        if p.exists():
            return str(p)
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    for base in (here.parent, here.parent.parent):
        p = base / "bin" / name
        if p.exists():
            return str(p)
    return shutil.which("ffmpeg") or "ffmpeg"


def _run(args: List[str]) -> subprocess.CompletedProcess:
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        return subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-nostdin", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
        )
    except FileNotFoundError:
        raise AudioError("ffmpeg was not found. Install ffmpeg or put it in the bin folder.")


_STREAM = re.compile(r"^\s*Stream #\d+:\d+(?:\[[^\]]*\])?(?:\((\w+)\))?: Audio: ([\w-]+)(.*)$")
_TITLE = re.compile(r"^\s+title\s*:\s*(.+?)\s*$")
_ANY_STREAM = re.compile(r"^\s*Stream #")


def parse_audio_tracks(ffmpeg_info: str) -> List[dict]:
    """Read the audio streams from the text `ffmpeg -i file` prints."""
    tracks = []
    current = None
    for line in ffmpeg_info.splitlines():
        m = _STREAM.match(line)
        if m:
            language, codec, rest = m.groups()
            current = {
                "index": len(tracks),
                "language": None if language in (None, "und") else language,
                "codec": codec,
                "title": None,
                "default": "(default)" in rest,
            }
            tracks.append(current)
        elif _ANY_STREAM.match(line):
            current = None
        elif current is not None:
            t = _TITLE.match(line)
            if t:
                current["title"] = t.group(1)
    return tracks


_DURATION = re.compile(r"^\s*Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", re.MULTILINE)


def parse_duration(ffmpeg_info: str) -> Optional[float]:
    """The length of the file in seconds, from the text `ffmpeg -i file` prints; None when unknown."""
    m = _DURATION.search(ffmpeg_info)
    if not m:
        return None
    hours, minutes, seconds = m.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# ffmpeg may only read the uploaded file itself: no network, no other protocol.
SAFE_INPUT = ["-protocol_whitelist", "file"]
# Formats that are lists of other files or addresses (playlists, concat lists, manifests).
_LIST_FORMATS = {"hls", "applehttp", "concat", "ffconcat", "dash", "webm_dash_manifest", "image2", "tee"}


def input_formats(ffmpeg_info: str) -> List[str]:
    """The format names in the `Input #0, mov,mp4,m4a, from` line."""
    m = re.search(r"^Input #0, (.*), from ", ffmpeg_info, re.M)
    return [f.strip() for f in m.group(1).split(",")] if m else []


def probe(src: Path) -> Tuple[List[dict], Optional[float]]:
    """The audio tracks of a file and its length in seconds."""
    info = _run([*SAFE_INPUT, "-i", str(src)]).stderr.decode("utf-8", errors="replace")
    if "Input #0" not in info or _LIST_FORMATS.intersection(input_formats(info)):
        raise AudioError("This file could not be read as audio or video.")
    return parse_audio_tracks(info), parse_duration(info)


def probe_audio_tracks(src: Path) -> List[dict]:
    return probe(src)[0]


def extract_wav(src: Path, dst: Path, track: int = 0, max_seconds: Optional[float] = None) -> None:
    """max_seconds: stop after this much audio, whatever length the file claims to have."""
    limit = ["-t", f"{float(max_seconds):g}"] if max_seconds else []
    result = _run([
        "-y", *SAFE_INPUT, "-i", str(src),
        "-map", f"0:a:{int(track)}", "-vn", "-ac", "1", "-ar", "16000", *limit, str(dst),
    ])
    if result.returncode != 0:
        log = result.stderr.decode("utf-8", errors="replace")
        if "Input #0" not in log:
            raise AudioError("This file could not be read as audio or video.")
        if "matches no streams" in log:
            raise AudioError("The file has no audio track with that number.")
        last = log.strip().splitlines()[-1] if log.strip() else "unknown error"
        raise AudioError(f"The audio could not be extracted: {last}")
