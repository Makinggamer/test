"""YouTube Live チャット連携 (ST-03) とスーパーチャットの収益取り込み (RV-05)。

接続方式:
  - API キー + 動画 ID: 設定が簡単。配信ごとに動画 ID を指定する
  - OAuth (youtube.readonly): 自分のチャンネルで配信中の枠を自動検出する
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from ..http import HTTPError, request
from . import ChatMessage

API = "https://www.googleapis.com/youtube/v3"
PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube API の割り当ては太平洋時間の0時にリセット


class YouTubeError(RuntimeError):
    pass


class ChatEnded(YouTubeError):
    pass


class QuotaTracker:
    """YouTube API の1日の割り当て消費を DB に記録する。"""

    def __init__(self, conn: sqlite3.Connection, daily: int, reserve: int, api: str = "youtube",
                 now: Callable[[], datetime] | None = None):
        self.conn, self.daily, self.reserve, self.api = conn, daily, reserve, api
        self.now = now or (lambda: datetime.now(PACIFIC))

    def _day(self) -> str:
        return self.now().astimezone(PACIFIC).date().isoformat()

    def used(self) -> int:
        r = self.conn.execute("SELECT units FROM api_usage WHERE api=? AND day=?", (self.api, self._day())).fetchone()
        return r["units"] if r else 0

    def add(self, units: int) -> None:
        self.conn.execute(
            "INSERT INTO api_usage(api, day, units) VALUES (?,?,?)"
            " ON CONFLICT(api, day) DO UPDATE SET units = units + excluded.units", (self.api, self._day(), units))
        self.conn.commit()

    def available(self) -> int:
        """配信に使ってよい残り（予備分を除く）。"""
        return self.daily - self.reserve - self.used()

    def poll_interval(self, poll_cost: int, stream_hours: float) -> float:
        """配信時間いっぱい割り当てが持つポーリング間隔（秒）。"""
        polls = self.available() // max(1, poll_cost)
        if polls <= 0:
            return float("inf")
        return stream_hours * 3600 / polls


def to_jpy(amount_micros: int | str, currency: str, rates: dict) -> int | None:
    rate = rates.get(currency.upper())
    if rate is None:
        return None
    return round(int(amount_micros) / 1_000_000 * rate)


class YouTubeClient:
    def __init__(self, *, api_key: str = "", token_provider: Callable[[], str] | None = None,
                 fetch=request, quota: QuotaTracker | None = None):
        if not api_key and not token_provider:
            raise YouTubeError("YouTube の API キーか OAuth のどちらかを設定してください")
        self.api_key = api_key
        self.token_provider = token_provider
        self.fetch = fetch
        self.quota = quota

    def _get(self, path: str, params: dict, cost: int) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        headers = {}
        if self.token_provider:
            headers["Authorization"] = f"Bearer {self.token_provider()}"
        else:
            params["key"] = self.api_key
        if self.quota:
            self.quota.add(cost)  # 失敗したリクエストも割り当てを消費する
        try:
            return json.loads(self.fetch("GET", f"{API}/{path}", params=params, headers=headers).decode("utf-8"))
        except HTTPError as e:
            reason = _error_reason(e.body)
            if reason in ("liveChatEnded", "liveChatNotFound", "liveChatDisabled"):
                raise ChatEnded(reason) from e
            raise YouTubeError(f"YouTube API エラー ({e.status} {reason or e})") from e

    def live_chat_id_for_video(self, video_id: str) -> str:
        data = self._get("videos", {"part": "liveStreamingDetails", "id": video_id}, cost=1)
        items = data.get("items") or []
        chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId") if items else None
        if not chat_id:
            raise YouTubeError(f"動画 {video_id} は配信中ではないか、チャットが無効です")
        return chat_id

    def my_active_live_chat_id(self) -> str:
        if not self.token_provider:
            raise YouTubeError("自分の配信の自動検出には OAuth が必要です（atena youtube auth）")
        data = self._get("liveBroadcasts", {"part": "snippet", "broadcastStatus": "active",
                                            "broadcastType": "all"}, cost=1)
        for item in data.get("items") or []:
            chat_id = item.get("snippet", {}).get("liveChatId")
            if chat_id:
                return chat_id
        raise YouTubeError("配信中の枠が見つかりません。YouTube Studio で配信を開始してください")

    def list_messages(self, chat_id: str, page_token: str | None, cost: int) -> dict:
        return self._get("liveChat/messages", {"liveChatId": chat_id, "part": "snippet,authorDetails",
                                               "pageToken": page_token, "maxResults": 2000}, cost=cost)


def _error_reason(body: str) -> str:
    try:
        return json.loads(body)["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def parse_item(item: dict, rates: dict) -> ChatMessage | None:
    """liveChatMessage リソースを ChatMessage に変換。対象外のイベントは None。"""
    sn = item.get("snippet", {})
    au = item.get("authorDetails", {})
    if au.get("isChatOwner"):
        return None  # 配信者（キャラ自身のチャンネル）の書き込みには反応しない
    kind_raw = sn.get("type")
    base = dict(platform="youtube", author=au.get("displayName", "視聴者"), viewer_id=au.get("channelId"),
                event_id=item.get("id"))
    if kind_raw == "textMessageEvent":
        text = sn.get("textMessageDetails", {}).get("messageText") or sn.get("displayMessage", "")
        return ChatMessage(text=text, **base)
    if kind_raw in ("superChatEvent", "superStickerEvent"):
        d = sn.get("superChatDetails") or sn.get("superStickerDetails") or {}
        return ChatMessage(text=d.get("userComment", "") if kind_raw == "superChatEvent" else "",
                           kind="superchat", amount_jpy=to_jpy(d.get("amountMicros", 0), d.get("currency", ""), rates),
                           amount_display=d.get("amountDisplayString", ""), **base)
    if kind_raw in ("newSponsorEvent", "memberMilestoneChatEvent"):
        text = sn.get("memberMilestoneChatDetails", {}).get("userComment", "")
        return ChatMessage(text=text, kind="membership", **base)
    return None


@dataclass
class YouTubeLiveChat:
    """ChatSource 実装。新着をポーリングし、無いときは None を返す。"""

    client: YouTubeClient
    chat_id: str
    rates: dict
    poll_cost: int = 5
    stream_hours: float = 3.0
    include_backlog: bool = False
    sleep: Callable[[float], None] = time.sleep
    stop: Callable[[], bool] = lambda: False
    log: Callable[[str], None] = print

    def _interval(self, api_ms: int | None) -> float:
        api_s = (api_ms or 5000) / 1000
        if not self.client.quota:
            return api_s
        return max(api_s, self.client.quota.poll_interval(self.poll_cost, self.stream_hours))

    def messages(self) -> Iterator[ChatMessage | None]:
        token = None
        first = True
        while not self.stop():
            if self.client.quota and self.client.quota.available() < self.poll_cost:
                self.log("YouTube API の割り当てが残りわずかなため、チャット取得を停止します")
                return
            try:
                data = self.client.list_messages(self.chat_id, token, self.poll_cost)
            except ChatEnded:
                self.log("ライブチャットが終了しました")
                return
            token = data.get("nextPageToken", token)
            if not (first and not self.include_backlog):  # 接続前の過去ログには返答しない
                for item in data.get("items", []):
                    msg = parse_item(item, self.rates)
                    if msg:
                        yield msg
            first = False
            if data.get("offlineAt"):
                self.log("配信が終了しました")
                return
            yield None
            self.sleep(self._interval(data.get("pollingIntervalMillis")))
