import json
import unittest

from atena.goods import GoodsDesk
from atena.manager import ProjectManager

from .helpers import make_office

SHEET = json.dumps({"item": "アクリルスタンド", "summary": "ひかりのゲーム中ポーズのアクスタ", "price_jpy": 1500,
                    "est_cost_jpy": 30000, "channel": "BOOTH", "first_lot": 30,
                    "risks": ["立ち絵の生成モデルの利用条件", "特定商取引法の表記"]}, ensure_ascii=False)


class GoodsTest(unittest.TestCase):
    def test_flow_with_owner_approval_and_sales(self):
        o, _ = make_office([SHEET])
        desk = GoodsDesk(o)
        r = desk.propose("hikari", {"title": "ゲーム実況アクスタ", "revenue_idea": "配信で紹介"})
        g = desk.get(r["id"])
        self.assertEqual((g["status"], g["est_cost_jpy"], g["price_jpy"]), ("proposal", 30000, 1500))
        a = o.approvals.get(r["approval_id"])
        self.assertEqual((a["kind"], a["level"], a["status"]), ("goods", 3, "pending"))  # 費用はオーナー
        self.assertIn("約30,000円", a["summary"])
        with self.assertRaises(PermissionError):
            desk.set_status(r["id"], "approved", actor="manager")       # AI は承認できない
        ProjectManager(o).decide(r["approval_id"], True)                # オーナーが承認
        self.assertEqual(desk.get(r["id"])["status"], "approved")
        with self.assertRaises(ValueError):
            desk.record_sale(r["id"], 1)                               # 販売前
        desk.set_status(r["id"], "producing")
        desk.set_status(r["id"], "on_sale")
        desk.record_sale(r["id"], 3)
        self.assertEqual(desk.get(r["id"])["sold"], 3)
        rev = o.conn.execute("SELECT source, amount_jpy FROM revenue").fetchone()
        self.assertEqual((rev["source"], rev["amount_jpy"]), ("goods", 4500))
        with self.assertRaises(ValueError):
            desk.set_status(r["id"], "proposal")

    def test_reject(self):
        o, _ = make_office([SHEET])
        r = GoodsDesk(o).propose("hikari", {"title": "x"})
        ProjectManager(o).decide(r["approval_id"], False)
        self.assertEqual(GoodsDesk(o).get(r["id"])["status"], "rejected")

    def test_sheet_failure_still_recorded(self):
        o, _ = make_office(["not json"])
        r = GoodsDesk(o).propose("shizuku", {"title": "雨音ASMR", "revenue_idea": "BOOTHで販売"})
        g = GoodsDesk(o).get(r["id"])
        self.assertEqual((g["status"], g["summary"]), ("proposal", "BOOTHで販売"))
        self.assertIn("未見積もり", o.approvals.get(r["approval_id"])["summary"])


if __name__ == "__main__":
    unittest.main()
