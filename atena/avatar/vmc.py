"""VMC プロトコル送信（Inochi2D の Inochi Session など、外部のアバターソフト用）(AV-10)。

VMC プロトコルは OSC（UDP）で表情（ブレンドシェイプ）の値を送る共通の方式。
Inochi Session・VSeeFace など多くのアバターソフトが受信に対応しているので、
Live2D 系の動くモデルに移るときも Atena 側の作りは変えずに済む。

送るもの（名前は [avatar] vmc_names で変更可。受信側でモデルのパラメータに割り当てる）:
  口の開き "A"、まばたき "Blink"、感情ごとの値（joy → "Joy" など、今の感情だけ 1、ほかは 0）
表情は 0.25 秒ほどかけて切り替え、まばたきは数秒おきに自動で入れる。
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time

from . import EMOTIONS

# VRM の標準名に寄せた既定値（VSeeFace 等でそのまま使える。Inochi Session では受信側で割り当てる）
DEFAULT_NAMES = {
    "mouth": "A", "blink": "Blink",
    "neutral": "Neutral", "joy": "Joy", "shy": "Shy", "sad": "Sorrow", "worry": "Worry",
    "angry": "Angry", "surprise": "Surprised",
}


def _osc_str(s: str) -> bytes:
    b = s.encode("utf-8") + b"\x00"
    return b + b"\x00" * (-len(b) % 4)


def osc_message(address: str, *args) -> bytes:
    """OSC メッセージ（文字列・float のみ）を組み立てる。"""
    tags, data = ",", b""
    for a in args:
        if isinstance(a, str):
            tags += "s"
            data += _osc_str(a)
        else:
            tags += "f"
            data += struct.pack(">f", float(a))
    return _osc_str(address) + _osc_str(tags) + data


def osc_bundle(messages: list[bytes]) -> bytes:
    out = _osc_str("#bundle") + struct.pack(">Q", 1)  # timetag 1 = すぐ反映
    for m in messages:
        out += struct.pack(">i", len(m)) + m
    return out


class VmcAvatar:
    def __init__(self, host: str = "127.0.0.1", port: int = 39539, *, names: dict | None = None,
                 fps: int = 30, blink: bool = True, fade_sec: float = 0.25, rng: random.Random | None = None,
                 start: bool = True):
        self.addr = (host, port)
        self.names = {**DEFAULT_NAMES, **(names or {})}
        self.fps = max(1, fps)
        self.blink_enabled = blink
        self.fade_sec = fade_sec
        self.rng = rng or random.Random()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = "neutral"
        self.weights = {e: (1.0 if e == "neutral" else 0.0) for e in EMOTIONS}
        self.mouth_value = 0.0
        self.blink_value = 0.0
        self._next_blink: float | None = None  # 最初の step で決める
        self._blink_until = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    # ---- Avatar インターフェース -------------------------------------
    def set_emotion(self, character, emotion: str) -> None:
        with self._lock:
            self.target = emotion if emotion in self.weights else "neutral"

    def mouth(self, value: float) -> None:
        with self._lock:
            self.mouth_value = max(0.0, min(1.0, float(value)))

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        try:
            self.sock.sendto(self.packet(reset=True), self.addr)
        except OSError:
            pass
        self.sock.close()

    # ---- 送信 ---------------------------------------------------------
    def step(self, now: float, dt: float) -> None:
        """1 フレーム分、表情のフェードとまばたきを進める。"""
        with self._lock:
            k = 1.0 if self.fade_sec <= 0 else min(1.0, dt / self.fade_sec)
            for e in self.weights:
                goal = 1.0 if e == self.target else 0.0
                self.weights[e] += (goal - self.weights[e]) * k
                if abs(self.weights[e] - goal) < 0.01:
                    self.weights[e] = goal
            if self._next_blink is None:
                self._next_blink = now + self.rng.uniform(2.5, 6.0)
            if self.blink_enabled and now >= self._next_blink:
                self._blink_until = now + 0.12
                double = self.rng.random() < 0.2
                self._next_blink = now + (0.26 if double else self.rng.uniform(2.5, 6.0))
            self.blink_value = 1.0 if now < self._blink_until else 0.0

    def packet(self, *, reset: bool = False) -> bytes:
        with self._lock:
            n = self.names
            vals = [(n["mouth"], 0.0 if reset else self.mouth_value), (n["blink"], 0.0 if reset else self.blink_value)]
            vals += [(n[e], 0.0 if reset else w) for e, w in self.weights.items() if n.get(e)]
        msgs = [osc_message("/VMC/Ext/Blend/Val", name, round(v, 4)) for name, v in vals]
        msgs.append(osc_message("/VMC/Ext/Blend/Apply"))
        return osc_bundle(msgs)

    def _loop(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(1 / self.fps):
            now = time.monotonic()
            self.step(now, now - last)
            last = now
            try:
                self.sock.sendto(self.packet(), self.addr)
            except OSError:
                pass  # 受信側が起動していなくても配信は続ける
