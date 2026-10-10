"""Check the actual frozen Mac app and the Chromium shipped inside its bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright
from pypdf import PdfReader


def main() -> None:
    bundle = Path(__file__).resolve().parents[1] / "dist/Case PDF Downloader.app"
    binary = bundle / "Contents/MacOS/Case PDF Downloader"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, CASE_DOWNLOADER_NO_BROWSER="1", CASE_DOWNLOADER_PORT=str(port))
    with tempfile.TemporaryDirectory() as temporary:
        with (Path(temporary) / "startup.log").open("w+") as log:
            process = subprocess.Popen([str(binary)], env=env, stdout=log, stderr=log)
            try:
                base = f"http://127.0.0.1:{port}"
                for _ in range(60):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(f"Packaged app exited: {log.read()}")
                    try:
                        with urllib.request.urlopen(base + "/api/config", timeout=2) as response:
                            json.load(response)
                        break
                    except OSError:
                        time.sleep(1)
                else:
                    raise RuntimeError("Packaged app did not start within 60 seconds")
                with urllib.request.urlopen(base + "/static/app.js") as response:
                    script = response.read().decode()
                assert "Verify this match" in script
                assert "reporter_page_verified" in script
                request = urllib.request.Request(
                    base + "/api/resolve-cases",
                    data=json.dumps({"cases": [{"ordinal": 1, "title": "Alpha v. Beta", "docket": "123456", "decision_date": "2020-01-02"}]}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request) as response:
                    json.load(response)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        browsers = list(bundle.rglob("Chromium.app/Contents/MacOS/Chromium"))
        assert browsers, "Bundled Chromium was not found"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(browsers[0]), headless=True)
            try:
                page = browser.new_page()
                page.set_content("<h1>Packaged browser PDF smoke test</h1>")
                pdf = Path(temporary) / "smoke.pdf"
                page.pdf(path=str(pdf))
                assert "Packaged browser PDF smoke test" in PdfReader(pdf).pages[0].extract_text()
            finally:
                browser.close()
    print("Packaged app startup, updated verification UI, resolver, and bundled PDF browser passed.")


if __name__ == "__main__":
    main()
