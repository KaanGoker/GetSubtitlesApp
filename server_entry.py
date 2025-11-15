import os
from pathlib import Path
import uvicorn

base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "GetSubtitles"
os.environ.setdefault("HF_HOME", str(base / "hf"))
os.environ.setdefault("CTRANSLATE2_HOME", str(base / "ct2"))

from app.main import app 

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
