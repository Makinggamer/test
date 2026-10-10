"""ラウンジ（キャラ休憩所）とルームマスター (LG-01〜LG-09)。

話題は「配信・収益の情報交換」と「誰かの好きなもの・仕事の雑談」の2種類。
発言順は性格（口数）で決まり、しばらく黙っているキャラにはルームマスターか他のキャラが話を振る。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime

from .db import now_iso
from .guardian import CAT_CONFLICT, CAT_ENV, CAT_NG, CAT_OWNER, CAT_PROMPT
from .agent import ai_opener, strip_opener
from .llm import LLMError, parse_json

ROOM_MASTER = "ルームマスター"
MANAGER_NAME = "プロジェクトマネージャー"

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
キャラ同士が配信や収益のノウハウを交換できる、具体的で前向きな話題にしてください。最近の話題と被らないこと。
JSON だけを出力: {{"topic": "話題"}}"""

KNOWLEDGE_PROMPT = """\
以下はラウンジでの会話です。配信・企画・収益アップに役立つ具体的なノウハウだけを抽出してください。
雑談や感想、個人情報、配信環境の情報は除外します。無ければ空配列。
JSON だけを出力: {{"items": [{{"topic": "短い分類名", "content": "ノウハウ1文"}}]}}

会話:
{transcript}"""

TRAIT_PROMPT = """\
次のキャラクター設定から、仲間との雑談での口数（おしゃべり度）を推定してください。
0.0 = とても無口（聞き役、話を振られたら短く答える） / 0.5 = ふつう / 1.0 = とてもおしゃべり（自分から話を広げる）
JSON だけを出力: {{"talkativeness": 0.5}}

名前: {name}
人格: {persona}
話し方: {style}"""

REVIEW_PROMPT = """\
あなたは AI タレント事務所のプロジェクトマネージャーです。いま終わったラウンジの会話を振り返り、
次回もっと自然で楽しい会話になるよう、参加キャラにラウンジでの心がけを助言してください。
観点: キャラらしさ（口調・考え方がキャラごとに違って聞こえるか。全員が同じ優等生の口調になっていないか）、
AI っぽさ（「なるほど」「素晴らしい」などの決まり文句、相手の言葉のおうむ返し、何でも肯定、一般論）、
相手の話への反応、話の広がり、同じ言い回しの繰り返し、長すぎ・短すぎ、発言の偏り。
人格・設定・目標は変えず、ラウンジでの振る舞いだけを助言します。問題が無いキャラは note を空にします。
talk は口数の微調整（喋りすぎ -0.1 / そのまま 0 / 黙りすぎ 0.1）。
人格の文章そのものを見直すべきだと思う場合だけ persona_suggestion に書きます（オーナーが判断します）。

参加者（口数の目安 0=無口〜1=おしゃべり / この回の発言数）:
{members}

会話:
{transcript}

JSON だけを出力:
{{"summary": "全体の一言", "advice": [{{"name": "キャラ名", "note": "次回の心がけ（40字以内）", "talk": 0}}],
 "persona_suggestion": [{{"name": "キャラ名", "suggestion": "人格設定の見直し案"}}]}}"""

HIGHLIGHT_PROMPT = """\
以下はラウンジでの会話です（行頭は通し番号）。切り抜き動画にすると面白い掛け合いを最大2つ選んでください。
誰かを貶める場面は選ばないこと。無ければ空配列。
JSON だけを出力: {{"highlights": [{{"start": 開始番号, "end": 終了番号, "title": "切り抜きタイトル案", "reason": "面白い理由"}}]}}

会話:
{transcript}"""


BUSINESS, HOBBY = "business", "hobby"

# その回の気分（毎回少し違う人間らしさ。ふつうが多め）
MOODS = [("", 5), ("少し眠い（言葉少なめ。でも話には乗る）", 1), ("機嫌がいい（いつもより少し饒舌）", 1),
         ("ちょっと考えごとをしている（ときどき上の空）", 1), ("わくわくしている（何か話したいことがある）", 1),
         ("少し疲れている（短めに、でも優しく）", 1)]


@dataclass
class LoungeResult:
    session_id: str
    topic: str
    mode: str = BUSINESS
    host: str | None = None  # 好きなもの・専門の回の主役（キャラ ID）
    review: dict | None = None  # マネージャーの振り返り（適用した内容）
    repeats: int = 0            # 繰り返しで発言を見送った回数
    transcript: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    knowledge_ids: list[int] = field(default_factory=list)
    highlight_ids: list[int] = field(default_factory=list)


