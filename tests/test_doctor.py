import unittest

from atena.doctor import NG, OK, WARN, Doctor, render
from atena.llm import LLMError
from atena.stream.voice import TTSError

from .helpers import make_office


class FakeOllama:
    def __init__(self, models=None, down=False):
        self.models, self.down = models or [], down

    def tags(self):
        if self.down:
            raise LLMError("refused")
        return self.models


class FakeIrodori:
    def __init__(self, voices=None, down=False):
        self.v, self.down = voices or [], down

    def voices(self):
        if self.down:
            raise TTSError("refused")
        return self.v


def run(o, hosts, irodori, which=lambda n: "/usr/bin/ffmpeg"):
    d = Doctor(o, ollama_factory=lambda h: hosts[h], irodori=irodori, which=which, is_mac=False)
    return {c.item: c for c in d.run()}


class DoctorTest(unittest.TestCase):
    def test_missing_models_and_voices(self):
        o, _ = make_office()
        o.characters["hikari"].voice_id = "Hikari"
        o.characters["shizuku"].model = "shizuku:latest"
        r = run(o, {"http://localhost:11434": FakeOllama(["qwen2.5:7b"])}, FakeIrodori([{"id": "Sora"}]))
        self.assertEqual(r["Ollama（この PC）"].level, OK)
        self.assertEqual(r["モデル qwen2.5:7b"].level, OK)
        self.assertEqual(r["モデル qwen2.5:3b"].level, NG)
        self.assertIn("ollama pull qwen2.5:3b", r["モデル qwen2.5:3b"].fix)
        self.assertEqual(r["モデル shizuku:latest"].level, NG)                 # キャラ専用モデル
        self.assertEqual(r["ひかり: 声"].level, NG)                             # サーバーに無い声
        self.assertEqual(r["しずく: 声"].level, WARN)                           # voice_id なし
        self.assertIn("要対応", render(list(r.values())))

    def test_all_good(self):
        o, _ = make_office(config_toml='[guardian]\nuse_llm_judge = false\n'
                                       '[ollama]\nbatch_host = "http://192.168.0.112:11434"\n')
        for c in o.characters.values():
            c.voice_id = c.id
        r = run(o, {"http://localhost:11434": FakeOllama(["qwen2.5:7b", "qwen2.5:3b", "qwen2.5:14b"]),
                    "http://192.168.0.112:11434": FakeOllama(["qwen2.5:7b"])},
                FakeIrodori(["hikari", "shizuku"]))
        self.assertEqual(r["Ollama（別 PC）"].level, OK)
        self.assertEqual(r["ひかり: 声"].level, OK)
        self.assertEqual(r["ffmpeg（切り抜きの書き出し）"].level, OK)

    def test_services_down(self):
        o, _ = make_office(config_toml='[ollama]\nbatch_host = "http://192.168.0.112:11434"\n')
        r = run(o, {"http://localhost:11434": FakeOllama(down=True)}, FakeIrodori(down=True), which=lambda n: None)
        self.assertEqual(r["Ollama（この PC）"].level, NG)
        self.assertEqual(r["Irodori-TTS-Server"].level, WARN)                 # 声が無くても配信はできる
        self.assertEqual(r["ffmpeg（切り抜きの書き出し）"].level, WARN)

    def test_discord_keys(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        (d / "w.toml").write_text('room_master = "https://discord.com/api/webhooks/1/a"\n'
                                  'mioo = "https://discord.com/api/webhooks/2/b"\n', encoding="utf-8")
        o, _ = make_office(config_toml=f'[discord]\nenabled = true\nwebhooks_file = "{(d / "w.toml").as_posix()}"\n')
        r = run(o, {"http://localhost:11434": FakeOllama(down=True)}, FakeIrodori(down=True))
        self.assertEqual(r["Discord"].level, WARN)
        self.assertIn("mioo", r["Discord"].detail)                            # 書き間違いを指摘


if __name__ == "__main__":
    unittest.main()


class YouTubeKeyFileTest(unittest.TestCase):
    def test_key_from_file(self):
        import tempfile
        from pathlib import Path
        from atena.config import load_config
        root = Path(tempfile.mkdtemp())
        (root / "config").mkdir()
        (root / "secret.toml").write_text('api_key = "AIzaTEST"\n', encoding="utf-8")
        (root / "plain.txt").write_text("AIzaPLAIN\n", encoding="utf-8")
        cfg_file = root / "config" / "atena.toml"
        cfg_file.write_text('[youtube]\napi_key_file = "secret.toml"\n', encoding="utf-8")
        self.assertEqual(load_config(root).youtube.api_key, "AIzaTEST")
        cfg_file.write_text(f'[youtube]\napi_key_file = "{(root / "plain.txt").as_posix()}"\n', encoding="utf-8")
        self.assertEqual(load_config(root).youtube.api_key, "AIzaPLAIN")
        cfg_file.write_text('[youtube]\napi_key = "direct"\napi_key_file = "secret.toml"\n', encoding="utf-8")
        self.assertEqual(load_config(root).youtube.api_key, "direct")       # 直接書いたものが優先
        cfg_file.write_text('[youtube]\napi_key_file = "none.toml"\n', encoding="utf-8")
        self.assertEqual(load_config(root).youtube.api_key, "")
