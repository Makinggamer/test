"""事務所 (Office): 各役職のサービスを束ねる入口。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .audit import AuditLog
from .character import Character, load_characters
from .config import Config
from .db import connect
from .guardian import Guardian
from .knowledge import KnowledgeBase
from .llm import LLM, OllamaClient
from .memory import MemoryLimits, MemoryManager
from .moderator import CommentModerator
from .monitor import ResourceMonitor, take_snapshot
from .revenue import RevenueLedger, ranking_note_for
from .scheduler import Scheduler
from .tasks import ApprovalQueue, TaskBoard


class Office:
    def __init__(self, cfg: Config, *, llm: LLM | None = None, db_path=None,
                 characters: dict[str, Character] | None = None, sampler=take_snapshot):
        self.cfg = cfg
        self.conn = connect(db_path or cfg.db_path)
        self.llm: LLM = llm or OllamaClient(cfg.ollama.host, cfg.ollama.timeout_sec)
        self.audit = AuditLog(self.conn)
        self.characters = characters if characters is not None else load_characters(cfg.characters_dir)
        self.guardian = Guardian.from_config(cfg, llm=self.llm, conn=self.conn, audit=self.audit)
        self.guardian.set_roster({c.id: c.name for c in self.characters.values()})
        self.moderator = CommentModerator(self.guardian)
        m = cfg.memory
        self.memory = MemoryManager(
            self.conn, self.guardian, llm=self.llm, model=cfg.ollama.staff_model, audit=self.audit,
            limits=MemoryLimits(m.max_items, m.max_chars, m.keep_recent, m.digest_batch, m.viewer_cap))
        self.knowledge = KnowledgeBase(self.conn)
        self.ledger = RevenueLedger(self.conn, self.audit)
        self.monitor = ResourceMonitor(cfg.resources, self.conn, sampler=sampler)
        self.scheduler = Scheduler(self.conn, cfg.resources, audit=self.audit, monitor=self.monitor)
        self.tasks = TaskBoard(self.conn, self.audit)
        self.approvals = ApprovalQueue(self.conn, self.audit, cfg.approvals.auto_approve_levels)

    def character(self, char_id: str) -> Character:
        try:
            return self.characters[char_id]
        except KeyError:
            raise KeyError(f"キャラクター '{char_id}' は登録されていません") from None

    def names(self) -> dict[str, str]:
        return {c.id: c.name for c in self.characters.values()}

    def current_ranking(self, today: date | None = None):
        """直近30日のランキング。"""
        today = today or datetime.now().date()
        return self.ledger.ranking(today - timedelta(days=29), today + timedelta(days=1), list(self.characters))

    def ranking_note(self, char_id: str) -> str:
        return ranking_note_for(self.current_ranking(), char_id, self.names())

    def agent(self, char_id: str):
        from .agent import CharacterAgent
        return CharacterAgent(self, self.character(char_id))

    def close(self) -> None:
        self.conn.close()