_STRAY_END = re.compile(r"(?<=[ぁ-んァ-ヶー一-龠。、！？…」])\s+[A-Za-z][A-Za-z'-]{2,}[.!?]?\s*$")
_STRAY_MID = re.compile(r"(?<=[ぁ-んァ-ヶー一-龠。、！？])\s+[a-z][a-z'-]{2,}\s+(?=[ぁ-んァ-ヶー一-龠])")


def clean_line(text: str) -> str:
    """ローカル LLM が日本語の文に紛れ込ませる英単語（例「…だよね。 getaway」）を取り除く。"""
    text = _STRAY_END.sub("", text.strip())
    return _STRAY_MID.sub("", text).strip()


def _norm(s: str) -> str:
    return re.sub(r"[\s、。！？!?…〜ー～「」]", "", s)


def is_repeat(text: str, previous: list[str], threshold: float = 0.85) -> bool:
    """自分の前の発言とほぼ同じか。"""
    from difflib import SequenceMatcher
    a = _norm(text)
    for p in map(_norm, previous):
        short, long_ = sorted((a, p), key=len)
        if a == p or (len(short) >= 4 and short in long_) or SequenceMatcher(None, a, p).ratio() >= threshold:
            return True
    return False


@dataclass
class _Seat:
    agent: object
    talk: float
    last: int = -1      # 最後に発言したターン
    strikes: int = 0
    mood: str = ""

    @property
    def id(self) -> str:
        return self.agent.c.id

    @property
    def name(self) -> str:
        return self.agent.c.name

    def gap(self, turn: int) -> int:
        return turn - self.last if self.last >= 0 else turn + 2


