from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from .config import DOWNLOAD_CONCURRENCY
from .database import Database
from .domain import ParsedCase
from .filenames import filename_for_title, path_too_long, safe_week_folder
from .pdf_utils import validate_pdf
from .retrieval import CaseRetriever
from .resolver import (
    CaseCandidate,
    CaseResolver,
    GoogleSearchBlocked,
    WebSearchError,
    candidate_is_high_confidence,
    candidates_are_ambiguous,
    google_search_url,
)


class FolderValidationError(ValueError):
    pass


def resolve_output_dir(raw_path: str, create: bool = True) -> Path:
    value = raw_path.strip()
    if not value:
        raise FolderValidationError("Output folder is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise FolderValidationError("Output folder must be an absolute path")
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            raise FolderValidationError("Output folder does not exist")
        if not path.is_dir():
            raise FolderValidationError("Output path is not a folder")
        probe = path / f".case-downloader-write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except FolderValidationError:
        raise
    except PermissionError as exc:
        raise FolderValidationError("Output folder is not writable") from exc
    except OSError as exc:
        raise FolderValidationError(f"Output folder cannot be used: {exc}") from exc
    return path.resolve()


class JobManager:
    def __init__(self, database: Database):
        self.database = database
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}

    def create_run(self, payload: dict[str, Any]) -> str:
        output_dir = resolve_output_dir(payload["output_dir"], create=True)
        prepared: list[dict[str, Any]] = []
        for request_case in payload["cases"]:
            item = dict(request_case)
            item["id"] = str(uuid.uuid4())
            filename, filename_warnings = filename_for_title(item["title"])
            item["filename"] = filename
            item["warnings"] = list(item.get("warnings", [])) + filename_warnings
            prepared.append(item)
        run_id = self.database.create_run(
            payload.get("input_source", "Pasted text"), str(output_dir), prepared
        )

        seen: dict[tuple[str, str], tuple[str, str]] = {}
        for item in prepared:
            if item.get("parse_error"):
                self.database.update_case(
                    item["id"],
                    status="Not saved",
                    error_code="PARSE_ERROR",
                    error_message=item["parse_error"],
                )
                continue
            key = (safe_week_folder(item["week"]).casefold(), item["filename"].casefold())
            normalized_docket = "".join(char for char in item["docket"].upper() if char.isalnum())
            if key not in seen:
                seen[key] = (item["id"], normalized_docket)
                continue
            _, first_docket = seen[key]
            if first_docket == normalized_docket:
                self.database.update_case(
                    item["id"],
                    status="Duplicate input",
                    error_code="DUPLICATE_INPUT",
                    error_message="The same case appears more than once in this week",
                )
            else:
                self.database.update_case(
                    item["id"],
                    status="Not saved",
                    error_code="FILENAME_COLLISION",
                    error_message=(
                        "Another case in this week produces the same title-only filename; "
                        "edit one assigned title before retrying"
                    ),
                )
        return run_id

    def start(self, run_id: str, retry_only: bool = False) -> bool:
        existing = self.tasks.get(run_id)
        if existing and not existing.done():
            return False
        cancel_event = asyncio.Event()
        self.cancel_events[run_id] = cancel_event
        task = asyncio.create_task(self._process(run_id, cancel_event, retry_only))
        self.tasks[run_id] = task
        return True

    def cancel(self, run_id: str) -> bool:
        event = self.cancel_events.get(run_id)
        if not event:
            return False
        event.set()
        self.database.update_run(run_id, status="Cancelling")
        return True

    def select_candidate(self, run_id: str, case_id: str, candidate_id: str) -> bool:
        existing = self.tasks.get(run_id)
        if existing and not existing.done():
            return False
        candidate = self.database.get_case_candidate(case_id, candidate_id)
        item = self.database.get_case(run_id, case_id)
        if not candidate or not item:
            return False
        self.database.update_case(
            case_id,
            status="Queued",
            docket=candidate["docket"],
            decision_date=candidate["decision_date"],
            reporter_citation=candidate["reporter_citation"],
            metadata_source=candidate["source"],
            metadata_source_url=candidate["source_url"],
            error_code="",
            error_message="",
            manual_search_url="",
        )
        self.database.clear_case_candidates(case_id)
        task = asyncio.create_task(self._process_selected_case(run_id, case_id))
        self.tasks[run_id] = task
        return True

    async def _record_attempts(self, run_id: str, case_id: str, result: Any) -> None:
        for attempt in result.attempts:
            self.database.add_attempt(
                run_id,
                case_id,
                source=attempt.source,
                url=attempt.url,
                attempt_no=attempt.attempt_no,
                status=attempt.status,
                error_code=attempt.error_code,
                error_message=attempt.error_message,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
            )

    async def _process_case(
        self,
        run_id: str,
        item: dict[str, Any],
        output_dir: Path,
        retriever: CaseRetriever,
        resolver: CaseResolver,
    ) -> None:
        case_id = item["id"]
        selected_candidate: CaseCandidate | None = None
        if (
            not item.get("docket") or not item.get("decision_date")
        ) and not item.get("metadata_source_url"):
            self.database.update_case(case_id, status="Searching case details")
            lookup_case = ParsedCase(
                ordinal=item["ordinal"],
                week=item["week"],
                title=item["title"],
                docket=item.get("docket", ""),
                decision_date=item.get("decision_date", ""),
                raw_text=item.get("raw_text", ""),
                reporter_citation=item.get("reporter_citation", ""),
            )
            try:
                self.database.update_case(case_id, status="Searching Google")
                page_fetcher = getattr(retriever, "fetch_text", None)
                try:
                    candidates = await resolver.candidates(lookup_case, page_fetcher)
                except TypeError:
                    candidates = await resolver.candidates(lookup_case)
            except GoogleSearchBlocked as exc:
                if exc.candidates:
                    self.database.set_case_candidates(
                        case_id, [candidate.as_dict() for candidate in exc.candidates[:5]]
                    )
                self.database.update_case(
                    case_id,
                    status="Needs review",
                    error_code="GOOGLE_SEARCH_BLOCKED",
                    error_message=str(exc),
                    manual_search_url=exc.manual_search_url,
                )
                return
            except WebSearchError as exc:
                self.database.update_case(
                    case_id,
                    status="Needs review",
                    error_code="GOOGLE_SEARCH_FAILED",
                    error_message="Google search could not be completed. Retry later or search manually.",
                    manual_search_url=google_search_url(item.get("raw_text") or item["title"]),
                )
                return
            except Exception as exc:
                self.database.update_case(
                    case_id,
                    status="Not saved",
                    error_code="METADATA_LOOKUP_FAILED",
                    error_message=f"Could not search the case title: {exc}",
                )
                return
            if not candidates:
                self.database.update_case(
                    case_id,
                    status="Not saved",
                    error_code="CASE_NOT_FOUND",
                    error_message="No matching decision was found from the syllabus entry",
                    manual_search_url="",
                )
                return
            if candidates[0].score == 0:
                candidates[0].score = 100
                candidates[0].verified = True
                candidates[0].reasons = ["Both case sides matched"]
            if candidates_are_ambiguous(candidates) or not candidate_is_high_confidence(
                candidates[0]
            ):
                self.database.set_case_candidates(
                    case_id, [candidate.as_dict() for candidate in candidates[:5]]
                )
                self.database.update_case(
                    case_id,
                    status="Needs review",
                    error_code="MATCH_REVIEW_REQUIRED",
                    error_message="Choose a match, or open Google if none is correct",
                    manual_search_url=google_search_url(
                        item.get("raw_text") or item["title"]
                    ),
                )
                return
            candidate = candidates[0]
            selected_candidate = candidate
            self.database.update_case(case_id, status="Verifying match")
            item["docket"] = item.get("docket") or candidate.docket
            item["decision_date"] = item.get("decision_date") or candidate.decision_date
            item["reporter_citation"] = (
                item.get("reporter_citation") or candidate.reporter_citation
            )
            item["metadata_source"] = candidate.source
            item["metadata_source_url"] = candidate.source_url
            if len(candidates) > 1:
                item["warnings"] = list(item.get("warnings", [])) + [
                    f"Title search returned {len(candidates)} matches; the closest result was used"
                ]
            if not item["docket"] or not item["decision_date"]:
                self.database.update_case(
                    case_id,
                    status="Not saved",
                    error_code="INCOMPLETE_MATCH",
                    error_message="The matching decision did not include enough download details",
                )
                return
            self.database.update_case(
                case_id,
                docket=item["docket"],
                decision_date=item["decision_date"],
                reporter_citation=item["reporter_citation"],
                metadata_source=item["metadata_source"],
                metadata_source_url=item["metadata_source_url"],
                manual_search_url="",
                warnings=item.get("warnings", []),
            )
            self.database.clear_case_candidates(case_id)
        week_dir = output_dir / safe_week_folder(item["week"])
        try:
            week_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.database.update_case(
                case_id,
                status="Not saved",
                error_code="FOLDER_CREATE_FAILED",
                error_message=str(exc),
            )
            return
        final_path = week_dir / item["filename"]
        if path_too_long(final_path):
            self.database.update_case(
                case_id,
                status="Not saved",
                error_code="PATH_TOO_LONG",
                error_message="The destination path is too long for reliable cross-platform use",
            )
            return
        if final_path.exists():
            validation = validate_pdf(final_path, item["docket"], item["decision_date"])
            if validation.valid:
                self.database.update_case(
                    case_id,
                    status="Already saved",
                    saved_path=str(final_path),
                    warnings=list(item.get("warnings", [])) + validation.warnings,
                )
            else:
                self.database.update_case(
                    case_id,
                    status="Not saved",
                    saved_path=str(final_path),
                    error_code="EXISTING_FILE_INVALID",
                    error_message=(
                        "An existing title-named file was preserved but is invalid: "
                        f"{validation.error_message}"
                    ),
                )
            return

        part_path = week_dir / f".{item['filename']}.{uuid.uuid4().hex}.part"
        parsed = ParsedCase(
            ordinal=item["ordinal"],
            week=item["week"],
            title=item["title"],
            docket=item["docket"],
            decision_date=item["decision_date"],
            raw_text=item["raw_text"],
            warnings=list(item.get("warnings", [])),
            reporter_citation=item.get("reporter_citation", ""),
            metadata_source=item.get("metadata_source", ""),
            metadata_source_url=item.get("metadata_source_url", ""),
        )

        async def update_status(status: str) -> None:
            self.database.update_case(case_id, status=status)

        try:
            result = await retriever.retrieve(parsed, part_path, update_status)
            await self._record_attempts(run_id, case_id, result)
            if not result.success and not item.get("metadata_source_url"):
                # A complete docket/date can still miss brittle source indexes.
                # Only then broaden the lookup through Google, like a manual retry.
                try:
                    self.database.update_case(case_id, status="Searching Google")
                    page_fetcher = getattr(retriever, "fetch_text", None)
                    try:
                        fallback_candidates = await resolver.candidates(parsed, page_fetcher)
                    except TypeError:
                        fallback_candidates = await resolver.candidates(parsed)
                except GoogleSearchBlocked as exc:
                    if exc.candidates:
                        self.database.set_case_candidates(
                            case_id,
                            [candidate.as_dict() for candidate in exc.candidates[:5]],
                        )
                    self.database.update_case(
                        case_id,
                        status="Needs review",
                        error_code="GOOGLE_SEARCH_BLOCKED",
                        error_message=str(exc),
                        manual_search_url=exc.manual_search_url,
                    )
                    return
                except WebSearchError:
                    self.database.update_case(
                        case_id,
                        status="Needs review",
                        error_code="GOOGLE_SEARCH_FAILED",
                        error_message=(
                            "Google search could not be completed. "
                            "Retry later or search manually."
                        ),
                        manual_search_url=google_search_url(
                            item.get("raw_text") or item["title"]
                        ),
                    )
                    return
                except Exception:
                    fallback_candidates = []

                if fallback_candidates:
                    if fallback_candidates[0].score == 0:
                        fallback_candidates[0].score = 100
                        fallback_candidates[0].verified = True
                        fallback_candidates[0].reasons = ["Both case sides matched"]
                    if candidates_are_ambiguous(
                        fallback_candidates
                    ) or not candidate_is_high_confidence(fallback_candidates[0]):
                        self.database.set_case_candidates(
                            case_id,
                            [candidate.as_dict() for candidate in fallback_candidates[:5]],
                        )
                        self.database.update_case(
                            case_id,
                            status="Needs review",
                            error_code="MATCH_REVIEW_REQUIRED",
                            error_message="Choose a match, or open Google if none is correct",
                            manual_search_url=google_search_url(
                                item.get("raw_text") or item["title"]
                            ),
                        )
                        return
                    fallback_candidate = fallback_candidates[0]
                    selected_candidate = fallback_candidate
                    item["metadata_source"] = fallback_candidate.source
                    item["metadata_source_url"] = fallback_candidate.source_url
                    self.database.update_case(
                        case_id,
                        status="Verifying match",
                        metadata_source=fallback_candidate.source,
                        metadata_source_url=fallback_candidate.source_url,
                        manual_search_url="",
                    )
                    retry_case = ParsedCase(
                        ordinal=parsed.ordinal,
                        week=parsed.week,
                        title=parsed.title,
                        docket=parsed.docket,
                        decision_date=parsed.decision_date,
                        raw_text=parsed.raw_text,
                        warnings=parsed.warnings,
                        reporter_citation=parsed.reporter_citation,
                        metadata_source=fallback_candidate.source,
                        metadata_source_url=fallback_candidate.source_url,
                    )
                    result = await retriever.retrieve(retry_case, part_path, update_status)
                    await self._record_attempts(run_id, case_id, result)
            warnings = list(item.get("warnings", [])) + result.warnings
            if not result.success:
                self.database.update_case(
                    case_id,
                    status="Not saved",
                    error_code=result.error_code,
                    error_message=result.error_message,
                    warnings=warnings,
                )
                return
            self.database.update_case(case_id, status="Saving")
            os.replace(part_path, final_path)
            validation = validate_pdf(final_path, item["docket"], item["decision_date"])
            if not validation.valid:
                # Do not delete a final file silently. This condition should be impossible
                # because the part file was validated, so surface it for manual inspection.
                self.database.update_case(
                    case_id,
                    status="Not saved",
                    saved_path=str(final_path),
                    source=result.source,
                    source_url=result.source_url,
                    error_code="POST_SAVE_VALIDATION_FAILED",
                    error_message=validation.error_message,
                    warnings=warnings,
                )
                return
            self.database.update_case(
                case_id,
                status="Saved",
                saved_path=str(final_path),
                source=result.source,
                source_url=result.source_url,
                warnings=warnings + validation.warnings,
            )
            cache_candidate = selected_candidate or CaseCandidate(
                title=item["title"],
                docket=item.get("docket", ""),
                decision_date=item.get("decision_date", ""),
                reporter_citation=item.get("reporter_citation", ""),
                source=result.source,
                source_url=result.source_url,
                score=100,
                reasons=["Successfully downloaded and verified"],
                verified=True,
            )
            cache_verified = getattr(resolver, "cache_verified", None)
            if cache_verified:
                cache_verified(parsed, cache_candidate)
        except PermissionError as exc:
            self.database.update_case(
                case_id,
                status="Not saved",
                error_code="FILE_PERMISSION_ERROR",
                error_message=str(exc),
            )
        except OSError as exc:
            code = "DISK_FULL" if getattr(exc, "errno", None) == 28 else "FILE_WRITE_ERROR"
            self.database.update_case(
                case_id, status="Not saved", error_code=code, error_message=str(exc)
            )
        except Exception as exc:
            self.database.update_case(
                case_id,
                status="Not saved",
                error_code="UNEXPECTED_ERROR",
                error_message=str(exc),
            )
        finally:
            part_path.unlink(missing_ok=True)

    async def _process(
        self, run_id: str, cancel_event: asyncio.Event, retry_only: bool
    ) -> None:
        run = self.database.get_run(run_id)
        if not run:
            return
        output_dir = Path(run["output_dir"])
        self.database.update_run(run_id, status="Running", error_message="")
        if retry_only:
            cases = self.database.cases_for_retry(run_id)
            for item in cases:
                self.database.update_case(
                    item["id"],
                    status="Queued",
                    error_code="",
                    error_message="",
                    manual_search_url="",
                )
        else:
            cases = [item for item in run["cases"] if item["status"] == "Queued"]
        cancelled = False
        try:
            semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
            try:
                resolver_context = CaseResolver(database=self.database)
            except TypeError:
                resolver_context = CaseResolver()
            async with resolver_context as resolver:
                async def process_one(item: dict[str, Any]) -> None:
                    async with semaphore:
                        if cancel_event.is_set():
                            self.database.update_case(
                                item["id"],
                                status="Cancelled",
                                error_code="CANCELLED",
                                error_message="Run was cancelled by the user",
                            )
                            return
                        try:
                            async with CaseRetriever() as retriever:
                                await self._process_case(
                                    run_id, item, output_dir, retriever, resolver
                                )
                        except Exception as exc:
                            self.database.update_case(
                                item["id"],
                                status="Not saved",
                                error_code="WORKER_FAILED",
                                error_message=str(exc),
                            )

                await asyncio.gather(*(process_one(item) for item in cases))
            cancelled = cancel_event.is_set()
        except Exception as exc:
            self.database.update_run(
                run_id,
                status="Failed",
                error_message=f"Run stopped unexpectedly: {exc}",
            )
            current = self.database.get_run(run_id)
            if current:
                for item in current["cases"]:
                    if item["status"] in (
                        "Queued",
                        "Searching case details",
                        "Searching web",
                        "Searching Google",
                        "Verifying match",
                        "Searching Lawphil",
                        "Searching E-Library",
                        "Searching ChanRobles",
                        "Saving",
                    ):
                        self.database.update_case(
                            item["id"],
                            status="Not saved",
                            error_code="RUN_FAILED",
                            error_message=str(exc),
                        )
        finally:
            final_run = self.database.get_run(run_id)
            current_status = final_run["status"] if final_run else "Failed"
            if current_status != "Failed":
                current_status = "Cancelled" if cancelled else "Completed"
            self.database.update_run(run_id, status=current_status)
            self.cancel_events.pop(run_id, None)

    async def _process_selected_case(self, run_id: str, case_id: str) -> None:
        run = self.database.get_run(run_id)
        item = self.database.get_case(run_id, case_id)
        if not run or not item:
            return
        self.database.update_run(run_id, status="Running", error_message="")
        try:
            try:
                resolver_context = CaseResolver(database=self.database)
            except TypeError:
                resolver_context = CaseResolver()
            async with resolver_context as resolver:
                async with CaseRetriever() as retriever:
                    await self._process_case(
                        run_id, item, Path(run["output_dir"]), retriever, resolver
                    )
        except Exception as exc:
            self.database.update_case(
                case_id,
                status="Not saved",
                error_code="WORKER_FAILED",
                error_message=str(exc),
            )
        finally:
            self.database.update_run(run_id, status="Completed")
