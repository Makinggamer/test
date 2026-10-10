"""加入（member）と常時運転のテスト。"""
import random
import unittest
from datetime import datetime, timedelta

from atena.api import ApiError, AtenaAPI
from atena.autopilot import Autopilot
from atena.character import Character, load_character
from atena.lounge import RoomMaster

from .helpers import make_office
from .test_autopilot import FakePM, FakeRM


class MemberTest(unittest.TestCase):
    def _office(self, **kw):
        o, llm = make_office(**kw)
        o.cfg.characters_dir.mkdir(parents=True, exist_ok=True)
        return o, llm

    def test_put_member_false_hides_from_lounge_but_keeps_character(self):
        o, _ = self._office()
        api = AtenaAPI(o)
        api.dispatch("PUT", "/api/characters/misaki", {}, {"name": "ミサキ", "persona": "p", "member": False})
        self.assertIn("misaki", o.all_characters)
        self.assertNotIn("misaki", o.characters)
        listed = {c["id"]: c["member"] for c in api.dispatch("GET", "/api/characters", {}, {})["characters"]}
        self.assertFalse(listed["misaki"])
        self.assertEqual(o.character("misaki").name, "ミサキ")  # 個別の会話はできる
        with self.assertRaises(ValueError):
            RoomMaster(o).run(["hikari", "misaki"], topic="t", turns=1)

    def test_join_via_put_and_keep_state_when_omitted(self):
        o, _ = self._office()
        api = AtenaAPI(o)
        api.dispatch("PUT", "/api/characters/misaki", {}, {"name": "ミサキ", "member": False})
        api.dispatch("PUT", "/api/characters/misaki", {}, {"name": "ミサキ", "persona": "更新"})
        self.assertNotIn("misaki", o.characters)  # member を送らなければ今のまま
        api.dispatch("PUT", "/api/characters/misaki", {}, {"name": "ミサキ", "member": True})
        self.assertIn("misaki", o.characters)
        self.assertTrue(load_character(o.cfg.characters_dir / "misaki.toml").member)

    def test_member_must_be_bool(self):
        o, _ = self._office()
        with self.assertRaises(ApiError):
            AtenaAPI(o).dispatch("PUT", "/api/characters/misaki", {}, {"name": "ミサキ", "member": "yes"})

    def test_toml_roundtrip(self):
        c = Character(id="x", name="X", member=False)
        self.assertIn("member = false", c.to_toml())
        self.assertNotIn("member", Character(id="y", name="Y").to_toml())


class ContinuousTest(unittest.TestCase):
    def test_next_lounge_after_break_from_end(self):
        o, _ = make_office(config_toml="[guardian]\nuse_llm_judge = false\n"
                                       "[autopilot]\ncontinuous = true\nbreak_min = 5\n"
                                       "active_start = \"00:00\"\nactive_end = \"00:00\"\n")
        start = datetime(2026, 10, 10, 3, 0)
        end = start + timedelta(minutes=12)
        rm = FakeRM()
        ap = Autopilot(o, manager=FakePM(), room_master_factory=lambda: rm, rng=random.Random(1),
                       log=lambda *a: None, clock=lambda: end)
        ap._set("daily_day", start.date().isoformat())
        self.assertEqual(ap.tick(start).action, "lounge")
        nxt = datetime.fromisoformat(ap._get("lounge_next"))
        self.assertTrue(end + timedelta(minutes=3.5) <= nxt <= end + timedelta(minutes=6.5))
        self.assertEqual(ap.tick(end + timedelta(minutes=1)).action, "idle")
        self.assertEqual(ap.tick(nxt).action, "lounge")

    def test_non_members_not_picked(self):
        o, _ = make_office()
        o.characters["mio"] = Character(id="mio", name="ミオ")
        o.all_characters["misaki"] = Character(id="misaki", name="ミサキ", member=False)
        rm = FakeRM()
        ap = Autopilot(o, manager=FakePM(), room_master_factory=lambda: rm, rng=random.Random(0),
                       log=lambda *a: None)
        now = datetime(2026, 10, 10, 12, 0)
        ap._set("daily_day", now.date().isoformat())
        for i in range(10):
            ap._set("lounge_next", now.isoformat())
            ap.tick(now)
        self.assertTrue(rm.runs)
        self.assertTrue(all("misaki" not in r for r in rm.runs))


class ReviewEveryTest(unittest.TestCase):
    def test_review_every_n_sessions(self):
        o, _ = make_office(config_toml="[guardian]\nuse_llm_judge = false\n[lounge]\nreview_every = 2\n")
        rm = RoomMaster(o)
        rm.review = lambda *a: {"done": True}
        r1 = rm.run(["hikari", "shizuku"], topic="t", turns=2)
        r2 = rm.run(["hikari", "shizuku"], topic="t", turns=2)
        self.assertIsNone(r1.review)
        self.assertEqual(r2.review, {"done": True})


if __name__ == "__main__":
    unittest.main()
