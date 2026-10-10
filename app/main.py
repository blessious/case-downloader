from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import APP_NAME, default_output_dir, resource_path
from .database import Database
from .filenames import filename_for_title, safe_week_folder
from .jobs import FolderValidationError, JobManager, resolve_output_dir
from .parser import extract_docx_text, extract_pdf_text, parse_text
from .resolver import resolve_case_rows
from .schemas import (
    CandidateSelectionRequest,
    FolderRequest,
    ResolveCasesRequest,
    RunRequest,
    TextParseRequest,
)


database = Database()
jobs = JobManager(database)
app = FastAPI(title=APP_NAME, version=__version__)

STATIC_DIR = resource_path("app", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def config() -> dict[str, object]:
    return {
        "default_output_dir": str(default_output_dir()),
        "version": app.version,
    }


def _parsed_response(text: str) -> dict[str, object]:
    parsed = parse_text(text)
    rows = []
    for item in parsed:
        data = item.as_dict()
        data["original_input"] = {key: getattr(item, key) for key in ("title", "docket", "decision_date", "reporter_citation")}
        filename, filename_warnings = filename_for_title(item.title)
        data["filename"] = filename
        data["warnings"] = item.warnings + filename_warnings
        rows.append(data)
    return {"cases": rows, "count": len(rows)}


@app.post("/api/parse-text")
async def parse_text_endpoint(payload: TextParseRequest) -> dict[str, object]:
    return _parsed_response(payload.text)


@app.post("/api/parse-docx")
async def parse_docx_endpoint(file: UploadFile = File(...)) -> dict[str, object]:
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Please upload a DOCX file")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="DOCX file is larger than 15 MB")
    try:
        text = extract_docx_text(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"DOCX could not be read: {exc}") from exc
    if not text.strip():
        raise HTTPException(status_code=400, detail="DOCX contains no readable text")
    response = _parsed_response(text)
    response["source_name"] = file.filename
    return response


@app.post("/api/parse-file")
async def parse_file_endpoint(
    file: UploadFile = File(...),
    page_range: str = Form(""),
    folder_name: str = Form(""),
) -> dict[str, object]:
    filename = file.filename or ""
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix not in {"docx", "pdf"}:
        raise HTTPException(status_code=400, detail="Please upload a DOCX or PDF file")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File is larger than 15 MB")
    try:
        text = (
            extract_docx_text(data)
            if suffix == "docx"
            else extract_pdf_text(data, page_range)
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"File could not be read: {exc}") from exc
    if not text.strip():
        detail = (
            "PDF contains no readable text; scanned PDFs are not supported"
            if suffix == "pdf"
            else "DOCX contains no readable text"
        )
        raise HTTPException(status_code=400, detail=detail)
    response = _parsed_response(text)
    requested_folder = safe_week_folder(
        folder_name.strip() or Path(filename).stem or "Syllabus Cases"
    )
    for case in response["cases"]:
        case["week"] = requested_folder
    response["source_name"] = filename
    response["folder_name"] = requested_folder
    return response


@app.post("/api/resolve-cases")
async def resolve_cases_endpoint(payload: ResolveCasesRequest) -> dict[str, object]:
    rows = [case.model_dump() for case in payload.cases]
    resolved = await resolve_case_rows(rows)
    for row in resolved:
        filename, filename_warnings = filename_for_title(str(row.get("title", "")))
        row["filename"] = filename
        row["warnings"] = list(row.get("warnings", [])) + filename_warnings
    return {"cases": resolved, "count": len(resolved)}


@app.post("/api/validate-folder")
async def validate_folder(payload: FolderRequest) -> dict[str, str | bool]:
    try:
        path = resolve_output_dir(payload.path, create=payload.create)
        return {"valid": True, "path": str(path), "message": "Folder is ready"}
    except FolderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs", status_code=202)
async def create_run(payload: RunRequest) -> dict[str, str]:
    try:
        run_id = jobs.create_run(payload.model_dump())
    except FolderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    started = jobs.start(run_id)
    return {"run_id": run_id, "status": "Queued" if started else "Not started"}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, object]:
    run = database.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, bool]:
    if not database.get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return {"cancel_requested": jobs.cancel(run_id)}


@app.post("/api/runs/{run_id}/retry", status_code=202)
async def retry_run(run_id: str) -> dict[str, bool]:
    run = database.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"started": jobs.start(run_id, retry_only=True)}


@app.post("/api/runs/{run_id}/cases/{case_id}/select-candidate", status_code=202)
async def select_case_candidate(
    run_id: str, case_id: str, payload: CandidateSelectionRequest
) -> dict[str, bool]:
    if not database.get_case(run_id, case_id):
        raise HTTPException(status_code=404, detail="Case not found")
    started = jobs.select_candidate(run_id, case_id, payload.candidate_id)
    if not started:
        raise HTTPException(status_code=409, detail="Candidate is unavailable or the run is active")
    return {"started": True}
