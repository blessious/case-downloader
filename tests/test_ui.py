from __future__ import annotations

from bs4 import BeautifulSoup


def test_personal_ui_keeps_all_required_controls() -> None:
    html = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    required_ids = {
        "history-button",
        "paste-tab",
        "docx-tab",
        "case-text",
        "parse-text-button",
        "docx-file",
        "parse-docx-button",
        "review-section",
        "review-body",
        "output-dir",
        "validate-folder-button",
        "start-button",
        "run-section",
        "cancel-button",
        "retry-button",
        "report-button",
        "results-body",
        "history-section",
        "history-list",
    }
    assert not [control_id for control_id in required_ids if soup.find(id=control_id) is None]
    assert "Turn your assigned-case list" not in soup.get_text(" ", strip=True)
    assert "Download case PDFs" in soup.get_text(" ", strip=True)
