from __future__ import annotations

from pathlib import Path

from app.filenames import filename_for_title, path_too_long, safe_week_folder


def test_requested_title_only_filename() -> None:
    filename, warnings = filename_for_title(
        "Honda Philippines, Inc. vs Samahan ng Malalaya sa Honda"
    )
    assert filename == "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda.pdf"
    assert warnings == []


def test_cross_platform_invalid_characters_are_replaced() -> None:
    filename, _ = filename_for_title('People: "A/B" v. C?*')
    assert filename == "People- -A-B- v. C--.pdf"
    assert not any(char in filename for char in '<>:"/\\|?*')


def test_reserved_name_and_length() -> None:
    filename, warnings = filename_for_title("CON")
    assert filename == "_CON.pdf"
    assert warnings
    long_name, long_warnings = filename_for_title("A" * 250)
    assert len(long_name) <= 180
    assert long_warnings


def test_week_folder_and_path_limit() -> None:
    assert safe_week_folder("week 02") == "Week 2"
    assert safe_week_folder("Section A") == "Uncategorized"
    assert path_too_long(Path("C:/") / ("a" * 250))
