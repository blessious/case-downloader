from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from .verification import VerificationResult, heading_identity, verify_identity


@dataclass(slots=True)
class PdfValidation:
    valid: bool
    page_count: int = 0
    error_code: str = ""
    error_message: str = ""
    warnings: list[str] = field(default_factory=list)
    verification: VerificationResult = field(default_factory=VerificationResult)


def validate_pdf(path: Path, docket: str = "", decision_date: str = "", title: str = "", source_url: str = "") -> PdfValidation:
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
        text = " ".join(extracted)
        verification = verify_identity(
            {"docket": docket, "decision_date": decision_date, "title": title},
            heading_identity(text), source_url,
        )
        if not verification.verified:
            return PdfValidation(False, page_count=len(reader.pages),
                error_code="PDF_IDENTITY_MISMATCH" if verification.outcome == "conflicting_evidence" else "PDF_IDENTITY_UNVERIFIED",
                error_message=verification.message, verification=verification)
        return PdfValidation(True, page_count=len(reader.pages), verification=verification)

    except Exception as exc:
        return PdfValidation(False, error_code="PDF_UNREADABLE", error_message=str(exc))
