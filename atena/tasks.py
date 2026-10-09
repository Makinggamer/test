"""タスク割り当てと承認キュー (PM-01, PM-03)。"""

from __future__ import annotations

import sqlite3

from .audit import AuditLog
from .db import now_iso

TASK_STATUSES = ("open", "doing", "done", "cancelled")

# 自律度レベル (企画書 4章)
LEVEL_DESCRIPTIONS = {
    0: "L0 自由（ガーディアン自動検査のみ）",
    1: "L1 マネージャー自動審査",
    2: "L2 オーナー確認",
    3: "L3 オーナーのみ（お金・契約・アカウント）",
}


class TaskBoard:
    def __init__(self, conn: sqlite3.Connection, audit: AuditLog | None = None):
        self.conn = conn
        self.audit = audit

    def add(self, title: str, assignee: str, *, created_by: str, description: str = "",
            due: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks(title, description, assignee, created_by, due, created_at) VALUES (?,?,?,?,?,?)",
            (title, description, assignee, created_by, due, now_iso()))
        self.conn.commit()
        if self.audit:
            self.audit.record(created_by, "task:add", {"id": cur.lastrowid, "title": title, "assignee": assignee})
        return cur.lastrowid

    def set_status(self, task_id: int, status: str, actor: str) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"unknown status: {status}")
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        self.conn.commit()
        if self.audit:
            self.audit.record(actor, f"task:{status}", {"id": task_id})

    def list(self, *, assignee: str | None = None, status: str | None = "open") -> list[sqlite3.Row]:
        q, args = "SELECT * FROM tasks WHERE 1=1", []
        if assignee:
            q += " AND assignee=?"
            args.append(assignee)
        if status:
            q += " AND status=?"
            args.append(status)
        return self.conn.execute(q + " ORDER BY id", args).fetchall()


class ApprovalQueue:
    def __init__(self, conn: sqlite3.Connection, audit: AuditLog | None = None,
                 auto_approve_levels: list[int] | None = None):
        self.conn = conn
        self.audit = audit
        self.auto_levels = set(auto_approve_levels if auto_approve_levels is not None else [0, 1])

    def request(self, kind: str, summary: str, *, level: int, requested_by: str,
                ref_id: int | None = None) -> tuple[int, str]:
        """承認依頼を作成。自動承認レベルなら即 approved。(id, status) を返す。"""
        auto = level in self.auto_levels and level < 3  # L3 は設定に関わらず必ずオーナー
        status = "approved" if auto else "pending"
        decided_by = "manager(auto)" if auto else None
        cur = self.conn.execute(
            "INSERT INTO approvals(kind, ref_id, requested_by, level, summary, status, decided_by, decided_at,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, ref_id, requested_by, level, summary, status, decided_by, now_iso() if auto else None, now_iso()))
        self.conn.commit()
        if self.audit:
            self.audit.record(requested_by, f"approval:request:{status}",
                              {"id": cur.lastrowid, "kind": kind, "level": level, "summary": summary})
        return cur.lastrowid, status

    def decide(self, approval_id: int, approve: bool, actor: str = "owner") -> sqlite3.Row:
        row = self.get(approval_id)
        if not row:
            raise KeyError(f"approval #{approval_id} not found")
        if row["status"] != "pending":
            raise ValueError(f"approval #{approval_id} は既に {row['status']} です")
        status = "approved" if approve else "rejected"
        self.conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                          (status, actor, now_iso(), approval_id))
        self.conn.commit()
        if self.audit:
            self.audit.record(actor, f"approval:{status}", {"id": approval_id, "kind": row["kind"]})
        return self.get(approval_id)

    def get(self, approval_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()

    def pending(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id").fetchall()
