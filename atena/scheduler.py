"""スケジューラ: 配信枠の提案・制約チェック・空き枠提案・プリフライト (SC-01〜SC-04)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .audit import AuditLog
from .config import ResourceConfig
from .db import now_iso
from .monitor import CRITICAL, WARN, Health, ResourceMonitor

STATUSES = ("proposed", "approved", "rejected", "done", "cancelled")


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(second=0, microsecond=0)


def fmt_dt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def _overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 < b1 and b0 < a1


@dataclass
class Preflight:
    go: bool
    health: Health
    message: str


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, cfg: ResourceConfig, *, audit: AuditLog | None = None,
                 monitor: ResourceMonitor | None = None):
        self.conn = conn
        self.cfg = cfg
        self.audit = audit
        self.monitor = monitor

    # ---- 制約 ---------------------------------------------------------
    def _blocked_intervals(self, day: date) -> list[tuple[datetime, datetime, str]]:
        out = []
        for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
            for w in self.cfg.blocked_windows:
                if w.weekday is not None and d.weekday() != w.weekday:
                    continue
                s = datetime.combine(d, time.fromisoformat(w.start))
                e = datetime.combine(d, time.fromisoformat(w.end))
                if e <= s:  # 日またぎ
                    e += timedelta(days=1)
                out.append((s, e, w.reason or "オーナー使用時間"))
        return out

    def _approved(self, exclude_id: int | None = None) -> list[sqlite3.Row]:
        return [r for r in self.conn.execute("SELECT * FROM schedule WHERE status='approved'")
                if r["id"] != exclude_id]

    def validate(self, start: str, end: str, *, est_vram_mb: int | None = None,
                 exclude_id: int | None = None) -> list[str]:
        s, e = parse_dt(start), parse_dt(end)
        problems = []
        if e <= s:
            return ["終了時刻が開始時刻以前です"]

        for b0, b1, reason in self._blocked_intervals(s.date()):
            if _overlaps(s, e, b0, b1):
                problems.append(f"配信禁止枠と重複: {reason} ({fmt_dt(b0)}〜{fmt_dt(b1)})")

        approved = self._approved(exclude_id)
        overlapping = [r for r in approved if _overlaps(s, e, parse_dt(r["start"]), parse_dt(r["end"]))]
        if len(overlapping) >= self.cfg.max_concurrent_streams:
            names = ", ".join(f"#{r['id']} {r['character_id']}" for r in overlapping)
            problems.append(f"同時配信数の上限({self.cfg.max_concurrent_streams})を超えます: {names}")

        day_hours = sum(
            (parse_dt(r["end"]) - parse_dt(r["start"])).total_seconds() / 3600
            for r in approved if parse_dt(r["start"]).date() == s.date())
        total = day_hours + (e - s).total_seconds() / 3600
        if total > self.cfg.max_stream_hours_per_day:
            problems.append(f"1日の配信時間上限 {self.cfg.max_stream_hours_per_day}h を超えます (合計 {total:.1f}h)")

        vram = est_vram_mb or self.cfg.default_stream_vram_mb
        total_vram = self.monitor.known_vram_total_mb() if self.monitor else None
        if total_vram and vram > total_vram:
            problems.append(f"VRAM 見積もり {vram}MB が GPU 総量 {total_vram:.0f}MB を超えます")
        return problems

    # ---- 操作 ---------------------------------------------------------
    def propose(self, character_id: str, title: str, start: str, end: str, *, kind: str = "stream",
                est_vram_mb: int | None = None, notes: str = "") -> tuple[int, list[str]]:
        problems = self.validate(start, end, est_vram_mb=est_vram_mb)
        cur = self.conn.execute(
            "INSERT INTO schedule(character_id, title, kind, start, end, status, est_vram_mb, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (character_id, title, kind, fmt_dt(parse_dt(start)), fmt_dt(parse_dt(end)), "proposed",
             est_vram_mb, "\n".join(filter(None, [notes, *problems])), now_iso()))
        self.conn.commit()
        if self.audit:
            self.audit.record(character_id, "schedule:propose",
                              {"id": cur.lastrowid, "title": title, "start": start, "end": end, "problems": problems})
        return cur.lastrowid, problems

    def get(self, slot_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM schedule WHERE id=?", (slot_id,)).fetchone()

    def set_status(self, slot_id: int, status: str, actor: str) -> list[str]:
        """状態を変更。approve 時は制約を再チェックし、問題があれば変更せずに返す。"""
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        slot = self.get(slot_id)
        if not slot:
            raise KeyError(f"schedule #{slot_id} not found")
        if status == "approved":
            problems = self.validate(slot["start"], slot["end"], est_vram_mb=slot["est_vram_mb"],
                                     exclude_id=slot_id)
            if problems:
                return problems
        self.conn.execute("UPDATE schedule SET status=? WHERE id=?", (status, slot_id))
        self.conn.commit()
        if self.audit:
            self.audit.record(actor, f"schedule:{status}", {"id": slot_id})
        return []

    def list(self, *, start_from: str | None = None, until: str | None = None,
             status: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM schedule WHERE 1=1", []
        if start_from:
            q += " AND start >= ?"
            args.append(start_from)
        if until:
            q += " AND start < ?"
            args.append(until)
        if status:
            q += " AND status = ?"
            args.append(status)
        return self.conn.execute(q + " ORDER BY start", args).fetchall()

    def suggest(self, day: date, duration_min: int, *, earliest: str = "10:00", latest: str = "24:00",
                step_min: int = 30, limit: int = 3, est_vram_mb: int | None = None) -> list[tuple[str, str]]:
        """SC-03: 指定日の空き枠を最大 limit 件返す。"""
        start = datetime.combine(day, time.fromisoformat(earliest))
        last_end = datetime.combine(day, time(0)) + timedelta(
            hours=int(latest.split(":")[0]), minutes=int(latest.split(":")[1]))
        out, cur, dur = [], start, timedelta(minutes=duration_min)
        while cur + dur <= last_end and len(out) < limit:
            s, e = fmt_dt(cur), fmt_dt(cur + dur)
            if not self.validate(s, e, est_vram_mb=est_vram_mb):
                out.append((s, e))
                cur += dur  # 候補同士が重ならないように進める
            else:
                cur += timedelta(minutes=step_min)
        return out

    def preflight(self, slot_id: int) -> Preflight:
        """SC-04: 配信直前の PC 状態チェック。"""
        if not self.monitor:
            raise RuntimeError("monitor が設定されていません")
        h = self.monitor.check()
        if h.status == CRITICAL:
            msg = "PC 負荷が高すぎます。配信開始を延期してください: " + " / ".join(h.reasons)
            go = False
        elif h.status == WARN:
            msg = "注意しつつ配信可能です: " + " / ".join(h.reasons)
            go = True
        else:
            msg = "PC 状態は良好です。配信を開始できます。"
            go = True
        if self.audit:
            self.audit.record("resource_monitor", "schedule:preflight",
                              {"id": slot_id, "go": go, "status": h.status, "reasons": h.reasons})
        return Preflight(go, h, msg)
