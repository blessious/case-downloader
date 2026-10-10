from __future__ import annotations

import asyncio

import httpx
import pytest

from app.database import Database
from app.domain import ParsedCase
from app.resolver import (
    CaseCandidate,
    CaseResolver,
    GoogleSearchBlocked,
    apply_resolution,
    candidate_is_high_confidence,
    candidates_are_ambiguous,
    google_search_url,
    parse_google_results,
    _reporter_matches,
)


@pytest.mark.asyncio
async def test_reporter_citation_search_returns_matching_candidate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if "/api/" not in str(request.url):
            return httpx.Response(200, text="G.R. No. 159333 July 31, 2006 Arsenio T. Mendiola v. Court of Appeals 497 SCRA 346 DECISION", request=request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "gr_number": "G.R. No. 159333",
                        "date": "2006-07-31T00:00:00.000000Z",
                        "citation": "Arsenio T. Mendiola vs. Court of Appeals, et al.",
                        "reference": "497 SCRA 346",
                        "url": "juri2006/jul2006/gr_159333_2006.html",
                    }
                ]
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    case = ParsedCase(
        1,
        "Module 1",
        "Mendiola v. Court of Appeals",
        "",
        "",
        "Mendiola v. Court of Appeals (497 SCRA 346)",
        reporter_citation="497 SCRA 346",
    )
    async with CaseResolver(client) as resolver:
        candidates = await resolver.candidates(case)
    await client.aclose()
    assert len(candidates) == 1
    assert candidates[0].docket == "G.R. No. 159333"
    assert candidates[0].decision_date == "2006-07-31"


@pytest.mark.asyncio
async def test_exact_docket_accepts_expanded_title_for_abbreviated_syllabus_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/" not in str(request.url):
            return httpx.Response(200, text="G.R. Nos. 167707 and 173775 October 8, 2008 The Secretary of the Department of Environment and Natural Resources v. Mayor Jose S. Yap DECISION", request=request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "gr_number": "G.R. No. 167707/G.R. No. 173775",
                        "date": "2008-10-08T00:00:00.000000Z",
                        "citation": (
                            "The Secretary of the Department of Environment and "
                            "Natural Resources vs. Mayor Jose S. Yap, et al."
                        ),
                        "reference": "",
                        "url": "juri2008/oct2008/gr_167707_2008.html",
                    }
                ]
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    case = ParsedCase(
        1,
        "Uncategorized",
        "DENR Sec. v. Yap",
        "G.R. No. 167707",
        "2008-10-08",
        "DENR Sec. v. Yap, GR No. 167707",
    )
    async with CaseResolver(client) as resolver:
        candidates = await resolver.candidates(case)
    await client.aclose()

    assert len(candidates) == 1
    assert candidates[0].decision_date == "2008-10-08"


def test_unverified_candidate_does_not_auto_fill_metadata() -> None:
    row = {
        "title": "Mendiola v. Court of Appeals",
        "docket": "",
        "decision_date": "",
        "reporter_citation": "497 SCRA 346",
        "warnings": ["Missing or malformed docket number", "Missing or malformed decision date"],
    }
    candidate = CaseCandidate(
        "Mendiola v. Court of Appeals",
        "G.R. No. 159333",
        "2006-07-31",
        "",
        "Lawphil",
        "https://lawphil.net/example",
    )
    resolved = apply_resolution(row, [candidate])
    assert resolved["resolution_status"] == "needs_review"
    assert resolved["docket"] == ""
    assert resolved["original_input"]["reporter_citation"] == "497 SCRA 346"


def test_multiple_candidates_require_review() -> None:
    row = {"title": "People v. Reyes", "docket": "", "decision_date": ""}
    candidates = [
        CaseCandidate("People v. Reyes", "G.R. No. 111111", "2001-01-01", "", "Source", "https://example/1"),
        CaseCandidate("People v. Reyes", "G.R. No. 222222", "2002-02-02", "", "Source", "https://example/2"),
    ]
    resolved = apply_resolution(row, candidates)
    assert resolved["resolution_status"] == "needs_review"
    assert len(resolved["candidates"]) == 2


