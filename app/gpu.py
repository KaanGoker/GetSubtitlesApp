"""GPU support for the packaged app: the CUDA libraries Whisper needs, downloaded once from PyPI.

Windows keeps the DLLs from each wheel's bin folder, Linux the .so files from its lib folder.
"""
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.request import urlopen

# The versions the app is tested with. CTranslate2 4.x needs cuBLAS 12 and cuDNN 9.
WHEELS = {"nvidia-cublas-cu12": "12.9.2.10", "nvidia-cudnn-cu12": "9.28.1.4"}
PLATFORMS = {
    "windows": {"wheel": "-win_amd64.whl", "folder": "bin"},
    "linux": {"wheel": "_x86_64.whl", "folder": "lib"},
}
DOWNLOAD_BYTES = 1_250_000_000      # about the size of the two wheels, for the page


def _fetch_json(url: str) -> dict:
    with urlopen(url, timeout=30) as r:
        return json.load(r)


def _open_url(url: str):
    return urlopen(url, timeout=60)


def this_platform() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def wheel_for(package: str, version: str, fetch_json=_fetch_json, platform: str = "windows") -> dict:
    """The wheel of one package version for this platform: its address, size and checksum."""
    data = fetch_json(f"https://pypi.org/pypi/{package}/{version}/json")
    suffix = PLATFORMS[platform]["wheel"]
    for f in data["urls"]:
        name = f["filename"]
        if name.endswith(suffix) and (platform == "windows" or "manylinux" in name):
            return {"url": f["url"], "size": f["size"], "sha256": f["digests"]["sha256"],
                    "filename": f["filename"]}
    raise RuntimeError(f"PyPI has no {platform} build of {package} {version}.")


def _is_library(name: str, platform: str) -> bool:
    parts = PurePosixPath(name).parts
    if len(parts) < 4 or parts[0] != "nvidia" or parts[2] != PLATFORMS[platform]["folder"] or ".." in parts:
        return False
    return name.lower().endswith(".dll") if platform == "windows" else ".so" in parts[-1]


def install(dest, on_progress: Callable[[float], None],
            fetch_json=_fetch_json, open_url=_open_url, wheels=WHEELS, platform=None) -> None:
    """Download the wheels and keep only their libraries, in dest/nvidia/<package>/bin (or lib).

    The files go to a side folder first, so the app never sees half of the libraries.
    """
    final = Path(dest)
    dest = final.with_name(final.name + ".part")
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    platform = platform or this_platform()
    files = [wheel_for(p, v, fetch_json, platform) for p, v in wheels.items()]
    total = sum(f["size"] for f in files) or 1
    done = 0
    for f in files:
        part = dest / (f["filename"] + ".part")
        digest = hashlib.sha256()
        try:
            with open_url(f["url"]) as r, open(part, "wb") as out:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    on_progress(min(done / total, 0.99))
            if digest.hexdigest() != f["sha256"]:
                raise RuntimeError(f"{f['filename']} did not download correctly. Try again.")
            with zipfile.ZipFile(part) as z:
                for name in z.namelist():
                    if not _is_library(name, platform):
                        continue
                    target = dest / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
        finally:
            part.unlink(missing_ok=True)
    shutil.rmtree(final, ignore_errors=True)
    dest.rename(final)
    on_progress(1.0)
