"""仮の立ち絵（PNGTuber 用の感情差分・口開き差分・まばたき差分）を生成する (AV-07)。

本番の立ち絵ができるまでの間に、OBS の配置・口パク・表情切替を確認するためのもの。
画像ライブラリを使わず、標準ライブラリ（zlib）だけで透過 PNG を書き出す。
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

from . import EMOTIONS

SIZE = 320


def write_png(path: Path, width: int, height: int, rgba: bytearray) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgba[y * width * 4:(y + 1) * width * 4]) for y in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


class Canvas:
    def __init__(self, size: int = SIZE):
        self.w = self.h = size
        self.px = bytearray(size * size * 4)

    def dot(self, x: int, y: int, color: tuple) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 4
            self.px[i:i + 4] = bytes(color)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, color: tuple, *, ring: float = 0) -> None:
        for y in range(int(cy - ry) - 1, int(cy + ry) + 2):
            for x in range(int(cx - rx) - 1, int(cx + rx) + 2):
                d = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2
                if d <= 1 and (not ring or d >= (1 - ring) ** 2):
                    self.dot(x, y, color)

    def line(self, x0: float, y0: float, x1: float, y1: float, color: tuple, width: float = 3) -> None:
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for i in range(n + 1):
            t = i / n
            self.ellipse(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, width / 2, width / 2, color)

    def arc(self, cx: float, cy: float, r: float, a0: float, a1: float, color: tuple, width: float = 3) -> None:
        steps = 40
        pts = [(cx + r * math.cos(a0 + (a1 - a0) * i / steps), cy + r * math.sin(a0 + (a1 - a0) * i / steps))
               for i in range(steps + 1)]
        for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
            self.line(xa, ya, xb, yb, color, width)


SKIN = (255, 228, 205, 255)
LINE = (70, 50, 50, 255)
MOUTH = (190, 70, 80, 255)
BLUSH = (255, 150, 160, 200)
TEAR = (120, 180, 255, 230)
HAIR = (90, 110, 160, 255)


def draw(emotion: str, mouth_open: bool, hair: tuple = HAIR, *, blink: bool = False) -> Canvas:
    c = Canvas()
    s = SIZE
    cx, cy = s / 2, s * 0.55
    c.ellipse(cx, cy - s * 0.06, s * 0.36, s * 0.34, hair)              # 髪
    c.ellipse(cx, cy, s * 0.3, s * 0.32, SKIN)                            # 顔
    c.ellipse(cx, cy, s * 0.3, s * 0.32, LINE, ring=0.02)
    ex, ey = s * 0.11, cy - s * 0.04
    # 目
    for side in (-1, 1):
        x = cx + side * ex
        if blink and emotion != "joy":
            c.arc(x, ey - 6, 13, math.pi * 0.2, math.pi * 0.8, LINE, 4)  # 閉じた目
        elif emotion == "joy":
            c.arc(x, ey + 6, 14, math.pi * 1.1, math.pi * 1.9, LINE, 4)  # にっこり目
        elif emotion == "surprise":
            c.ellipse(x, ey, 13, 16, LINE)
            c.ellipse(x, ey, 6, 8, (255, 255, 255, 255))
        elif emotion == "angry":
            c.ellipse(x, ey + 2, 10, 9, LINE)
        else:
            c.ellipse(x, ey, 10, 13, LINE)
        # 眉
        brow_y = ey - 26
        inner, outer = x - side * 14, x + side * 14  # 顔の中心側 / 外側
        if emotion == "angry":
            c.line(inner, brow_y + 7, outer, brow_y - 7, LINE, 4)   # 内側が下がる
        elif emotion in ("sad", "worry"):
            c.line(inner, brow_y - 7, outer, brow_y + 7, LINE, 4)   # 内側が上がる
        elif emotion == "surprise":
            c.arc(x, brow_y + 4, 16, math.pi * 1.15, math.pi * 1.85, LINE, 3)
        else:
            c.line(x - 14, brow_y, x + 14, brow_y, LINE, 3)
    if emotion == "shy":
        for side in (-1, 1):
            c.ellipse(cx + side * s * 0.17, cy + s * 0.06, 18, 9, BLUSH)
    if emotion == "sad":
        c.ellipse(cx + ex + 4, ey + 26, 5, 9, TEAR)
    # 口
    my = cy + s * 0.15
    if mouth_open:
        ry = 16 if emotion == "surprise" else 12
        c.ellipse(cx, my, 16, ry, MOUTH)
        c.ellipse(cx, my, 16, ry, LINE, ring=0.15)
    elif emotion in ("joy", "shy"):
        c.arc(cx, my - 10, 16, math.pi * 0.15, math.pi * 0.85, LINE, 4)
    elif emotion in ("sad", "worry", "angry"):
        c.arc(cx, my + 12, 14, math.pi * 1.2, math.pi * 1.8, LINE, 4)
    else:
        c.line(cx - 12, my, cx + 12, my, LINE, 4)
    return c


def generate(out_dir: Path) -> list[Path]:
    """7 感情 × 口の開閉 × 目の開閉 = 28 枚。"""
    paths = []
    for emo in EMOTIONS:
        for blink in (False, True):
            for opened in (False, True):
                name = emo + ("_blink" if blink else "") + ("_open" if opened else "")
                p = out_dir / f"{name}.png"
                cv = draw(emo, opened, blink=blink)
                write_png(p, cv.w, cv.h, cv.px)
                paths.append(p)
    return paths


def check(avatar_dir: Path) -> dict:
    """立ち絵フォルダの揃い具合を調べる。"""
    have = {p.stem for p in avatar_dir.glob("*.png")} if avatar_dir.is_dir() else set()
    sizes = set()
    for p in avatar_dir.glob("*.png") if avatar_dir.is_dir() else []:
        head = p.read_bytes()[:24]
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            sizes.add(struct.unpack(">II", head[16:24]))
    return {
        "missing_closed": [e for e in EMOTIONS if e not in have],
        "missing_open": [e for e in EMOTIONS if f"{e}_open" not in have],
        "missing_blink": [e for e in EMOTIONS if e in have and f"{e}_blink" not in have],
        "missing_blink_open": [e for e in EMOTIONS if f"{e}_open" in have and f"{e}_blink_open" not in have],
        "half": [e for e in EMOTIONS if f"{e}_half" in have],
        "sizes": sorted(sizes),
        "ok": "neutral" in have,
    }
