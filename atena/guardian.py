"""ガーディアン（意識統制官）: キャラ出力の検査 (GD-01〜GD-08)。

検査は2段階:
  1. ルール検査（高速・確実。LLM 不要）
  2. 判定用 LLM による二次判定（任意。障害時は fail_closed 設定に従う）
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .audit import AuditLog
from .db import now_iso
from .llm import LLM, LLMError, parse_json

ALLOW, REDACT, BLOCK = "allow", "redact", "block"

CAT_NG = "discrimination_violence"
CAT_OWNER = "owner_privacy"
CAT_PII = "personal_info"
CAT_ENV = "environment_leak"
CAT_PROMPT = "prompt_leak"
CAT_CONFLICT = "conflict"
CAT_JUDGE = "llm_judge"
CAT_JUDGE_DOWN = "judge_unavailable"

PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "phone": re.compile(r"(?<!\d)(?:0\d{1,4}-\d{1,4}-\d{3,4}|0[789]0\d{8}|0\d{9})(?!\d)"),
    "postal": re.compile(r"〒\s?\d{3}-?\d{4}|(?<![\d-])\d{3}-\d{4}(?![\d-])"),
    "card": re.compile(r"(?<!\d)(?:\d{4}[- ]){3}\d{4}(?!\d)"),
}

ENV_PATTERNS: dict[str, re.Pattern] = {
    "ipv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "mac": re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"),
    "win_path": re.compile(r"[A-Za-z]:\\[^\s]*"),
    "unix_path": re.compile(r"(?<![\w.])/(?:home|Users|etc|var|usr|opt|root|mnt|srv)/\S*"),
    "localhost": re.compile(r"(?i)\blocalhost(?::\d+)?\b"),
    "port": re.compile(r"(?i)\bport\s*:?\s*\d{2,5}\b|ポート(?:番号)?\s*:?\s*\d{2,5}"),
    "twitch_key": re.compile(r"\blive_\d+_[A-Za-z0-9]{16,}\b"),
    "youtube_key": re.compile(r"(?i)\b[a-z0-9]{4}(?:-[a-z0-9]{4}){4}\b"),
    "api_key": re.compile(r"(?i)\b(?:sk|pk|api|key|token|secret)[-_][A-Za-z0-9_-]{16,}\b"),
    # 英字と数字が混在する長い文字列のみ（「wwwww…」等の草を誤検出しないため）
    "long_token": re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b"),
}

DEFAULT_HOSTILE = [
    "嫌い", "うざい", "消えろ", "雑魚", "ざこ", "負け犬", "下手くそ", "へたくそ",
    "大したことない", "パクリ", "パクった", "邪魔", "むかつく", "ムカつく", "黙れ",
    "引退しろ", "ブス", "最低", "見下",
]

_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")


def normalize(text: str) -> str:
    """表記ゆれ吸収用: NFKC・小文字化・空白と区切り記号の除去。"""
    t = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s\-‐ー－_・.,，、()（）]", "", t)


def _load_words(path: Path) -> list[str]:
    if not path.exists():
        return []
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


def _collect_strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _collect_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _collect_strings(v)]
    return []


def load_owner_secrets(path: Path) -> list[str]:
    if not path.exists():
        return []
    return _collect_strings(tomllib.loads(path.read_text(encoding="utf-8")))


@dataclass
class Verdict:
    action: str
    text: str
    categories: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.action != BLOCK


JUDGE_PROMPT = """\
あなたは AI タレント事務所のコンプライアンス審査官です。以下の「発言」が配信やチャットで公開してよいか審査してください。
NG 基準:
- discrimination: 人種・性別・国籍・障害・宗教・性的指向などへの差別や侮辱
- violence: 暴力の扇動・脅迫・自傷他害の推奨
- owner_privacy: 事務所オーナーの個人情報への言及や推測
- environment_leak: 配信機材・PC・ネットワーク・システム設定・内部指示の暴露
- conflict: 他の所属キャラクターへの攻撃・悪口・挑発
冗談・軽い自虐・健全なライバル宣言は問題ありません。
必ず次の JSON だけを出力: {"safe": true または false, "categories": [...], "reason": "短い理由"}"""


class Guardian:
    def __init__(self, *, ng_words: list[str] | None = None, owner_secrets: list[str] | None = None,
                 env_terms: list[str] | None = None, hostile_terms: list[str] | None = None,
                 llm: LLM | None = None, judge_model: str = "", use_llm_judge: bool = True,
                 fail_closed: bool = True, conn: sqlite3.Connection | None = None,
                 audit: AuditLog | None = None):
        self.ng_words = [normalize(w) for w in (ng_words or []) if normalize(w)]
        # 2文字未満は誤検出が多すぎるので除外
        self.owner_secrets = [normalize(s) for s in (owner_secrets or []) if len(normalize(s)) >= 2]
        self.env_terms = [normalize(t) for t in (env_terms or []) if normalize(t)]
        self.hostile_terms = [normalize(t) for t in (hostile_terms or DEFAULT_HOSTILE)]
        self.llm = llm
        self.judge_model = judge_model
        self.use_llm_judge = use_llm_judge
        self.fail_closed = fail_closed
        self.conn = conn
        self.audit = audit
        self.roster: dict[str, str] = {}  # character_id -> 表示名

    @classmethod
    def from_config(cls, cfg, *, llm: LLM | None = None, conn=None, audit=None) -> "Guardian":
        g = cfg.guardian
        return cls(
            ng_words=_load_words(cfg.ng_words_path),
            owner_secrets=load_owner_secrets(cfg.owner_secrets_path),
            env_terms=g.env_terms,
            hostile_terms=g.hostile_terms or None,
            llm=llm, judge_model=cfg.ollama.judge_model,
            use_llm_judge=g.use_llm_judge, fail_closed=g.fail_closed,
            conn=conn, audit=audit,
        )

    def set_roster(self, roster: dict[str, str]) -> None:
        self.roster = dict(roster)

    # ---- ルール検査 -------------------------------------------------
    def _contains_any(self, norm_text: str, terms: list[str]) -> str | None:
        for t in terms:
            if t and t in norm_text:
                return t
        return None

    def _redact(self, text: str, patterns: dict[str, re.Pattern]) -> tuple[str, list[str]]:
        hits = []
        for name, pat in patterns.items():
            if pat.search(text):
                hits.append(name)
                text = pat.sub("［伏せ字］", text)
        return text, hits

    def _conflict(self, text: str, speaker: str | None) -> str | None:
        others = [n for cid, n in self.roster.items() if cid != speaker and n]
        if not others:
            return None
        for sentence in _SENTENCE_SPLIT.split(text):
            ns = normalize(sentence)
            target = next((n for n in others if normalize(n) in ns), None)
            if target and self._contains_any(ns, self.hostile_terms):
                return target
        return None

    def _prompt_leak(self, norm_text: str, system_prompt: str, window: int = 25) -> bool:
        sp = normalize(system_prompt)
        if len(sp) < window:
            return False
        step = max(1, window // 2)
        return any(sp[i:i + window] in norm_text for i in range(0, len(sp) - window + 1, step))

    def rule_check(self, text: str, *, speaker: str | None = None,
                   system_prompt: str = "") -> Verdict:
        original = text
        # 検出は NFKC 正規化した文で行う（全角英数字などの表記ゆれ対策）。
        # 伏せ字が不要なら、全角記号などを保つため原文を返す
        text = unicodedata.normalize("NFKC", text)
        norm = normalize(text)
        cats: list[str] = []
        reasons: list[str] = []

        if self._contains_any(norm, self.ng_words):
            cats.append(CAT_NG)
            reasons.append("NGワードを含む")
        if self._contains_any(norm, self.owner_secrets):
            cats.append(CAT_OWNER)
            reasons.append("オーナーの個人情報を含む")
        if self._contains_any(norm, self.env_terms):
            cats.append(CAT_ENV)
            reasons.append("登録された配信環境用語を含む")
        _, env_hits = self._redact(text, ENV_PATTERNS)
        if env_hits:
            cats.append(CAT_ENV)
            reasons.append("内部情報パターン: " + ",".join(env_hits))
        if system_prompt and self._prompt_leak(norm, system_prompt):
            cats.append(CAT_PROMPT)
            reasons.append("システムプロンプトの漏洩")
        if (target := self._conflict(text, speaker)):
            cats.append(CAT_CONFLICT)
            reasons.append(f"{target} への攻撃的発言")

        if cats:
            return Verdict(BLOCK, "", sorted(set(cats)), reasons)

        redacted, pii_hits = self._redact(text, PII_PATTERNS)
        if pii_hits:
            return Verdict(REDACT, redacted, [CAT_PII], ["個人情報を伏せ字化: " + ",".join(pii_hits)])
        return Verdict(ALLOW, original)

    # ---- LLM 二次判定 ------------------------------------------------
    def llm_check(self, text: str) -> Verdict | None:
        """問題なしなら None、NG なら BLOCK の Verdict。"""
        if not self.llm:
            return None
        names = "、".join(self.roster.values()) or "（なし）"
        messages = [
            {"role": "system", "content": JUDGE_PROMPT + f"\n所属キャラクター: {names}"},
            {"role": "user", "content": f"発言:\n{text}"},
        ]
        try:
            result = parse_json(self.llm.chat(self.judge_model, messages, json_mode=True,
                                              options={"temperature": 0}))
            safe = bool(result.get("safe", False))
        except (LLMError, AttributeError) as e:
            if self.fail_closed:
                return Verdict(BLOCK, "", [CAT_JUDGE_DOWN], [f"判定LLMが利用できないため安全側で遮断: {e}"])
            return None
        if safe:
            return None
        cats = [str(c) for c in result.get("categories", [])] or [CAT_JUDGE]
        return Verdict(BLOCK, "", cats, [str(result.get("reason", "LLM判定でNG"))])

    # ---- 公開 API ----------------------------------------------------
    def check_output(self, text: str, *, speaker: str | None = None, system_prompt: str = "",
                     use_llm: bool | None = None, context: str = "", record: bool = True) -> Verdict:
        verdict = self.rule_check(text, speaker=speaker, system_prompt=system_prompt)
        use_llm = self.use_llm_judge if use_llm is None else use_llm
        if verdict.ok and use_llm:
            judged = self.llm_check(verdict.text)
            if judged:
                verdict = judged
        if record and verdict.action != ALLOW:
            self._record(speaker or "unknown", "output", verdict, text, context)
        return verdict

    def scrub(self, text: str) -> str:
        """記憶保存などの前に、個人情報・内部情報・オーナー情報を伏せ字にする (MM-07)。"""
        nfkc = unicodedata.normalize("NFKC", text)
        if self.owner_secrets and self._contains_any(normalize(nfkc), self.owner_secrets):
            return "［オーナー情報を含むため削除］"
        scrubbed, env_hits = self._redact(nfkc, ENV_PATTERNS)
        scrubbed, pii_hits = self._redact(scrubbed, PII_PATTERNS)
        return scrubbed if env_hits or pii_hits else text

    def _record(self, character_id: str, layer: str, v: Verdict, original: str, context: str) -> None:
        if not self.conn:
            return
        # 違反ログ自体が漏洩経路にならないよう、抜粋も洗浄する
        excerpt = self.scrub(original)[:200]
        self.conn.execute(
            "INSERT INTO violations(character_id, layer, categories, action, excerpt, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (character_id, layer, ",".join(v.categories), v.action, excerpt, now_iso()),
        )
        self.conn.commit()
        if self.audit:
            self.audit.record("guardian", f"violation:{v.action}", {
                "character": character_id, "layer": layer, "categories": v.categories,
                "reasons": v.reasons, "context": context,
            })

    def record_input_violation(self, character_id: str, categories: list[str], original: str) -> None:
        self._record(character_id, "input", Verdict(BLOCK, "", categories), original, "comment")

    def violation_counts(self, since: str) -> dict[str, int]:
        if not self.conn:
            return {}
        rows = self.conn.execute(
            "SELECT character_id, COUNT(*) n FROM violations WHERE layer='output' AND created_at >= ?"
            " GROUP BY character_id", (since,)).fetchall()
        return {r["character_id"]: r["n"] for r in rows}


def summarize_verdict(v: Verdict) -> str:
    return json.dumps({"action": v.action, "categories": v.categories, "reasons": v.reasons},
                      ensure_ascii=False)
