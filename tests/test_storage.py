"""Where the desktop app keeps its models, shown on the page with a button that opens the folder."""
import pytest


def fake_model(storage, repo, size, done=True):
    folder = storage / "hf" / "hub" / f"models--{repo.replace('/', '--')}"
    (folder / "blobs").mkdir(parents=True)
    (folder / "snapshots" / "abc").mkdir(parents=True)
    if done:
        (folder / "snapshots" / "abc" / "model.bin").write_bytes(b"x" * size)
    else:
        (folder / "blobs" / "abc.incomplete").write_bytes(b"x" * size)


@pytest.fixture
def storage(tmp_path):
    d = tmp_path / "GetSubtitles"
    fake_model(d, "Systran/faster-whisper-small", 1000)
    fake_model(d, "mobiuslabsgmbh/faster-whisper-large-v3-turbo", 3000)
    fake_model(d, "someone/other-model", 50)          # not ours: not listed
    fake_model(d, "Systran/faster-whisper-large-v2", 700, done=False)   # still downloading: not listed
    return d


def test_storage_lists_the_folder_and_the_downloaded_models(make_client, storage):
    client = make_client(token="secret", storage_dir=storage, models_cache=storage / "hf" / "hub")
    body = client.get("/storage").json()
    assert body["path"] == str(storage)
    assert body["models"] == [{"model": "fast", "size_bytes": 1000}, {"model": "turbo", "size_bytes": 3000}]


def test_storage_is_off_without_a_folder_and_in_the_demo(make_client, storage):
    assert make_client().get("/storage").status_code == 404
    assert make_client(demo=True, storage_dir=storage).get("/storage").status_code == 404


def test_open_folder_needs_the_token_and_opens_the_storage_folder(make_client, storage):
    opened = []
    client = make_client(token="secret", storage_dir=storage, open_folder=opened.append)
    assert client.post("/storage/open").status_code == 403
    assert client.post("/storage/open", headers={"X-Token": "secret"}).json() == {"ok": True}
    assert opened == [storage]
