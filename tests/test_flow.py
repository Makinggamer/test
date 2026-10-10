"""常時運転の「流れる会話」（ルームマスターの開始・締めなし）のテスト。"""
import json
import random
import unittest
from datetime import datetime, timedelta

from atena.autopilot import Autopilot
from atena.discord import DiscordPoster, LoungeRelay
from atena.lounge import ROOM_MASTER, RoomMaster

from .helpers import make_office
from .test_autopilot import FakePM
from .test_discord import HOOK, FakeDiscord


class Recorder:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *a: self.events.append((name, a))


def _lounge_prompts(llm):
    return [c["messages"][-1]["content"] for c in llm.calls if "休憩所（ラウンジ）" in c["messages"][-1]["content"]]


class FlowRunTest(unittest.TestCase):
    def test_no_room_master_open_or_close(self):
        o, llm = make_office(default='{"thought": "", "say": "そういえば最近どう？"}')
        rec = Recorder()
        res = RoomMaster(o, listeners=[rec], jitter=0).run(["hikari", "shizuku"], topic="猫", turns=2, flow=True)
        names = [e[0] for e in rec.events]
        self.assertIn("flow_start", names)
        self.assertIn("flow_end", names)
        self.assertNotIn("session_start", names)
        self.assertNotIn("session_end", names)
        self.assertFalse([w for w, _ in res.transcript if w == ROOM_MASTER])
        self.assertIn("自分から「猫」の話を切り出して", _lounge_prompts(llm)[0])

    def test_continue_from_carried_lines(self):
        o, llm = make_office(default='{"thought": "", "say": "それでね、昨日も見たんだ"}')
        carry = {"topic": "猫", "mode": "business", "lines": [["ひかり", "しずくは猫飼ってる？"]]}
        res = RoomMaster(o, listeners=[], jitter=0).run(["hikari", "shizuku"], turns=1, flow=True, carry=carry)
        self.assertEqual(res.topic, "猫")
        prompt = _lounge_prompts(llm)[0]
        self.assertIn("ひかり: しずくは猫飼ってる？", prompt)   # 直前の会話が見えている
        self.assertIn("さっきまでの会話の続き", prompt)
        self.assertEqual(res.transcript[0][0], "しずく")        # 名前を呼ばれた人から続く
        self.assertNotIn(("ひかり", "しずくは猫飼ってる？"), res.transcript)  # 引き継いだ行は今回の記録に入れない


class FlowRelayTest(unittest.TestCase):
    def test_channel_posts_no_header_or_footer(self):
        f = FakeDiscord()
        relay = LoungeRelay(DiscordPoster({"room_master": HOOK + "r"}, fetch=f))
        relay.flow_start("s", "猫", "business", ["ひかり"], None, False)
        relay.message("hikari", "ひかり", "やあ", "ok")
        relay.flow_end(None)
        self.assertEqual(len(f.calls), 1)

    def test_forum_keeps_thread_while_continuing(self):
        f = FakeDiscord()
        relay = LoungeRelay(DiscordPoster({"room_master": HOOK + "r"}, forum=True, fetch=f))
        relay.flow_start("s", "猫", "business", ["ひかり"], "555", True)
        self.assertEqual(relay.thread_id, "555")
        self.assertEqual(f.calls, [])
        relay.flow_start("s2", "犬", "business", ["ひかり"], "555", False)  # 話題が変われば新しいスレッド
        self.assertEqual(relay.thread_id, "999")
        self.assertEqual(f.calls[0]["body"]["thread_name"], "犬")


class FakeFlowRM:
    def __init__(self):
        self.calls = []

    def run(self, ids, **kw):
        self.calls.append((ids, kw))
        carry = kw.get("carry") or {}

        class R:
            session_id, topic, mode, host, subject, thread_id = "s", carry.get("topic") or f"t{len(self.calls)}", \
                "business", None, "", "777"
            transcript = [("ひかり", f"line{len(self.calls)}")]
        return R()


class FlowAutopilotTest(unittest.TestCase):
    def test_same_topic_for_topic_rounds_then_change(self):
        o, _ = make_office(config_toml="[guardian]\nuse_llm_judge = false\n"
                                       "[autopilot]\ncontinuous = true\nbreak_min = 2\n"
                                       "active_start = \"00:00\"\nactive_end = \"00:00\"\n[lounge]\ntopic_rounds = 2\n")
        now = datetime(2026, 10, 10, 12, 0)
        rm = FakeFlowRM()
        ap = Autopilot(o, manager=FakePM(), room_master_factory=lambda: rm, rng=random.Random(0),
                       log=lambda *a: None, clock=lambda: now)
        ap._set("daily_day", now.date().isoformat())
        for _ in range(3):
            ap._set("lounge_next", now.isoformat())
            ap.tick(now)
        (m1, k1), (m2, k2), (m3, k3) = rm.calls
        self.assertTrue(all(k["flow"] for k in (k1, k2, k3)))
        self.assertEqual(k1["carry"], {})
        self.assertEqual(k2["carry"]["topic"], "t1")            # 2回目は同じ話題・同じ顔ぶれで続き
        self.assertEqual(m2, m1)
        self.assertEqual(k2["carry"]["thread_id"], "777")
        self.assertEqual(k2["carry"]["lines"], [["ひかり", "line1"]])
        self.assertNotIn("topic", k3["carry"])                   # topic_rounds に達したら話題を変える
        self.assertEqual(k3["carry"]["lines"][-1], ["ひかり", "line2"])  # 直前の流れは見せる
        self.assertEqual(json.loads(ap._get("lounge_flow"))["rounds"], 1)


if __name__ == "__main__":
    unittest.main()


class TickOnceTest(unittest.TestCase):
    def test_lock_prevents_overlap_and_is_released(self):
        import os
        import tempfile
        import time
        from pathlib import Path

        from atena.autopilot import TickResult, tick_once

        class AP:
            n = 0

            def tick(self):
                AP.n += 1
                return TickResult("idle")

        lock = Path(tempfile.mkdtemp()) / "autopilot.lock"
        self.assertEqual(tick_once(AP(), lock).action, "idle")
        self.assertFalse(lock.exists())                      # 終われば印は消える
        lock.write_text("123")
        self.assertEqual(tick_once(AP(), lock).action, "busy")  # 実行中の印があれば何もしない
        old = time.time() - 31 * 60
        os.utime(lock, (old, old))
        self.assertEqual(tick_once(AP(), lock).action, "idle")  # 古い印は残骸として消して進める
        self.assertEqual(AP.n, 2)


class UnloadTest(unittest.TestCase):
    def test_models_unloaded_after_lounge(self):
        o, llm = make_office(config_toml="[guardian]\nuse_llm_judge = false\n[autopilot]\ncontinuous = true\n"
                                         "active_start = \"00:00\"\nactive_end = \"00:00\"\n")
        unloaded = []
        llm.unload = unloaded.append
        now = datetime(2026, 10, 10, 12, 0)
        ap = Autopilot(o, manager=FakePM(), room_master_factory=lambda: FakeFlowRM(), rng=random.Random(0),
                       log=lambda *a: None, clock=lambda: now)
        ap._set("daily_day", now.date().isoformat())
        self.assertEqual(ap.tick(now).action, "lounge")
        self.assertIn(o.cfg.ollama.character_model, unloaded)
        self.assertIn(o.cfg.ollama.judge_model, unloaded)
