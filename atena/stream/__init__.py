"""配信チャット連携 (ST-01)。Phase 2 で Twitch / YouTube のアダプタを追加する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Protocol


@dataclass
class ChatMessage:
    platform: str
    author: str
    text: str
    kind: str = "text"              # text | superchat | membership
    viewer_id: str | None = None    # YouTube のチャンネル ID など（表示名変更に強い）
    amount_jpy: int | None = None   # スーパーチャット等の円換算額（手数料控除前）
    amount_display: str = ""        # 「¥500」など表示用
    event_id: str | None = None     # 重複取り込み防止用


class ChatSource(Protocol):
    def messages(self) -> Iterator[ChatMessage | None]:
        """新着が無いときは None（ハートビート）を返してよい。"""
        ...


class ConsoleChat:
    """コンソールから「名前: コメント」形式で入力する模擬配信。空行で終了。"""

    def __init__(self, input_fn: Callable[[str], str] = input):
        self.input_fn = input_fn

    def messages(self) -> Iterator[ChatMessage]:
        while True:
            try:
                line = self.input_fn("comment> ").strip()
            except EOFError:
                return
            if not line:
                return
            author, _, text = line.partition(":")
            if not text:
                author, text = "視聴者", line
            yield ChatMessage("console", author.strip(), text.strip())


def run_stream(office, character_id: str, source: ChatSource, speak: Callable[[str], None] = print,
               use_llm_judge: bool | None = None) -> int:
    """挨拶なしの簡易配信ループ。返答した数を返す。"""
    from .session import StreamSession
    return StreamSession(office, character_id, source, speak=speak, use_llm_judge=use_llm_judge,
                         greet=False).run().replies
