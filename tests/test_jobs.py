from __future__ import annotations

from pathlib import Path

from app.database import Database
from app.jobs import JobManager, resolve_output_dir


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
