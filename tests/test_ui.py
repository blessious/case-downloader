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
        "paste-tab",
        "docx-tab",
        "resolution-message",
        "case-text",
        "parse-text-button",
        "docx-file",
        "page-range",
        "page-range-help",
        "syllabus-folder-name",
        "parse-docx-button",
        "review-section",
        "review-body",
        "output-dir",
        "validate-folder-button",
        "start-button",
        "run-section",
        "cancel-button",
        "retry-button",
        "results-body",
        "progress-bar",
        "last-updated",
        "theme-toggle",
        "undo-toast",
        "undo-delete-button",
    }
    assert not [control_id for control_id in required_ids if soup.find(id=control_id) is None]
    removed_ids = {
        "history-button",
        "history-section",
        "history-list",
        "report-button",
        "stat-total",
        "stat-saved",
        "stat-existing",
        "stat-failed",
        "search-settings-button",
        "search-setup",
        "search-key-form",
        "brave-api-key",
    }
    assert not [control_id for control_id in removed_ids if soup.find(id=control_id) is not None]
    assert "Turn your assigned-case list" not in soup.get_text(" ", strip=True)
    assert "Assigned cases" in soup.get_text(" ", strip=True)
    assert soup.find(id="docx-file").get("accept") == ".docx,.pdf"
    page_field = soup.find(id="page-range-field")
    assert "hidden" not in page_field.get("class", [])
    assert soup.find(id="page-range").has_attr("disabled")
    assert "Leave blank to read all pages" in page_field.get_text(" ", strip=True)
    review_text = soup.find(id="review-section").get_text(" ", strip=True)
    assert "Case title / filename" not in review_text
    assert "G.R. number" not in review_text


def test_review_script_supports_edit_delete_and_undo() -> None:
    script = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert 'api("/api/parse-text"' in script
    assert "saveCaseEdit" in script
    assert "deleteCase" in script
    assert "undoDelete" in script
    assert 'actionButton("Edit case"' in script
    assert 'actionButton("Delete case"' in script
    assert '"Searching Google"' in script
    assert '"Verifying match"' in script
    assert 'status === "Needs review"' in script
    assert "selectCandidate" in script
    assert "/select-candidate" in script
    assert "None match? Open Google search" in script
    assert 'item.status === "Needs review"' in script
    assert "/api/settings/search-key" not in script
