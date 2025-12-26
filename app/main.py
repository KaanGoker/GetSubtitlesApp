from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from pathlib import Path
from uuid import uuid4
import time, subprocess, threading
from typing import Dict, Tuple, Optional, List
from faster_whisper import WhisperModel
import sys, shutil
import os 
import ctranslate2 as ct2

app = FastAPI(title="Get Subtitles — MVP")

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
DEFAULT_OUTPUT_DIR.mkdir(exist_ok=True)

# --- Local model folder helpers ---
def _bundle_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return BASE_DIR

def _local_model_path(choice: str) -> Optional[Path]:
    mroot = _bundle_base() / "models"
    key = (choice or "").lower().strip()
    folder = {"fast": "small", "balanced": "medium", "best": "large-v2"}.get(key)
    if not folder:
        return None
    p = mroot / folder
    return p if p.exists() else None

# --- Helpers for CUDA & CPU ---
def _detect_device() -> str:
    try:
        return "cuda" if ct2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"

def _compute_candidates(profile: str, device: str) -> list[str]:
    if device == "cuda":
        base = {"fast": "int8_float16", "balanced": "float16", "best": "float32"} \
               .get(profile, "float16")
        return [base, "float16", "float32"]
    else:
        base = {"fast": "int8", "balanced": "int16", "best": "float32"} \
               .get(profile, "int16")
        return [base, "int16", "float32"]

# ----------------- Model cache -----------------
_model_cache: Dict[str, WhisperModel] = {}
_model_meta: Dict[str, dict] = {}

def get_model(model_choice: str) -> Tuple[WhisperModel, dict]:
    key = (model_choice or "fast").lower().strip()
    if key in _model_cache:
        return _model_cache[key], _model_meta[key]

    size = "small" if key == "fast" else "medium" if key == "balanced" else "large-v2"
    if key not in ("fast", "balanced", "best"):
        key, size = "balanced", "medium"

    local = _local_model_path(key)
    model_id = str(local) if local else size

    device = _detect_device()
    candidates = _compute_candidates(key, device)

    last_err = None
    for compute in candidates:
        try:
            print(f"[GETSUBTITLES] Loading '{model_id}' device={device} compute_type='{compute}'")
            model = WhisperModel(model_id, device=device, compute_type=compute)
            meta = {
                "model_choice": key,
                "model_name": model_id,
                "compute_type": compute,
                "device": device,
                "source": "local" if local else "hub",
            }
            _model_cache[key] = model
            _model_meta[key] = meta
            return model, meta
        except Exception as e:
            print(f"[GETSUBTITLES] Failed compute_type={compute}: {e}")
            last_err = e

    raise RuntimeError(f"Could not load model on {device} with any compute type. Last error: {last_err}")


# ----------------- Utils -----------------

def _ffmpeg_path() -> str:
    if hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if p.exists():
            return str(p)
    here = Path(getattr(sys, "frozen", False) and sys.executable or __file__).resolve()
    cand = Path(here).parent / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if cand.exists():
        return str(cand)
    return shutil.which("ffmpeg") or "ffmpeg"

