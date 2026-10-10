"""グッズプロデューサー: グッズの企画書・制作・販売の管理 (GD-01〜GD-05)。

流れ: 企画（キャラの提案）→ 企画書（グッズプロデューサーが作成）→ 制作の承認（費用が出るので必ずオーナー = L3）
      → 制作中 → 販売中（売れたら収益台帳へ）→ 終了
発注・支払い・販売ページの公開は Atena では行わない（オーナーが行い、状態だけ記録する）。
"""

from __future__ import annotations

from .db import now_iso
from .llm import LLMError, parse_json

GOODS_PRODUCER = "グッズプロデューサー"
STATUSES = ("proposal", "approved", "producing", "on_sale", "ended", "rejected")
STATUS_JP = {"proposal": "承認待ち", "approved": "制作承認済み", "producing": "制作中", "on_sale": "販売中",
             "ended": "終了", "rejected": "見送り"}
# 状態の進め方（オーナーの操作・承認で進む）
NEXT = {"proposal": {"approved", "rejected"}, "approved": {"producing", "rejected"},
        "producing": {"on_sale", "ended"}, "on_sale": {"ended"}, "ended": set(), "rejected": set()}

SHEET_PROMPT = """\
あなたは AI タレント事務所のグッズプロデューサーです。所属キャラ「{name}」から次のグッズ企画が出ました。
企画: {title}
キャラの説明: {persona}
収益のアイデア: {idea}
小さく始められる形（少量生産・受注生産・デジタル販売など）で企画書にしてください。金額は日本円の概算。
権利面の注意（使う画像・音声・フォント・BGM の利用条件、販売時の特定商取引法の表記など）も挙げてください。
JSON だけを出力:
{{"item": "グッズの種類（例: アクリルスタンド、ボイスデータ）", "summary": "内容（100字以内）",
 "price_jpy": 販売価格, "est_cost_jpy": 初期費用の概算, "channel": "販売先の候補（例: BOOTH）",
 "first_lot": 最初の数量（デジタルなら 0）, "risks": ["注意点"]}}"""


class GoodsDesk:
    def __init__(self, office):
        self.o = office

    def propose(self, character_id: str, plan: dict) -> dict:
        """キャラのグッズ企画から企画書を作り、制作の承認をオーナーに求める。"""
        c = self.o.character(character_id)
        title = str(plan.get("title") or "グッズ")[:80]
        sheet = {}
        try:
            raw = self.o.llm.chat(self.o.cfg.ollama.staff_model, [{"role": "user", "content": SHEET_PROMPT.format(
                name=c.name, title=title, persona=c.persona[:400] or "（未設定）",
                idea=str(plan.get("revenue_idea") or "なし")[:200])}], json_mode=True)
            data = parse_json(raw)
            sheet = data if isinstance(data, dict) else {}
        except LLMError:
            sheet = {}

        def num(key):
            try:
                return max(0, int(float(sheet.get(key) or 0)))
            except (TypeError, ValueError):
                return 0
        summary = str(sheet.get("summary") or plan.get("revenue_idea") or "")[:200]
        if summary and not self.o.guardian.rule_check(summary).ok:
            summary = ""
        risks = [str(r)[:120] for r in (sheet.get("risks") or []) if isinstance(r, str)][:5]
        cost = num("est_cost_jpy")
        cur = self.o.conn.execute(
            "INSERT INTO goods(character_id, title, item, summary, price_jpy, est_cost_jpy, channel, first_lot, risks,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (character_id, title, str(sheet.get("item") or "")[:40], summary, num("price_jpy"), cost,
             str(sheet.get("channel") or "")[:40], num("first_lot"), "\n".join(risks), "proposal",
             now_iso(), now_iso()))
        gid = cur.lastrowid
        cost_txt = f"初期費用 約{cost:,}円" if cost else "初期費用は未見積もり"
        aid, _ = self.o.approvals.request("goods", f"{c.name}: グッズ「{title}」の制作（{cost_txt}）",
                                          level=3, requested_by=GOODS_PRODUCER, ref_id=gid)
        self.o.conn.execute("UPDATE goods SET approval_id=? WHERE id=?", (aid, gid))
        self.o.conn.commit()
        self.o.audit.record(GOODS_PRODUCER, "goods:propose", {"id": gid, "approval": aid, "cost": cost})
        return {"id": gid, "approval_id": aid}

    def get(self, gid: int):
        row = self.o.conn.execute("SELECT * FROM goods WHERE id=?", (gid,)).fetchone()
        if row is None:
            raise KeyError(f"グッズ #{gid} はありません")
        return row

    def set_status(self, gid: int, status: str, actor: str = "owner") -> None:
        row = self.get(gid)
        if status not in STATUSES:
            raise ValueError(f"状態は {', '.join(STATUSES)} のいずれか")
        if status not in NEXT[row["status"]]:
            raise ValueError(f"「{STATUS_JP[row['status']]}」から「{STATUS_JP[status]}」には進められません")
        if row["status"] == "proposal" and actor != "owner" and not actor.startswith("approval"):
            raise PermissionError("制作の承認はオーナーだけが行えます")
        self.o.conn.execute("UPDATE goods SET status=?, updated_at=? WHERE id=?", (status, now_iso(), gid))
        self.o.conn.commit()
        self.o.audit.record(actor, "goods:status", {"id": gid, "from": row["status"], "to": status})

    def on_approval(self, gid: int, approve: bool) -> None:
        """承認待ち（approvals）の結果を反映する。"""
        if self.get(gid)["status"] == "proposal":
            self.set_status(gid, "approved" if approve else "rejected", actor="approval(owner)")

    def record_sale(self, gid: int, qty: int, *, price_jpy: int | None = None, actor: str = "owner") -> int:
        """売れた数を記録し、売上を収益台帳に入れる（手数料・送料を引く前の金額）。"""
        row = self.get(gid)
        if row["status"] != "on_sale":
            raise ValueError("販売中のグッズだけ売上を記録できます")
        price = row["price_jpy"] if price_jpy is None else price_jpy
        if qty <= 0 or price <= 0:
            raise ValueError("数量と価格は 1 以上")
        self.o.conn.execute("UPDATE goods SET sold=sold+?, updated_at=? WHERE id=?", (qty, now_iso(), gid))
        self.o.conn.commit()
        return self.o.ledger.add(row["character_id"], "goods", qty * price, memo=f"{row['title']} ×{qty}",
                                 actor=actor)

    def list(self, status: str | None = None):
        q, args = "SELECT * FROM goods", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        return self.o.conn.execute(q + " ORDER BY id DESC", args).fetchall()
