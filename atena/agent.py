"""AI キャラクター（タレント）のエージェント (CH-05〜CH-07)。

すべての発言は「プロンプト組み立て → 生成 → ガーディアン検査 → (違反なら1回だけ言い直し)」を通る。
"""

from __future__ import annotations

from dataclasses import dataclass

from .avatar import EMOTION_INSTRUCTION, parse_emotion
from .character import Character
from .guardian import BLOCK, Verdict
from .llm import LLMError, parse_json
from .moderator import DEFLECT, DROP

SAFE_FALLBACK = "（おっと、ちょっと言葉を選び直しますね！）"

PLAN_PROMPT = """\
あなたの収益を伸ばすための、次の活動企画を1つ考えてください。ランキング・記憶・ナレッジを参考に、自分の個性を活かしてください。
他キャラとのコラボも歓迎ですが、相手を下げる企画は不可です。
必ず次の JSON だけを出力:
{"title": "企画名", "kind": "stream|goods|clip|other", "duration_min": 60, "preferred_time": "HH:MM",
 "revenue_idea": "なぜ収益につながるか", "needs_system": "必要な配信システムや道具(不要なら空文字)",
 "collab_with": ["コラボしたいキャラ名"]}"""


@dataclass
class Utterance:
    text: str | None
    verdict: Verdict
    retried: bool = False
    emotion: str = "neutral"


