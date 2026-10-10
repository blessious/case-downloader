import asyncio
import uuid
import httpx
import pytest
from app.database import Database
from app.domain import ParsedCase
from app.jobs import JobManager
from app.resolver import CaseCandidate, CaseResolver, candidate_is_high_confidence, candidates_are_ambiguous, verify_concordant_mirrors
from app.verification import heading_identity, verify_identity
from app.retrieval import CaseRetriever
from app.network import source_request
from pdf_factory import identity_pdf


def mirror_fixture(source="ChanRobles", date="2000-01-02", title="Alpha Industries v. Beta Holdings", citation=""):
    case = ParsedCase(1, "Week 1", "Alpha Industries v. Beta Holdings", "", "", "", reporter_citation="20 SCRA 100")
    actual = {"title": title, "docket": "G.R. No. 123456", "decision_date": date, "reporter_citation": citation}
    candidate = CaseCandidate(title, actual["docket"], date, citation, source, "https://" + ("lawphil.net" if source == "Lawphil" else "chanrobles.com") + "/decision")
    candidate.verification = verify_identity(case, actual, candidate.source_url).as_dict()
    return case, candidate


def test_two_sources_recover_missing_reporter_page_with_fresh_volume_reference():
    case, a = mirror_fixture("Lawphil", citation="20 SCRA")
    _, b = mirror_fixture()
    a.verification["source_reference"] = {"source": "Lawphil", "reporter_citation": "20 SCRA"}
    verify_concordant_mirrors(case, [a, b])
    assert a.verified and b.verified
    assert a.verification["reporter_page_verified"] is False
    assert len(a.verification["mirror_evidence"]) == 2


def test_parallel_phil_citation_is_not_a_conflicting_scra_reference():
    case, a = mirror_fixture("Lawphil", citation="20 SCRA")
    _, b = mirror_fixture("Supreme Court E-Library", citation="100 Phil 200")
    a.verification["source_reference"] = {"source": "Lawphil", "reporter_citation": "20 SCRA"}
    verify_concordant_mirrors(case, [a,b])
    assert a.verified and b.verified


@pytest.mark.asyncio
async def test_discovery_pipeline_confirms_missing_page_before_google():
    case = ParsedCase(1, "Week 1", "Alpha Industries v. Beta Holdings", "", "", "", reporter_citation="20 SCRA 100")
    def handler(request):
        if request.url.host == "lawphil.net":
            return httpx.Response(200, json={"data": [{"citation": case.title,
                "gr_number": "G.R. No. 123456", "date": "2000-01-02", "reference": "20 SCRA",
                "url": "example.html"}]}, request=request)
        return httpx.Response(200, text='<a class="file_link" href="https://elibrary.judiciary.gov.ph/decision">G.R. No. 123456</a>', request=request)
    async def fetch(source, url):
        if "decisions.php" in url:
            return "", url
        return "G.R. No. 123456 January 2, 2000 Alpha Industries v. Beta Holdings DECISION", url
    async def google(query):
        raise AssertionError("Concordant direct sources should resolve before Google")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with CaseResolver(client=client, google_searcher=google) as resolver:
            candidates = await resolver.candidates(case, fetch)
    assert candidate_is_high_confidence(candidates[0])
    assert candidates[0].verification["verification_method"] == "concordant_source_headings"


@pytest.mark.asyncio
async def test_broken_lawphil_filename_repaired_without_changing_docket_identity():
    case = ParsedCase(1, "Week 1", "Alpha Industries v. Beta Holdings", "", "", "", reporter_citation="20 SCRA 100")
    candidate = CaseCandidate(case.title, "G.R. No. L-123456", "2000-01-02", "20 SCRA 100", "Lawphil", "https://lawphil.net/gr_l-123456_2000.html")
    calls = []
    async def fetch(source, url):
        calls.append(url)
        if "gr_l" in url:
            raise RuntimeError("404")
        return "G.R. No. L-123456 January 2, 2000 Alpha Industries v. Beta Holdings DECISION", url
    async with CaseResolver() as resolver:
        result = await resolver.verify_candidate(case, candidate, fetch)
    assert candidate_is_high_confidence(result)
    assert result.source_url.endswith("/gr_123456_2000.html")
    assert result.docket == "G.R. No. L-123456"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_index_prefix_discrepancy_does_not_rewrite_supplied_docket():
    candidate = CaseCandidate("Alpha Industries v. Beta Holdings", "G.R. No. L-123456", "2000-01-02", "20 SCRA 100", "Lawphil", "https://lawphil.net/decision")
    async def fetch(source, url):
        return "G.R. No. 123456 January 2, 2000 Alpha Industries v. Beta Holdings DECISION", url
    async with CaseResolver() as resolver:
        original = ParsedCase(1, "Week 1", candidate.title, "", "", "", reporter_citation="20 SCRA 100")
        result = await resolver.verify_candidate(original, candidate, fetch)
        assert candidate_is_high_confidence(result)
        assert result.verification["source_reference"]["index_docket_discrepancy"]
        original.docket = "G.R. No. L-123456"
        result = await resolver.verify_candidate(original, candidate, fetch)
        assert not candidate_is_high_confidence(result)


