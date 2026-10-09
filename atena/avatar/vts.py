"""VTube Studio 連携（Live2D モデルの口パク・表情）(AV-03)。

VTube Studio の公開 API（WebSocket, 既定 ws://localhost:8001）にプラグインとして接続する。
初回は VTube Studio 側に許可ダイアログが出るので「許可」を押す。トークンは data/ に保存。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Callable

PLUGIN_NAME = "Atena project"
PLUGIN_DEV = "Atena"


class VTSError(RuntimeError):
    pass


def _default_connect(url: str):
    try:
        from websockets.sync.client import connect
    except ImportError as e:  # pragma: no cover - 環境依存
        raise VTSError("websockets が必要です: pip install 'atena[avatar]'") from e
    return connect(url, open_timeout=5, close_timeout=2)


class VTubeStudioAvatar:
    def __init__(self, url: str = "ws://localhost:8001", *, token_file: Path, mouth_param: str = "MouthOpen",
                 connect: Callable = _default_connect):
        self.url = url
        self.token_file = token_file
        self.mouth_param = mouth_param
        self._connect = connect
        self.ws = None
        self._lock = threading.Lock()

    def _call(self, message_type: str, data: dict | None = None) -> dict:
        req = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0", "requestID": uuid.uuid4().hex[:16],
               "messageType": message_type, "data": data or {}}
        with self._lock:
            if self.ws is None:
                raise VTSError("VTube Studio に未接続です")
            self.ws.send(json.dumps(req))
            resp = json.loads(self.ws.recv())
        if resp.get("messageType") == "APIError":
            raise VTSError(f"VTube Studio エラー: {resp.get('data', {}).get('message', resp)}")
        return resp.get("data", {})

    def connect(self) -> None:
        """接続して認証する。トークンが無ければ取得（VTube Studio 側で許可が必要）。"""
        self.ws = self._connect(self.url)
        token = self.token_file.read_text(encoding="utf-8").strip() if self.token_file.exists() else ""
        if token and self._authenticate(token):
            return
        data = self._call("AuthenticationTokenRequest", {"pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEV})
        token = data.get("authenticationToken", "")
        if not token or not self._authenticate(token):
            raise VTSError("VTube Studio の認証に失敗しました（許可ダイアログで「許可」を押してください）")
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(token, encoding="utf-8")
        os.chmod(self.token_file, 0o600)

    def _authenticate(self, token: str) -> bool:
        data = self._call("AuthenticationRequest", {"pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEV,
                                                    "authenticationToken": token})
        return bool(data.get("authenticated"))

    def hotkeys(self) -> list[dict]:
        return self._call("HotkeysInCurrentModelRequest").get("availableHotkeys", [])

    def set_emotion(self, character, emotion: str) -> None:
        """キャラ定義の vts_hotkeys（感情 → VTube Studio のホットキー名）で表情を切り替える。"""
        hotkey = (character.vts_hotkeys or {}).get(emotion)
        if hotkey:
            self._call("HotkeyTriggerRequest", {"hotkeyID": hotkey})

    def mouth(self, value: float) -> None:
        self._call("InjectParameterDataRequest", {
            "mode": "set", "parameterValues": [{"id": self.mouth_param, "value": max(0.0, min(1.0, value))}]})

    def close(self) -> None:
        with self._lock:
            if self.ws is not None:
                try:
                    self.ws.close()
                finally:
                    self.ws = None
