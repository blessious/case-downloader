from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import APP_NAME, default_output_dir, resource_path
from .database import Database
from .filenames import filename_for_title
from .jobs import FolderValidationError, JobManager, resolve_output_dir
from .parser import extract_docx_text, parse_text
from .schemas import FolderRequest, RunRequest, TextParseRequest


database = Database()
jobs = JobManager(database)
app = FastAPI(title=APP_NAME, version=__version__)

STATIC_DIR = resource_path("app", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def config() -> dict[str, str]:
    return {"default_output_dir": str(default_output_dir()), "version": app.version}


def _parsed_response(text: str) -> dict[str, object]:
    parsed = parse_text(text)
    rows = []
    for item in parsed:
        data = item.as_dict()
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


@app.get("/api/runs")
async def list_runs() -> dict[str, object]:
    return {"runs": database.list_runs()}


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


@app.get("/api/runs/{run_id}/report")
async def download_report(run_id: str) -> FileResponse:
    run = database.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    path_value = run.get("report_path", "")
    if not path_value or ";" in path_value:
        raise HTTPException(status_code=404, detail="Excel report is unavailable")
    path = Path(path_value)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report file no longer exists")
    return FileResponse(path, filename=path.name)