class RoomMaster:
    def __init__(self, office, *, mute_after: int = 2, rng: random.Random | None = None,
                 jitter: float | None = None, listeners: list | None = None):
        """listeners: session_start / message / session_end を持つ観覧先（Discord など）。
        None なら設定（[discord]）から作る。"""
        self.office = office
        if listeners is None:
            from .discord import make_relay
            relay = make_relay(office.cfg)
            listeners = [relay] if relay else []
        self.listeners = listeners
        self.mute_after = mute_after
        self.rng = rng or random.Random()
        self.jitter = office.cfg.lounge.jitter if jitter is None else jitter
        self.last_error = ""

    @property
    def model(self) -> str:
        return self.office.cfg.ollama.staff_model

    def _ask_json(self, prompt: str) -> dict | None:
        """裏方のモデル（14B 等）で JSON を得る。そのモデルが無い・失敗したらキャラ用のモデルでもう一度。"""
        self.last_error = ""
        models = [self.model]
        fallback = self.office.cfg.ollama.character_model
        if fallback and fallback != self.model:
            models.append(fallback)
        for model in models:
            try:
                raw = self.office.llm.chat(model, [{"role": "system", "content": MASTER_SYSTEM},
                                                   {"role": "user", "content": prompt}], json_mode=True)
                data = parse_json(raw)
                if isinstance(data, dict):
                    return data
                self.last_error = f"{model} の応答が JSON ではありません"
            except LLMError as e:
                self.last_error = f"{model}: {e}"
        return None

    def _post(self, session_id: str, speaker: str, content: str, status: str, speaker_id: str = "") -> int:
        cur = self.office.conn.execute(
            "INSERT INTO lounge_messages(session_id, speaker, content, status, created_at) VALUES (?,?,?,?,?)",
            (session_id, speaker, content, status, now_iso()))
        self.office.conn.commit()
        self._notify("message", speaker_id or "room_master", speaker, content, status)
        return cur.lastrowid

    def _notify(self, event: str, *args) -> None:
        for ls in self.listeners:
            try:
                getattr(ls, event)(*args)
            except Exception as e:  # noqa: BLE001 - 観覧先の不調でラウンジを止めない
                print(f"[ラウンジ] {type(ls).__name__}.{event} 失敗: {e}")

    # ---- 性格（口数） -------------------------------------------------
    def talkativeness(self, c) -> float:
        """キャラ定義の値を優先。無ければ人格から LLM で推定し、人格が変わるまでキャッシュする。"""
        return max(0.0, min(1.0, self._base_talkativeness(c) + self.tuning(c.id)["talk_offset"]))

    def tuning(self, cid: str) -> dict:
        row = self.office.conn.execute("SELECT talk_offset, note FROM lounge_tuning WHERE character_id=?",
                                       (cid,)).fetchone()
        return {"talk_offset": row["talk_offset"], "note": row["note"]} if row else {"talk_offset": 0.0, "note": ""}

    def _base_talkativeness(self, c) -> float:
        if c.talkativeness is not None:
            return max(0.0, min(1.0, float(c.talkativeness)))
        h = hashlib.sha256(f"{c.persona}\n{c.speaking_style}".encode()).hexdigest()[:16]
        conn = self.office.conn
        row = conn.execute("SELECT talkativeness FROM lounge_traits WHERE character_id=? AND persona_hash=?",
                           (c.id, h)).fetchone()
        if row:
            return row["talkativeness"]
        data = self._ask_json(TRAIT_PROMPT.format(name=c.name, persona=c.persona[:1500] or "（未設定）",
                                                  style=c.speaking_style[:300] or "（未設定）"))
        try:
            t = max(0.0, min(1.0, float((data or {})["talkativeness"])))
        except (KeyError, TypeError, ValueError):
            return 0.5  # 推定できなければふつう扱い（キャッシュしない）
        conn.execute("INSERT OR REPLACE INTO lounge_traits(character_id, persona_hash, talkativeness) VALUES (?,?,?)",
                     (c.id, h, t))
        conn.commit()
        return t

    # ---- 話題 ---------------------------------------------------------
    def _hobby_candidates(self, participant_ids: list[str]) -> list[tuple[str, str, str]]:
        """(キャラ ID, 種類, 題材)。種類は「仕事」か「好きなもの」。"""
        out = []
        for cid in participant_ids:
            c = self.office.character(cid)
            out += [(cid, "仕事", x) for x in c.specialties] + [(cid, "好きなもの", x) for x in c.favorites]
        return out

    def _hobby_topic(self, cid: str, kind: str, subject: str) -> str:
        return f"{self.office.character(cid).name}さんの{kind}「{subject}」"

    def pick_topic(self, participant_ids: list[str] | None = None) -> tuple[str, str, str | None, str]:
        """(話題, モード, 主役のキャラ ID, 題材) を返す。"""
        cands = self._hobby_candidates(participant_ids or [])
        if cands and self.rng.random() < self.office.cfg.lounge.hobby_ratio:
            # 最近ラウンジで扱っていない題材を優先
            last_used = {r["subject"]: r["created_at"] for r in self.office.conn.execute(
                "SELECT subject, MAX(created_at) AS created_at FROM lounge_sessions WHERE mode=? GROUP BY subject",
                (HOBBY,))}
            self.rng.shuffle(cands)
            cid, kind, subject = min(cands, key=lambda x: last_used.get(x[2], ""))
            return self._hobby_topic(cid, kind, subject), HOBBY, cid, subject
        return self._business_topic(), BUSINESS, None, ""

    def _business_topic(self) -> str:
        names = self.office.names()
        ranking = [f"{e.rank}位 {names.get(e.character_id, e.character_id)}"
                   for e in self.office.current_ranking()[:5]]
        recent_k = [r["content"] for r in self.office.knowledge.list(5)]
        recent_t = [r["topic"] for r in self.office.conn.execute(
            "SELECT topic FROM lounge_sessions WHERE mode=? ORDER BY created_at DESC LIMIT 8", (BUSINESS,))]
        context = (f"ランキング: {', '.join(ranking) or 'なし'}\n最近のナレッジ: {' / '.join(recent_k) or 'なし'}\n"
                   f"最近の話題: {' / '.join(recent_t) or 'なし'}")
        data = self._ask_json(TOPIC_PROMPT.format(context=context))
        topic = str((data or {}).get("topic", "")).strip()
        if topic and topic not in recent_t and self.office.guardian.rule_check(topic).action == "allow":
            return topic[:80]
        return FALLBACK_TOPICS[datetime.now().toordinal() % len(FALLBACK_TOPICS)]

    # ---- 発言順 -------------------------------------------------------
    def _next_seat(self, seats: list[_Seat], turn: int, last_id: str | None, host_id: str | None) -> _Seat:
        """口数が多いほど・長く黙っているほど指名されやすい。直前の発言者は少し下げる。"""
        def score(p: _Seat) -> float:
            v = p.talk + 0.15 * min(p.gap(turn), 6)
            if p.id == last_id:
                v -= 0.5
            if p.id == host_id:
                v += 0.3  # 雑談回は主役の出番を多めに
            return v + (self.rng.uniform(0, self.jitter) if self.jitter else 0)
        return max(seats, key=score)  # 同点は参加者順

    def _addressed(self, text: str, speaker: _Seat, seats: list[_Seat]) -> _Seat | None:
        """発言の中で名前を呼んで質問された相手（最後に呼ばれた人）。"""
        if "？" not in text and "?" not in text:
            return None
        hits = [(text.rfind(p.name), p) for p in seats if p is not speaker and p.name and p.name in text]
        return max(hits, key=lambda h: h[0])[1] if hits else None

    def run(self, participant_ids: list[str], *, topic: str | None = None, turns: int | None = None) -> LoungeResult:
        if len(set(participant_ids)) < 2:
            raise ValueError("ラウンジには2人以上の参加者が必要です")
        participant_ids = list(dict.fromkeys(participant_ids))
        out = [cid for cid in participant_ids if cid in self.office.all_characters and cid not in self.office.characters]
        if out:
            raise ValueError(f"Atena project に加入していないキャラはラウンジに出られません: {', '.join(out)}")
        agents = [self.office.agent(cid) for cid in participant_ids]
        turns = turns or self.office.cfg.lounge.turns
        pass_after = self.office.cfg.lounge.pass_after
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)

        host_id, subject, mode = None, "", BUSINESS
        if topic:  # 参加者の好きなもの・専門と一致すれば、その人が主役の雑談回
            for cid, kind, x in self._hobby_candidates(participant_ids):
                if topic.strip() == x:
                    host_id, subject, mode = cid, x, HOBBY
                    topic = self._hobby_topic(cid, kind, x)
                    break
        else:
            topic, mode, host_id, subject = self.pick_topic(participant_ids)
        res = LoungeResult(session_id, topic, mode, host_id)
        seats = [_Seat(a, self.talkativeness(a.c), mood=self.rng.choices([m for m, _ in MOODS],
                                                                         [w for _, w in MOODS])[0])
                 for a in agents]
        by_id = {p.id: p for p in seats}
        self._notify("session_start", session_id, topic, mode, [p.name for p in seats])

        if mode == HOBBY:
            host_name = by_id[host_id].name
            opening = f"今日は{topic}の話を聞きましょう！{host_name}さん、どんなところが面白いんですか？"
            addressed: _Seat | None = by_id[host_id]
        else:
            opening = f"今日の話題は「{topic}」です。収益アップのヒントをどんどん共有しましょう！"
            addressed = None
        self._say_master(session_id, res, opening)

        shown: list[tuple[int, str, str]] = []  # (message_id, speaker, text) — 掲示された発言のみ
        last_id: str | None = None
        for turn in range(turns):
            active = [p for p in seats if p.strikes < self.mute_after]
            if len(active) < 2:
                self._say_master(session_id, res, "今日のラウンジはここまでにしましょう。おつかれさまでした！")
                break
            asked_by = addressed if any(p is addressed for p in active) else None
            seat = asked_by or self._next_seat(active, turn, last_id, host_id)
            addressed = None
            # しばらく黙っている人がいれば、話好きな発言者に話を振ってもらう
            quiet = [p for p in active if p is not seat and p.gap(turn) > pass_after]
            target = max(quiet, key=lambda p: p.gap(turn)) if quiet else None

            hints = []
            if asked_by is not None:
                hints.append("話を振られたので、それに答えてください。")
            if mode == HOBBY:
                if seat.id == host_id:
                    hints.append(f"あなたが主役です。自分の{subject}について、覚えている知識から1つ紹介したり、"
                                 "質問に答えたりしてください。確かでないことは断定せず、知らないことは「調べておくね」と言う。")
                else:
                    hints.append(f"{by_id[host_id].name}さんの話を聞く側です。いまの話の中身に触れて、"
                                 f"{subject}について具体的な質問を1つするか、自分の経験と結びつけた感想を言ってください"
                                 "（「なるほど」だけで終わらせない）。知らないことを知ったかぶりしない。")
            note = self.tuning(seat.id)["note"]
            if note:
                hints.append(f"（マネージャーからの心がけ: {note}）")
            if target is not None and seat.talk >= 0.35:
                hints.append(f"発言の最後に、{target.name}さんに話を振ってください（質問や「どう思う？」など）。")

            mine = [t for w, t in res.transcript if w == seat.name]
            skipped = False
            u = seat.agent.lounge_line(topic, res.transcript, talkativeness=seat.talk,
                                       role_hint="".join(hints), subject=subject, avoid=mine, mood=seat.mood)
            if u.text and ai_opener(u.text):  # 「なるほど」などの決まり文句で始めたら 1 回だけ言い直させる
                retry = seat.agent.lounge_line(topic, res.transcript, talkativeness=seat.talk, subject=subject,
                                               role_hint="".join(hints) + f"「{ai_opener(u.text)}」で始めず、"
                                               "自分の気持ちや意見から話し始めてください。", avoid=mine, mood=seat.mood)
                u = retry if retry.text else u
                if u.text:
                    u.text = strip_opener(u.text)
            if u.text:
                u.text = clean_line(u.text)
                if is_repeat(u.text, mine):  # 同じことの繰り返しは 1 回だけ言い直させ、それでもなら今回は黙る
                    u = seat.agent.lounge_line(topic, res.transcript, talkativeness=seat.talk, subject=subject,
                                               role_hint="".join(hints) + "直前の案が自分の前の発言と同じでした。"
                                               "別の内容（新しい感想・具体的な質問・自分の体験）にしてください。",
                                               avoid=mine, mood=seat.mood)
                    if u.text:
                        u.text = clean_line(u.text)
                    if u.text and is_repeat(u.text, mine):
                        skipped = True
                        res.repeats += 1
            seat.last, last_id = turn, seat.id
            if skipped:
                pass
            elif u.text:
                status = "redacted" if u.verdict.action == "redact" else "ok"
                mid = self._post(session_id, seat.name, u.text, status, seat.id)
                res.transcript.append((seat.name, u.text))
                shown.append((mid, seat.name, u.text))
                addressed = self._addressed(u.text, seat, active)
            elif not u.verdict.ok and "llm_error" not in u.verdict.categories:
                # LG-03: 違反発言は掲示せず、ルームマスターが規制指示を出す
                seat.strikes += 1
                cats = "・".join(CATEGORY_JP.get(c, c) for c in u.verdict.categories)
                self._post(session_id, seat.name, f"［規制により非表示: {cats}］", "blocked", seat.id)
                warn = f"{seat.name}さん、今の発言は事務所ルール（{cats}）に触れたので掲示を控えました。"
                if seat.strikes >= self.mute_after:
                    warn += "今日はここまで聞き役でお願いします。"
                else:
                    warn += "言い方を変えて、前向きにいきましょう！"
                self._say_master(session_id, res, warn)
                res.warnings.append(warn)
            # 振るよう頼んだのに振らなかった（または発言できなかった）ら、ルームマスターが振る
            if target is not None and addressed is not target and target.strikes < self.mute_after \
                    and turn + 1 < turns:
                self._say_master(session_id, res, f"{target.name}さんはどう思う？")
                addressed = target

        self.office.conn.execute(
            "INSERT INTO lounge_sessions(session_id, topic, mode, host_id, subject, participants, messages, warnings,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, topic, mode, host_id, subject or None, json.dumps(participant_ids), len(shown),
             len(res.warnings), now_iso()))
        self.office.conn.commit()
        self.office.audit.record("room_master", "lounge:session", {
            "session": session_id, "topic": topic, "mode": mode, "participants": participant_ids,
            "messages": len(shown), "warnings": len(res.warnings)})

        # 雑談回の発言は LLM の生成なので、事務所ナレッジやキャラの知識には入れない
        res.knowledge_ids = self._extract_knowledge(session_id, shown) if mode == BUSINESS else []
        res.highlight_ids = self._pick_highlights(session_id, shown)
        for p in seats:
            if mode == HOBBY and p.id == host_id:
                note = f"ラウンジでみんなに{subject}の話をした"
            elif mode == HOBBY:
                note = f"ラウンジで{by_id[host_id].name}さんから{subject}の話を聞いた"
            else:
                note = f"ラウンジで「{topic}」について仲間と情報交換した"
            self.office.memory.remember(p.id, note, kind="episode", importance=0.3)
        self._notify("session_end", res)
        if self.office.cfg.lounge.review and shown and self._review_due():
            res.review = self.review(res, seats, shown)
        return res

    # ---- 振り返り（マネージャー → 各キャラへの心がけ） --------------------
    def _review_due(self) -> bool:
        """[lounge] review_every 回に 1 回だけ振り返る（常時運転で #運営報告 が埋まらないように）。"""
        every = max(1, self.office.cfg.lounge.review_every)
        n = self.office.conn.execute("SELECT COUNT(*) FROM lounge_sessions").fetchone()[0]
        return n % every == 0

    def review(self, res: LoungeResult, seats: list[_Seat], shown: list[tuple[int, str, str]]) -> dict | None:
        """会話を振り返り、ラウンジでの心がけと口数の微調整を自動で反映する。
        人格の見直し案は反映せず、オーナーの承認待ちに回す（人格の変更はオーナーが決める）。"""
        counts = {p.name: sum(1 for _, w, _ in shown if w == p.name) for p in seats}
        members = "\n".join(f"- {p.name}: 口数 {p.talk:.1f} / 発言 {counts[p.name]}" for p in seats)
        transcript = "\n".join(f"{who}: {text}" for _, who, text in shown)
        data = self._ask_json(REVIEW_PROMPT.format(members=members, transcript=transcript))
        if not data:
            # 振り返れなかったことも運営メモに出す（オーナーが気づけるように）
            result = {"summary": "", "applied": [], "proposals": [], "error": self.last_error or "応答なし"}
            self._notify("review", result)
            return result
        by_name = {p.name: p for p in seats}
        g = self.office.guardian
        applied, proposals = [], []
        for a in data.get("advice") or []:
            if not isinstance(a, dict) or a.get("name") not in by_name:
                continue
            p = by_name[a["name"]]
            note = str(a.get("note") or "").strip()[:60]
            if note and not g.rule_check(note).ok:
                note = ""
            try:
                delta = max(-0.1, min(0.1, float(a.get("talk") or 0)))
            except (TypeError, ValueError):
                delta = 0.0
            if not note and not delta:
                continue
            cur = self.tuning(p.id)
            offset = max(-0.3, min(0.3, cur["talk_offset"] + delta))
            self.office.conn.execute(
                "INSERT INTO lounge_tuning(character_id, talk_offset, note, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(character_id) DO UPDATE SET talk_offset=excluded.talk_offset, note=excluded.note,"
                " updated_at=excluded.updated_at", (p.id, offset, note or cur["note"], now_iso()))
            applied.append({"character": p.id, "name": p.name, "note": note, "talk": delta})
        for s in data.get("persona_suggestion") or []:
            if not isinstance(s, dict) or s.get("name") not in by_name:
                continue
            text = str(s.get("suggestion") or "").strip()[:300]
            if not text or not g.rule_check(text).ok:
                continue
            aid, _ = self.office.approvals.request(
                "persona", f"{s['name']} の人格の見直し案（ラウンジの振り返りより）: {text}",
                level=3, requested_by=MANAGER_NAME)
            proposals.append({"approval_id": aid, "name": s["name"], "suggestion": text})
        self.office.conn.commit()
        summary = str(data.get("summary") or "").strip()[:200]
        if summary and not g.rule_check(summary).ok:
            summary = ""
        result = {"summary": summary, "applied": applied, "proposals": proposals}
        self.office.audit.record(MANAGER_NAME, "lounge:review", {"session": res.session_id, **result})
        self._notify("review", result)  # 指摘が無い回も「問題なし」として出す
        return result

    def _say_master(self, session_id: str, res: LoungeResult, text: str) -> None:
        self._post(session_id, ROOM_MASTER, text, "system")
        res.transcript.append((ROOM_MASTER, text))

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


def list_sessions(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM lounge_sessions ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    return [{**dict(r), "participants": json.loads(r["participants"])} for r in rows]


def get_session(conn, session_id: str) -> dict | None:
    """ラウンジ閲覧アプリ用: セッション情報と掲示された発言（規制された発言は本文なし）。"""
    r = conn.execute("SELECT * FROM lounge_sessions WHERE session_id=?", (session_id,)).fetchone()
    if r is None:
        return None
    msgs = [dict(m) for m in conn.execute(
        "SELECT id, speaker, content, status, created_at FROM lounge_messages WHERE session_id=? ORDER BY id",
        (session_id,))]
    hl = [dict(h) for h in conn.execute(
        "SELECT id, first_message_id, last_message_id, title, reason FROM highlights WHERE session_id=? ORDER BY id",
        (session_id,))]
    return {**dict(r), "participants": json.loads(r["participants"]), "messages": msgs, "highlights": hl}