@pytest.mark.parametrize("problem", ["one_source", "wrong_date", "wrong_party", "wrong_volume", "conflicting_page", "cached_only"])
def test_mirror_confirmation_rejects_inadequate_or_conflicting_evidence(problem):
    case, a = mirror_fixture("Lawphil", citation="20 SCRA")
    _, b = mirror_fixture("Lawphil" if problem == "one_source" else "ChanRobles",
                          date="2000-01-03" if problem == "wrong_date" else "2000-01-02",
                          title="Alpha Shipping v. Beta Holdings" if problem == "wrong_party" else case.title,
                          citation="20 SCRA 999" if problem == "conflicting_page" else "")
    if problem != "cached_only":
        a.verification["source_reference"] = {"source": "Lawphil", "reporter_citation": "21 SCRA" if problem == "wrong_volume" else "20 SCRA"}
    verify_concordant_mirrors(case, [a, b])
    assert not a.verified and not b.verified


@pytest.mark.asyncio
async def test_party_phrase_discovery_retains_strict_heading_gate():
    case = ParsedCase(1, "Week 1", "Alpha Industries v. CA", "", "", "",
                      reporter_citation="20 SCRA 100")
    queries = []
    def handler(request):
        query = request.url.params.get("search")
        queries.append(query)
        rows = []
        if query == "Alpha Industries":
            rows = [{"citation": "Alpha Industries, Inc. v. Court of Appeals",
                     "gr_number": "G.R. No. 123456", "date": "2000-01-02",
                     "reference": "20 SCRA 100", "url": "example.html"}]
        return httpx.Response(200, json={"data": rows}, request=request)
    async def fetch(source, url):
        return "G.R. No. 123456 January 2, 2000 Alpha Industries, Inc. v. Court of Appeals DECISION", url
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with CaseResolver(client=client) as resolver:
            candidates = await resolver.candidates(case, fetch)
    assert "Alpha Industries" in queries
    assert candidate_is_high_confidence(candidates[0])


@pytest.mark.asyncio
async def test_elibrary_http_result_is_upgraded_and_heading_verified():
    case = ParsedCase(1, "Week 1", "SEC v. Subic Bay Golf", "G.R. No. 179047",
                      "2015-03-11", "", reporter_citation="755 Phil 553")
    calls = []
    def handler(request):
        return httpx.Response(200, text='<a class="file_link" href="http://elibrary.judiciary.gov.ph/decision">result</a>', request=request)
    async def fetch(source, url):
        calls.append(url)
        return "755 Phil. 553 G.R. No. 179047 March 11, 2015 Securities and Exchange Commission v. Subic Bay Golf DECISION", url
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with CaseResolver(client=client) as resolver:
            candidates = await resolver._direct_discovery(case, [], fetch)
    assert calls[0] == "https://elibrary.judiciary.gov.ph/decision"
    assert all(url.startswith("https://") for url in calls)
    assert candidate_is_high_confidence(candidates[0])

