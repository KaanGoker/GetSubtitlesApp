import pytest

from app.engine import Cancelled, Engine
from tests.fakes import FakeWhisper


def no_download(size, on_progress):
    return size


def make_engine(device="cpu", **kwargs):
    kwargs.setdefault("download", no_download)
    return Engine(model_factory=FakeWhisper, detect_device=lambda: device, **kwargs)


def run(engine, **kwargs):
    options = {"model_choice": "fast", "language": None, "task": "transcribe"}
    options.update(kwargs)
    return engine.transcribe("audio.wav", **options)


def test_auto_language_lets_the_model_detect_it():
    result = run(make_engine(), language=None)
    assert FakeWhisper.calls[0]["language"] is None
    assert result["language"] == "en"
    assert result["language_probability"] == pytest.approx(0.97)


def test_chosen_language_is_passed_to_the_model():
    result = run(make_engine(), language="ja")
    assert FakeWhisper.calls[0]["language"] == "ja"
    assert result["language"] == "ja"


def test_silence_filter_and_word_times_are_always_on():
    run(make_engine())
    assert FakeWhisper.calls[0]["vad_filter"] is True
    assert FakeWhisper.calls[0]["word_timestamps"] is True


def test_segments_come_back_as_plain_dicts_with_raw_word_text():
    result = run(make_engine())
    first = result["segments"][0]
    assert first["text"] == " Hello there."
    assert first["words"][0] == {"text": " Hello", "start": 0.0, "end": 0.5}


def test_progress_rises_to_the_end_of_the_audio():
    seen = []
    run(make_engine(), on_progress=seen.append)
    assert seen == sorted(seen) and seen[-1] == pytest.approx(1.0)


def test_model_choices_map_to_whisper_sizes():
    engine = make_engine()
    for choice in ("fast", "balanced", "best", "turbo"):
        run(engine, model_choice=choice)
    assert [b[0] for b in FakeWhisper.built] == ["small", "medium", "large-v2", "large-v3-turbo"]


def test_unknown_model_choice_is_refused():
    with pytest.raises(ValueError):
        run(make_engine(), model_choice="huge")


def test_a_model_is_loaded_once_and_reused():
    engine = make_engine()
    run(engine)
    run(engine)
    assert len(FakeWhisper.built) == 1


def test_clear_drops_loaded_models():
    engine = make_engine()
    run(engine)
    engine.clear()
    run(engine)
    assert len(FakeWhisper.built) == 2


def test_loading_falls_back_to_the_next_compute_type():
    FakeWhisper.fail_compute_types = {"int8"}
    result = run(make_engine())
    assert [b[2] for b in FakeWhisper.built] == ["int8", "int16"]
    assert result["compute_type"] == "int16"


def test_gpu_failure_while_decoding_retries_on_cpu():
    FakeWhisper.fail_on_device = {"cuda"}
    result = run(make_engine(device="cuda"))
    assert result["device"] == "cpu"
    assert len(result["segments"]) == 2


def test_after_a_gpu_failure_later_jobs_go_straight_to_cpu():
    FakeWhisper.fail_on_device = {"cuda"}
    engine = make_engine(device="cuda")
    run(engine)
    FakeWhisper.calls.clear()
    run(engine)
    assert [c["device"] for c in FakeWhisper.calls] == ["cpu"]


def test_cpu_failure_is_not_retried():
    FakeWhisper.fail_on_device = {"cpu"}
    with pytest.raises(RuntimeError):
        run(make_engine())
    assert len(FakeWhisper.calls) == 1


def test_turbo_cannot_translate_so_best_is_used():
    result = run(make_engine(), model_choice="turbo", task="translate")
    assert FakeWhisper.built[0][0] == "large-v2"
    assert result["model_choice"] == "best"


def test_a_model_folder_next_to_the_app_is_used_before_the_hub(tmp_path):
    (tmp_path / "small").mkdir()
    run(make_engine(models_dir=tmp_path))
    assert FakeWhisper.built[0][0] == str(tmp_path / "small")


