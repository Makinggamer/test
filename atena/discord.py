"""ラウンジの会話を Discord に流す (LG-11)。

キャラごとに Discord の Webhook（名前とアイコンを持つ投稿用アカウント）を作り、
ラウンジの発言をそのキャラの名前・アイコンで投稿する。常駐ボットは不要。
Webhook の URL は秘密情報なので config/discord_webhooks.toml（git 管理外）に置く:

  room_master = "https://discord.com/api/webhooks/..."   # ルームマスター
  mio = "https://discord.com/api/webhooks/..."           # キャラ ID ごと
  manager = "https://discord.com/api/webhooks/..."       # プロジェクトマネージャー（振り返り・運営メモ）
  default = "https://discord.com/api/webhooks/..."       # 専用が無いキャラはこれに名前だけ変えて投稿

投稿するのはガーディアンを通った発言だけ。規制された発言は「［規制により非表示］」として出す（本文は出さない）。
発言内の @everyone などで通知が飛ばないよう、メンションはすべて無効にして送る。
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from pathlib import Path

from . import http

ROOM_MASTER_KEY = "room_master"
MANAGER_KEY = "manager"
_WEBHOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
USER_AGENT = "AtenaProject (https://github.com/Makinggamer/test, 0.3)"
MAX_LEN = 2000  # Discord の1メッセージの上限


def load_webhooks(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = {}
    for key, url in data.items():
        if not isinstance(url, str) or not _WEBHOOK_RE.match(url.strip()):
            raise ValueError(f"{path.name} の {key} は Discord の Webhook URL ではありません")
        out[str(key)] = url.strip()
    return out


class DiscordPoster:
    def __init__(self, webhooks: dict[str, str], *, forum: bool = False, fetch=http.request,
                 sleep=time.sleep, log=print, max_retries: int = 3):
        self.webhooks = webhooks
        self.forum = forum
        self.fetch = fetch
        self.sleep = sleep
        self.log = log
        self.max_retries = max_retries

    def url_for(self, key: str) -> tuple[str | None, bool]:
        """(URL, 専用か)。専用が無ければ default、それも無ければルームマスターの Webhook に名前を変えて投稿。"""
        if key in self.webhooks:
            return self.webhooks[key], True
        return self.webhooks.get("default") or self.webhooks.get(ROOM_MASTER_KEY), False

    def send(self, key: str, name: str, content: str, *, thread_id: str | None = None,
             thread_name: str | None = None) -> dict | None:
        """1 件投稿する。失敗しても例外は出さず None（ラウンジを止めない）。"""
        url, own = self.url_for(key)
        if not url:
            return None
        body: dict = {"content": content[:MAX_LEN], "allowed_mentions": {"parse": []}}
        if not own:
            body["username"] = name[:80]  # 共用 Webhook では名前だけ差し替える
        if thread_name:
            body["thread_name"] = thread_name[:100]  # フォーラムチャンネルでは新しい投稿（スレッド）になる
        params = {"wait": "true"}
        if thread_id:
            params["thread_id"] = thread_id
        for attempt in range(self.max_retries + 1):
            try:
                raw = self.fetch("POST", url, params=params, json_body=body,
                                 headers={"User-Agent": USER_AGENT}, timeout=15)
                return json.loads(raw.decode("utf-8")) if raw else {}
            except http.HTTPError as e:
                if e.status == 429 and attempt < self.max_retries:
                    try:
                        wait = float(json.loads(e.body).get("retry_after", 1))
                    except (ValueError, AttributeError):
                        wait = 1.0
                    self.sleep(min(wait, 30))
                    continue
                self.log(f"[Discord] 投稿に失敗（{name}）: {e}")
                return None
        return None


class LoungeRelay:
    """RoomMaster の発言をリアルタイムに Discord へ流すリスナー。"""

    def __init__(self, poster: DiscordPoster, *, post_blocked: bool = True):
        self.poster = poster
        self.post_blocked = post_blocked
        self.thread_id: str | None = None

    def session_start(self, session_id: str, topic: str, mode: str, names: list[str]) -> None:
        kind = "雑談" if mode == "hobby" else "情報交換"
        header = f"🛋 **ラウンジ**［{kind}］ 話題: {topic}\n参加: {'、'.join(names)}　`{session_id}`"
        self.thread_id = None
        r = self.poster.send(ROOM_MASTER_KEY, "ルームマスター", header,
                             thread_name=f"{topic}" if self.poster.forum else None)
        if self.poster.forum and r:
            self.thread_id = r.get("channel_id")  # フォーラムでは作られたスレッドの ID

    def message(self, speaker_key: str, name: str, text: str, status: str) -> None:
        if status == "blocked":
            if not self.post_blocked:
                return
            text = "［規制により非表示］"
        self.poster.send(speaker_key, name, text, thread_id=self.thread_id)

    def session_end(self, result) -> None:
        lines = [f"— おわり（発言 {sum(1 for w, _ in result.transcript if w != 'ルームマスター')} 件・"
                 f"規制 {len(result.warnings)} 件・ナレッジ {len(result.knowledge_ids)} 件・"
                 f"切り抜き候補 {len(result.highlight_ids)} 件）"]
        self.poster.send(ROOM_MASTER_KEY, "ルームマスター", "\n".join(lines), thread_id=self.thread_id)


    def review(self, result: dict) -> None:
        """マネージャーの振り返り（各キャラへの心がけ）を運営メモとして流す。"""
        lines = ["📋 **運営メモ**（ラウンジの振り返り）"]
        if result.get("summary"):
            lines.append(result["summary"])
        for a in result.get("applied", []):
            talk = {0.1: "・口数を少し増やす", -0.1: "・口数を少し減らす"}.get(round(a["talk"], 1), "")
            lines.append(f"→ {a['name']}さんへ: {a['note'] or '（心がけはそのまま）'}{talk}")
        for s in result.get("proposals", []):
            lines.append(f"🔒 {s['name']}さんの人格の見直し案をオーナー承認待ちに出しました（#{s['approval_id']}）")
        self.poster.send(MANAGER_KEY, "プロジェクトマネージャー", "\n".join(lines), thread_id=self.thread_id)


def make_relay(cfg, log=print) -> LoungeRelay | None:
    d = cfg.discord
    if not d.enabled:
        return None
    hooks = load_webhooks(cfg.path(d.webhooks_file))
    if not hooks:
        log(f"[Discord] {d.webhooks_file} に Webhook がありません。投稿しません")
        return None
    return LoungeRelay(DiscordPoster(hooks, forum=d.forum, log=log), post_blocked=d.post_blocked)


def replay_session(conn, session_id: str, relay: LoungeRelay, names_to_ids: dict[str, str]) -> int:
    """保存済みのセッションを Discord に流し直す（投稿に失敗したときや、過去分を見たいとき）。"""
    s = conn.execute("SELECT * FROM lounge_sessions WHERE session_id=?", (session_id,)).fetchone()
    if s is None:
        raise KeyError(f"セッション {session_id} はありません")
    ids = json.loads(s["participants"])
    id_to_name = {v: k for k, v in names_to_ids.items()}
    relay.session_start(session_id, s["topic"], s["mode"], [id_to_name.get(i, i) for i in ids])
    n = 0
    for m in conn.execute("SELECT speaker, content, status FROM lounge_messages WHERE session_id=? ORDER BY id",
                          (session_id,)):
        key = ROOM_MASTER_KEY if m["status"] == "system" else names_to_ids.get(m["speaker"], m["speaker"])
        relay.message(key, m["speaker"], m["content"], m["status"])
        n += 1
    return n