@pytest.mark.asyncio
@pytest.mark.parametrize("selected_source", ["Lawphil", "Supreme Court E-Library"])
async def test_selected_url_cannot_bypass_download_order(tmp_path, monkeypatch, selected_source):
    case = ParsedCase(1, "Week 1", "Alpha v. Beta", "G.R. No. 123456", "2000-01-02", "",
        metadata_source=selected_source, metadata_source_url="https://lawphil.net/example" if selected_source == "Lawphil" else "https://elibrary.judiciary.gov.ph/example")
    calls = []
    async def missing(*args):
        calls.append("ChanRobles")
        return False, "", [], "NOT_FOUND", ""
    async def lawphil(*args):
        calls.append("Lawphil")
        return False, "", [], "NOT_FOUND", ""
    async def elibrary(case, path, *args):
        calls.append("Supreme Court E-Library")
        path.write_bytes(identity_pdf("G.R. No. 123456 January 2, 2000 Alpha v. Beta DECISION"))
        return True, "https://elibrary.judiciary.gov.ph/example", [], "", ""
    async with CaseRetriever() as retriever:
        monkeypatch.setattr(retriever, "_chanrobles", missing)
        monkeypatch.setattr(retriever, "_lawphil", lawphil)
        monkeypatch.setattr(retriever, "_lawphil_direct", lawphil)
        monkeypatch.setattr(retriever, "_elibrary", elibrary)
        monkeypatch.setattr(retriever, "_trusted_direct", elibrary)
        result = await retriever.retrieve(case, tmp_path / "case.part", lambda _s: asyncio.sleep(0))
    assert result.success
    assert calls == ["ChanRobles", "Lawphil", "Supreme Court E-Library"]

@pytest.mark.asyncio
async def test_misleading_snippet_cannot_supply_identity():
    resolver = CaseResolver()
    case = ParsedCase(1, "Week 1", "Alpha v. Beta", "", "", "", reporter_citation="10 SCRA 20")
    async def fetch(_source, url):
        return "G.R. No. 999999 January 2, 2000 Other v. Respondent DECISION Alpha v. Beta 10 SCRA 20", url
    try:
        candidate = await resolver._candidate_from_web_result(case,
            {"url": "https://lawphil.net/example", "title": "Alpha v. Beta", "description": "10 SCRA 20"}, "reporter", fetch)
        assert not candidate_is_high_confidence(candidate)
    finally:
        await resolver.__aexit__()

