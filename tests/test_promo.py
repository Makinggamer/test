import json
import unittest
from datetime import date

from atena.promo import AI_NOTICE, PromoDesk

from .helpers import make_office


def _slot(o, status="approved", start="2026-10-11T20:00", kind="stream"):
    sid, _ = o.scheduler.propose("hikari", "マリカ視聴者参加型", start, start[:11] + "21:30", kind=kind)
    if status == "approved":
        o.scheduler.set_status(sid, "approved", "owner")
    return sid


DRAFT = json.dumps({"title": "【参加型】マリカでみんなと勝負！", "description": "今夜はみんなとマリカ！コメントで参加してね",
                    "x_post": "今夜20時からマリカ参加型！ #ひかり配信"}, ensure_ascii=False)


class PromoTest(unittest.TestCase):
    def test_draft_goes_to_approval_with_ai_notice(self):
        o, llm = make_office([DRAFT])
        sid = _slot(o)
        d = PromoDesk(o).draft(sid)
        self.assertTrue(d["description"].endswith(AI_NOTICE))       # AI 明記を必ず付ける
        self.assertEqual(d["title"], "【参加型】マリカでみんなと勝負！")
        pending = o.approvals.pending()
        self.assertEqual((pending[0]["kind"], pending[0]["level"]), ("publish", 3))
        prompt = llm.calls[0]["messages"][-1]["content"]
        self.assertIn("10/11（日）20:00", prompt)
        self.assertIn("約 90 分", prompt)
        self.assertIn("ゲームが大好き", llm.calls[0]["messages"][0]["content"])  # キャラの口調で書く

    def test_long_fields_trimmed(self):
        long = json.dumps({"title": "あ" * 100, "description": "い" * 900, "x_post": "う" * 300}, ensure_ascii=False)
        o, _ = make_office([long])
        d = PromoDesk(o).draft(_slot(o))
        self.assertEqual(len(d["title"]), 60)
        self.assertEqual(len(d["x_post"]), 140)
        self.assertTrue(d["description"].startswith("い" * 600 + "\n\n"))
        self.assertTrue(d["description"].endswith(AI_NOTICE))

    def test_unsafe_field_rejects_whole_draft(self):
        bad = json.dumps({"title": "t", "description": "d", "x_post": "家のIPは192.168.0.105"}, ensure_ascii=False)
        o, _ = make_office([bad, bad])
        self.assertIsNone(PromoDesk(o).draft(_slot(o)))
        self.assertEqual(o.approvals.pending(), [])

    def test_upcoming_only_approved_streams_once(self):
        o, _ = make_office([DRAFT, DRAFT, DRAFT])
        _slot(o)                                   # 対象
        _slot(o, status="proposed", start="2026-10-11T22:00")   # 未確定
        _slot(o, start="2026-10-20T20:00")         # 先すぎる
        desk = PromoDesk(o)
        self.assertEqual(len(desk.draft_upcoming(date(2026, 10, 10))), 1)
        self.assertEqual(desk.draft_upcoming(date(2026, 10, 10)), [])  # 2 回目は作らない
        self.assertEqual(len(desk.list()), 1)


if __name__ == "__main__":
    unittest.main()
