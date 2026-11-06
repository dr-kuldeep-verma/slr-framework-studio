"""SQLite access for a single `project.slr` database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION, __version__
from .schema import MIGRATIONS, SCHEMA_SQL

__all__ = ["Database", "utc_now", "MigrationError"]


class MigrationError(RuntimeError):
    """Raised when a project file was written by an incompatible version."""


def utc_now() -> str:
    """ISO-8601 UTC timestamp, second precision, with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """A thin, explicit wrapper around one project database.

    Deliberately not an ORM. The schema is the contract between stages and a
    reviewer may need to open `project.slr` in any SQLite browser years later
    to answer a question about how a decision was made.
    """

    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        #: Versions brought forward when this project was opened, for the log.
        self.applied_migrations: list[int] = []
        if not create and not self.path.exists():
            raise FileNotFoundError(f"No project database at {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the interface run a long import or
        # deduplication on a worker thread. The application serialises that
        # work -- one background task at a time, with the triggering controls
        # disabled -- so the connection is never used concurrently.
        self.conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self._configure()
        if create:
            self._create_schema()
        self._check_version()

    # -- connection setup --------------------------------------------------

    def _configure(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
        # A project on a network share can see brief locks; wait rather than fail.
        cur.execute("PRAGMA busy_timeout = 5000")

    def _create_schema(self) -> None:
        # executescript() commits any open transaction before it runs, so it
        # must not be wrapped in one of ours.
        self.conn.executescript(SCHEMA_SQL)
        with self.transaction():
            existing = self.get_meta("schema_version")
            if existing is None:
                now = utc_now()
                self.set_meta("schema_version", str(SCHEMA_VERSION))
                self.set_meta("app_version", __version__)
                self.set_meta("created_at", now)

    def _check_version(self) -> None:
        raw = self.get_meta("schema_version")
        if raw is None:
            raise MigrationError(
                f"{self.path} is not a SLR Framework Studio project "
                "(no schema_version in meta)."
            )
        found = int(raw)
        if found > SCHEMA_VERSION:
            raise MigrationError(
                f"{self.path} was written by a newer version of the app "
                f"(schema v{found}, this build understands v{SCHEMA_VERSION}). "
                "Upgrade the application to open it."
            )
        if found < SCHEMA_VERSION:
            self._migrate(found)

    def _migrate(self, from_version: int) -> None:
        """Bring an older project forward, one version at a time.

        Each step only adds tables, indexes and views. Nothing an older build
        wrote is dropped or rewritten, because the audit trail refers to it and
        a reviewer may have to explain a decision made two versions ago.
        """
        for version in range(from_version + 1, SCHEMA_VERSION + 1):
            step = MIGRATIONS.get(version)
            if step is None:
                continue
            if callable(step):
                step(self.conn)
            else:
                self.conn.executescript(step)
            self.set_meta("schema_version", str(version))
            self.applied_migrations.append(version)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    # -- transactions ------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one transaction, rolling back on any exception.

        Nested use is safe: an inner block joins the outer transaction instead
        of committing early.
        """
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        else:
            # A statement inside the block (DDL via executescript, say) can
            # have committed already; committing again would raise.
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")

    # -- query helpers -----------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, seq)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        placeholders = ", ".join(f":{c}" for c in values)
        cur = self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", values
        )
        return int(cur.lastrowid)

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return default if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Database {self.path}>"
