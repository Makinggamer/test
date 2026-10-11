"""ラウンジの会話から縦型ショート（1080x1920・約 1 分）を作る (SH-01〜SH-07)。

流れ:
  1. ラウンジの記録から、2 人の掛け合いで約 1 分の区間を選ぶ（LLM。だめなら文字数で機械的に）
  2. 台詞ごとに感情を決め、Irodori でそのキャラの声で読み上げる（声なしの試作も可）
  3. 2 人を左右に立たせ、話している方を明るく・少し大きく・弾ませる。口パクは音声の音量、
     まばたきは数秒おき、表情は台詞の感情。下に名前つき字幕、上にタイトル
  4. ffmpeg で mp4 に書き出し、公開はオーナー承認待ち（公開範囲 = L3）に出す
立ち絵は各キャラの avatar_dir（PNGTuber と同じ名前の付け方）。無ければ仮の立ち絵を使う。
画像の合成に Pillow を使う（pip install -e ".[shorts]"）。
"""

from __future__ import annotations

import array
import io
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .avatar import EMOTIONS, EMOTION_JP
from .db import now_iso
from .llm import LLMError, parse_json

SHORTS = "ショート制作"
CHARS_PER_SEC = 7.0          # 声なしのときの読み上げ速度の目安（日本語）
W, H, FPS = 1080, 1920, 30

PICK_PROMPT = """\
以下は AI キャラクターたちの休憩所（ラウンジ）での会話です。行頭は [番号] 話し手。
縦型ショート動画（約 {target} 秒。読み上げは 1 秒に約 7 文字）にすると面白い、連続した区間を 1 つ選んでください。
条件:
- 2 人だけが交互に話している区間（3 人目が話す行を含めない）
- 前後が無くても話が分かる。最後に反応・オチ・笑いがあると良い
- 誰かを貶める場面、個人情報や配信環境の話は選ばない
各行の感情を {emotions} から 1 つ選ぶ。
JSON だけを出力:
{{"start": 開始番号, "end": 終了番号, "title": "ショートのタイトル（25字以内・内容が伝わる）",
 "emotions": {{"番号": "joy"}}}}

会話:
{lines}"""

FONT_CANDIDATES = [
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
]
COLORS = [(236, 96, 140), (70, 140, 230)]   # 左・右のキャラの名札の色


class ShortsError(RuntimeError):
    pass


@dataclass
class Line:
    message_id: int
    speaker_id: str
    name: str
    text: str
    emotion: str = "neutral"
    start: float = 0.0
    end: float = 0.0
    envelope: list[float] = field(default_factory=list)  # フレームごとの口の開き 0〜1


# ---- 区間の選び方 ---------------------------------------------------------------
def _speakers(lines: list[Line]) -> list[str]:
    return list(dict.fromkeys(x.speaker_id for x in lines))


def fallback_window(lines: list[Line], target: float) -> tuple[int, int]:
    """2 人だけで話していて、目安の長さに一番近い連続区間（両端を含む番号）。"""
    budget = target * CHARS_PER_SEC
    best, best_score = (0, min(len(lines), 2) - 1), -1e9
    for i in range(len(lines)):
        chars = 0
        for j in range(i, len(lines)):
            if len(_speakers(lines[i:j + 1])) > 2:
                break
            chars += len(lines[j].text)
            if chars > budget * 1.25:
                break
            if j - i >= 1 and len(_speakers(lines[i:j + 1])) == 2:
                score = -abs(budget - chars) + 15 * (j - i)
                if score > best_score:
                    best, best_score = (i, j), score
    return best


def guess_emotion(text: str) -> str:
    if any(k in text for k in ("！？", "えっ", "うそ", "まさか", "びっくり")):
        return "surprise"
    if any(k in text for k in ("照れ", "恥ずかし", "やめてよ")):
        return "shy"
    if any(k in text for k in ("心配", "大丈夫？", "不安")):
        return "worry"
    if any(k in text for k in ("残念", "悲し", "さみし", "寂し")):
        return "sad"
    if any(k in text for k in ("！", "楽し", "好き", "うれし", "嬉し", "最高", "いいね", "笑")):
        return "joy"
    return "neutral"


# ---- 音声 -----------------------------------------------------------------------
def _pcm(data: bytes) -> tuple[int, int, array.array]:
    with wave.open(io.BytesIO(data)) as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ShortsError("16bit PCM の WAV のみ対応しています")
    return ch, rate, array.array("h", raw)


