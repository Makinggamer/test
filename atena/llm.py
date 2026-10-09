"""Ollama クライアント。標準ライブラリのみで HTTP API を呼ぶ。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def chat(self, model: str, messages: list[dict], *, json_mode: bool = False,
             options: dict | None = None) -> str: ...


class OllamaClient:
    def __init__(self, host: str = "http://localhost:11434", timeout: float = 60.0):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise LLMError(f"Ollama への接続に失敗しました ({path}): {e}") from e

    def chat(self, model: str, messages: list[dict], *, json_mode: bool = False,
             options: dict | None = None) -> str:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if json_mode:
            body["format"] = "json"
        if options:
            body["options"] = options
        data = self._post("/api/chat", body)
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as e:
            raise LLMError(f"Ollama の応答形式が想定外です: {data!r}") from e

    def show(self, model: str) -> dict:
        return self._post("/api/show", {"model": model})


def parse_json(text: str) -> Any:
    """LLM 出力から JSON を取り出す。コードフェンスや前置きが付いていても拾う。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"JSON を解析できませんでした: {text[:200]}")


class ScriptedLLM:
    """テスト・オフライン用。登録した応答を順に返す。応答が尽きたら default を返す。"""

    def __init__(self, responses: list[str] | None = None, default: str = "了解です！"):
        self.responses = list(responses or [])
        self.default = default
        self.calls: list[dict] = []

    def chat(self, model: str, messages: list[dict], *, json_mode: bool = False,
             options: dict | None = None) -> str:
        self.calls.append({"model": model, "messages": messages, "json_mode": json_mode})
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return self.default
