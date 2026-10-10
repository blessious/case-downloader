from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from .domain import ParsedCase
from .filenames import normalize_case_title
from .network import trusted_ssl_context, source_request
from .parser import DATE_RE, REPORTER_RE, docket_tokens, parse_date
from .verification import heading_identity, verify_identity, title_matches, docket_ids, normalize_party, party_sides, DOCKET_RE, reporter_ids, VERIFIER_VERSION

if TYPE_CHECKING:
    from .database import Database


LAWPHIL_SEARCH_URL = "https://lawphil.net/api/public/jurisprudence"
GOOGLE_SEARCH_URL = "https://www.google.com/search"
APPROVED_SOURCE_HOSTS = {
    "lawphil.net": "Lawphil",
    "www.lawphil.net": "Lawphil",
    "elibrary.judiciary.gov.ph": "Supreme Court E-Library",
    "chanrobles.com": "ChanRobles",
    "www.chanrobles.com": "ChanRobles",
}

PageFetcher = Callable[[str, str], Awaitable[tuple[str, str]]]
GoogleSearcher = Callable[[str], Awaitable[list[dict[str, str]]]]


class WebSearchError(RuntimeError):
    pass


@dataclass(slots=True)
class CaseCandidate:
    title: str
    docket: str
    decision_date: str
    reporter_citation: str
    source: str
    source_url: str
    score: float = 0
    reasons: list[str] = field(default_factory=list)
    verified: bool = False
    verification: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GoogleSearchBlocked(WebSearchError):
    def __init__(
        self,
        message: str,
        manual_search_url: str,
        candidates: list[CaseCandidate] | None = None,
    ):
        super().__init__(message)
        self.manual_search_url = manual_search_url
        self.candidates = candidates or []


def google_search_url(query: str) -> str:
    return f"{GOOGLE_SEARCH_URL}?q={quote_plus(query[:400])}&num=10&hl=en&gl=ph&pws=0"


def _unwrap_google_url(value: str) -> str:
    absolute = urljoin("https://www.google.com", value)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").casefold()
    if host.endswith("google.com") and parsed.path == "/url":
        parameters = parse_qs(parsed.query)
        absolute = unquote((parameters.get("q") or parameters.get("url") or [""])[0])
        parsed = urlparse(absolute)
        host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or host.endswith("google.com"):
        return ""
    return absolute


