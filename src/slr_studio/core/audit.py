"""The append-only audit trail.

Every stage writes here. Entries are chained: each row's hash covers its own
content plus the previous row's hash, so removing or editing history breaks the
chain and :func:`verify` says exactly where.

This is what makes the generated Methods section honest — it is written from
these rows, not from what anyone remembers doing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import Database, utc_now
from .hashing import canonical_json, sha256_text

__all__ = ["AuditLog", "AuditEntry", "ChainBreak"]

GENESIS = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: str
    stage: str
    action: str
    actor: str
    entity: str | None
    entity_id: str | None
    detail: dict[str, Any]
    entry_hash: str


@dataclass(frozen=True)
class ChainBreak:
    entry_id: int
    reason: str


class AuditLog:
    def __init__(self, db: Database, *, actor: str = "system") -> None:
        self.db = db
        self.actor = actor

    def with_actor(self, actor: str) -> AuditLog:
        return AuditLog(self.db, actor=actor)

    # -- writing -----------------------------------------------------------

    def log(
        self,
        stage: str,
        action: str,
        *,
        entity: str | None = None,
        entity_id: str | int | None = None,
        detail: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> int:
        """Append one entry and return its row id."""
        ts = utc_now()
        actor = actor or self.actor
        detail_json = canonical_json(detail or {})
        prev = self.db.scalar(
            "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        )
        prev_hash = str(prev) if prev else GENESIS
        entity_id_str = None if entity_id is None else str(entity_id)
        entry_hash = self._hash(
            ts, stage, action, actor, entity, entity_id_str, detail_json, prev_hash
        )
        return self.db.insert(
            "audit_log",
            {
                "ts": ts,
                "stage": stage,
                "action": action,
                "actor": actor,
                "entity": entity,
                "entity_id": entity_id_str,
                "detail": detail_json,
                "prev_hash": prev_hash,
                "entry_hash": entry_hash,
            },
        )

    def log_override(
        self,
        stage: str,
        entity: str,
        entity_id: str | int,
        field: str,
        ai_value: Any,
        human_value: Any,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> int:
        """Record a human overruling the AI (integrity rule 3).

        Writes both the structured `human_override` row and an audit entry, so
        overrides can be counted for the Methods section without scanning JSON.
        """
        actor = actor or self.actor
        override_id = self.db.insert(
            "human_override",
            {
                "stage": stage,
                "entity": entity,
                "entity_id": str(entity_id),
                "field": field,
                "ai_value": None if ai_value is None else canonical_json(ai_value),
                "human_value": None if human_value is None else canonical_json(human_value),
                "reason": reason,
                "actor": actor,
                "created_at": utc_now(),
            },
        )
        self.log(
            stage,
            "human_override",
            entity=entity,
            entity_id=entity_id,
            detail={
                "field": field,
                "ai_value": ai_value,
                "human_value": human_value,
                "reason": reason,
                "override_id": override_id,
            },
            actor=actor,
        )
        return override_id

    @staticmethod
    def _hash(
        ts: str,
        stage: str,
        action: str,
        actor: str,
        entity: str | None,
        entity_id: str | None,
        detail_json: str,
        prev_hash: str,
    ) -> str:
        payload = "\x1f".join(
            [ts, stage, action, actor, entity or "", entity_id or "", detail_json, prev_hash]
        )
        return sha256_text(payload)

    # -- reading -----------------------------------------------------------

    def entries(
        self, *, stage: str | None = None, limit: int | None = None
    ) -> list[AuditEntry]:
        import json

        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if stage:
            sql += " WHERE stage = ?"
            params.append(stage)
        sql += " ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [
            AuditEntry(
                id=r["id"],
                ts=r["ts"],
                stage=r["stage"],
                action=r["action"],
                actor=r["actor"],
                entity=r["entity"],
                entity_id=r["entity_id"],
                detail=json.loads(r["detail"] or "{}"),
                entry_hash=r["entry_hash"],
            )
            for r in self.db.query(sql, params)
        ]

    def verify(self) -> list[ChainBreak]:
        """Recompute the chain. An empty list means the history is intact."""
        breaks: list[ChainBreak] = []
        expected_prev = GENESIS
        for row in self.db.query("SELECT * FROM audit_log ORDER BY id"):
            if row["prev_hash"] != expected_prev:
                breaks.append(
                    ChainBreak(row["id"], "previous hash does not match the row before it")
                )
            recomputed = self._hash(
                row["ts"],
                row["stage"],
                row["action"],
                row["actor"],
                row["entity"],
                row["entity_id"],
                row["detail"],
                row["prev_hash"],
            )
            if recomputed != row["entry_hash"]:
                breaks.append(ChainBreak(row["id"], "entry content does not match its hash"))
            expected_prev = row["entry_hash"]
        return breaks
