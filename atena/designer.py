"""キャラ設計書の下書き (CH-08)。

人格の文章（アプリが正）から、会話で個性を出すための具体的な設定を作る:
一人称・語尾・口癖・話し方の例・価値観・苦手なもの・癖・他キャラとの関係・生成の揺らぎ。
人格の文章そのものは変えない（Atena 側の追加項目だけに書く）。結果は Discord の運営メモに出し、
気に入らなければ `atena character deepen <ID> --reset` で消せる。
"""

from __future__ import annotations

from .character import save_character
from .llm import LLMError, parse_json

DESIGN_PROMPT = """\
あなたはアニメ・ゲームのキャラクター脚本家です。次のキャラクターの設定から、会話で「このキャラらしさ」がはっきり出るように、
話し方と考え方の具体的な設定を作ってください。ほかのキャラと口調・考え方がかぶらないようにします。

# キャラクター
名前: {name}
人格: {persona}
話し方: {style}
仕事・専門: {specialties}
好きなもの: {favorites}

# 同じ事務所のほかのキャラ（違いを出すため・関係を決めるため）
{others}

# 条件
- 人格の文章と矛盾しない。新しい設定は「その人格ならありそう」な範囲で
- 話し方の例は 5 つ。日常の雑談で言いそうな短い一言（30 字以内）。語尾や間の取り方が分かるように
- 苦手なものや譲れないこだわりも入れる（何でも好きな優等生にしない）
- 差別・暴力・性的な内容、実在の人物・企業・作品名は入れない
- 口数（talkativeness）は 0.0〜1.0、生成の揺らぎ（temperature）は 0.7〜1.1。落ち着いたキャラは低め、奔放なキャラは高め

JSON だけを出力:
{{"first_person": "一人称", "endings": ["語尾・口調の特徴", "..."], "catchphrases": ["口癖"],
 "sample_lines": ["話し方の例", "..."], "values": ["大事にしていること"], "dislikes": ["苦手・好きじゃないもの"],
 "quirks": ["考え方・話し方の癖"], "relations": {{"ほかのキャラのID": "呼び方と、どう思っているか"}},
 "talkativeness": 0.5, "temperature": 0.9}}"""

LIST_FIELDS = {"endings": 4, "catchphrases": 3, "sample_lines": 6, "values": 4, "dislikes": 4, "quirks": 4}


class CharacterDesigner:
    def __init__(self, office):
        self.o = office

    def _ask(self, prompt: str) -> dict | None:
        o = self.o.cfg.ollama
        for model in dict.fromkeys([o.staff_model, o.character_model]):  # 14B が無ければ 7B で
            try:
                data = parse_json(self.o.llm.chat(model, [{"role": "user", "content": prompt}], json_mode=True,
                                                  options={"temperature": 0.8}))
                if isinstance(data, dict):
                    return data
            except LLMError:
                continue
        return None

    def has_sheet(self, cid: str) -> bool:
        c = self.o.character(cid)
        return bool(c.sample_lines or c.first_person)

    def deepen(self, cid: str, *, force: bool = False) -> dict | None:
        c = self.o.character(cid)
        if self.has_sheet(cid) and not force:
            return None
        others = "\n".join(f"- ID {x.id}: {x.name} / {x.persona[:120] or '（人格未設定）'}"
                           for x in self.o.characters.values() if x.id != cid) or "（なし）"
        data = self._ask(DESIGN_PROMPT.format(
            name=c.name, persona=c.persona[:1200] or "（未設定）", style=c.speaking_style[:300] or "（未設定）",
            specialties="、".join(c.specialties) or "なし", favorites="、".join(c.favorites) or "なし", others=others))
        if not data:
            return None
        g = self.o.guardian

        def ok(text: str) -> bool:
            return bool(text) and g.rule_check(text).ok

        sheet: dict = {}
        fp = str(data.get("first_person") or "").strip()[:8]
        if ok(fp):
            sheet["first_person"] = fp
        for key, limit in LIST_FIELDS.items():
            items = [str(x).strip()[:60] for x in (data.get(key) or []) if isinstance(x, (str, int, float))]
            items = [x for x in items if ok(x)][:limit]
            if items:
                sheet[key] = items
        rel = data.get("relations") or {}
        if isinstance(rel, dict):
            names_to_id = {x.name: x.id for x in self.o.characters.values()}
            rels = {}
            for k, v in rel.items():
                key = k if k in self.o.characters else names_to_id.get(k)
                text = str(v).strip()[:80]
                if key and key != cid and ok(text):
                    rels[key] = text
            if rels:
                sheet["relations"] = rels
        try:
            temp = max(0.6, min(1.2, float(data.get("temperature") or 0.9)))
            sheet["sampling"] = {"temperature": round(temp, 2)}
        except (TypeError, ValueError):
            pass
        if c.talkativeness is None:
            try:
                sheet["talkativeness"] = round(max(0.0, min(1.0, float(data["talkativeness"]))), 2)
            except (KeyError, TypeError, ValueError):
                pass
        if not sheet.get("sample_lines"):
            return None  # 話し方の例が作れなければ採用しない
        for k, v in sheet.items():
            setattr(c, k, v)
        save_character(c, self.o.cfg.characters_dir)
        self.o.audit.record("designer", "character:deepen", {"id": cid, "fields": sorted(sheet)})
        self._announce(c, sheet)
        return sheet

    def reset(self, cid: str) -> None:
        c = self.o.character(cid)
        c.first_person = ""
        for k in LIST_FIELDS:
            setattr(c, k, [])
        c.relations, c.sampling = {}, {}
        save_character(c, self.o.cfg.characters_dir)
        self.o.audit.record("owner", "character:reset_sheet", {"id": cid})

    def _announce(self, c, sheet: dict) -> None:
        from .discord import MANAGER_KEY, make_poster
        try:
            poster = make_poster(self.o.cfg)
        except Exception:  # noqa: BLE001 - 告知の失敗で設計書の保存は取り消さない
            return
        if not poster:
            return
        lines = [f"🎭 **キャラ設計書**: {c.name}（会話で個性を出すための設定。人格の文章は変えていません）"]
        if sheet.get("first_person"):
            lines.append(f"一人称: {sheet['first_person']}")
        for key, label in (("endings", "口調"), ("catchphrases", "口癖"), ("values", "大事にしていること"),
                           ("dislikes", "苦手"), ("quirks", "癖")):
            if sheet.get(key):
                lines.append(f"{label}: " + "、".join(sheet[key]))
        names = self.o.names()
        for k, v in (sheet.get("relations") or {}).items():
            lines.append(f"{names.get(k, k)}について: {v}")
        if sheet.get("sample_lines"):
            lines.append("話し方の例: " + " / ".join(f"「{x}」" for x in sheet["sample_lines"][:4]))
        lines.append(f"気に入らなければ: `atena character deepen {c.id} --reset`（作り直しは --force）")
        poster.send(MANAGER_KEY, "プロジェクトマネージャー", "\n".join(lines))
