from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Browser, Playwright, async_playwright

from .config import MAX_ATTEMPTS, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT_SECONDS
from .domain import ParsedCase, RetrievalAttempt, RetrievalResult
from .parser import DATE_RE, docket_tokens
from .pdf_utils import validate_pdf


StatusCallback = Callable[[str], Awaitable[None]]

MONTH_CODES = {
    1: "jan",
    2: "feb",
    3: "mar",
    4: "apr",
    5: "may",
    6: "jun",
    7: "jul",
    8: "aug",
    9: "sep",
    10: "oct",
    11: "nov",
    12: "dec",
}
ALLOWED_HOSTS = {
    "lawphil.net",
    "www.lawphil.net",
    "elibrary.judiciary.gov.ph",
    "chanrobles.com",
    "www.chanrobles.com",
}
ELIBRARY_SEARCH_URL = (
    "https://elibrary.judiciary.gov.ph/assets/dtSearch/"
    "dtSearch_system_files/dtisapi6.dll"
)
ELIBRARY_INDEX = "*{81aab6aa66450ae68a1f722f505a17c7} sc_decision"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _friendly_http_error(status: int) -> tuple[str, str]:
    if status in (401, 403):
        return "ACCESS_BLOCKED", f"Website refused access (HTTP {status})"
    if status == 404:
        return "NOT_FOUND", "Page was not found"
    if status == 429:
        return "RATE_LIMITED", "Website rate limit reached"
    if 500 <= status:
        return "SITE_UNAVAILABLE", f"Website returned HTTP {status}"
    return "HTTP_ERROR", f"Website returned HTTP {status}"


def _looks_blocked(content: str) -> bool:
    lower = content.lower()
    indicators = (
        "captcha",
        "access denied",
        "please login to search",
        "cloudflare ray id",
        "temporarily blocked",
    )
    return any(value in lower for value in indicators)


def _normalize_url(url: str, base: str) -> str:
    absolute = urljoin(base, url)
    parsed = urlparse(absolute)
    if parsed.scheme == "http" and parsed.hostname in ALLOWED_HOSTS:
        absolute = parsed._replace(scheme="https").geturl()
    return absolute


def _assert_allowed(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"Blocked non-approved source URL: {url}")


