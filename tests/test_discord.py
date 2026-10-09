import json
import unittest
from pathlib import Path

from atena import http
from atena.discord import DiscordPoster, LoungeRelay, load_webhooks, replay_session
from atena.lounge import RoomMaster

from .helpers import make_office

HOOK = "https://discord.com/api/webhooks/123/abc_DEF-1"


class FakeDiscord:
    def __init__(self, fail_first_429=False):
        self.calls = []
        self.fail_first_429 = fail_first_429

    def __call__(self, method, url, *, params=None, json_body=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "body": json_body, "headers": headers})
        if self.fail_first_429:
            self.fail_first_429 = False
            raise http.HTTPError(429, "Too Many Requests", json.dumps({"retry_after": 0.5}))
        return json.dumps({"id": str(len(self.calls)), "channel_id": "999"}).encode()


class PosterTest(unittest.TestCase):
    def test_own_and_default_webhook(self):
        f = FakeDiscord()
        p = DiscordPoster({"mio": HOOK + "m", "default": HOOK + "d"}, fetch=f)
        p.send("mio", "ミオ", "こんにちは @everyone")
        p.send("sora", "ソラ", "やあ")
        self.assertEqual(f.calls[0]["url"], HOOK + "m")
        self.assertNotIn("username", f.calls[0]["body"])          # 専用 Webhook の名前・アイコンを使う
        self.assertEqual(f.calls[0]["body"]["allowed_mentions"], {"parse": []})  # 通知を飛ばさない
        self.assertEqual(f.calls[1]["url"], HOOK + "d")
        self.assertEqual(f.calls[1]["body"]["username"], "ソラ")   # 共用は名前だけ差し替え
        self.assertEqual(f.calls[0]["params"]["wait"], "true")
        self.assertIn("AtenaProject", f.calls[0]["headers"]["User-Agent"])

    def test_no_webhook_and_failures_do_not_raise(self):
        def broken(*a, **k):
            raise http.HTTPError(500, "x")
        logs = []
        p = DiscordPoster({"mio": HOOK}, fetch=broken, log=logs.append)
        self.assertIsNone(p.send("sora", "ソラ", "x"))  # Webhook なし
        self.assertIsNone(p.send("mio", "ミオ", "x"))
        self.assertTrue(logs)

    def test_rate_limit_retry(self):
        f, slept = FakeDiscord(fail_first_429=True), []
        p = DiscordPoster({"mio": HOOK}, fetch=f, sleep=slept.append)
        self.assertIsNotNone(p.send("mio", "ミオ", "x"))
        self.assertEqual(slept, [0.5])
        self.assertEqual(len(f.calls), 2)

    def test_long_message_truncated(self):
        f = FakeDiscord()
        DiscordPoster({"mio": HOOK}, fetch=f).send("mio", "ミオ", "あ" * 3000)
        self.assertEqual(len(f.calls[0]["body"]["content"]), 2000)

    def test_load_webhooks_validates(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "w.toml").write_text(f'mio = "{HOOK}"\n', encoding="utf-8")
        self.assertEqual(load_webhooks(d / "w.toml"), {"mio": HOOK})
        (d / "bad.toml").write_text('mio = "http://127.0.0.1/steal"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_webhooks(d / "bad.toml")
        self.assertEqual(load_webhooks(d / "none.toml"), {})


class RelayTest(unittest.TestCase):
    def _office(self, responses):
        o, llm = make_office(responses, default="{}")
        for c in o.characters.values():
            c.talkativeness = 0.5
        return o, llm

    def test_live_relay_during_lounge(self):
        o, _ = self._office(["配信の最初に流れを話すといいよ", "ひかりの配信は退屈で邪魔", "ひかりはやっぱり邪魔"])
        f = FakeDiscord()
        hooks = {"room_master": HOOK + "r", "hikari": HOOK + "h", "shizuku": HOOK + "s"}
        relay = LoungeRelay(DiscordPoster(hooks, fetch=f))
        res = RoomMaster(o, jitter=0, listeners=[relay]).run(["hikari", "shizuku"], topic="初見さん", turns=2)
        urls = [c["url"] for c in f.calls]
        texts = [c["body"]["content"] for c in f.calls]
        self.assertTrue(texts[0].startswith("🛋 **ラウンジ**"))           # 見出し
        self.assertIn(res.session_id, texts[0])
        self.assertEqual(urls[1], HOOK + "r")                               # 開始のあいさつ
        self.assertIn((HOOK + "h", "配信の最初に流れを話すといいよ"), list(zip(urls, texts)))
        self.assertIn((HOOK + "s", "［規制により非表示］"), list(zip(urls, texts)))  # 本文は出さない
        self.assertFalse(any("邪魔" in t for t in texts))
        self.assertTrue(any("事務所ルール" in t for t in texts))           # ルームマスターの注意は出る
        self.assertTrue(texts[-1].startswith("— おわり"))

    def test_forum_thread(self):
        o, _ = self._office(["a", "b"])
        f = FakeDiscord()
        relay = LoungeRelay(DiscordPoster({"default": HOOK}, fetch=f, forum=True))
        RoomMaster(o, jitter=0, listeners=[relay]).run(["hikari", "shizuku"], topic="t", turns=2)
        self.assertEqual(f.calls[0]["body"]["thread_name"], "t")
        self.assertTrue(all(c["params"].get("thread_id") == "999" for c in f.calls[1:]))

    def test_relay_failure_does_not_stop_lounge(self):
        class Boom:
            def __getattr__(self, name):
                def f(*a):
                    raise RuntimeError("down")
                return f
        o, _ = self._office(["a", "b"])
        res = RoomMaster(o, jitter=0, listeners=[Boom()]).run(["hikari", "shizuku"], topic="t", turns=2)
        self.assertEqual(len([w for w, _ in res.transcript if w != "ルームマスター"]), 2)

    def test_disabled_by_default(self):
        o, _ = self._office([])
        self.assertEqual(RoomMaster(o).listeners, [])

    def test_replay(self):
        o, _ = self._office(["a", "b"])
        res = RoomMaster(o, jitter=0, listeners=[]).run(["hikari", "shizuku"], topic="t", turns=2)
        f = FakeDiscord()
        relay = LoungeRelay(DiscordPoster({"default": HOOK}, fetch=f))
        n = replay_session(o.conn, res.session_id, relay, {c.name: c.id for c in o.characters.values()})
        self.assertEqual(n, 3)  # 開始のあいさつ + 2 発言
        self.assertEqual([c["body"].get("username") for c in f.calls[1:]], ["ルームマスター", "ひかり", "しずく"])


if __name__ == "__main__":
    unittest.main()
