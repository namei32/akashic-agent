"""Append target ownership for initiative proposals without rewriting old rows."""

from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3

from .initiative_control_v3 import CONTROL_SQL, RECEIPTS_SQL, _V1_SQL_SHA256, _V2_SQL_SHA256

TARGET_SQL = """CREATE TABLE initiative_proposals (
    proposal_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY(proposal_id, revision),
    FOREIGN KEY(proposal_id, revision) REFERENCES proposals(proposal_id, revision)
)"""


def create_targets(db: sqlite3.Connection) -> None:
    db.execute(TARGET_SQL)


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def upgrade(path: Path) -> None:
    if not path.exists():
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 4:
                db.rollback()
                return
            if version not in (1, 3):
                raise RuntimeError(f"Drift target migration expects v1 or v3, found {version}")
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            expected = [("proposals",)] if version == 1 else [
                ("initiative_control",), ("initiative_control_receipts",), ("proposals",)]
            if tables != expected:
                raise RuntimeError("Drift target migration table ownership mismatch")
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='proposals'").fetchone()[0]
            digest = _V1_SQL_SHA256 if version == 1 else _V2_SQL_SHA256
            if hashlib.sha256(_normalize(sql).encode()).hexdigest() != digest:
                raise RuntimeError("Drift target migration proposal schema mismatch")
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Drift target migration integrity check failed")
            if version == 1:
                db.rollback()  # The retained owner upgrades v1 when it is enabled.
                return
            for name, expected_sql in (("initiative_control", CONTROL_SQL),
                                       ("initiative_control_receipts", RECEIPTS_SQL)):
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                if _normalize(sql) != _normalize(expected_sql):
                    raise RuntimeError(f"Drift target migration {name} schema mismatch")
            old = {name: db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
                   for name in ("proposals", "initiative_control", "initiative_control_receipts")}
            create_targets(db)
            if any(db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall() != rows
                   for name, rows in old.items()):
                raise RuntimeError("Drift target migration changed existing rows")
            db.execute("PRAGMA user_version = 4")
            db.commit()
        except BaseException:
            db.rollback()
            raise
