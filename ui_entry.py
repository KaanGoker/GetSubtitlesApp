import os, sys, time, socket, subprocess, webbrowser
from pathlib import Path

BACKEND = ("127.0.0.1", 8000)

def _exe_dir() -> Path:
    return Path(getattr(sys, "frozen", False) and sys.executable or __file__).resolve().parent

def _bundle_dir() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return _exe_dir()

def _open_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", buffering=1, encoding="utf-8")

def port_open(host, port):
    s = socket.socket(); s.settimeout(0.3)
    ok = s.connect_ex((host, port)) == 0
    s.close(); return ok

def start_server_if_needed():
    exe_dir = _exe_dir()
    server = exe_dir / ("GetSubtitlesServer.exe" if os.name == "nt" else "GetSubtitlesServer")
    if server.exists() and not port_open(*BACKEND):
        popen_kwargs = {}
        if os.name == "nt":
            DETACHED_PROCESS         = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW         = 0x08000000
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            popen_kwargs["creationflags"] = flags
            popen_kwargs["stdout"] = _open_log(exe_dir / "server_run.log")
            popen_kwargs["stderr"] = subprocess.STDOUT
        else:
            popen_kwargs["start_new_session"] = True
            popen_kwargs["stdout"] = _open_log(exe_dir / "server_run.log")
            popen_kwargs["stderr"] = subprocess.STDOUT
            popen_kwargs["close_fds"] = True
        subprocess.Popen([str(server)], **popen_kwargs)

def find_free_port(candidates):
    for p in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

if __name__ == "__main__":
    exe_dir = _exe_dir()
    ui_log = _open_log(exe_dir / "ui_run.log")
    sys.stdout = sys.stderr = ui_log

    if not port_open(*BACKEND):
        start_server_if_needed()
        for _ in range(40):
            if port_open(*BACKEND): break
            time.sleep(0.25)

    for k in list(os.environ.keys()):
        if k.upper().startswith("STREAMLIT_DEV") or "DEV_SERVER" in k.upper():
            os.environ.pop(k, None)
    os.environ["STREAMLIT_GLOBAL_DEVELOPMENTMODE"] = "false"
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"

    target = _bundle_dir() / "streamlit_app.py"
    if not target.exists():
        print(f"streamlit_app.py not found at {target}")
        sys.exit(1)

    os.chdir(str(Path.home()))

    preferred = int(os.environ.get("STREAMLIT_SERVER_PORT", "8501") or "8501")
    port = find_free_port([preferred, 8502, 8503, 8510, 8520])

    os.environ["STREAMLIT_SERVER_ADDRESS"] = "127.0.0.1"

    webbrowser.open(f"http://127.0.0.1:{port}")

    from streamlit.web.cli import main as st_main
    sys.argv = [
        "streamlit", "run", str(target),
        "--global.developmentMode=false",
        "--server.address", "127.0.0.1",
        "--server.port", str(port),
        "--server.headless=true",
        "--server.maxUploadSize", "8192",
        "--browser.serverAddress=localhost",
        "--browser.gatherUsageStats=false",
    ]
    sys.exit(st_main())
