"""Append memory consent without changing prior Drift facts or receipts."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from .initiative_control_v3 import CONTROL_SQL, RECEIPTS_SQL, _V1_SQL_SHA256, _V2_SQL_SHA256
from .initiative_target_v4 import TARGET_SQL

CONSENT_SQL = """CREATE TABLE initiative_memory_consent (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    target_json TEXT,
    version INTEGER NOT NULL CHECK(version >= 0),
    CHECK(enabled = 0 OR target_json IS NOT NULL)
)"""
CONSENT_RECEIPTS_SQL = """CREATE TABLE initiative_memory_receipts (
    request_id TEXT PRIMARY KEY NOT NULL,
    fingerprint TEXT NOT NULL,
    receipt TEXT NOT NULL
)"""


def create_consent(db: sqlite3.Connection) -> None:
    db.execute(CONSENT_SQL)
    db.execute(CONSENT_RECEIPTS_SQL)
    db.execute("INSERT INTO initiative_memory_consent VALUES (1, 0, NULL, 0)")


def _normal(sql: str) -> str:
    return " ".join(sql.split())


def upgrade(path: Path) -> None:
    if not path.exists():
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 5:
                db.rollback()
                return
            if version not in (1, 4):
                raise RuntimeError(f"Drift memory consent migration expects v1 or v4, found {version}")
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            expected = [("proposals",)] if version == 1 else [
                ("initiative_control",), ("initiative_control_receipts",),
                ("initiative_proposals",), ("proposals",),
            ]
            if tables != expected:
                raise RuntimeError("Drift memory consent table ownership mismatch")
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='proposals'").fetchone()[0]
            expected_hash = _V1_SQL_SHA256 if version == 1 else _V2_SQL_SHA256
            if hashlib.sha256(_normal(sql).encode()).hexdigest() != expected_hash:
                raise RuntimeError("Drift memory consent proposal schema mismatch")
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Drift memory consent integrity check failed")
            if version == 1:
                db.rollback()  # The original owner performs the v1 upgrade at activation.
                return
            for name, expected_sql in (("initiative_control", CONTROL_SQL),
                                       ("initiative_control_receipts", RECEIPTS_SQL),
                                       ("initiative_proposals", TARGET_SQL)):
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                if _normal(sql) != _normal(expected_sql):
                    raise RuntimeError(f"Drift memory consent {name} schema mismatch")
            names = ("proposals", "initiative_control", "initiative_control_receipts", "initiative_proposals")
            old = {name: db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall() for name in names}
            create_consent(db)
            if any(db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall() != rows
                   for name, rows in old.items()):
                raise RuntimeError("Drift memory consent migration changed existing rows")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Drift memory consent migration found invalid targets")
            db.execute("PRAGMA user_version = 5")
            db.commit()
        except BaseException:
            db.rollback()
            raise
