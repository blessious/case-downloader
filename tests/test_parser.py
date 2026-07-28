from __future__ import annotations

import io

from docx import Document

from app.parser import extract_docx_text, parse_text


SAMPLE = """ASSIGNED CASES

WEEK 2
1. Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda, G.R. No. 145561, June 15, 2005
2. TMPCA v. NLRC, G.R. Nos. 158786 & 158789, October 19, 2007

WEEK 3
1. Duncans Association of Detailman - PTGWO vs Glaxo Wellcome, G.R. No. 162994, September 17, 2004
"""


def test_parse_numbered_week_list_preserves_assigned_title() -> None:
    cases = parse_text(SAMPLE)
    assert len(cases) == 3
    assert cases[0].week == "Week 2"
    assert cases[0].title == "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda"
    assert cases[0].docket == "G.R. No. 145561"
    assert cases[0].decision_date == "2005-06-15"
    assert cases[1].docket == "G.R. Nos. 158786 & 158789"
    assert cases[2].week == "Week 3"
    assert " v. " in cases[2].title
    assert not any(case.parse_error for case in cases)


def test_parse_wrapped_case_line() -> None:
    text = """WEEK 1
1. A Very Long Company Name v. Another Company,
G.R. No. 123456, January 2, 2020
"""
    cases = parse_text(text)
    assert len(cases) == 1
    assert cases[0].docket == "G.R. No. 123456"
    assert cases[0].decision_date == "2020-01-02"


def test_non_numbered_docx_paragraphs_remain_separate() -> None:
    document = Document()
    document.add_paragraph("WEEK 4")
    document.add_paragraph("First Case v. Second Case, G.R. No. 111111, March 4, 2019")
    document.add_paragraph("Third Case vs Fourth Case, G.R. No. 222222, April 5, 2020")
    stream = io.BytesIO()
    document.save(stream)
    text = extract_docx_text(stream.getvalue())
    cases = parse_text(text)
    assert len(cases) == 2
    assert [case.title for case in cases] == [
        "First Case v. Second Case",
        "Third Case v. Fourth Case",
    ]


def test_malformed_row_is_not_silently_dropped() -> None:
    cases = parse_text("WEEK 1\n1. Case without required information")
    assert len(cases) == 1
    assert "Missing or malformed docket number" in cases[0].parse_error
    assert "Missing or malformed decision date" in cases[0].parse_error


def test_l_number_is_supported() -> None:
    cases = parse_text(
        "1. PLDT v. NLRC, G.R. No. L-80609, August 23, 1988"
    )
    assert len(cases) == 1
    assert cases[0].docket == "G.R. No. L-80609"
    assert cases[0].decision_date == "1988-08-23"
