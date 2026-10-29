"""Add initiative admission control without rewriting existing proposals."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

_V1_SQL_SHA256 = "f567a75791c021f38bc177999cafaf3a6af948e085a2541ab8471bff9e8ddaf7"
_V2_SQL_SHA256 = "e03b92c42560ef1f779491f3cf14e5a2b47a472b7d598f5ec8aa53ae2497a225"

CONTROL_SQL = """CREATE TABLE initiative_control (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    paused INTEGER NOT NULL CHECK(paused IN (0, 1)),
    version INTEGER NOT NULL CHECK(version >= 0)
)"""
RECEIPTS_SQL = """CREATE TABLE initiative_control_receipts (
    request_id TEXT PRIMARY KEY NOT NULL,
    paused INTEGER NOT NULL CHECK(paused IN (0, 1)),
    expected_version INTEGER NOT NULL CHECK(expected_version >= 0),
    receipt TEXT NOT NULL
)"""


def create_control(connection: sqlite3.Connection) -> None:
    connection.execute(CONTROL_SQL)
    connection.execute(RECEIPTS_SQL)
    connection.execute("INSERT INTO initiative_control VALUES (1, 0, 0)")


def upgrade(path: Path) -> None:
    if not path.exists():
        return  # Fresh workspaces are initialized by the runtime owner.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 3:
                db.rollback()
                return
            if version not in (1, 2):
                raise RuntimeError(
                    f"Drift initiative migration expects v1 or v2, found {version}"
                )
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            if tables != [("proposals",)]:
                raise RuntimeError(f"Drift v{version} table ownership mismatch")
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='proposals'").fetchone()[0]
            expected = _V1_SQL_SHA256 if version == 1 else _V2_SQL_SHA256
            if hashlib.sha256(" ".join(sql.split()).encode()).hexdigest() != expected:
                raise RuntimeError(f"Drift v{version} proposal schema mismatch")
            if version == 1:
                indexes = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                if indexes != [("proposals_due_idx",), ("proposals_selected_turn_idx",)]:
                    raise RuntimeError("Drift v1 index ownership mismatch")
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise RuntimeError("Drift migration integrity check failed")
                # The runtime owner retains its v1 upgrade and orphan handling.
                db.rollback()
                return
            before = db.execute(
                "SELECT * FROM proposals ORDER BY proposal_id, revision"
            ).fetchall()
            create_control(db)
            if (
                db.execute(
                    "SELECT * FROM proposals ORDER BY proposal_id, revision"
                ).fetchall()
                != before
            ):
                raise RuntimeError("Drift migration changed existing proposals")
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Drift migration integrity check failed")
            db.execute("PRAGMA user_version = 3")
            db.commit()
        except BaseException:
            db.rollback()
            raise
