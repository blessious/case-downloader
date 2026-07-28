from __future__ import annotations

import io
import re
from datetime import datetime

from docx import Document

from .domain import ParsedCase
from .filenames import normalize_case_title


MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
DATE_RE = re.compile(
    rf"(?P<date>(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}})\s*\.?\s*$",
    re.IGNORECASE,
)
WEEK_RE = re.compile(r"^\s*WEEK\s*(?P<number>\d+)\s*:?\s*$", re.IGNORECASE)
LIST_PREFIX_RE = re.compile(r"^\s*(?:\d+[\.\)]|[-•])\s+")
DOCKET_START_RE = re.compile(
    r"(?<![A-Za-z])(?:G\.?\s*R\.?\s*(?:Nos?\.?)?|G\.?\s*R\.?|L-)\s*",
    re.IGNORECASE,
)
DOCKET_TOKEN_RE = re.compile(
    r"(?:G\.?\s*R\.?\s*(?:Nos?\.?)?\s*)?"
    r"(?:L-)?\d{3,}(?:\s*(?:,|&|and|/|-)\s*(?:L-)?\d{3,})*",
    re.IGNORECASE,
)


def _logical_lines(text: str) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            if current:
                logical.append(current)
                current = ""
            continue
        if WEEK_RE.match(line):
            if current:
                logical.append(current)
                current = ""
            logical.append(line)
            continue
        complete_case = bool(DATE_RE.search(line) and DOCKET_START_RE.search(line))
        if complete_case and not LIST_PREFIX_RE.match(line):
            if current:
                logical.append(f"{current} {line}")
                current = ""
            else:
                logical.append(line)
            continue
        if LIST_PREFIX_RE.match(line):
            if current:
                logical.append(current)
            current = LIST_PREFIX_RE.sub("", line, count=1).strip()
        elif current:
            current = f"{current} {line}"
        else:
            current = line
    if current:
        logical.append(current)
    return logical


def _parse_case_line(line: str, ordinal: int, week: str) -> ParsedCase:
    warnings: list[str] = []
    date_match = DATE_RE.search(line)
    decision_date = ""
    before_date = line
    if date_match:
        raw_date = date_match.group("date")
        try:
            decision_date = datetime.strptime(raw_date, "%B %d, %Y").date().isoformat()
        except ValueError:
            warnings.append(f"Unrecognized date: {raw_date}")
        before_date = line[: date_match.start()].rstrip(" ,")

    docket_match = DOCKET_START_RE.search(before_date)
    title = before_date
    docket = ""
    if docket_match:
        title = before_date[: docket_match.start()].rstrip(" ,")
        docket = before_date[docket_match.start() :].strip(" ,")
    else:
        token_matches = list(DOCKET_TOKEN_RE.finditer(before_date))
        if token_matches:
            candidate = token_matches[-1]
            prefix = before_date[max(0, candidate.start() - 8) : candidate.start()]
            if "G.R" in prefix.upper() or "G R" in prefix.upper():
                title = before_date[: candidate.start()].rstrip(" ,")
                docket = before_date[candidate.start() :].strip(" ,")

    title = normalize_case_title(title)
    errors: list[str] = []
    if not title:
        errors.append("Missing case title")
    if not docket:
        errors.append("Missing or malformed docket number")
    if not decision_date:
        errors.append("Missing or malformed decision date")
    return ParsedCase(
        ordinal=ordinal,
        week=week,
        title=title,
        docket=docket,
        decision_date=decision_date,
        raw_text=line,
        parse_error="; ".join(errors),
        warnings=warnings,
    )


def parse_text(text: str) -> list[ParsedCase]:
    cases: list[ParsedCase] = []
    week = "Uncategorized"
    for line in _logical_lines(text):
        week_match = WEEK_RE.match(line)
        if week_match:
            week = f"Week {int(week_match.group('number'))}"
            continue
        if re.fullmatch(r"(?:ASSIGNED\s+)?CASES?", line, re.IGNORECASE):
            continue
        cases.append(_parse_case_line(line, len(cases) + 1, week))
    return cases


def extract_docx_text(data: bytes) -> str:
    document = Document(io.BytesIO(data))
    parts: list[str] = []
    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            parts.append(paragraph.text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" ".join(cells))
    return "\n".join(parts)


def docket_tokens(docket: str) -> list[str]:
    tokens = re.findall(r"(?:L-)?\d{3,}", docket.upper())
    return [token.replace("L-", "") for token in tokens]
