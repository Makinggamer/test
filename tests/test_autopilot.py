import json
import random
import unittest
from datetime import datetime

from atena.autopilot import Autopilot, in_window
from atena.discord import DiscordPoster, LoungeRelay
from atena.lounge import RoomMaster
from atena.monitor import Snapshot

from .helpers import make_office
from .test_discord import HOOK, FakeDiscord

HOT = Snapshot(cpu_pct=99, ram_pct=97, gpu_pct=99, vram_used_mb=15900, vram_total_mb=16000, gpu_temp_c=95)


class FakeRM:
    def __init__(self):
        self.runs = []

    def run(self, ids):
        self.runs.append(ids)

        class R:
            session_id, topic = "s1", "t"
        return R()


class FakePM:
    def __init__(self):
        self.days = []

    def daily_cycle(self, day, lounge=None):
        self.days.append((day, lounge))

        class Rep:
            outcomes, learning, alerts = [], {}, []
        return Rep()


class WindowTest(unittest.TestCase):
    def test_windows(self):
        d = lambda h, m=0: datetime(2026, 10, 10, h, m)
        self.assertTrue(in_window(d(10), "10:00", "00:00"))
        self.assertTrue(in_window(d(23, 59), "10:00", "00:00"))
        self.assertFalse(in_window(d(9, 59), "10:00", "00:00"))
        self.assertTrue(in_window(d(1), "22:00", "02:00"))
        self.assertFalse(in_window(d(3), "22:00", "02:00"))
        self.assertTrue(in_window(d(3), "00:00", "00:00"))


class AutopilotTest(unittest.TestCase):
    def _ap(self, snapshot=None):
        o, _ = make_office(snapshot=snapshot)
        rm, pm = FakeRM(), FakePM()
        ap = Autopilot(o, manager=pm, room_master_factory=lambda: rm, rng=random.Random(0), log=lambda *a: None)
        return o, ap, rm, pm

    def test_daily_once_per_day_then_lounges(self):
        o, ap, rm, pm = self._ap()
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 5, 0)).action, "idle")      # 日次の前・活動時間外
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 5, 30)).action, "daily")
        self.assertEqual(pm.days[0][1], False)                                       # ラウンジは別枠
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 6, 0)).action, "idle")
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 10, 0)).action, "lounge")
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 10, 30)).action, "idle")     # 間隔待ち
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 12, 30)).action, "lounge")   # 120分±15% 後
        self.assertEqual(len(rm.runs), 2)
        self.assertTrue(all(len(r) == 2 for r in rm.runs))
        self.assertEqual(ap.tick(datetime(2026, 10, 11, 5, 31)).action, "daily")    # 翌日
        self.assertEqual(len(pm.days), 2)

    def test_state_survives_restart(self):
        o, ap, rm, pm = self._ap()
        ap.tick(datetime(2026, 10, 10, 5, 30))
        ap2 = Autopilot(o, manager=pm, room_master_factory=lambda: rm, log=lambda *a: None)
        self.assertEqual(ap2.tick(datetime(2026, 10, 10, 6, 0)).action, "idle")      # 同じ日は再実行しない

    def test_skip_when_busy_and_retry(self):
        o, ap, rm, pm = self._ap(snapshot=HOT)
        ap._set("daily_day", "2026-10-10")
        r = ap.tick(datetime(2026, 10, 10, 11, 0))
        self.assertEqual(r.action, "skip")
        self.assertEqual(rm.runs, [])
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 11, 10)).action, "idle")    # 再挑戦は20分後
        o.monitor.sampler = lambda: Snapshot(cpu_pct=10, ram_pct=30, gpu_pct=5, vram_used_mb=1000,
                                             vram_total_mb=16000, gpu_temp_c=40)
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 11, 21)).action, "lounge")