def parse_google_results(html: str) -> list[dict[str, str]]:
    """Extract ordinary Google result cards without trusting page instructions."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for heading in soup.select("a h3"):
        anchor = heading.find_parent("a", href=True)
        if not anchor:
            continue
        target = _unwrap_google_url(str(anchor.get("href") or ""))
        if not target or target in seen:
            continue
        seen.add(target)
        container = anchor.find_parent("div")
        description = container.get_text(" ", strip=True) if container else ""
        title = heading.get_text(" ", strip=True)
        results.append(
            {
                "title": title,
                "url": target,
                "description": description[:2_000],
            }
        )
        if len(results) == 10:
            break
    return results


def _google_is_blocked(html: str, url: str) -> bool:
    evidence = f"{url} {html[:200_000]}".casefold()
    return any(
        indicator in evidence
        for indicator in (
            "/sorry/",
            "unusual traffic",
            "our systems have detected",
            "g-recaptcha",
            "captcha-form",
            "not a robot",
        )
    )


def _compact_title(value: str) -> str:
    normalized = normalize_case_title(value)
    ascii_value = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


_PARTY_STOPWORDS = {
    "and", "company", "co", "corporation", "corp", "inc", "the", "honorable",
    "et", "al", "of", "spouses", "sp", "case", "cases",
}


def _party_tokens(value: str) -> set[str]:
    return {
        token
        for token in _compact_title(value).split()
        if len(token) > 1 and token not in _PARTY_STOPWORDS
    }


def _title_matches(expected: str, actual: str) -> bool:
    return title_matches(expected, actual)


def _party_sides_found(title: str, evidence: str) -> bool:
    return title_matches(title, heading_identity(evidence)["title"])


def _title_search_query(title: str) -> str:
    return " ".join(normalize_party(title))


def _party_search_queries(title: str) -> list[str]:
    """Search contiguous party phrases, retaining punctuation used by source indexes.

    A full normalized caption is often absent from an index whose caption includes
    additional parties. These queries discover candidates only; they confer no proof.
    """
    sides = re.split(r"\b(?:versus\s+|vs?\.\s*|vs?\s+)", title, maxsplit=1, flags=re.I)
    if len(sides) != 2:
        return []
    queries = []
    for side in sides:
        phrase = side.strip(" ,.;")
        if len(normalize_party(phrase)) >= 2:
            queries.append(phrase)
            normalized = " ".join(normalize_party(phrase))
            if normalized.casefold() != phrase.casefold():
                queries.append(normalized)
        # A distinctive surname or business word also finds captions with
        # intervening qualifiers. Common institutional words are too broad.
        broad = {"court", "appeals", "commission", "national", "government",
                 "republic", "philippine", "philippines", "international",
                 "department", "labor", "relations", "first", "instance"}
        broad.update({"enterprises", "industries", "management", "services"})
        tokens = [t for t in normalize_party(phrase) if len(t) >= 5 and t not in broad]
        queries.extend(sorted(tokens, key=len, reverse=True)[:1])
    return list(dict.fromkeys(queries))


def _plausible_party_hint(expected: str, actual: str) -> bool:
    """Loose index filtering only; the fetched heading must still pass verification."""
    left, right = party_sides(expected), party_sides(actual)
    return bool(left and right and all(set(a).intersection(b) for a, b in zip(left, right)))


def _reporter_keys(value: str) -> set[tuple[str, str, str]]:
    return {
        (str(int(match.group(1))), match.group(2).casefold().rstrip("."), str(int(match.group(3))))
        for match in re.finditer(
            r"\b(\d{1,4})\s*(SCRA|PHIL\.?)\s*(\d{1,4})\b",
            value,
            re.IGNORECASE,
        )
    }


def _reporter_matches(expected: str, actual: str) -> bool:
    expected_keys = _reporter_keys(expected)
    return bool(expected_keys and expected_keys.intersection(_reporter_keys(actual)))


def _reporter_volume_keys(value: str) -> set[tuple[str, str]]:
    return {(str(int(n)), kind.upper().rstrip(".")) for n, kind in
            re.findall(r"\b(\d{1,4})\s*(SCRA|PHIL\.?)\b", value, re.I)}


def _normalized_clue(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def lookup_keys(case: ParsedCase) -> list[str]:
    values: list[tuple[str, str]] = []
    docket = ":".join(docket_tokens(case.docket))
    if docket:
        values.append(("docket", docket))
    if case.reporter_citation:
        values.append(("reporter", _normalized_clue(case.reporter_citation)))
    if case.title:
        title_key = _compact_title(case.title)
        values.append(("title-date", f"{title_key}:{case.decision_date}"))
        values.append(("title", title_key))
    return [f"{kind}:{value}" for kind, value in values if value]


def _candidate_from_lawphil(row: dict[str, Any]) -> CaseCandidate:
    citation_html = str(row.get("citation") or "")
    title = BeautifulSoup(citation_html, "html.parser").get_text(" ", strip=True)
    date_value = str(row.get("date") or "")
    return CaseCandidate(
        title=normalize_case_title(title),
        docket=str(row.get("gr_number") or row.get("am_number") or "").strip(),
        decision_date=date_value[:10] if len(date_value) >= 10 else "",
        reporter_citation=re.sub(r"\s+", " ", str(row.get("reference") or "")).strip(),
        source="Lawphil",
        source_url=urljoin("https://lawphil.net/judjuris/", str(row.get("url") or "")),
        verified=True,
    )


def _source_for_url(url: str) -> str:
    return APPROVED_SOURCE_HOSTS.get((urlparse(url).hostname or "").casefold(), "")


def _extract_docket(text: str) -> str:
    match = re.search(
        r"\bG\.?\s*R\.?\s*(?:Nos?\.?)?\s*((?:L-)?\d{3,})|\bL-(\d{3,})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    value = match.group(1) or f"L-{match.group(2)}"
    return f"G.R. No. {value.upper()}"


def _extract_date(text: str) -> str:
    match = DATE_RE.search(text)
    return parse_date(match.group("date")) if match else ""


def _extract_reporter(text: str) -> str:
    match = REPORTER_RE.search(text)
    return re.sub(r"\s+", " ", match.group("citation")).strip() if match else ""


def _year_clue(case: ParsedCase) -> str:
    if case.decision_date:
        return case.decision_date[:4]
    matches = re.findall(r"\b(?:18|19|20)\d{2}\b", case.raw_text)
    return matches[-1] if matches else ""


def _score_candidate(
    case: ParsedCase,
    candidate: CaseCandidate,
    evidence: str,
    *,
    exact_query_kind: str = "",
) -> CaseCandidate | None:
    score = 0.0
    reasons: list[str] = []
    expected_docket = docket_tokens(case.docket)
    candidate_docket = docket_tokens(candidate.docket)
    if expected_docket:
        # Consolidated decisions often list extra dockets. An exact requested
        # identifier remains authoritative when it is one of those dockets.
        if not candidate_docket or not all(
            token in candidate_docket for token in expected_docket
        ):
            return None
        score += 100
        reasons.append("Exact docket number")
    elif candidate_docket:
        score += 15
        reasons.append("Decision docket found")

    reporter_evidence = candidate.reporter_citation or evidence
    if case.reporter_citation and _reporter_matches(
        case.reporter_citation, reporter_evidence
    ):
        score += 75
        reasons.append("Exact reporter citation")
    elif case.reporter_citation and exact_query_kind == "reporter":
        score += 15
        reasons.append("Discovered from exact citation search")

    if _title_matches(case.title, candidate.title) or _party_sides_found(case.title, evidence):
        score += 45
        reasons.append("Both case sides matched")

    if case.decision_date and candidate.decision_date:
        if case.decision_date != candidate.decision_date:
            return None
        score += 25
        reasons.append("Exact decision date")
    elif case.decision_date and case.decision_date[:4] in evidence:
        score += 15
        reasons.append("Decision year matched")
    elif not case.decision_date and _year_clue(case) and _year_clue(case) in evidence:
        score += 15
        reasons.append("Syllabus year matched")

    if candidate.source in set(APPROVED_SOURCE_HOSTS.values()):
        score += 10
        reasons.append("Approved legal source")

    candidate.score = score
    candidate.reasons = list(dict.fromkeys(reasons))
    return candidate if score >= 35 else None


def candidates_are_ambiguous(candidates: list[CaseCandidate]) -> bool:
    identities = {(tuple(sorted(docket_ids(c.docket))), c.decision_date)
                  for c in candidates if candidate_is_high_confidence(c)}
    return len(identities) > 1


def candidate_is_high_confidence(candidate: CaseCandidate) -> bool:
    return bool(candidate.verified and candidate.verification.get("outcome") == "verified")


def verify_concordant_mirrors(case: ParsedCase, candidates: list[CaseCandidate]) -> None:
    """Recover missing reporter pages through fresh, independent source headings.

    This cannot rescue a conflicting identifier or party name. A fresh structured
    index must establish the requested reporter volume; a missing page remains
    explicitly disclosed in the evidence rather than being invented.
    """
    wanted = reporter_ids(case.reporter_citation)
    if not wanted:
        return
    groups: dict[tuple, list[CaseCandidate]] = {}
    for candidate in candidates:
        proof = candidate.verification
        actual = proof.get("actual_identity", {})
        if not actual or not docket_ids(actual.get("docket", "")) or not actual.get("decision_date"):
            continue
        key = (tuple(sorted(docket_ids(actual["docket"]))), actual["decision_date"])
        groups.setdefault(key, []).append(candidate)
    for mirrors in groups.values():
        usable = []
        volumes = set()
        conflict = False
        for candidate in mirrors:
            proof = candidate.verification
            actual = proof["actual_identity"]
            if proof.get("outcome") == "conflicting_evidence":
                conflict = True
                break
            found = {item for item in reporter_ids(actual.get("reporter_citation", ""))
                     if item[1] in {expected[1] for expected in wanted}}
            if found and not wanted.issubset(found):
                conflict = True
                break
            # Substitute only the absent citation requirement, never supplied
            # docket/date/parties, then verify the full fetched heading.
            expected = {"title": case.title, "docket": case.docket or actual["docket"],
                        "decision_date": case.decision_date or actual["decision_date"]}
            if not verify_identity(expected, actual, candidate.source_url).verified:
                continue
            usable.append(candidate)
            reference = proof.get("source_reference", {})
            if reference.get("source") == "Lawphil":
                volumes.update(_reporter_volume_keys(reference.get("reporter_citation", "")))
        required_volumes = {(n, kind) for n, kind, _page in wanted}
        if conflict or len({c.source for c in usable}) < 2 or not required_volumes.issubset(volumes):
            continue
        evidence = [{"source": c.source, "source_url": c.source_url,
                     "identity": c.verification["actual_identity"]} for c in usable]
        references = [c.verification["source_reference"] for c in usable if c.verification.get("source_reference")]
        for candidate in usable:
            candidate.verification.update(
                outcome="verified", verifier_version=VERIFIER_VERSION,
                message="Case identity independently confirmed by two approved sources; exact reporter page unavailable",
                verification_method="concordant_source_headings",
                reporter_page_verified=False, mirror_evidence=evidence, source_references=references,
                evidence=["Same exact docket and decision date in independent source headings",
                          "Both supplied party sides match each source heading",
                          f"Fresh structured source index confirms reporter volume: {case.reporter_citation}",
                          "Exact reporter page is unavailable; no conflicting citation found"])
            candidate.verified = True


class CaseResolver:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        database: Database | None = None,
        google_searcher: GoogleSearcher | None = None,
    ):
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=True, verify=trusted_ssl_context()
        )
        self.database = database
        self._google_semaphore = asyncio.Semaphore(1)
        self._google_searcher = google_searcher
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._google_context: BrowserContext | None = None
        self._google_blocked = False

    async def __aenter__(self) -> "CaseResolver":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._google_context:
            await self._google_context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        if self._owns_client:
            await self.client.aclose()

    async def _search_lawphil(self, query: str) -> list[CaseCandidate]:
        unique: dict[str, CaseCandidate] = {}
        for page in range(1, 5):
            response = await source_request(self.client, "GET", LAWPHIL_SEARCH_URL,
                params={"rows": 25, "sort": "newest", "search": query, "page": page})
            response.raise_for_status()
            rows = response.json().get("data", [])
            added = 0
            for row in rows:
                candidate = _candidate_from_lawphil(row)
                if candidate.source_url not in unique:
                    added += 1
                unique[candidate.source_url] = candidate
            if len(rows) < 25 or not added:
                break
        return list(unique.values())

    async def _ensure_google_context(self) -> BrowserContext:
        if self._google_context:
            return self._google_context
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._google_context = await self._browser.new_context(
                locale="en-PH",
                viewport={"width": 1280, "height": 900},
            )
            return self._google_context
        except Exception as exc:
            raise WebSearchError(
                "Google search browser is unavailable; run the application setup again"
            ) from exc

    async def _search_google(self, query: str) -> list[dict[str, str]]:
        manual_url = google_search_url(query)
        async with self._google_semaphore:
            if self._google_blocked:
                raise GoogleSearchBlocked(
                    "Google paused automated searching. Open this search manually or retry later.",
                    manual_url,
                )
            if self._google_searcher:
                try:
                    return (await self._google_searcher(query))[:10]
                except GoogleSearchBlocked:
                    self._google_blocked = True
                    raise
            page = None
            try:
                context = await self._ensure_google_context()
                page = await context.new_page()
                response = await page.goto(
                    manual_url, wait_until="domcontentloaded", timeout=60_000
                )
                html = await page.content()
                if (
                    not response
                    or response.status in {403, 429}
                    or _google_is_blocked(html, page.url)
                ):
                    self._google_blocked = True
                    raise GoogleSearchBlocked(
                        "Google paused automated searching. Open this search manually or retry later.",
                        manual_url,
                    )
                results = parse_google_results(html)
                if not results and "did not match any documents" not in html.casefold():
                    self._google_blocked = True
                    raise GoogleSearchBlocked(
                        "Google did not return readable results. Open this search manually or retry later.",
                        manual_url,
                    )
                return results
            except GoogleSearchBlocked:
                raise
            except Exception as exc:
                self._google_blocked = True
                raise GoogleSearchBlocked(
                    "Google search could not be completed. Open this search manually or retry later.",
                    manual_url,
                ) from exc
            finally:
                if page:
                    await page.close()

    def _web_queries(self, case: ParsedCase) -> list[tuple[str, str]]:
        queries: list[tuple[str, str]] = []
        if case.docket:
            queries.append(("docket", f'"{case.docket}"'))
        if case.reporter_citation:
            queries.append(("reporter", f'"{case.reporter_citation}" {case.title}'))
        if case.raw_text:
            queries.append(("raw", case.raw_text))
        year = _year_clue(case)
        trusted_sites = (
            "(site:lawphil.net OR site:elibrary.judiciary.gov.ph OR "
            "site:sc.judiciary.gov.ph OR site:chanrobles.com)"
        )
        queries.append(("title", f"{case.title} {year} {trusted_sites}".strip()))
        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for kind, query in queries:
            clean = re.sub(r"\s+", " ", query).strip()
            if clean and clean.casefold() not in seen:
                seen.add(clean.casefold())
                unique.append((kind, clean))
        return unique[:3]

    async def _candidate_from_web_result(
        self,
        case: ParsedCase,
        result: dict[str, str],
        query_kind: str,
        page_fetcher: PageFetcher | None,
    ) -> CaseCandidate | None:
        source = _source_for_url(result["url"])
        if not source:
            return None
        try:
            if page_fetcher:
                html, resolved_url = await page_fetcher(source, result["url"])
            else:
                response = await source_request(self.client, "GET", result["url"])
                response.raise_for_status()
                html, resolved_url = response.text, str(response.url)
        except Exception:
            return None
        if not _source_for_url(resolved_url):
            return None
        actual = heading_identity(html)
        candidate = CaseCandidate(
            title=actual["title"], docket=actual["docket"],
            decision_date=actual["decision_date"], reporter_citation=actual["reporter_citation"],
            source=_source_for_url(resolved_url), source_url=resolved_url,
        )
        verification = verify_identity(case, actual, resolved_url)
        candidate.verification = verification.as_dict()
        candidate.verified = verification.verified
        # Scores are discovery/ranking hints only; verification is a separate gate.
        scored = _score_candidate(case, candidate, actual["heading"], exact_query_kind=query_kind)
        return scored or candidate

    async def verify_candidate(self, case: ParsedCase, candidate: CaseCandidate,
                               page_fetcher: PageFetcher | None = None) -> CaseCandidate:
        fresh = await self._candidate_from_web_result(case,
            {"url": candidate.source_url, "title": "", "description": ""}, "", page_fetcher)
        if candidate.source == "Lawphil" and (fresh is None or not fresh.docket):
            # Legacy Lawphil filenames use both gr_l-N and gr_l_N. A broken
            # index URL is a hint, not a reason to abandon the same decision.
            alternates = [re.sub(r"/gr_l([-_])", lambda m: "/gr_l" + ("_" if m.group(1) == "-" else "-"), candidate.source_url, flags=re.I),
                          re.sub(r"/gr_l[-_]", "/gr_", candidate.source_url, flags=re.I)]
            for alternate in dict.fromkeys(alternates):
                if alternate == candidate.source_url:
                    continue
                repaired = await self._candidate_from_web_result(case,
                    {"url": alternate, "title": "", "description": ""}, "", page_fetcher)
                if repaired and repaired.docket:
                    fresh = repaired
                    break
        if fresh is None:
            candidate.verified = False
            candidate.verification = {"outcome": "insufficient_evidence", "message": "Source could not be fetched for verification"}
            return candidate
        # An explicit reporter reference in the source's structured index may
        # establish citation; snippets and cached metadata may never do so.
        if candidate.source == "Lawphil" and candidate.reporter_citation:
            actual = {"title": fresh.title, "docket": fresh.docket,
                      "decision_date": fresh.decision_date,
                      "reporter_citation": fresh.reporter_citation or candidate.reporter_citation}
            same_docket = docket_ids(candidate.docket) == docket_ids(fresh.docket)
            linked_prefix_discrepancy = (
                not case.docket and candidate.source == fresh.source
                and bool(docket_ids(candidate.docket) and docket_ids(fresh.docket))
                and {d.removeprefix("L-") for d in docket_ids(candidate.docket)}
                    == {d.removeprefix("L-") for d in docket_ids(fresh.docket)})
            same_identity = ((same_docket or linked_prefix_discrepancy)
                             and candidate.decision_date == fresh.decision_date
                             and title_matches(candidate.title, fresh.title))
            if same_identity:
                result = verify_identity(case, actual, fresh.source_url)
                fresh.verification = result.as_dict()
                fresh.verification["source_reference"] = {
                    "source": "Lawphil", "source_url": candidate.source_url,
                    "reporter_citation": candidate.reporter_citation,
                    "docket": candidate.docket, "decision_date": candidate.decision_date}
                if not same_docket:
                    fresh.verification["source_reference"]["index_docket_discrepancy"] = {
                        "index": candidate.docket, "decision_heading": fresh.docket}
                fresh.verified = result.verified
                fresh.reporter_citation = actual["reporter_citation"]
        return fresh

    async def _direct_discovery(self, case: ParsedCase, seeds: list[CaseCandidate],
                                page_fetcher: PageFetcher | None) -> list[CaseCandidate]:
        from .retrieval import ELIBRARY_SEARCH_URL, ELIBRARY_INDEX, _normalize_url
        found: dict[str, CaseCandidate] = {}
        docket_hints = [docket_ids(case.docket)] if case.docket else [docket_ids(c.docket) for c in seeds]
        docket_hints = [ids for ids in docket_hints if ids]
        queries = list(dict.fromkeys(q for q in (case.docket, case.reporter_citation, case.title) if q))
        for query in queries:
            try:
                response = await source_request(self.client, "POST", ELIBRARY_SEARCH_URL, data={
                    "cmd": "search", "request": query, "searchType": "phrase",
                    "index": ELIBRARY_INDEX, "sort": "Hits", "pageSize": "25"})
                response.raise_for_status()
                seen_pages: set[str] = set()
                for _page in range(4):
                    page_url = str(response.url)
                    if page_url in seen_pages:
                        break
                    seen_pages.add(page_url)
                    soup = BeautifulSoup(response.text, "html.parser")
                    for link in soup.select("a.file_link[href]")[:25]:
                        label = link.get_text(" ", strip=True)
                        leading_docket = DOCKET_RE.search(label)
                        if docket_hints and leading_docket:
                            # Search snippets can quote the target inside a different
                            # decision. Fetch the result's own docket, not that quote.
                            result_ids = docket_ids(leading_docket.group())
                            if not any(ids.issubset(result_ids) for ids in docket_hints):
                                continue
                        url = _normalize_url(str(link["href"]), page_url)
                        if url in found:
                            continue
                        candidate = await self._candidate_from_web_result(case,
                            {"url": url, "title": "", "description": ""}, "", page_fetcher)
                        if candidate:
                            found[url] = candidate
                    next_link = next((a for a in soup.select("a[href]")
                        if "next" in a.get("rel", []) or re.fullmatch(r"next(?:\s*(?:page|>|»))*", a.get_text(" ", strip=True), re.I)), None)
                    if not next_link or _page == 3:
                        break
                    next_url = _normalize_url(str(next_link["href"]), page_url)
                    if urlparse(next_url).hostname != "elibrary.judiciary.gov.ph" or urlparse(next_url).scheme != "https":
                        break
                    response = await source_request(self.client, "GET", next_url)

            except (httpx.HTTPError, ValueError):
                continue
        dates = {c.decision_date for c in seeds if c.decision_date}
        if case.decision_date:
            dates.add(case.decision_date)
        for date in sorted(dates)[:4]:
            year, month = date[:4], int(date[5:7])
            url = f"https://chanrobles.com/cralaw/{year}decisions.php?id={month}"
            try:
                if page_fetcher:
                    html, base = await page_fetcher("ChanRobles", url)
                else:
                    response = await source_request(self.client, "GET", url)
                    response.raise_for_status()
                    html, base = response.text, str(response.url)
                soup = BeautifulSoup(html, "html.parser")
                for link in soup.select("a[href]"):
                    label = link.get_text(" ", strip=True)
                    if not (_title_matches(case.title, label) or (docket_ids(case.docket) and docket_ids(case.docket).issubset(docket_ids(label)))):
                        continue
                    target = urljoin(base, str(link["href"]))
                    if target in found:
                        continue
                    candidate = await self._candidate_from_web_result(case,
                        {"url": target, "title": "", "description": ""}, "", page_fetcher)
                    if candidate:
                        found[target] = candidate
                    if len(found) >= 100:
                        break
            except (httpx.HTTPError, ValueError):
                continue
        return list(found.values())

    async def candidates(
        self, case: ParsedCase, page_fetcher: PageFetcher | None = None
    ) -> list[CaseCandidate]:
        official: dict[str, CaseCandidate] = {}
        # Cache entries are hints; fetch and verify without trusting cached evidence.
        if self.database:
            for hint in self.database.get_cached_candidates(lookup_keys(case)):
                candidate = await self._candidate_from_web_result(case,
                    {"url": hint["source_url"], "title": "", "description": ""}, "", page_fetcher)
                if candidate:
                    official[candidate.source_url] = candidate
        volume_query = re.search(r"\b\d{1,4}\s*(?:SCRA|PHIL\.?)\b", case.reporter_citation, re.I)
        queries = [case.docket, case.reporter_citation, volume_query.group() if volume_query else "", _title_search_query(case.title),
                   *_party_search_queries(case.title)]
        seeds: list[CaseCandidate] = []
        checked_official: set[str] = set()
        for query in dict.fromkeys(value.strip() for value in queries if value.strip()):
            try:
                found = await self._search_lawphil(query)
            except (httpx.HTTPError, ValueError):
                found = []
            for candidate in found:
                if candidate.source_url in checked_official:
                    continue
                if docket_ids(case.docket) and not docket_ids(case.docket).issubset(docket_ids(candidate.docket)):
                    continue
                if volume_query:
                    wanted_volumes = _reporter_volume_keys(case.reporter_citation)
                    comparable = {item for item in _reporter_volume_keys(candidate.reporter_citation)
                                  if item[1] in {kind for _n, kind in wanted_volumes}}
                    if comparable and not wanted_volumes.issubset(comparable):
                        continue
                if not (_plausible_party_hint(case.title, candidate.title)
                        or (case.reporter_citation and _reporter_matches(case.reporter_citation, candidate.reporter_citation))
                        or docket_ids(case.docket)):
                    continue
                checked_official.add(candidate.source_url)
                seeds.append(candidate)
                fresh = await self.verify_candidate(case, candidate, page_fetcher)
                official[fresh.source_url] = fresh
        if any(candidate_is_high_confidence(c) for c in official.values()) and not candidates_are_ambiguous(list(official.values())):
            return sorted(official.values(), key=lambda c: (candidate_is_high_confidence(c), c.score), reverse=True)
        for candidate in await self._direct_discovery(case, seeds, page_fetcher):
            official[candidate.source_url] = candidate
        verify_concordant_mirrors(case, list(official.values()))
        if any(candidate_is_high_confidence(c) for c in official.values()) and not candidates_are_ambiguous(list(official.values())):
            return sorted(official.values(), key=lambda c: (candidate_is_high_confidence(c), c.score), reverse=True)
        web_candidates: dict[str, CaseCandidate] = dict(official)
        web_queries = self._web_queries(case)
        for query_kind, query in web_queries:
            try:
                results = await self._search_google(query)
            except GoogleSearchBlocked as exc:
                exc.candidates = sorted(
                    web_candidates.values(), key=lambda item: (candidate_is_high_confidence(item), item.score), reverse=True
                )
                raise
            for result in results:
                candidate = await self._candidate_from_web_result(
                    case, result, query_kind, page_fetcher
                )
                if candidate:
                    existing = web_candidates.get(candidate.source_url)
                    if not existing or candidate.score > existing.score:
                        web_candidates[candidate.source_url] = candidate

                # A result from any domain may reveal the docket. Re-query only
                # Lawphil's official API with that clue; never download that result.
                clue_text = f"{result['title']} {result['description']}"
                discovered_docket = _extract_docket(clue_text)
                if discovered_docket and not docket_tokens(case.docket):
                    try:
                        discovered = await self._search_lawphil(discovered_docket)
                    except (httpx.HTTPError, ValueError):
                        discovered = []
                    for lawphil_candidate in discovered:
                        fresh = await self.verify_candidate(case, lawphil_candidate, page_fetcher)
                        web_candidates[fresh.source_url] = fresh
            verify_concordant_mirrors(case, list(web_candidates.values()))
            ranked = sorted(web_candidates.values(), key=lambda item: (candidate_is_high_confidence(item), item.score), reverse=True)
            if ranked and candidate_is_high_confidence(ranked[0]) and not candidates_are_ambiguous(ranked):
                return ranked
        return sorted(web_candidates.values(), key=lambda item: (candidate_is_high_confidence(item), item.score), reverse=True)

    def cache_verified(self, case: ParsedCase, candidate: CaseCandidate) -> None:
        if not self.database or not candidate_is_high_confidence(candidate):
            return
        cached = candidate.as_dict()
        cached["verified"] = True
        self.database.cache_candidate(lookup_keys(case), cached)


def apply_resolution(case_data: dict[str, Any], candidates: list[CaseCandidate]) -> dict[str, Any]:
    result = dict(case_data)
    result["candidates"] = [candidate.as_dict() for candidate in candidates]
    result["resolution_status"] = "ready" if result.get("docket") and result.get("decision_date") else "unresolved"
    if not result.get("original_input"):
        result["original_input"] = {key: result.get(key, "") for key in ("title", "docket", "decision_date", "reporter_citation")}
    if candidates and candidate_is_high_confidence(candidates[0]) and not candidates_are_ambiguous(candidates):
        candidate = candidates[0]
        result["docket"] = result.get("docket") or candidate.docket
        result["decision_date"] = result.get("decision_date") or candidate.decision_date
        result["reporter_citation"] = result.get("reporter_citation") or candidate.reporter_citation
        result["metadata_source"] = candidate.source
        result["metadata_source_url"] = candidate.source_url
        if result["docket"] and result["decision_date"]:
            result["resolution_status"] = "auto_filled"
            result["candidates"] = []
            result["warnings"] = [
                warning for warning in result.get("warnings", [])
                if not warning.startswith("Missing or malformed")
            ]
    elif candidates:
        result["resolution_status"] = "needs_review"
    return result


async def resolve_case_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(2)
    async with CaseResolver() as resolver:
        async def resolve(row: dict[str, Any]) -> dict[str, Any]:
            if row.get("docket") and row.get("decision_date"):
                complete = dict(row)
                complete["resolution_status"] = "ready"
                complete["candidates"] = []
                return complete
            case = ParsedCase(
                ordinal=row["ordinal"],
                week=row.get("week", "Uncategorized"),
                title=row.get("title", ""),
                docket=row.get("docket", ""),
                decision_date=row.get("decision_date", ""),
                raw_text=row.get("raw_text", ""),
                reporter_citation=row.get("reporter_citation", ""),
            )
            try:
                async with semaphore:
                    candidates = await resolver.candidates(case)
                return apply_resolution(row, candidates)
            except Exception as exc:
                failed = dict(row)
                failed["resolution_status"] = "unresolved"
                failed["candidates"] = []
                failed["warnings"] = list(failed.get("warnings", [])) + [
                    f"Metadata lookup failed: {exc}"
                ]
                return failed

        return await asyncio.gather(*(resolve(row) for row in rows))
