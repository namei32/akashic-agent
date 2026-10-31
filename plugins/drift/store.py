from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Generator, NotRequired, TypedDict

from .drift_migrations.helpers.initiative_control_v3 import (
    CONTROL_SQL,
    RECEIPTS_SQL,
    create_control,
)
from .drift_migrations.helpers.initiative_target_v4 import TARGET_SQL, create_targets
from .drift_migrations.helpers.memory_consent_v5 import CONSENT_RECEIPTS_SQL, CONSENT_SQL, create_consent
from .schema import ContactTarget, ControlReceipt, ControlRequest, MemoryConsentReceipt, MemoryConsentRequest

_SCHEMA_VERSION = 5
_TABLE_SQL_V1 = """
    CREATE TABLE proposals(
        proposal_id TEXT NOT NULL,
        revision TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        due_at TEXT NOT NULL,
        next_due TEXT,
        state_version INTEGER NOT NULL,
        selection_token TEXT UNIQUE,
        selected_session_id TEXT,
        selected_turn_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(proposal_id, revision)
    )
"""
_TABLE_SQL = """
    CREATE TABLE proposals(
        proposal_id TEXT NOT NULL,
        revision TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        due_at TEXT NOT NULL,
        next_due TEXT,
        state_version INTEGER NOT NULL,
        selection_token TEXT UNIQUE,
        selected_session_id TEXT,
        selected_turn_id TEXT,
        settlement_ref TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(proposal_id, revision)
    )
"""
_INDEX_SQL = (
    """
    CREATE UNIQUE INDEX proposals_selected_turn_idx
    ON proposals(selected_session_id, selected_turn_id)
    WHERE selected_turn_id IS NOT NULL
    """,
    "CREATE INDEX proposals_due_idx ON proposals(status, due_at)",
    "CREATE UNIQUE INDEX proposals_settlement_ref_idx "
    "ON proposals(settlement_ref) WHERE settlement_ref IS NOT NULL",
)


class DriftRef(TypedDict):
    proposal_id: str
    revision: str
    state_version: int


class DriftProposal(TypedDict):
    ref: DriftRef
    payload: dict[str, object]
    due_at: str
    next_due: str | None
    due: bool


class DriftSnapshot(TypedDict):
    next_due: str | None
    proposals: tuple[DriftProposal, ...]


class DriftSelectResult(TypedDict):
    selected: bool
    reason: NotRequired[str]
    selection_token: str | None
    accepted_turn: dict[str, str] | None


class DriftSelectionReceipt(TypedDict):
    selection_token: str
    ref: dict[str, str]
    payload: dict[str, object]
    due_at: str
    next_due: str | None
    status: str
    accepted_turn: dict[str, str]


