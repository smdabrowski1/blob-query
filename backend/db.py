"""
SQLite-backed registry tracking dataset *versions* — which one is active,
its status, size, and file paths. Not to be confused with registry.py (the
static config of how to fetch/generate each dataset).

Flipping the active version needs to be atomic, so this uses a real
transaction instead of a hand-edited pointer file (a crash mid-write would
leave that truncated). The data files themselves still need
write-temp-then-validate-then-rename — see refresh.py.
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
    # Migrate older registry.db files (predate progress tracking) one column
    # at a time; CREATE TABLE IF NOT EXISTS won't add columns to an existing table.
    for col in ("progress_current INTEGER", "progress_total INTEGER", "log TEXT"):
        try:
            con.execute(f"ALTER TABLE dataset_versions ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # already has this column

    # Keyed by (dataset_name, column_name), not version — the schema shape is
    # fixed per dataset, so version-scoping would just lose descriptions on
    # every refresh.
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



# A real job (ArcGIS fetch + tiling) can run several minutes, so only rows
# non-terminal for longer than that are presumed dead rather than in flight.
STALE_AFTER_MINUTES = 60


def reap_stale_jobs(con: sqlite3.Connection, name: str) -> None:
    """Mark old non-terminal rows for `name` as failed. Age-gated — an
    unconditional version would reap a job that's still genuinely running,
    defeating the duplicate-refresh check."""
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
    """Insert the row marking a refresh as in-progress, before any file I/O,
    so `has_running_job` is answerable immediately."""
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
    """Informational only, so no transaction wrapper — worst case on a lost
    update is a briefly stale progress bar, not a correctness issue."""
    con.execute(
        "UPDATE dataset_versions SET progress_current=?, progress_total=? WHERE name=? AND version=?",
        (current, total, name, version),
    )
    con.commit()


MAX_LOG_LINES = 200


def append_log(con: sqlite3.Connection, name: str, version: str, message: str) -> None:
    """Append one line to a job's log — progress says how far along, this
    says what it's actually doing. Same no-transaction reasoning as
    set_progress. Trimmed to MAX_LOG_LINES so a chatty job (tippecanoe
    ticking for minutes) can't grow this column unbounded."""
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
    """The one place a refresh (or manual version-switch) changes what the
    app serves. Old current -> superseded, named version -> active, in one
    transaction so there's never a moment with zero or two current versions."""
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
    """Upsert. An empty string deletes the row rather than storing a blank —
    the UI treats both the same way, so no reason to keep it."""
    with con:
        # This text lands verbatim in the AI endpoints' system prompt
        # (main.py's _schema_prompt) via an unauthenticated endpoint, so
        # collapse newlines and cap length — stops a description from
        # impersonating a new prompt section or dominating the prompt with
        # injected text. Doesn't make it trustworthy on its own; the "data,
        # not instructions" framing in _schema_prompt does that part.
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
    """Every dataset with a non-terminal job, across all names — lets the
    frontend discover what's running after a page load in one query."""
    con.row_factory = sqlite3.Row
    placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
    return con.execute(
        f"SELECT * FROM dataset_versions WHERE status NOT IN ({placeholders})", tuple(TERMINAL_STATUSES)
    ).fetchall()
