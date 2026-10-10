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

    def flow_start(self, session_id: str, topic: str, mode: str, names: list[str], thread_id: str | None,
                   continuing: bool) -> None:
        """常時運転: 区切りの案内は出さない。フォーラムでは話題ごとに 1 つの投稿（スレッド）に流し続ける。"""
        if not self.poster.forum:
            self.thread_id = None
            return
        if continuing and thread_id:
            self.thread_id = thread_id
            return
        r = self.poster.send(ROOM_MASTER_KEY, "ルームマスター", f"🛋 {topic}", thread_name=topic)
        self.thread_id = r.get("channel_id") if r else None

    def flow_end(self, result) -> None:
        """常時運転では締めのあいさつを出さない（会話はそのまま次の回に続く）。"""

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
        if result.get("error"):
            lines.append(f"⚠ 今回は振り返りができませんでした（{result['error'][:150]}）。"
                         "`atena doctor` でモデルを確認してください")
        elif not (result.get("summary") or result.get("applied") or result.get("proposals")):
            lines.append("特に指摘なし。このまま続けます。")
        if result.get("summary"):
            lines.append(result["summary"])
        for a in result.get("applied", []):
            talk = {0.1: "・口数を少し増やす", -0.1: "・口数を少し減らす"}.get(round(a["talk"], 1), "")
            lines.append(f"→ {a['name']}さんへ: {a['note'] or '（心がけはそのまま）'}{talk}")
        for s in result.get("proposals", []):
            lines.append(f"🔒 {s['name']}さんの人格の見直し案をオーナー承認待ちに出しました（#{s['approval_id']}）")
        # マネージャー専用の Webhook は別チャンネル（#運営報告）にある想定なので、ラウンジのスレッドには入れない
        own = MANAGER_KEY in self.poster.webhooks
        self.poster.send(MANAGER_KEY, "プロジェクトマネージャー", "\n".join(lines),
                         thread_id=None if own else self.thread_id)


def make_poster(cfg, log=print) -> DiscordPoster | None:
    """ラウンジ以外（運営報告など）の投稿用。[discord] が無効か Webhook が無ければ None。"""
    d = cfg.discord
    if not d.enabled:
        return None
    hooks = load_webhooks(cfg.path(d.webhooks_file))
    return DiscordPoster(hooks, forum=d.forum, log=log) if hooks else None


STATUS_JP = {
    "scheduled": "枠を確保", "pending_owner": "オーナー確認待ち", "duplicate": "ネタ被りで見送り",
    "no_slot": "空き枠なし", "goods_pending": "グッズ企画として起票", "no_plan": "企画なし",
    "needs_fix": "要修正",
}


def daily_report_text(office, rep) -> str:
    """日次サイクルの結果を、Discord で読む運営報告にする。"""
    names = office.names()
    lines = [f"🗓 **運営報告** {rep.day:%m/%d}（PC: {rep.health}）"]
    if rep.outcomes:
        lines.append("**企画**")
        for x in rep.outcomes:
            title = x.plan["title"] if x.plan else "—"
            lines.append(f"・{names.get(x.character_id, x.character_id)}: {title}（{STATUS_JP.get(x.status, x.status)}）")
    if rep.learning:
        learned = [f"{names.get(cid, cid)} +{r.get('added', 0) + r.get('from_comments', 0)}"
                   for cid, r in rep.learning.items()]
        lines.append("**学習**: " + " / ".join(learned))
    ranking = office.current_ranking()[:3]
    if ranking and any(e.total for e in ranking):
        lines.append("**ランキング（30日）**: " + " / ".join(
            f"{e.rank}位 {names.get(e.character_id, e.character_id)} {e.total:,}円" for e in ranking))
    for p in getattr(rep, "promos", []) or []:
        lines.append(f"📣 **告知の下書き**（#{p['approval_id']} 承認待ち。コピーして投稿できます）\n"
                     f"タイトル: {p['title']}\nX: {p['x_post']}")
    pending = office.approvals.pending()
    if pending:
        lines.append(f"**🔒 オーナー承認待ち {len(pending)} 件**（放置しても何も変わりません）")
        for a in pending[:5]:
            lines.append(f"・#{a['id']} {a['summary'][:80]}")
    for a in rep.alerts:
        lines.append(f"⚠ {a}")
    return "\n".join(lines)


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
