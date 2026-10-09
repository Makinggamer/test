"""設定ファイル (config/atena.toml) の読み込み。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OllamaConfig:
    host: str = "http://localhost:11434"
    character_model: str = "qwen2.5:7b"
    judge_model: str = "qwen2.5:3b"
    staff_model: str = "qwen2.5:7b"
    timeout_sec: float = 60.0


@dataclass
class BlockedWindow:
    """オーナーが PC を使う時間帯など、配信を入れない枠。weekday: 0=月 … 6=日, None=毎日"""

    start: str
    end: str
    weekday: int | None = None
    reason: str = ""


@dataclass
class ResourceConfig:
    max_cpu_pct: float = 85.0
    max_ram_pct: float = 85.0
    max_gpu_pct: float = 92.0
    max_vram_pct: float = 92.0
    max_gpu_temp_c: float = 83.0
    warn_ratio: float = 0.9
    max_concurrent_streams: int = 1
    max_stream_hours_per_day: float = 8.0
    default_stream_vram_mb: int = 7000
    unified_gpu_fraction: float = 0.66   # Apple Silicon: GPU が使えるユニファイドメモリの割合（目安）
    max_swap_mb: float = 4096
    min_cpu_speed_limit_pct: float = 85  # macOS: これ未満のサーマル制限で critical
    blocked_windows: list[BlockedWindow] = field(default_factory=list)


@dataclass
class GuardianConfig:
    use_llm_judge: bool = True
    fail_closed: bool = True
    env_terms: list[str] = field(default_factory=list)
    hostile_terms: list[str] = field(default_factory=list)
    violation_alert_threshold: int = 3


@dataclass
class MemoryConfig:
    max_items: int = 500
    max_chars: int = 60000
    keep_recent: int = 50
    digest_batch: int = 30
    viewer_cap: int = 1000


@dataclass
class ApprovalConfig:
    auto_approve_levels: list[int] = field(default_factory=lambda: [0, 1])


@dataclass
class LoungeConfig:
    turns: int = 8


@dataclass
class YouTubeConfig:
    api_key: str = ""                    # API キー方式（動画IDを指定して接続）
    client_secret_file: str = ""         # OAuth 方式（自分の配信を自動検出）。Google Cloud の「デスクトップアプリ」
    token_file: str = "data/youtube_token.json"
    daily_quota: int = 10000
    quota_reserve: int = 1500            # 配信以外の用途のために残す分
    poll_cost: int = 5                   # liveChatMessages.list 1回あたりのユニット（Google の料金表で要確認）
    expected_stream_hours: float = 3.0   # 割り当てを使い切らないためのポーリング間隔計算に使用
    fx_rates: dict = field(default_factory=lambda: {"JPY": 1.0, "USD": 150.0, "EUR": 160.0, "TWD": 4.6,
                                                    "KRW": 0.11})


@dataclass
class VoiceConfig:
    engine: str = "voicevox"
    host: str = "http://127.0.0.1:50021"
    subtitle_file: str = "data/obs/subtitle.txt"   # OBS のテキストソース「ファイルから読み込む」に指定
    comment_file: str = "data/obs/comment.txt"


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""                      # 空なら起動時に data/api_token を生成


@dataclass
class Config:
    root: Path
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    resources: ResourceConfig = field(default_factory=ResourceConfig)
    guardian: GuardianConfig = field(default_factory=GuardianConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    approvals: ApprovalConfig = field(default_factory=ApprovalConfig)
    lounge: LoungeConfig = field(default_factory=LoungeConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    api: ApiConfig = field(default_factory=ApiConfig)

    def path(self, p: str) -> Path:
        """設定内の相対パスをプロジェクトルート基準で解決する。"""
        q = Path(p).expanduser()
        return q if q.is_absolute() else self.root / q

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def characters_dir(self) -> Path:
        return self.config_dir / "characters"

    @property
    def db_path(self) -> Path:
        return self.root / "data" / "atena.db"

    @property
    def ng_words_path(self) -> Path:
        return self.config_dir / "ng_words.txt"

    @property
    def owner_secrets_path(self) -> Path:
        return self.config_dir / "owner_secrets.toml"


def _build(cls, data: dict | None):
    data = data or {}
    known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    return cls(**known)


def load_config(root: str | Path = ".") -> Config:
    root = Path(root).resolve()
    path = root / "config" / "atena.toml"
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

    res_raw = dict(raw.get("resources", {}))
    windows = [_build(BlockedWindow, w) for w in res_raw.pop("blocked_windows", [])]
    resources = _build(ResourceConfig, res_raw)
    resources.blocked_windows = windows

    return Config(
        root=root,
        ollama=_build(OllamaConfig, raw.get("ollama")),
        resources=resources,
        guardian=_build(GuardianConfig, raw.get("guardian")),
        memory=_build(MemoryConfig, raw.get("memory")),
        approvals=_build(ApprovalConfig, raw.get("approvals")),
        lounge=_build(LoungeConfig, raw.get("lounge")),
        youtube=_build(YouTubeConfig, raw.get("youtube")),
        voice=_build(VoiceConfig, raw.get("voice")),
        api=_build(ApiConfig, raw.get("api")),
    )
