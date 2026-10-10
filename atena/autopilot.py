"""自動運転: オーナーが何もしなくても事務所が回るようにする (AP-01〜AP-05)。

常駐して 1 分ごとに次を判断する:
  - 毎朝 daily_time に日次サイクル（企画・学習・記憶整理。ラウンジは下で別に開く）
  - 活動時間帯の間、lounge_interval_min ごとにラウンジを開く（参加者は抽選。終われば振り返りで自動調整）
    常時運転（continuous）では、1 回が終わってから break_min 分の休憩で次の回を開く
  - 配信中・重い処理中・高負荷のときは見送り、retry_min 後にもう一度試す
オーナーは Discord で眺めるだけでよい。人格・お金・公開範囲の変更はここでは行わず、承認待ちに回る。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .monitor import CRITICAL


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(now: datetime, start: str, end: str) -> bool:
    """活動時間帯か。end が start より前なら日をまたぐ（例 10:00〜02:00）。24:00 も可。"""
    t, a, b = now.hour * 60 + now.minute, _hm(start), _hm(end)
    if a == b:
        return True
    return a <= t < b if a < b else (t >= a or t < b)


@dataclass
class TickResult:
    action: str          # daily / lounge / skip / idle
    detail: str = ""


class Autopilot:
    def __init__(self, office, *, manager=None, room_master_factory=None, rng: random.Random | None = None,
                 log=print, poster=None, clock=datetime.now):
        from .lounge import RoomMaster
        from .manager import ProjectManager
        self.o = office
        self.cfg = office.cfg.autopilot
        self.pm = manager or ProjectManager(office)
        # 引数 remote_only=True のときは、別 PC に繋がらなくても手元（Mac）では動かさない
        self.room_master_factory = room_master_factory or (
            lambda remote_only=False: RoomMaster(office.batch_view(log=log, remote_only=remote_only)))
        self.rng = rng or random.Random()
        self.log = log
        self.poster = poster  # 運営報告の投稿先（None なら設定から作る）
        self.clock = clock

    # ---- 状態（プロセスを再起動しても続きから） -------------------------
    def _get(self, key: str) -> str | None:
        row = self.o.conn.execute("SELECT value FROM autopilot_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set(self, key: str, value: str) -> None:
        self.o.conn.execute("INSERT INTO autopilot_state(key, value) VALUES (?,?)"
                            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.o.conn.commit()

    def _due(self, key: str, now: datetime) -> bool:
        v = self._get(key)
        return v is None or datetime.fromisoformat(v) <= now

    # ---- 1 回分の判断 ---------------------------------------------------
    def tick(self, now: datetime | None = None) -> TickResult:
        now = now or self.clock()
        if self._get("daily_day") != now.date().isoformat() and now.hour * 60 + now.minute >= _hm(self.cfg.daily_time):
            return self._daily(now)
        if in_window(now, self.cfg.active_start, self.cfg.active_end) and self._due("lounge_next", now):
            return self._lounge(now)
        return TickResult("idle")

    def _remote_ready(self) -> bool:
        """[ollama] batch_host（別 PC の Ollama）に繋がるか。"""
        host = self.o.cfg.ollama.batch_host
        if not host:
            return False
        from .llm import LLMError, OllamaClient
        try:
            OllamaClient(host, 5).tags()
            return True
        except LLMError:
            return False

    def _busy(self) -> str | None:
        h = self.o.monitor.check(record=False)
        return " / ".join(h.reasons) if h.status == CRITICAL else None

    def _daily(self, now: datetime) -> TickResult:
        busy = self._busy()
        if busy:
            return TickResult("skip", f"日次サイクルを見送り（{busy}）")
        self._set("daily_day", now.date().isoformat())  # 失敗しても同じ日に何度も繰り返さない
        rep = self.pm.daily_cycle(now.date(), lounge=False)
        self._ensure_sheets()
        detail = f"企画 {len(rep.outcomes)} 件 / 学習 {len(rep.learning)} 人 / 注意 {len(rep.alerts)} 件"
        self._post_report(rep)
        self.o.audit.record("autopilot", "daily", {"day": now.date().isoformat(), "detail": detail})
        return TickResult("daily", detail)

    def _ensure_sheets(self) -> None:
        """キャラ設計書の無いキャラに下書きを作る（1 日 1 回、作れたものだけ）。"""
        from .designer import CharacterDesigner
        d = CharacterDesigner(self.o)
        for cid in list(self.o.characters):
            try:
                if not d.has_sheet(cid):
                    d.deepen(cid)
            except Exception as e:  # noqa: BLE001
                self.log(f"[自動運転] {cid} の設計書づくりに失敗: {e}")

    def _post_report(self, rep) -> None:
        from .discord import MANAGER_KEY, daily_report_text, make_poster
        try:
            poster = self.poster if self.poster is not None else make_poster(self.o.cfg, log=self.log)
            if poster:
                # マネージャー専用が無くラウンジのフォーラムに流す場合は、報告ごとに投稿（スレッド）を作る
                in_forum = poster.forum and MANAGER_KEY not in poster.webhooks
                poster.send(MANAGER_KEY, "プロジェクトマネージャー", daily_report_text(self.o, rep),
                            thread_name=f"運営報告 {rep.day:%m/%d}" if in_forum else None)
        except Exception as e:  # noqa: BLE001 - 報告の失敗で自動運転を止めない
            self.log(f"[自動運転] 運営報告の投稿に失敗: {e}")

    def _lounge(self, now: datetime) -> TickResult:
        ids = list(self.o.characters)
        if len(ids) < 2:
            self._set("lounge_next", (now + timedelta(minutes=self.cfg.lounge_interval_min)).isoformat())
            return TickResult("skip", "キャラが2人未満")
        busy = self._busy()
        remote_only = False
        if busy:
            if self._remote_ready():
                remote_only = True  # Mac は忙しいが、AI の計算は別 PC でできるので開く
            else:
                self._set("lounge_next", (now + timedelta(minutes=self.cfg.retry_min)).isoformat())
                return TickResult("skip", f"ラウンジを見送り（{busy}）。{self.cfg.retry_min} 分後に再挑戦")
        flow = self._flow() if self.cfg.continuous else None
        if flow and flow["rounds"] < self.o.cfg.lounge.topic_rounds and all(m in ids for m in flow["members"]):
            members = flow["members"]  # 同じ顔ぶれ・同じ話題で、さっきの会話の続き
            carry = {k: flow.get(k) for k in ("topic", "mode", "host", "subject", "lines", "thread_id")}
        else:
            lo = max(2, min(self.cfg.min_participants, len(ids)))
            hi = max(lo, min(self.o.cfg.lounge.max_participants, len(ids)))
            members = self.rng.sample(ids, self.rng.randint(lo, hi))
            carry = {"lines": flow["lines"]} if flow else {}  # 話題を変える。直前の会話からの流れは見せる
        # 間隔に少し揺らぎを入れて、毎回同じ時刻にならないようにする
        self._set("lounge_next", (now + self._gap()).isoformat())
        try:
            rm = self.room_master_factory(remote_only=True) if remote_only else self.room_master_factory()
            res = rm.run(members, flow=True, carry=carry) if self.cfg.continuous else rm.run(members)
        except Exception as e:  # noqa: BLE001 - 1 回の失敗で自動運転を止めない
            self.log(f"[自動運転] ラウンジでエラー: {e}")
            return TickResult("skip", f"ラウンジでエラー: {e}")
        if self.cfg.unload_after_lounge:
            self._unload_models()
        if self.cfg.continuous:  # 常時運転: 会話が終わった時刻から休憩を数える
            self._set("lounge_next", (self.clock() + self._gap()).isoformat())
            same = bool(carry.get("topic"))
            lines = [list(x) for x in ((carry.get("lines") or []) + list(getattr(res, "transcript", [])))][-10:]
            self._set("lounge_flow", json.dumps({
                "members": members, "topic": res.topic, "mode": getattr(res, "mode", ""),
                "host": getattr(res, "host", None), "subject": getattr(res, "subject", ""),
                "lines": lines, "thread_id": getattr(res, "thread_id", None),
                "rounds": (flow["rounds"] + 1) if (flow and same) else 1}, ensure_ascii=False))
        self._set("lounge_last", json.dumps({"session": res.session_id, "at": now.isoformat()}))
        return TickResult("lounge", f"{res.session_id} 「{res.topic}」 {len(members)} 人")

    def _unload_models(self) -> None:
        """手元の Ollama に残ったモデルを降ろす（次の回の資源判定が自分のモデルで critical にならないように）。"""
        unload = getattr(self.o.llm, "unload", None)
        if unload is None:
            return
        oc = self.o.cfg.ollama
        models = {oc.character_model, oc.staff_model, oc.judge_model}
        models |= {c.model for c in self.o.characters.values() if c.model}
        for m in sorted(x for x in models if x):
            try:
                unload(m)
            except Exception as e:  # noqa: BLE001 - 降ろせなくても自動運転は続ける
                self.log(f"[自動運転] {m} を降ろせませんでした: {e}")

    def _flow(self) -> dict | None:
        v = self._get("lounge_flow")
        try:
            return json.loads(v) if v else None
        except ValueError:
            return None

    def _gap(self) -> timedelta:
        if self.cfg.continuous:
            return timedelta(minutes=max(1.0, self.cfg.break_min * self.rng.uniform(0.7, 1.3)))
        return timedelta(minutes=self.cfg.lounge_interval_min * (1 + self.rng.uniform(-0.15, 0.15)))

    # ---- 常駐 -----------------------------------------------------------
    def run_forever(self, *, poll_sec: int = 60, sleep=time.sleep) -> None:
        self.log("[自動運転] 開始（Ctrl+C で停止）")
        while True:
            r = self.tick()
            if r.action != "idle":
                self.log(f"[自動運転] {datetime.now():%m/%d %H:%M} {r.action}: {r.detail}")
            sleep(poll_sec)


def tick_once(ap: "Autopilot", lock_path, *, stale_min: int = 30) -> TickResult:
    """1 回分だけ判断して終わる（launchd から 1 分ごとに呼ぶ形）。
    常駐プロセスは Mac の省電力で眠ったまま起きなくなることがあるため、毎回新しいプロセスで動かす。
    前の回のラウンジがまだ走っていれば（印があれば）何もしない。stale_min 分より古い印は残骸として消す。"""
    import os
    from pathlib import Path
    lock = Path(lock_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age_min = (time.time() - lock.stat().st_mtime) / 60
        if age_min < stale_min:
            return TickResult("busy", f"前の回が実行中（{age_min:.0f} 分前から）")
        lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return ap.tick()
    finally:
        lock.unlink(missing_ok=True)


LAUNCHD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{atena}</string>
    <string>--root</string><string>{root}</string>
    <string>autopilot</string><string>--once</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{root}/data/autopilot.log</string>
  <key>StandardErrorPath</key><string>{root}/data/autopilot.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict>
</plist>
"""
LAUNCHD_LABEL = "com.atena.autopilot"
