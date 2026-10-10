"""切り抜きエディター: 配信の字幕から切り抜き区間を提案し、録画から縦型ショートを書き出す (CL-01〜CL-05)。

材料（配信ごとに Atena が自動で保存）:
  data/streams/<日時>-<キャラ>.srt          キャラの発言の字幕（時刻つき）
  data/streams/<日時>-<キャラ>.events.json  コメント・スパチャが来た時刻（盛り上がりの目安）
録画は OBS の録画ファイルを使う。Atena の配信開始より録画開始が早い場合は offset（秒）で合わせる。
書き出しは手元のファイルまで。公開（YouTube ショート等への投稿）はオーナー承認後（公開範囲 = L3）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .db import now_iso
from .llm import LLMError, parse_json

CLIP_EDITOR = "切り抜きエディター"
_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")

PICK_PROMPT = """\
以下は配信のキャラの発言（字幕）です。行頭は [番号 開始秒]。★ はその前後にコメントやスパチャが多かった印です。
ショート動画（{min_len}〜{max_len}秒）にすると面白い場面を最大 {n} 個選んでください。
条件: 前後の流れが無くても分かる、オチや反応がある、視聴者や仲間を貶める場面は選ばない、個人情報や配信環境の話は選ばない。
JSON だけを出力: {{"clips": [{{"start": 開始番号, "end": 終了番号, "title": "ショートのタイトル案（30字以内）", "reason": "面白い理由"}}]}}

