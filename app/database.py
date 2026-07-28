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
                    raw_text TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL,
                    saved_path TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
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
                """
            )
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
                WHERE status IN ('Queued', 'Searching Lawphil', 'Searching E-Library', 'Saving')
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
                        id,run_id,ordinal,week,title,docket,decision_date,raw_text,filename,status,
                        warnings_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'Queued',?,?,?)
                    """,
                    (
                        item["id"],
                        run_id,
                        item["ordinal"],
                        item["week"],
                        item["title"],
                        item["docket"],
                        item["decision_date"],
                        item.get("raw_text", ""),
                        item["filename"],
                        json.dumps(item.get("warnings", [])),
                        now,
                        now,
                    ),
                )
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
            "saved_path",
            "source",
            "source_url",
            "error_code",
            "error_message",
            "warnings_json",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
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

    @staticmethod
    def _decode_case(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
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
        result = dict(run)
        result["cases"] = [self._decode_case(row) for row in cases]
        result["attempts"] = [dict(row) for row in attempts]
        return result

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT r.*,
                    COUNT(c.id) AS total,
                    SUM(CASE WHEN c.status='Saved' THEN 1 ELSE 0 END) AS saved,
                    SUM(CASE WHEN c.status='Already saved' THEN 1 ELSE 0 END) AS already_saved,
                    SUM(CASE WHEN c.status IN ('Not saved','Duplicate input') THEN 1 ELSE 0 END) AS not_saved
                FROM runs r LEFT JOIN cases c ON c.run_id=r.id
                GROUP BY r.id ORDER BY r.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cases_for_retry(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM cases WHERE run_id=?
                AND status='Not saved'
                AND error_code NOT IN ('PARSE_ERROR','FILENAME_COLLISION','EXISTING_FILE_INVALID')
                ORDER BY ordinal
                """,
                (run_id,),
            ).fetchall()
        return [self._decode_case(row) for row in rows]
