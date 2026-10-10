"""事務所 (Office): 各役職のサービスを束ねる入口。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .audit import AuditLog
from .character import Character, load_characters
from .config import Config
from .db import connect
from .expertise import ExpertiseStore
from .guardian import Guardian
from .knowledge import KnowledgeBase
from .llm import LLM, OllamaClient
from .memory import MemoryLimits, MemoryManager
from .moderator import CommentModerator
from .monitor import ResourceMonitor, make_sampler
from .revenue import RevenueLedger, ranking_note_for
from .scheduler import Scheduler
from .tasks import ApprovalQueue, TaskBoard


class Office:
    def __init__(self, cfg: Config, *, llm: LLM | None = None, db_path=None,
                 characters: dict[str, Character] | None = None, sampler=None):
        self.cfg = cfg
        self.conn = connect(db_path or cfg.db_path)
        self.llm: LLM = llm or OllamaClient(cfg.ollama.host, cfg.ollama.timeout_sec)
        self.audit = AuditLog(self.conn)
        self._set_characters(characters if characters is not None else load_characters(cfg.characters_dir))
        self.guardian = Guardian.from_config(cfg, llm=self.llm, conn=self.conn, audit=self.audit)
        self.guardian.set_roster(self.names())
        self.moderator = CommentModerator(self.guardian)
        m = cfg.memory
        self.memory = MemoryManager(
            self.conn, self.guardian, llm=self.llm, model=cfg.ollama.staff_model, audit=self.audit,
            limits=MemoryLimits(m.max_items, m.max_chars, m.keep_recent, m.digest_batch, m.viewer_cap))
        self.knowledge = KnowledgeBase(self.conn)
        lc = cfg.learning
        self.expertise = ExpertiseStore(self.conn, self.guardian, llm=self.llm, judge_model=cfg.ollama.judge_model,
                                        staff_model=cfg.ollama.staff_model, audit=self.audit,
                                        max_facts=lc.max_facts, max_per_topic=lc.max_per_topic)
        self.ledger = RevenueLedger(self.conn, self.audit)
        self.monitor = ResourceMonitor(cfg.resources, self.conn,
                                       sampler=sampler or make_sampler(cfg.resources, cfg.ollama.host))
        self.scheduler = Scheduler(self.conn, cfg.resources, audit=self.audit, monitor=self.monitor)
        self.tasks = TaskBoard(self.conn, self.audit)
        self.approvals = ApprovalQueue(self.conn, self.audit, cfg.approvals.auto_approve_levels)

    def _set_characters(self, chars: dict[str, Character]) -> None:
        # all_characters: 登録済みの全員（アプリとの同期・個別の会話用）
        # characters: 加入中（member）のキャラだけ。ラウンジ・Discord・自動運転・日次サイクルはこちらを使う
        self.all_characters = chars
        self.characters = {k: c for k, c in chars.items() if c.member}

    def reload_characters(self) -> None:
        self._set_characters(load_characters(self.cfg.characters_dir))
        self.guardian.set_roster(self.names())

    def character(self, char_id: str) -> Character:
        try:
            return self.characters.get(char_id) or self.all_characters[char_id]
        except KeyError:
            raise KeyError(f"キャラクター '{char_id}' は登録されていません") from None

    def names(self) -> dict[str, str]:
        return {c.id: c.name for c in {**self.all_characters, **self.characters}.values()}

    def current_ranking(self, today: date | None = None):
        """直近30日のランキング。"""
        today = today or datetime.now().date()
        return self.ledger.ranking(today - timedelta(days=29), today + timedelta(days=1), list(self.characters))

    def ranking_note(self, char_id: str) -> str:
        return ranking_note_for(self.current_ranking(), char_id, self.names())

    def agent(self, char_id: str):
        from .agent import CharacterAgent
        return CharacterAgent(self, self.character(char_id))

    def batch_view(self, log=print, *, remote_only: bool = False, remote=None):
        """ラウンジなど裏方の処理用。[ollama] batch_host があれば別 PC の Ollama を使う事務所を返す。
        remote_only=True なら別 PC に繋がらなくても手元では動かさない（Mac が配信中など）。"""
        o = self.cfg.ollama
        if not o.batch_host:
            return self
        from .llm import RoutedLLM
        remote = remote or OllamaClient(o.batch_host, max(o.timeout_sec, 120))
        llm = RoutedLLM(remote, self.llm, fallback_model=o.batch_model,
                        local_fallback=o.batch_local_fallback and not remote_only, log=log)
        return _LLMView(self, llm)

    def close(self) -> None:
        self.conn.close()


class _LLMView:
    """Office と同じものを見せつつ、LLM（とそれを使うガーディアン）だけ差し替える。"""

    def __init__(self, office: Office, llm):
        import copy
        self._office = office
        self.llm = llm
        self.guardian = copy.copy(office.guardian)
        self.guardian.llm = llm

    def __getattr__(self, name):
        return getattr(self._office, name)

    def agent(self, char_id: str):
        from .agent import CharacterAgent
        return CharacterAgent(self, self._office.character(char_id))
