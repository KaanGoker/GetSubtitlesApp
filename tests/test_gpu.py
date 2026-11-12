"""GPU support for the packaged app: the CUDA libraries downloaded once from PyPI, offered on the page."""
import hashlib
import io
import threading
import time
import zipfile

import pytest

from app.gpu import install


def fake_wheel(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in names:
            z.writestr(name, b"dll " + name.encode())
    return buf.getvalue()


@pytest.fixture
def pypi():
    wheels = {
        "a": fake_wheel(["nvidia/cublas/bin/cublas64_12.dll", "nvidia/cublas/bin/cublasLt64_12.dll",
                         "nvidia/cublas/include/cublas.h", "nvidia_cublas-12.dist-info/METADATA"]),
        "b": fake_wheel(["nvidia/cudnn/bin/cudnn64_9.dll", "nvidia/cudnn/bin/../../../evil.dll"]),
    }
    sums = {k: hashlib.sha256(v).hexdigest() for k, v in wheels.items()}

    def fetch_json(url):
        key = "a" if "cublas" in url else "b"
        return {"urls": [
            {"filename": f"{key}-py3-none-manylinux_x86_64.whl", "url": "linux", "size": 1,
             "digests": {"sha256": "x"}},
            {"filename": f"{key}-py3-none-win_amd64.whl", "url": key, "size": len(wheels[key]),
             "digests": {"sha256": sums[key]}},
        ]}

    return wheels, sums, fetch_json


def test_install_keeps_only_the_dlls_of_the_windows_wheels(tmp_path, pypi):
    wheels, _, fetch_json = pypi
    seen = []
    install(tmp_path / "cuda", seen.append, fetch_json=fetch_json,
            open_url=lambda url: io.BytesIO(wheels[url]), wheels={"nvidia-cublas-cu12": "1", "nvidia-cudnn-cu12": "1"},
            platform="windows")
    files = sorted(p.relative_to(tmp_path / "cuda").as_posix() for p in (tmp_path / "cuda").rglob("*") if p.is_file())
    assert files == ["nvidia/cublas/bin/cublas64_12.dll", "nvidia/cublas/bin/cublasLt64_12.dll",
                     "nvidia/cudnn/bin/cudnn64_9.dll"]
    assert not (tmp_path / "evil.dll").exists()
    assert not (tmp_path / "cuda.part").exists()
    assert seen[-1] == 1.0 and seen == sorted(seen)


def test_a_broken_download_leaves_no_libraries(tmp_path, pypi):
    wheels, _, fetch_json = pypi
    with pytest.raises(RuntimeError, match="did not download correctly"):
        install(tmp_path / "cuda", lambda f: None, fetch_json=fetch_json,
                open_url=lambda url: io.BytesIO(wheels[url][:-10]), wheels={"nvidia-cublas-cu12": "1"},
                platform="windows")
    assert not (tmp_path / "cuda").exists()


def test_install_on_linux_keeps_the_shared_libraries(tmp_path):
    wheel = fake_wheel(["nvidia/cublas/lib/libcublas.so.12", "nvidia/cublas/lib/libcublasLt.so.12",
                        "nvidia/cublas/include/cublas.h", "nvidia/cublas/lib/__init__.py"])
    digest = hashlib.sha256(wheel).hexdigest()

    def fetch_json(url):
        return {"urls": [
            {"filename": "a-py3-none-win_amd64.whl", "url": "win", "size": 1, "digests": {"sha256": "x"}},
            {"filename": "a-py3-none-manylinux_2_27_aarch64.whl", "url": "arm", "size": 1,
             "digests": {"sha256": "x"}},
            {"filename": "a-py3-none-manylinux_2_27_x86_64.whl", "url": "x86", "size": len(wheel),
             "digests": {"sha256": digest}},
        ]}

    install(tmp_path / "cuda", lambda f: None, fetch_json=fetch_json,
            open_url=lambda url: io.BytesIO(wheel), wheels={"nvidia-cublas-cu12": "1"}, platform="linux")
    files = sorted(p.name for p in (tmp_path / "cuda").rglob("*") if p.is_file())
    assert files == ["libcublas.so.12", "libcublasLt.so.12"]


def test_cuda_folder_is_found_by_the_engine(tmp_path, monkeypatch):
    from app import engine
    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    assert engine.cuda_lib_dirs([tmp_path]) == [tmp_path / "nvidia" / "cublas" / "bin"]


def test_gpu_routes_are_off_without_an_installer(make_client, tmp_path):
    assert make_client(token="t", storage_dir=tmp_path).get("/gpu").status_code == 404


def test_gpu_install_needs_the_token_and_reports_progress(make_client, tmp_path):
    gate = threading.Event()
    calls = []

    def fake_install(folder, on_progress):
        calls.append(folder)
        on_progress(0.5)
        gate.wait(5)

    client = make_client(token="secret", storage_dir=tmp_path, gpu_install=fake_install, gpu_present=lambda: True)
    assert client.get("/gpu").json()["present"] is True
    assert client.post("/gpu/install").status_code == 403
    assert client.post("/gpu/install", headers={"X-Token": "secret"}).status_code == 200
    for _ in range(100):
        if client.get("/gpu").json()["progress"] == 0.5:
            break
        time.sleep(0.02)
    state = client.get("/gpu").json()
    assert state["running"] is True and state["progress"] == 0.5
    assert client.post("/gpu/install", headers={"X-Token": "secret"}).status_code == 409
    gate.set()
    for _ in range(100):
        if not client.get("/gpu").json()["running"]:
            break
        time.sleep(0.02)
    assert client.get("/gpu").json()["error"] is None
    assert calls == [tmp_path / "cuda"]


def test_gpu_install_error_is_shown(make_client, tmp_path):
    def broken(folder, on_progress):
        raise OSError("no network")

    client = make_client(token="secret", storage_dir=tmp_path, gpu_install=broken, gpu_present=lambda: True)
    client.post("/gpu/install", headers={"X-Token": "secret"})
    for _ in range(100):
        if client.get("/gpu").json()["error"]:
            break
        time.sleep(0.02)
    assert "no network" in client.get("/gpu").json()["error"]
