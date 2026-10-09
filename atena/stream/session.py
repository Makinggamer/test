"""配信セッション: コメント応答・スパチャ記録・読み上げ・字幕・配信中の負荷監視 (SC-05)。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..db import now_iso
from ..monitor import CRITICAL
from ..moderator import PASS
from . import ChatMessage, ChatSource

OPENING = "配信開始の挨拶をしてください。今日も来てくれた視聴者にお礼を言い、コメントを歓迎してください。"
CLOSING = "配信の締めの挨拶をしてください。来てくれたお礼と、次回も来てほしいことを伝えてください。"
EARLY_CLOSING = ("事情があって今日は配信を早めに終える必要があります。理由の詳細には触れずに、"
                 "早めに締めることを謝りつつ、来てくれたお礼と次回の告知をしてください。")


@dataclass
class StreamResult:
    replies: int = 0
    superchat_jpy: int = 0
    superchats: int = 0
    memberships: int = 0
    ended_early: bool = False
    aborted: str = ""
    log: list[str] = field(default_factory=list)


class StreamSession:
    def __init__(self, office, character_id: str, source: ChatSource, *, tts=None, overlay=None,
                 speak: Callable[[str], None] = print, schedule_id: int | None = None,
                 health_interval_sec: float = 60, critical_limit: int = 3, use_llm_judge: bool | None = None,
                 greet: bool = True, clock: Callable[[], float] = time.monotonic):
        self.o = office
        self.agent = office.agent(character_id)
        self.source = source
        self.tts = tts
        self.overlay = overlay
        self.out = speak
        self.schedule_id = schedule_id
        self.health_interval = health_interval_sec
        self.critical_limit = critical_limit
        self.use_llm_judge = use_llm_judge
        self.greet = greet
        self.clock = clock
        self.result = StreamResult()
        self._critical_streak = 0
        self._last_check = clock()

    # ---- 出力 -----------------------------------------------------------
    def _speak(self, text: str) -> None:
        line = f"{self.agent.c.name}: {text}"
        self.out(line)
        self.result.log.append(line)
        if self.overlay:
            self.overlay.subtitle(text)
        if self.tts and self.agent.c.voice_speaker is not None:
            try:
                self.tts.speak(text, self.agent.c.voice_speaker)
            except RuntimeError as e:  # 読み上げが落ちても配信は止めない
                self.out(f"[読み上げエラー] {e}")

    def _line(self, instruction: str) -> None:
        text = self.agent.stream_line(instruction, use_llm_judge=self.use_llm_judge)
        if text:
            self._speak(text)

    # ---- 収益 -----------------------------------------------------------
    def _first_time(self, msg: ChatMessage) -> bool:
        if not msg.event_id:
            return True
        cur = self.o.conn.execute("INSERT OR IGNORE INTO external_events(source, event_id, created_at)"
                                  " VALUES (?,?,?)", (msg.platform, msg.event_id, now_iso()))
        self.o.conn.commit()
        return cur.rowcount == 1

    def _record_support(self, msg: ChatMessage) -> str:
        cid = self.agent.c.id
        if msg.kind == "superchat":
            self.result.superchats += 1
            if msg.amount_jpy:
                self.o.ledger.add(cid, "superchat", msg.amount_jpy, actor=f"{msg.platform}-import",
                                  memo=f"{msg.platform} {msg.amount_display}（手数料控除前）")
                self.result.superchat_jpy += msg.amount_jpy
            else:
                self.o.tasks.add(f"未換算のスパチャを手入力: {msg.amount_display}", "経理", created_by="stream")
            return f"（{msg.author}さんから {msg.amount_display} のスーパーチャットが届きました。心を込めてお礼を言ってください）"
        if msg.kind == "membership":
            self.result.memberships += 1
            return f"（{msg.author}さんがメンバーになってくれました。歓迎とお礼を伝えてください）"
        return ""

    # ---- 負荷監視 -------------------------------------------------------
    def _health_tick(self) -> bool:
        """True を返したら早めに締める。"""
        now = self.clock()
        if now - self._last_check < self.health_interval:
            return False
        self._last_check = now
        h = self.o.monitor.check()
        self._critical_streak = self._critical_streak + 1 if h.status == CRITICAL else 0
        if h.status != "ok":
            self.out(f"[PC {h.status}] " + " / ".join(h.reasons))
        return self._critical_streak >= self.critical_limit

    # ---- 本体 -----------------------------------------------------------
    def run(self) -> StreamResult:
        if self.schedule_id is not None:
            pf = self.o.scheduler.preflight(self.schedule_id)
            self.out(("GO: " if pf.go else "STOP: ") + pf.message)
            if not pf.go:
                self.result.aborted = pf.message
                return self.result
        self.o.audit.record(self.agent.c.id, "stream:start", {"schedule": self.schedule_id})
        if self.greet:
            self._line(OPENING)

        try:
            for msg in self.source.messages():
                if self._health_tick():
                    self.result.ended_early = True
                    self._line(EARLY_CLOSING)
                    break
                if msg is None:
                    continue
                if msg.kind != "text" and not self._first_time(msg):
                    continue
                extra = self._record_support(msg)
                text = msg.text or ("（コメントなしの応援）" if extra else "")
                reply = self.agent.reply_to_comment(text, msg.author, platform=msg.platform,
                                                    viewer_id=msg.viewer_id, extra_context=extra,
                                                    use_llm_judge=self.use_llm_judge)
                mod = self.agent.last_moderation
                if self.overlay and mod and mod.action == PASS:
                    self.overlay.comment(f"{msg.author}: {mod.text}")
                if reply:
                    self.result.replies += 1
                    self._speak(reply)
        except KeyboardInterrupt:
            self.out("手動で終了します")

        if not self.result.ended_early and self.greet:
            self._line(CLOSING)
        if self.overlay:
            self.overlay.clear()
        self._finish()
        return self.result

    def _finish(self) -> None:
        r = self.result
        summary = (f"配信を行い、{r.replies}件のコメントに返答した。スパチャ{r.superchats}件"
                   f"（約{r.superchat_jpy:,}円）、新規メンバー{r.memberships}人"
                   + ("。PC の負荷が高く早めに終了した" if r.ended_early else ""))
        self.o.memory.remember(self.agent.c.id, summary, kind="episode", importance=0.6)
        if self.schedule_id is not None:
            self.o.scheduler.set_status(self.schedule_id, "done", "stream")
        self.o.audit.record(self.agent.c.id, "stream:end", {
            "schedule": self.schedule_id, "replies": r.replies, "superchat_jpy": r.superchat_jpy,
            "ended_early": r.ended_early})
