"""監査ログ係: ハッシュチェーン付きの操作記録 (AU-01, AU-02)。"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from .db import now_iso

GENESIS = "0" * 64


def _digest(prev_hash: str, actor: str, action: str, detail: str, created_at: str) -> str:
    payload = json.dumps([prev_hash, actor, action, detail, created_at], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, actor: str, action: str, detail: dict | str = "") -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        row = self.conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        created = now_iso()
        h = _digest(prev, actor, action, detail, created)
        self.conn.execute(
            "INSERT INTO audit_log(actor, action, detail, created_at, prev_hash, hash) VALUES (?,?,?,?,?,?)",
            (actor, action, detail, created, prev, h),
        )
        self.conn.commit()

    def verify(self) -> tuple[bool, int | None]:
        """チェーンを検証。(OK か, 最初に壊れていた行ID)"""
        prev = GENESIS
        for r in self.conn.execute("SELECT * FROM audit_log ORDER BY id"):
            expected = _digest(prev, r["actor"], r["action"], r["detail"], r["created_at"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return False, r["id"]
            prev = r["hash"]
        return True, None

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
