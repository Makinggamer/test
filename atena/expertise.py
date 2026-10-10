"""キャラごとの専門知識（好きなもの・仕事）の記憶領域 (EX-01〜EX-08)。

情報源の優先順位（高いほうが正）:
  owner(5)     オーナーが登録
  reference(4) Wikipedia・登録サイト・信頼ドメイン、または別々の2サイト以上で裏付けが取れた Web 情報
  web(3)       検索で見つけた一般サイト
  digest(2)    要約
  comment(1)   視聴者コメント

- コメント由来の知識は「未確認」で入り、Web で裏付けが取れたら確認済みに上がる
- 新しい知識が既存の知識と食い違うときは、判定用 LLM で矛盾を確認し、優先順位の高い方を残す
  （同じ優先順位なら両方「要確認」にして話題には使わない）
- 容量を超えたら、記憶マネージャーが古い・使われない・格下げ済みの知識から整理する
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse
from datetime import datetime, timedelta

from .audit import AuditLog
from .db import now_iso
from .guardian import Guardian
from .llm import LLM, LLMError, parse_json
from .memory import _bigrams

PRIORITY = {"owner": 5, "reference": 4, "web": 3, "digest": 2, "comment": 1}
ACTIVE, UNVERIFIED, DISPUTED, SUPERSEDED = "active", "unverified", "disputed", "superseded"

SCHEMA = """
CREATE TABLE IF NOT EXISTS expertise (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT NOT NULL,           -- owner | web | digest | comment
    source_ref TEXT NOT NULL DEFAULT '', -- URL や「視聴者 ○○さん」
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,                -- active | unverified | disputed | superseded
    superseded_by INTEGER,
    uses INTEGER NOT NULL DEFAULT 0,
    last_used TEXT,
    created_at TEXT NOT NULL,
    verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_expertise_char ON expertise(character_id, status);

CREATE TABLE IF NOT EXISTS learning_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0
);
"""

CONFLICT_PROMPT = """\
あなたは知識の整合性チェック係です。「新しい情報」と矛盾する（同時には成り立たない）「既存の情報」の番号を答えてください。
言い換え・補足・別の側面の話は矛盾ではありません。事実（年・人物・数値・名称など）が食い違うものだけが矛盾です。
JSON だけを出力: {"contradicts": [番号, ...]}"""

MERGE_PROMPT = """\
以下は AI タレント「{name}」が「{topic}」について覚えている知識です。重複をまとめ、配信の話題に使いやすい知識 {n} 件以内に整理してください。
事実を変えたり、無い情報を足したりしないこと。
JSON だけを出力: {{"facts": ["知識1", "知識2", ...]}}"""


def _domain(url: str) -> str:
    try:
        host = urlparse(url.split(" + ")[-1]).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


@dataclass
class Fact:
    id: int
    topic: str
    content: str
    source_type: str
    source_ref: str
    status: str

    def label(self) -> str:
        if self.status == UNVERIFIED:
            return f"[視聴者さん情報・未確認] {self.content}"
        if self.source_type in ("owner", "reference"):
            return f"[確かな情報] {self.content}"
        if self.source_type == "web":
            return f"[Web情報] {self.content}"
        return self.content


@dataclass
class AddResult:
    id: int | None
    status: str          # added | duplicate | upgraded | rejected | superseded_on_arrival
    superseded: list[int]
    disputed: list[int]


class ExpertiseStore:
    def __init__(self, conn: sqlite3.Connection, guardian: Guardian, *, llm: LLM | None = None, judge_model: str = "",
                 staff_model: str = "", audit: AuditLog | None = None, max_facts: int = 300, max_per_topic: int = 60):
        self.conn = conn
        conn.executescript(SCHEMA)
        self.guardian = guardian
        self.llm = llm
        self.judge_model = judge_model
        self.staff_model = staff_model or judge_model
        self.audit = audit
        self.max_facts = max_facts
        self.max_per_topic = max_per_topic

    # ---- 追加・食い違いの解決 -------------------------------------------
    def _similar(self, a: str, b: str) -> float:
        x, y = _bigrams(a), _bigrams(b)
        return len(x & y) / len(x | y) if x and y else 0.0

    def _rows(self, cid: str, statuses=(ACTIVE, UNVERIFIED)) -> list[sqlite3.Row]:
        q = "SELECT * FROM expertise WHERE character_id=? AND status IN (%s)" % ",".join("?" * len(statuses))
        return self.conn.execute(q, (cid, *statuses)).fetchall()

    def _contradictions(self, content: str, candidates: list[sqlite3.Row]) -> list[int]:
        if not self.llm or not candidates:
            return []
        listing = "\n".join(f"{i}: {r['content']}" for i, r in enumerate(candidates))
        try:
            data = parse_json(self.llm.chat(self.judge_model, [
                {"role": "system", "content": CONFLICT_PROMPT},
                {"role": "user", "content": f"新しい情報: {content}\n\n既存の情報:\n{listing}"}],
                json_mode=True, options={"temperature": 0}))
            idx = [int(i) for i in data.get("contradicts", [])]
        except (LLMError, AttributeError, TypeError, ValueError):
            return []
        return [candidates[i]["id"] for i in idx if 0 <= i < len(candidates)]

    def add(self, cid: str, topic: str, content: str, *, source_type: str, source_ref: str = "") -> AddResult:
        if source_type not in PRIORITY:
            raise ValueError(f"source_type は {list(PRIORITY)} のいずれか")
        topic = topic.strip()[:40] or "その他"
        # 先に検査（内部情報・NG は伏せ字にせず丸ごと拒否）、個人情報だけは伏せ字にして保存
        verdict = self.guardian.rule_check(content)
        if not verdict.ok or not self.guardian.rule_check(topic).ok:
            return AddResult(None, "rejected", [], [])
        content = self.guardian.scrub(verdict.text).strip()[:400]
        if not content:
            return AddResult(None, "rejected", [], [])
        prio = PRIORITY[source_type]
        now = now_iso()

        existing = self._rows(cid)
        # ほぼ同じ内容: 新しい方が上位の情報源なら格上げ。一般 Web 同士でも別のサイトなら「裏付けあり」で格上げ
        for r in existing:
            if self._similar(content, r["content"]) >= 0.8:
                if (source_type == "web" and r["source_type"] == "web" and _domain(source_ref)
                        and _domain(source_ref) != _domain(r["source_ref"])):
                    source_type, prio = "reference", PRIORITY["reference"]
                    source_ref = f"{r['source_ref']} + {source_ref}"
                if prio > r["priority"]:
                    self.conn.execute(
                        "UPDATE expertise SET source_type=?, source_ref=?, priority=?, status=?, verified_at=? WHERE id=?",
                        (source_type, source_ref, prio, ACTIVE, now, r["id"]))
                    self.conn.commit()
                    return AddResult(r["id"], "upgraded", [], [])
                return AddResult(r["id"], "duplicate", [], [])

        # 同じ物事について書かれていそうな既存知識だけを矛盾判定にかける
        related = sorted((r for r in existing if r["topic"] == topic or self._similar(content, r["content"]) >= 0.25),
                         key=lambda r: -self._similar(content, r["content"]))[:8]
        conflicts = self._contradictions(content, related)

        status = UNVERIFIED if source_type == "comment" else ACTIVE
        losers, disputed = [], []
        new_loses = False
        for row in (r for r in related if r["id"] in conflicts):
            if prio > row["priority"]:
                losers.append(row["id"])
            elif prio < row["priority"]:
                new_loses = True
            else:
                disputed.append(row["id"])
        if new_loses:
            status = SUPERSEDED  # 記録は残す（誰が何を言ったかの追跡用）が、話題には使わない
        elif disputed:
            status = DISPUTED
        cur = self.conn.execute(
            "INSERT INTO expertise(character_id, topic, content, source_type, source_ref, priority, status, created_at,"
            " verified_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, topic, content, source_type, source_ref, prio, status, now, now if status == ACTIVE else None))
        fid = cur.lastrowid
        if losers:
            self.conn.executemany("UPDATE expertise SET status=?, superseded_by=? WHERE id=?",
                                  [(SUPERSEDED, fid, i) for i in losers])
        if disputed:
            self.conn.executemany("UPDATE expertise SET status=? WHERE id=?", [(DISPUTED, i) for i in disputed])
        self.conn.commit()
        if self.audit and (losers or disputed or new_loses):
            self.audit.record(f"memory_manager:{cid}", "expertise:conflict", {
                "new": fid, "source": source_type, "superseded": losers, "disputed": disputed,
                "new_superseded": new_loses})
        return AddResult(fid, "superseded_on_arrival" if new_loses else "added", losers, disputed)

    def verify(self, fact_id: int, supported: bool, *, source_ref: str = "", source_type: str = "web") -> None:
        """未確認の知識を Web で照合した結果を反映。裏付けあり → その Web の扱いで確認済み、反証 → 格下げ。"""
        if supported:
            self.conn.execute("UPDATE expertise SET status=?, priority=?, source_type=?, source_ref=?,"
                              " verified_at=? WHERE id=?",
                              (ACTIVE, PRIORITY[source_type], source_type, source_ref, now_iso(), fact_id))
        else:
            self.conn.execute("UPDATE expertise SET status=? WHERE id=?", (SUPERSEDED, fact_id))
        self.conn.commit()

    # ---- 取り出し ------------------------------------------------------
    def recall(self, cid: str, query: str, k: int = 4, *, mark_used: bool = True) -> list[Fact]:
        q = _bigrams(query)
        scored = []
        for r in self._rows(cid):
            m = _bigrams(r["topic"] + r["content"])
            overlap = len(q & m) / len(q) if q else 0
            if r["topic"] and (r["topic"] in query or (query and query in r["topic"])):
                overlap += 0.5  # 「猫」のような短い話題名でも引けるように
            if overlap <= 0:
                continue
            scored.append((overlap + 0.05 * r["priority"], r))
        scored.sort(key=lambda t: -t[0])
        facts = [self._fact(r) for _, r in scored[:k]]
        if mark_used:
            self._mark_used([f.id for f in facts])
        return facts

    def pick_for_talk(self, cid: str, topics: list[str], exclude: set[int] | None = None) -> Fact | None:
        """場繋ぎ用: 好きな話題・専門の知識から、最近使っていない確かなものを1つ選ぶ。"""
        exclude = exclude or set()
        rows = [r for r in self._rows(cid, (ACTIVE,)) if r["id"] not in exclude]
        if topics:
            preferred = [r for r in rows if r["topic"] in topics]
            rows = preferred or rows
        if not rows:
            return None
        rows.sort(key=lambda r: (r["last_used"] or "", r["uses"], -r["priority"]))
        fact = self._fact(rows[0])
        self._mark_used([fact.id])
        return fact

    def _fact(self, r) -> Fact:
        return Fact(r["id"], r["topic"], r["content"], r["source_type"], r["source_ref"], r["status"])

    def _mark_used(self, ids: list[int]) -> None:
        if ids:
            self.conn.executemany("UPDATE expertise SET uses=uses+1, last_used=? WHERE id=?",
                                  [(now_iso(), i) for i in ids])
            self.conn.commit()

    def list(self, cid: str, *, topic: str | None = None, statuses=(ACTIVE, UNVERIFIED, DISPUTED)) -> list[sqlite3.Row]:
        rows = self._rows(cid, statuses)
        return [r for r in rows if topic is None or r["topic"] == topic]

    def unverified(self, cid: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM expertise WHERE character_id=? AND status IN (?,?) ORDER BY id LIMIT ?",
                                 (cid, UNVERIFIED, DISPUTED, limit)).fetchall()

    def topic_last_studied(self, cid: str, topic: str) -> str:
        r = self.conn.execute("SELECT MAX(created_at) t FROM expertise WHERE character_id=? AND topic=? AND"
                              " source_type IN ('web','reference')", (cid, topic)).fetchone()
        return r["t"] or ""

    def stats(self, cid: str) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM expertise WHERE character_id=? GROUP BY status",
                                 (cid,)).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        topics = {r["topic"]: r["n"] for r in self.conn.execute(
            "SELECT topic, COUNT(*) n FROM expertise WHERE character_id=? AND status IN (?,?) GROUP BY topic",
            (cid, ACTIVE, UNVERIFIED))}
        usable = by_status.get(ACTIVE, 0) + by_status.get(UNVERIFIED, 0)
        return {"by_status": by_status, "topics": topics, "usable": usable, "usage": usable / self.max_facts}

    # ---- コメントから学ぶ候補 -----------------------------------------
    def queue_comment(self, cid: str, author: str, text: str) -> None:
        self.conn.execute("INSERT INTO learning_queue(character_id, author, text, created_at) VALUES (?,?,?,?)",
                          (cid, author, self.guardian.scrub(text)[:300], now_iso()))
        self.conn.commit()

    def pending_comments(self, cid: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM learning_queue WHERE character_id=? AND processed=0 ORDER BY id LIMIT ?",
                                 (cid, limit)).fetchall()

    def mark_processed(self, ids: list[int]) -> None:
        self.conn.executemany("UPDATE learning_queue SET processed=1 WHERE id=?", [(i,) for i in ids])
        self.conn.commit()

    # ---- 容量管理（記憶マネージャー） ---------------------------------
    def _merge_topic(self, cid: str, name: str, topic: str, rows: list[sqlite3.Row], target: int) -> int:
        if not self.llm:
            return 0
        listing = "\n".join(f"- {r['content']}" for r in rows)
        try:
            data = parse_json(self.llm.chat(self.staff_model, [
                {"role": "system", "content": MERGE_PROMPT.format(name=name, topic=topic, n=target)},
                {"role": "user", "content": listing}], json_mode=True))
            facts = [str(f).strip() for f in data.get("facts", []) if str(f).strip()][:target]
        except (LLMError, AttributeError):
            return 0
        if not facts:
            return 0
        # まとめた結果は、元の知識の中で一番弱い情報源の扱いにする（コメント由来が混ざれば未確認のまま）
        prio = min(r["priority"] for r in rows)
        src = {v: k for k, v in PRIORITY.items()}[prio]
        status = UNVERIFIED if src == "comment" else ACTIVE
        now = now_iso()
        for f in facts:
            f = self.guardian.scrub(f)[:400]
            if self.guardian.rule_check(f).ok:
                self.conn.execute(
                    "INSERT INTO expertise(character_id, topic, content, source_type, source_ref, priority, status,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?)", (cid, topic, f, src, "要約", prio, status, now))
        self.conn.executemany("DELETE FROM expertise WHERE id=?", [(r["id"],) for r in rows])
        self.conn.commit()
        return len(rows)

    def maintain(self, cid: str, name: str = "") -> dict:
        name = name or cid
        result = {"deleted": 0, "merged": 0}
        # 1. 格下げされて30日たった知識、7日以上未確認のまま裏付けの取れないコメント情報を消す
        old = (datetime.now() - timedelta(days=30)).replace(microsecond=0).isoformat()
        stale = (datetime.now() - timedelta(days=7)).replace(microsecond=0).isoformat()
        cur = self.conn.execute("DELETE FROM expertise WHERE character_id=? AND ((status=? AND created_at<?) OR"
                                " (status IN (?,?) AND created_at<?))",
                                (cid, SUPERSEDED, old, UNVERIFIED, DISPUTED, stale))
        result["deleted"] += cur.rowcount
        # 2. 話題ごとの上限を超えたら、古い知識を要約してまとめる
        for topic, n in self.stats(cid)["topics"].items():
            if n > self.max_per_topic:
                rows = self.conn.execute(
                    "SELECT * FROM expertise WHERE character_id=? AND topic=? AND status=? ORDER BY uses, created_at"
                    " LIMIT ?", (cid, topic, ACTIVE, n - self.max_per_topic // 2)).fetchall()
                if len(rows) >= 2:
                    result["merged"] += self._merge_topic(cid, name, topic, rows, max(3, len(rows) // 4))
        # 3. 全体の上限を超えたら、優先度・使用回数・新しさの低い順に消す
        usable = self.stats(cid)["usable"]
        if usable > self.max_facts:
            cur = self.conn.execute(
                "DELETE FROM expertise WHERE id IN (SELECT id FROM expertise WHERE character_id=? AND status IN (?,?)"
                " ORDER BY priority, uses, COALESCE(last_used, created_at) LIMIT ?)",
                (cid, ACTIVE, UNVERIFIED, usable - self.max_facts))
            result["deleted"] += cur.rowcount
        self.conn.commit()
        if self.audit and (result["deleted"] or result["merged"]):
            self.audit.record(f"memory_manager:{cid}", "expertise:maintain", result)
        return result
