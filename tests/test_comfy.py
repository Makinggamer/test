import json
import unittest

from atena.comfy import DEFAULT_WORKFLOW, BackgroundMaker, ComfyClient, ComfyError, fill
from atena import http

from .helpers import make_office


class FakeComfy:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.polls = 0

    def __call__(self, method, url, *, params=None, json_body=None, timeout=None, **kw):
        self.calls.append((method, url, params, json_body))
        if url.endswith("/prompt"):
            if self.fail:
                raise http.HTTPError(0, "refused")
            return json.dumps({"prompt_id": "p1"}).encode()
        if "/history/" in url:
            self.polls += 1
            if self.polls < 2:
                return b"{}"
            return json.dumps({"p1": {"status": {"completed": True}, "outputs": {"9": {"images": [
                {"filename": "atena_bg_0001.png", "subfolder": "", "type": "output"}]}}}}).encode()
        if url.endswith("/view"):
            return b"PNGDATA"
        return b"{}"


class FillTest(unittest.TestCase):
    def test_fill_keeps_types(self):
        wf = fill(DEFAULT_WORKFLOW, {"PROMPT": "a, b", "NEGATIVE": "n", "SEED": 42, "WIDTH": 768, "HEIGHT": 1344,
                                     "CKPT": "m.safetensors"})
        self.assertEqual(wf["3"]["inputs"]["seed"], 42)
        self.assertEqual(wf["5"]["inputs"]["width"], 768)
        self.assertEqual(wf["6"]["inputs"]["text"], "a, b")
        self.assertEqual(fill({"t": "x %PROMPT% y"}, {"PROMPT": "p"}), {"t": "x p y"})


class MakerTest(unittest.TestCase):
    def _office(self, responses=None):
        o, llm = make_office(responses or ['{"prompt": "window, sunset, bookshelf, 日本語"}'],
                             config_toml='[guardian]\nuse_llm_judge = false\n[shorts]\ncomfy_checkpoint = "m.safetensors"\n')
        return o

    def test_generate_and_cache(self):
        o = self._office()
        f = FakeComfy()
        bm = BackgroundMaker(o, ComfyClient(fetch=f, sleep=lambda s: None))
        p = bm.make("夕焼けの話")
        self.assertEqual(p.read_bytes(), b"PNGDATA")
        sent = next(c[3] for c in f.calls if c[1].endswith("/prompt"))["prompt"]
        self.assertIn("sunset", sent["6"]["inputs"]["text"])
        self.assertNotIn("日本語", sent["6"]["inputs"]["text"])     # 英語のタグだけ
        self.assertIn("no humans", sent["6"]["inputs"]["text"])
        n = len(f.calls)
        self.assertEqual(bm.make("夕焼けの話"), p)                   # 同じ話題は作り置き
        self.assertEqual(len(f.calls), n)

    def test_comfy_down_falls_back_in_shorts(self):
        from atena.shorts import LoungeShortMaker
        o = self._office()
        o.cfg.shorts.background = "comfy"
        logs = []
        sm = LoungeShortMaker(o, log=logs.append)
        import atena.comfy as comfy
        orig = comfy.ComfyClient.__init__

        def init(self, host="", **kw):
            orig(self, host, fetch=FakeComfy(fail=True), sleep=lambda s: None)
        comfy.ComfyClient.__init__ = init
        try:
            self.assertIsNone(sm.background("猫"))
        finally:
            comfy.ComfyClient.__init__ = orig
        self.assertIn("グラデーション", logs[0])

    def test_requires_workflow_or_checkpoint(self):
        o, _ = make_office()
        with self.assertRaises(ComfyError):
            BackgroundMaker(o, ComfyClient(fetch=FakeComfy())).make("猫")
