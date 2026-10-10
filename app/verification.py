"""Conservative identity verification shared by discovery and PDF publication."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from bs4 import BeautifulSoup
from .parser import DATE_RE, parse_date

VERIFIER_VERSION = "5"
SOURCE_HOSTS = {"chanrobles.com", "www.chanrobles.com", "lawphil.net", "www.lawphil.net", "elibrary.judiciary.gov.ph"}

@dataclass
class VerificationResult:
    outcome: str = "insufficient_evidence"
    evidence: list[str] = field(default_factory=list)
    source_url: str = ""
    verifier_version: str = VERIFIER_VERSION
    message: str = "Identity could not be established from the decision heading"
    expected_identity: dict[str, str] = field(default_factory=dict)
    actual_identity: dict[str, str] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.outcome == "verified"

    def as_dict(self):
        return asdict(self)

ALIASES = {
    "clubs": "club", "states": "state", "ca": "court of appeals", "iac": "intermediate appellate court",
    "nlrc": "national labor relations commission", "lrc": "land registration commission",
    "phils": "philippines", "phil": "philippine",
    "pnb": "philippine national bank", "cfi": "court of first instance",
    "gov t": "government", "govt": "government", "estates": "estate",
    "intl": "international", "int l": "international", "sec": "secretary",
    "denr": "department of environment and natural resources",
}
STOPWORDS = {"the", "of", "and", "et", "al", "inc", "incorporated", "co", "company", "corp", "corporation", "petitioner", "petitioners", "respondent", "respondents", "honorable"}

def normalize_party(value: str) -> list[str]:
    # SEC alone names the commission; "DENR Sec." names a secretary.
    if re.fullmatch(r"\s*SEC\.?\s*", value, re.I):
        value = "Securities and Exchange Commission"
    value = re.sub(r"\bde\s*la\b", "de la", value, flags=re.I)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    for short, expanded in ALIASES.items():
        value = re.sub(r"\b" + re.escape(short) + r"\b", expanded, value)
    return [t for t in value.split() if t not in STOPWORDS and len(t) > 1]

def party_sides(value: str):
    sides = re.split(r"\b(?:versus(?:\s+|-)|vs?\.\s*|vs?\s+)", value, maxsplit=1, flags=re.I)
    return [normalize_party(s) for s in sides] if len(sides) == 2 else []

def title_matches(expected: str, actual: str) -> bool:
    left, right = party_sides(expected), party_sides(actual)
    if not left or not right:
        return False
    # Every substantive expected token must occur on its corresponding side.
    return all(a and set(a).issubset(set(b)) for a, b in zip(left, right))

DOCKET_RE = re.compile(r"\bG\.?\s*R\.?\s*(?:Nos?\.?|#)?\s*[-:]?\s*((?:L\s*-?\s*)?\d{3,}(?:\s*-\s*\d{1,6})?(?:\s*(?:,|&|and|/|to|\s+(?=\d{5,}))\s*(?:L\s*-?\s*)?\d{3,}(?:\s*-\s*\d{1,6})?)*)", re.I)

def docket_ids(value: str) -> set[str]:
    matches = list(DOCKET_RE.finditer(value))
    values = [m.group(1) for m in matches] if matches else [value] if re.fullmatch(r"\s*(?:L\s*-?\s*)?\d{3,}\s*", value, re.I) else []
    ids = set()
    for value in values:
        for match in re.finditer(r"(?P<prefix>L\s*-?\s*)?(?P<start>\d{3,})(?:\s*-\s*(?P<end>\d{1,6}))?", value, re.I):
            prefix = "L-" if match.group("prefix") else ""
            start = match.group("start")
            ids.add(prefix + start)
            end = match.group("end")
            if end:
                expanded = start[:-len(end)] + end if len(end) < len(start) else end
                if 0 <= int(expanded) - int(start) <= 100:
                    ids.update(prefix + str(number) for number in range(int(start), int(expanded) + 1))
                else:
                    # Retain an unmatchable range marker rather than silently
                    # discarding requested dockets in a malformed/large range.
                    ids.add(prefix + start + "-" + end)
    return ids

def reporter_ids(value: str):
    return {(str(int(a)), b.upper().rstrip("."), str(int(c))) for a,b,c in re.findall(r"\b(\d{1,4})\s*(SCRA|PHIL\.?)\s*(\d{1,4})\b", value, re.I)}

def heading_text(content: str) -> str:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup.select("script, style, nav, footer, header, .footnotes"):
        tag.decompose()
    container = soup.select_one(".mainContent > .content, #content, .judgment") or soup
    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
    # Stop before the opinion, rather than treating cited precedents as identity.
    boundary = re.search(r"\b(?:D\s*E\s*C\s*I\s*S\s*I\s*O\s*N|R\s*E\s*S\s*O\s*L\s*U\s*T\s*I\s*O\s*N|SYLLABUS|SYNOPSIS)\b|\b[A-Z][A-Z ,.-]{2,40},\s*(?:C\s*\.?\s*J|J)\s*\.?\s*:|\b(?:This is|Before us|The petition|We resolve|We decide)\b", text)
    if not boundary:
        return ""
    text = text[:boundary.start()]
    return text[:2500]

def heading_identity(content: str) -> dict[str, str]:
    text = heading_text(content)
    dockets = list(DOCKET_RE.finditer(text))
    dates = list(DATE_RE.finditer(text))
    # Ignore a site's current date and other navigation dates before the
    # decision label. Pair the date with the last repeated heading docket.
    following = [d for d in dates if dockets and 0 <= d.start() - dockets[-1].end() <= 350]
    preceding = [d for d in dates if dockets and 0 <= dockets[0].start() - d.end() <= 350]
    date = following[0] if following else preceding[-1] if preceding else None
    if not dockets or not date:
        return {"title": "", "docket": "", "decision_date": "", "reporter_citation": "", "heading": text}
    start = max(dockets[-1].end(), date.end())
    caption = text[start:]
    if not party_sides(caption):
        caption = text[:dockets[0].start()]
    if not all(party_sides(caption)) or not party_sides(caption):
        # Printed two-column headings can put docket/date between the party
        # sides. Remove only those heading labels, retaining both sides.
        caption = DOCKET_RE.sub(" ", text)
        caption = DATE_RE.sub(" ", caption)
    caption = re.sub(r"\b\d{1,4}\s*(?:SCRA|PHIL\.?)\s*\d{1,4}\b", "", caption, flags=re.I)
    return {"title": caption.strip(" ,;[]"), "docket": "; ".join(m.group() for m in dockets), "decision_date": parse_date(date.group("date")), "reporter_citation": " ".join(m.group() for m in re.finditer(r"\b\d{1,4}\s*(?:SCRA|PHIL\.?)\s*\d{1,4}\b", text, re.I)), "heading": text}

def verify_identity(expected, actual, source_url: str = "") -> VerificationResult:
    result = VerificationResult(source_url=source_url)
    def get(obj, key):
        return str(obj.get(key, "") if isinstance(obj, dict) else getattr(obj, key, ""))
    keys = ("title", "docket", "decision_date", "reporter_citation")
    result.expected_identity = {key: get(expected, key) for key in keys}
    result.actual_identity = {key: get(actual, key) for key in keys}
    for key, normalize in (("docket", docket_ids), ("reporter_citation", reporter_ids)):
        wanted, found = normalize(get(expected, key)), normalize(get(actual, key))
        comparable = ({citation for citation in found if citation[1] in {item[1] for item in wanted}}
                      if key == "reporter_citation" else found)
        if wanted and comparable and not wanted.issubset(comparable):
            result.outcome = "conflicting_evidence"
            result.message = f"Decision heading conflicts with supplied {key.replace('_', ' ')}"
            return result
        if wanted and not wanted.issubset(found):
            result.message = f"Decision heading does not establish supplied {key.replace('_', ' ')}"
            return result
        if wanted:
            result.evidence.append(f"Exact {key.replace('_', ' ')}: {get(expected, key)}")
    wanted_date, found_date = get(expected, "decision_date"), get(actual, "decision_date")
    if wanted_date and wanted_date != found_date:
        result.outcome = "conflicting_evidence" if found_date else "insufficient_evidence"
        result.message = "Decision heading does not match the supplied decision date"
        return result
    parties_match = title_matches(get(expected, "title"), get(actual, "title"))
    if get(expected, "title") and not party_sides(get(actual, "title")):
        result.message = "Decision heading does not establish both party sides"
        return result
    if get(expected, "title") and not parties_match:
        result.outcome = "conflicting_evidence"
        result.message = "Decision heading does not match both supplied party sides"
        return result
    strong_docket = bool(docket_ids(get(expected, "docket")) and wanted_date)
    strong_reporter = bool(reporter_ids(get(expected, "reporter_citation")) and parties_match)
    if not (strong_docket or strong_reporter) or not docket_ids(get(actual, "docket")) or not found_date:
        result.message = "An exact docket and date, or exact reporter citation and both party sides, is required"
        return result
    result.outcome = "verified"
    result.message = "Decision identity verified"
    result.evidence.append(f"Decision date: {found_date}")
    if parties_match:
        result.evidence.append("Both party sides verified")
    return result
