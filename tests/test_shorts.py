import io
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from atena.lounge import RoomMaster
from atena.shorts import Line, LoungeShortMaker, envelope, fallback_window, guess_emotion, pick_frame, wrap

from .helpers import make_office

LINES = ["そういえばさ、昨日の夕焼け見た？めっちゃ赤かったじゃん！", "見ました。思わず本を読む手を止めたんです。",
         "でしょでしょ！あれってちりが多いと赤くなるんだって…たぶん！", "たぶん、なんですね。でも、それもらしいです。",
         "えー、ちょっとひどくない？", "ふふ、今度一緒に屋上で見ましょうか。"]


def _wav(seconds=0.5, amp=8000, rate=24000):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        import array, math
        w.writeframes(array.array("h", (int(amp * math.sin(i / 10)) for i in range(int(rate * seconds)))).tobytes())
    return out.getvalue()


class FakeTTS:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, character, emotion="neutral"):
        self.calls.append((character.id, emotion, text))
        return _wav(0.6)


def _office_with_lounge(pick=None):
    resp = [json.dumps({"thought": "", "say": s}, ensure_ascii=False) for s in LINES]
    o, llm = make_office(resp, default='{"thought": "", "say": "うん"}')
    for c in o.characters.values():
        c.voice_id = c.id
    RoomMaster(o, listeners=[], jitter=0).run(["hikari", "shizuku"], topic="夕焼け", turns=6)
    if pick is not None:
        llm.responses.append(pick)
    return o, llm


class PartsTest(unittest.TestCase):
    def test_fallback_window_two_speakers(self):
        ls = [Line(i, s, s, "あ" * 20) for i, s in enumerate(["a", "b", "a", "c", "a", "b", "a", "b"])]
        a, b = fallback_window(ls, 10)
        self.assertLessEqual(len({x.speaker_id for x in ls[a:b + 1]}), 2)
        self.assertGreater(b, a)

    def test_pick_frame_fallbacks(self):
        frames = {"neutral": {"closed": "n", "open": "no", "blink_closed": "nb"}, "joy": {"closed": "j"}}
        self.assertEqual(pick_frame(frames, "joy", 2, False), "j")           # 口開きが無ければ閉じ
        self.assertEqual(pick_frame(frames, "sad", 2, False), "no")          # 無い感情は neutral
        self.assertEqual(pick_frame(frames, "neutral", 1, False), "no")      # 半開きが無ければ開き
        self.assertEqual(pick_frame(frames, "neutral", 0, True), "nb")

    def test_envelope_and_wrap_and_emotion(self):
        env = envelope(_wav(1.0))
        self.assertAlmostEqual(max(env), 1.0)
        self.assertEqual(len(env), 30)
        self.assertTrue(all(len(x) <= 17 for x in wrap("あ" * 40 + "。", 16)))
        self.assertEqual(guess_emotion("えっ、ほんと！？"), "surprise")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が無い")
class MakeTest(unittest.TestCase):
    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow が無い")
        self.out = Path(tempfile.mkdtemp())

    def test_voiced_short_with_llm_pick(self):
        pick = json.dumps({"start": 0, "end": 3, "title": "夕焼けが赤い理由", "emotions": {"0": "joy", "4": "angry"}})
        o, _ = _office_with_lounge(pick)
        tts = FakeTTS()
        r = LoungeShortMaker(o, tts).make(target=20, out_dir=self.out)
        self.assertEqual(r["title"], "夕焼けが赤い理由")
        self.assertEqual(len(tts.calls), 4)
        self.assertEqual(tts.calls[0][1], "joy")
        self.assertTrue(r["approval_id"])                       # 公開はオーナー承認待ち
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                "-of", "csv=p=0", str(r["path"])], capture_output=True, text=True).stdout
        self.assertIn("video,1080,1920", probe)
        self.assertIn("audio", probe)
        meta = json.loads(r["path"].with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(len(meta["lines"]), 4)
        self.assertEqual(len(LoungeShortMaker(o).list()), 1)

    def test_no_voice_preview_skips_approval(self):
        o, _ = _office_with_lounge("not json")
        r = LoungeShortMaker(o).make(target=6, out_dir=self.out, with_voice=False)
        self.assertIsNone(r["approval_id"])
        self.assertTrue(r["path"].exists())
