"""Start the AutoCut web app: python run_server.py"""
import os
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "backend"))

import uvicorn  # noqa: E402

from autocut.config import HOST, PORT  # noqa: E402

if __name__ == "__main__":
    if not os.environ.get("AUTOCUT_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run("autocut.server:app", host=HOST, port=PORT, log_level="warning")
