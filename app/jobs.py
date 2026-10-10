from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from .config import DOWNLOAD_CONCURRENCY
from .database import Database
from .domain import ParsedCase
from .verification import VerificationResult
from .parser import parse_text
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

        seen: dict[tuple[str, str], tuple[str, str, str]] = {}
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
                seen[key] = (normalized_docket, item.get("decision_date", ""), item.get("reporter_citation", "").casefold())
                continue
            first_docket, first_date, first_citation = seen[key]
            if (first_docket == normalized_docket and first_date == item.get("decision_date", "")
                    and first_citation == item.get("reporter_citation", "").casefold()):
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
        self, run_id: str, item: dict[str, Any], output_dir: Path,
        retriever: CaseRetriever, resolver: CaseResolver,
    ) -> None:
        case_id = item["id"]
        original = item.get("original_input") or {}
        if not original:
            # Historical inferred metadata must never be promoted to original evidence.
            parsed_rows = parse_text(item.get("raw_text", ""))
            if parsed_rows:
                original = parsed_rows[0].as_dict()
            elif not item.get("metadata_source_url"):
                original = item
            else:
                original = {"title": item["title"]}
            original = {key: original.get(key, "") for key in ("title", "docket", "decision_date", "reporter_citation")}
            self.database.update_case(case_id, original_input=original)
        lookup = ParsedCase(item["ordinal"], item["week"], original.get("title", item["title"]),
            original.get("docket", ""), original.get("decision_date", ""), item.get("raw_text", ""),
            reporter_citation=original.get("reporter_citation", ""))
        selected = None
        needs_resolution = bool(not lookup.docket or not lookup.decision_date or lookup.reporter_citation or item.get("metadata_source_url"))
        if needs_resolution:
            self.database.update_case(case_id, status="Searching case details")
            try:
                fetcher = getattr(retriever, "fetch_text", None)
                candidates = await resolver.candidates(lookup, fetcher)
            except GoogleSearchBlocked as exc:
                candidates = exc.candidates
                if not any(candidate_is_high_confidence(c) for c in candidates):
                    self.database.set_case_candidates(case_id, [c.as_dict() for c in candidates[:5]])
                    evidence = (candidates[0].verification if candidates and candidates[0].verification
                                else VerificationResult(message="No conclusive direct-source match; Google is unavailable").as_dict())
                    self.database.update_case(case_id, status="Needs review", error_code="GOOGLE_SEARCH_BLOCKED",
                        error_message=f"{evidence.get('message', 'Insufficient identity evidence')}. {exc}",
                        manual_search_url=exc.manual_search_url, verification=evidence,
                        failure_stage="identity" if candidates else "discovery")
                    return
            except Exception as exc:
                self.database.update_case(case_id, status="Needs review", error_code="METADATA_LOOKUP_FAILED",
                    error_message=f"Could not verify case identity: {exc}", verification=VerificationResult().as_dict())
                return
            verified = [c for c in candidates if candidate_is_high_confidence(c)]
            selected_url = item.get("metadata_source_url", "")
            selected = (next((c for c in verified if c.source_url == selected_url), None)
                        if selected_url else verified[0] if verified else None)
            if not selected or candidates_are_ambiguous(candidates):
                self.database.set_case_candidates(case_id, [c.as_dict() for c in candidates[:5]])
                evidence = candidates[0].verification if candidates else VerificationResult(message="No matching decision was found").as_dict()
                if selected_url and not selected:
                    requested = next((c for c in candidates if c.source_url == selected_url), None)
                    evidence = (requested.verification if requested and requested.verification else
                        VerificationResult(source_url=selected_url, message="Selected candidate could not be independently verified against the original identifiers").as_dict())
                self.database.update_case(case_id, status="Needs review", error_code="MATCH_REVIEW_REQUIRED",
                    error_message="Conflicting decisions require corrected identifiers" if candidates_are_ambiguous(candidates) else evidence.get("message", "Insufficient identity evidence"),
                    verification=evidence, manual_search_url=google_search_url(item.get("raw_text") or item["title"]))
                return
            item.update(docket=selected.docket, decision_date=selected.decision_date,
                reporter_citation=selected.reporter_citation, metadata_source=selected.source,
                metadata_source_url=selected.source_url)
            self.database.update_case(case_id, docket=selected.docket, decision_date=selected.decision_date,
                reporter_citation=selected.reporter_citation, metadata_source=selected.source,
                metadata_source_url=selected.source_url, verification=selected.verification)
            self.database.clear_case_candidates(case_id)
        else:
            item["docket"], item["decision_date"] = lookup.docket, lookup.decision_date
        week_dir = output_dir / safe_week_folder(item["week"])
        part_path = None
        final_path = week_dir / item["filename"]
        try:
            week_dir.mkdir(parents=True, exist_ok=True)
            if path_too_long(final_path):
                self.database.update_case(case_id, status="Not saved", error_code="PATH_TOO_LONG", error_message="Destination path is too long")
                return
            def validate(path, url=""):
                return validate_pdf(path, item["docket"], item["decision_date"], lookup.title, url)
            def evidence_for(validation):
                evidence = validation.verification.as_dict()
                if selected:
                    evidence["identity_verification"] = selected.verification
                    evidence["evidence"] = selected.verification.get("evidence", []) + evidence["evidence"]
                return evidence
            if final_path.exists():
                validation = validate(final_path, item.get("source_url", ""))
                self.database.update_case(case_id, status="Already saved" if validation.valid else "Not saved",
                    saved_path=str(final_path), verification=evidence_for(validation),
                    error_code="" if validation.valid else "EXISTING_FILE_INVALID",
                    error_message="" if validation.valid else f"Existing file preserved: {validation.error_message}")
                return
            part_path = week_dir / f".{item['filename']}.{uuid.uuid4().hex}.part"
            parsed = ParsedCase(item["ordinal"], item["week"], lookup.title, item["docket"],
                item["decision_date"], item.get("raw_text", ""),
                metadata_source=item.get("metadata_source", ""), metadata_source_url=item.get("metadata_source_url", ""))
            async def update_status(status):
                self.database.update_case(case_id, status=status)
            result = await retriever.retrieve(parsed, part_path, update_status)
            await self._record_attempts(run_id, case_id, result)
            if not result.success and not needs_resolution:
                self.database.update_case(case_id, status="Searching case details")
                try:
                    candidates = await resolver.candidates(lookup, getattr(retriever, "fetch_text", None))
                except GoogleSearchBlocked as exc:
                    candidates = exc.candidates
                    if not any(candidate_is_high_confidence(c) for c in candidates):
                        self.database.set_case_candidates(case_id, [c.as_dict() for c in candidates[:5]])
                        self.database.update_case(case_id, status="Needs review", error_code="GOOGLE_SEARCH_BLOCKED",
                            error_message=str(exc), manual_search_url=exc.manual_search_url,
                            verification=candidates[0].verification if candidates else VerificationResult().as_dict())
                        return
                selected = next((c for c in candidates if candidate_is_high_confidence(c)), None)
                if selected and not candidates_are_ambiguous(candidates):
                    parsed.metadata_source_url = selected.source_url
                    parsed.metadata_source = selected.source
                    self.database.update_case(case_id, metadata_source=selected.source, metadata_source_url=selected.source_url)
                    result = await retriever.retrieve(parsed, part_path, update_status)
                    await self._record_attempts(run_id, case_id, result)
                elif candidates:
                    self.database.set_case_candidates(case_id, [c.as_dict() for c in candidates[:5]])
                    self.database.update_case(case_id, status="Needs review", error_code="MATCH_REVIEW_REQUIRED",
                        error_message="No conclusive source identity match", verification=candidates[0].verification)
                    return
            if not result.success:
                self.database.update_case(case_id, status="Not saved", error_code=result.error_code,
                    error_message=result.error_message, warnings=result.warnings,
                    verification=result.verification or VerificationResult(message=result.error_message).as_dict())
                return
            validation = validate(part_path, result.source_url)
            if not validation.valid:
                self.database.update_case(case_id, status="Needs review", error_code=validation.error_code,
                    error_message=validation.error_message, verification=validation.verification.as_dict())
                return
            self.database.update_case(case_id, status="Saving")
            # Publish atomically without replacing an existing destination,
            # including files created by another run after the initial check.
            if os.name == "nt":
                os.rename(part_path, final_path)
            else:
                os.link(part_path, final_path)
            evidence = evidence_for(validation)
            self.database.update_case(case_id, status="Saved", saved_path=str(final_path),
                source=result.source, source_url=result.source_url, warnings=result.warnings,
                verification=evidence, error_code="", error_message="", manual_search_url="")
            if selected:
                cache_verified = getattr(resolver, "cache_verified", None)
                if cache_verified:
                    try:
                        cache_verified(lookup, selected)
                    except Exception as exc:
                        self.database.update_case(case_id, warnings=result.warnings + [f"Verified mapping cache could not be updated: {exc}"])
        except OSError as exc:
            self.database.update_case(case_id, status="Not saved", error_code="FILE_WRITE_ERROR", error_message=str(exc))
        except Exception as exc:
            self.database.update_case(case_id, status="Not saved", error_code="UNEXPECTED_ERROR", error_message=str(exc))
        finally:
            if part_path:
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
                        "Searching ChanRobles",
                        "Searching Lawphil",
                        "Searching E-Library",
                        "Searching Supreme Court",
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
