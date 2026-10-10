from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter

from app.pdf_utils import validate_pdf


def write_blank_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)


def test_pdf_with_unextractable_identity_requires_review(tmp_path: Path) -> None:
    path = tmp_path / "case.pdf"
    write_blank_pdf(path)
    result = validate_pdf(path, "G.R. No. 145561", "2005-06-15")
    assert not result.valid
    assert result.page_count == 1
    assert result.error_code == "PDF_IDENTITY_UNVERIFIED"


def test_html_masquerading_as_pdf_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.pdf"
    path.write_text("<html>Access denied</html>", encoding="utf-8")
    result = validate_pdf(path)
    assert not result.valid
    assert result.error_code in {"PDF_TOO_SMALL", "INVALID_PDF_SIGNATURE"}
