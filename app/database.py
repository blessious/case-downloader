from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATABASE_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path = DATABASE_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self._lock, self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    input_source TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_path TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    week TEXT NOT NULL,
                    title TEXT NOT NULL,
                    docket TEXT NOT NULL,
                    decision_date TEXT NOT NULL,
                    reporter_citation TEXT NOT NULL DEFAULT '',
                    metadata_source TEXT NOT NULL DEFAULT '',
                    metadata_source_url TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL,
                    saved_path TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    manual_search_url TEXT NOT NULL DEFAULT '',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cases_run ON cases(run_id, ordinal);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    url TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id, case_id);
                CREATE TABLE IF NOT EXISTS case_candidates (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    rank INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    docket TEXT NOT NULL,
                    decision_date TEXT NOT NULL,
                    reporter_citation TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    score REAL NOT NULL,
                    reasons_json TEXT NOT NULL DEFAULT '[]',
                    verified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_case
                    ON case_candidates(case_id, rank);
                CREATE TABLE IF NOT EXISTS case_lookup_cache (
                    lookup_key TEXT PRIMARY KEY,
                    candidate_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            case_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(cases)").fetchall()
            }
            for column in (
                "reporter_citation",
                "metadata_source",
                "metadata_source_url",
                "manual_search_url",
            ):
                if column not in case_columns:
                    db.execute(
                        f"ALTER TABLE cases ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            for column, default in (("original_input_json", "{}"), ("verification_json", "{}"), ("failure_stage", "")):
                if column not in case_columns:
                    db.execute(f"ALTER TABLE cases ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
            candidate_columns = {row["name"] for row in db.execute("PRAGMA table_info(case_candidates)")}
            if "verification_json" not in candidate_columns:
                db.execute("ALTER TABLE case_candidates ADD COLUMN verification_json TEXT NOT NULL DEFAULT '{}'")
            now = utc_now()
            db.execute(
                """
                UPDATE runs SET status='Interrupted', updated_at=?,
                    error_message='Application closed before this run completed'
                WHERE status IN ('Queued', 'Running', 'Cancelling')
                """,
                (now,),
            )
            db.execute(
                """
                UPDATE cases SET status='Not saved', updated_at=?,
                    error_code='INTERRUPTED',
                    error_message='Application closed before this case completed'
                WHERE status IN (
                    'Queued',
                    'Searching case details',
                    'Searching web',
                    'Searching Google',
                    'Verifying match',
                    'Searching Lawphil',
                    'Searching E-Library',
                    'Searching ChanRobles',
                    'Saving'
                )
                """,
                (now,),
            )

    def create_run(self, input_source: str, output_dir: str, cases: list[dict[str, Any]]) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,created_at,updated_at,input_source,output_dir,status) VALUES(?,?,?,?,?,'Queued')",
                (run_id, now, now, input_source, output_dir),
            )
            for item in cases:
                db.execute(
                    """
                    INSERT INTO cases(
                        id,run_id,ordinal,week,title,docket,decision_date,reporter_citation,
                        metadata_source,metadata_source_url,raw_text,filename,status,
                        warnings_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'Queued',?,?,?)
                    """,
                    (
                        item["id"],
                        run_id,
                        item["ordinal"],
                        item["week"],
                        item["title"],
                        item["docket"],
                        item["decision_date"],
                        item.get("reporter_citation", ""),
                        item.get("metadata_source", ""),
                        item.get("metadata_source_url", ""),
                        item.get("raw_text", ""),
                        item["filename"],
                        json.dumps(item.get("warnings", [])),
                        now,
                        now,
                    ),
                )
                original = item.get("original_input") or {key: item.get(key, "") for key in ("title", "docket", "decision_date", "reporter_citation")}
                db.execute("UPDATE cases SET original_input_json=? WHERE id=?", (json.dumps(original), item["id"]))
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "report_path", "error_message", "output_dir"}
        values = {key: value for key, value in fields.items() if key in allowed}
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock, self.connect() as db:
            db.execute(
                f"UPDATE runs SET {assignments} WHERE id=?",
                (*values.values(), run_id),
            )

    def update_case(self, case_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "docket",
            "decision_date",
            "reporter_citation",
            "metadata_source",
            "metadata_source_url",
            "saved_path",
            "source",
            "source_url",
            "error_code",
            "error_message",
            "manual_search_url",
            "warnings_json",
            "verification_json",
            "failure_stage",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if "original_input" in fields:
            values["original_input_json"] = json.dumps(fields["original_input"])
        if "verification" in fields:
            values["verification_json"] = json.dumps(fields["verification"])
        code = str(fields.get("error_code", ""))
        if code and "failure_stage" not in fields:
            values["failure_stage"] = (
                "pdf_validation" if "PDF" in code or code == "EXISTING_FILE_INVALID"
                else "filesystem" if any(t in code for t in ("FILE_", "FOLDER", "PATH_", "DISK"))
                else "identity" if any(t in code for t in ("IDENTITY", "MATCH", "VERIFICATION"))
                else "discovery" if any(t in code for t in ("GOOGLE", "LOOKUP", "CASE_NOT_FOUND"))
                else "retrieval")
        if fields.get("status") in {"Saved", "Already saved"}:
            values["failure_stage"] = ""
        if "warnings" in fields:
            values["warnings_json"] = json.dumps(fields["warnings"])
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock, self.connect() as db:
            db.execute(
                f"UPDATE cases SET {assignments} WHERE id=?",
                (*values.values(), case_id),
            )

    def add_attempt(
        self,
        run_id: str,
        case_id: str,
        *,
        source: str,
        url: str,
        attempt_no: int,
        status: str,
        error_code: str = "",
        error_message: str = "",
        started_at: str = "",
        finished_at: str = "",
    ) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                """
                INSERT INTO attempts(
                    run_id,case_id,source,url,attempt_no,status,error_code,error_message,started_at,finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    case_id,
                    source,
                    url,
                    attempt_no,
                    status,
                    error_code,
                    error_message,
                    started_at or utc_now(),
                    finished_at or utc_now(),
                ),
            )

    def set_case_candidates(self, case_id: str, candidates: list[dict[str, Any]]) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM case_candidates WHERE case_id=?", (case_id,))
            for rank, candidate in enumerate(candidates, start=1):
                db.execute(
                    """
                    INSERT INTO case_candidates(
                        id,case_id,rank,title,docket,decision_date,reporter_citation,
                        source,source_url,score,reasons_json,verified
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid.uuid4()),
                        case_id,
                        rank,
                        candidate.get("title", ""),
                        candidate.get("docket", ""),
                        candidate.get("decision_date", ""),
                        candidate.get("reporter_citation", ""),
                        candidate.get("source", ""),
                        candidate.get("source_url", ""),
                        float(candidate.get("score", 0)),
                        json.dumps(candidate.get("reasons", [])),
                        1 if candidate.get("verified") else 0,
                    ),
                )
                db.execute("UPDATE case_candidates SET verification_json=? WHERE case_id=? AND rank=?", (json.dumps(candidate.get("verification", {})), case_id, rank))


    def clear_case_candidates(self, case_id: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM case_candidates WHERE case_id=?", (case_id,))

    @staticmethod
    def _decode_candidate(row: sqlite3.Row) -> dict[str, Any]:
        candidate = dict(row)
        candidate["reasons"] = json.loads(candidate.pop("reasons_json") or "[]")
        candidate["verified"] = bool(candidate["verified"])
        candidate["verification"] = json.loads(candidate.pop("verification_json", "{}") or "{}")
        return candidate

    def get_case_candidate(self, case_id: str, candidate_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM case_candidates WHERE id=? AND case_id=?",
                (candidate_id, case_id),
            ).fetchone()
        return self._decode_candidate(row) if row else None

    def get_case(self, run_id: str, case_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM cases WHERE id=? AND run_id=?", (case_id, run_id)
            ).fetchone()
        return self._decode_case(row) if row else None

    def get_cached_candidates(self, lookup_keys: list[str]) -> list[dict[str, Any]]:
        if not lookup_keys:
            return []
        placeholders = ",".join("?" for _ in lookup_keys)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT candidate_json FROM case_lookup_cache WHERE lookup_key IN ({placeholders})",
                tuple(lookup_keys),
            ).fetchall()
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            candidate = json.loads(row["candidate_json"])
            unique[candidate.get("source_url", "")] = candidate
        return list(unique.values())

    def cache_candidate(self, lookup_keys: list[str], candidate: dict[str, Any]) -> None:
        payload = json.dumps(candidate)
        now = utc_now()
        with self._lock, self.connect() as db:
            for lookup_key in lookup_keys:
                db.execute(
                    """
                    INSERT INTO case_lookup_cache(lookup_key,candidate_json,updated_at)
                    VALUES(?,?,?)
                    ON CONFLICT(lookup_key) DO UPDATE SET
                        candidate_json=excluded.candidate_json,
                        updated_at=excluded.updated_at
                    """,
                    (lookup_key, payload, now),
                )

    @staticmethod
    def _decode_case(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        item["original_input"] = json.loads(item.pop("original_input_json", "{}") or "{}")
        item["verification"] = json.loads(item.pop("verification_json", "{}") or "{}")
        return item

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                return None
            cases = db.execute(
                "SELECT * FROM cases WHERE run_id=? ORDER BY ordinal", (run_id,)
            ).fetchall()
            attempts = db.execute(
                "SELECT * FROM attempts WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            candidate_rows = db.execute(
                """
                SELECT cc.* FROM case_candidates cc
                JOIN cases c ON c.id=cc.case_id
                WHERE c.run_id=? ORDER BY cc.case_id,cc.rank
                """,
                (run_id,),
            ).fetchall()
        result = dict(run)
        candidates_by_case: dict[str, list[dict[str, Any]]] = {}
        for row in candidate_rows:
            candidate = self._decode_candidate(row)
            candidates_by_case.setdefault(candidate["case_id"], []).append(candidate)
        result["cases"] = []
        for row in cases:
            case = self._decode_case(row)
            case["candidates"] = candidates_by_case.get(case["id"], [])
            result["cases"].append(case)
        result["verified_count"] = sum(c["status"] in {"Saved", "Already saved"} and c["verification"].get("outcome") == "verified" for c in result["cases"])
        result["attempts"] = [dict(row) for row in attempts]
        return result

    def cases_for_retry(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM cases WHERE run_id=?
                AND (
                    (
                        status='Not saved'
                        AND error_code NOT IN (
                            'PARSE_ERROR','FILENAME_COLLISION','EXISTING_FILE_INVALID'
                        )
                    )
                    OR (
                        status='Needs review'
                        AND error_code NOT IN ('PARSE_ERROR','FILENAME_COLLISION','EXISTING_FILE_INVALID')
                    )
                )
                ORDER BY ordinal
                """,
                (run_id,),
            ).fetchall()
        return [self._decode_case(row) for row in rows]
