import unittest

from atena.db import connect
from atena.guardian import Guardian
from atena.llm import LLMError, ScriptedLLM
from atena.memory import MemoryLimits, MemoryManager


def mm(llm=None, **limits):
    g = Guardian(owner_secrets=["山田太郎"], use_llm_judge=False)
    return MemoryManager(connect(":memory:"), g, llm=llm, model="m", limits=MemoryLimits(**limits))


class MemoryTest(unittest.TestCase):
    def test_remember_and_recall(self):
        m = mm()
        m.remember("a", "マリオカートの配信で視聴者参加型レースをしたら大盛り上がり", importance=0.8)
        m.remember("a", "紅茶の淹れ方について話した", importance=0.3)
        res = m.recall("a", "マリオカート 参加型", k=1)
        self.assertEqual(len(res), 1)
        self.assertIn("マリオカート", res[0])
        self.assertEqual(m.recall("b", "マリオカート"), [])

    def test_scrub_on_save(self):
        m = mm()
        m.remember("a", "視聴者のメールは x@y.com らしい")
        self.assertNotIn("x@y.com", m.recall("a", "視聴者 メール")[0])
        m.remember("a", "山田太郎の話をした")
        self.assertFalse(any("山田" in r for r in m.recall("a", "山田太郎 話")))

    def test_viewer_profile(self):
        m = mm()
        m.observe_viewer("a", "yt", "taro", "初見")
        m.observe_viewer("a", "yt", "taro", "マイクラ好き")
        p = m.viewer_profile("a", "yt", "taro")
        self.assertEqual(p["visits"], 2)
        self.assertIn("マイクラ好き", p["notes"])
        self.assertIsNone(m.viewer_profile("a", "twitch", "taro"))

    def test_maintain_compresses_with_llm(self):
        llm = ScriptedLLM(default="- 要約された記憶")
        m = mm(llm, max_items=10, keep_recent=3, digest_batch=5)
        for i in range(15):
            m.remember("a", f"出来事{i}", importance=0.5)
        r = m.maintain("a", "ひかり")
        s = m.stats("a")
        self.assertLessEqual(s["items"], 10)
        self.assertGreaterEqual(r["digests_created"], 1)
        self.assertIn("digest", s["by_kind"])
        # 直近 keep_recent 件は残る
        self.assertTrue(m.recall("a", "出来事14"))

    def test_maintain_without_llm(self):
        m = mm(ScriptedLLM([LLMError("down")] * 10), max_items=5, keep_recent=1, digest_batch=10)
        for i in range(8):
            m.remember("a", f"記憶{i}")
        m.maintain("a")
        self.assertLessEqual(m.stats("a")["items"], 5)

    def test_viewer_cap(self):
        m = mm(viewer_cap=3)
        for i in range(5):
            m.observe_viewer("a", "yt", f"v{i}")
        self.assertEqual(m.maintain("a")["viewers_pruned"], 2)
        self.assertEqual(m.stats("a")["viewers"], 3)


if __name__ == "__main__":
    unittest.main()
