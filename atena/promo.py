"""広報: 配信の告知文・タイトル・概要欄の下書き (PR-01〜PR-04)。

確定した配信枠について、そのキャラ自身が下書きを書く（キャラの口調で）。
すべてガーディアンを通し、公開はしない。オーナー承認待ち（公開範囲＝L3）に出し、
オーナーがコピーして投稿するか、承認後に別の仕組みで投稿する。
概要欄には AI キャラクターであることの明記を必ず入れる（YouTube の規約・事務所憲章）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .db import now_iso
from .llm import LLMError, parse_json

PR_OFFICER = "広報"
AI_NOTICE = "※このチャンネルは AI キャラクターによる配信です。"
FIELDS = {"title": 60, "description": 600, "x_post": 140}  # 文字数の上限

PROMO_PROMPT = """\
次の配信の告知を、あなた自身の口調で下書きしてください。
配信: {title}
日時: {when}（約 {minutes} 分）
内容のメモ: {notes}
- title: YouTube の配信タイトル（{t_max}字以内。内容が分かり、見たくなるもの。誇大な表現はしない）
- description: 概要欄（{d_max}字以内。何をするか・見どころ・コメントでの参加の呼びかけ）
- x_post: X（Twitter）の告知（{x_max}字以内。日時と見どころ。ハッシュタグは2つまで）
URL・メールアドレス・他人の名前・配信環境や機材の話は書かないこと。
JSON だけを出力: {{"title": "...", "description": "...", "x_post": "..."}}"""


class PromoDesk:
    def __init__(self, office):
        self.o = office

    def _when(self, start: str) -> str:
        dt = datetime.fromisoformat(start)
        return f"{dt.month}/{dt.day}（{'月火水木金土日'[dt.weekday()]}）{dt:%H:%M}"

    def draft(self, schedule_id: int) -> dict | None:
        """配信枠 1 件の下書きを作り、承認待ちに出す。作れなければ None。"""
        slot = self.o.scheduler.get(schedule_id)
        if slot is None:
            raise KeyError(f"配信枠 #{schedule_id} はありません")
        agent = self.o.agent(slot["character_id"])
        start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
        prompt = PROMO_PROMPT.format(title=slot["title"], when=self._when(slot["start"]),
                                     minutes=int((end - start).total_seconds() // 60),
                                     notes=(slot["notes"] or "なし")[:200],
                                     t_max=FIELDS["title"], d_max=FIELDS["description"], x_max=FIELDS["x_post"])
        u = agent._say(agent.system_prompt(slot["title"]), [{"role": "user", "content": prompt}],
                       context="promo", json_mode=True)
        if not u.text:
            return None
        try:
            data = parse_json(u.text)
        except LLMError:
            return None
        if not isinstance(data, dict):
            return None
        out = {}
        g = self.o.guardian
        for key, limit in FIELDS.items():
            text = str(data.get(key) or "").strip()
            if key == "description":  # 本文を切り詰めてから、AI であることの明記を必ず末尾に付ける
                body = text.replace(AI_NOTICE, "").strip()[:limit].rstrip()
                text = (body + "\n\n" if body else "") + AI_NOTICE
            else:
                text = text[:limit]
            # 項目ごとに検査し直す（JSON 全体では通っても、一部だけ取り出すと意味が変わることがある）
            v = g.check_output(text, speaker=slot["character_id"], context=f"promo:{key}")
            if not v.ok or not text:
                return None
            out[key] = v.text
        aid, _ = self.o.approvals.request(
            "publish", f"[{agent.c.name}] 配信告知の公開: {out['title']}", level=3, requested_by=PR_OFFICER,
            ref_id=schedule_id)
        cur = self.o.conn.execute(
            "INSERT INTO promo_drafts(schedule_id, character_id, title, description, x_post, approval_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (schedule_id, slot["character_id"], out["title"], out["description"], out["x_post"], aid, now_iso()))
        self.o.conn.commit()
        self.o.audit.record(PR_OFFICER, "promo:draft", {"schedule": schedule_id, "approval": aid})
        return {"id": cur.lastrowid, "approval_id": aid, **out}

    def draft_upcoming(self, today=None, days: int = 2) -> list[dict]:
        """確定済み（approved）で、days 日以内に始まる枠のうち、まだ下書きが無いものを作る。"""
        today = today or datetime.now().date()
        until = (today + timedelta(days=days + 1)).isoformat()
        done = {r["schedule_id"] for r in self.o.conn.execute("SELECT schedule_id FROM promo_drafts")}
        out = []
        for slot in self.o.scheduler.list(start_from=today.isoformat(), until=until, status="approved"):
            if slot["id"] in done or slot["kind"] != "stream":
                continue
            d = self.draft(slot["id"])
            if d:
                out.append(d)
        return out

    def list(self, limit: int = 20):
        return self.o.conn.execute("SELECT * FROM promo_drafts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
