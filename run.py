from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from app.config import DEFAULT_PORT, HOST
from app.main import app


if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def available_port(preferred: int) -> int:
    for port in range(preferred, preferred + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def open_browser(url: str) -> None:
    time.sleep(1.2)
    webbrowser.open(url)


if __name__ == "__main__":
    port = available_port(DEFAULT_PORT)
    url = f"http://{HOST}:{port}"
    if os.environ.get("CASE_DOWNLOADER_NO_BROWSER") != "1":
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    uvicorn.run(app, host=HOST, port=port, log_level="info")