@pytest.mark.asyncio
async def test_google_discovers_and_verifies_an_approved_source(monkeypatch: pytest.MonkeyPatch) -> None:
    case = ParsedCase(
        1,
        "Uncategorized",
        "Serg's Products, Inc. v. PCI Leasing and Finance Inc.",
        "",
        "",
        "Serg's Products, Inc. v. PCI Leasing and Finance Inc., 338 SCRA 499",
        reporter_citation="338 SCRA 499",
    )
    google_queries: list[str] = []

    async def search_google(query: str) -> list[dict[str, str]]:
        google_queries.append(query)
        return [{
            "title": "G.R. No. 137705 - SERG'S PRODUCTS, INC. v. PCI LEASING",
            "url": "https://lawphil.net/judjuris/juri2000/aug2000/gr_137705_2000.html",
            "description": "August 22, 2000 decision, 338 SCRA 499",
        }]

    async def fetch_page(_source: str, url: str) -> tuple[str, str]:
        return (
            "<html><body>G.R. No. 137705 August 22, 2000 "
            "SERG'S PRODUCTS, INC. v. PCI LEASING AND FINANCE, INC. "
            "338 SCRA 499 DECISION</body></html>",
            url,
        )

    resolver = CaseResolver(google_searcher=search_google)
    monkeypatch.setattr(resolver, "_direct_discovery", lambda *_args: _async_value([]))
    monkeypatch.setattr(resolver, "_search_lawphil", lambda _query: _async_value([]))
    try:
        candidates = await resolver.candidates(case, fetch_page)
    finally:
        await resolver.__aexit__()

    assert google_queries
    assert len(google_queries) <= 3
    assert candidates[0].source == "Lawphil"
    assert candidates[0].docket == "G.R. No. 137705"
    assert candidates[0].verified is True
    assert candidate_is_high_confidence(candidates[0])


@pytest.mark.asyncio
async def test_unapproved_result_is_only_a_metadata_clue(monkeypatch: pytest.MonkeyPatch) -> None:
    case = ParsedCase(
        1, "Uncategorized", "Serg's Products v. PCI Leasing", "", "",
        "Serg's Products v. PCI Leasing, 338 SCRA 499",
        reporter_citation="338 SCRA 499",
    )
    official = CaseCandidate(
        "Serg's Products, Inc. v. PCI Leasing and Finance, Inc.",
        "G.R. No. 137705",
        "2000-08-22",
        "338 SCRA 499",
        "Lawphil",
        "https://lawphil.net/judjuris/juri2000/aug2000/gr_137705_2000.html",
        verified=True,
    )
    async def lawphil(query: str) -> list[CaseCandidate]:
        return [official] if "137705" in query else []

    async def google(_query: str) -> list[dict[str, str]]:
        return [{
            "title": "Serg's Products v. PCI Leasing, G.R. No. 137705",
            "url": "https://example.com/unapproved-copy",
            "description": "338 SCRA 499",
        }]

    resolver = CaseResolver(google_searcher=google)
    monkeypatch.setattr(resolver, "_direct_discovery", lambda *_args: _async_value([]))
    monkeypatch.setattr(resolver, "_search_lawphil", lawphil)
    async def fetch(_source, url):
        return "G.R. No. 137705 August 22, 2000 Serg's Products, Inc. v. PCI Leasing and Finance, Inc. 338 SCRA 499 DECISION", url

    try:
        candidates = await resolver.candidates(case, fetch)
    finally:
        await resolver.__aexit__()

    assert candidates[0].source_url.startswith("https://lawphil.net/")
    assert all("example.com" not in item.source_url for item in candidates)


@pytest.mark.asyncio
async def test_blocked_google_search_provides_manual_url_after_official_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked(query: str):
        raise GoogleSearchBlocked("Google paused automated searching", google_search_url(query))

    resolver = CaseResolver(google_searcher=blocked)
    monkeypatch.setattr(resolver, "_direct_discovery", lambda *_args: _async_value([]))
    monkeypatch.setattr(resolver, "_search_lawphil", lambda _query: _async_value([]))
    case = ParsedCase(1, "Uncategorized", "Unknown v. Missing", "", "", "Unknown v. Missing")
    try:
        with pytest.raises(GoogleSearchBlocked) as error:
            await resolver.candidates(case)
    finally:
        await resolver.__aexit__()
    assert error.value.manual_search_url.startswith("https://www.google.com/search?")