def envelope(data: bytes, fps: int = FPS) -> list[float]:
    """フレームごとの音量（0〜1、その台詞の最大で正規化）。口パクに使う。"""
    ch, rate, s = _pcm(data)
    step = max(1, rate // fps)
    vals = []
    for k in range(0, len(s) // ch, step):
        seg = s[k * ch:(k + step) * ch:ch]
        vals.append(math.sqrt(sum(x * x for x in seg) / len(seg)) if seg else 0.0)
    top = max(vals, default=0) or 1.0
    return [v / top for v in vals]


def silence(seconds: float, rate: int = 24000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return out.getvalue()


def fake_envelope(text: str, seconds: float, fps: int = FPS) -> list[float]:
    """声なしの試作用: 文字の並びから口パクらしい動きを作る。"""
    n = max(1, int(seconds * fps))
    return [0.0 if text[min(len(text) - 1, i * len(text) // n)] in "、。！？…ー 　" else
            0.45 + 0.55 * abs(math.sin(i * 0.9)) for i in range(n)]


# ---- 立ち絵 ---------------------------------------------------------------------
def load_frames(avatar_dir: Path | None, placeholder_dir: Path) -> dict:
    """{感情: {差分の種類: Path}}。立ち絵が無ければ仮の立ち絵を作って使う。"""
    from .avatar.pngtuber import ALLOWED, parse_frame_name
    d = avatar_dir if avatar_dir and avatar_dir.is_dir() else None
    if d is None:
        if not placeholder_dir.is_dir() or not any(placeholder_dir.glob("*.png")):
            from .avatar.placeholder import generate
            generate(placeholder_dir)
        d = placeholder_dir
    out: dict = {}
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in ALLOWED:
            emo, kind = parse_frame_name(p.stem)
            out.setdefault(emo, {})[kind] = p
    if not out:
        raise ShortsError(f"立ち絵がありません: {d}")
    return out


def pick_frame(frames: dict, emotion: str, mouth: int, blink: bool) -> Path:
    set_ = frames.get(emotion) or frames.get("neutral") or next(iter(frames.values()))
    order = {0: ["closed", "half", "open"], 1: ["half", "open", "closed"], 2: ["open", "half", "closed"]}[mouth]
    kinds = ([f"blink_{k}" for k in order] if blink else []) + order
    for k in kinds:
        if k in set_:
            return set_[k]
    return next(iter(set_.values()))


def find_font(configured: str = "") -> str | None:
    for f in ([configured] if configured else []) + FONT_CANDIDATES:
        if f and Path(f).expanduser().exists():
            return str(Path(f).expanduser())
    return None


def wrap(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= width and ch not in "、。！？…」）":
            lines.append(cur)
            cur = ""
    if cur:
        if lines and len(cur) <= 2:  # 句読点だけが次の行に落ちないように
            lines[-1] += cur
        else:
            lines.append(cur)
    return lines


# ---- 本体 -----------------------------------------------------------------------
class LoungeShortMaker:
    def __init__(self, office, tts=None, *, rng: random.Random | None = None, log=print):
        self.o = office
        self.tts = tts
        self.rng = rng or random.Random()
        self.log = log

    # 1. 区間
    def lines_of(self, session_id: str) -> list[Line]:
        by_name = {c.name: c.id for c in self.o.all_characters.values()}
        rows = self.o.conn.execute(
            "SELECT id, speaker, content FROM lounge_messages WHERE session_id=? AND status IN ('ok','redacted')"
            " ORDER BY id", (session_id,)).fetchall()
        return [Line(r["id"], by_name[r["speaker"]], r["speaker"], r["content"]) for r in rows
                if r["speaker"] in by_name]

    def latest_session(self) -> str:
        used = {r["session_id"] for r in self.o.conn.execute("SELECT session_id FROM lounge_shorts")}
        rows = [r["session_id"] for r in self.o.conn.execute(
            "SELECT session_id FROM lounge_sessions ORDER BY created_at DESC LIMIT 50") if r["session_id"] not in used]

        def has_avatar(cid: str) -> bool:
            d = self.o.character(cid).avatar_dir
            return bool(d) and Path(d).expanduser().is_dir()
        for need_avatar in (True, False):  # 立ち絵がそろっている会話を優先
            for sid in rows:
                lines = self.lines_of(sid)
                if len(lines) >= 4 and len(_speakers(lines)) >= 2 and (
                        not need_avatar or all(has_avatar(c) for c in _speakers(lines))):
                    return sid
        raise ShortsError("ショートにできるラウンジの記録がありません（2 人以上・4 発言以上）")

    def choose(self, lines: list[Line], target: float) -> tuple[list[Line], str]:
        listing = "\n".join(f"[{i}] {x.name}: {x.text}" for i, x in enumerate(lines))
        title, seg = "", None
        try:
            data = parse_json(self.o.llm.chat(self.o.cfg.ollama.staff_model, [{"role": "user", "content": PICK_PROMPT.format(
                target=int(target), emotions=" / ".join(EMOTIONS), lines=listing)}], json_mode=True))
            a, b = int(data["start"]), int(data["end"])
            if 0 <= a < b < len(lines) and len(_speakers(lines[a:b + 1])) == 2:
                seg = lines[a:b + 1]
                title = str(data.get("title") or "").strip()[:30]
                emos = data.get("emotions") or {}
                for i, x in enumerate(seg, start=a):
                    e = str(emos.get(str(i), "")).strip()
                    x.emotion = e if e in EMOTIONS else guess_emotion(x.text)
        except (LLMError, KeyError, TypeError, ValueError, AttributeError):
            seg = None
        if seg is None:
            a, b = fallback_window(lines, target)
            seg = lines[a:b + 1]
            for x in seg:
                x.emotion = guess_emotion(x.text)
        # 長すぎたら後ろを落とす（最低 2 行）
        while len(seg) > 2 and sum(len(x.text) for x in seg) > target * CHARS_PER_SEC * 1.3:
            seg = seg[:-1]
        if title and not self.o.guardian.rule_check(title).ok:
            title = ""
        return seg, title

    # 2. 音声
    def voice(self, seg: list[Line], *, with_voice: bool, gap: float = 0.35, lead: float = 0.5) -> bytes:
        from .voicework import join_wavs
        parts = [(silence(lead), 0.0)]
        t = lead
        for i, x in enumerate(seg):
            if with_voice:
                c = self.o.character(x.speaker_id)
                if not c.voice_id:
                    raise ShortsError(f"{c.name} に voice_id がありません（声なしで試すなら --no-voice）")
                wav = self.tts.synthesize(x.text, c, x.emotion)
                x.envelope = envelope(wav)
            else:
                secs = max(1.2, len(x.text) / CHARS_PER_SEC)
                wav = silence(secs)
                x.envelope = fake_envelope(x.text, secs)
            with wave.open(io.BytesIO(wav)) as w:
                dur = w.getnframes() / w.getframerate()
            pause = 1.0 if i == len(seg) - 1 else gap
            x.start, x.end = t, t + dur
            t += dur + pause
            parts.append((wav, pause))
        if with_voice:  # 1 本目の無音を声と同じ形式にそろえる
            ch, rate, _ = _pcm(parts[1][0])
            out = io.BytesIO()
            with wave.open(out, "wb") as w:
                w.setnchannels(ch)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(b"\x00\x00" * ch * int(rate * lead))
            parts[0] = (out.getvalue(), 0.0)
        return join_wavs(parts, gap=0.0)

    # 3. 映像
    def render(self, seg: list[Line], title: str, topic: str, audio: bytes, out: Path,
               background: Path | None = None) -> Path:
        try:
            from PIL import Image, ImageDraw, ImageEnhance, ImageFont
        except ImportError as e:
            raise ShortsError('画像の合成に Pillow が必要です: pip install -e ".[shorts]"') from e
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ShortsError("ffmpeg が見つかりません（brew install ffmpeg）")
        cfg = self.o.cfg.shorts
        font_path = find_font(cfg.font)
        if not font_path:
            raise ShortsError("日本語フォントが見つかりません。[shorts] font にフォントのパスを書いてください")

        def font(size: int):
            return ImageFont.truetype(font_path, size)

        ids = _speakers(seg)
        left, right = ids[0], ids[1]
        side = {left: 0, right: 1}
        chars = {cid: self.o.character(cid) for cid in ids}
        cache_dir = self.o.cfg.root / "data" / "cache" / "placeholder_avatar"
        frames = {cid: load_frames(Path(chars[cid].avatar_dir).expanduser() if chars[cid].avatar_dir else None,
                                   cache_dir) for cid in ids}

        # 背景（タイトル・注意書きまで描いておく）
        bg = Image.new("RGB", (W, H))
        if background and background.exists():
            b = Image.open(background).convert("RGB")
            scale = max(W / b.width, H / b.height)
            b = b.resize((int(b.width * scale) + 1, int(b.height * scale) + 1))
            bg.paste(b, ((W - b.width) // 2, (H - b.height) // 2))
        else:
            px = ImageDraw.Draw(bg)
            for y in range(H):  # 夕方のラウンジ風のグラデーション
                k = y / H
                px.line([(0, y), (W, y)], fill=(int(255 - 60 * k), int(226 - 90 * k), int(206 - 40 * k)))
        d = ImageDraw.Draw(bg)
        d.rounded_rectangle((60, 110, W - 60, 380), radius=36, fill=(255, 255, 255))
        d.text((100, 140), "Atena project ラウンジより", font=font(38), fill=(150, 120, 120))
        tl = wrap(title or topic, 15)[:2]
        for i, s in enumerate(tl):
            d.text((100, 200 + i * 78), s, font=font(66), fill=(60, 40, 50))
        d.text((W // 2, H - 60), "※AI キャラクター同士の会話です", font=font(30), fill=(90, 70, 70), anchor="ms")

        # 立ち絵の拡大縮小は使うたびに作らず覚えておく
        target_h = int(H * 0.5)
        img_cache: dict = {}

        def avatar(cid: str, path: Path, active: bool, flip: bool):
            key = (path, active, flip)
            if key not in img_cache:
                im = Image.open(path).convert("RGBA")
                s = min(target_h / im.height, W * 0.5 / im.width) * (1.0 if active else 0.9)
                im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
                if flip:
                    im = im.transpose(Image.FLIP_LEFT_RIGHT)
                if not active:
                    rgb = ImageEnhance.Brightness(im.convert("RGB")).enhance(0.62)
                    rgb.putalpha(im.getchannel("A"))
                    im = rgb
                img_cache[key] = im
            return img_cache[key]

        sub_cache: dict = {}

        def subtitle(x: Line):
            if x.message_id not in sub_cache:
                panel = Image.new("RGBA", (W - 80, 330), (0, 0, 0, 0))
                p = ImageDraw.Draw(panel)
                p.rounded_rectangle((0, 40, W - 80, 330), radius=30, fill=(255, 255, 255, 236),
                                    outline=COLORS[side[x.speaker_id]], width=6)
                name_w = int(p.textlength(x.name, font=font(40))) + 60
                p.rounded_rectangle((30, 0, 30 + name_w, 74), radius=24, fill=COLORS[side[x.speaker_id]])
                p.text((60, 12), x.name, font=font(40), fill=(255, 255, 255))
                for i, s in enumerate(wrap(x.text, 16)[:4]):
                    p.text((50, 100 + i * 56), s, font=font(48), fill=(40, 30, 35))
                sub_cache[x.message_id] = panel
            return sub_cache[x.message_id]

        # まばたきの予定（キャラごとに 2.5〜5.5 秒おき、0.12 秒）
        total = seg[-1].end + 1.0
        blinks = {}
        for cid in ids:
            t, ts = self.rng.uniform(0.5, 2.0), []
            while t < total:
                ts.append(t)
                t += self.rng.uniform(2.5, 5.5)
            blinks[cid] = ts
        last_emotion = {cid: "neutral" for cid in ids}

        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "voice.wav"
            wav_path.write_bytes(audio)
            out.parent.mkdir(parents=True, exist_ok=True)
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                   "-r", str(FPS), "-i", "-", "-i", str(wav_path), "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest",
                   "-movflags", "+faststart", str(out)]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                n_frames = int(total * FPS)
                for f in range(n_frames):
                    t = f / FPS
                    cur = next((x for x in seg if x.start <= t < x.end), None)
                    shown = cur or max((x for x in seg if x.start <= t), key=lambda x: x.start, default=seg[0])
                    frame = bg.copy()
                    for cid in sorted(ids, key=lambda c: shown.speaker_id == c):  # 話している方を手前に
                        talking = cur is not None and cur.speaker_id == cid
                        if talking:
                            last_emotion[cid] = cur.emotion
                            k = int((t - cur.start) * FPS)
                            v = cur.envelope[k] if k < len(cur.envelope) else 0.0
                            mouth = 2 if v >= 0.6 else 1 if v >= 0.3 else 0
                        else:
                            mouth = 0
                        blink = any(b <= t < b + 0.12 for b in blinks[cid])
                        active = shown.speaker_id == cid
                        im = avatar(cid, pick_frame(frames[cid], last_emotion[cid], mouth, blink), active,
                                    flip=(side[cid] == 1 and cfg.flip_right))
                        cx = int(W * (0.29 if side[cid] == 0 else 0.71))
                        bounce = int(14 * abs(math.sin(t * 7))) if talking else int(4 * math.sin(t * 1.6 + side[cid]))
                        base_y = int(H * 0.79)
                        frame.paste(im, (cx - im.width // 2, base_y - im.height - bounce), im)
                    panel = subtitle(shown)
                    frame.paste(panel, (40, H - 120 - panel.height), panel)
                    proc.stdin.write(frame.tobytes())
                proc.stdin.close()
                err = proc.stderr.read().decode("utf-8", "replace")
                if proc.wait() != 0:
                    raise ShortsError(f"ffmpeg での書き出しに失敗しました: {err[-400:]}")
            except BrokenPipeError as e:
                err = proc.stderr.read().decode("utf-8", "replace")
                proc.wait()
                raise ShortsError(f"ffmpeg が途中で止まりました: {err[-400:]}") from e
        return out

    def background(self, topic: str) -> Path | None:
        """[shorts] background: 画像のパス / "comfy"（話題に合わせて ComfyUI で生成）/ 空（グラデーション）。"""
        b = self.o.cfg.shorts.background.strip()
        if b == "comfy":
            from .comfy import BackgroundMaker, ComfyError
            try:
                return BackgroundMaker(self.o).make(topic)
            except ComfyError as e:
                self.log(f"[ショート] 背景を作れなかったのでグラデーションにします: {e}")
                return None
        p = self.o.cfg.path(b) if b else None
        return p if p and p.exists() else None

    # まとめ
    def make(self, session_id: str | None = None, *, target: float = 60.0, out_dir: Path | None = None,
             with_voice: bool = True, force: bool = False) -> dict:
        from .monitor import CRITICAL
        if with_voice and self.tts is None:
            raise ShortsError("読み上げ（Irodori）が設定されていません（声なしで試すなら --no-voice）")
        sid = session_id or self.latest_session()
        topic_row = self.o.conn.execute("SELECT topic FROM lounge_sessions WHERE session_id=?", (sid,)).fetchone()
        if topic_row is None:
            raise ShortsError(f"ラウンジの記録 {sid} がありません")
        lines = self.lines_of(sid)
        if len(lines) < 2 or len(_speakers(lines)) < 2:
            raise ShortsError("2 人以上の会話がありません")
        if with_voice:
            h = self.o.monitor.check(record=False)
            if h.status == CRITICAL and not force:
                raise ShortsError("PC が忙しいので合成を始めません: " + " / ".join(h.reasons))
        seg, title = self.choose(lines, target)
        audio = self.voice(seg, with_voice=with_voice)
        out_dir = out_dir or self.o.cfg.path(self.o.cfg.shorts.out_dir)
        out = out_dir / f"short-{datetime.now():%Y%m%d-%H%M%S}-{'-'.join(_speakers(seg))}.mp4"
        self.render(seg, title, topic_row["topic"], audio, out, background=self.background(topic_row["topic"]))
        secs = round(seg[-1].end + 1.0, 1)
        title = title or topic_row["topic"][:30]
        out.with_suffix(".json").write_text(json.dumps({
            "session": sid, "title": title, "seconds": secs, "voice": with_voice,
            "lines": [{"speaker": x.name, "text": x.text, "emotion": x.emotion, "start": round(x.start, 2),
                       "end": round(x.end, 2)} for x in seg]}, ensure_ascii=False, indent=2), encoding="utf-8")
        aid = None
        if with_voice:  # 声なしの試作は公開しないので承認には出さない
            aid, _ = self.o.approvals.request(
                "publish", f"ラウンジのショートの公開: {title}（{out.name}・{secs:.0f} 秒）", level=3,
                requested_by=SHORTS)
        cur = self.o.conn.execute(
            "INSERT INTO lounge_shorts(session_id, first_message_id, last_message_id, title, out_path, seconds,"
            " voice, approval_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, seg[0].message_id, seg[-1].message_id, title, str(out), secs, int(with_voice), aid, now_iso()))
        self.o.conn.commit()
        self.o.audit.record(SHORTS, "short:render", {"id": cur.lastrowid, "session": sid, "path": str(out)})
        return {"id": cur.lastrowid, "path": out, "title": title, "seconds": secs, "approval_id": aid,
                "lines": seg}

    def list(self, limit: int = 20):
        return self.o.conn.execute("SELECT * FROM lounge_shorts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


