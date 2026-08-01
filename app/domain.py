from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


@dataclass(slots=True)
class ParsedCase:
    ordinal: int
    week: str
    title: str
    docket: str
    decision_date: str
    raw_text: str
    parse_error: str = ""
    warnings: list[str] = field(default_factory=list)
    reporter_citation: str = ""
    metadata_source: str = ""
    metadata_source_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RetrievalAttempt:
    source: str
    url: str
    attempt_no: int
    status: str
    error_code: str = ""
    error_message: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass(slots=True)
class RetrievalResult:
    success: bool
    source: str = ""
    source_url: str = ""
    temp_path: str = ""
    error_code: str = ""
    error_message: str = ""
    attempts: list[RetrievalAttempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


FINAL_STATUSES = {
    "Saved",
    "Already saved",
    "Not saved",
    "Duplicate input",
    "Cancelled",
    "Needs review",
}


def iso_today() -> str:
    return date.today().isoformat()
