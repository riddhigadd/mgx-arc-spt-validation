"""SQLite persistence for snapshots, issues, activity, jobs, and log steps."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from backend.models import (
    ActionKind,
    ActionRecord,
    Issue,
    IssueSeverity,
    IssueStatus,
    Job,
    JobLogStep,
    JobStatus,
    Snapshot,
    SnapshotKind,
    to_plain,
    utc_now_iso,
)

SCHEMA_VERSION = 1
DEFAULT_DB_NAME = "mgx_arc_health.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT,
    target_id TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS activity (
    id TEXT PRIMARY KEY,
    target_id TEXT,
    action TEXT NOT NULL,
    actor TEXT,
    status TEXT,
    created_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_id TEXT,
    status TEXT NOT NULL,
    percent INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS job_log_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    style TEXT NOT NULL,
    line TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_target ON snapshots(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_issues_target ON issues(target_id, status);
CREATE INDEX IF NOT EXISTS idx_activity_target ON activity(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_target ON jobs(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_log_steps(job_id, seq);
"""


def default_db_path() -> Path:
    env = os.environ.get("MGX_ARC_DATA_DIR", "").strip()
    if env:
        return Path(env) / DEFAULT_DB_NAME
    return Path(__file__).resolve().parent.parent / "data" / DEFAULT_DB_NAME


def _dumps(value: Any) -> str:
    return json.dumps(to_plain(value), separators=(",", ":"))


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    return json.loads(text)


