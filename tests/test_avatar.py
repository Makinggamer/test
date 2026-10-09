import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from atena.avatar import MultiAvatar, mouth_envelope, parse_emotion
from atena.avatar.pngtuber import PngTuberOverlay
from atena.avatar.vts import VTSError, VTubeStudioAvatar
from atena.character import Character, load_character, save_character
from atena.stream import ChatMessage
from atena.stream.session import StreamSession
from atena.stream.voice import IrodoriTTS

from .helpers import make_office
from .test_phase2 import FakePlayer, FakeTTS, ListSource, make_wav


class EmotionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_emotion("[うれしい]やったー"), ("joy", "やったー"))
        self.assertEqual(parse_emotion("［照れ］えへへ"), ("shy", "えへへ"))
        self.assertEqual(parse_emotion("(surprise) えっ"), ("surprise", "えっ"))
        self.assertEqual(parse_emotion("[謎]本文"), ("neutral", "[謎]本文"))
        self.assertEqual(parse_emotion("タグなし"), ("neutral", "タグなし"))

    def test_mouth_envelope(self):
        import array, io, math, wave
        rate = 8000
        samples = array.array("h", [0] * (rate // 2) + [int(12000 * math.sin(i / 5)) for i in range(rate // 2)])
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(samples.tobytes())
        env = mouth_envelope(buf.getvalue(), frame_ms=50)
        self.assertEqual(len(env), 20)
        self.assertEqual(max(env[:10]), 0.0)   # 無音の間は口を閉じる
        self.assertGreater(env[-1], 0.8)        # 声の間は開く
        self.assertEqual(mouth_envelope(b"not wav"), [])


class FakeWS:
    def __init__(self, replies):
        self.replies, self.sent, self.closed = list(replies), [], False

    def send(self, s):
        self.sent.append(json.loads(s))

    def recv(self):
        return json.dumps(self.replies.pop(0))

    def close(self):
        self.closed = True


def vts_reply(data, t="Response"):
    return {"messageType": t, "data": data}


class VTSTest(unittest.TestCase):
    def test_auth_flow_and_calls(self):
        d = Path(tempfile.mkdtemp())
        ws = FakeWS([vts_reply({"authenticationToken": "TOK"}), vts_reply({"authenticated": True}),
                     vts_reply({}), vts_reply({})])
        av = VTubeStudioAvatar(token_file=d / "tok", connect=lambda url: ws)
        av.connect()
        self.assertEqual((d / "tok").read_text(), "TOK")
        self.assertEqual(ws.sent[0]["messageType"], "AuthenticationTokenRequest")
        self.assertEqual(ws.sent[1]["data"]["authenticationToken"], "TOK")
        c = Character(id="s", name="ソラ", vts_hotkeys={"joy": "Smile"})
        av.set_emotion(c, "joy")
        av.set_emotion(c, "sad")  # 未設定の感情は何も送らない
        av.mouth(1.7)
        self.assertEqual(ws.sent[2]["data"], {"hotkeyID": "Smile"})
        self.assertEqual(ws.sent[3]["data"]["parameterValues"], [{"id": "MouthOpen", "value": 1.0}])
        self.assertEqual(len(ws.sent), 4)

    def test_saved_token_and_error(self):
        d = Path(tempfile.mkdtemp())
        (d / "tok").write_text("SAVED")
        ws = FakeWS([vts_reply({"authenticated": True}),
                     vts_reply({"message": "bad"}, "APIError")])
        av = VTubeStudioAvatar(token_file=d / "tok", connect=lambda url: ws)
        av.connect()
        self.assertEqual(len(ws.sent), 1)
        with self.assertRaises(VTSError):
            av.mouth(0.5)

    def test_multi_avatar_isolates_failures(self):
        class Bad:
            def mouth(self, v):
                raise RuntimeError("x")
        got = []

        class Good:
            def mouth(self, v):
                got.append(v)
        logs = []
        MultiAvatar([Bad(), Good()], log=logs.append).mouth(0.5)
        self.assertEqual(got, [0.5])
        self.assertTrue(logs)


class PngTuberTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for n in ("neutral.png", "neutral_open.png", "joy.png", "secret.txt"):
            (self.dir / n).write_bytes(b"\x89PNG" if n.endswith(".png") else b"secret")
        (self.dir.parent / "outside.png").write_bytes(b"x")
        self.av = PngTuberOverlay(port=0)
        self.base = f"http://127.0.0.1:{self.av.port}"
        self.c = Character(id="s", name="ソラ", avatar_dir=str(self.dir))

    def tearDown(self):
        self.av.close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=3) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_state_and_images(self):
        self.av.set_emotion(self.c, "joy")
        self.av.mouth(0.9)
        st = self.av.state
        self.assertEqual((st["emotion"], st["open"]), ("joy", True))
        self.assertEqual(st["images"]["neutral"], {"closed": "/img/neutral.png", "open": "/img/neutral_open.png"})
        self.assertNotIn("secret", st["images"])
        self.assertEqual(self.get("/img/joy.png"), (200, b"\x89PNG"))
        self.assertEqual(self.get("/img/secret.txt")[0], 404)
        self.assertEqual(self.get("/img/..%2Foutside.png")[0], 404)
        self.assertEqual(self.get("/img/../outside.png")[0], 404)
        self.assertIn(b"EventSource", self.get("/")[1])

    def test_sse(self):
        self.av.set_emotion(self.c, "shy")
        with urllib.request.urlopen(self.base + "/events", timeout=3) as r:
            line = r.readline().decode()
        self.assertEqual(json.loads(line[len("data: "):])["emotion"], "shy")


class PlaceholderTest(unittest.TestCase):
    def test_generate_and_check(self):
        from atena.avatar.placeholder import check, generate
        d = Path(tempfile.mkdtemp())
        paths = generate(d)
        self.assertEqual(len(paths), 14)
        r = check(d)
        self.assertEqual((r["missing_closed"], r["missing_open"], r["sizes"], r["ok"]), ([], [], [(320, 320)], True))
        (d / "joy_open.png").unlink()
        self.assertEqual(check(d)["missing_open"], ["joy"])
        self.assertNotEqual((d / "neutral.png").read_bytes(), (d / "neutral_open.png").read_bytes())


class EmotionPipelineTest(unittest.TestCase):
    def test_irodori_caption_by_emotion(self):
        sent = []
        tts = IrodoriTTS(fetch=lambda m, u, json_body=None, **k: sent.append(json_body) or b"wav")
        c = Character(id="s", name="ソラ", voice_id="Sora", voice_caption="ふつうの声",
                      voice_captions={"joy": "明るく弾んだ声"})
        tts.synthesize("やった", c, "joy")
        tts.synthesize("うーん", c, "sad")
        self.assertEqual([b["irodori"]["caption"] for b in sent], ["明るく弾んだ声", "ふつうの声"])

    def test_session_passes_emotion_to_voice_and_avatar(self):
        o, llm = make_office(["[うれしい]来てくれてありがとう！"], default="[ふつう]またね")
        o.characters["hikari"].voice_id = "Hikari"

        class RecTTS(FakeTTS):
            def __init__(self):
                super().__init__()
                self.emotions = []

            def synthesize(self, text, c, emotion="neutral"):
                self.emotions.append(emotion)
                return super().synthesize(text, c, emotion)

        class RecAvatar:
            def __init__(self):
                self.emotions, self.mouths = [], []

            def set_emotion(self, c, e):
                self.emotions.append(e)

            def mouth(self, v):
                self.mouths.append(v)

            def close(self):
                pass
        tts, av = RecTTS(), RecAvatar()
        StreamSession(o, "hikari", ListSource([ChatMessage("console", "a", "やあ")]), tts=tts,
                      player=FakePlayer(), avatar=av, speak=lambda s: None, greet=False).run()
        self.assertEqual(tts.emotions, ["joy"])
        self.assertEqual(tts.texts, ["来てくれてありがとう！"])  # タグは読み上げない
        self.assertEqual(av.emotions[0], "joy")
        self.assertIn(0.8, av.mouths)  # 再生に合わせて口が動く
        self.assertIn("感情タグ", llm.calls[0]["messages"][0]["content"])

    def test_character_roundtrip_with_tables(self):
        d = Path(tempfile.mkdtemp())
        c = Character(id="s", name="ソラ", voice_captions={"joy": "明るく", "sad": '静かに"小声で"'},
                      vts_hotkeys={"joy": "Smile"}, avatar_dir="~/vid2anime/characters/Sora/avatar")
        self.assertEqual(load_character(save_character(c, d)), c)


if __name__ == "__main__":
    unittest.main()
