from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    """Own observations and report receipts; never write Core messages or other plugin data."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("不支持的战绩数据库版本")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots(
                    fingerprint TEXT PRIMARY KEY, observed_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS matches(
                    player_key TEXT NOT NULL, match_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    ended_at TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(player_key, match_id, fingerprint));
                CREATE TABLE IF NOT EXISTS reports(
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, facts TEXT NOT NULL,
                    body TEXT, error TEXT, published INTEGER NOT NULL DEFAULT 0, receipt TEXT);
                PRAGMA user_version=1;
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES('started_at', ?)",
                       (datetime.now(UTC).isoformat(),))

    def started_at(self) -> datetime:
        with self.db() as db:
            return datetime.fromisoformat(db.execute(
                "SELECT value FROM metadata WHERE key='started_at'").fetchone()[0])

    def ingest(self, snapshot: dict) -> bool:
        if snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("players"), list):
            raise ValueError("Windows 采集文件格式无效")
        observed = datetime.fromisoformat(snapshot["observed_at"])
        if observed.tzinfo is None:
            raise ValueError("采集时间缺少时区")
        text = encoded(snapshot)
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        with self.db() as db:
            if db.execute("SELECT 1 FROM snapshots WHERE fingerprint=?", (fingerprint,)).fetchone():
                return False
            for player in snapshot["players"]:
                if player.get("status") != "ok":
                    continue
                key = player["platform"] + ":" + player["riot_id"].casefold()
                for match in player["matches"]:
                    if match["puuid"] != player["puuid"] or match["platform"] != player["platform"]:
                        raise ValueError("对局不属于指定玩家或区服")
                    ended = datetime.fromisoformat(match["ended_at"])
                    if ended.tzinfo is None:
                        raise ValueError("对局时间缺少时区")
                    data = encoded(match)
                    db.execute("INSERT OR IGNORE INTO matches VALUES(?,?,?,?,?)",
                               (key, str(match["match_id"]), hashlib.sha256(data.encode()).hexdigest(),
                                ended.astimezone(UTC).isoformat(), data))
            db.execute("INSERT INTO snapshots VALUES(?,?,?)", (fingerprint, observed.isoformat(), text))
        return True

    def latest(self) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT data FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
            return None if row is None else json.loads(row[0])

    def matches(self, player_key: str, start: datetime | None = None,
                end: datetime | None = None, limit: int = 200) -> list[dict]:
        query = "SELECT data FROM matches WHERE rowid IN (SELECT MAX(rowid) FROM matches WHERE player_key=? GROUP BY match_id)"
        args: list[object] = [player_key]
        if start is not None:
            query += " AND ended_at>=?"
            args.append(start.astimezone(UTC).isoformat())
        if end is not None:
            query += " AND ended_at<?"
            args.append(end.astimezone(UTC).isoformat())
        query += " ORDER BY ended_at DESC LIMIT ?"
        args.append(limit)
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute(query, args)]

    def report(self, identity: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM reports WHERE id=?", (identity,)).fetchone()
            return dict(row) if row else None

    def claim(self, identity: str, facts: dict) -> bool:
        with self.db() as db:
            return bool(db.execute("INSERT OR IGNORE INTO reports(id,created_at,facts) VALUES(?,?,?)",
                                   (identity, datetime.now(UTC).isoformat(), encoded(facts))).rowcount)

    def finish(self, identity: str, body: str, error: str | None = None):
        with self.db() as db:
            db.execute("UPDATE reports SET body=?,error=? WHERE id=? AND body IS NULL", (body, error, identity))

    def published(self, identity: str, receipt: dict):
        with self.db() as db:
            db.execute("UPDATE reports SET published=1,receipt=? WHERE id=?", (encoded(receipt), identity))

    def pending(self) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM reports WHERE published=0 ORDER BY created_at")]

    def reports(self, limit: int = 20) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT id,created_at,body,error,published FROM reports ORDER BY created_at DESC LIMIT ?", (limit,))]
