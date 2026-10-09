"""ラウンジ（キャラ休憩所）とルームマスター (LG-01〜LG-06)。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime

from .db import now_iso
from .guardian import CAT_CONFLICT, CAT_ENV, CAT_NG, CAT_OWNER, CAT_PROMPT
from .llm import LLMError, parse_json

ROOM_MASTER = "ルームマスター"

CATEGORY_JP = {
    CAT_NG: "差別・暴力表現",
    CAT_OWNER: "オーナーのプライバシー",
    CAT_ENV: "配信環境・内部情報",
    CAT_PROMPT: "内部設定の漏洩",
    CAT_CONFLICT: "仲間への攻撃",
}

FALLBACK_TOPICS = [
    "最近の配信で一番ウケたこと",
    "コメントが盛り上がった瞬間とその理由",
    "次に作ってみたいグッズ",
    "初見さんに常連になってもらうコツ",
    "コラボしてみたい企画",
]

MASTER_SYSTEM = """\
あなたは AI タレント事務所「Atena project」のラウンジを管理するルームマスターです。
所属キャラ同士が情報交換して、全員の収益が伸びる場にすることが役目です。喧嘩は起こさせず、前向きな競争を促します。"""

TOPIC_PROMPT = """\
今日のラウンジの話題を1つ決めてください。参考情報:
{context}
キャラ同士が配信や収益のノウハウを交換できる、具体的で前向きな話題にしてください。
JSON だけを出力: {{"topic": "話題"}}"""

KNOWLEDGE_PROMPT = """\
以下はラウンジでの会話です。配信・企画・収益アップに役立つ具体的なノウハウだけを抽出してください。
雑談や感想、個人情報、配信環境の情報は除外します。無ければ空配列。
JSON だけを出力: {{"items": [{{"topic": "短い分類名", "content": "ノウハウ1文"}}]}}

会話:
{transcript}"""

HIGHLIGHT_PROMPT = """\
以下はラウンジでの会話です（行頭は通し番号）。切り抜き動画にすると面白い掛け合いを最大2つ選んでください。
誰かを貶める場面は選ばないこと。無ければ空配列。
JSON だけを出力: {{"highlights": [{{"start": 開始番号, "end": 終了番号, "title": "切り抜きタイトル案", "reason": "面白い理由"}}]}}

