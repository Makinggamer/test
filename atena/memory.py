"""記憶マネージャー: キャラごとの記憶領域の保存・検索・圧縮 (MM-01〜MM-08)。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .audit import AuditLog
from .db import now_iso
from .guardian import Guardian, normalize
from .llm import LLM, LLMError

KINDS = ("episode", "fact", "viewer_note", "digest")

DIGEST_PROMPT = """\
あなたは AI タレント「{name}」の記憶マネージャーです。以下の古い記憶を、{name} が今後の配信や企画に活かせるよう要約してください。
- 印象的な出来事、ウケた企画、常連視聴者の傾向、学んだことを優先して残す
- 個人情報や配信環境の情報は書かない
- 箇条書きで 400 文字以内"""


def _bigrams(text: str) -> set[str]:
    n = normalize(text)
    if len(n) < 2:
        return {n} if n else set()
    return {n[i:i + 2] for i in range(len(n) - 1)}


def viewer_key(platform: str, handle: str) -> str:
    return hashlib.sha256(f"{platform}:{handle}".encode("utf-8")).hexdigest()[:16]


@dataclass
class MemoryLimits:
    max_items: int = 500
    max_chars: int = 60000
    keep_recent: int = 50
    digest_batch: int = 30
    viewer_cap: int = 1000


class MemoryManager:
    def __init__(self, conn: sqlite3.Connection, guardian: Guardian, *, limits: MemoryLimits | None = None,
                 llm: LLM | None = None, model: str = "", audit: AuditLog | None = None):
        self.conn = conn
        self.guardian = guardian
        self.limits = limits or MemoryLimits()
        self.llm = llm
        self.model = model
        self.audit = audit

    # ---- 保存 ---------------------------------------------------------
    def remember(self, character_id: str, content: str, *, kind: str = "episode",
                 importance: float = 0.5) -> int | None:
        if kind not in KINDS:
            raise ValueError(f"unknown memory kind: {kind}")
        content = self.guardian.scrub(content).strip()
        if not content:
            return None
        cur = self.conn.execute(
            "INSERT INTO memories(character_id, kind, content, importance, created_at) VALUES (?,?,?,?,?)",
            (character_id, kind, content, max(0.0, min(1.0, importance)), now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def observe_viewer(self, character_id: str, platform: str, handle: str, note: str = "") -> None:
        key = viewer_key(platform, handle)
        now = now_iso()
        note = self.guardian.scrub(note).strip() if note else ""
        row = self.conn.execute("SELECT notes FROM viewers WHERE character_id=? AND viewer_key=?",
                                (character_id, key)).fetchone()
        if row:
            notes = row["notes"]
            if note and note not in notes:
                notes = (notes + " / " + note).strip(" /")[-300:]
            self.conn.execute(
                "UPDATE viewers SET last_seen=?, visits=visits+1, notes=?, display_name=?"
                " WHERE character_id=? AND viewer_key=?",
                (now, notes, handle, character_id, key))
        else:
            self.conn.execute(
                "INSERT INTO viewers(character_id, viewer_key, display_name, platform, first_seen, last_seen, notes)"
                " VALUES (?,?,?,?,?,?,?)", (character_id, key, handle, platform, now, now, note))
        self.conn.commit()

    def viewer_profile(self, character_id: str, platform: str, handle: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM viewers WHERE character_id=? AND viewer_key=?",
                                 (character_id, viewer_key(platform, handle))).fetchone()

    # ---- 検索 ---------------------------------------------------------
    def recall(self, character_id: str, query: str, k: int = 5) -> list[str]:
        q = _bigrams(query)
        rows = self.conn.execute("SELECT id, content, importance, created_at FROM memories WHERE character_id=?",
                                 (character_id,)).fetchall()
        now = datetime.now()
        scored = []
        for r in rows:
            m = _bigrams(r["content"])
            overlap = len(q & m) / len(q) if q else 0.0
            if q and overlap == 0:
                continue
            age_days = max(0.0, (now - datetime.fromisoformat(r["created_at"])).total_seconds() / 86400)
            recency = 1.0 / (1.0 + age_days / 7.0)
            scored.append((0.6 * overlap + 0.25 * r["importance"] + 0.15 * recency, r["id"], r["content"]))
        scored.sort(reverse=True)
        top = scored[:k]
        if top:
            self.conn.executemany("UPDATE memories SET last_access=? WHERE id=?",
                                  [(now_iso(), t[1]) for t in top])
            self.conn.commit()
        return [t[2] for t in top]

    # ---- 容量管理 -----------------------------------------------------
    def stats(self, character_id: str) -> dict:
        r = self.conn.execute("SELECT COUNT(*) n, COALESCE(SUM(LENGTH(content)),0) c FROM memories"
                              " WHERE character_id=?", (character_id,)).fetchone()
        kinds = {row["kind"]: row["n"] for row in self.conn.execute(
            "SELECT kind, COUNT(*) n FROM memories WHERE character_id=? GROUP BY kind", (character_id,))}
        viewers = self.conn.execute("SELECT COUNT(*) n FROM viewers WHERE character_id=?",
                                    (character_id,)).fetchone()["n"]
        lim = self.limits
        return {
            "items": r["n"], "chars": r["c"], "viewers": viewers, "by_kind": kinds,
            "items_usage": r["n"] / lim.max_items, "chars_usage": r["c"] / lim.max_chars,
        }

    def _over_limit(self, character_id: str) -> bool:
        s = self.stats(character_id)
        return s["items"] > self.limits.max_items or s["chars"] > self.limits.max_chars

    def _summarize(self, name: str, contents: list[str]) -> str:
        if self.llm:
            messages = [
                {"role": "system", "content": DIGEST_PROMPT.format(name=name)},
                {"role": "user", "content": "\n".join(f"- {c}" for c in contents)},
            ]
            try:
                summary = self.llm.chat(self.model, messages).strip()
                if summary:
                    return summary[:800]
            except LLMError:
                pass
        # MM-05: LLM が無くても止まらないよう機械的に圧縮
        return " / ".join(c[:40] for c in contents)[:600]

    def maintain(self, character_id: str, name: str | None = None) -> dict:
        """上限超過時に古く重要度の低い記憶を digest に圧縮し、視聴者を忘却する。"""
        name = name or character_id
        digests = forgotten = 0
        for _ in range(50):
            if not self._over_limit(character_id):
                break
            rows = self.conn.execute(
                "SELECT id, content, importance, created_at FROM memories WHERE character_id=?"
                " ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?",
                (character_id, self.limits.keep_recent)).fetchall()
            if len(rows) < 2:
                break
            batch = sorted(rows, key=lambda r: (r["importance"], r["created_at"], r["id"]))
            batch = batch[: self.limits.digest_batch]
            summary = self.guardian.scrub(self._summarize(name, [r["content"] for r in batch]))
            importance = min(0.9, sum(r["importance"] for r in batch) / len(batch) + 0.1)
            created = max(r["created_at"] for r in batch)
            self.conn.execute(
                "INSERT INTO memories(character_id, kind, content, importance, created_at) VALUES (?,?,?,?,?)",
                (character_id, "digest", summary, importance, created))
            self.conn.executemany("DELETE FROM memories WHERE id=?", [(r["id"],) for r in batch])
            self.conn.commit()
            digests += 1
            forgotten += len(batch)

        excess = self.conn.execute("SELECT COUNT(*) n FROM viewers WHERE character_id=?",
                                   (character_id,)).fetchone()["n"] - self.limits.viewer_cap
        pruned = 0
        if excess > 0:
            self.conn.execute(
                "DELETE FROM viewers WHERE rowid IN (SELECT rowid FROM viewers WHERE character_id=?"
                " ORDER BY last_seen ASC LIMIT ?)", (character_id, excess))
            self.conn.commit()
            pruned = excess

        result = {"digests_created": digests, "memories_compressed": forgotten, "viewers_pruned": pruned}
        if self.audit and (digests or pruned):
            self.audit.record(f"memory_manager:{character_id}", "maintain", result)
        return result
