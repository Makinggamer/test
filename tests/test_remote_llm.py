import random
import unittest
from datetime import datetime

from atena.autopilot import Autopilot
from atena.llm import LLMError, RoutedLLM, ScriptedLLM

from .helpers import make_office
from .test_autopilot import HOT, FakePM


class FakeRemote:
    def __init__(self, models=("qwen2.5:7b",), down=False):
        self.models, self.down, self.calls = list(models), down, []

    def tags(self):
        if self.down:
            raise LLMError("down")
        return self.models

    def chat(self, model, messages, *, json_mode=False, options=None):
        if self.down:
            raise LLMError("down")
        self.calls.append(model)
        return "remote"


class RoutedLLMTest(unittest.TestCase):
    def test_uses_remote_and_substitutes_missing_models(self):
        remote, local = FakeRemote(), ScriptedLLM(default="local")
        r = RoutedLLM(remote, local, fallback_model="qwen2.5:7b", log=lambda *a: None)
        self.assertEqual(r.chat("qwen2.5:7b", []), "remote")
        self.assertEqual(r.chat("sora:latest", []), "remote")    # キャラ専用モデルは代用
        self.assertEqual(r.chat("qwen2.5:3b", []), "remote")     # 判定用 3B も代用
        self.assertEqual(remote.calls, ["qwen2.5:7b"] * 3)
        self.assertEqual(local.calls, [])

    def test_latest_tag_matches(self):
        remote = FakeRemote(models=["mio:latest"])
        r = RoutedLLM(remote, ScriptedLLM(), fallback_model="qwen2.5:7b")
        r.chat("mio", [])
        self.assertEqual(remote.calls, ["mio"])

    def test_falls_back_to_local_when_down(self):
        local = ScriptedLLM(default="local")
        r = RoutedLLM(FakeRemote(down=True), local, fallback_model="x", log=lambda *a: None)
        self.assertEqual(r.chat("qwen2.5:7b", []), "local")

    def test_remote_only_does_not_touch_local(self):
        local = ScriptedLLM(default="local")
        r = RoutedLLM(FakeRemote(down=True), local, fallback_model="x", local_fallback=False)
        with self.assertRaises(LLMError):
            r.chat("qwen2.5:7b", [])
        self.assertEqual(local.calls, [])


class BatchViewTest(unittest.TestCase):
    def test_lounge_runs_on_remote(self):
        from atena.lounge import RoomMaster
        o, local = make_office(config_toml='[guardian]\nuse_llm_judge = true\n'
                                           '[ollama]\nbatch_host = "http://192.168.0.112:11434"\n')
        for c in o.characters.values():
            c.talkativeness = 0.5
        remote = FakeRemote()
        view = o.batch_view(log=lambda *a: None, remote=remote)
        self.assertIsNot(view.guardian, o.guardian)
        self.assertIs(view.guardian.llm, view.llm)
        self.assertIs(o.guardian.llm, local)                     # 本体（配信側）はそのまま
        RoomMaster(view, jitter=0, listeners=[]).run(["hikari", "shizuku"], topic="t", turns=2)
        self.assertGreater(len(remote.calls), 2)
        self.assertEqual(local.calls, [])                        # Mac の Ollama は使っていない
        self.assertTrue(o.conn.execute("SELECT COUNT(*) FROM lounge_messages").fetchone()[0])  # 記録は Mac に

    def test_without_batch_host_is_same_office(self):
        o, _ = make_office()
        self.assertIs(o.batch_view(), o)


class AutopilotRemoteTest(unittest.TestCase):
    def test_runs_on_remote_even_when_mac_busy(self):
        o, _ = make_office(snapshot=HOT, config_toml='[ollama]\nbatch_host = "http://192.168.0.112:11434"\n')
        calls = []

        class RM:
            def run(self, ids):
                class R:
                    session_id, topic = "s", "t"
                return R()

        def factory(remote_only=False):
            calls.append(remote_only)
            return RM()
        ap = Autopilot(o, manager=FakePM(), room_master_factory=factory, rng=random.Random(0), log=lambda *a: None)
        ap._set("daily_day", "2026-10-10")
        ap._remote_ready = lambda: True
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 11, 0)).action, "lounge")
        self.assertEqual(calls, [True])                          # Mac 側では動かさない
        ap._remote_ready = lambda: False
        ap._set("lounge_next", "2026-10-10T00:00:00")
        self.assertEqual(ap.tick(datetime(2026, 10, 10, 14, 0)).action, "skip")


if __name__ == "__main__":
    unittest.main()