def to_wav16k_mono(src: Path, dst: Path):
    cmd = [_ffmpeg_path(), "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def write_srt_from_segments(segments, out_path: Path):
    """Default behavior: one block per model segment (unchanged)."""
    def fmt(t):
        h = int(t // 3600); t -= h*3600
        m = int(t // 60);   t -= m*60
        s = int(t);         ms = int((t - s)*1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    with out_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n{fmt(seg.start)} --> {fmt(seg.end)}\n{seg.text.strip()}\n\n")

# ---------- Vertical mode helpers (word-based packer) ----------
VERT_MAX_CHARS_PER_LINE  = 38
VERT_MAX_WORDS_PER_BLOCK = 10
VERT_MAX_DURATION_S      = 2.2
VERT_MIN_DURATION_S      = 0.7
PUNCT_BREAK = {".", ",", "!", "?", "…", ":", ";", "—", "–"}

def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def _clean_spaces(s: str) -> str:
    return (s.replace(" ,", ",")
             .replace(" .", ".")
             .replace(" !", "!")
             .replace(" ?", "?")
             .replace(" …", "…")
             .replace(" :", ":")
             .replace(" ;", ";"))

def _should_break(line: str, block_start: float, last_end: float, last_token: str) -> bool:
    duration = (last_end - block_start) if (last_end is not None and block_start is not None) else 0.0
    if duration >= VERT_MAX_DURATION_S:
        return True
    if len(line) >= VERT_MAX_CHARS_PER_LINE - 3 and last_token and last_token[-1] in PUNCT_BREAK:
        return True
    return False

def build_vertical_blocks(words: List[dict]) -> List[dict]:
    """Pack words into short, single-line blocks for reels/shorts."""
    blocks = []
    i, n = 0, len(words)

    while i < n:
        line = ""
        block_start = words[i]["start"]
        block_end   = words[i]["end"]
        count = 0
        j = i

        while j < n:
            w = words[j]
            token = w["text"]
            extra_len = (1 if line else 0) + len(token)

            if len(line) + extra_len > VERT_MAX_CHARS_PER_LINE and count > 0:
                break
            if count >= VERT_MAX_WORDS_PER_BLOCK:
                break

            line = f"{line} {token}" if line else token
            block_end = w["end"]
            count += 1
            j += 1

            if _should_break(line, block_start, block_end, token):
                break

        while j < n:
            duration = block_end - block_start
            if duration >= VERT_MIN_DURATION_S:
                break
            last_token = words[j-1]["text"] if j-1 >= 0 else ""
            if last_token and last_token[-1] in {".", "!", "?"}:
                break
            next_token = words[j]["text"]
            extra_len = (1 if line else 0) + len(next_token)
            if len(line) + extra_len > VERT_MAX_CHARS_PER_LINE:
                break
            line = f"{line} {next_token}"
            block_end = words[j]["end"]
            count += 1
            j += 1

        if line.strip():
            blocks.append({
                "start": block_start,
                "end": block_end,
                "text": _clean_spaces(line.strip())
            })

        i = j if j > i else i + 1

    return blocks

def write_srt_from_blocks(blocks: List[dict], out_path: Path):
    with out_path.open("w", encoding="utf-8") as f:
        for idx, b in enumerate(blocks, start=1):
            f.write(f"{idx}\n")
            f.write(f"{_fmt_time(b['start'])} --> {_fmt_time(b['end'])}\n")
            f.write(f"{b['text']}\n\n")

# ----------------- Job store -----------------
class Job:
    def __init__(self, job_id: str, original_name: str, out_dir: Path):
        self.job_id = job_id
        self.original_name = original_name
        self.out_dir = out_dir
        self.duration: float = 0.0
        self.progress: float = 0.0  # 0..1
        self.status: str = "queued"
        self.error_msg: Optional[str] = None
        self.language: Optional[str] = None
        self.model_choice: Optional[str] = None
        self.model_name: Optional[str] = None
        self.compute_type: Optional[str] = None
        self.started_at: float = time.time()
        self.finished_at: Optional[float] = None
        self.srt_path: Optional[Path] = None

JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

# ----------------- Routes -----------------
@app.get("/", response_class=HTMLResponse)
def index():
    return "<h2>Get Subtitles — MVP</h2><p>See <a href='/docs'>/docs</a>.</p>"

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/download/{job_id}.srt")
def download_srt(job_id: str):
    p = DEFAULT_OUTPUT_DIR / f"{job_id}.srt"
    if p.exists():
        return FileResponse(p, media_type="text/plain", filename=f"{job_id}.srt")
    return JSONResponse({"error": "not found"}, status_code=404)

@app.post("/models/clear_cache")
def clear_cache():
    _model_cache.clear()
    _model_meta.clear()
    return {"cleared": True}

# --- Graceful shutdown endpoint ---
@app.post("/shutdown")
def shutdown():
    """
    Immediately terminates the FastAPI process.
    Called from the Streamlit 'Close app' button.
    """
    import threading, os, time
    def _exit():
        time.sleep(0.1)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return {"ok": True}

# --------- Background worker ----------
def run_transcription(job_id: str,
                      wav_path: Path,
                      language: Optional[str],
                      task: str,
                      model_choice: str,
                      out_dir: Path,
                      style: str):
    """
    style: "default" | "vertical"
    - default  -> segment-based SRT (unchanged)
    - vertical -> word-timestamp packing for reels/shorts
    """
    job = JOBS[job_id]
    try:
        job.status = "loading_model"
        
        model, meta = get_model(model_choice)
        job.model_choice = meta["model_choice"]
        job.model_name = meta["model_name"]
        job.compute_type = meta["compute_type"]

        use_word_ts = (style == "vertical")
        segments, info = model.transcribe(
            str(wav_path),
            task=task,
            language=language,
            word_timestamps=use_word_ts
        )

        job.language = info.language
        job.duration = float(info.duration or 0.0)
        job.status = "running"

        # Progress based on last end time vs duration
        seg_list = []
        last_end = 0.0
        for seg in segments:
            seg_list.append(seg)
            last_end = max(last_end, float(getattr(seg, "end", 0.0) or 0.0))
            if job.duration > 0:
                job.progress = min(last_end / job.duration, 0.999)

        # Write SRT
        srt_path = out_dir / f"{job_id}.srt"

        if style == "vertical":
            # Build word list
            words = []
            for seg in seg_list:
                if getattr(seg, "words", None):
                    for w in seg.words:
                        token = (w.word or "").strip()
                        if not token:
                            continue
                        if w.start is None or w.end is None:
                            continue
                        words.append({"text": token, "start": float(w.start), "end": float(w.end)})

            if words:
                blocks = build_vertical_blocks(words)
                write_srt_from_blocks(blocks, srt_path)
            else:
                write_srt_from_segments(seg_list, srt_path)
        else:
            write_srt_from_segments(seg_list, srt_path)

        job.srt_path = srt_path
        job.progress = 1.0
        job.status = "done"
        job.finished_at = time.time()

    except Exception as e:
        job.status = "error"
        job.error_msg = str(e)
        job.finished_at = time.time()

# ---------- Async start + progress ----------
@app.post("/transcribe_start")
async def transcribe_start(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    language: str = Form("auto"),
    task: str = Form("transcribe"),
    model_choice: str = Form("fast"),
    output_dir: str = Form(None),
    style: str = Form("default"),
):
    job_id = str(uuid4())[:8]

    src_path = UPLOAD_DIR / f"{job_id}_{file.filename}"
    src_path.write_bytes(await file.read())

    out_dir = Path(output_dir).expanduser() if output_dir else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    wav_path = UPLOAD_DIR / f"{job_id}.wav"
    to_wav16k_mono(src_path, wav_path)

    with JOBS_LOCK:
        JOBS[job_id] = Job(job_id, file.filename, out_dir)

    lang = None if language == "auto" else language
    background_tasks.add_task(
        run_transcription, job_id, wav_path, lang, task, model_choice, out_dir, style
    )

    return {"job_id": job_id, "original_filename": file.filename}

@app.get("/progress/{job_id}")
def progress(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)

    eta_sec = None
    if job.status == "running" and job.progress and job.started_at:
        elapsed = time.time() - job.started_at
        if job.progress > 0:
            eta_sec = max(elapsed * (1.0 - job.progress) / job.progress, 0.0)

    resp = {
        "status": job.status,
        "progress": round(job.progress, 4),
        "eta_sec": None if eta_sec is None else round(eta_sec, 1),
        "language": job.language,
        "model_choice": job.model_choice,
        "model_name": job.model_name,
        "compute_type": job.compute_type,
        "duration_sec": round(job.duration, 2) if job.duration else None,
        "srt_path": str(job.srt_path) if job.srt_path else None,
        "srt_url": f"/download/{job.job_id}.srt" if job.srt_path and job.out_dir == DEFAULT_OUTPUT_DIR else "/download/disabled_for_custom_dir",
        "error": job.error_msg,
    }
    return resp
