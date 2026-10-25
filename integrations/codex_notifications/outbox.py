"""WSL 本地发件箱；保留事件、终态回执和失败证据，不自动删除历史。"""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class Outbox:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or (directory / "outbox.sqlite3").is_symlink():
            raise ValueError("发件箱路径不能是符号链接")
        self.db = sqlite3.connect(directory / "outbox.sqlite3", timeout=3)
        (directory / "outbox.sqlite3").chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("BEGIN IMMEDIATE")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("不支持的发件箱版本")
        if version == 0:
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables:
                self.db.close()
                raise ValueError("目标不是空的 Codex 发件箱")
            with self.db:
                self.db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                self.db.execute("CREATE TABLE events (id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                    "fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
                    "attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, "
                    "error TEXT, receipt TEXT)")
                self.db.execute("INSERT INTO metadata VALUES ('bridge_id', ?)", (uuid4().hex,))
                self.db.execute("PRAGMA user_version=1")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def bind(self, endpoint: str) -> None:
        if not Path(endpoint).is_absolute():
            raise ValueError("通知桥只接受 WSL 本机 Unix socket 的绝对路径")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('endpoint', ?)", (endpoint,))
            saved = self.db.execute("SELECT value FROM metadata WHERE key='endpoint'").fetchone()[0]
            if saved != endpoint:
                raise ValueError("此发件箱已绑定其他接收端；不能将未结事件改投其他实例")

    def collect(self, source: dict) -> str | None:
        if source.get("hook_event_name") != "Stop":
            return None
        fields = {}
        for name, limit in (("session_id", 256), ("turn_id", 256), ("cwd", 4096)):
            value = source.get(name)
            if not isinstance(value, str) or not value or len(value) > limit:
                raise ValueError(f"Codex {name} 缺失或过长")
            if value != value.strip() or any(ord(c) < 32 for c in value):
                raise ValueError(f"Codex {name} 含控制字符或首尾空白")
            fields[name] = value
        summary = source.get("last_assistant_message")
        if summary is None:
            summary = ""
        if not isinstance(summary, str):
            raise ValueError("Codex 最终回复必须是文本")
        bridge_id = self.db.execute("SELECT value FROM metadata WHERE key='bridge_id'").fetchone()[0]
        event = {"schema_version": 1, "bridge_id": bridge_id, **fields, "event": "Stop",
                 "summary": summary[:6000], "truncated": len(summary) > 6000}
        identity = hashlib.sha256(json.dumps(
            [bridge_id, fields["session_id"], fields["turn_id"], "Stop"], ensure_ascii=False).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        event["observed_at"] = datetime.now(UTC).isoformat()
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO events(id,payload,fingerprint) VALUES (?,?,?)",
                            (identity, payload, fingerprint))
            old = self.db.execute("SELECT fingerprint FROM events WHERE id=?", (identity,)).fetchone()[0]
            if old != fingerprint:
                raise ValueError("同一个 Codex 回合已记录不同内容，原事件予以保留")
        return identity

    def pending(self, *, now: bool = False) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE status='pending' AND next_attempt<=? "
                               "ORDER BY rowid LIMIT 100", (float('inf') if now else time.time(),))
        return [dict(row) for row in rows]

    def ack(self, identity: str, receipt: dict) -> None:
        if (receipt.get("schema_version") != 1 or receipt.get("event_id") != identity
                or receipt.get("status") != "committed" or not isinstance(receipt.get("message_id"), str)
                or not str(receipt.get("session_id", "")).startswith("akashic:")
                or type(receipt.get("seq")) is not int):
            raise ValueError("Akashic 返回的事件回执不匹配")
        with self.db:
            self.db.execute("UPDATE events SET status='committed',receipt=?,error=NULL WHERE id=?",
                            (json.dumps(receipt, ensure_ascii=False), identity))

    def fail(self, identity: str, error: str, *, blocked: bool = False) -> None:
        with self.db:
            self.db.execute("UPDATE events SET attempts=attempts+1,error=?,status=?,"
                            "next_attempt=?+min(300,5*(1<<min(attempts,6))) WHERE id=? AND status='pending'",
                            (error[:1000], 'blocked' if blocked else 'pending', time.time(), identity))

    def retry(self, identity: str) -> None:
        with self.db:
            changed = self.db.execute("UPDATE events SET status='pending',next_attempt=0 WHERE id=? "
                                      "AND status IN ('blocked','pending')", (identity,)).rowcount
            if not changed:
                raise ValueError("事件不存在或已经提交；已提交事件不能重新发送")

    def status(self) -> dict:
        counts = dict(self.db.execute("SELECT status,COUNT(*) FROM events GROUP BY status"))
        errors = [dict(row) for row in self.db.execute(
            "SELECT id,status,attempts,error FROM events WHERE error IS NOT NULL ORDER BY rowid DESC LIMIT 10")]
        return {"counts": counts, "errors": errors}
