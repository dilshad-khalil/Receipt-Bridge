"""Recent print-job history, backed by a small SQLite database.

Powers `GET /logs` and the frontend's Logs page - every attempt through
`/print/text`, `/print/raw`, `/print/test`, and `/print/pdf` is recorded
here, success or failure, with the error message on failure.

SQLite (rather than a plain in-memory list) so history survives the tray's
"Restart server" and process restarts in general - useful for exactly the
case you'd want a log for, tracking down an intermittent printer failure
that happened before the app was last restarted.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from app import config as config_mod

# Keep only the most recent N attempts - this is an operational log for
# spot-checking/debugging, not an audit trail, so unbounded growth isn't
# worth the disk/complexity.
MAX_ENTRIES = 200


def _db_path() -> Path:
    return config_mod.get_logs_dir() / "jobs.db"


def _connect() -> sqlite3.Connection:
    """Open a fresh connection with the table created if needed.

    A short-lived connection per call (rather than one long-lived shared
    connection) keeps this safe to call concurrently from multiple request
    threads without extra locking - sqlite3 connections aren't meant to be
    shared across threads, and each of these calls is a single small
    transaction anyway.
    """
    conn = sqlite3.connect(_db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            endpoint TEXT NOT NULL,
            printer TEXT,
            ok INTEGER NOT NULL,
            error TEXT,
            job_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 1,
            served_by TEXT
        )
        """
    )
    # `CREATE TABLE IF NOT EXISTS` above is a no-op against a jobs.db that
    # already existed before a column was added later (`attempts` for
    # retry-with-backoff, `served_by` for failover - see app/escpos_jobs.py)
    # - add each by hand so older installs don't break on their first
    # write/read after an update.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "attempts" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")
    if "served_by" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN served_by TEXT")
    return conn


def record(
    endpoint: str,
    printer: Optional[str],
    ok: bool,
    error: Optional[str] = None,
    job_id: Optional[str] = None,
    attempts: int = 1,
    served_by: Optional[str] = None,
) -> None:
    """Append one print-job attempt and trim history back to MAX_ENTRIES.

    :param endpoint: which API endpoint handled the job, e.g. "print/text".
    :param printer: the logical printer name requested (may be unknown/
        unmapped - that's still worth logging as a failure).
    :param ok: whether the job was accepted by the Windows print spooler.
    :param error: human-readable failure detail, if `ok` is False.
    :param job_id: the job's UUID, for cross-referencing with the text log.
    :param attempts: how many attempts escpos_jobs.py's retry loop took -
        1 means it worked (or failed) on the first try with no retry;
        higher values mean a transient failure (spooler busy, printer
        temporarily offline/unreachable) was retried before the final
        outcome, success or failure - see app/escpos_jobs.py.
    :param served_by: which configured target actually completed the job
        (see app/printers.py's describe_target) - `None` if the job never
        reached a target (e.g. an unknown logical printer) or every target
        failed. On a mapping with backup targets, this is how "the primary
        was down, the backup handled it" becomes visible on the Logs page,
        not just in the app's own log file.
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO jobs (ts, endpoint, printer, ok, error, job_id, attempts, served_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), endpoint, printer, 1 if ok else 0, error, job_id, attempts, served_by),
        )
        conn.execute(
            "DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)",
            (MAX_ENTRIES,),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent(limit: int = MAX_ENTRIES) -> list[dict[str, Any]]:
    """Return recent job attempts, newest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ts, endpoint, printer, ok, error, job_id, attempts, served_by "
            "FROM jobs ORDER BY id DESC LIMIT ?",
            (max(0, min(limit, MAX_ENTRIES)),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "timestamp": ts,
            "endpoint": endpoint,
            "printer": printer,
            "ok": bool(ok),
            "error": error,
            "job_id": job_id,
            "attempts": attempts,
            "served_by": served_by,
        }
        for ts, endpoint, printer, ok, error, job_id, attempts, served_by in rows
    ]
