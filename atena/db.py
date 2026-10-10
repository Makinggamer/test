"""SQLite スキーマと接続。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- episode | fact | viewer_note | digest
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    last_access TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_char ON memories(character_id, created_at);

CREATE TABLE IF NOT EXISTS viewers (
    character_id TEXT NOT NULL,
    viewer_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    visits INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (character_id, viewer_key)
);

CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'stream',
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed|approved|rejected|done|cancelled
    est_vram_mb INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    assignee TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',      -- open|doing|done|cancelled
    due TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref_id INTEGER,
    requested_by TEXT NOT NULL,
    level INTEGER NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revenue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    source TEXT NOT NULL,
    amount_jpy INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lounge_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,                     -- ok|redacted|blocked|system
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lounge_sessions (
    session_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    mode TEXT NOT NULL,                       -- business|hobby
    host_id TEXT,                             -- hobby のとき話題の持ち主
    subject TEXT,                             -- hobby のときの好きなもの・専門
    participants TEXT NOT NULL,               -- JSON 配列（キャラ ID）
    messages INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lounge_traits (
    character_id TEXT NOT NULL,               -- 人格から推定した口数のキャッシュ
    persona_hash TEXT NOT NULL,
    talkativeness REAL NOT NULL,
    PRIMARY KEY (character_id, persona_hash)
);

CREATE TABLE IF NOT EXISTS lounge_tuning (
    character_id TEXT PRIMARY KEY,            -- マネージャーの振り返りによるラウンジでの調整
    talk_offset REAL NOT NULL DEFAULT 0,      -- 口数の補正（±0.3 まで）
    note TEXT NOT NULL DEFAULT '',            -- 次回の心がけ（人格は変えない）
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS autopilot_state (
    key TEXT PRIMARY KEY,                     -- 自動運転の実行記録（最後に何をいつ実行したか）
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,      -- 広報の下書き（公開はオーナー承認後）
    schedule_id INTEGER NOT NULL,
    character_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    x_post TEXT NOT NULL,
    approval_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS highlights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    first_message_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    layer TEXT NOT NULL,                      -- output|input
    categories TEXT NOT NULL,
    action TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    cpu_pct REAL, ram_pct REAL, gpu_pct REAL,
    vram_used_mb REAL, vram_total_mb REAL, gpu_temp_c REAL,
    swap_used_mb REAL, llm_mem_mb REAL, cpu_speed_limit_pct REAL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    api TEXT NOT NULL,
    day TEXT NOT NULL,                        -- 割り当てのリセット基準日（YouTube は太平洋時間）
    units INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (api, day)
);

CREATE TABLE IF NOT EXISTS external_events (
    source TEXT NOT NULL,                     -- 取り込み済み外部イベントの重複防止
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, event_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # API サーバーは単一スレッドで直列に処理するため、作成スレッド以外からの利用を許可する
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# 既存 DB に後から追加した列
_ADDED_COLUMNS = {
    "resource_samples": ["swap_used_mb REAL", "llm_mem_mb REAL", "cpu_speed_limit_pct REAL"],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col.split()[0] not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    conn.commit()
