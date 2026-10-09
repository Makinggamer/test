import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from atena.audit import AuditLog
from atena.character import (Character, from_modelfile, from_ollama_model, from_webui_export, load_character,
                             parse_modelfile, save_character)
from atena.db import connect
from atena.revenue import RevenueLedger, ranking_note_for
from atena.tasks import ApprovalQueue


class RevenueTest(unittest.TestCase):
    def test_ranking_and_growth(self):
        led = RevenueLedger(connect(":memory:"))
        led.add("a", "superchat", 1000, occurred_at="2026-09-10T20:00:00")
        led.add("a", "superchat", 3000, occurred_at="2026-10-02T20:00:00")
        led.add("a", "goods", 1000, occurred_at="2026-10-03T20:00:00")
        led.add("b", "ads", 4000, occurred_at="2026-10-05T20:00:00")
        r = led.monthly_ranking(2026, 10, ["a", "b", "c"])
        self.assertEqual([(e.character_id, e.rank, e.total) for e in r],
                         [("a", 1, 4000), ("b", 1, 4000), ("c", 3, 0)])
        a = r[0]
        self.assertEqual(a.breakdown, {"superchat": 3000, "goods": 1000})
        self.assertAlmostEqual(a.growth_pct, 300.0)
        note = ranking_note_for(r, "c", {"a": "A", "b": "B", "c": "C"})
        self.assertIn("3位", note)
        with self.assertRaises(ValueError):
            led.add("a", "bitcoin", 1)

    def test_december_and_csv(self):
        led = RevenueLedger(connect(":memory:"))
        led.add("a", "ads", 500, occurred_at="2026-12-31T23:00:00")
        self.assertEqual(led.monthly_ranking(2026, 12)[0].total, 500)
        self.assertIn("2026-12-31", led.export_csv())


class AuditTest(unittest.TestCase):
    def test_chain(self):
        conn = connect(":memory:")
        log = AuditLog(conn)
        log.record("owner", "x", {"a": 1})
        log.record("manager", "y", "z")
        self.assertEqual(log.verify(), (True, None))
        conn.execute("UPDATE audit_log SET detail='tampered' WHERE id=1")
        self.assertEqual(log.verify(), (False, 1))


class ApprovalTest(unittest.TestCase):
    def test_levels(self):
        q = ApprovalQueue(connect(":memory:"), auto_approve_levels=[0, 1, 2, 3])
        self.assertEqual(q.request("x", "s", level=1, requested_by="a")[1], "approved")
        aid, st = q.request("goods", "s", level=3, requested_by="a")
        self.assertEqual(st, "pending")  # L3 は必ずオーナー
        self.assertEqual(q.decide(aid, True)["status"], "approved")
        with self.assertRaises(ValueError):
            q.decide(aid, False)


class CharacterTest(unittest.TestCase):
    def test_modelfile(self):
        mf = 'FROM qwen2.5:7b\nPARAMETER temperature 0.8\nSYSTEM """\nあなたは元気な猫耳少女です。\n"""\n'
        self.assertEqual(parse_modelfile(mf), ("qwen2.5:7b", "あなたは元気な猫耳少女です。"))
        self.assertEqual(parse_modelfile("FROM x\nSYSTEM 一行の設定\n")[1], "一行の設定")
        c = from_modelfile("neko", "ねこ", mf)
        self.assertEqual(c.model, "qwen2.5:7b")

    def test_ollama_show(self):
        class Fake:
            def show(self, model):
                return {"modelfile": 'FROM base\nSYSTEM """設定文"""'}
        c = from_ollama_model(Fake(), "neko:latest", "neko", "ねこ")
        self.assertEqual((c.model, c.persona), ("neko:latest", "設定文"))

    def test_webui(self):
        data = [{"id": "Neko Chan!", "name": "ねこちゃん", "base_model_id": "llama3",
                 "params": {"system": "猫です"}, "meta": {"description": "説明"}}]
        c = from_webui_export(data)[0]
        self.assertEqual((c.id, c.name, c.model), ("neko_chan", "ねこちゃん", "llama3"))
        self.assertIn("説明", c.persona)

    def test_roundtrip(self):
        d = Path(tempfile.mkdtemp())
        c = Character(id="x", name="エックス", persona='複数行\n"""引用"""と \\ バックスラッシュ', goals=["g1"],
                      voice_id="Sora", voice_caption="明るく")
        loaded = load_character(save_character(c, d))
        self.assertEqual(loaded, c)

    def test_prompt_has_no_secrets_section(self):
        sp = Character(id="x", name="X").system_prompt(ranking_note="1位 X", memories=["m"], knowledge=["k"])
        self.assertIn("事務所憲章", sp)
        self.assertIn("1位 X", sp)


if __name__ == "__main__":
    unittest.main()