class CharacterAgent:
    def __init__(self, office, character: Character):
        self.office = office
        self.c = character
        self.last_moderation = None
        self.last_emotion = "neutral"

    @property
    def model(self) -> str:
        return self.c.model or self.office.cfg.ollama.character_model

    def system_prompt(self, query: str) -> str:
        return self.c.system_prompt(
            ranking_note=self.office.ranking_note(self.c.id),
            memories=self.office.memory.recall(self.c.id, query, k=5),
            knowledge=self.office.knowledge.search(query, k=3),
        )

    def _say(self, system: str, messages: list[dict], *, context: str, use_llm_judge: bool | None = None,
             json_mode: bool = False, emotion: bool = False) -> Utterance:
        """emotion=True なら発言先頭の感情タグを取り外してから検査し、Utterance.emotion に入れる。"""
        if emotion:
            system = system + "\n\n# 感情タグ\n" + EMOTION_INSTRUCTION
        g = self.office.guardian
        # 漏洩検査は固定部分（憲章+人格）に対して行う。記憶やナレッジは話題にしてよい
        secret = self.c.system_prompt()
        convo = [{"role": "system", "content": system}, *messages]
        try:
            raw = self.office.llm.chat(self.model, convo, json_mode=json_mode)
        except LLMError as e:
            return Utterance(None, Verdict(BLOCK, "", ["llm_error"], [str(e)]))
        emo, body = parse_emotion(raw) if emotion else ("neutral", raw)
        v = g.check_output(body, speaker=self.c.id, system_prompt=secret, use_llm=use_llm_judge, context=context)
        if v.ok:
            return Utterance(v.text, v, emotion=emo)
        # ガーディアンからの是正指示つきで1回だけ言い直させる
        correction = {"role": "system", "content":
                      "直前の発言案は事務所ルールに抵触したため公開されませんでした。理由: "
                      + " / ".join(v.reasons) + "。ルールを守って言い直してください。"}
        try:
            raw2 = self.office.llm.chat(self.model, [*convo, {"role": "assistant", "content": raw}, correction],
                                        json_mode=json_mode)
        except LLMError:
            return Utterance(None, v, retried=True)
        emo2, body2 = parse_emotion(raw2) if emotion else ("neutral", raw2)
        v2 = g.check_output(body2, speaker=self.c.id, system_prompt=secret, use_llm=use_llm_judge,
                            context=context + ":retry")
        return Utterance(v2.text if v2.ok else None, v2, retried=True, emotion=emo2)

    # ---- 配信コメント応答 ---------------------------------------------
    def reply_to_comment(self, comment: str, author: str, *, platform: str = "console",
                         use_llm_judge: bool | None = None, viewer_id: str | None = None,
                         extra_context: str = "") -> str | None:
        """extra_context: スーパーチャット等、事務所側で確認済みの事実（視聴者の入力は含めない）。"""
        mod = self.office.moderator.check(comment, author, character_id=self.c.id)
        self.last_moderation = mod
        if mod.action == DROP:
            return None
        mem = self.office.memory
        profile = mem.viewer_profile(self.c.id, platform, author, viewer_id)
        viewer_note = ""
        if profile:
            viewer_note = f"（{author}さんは {profile['visits']} 回目の来訪。メモ: {profile['notes'] or 'なし'}）"
        if mod.action == DEFLECT:
            user_msg = f"{author}さんのコメント: {mod.text}"
        else:
            user_msg = f"{author}さんのコメント: {mod.text}{viewer_note}\n配信中なので2〜3文で返答してください。"
        if extra_context:
            user_msg = f"{extra_context}\n{user_msg}"
        system = self.system_prompt(mod.text if mod.action != DEFLECT else author)
        u = self._say(system, [{"role": "user", "content": user_msg}], context=f"stream:{platform}",
                      use_llm_judge=use_llm_judge, emotion=True)
        self.last_emotion = u.emotion
        if "llm_error" in u.verdict.categories:
            return None  # LLM 停止中は変な定型文を流さず黙る
        reply = u.text or SAFE_FALLBACK
        mem.observe_viewer(self.c.id, platform, author, viewer_id=viewer_id)
        if mod.action != DEFLECT and u.text:
            mem.remember(self.c.id, f"配信で{author}さん「{mod.text[:80]}」に「{u.text[:80]}」と返した",
                         kind="episode", importance=0.4)
        return reply

    def stream_line(self, instruction: str, *, use_llm_judge: bool | None = None) -> str | None:
        """配信の挨拶・締めなど、事務所からの指示に沿った一言。"""
        system = self.system_prompt(instruction)
        u = self._say(system, [{"role": "user", "content": instruction + "\n配信中なので2〜3文で話してください。"}],
                      context="stream:line", use_llm_judge=use_llm_judge, emotion=True)
        self.last_emotion = u.emotion
        return u.text

    # ---- 企画提案 -----------------------------------------------------
    def propose_plan(self) -> dict | None:
        system = self.system_prompt("企画 収益 配信 グッズ")
        u = self._say(system, [{"role": "user", "content": PLAN_PROMPT}], context="plan", json_mode=True)
        if not u.text:
            return None
        try:
            plan = parse_json(u.text)
        except LLMError:
            return None
        if not isinstance(plan, dict) or not plan.get("title"):
            return None
        plan.setdefault("kind", "stream")
        try:
            plan["duration_min"] = max(15, min(240, int(plan.get("duration_min") or 60)))
        except (TypeError, ValueError):
            plan["duration_min"] = 60
        plan.setdefault("needs_system", "")
        plan["collab_with"] = [str(x) for x in plan.get("collab_with") or []]
        return plan

    # ---- ラウンジ発言 -------------------------------------------------
    def lounge_line(self, topic: str, transcript: list[tuple[str, str]]) -> Utterance:
        recent = "\n".join(f"{who}: {text}" for who, text in transcript[-10:]) or "（まだ誰も話していません）"
        prompt = (f"ここは所属キャラだけの休憩所（ラウンジ）です。話題: {topic}\n"
                  f"これまでの会話:\n{recent}\n\n"
                  f"{self.c.name}として、配信や収益アップのために試したこと・気づきを交えて1〜3文で発言してください。"
                  "名前の接頭辞は付けないでください。")
        system = self.system_prompt(topic)
        return self._say(system, [{"role": "user", "content": prompt}], context="lounge")
