from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .config import digest


class Journal:
    """Plugin-owned durable queue. Discovery and enqueue commit together."""

    def __init__(self, path: Path, identity: str):
        self.path, self.identity = path, identity

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL, bank TEXT NOT NULL, document_id TEXT NOT NULL,
                    operation_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','submitted','completed','failed','cancelled')),
                    error TEXT);
                CREATE TABLE IF NOT EXISTS feedback(
                    cursor INTEGER PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,payload_hash TEXT NOT NULL,
                    job_id TEXT,skip_reason TEXT);
                CREATE TABLE IF NOT EXISTS recalls(key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, value TEXT NOT NULL);
            """)
            stored = db.execute(
                "SELECT value FROM metadata WHERE key='identity'"
            ).fetchone()
            if stored is not None and stored[0] != self.identity:
                raise ValueError(
                    "Hindsight 后端/范围配置与既有同步账本不一致；需使用新数据目录或迁移"
                )
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES('identity',?)", (self.identity,)
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _enqueue(
        db: sqlite3.Connection, job_id: str, bank: str, item: Mapping[str, object]
    ) -> None:
        body = json.dumps(
            dict(item), ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        checksum = digest(dict(item))
        old = db.execute(
            "SELECT bank,payload_hash FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if old is not None:
            if (old["bank"], old["payload_hash"]) != (bank, checksum):
                raise ValueError("已登记 Hindsight 材料发生漂移")
            return
        document = item["document_id"]
        if not isinstance(document, str) or not document:
            raise ValueError("同步材料缺少 document_id")
        operation = str(
            uuid5(
                NAMESPACE_URL,
                "akashic-hindsight:" + bank + ":" + job_id + ":" + checksum,
            )
        )
        db.execute(
            "INSERT INTO jobs(job_id,bank,document_id,operation_id,payload,payload_hash,status) VALUES(?,?,?,?,?,?,'queued')",
            (job_id, bank, document, operation, body, checksum),
        )

    def enqueue(self, job_id: str, bank: str, item: Mapping[str, object]) -> None:
        with self.connection() as db:
            self._enqueue(db, job_id, bank, item)

    def feedback_cursor(self) -> int:
        with self.connection() as db:
            return int(
                db.execute("SELECT COALESCE(MAX(cursor),0) FROM feedback").fetchone()[0]
            )

    def record_feedback_page(
        self, after: int, rows: Sequence[Mapping[str, object]]
    ) -> None:
        with self.connection() as db:
            cursor = int(
                db.execute("SELECT COALESCE(MAX(cursor),0) FROM feedback").fetchone()[0]
            )
            if cursor != after:
                raise ValueError("反馈消费 cursor 已变化")
            for row in rows:
                next_cursor = row["cursor"]
                if type(next_cursor) is not int or next_cursor <= cursor:
                    raise ValueError("反馈 cursor 必须严格递增")
                item = row.get("item")
                job_id = row.get("job_id")
                if item is not None:
                    if (
                        not isinstance(item, Mapping)
                        or not isinstance(job_id, str)
                        or not isinstance(row.get("bank"), str)
                    ):
                        raise ValueError("反馈同步材料格式无效")
                    self._enqueue(db, job_id, str(row["bank"]), item)
                db.execute(
                    "INSERT INTO feedback VALUES(?,?,?,?,?)",
                    (
                        next_cursor,
                        row["event_id"],
                        row["payload_hash"],
                        job_id,
                        row.get("skip_reason"),
                    ),
                )
                cursor = next_cursor

    def pending(self, limit: int) -> tuple[dict[str, object], ...]:
        with self.connection() as db:
            return tuple(
                dict(row)
                for row in db.execute(
                    "SELECT * FROM jobs WHERE status IN ('queued','submitted') ORDER BY position LIMIT ?",
                    (limit,),
                )
            )

    def has_completed(self, bank: str) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM jobs WHERE bank=? AND status='completed' LIMIT 1",
                    (bank,),
                ).fetchone()
                is not None
            )

    def job(self, identity: str) -> dict[str, object] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE job_id=?", (identity,)
            ).fetchone()
            return None if row is None else dict(row)

    def retry(self, identity: str, remote_status: str) -> None:
        with self.connection() as db:
            row = db.execute(
                "SELECT status FROM jobs WHERE job_id=?", (identity,)
            ).fetchone()
            if row is None or row[0] not in {"failed", "cancelled"}:
                raise ValueError("只允许显式恢复失败或取消的同步任务")
            status = "completed" if remote_status == "completed" else "submitted"
            db.execute(
                "UPDATE jobs SET status=?,error=NULL WHERE job_id=?", (status, identity)
            )

    def mark(self, job_id: str, status: str, error: str | None = None) -> None:
        with self.connection() as db:
            old = db.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if old is None:
                raise ValueError("同步任务不存在")
            if old[0] in {"completed", "failed", "cancelled"} and old[0] != status:
                raise ValueError("不能自动重置终态同步任务")
            db.execute(
                "UPDATE jobs SET status=?,error=? WHERE job_id=?",
                (status, error, job_id),
            )

    def record_error(self, job_id: str, error: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET error=? WHERE job_id=? AND status IN ('queued','submitted')",
                (error, job_id),
            )

    def document(self, bank: str, document_id: str) -> Mapping[str, object] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM jobs WHERE bank=? AND document_id=? AND status='completed' ORDER BY position DESC LIMIT 1",
                (bank, document_id),
            ).fetchone()
            return None if row is None else json.loads(row[0])

    def recall(
        self, key: str, request: Mapping[str, object]
    ) -> dict[str, object] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT request_hash,value FROM recalls WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row[0] != digest(dict(request)):
                raise ValueError("原召回身份的请求发生漂移")
            return json.loads(row[1])

    def save_recall(
        self, key: str, request: Mapping[str, object], material: Mapping[str, object]
    ) -> None:
        with self.connection() as db:
            payload = json.dumps(
                dict(material), ensure_ascii=False, sort_keys=True, allow_nan=False
            )
            previous = db.execute(
                "SELECT request_hash,value FROM recalls WHERE key=?", (key,)
            ).fetchone()
            if previous is not None:
                if (previous[0], previous[1]) != (digest(dict(request)), payload):
                    raise ValueError("原召回记录不可覆盖")
                return
            db.execute(
                "INSERT INTO recalls VALUES(?,?,?)",
                (key, digest(dict(request)), payload),
            )

    def recall_value(self, key: str) -> dict[str, object] | None:
        with self.connection() as db:
            row = db.execute("SELECT value FROM recalls WHERE key=?", (key,)).fetchone()
            return None if row is None else json.loads(row[0])

    def status(self) -> dict[str, object]:
        with self.connection() as db:
            counts = {
                str(row[0]): int(row[1])
                for row in db.execute(
                    "SELECT status,COUNT(*) FROM jobs GROUP BY status"
                )
            }
            cursor = int(
                db.execute("SELECT COALESCE(MAX(cursor),0) FROM feedback").fetchone()[0]
            )
            blocked = db.execute(
                "SELECT MIN(f.cursor) FROM feedback f JOIN jobs j USING(job_id) WHERE j.status!='completed'"
            ).fetchone()[0]
            done = (
                cursor
                if blocked is None
                else int(
                    db.execute(
                        "SELECT COALESCE(MAX(cursor),0) FROM feedback WHERE cursor<?",
                        (blocked,),
                    ).fetchone()[0]
                )
            )
            failures = [
                dict(row)
                for row in db.execute(
                    "SELECT job_id,operation_id,status,error FROM jobs WHERE status IN ('failed','cancelled') OR error IS NOT NULL ORDER BY position LIMIT 20"
                )
            ]
            return {
                "jobs": counts,
                "failed_jobs": failures,
                "feedback_discovered_cursor": cursor,
                "feedback_applied_cursor": done,
            }
