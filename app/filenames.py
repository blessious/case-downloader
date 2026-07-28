from __future__ import annotations

import re
from pathlib import Path

from .config import MAX_FILENAME_LENGTH


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
VERSUS = re.compile(r"(?<!\w)(?:v(?:s)?\.?)(?!\w)", re.IGNORECASE)
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def normalize_case_title(title: str) -> str:
    value = VERSUS.sub("v.", title.strip())
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,.;])", r"\1", value)
    value = re.sub(r"(?<!\s)v\.(?!\s)", "v. ", value)
    return value.strip(" ,")


def filename_for_title(title: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    clean = normalize_case_title(title)
    clean = INVALID_FILENAME_CHARS.sub("-", clean)
    clean = re.sub(r"\s+", " ", clean).strip().rstrip(". ")
    if not clean:
        clean = "Untitled case"
        warnings.append("Empty title replaced with Untitled case")
    if clean.upper() in WINDOWS_RESERVED:
        clean = f"_{clean}"
        warnings.append("Reserved system filename was prefixed")
    max_stem = MAX_FILENAME_LENGTH - len(".pdf")
    if len(clean) > max_stem:
        clean = clean[:max_stem].rstrip(". ")
        warnings.append("Filename was shortened to remain cross-platform compatible")
    return f"{clean}.pdf", warnings


def safe_week_folder(week: str) -> str:
    match = re.fullmatch(r"\s*week\s*(\d+)\s*", week, re.IGNORECASE)
    if match:
        return f"Week {int(match.group(1))}"
    return "Uncategorized"


def path_too_long(path: Path) -> bool:
    # The conservative limit avoids legacy Windows APIs while remaining safe on macOS.
    return len(str(path.resolve(strict=False))) > 240
