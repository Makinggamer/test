"""アバター（見た目）層: 感情タグ・口パク・表示先 (AV-01〜AV-06)。

声と見た目を Atena 側で同期させる:
  - キャラの発言の先頭に付けさせた感情タグで、表情と Irodori の喋り方を切り替える
  - 口パクは合成した WAV の音量から作る（Mac で内部音声を VTube Studio に流す仕組みが不要）
"""

from __future__ import annotations

import array
import io
import re
import wave
from typing import Protocol

# デスクトップアプリ（studio-chat）の7感情に合わせる
EMOTIONS = ("neutral", "joy", "shy", "sad", "worry", "angry", "surprise")
_ALIASES = {
    "neutral": "neutral", "ふつう": "neutral", "普通": "neutral", "通常": "neutral",
    "joy": "joy", "happy": "joy", "うれしい": "joy", "嬉しい": "joy", "楽しい": "joy",
    "shy": "shy", "照れ": "shy", "てれ": "shy",
    "sad": "sad", "悲しい": "sad", "かなしい": "sad",
    "worry": "worry", "心配": "worry", "不安": "worry",
    "angry": "angry", "怒り": "angry", "おこ": "angry",
    "surprise": "surprise", "驚き": "surprise", "びっくり": "surprise",
}
EMOTION_JP = {"neutral": "ふつう", "joy": "うれしい", "shy": "照れ", "sad": "悲しい", "worry": "心配",
              "angry": "怒り", "surprise": "驚き"}
EMOTION_INSTRUCTION = ("発言の先頭に、そのときの感情タグを1つだけ付けてください: "
                       + " ".join(f"[{v}]" for v in EMOTION_JP.values()) + "（例: [うれしい]ありがとう！）")

_TAG = re.compile(r"^\s*[\[［【(（]\s*([^\]］】)）]{1,12}?)\s*[\]］】)）]\s*")


def parse_emotion(text: str) -> tuple[str, str]:
    """先頭の感情タグを取り出す。(emotion, タグを除いた本文)。タグが無ければ neutral。"""
    m = _TAG.match(text or "")
    if m:
        emo = _ALIASES.get(m.group(1).strip().lower())
        if emo:
            return emo, text[m.end():].lstrip()
    return "neutral", text


def mouth_envelope(wav: bytes, frame_ms: int = 50, gain: float = 1.0) -> list[float]:
    """WAV から口の開き具合（0〜1）の列を frame_ms 間隔で作る。16bit PCM のみ対応。"""
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2:
                return []
            ch, rate = w.getnchannels(), w.getframerate()
            pcm = array.array("h", w.readframes(w.getnframes()))
    except (wave.Error, EOFError):
        return []
    step = max(1, int(rate * frame_ms / 1000)) * ch
    rms = []
    for i in range(0, len(pcm), step):
        chunk = pcm[i:i + step]
        if chunk:
            rms.append((sum(x * x for x in chunk) / len(chunk)) ** 0.5)
    peak = max(rms, default=0)
    if peak <= 0:
        return [0.0] * len(rms)
    out, prev = [], 0.0
    for r in rms:
        v = min(1.0, (r / peak) * 1.4 * gain)  # 普通の声量で口がしっかり開くよう少し持ち上げる
        v = 0.0 if v < 0.12 else v              # 息・無音の小さな揺れでパクパクしない
        prev = 0.6 * v + 0.4 * prev             # なめらかに
        out.append(round(prev, 3))
    return out


class Avatar(Protocol):
    def set_emotion(self, character, emotion: str) -> None: ...
    def mouth(self, value: float) -> None: ...
    def close(self) -> None: ...


class NullAvatar:
    def set_emotion(self, character, emotion: str) -> None:
        pass

    def mouth(self, value: float) -> None:
        pass

    def close(self) -> None:
        pass


class MultiAvatar:
    """複数の表示先（例: VTube Studio と PNGTuber）に同時に送る。片方の失敗で他を止めない。"""

    def __init__(self, avatars: list, log=print):
        self.avatars, self.log = avatars, log

    def _each(self, fn: str, *a) -> None:
        for av in self.avatars:
            try:
                getattr(av, fn)(*a)
            except Exception as e:  # noqa: BLE001 - 表示の不調で配信を止めない
                self.log(f"[アバター] {type(av).__name__}.{fn} 失敗: {e}")

    def set_emotion(self, character, emotion: str) -> None:
        self._each("set_emotion", character, emotion)

    def mouth(self, value: float) -> None:
        self._each("mouth", value)

    def close(self) -> None:
        self._each("close")


def build_avatar(cfg, log=print):
    """設定 [avatar] engines から表示先を作る。VTube Studio は接続・認証まで行う。"""
    a = cfg.avatar
    avatars = []
    for name in a.engines:
        if name == "vtube_studio":
            from .vts import VTubeStudioAvatar
            vts = VTubeStudioAvatar(a.vts_url, token_file=cfg.path(a.vts_token_file), mouth_param=a.vts_mouth_param)
            vts.connect()
            avatars.append(vts)
        elif name == "pngtuber":
            from .pngtuber import PngTuberOverlay
            png = PngTuberOverlay(port=a.pngtuber_port)
            log(f"[アバター] PNGTuber: OBS のブラウザソースに http://127.0.0.1:{png.port}/ を指定")
            avatars.append(png)
        else:
            raise ValueError(f"未対応のアバター表示: {name}（vtube_studio / pngtuber）")
    if not avatars:
        return None
    return avatars[0] if len(avatars) == 1 else MultiAvatar(avatars, log)
