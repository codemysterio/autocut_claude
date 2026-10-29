"""Paths and settings shared by the server and the MCP bridge."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("AUTOCUT_DATA", ROOT / "data"))
PROJECTS_DIR = DATA_DIR / "projects"
FRONTEND_DIR = ROOT / "frontend"

HOST = os.environ.get("AUTOCUT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AUTOCUT_PORT", "8765"))

# Whisper model used when the user does not pick one. large-v3-turbo is fast and
# good for English/Italian; large-v3 is more accurate for Hindi/Hinglish.
DEFAULT_MODEL = os.environ.get("AUTOCUT_MODEL", "large-v3-turbo")

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