@pytest.mark.asyncio
async def test_existing_wrong_pdf_is_preserved(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    manager = JobManager(db)
    run_id = manager.create_run({"output_dir": str(tmp_path / "out"), "cases": [{"ordinal": 1, "week": "Week 1", "title": "Alpha v. Beta", "docket": "G.R. No. 123456", "decision_date": "2000-01-02"}]})
    item = db.get_run(run_id)["cases"][0]
    folder = tmp_path / "out" / "Week 1"
    folder.mkdir()
    path = folder / item["filename"]
    wrong = identity_pdf("G.R. No. 999999 January 2, 2000 Other v. Respondent DECISION")
    path.write_bytes(wrong)
    await manager._process_case(run_id, item, tmp_path / "out", object(), object())
    result = db.get_run(run_id)["cases"][0]
    assert result["status"] == "Not saved"
    assert result["error_code"] == "EXISTING_FILE_INVALID"
    assert result["failure_stage"] == "pdf_validation"
    assert path.read_bytes() == wrong

@pytest.mark.asyncio
async def test_transient_retries_honor_bounded_retry_after(monkeypatch):
    statuses = [429, 503, 200]
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr("asyncio.sleep", sleep)
    def handler(request):
        return httpx.Response(statuses.pop(0), headers={"Retry-After": "1000"}, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await source_request(client, "GET", "https://lawphil.net/example")
    assert response.status_code == 200 and delays == [60, 60]

@pytest.mark.asyncio
async def test_selected_candidate_does_not_override_original_evidence(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    manager = JobManager(db)
    run_id = manager.create_run({"output_dir": str(tmp_path / "out"), "cases": [{"ordinal": 1, "week": "Week 1", "title": "Alpha v. Beta", "docket": "", "decision_date": "", "reporter_citation": "10 SCRA 20"}]})
    item = db.get_run(run_id)["cases"][0]
    candidate = CaseCandidate("Other v. Respondent", "G.R. No. 999999", "2000-01-02", "20 SCRA 30", "Lawphil", "https://lawphil.net/wrong")
    db.set_case_candidates(item["id"], [candidate.as_dict()])
    async def no_process(*args):
        pass
    manager._process_selected_case = no_process
    choice = db.get_run(run_id)["cases"][0]["candidates"][0]
    assert manager.select_candidate(run_id, item["id"], choice["id"])
    await manager.tasks[run_id]
    selected = db.get_case(run_id, item["id"])
    assert selected["docket"] == ""
    assert selected["original_input"]["reporter_citation"] == "10 SCRA 20"
    class Resolver:
        async def candidates(self, case, fetcher=None):
            assert case.reporter_citation == "10 SCRA 20"
            return [candidate]
    await manager._process_case(run_id, selected, tmp_path / "out", object(), Resolver())
    assert db.get_case(run_id, item["id"])["status"] == "Needs review"

def test_mirrors_group_by_identity_but_dates_remain_distinct():
    a = CaseCandidate("Alpha v. Beta", "G.R. No. 123456", "2000-01-02", "", "Lawphil", "https://lawphil.net/a", verified=True, verification={"outcome": "verified"})
    b = CaseCandidate("Alpha v. Beta", "G.R. No. 123456", "2000-01-02", "", "ChanRobles", "https://chanrobles.com/b", verified=True, verification={"outcome": "verified"})
    assert not candidates_are_ambiguous([a,b])
    b.decision_date = "2000-01-03"
    assert candidates_are_ambiguous([a,b])


def test_legacy_database_migration_does_not_claim_new_verification(tmp_path):
    db = Database(tmp_path / "legacy.sqlite3")
    manager = JobManager(db)
    run_id = manager.create_run({"output_dir": str(tmp_path / "out"), "cases": [{"ordinal": 1, "week": "Week 1", "title": "Alpha v. Beta", "docket": "G.R. No. 123456", "decision_date": "2000-01-02"}]})
    item = db.get_run(run_id)["cases"][0]
    db.update_case(item["id"], status="Saved", saved_path="historical.pdf")
    db.update_run(run_id, status="Completed")
    with db.connect() as connection:
        for column in ("original_input_json", "verification_json", "failure_stage"):
            connection.execute(f"ALTER TABLE cases DROP COLUMN {column}")
        connection.execute("ALTER TABLE case_candidates DROP COLUMN verification_json")
    migrated = Database(tmp_path / "legacy.sqlite3").get_run(run_id)
    assert migrated["cases"][0]["status"] == "Saved"
    assert migrated["cases"][0]["saved_path"] == "historical.pdf"
    assert migrated["verified_count"] == 0
    assert migrated["cases"][0]["verification"] == {}

@pytest.mark.asyncio
async def test_stale_cached_evidence_cannot_verify_a_changed_page(tmp_path, monkeypatch):
    db = Database(tmp_path / "cache.sqlite3")
    expected = ParsedCase(1, "Week 1", "Alpha v. Beta", "G.R. No. 123456", "2000-01-02", "")
    db.cache_candidate(["docket:123456"], {"source_url": "https://lawphil.net/example", "verified": True, "score": 1000})
    resolver = CaseResolver(database=db, google_searcher=lambda q: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(resolver, "_search_lawphil", lambda q: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(resolver, "_direct_discovery", lambda *a: asyncio.sleep(0, result=[]))
    async def fetch(source, url):
        return "G.R. No. 999999 January 2, 2000 Other v. Respondent DECISION", url
    try:
        candidates = await resolver.candidates(expected, fetch)
    finally:
        await resolver.__aexit__()
    assert candidates and not any(candidate_is_high_confidence(c) for c in candidates)


@pytest.mark.asyncio
async def test_fresh_index_citation_can_complete_rechecked_cache_hint(tmp_path, monkeypatch):
    db = Database(tmp_path / "cache.sqlite3")
    expected = ParsedCase(1, "Week 1", "Alpha v. Beta", "", "", "", reporter_citation="10 SCRA 20")
    candidate = CaseCandidate("Alpha v. Beta", "G.R. No. 123456", "2000-01-02", "10 SCRA 20", "Lawphil", "https://lawphil.net/example")
    db.cache_candidate(["reporter:10scra20"], candidate.as_dict())
    resolver = CaseResolver(database=db)
    monkeypatch.setattr(resolver, "_search_lawphil", lambda q: asyncio.sleep(0, result=[candidate]))
    async def fetch(source, url):
        return "G.R. No. 123456 January 2, 2000 Alpha v. Beta DECISION", url
    try:
        candidates = await resolver.candidates(expected, fetch)
    finally:
        await resolver.__aexit__()
    assert candidate_is_high_confidence(candidates[0])

@pytest.mark.asyncio
async def test_publication_never_overwrites_a_file_created_during_download(tmp_path):
    from app.domain import RetrievalResult
    db = Database(tmp_path / "db.sqlite3")
    manager = JobManager(db)
    run_id = manager.create_run({"output_dir": str(tmp_path / "out"), "cases": [{"ordinal": 1, "week": "Week 1", "title": "Alpha v. Beta", "docket": "G.R. No. 123456", "decision_date": "2000-01-02"}]})
    item = db.get_run(run_id)["cases"][0]
    target = tmp_path / "out" / "Week 1" / item["filename"]
    class Retriever:
        async def retrieve(self, case, part, callback):
            target.write_bytes(b"file from another run")
            part.write_bytes(identity_pdf("G.R. No. 123456 January 2, 2000 Alpha v. Beta DECISION"))
            return RetrievalResult(True, source="Lawphil", source_url="https://lawphil.net/example")
    await manager._process_case(run_id, item, tmp_path / "out", Retriever(), object())
    assert db.get_case(run_id, item["id"])["status"] == "Not saved"
    assert target.read_bytes() == b"file from another run"


def test_distinct_dates_on_same_docket_are_not_duplicate_input(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    manager = JobManager(db)
    cases = [{"ordinal": i, "week": "Week 1", "title": "Alpha v. Beta", "docket": "G.R. No. 123456", "decision_date": date} for i,date in enumerate(("2000-01-02", "2001-01-02"), 1)]
    run_id = manager.create_run({"output_dir": str(tmp_path / "out"), "cases": cases})
    result = db.get_run(run_id)["cases"]
    assert result[1]["status"] != "Duplicate input"
    assert result[1]["error_code"] == "FILENAME_COLLISION"


@pytest.mark.asyncio
async def test_wrong_source_pdf_is_rejected_then_verified_html_is_rendered(tmp_path, monkeypatch):
    correct = "G.R. No. 123456 January 2, 2000 Alpha v. Beta DECISION"
    wrong = identity_pdf("G.R. No. 999999 January 2, 2000 Other v. Respondent DECISION")
    def handler(request):
        return httpx.Response(200, content=wrong, headers={"content-type": "application/pdf"}, request=request)
    case = ParsedCase(1, "Week 1", "Alpha v. Beta", "G.R. No. 123456", "2000-01-02", "")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with CaseRetriever(client) as retriever:
            async def render(source, url, path):
                path.write_bytes(identity_pdf(correct))
            monkeypatch.setattr(retriever, "_render_pdf", render)
            result = await retriever._download_or_render(case, "Lawphil", "https://lawphil.net/example", correct + '<a href="bad.pdf">PDF</a>', tmp_path / "case.part")
    assert result[0]
    assert result[1] == "https://lawphil.net/example"
    assert any("Source PDF rejected" in w for w in result[2])

@pytest.mark.asyncio
async def test_lawphil_pagination_is_bounded_to_four_pages():
    pages = []
    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json={"data": [{"gr_number": f"G.R. No. {page}{i:05}", "url": f"page-{page}-{i}.html"} for i in range(25)]}, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with CaseResolver(client) as resolver:
            candidates = await resolver._search_lawphil("Alpha")
    assert pages == [1,2,3,4]
    assert len(candidates) == 100

@pytest.mark.asyncio
async def test_blocked_google_does_not_prevent_next_direct_match(monkeypatch):
    from app.resolver import GoogleSearchBlocked, google_search_url
    async def blocked(query):
        raise GoogleSearchBlocked("blocked", google_search_url(query))
    resolver = CaseResolver(google_searcher=blocked)
    monkeypatch.setattr(resolver, "_search_lawphil", lambda q: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(resolver, "_direct_discovery", lambda *a: asyncio.sleep(0, result=[]))
    try:
        with pytest.raises(GoogleSearchBlocked):
            await resolver.candidates(ParsedCase(1,"Week 1","Unknown v. Missing","","",""))
        candidate = CaseCandidate("Alpha v. Beta", "G.R. No. 123456", "2000-01-02", "10 SCRA 20", "Lawphil", "https://lawphil.net/example")
        monkeypatch.setattr(resolver, "_search_lawphil", lambda q: asyncio.sleep(0, result=[candidate]))
        async def fetch(source,url):
            return "G.R. No. 123456 January 2, 2000 Alpha v. Beta DECISION", url
        candidates = await resolver.candidates(ParsedCase(2,"Week 1","Alpha v. Beta","","","",reporter_citation="10 SCRA 20"), fetch)
        assert candidate_is_high_confidence(candidates[0])
    finally:
        await resolver.__aexit__()
