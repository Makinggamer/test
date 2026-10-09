"""読み上げ (Irodori-TTS) と OBS 用の字幕ファイル出力 (ST-04, ST-05, ST-07)。

- 生配信では SpeechQueue で読み上げを裏スレッドに回し、合成待ちの間もコメント処理を止めない
- 声は Irodori のみ。途中で別の声に切り替えると不自然なため、失敗時は予備の声を使わず
  「ボイストラブル中」表示 + 字幕のみに切り替え、一定時間後に再挑戦する
- OBS 連携は「テキストファイルを OBS のテキストソースで読む」方式。字幕は音声の再生開始に合わせて出す
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..http import HTTPError, request


class TTSError(RuntimeError):
    pass


def default_player() -> list[str] | None:
    if platform.system() == "Darwin":
        return ["afplay"]
    for cmd in (["paplay"], ["aplay", "-q"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


class AudioPlayer:
    def __init__(self, player: list[str] | None = None, run=subprocess.run):
        self.player = player if player is not None else default_player()
        self.run = run

    def play(self, wav: bytes) -> None:
        if not self.player:
            raise TTSError("音声再生コマンドが見つかりません")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            path = f.name
        try:
            self.run([*self.player, path], check=False)
        finally:
            os.unlink(path)


class IrodoriTTS:
    """Irodori-TTS-Server（OpenAI 互換 /v1/audio/speech）を使う。

    キャラ定義の voice_id = サーバーの voices/ に置いた参照音声の ID、
    voice_caption = 話し方の説明（キャプション対応モデルのみ有効）。
    """

    name = "irodori"

    def __init__(self, host: str = "http://127.0.0.1:8088", *, model: str = "irodori-tts", num_steps: int | None = None,
                 seed: int | None = None, timeout: float = 120, fetch=request):
        self.host = host.rstrip("/")
        self.model = model
        self.num_steps = num_steps
        self.seed = seed
        self.timeout = timeout
        self.fetch = fetch

    def ready(self, character) -> bool:
        return bool(character.voice_id)

    def voices(self) -> list:
        try:
            data = json.loads(self.fetch("GET", f"{self.host}/v1/audio/voices").decode("utf-8"))
        except HTTPError as e:
            raise TTSError(f"Irodori-TTS-Server に接続できません（起動していますか？）: {e}") from e
        return data.get("data", data.get("voices", [])) if isinstance(data, dict) else data

    def synthesize(self, text: str, character) -> bytes:
        if not character.voice_id:
            raise TTSError(f"{character.name} に voice_id が設定されていません")
        opts: dict = {}
        if character.voice_caption:
            opts["caption"] = character.voice_caption
        if self.num_steps:
            opts["num_steps"] = self.num_steps
        if self.seed is not None:
            opts["seed"] = self.seed
        body = {"model": self.model, "input": text, "voice": character.voice_id, "response_format": "wav"}
        if opts:
            body["irodori"] = opts
        try:
            return self.fetch("POST", f"{self.host}/v1/audio/speech", json_body=body, timeout=self.timeout)
        except HTTPError as e:
            raise TTSError(f"Irodori での音声合成に失敗しました: {e}") from e


def build_tts(voice_cfg):
    if voice_cfg.engine != "irodori":
        raise ValueError(f"未対応の読み上げエンジン: {voice_cfg.engine}（irodori のみ対応）")
    return IrodoriTTS(voice_cfg.irodori_host, model=voice_cfg.irodori_model,
                      num_steps=voice_cfg.irodori_num_steps or None, timeout=voice_cfg.irodori_timeout_sec)


@dataclass
class BenchResult:
    seconds: float
    audio_seconds: float

    @property
    def rtf(self) -> float:
        """リアルタイム係数（合成時間 ÷ 音声の長さ）。1 未満なら喋るより速く作れる。"""
        return self.seconds / self.audio_seconds if self.audio_seconds else float("inf")


def wav_seconds(wav: bytes) -> float:
    import io
    import wave
    try:
        with wave.open(io.BytesIO(wav)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        return 0.0


def bench(tts, character, text: str, clock=time.monotonic) -> BenchResult:
    t0 = clock()
    wav = tts.synthesize(text, character)
    return BenchResult(clock() - t0, wav_seconds(wav))


class SpeechQueue:
    """読み上げを裏スレッドで順に処理する。

    - 合成中もメインはコメント処理を続けられる
    - 溜まりすぎたら古い通常返答から捨てる（字幕だけ出す）。スパチャのお礼などの priority は捨てない
    - 字幕は音声の再生開始に合わせて更新する
    - 合成に失敗したら「ボイストラブル中」に入り（on_trouble(True)）、retry_after_sec の間は字幕のみ。
      その後の再挑戦で成功したら復帰（on_trouble(False)）
    """

    def __init__(self, tts, character, *, player: AudioPlayer | None = None,
                 on_start: Callable[[str], None] = lambda t: None,
                 on_trouble: Callable[[bool], None] = lambda b: None, max_pending: int = 3,
                 retry_after_sec: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print):
        self.tts, self.c = tts, character
        self.player = player or AudioPlayer()
        self.on_start = on_start
        self.on_trouble = on_trouble
        self.max_pending = max_pending
        self.retry_after = retry_after_sec
        self.clock = clock
        self.log = log
        self._items: list[tuple[str, bool]] = []
        self._cv = threading.Condition()
        self._closed = False
        self.dropped = 0
        self.spoken = 0
        self.subtitle_only = 0
        self.in_trouble = False
        self._failed_at = 0.0
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def say(self, text: str, *, priority: bool = False) -> None:
        with self._cv:
            self._items.append((text, priority))
            while len(self._items) > self.max_pending:
                idx = next((i for i, (_, p) in enumerate(self._items) if not p), None)
                if idx is None:
                    break
                dropped, _ = self._items.pop(idx)
                self.dropped += 1
                self.on_start(dropped)  # 読まない返答も字幕には出す
            self._cv.notify()

    def _set_trouble(self, value: bool) -> None:
        if value != self.in_trouble:
            self.in_trouble = value
            self.on_trouble(value)

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._items and not self._closed:
                    self._cv.wait()
                if not self._items and self._closed:
                    return
                text, _ = self._items.pop(0)
            if self.in_trouble and self.clock() - self._failed_at < self.retry_after:
                self.subtitle_only += 1
                self.on_start(text)
                continue
            try:
                wav = self.tts.synthesize(text, self.c)
            except (TTSError, OSError) as e:
                self._failed_at = self.clock()
                self.log(f"[ボイストラブル] {e}")
                self._set_trouble(True)
                self.subtitle_only += 1
                self.on_start(text)
                continue
            self._set_trouble(False)
            self.on_start(text)
            try:
                self.player.play(wav)
                self.spoken += 1
            except (TTSError, OSError) as e:
                self.log(f"[再生エラー] {e}")

    def close(self, wait: bool = True, timeout: float | None = None) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify()
        if wait:
            self._thread.join(timeout)


class SubtitleRecorder:
    """配信中に出した字幕を記録し、切り抜き用の SRT を書き出す。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.t0 = clock()
        self.items: list[tuple[float, str]] = []

    def add(self, text: str) -> None:
        if text:
            self.items.append((self.clock() - self.t0, text))

    @staticmethod
    def _ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    def to_srt(self) -> str:
        out = []
        for i, (start, text) in enumerate(self.items):
            # 次の字幕まで、ただし1文字0.2秒（最短2秒・最長10秒）を目安に消す
            est = min(10.0, max(2.0, len(text) * 0.2))
            end = start + est
            if i + 1 < len(self.items):
                end = min(end, self.items[i + 1][0])
            out.append(f"{i + 1}\n{self._ts(start)} --> {self._ts(max(end, start + 0.5))}\n{text}\n")
        return "\n".join(out)

    def save(self, path: Path) -> Path | None:
        if not self.items:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_srt(), encoding="utf-8")
        return path


class OverlayWriter:
    """OBS のテキストソースが読むファイルを書き換える（途中状態を読まれないよう置き換えで書く）。"""

    def __init__(self, subtitle_file: Path, comment_file: Path, notice_file: Path | None = None):
        self.subtitle_file, self.comment_file = subtitle_file, comment_file
        self.notice_file = notice_file

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def subtitle(self, text: str) -> None:
        self._write(self.subtitle_file, text)

    def comment(self, text: str) -> None:
        self._write(self.comment_file, text)

    def notice(self, text: str) -> None:
        """「ボイストラブル中」などの注意書き（OBS で画面上部などに表示）。"""
        if self.notice_file:
            self._write(self.notice_file, text)

    def clear(self) -> None:
        self.subtitle("")
        self.comment("")
        self.notice("")