class SnapshotStore:
    """Thread-safe SQLite store with WAL journaling."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database {self.path} is schema v{row['value']}; "
                    f"this build supports v{SCHEMA_VERSION}."
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- snapshots ---

    def save_snapshot(self, snapshot: Snapshot) -> Snapshot:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO snapshots(id, target_id, kind, created_at, raw_json, normalized_json, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    target_id=excluded.target_id,
                    kind=excluded.kind,
                    created_at=excluded.created_at,
                    raw_json=excluded.raw_json,
                    normalized_json=excluded.normalized_json,
                    meta_json=excluded.meta_json
                """,
                (
                    snapshot.id,
                    snapshot.target_id,
                    str(snapshot.kind),
                    snapshot.created_at,
                    _dumps(snapshot.raw),
                    _dumps(snapshot.normalized),
                    _dumps(snapshot.meta),
                ),
            )
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> Snapshot | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        return _row_to_snapshot(row) if row else None

    def list_snapshots(
        self,
        *,
        target_id: str | None = None,
        kind: SnapshotKind | str | None = None,
        limit: int = 100,
    ) -> list[Snapshot]:
        sql = "SELECT * FROM snapshots"
        clauses: list[str] = []
        args: list[Any] = []
        if target_id:
            clauses.append("target_id = ?")
            args.append(target_id)
        if kind is not None:
            clauses.append("kind = ?")
            args.append(str(kind))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_snapshot(row) for row in rows]

    # --- issues ---

    def save_issue(self, issue: Issue) -> Issue:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO issues(
                    id, snapshot_id, target_id, code, severity, status, title,
                    detail, created_at, updated_at, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    snapshot_id=excluded.snapshot_id,
                    target_id=excluded.target_id,
                    code=excluded.code,
                    severity=excluded.severity,
                    status=excluded.status,
                    title=excluded.title,
                    detail=excluded.detail,
                    updated_at=excluded.updated_at,
                    extra_json=excluded.extra_json
                """,
                (
                    issue.id,
                    issue.snapshot_id,
                    issue.target_id,
                    issue.code,
                    str(issue.severity),
                    str(issue.status),
                    issue.title,
                    issue.detail,
                    issue.created_at,
                    issue.updated_at,
                    _dumps(issue.extra),
                ),
            )
        return issue

    def get_issue(self, issue_id: str) -> Issue | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM issues WHERE id = ?", (issue_id,)
            ).fetchone()
        return _row_to_issue(row) if row else None

    def list_issues(
        self,
        *,
        target_id: str | None = None,
        status: IssueStatus | str | None = None,
        limit: int = 200,
    ) -> list[Issue]:
        sql = "SELECT * FROM issues"
        clauses: list[str] = []
        args: list[Any] = []
        if target_id:
            clauses.append("target_id = ?")
            args.append(target_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(str(status))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_issue(row) for row in rows]

    def update_issue(self, issue_id: str, **fields: Any) -> Issue | None:
        allowed = {"status", "title", "detail", "severity", "extra", "snapshot_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported issue fields: {sorted(unknown)}")
        issue = self.get_issue(issue_id)
        if issue is None:
            return None
        if "status" in fields:
            issue.status = IssueStatus(str(fields["status"]))
        if "severity" in fields:
            issue.severity = IssueSeverity(str(fields["severity"]))
        if "title" in fields:
            issue.title = str(fields["title"])
        if "detail" in fields:
            issue.detail = str(fields["detail"])
        if "extra" in fields:
            issue.extra = dict(fields["extra"] or {})
        if "snapshot_id" in fields:
            issue.snapshot_id = fields["snapshot_id"]
        issue.updated_at = utc_now_iso()
        return self.save_issue(issue)

    # --- activity ---

    def save_activity(self, record: ActionRecord) -> ActionRecord:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activity(id, target_id, action, actor, status, created_at, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.target_id,
                    str(record.action),
                    record.actor,
                    record.status,
                    record.created_at,
                    _dumps(record.detail),
                ),
            )
        return record

    def list_activity(
        self,
        *,
        target_id: str | None = None,
        limit: int = 200,
    ) -> list[ActionRecord]:
        sql = "SELECT * FROM activity"
        args: list[Any] = []
        if target_id:
            sql += " WHERE target_id = ?"
            args.append(target_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_activity(row) for row in rows]

    # --- jobs ---

    def save_job(self, job: Job) -> Job:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs(
                    id, kind, target_id, status, percent, message, error,
                    created_at, updated_at, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    target_id=excluded.target_id,
                    status=excluded.status,
                    percent=excluded.percent,
                    message=excluded.message,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    extra_json=excluded.extra_json
                """,
                (
                    job.id,
                    job.kind,
                    job.target_id,
                    str(job.status),
                    int(job.percent),
                    job.message,
                    job.error,
                    job.created_at,
                    job.updated_at,
                    _dumps(job.extra),
                ),
            )
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(
        self,
        *,
        target_id: str | None = None,
        status: JobStatus | str | None = None,
        limit: int = 100,
    ) -> list[Job]:
        sql = "SELECT * FROM jobs"
        clauses: list[str] = []
        args: list[Any] = []
        if target_id:
            clauses.append("target_id = ?")
            args.append(target_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(str(status))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_job(row) for row in rows]

    def update_job(self, job_id: str, **fields: Any) -> Job | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        if "status" in fields and fields["status"] is not None:
            job.status = JobStatus(str(fields["status"]))
        if "percent" in fields and fields["percent"] is not None:
            job.percent = max(0, min(100, int(fields["percent"])))
        if "message" in fields:
            job.message = str(fields["message"] or "")
        if "error" in fields:
            job.error = fields["error"]
        if "extra" in fields and fields["extra"] is not None:
            job.extra = dict(fields["extra"])
        job.updated_at = utc_now_iso()
        return self.save_job(job)

    def append_job_log(
        self,
        job_id: str,
        line: str,
        *,
        style: str = "out",
    ) -> JobLogStep:
        if job_id is None or line is None:
            raise ValueError("job_id and line are required")
        style = style if style in {"out", "err", "cmd", "info"} else "out"
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM job_log_steps WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            seq = int(row["seq"]) + 1 if row else 1
            created = utc_now_iso()
            cur = self._conn.execute(
                """
                INSERT INTO job_log_steps(job_id, seq, style, line, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, seq, style, str(line), created),
            )
            self._conn.execute(
                "UPDATE jobs SET updated_at = ? WHERE id = ?",
                (created, job_id),
            )
            log_id = int(cur.lastrowid)
        return JobLogStep(
            id=log_id,
            job_id=job_id,
            seq=seq,
            line=str(line),
            style=style,
            created_at=created,
        )

    def list_job_logs(self, job_id: str, *, limit: int = 2000) -> list[JobLogStep]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM job_log_steps
                WHERE job_id = ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (job_id, int(limit)),
            ).fetchall()
        return [
            JobLogStep(
                id=int(row["id"]),
                job_id=row["job_id"],
                seq=int(row["seq"]),
                style=row["style"],
                line=row["line"],
                created_at=row["created_at"],
            )
            for row in rows
        ]


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        id=row["id"],
        target_id=row["target_id"],
        kind=SnapshotKind(row["kind"]),
        created_at=row["created_at"],
        raw=_loads(row["raw_json"], {}),
        normalized=_loads(row["normalized_json"], {}),
        meta=_loads(row["meta_json"], {}),
    )


def _row_to_issue(row: sqlite3.Row) -> Issue:
    return Issue(
        id=row["id"],
        snapshot_id=row["snapshot_id"],
        target_id=row["target_id"],
        code=row["code"],
        severity=IssueSeverity(row["severity"]),
        status=IssueStatus(row["status"]),
        title=row["title"],
        detail=row["detail"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        extra=_loads(row["extra_json"], {}),
    )


def _row_to_activity(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        id=row["id"],
        target_id=row["target_id"],
        action=ActionKind(row["action"]),
        actor=row["actor"] or "system",
        status=row["status"] or "recorded",
        created_at=row["created_at"],
        detail=_loads(row["detail_json"], {}),
    )


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        target_id=row["target_id"],
        status=JobStatus(row["status"]),
        percent=int(row["percent"] or 0),
        message=row["message"] or "",
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        extra=_loads(row["extra_json"], {}),
    )
