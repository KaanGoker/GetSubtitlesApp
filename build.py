"""Build the desktop app with PyInstaller.

    pip install pyinstaller
    python build.py

The result is dist/GetSubtitles.exe on Windows, dist/GetSubtitles.app on macOS and
dist/GetSubtitles on Linux. ffmpeg is packed in: bin/ffmpeg(.exe) when it is there, otherwise
the static build of the imageio-ffmpeg package (pip install imageio-ffmpeg).
"""
import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
SEP = os.pathsep

args = [
    str(ROOT / "launcher.py"),
    "--name", "GetSubtitles",
    # macOS wants an .app folder; one file elsewhere
    "--onedir" if sys.platform == "darwin" else "--onefile",
    "--windowed" if sys.platform == "darwin" else "--noconsole",
    "--clean",
    "--noconfirm",
    "--add-data", f"{ROOT / 'app' / 'static'}{SEP}app/static",   # the page
    "--collect-data", "faster_whisper",                          # the silence filter model
    "--collect-submodules", "uvicorn",
    "--hidden-import", "ctranslate2",
    "--hidden-import", "faster_whisper",
]

ffmpeg = ROOT / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
if not ffmpeg.exists():
    try:
        import imageio_ffmpeg
    except ImportError:
        sys.exit(f"{ffmpeg} is missing. Put a static ffmpeg build there, or pip install imageio-ffmpeg.")
    ffmpeg.parent.mkdir(exist_ok=True)
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg)
    ffmpeg.chmod(0o755)
args += ["--add-binary", f"{ffmpeg}{SEP}bin"]

if sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "dev.kaangoker.getsubtitles"]
if os.name == "nt":
    args += ["--icon", str(ROOT / "icon assets" / "getsubtitles.ico")]

PyInstaller.__main__.run(args)
