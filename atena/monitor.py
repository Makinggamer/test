"""リソースモニター: PC 状態の計測と判定 (RS-01〜RS-03)。LLM は使わない。"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import ResourceConfig
from .db import now_iso

try:  # psutil は任意（Windows/macOS ではあると正確）
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - 環境依存
    psutil = None

OK, WARN, CRITICAL = "ok", "warn", "critical"


@dataclass
class Snapshot:
    cpu_pct: float | None = None
    ram_pct: float | None = None
    gpu_pct: float | None = None
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None
    gpu_temp_c: float | None = None

    @property
    def vram_pct(self) -> float | None:
        if self.vram_used_mb is None or not self.vram_total_mb:
            return None
        return 100.0 * self.vram_used_mb / self.vram_total_mb


@dataclass
class Health:
    status: str
    reasons: list[str] = field(default_factory=list)
    snapshot: Snapshot | None = None


def _read_proc_cpu() -> tuple[int, int] | None:
    try:
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    except (OSError, ValueError, IndexError):
        return None


def _cpu_pct(interval: float) -> float | None:
    if psutil:
        return float(psutil.cpu_percent(interval=interval))
    a = _read_proc_cpu()
    if not a:
        return None
    time.sleep(interval)
    b = _read_proc_cpu()
    if not b or b[1] == a[1]:
        return None
    return 100.0 * (1 - (b[0] - a[0]) / (b[1] - a[1]))


def _ram_pct() -> float | None:
    if psutil:
        return float(psutil.virtual_memory().percent)
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
        return 100.0 * (1 - info["MemAvailable"] / info["MemTotal"])
    except (OSError, KeyError, ValueError):
        return None


def _gpu() -> dict:
    """NVIDIA GPU のみ対応。取れなければ空。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True).stdout
        first = out.strip().splitlines()[0]
        util, used, total, temp = (float(x.strip()) for x in first.split(","))
        return {"gpu_pct": util, "vram_used_mb": used, "vram_total_mb": total, "gpu_temp_c": temp}
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return {}


def take_snapshot(interval: float = 0.5) -> Snapshot:
    return Snapshot(cpu_pct=_cpu_pct(interval), ram_pct=_ram_pct(), **_gpu())


def evaluate(s: Snapshot, cfg: ResourceConfig) -> Health:
    checks = [
        ("CPU", s.cpu_pct, cfg.max_cpu_pct, "%"),
        ("RAM", s.ram_pct, cfg.max_ram_pct, "%"),
        ("GPU", s.gpu_pct, cfg.max_gpu_pct, "%"),
        ("VRAM", s.vram_pct, cfg.max_vram_pct, "%"),
        ("GPU温度", s.gpu_temp_c, cfg.max_gpu_temp_c, "℃"),
    ]
    status, reasons = OK, []
    for label, value, limit, unit in checks:
        if value is None:
            continue
        if value >= limit:
            status = CRITICAL
            reasons.append(f"{label} {value:.0f}{unit} が上限 {limit:.0f}{unit} 以上")
        elif value >= limit * cfg.warn_ratio:
            if status != CRITICAL:
                status = WARN
            reasons.append(f"{label} {value:.0f}{unit} が上限に接近")
    return Health(status, reasons, s)


class ResourceMonitor:
    def __init__(self, cfg: ResourceConfig, conn: sqlite3.Connection | None = None, sampler=take_snapshot):
        self.cfg = cfg
        self.conn = conn
        self.sampler = sampler

    def check(self, record: bool = True) -> Health:
        h = evaluate(self.sampler(), self.cfg)
        if record and self.conn:
            s = h.snapshot
            self.conn.execute(
                "INSERT INTO resource_samples(taken_at, cpu_pct, ram_pct, gpu_pct, vram_used_mb, vram_total_mb,"
                " gpu_temp_c, status) VALUES (?,?,?,?,?,?,?,?)",
                (now_iso(), s.cpu_pct, s.ram_pct, s.gpu_pct, s.vram_used_mb, s.vram_total_mb, s.gpu_temp_c,
                 h.status))
            self.conn.commit()
        return h

    def history(self, limit: int = 20) -> list[sqlite3.Row]:
        if not self.conn:
            return []
        return self.conn.execute("SELECT * FROM resource_samples ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def known_vram_total_mb(self) -> float | None:
        if self.conn:
            r = self.conn.execute("SELECT vram_total_mb FROM resource_samples WHERE vram_total_mb IS NOT NULL"
                                  " ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                return r["vram_total_mb"]
        return None


def snapshot_dict(s: Snapshot) -> dict:
    d = asdict(s)
    d["vram_pct"] = s.vram_pct
    return d
