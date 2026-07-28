from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from .parser import docket_tokens


@dataclass(slots=True)
class PdfValidation:
    valid: bool
    page_count: int = 0
    error_code: str = ""
    error_message: str = ""
    warnings: list[str] = field(default_factory=list)


def validate_pdf(path: Path, docket: str = "", decision_date: str = "") -> PdfValidation:
    try:
        if not path.exists():
            return PdfValidation(False, error_code="PDF_MISSING", error_message="PDF file does not exist")
        if path.stat().st_size < 128:
            return PdfValidation(False, error_code="PDF_TOO_SMALL", error_message="PDF file is empty or truncated")
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return PdfValidation(False, error_code="INVALID_PDF_SIGNATURE", error_message="Downloaded content is not a PDF")
        reader = PdfReader(str(path), strict=False)
        if len(reader.pages) < 1:
            return PdfValidation(False, error_code="PDF_NO_PAGES", error_message="PDF contains no readable pages")
        extracted: list[str] = []
        for page in reader.pages[:5]:
            try:
                extracted.append(page.extract_text() or "")
            except Exception:
                continue
        text = re.sub(r"\s+", "", " ".join(extracted)).upper()
        warnings: list[str] = []
        expected = docket_tokens(docket)
        if text and expected and not any(token in text for token in expected):
            return PdfValidation(
                False,
                page_count=len(reader.pages),
                error_code="PDF_IDENTITY_MISMATCH",
                error_message="PDF text does not contain the expected docket number",
            )
        if not text:
            warnings.append("PDF has no extractable text; identity could not be verified automatically")
        return PdfValidation(True, page_count=len(reader.pages), warnings=warnings)
    except Exception as exc:
        return PdfValidation(False, error_code="PDF_UNREADABLE", error_message=str(exc))
