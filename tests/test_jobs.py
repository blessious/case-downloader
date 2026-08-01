from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.database import Database
from app.jobs import JobManager, resolve_output_dir
from app.pdf_utils import PdfValidation
from app.domain import RetrievalResult
from app.resolver import CaseCandidate, GoogleSearchBlocked, google_search_url


def request_case(ordinal: int, title: str, docket: str) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "week": "Week 2",
        "title": title,
        "docket": docket,
        "decision_date": "2005-06-15",
        "raw_text": "",
        "parse_error": "",
        "warnings": [],
    }


def test_duplicate_and_collision_are_reported(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.sqlite3")
    manager = JobManager(db)
    run_id = manager.create_run(
        {
            "input_source": "Test",
            "output_dir": str(tmp_path / "output"),
            "cases": [
                request_case(1, "Same v. Title", "G.R. No. 111111"),
                request_case(2, "Same vs Title", "G.R. No. 111111"),
                request_case(3, "Same v. Title", "G.R. No. 222222"),
            ],
        }
    )
    run = db.get_run(run_id)
    assert run is not None
    assert [item["status"] for item in run["cases"]] == [
        "Queued",
        "Duplicate input",
        "Not saved",
    ]
    assert run["cases"][2]["error_code"] == "FILENAME_COLLISION"


def test_output_dir_must_be_absolute(tmp_path: Path) -> None:
    try:
        resolve_output_dir("relative/path")
    except ValueError as exc:
        assert "absolute" in str(exc)
    else:
        raise AssertionError("Relative path should not be accepted")


def test_unresolved_case_is_queued_for_title_search(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.sqlite3")
    manager = JobManager(db)
    unresolved = request_case(1, "Mendiola v. Court of Appeals", "")
    unresolved["decision_date"] = ""
    ready = request_case(2, "Ready v. Case", "G.R. No. 123456")
    run_id = manager.create_run(
        {
            "input_source": "Syllabus.pdf",
            "output_dir": str(tmp_path / "output"),
            "cases": [unresolved, ready],
        }
    )
    run = db.get_run(run_id)
    assert run is not None
    assert run["cases"][0]["status"] == "Queued"
    assert run["cases"][1]["status"] == "Queued"


@pytest.mark.asyncio
async def test_download_workers_process_cases_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "history.sqlite3")
    manager = JobManager(db)
    run_id = manager.create_run(
        {
            "input_source": "Test",
            "output_dir": str(tmp_path / "output"),
            "cases": [
                request_case(index, f"Case {index} v. Respondent", f"G.R. No. 12345{index}")
                for index in range(1, 5)
            ],
        }
    )

    class FakeContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("app.jobs.CaseResolver", FakeContext)
    monkeypatch.setattr("app.jobs.CaseRetriever", FakeContext)
    active = 0
    peak = 0

    async def fake_process(_run_id, item, _output_dir, _retriever, _resolver):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        db.update_case(item["id"], status="Saved")
        active -= 1

    monkeypatch.setattr(manager, "_process_case", fake_process)
    await manager._process(run_id, asyncio.Event(), retry_only=False)
    assert peak == 4


@pytest.mark.asyncio
async def test_missing_metadata_is_resolved_during_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "history.sqlite3")
    manager = JobManager(db)
    unresolved = request_case(1, "Mendiola v. Court of Appeals", "")
    unresolved["decision_date"] = ""
    run_id = manager.create_run(
        {
            "input_source": "Syllabus.pdf",
            "output_dir": str(tmp_path / "output"),
            "cases": [unresolved],
        }
    )
    item = db.get_run(run_id)["cases"][0]

    class FakeResolver:
        async def candidates(self, _case):
            return [
                CaseCandidate(
                    "Mendiola v. Court of Appeals",
                    "G.R. No. 159333",
                    "2006-07-31",
                    "497 SCRA",
                    "Lawphil",
                    "https://lawphil.net/example",
                )
            ]

    class FakeRetriever:
        async def retrieve(self, _case, output_path, _status):
            output_path.write_bytes(b"%PDF-test")
            return RetrievalResult(
                True,
                source="Lawphil",
                source_url="https://lawphil.net/example",
                temp_path=str(output_path),
            )

    monkeypatch.setattr("app.jobs.validate_pdf", lambda *_args: PdfValidation(True, 1))
    await manager._process_case(
        run_id, item, tmp_path / "output", FakeRetriever(), FakeResolver()
    )
    saved = db.get_run(run_id)["cases"][0]
    assert saved["status"] == "Saved"
    assert saved["docket"] == "G.R. No. 159333"
    assert saved["decision_date"] == "2006-07-31"


