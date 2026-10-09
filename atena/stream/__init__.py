"""配信チャット連携 (ST-01)。Phase 2 で Twitch / YouTube のアダプタを追加する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Protocol


@dataclass
class ChatMessage:
    platform: str
    author: str
    text: str


class ChatSource(Protocol):
    def messages(self) -> Iterator[ChatMessage]: ...


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
    """コメントを受けて返答する配信ループ。返答した数を返す。"""
    agent = office.agent(character_id)
    count = 0
    for msg in source.messages():
        reply = agent.reply_to_comment(msg.text, msg.author, platform=msg.platform, use_llm_judge=use_llm_judge)
        if reply:
            speak(f"{agent.c.name}: {reply}")
            count += 1
    office.memory.remember(character_id, f"配信を行い、{count}件のコメントに返答した", kind="episode",
                           importance=0.5)
    return count
