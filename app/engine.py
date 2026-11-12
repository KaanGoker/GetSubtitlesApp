"""Whisper models: loading, caching, and one transcription call that returns plain data."""
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

MODEL_SIZES = {
    "fast": "small",
    "balanced": "medium",
    "best": "large-v2",
    "turbo": "large-v3-turbo",
}

COMPUTE_TYPES = {
    "cuda": {"fast": "int8_float16", "balanced": "float16", "best": "float32", "turbo": "float16"},
    "cpu": {"fast": "int8", "balanced": "int16", "best": "float32", "turbo": "int8"},
}
COMPUTE_FALLBACKS = {"cuda": ["float16", "float32"], "cpu": ["int16", "float32"]}


def _default_factory(model_id, device, compute_type):
    from faster_whisper import WhisperModel
    return WhisperModel(model_id, device=device, compute_type=compute_type)


def cuda_lib_dirs(search_paths) -> list:
    """Folders of CUDA libraries installed with pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)."""
    found = []
    for base in search_paths:
        nvidia = Path(base) / "nvidia"
        if not nvidia.is_dir():
            continue
        for pkg in sorted(nvidia.iterdir()):
            found += [pkg / sub for sub in ("bin", "lib") if (pkg / sub).is_dir()]
    return found


_added_dirs = set()