@pytest.mark.asyncio
async def test_verified_mapping_cache_is_reverified(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "cache.sqlite3")
    case = ParsedCase(1, "Uncategorized", "Alpha v. Beta", "G.R. No. 123456", "2020-01-02", "Alpha v. Beta")
    saved = CaseCandidate("Alpha v. Beta", "G.R. No. 123456", "2020-01-02", "", "Lawphil", "https://lawphil.net/example", score=110, verified=True)
    resolver = CaseResolver(database=database, google_searcher=lambda _q: _async_value([]))
    database.cache_candidate(["docket:123456"], saved.as_dict())
    calls = []
    async def fetch(_source, url):
        calls.append(url)
        return "G.R. No. 123456 January 2, 2020 Alpha v. Beta DECISION", url
    monkeypatch.setattr(resolver, "_search_lawphil", lambda _q: _async_value([]))
    try:
        candidates = await resolver.candidates(case, fetch)
    finally:
        await resolver.__aexit__()
    assert calls == [saved.source_url]
    assert candidates[0].verified



def test_close_verified_candidates_are_ambiguous() -> None:
    candidates = [
        CaseCandidate("A v. B", "G.R. No. 1", "2000-01-01", "", "Lawphil", "https://lawphil.net/1", 90, verified=True, verification={"outcome": "verified"}),
        CaseCandidate("A v. B", "G.R. No. 2", "2001-01-01", "", "Supreme Court", "https://sc.judiciary.gov.ph/2", 82, verified=True, verification={"outcome": "verified"}),
    ]
    assert candidates_are_ambiguous(candidates)


def test_reporter_citation_does_not_match_as_a_numeric_substring() -> None:
    assert not _reporter_matches("29 SCRA 48", "929 SCRA 482")
    case = ParsedCase(
        1,
        "Uncategorized",
        "Ayala Land v. Ray Burton",
        "",
        "",
        "Ayala Land v. Ray Burton 29 SCRA 48",
        reporter_citation="29 SCRA 48",
    )
    wrong = CaseCandidate(
        "National Power Corporation v. Heirs of Serra",
        "G.R. No. 224324",
        "2020-01-22",
        "929 SCRA 482",
        "Lawphil",
        "https://lawphil.net/wrong",
        score=100,
        reasons=["Exact reporter citation"],
        verified=True,
    )
    assert candidate_is_high_confidence(wrong) is False


def test_google_result_parser_unwraps_redirects_and_limits_results() -> None:
    cards = "".join(
        f'<div><a href="/url?q=https%3A%2F%2Flawphil.net%2Fcase-{number}&sa=U">'
        f'<h3>Case {number}</h3></a><span>G.R. No. {number:06d}</span></div>'
        for number in range(12)
    )
    results = parse_google_results(f"<html><body>{cards}</body></html>")
    assert len(results) == 10
    assert results[0]["url"] == "https://lawphil.net/case-0"
    assert all("google.com/url" not in result["url"] for result in results)


def test_google_queries_are_limited_and_order_strong_clues_first() -> None:
    case = ParsedCase(
        1,
        "Uncategorized",
        "People v. Pletcha",
        "G.R. No. 19029",
        "1977-06-27",
        "People v. Pletcha, G.R. No. 19029, June 27, 1977",
        reporter_citation="80 SCRA 1",
    )
    resolver = CaseResolver(google_searcher=lambda _query: _async_value([]))
    queries = resolver._web_queries(case)
    assert len(queries) == 3
    assert queries[0][0] == "docket"
    assert queries[1][0] == "reporter"
    assert queries[2][0] == "raw"

    title_only = ParsedCase(
        1,
        "Uncategorized",
        "Manila International Airport Authority v. Court of Appeals",
        "",
        "",
        "Manila International Airport Authority v. CA (2006)",
    )
    title_queries = resolver._web_queries(title_only)
    assert len(title_queries) <= 3
    assert any("site:lawphil.net" in query for _kind, query in title_queries)


@pytest.mark.asyncio
async def test_google_browser_searches_are_serialized() -> None:
    active = 0
    peak = 0

    async def search(_query: str) -> list[dict[str, str]]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    resolver = CaseResolver(google_searcher=search)
    await asyncio.gather(
        resolver._search_google("first case"),
        resolver._search_google("second case"),
        resolver._search_google("third case"),
    )
    await resolver.__aexit__()
    assert peak == 1


@pytest.mark.asyncio
async def test_google_block_is_reused_for_rest_of_run() -> None:
    calls = 0

    async def blocked(query: str):
        nonlocal calls
        calls += 1
        raise GoogleSearchBlocked("blocked", google_search_url(query))

    resolver = CaseResolver(google_searcher=blocked)
    with pytest.raises(GoogleSearchBlocked):
        await resolver._search_google("first case")
    with pytest.raises(GoogleSearchBlocked) as second:
        await resolver._search_google("second case")
    await resolver.__aexit__()
    assert calls == 1
    assert "second+case" in second.value.manual_search_url


async def _async_value(value):
    return value
