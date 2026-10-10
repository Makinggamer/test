"""ボイス・ASMR 作品の制作 (VW-01〜VW-06)。

流れ: キャラが台本を書く（ガーディアンで 1 行ずつ検査）→ Irodori で感情ごとに合成 →
      間（無音）を入れてつなぎ、音量をそろえて 1 本の WAV に → 試聴用の冒頭 30 秒も作る →
      グッズ台帳に「ボイス作品」として登録（販売＝公開なので、オーナー承認待ち）。
合成は Mac の GPU を使う重い処理なので、配信中・他の重い処理中は始めず、合成中は重い処理のロックを置く。
"""

from __future__ import annotations

import array
import io
import json
import os
import wave
from datetime import datetime
from pathlib import Path

from .avatar import EMOTIONS, EMOTION_JP
from .db import now_iso
from .llm import LLMError, parse_json

KINDS = {"asmr": "ASMR（ささやき・寄り添い・寝かしつけなど、静かで癒やされる作品）",
         "voice": "シチュエーションボイス（短いドラマ・応援・おはよう/おやすみボイスなど）"}
MAX_LINE = 60

SCRIPT_PROMPT = """\
あなた自身が演じる{kind}の台本を書いてください。テーマ: {theme}
長さの目安: 読み上げて約 {minutes} 分（全部で {lines} 行前後）。聞き手に「あなた」と語りかける形。
- 1 行は {max_line} 字以内の、声に出して自然なせりふ
- 場面ごとに感情を1つ選ぶ: {emotions}
- pause_after は場面の後の間（秒、0.5〜4）
- 性的な表現、暴力、実在の人物・店名・作品名、個人情報、配信環境の話は入れない
JSON だけを出力:
{{"title": "作品タイトル（30字以内）", "summary": "作品紹介（100字以内）",
 "scenes": [{{"emotion": "neutral", "lines": ["せりふ", "..."], "pause_after": 2.0}}]}}"""


class VoiceWorkError(RuntimeError):
    pass


def _read_wav(data: bytes) -> tuple[tuple, bytes]:
    with wave.open(io.BytesIO(data)) as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate()), w.readframes(w.getnframes())


def join_wavs(parts: list[tuple[bytes, float]], *, gap: float = 0.6, peak: float = 0.89) -> bytes:
    """[(WAV, この後の間(秒))] を 1 本につなぎ、ピークを peak（約 -1dBFS）にそろえる。16bit PCM のみ。"""
    params, frames = None, []
    for data, pause in parts:
        p, pcm = _read_wav(data)
        if params is None:
            params = p
        elif p != params:
            raise VoiceWorkError(f"音声の形式がそろっていません: {p} と {params}")
        ch, width, rate = p
        if width != 2:
            raise VoiceWorkError("16bit PCM の WAV のみ対応しています")
        frames.append(pcm)
        frames.append(b"\x00\x00" * ch * int(rate * max(gap, pause)))
    if params is None:
        raise VoiceWorkError("音声がありません")
    samples = array.array("h", b"".join(frames))
    top = max((abs(s) for s in samples), default=0)
    if top:
        k = peak * 32767 / top
        samples = array.array("h", (max(-32768, min(32767, int(s * k))) for s in samples))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(2)
        w.setframerate(params[2])
        w.writeframes(samples.tobytes())
    return out.getvalue()


def trim_wav(data: bytes, seconds: float, fade: float = 1.5) -> bytes:
    """冒頭 seconds 秒（最後は fade 秒でフェードアウト）。試聴用。"""
    (ch, width, rate), pcm = _read_wav(data)
    samples = array.array("h", pcm[: int(rate * seconds) * ch * width])
    n_fade = min(len(samples), int(rate * fade) * ch)
    for i in range(n_fade):
        idx = len(samples) - n_fade + i
        samples[idx] = int(samples[idx] * (1 - i / n_fade))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return out.getvalue()


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


