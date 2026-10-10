"""リソースモニター: PC 状態の計測と判定 (RS-01〜RS-03)。LLM は使わない。

対応:
  - macOS (Apple Silicon): psutil / vm_stat / top, GPU 使用率は ioreg, サーマル制限は pmset,
    GPU に割り当て可能なメモリはユニファイドメモリ × unified_gpu_fraction として扱う
  - Linux / Windows: psutil または /proc, GPU は nvidia-smi
  - 共通: Ollama がロード中のモデルのメモリ使用量 (/api/ps)
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import ResourceConfig
from .db import now_iso

try:  # psutil は任意（macOS / Windows ではあると正確）
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - 環境依存
    psutil = None

OK, WARN, CRITICAL = "ok", "warn", "critical"
IS_MAC = platform.system() == "Darwin"


@dataclass
class Snapshot:
    cpu_pct: float | None = None
    ram_pct: float | None = None
    gpu_pct: float | None = None
    vram_used_mb: float | None = None   # Mac では Ollama モデルが GPU に載せている量
    vram_total_mb: float | None = None  # Mac ではユニファイドメモリ × unified_gpu_fraction
    gpu_temp_c: float | None = None
    mem_total_mb: float | None = None
    swap_used_mb: float | None = None
    llm_mem_mb: float | None = None     # Ollama がロード中のモデル合計
    cpu_speed_limit_pct: float | None = None  # macOS サーマル制限 (100 = 制限なし)
    external_jobs: list[str] = field(default_factory=list)  # 他アプリの重い処理（ロックファイル）

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


def _run(cmd: list[str], timeout: float = 5) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return None


# ---- 出力パーサ（テスト可能なよう純粋関数に分離） ----------------------
def parse_top_cpu(text: str) -> float | None:
    """macOS `top -l 2 -n 0` の最後の CPU usage 行から使用率を得る。"""
    m = re.findall(r"CPU usage:.*?([\d.]+)% idle", text)
    return 100.0 - float(m[-1]) if m else None


def parse_vm_stat(text: str, total_bytes: int) -> float | None:
    """macOS `vm_stat` から使用率 (active + wired + compressed) を得る。"""
    page = re.search(r"page size of (\d+) bytes", text)
    if not page or not total_bytes:
        return None
    size = int(page.group(1))

    def pages(label: str) -> int:
        m = re.search(rf"{label}:\s+(\d+)", text)
        return int(m.group(1)) if m else 0

    used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * size
    return 100.0 * used / total_bytes


def parse_ioreg_gpu(text: str) -> float | None:
    m = re.search(r'"Device Utilization %"\s*=\s*(\d+)', text)
    return float(m.group(1)) if m else None


def parse_pmset_therm(text: str) -> float | None:
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    return float(m.group(1)) if m else None


def parse_nvidia_smi(text: str) -> dict:
    try:
        util, used, total, temp = (float(x.strip()) for x in text.strip().splitlines()[0].split(","))
    except (ValueError, IndexError):
        return {}
    return {"gpu_pct": util, "vram_used_mb": used, "vram_total_mb": total, "gpu_temp_c": temp}


def parse_ollama_ps(data: dict) -> tuple[float, float]:
    """(GPU に載っている MB, 全体 MB)"""
    models = data.get("models") or []
    vram = sum(m.get("size_vram", 0) for m in models) / 1024 / 1024
    total = sum(m.get("size", 0) for m in models) / 1024 / 1024
    return vram, total


def pid_alive(pid: int) -> bool:
    if platform.system() == "Windows":
        # Windows の os.kill(pid, 0) は確認ではなくプロセスを終了させてしまうので使わない
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(path: Path, alive: Callable[[int], bool] = pid_alive) -> str | None:
    """ロックファイルがあれば内容の説明を返す。持ち主のプロセスが居なければ古いロックとして無視。

    想定形式（デスクトップアプリの .heavy.lock）: {"pid": 123, "what": "LoRA 学習", "started": "08:14"}
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        info = {}
    if not isinstance(info, dict):
        info = {}
    pid = info.get("pid")
    if isinstance(pid, int) and (pid == os.getpid() or not alive(pid)):
        return None  # 自分（Atena 配信中）のロック、または持ち主が居ない古いロック
    what = str(info.get("what") or path.name)
    started = f"（{info['started']} 開始）" if info.get("started") else ""
    return what + started


# ---- 計測 ------------------------------------------------------------
def _read_proc_cpu() -> tuple[int, int] | None:
    try:
        vals = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        return vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
    except (OSError, ValueError, IndexError):
        return None


def _cpu_pct(interval: float) -> float | None:
    if psutil:
        return float(psutil.cpu_percent(interval=interval))
    if IS_MAC:
        out = _run(["top", "-l", "2", "-n", "0", "-s", "1"], timeout=10)
        return parse_top_cpu(out) if out else None
    a = _read_proc_cpu()
    if not a:
        return None
    time.sleep(interval)
    b = _read_proc_cpu()
    if not b or b[1] == a[1]:
        return None
    return 100.0 * (1 - (b[0] - a[0]) / (b[1] - a[1]))


