import array
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path

from atena.monitor import Snapshot
from atena.voicework import VoiceWorkError, VoiceWorkStudio, join_wavs, trim_wav, wav_seconds

from .helpers import make_office

SCRIPT = json.dumps({"title": "雨の夜のおやすみ", "summary": "雨音の夜に寄り添う寝かしつけ",
                     "scenes": [{"emotion": "neutral", "lines": ["こんばんは。", "今日もおつかれさま。"], "pause_after": 2},
                                {"emotion": "shy", "lines": ["……となり、いてもいい？"], "pause_after": 3}]},
                    ensure_ascii=False)


def tone(seconds=0.5, rate=8000, amp=3000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(array.array("h", [amp if i % 20 < 10 else -amp for i in range(int(seconds * rate))]).tobytes())
    return buf.getvalue()


class RecTTS:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, character, emotion="neutral"):
        self.calls.append((text, emotion))
        return tone()


class AudioTest(unittest.TestCase):
    def test_join_with_pauses_and_normalize(self):
        out = join_wavs([(tone(0.5), 0.6), (tone(0.5), 2.0)])
        self.assertAlmostEqual(wav_seconds(out), 0.5 + 0.6 + 0.5 + 2.0, places=2)
        with wave.open(io.BytesIO(out)) as w:
            peak = max(abs(s) for s in array.array("h", w.readframes(w.getnframes())))
        self.assertAlmostEqual(peak, int(0.89 * 32767), delta=2)

    def test_mismatched_format_rejected(self):
        with self.assertRaises(VoiceWorkError):
            join_wavs([(tone(rate=8000), 0), (tone(rate=16000), 0)])

    def test_trim(self):
        self.assertAlmostEqual(wav_seconds(trim_wav(tone(5), 2, fade=0.5)), 2.0, places=2)


class StudioTest(unittest.TestCase):
    def test_script_render_and_sale_approval(self):
        o, llm = make_office([SCRIPT])
        o.characters["shizuku"].voice_id = "Shizuku"
        tts = RecTTS()
        st = VoiceWorkStudio(o, tts)
        s = st.write_script("shizuku", "雨の夜の寝かしつけ", minutes=1)
        self.assertEqual([sc["emotion"] for sc in s["scenes"]], ["neutral", "shy"])
        self.assertIn("性的な表現", llm.calls[0]["messages"][-1]["content"])
        lock = Path(tempfile.mkdtemp()) / ".heavy.lock"
        o.cfg.resources.stream_lock_file = str(lock)
        r = st.render(s["id"], Path(tempfile.mkdtemp()))
        self.assertEqual(tts.calls, [("こんばんは。", "neutral"), ("今日もおつかれさま。", "neutral"),
                                     ("……となり、いてもいい？", "shy")])   # 感情ごとの声
        self.assertAlmostEqual(r["seconds"], 0.5 * 3 + 0.6 + 2 + 3, places=1)
        self.assertTrue(r["sample"].exists())
        self.assertFalse(lock.exists())                                    # 合成後にロックを外す
        a = o.approvals.get(r["approval_id"])
        self.assertEqual((a["kind"], a["level"], a["status"]), ("goods", 3, "pending"))  # 販売はオーナー承認
        g = o.conn.execute("SELECT * FROM goods WHERE id=?", (r["goods_id"],)).fetchone()
        self.assertEqual((g["item"], g["est_cost_jpy"], g["status"]), ("ASMR", 0, "proposal"))

    def test_unsafe_line_discards_script(self):
        bad = json.dumps({"title": "t", "scenes": [{"emotion": "joy", "lines": ["IPは10.0.0.1だよ"]}]},
                         ensure_ascii=False)
        o, _ = make_office([bad, bad])
        self.assertIsNone(VoiceWorkStudio(o).write_script("hikari", "おはようボイス"))
        self.assertEqual(VoiceWorkStudio(o).list(), [])

    def test_busy_pc_refuses_render(self):
        hot = Snapshot(cpu_pct=99, ram_pct=97, gpu_pct=99, vram_used_mb=15900, vram_total_mb=16000, gpu_temp_c=95)
        o, _ = make_office([SCRIPT], snapshot=hot)
        o.characters["shizuku"].voice_id = "Shizuku"
        st = VoiceWorkStudio(o, RecTTS())
        s = st.write_script("shizuku", "雨")
        with self.assertRaises(VoiceWorkError):
            st.render(s["id"], Path(tempfile.mkdtemp()))
        self.assertTrue(st.render(s["id"], Path(tempfile.mkdtemp()), force=True)["wav"].exists())


if __name__ == "__main__":
    unittest.main()
