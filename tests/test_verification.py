from pathlib import Path
import pytest
from app.verification import heading_identity, verify_identity, title_matches, docket_ids
from app.pdf_utils import validate_pdf
from pdf_factory import identity_pdf

EXPECTED = {"title": "Alpha v. CA", "docket": "G.R. No. L-123456", "decision_date": "2000-01-02"}
HEADER = "G.R. No. L-123456 January 2, 2000 Alpha, Inc. v. Court of Appeals DECISION "

def test_heading_and_aliases_verify():
    assert verify_identity(EXPECTED, heading_identity(HEADER)).verified
    assert title_matches("Lions Club Int'l v. CA", "Lions Club International v. Court of Appeals")
    assert title_matches("Chung Ka Bio v. IAC", "Chung Ka Bio v. Intermediate Appellate Court")
    assert title_matches("Cebu Port Labor Union v. State Marine", "Cebu Port Labor Union v. States Marine Corporation")
    assert not title_matches("Alpha Manufacturing v. CA", "Alpha Holdings v. Court of Appeals")

@pytest.mark.parametrize("text", [
    HEADER.replace("L-123456", "L-9123456"),
    HEADER.replace("January 2", "January 3"),
    HEADER.replace("Alpha, Inc.", "Omega, Inc."),
    HEADER.replace("L-123456", "123456"),
    "G.R. No. 999999 January 3, 2000 Omega v. Sigma DECISION We cite " + HEADER,
    "G.R. No. 999999 January 3, 2000 Omega v. Sigma RESOLUTION We cite " + HEADER,
])
def test_wrong_and_cited_cases_rejected(text):
    assert not verify_identity(EXPECTED, heading_identity(text)).verified

def test_citation_requires_exact_reference_and_both_sides():
    expected = {"title": "Alpha v. CA", "reporter_citation": "20 SCRA 100"}
    assert verify_identity(expected, heading_identity(HEADER.replace(" DECISION", " 20 SCRA 100 DECISION"))).verified
    assert not verify_identity(expected, heading_identity(HEADER + "20 SCRA 100")).verified
    assert not verify_identity(expected, heading_identity(HEADER.replace(" DECISION", " 120 SCRA 1000 DECISION"))).verified
    assert not verify_identity({"title": "Alpha v. CA"}, heading_identity(HEADER)).verified

def test_all_consolidated_dockets_required():
    expected = {**EXPECTED, "docket": "G.R. Nos. L-123456 and L-654321"}
    assert not verify_identity(expected, heading_identity(HEADER)).verified
    assert verify_identity(expected, heading_identity(HEADER.replace("G.R. No. L-123456", "G.R. Nos. L-123456 and L-654321"))).verified


def test_shorthand_dockets_are_expanded_and_all_required():
    assert docket_ids("G.R. Nos. 219902 219903 & 219943-44") == {"219902", "219903", "219943", "219944"}
    assert docket_ids("G.R. Nos. L-81124-26") == {"L-81124", "L-81125", "L-81126"}
    expected = {**EXPECTED, "docket": "G.R. Nos. L-123456-57"}
    assert not verify_identity(expected, heading_identity(HEADER)).verified
    assert verify_identity(expected, heading_identity(HEADER.replace("G.R. No. L-123456", "G.R. Nos. L-123456 and L-123457"))).verified

@pytest.mark.parametrize("text", [HEADER.replace("January 2", "January 3"), HEADER.replace("L-123456", "L-1234567"), "G.R. No. 999999 January 2, 2000 Omega v. Sigma DECISION " + HEADER])
def test_final_pdf_identity_rejects_wrong_document(tmp_path, text):
    path = tmp_path / "case.pdf"
    path.write_bytes(identity_pdf(text))
    assert not validate_pdf(path, EXPECTED["docket"], EXPECTED["decision_date"], EXPECTED["title"]).valid

def test_final_pdf_accepts_verified_header(tmp_path):
    path = tmp_path / "case.pdf"
    path.write_bytes(identity_pdf(HEADER))
    result = validate_pdf(path, EXPECTED["docket"], EXPECTED["decision_date"], EXPECTED["title"])
    assert result.valid and result.verification.verified


def test_index_row_is_not_a_decision_heading():
    text = "G.R. No. L-123456 January 2, 2000 Alpha v. Court of Appeals"
    assert not verify_identity(EXPECTED, heading_identity(text)).verified


def test_navigation_date_cannot_replace_decision_date():
    text = "Today is October 10, 2026 " + HEADER
    actual = heading_identity(text)
    assert actual["decision_date"] == "2000-01-02"
    assert verify_identity(EXPECTED, actual).verified


def test_pdf_spacing_around_versus_preserves_party_identity():
    text = HEADER.replace("Alpha, Inc. v. Court", "Alpha, Inc.,petitioner,vs.Court")
    assert verify_identity(EXPECTED, heading_identity(text)).verified


def test_older_decision_with_syllabus_and_zero_padded_citation():
    text = HEADER.replace(" DECISION", " 051 Phil 115 SYLLABUS")
    expected = {"title": "Alpha v. CA", "reporter_citation": "51 Phil 115"}
    assert verify_identity(expected, heading_identity(text)).verified


def test_commission_abbreviation_and_compound_surname_spacing():
    assert title_matches("SEC v. Subic Bay Golf", "Securities and Exchange Commission v. Subic Bay Golf")
    assert title_matches("DENR Sec. v. Yap", "Secretary of the Department of Environment and Natural Resources v. Yap")
    assert title_matches("Facilities Management v. Dela Osa", "Facilities Management Corporation v. Leonardo De La Osa")
    assert not title_matches("SEC v. Subic Bay Golf", "Secretary v. Subic Bay Golf")


def test_synopsis_cannot_supply_identity():
    assert not verify_identity(EXPECTED, heading_identity(
        "G.R. No. 999999 January 2, 2000 Omega v. Sigma SYNOPSIS " + HEADER)).verified


def test_docket_between_party_sides_in_printed_heading():
    text = "SUPREME COURT EN BANC Alpha, Inc., petitioner -versus- G.R. No. L-123456 January 2, 2000 Court of Appeals, respondent DECISION"
    assert verify_identity(EXPECTED, heading_identity(text)).verified


@pytest.mark.parametrize("judge", ["CONCEPCION, J .:", "MAKASIAR, J:", "LEONEN, J.:"])
def test_old_judge_labels_establish_body_boundary(judge):
    text = HEADER.replace("DECISION", judge)
    assert verify_identity(EXPECTED, heading_identity(text)).verified
