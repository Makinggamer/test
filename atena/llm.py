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

    def unload(self, model: str) -> None:
        """モデルをメモリから降ろす（keep_alive=0）。入っていなくても失敗にしない。"""
        try:
            self._post("/api/generate", {"model": model, "keep_alive": 0})
        except LLMError:
            pass

    def show(self, model: str) -> dict:
        return self._post("/api/show", {"model": model})

    def tags(self) -> list[str]:
        """入っているモデル名の一覧。"""
        req = urllib.request.Request(self.host + "/api/tags", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout, 5)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise LLMError(f"Ollama に接続できません ({self.host}): {e}") from e
        return [m.get("name", "") for m in data.get("models", [])]


class RoutedLLM:
    """ラウンジなど裏方の処理を別 PC の Ollama で動かす (IN-07)。

    - 別 PC に無いモデル（アプリで作ったキャラ専用モデル、判定用の 3B など）は fallback_model に置き換える。
      キャラの人格は Atena がシステムプロンプトで渡すので、汎用モデルでもキャラとして話せる
    - 別 PC に繋がらなければ、手元（Mac）の Ollama で続ける（local_fallback=False なら失敗として返す）
    """

    def __init__(self, remote, local, *, fallback_model: str, local_fallback: bool = True, log=print,
                 tags_ttl: float = 300.0, clock=None):
        import time as _time
        self.remote, self.local = remote, local
        self.fallback_model = fallback_model
        self.local_fallback = local_fallback
        self.log = log
        self.tags_ttl = tags_ttl
        self.clock = clock or _time.monotonic
        self._tags: set[str] | None = None
        self._tags_at = 0.0

    def _remote_models(self) -> set[str] | None:
        if self._tags is None or self.clock() - self._tags_at > self.tags_ttl:
            try:
                self._tags = set(self.remote.tags())
                self._tags_at = self.clock()
            except LLMError:
                return None
        return self._tags

    @staticmethod
    def _has(models: set[str], model: str) -> bool:
        return model in models or (":" not in model and f"{model}:latest" in models)

    def chat(self, model: str, messages: list[dict], *, json_mode: bool = False,
             options: dict | None = None) -> str:
        models = self._remote_models()
        if models is not None:
            use = model if self._has(models, model) else self.fallback_model
            try:
                return self.remote.chat(use, messages, json_mode=json_mode, options=options)
            except LLMError as e:
                self._tags = None  # 次回は一覧から取り直す
                if not self.local_fallback:
                    raise
                self.log(f"[Ollama] 別 PC で失敗したので手元で続けます: {e}")
        elif not self.local_fallback:
            raise LLMError("別 PC の Ollama に接続できません")
        return self.local.chat(model, messages, json_mode=json_mode, options=options)


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
