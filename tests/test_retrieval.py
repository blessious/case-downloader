from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader, PdfWriter

from app.domain import ParsedCase
from app.retrieval import CaseRetriever


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_lawphil_success_uses_source_pdf(tmp_path: Path) -> None:
    pdf = blank_pdf()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/jun2005.html"):
            return httpx.Response(
                200,
                text=(
                    '<table><tr><td><a href="gr_145561_2005.html">'
                    "G.R. No. 145561</a></td><td>June 15, 2005</td></tr></table>"
                ),
                request=request,
            )
        if url.endswith("gr_145561_2005.html"):
            return httpx.Response(
                200,
                text=(
                    "<html><body>G.R. No. 145561 June 15, 2005"
                    '<a href="case.pdf">PDF</a></body></html>'
                ),
                request=request,
            )
        if url.endswith("case.pdf"):
            return httpx.Response(
                200,
                content=pdf,
                headers={"content-type": "application/pdf"},
                request=request,
            )
        return httpx.Response(404, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    case = ParsedCase(
        1,
        "Week 2",
        "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda",
        "G.R. No. 145561",
        "2005-06-15",
        "",
    )
    statuses: list[str] = []

    async def status(value: str) -> None:
        statuses.append(value)

    async with CaseRetriever(client) as retriever:
        result = await retriever.retrieve(case, tmp_path / "case.part", status)
    await client.aclose()
    assert result.success
    assert result.source == "Lawphil"
    assert (tmp_path / "case.part").exists()
    assert statuses == ["Searching Lawphil"]


@pytest.mark.asyncio
async def test_elibrary_fallback_is_used_when_lawphil_missing(tmp_path: Path) -> None:
    pdf = blank_pdf()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "lawphil.net" in url:
            return httpx.Response(404, request=request)
        if request.method == "POST":
            return httpx.Response(
                200,
                text=(
                    '<a class="file_link" href="?cmd=getdoc&DocId=48612">'
                    "[ G.R. NO. 145561. June 15, 2005 ]</a>"
                ),
                request=request,
            )
        if "cmd=getdoc" in url:
            return httpx.Response(
                200,
                text=(
                    "<html><body>G.R. NO. 145561. June 15, 2005"
                    '<a href="/assets/pdf/philrep/2005/GR145561.pdf">PDF</a></body></html>'
                ),
                request=request,
            )
        if url.endswith("GR145561.pdf"):
            return httpx.Response(
                200,
                content=pdf,
                headers={"content-type": "application/pdf"},
                request=request,
            )
        return httpx.Response(404, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    case = ParsedCase(
        1,
        "Week 2",
        "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda",
        "G.R. No. 145561",
        "2005-06-15",
        "",
    )
    statuses: list[str] = []

    async def status(value: str) -> None:
        statuses.append(value)

    async with CaseRetriever(client) as retriever:
        result = await retriever.retrieve(case, tmp_path / "case.part", status)
    await client.aclose()
    assert result.success
    assert result.source == "Supreme Court E-Library"
    assert statuses == ["Searching Lawphil", "Searching E-Library"]


@pytest.mark.asyncio
async def test_chanrobles_fallback_is_used_when_other_sources_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = blank_pdf()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "lawphil.net" in url:
            return httpx.Response(404, request=request)
        if "elibrary.judiciary.gov.ph" in url:
            return httpx.Response(200, text="<html></html>", request=request)
        if url.endswith("2005decisions.php?id=6"):
            return httpx.Response(
                200,
                text=(
                    '<a href="2005junedecisions.php?id=596">'
                    "G.R. No. 145561 - Honda Philippines</a>"
                ),
                request=request,
            )
        if url.endswith("2005junedecisions.php?id=596"):
            return httpx.Response(
                200,
                text="<html><body>G.R. No. 145561 : June 15, 2005</body></html>",
                request=request,
            )
        return httpx.Response(404, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    case = ParsedCase(
        1,
        "Week 2",
        "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda",
        "G.R. No. 145561",
        "2005-06-15",
        "",
    )
    statuses: list[str] = []

    async def status(value: str) -> None:
        statuses.append(value)

    async def render_pdf(_source: str, _url: str, output_path: Path) -> None:
        output_path.write_bytes(pdf)

    async with CaseRetriever(client) as retriever:
        monkeypatch.setattr(retriever, "_render_pdf", render_pdf)
        result = await retriever.retrieve(case, tmp_path / "case.part", status)
    await client.aclose()
    assert result.success
    assert result.source == "ChanRobles"
    assert result.source_url.endswith("2005junedecisions.php?id=596")
    assert statuses == [
        "Searching Lawphil",
        "Searching E-Library",
        "Searching ChanRobles",
    ]


@pytest.mark.asyncio
async def test_browser_is_used_when_chanrobles_rejects_plain_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    browser_calls: list[str] = []

    async def browser_html(_source: str, url: str) -> tuple[str, str]:
        browser_calls.append(url)
        return "<html>Browser response</html>", url

    async with CaseRetriever(client) as retriever:
        monkeypatch.setattr(retriever, "_browser_html", browser_html)
        content, url = await retriever._html_with_browser_fallback(
            "ChanRobles", "https://chanrobles.com/cralaw/2005decisions.php?id=6"
        )
    await client.aclose()
    assert content == "<html>Browser response</html>"
    assert url == browser_calls[0]


@pytest.mark.asyncio
async def test_verified_supreme_court_pdf_is_saved_directly(tmp_path: Path) -> None:
    pdf = blank_pdf()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=pdf,
            headers={"content-type": "application/pdf"},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    source_url = "https://sc.judiciary.gov.ph/wp-content/uploads/case.pdf"
    case = ParsedCase(
        1,
        "Week 2",
        "Example v. Respondent",
        "G.R. No. 145561",
        "2005-06-15",
        "",
        metadata_source="Supreme Court",
        metadata_source_url=source_url,
    )

    async def status(_value: str) -> None:
        return None

    target = tmp_path / "direct.part"
    async with CaseRetriever(client) as retriever:
        result = await retriever.retrieve(case, target, status)
    await client.aclose()
    assert result.success
    assert result.source == "Supreme Court"
    assert result.source_url == source_url
    assert target.read_bytes().startswith(b"%PDF")


@pytest.mark.asyncio
async def test_lawphil_render_removes_cookie_notice(tmp_path: Path) -> None:
    case = ParsedCase(
        1,
        "Week 2",
        "Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda",
        "G.R. No. 145561",
        "2005-06-15",
        "",
    )

    async def status(_: str) -> None:
        return None

    target = tmp_path / "honda.part"
    async with CaseRetriever() as retriever:
        result = await retriever.retrieve(case, target, status)
    assert result.success
    reader = PdfReader(str(target))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    assert "We use cookies to ensure" not in text
    assert "By continuing to browse our site" not in text
