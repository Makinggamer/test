import unittest
from datetime import date

from atena.config import BlockedWindow, ResourceConfig
from atena.db import connect
from atena.monitor import CRITICAL, OK, WARN, ResourceMonitor, Snapshot, evaluate
from atena.scheduler import Scheduler


def sched(snapshot=None, **kw):
    cfg = ResourceConfig(**kw)
    conn = connect(":memory:")
    snap = snapshot or Snapshot(cpu_pct=10, ram_pct=10, vram_used_mb=1000, vram_total_mb=8000)
    mon = ResourceMonitor(cfg, conn, sampler=lambda: snap)
    return Scheduler(conn, cfg, monitor=mon), mon


class MonitorTest(unittest.TestCase):
    def test_evaluate(self):
        cfg = ResourceConfig()
        self.assertEqual(evaluate(Snapshot(cpu_pct=10), cfg).status, OK)
        self.assertEqual(evaluate(Snapshot(cpu_pct=80), cfg).status, WARN)
        h = evaluate(Snapshot(cpu_pct=10, vram_used_mb=7900, vram_total_mb=8000, gpu_temp_c=90), cfg)
        self.assertEqual(h.status, CRITICAL)
        self.assertEqual(len(h.reasons), 2)
        self.assertEqual(evaluate(Snapshot(), cfg).status, OK)  # 不明値は無視


class SchedulerTest(unittest.TestCase):
    def test_concurrency(self):
        s, _ = sched()
        a, p = s.propose("hikari", "配信A", "2026-10-10T20:00", "2026-10-10T21:00")
        self.assertEqual(p, [])
        self.assertEqual(s.set_status(a, "approved", "owner"), [])
        b, p = s.propose("shizuku", "配信B", "2026-10-10T20:30", "2026-10-10T21:30")
        self.assertTrue(any("同時配信" in x for x in p))
        self.assertTrue(s.set_status(b, "approved", "owner"))  # 確定できない
        self.assertEqual(s.get(b)["status"], "proposed")

    def test_blocked_window_overnight(self):
        s, _ = sched(blocked_windows=[BlockedWindow(start="23:00", end="02:00", reason="就寝")])
        _, p = s.propose("hikari", "深夜", "2026-10-11T01:00", "2026-10-11T01:30")
        self.assertTrue(any("就寝" in x for x in p))
        _, p = s.propose("hikari", "昼", "2026-10-11T12:00", "2026-10-11T13:00")
        self.assertEqual(p, [])

    def test_weekday_window(self):
        # 2026-10-10 は土曜 (weekday=5)
        s, _ = sched(blocked_windows=[BlockedWindow(start="13:00", end="18:00", weekday=5)])
        self.assertTrue(s.validate("2026-10-10T14:00", "2026-10-10T15:00"))
        self.assertFalse(s.validate("2026-10-11T14:00", "2026-10-11T15:00"))

    def test_daily_hours_and_vram(self):
        s, mon = sched(max_stream_hours_per_day=2)
        a, _ = s.propose("hikari", "長時間", "2026-10-10T10:00", "2026-10-10T11:30")
        s.set_status(a, "approved", "o")
        self.assertTrue(any("1日の配信時間" in x for x in s.validate("2026-10-10T15:00", "2026-10-10T16:00")))
        mon.check()
        self.assertTrue(any("VRAM" in x for x in s.validate("2026-10-11T15:00", "2026-10-11T16:00",
                                                            est_vram_mb=9000)))

    def test_suggest(self):
        s, _ = sched(blocked_windows=[BlockedWindow(start="00:00", end="12:00")])
        a, _ = s.propose("hikari", "x", "2026-10-10T12:00", "2026-10-10T13:00")
        s.set_status(a, "approved", "o")
        slots = s.suggest(date(2026, 10, 10), 60, earliest="10:00", limit=2)
        self.assertEqual(slots[0], ("2026-10-10T13:00", "2026-10-10T14:00"))
        self.assertEqual(len(slots), 2)

    def test_preflight(self):
        s, _ = sched(snapshot=Snapshot(cpu_pct=99))
        a, _ = s.propose("hikari", "x", "2026-10-10T12:00", "2026-10-10T13:00")
        pf = s.preflight(a)
        self.assertFalse(pf.go)
        s2, _ = sched()
        self.assertTrue(s2.preflight(a).go)


if __name__ == "__main__":
    unittest.main()