def _date_matches(text: str, iso_date: str) -> bool:
    if not iso_date:
        return False
    try:
        expected = datetime.fromisoformat(iso_date)
    except ValueError:
        return False
    pattern = re.compile(
        rf"\b{re.escape(expected.strftime('%B'))}\s+0?{expected.day},\s+{expected.year}\b",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


def _primary_docket_label(docket: str) -> str:
    l_match = re.search(r"\bL-\d{3,}\b", docket, re.IGNORECASE)
    if l_match:
        return f"G.R. No. {l_match.group(0).upper()}"
    tokens = docket_tokens(docket)
    return f"G.R. No. {tokens[0]}" if tokens else docket


class CaseRetriever:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "CasePDFDownloader/1.0 "
                    "(personal legal research; polite automated retrieval)"
                )
            },
        )
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.attempts: list[RetrievalAttempt] = []

    async def __aenter__(self) -> "CaseRetriever":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        if self._owns_client:
            await self.client.aclose()

    async def _request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        _assert_allowed(url)
        last_error: Exception | None = None
        for attempt_no in range(1, MAX_ATTEMPTS + 1):
            started = utc_now()
            try:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)
                response = await self.client.request(method, url, data=data)
                content_type = response.headers.get("content-type", "")
                preview = ""
                if "text" in content_type or "html" in content_type:
                    preview = response.text[:200_000]
                if _looks_blocked(preview):
                    record = RetrievalAttempt(
                        source,
                        str(response.url),
                        attempt_no,
                        "Failed",
                        "ACCESS_BLOCKED",
                        "Website presented a CAPTCHA, login, or access-block page",
                        started,
                        utc_now(),
                    )
                    self.attempts.append(record)
                    raise PermissionError(record.error_message)
                if response.status_code < 400:
                    self.attempts.append(
                        RetrievalAttempt(
                            source,
                            str(response.url),
                            attempt_no,
                            "Success",
                            started_at=started,
                            finished_at=utc_now(),
                        )
                    )
                    return response
                code, message = _friendly_http_error(response.status_code)
                self.attempts.append(
                    RetrievalAttempt(
                        source,
                        str(response.url),
                        attempt_no,
                        "Failed",
                        code,
                        message,
                        started,
                        utc_now(),
                    )
                )
                if response.status_code in (401, 403, 404) or attempt_no == MAX_ATTEMPTS:
                    response.raise_for_status()
                delay = float(response.headers.get("Retry-After", 0) or 0)
                await asyncio.sleep(max(delay, (2**attempt_no) + random.random()))
            except PermissionError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                code = "TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "NETWORK_ERROR"
                self.attempts.append(
                    RetrievalAttempt(
                        source,
                        url,
                        attempt_no,
                        "Failed",
                        code,
                        str(exc) or code.replace("_", " ").title(),
                        started,
                        utc_now(),
                    )
                )
                if attempt_no < MAX_ATTEMPTS:
                    await asyncio.sleep((2**attempt_no) + random.random())
            except httpx.HTTPStatusError as exc:
                last_error = exc
                break
        if last_error:
            raise last_error
        raise RuntimeError("Request failed without a response")

    async def _ensure_browser(self) -> Browser:
        if self._browser:
            return self._browser
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            return self._browser
        except Exception as exc:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            raise RuntimeError(
                "PDF browser engine is unavailable. Run the app setup to install Chromium."
            ) from exc

    async def _browser_html(self, source: str, url: str) -> tuple[str, str]:
        _assert_allowed(url)
        started = utc_now()
        context = None
        page = None
        try:
            await asyncio.sleep(REQUEST_DELAY_SECONDS)
            browser = await self._ensure_browser()
            context = await browser.new_context()
            page = await context.new_page()
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=90_000
            )
            if not response or response.status >= 400:
                status = response.status if response else "no response"
                raise RuntimeError(f"Could not load source page ({status})")
            content = await page.content()
            if _looks_blocked(content):
                raise PermissionError(
                    "Website presented a CAPTCHA, login, or access-block page"
                )
            final_url = page.url
            _assert_allowed(final_url)
            self.attempts.append(
                RetrievalAttempt(
                    source,
                    final_url,
                    1,
                    "Success",
                    started_at=started,
                    finished_at=utc_now(),
                )
            )
            return content, final_url
        except Exception as exc:
            code = "ACCESS_BLOCKED" if isinstance(exc, PermissionError) else "BROWSER_LOAD_FAILED"
            self.attempts.append(
                RetrievalAttempt(
                    source,
                    url,
                    1,
                    "Failed",
                    code,
                    str(exc),
                    started,
                    utc_now(),
                )
            )
            raise
        finally:
            if page:
                await page.close()
            if context:
                await context.close()

    async def _html_with_browser_fallback(
        self, source: str, url: str
    ) -> tuple[str, str]:
        try:
            response = await self._request(source, "GET", url)
            return response.text, str(response.url)
        except PermissionError:
            return await self._browser_html(source, url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 403:
                raise
            return await self._browser_html(source, url)

    async def _render_pdf(self, source: str, url: str, output_path: Path) -> None:
        _assert_allowed(url)
        started = utc_now()
        try:
            browser = await self._ensure_browser()
            context = await browser.new_context()
            if urlparse(url).hostname in {"lawphil.net", "www.lawphil.net"}:
                await context.add_cookies(
                    [
                        {
                            "name": "Lawphil",
                            "value": "TheLawphilProject",
                            "domain": "lawphil.net",
                            "path": "/",
                        }
                    ]
                )
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                if not response or response.status >= 400:
                    status = response.status if response else "no response"
                    raise RuntimeError(f"Could not load decision page ({status})")
                content = await page.content()
                if _looks_blocked(content):
                    raise PermissionError("Website presented a CAPTCHA, login, or access-block page")
                # Lawphil writes its cookie notice into the DOM. The cookie prevents it
                # on normal visits; removing the node is a second safeguard for printing.
                await page.evaluate(
                    """
                    () => {
                        for (const selector of [
                            '#cookie_box',
                            '.cookie_box',
                            '[class*="cookie-consent"]',
                            '[id*="cookie-consent"]'
                        ]) {
                            document.querySelectorAll(selector).forEach((node) => node.remove());
                        }
                    }
                    """
                )
                await page.emulate_media(media="print")
                await page.pdf(
                    path=str(output_path),
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "14mm",
                        "right": "14mm",
                        "bottom": "14mm",
                        "left": "14mm",
                    },
                )
            finally:
                await page.close()
                await context.close()
            self.attempts.append(
                RetrievalAttempt(
                    source,
                    url,
                    1,
                    "Success",
                    started_at=started,
                    finished_at=utc_now(),
                )
            )
        except Exception as exc:
            code = "ACCESS_BLOCKED" if isinstance(exc, PermissionError) else "PDF_RENDER_FAILED"
            self.attempts.append(
                RetrievalAttempt(
                    source,
                    url,
                    1,
                    "Failed",
                    code,
                    str(exc),
                    started,
                    utc_now(),
                )
            )
            raise

    async def _download_or_render(
        self,
        case: ParsedCase,
        source: str,
        page_url: str,
        page_html: str,
        output_path: Path,
    ) -> tuple[bool, str, list[str]]:
        soup = BeautifulSoup(page_html, "html.parser")
        pdf_urls: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            if ".pdf" in href.lower():
                candidate = _normalize_url(href, page_url)
                if urlparse(candidate).hostname in ALLOWED_HOSTS:
                    pdf_urls.append(candidate)

        warnings: list[str] = []
        for pdf_url in dict.fromkeys(pdf_urls):
            try:
                response = await self._request(source, "GET", pdf_url)
                output_path.write_bytes(response.content)
                validation = validate_pdf(output_path, case.docket, case.decision_date)
                if validation.valid:
                    warnings.extend(validation.warnings)
                    return True, pdf_url, warnings
                warnings.append(f"Source PDF rejected: {validation.error_message}")
                output_path.unlink(missing_ok=True)
            except Exception as exc:
                warnings.append(f"Source PDF failed: {exc}")
                output_path.unlink(missing_ok=True)

        try:
            await self._render_pdf(source, page_url, output_path)
            validation = validate_pdf(output_path, case.docket, case.decision_date)
            if validation.valid:
                warnings.extend(validation.warnings)
                return True, page_url, warnings
            warnings.append(validation.error_message)
            output_path.unlink(missing_ok=True)
        except Exception as exc:
            warnings.append(str(exc))
            output_path.unlink(missing_ok=True)
        return False, page_url, warnings

    async def _lawphil(
        self, case: ParsedCase, output_path: Path
    ) -> tuple[bool, str, list[str], str, str]:
        try:
            parsed_date = datetime.fromisoformat(case.decision_date)
        except ValueError:
            return False, "", [], "INVALID_DATE", "Decision date is invalid"
        month = MONTH_CODES[parsed_date.month]
        year = parsed_date.year
        index_url = (
            f"https://lawphil.net/judjuris/juri{year}/{month}{year}/{month}{year}.html"
        )
        try:
            index_response = await self._request("Lawphil", "GET", index_url)
        except PermissionError as exc:
            return False, index_url, [], "ACCESS_BLOCKED", str(exc)
        except Exception as exc:
            return False, index_url, [], "LAWPHIL_INDEX_ERROR", str(exc)

        soup = BeautifulSoup(index_response.text, "html.parser")
        tokens = docket_tokens(case.docket)
        if not tokens:
            return False, index_url, [], "INVALID_DOCKET", "No docket number could be parsed"
        primary = tokens[0]
        candidates: list[str] = []
        for anchor in soup.find_all("a", href=True):
            container = anchor.find_parent(["tr", "li"]) or anchor.parent
            context = (
                container.get_text(" ", strip=True)
                if container
                else anchor.get_text(" ", strip=True)
            )
            searchable = re.sub(r"\D", "", f"{anchor.get_text(' ', strip=True)} {anchor['href']}")
            if primary not in searchable:
                continue
            if case.decision_date and not _date_matches(context, case.decision_date):
                continue
            url = _normalize_url(str(anchor["href"]), str(index_response.url))
            if urlparse(url).hostname in ALLOWED_HOSTS:
                candidates.append(url)
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            return False, index_url, [], "NOT_FOUND", "No matching Lawphil case was found"
        if len(candidates) > 1:
            return (
                False,
                index_url,
                [],
                "AMBIGUOUS_MATCH",
                f"Lawphil returned {len(candidates)} matching decision links",
            )
        page_url = candidates[0]
        try:
            page_response = await self._request("Lawphil", "GET", page_url)
        except Exception as exc:
            return False, page_url, [], "LAWPHIL_PAGE_ERROR", str(exc)
        page_text = BeautifulSoup(page_response.text, "html.parser").get_text(" ", strip=True)
        compact = re.sub(r"\D", "", page_text[:10_000])
        if primary not in compact or not _date_matches(page_text[:10_000], case.decision_date):
            return (
                False,
                page_url,
                [],
                "IDENTITY_MISMATCH",
                "Lawphil page did not match the expected docket number and date",
            )
        success, used_url, warnings = await self._download_or_render(
            case, "Lawphil", page_url, page_response.text, output_path
        )
        if success:
            return True, used_url, warnings, "", ""
        return False, page_url, warnings, "PDF_CREATION_FAILED", "Lawphil document could not be saved as a valid PDF"

    async def _elibrary(
        self, case: ParsedCase, output_path: Path
    ) -> tuple[bool, str, list[str], str, str]:
        query = _primary_docket_label(case.docket)
        form = {
            "cmd": "search",
            "request": query,
            "searchType": "phrase",
            "index": ELIBRARY_INDEX,
            "sort": "Hits",
            "SearchForm": "%%SearchForm%%",
            "OrigSearchForm": (
                "https://elibrary.judiciary.gov.ph/assets/dtSearch/"
                "dtSearch_default_form/dtSearch_form.html"
            ),
            "fuzziness": "3",
            "pageSize": "10",
            "autoTermWeight": "1",
            "fileConditions": "",
            "booleanConditions": "",
        }
        try:
            search_response = await self._request(
                "Supreme Court E-Library", "POST", ELIBRARY_SEARCH_URL, data=form
            )
        except PermissionError as exc:
            return False, ELIBRARY_SEARCH_URL, [], "ACCESS_BLOCKED", str(exc)
        except Exception as exc:
            return False, ELIBRARY_SEARCH_URL, [], "ELIBRARY_SEARCH_ERROR", str(exc)

        soup = BeautifulSoup(search_response.text, "html.parser")
        tokens = docket_tokens(case.docket)
        if not tokens:
            return False, ELIBRARY_SEARCH_URL, [], "INVALID_DOCKET", "No docket number could be parsed"
        primary = tokens[0]
        candidates: list[str] = []
        for anchor in soup.select("a.file_link[href]"):
            heading = anchor.get_text(" ", strip=True)
            if primary not in re.sub(r"\D", "", heading):
                continue
            if not _date_matches(heading, case.decision_date):
                continue
            candidates.append(_normalize_url(str(anchor["href"]), str(search_response.url)))
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            return False, ELIBRARY_SEARCH_URL, [], "NOT_FOUND", "No matching E-Library case was found"
        if len(candidates) > 1:
            return (
                False,
                ELIBRARY_SEARCH_URL,
                [],
                "AMBIGUOUS_MATCH",
                f"E-Library returned {len(candidates)} matching decision links",
            )
        page_url = candidates[0]
        try:
            page_response = await self._request("Supreme Court E-Library", "GET", page_url)
        except Exception as exc:
            return False, page_url, [], "ELIBRARY_PAGE_ERROR", str(exc)
        page_text = BeautifulSoup(page_response.text, "html.parser").get_text(" ", strip=True)
        if primary not in re.sub(r"\D", "", page_text[:10_000]) or not _date_matches(
            page_text[:10_000], case.decision_date
        ):
            return (
                False,
                page_url,
                [],
                "IDENTITY_MISMATCH",
                "E-Library page did not match the expected docket number and date",
            )
        success, used_url, warnings = await self._download_or_render(
            case,
            "Supreme Court E-Library",
            page_url,
            page_response.text,
            output_path,
        )
        if success:
            return True, used_url, warnings, "", ""
        return False, page_url, warnings, "PDF_CREATION_FAILED", "E-Library document could not be saved as a valid PDF"

    async def _chanrobles(
        self, case: ParsedCase, output_path: Path
    ) -> tuple[bool, str, list[str], str, str]:
        try:
            parsed_date = datetime.fromisoformat(case.decision_date)
        except ValueError:
            return False, "", [], "INVALID_DATE", "Decision date is invalid"
        index_url = (
            f"https://chanrobles.com/cralaw/{parsed_date.year}decisions.php"
            f"?id={parsed_date.month}"
        )
        try:
            index_html, resolved_index_url = await self._html_with_browser_fallback(
                "ChanRobles", index_url
            )
        except PermissionError as exc:
            return False, index_url, [], "ACCESS_BLOCKED", str(exc)
        except Exception as exc:
            return False, index_url, [], "CHANROBLES_INDEX_ERROR", str(exc)

        tokens = docket_tokens(case.docket)
        if not tokens:
            return False, index_url, [], "INVALID_DOCKET", "No docket number could be parsed"
        primary = tokens[0]
        soup = BeautifulSoup(index_html, "html.parser")
        candidates: list[str] = []
        for anchor in soup.find_all("a", href=True):
            if primary not in re.sub(r"\D", "", anchor.get_text(" ", strip=True)):
                continue
            url = _normalize_url(str(anchor["href"]), resolved_index_url)
            if urlparse(url).hostname in ALLOWED_HOSTS:
                candidates.append(url)
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            return False, index_url, [], "NOT_FOUND", "No matching ChanRobles case was found"
        if len(candidates) > 1:
            return (
                False,
                index_url,
                [],
                "AMBIGUOUS_MATCH",
                f"ChanRobles returned {len(candidates)} matching decision links",
            )

        page_url = candidates[0]
        try:
            page_html, resolved_page_url = await self._html_with_browser_fallback(
                "ChanRobles", page_url
            )
        except Exception as exc:
            return False, page_url, [], "CHANROBLES_PAGE_ERROR", str(exc)
        page_text = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)
        if primary not in re.sub(r"\D", "", page_text[:10_000]) or not _date_matches(
            page_text[:10_000], case.decision_date
        ):
            return (
                False,
                page_url,
                [],
                "IDENTITY_MISMATCH",
                "ChanRobles page did not match the expected docket number and date",
            )
        success, used_url, warnings = await self._download_or_render(
            case, "ChanRobles", resolved_page_url, page_html, output_path
        )
        if success:
            return True, used_url, warnings, "", ""
        return (
            False,
            page_url,
            warnings,
            "PDF_CREATION_FAILED",
            "ChanRobles document could not be saved as a valid PDF",
        )

    async def retrieve(
        self,
        case: ParsedCase,
        output_path: Path,
        status_callback: StatusCallback,
    ) -> RetrievalResult:
        self.attempts = []
        await status_callback("Searching Lawphil")
        lawphil = await self._lawphil(case, output_path)
        if lawphil[0]:
            return RetrievalResult(
                True,
                "Lawphil",
                lawphil[1],
                str(output_path),
                attempts=list(self.attempts),
                warnings=lawphil[2],
            )
        output_path.unlink(missing_ok=True)
        await status_callback("Searching E-Library")
        elibrary = await self._elibrary(case, output_path)
        if elibrary[0]:
            return RetrievalResult(
                True,
                "Supreme Court E-Library",
                elibrary[1],
                str(output_path),
                attempts=list(self.attempts),
                warnings=lawphil[2] + elibrary[2],
            )
        output_path.unlink(missing_ok=True)
        await status_callback("Searching ChanRobles")
        chanrobles = await self._chanrobles(case, output_path)
        if chanrobles[0]:
            return RetrievalResult(
                True,
                "ChanRobles",
                chanrobles[1],
                str(output_path),
                attempts=list(self.attempts),
                warnings=lawphil[2] + elibrary[2] + chanrobles[2],
            )
        output_path.unlink(missing_ok=True)
        error_code = chanrobles[3] or elibrary[3] or lawphil[3] or "NOT_FOUND"
        error_message = (
            f"Lawphil: {lawphil[4] or 'not saved'}; "
            f"E-Library: {elibrary[4] or 'not saved'}; "
            f"ChanRobles: {chanrobles[4] or 'not saved'}"
        )
        return RetrievalResult(
            False,
            error_code=error_code,
            error_message=error_message,
            attempts=list(self.attempts),
            warnings=lawphil[2] + elibrary[2] + chanrobles[2],
        )