class ReviewTest(unittest.TestCase):
    REVIEW = json.dumps({
        "summary": "テンポは良いが、しずくさんが聞き役に回りすぎ",
        "advice": [{"name": "しずく", "note": "相手の話に一言感想を添えてから話す", "talk": 0.1},
                   {"name": "ひかり", "note": "", "talk": -0.1},
                   {"name": "知らない人", "note": "x", "talk": 0.1}],
        "persona_suggestion": [{"name": "ひかり", "suggestion": "もう少し落ち着いた一面も足す"}]},
        ensure_ascii=False)

    def _run(self, responses, listeners=None):
        o, llm = make_office(responses, default="{}")
        for c in o.characters.values():
            c.talkativeness = 0.5
        rm = RoomMaster(o, jitter=0, listeners=listeners or [])
        return o, llm, rm, rm.run(["hikari", "shizuku"], topic="t", turns=2)

    def test_review_applies_tuning_and_requests_persona_approval(self):
        # 2 発言 → ナレッジ抽出 → 切り抜き → 振り返り
        o, llm, rm, res = self._run(["a", "b", "{}", "{}", self.REVIEW])
        self.assertEqual(len(res.review["applied"]), 2)
        t = rm.tuning("shizuku")
        self.assertEqual((t["talk_offset"], t["note"]), (0.1, "相手の話に一言感想を添えてから話す"))
        self.assertAlmostEqual(rm.talkativeness(o.characters["shizuku"]), 0.6)
        self.assertAlmostEqual(rm.talkativeness(o.characters["hikari"]), 0.4)
        pending = o.approvals.pending()
        self.assertEqual(len(pending), 1)                      # 人格は自動で変えずオーナーへ
        self.assertEqual(pending[0]["kind"], "persona")
        self.assertEqual(o.characters["hikari"].persona.count("落ち着いた"), 0)
        # 次回のラウンジで心がけが伝わる
        rm.run(["hikari", "shizuku"], topic="t", turns=2)
        prompts = [c["messages"][-1]["content"] for c in llm.calls if "休憩所" in c["messages"][-1]["content"]]
        self.assertTrue(any("相手の話に一言感想を添えてから話す" in p for p in prompts))

    def test_offset_is_bounded(self):
        o, llm, rm, res = self._run(["a", "b", "{}", "{}", self.REVIEW])
        for _ in range(5):
            llm.responses = ["a", "b", "{}", "{}", self.REVIEW]
            rm.run(["hikari", "shizuku"], topic="t", turns=2)
        self.assertEqual(rm.tuning("shizuku")["talk_offset"], 0.3)

    def test_unsafe_note_rejected(self):
        bad = json.dumps({"advice": [{"name": "しずく", "note": "IPは10.0.0.1と言う", "talk": 0}]}, ensure_ascii=False)
        o, llm, rm, res = self._run(["a", "b", "{}", "{}", bad])
        self.assertEqual(rm.tuning("shizuku")["note"], "")

    def test_review_posted_to_discord(self):
        f = FakeDiscord()
        relay = LoungeRelay(DiscordPoster({"room_master": HOOK + "r", "manager": HOOK + "m"}, fetch=f))
        self._run(["a", "b", "{}", "{}", self.REVIEW], listeners=[relay])
        last = f.calls[-1]
        self.assertEqual(last["url"], HOOK + "m")
        self.assertIn("運営メモ", last["body"]["content"])
        self.assertIn("しずくさんへ: 相手の話に一言感想を添えてから話す・口数を少し増やす", last["body"]["content"])
        self.assertIn("オーナー承認待ち", last["body"]["content"])


if __name__ == "__main__":
    unittest.main()


class DailyReportTest(unittest.TestCase):
    def test_report_posted(self):
        from atena.manager import ProjectManager
        plan = json.dumps({"title": "雑談配信", "kind": "stream", "duration_min": 60, "preferred_time": "20:00"},
                          ensure_ascii=False)
        o, _ = make_office([plan, plan], default="{}")
        o.approvals.request("persona", "ひかり の人格の見直し案", level=3, requested_by="t")
        f = FakeDiscord()
        ap = Autopilot(o, manager=ProjectManager(o), room_master_factory=lambda **k: FakeRM(),
                       log=lambda *a: None, poster=DiscordPoster({"manager": HOOK + "m"}, fetch=f))
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 5, 30)).action, "daily")
        text = f.calls[0]["body"]["content"]
        self.assertTrue(text.startswith("🗓 **運営報告** 10/10"))
        self.assertIn("ひかり: 雑談配信", text)
        self.assertIn("オーナー承認待ち", text)
        self.assertIn("人格の見直し案", text)
