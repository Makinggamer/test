"""読み上げ (VOICEVOX) と OBS 用の字幕ファイル出力 (ST-04, ST-05)。

OBS 連携は WebSocket を使わず「テキストファイルを OBS のテキストソースで読む」方式にしている。
設定が簡単で、OBS の設定やパスワードを Atena 側に持たなくて済む。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

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


class VoicevoxTTS:
    def __init__(self, host: str = "http://127.0.0.1:50021", *, fetch=request, player: list[str] | None = None,
                 run=subprocess.run):
        self.host = host.rstrip("/")
        self.fetch = fetch
        self.player = player if player is not None else default_player()
        self.run = run

    def speakers(self) -> list[dict]:
        try:
            return json.loads(self.fetch("GET", f"{self.host}/speakers").decode("utf-8"))
        except HTTPError as e:
            raise TTSError(f"VOICEVOX に接続できません（起動していますか？）: {e}") from e

    def synthesize(self, text: str, speaker: int) -> bytes:
        try:
            query = self.fetch("POST", f"{self.host}/audio_query", params={"text": text, "speaker": speaker})
            return self.fetch("POST", f"{self.host}/synthesis", params={"speaker": speaker},
                              data=query, headers={"Content-Type": "application/json"}, timeout=120)
        except HTTPError as e:
            raise TTSError(f"VOICEVOX での音声合成に失敗しました: {e}") from e

    def speak(self, text: str, speaker: int) -> None:
        wav = self.synthesize(text, speaker)
        if not self.player:
            raise TTSError("音声再生コマンドが見つかりません")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            path = f.name
        try:
            self.run([*self.player, path], check=False)
        finally:
            os.unlink(path)


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
