"""読み上げ (Irodori-TTS / VOICEVOX) と OBS 用の字幕ファイル出力 (ST-04, ST-05)。

- 読み上げエンジンは共通インターフェース `synthesize(text, character) -> wav` を持つ
- 生配信では SpeechQueue で読み上げを裏スレッドに回し、合成待ちの間もコメント処理を止めない
- OBS 連携は「テキストファイルを OBS のテキストソースで読む」方式。字幕は音声の再生開始に合わせて出す
"""

from __future__ import annotations

import json
import os
import platform
import queue
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


class VoicevoxTTS:
    """キャラ定義の voice_speaker（話者 ID）を使う。"""

    name = "voicevox"

    def __init__(self, host: str = "http://127.0.0.1:50021", *, fetch=request):
        self.host = host.rstrip("/")
        self.fetch = fetch

    def ready(self, character) -> bool:
        return character.voice_speaker is not None

    def speakers(self) -> list[dict]:
        try:
            return json.loads(self.fetch("GET", f"{self.host}/speakers").decode("utf-8"))
        except HTTPError as e:
            raise TTSError(f"VOICEVOX に接続できません（起動していますか？）: {e}") from e

    def synthesize(self, text: str, character) -> bytes:
        if character.voice_speaker is None:
            raise TTSError(f"{character.name} に voice_speaker が設定されていません")
        sp = character.voice_speaker
        try:
            query = self.fetch("POST", f"{self.host}/audio_query", params={"text": text, "speaker": sp})
            return self.fetch("POST", f"{self.host}/synthesis", params={"speaker": sp},
                              data=query, headers={"Content-Type": "application/json"}, timeout=120)
        except HTTPError as e:
            raise TTSError(f"VOICEVOX での音声合成に失敗しました: {e}") from e


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


class FallbackTTS:
    """主エンジンが失敗・未設定なら予備エンジンで読む（生配信で無音にしないため）。"""

    def __init__(self, primary, secondary, log: Callable[[str], None] = print):
        self.primary, self.secondary, self.log = primary, secondary, log
        self.name = f"{primary.name}+{secondary.name}"

    def ready(self, character) -> bool:
        return self.primary.ready(character) or self.secondary.ready(character)

    def synthesize(self, text: str, character) -> bytes:
        if self.primary.ready(character):
            try:
                return self.primary.synthesize(text, character)
            except TTSError as e:
                if not self.secondary.ready(character):
                    raise
                self.log(f"[読み上げ] {self.primary.name} 失敗のため {self.secondary.name} で代替: {e}")
        return self.secondary.synthesize(text, character)


def build_tts(voice_cfg, log: Callable[[str], None] = print):
    engines = {
        "irodori": lambda: IrodoriTTS(voice_cfg.irodori_host, model=voice_cfg.irodori_model,
                                      num_steps=voice_cfg.irodori_num_steps or None,
                                      timeout=voice_cfg.irodori_timeout_sec),
        "voicevox": lambda: VoicevoxTTS(voice_cfg.host),
    }
    if voice_cfg.engine not in engines:
        raise ValueError(f"未対応の読み上げエンジン: {voice_cfg.engine}")
    tts = engines[voice_cfg.engine]()
    fb = voice_cfg.fallback
    if fb and fb != voice_cfg.engine:
        if fb not in engines:
            raise ValueError(f"未対応の予備エンジン: {fb}")
        tts = FallbackTTS(tts, engines[fb](), log)
    return tts


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
    """

    def __init__(self, tts, character, *, player: AudioPlayer | None = None,
                 on_start: Callable[[str], None] = lambda t: None, max_pending: int = 3,
                 log: Callable[[str], None] = print):
        self.tts, self.c = tts, character
        self.player = player or AudioPlayer()
        self.on_start = on_start
        self.max_pending = max_pending
        self.log = log
        self._items: list[tuple[str, bool]] = []
        self._cv = threading.Condition()
        self._closed = False
        self.dropped = 0
        self.spoken = 0
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

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._items and not self._closed:
                    self._cv.wait()
                if not self._items and self._closed:
                    return
                text, _ = self._items.pop(0)
            try:
                wav = self.tts.synthesize(text, self.c)
                self.on_start(text)
                self.player.play(wav)
                self.spoken += 1
            except (TTSError, OSError) as e:  # 読み上げが落ちても配信は止めない
                self.on_start(text)
                self.log(f"[読み上げエラー] {e}")

    def close(self, wait: bool = True, timeout: float | None = None) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify()
        if wait:
            self._thread.join(timeout)


class OverlayWriter:
    """OBS のテキストソースが読むファイルを書き換える（途中状態を読まれないよう置き換えで書く）。"""

    def __init__(self, subtitle_file: Path, comment_file: Path):
        self.subtitle_file, self.comment_file = subtitle_file, comment_file

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

    def clear(self) -> None:
        self.subtitle("")
        self.comment("")