def _memory() -> dict:
    if psutil:
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        return {"ram_pct": float(vm.percent), "mem_total_mb": vm.total / 1024 / 1024,
                "swap_used_mb": sw.used / 1024 / 1024}
    if IS_MAC:
        total = _run(["sysctl", "-n", "hw.memsize"])
        vm = _run(["vm_stat"])
        if total and vm:
            t = int(total.strip())
            return {"ram_pct": parse_vm_stat(vm, t), "mem_total_mb": t / 1024 / 1024}
        return {}
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
        return {"ram_pct": 100.0 * (1 - info["MemAvailable"] / info["MemTotal"]),
                "mem_total_mb": info["MemTotal"] / 1024,
                "swap_used_mb": (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1024}
    except (OSError, KeyError, ValueError):
        return {}


def _ollama_ps(host: str) -> dict | None:
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/ps", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def make_sampler(cfg: ResourceConfig, ollama_host: str = "http://localhost:11434",
                 interval: float = 0.5) -> Callable[[], Snapshot]:
    def sample() -> Snapshot:
        s = Snapshot(cpu_pct=_cpu_pct(interval), **_memory())
        s.external_jobs = [d for p in cfg.external_locks
                           if (d := read_lock(Path(p).expanduser())) is not None]
        ps = _ollama_ps(ollama_host)
        llm_vram = None
        if ps is not None:
            llm_vram, s.llm_mem_mb = parse_ollama_ps(ps)
        if IS_MAC:
            out = _run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"])
            s.gpu_pct = parse_ioreg_gpu(out) if out else None
            out = _run(["pmset", "-g", "therm"])
            s.cpu_speed_limit_pct = parse_pmset_therm(out) if out else None
            if s.mem_total_mb:
                s.vram_total_mb = s.mem_total_mb * cfg.unified_gpu_fraction
            s.vram_used_mb = llm_vram
        else:
            out = _run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                        "--format=csv,noheader,nounits"])
            if out:
                for k, v in parse_nvidia_smi(out).items():
                    setattr(s, k, v)
        return s
    return sample


def take_snapshot(interval: float = 0.5) -> Snapshot:
    return make_sampler(ResourceConfig(), interval=interval)()


def evaluate(s: Snapshot, cfg: ResourceConfig) -> Health:
    checks = [
        ("CPU", s.cpu_pct, cfg.max_cpu_pct, "%"),
        ("メモリ", s.ram_pct, cfg.max_ram_pct, "%"),
        ("GPU", s.gpu_pct, cfg.max_gpu_pct, "%"),
        ("GPUメモリ", s.vram_pct, cfg.max_vram_pct, "%"),
        ("GPU温度", s.gpu_temp_c, cfg.max_gpu_temp_c, "℃"),
        ("スワップ", s.swap_used_mb, cfg.max_swap_mb, "MB"),
    ]
    status, reasons = OK, []

    def bump(new: str):
        nonlocal status
        if new == CRITICAL or (new == WARN and status == OK):
            status = new

    for label, value, limit, unit in checks:
        if value is None or not limit:
            continue
        if value >= limit:
            bump(CRITICAL)
            reasons.append(f"{label} {value:.0f}{unit} が上限 {limit:.0f}{unit} 以上")
        elif value >= limit * cfg.warn_ratio:
            bump(WARN)
            reasons.append(f"{label} {value:.0f}{unit} が上限に接近")
    if s.cpu_speed_limit_pct is not None and s.cpu_speed_limit_pct < 100:
        bump(CRITICAL if s.cpu_speed_limit_pct < cfg.min_cpu_speed_limit_pct else WARN)
        reasons.append(f"熱で CPU 速度が {s.cpu_speed_limit_pct:.0f}% に制限されています")
    for job in s.external_jobs:
        bump(CRITICAL)
        reasons.append(f"他アプリの重い処理中: {job}")
    return Health(status, reasons, s)


class ResourceMonitor:
    def __init__(self, cfg: ResourceConfig, conn: sqlite3.Connection | None = None,
                 sampler: Callable[[], Snapshot] | None = None):
        self.cfg = cfg
        self.conn = conn
        self.sampler = sampler or make_sampler(cfg)

    def check(self, record: bool = True) -> Health:
        h = evaluate(self.sampler(), self.cfg)
        if record and self.conn:
            s = h.snapshot
            self.conn.execute(
                "INSERT INTO resource_samples(taken_at, cpu_pct, ram_pct, gpu_pct, vram_used_mb, vram_total_mb,"
                " gpu_temp_c, swap_used_mb, llm_mem_mb, cpu_speed_limit_pct, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), s.cpu_pct, s.ram_pct, s.gpu_pct, s.vram_used_mb, s.vram_total_mb, s.gpu_temp_c,
                 s.swap_used_mb, s.llm_mem_mb, s.cpu_speed_limit_pct, h.status))
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
