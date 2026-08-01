from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.jobs import JobManager
from app.main import app


def sample_case(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "ordinal": 1,
        "week": "Week 2",
        "title": "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda",
        "docket": "G.R. No. 145561",
        "decision_date": "2005-06-15",
        "raw_text": "sample",
        "filename": "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda.pdf",
        "warnings": [],
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_completed_run_does_not_create_report_files(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.sqlite3")
    manager = JobManager(db)
    run_id = manager.create_run(
        {
            "input_source": "Test",
            "output_dir": str(tmp_path / "output"),
            "cases": [
                {
                    "ordinal": 1,
                    "week": "Week 2",
                    "title": "Invalid case",
                    "docket": "",
                    "decision_date": "",
                    "raw_text": "",
                    "parse_error": "Missing docket; Missing date",
                    "warnings": [],
                }
            ],
        }
    )
    await manager._process(run_id, asyncio.Event(), retry_only=False)

    run = db.get_run(run_id)
    assert run is not None
    assert run["status"] == "Completed"
    assert run["report_path"] == ""
    assert not list((tmp_path / "output").glob("case-download-report_*"))


def test_history_and_report_endpoints_are_removed() -> None:
    client = TestClient(app)
    assert client.get("/api/runs").status_code == 405
    assert client.get(f"/api/runs/{uuid.uuid4()}/report").status_code == 404


def test_interrupted_rows_are_recovered_on_database_open(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    db = Database(path)
    case = sample_case()
    run_id = db.create_run("Test", str(tmp_path), [case])
    db.update_run(run_id, status="Running")
    db.update_case(str(case["id"]), status="Searching Lawphil")
    recovered = Database(path).get_run(run_id)
    assert recovered is not None
    assert recovered["status"] == "Interrupted"
    assert recovered["cases"][0]["status"] == "Not saved"
    assert recovered["cases"][0]["error_code"] == "INTERRUPTED"


def test_review_candidates_and_verified_cache_are_persisted(tmp_path: Path) -> None:
    db = Database(tmp_path / "review.sqlite3")
    case = sample_case(docket="", decision_date="")
    run_id = db.create_run("Test", str(tmp_path), [case])
    candidate = {
        "title": "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda",
        "docket": "G.R. No. 145561",
        "decision_date": "2005-06-15",
        "reporter_citation": "",
        "source": "Lawphil",
        "source_url": "https://lawphil.net/example",
        "score": 82,
        "reasons": ["Both case sides matched"],
        "verified": True,
    }
    db.set_case_candidates(str(case["id"]), [candidate])

    stored = db.get_run(run_id)
    assert stored is not None
    choice = stored["cases"][0]["candidates"][0]
    assert choice["verified"] is True
    assert choice["reasons"] == ["Both case sides matched"]
    assert db.get_case_candidate(str(case["id"]), choice["id"])["source"] == "Lawphil"

    db.cache_candidate(["docket:145561"], candidate)
    assert db.get_cached_candidates(["docket:145561"])[0]["source_url"] == candidate["source_url"]


def test_blocked_google_review_is_persisted_and_retryable(tmp_path: Path) -> None:
    db = Database(tmp_path / "google-review.sqlite3")
    case = sample_case(docket="", decision_date="")
    run_id = db.create_run("Test", str(tmp_path), [case])
    manual_url = "https://www.google.com/search?q=Lopez+v+Oroso"
    db.update_case(
        str(case["id"]),
        status="Needs review",
        error_code="GOOGLE_SEARCH_BLOCKED",
        error_message="Google paused automated searching.",
        manual_search_url=manual_url,
    )

    stored = db.get_run(run_id)
    assert stored is not None
    assert stored["cases"][0]["manual_search_url"] == manual_url
    assert db.cases_for_retry(run_id)[0]["id"] == case["id"]
