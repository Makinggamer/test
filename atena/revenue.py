"""経理・アナリスト: 収益台帳とランキング (RV-01〜RV-04)。"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .audit import AuditLog
from .db import now_iso

SOURCES = ("superchat", "membership", "ads", "goods", "other")


@dataclass
class RankEntry:
    rank: int
    character_id: str
    total: int
    previous: int
    breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def growth_pct(self) -> float | None:
        if not self.previous:
            return None
        return 100.0 * (self.total - self.previous) / self.previous


class RevenueLedger:
    def __init__(self, conn: sqlite3.Connection, audit: AuditLog | None = None):
        self.conn = conn
        self.audit = audit

    def add(self, character_id: str, source: str, amount_jpy: int, *, occurred_at: str | None = None,
            memo: str = "", actor: str = "owner") -> int:
        if source not in SOURCES:
            raise ValueError(f"source は {SOURCES} のいずれか")
        cur = self.conn.execute(
            "INSERT INTO revenue(character_id, source, amount_jpy, occurred_at, memo) VALUES (?,?,?,?,?)",
            (character_id, source, int(amount_jpy), occurred_at or now_iso(), memo))
        self.conn.commit()
        if self.audit:
            self.audit.record(actor, "revenue:add",
                              {"id": cur.lastrowid, "character": character_id, "source": source, "amount": amount_jpy})
        return cur.lastrowid

    def _totals(self, start: str, end: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.conn.execute(
                "SELECT character_id, source, SUM(amount_jpy) s FROM revenue"
                " WHERE occurred_at >= ? AND occurred_at < ? GROUP BY character_id, source", (start, end)):
            out.setdefault(r["character_id"], {})[r["source"]] = r["s"]
        return out

    def ranking(self, start: date, end: date, character_ids: list[str] | None = None) -> list[RankEntry]:
        """[start, end) の期間ランキング。前期間（同じ長さ）との比較付き。"""
        span = end - start
        cur = self._totals(start.isoformat(), end.isoformat())
        prev = self._totals((start - span).isoformat(), start.isoformat())
        ids = set(cur) | set(character_ids or [])
        entries = [RankEntry(0, cid, sum(cur.get(cid, {}).values()), sum(prev.get(cid, {}).values()),
                             dict(cur.get(cid, {}))) for cid in ids]
        entries.sort(key=lambda e: (-e.total, e.character_id))
        for i, e in enumerate(entries):
            # 同額は同順位
            e.rank = entries[i - 1].rank if i and entries[i - 1].total == e.total else i + 1
        return entries

    def monthly_ranking(self, year: int, month: int, character_ids: list[str] | None = None) -> list[RankEntry]:
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        return self.ranking(start, end, character_ids)

    def export_csv(self, start: str | None = None, end: str | None = None) -> str:
        q, args = "SELECT * FROM revenue WHERE 1=1", []
        if start:
            q += " AND occurred_at >= ?"
            args.append(start)
        if end:
            q += " AND occurred_at < ?"
            args.append(end)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "character_id", "source", "amount_jpy", "occurred_at", "memo"])
        for r in self.conn.execute(q + " ORDER BY occurred_at", args):
            w.writerow([r["id"], r["character_id"], r["source"], r["amount_jpy"], r["occurred_at"], r["memo"]])
        return buf.getvalue()


def ranking_note_for(entries: list[RankEntry], character_id: str, names: dict[str, str]) -> str:
    """キャラのプロンプトに入れる短いランキング情報。競争心は煽るが他者を下げない書き方にする。"""
    if not entries:
        return "まだ収益データがありません。最初の一歩を踏み出しましょう。"
    lines = [f"{e.rank}位 {names.get(e.character_id, e.character_id)}: {e.total:,}円" for e in entries[:5]]
    mine = next((e for e in entries if e.character_id == character_id), None)
    if mine:
        g = f"（前期比 {mine.growth_pct:+.0f}%）" if mine.growth_pct is not None else ""
        lines.append(f"あなたは {mine.rank}位{g}。仲間の良いところは参考にしつつ、自分らしいやり方で上を目指しましょう。")
    return "\n".join(lines)


def last_n_days(n: int, today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now().date()
    return today - timedelta(days=n - 1), today + timedelta(days=1)