@pytest.mark.asyncio
async def test_blocked_google_search_becomes_review_with_manual_link(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.sqlite3")
    manager = JobManager(db)
    unresolved = request_case(1, "Lopez v. Oroso", "")
    unresolved["decision_date"] = ""
    unresolved["raw_text"] = "Lopez v. Oroso February 28, 1958"
    run_id = manager.create_run(
        {
            "input_source": "Syllabus.pdf",
            "output_dir": str(tmp_path / "output"),
            "cases": [unresolved],
        }
    )
    item = db.get_run(run_id)["cases"][0]

    class BlockedResolver:
        async def candidates(self, case):
            query = case.raw_text or case.title
            raise GoogleSearchBlocked(
                "Google paused automated searching. Open this search manually or retry later.",
                google_search_url(query),
            )

    class UnusedRetriever:
        async def retrieve(self, *_args):
            raise AssertionError("an unresolved blocked case must not start downloading")

    await manager._process_case(
        run_id, item, tmp_path / "output", UnusedRetriever(), BlockedResolver()
    )
    reviewed = db.get_run(run_id)["cases"][0]
    assert reviewed["status"] == "Needs review"
    assert reviewed["error_code"] == "GOOGLE_SEARCH_BLOCKED"
    assert reviewed["manual_search_url"].startswith("https://www.google.com/search?")


@pytest.mark.asyncio
async def test_uncertain_candidate_also_offers_manual_google_search(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.sqlite3")
    manager = JobManager(db)
    unresolved = request_case(1, "Tumalad v. Vicencio", "")
    unresolved["decision_date"] = ""
    unresolved["reporter_citation"] = "122 SCRA 296"
    unresolved["raw_text"] = "Tumalad v. Vicencio 122 SCRA 296"
    run_id = manager.create_run(
        {
            "input_source": "Syllabus.pdf",
            "output_dir": str(tmp_path / "output"),
            "cases": [unresolved],
        }
    )
    item = db.get_run(run_id)["cases"][0]

    class UncertainResolver:
        async def candidates(self, _case):
            return [
                CaseCandidate(
                    "Makati Leasing v. Wearever Textile Mills",
                    "G.R. No. L-58469",
                    "1983-05-16",
                    "122 SCRA 296",
                    "Lawphil",
                    "https://lawphil.net/wrong-case",
                    score=100,
                    reasons=["Exact reporter citation"],
                    verified=True,
                )
            ]

    class UnusedRetriever:
        async def retrieve(self, *_args):
            raise AssertionError("an uncertain match must wait for user review")

    await manager._process_case(
        run_id, item, tmp_path / "output", UnusedRetriever(), UncertainResolver()
    )
    reviewed = db.get_run(run_id)["cases"][0]
    assert reviewed["status"] == "Needs review"
    assert reviewed["error_code"] == "MATCH_REVIEW_REQUIRED"
    assert reviewed["manual_search_url"].startswith("https://www.google.com/search?")
    assert reviewed["candidates"][0]["title"].startswith("Makati Leasing")


@pytest.mark.asyncio
async def test_complete_case_retries_verified_google_candidate_after_source_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "history.sqlite3")
    manager = JobManager(db)
    complete = request_case(1, "Prudential Bank v. Judge Panis", "G.R. No. 50008")
    complete["decision_date"] = "1988-08-31"
    run_id = manager.create_run(
        {
            "input_source": "Syllabus.pdf",
            "output_dir": str(tmp_path / "output"),
            "cases": [complete],
        }
    )
    item = db.get_run(run_id)["cases"][0]

    class GoogleResolver:
        async def candidates(self, _case):
            return [
                CaseCandidate(
                    "Prudential Bank v. Judge Panis",
                    "G.R. No. 50008",
                    "1988-08-31",
                    "",
                        "Lawphil",
                        "https://lawphil.net/judjuris/example.html",
                        score=110,
                        reasons=["Exact docket number"],
                        verified=True,
                )
            ]

    class RetryRetriever:
        def __init__(self):
            self.calls = 0

        async def retrieve(self, case, output_path, _status):
            self.calls += 1
            if self.calls == 1:
                return RetrievalResult(
                    False,
                    error_code="NOT_FOUND",
                    error_message="No matching decision was found.",
                )
            assert case.metadata_source_url.startswith("https://lawphil.net/")
            output_path.write_bytes(b"%PDF-test")
            return RetrievalResult(
                True,
                source="Lawphil",
                source_url=case.metadata_source_url,
                temp_path=str(output_path),
            )

    retriever = RetryRetriever()
    monkeypatch.setattr("app.jobs.validate_pdf", lambda *_args: PdfValidation(True, 1))
    await manager._process_case(
        run_id, item, tmp_path / "output", retriever, GoogleResolver()
    )
    saved = db.get_run(run_id)["cases"][0]
    assert retriever.calls == 2
    assert saved["status"] == "Saved"
    assert saved["metadata_source_url"].startswith("https://lawphil.net/")
