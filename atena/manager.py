"""プロジェクトマネージャー: 日次サイクル・企画審査・承認連携・状況レポート (PM-01〜PM-05)。"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .lounge import RoomMaster
from .memory import _bigrams
from .monitor import CRITICAL

TECH_DIRECTOR = "テクニカルディレクター"
GOODS_PRODUCER = "グッズプロデューサー"
MANAGER = "マネージャー"


def _similar(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


@dataclass
class PlanOutcome:
    character_id: str
    plan: dict | None
    status: str  # scheduled | pending_owner | duplicate | no_slot | goods_pending | no_plan | needs_fix
    detail: str = ""
    schedule_id: int | None = None
    approval_id: int | None = None


@dataclass
class DailyReport:
    day: date
    health: str
    health_reasons: list[str] = field(default_factory=list)
    outcomes: list[PlanOutcome] = field(default_factory=list)
    memory: dict[str, dict] = field(default_factory=dict)
    learning: dict[str, dict] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)
    lounge: dict | None = None
    promos: list[dict] = field(default_factory=list)


class ProjectManager:
    def __init__(self, office, learner=None):
        self.o = office
        self.learner = learner

    # ---- 企画審査（企画プロデューサー役） ------------------------------
    def _duplicate_of(self, title: str, others: list[str]) -> str | None:
        for t in others:
            if _similar(title, t) >= 0.6:
                return t
        return None

    def _review_and_place(self, cid: str, plan: dict, target_day: date, taken_titles: list[str]) -> PlanOutcome:
        o = self.o
        title = str(plan["title"])[:80]
        if (dup := self._duplicate_of(title, taken_titles)):
            return PlanOutcome(cid, plan, "duplicate", f"「{dup}」とネタ被り。差別化して再提案を")

        if plan.get("needs_system"):
            o.tasks.add(f"[{o.character(cid).name}] システム構築依頼: {plan['needs_system']}"[:120],
                        TECH_DIRECTOR, created_by=MANAGER, description=f"企画「{title}」のため")

        if plan.get("kind") == "goods":
            from .goods import GoodsDesk
            aid = GoodsDesk(o).propose(cid, plan)["approval_id"]
            o.tasks.add(f"[{o.character(cid).name}] グッズ企画書作成: {title}"[:120], GOODS_PRODUCER,
                        created_by=MANAGER)
            return PlanOutcome(cid, plan, "goods_pending", "グッズはオーナー承認待ち", approval_id=aid)

        pref = str(plan.get("preferred_time") or "")
        earliest = pref if len(pref) == 5 and pref[2] == ":" and pref[:2].isdigit() and pref[3:].isdigit() \
            and int(pref[:2]) < 24 else "10:00"
        slots = o.scheduler.suggest(target_day, plan["duration_min"], earliest=earliest, limit=1) \
            or o.scheduler.suggest(target_day, plan["duration_min"], limit=1)
        if not slots:
            return PlanOutcome(cid, plan, "no_slot", f"{target_day} に空き枠なし（PC負荷・禁止枠・上限のため）")
        start, end = slots[0]
        notes = f"収益案: {plan.get('revenue_idea', '')}"
        if plan.get("collab_with"):
            notes += f"\nコラボ希望: {', '.join(plan['collab_with'])}"
        sid, _ = o.scheduler.propose(cid, title, start, end, kind=str(plan.get("kind", "stream")), notes=notes)
        # 配信スケジュール確定は L2（設定で自動承認にもできる）
        aid, status = o.approvals.request("schedule", f"{o.character(cid).name}: {title} {start}〜{end[-5:]}",
                                          level=2, requested_by=cid, ref_id=sid)
        if status == "approved":
            problems = o.scheduler.set_status(sid, "approved", "manager(auto)")
            if problems:
                return PlanOutcome(cid, plan, "needs_fix", " / ".join(problems), sid, aid)
            return PlanOutcome(cid, plan, "scheduled", f"{start}〜{end[-5:]}", sid, aid)
        return PlanOutcome(cid, plan, "pending_owner", f"{start}〜{end[-5:]} で仮押さえ", sid, aid)

    # ---- 日次サイクル -------------------------------------------------
    def learn(self, cid: str, *, web: bool | None = None) -> dict:
        """キャラの知識を育てる: コメントから抽出 → 未確認を Web で照合 → Web で学習 → 容量整理。"""
        from .learning import Learner
        o = self.o
        lc = o.cfg.learning
        web = lc.web_enabled if web is None else web
        c = o.character(cid)
        learner = self.learner or Learner(o, wiki_lang=lc.wiki_lang)
        out: dict = {}
        r = learner.learn_from_comments(cid)
        out["from_comments"] = r.from_comments
        if web:
            r2 = learner.verify_pending(cid, lc.verify_per_run)
            out.update(verified=r2.verified, refuted=r2.refuted)
            if Learner.topics(c) or c.learning_sources:
                r3 = learner.study(cid, max_topics=lc.topics_per_run, queries_per_topic=lc.queries_per_topic)
                out.update(added=r3.added, superseded=r3.superseded, errors=r3.errors[:3])
        out["maintain"] = o.expertise.maintain(cid, c.name)
        out["stats"] = o.expertise.stats(cid)
        if out["stats"]["usage"] >= 0.9:
            title = f"{c.name} の知識が上限の {out['stats']['usage']:.0%}"
            if not any(t["title"].startswith(f"{c.name} の知識が上限") for t in o.tasks.list(assignee=MANAGER)):
                o.tasks.add(title, MANAGER, created_by=f"memory_manager:{cid}",
                            description="max_facts の引き上げ、または話題（好きなもの・専門）の絞り込みを検討")
        return out

    def daily_cycle(self, today: date | None = None, *, learn: bool = True,
                    lounge: bool | None = None) -> DailyReport:
        o = self.o
        today = today or datetime.now().date()
        target = today + timedelta(days=1)
        health = o.monitor.check()
        report = DailyReport(today, health.status, health.reasons)

        recent = o.scheduler.list(start_from=(today - timedelta(days=14)).isoformat())
        taken = [r["title"] for r in recent if r["status"] in ("proposed", "approved", "done")]

        for cid in o.characters:
            plan = o.agent(cid).propose_plan()
            if not plan:
                report.outcomes.append(PlanOutcome(cid, None, "no_plan", "企画案を取得できませんでした（LLM 未接続・応答不正・ルール違反のいずれか）"))
                continue
            outcome = self._review_and_place(cid, plan, target, taken)
            if outcome.status not in ("duplicate",):
                taken.append(str(plan["title"]))
            report.outcomes.append(outcome)

        for cid, c in o.characters.items():
            report.memory[cid] = o.memory.maintain(cid, c.name)
            if learn:
                report.learning[cid] = self.learn(cid)

        try:  # 確定した直近の配信枠の告知を広報が下書き（公開は承認後）
            from .promo import PromoDesk
            report.promos = PromoDesk(o).draft_upcoming(today)
        except Exception as e:  # noqa: BLE001 - 下書きの失敗で日次サイクルを止めない
            report.alerts.append(f"告知の下書きに失敗: {e}")

        if o.cfg.lounge.daily if lounge is None else lounge:
            report.lounge = self._daily_lounge()

        since = (datetime.now() - timedelta(days=7)).replace(microsecond=0).isoformat()
        threshold = o.cfg.guardian.violation_alert_threshold
        open_titles = {t["title"] for t in o.tasks.list(assignee=MANAGER)}
        for cid, n in o.guardian.violation_counts(since).items():
            if n >= threshold:
                name = o.characters[cid].name if cid in o.characters else cid
                title = f"{name} の発言傾向を確認（7日間で違反 {n} 件）"
                report.alerts.append(title)
                if not any(t.startswith(f"{name} の発言傾向を確認") for t in open_titles):
                    o.tasks.add(title, MANAGER, created_by="guardian",
                                description="人格プロンプトの見直し、自律度レベルの引き下げを検討")

        o.audit.record("manager", "daily_cycle", {
            "day": today.isoformat(), "health": health.status,
            "outcomes": [{"character": x.character_id, "status": x.status} for x in report.outcomes]})
        return report

    def _daily_lounge(self) -> dict:
        """LG-07: 1日1回、配信していない時間にラウンジを開く（参加者は最大 max_participants 人を抽選）。"""
        o = self.o
        ids = list(o.characters)
        if len(ids) < 2:
            return {"skipped": "キャラが2人未満"}
        health = o.monitor.check(record=False)
        if health.status == CRITICAL:  # 配信中ロック・高負荷の間は開かない
            return {"skipped": "PC 負荷: " + " / ".join(health.reasons)}
        n = max(2, o.cfg.lounge.max_participants)
        if len(ids) > n:
            ids = random.sample(ids, n)
        try:
            res = RoomMaster(o.batch_view()).run(ids)
        except Exception as e:  # ラウンジの失敗で日次サイクル全体を止めない
            return {"skipped": f"エラー: {e}"}
        return {"session_id": res.session_id, "topic": res.topic, "mode": res.mode,
                "participants": ids, "warnings": len(res.warnings)}

    # ---- 承認（オーナー操作） -----------------------------------------
    def decide(self, approval_id: int, approve: bool, actor: str = "owner") -> list[str]:
        o = self.o
        row = o.approvals.decide(approval_id, approve, actor)
        problems: list[str] = []
        if row["kind"] == "goods" and row["ref_id"]:
            from .goods import GoodsDesk
            GoodsDesk(o).on_approval(row["ref_id"], approve)
        if row["kind"] == "schedule" and row["ref_id"]:
            if approve:
                problems = o.scheduler.set_status(row["ref_id"], "approved", actor)
            else:
                o.scheduler.set_status(row["ref_id"], "rejected", actor)
        return problems

    # ---- 状況レポート -------------------------------------------------
    def status_report(self, today: date | None = None) -> str:
        o = self.o
        today = today or datetime.now().date()
        names = o.names()
        lines = [f"# Atena project 状況レポート ({today})", ""]

        h = o.monitor.check()
        lines += ["## PC 状態", f"- 判定: {h.status}"] + [f"- {r}" for r in h.reasons] + [""]

        lines.append("## 今後7日の配信予定")
        upcoming = o.scheduler.list(start_from=today.isoformat(),
                                    until=(today + timedelta(days=7)).isoformat())
        for r in upcoming:
            if r["status"] in ("approved", "proposed"):
                lines.append(f"- #{r['id']} [{r['status']}] {r['start']}〜{r['end'][-5:]} "
                             f"{names.get(r['character_id'], r['character_id'])}: {r['title']}")
        if not upcoming:
            lines.append("- なし")

        lines += ["", "## 承認待ち"]
        pend = o.approvals.pending()
        lines += [f"- #{a['id']} (L{a['level']}) {a['kind']}: {a['summary']}" for a in pend] or ["- なし"]

        lines += ["", "## 収益ランキング（直近30日）"]
        for e in o.current_ranking(today):
            g = f" ({e.growth_pct:+.0f}%)" if e.growth_pct is not None else ""
            lines.append(f"- {e.rank}位 {names.get(e.character_id, e.character_id)}: {e.total:,}円{g}")

        since = (datetime.now() - timedelta(days=7)).replace(microsecond=0).isoformat()
        lines += ["", "## ガーディアン（直近7日の違反）"]
        counts = o.guardian.violation_counts(since)
        lines += [f"- {names.get(k, k)}: {v}件" for k, v in counts.items()] or ["- なし"]

        lines += ["", "## 未完了タスク"]
        lines += [f"- #{t['id']} [{t['assignee']}] {t['title']}" for t in o.tasks.list()] or ["- なし"]
        return "\n".join(lines)
