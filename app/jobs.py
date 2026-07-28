from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .database import Database
from .domain import ParsedCase
from .filenames import filename_for_title, path_too_long, safe_week_folder
from .pdf_utils import validate_pdf
from .reporting import create_fallback_reports, create_xlsx_report
from .retrieval import CaseRetriever


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
    ) -> None:
        case_id = item["id"]
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
        )

        async def update_status(status: str) -> None:
            self.database.update_case(case_id, status=status)

        try:
            result = await retriever.retrieve(parsed, part_path, update_status)
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

    def _write_report(self, run_id: str, output_dir: Path) -> str:
        run = self.database.get_run(run_id)
        if not run:
            return ""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = output_dir / f"case-download-report_{timestamp}.xlsx"
        try:
            return str(create_xlsx_report(run, report_path))
        except Exception as xlsx_error:
            try:
                paths = create_fallback_reports(run)
                self.database.update_run(
                    run_id,
                    error_message=(
                        f"Excel report failed ({xlsx_error}); fallback reports were created"
                    ),
                )
                return "; ".join(str(path) for path in paths)
            except Exception as fallback_error:
                self.database.update_run(
                    run_id,
                    error_message=(
                        f"Report creation failed: {xlsx_error}; fallback failed: {fallback_error}"
                    ),
                )
                return ""

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
                )
        else:
            cases = [item for item in run["cases"] if item["status"] == "Queued"]
        cancelled = False
        try:
            async with CaseRetriever() as retriever:
                for index, item in enumerate(cases):
                    if cancel_event.is_set():
                        cancelled = True
                        for remaining in cases[index:]:
                            if remaining["status"] == "Queued":
                                self.database.update_case(
                                    remaining["id"],
                                    status="Cancelled",
                                    error_code="CANCELLED",
                                    error_message="Run was cancelled by the user",
                                )
                        break
                    await self._process_case(run_id, item, output_dir, retriever)
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
            report_path = await asyncio.to_thread(self._write_report, run_id, output_dir)
            final_run = self.database.get_run(run_id)
            current_status = final_run["status"] if final_run else "Failed"
            if current_status != "Failed":
                current_status = "Cancelled" if cancelled else "Completed"
            self.database.update_run(
                run_id, status=current_status, report_path=report_path
            )
            self.cancel_events.pop(run_id, None)
