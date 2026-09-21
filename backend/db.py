"""
Small SQLite-backed registry tracking dataset *versions* — which one is
active, its status, size, and file paths. Not to be confused with
registry.py (the static Python config of *how* to fetch/generate each
dataset). Uses stdlib sqlite3 — already a dependency here (backend/main.py
reads .mbtiles the same way).

Why a database instead of a flat "current version" text file: flipping
which version is active needs to be atomic (never a moment where zero or
two versions look "current"), and a bare text file edited in place isn't —
a power cut mid-write leaves it truncated. A SQLite transaction gives that
atomicity for free instead of requiring a hand-rolled write-temp-then-rename
dance for the pointer itself (the big data files still need that dance —
see refresh.py — a transaction can't retroactively validate a half-written
90MB parquet file, that's a separate, file-level concern).
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

# Job/version statuses. Anything not in TERMINAL is "a refresh is running".
TERMINAL_STATUSES = {"active", "superseded", "failed"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS dataset_versions (
          name TEXT NOT NULL,
          version TEXT NOT NULL,
          status TEXT NOT NULL,
          is_current INTEGER NOT NULL DEFAULT 0,
          has_geometry INTEGER NOT NULL DEFAULT 0,
          row_count INTEGER,
          created_at TEXT NOT NULL,
          completed_at TEXT,
          error TEXT,
          parquet_path TEXT,
          mbtiles_path TEXT,
          progress_current INTEGER,
          progress_total INTEGER,
          PRIMARY KEY (name, version)
        )
        """
    )
    # CREATE TABLE IF NOT EXISTS doesn't add columns to a table that already
    # existed under the old schema (this app's registry.db predates progress
    # tracking) — ALTER TABLE ADD COLUMN one at a time, ignoring "duplicate
    # column" if it's already been done.
    for col in ("progress_current INTEGER", "progress_total INTEGER"):
        try:
            con.execute(f"ALTER TABLE dataset_versions ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # already has this column
    return con



# A job legitimately running (fetching thousands of ArcGIS pages, then
# tiling) can take several minutes — MSOA alone took a few. Only a row
# non-terminal for *longer than any real job plausibly takes* is actually
# stale (the process that owned it died); anything younger is presumed to
# still be genuinely in flight.
STALE_AFTER_MINUTES = 60


def reap_stale_jobs(con: sqlite3.Connection, name: str) -> None:
    """Mark old non-terminal rows for `name` as failed. Age-gated on purpose
    — this used to unconditionally fail *any* non-terminal row, which meant
    calling it right before checking has_running_job() would reap a job
    that was still actually running seconds ago, silently defeating the
    409-on-duplicate-refresh check."""
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=STALE_AFTER_MINUTES)).isoformat(
        timespec="seconds"
    )
    placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
    with con:
        con.execute(
            f"UPDATE dataset_versions SET status='failed', error='Interrupted (process restarted)', "
            f"completed_at=? WHERE name=? AND status NOT IN ({placeholders}) AND created_at < ?",
            (_now(), name, *TERMINAL_STATUSES, cutoff),
        )


def has_running_job(con: sqlite3.Connection, name: str) -> bool:
    placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
    row = con.execute(
        f"SELECT 1 FROM dataset_versions WHERE name=? AND status NOT IN ({placeholders}) LIMIT 1",
        (name, *TERMINAL_STATUSES),
    ).fetchone()
    return row is not None


def start_job(con: sqlite3.Connection, name: str, version: str, has_geometry: bool) -> None:
    """Insert the row marking a refresh as in-progress. Written before any
    file I/O happens, so `has_running_job` is answerable immediately."""
    with con:
        con.execute(
            "INSERT INTO dataset_versions (name, version, status, has_geometry, created_at) "
            "VALUES (?, ?, 'fetching', ?, ?)",
            (name, version, int(has_geometry), _now()),
        )


def set_status(con: sqlite3.Connection, name: str, version: str, status: str, error: str | None = None) -> None:
    with con:
        con.execute(
            "UPDATE dataset_versions SET status=?, error=? WHERE name=? AND version=?",
            (status, error, name, version),
        )


def set_progress(con: sqlite3.Connection, name: str, version: str, current: int, total: int) -> None:
    """Informational only — not wrapped in `with con:` transaction ceremony
    the way is_current flips are, since a torn/lost progress update has no
    correctness consequence (worst case: the bar briefly shows a stale
    number), unlike a torn is_current update."""
    con.execute(
        "UPDATE dataset_versions SET progress_current=?, progress_total=? WHERE name=? AND version=?",
        (current, total, name, version),
    )
    con.commit()


def fail_job(con: sqlite3.Connection, name: str, version: str, error: str) -> None:
    with con:
        con.execute(
            "UPDATE dataset_versions SET status='failed', error=?, completed_at=? WHERE name=? AND version=?",
            (error, _now(), name, version),
        )


def activate_version(
    con: sqlite3.Connection,
    name: str,
    version: str,
    row_count: int | None = None,
    parquet_path: str | None = None,
    mbtiles_path: str | None = None,
) -> None:
    """The one moment a refresh (or a manual version-switch) can change what
    the running app serves. Single transaction: old current -> superseded,
    named version -> current/active. sqlite3's `with con:` commits on success
    and rolls back on any exception, so this can never leave two versions (or
    zero) marked current for the same dataset."""
    with con:
        con.execute(
            "UPDATE dataset_versions SET is_current=0, status='superseded' "
            "WHERE name=? AND is_current=1",
            (name,),
        )
        fields = ["is_current=1", "status='active'", "completed_at=?"]
        params: list = [_now()]
        if row_count is not None:
            fields.append("row_count=?")
            params.append(row_count)
        if parquet_path is not None:
            fields.append("parquet_path=?")
            params.append(parquet_path)
        if mbtiles_path is not None:
            fields.append("mbtiles_path=?")
            params.append(mbtiles_path)
        params += [name, version]
        con.execute(f"UPDATE dataset_versions SET {', '.join(fields)} WHERE name=? AND version=?", params)


def get_current(con: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT * FROM dataset_versions WHERE name=? AND is_current=1", (name,)
    ).fetchone()


def get_latest_status(con: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT * FROM dataset_versions WHERE name=? ORDER BY created_at DESC LIMIT 1", (name,)
    ).fetchone()


def list_versions(con: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT * FROM dataset_versions WHERE name=? ORDER BY created_at DESC", (name,)
    ).fetchall()


def list_running_jobs(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every dataset with a currently non-terminal job, across all names —
    one query instead of the frontend polling each dataset individually just
    to discover what's running after a page load."""
    con.row_factory = sqlite3.Row
    placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
    return con.execute(
        f"SELECT * FROM dataset_versions WHERE status NOT IN ({placeholders})", tuple(TERMINAL_STATUSES)
    ).fetchall()
