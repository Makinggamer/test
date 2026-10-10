import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from atena.clips import ClipEditor, parse_srt, shift_srt
from atena.stream.voice import SubtitleRecorder

from .helpers import make_office

SRT = """1
00:00:05,000 --> 00:00:08,000
こんばんは、今日もマリカやるよ！

2
00:00:30,000 --> 00:00:34,000
えっ、今の甲羅どこから飛んできたの！？

3
00:00:35,000 --> 00:00:38,000
みんな笑いすぎだってば！

4
00:01:30,000 --> 00:01:33,000
次のレースいくよー
"""


def _files(events=None):
    d = Path(tempfile.mkdtemp())
    srt = d / "20261010-2000-hikari.srt"
    srt.write_text(SRT, encoding="utf-8")
    if events is not None:
        srt.with_suffix(".events.json").write_text(json.dumps(events), encoding="utf-8")
    return d, srt


class SrtTest(unittest.TestCase):
    def test_parse_and_shift(self):
        cues = parse_srt(SRT)
        self.assertEqual((len(cues), cues[1].start, cues[1].end), (4, 30.0, 34.0))
        s = shift_srt(cues, 29.0, 36.0)
        self.assertIn("00:00:01,000 --> 00:00:05,000\nえっ", s)
        self.assertIn("00:00:06,000 --> 00:00:07,000\nみんな", s)  # 区間の終わりで切る
        self.assertNotIn("こんばんは", s)

    def test_recorder_saves_events(self):
        t = [0.0]
        rec = SubtitleRecorder(lambda: t[0])
        t[0] = 3.0
        rec.add("やあ")
        rec.mark("comment")
        t[0] = 4.5
        rec.mark("superchat")
        p = rec.save(Path(tempfile.mkdtemp()) / "a.srt")
        ev = json.loads(p.with_suffix(".events.json").read_text(encoding="utf-8"))
        self.assertEqual(ev, [{"t": 3.0, "kind": "comment"}, {"t": 4.5, "kind": "superchat"}])


class SuggestTest(unittest.TestCase):
    def test_llm_pick_with_activity_marks(self):
        o, llm = make_office(['{"clips": [{"start": 1, "end": 2, "title": "甲羅どこから!?", "reason": "反応"}]}'])
        _, srt = _files([{"t": 31, "kind": "comment"}] * 6 + [{"t": 33, "kind": "superchat"}])
        picks = ClipEditor(o).suggest(srt, character_id="hikari")
        self.assertEqual(len(picks), 1)
        self.assertEqual((picks[0]["start"], picks[0]["end"]), (28.5, 43.5))   # 前後の余白 + 最短15秒
        self.assertIn("★", llm.calls[0]["messages"][0]["content"])             # 盛り上がりの印
        self.assertEqual(o.conn.execute("SELECT status FROM clip_candidates").fetchone()[0], "suggested")

    def test_heuristic_when_llm_fails(self):
        o, _ = make_office(["not json"])
        _, srt = _files([{"t": 36, "kind": "comment"}] * 4 + [{"t": 37, "kind": "superchat"}])
        picks = ClipEditor(o).suggest(srt)
        self.assertTrue(picks)
        self.assertLessEqual(picks[0]["start"], 35.0)

    def test_unsafe_clip_rejected(self):
        o, _ = make_office(['{"clips": [{"start": 0, "end": 0, "title": "IPは10.0.0.1"}]}'])
        _, srt = _files()
        self.assertEqual(ClipEditor(o).suggest(srt), [])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が無い環境")
class CutTest(unittest.TestCase):
    def test_cut_vertical_with_subtitles(self):
        o, _ = make_office(['{"clips": [{"start": 1, "end": 2, "title": "t"}]}'])
        d, srt = _files()
        video = d / "rec 1.mp4"  # 空白入りのパスでも動くこと
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "50", "-c:v", "libx264", "-preset",
                        "ultrafast", "-c:a", "aac", "-shortest", str(video)], check=True)
        ed = ClipEditor(o)
        cid = ed.suggest(srt)[0]["id"]
        out = ed.cut(cid, video, d / "out", offset=2.0)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration",
                                "-of", "json", str(out)], capture_output=True, text=True, check=True)
        info = json.loads(probe.stdout)
        v = [s for s in info["streams"] if "width" in s][0]
        self.assertEqual((v["width"], v["height"]), (1080, 1920))
        self.assertAlmostEqual(float(info["format"]["duration"]), 15.0, delta=0.6)
        self.assertEqual(o.approvals.pending()[0]["kind"], "publish")


if __name__ == "__main__":
    unittest.main()