字幕:
{lines}"""


@dataclass
class Cue:
    start: float
    end: float
    text: str


def parse_srt(text: str) -> list[Cue]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = _TS.search(line)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                cues.append(Cue(start, end, " ".join(lines[i + 1:]).strip()))
                break
    return cues


def _ts(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def shift_srt(cues: list[Cue], start: float, end: float) -> str:
    """start〜end に入る字幕だけを、0 秒始まりに詰め直した SRT にする。"""
    out, n = [], 0
    for c in cues:
        if c.end <= start or c.start >= end:
            continue
        n += 1
        out.append(f"{n}\n{_ts(max(c.start, start) - start)} --> {_ts(min(c.end, end) - start)}\n{c.text}\n")
    return "\n".join(out)


def activity(events: list[dict], t0: float, t1: float) -> tuple[int, int]:
    """区間内の (コメント数, スパチャ数)。"""
    inside = [e for e in events if t0 <= float(e.get("t", -1)) < t1]
    return sum(1 for e in inside if e.get("kind") != "superchat"), sum(1 for e in inside if e.get("kind") == "superchat")


class ClipEditor:
    def __init__(self, office, *, min_len: float = 15, max_len: float = 60, pad: float = 1.5):
        self.o = office
        self.min_len, self.max_len, self.pad = min_len, max_len, pad

    @staticmethod
    def load(srt_path: Path) -> tuple[list[Cue], list[dict]]:
        cues = parse_srt(Path(srt_path).read_text(encoding="utf-8"))
        ev_path = Path(srt_path).with_suffix(".events.json")
        events = json.loads(ev_path.read_text(encoding="utf-8")) if ev_path.exists() else []
        return cues, events

    def _fit(self, cues: list[Cue], i: int, j: int) -> tuple[float, float] | None:
        start = max(0.0, cues[i].start - self.pad)
        end = cues[j].end + self.pad
        if end - start > self.max_len:
            end = start + self.max_len
        if end - start < self.min_len:
            end = start + self.min_len  # 短すぎる場合は後ろを足す（反応まで入れる）
        return start, end

    def _heuristic(self, cues: list[Cue], events: list[dict], n: int) -> list[dict]:
        """LLM が使えないとき: コメント・スパチャが多い区間を選ぶ。"""
        scored = []
        for i, c in enumerate(cues):
            j = i
            while j + 1 < len(cues) and cues[j + 1].end - c.start <= self.max_len * 0.8:
                j += 1
            com, sc = activity(events, c.start - 5, cues[j].end + 15)
            scored.append((com + sc * 5, i, j))
        picked, used = [], []
        for score, i, j in sorted(scored, reverse=True):
            if score <= 0 or any(not (j < a or i > b) for a, b in used):
                continue
            used.append((i, j))
            picked.append({"start": i, "end": j, "title": cues[i].text[:30], "reason": f"盛り上がり（{score}）"})
            if len(picked) >= n:
                break
        return picked

    def suggest(self, srt_path: Path, *, n: int = 3, character_id: str = "") -> list[dict]:
        cues, events = self.load(srt_path)
        if not cues:
            return []
        lines = []
        for k, c in enumerate(cues):
            com, sc = activity(events, c.start - 5, c.end + 15)
            mark = "★" * min(3, (com + sc * 5) // 3)
            lines.append(f"[{k} {int(c.start)}s]{mark} {c.text}")
        picks = None
        try:
            raw = self.o.llm.chat(self.o.cfg.ollama.staff_model, [{"role": "user", "content": PICK_PROMPT.format(
                min_len=int(self.min_len), max_len=int(self.max_len), n=n, lines="\n".join(lines)[:12000])}],
                json_mode=True)
            data = parse_json(raw)
            picks = data.get("clips") if isinstance(data, dict) else None
        except LLMError:
            picks = None
        if not picks:
            picks = self._heuristic(cues, events, n)
        out = []
        for p in picks[:n]:
            try:
                i, j = int(p["start"]), int(p["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= i <= j < len(cues)):
                continue
            title = str(p.get("title") or cues[i].text)[:30]
            body = " ".join(c.text for c in cues[i:j + 1])
            # 切り抜く発言そのものと、タイトルを検査（配信中に通った発言でも、切り出すと意味が変わることがある）
            if not self.o.guardian.rule_check(body).ok or not self.o.guardian.rule_check(title).ok:
                continue
            start, end = self._fit(cues, i, j)
            cur = self.o.conn.execute(
                "INSERT INTO clip_candidates(srt_path, character_id, start_sec, end_sec, title, reason, status,"
                " created_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(srt_path), character_id, round(start, 2), round(end, 2), title,
                 str(p.get("reason") or "")[:200], "suggested", now_iso()))
            out.append({"id": cur.lastrowid, "start": start, "end": end, "title": title, "text": body})
        self.o.conn.commit()
        self.o.audit.record(CLIP_EDITOR, "clips:suggest", {"srt": str(srt_path), "count": len(out)})
        return out

    def cut(self, clip_id: int, video: Path, out_dir: Path, *, offset: float = 0.0, layout: str = "fit",
            ffmpeg: str = "ffmpeg", run=subprocess.run) -> Path:
        """候補 1 件を、録画から 1080x1920 の縦型動画（字幕焼き込み）で書き出す。"""
        row = self.o.conn.execute("SELECT * FROM clip_candidates WHERE id=?", (clip_id,)).fetchone()
        if row is None:
            raise KeyError(f"切り抜き候補 #{clip_id} はありません")
        if not shutil.which(ffmpeg) and run is subprocess.run:
            raise RuntimeError("ffmpeg が見つかりません（Mac: brew install ffmpeg）")
        cues, _ = self.load(Path(row["srt_path"]))
        start, end = row["start_sec"], row["end_sec"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"clip{clip_id:04d}-{row['character_id'] or 'x'}.mp4"
        if layout == "crop":  # 中央を縦に切り出す（画面の真ん中にキャラがいる配置向け）
            frame = "crop=ih*9/16:ih,scale=1080:1920"
        else:  # 全体を縮小して上下に余白（配置を問わず切れない）
            frame = "scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "clip.srt").write_text(shift_srt(cues, start, end), encoding="utf-8")
            style = "FontSize=16,Outline=2,MarginV=60"
            cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", f"{start + offset:.2f}", "-i", str(Path(video).resolve()), "-t", f"{end - start:.2f}",
                   "-vf", f"{frame},subtitles=clip.srt:force_style='{style}'",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out.resolve())]
            r = run(cmd, cwd=tmp, capture_output=True, text=True)  # 字幕ファイルは相対パスで渡す（パスのエスケープ回避）
            if getattr(r, "returncode", 0) != 0:
                raise RuntimeError(f"ffmpeg が失敗しました: {getattr(r, 'stderr', '')[-500:]}")
        self.o.conn.execute("UPDATE clip_candidates SET status='exported', out_path=? WHERE id=?", (str(out), clip_id))
        aid, _ = self.o.approvals.request("publish", f"切り抜きの公開: {row['title']}（{out.name}）", level=3,
                                          requested_by=CLIP_EDITOR, ref_id=clip_id)
        self.o.conn.execute("UPDATE clip_candidates SET approval_id=? WHERE id=?", (aid, clip_id))
        self.o.conn.commit()
        return out

    def list(self, limit: int = 20):
        return self.o.conn.execute("SELECT * FROM clip_candidates ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