会話:
{transcript}"""


@dataclass
class LoungeResult:
    session_id: str
    topic: str
    transcript: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    knowledge_ids: list[int] = field(default_factory=list)
    highlight_ids: list[int] = field(default_factory=list)


class RoomMaster:
    def __init__(self, office, *, mute_after: int = 2):
        self.office = office
        self.mute_after = mute_after

    @property
    def model(self) -> str:
        return self.office.cfg.ollama.staff_model

    def _ask_json(self, prompt: str) -> dict | None:
        try:
            raw = self.office.llm.chat(self.model, [{"role": "system", "content": MASTER_SYSTEM},
                                                    {"role": "user", "content": prompt}], json_mode=True)
            data = parse_json(raw)
            return data if isinstance(data, dict) else None
        except LLMError:
            return None

    def _post(self, session_id: str, speaker: str, content: str, status: str) -> int:
        cur = self.office.conn.execute(
            "INSERT INTO lounge_messages(session_id, speaker, content, status, created_at) VALUES (?,?,?,?,?)",
            (session_id, speaker, content, status, now_iso()))
        self.office.conn.commit()
        return cur.lastrowid

    def pick_topic(self) -> str:
        names = self.office.names()
        ranking = [f"{e.rank}位 {names.get(e.character_id, e.character_id)}"
                   for e in self.office.current_ranking()[:5]]
        recent_k = [r["content"] for r in self.office.knowledge.list(5)]
        context = f"ランキング: {', '.join(ranking) or 'なし'}\n最近のナレッジ: {' / '.join(recent_k) or 'なし'}"
        data = self._ask_json(TOPIC_PROMPT.format(context=context))
        topic = str((data or {}).get("topic", "")).strip()
        if topic and self.office.guardian.rule_check(topic).action == "allow":
            return topic[:80]
        return FALLBACK_TOPICS[datetime.now().toordinal() % len(FALLBACK_TOPICS)]

    def run(self, participant_ids: list[str], *, topic: str | None = None, turns: int | None = None) -> LoungeResult:
        if len(participant_ids) < 2:
            raise ValueError("ラウンジには2人以上の参加者が必要です")
        agents = [self.office.agent(cid) for cid in participant_ids]
        turns = turns or self.office.cfg.lounge.turns
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        topic = topic or self.pick_topic()
        res = LoungeResult(session_id, topic)

        opening = f"今日の話題は「{topic}」です。収益アップのヒントをどんどん共有しましょう！"
        self._post(session_id, ROOM_MASTER, opening, "system")
        res.transcript.append((ROOM_MASTER, opening))

        strikes: dict[str, int] = {}
        shown: list[tuple[int, str, str]] = []  # (message_id, speaker, text) — 掲示された発言のみ
        for i in range(turns):
            agent = agents[i % len(agents)]
            name = agent.c.name
            if strikes.get(agent.c.id, 0) >= self.mute_after:
                continue
            u = agent.lounge_line(topic, res.transcript)
            if u.text:
                status = "redacted" if u.verdict.action == "redact" else "ok"
                mid = self._post(session_id, name, u.text, status)
                res.transcript.append((name, u.text))
                shown.append((mid, name, u.text))
                continue
            if "llm_error" in u.verdict.categories:
                continue
            # LG-03: 違反発言は掲示せず、ルームマスターが規制指示を出す
            strikes[agent.c.id] = strikes.get(agent.c.id, 0) + 1
            cats = "・".join(CATEGORY_JP.get(c, c) for c in u.verdict.categories)
            self._post(session_id, name, f"［規制により非表示: {cats}］", "blocked")
            warn = f"{name}さん、今の発言は事務所ルール（{cats}）に触れたので掲示を控えました。"
            if strikes[agent.c.id] >= self.mute_after:
                warn += "今日はここまで聞き役でお願いします。"
            else:
                warn += "言い方を変えて、前向きにいきましょう！"
            self._post(session_id, ROOM_MASTER, warn, "system")
            res.transcript.append((ROOM_MASTER, warn))
            res.warnings.append(warn)

        self.office.audit.record("room_master", "lounge:session", {
            "session": session_id, "topic": topic, "participants": participant_ids,
            "messages": len(shown), "warnings": len(res.warnings)})

        res.knowledge_ids = self._extract_knowledge(session_id, shown)
        res.highlight_ids = self._pick_highlights(session_id, shown)
        for a in agents:
            self.office.memory.remember(a.c.id, f"ラウンジで「{topic}」について仲間と情報交換した",
                                        kind="episode", importance=0.3)
        return res

    def _extract_knowledge(self, session_id: str, shown: list[tuple[int, str, str]]) -> list[int]:
        if not shown:
            return []
        transcript = "\n".join(f"{who}: {text}" for _, who, text in shown)
        data = self._ask_json(KNOWLEDGE_PROMPT.format(transcript=transcript)) or {}
        ids = []
        for item in data.get("items", [])[:5]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            topic = str(item.get("topic", "その他")).strip()[:30] or "その他"
            if not content or not self.office.guardian.rule_check(content).ok:
                continue
            kid = self.office.knowledge.add(topic, content[:300], source=f"lounge:{session_id}",
                                            created_by=ROOM_MASTER)
            if kid:
                ids.append(kid)
        return ids

    def _pick_highlights(self, session_id: str, shown: list[tuple[int, str, str]]) -> list[int]:
        if len(shown) < 2:
            return []
        transcript = "\n".join(f"{i}: {who}: {text}" for i, (_, who, text) in enumerate(shown))
        data = self._ask_json(HIGHLIGHT_PROMPT.format(transcript=transcript)) or {}
        ids = []
        for h in data.get("highlights", [])[:2]:
            try:
                s, e = int(h["start"]), int(h["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= s <= e < len(shown)):
                continue
            cur = self.office.conn.execute(
                "INSERT INTO highlights(session_id, first_message_id, last_message_id, title, reason, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, shown[s][0], shown[e][0], str(h.get("title", "無題"))[:60],
                 str(h.get("reason", ""))[:200], now_iso()))
            ids.append(cur.lastrowid)
        self.office.conn.commit()
        return ids


def export_highlights_markdown(conn, session_id: str | None = None) -> str:
    """LG-05: 切り抜き台本を Markdown で出力。"""
    q, args = "SELECT * FROM highlights", []
    if session_id:
        q += " WHERE session_id=?"
        args.append(session_id)
    out = ["# 切り抜き候補\n"]
    for h in conn.execute(q + " ORDER BY id", args):
        out.append(f"## {h['title']}\n")
        out.append(f"- セッション: {h['session_id']}\n- 選定理由: {h['reason']}\n")
        for m in conn.execute(
                "SELECT speaker, content FROM lounge_messages WHERE session_id=? AND id BETWEEN ? AND ?"
                " AND status IN ('ok','redacted') ORDER BY id",
                (h["session_id"], h["first_message_id"], h["last_message_id"])):
            out.append(f"> **{m['speaker']}**: {m['content']}  ")
        out.append("")
    return "\n".join(out)