class DriftStore:
    """Persist Drift proposals and their Turn-bound lifecycle."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """Create or validate the exact Drift schema."""

        with self._transaction(write=True) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION:
                raise RuntimeError(f"不支持的 Drift schema version: {version}")
            tables = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            )
            if tables != (
                "initiative_control", "initiative_control_receipts", "initiative_memory_consent",
                "initiative_memory_receipts", "initiative_proposals", "proposals",
            ):
                raise RuntimeError("Drift owned table set 不匹配")
            for name, expected in (
                ("initiative_control", CONTROL_SQL),
                ("initiative_control_receipts", RECEIPTS_SQL),
                ("initiative_memory_consent", CONSENT_SQL),
                ("initiative_memory_receipts", CONSENT_RECEIPTS_SQL),
                ("initiative_proposals", TARGET_SQL),
            ):
                row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,),
                ).fetchone()
                if row is None or _normalize_sql(row[0]) != _normalize_sql(expected):
                    raise RuntimeError(f"Drift {name} schema identity 不匹配")
            self._initiative_control(connection)
            self._memory_consent(connection)
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposals'"
            ).fetchone()
            if table_sql is None or _normalize_sql(str(table_sql[0])) != _normalize_sql(
                _TABLE_SQL
            ):
                raise RuntimeError("Drift constraint-bearing table SQL 不匹配")
            columns = tuple(
                (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute("PRAGMA table_info(proposals)")
            )
            if columns != _EXPECTED_COLUMNS:
                raise RuntimeError("Drift schema identity 不匹配")
            if _schema_indexes(connection) != _EXPECTED_INDEXES:
                raise RuntimeError("Drift index identity 不匹配")
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError("Drift SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Drift initiative proposal target references missing proposal")

    @staticmethod
    def _initiative_control(connection: sqlite3.Connection) -> dict[str, object]:
        row = connection.execute(
            "SELECT paused, version FROM initiative_control WHERE singleton=1"
        ).fetchone()
        if (
            row is None or type(row[0]) is not int or row[0] not in (0, 1)
            or type(row[1]) is not int or row[1] < 0
        ):
            raise RuntimeError("Drift initiative control is missing or corrupt")
        return {"paused": bool(row[0]), "control_version": row[1]}

    def initiative_status(self) -> dict[str, object]:
        with self._transaction(write=False) as db:
            return {
                **self._initiative_control(db),
                "proposals": dict(db.execute(
                    "SELECT status, count(*) FROM proposals GROUP BY status"
                )),
            }

    def set_initiative_paused(
        self, paused: bool, request_id: str, expected_version: int,
    ) -> dict[str, object]:
        if type(paused) is not bool:
            raise ValueError("Drift paused must be a boolean")
        request = ControlRequest(request_id=request_id, expected_version=expected_version)
        with self._transaction(write=True) as db:
            saved = db.execute(
                "SELECT paused, expected_version, receipt "
                "FROM initiative_control_receipts WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if saved is not None:
                if (saved[0], saved[1]) != (int(paused), request.expected_version):
                    raise ValueError("Drift control request ID reused for different arguments")
                saved_receipt = ControlReceipt.model_validate_json(saved[2])
                if (
                    saved_receipt.request_id != request.request_id
                    or saved_receipt.paused is not paused
                    or saved_receipt.version != request.expected_version + 1
                ):
                    raise RuntimeError("Drift control receipt is corrupt")
                return saved_receipt.model_dump(mode="json")
            current = self._initiative_control(db)
            if current["control_version"] != request.expected_version:
                raise ValueError("Drift initiative state changed; refresh before retrying")
            version = request.expected_version + 1
            receipt: dict[str, object] = {
                "request_id": request.request_id, "paused": paused, "version": version,
            }
            db.execute(
                "UPDATE initiative_control SET paused=?, version=? WHERE singleton=1",
                (int(paused), version),
            )
            db.execute(
                "INSERT INTO initiative_control_receipts VALUES (?,?,?,?)",
                (request.request_id, int(paused), request.expected_version,
                 json.dumps(receipt, sort_keys=True)),
            )
            return receipt

    @staticmethod
    def _memory_consent(connection: sqlite3.Connection) -> dict[str, object]:
        row = connection.execute(
            "SELECT enabled,target_json,version FROM initiative_memory_consent WHERE singleton=1"
        ).fetchone()
        if (row is None or type(row[0]) is not int or row[0] not in (0, 1)
                or type(row[2]) is not int or row[2] < 0):
            raise RuntimeError("Drift memory consent is missing or corrupt")
        try:
            target = None if row[1] is None else ContactTarget.model_validate_json(row[1]).model_dump()
        except (ValueError, TypeError) as error:
            raise RuntimeError("Drift memory consent target is corrupt") from error
        if row[0] == 1 and target is None:
            raise RuntimeError("Enabled Drift memory consent has no target")
        return {"enabled": bool(row[0]), "target": target, "consent_version": row[2]}

    @staticmethod
    def _consent_fingerprint(request: MemoryConsentRequest) -> str:
        value = request.model_dump(mode="json", exclude={"request_id"})
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def _consent_receipt(cls, db: sqlite3.Connection, request: MemoryConsentRequest) -> dict[str, object] | None:
        saved = db.execute(
            "SELECT fingerprint,receipt FROM initiative_memory_receipts WHERE request_id=?", (request.request_id,)
        ).fetchone()
        if saved is None:
            return None
        if saved[0] != cls._consent_fingerprint(request):
            raise ValueError("Drift memory consent request ID reused for different arguments")
        receipt = MemoryConsentReceipt.model_validate_json(saved[1])
        if (receipt.request_id != request.request_id or receipt.enabled is not request.enabled
                or receipt.version != request.expected_version + 1
                or (request.enabled and receipt.target != request.target)):
            raise RuntimeError("Drift memory consent receipt is corrupt")
        return receipt.model_dump(mode="json")

    def memory_consent(self) -> dict[str, object]:
        with self._transaction(write=False) as db:
            return self._memory_consent(db)

    def memory_consent_receipt(self, request: MemoryConsentRequest) -> dict[str, object] | None:
        with self._transaction(write=False) as db:
            return self._consent_receipt(db, request)

    def set_memory_consent(self, request: MemoryConsentRequest) -> dict[str, object]:
        with self._transaction(write=True) as db:
            saved = self._consent_receipt(db, request)
            if saved is not None:
                return saved
            current = self._memory_consent(db)
            if current["consent_version"] != request.expected_version:
                raise ValueError("Drift memory consent changed; refresh before retrying")
            target = request.target.model_dump(mode="json") if request.target is not None else current["target"]
            version = request.expected_version + 1
            receipt = {"request_id": request.request_id, "enabled": request.enabled, "target": target, "version": version}
            db.execute("UPDATE initiative_memory_consent SET enabled=?,target_json=?,version=? WHERE singleton=1",
                       (int(request.enabled), None if target is None else json.dumps(target, sort_keys=True), version))
            db.execute("INSERT INTO initiative_memory_receipts VALUES (?,?,?)",
                       (request.request_id, self._consent_fingerprint(request), json.dumps(receipt, sort_keys=True)))
            return receipt

    def propose(
        self,
        proposal_id: str,
        revision: str,
        payload: Mapping[str, object],
        due_at: datetime,
        *,
        next_due: datetime | None = None,
        initiative_target: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Append one idempotent proposal revision for its owning producer."""

        proposal = _identity("proposal_id", proposal_id)
        proposal_revision = _identity("revision", revision)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        due = _aware_utc(due_at)
        retry = _aware_utc(next_due) if next_due is not None else None
        target = _contact_target(initiative_target)
        now = datetime.now(UTC).isoformat()
        with self._transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT payload_json, due_at, next_due FROM proposals "
                "WHERE proposal_id = ? AND revision = ?",
                (proposal, proposal_revision),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (payload_json, due, retry):
                    raise RuntimeError(
                        f"Drift proposal identity conflict: {proposal}/{proposal_revision}"
                    )
                if target is not None:
                    saved = connection.execute(
                        "SELECT channel, recipient, session_id FROM initiative_proposals "
                        "WHERE proposal_id=? AND revision=?", (proposal, proposal_revision),
                    ).fetchone()
                    if saved is None or tuple(saved) != target:
                        raise RuntimeError("Drift initiative target identity conflict")
                return {
                    "inserted": False,
                    "ref": {
                        "proposal_id": proposal,
                        "revision": proposal_revision,
                        "state_version": 1,
                    },
                }
            if target is not None:
                if self._initiative_control(connection)["paused"] is True:
                    return {"inserted": False, "reason": "paused"}
                outstanding = connection.execute(
                    "SELECT p.payload_json FROM initiative_proposals i JOIN proposals p "
                    "ON p.proposal_id=i.proposal_id AND p.revision=i.revision "
                    "WHERE i.channel=? AND i.recipient=? AND i.session_id=? "
                    "AND p.status IN ('pending', 'deferred', 'selected', 'ready_for_delivery')",
                    target,
                ).fetchall()
                if any(not _initiative_expired(json.loads(row[0]), now) for row in outstanding):
                    return {"inserted": False, "reason": "outstanding"}
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, revision, payload_json, status, due_at, next_due,
                    state_version, selection_token, selected_session_id,
                    selected_turn_id, settlement_ref, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, 1, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (proposal, proposal_revision, payload_json, due, retry, now, now),
            )
            if target is not None:
                connection.execute("INSERT INTO initiative_proposals VALUES (?, ?, ?, ?, ?)",
                                   (proposal, proposal_revision, *target))
            return {
                "inserted": True,
                "ref": {
                    "proposal_id": proposal,
                    "revision": proposal_revision,
                    "state_version": 1,
                },
            }

    def snapshot(self, now: datetime, *, target: Mapping[str, object] | None = None) -> DriftSnapshot:
        """Return the next durable deadline and frozen due proposals."""

        instant = _aware_utc(now)
        contact = _contact_target(target)
        with self._transaction(write=False) as connection:
            rows = connection.execute("""
                SELECT p.proposal_id, p.revision, p.payload_json, p.due_at, p.next_due,
                       p.state_version, i.proposal_id AS initiative_id
                FROM proposals p LEFT JOIN initiative_proposals i
                  ON p.proposal_id=i.proposal_id AND p.revision=i.revision
                WHERE p.status IN ('pending', 'deferred') AND (
                    i.proposal_id IS NULL OR (
                      ? = 0 AND i.channel=? AND i.recipient=? AND i.session_id=?))
                ORDER BY p.due_at, p.proposal_id, p.revision
                """, (int(self._initiative_control(connection)["paused"] is True),
                      *(contact or ("", "", "")))).fetchall()
            rows = [row for row in rows if row["initiative_id"] is None
                    or not _initiative_expired(json.loads(row["payload_json"]), instant)]
            proposals: tuple[DriftProposal, ...] = tuple(
                {
                    "ref": {
                        "proposal_id": row["proposal_id"],
                        "revision": row["revision"],
                        "state_version": int(row["state_version"]),
                    },
                    "payload": json.loads(row["payload_json"]),
                    "due_at": row["due_at"],
                    "next_due": row["next_due"],
                    "due": row["due_at"] <= instant,
                }
                for row in rows
            )
            return {
                "next_due": rows[0]["due_at"] if rows else None,
                "proposals": proposals,
            }

    def select(
        self,
        ref: Mapping[str, object],
        accepted_turn: Mapping[str, object],
        now: datetime,
        *, target: Mapping[str, object] | None = None,
    ) -> DriftSelectResult:
        """CAS one due proposal into a Turn-bound selection."""

        proposal = _identity("proposal_id", ref.get("proposal_id"))
        revision = _identity("revision", ref.get("revision"))
        state_version = ref.get("state_version")
        if type(state_version) is not int or state_version <= 0:
            raise ValueError("Drift state_version 必须是正整数")
        accepted = _accepted_turn(accepted_turn)
        contact = _contact_target(target)
        instant = _aware_utc(now)
        token = "drift-selection:" + secrets.token_hex(16)
        with self._transaction(write=True) as connection:
            initiative = connection.execute(
                "SELECT i.channel, i.recipient, i.session_id, p.payload_json "
                "FROM initiative_proposals i JOIN proposals p "
                "ON p.proposal_id=i.proposal_id AND p.revision=i.revision "
                "WHERE i.proposal_id=? AND i.revision=?", (proposal, revision),
            ).fetchone()
            if initiative is not None:
                reason = ("target_mismatch" if contact != tuple(initiative)[:3] else
                          "expired" if _initiative_expired(json.loads(initiative["payload_json"]), instant) else
                          "paused" if self._initiative_control(connection)["paused"] is True else None)
                if reason is not None:
                    return {"selected": False, "reason": reason,
                            "selection_token": None, "accepted_turn": None}
            existing_turn = connection.execute(
                """
                SELECT 1 FROM proposals
                WHERE selected_session_id = ? AND selected_turn_id = ?
                """,
                (accepted["session_id"], accepted["turn_id"]),
            ).fetchone()
            if existing_turn is not None:
                return {
                    "selected": False,
                    "reason": "turn_already_selected",
                    "selection_token": None,
                    "accepted_turn": None,
                }
            changed = connection.execute(
                """
                UPDATE proposals
                SET status = 'selected', selection_token = ?,
                    selected_session_id = ?, selected_turn_id = ?,
                    state_version = state_version + 1, updated_at = ?
                WHERE proposal_id = ? AND revision = ? AND state_version = ?
                  AND status IN ('pending', 'deferred') AND due_at <= ?
                """,
                (
                    token,
                    accepted["session_id"],
                    accepted["turn_id"],
                    datetime.now(UTC).isoformat(),
                    proposal,
                    revision,
                    state_version,
                    instant,
                ),
            )
            return {
                "selected": changed.rowcount == 1,
                "selection_token": token if changed.rowcount == 1 else None,
                "accepted_turn": accepted if changed.rowcount == 1 else None,
            }

    def initiative_send_state(self, connection: sqlite3.Connection,
                              accepted_turn: Mapping[str, object],
                              target: Mapping[str, object]) -> str | None:
        accepted, contact = _accepted_turn(accepted_turn), _contact_target(target)
        row = connection.execute(
            "SELECT i.channel, i.recipient, i.session_id, p.payload_json, p.selection_token FROM proposals p "
            "JOIN initiative_proposals i ON p.proposal_id=i.proposal_id AND p.revision=i.revision "
            "WHERE p.selected_session_id=? AND p.selected_turn_id=?",
            (accepted["session_id"], accepted["turn_id"]),
        ).fetchone()
        if row is None:
            return None
        if tuple(row)[:3] != contact:
            return "target_mismatch"
        token = _identity("selection_token", row["selection_token"])
        if _initiative_expired(json.loads(row["payload_json"]), datetime.now(UTC).isoformat()):
            return "expired:" + token
        return "paused:" + token if self._initiative_control(connection)["paused"] is True else None

    def selected(self, limit: int = 100) -> tuple[DriftSelectionReceipt, ...]:
        """List stable selected receipts for startup reconciliation."""

        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                """
                SELECT proposal_id, revision, payload_json, due_at, next_due,
                       selection_token, selected_session_id, selected_turn_id
                FROM proposals WHERE status = 'selected'
                ORDER BY due_at, proposal_id, revision LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return tuple(_selection_receipt(row) for row in rows)

    def selection(
        self, accepted_turn: Mapping[str, object]
    ) -> DriftSelectionReceipt | None:
        """Read the exact Drift selection bound to one accepted Turn."""

        accepted = _accepted_turn(accepted_turn)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT proposal_id, revision, payload_json, due_at, next_due,
                       selection_token, selected_session_id, selected_turn_id
                FROM proposals
                WHERE status = 'selected' AND selected_session_id = ?
                  AND selected_turn_id = ?
                """,
                (accepted["session_id"], accepted["turn_id"]),
            ).fetchone()
            return None if row is None else _selection_receipt(row)

    def transition(self, token: str, action: str) -> dict[str, object]:
        """Commit one selected proposal terminal or retry transition."""

        selection = _identity("selection_token", token)
        if action not in {
            "ready_for_delivery",
            "failed",
            "defer",
            "await_change",
            "invalidated",
        }:
            raise ValueError(f"未知 Drift transition: {action}")
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT p.status, p.next_due, i.proposal_id AS initiative_id FROM proposals p "
                "LEFT JOIN initiative_proposals i ON p.proposal_id=i.proposal_id AND p.revision=i.revision "
                "WHERE p.selection_token = ?",
                (selection,),
            ).fetchone()
            if row is None:
                return {"changed": False, "reason": "selection_missing"}
            ready_invalidation = (action == "invalidated" and row["status"] == "ready_for_delivery"
                                  and row["initiative_id"] is not None)
            if row["status"] != "selected" and not ready_invalidation and not (action == "failed" and row["status"] == "ready_for_delivery"):
                return {"changed": False, "reason": f"status:{row['status']}"}
            if action == "defer" and row["next_due"] is None:
                raise RuntimeError("Drift defer 缺少 proposal owner 提供的 next_due")
            status = "deferred" if action == "defer" else action
            due_at = row["next_due"] if action == "defer" else None
            if action == "failed" or ready_invalidation:
                # 保留原领取身份，崩溃恢复能读到失败且不会再次选择同一 revision。
                connection.execute(
                    "UPDATE proposals SET status=?, state_version=state_version+1, updated_at=? WHERE selection_token=?",
                    (status, datetime.now(UTC).isoformat(), selection),
                )
            elif action == "ready_for_delivery":
                connection.execute(
                    """
                    UPDATE proposals
                    SET status = 'ready_for_delivery',
                        state_version = state_version + 1, updated_at = ?
                    WHERE selection_token = ? AND status = 'selected'
                    """,
                    (datetime.now(UTC).isoformat(), selection),
                )
            else:
                connection.execute(
                    """
                    UPDATE proposals
                    SET status = ?, due_at = COALESCE(?, due_at),
                        selection_token = NULL, selected_session_id = NULL,
                        selected_turn_id = NULL, state_version = state_version + 1,
                        updated_at = ?
                    WHERE selection_token = ? AND status = 'selected'
                    """,
                    (status, due_at, datetime.now(UTC).isoformat(), selection),
                )
            return {"changed": True, "status": status, "next_due": due_at}

    def pending_delivery(self, limit: int = 100) -> tuple[dict[str, object], ...]:
        """Return ready Drift selections without exposing proposal payloads."""

        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                """
                SELECT selection_token, selected_session_id, selected_turn_id
                FROM proposals
                WHERE status = 'ready_for_delivery'
                ORDER BY due_at, proposal_id, revision
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return tuple(
                {
                    "selection_token": _identity(
                        "selection_token", row["selection_token"]
                    ),
                    "accepted_turn": {
                        "session_id": _identity(
                            "selected_session_id", row["selected_session_id"]
                        ),
                        "turn_id": _identity(
                            "selected_turn_id", row["selected_turn_id"]
                        ),
                    },
                    "message_metadata": {
                        "tools_used": ["message_push"],
                        "evidence_item_ids": [],
                        "source_refs": [],
                        "state_summary_tag": "none",
                    },
                }
                for row in rows
            )

    def delivery(self, accepted_turn: Mapping[str, object]) -> dict[str, object] | None:
        """Read one Drift delivery state by its accepted Turn identity."""

        accepted = _accepted_turn(accepted_turn)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT selection_token, selected_session_id, selected_turn_id,
                       status, settlement_ref
                FROM proposals
                WHERE selected_session_id = ? AND selected_turn_id = ?
                """,
                (accepted["session_id"], accepted["turn_id"]),
            ).fetchone()
            if row is None:
                return None
            result: dict[str, object] = {
                "selection_token": row["selection_token"],
                "accepted_turn": dict(accepted),
                "status": row["status"],
                "settlement_ref": row["settlement_ref"],
            }
            if row["status"] == "settled":
                settlement = _identity("settlement_ref", row["settlement_ref"])
                result["receipt"] = _delivery_receipt(
                    str(row["selection_token"]), settlement
                )
            return result

    def settle_delivery(
        self, selection_token: str, settlement_ref: str
    ) -> dict[str, object]:
        """Idempotently settle one projected Drift message."""

        token = _identity("selection_token", selection_token)
        settlement = _identity("settlement_ref", settlement_ref)
        receipt = _delivery_receipt(token, settlement)
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT status, settlement_ref FROM proposals WHERE selection_token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return {"settled": False, "reason": "selection_missing"}
            if row["status"] == "settled":
                if row["settlement_ref"] != settlement:
                    raise RuntimeError("Drift delivery settlement identity conflict")
                return {
                    "settled": True,
                    "duplicate": True,
                    "status": "settled",
                    "receipt": receipt,
                }
            if row["status"] != "ready_for_delivery":
                return {"settled": False, "reason": f"status:{row['status']}"}
            connection.execute(
                """
                UPDATE proposals
                SET status = 'settled', settlement_ref = ?,
                    state_version = state_version + 1, updated_at = ?
                WHERE selection_token = ? AND status = 'ready_for_delivery'
                """,
                (settlement, datetime.now(UTC).isoformat(), token),
            )
            return {
                "settled": True,
                "duplicate": False,
                "status": "settled",
                "receipt": receipt,
            }

    @contextmanager
    def _transaction(self, *, write: bool) -> Generator[sqlite3.Connection]:
        """Open one SQLite transaction."""

        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path)
        else:
            # 只读入口不负责建库或迁移，也不申请 SQLite 写锁。
            connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True
            )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_schema(connection)
            else:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version != _SCHEMA_VERSION:
                    raise RuntimeError(f"Drift store 需要先初始化: schema version {version}")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version in (2, 3, 4):
            raise RuntimeError("Drift v2/v3/v4 requires the initiative installation migrations")
        if version not in (0, 1, _SCHEMA_VERSION):
            raise RuntimeError(f"不支持的 Drift schema version: {version}")
        if version == _SCHEMA_VERSION:
            return
        if version == 0:
            connection.execute(_TABLE_SQL)
            for statement in _INDEX_SQL:
                connection.execute(statement)
        else:
            # v1 cleared delivery identity too early. Invalidate only those exact
            # orphaned rows because their user-visible body cannot be recovered.
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposals'"
            ).fetchone()
            if table_sql is None or _normalize_sql(str(table_sql[0])) != _normalize_sql(
                _TABLE_SQL_V1
            ):
                raise RuntimeError("Drift v1 constraint-bearing table SQL 不匹配")
            connection.execute("DROP INDEX proposals_selected_turn_idx")
            connection.execute("DROP INDEX proposals_due_idx")
            connection.execute("ALTER TABLE proposals RENAME TO proposals_v1")
            connection.execute(_TABLE_SQL)
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, revision, payload_json, status, due_at, next_due,
                    state_version, selection_token, selected_session_id,
                    selected_turn_id, settlement_ref, created_at, updated_at
                )
                SELECT proposal_id, revision, payload_json,
                    CASE
                        WHEN status = 'ready_for_delivery'
                         AND selection_token IS NULL
                         AND selected_session_id IS NULL
                         AND selected_turn_id IS NULL
                        THEN 'invalidated'
                        ELSE status
                    END,
                    due_at, next_due,
                    CASE
                        WHEN status = 'ready_for_delivery'
                         AND selection_token IS NULL
                         AND selected_session_id IS NULL
                         AND selected_turn_id IS NULL
                        THEN state_version + 1
                        ELSE state_version
                    END,
                    selection_token, selected_session_id, selected_turn_id, NULL,
                    created_at,
                    CASE
                        WHEN status = 'ready_for_delivery'
                         AND selection_token IS NULL
                         AND selected_session_id IS NULL
                         AND selected_turn_id IS NULL
                        THEN ?
                        ELSE updated_at
                    END
                FROM proposals_v1
                """,
                (datetime.now(UTC).isoformat(),),
            )
            connection.execute("DROP TABLE proposals_v1")
            for statement in _INDEX_SQL:
                connection.execute(statement)
        create_control(connection)
        create_targets(connection)
        create_consent(connection)
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


_EXPECTED_COLUMNS = (
    ("proposal_id", "TEXT", 1, 1),
    ("revision", "TEXT", 1, 2),
    ("payload_json", "TEXT", 1, 0),
    ("status", "TEXT", 1, 0),
    ("due_at", "TEXT", 1, 0),
    ("next_due", "TEXT", 0, 0),
    ("state_version", "INTEGER", 1, 0),
    ("selection_token", "TEXT", 0, 0),
    ("selected_session_id", "TEXT", 0, 0),
    ("selected_turn_id", "TEXT", 0, 0),
    ("settlement_ref", "TEXT", 0, 0),
    ("created_at", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
)
_EXPECTED_INDEXES = (
    ("proposals_due_idx", 0, "c", 0, ("status", "due_at")),
    (
        "proposals_selected_turn_idx",
        1,
        "c",
        1,
        ("selected_session_id", "selected_turn_id"),
    ),
    (
        "proposals_settlement_ref_idx",
        1,
        "c",
        1,
        ("settlement_ref",),
    ),
    ("sqlite_autoindex_proposals_1", 1, "u", 0, ("selection_token",)),
    (
        "sqlite_autoindex_proposals_2",
        1,
        "pk",
        0,
        ("proposal_id", "revision"),
    ),
)


def _delivery_receipt(selection_token: str, settlement_ref: str) -> str:
    payload = f"{selection_token}\x00{settlement_ref}".encode()
    return "drift:" + hashlib.sha256(payload).hexdigest()


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


def _schema_indexes(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, int, str, int, tuple[str, ...]], ...]:
    indexes = []
    for row in connection.execute("PRAGMA index_list(proposals)"):
        name = str(row["name"])
        columns = tuple(
            str(column["name"])
            for column in connection.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (name,),
            )
        )
        indexes.append(
            (name, int(row["unique"]), str(row["origin"]), int(row["partial"]), columns)
        )
    return tuple(sorted(indexes))


def _selection_receipt(row: sqlite3.Row) -> DriftSelectionReceipt:
    return {
        "selection_token": row["selection_token"],
        "ref": {"proposal_id": row["proposal_id"], "revision": row["revision"]},
        "payload": json.loads(row["payload_json"]),
        "due_at": row["due_at"],
        "next_due": row["next_due"],
        "status": "selected",
        "accepted_turn": {
            "session_id": row["selected_session_id"],
            "turn_id": row["selected_turn_id"],
        },
    }


def _accepted_turn(value: Mapping[str, object]) -> dict[str, str]:
    return {
        "session_id": _identity("accepted_turn.session_id", value.get("session_id")),
        "turn_id": _identity("accepted_turn.turn_id", value.get("turn_id")),
    }


def _contact_target(value: Mapping[str, object] | None) -> tuple[str, str, str] | None:
    if value is None:
        return None
    return (_identity("target.channel", value.get("channel")),
            _identity("target.recipient", value.get("recipient")),
            _identity("target.session_id", value.get("session_id")))


def _initiative_expired(payload: Mapping[str, object], now: str) -> bool:
    deadline = payload.get("valid_until")
    if deadline is None:
        return False
    if not isinstance(deadline, str):
        raise ValueError("initiative valid_until 必须是带时区的时间")
    return _aware_utc(datetime.fromisoformat(deadline)) <= now


def _identity(field: str, value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field} 必须是非空且无首尾空白的字符串")
    return value


def _aware_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Drift 时间必须带时区")
    return value.astimezone(UTC).isoformat()
