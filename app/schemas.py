from __future__ import annotations

from pydantic import BaseModel, Field


class TextParseRequest(BaseModel):
    text: str = Field(min_length=1)


class FolderRequest(BaseModel):
    path: str = Field(min_length=1)
    create: bool = True


class CaseRequest(BaseModel):
    ordinal: int = Field(ge=1)
    week: str = "Uncategorized"
    title: str
    docket: str
    decision_date: str
    raw_text: str = ""
    parse_error: str = ""
    warnings: list[str] = Field(default_factory=list)


class RunRequest(BaseModel):
    input_source: str = "Pasted text"
    output_dir: str
    cases: list[CaseRequest] = Field(min_length=1)
