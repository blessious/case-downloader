from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_data_dir, user_downloads_dir


APP_NAME = "Case PDF Downloader"
APP_AUTHOR = "Case PDF Downloader"
APP_DATA_DIR = Path(user_data_dir(APP_NAME, APP_AUTHOR))
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = APP_DATA_DIR / "history.sqlite3"


def default_output_dir() -> Path:
    return Path(user_downloads_dir()) / "Case PDFs"


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath(*parts)


HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("CASE_DOWNLOADER_PORT", "8765"))
MAX_FILENAME_LENGTH = 180
REQUEST_TIMEOUT_SECONDS = 45.0
MAX_ATTEMPTS = 3
REQUEST_DELAY_SECONDS = 0.8
DOWNLOAD_CONCURRENCY = max(1, int(os.environ.get("CASE_DOWNLOADER_CONCURRENCY", "4")))
