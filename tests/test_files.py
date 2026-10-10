import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from atena.files import read_text_safely


class ReadSafelyTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.src = self.d / "w.toml"
        self.cache = self.d / "cache" / "w.toml"

    def test_reads_and_caches(self):
        self.src.write_text("a = 1", encoding="utf-8")
        self.assertEqual(read_text_safely(self.src, cache=self.cache), "a = 1")
        self.assertEqual(self.cache.read_text(encoding="utf-8"), "a = 1")
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_hanging_open_falls_back_to_cache(self):
        self.src.write_text("new", encoding="utf-8")
        self.cache.parent.mkdir()
        self.cache.write_text("old", encoding="utf-8")
        real = Path.read_text

        def slow(p, *a, **kw):
            if p == self.src:
                time.sleep(2)  # iCloud の取り寄せ待ちで open が返らない状態
            return real(p, *a, **kw)
        logs = []
        with mock.patch.object(Path, "read_text", slow):
            self.assertEqual(read_text_safely(self.src, cache=self.cache, timeout=0.2, log=logs.append), "old")
        self.assertIn("控え", logs[0])

    def test_missing_file_is_none(self):
        self.cache.parent.mkdir()
        self.cache.write_text("old", encoding="utf-8")
        self.assertIsNone(read_text_safely(self.src, cache=self.cache))