def test_status_callback_reports_model_loading_then_transcribing():
    seen = []
    run(make_engine(), on_status=seen.append)
    assert seen == ["loading_model", "transcribing"]


def test_cancelling_stops_between_segments():
    seen = []
    with pytest.raises(Cancelled):
        run(make_engine(), on_progress=seen.append, should_cancel=lambda: len(seen) >= 1)
    assert len(seen) == 1


def test_cancelling_on_the_gpu_is_not_taken_for_a_gpu_failure():
    with pytest.raises(Cancelled):
        run(make_engine(device="cuda"), should_cancel=lambda: True)
    assert [c["device"] for c in FakeWhisper.calls] == ["cuda"]


def test_device_is_reported_before_any_job():
    assert make_engine(device="cuda").device == "cuda"
    assert make_engine().device == "cpu"


def test_a_model_that_is_not_on_disk_is_downloaded_with_progress():
    def download(size, on_progress):
        on_progress(0.25)
        on_progress(1.0)
        return f"/cache/{size}"

    seen = []
    run(make_engine(download=download), on_download=seen.append)
    assert seen == [0.25, 1.0]
    assert FakeWhisper.built[0][0] == "/cache/small"


def test_download_progress_ends_at_one_even_when_the_cache_watcher_lags(monkeypatch):
    import faster_whisper.utils
    import app.engine as engine_module

    def download_model(size, local_files_only=False):
        if local_files_only:
            raise FileNotFoundError("not in the cache")
        return f"/cache/{size}"

    def lagging_watcher(size, on_progress, stop):
        on_progress(0.557)              # what the cache showed while hf_xet downloaded
        stop.wait(5)

    monkeypatch.setattr(faster_whisper.utils, "download_model", download_model)
    monkeypatch.setattr(engine_module, "_watch_download", lagging_watcher)
    seen = []
    assert engine_module._default_download("small", seen.append) == "/cache/small"
    assert seen[-1] == 1.0


def test_a_model_folder_next_to_the_app_is_not_downloaded(tmp_path):
    (tmp_path / "small").mkdir()
    calls = []
    run(make_engine(models_dir=tmp_path, download=lambda size, p: calls.append(size)))
    assert calls == []


def test_a_failed_download_says_that_internet_is_needed():
    def download(size, on_progress):
        raise OSError("Name or service not known")

    with pytest.raises(RuntimeError, match="could not be downloaded"):
        run(make_engine(download=download))
    assert FakeWhisper.built == []


def test_cuda_lib_dirs_finds_pip_installed_nvidia_libraries(tmp_path):
    from app.engine import cuda_lib_dirs
    for pkg in ("cublas", "cudnn"):
        (tmp_path / "nvidia" / pkg / "lib").mkdir(parents=True)
    (tmp_path / "nvidia" / "cudnn" / "bin").mkdir()          # Windows wheels keep DLLs in bin
    (tmp_path / "nvidia" / "other").mkdir()                  # no lib folder: skipped
    found = cuda_lib_dirs([str(tmp_path), str(tmp_path / "missing")])
    names = sorted(str(p.relative_to(tmp_path)) for p in found)
    assert names == ["nvidia/cublas/lib", "nvidia/cudnn/bin", "nvidia/cudnn/lib"]


def test_cuda_lib_dirs_is_empty_without_nvidia_packages(tmp_path):
    from app.engine import cuda_lib_dirs
    assert cuda_lib_dirs([str(tmp_path)]) == []


def test_cuda_libs_load_needs_cublas_and_cudnn():
    from app.engine import cuda_libs_load
    loaded = []

    def loader(name):
        loaded.append(name)
        if "cudnn" in name:
            raise OSError("not found")

    assert cuda_libs_load(loader, windows=True) is False
    assert loaded == ["cublas64_12.dll", "cudnn64_9.dll"]
    assert cuda_libs_load(lambda name: None, windows=False) is True