def gpu_count() -> int:
    """NVIDIA GPUs CTranslate2 sees, with or without the CUDA libraries."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def _load_cuda_libs() -> None:
    """Make pip-installed CUDA libraries visible to CTranslate2, so the GPU works without setup."""
    import ctypes
    import os
    import site
    paths = list(sys.path)
    try:
        paths += site.getsitepackages()
    except AttributeError:
        pass
    if os.environ.get("GETSUBTITLES_CUDA_DIR"):     # the GPU support the packaged app downloads
        paths.append(os.environ["GETSUBTITLES_CUDA_DIR"])
    for folder in cuda_lib_dirs(dict.fromkeys(paths)):
        if str(folder) in _added_dirs:
            continue
        _added_dirs.add(str(folder))
        if os.name == "nt":
            os.add_dll_directory(str(folder))
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")
            continue
        for lib in sorted(folder.glob("lib*.so*")):
            try:
                ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass        # a library that does not load leaves the CPU fallback in place


def cuda_libs_load(loader=None, windows=None) -> bool:
    """True when the CUDA libraries Whisper needs on the GPU (cuBLAS 12, cuDNN 9) can be loaded."""
    import ctypes
    import os
    if windows is None:
        windows = os.name == "nt"
    if loader is None:
        # winmode=0 searches PATH too, the way CTranslate2 finds the DLLs
        loader = (lambda name: ctypes.CDLL(name, winmode=0)) if windows else ctypes.CDLL
    names = ("cublas64_12.dll", "cudnn64_9.dll") if windows else ("libcublas.so.12", "libcudnn.so.9")
    try:
        for name in names:
            loader(name)
    except OSError:
        return False
    return True


def _default_detect_device() -> str:
    try:
        _load_cuda_libs()
    except Exception:
        pass
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() == 0:
            return "cpu"
        return "cuda" if cuda_libs_load() else "cpu"   # a GPU without the libraries fails on the first job
    except Exception:
        return "cpu"


MODEL_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json")


def _watch_download(size: str, on_progress: Callable[[float], None], stop: threading.Event) -> None:
    """Report how much of the model is on disk while faster-whisper downloads it."""
    try:
        from faster_whisper.utils import _MODELS
        from huggingface_hub import HfApi, constants
        repo = _MODELS[size]
        files = HfApi().model_info(repo, files_metadata=True).siblings
        total = sum(f.size or 0 for f in files
                    if f.rfilename in MODEL_FILES or f.rfilename.startswith("vocabulary."))
        blobs = Path(constants.HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}" / "blobs"
    except Exception as e:
        print(f"[GETSUBTITLES] No download progress to show: {e!r}")
        return                      # the download itself goes on
    if not total:
        return
    while not stop.wait(0.5):
        try:
            have = sum(f.stat().st_size for f in blobs.iterdir() if f.is_file())
        except OSError:
            have = 0
        on_progress(min(have / total, 0.99))


def _default_download(size: str, on_progress: Callable[[float], None]) -> str:
    """The folder of a Whisper model, downloaded from the Hugging Face hub when it is not on disk yet."""
    from faster_whisper.utils import download_model
    try:
        path = download_model(size, local_files_only=True)
        if (Path(path) / "model.bin").exists():
            return path
    except Exception:
        pass                        # not downloaded yet
    stop = threading.Event()
    watcher = threading.Thread(target=_watch_download, args=(size, on_progress, stop), daemon=True)
    watcher.start()
    try:
        path = download_model(size)
    finally:
        stop.set()
        watcher.join(timeout=2)
    # The watcher counts bytes in the cache; some downloaders (hf_xet) do not fill it gradually.
    on_progress(1.0)
    return path


def _default_models_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models"
    return Path(__file__).resolve().parent.parent / "models"


class Cancelled(Exception):
    """The caller asked to stop this transcription."""


class Engine:
    def __init__(self,
                 model_factory: Callable = _default_factory,
                 detect_device: Callable[[], str] = _default_detect_device,
                 models_dir: Optional[Path] = None,
                 download: Callable[[str, Callable[[float], None]], str] = _default_download):
        self._factory = model_factory
        self._download = download
        self._detect_device = detect_device
        self._models_dir = Path(models_dir) if models_dir else _default_models_dir()
        self._cache = {}  # (choice, device) -> (model, compute_type)
        self._gpu_broken = False
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @property
    def device(self) -> str:
        """The device the next job will run on."""
        return "cpu" if self._gpu_broken else self._detect_device()

    def _load(self, choice: str, device: str, on_download: Optional[Callable[[float], None]] = None):
        with self._lock:
            if (choice, device) in self._cache:
                return self._cache[(choice, device)]
            size = MODEL_SIZES[choice]
            local = self._models_dir / size
            if local.exists():
                model_id = str(local)
            else:
                try:
                    model_id = self._download(size, on_download or (lambda fraction: None))
                except Exception as e:
                    raise RuntimeError(f"The model could not be downloaded. The first run of a model "
                                       f"needs an internet connection. ({e})")
            candidates = list(dict.fromkeys([COMPUTE_TYPES[device][choice], *COMPUTE_FALLBACKS[device]]))
            last_err = None
            for compute_type in candidates:
                try:
                    print(f"[GETSUBTITLES] Loading '{model_id}' device={device} compute_type='{compute_type}'")
                    model = self._factory(model_id, device, compute_type)
                except Exception as e:  # this compute type is not available on this machine
                    print(f"[GETSUBTITLES] Failed compute_type={compute_type}: {e}")
                    last_err = e
                    continue
                self._cache[(choice, device)] = (model, compute_type)
                return model, compute_type
            raise RuntimeError(f"Could not load the model on {device}. Last error: {last_err}")

    def transcribe(self, wav_path, *, model_choice: str, language: Optional[str], task: str,
                   on_progress: Optional[Callable[[float], None]] = None,
                   on_status: Optional[Callable[[str], None]] = None,
                   should_cancel: Optional[Callable[[], bool]] = None,
                   on_download: Optional[Callable[[float], None]] = None) -> dict:
        if model_choice not in MODEL_SIZES:
            raise ValueError(f"Unknown model: {model_choice}")
        if model_choice == "turbo" and task == "translate":
            model_choice = "best"  # the turbo model was not trained to translate

        device = self.device
        callbacks = (on_progress, on_status, should_cancel, on_download)
        try:
            return self._run(wav_path, model_choice, device, language, task, *callbacks)
        except Cancelled:
            raise
        except Exception as e:
            if device != "cuda":
                raise
            # Typical case: the GPU is there but its cuDNN/cuBLAS libraries are not.
            print(f"[GETSUBTITLES] GPU run failed, retrying on CPU: {e}")
            self._gpu_broken = True
            with self._lock:
                self._cache.pop((model_choice, "cuda"), None)
            return self._run(wav_path, model_choice, "cpu", language, task, *callbacks)

    def _run(self, wav_path, choice, device, language, task,
             on_progress, on_status, should_cancel, on_download) -> dict:
        if on_status:
            on_status("loading_model")
        model, compute_type = self._load(choice, device, on_download)
        if on_status:
            on_status("transcribing")

        segments_iter, info = model.transcribe(
            str(wav_path),
            task=task,
            language=language,
            word_timestamps=True,
            vad_filter=True,
            # Detect the language on speech from up to two minutes, not only the first 30 seconds.
            language_detection_segments=4,
        )
        duration = float(info.duration or 0.0)

        segments = []
        for seg in segments_iter:
            if should_cancel and should_cancel():
                raise Cancelled()
            segments.append({
                "text": seg.text,
                "start": float(seg.start),
                "end": float(seg.end),
                "words": [
                    {"text": w.word, "start": float(w.start), "end": float(w.end)}
                    for w in (seg.words or [])
                    if w.start is not None and w.end is not None
                ],
            })
            if on_progress and duration > 0:
                on_progress(min(float(seg.end) / duration, 1.0))
        if on_progress:
            on_progress(1.0)

        return {
            "segments": segments,
            "language": info.language,
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
            "duration": duration,
            "model_choice": choice,
            "device": device,
            "compute_type": compute_type,
        }
