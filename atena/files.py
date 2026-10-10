"""秘密のファイル（Webhook・API キー）を安全に読む。

iCloud Drive のファイルは、端末から実体が追い出されていると open() が返ってこなくなることがある
（実機で 50 分以上固まり、ラウンジが止まった）。別スレッドで読み、時間内に読めなければ
前回読めたときの手元の控え（data/secrets_cache/、所有者だけ読める）を使う。
"""

from __future__ import annotations

import threading
from pathlib import Path


def read_text_safely(path: Path, *, cache: Path | None = None, timeout: float = 10.0,
                     log=print) -> str | None:
    """path を読む。読めたら控えを更新。時間切れ・読み取りエラーなら控えを返す（無ければ None）。
    ファイル自体が無いときは None（控えは使わない。消したのはオーナーの意思なので）。"""
    path = Path(path)
    if not path.exists():
        return None
    box: dict = {}

    def _read() -> None:
        try:
            box["text"] = path.read_text(encoding="utf-8")
        except OSError as e:
            box["error"] = e

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    text = box.get("text")
    if text is not None:
        if cache is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                if not cache.exists() or cache.read_text(encoding="utf-8") != text:
                    cache.write_text(text, encoding="utf-8")
                cache.chmod(0o600)
            except OSError:
                pass
        return text
    why = f"読み取りエラー（{box['error']}）" if "error" in box else f"{timeout:.0f} 秒で読めませんでした（iCloud の取り寄せ待ち？）"
    if cache is not None and cache.exists():
        log(f"[設定] {path.name}: {why}。前回の控えを使います")
        return cache.read_text(encoding="utf-8")
    log(f"[設定] {path.name}: {why}。控えも無いため使えません")
    return None


def cache_path(root: Path, name: str) -> Path:
    return Path(root) / "data" / "secrets_cache" / name