class VoiceWorkStudio:
    def __init__(self, office, tts=None):
        self.o = office
        self.tts = tts

    # ---- 台本 -----------------------------------------------------------
    def write_script(self, character_id: str, theme: str, *, kind: str = "asmr", minutes: float = 5) -> dict | None:
        if kind not in KINDS:
            raise ValueError(f"kind は {', '.join(KINDS)} のいずれか")
        g = self.o.guardian
        if not g.rule_check(theme).ok:
            raise ValueError("テーマが事務所ルールに触れています")
        agent = self.o.agent(character_id)
        prompt = SCRIPT_PROMPT.format(kind=KINDS[kind], theme=theme[:100], minutes=minutes,
                                      lines=max(6, int(minutes * 10)), max_line=MAX_LINE,
                                      emotions=" / ".join(f"{e}（{EMOTION_JP[e]}）" for e in EMOTIONS))
        u = agent._say(agent.system_prompt(theme), [{"role": "user", "content": prompt}],
                       context="voicework", json_mode=True)
        if not u.text:
            return None
        try:
            data = parse_json(u.text)
        except LLMError:
            return None
        if not isinstance(data, dict):
            return None
        scenes = []
        for s in data.get("scenes") or []:
            if not isinstance(s, dict):
                continue
            emo = s.get("emotion") if s.get("emotion") in EMOTIONS else "neutral"
            lines = []
            for line in s.get("lines") or []:
                text = str(line).strip()[:MAX_LINE]
                if not text:
                    continue
                v = g.check_output(text, speaker=character_id, context="voicework:line")
                if not v.ok:
                    return None  # 1 行でも不可なら台本ごと捨てる（作品の一部だけ抜けると意味が変わる）
                lines.append(v.text)
            try:
                pause = max(0.5, min(4.0, float(s.get("pause_after") or 1.5)))
            except (TypeError, ValueError):
                pause = 1.5
            if lines:
                scenes.append({"emotion": emo, "lines": lines, "pause_after": pause})
        title = str(data.get("title") or theme)[:30]
        summary = str(data.get("summary") or "")[:100]
        if not scenes or not g.rule_check(title).ok or (summary and not g.rule_check(summary).ok):
            return None
        script = {"title": title, "summary": summary, "scenes": scenes}
        cur = self.o.conn.execute(
            "INSERT INTO voice_works(character_id, kind, theme, title, script, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (character_id, kind, theme[:100], title, json.dumps(script, ensure_ascii=False), "script",
             now_iso(), now_iso()))
        self.o.conn.commit()
        self.o.audit.record(character_id, "voicework:script", {"id": cur.lastrowid, "title": title})
        return {"id": cur.lastrowid, **script}

    def get(self, work_id: int):
        row = self.o.conn.execute("SELECT * FROM voice_works WHERE id=?", (work_id,)).fetchone()
        if row is None:
            raise KeyError(f"ボイス作品 #{work_id} はありません")
        return row

    # ---- 合成 -----------------------------------------------------------
    def _lock_path(self) -> Path | None:
        p = self.o.cfg.resources.stream_lock_file
        return Path(p).expanduser() if p else None

    def render(self, work_id: int, out_dir: Path, *, force: bool = False) -> dict:
        from .monitor import CRITICAL
        if self.tts is None:
            raise VoiceWorkError("読み上げ（Irodori）が設定されていません")
        row = self.get(work_id)
        c = self.o.character(row["character_id"])
        if not c.voice_id:
            raise VoiceWorkError(f"{c.name} に voice_id が設定されていません")
        h = self.o.monitor.check(record=False)
        if h.status == CRITICAL and not force:
            raise VoiceWorkError("PC が忙しいので合成を始めません（配信中・重い処理中・高負荷）: " + " / ".join(h.reasons))
        script = json.loads(row["script"])
        lock, own = self._lock_path(), False
        if lock and not lock.exists():  # 合成中は他の重い処理（LoRA 学習など）を待たせる
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(json.dumps({"pid": os.getpid(), "what": f"Atena ボイス作品の合成（{c.name}）",
                                        "started": datetime.now().strftime("%H:%M")}, ensure_ascii=False),
                            encoding="utf-8")
            own = True
        try:
            parts = []
            for scene in script["scenes"]:
                for i, line in enumerate(scene["lines"]):
                    wav = self.tts.synthesize(line, c, scene["emotion"])
                    last = i == len(scene["lines"]) - 1
                    parts.append((wav, scene["pause_after"] if last else 0.6))
            full = join_wavs(parts)
        finally:
            if own:
                try:
                    if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
                        lock.unlink()
                except (OSError, ValueError):
                    pass
        out_dir.mkdir(parents=True, exist_ok=True)
        base = out_dir / f"voice{work_id:04d}-{c.id}"
        wav_path, sample_path = base.with_suffix(".wav"), base.parent / f"{base.name}_sample.wav"
        wav_path.write_bytes(full)
        sample_path.write_bytes(trim_wav(full, 30))
        secs = wav_seconds(full)
        from .goods import GoodsDesk
        goods = GoodsDesk(self.o).register_digital(
            c.id, script["title"], item="ボイス作品" if row["kind"] == "voice" else "ASMR",
            summary=script.get("summary", ""), note=f"{secs / 60:.1f} 分 / {wav_path.name}")
        self.o.conn.execute("UPDATE voice_works SET status='rendered', wav_path=?, seconds=?, goods_id=?, updated_at=?"
                            " WHERE id=?", (str(wav_path), round(secs, 1), goods["goods_id"], now_iso(), work_id))
        self.o.conn.commit()
        return {"wav": wav_path, "sample": sample_path, "seconds": secs, **goods}

    def list(self, limit: int = 20):
        return self.o.conn.execute("SELECT * FROM voice_works ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
