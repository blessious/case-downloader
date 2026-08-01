from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_pdf_syllabus_upload_uses_extracted_text(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.extract_pdf_text",
        lambda _data, _pages="": "Mangila vs Court of Appeals (GR No 124027, 12 August 2002)",
    )
    client = TestClient(app)
    response = client.post(
        "/api/parse-file",
        files={"file": ("syllabus.pdf", b"%PDF-test", "application/pdf")},
        data={"page_range": "4-8", "folder_name": "Civil Procedure"},
    )
    assert response.status_code == 200
    case = response.json()["cases"][0]
    assert case["docket"] == "GR No 124027"
    assert case["decision_date"] == "2002-08-12"
    assert case["week"] == "Civil Procedure"
    assert response.json()["folder_name"] == "Civil Procedure"


def test_image_only_pdf_has_clear_error(monkeypatch) -> None:
    monkeypatch.setattr("app.main.extract_pdf_text", lambda _data, _pages="": "")
    client = TestClient(app)
    response = client.post(
        "/api/parse-file",
        files={"file": ("scan.pdf", b"%PDF-test", "application/pdf")},
    )
    assert response.status_code == 400
    assert "scanned PDFs are not supported" in response.json()["detail"]


def test_config_has_no_paid_search_setup() -> None:
    client = TestClient(app)
    config = client.get("/api/config").json()
    assert "web_search_configured" not in config


def test_paid_search_key_endpoints_are_removed() -> None:
    client = TestClient(app)
    assert client.post("/api/settings/search-key", json={"api_key": "unused-key"}).status_code == 404
    assert client.delete("/api/settings/search-key").status_code == 404
