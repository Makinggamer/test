"""事務所ナレッジベース (LG-04, LG-06)。"""

from __future__ import annotations

import sqlite3

from .db import now_iso
from .memory import _bigrams


class KnowledgeBase:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, topic: str, content: str, *, source: str, created_by: str) -> int | None:
        content = content.strip()
        if not content:
            return None
        # ほぼ同じ内容の重複登録を避ける
        new = _bigrams(content)
        for r in self.conn.execute("SELECT content FROM knowledge WHERE topic=?", (topic,)):
            old = _bigrams(r["content"])
            if new and old and len(new & old) / len(new | old) > 0.8:
                return None
        cur = self.conn.execute(
            "INSERT INTO knowledge(topic, content, source, created_by, created_at) VALUES (?,?,?,?,?)",
            (topic, content, source, created_by, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def search(self, query: str, k: int = 3) -> list[str]:
        q = _bigrams(query)
        scored = []
        for r in self.conn.execute("SELECT topic, content FROM knowledge"):
            m = _bigrams(r["topic"] + r["content"])
            score = len(q & m) / len(q) if q else 0
            if score > 0:
                scored.append((score, f"[{r['topic']}] {r['content']}"))
        scored.sort(reverse=True)
        return [s[1] for s in scored[:k]]

    def list(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM knowledge ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
