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
          log TEXT,
          PRIMARY KEY (name, version)
        )
        """
    )
    # CREATE TABLE IF NOT EXISTS doesn't add columns to a table that already
    # existed under the old schema (this app's registry.db predates progress
    # tracking) — ALTER TABLE ADD COLUMN one at a time, ignoring "duplicate
    # column" if it's already been done.
    for col in ("progress_current INTEGER", "progress_total INTEGER", "log TEXT"):
        try:
            con.execute(f"ALTER TABLE dataset_versions ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # already has this column

    # Keyed by (dataset_name, column_name) — not by version. The column
    # *set* comes from registry.py's field_mapping/generator code, which is
    # identical across every version of a given dataset name; only the data
    # changes per version, never the schema shape. A version-scoped key
    # would just be duplicate rows with no real difference, and would mean
    # descriptions vanishing every time a dataset gets re-fetched.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS column_descriptions (
          dataset_name TEXT NOT NULL,
          column_name TEXT NOT NULL,
          description TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (dataset_name, column_name)
        )
        """
    )
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


MAX_LOG_LINES = 200


def append_log(con: sqlite3.Connection, name: str, version: str, message: str) -> None:
    """Append one line to a job's running log (which endpoint, which page,
    tippecanoe's own output, etc.) — the modal's progress bar says *how far
    along*, this says *what it's actually doing*. Informational only, same
    reasoning as set_progress: a read-modify-write with no transaction
    wrapper is fine here (worst case on a lost update is a gap in the log,
    never an inconsistent is_current). Trimmed to the most recent
    MAX_LOG_LINES so a chatty job (49 ArcGIS pages, a tippecanoe run ticking
    once a second for minutes) can't grow this column without bound."""
    row = con.execute(
        "SELECT log FROM dataset_versions WHERE name=? AND version=?", (name, version)
    ).fetchone()
    existing = row[0] if row and row[0] else ""
    lines = existing.split("\n") if existing else []
    lines.append(f"{_now()}  {message}")
    if len(lines) > MAX_LOG_LINES:
        lines = lines[-MAX_LOG_LINES:]
    con.execute(
        "UPDATE dataset_versions SET log=? WHERE name=? AND version=?",
        ("\n".join(lines), name, version),
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


def get_column_descriptions(con: sqlite3.Connection, dataset_name: str) -> dict[str, str]:
    rows = con.execute(
        "SELECT column_name, description FROM column_descriptions WHERE dataset_name=?", (dataset_name,)
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def set_column_description(con: sqlite3.Connection, dataset_name: str, column_name: str, description: str) -> None:
    """Upsert. An empty string clears a description back to "none set"
    rather than being a distinct valid value — the UI treats both the same
    way (falls back to no description), so there's no reason to keep a row
    with nothing meaningful in it."""
    with con:
        # Collapsed to one line and capped: this text gets spliced into the
        # AI endpoints' system prompt verbatim (main.py's _schema_prompt),
        # unauthenticated via this very endpoint — stripping newlines keeps
        # a stored description from visually impersonating a new prompt
        # line/section, and the length cap keeps one description from
        # dominating the prompt with injected text. Doesn't make the content
        # trustworthy on its own; _schema_prompt's own framing is what tells
        # the model to treat this as data, not instructions.
        description = " ".join(description.split())[:300]
        if not description:
            con.execute(
                "DELETE FROM column_descriptions WHERE dataset_name=? AND column_name=?",
                (dataset_name, column_name),
            )
            return
        con.execute(
            """
            INSERT INTO column_descriptions (dataset_name, column_name, description, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (dataset_name, column_name)
            DO UPDATE SET description=excluded.description, updated_at=excluded.updated_at
            """,
            (dataset_name, column_name, description, _now()),
        )


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
