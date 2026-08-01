from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable, TYPE_CHECKING
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from .domain import ParsedCase
from .filenames import normalize_case_title
from .network import trusted_ssl_context
from .parser import DATE_RE, REPORTER_RE, docket_tokens, parse_date

if TYPE_CHECKING:
    from .database import Database


LAWPHIL_SEARCH_URL = "https://lawphil.net/api/public/jurisprudence"
GOOGLE_SEARCH_URL = "https://www.google.com/search"
APPROVED_SOURCE_HOSTS = {
    "lawphil.net": "Lawphil",
    "www.lawphil.net": "Lawphil",
    "elibrary.judiciary.gov.ph": "Supreme Court E-Library",
    "sc.judiciary.gov.ph": "Supreme Court",
    "www.sc.judiciary.gov.ph": "Supreme Court",
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
    left = _compact_title(expected)
    right = _compact_title(actual)
    if not left or not right:
        return False
    if left == right or left in right or right in left or SequenceMatcher(None, left, right).ratio() >= 0.78:
        return True
    expected_sides = re.split(r"\s+v\s+", left, maxsplit=1)
    actual_sides = re.split(r"\s+v\s+", right, maxsplit=1)
    if len(expected_sides) != 2 or len(actual_sides) != 2:
        return False
    expected_left, expected_right = map(_party_tokens, expected_sides)
    actual_left, actual_right = map(_party_tokens, actual_sides)
    return bool(
        expected_left and expected_right
        and expected_left.intersection(actual_left)
        and expected_right.intersection(actual_right)
    )


def _party_sides_found(title: str, evidence: str) -> bool:
    sides = re.split(r"\s+v\s+", _compact_title(title), maxsplit=1)
    if len(sides) != 2:
        return False
    evidence_tokens = set(_compact_title(evidence).split())
    left, right = map(_party_tokens, sides)
    return bool(
        left and right
        and left.intersection(evidence_tokens)
        and right.intersection(evidence_tokens)
    )


def _title_search_query(title: str) -> str:
    tokens = _party_tokens(title)
    return " ".join(token for token in _compact_title(title).split() if token in tokens)


def _reporter_keys(value: str) -> set[tuple[str, str, str]]:
    return {
        (match.group(1), match.group(2).casefold().rstrip("."), match.group(3))
        for match in re.finditer(
            r"\b(\d{1,4})\s*(SCRA|PHIL\.?)\s*(\d{1,4})\b",
            value,
            re.IGNORECASE,
        )
    }


def _reporter_matches(expected: str, actual: str) -> bool:
    expected_keys = _reporter_keys(expected)
    return bool(expected_keys and expected_keys.intersection(_reporter_keys(actual)))


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
    if len(candidates) < 2:
        return False
    first, second = candidates[:2]
    return bool(
        first.verified and second.verified
        and first.source_url != second.source_url
        and first.score - second.score <= 12
    )


def candidate_is_high_confidence(candidate: CaseCandidate) -> bool:
    return bool(
        candidate.verified
        and candidate.score >= 70
        and (
            "Exact docket number" in candidate.reasons
            or "Both case sides matched" in candidate.reasons
        )
    )


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
        response = await self.client.get(
            LAWPHIL_SEARCH_URL,
            params={"rows": 25, "sort": "newest", "search": query, "page": 1},
        )
        response.raise_for_status()
        payload = response.json()
        unique: dict[str, CaseCandidate] = {}
        for row in payload.get("data", []):
            candidate = _candidate_from_lawphil(row)
            unique[candidate.source_url] = candidate
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
                response = await self.client.get(result["url"])
                response.raise_for_status()
                html, resolved_url = response.text, str(response.url)
        except Exception:
            return None
        if not _source_for_url(resolved_url):
            return None
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True)[:30_000]
        combined = f"{result['title']} {result['description']} {page_text}"
        docket = _extract_docket(page_text)
        expected_tokens = docket_tokens(case.docket)
        if expected_tokens and expected_tokens[0] in docket_tokens(page_text):
            docket = case.docket
        reporter = _extract_reporter(combined)
        if case.reporter_citation and _normalized_clue(case.reporter_citation) in _normalized_clue(combined):
            reporter = case.reporter_citation
        candidate = CaseCandidate(
            title=result["title"] if " v" in result["title"].casefold() else case.title,
            docket=docket,
            decision_date=_extract_date(page_text),
            reporter_citation=reporter,
            source=_source_for_url(resolved_url),
            source_url=resolved_url,
            verified=True,
        )
        return _score_candidate(
            case, candidate, combined, exact_query_kind=query_kind
        )

    async def candidates(
        self, case: ParsedCase, page_fetcher: PageFetcher | None = None
    ) -> list[CaseCandidate]:
        if self.database:
            cached = self.database.get_cached_candidates(lookup_keys(case))
            if cached:
                candidates = [CaseCandidate(**item) for item in cached]
                return sorted(candidates, key=lambda item: item.score, reverse=True)

        official: dict[str, CaseCandidate] = {}
        # Strong identifiers first, then the looser party-name lookup.
        queries = [case.docket, case.reporter_citation, _title_search_query(case.title)]
        for query in dict.fromkeys(value.strip() for value in queries if value.strip()):
            try:
                found = await self._search_lawphil(query)
            except (httpx.HTTPError, ValueError):
                # A temporary official-index failure must not prevent the
                # browser discovery fallback from finding the same decision.
                found = []
            for candidate in found:
                scored = _score_candidate(case, candidate, candidate.title)
                if scored:
                    official[scored.source_url] = scored
            if official:
                ranked = sorted(official.values(), key=lambda item: item.score, reverse=True)
                if candidate_is_high_confidence(ranked[0]) and not candidates_are_ambiguous(ranked):
                    return ranked

        web_candidates: dict[str, CaseCandidate] = dict(official)
        web_queries = self._web_queries(case)
        for query_kind, query in web_queries:
            try:
                results = await self._search_google(query)
            except GoogleSearchBlocked as exc:
                exc.candidates = sorted(
                    web_candidates.values(), key=lambda item: item.score, reverse=True
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
                        scored = _score_candidate(case, lawphil_candidate, clue_text)
                        if scored:
                            web_candidates[scored.source_url] = scored
            ranked = sorted(web_candidates.values(), key=lambda item: item.score, reverse=True)
            if ranked and candidate_is_high_confidence(ranked[0]) and not candidates_are_ambiguous(ranked):
                return ranked
        return sorted(web_candidates.values(), key=lambda item: item.score, reverse=True)

    def cache_verified(self, case: ParsedCase, candidate: CaseCandidate) -> None:
        if not self.database:
            return
        cached = candidate.as_dict()
        cached["verified"] = True
        self.database.cache_candidate(lookup_keys(case), cached)


def apply_resolution(case_data: dict[str, Any], candidates: list[CaseCandidate]) -> dict[str, Any]:
    result = dict(case_data)
    result["candidates"] = [candidate.as_dict() for candidate in candidates]
    result["resolution_status"] = "ready" if result.get("docket") and result.get("decision_date") else "unresolved"
    legacy_match = False
    if len(candidates) == 1 and candidates[0].score == 0:
        candidate = candidates[0]
        legacy_match = bool(
            (
                result.get("docket")
                and docket_tokens(result["docket"]) == docket_tokens(candidate.docket)
            )
            or _title_matches(result.get("title", ""), candidate.title)
        )
    if len(candidates) == 1 and (
        candidate_is_high_confidence(candidates[0]) or legacy_match
    ):
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
