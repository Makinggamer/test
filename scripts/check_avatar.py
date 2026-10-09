#!/usr/bin/env python3
"""立ち絵フォルダのチェック（Atena を入れていない Mac でも動く単体版）。

macOS 標準の python3（3.9）でも動くよう、標準ライブラリだけで書いています。
使い方:
  python3 check_avatar.py ~/vid2anime/characters/Mio/avatar
  curl -fsSL https://raw.githubusercontent.com/Makinggamer/test/atena-phase1/scripts/check_avatar.py \
    | python3 - ~/vid2anime/characters/Mio/avatar
"""

import struct
import sys
from pathlib import Path

EMOTIONS = ["neutral", "joy", "shy", "sad", "worry", "angry", "surprise"]
JP = {"neutral": "ふつう", "joy": "うれしい", "shy": "照れ", "sad": "悲しい", "worry": "心配",
      "angry": "怒り", "surprise": "驚き"}
REQUIRED = ["", "_open"]            # 基本・口開き
BLINK = ["_blink", "_blink_open"]   # まばたき
OPTIONAL = ["_half", "_blink_half"]  # 口半開き（任意）


def png_info(p):
    head = p.read_bytes()[:26]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", head[16:24])
    color_type = head[25]
    return w, h, color_type in (4, 6)  # 4/6 = アルファ（透過）あり


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    d = Path(argv[1]).expanduser()
    if not d.is_dir():
        print(f"フォルダがありません: {d}")
        return 1
    ok = True
    sizes, no_alpha, bad, same = {}, [], [], []
    print(f"立ち絵フォルダ: {d}\n")
    print("感情: 基本 / 口開き / 目閉じ / 目閉じ+口開き / 半開き(任意)")
    for e in EMOTIONS:
        marks = []
        for suffix in REQUIRED + BLINK + OPTIONAL[:1]:
            p = d / f"{e}{suffix}.png"
            if not p.exists():
                marks.append("—")
                if suffix in REQUIRED:
                    ok = False
                continue
            info = png_info(p)
            if info is None:
                bad.append(p.name)
                marks.append("×")
                ok = False
                continue
            w, h, alpha = info
            sizes.setdefault((w, h), []).append(p.name)
            if not alpha:
                no_alpha.append(p.name)
            if suffix and (d / f"{e}.png").exists() and p.read_bytes() == (d / f"{e}.png").read_bytes():
                same.append(p.name)
            marks.append("○")
        print(f"  {JP[e]}: " + " ".join(marks))

    print()
    if not (d / "neutral.png").exists():
        print("✗ neutral.png がありません（表示できません）")
        ok = False
    if len(sizes) > 1:
        ok = False
        print("✗ 画像サイズが揃っていません（切り替えで位置がずれます）:")
        for (w, h), names in sorted(sizes.items(), key=lambda x: -len(x[1])):
            print(f"   {w}×{h}: {len(names)} 枚 例 {', '.join(names[:3])}")
    elif sizes:
        print(f"○ サイズ: すべて {next(iter(sizes))[0]}×{next(iter(sizes))[1]}")
    if no_alpha:
        print(f"△ 透過なしの画像: {', '.join(no_alpha)}（背景が透けません）")
    if bad:
        print(f"✗ PNG として読めない: {', '.join(bad)}")
    if same:
        print(f"△ 基本の絵とまったく同じ差分: {', '.join(same)}（口や目が変わっていない可能性）")
    missing_blink = [JP[e] for e in EMOTIONS if (d / f"{e}.png").exists() and not (d / f"{e}_blink.png").exists()]
    if missing_blink:
        print(f"△ まばたき差分なし: {'、'.join(missing_blink)}（その感情はまばたきしないだけ）")
    total = len(list(d.glob("*.png")))
    print(f"\n合計 {total} 枚。" + ("配信に使えます。" if ok else "必須の不足・不備があります。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
