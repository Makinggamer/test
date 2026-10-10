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
    # ラウンジ（とその振り返り）を別 PC の Ollama で動かす。空なら手元で動かす
    batch_host: str = ""                 # 例 "http://192.168.0.112:11434"
    batch_model: str = "qwen2.5:7b"      # 別 PC に無いモデル（キャラ専用モデル・判定用 3B 等）の代わり
    batch_local_fallback: bool = True    # 別 PC に繋がらないときは手元で続ける


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
    # 他アプリの「重い処理中」ロックファイル。存在する間は critical 扱い（配信開始を止める）
    external_locks: list[str] = field(default_factory=list)
    # 配信中に Atena が置くロック（他アプリに重い処理を待ってもらう）。空なら置かない
    stream_lock_file: str = ""
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
    daily: bool = True               # 日次サイクルで1回開く
    max_participants: int = 4        # 日次で開くときの最大人数（多いと1人あたりの出番が減る）
    hobby_ratio: float = 0.5         # 好きなもの・専門の雑談回になる確率（残りは配信・収益の情報交換）
    pass_after: int = 3              # この発言数しゃべっていないキャラには話を振る
    jitter: float = 0.15             # 発言順の揺らぎ（0 で決定的）
    review: bool = True              # 終わるたびにマネージャーが振り返り、心がけ・口数を自動で微調整


@dataclass
class YouTubeConfig:
    api_key: str = ""                    # API キー方式（動画IDを指定して接続）
    api_key_file: str = ""               # api_key を別ファイル（api_key = "..."）から読む。iCloud 経由で渡す用
    client_secret_file: str = ""         # OAuth 方式（自分の配信を自動検出）。Google Cloud の「デスクトップアプリ」
    token_file: str = "data/youtube_token.json"
    daily_quota: int = 10000
    quota_reserve: int = 1500            # 配信以外の用途のために残す分
    poll_cost: int = 5                   # liveChatMessages.list 1回あたりのユニット（オーナー確認済み）
    expected_stream_hours: float = 3.0   # 割り当てを使い切らないためのポーリング間隔計算に使用
    fx_rates: dict = field(default_factory=lambda: {"JPY": 1.0, "USD": 150.0, "EUR": 160.0, "TWD": 4.6,
                                                    "KRW": 0.11})


@dataclass
class VoiceConfig:
    engine: str = "irodori"              # 声は Irodori のみ（途中で声が変わらないよう予備エンジンは持たない）
    irodori_host: str = "http://127.0.0.1:8088"
    irodori_model: str = "irodori-tts"
    irodori_num_steps: int = 0           # 0 ならサーバー既定。減らすと速く・粗くなる
    irodori_timeout_sec: float = 30      # これを超えたらボイストラブル扱い（字幕のみに切り替え）
    max_pending: int = 3                 # 読み上げ待ちがこれを超えたら古い通常返答は字幕のみ
    retry_after_sec: float = 60          # ボイストラブル後、再挑戦までの間隔
    trouble_notice: str = "ただいまボイストラブル中のため、字幕でお届けしています"
    no_voice_notice: str = "本日は字幕でお届けしています"
    subtitle_file: str = "data/obs/subtitle.txt"   # OBS のテキストソース「ファイルから読み込む」に指定
    comment_file: str = "data/obs/comment.txt"
    notice_file: str = "data/obs/notice.txt"       # ボイストラブル等の注意書き
    srt_dir: str = "data/streams"                  # 配信ごとの字幕 (SRT)。切り抜き作成用


@dataclass
class AvatarConfig:
    engines: list[str] = field(default_factory=list)  # "pngtuber" / "vmc" / "vtube_studio"（複数可）。空なら表示なし
    vts_url: str = "ws://localhost:8001"
    vts_mouth_param: str = "MouthOpen"   # 口の開きを入れる VTube Studio の入力パラメータ
    vts_token_file: str = "data/vts_token"
    pngtuber_port: int = 8771            # OBS ブラウザソース: http://127.0.0.1:8771/
    pngtuber_motion: bool = True         # 呼吸・話すときの弾み・表情フェード・感情の動き
    pngtuber_blink: bool = True          # まばたき（<感情>_blink.png がある感情のみ）
    vmc_host: str = "127.0.0.1"          # VMC プロトコルの送信先（Inochi Session など）
    vmc_port: int = 39539
    vmc_blink: bool = True               # Atena からまばたきを送る（受信側で自動まばたきするなら false）
    vmc_names: dict[str, str] = field(default_factory=dict)  # 送る名前の変更 {"mouth": "A", "joy": "Joy", ...}


@dataclass
class LearningConfig:
    web_enabled: bool = True             # Web（Wikipedia・登録サイト）から学ぶ
    wiki_lang: str = "ja"
    topics_per_run: int = 2              # 1回の学習で調べる話題の数
    queries_per_topic: int = 2
    verify_per_run: int = 5              # 1回に Web で照合する未確認知識の数
    search_provider: str = "duckduckgo"  # 一般 Web 検索: duckduckgo / searxng / brave / none
    searxng_url: str = "http://127.0.0.1:8888"
    brave_api_key: str = ""
    results_per_query: int = 2           # 1つの検索語で読む一般サイトの数
    respect_robots: bool = True          # サイトの robots.txt でクロール禁止なら読まない
    # 参考資料（Wikipedia と同格）として扱うドメイン。末尾一致
    trusted_domains: list[str] = field(default_factory=lambda: [
        "wikipedia.org", "go.jp", "ac.jp", "lg.jp", "ndl.go.jp", "kotobank.jp"])
    max_facts: int = 300                 # キャラごとの知識の上限（超えたら記憶マネージャーが整理）
    max_per_topic: int = 60


@dataclass
class StreamConfig:
    idle_after_sec: float = 40           # コメントがこの秒数途切れたら自分から話す
    idle_interval_sec: float = 60        # 場繋ぎトークの最短間隔
    max_idle_talks_in_row: int = 5       # 続けて話すのはここまで（以降は間隔を倍にする）


@dataclass
class AutopilotConfig:
    daily_time: str = "05:30"            # 毎日の日次サイクル（企画・学習・記憶整理）
    active_start: str = "10:00"          # ラウンジを開く時間帯（end が start より前なら日をまたぐ）
    active_end: str = "00:00"
    lounge_interval_min: int = 120       # ラウンジの間隔（±15% 揺らぐ）
    retry_min: int = 20                  # 配信中・高負荷で見送ったときの再挑戦まで
    min_participants: int = 2            # 参加者は min〜[lounge] max_participants 人を抽選


@dataclass
class DiscordConfig:
    enabled: bool = False
    webhooks_file: str = "config/discord_webhooks.toml"  # キャラ ID → Webhook URL（git 管理外）
    forum: bool = False          # フォーラムチャンネルなら、ラウンジ1回ごとに投稿（スレッド）を分ける
    post_blocked: bool = True    # 規制した発言を「［規制により非表示］」として出す（本文は出さない）


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8770                     # 8765 はデスクトップアプリ (studio-chat) が使用
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
    avatar: AvatarConfig = field(default_factory=AvatarConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    autopilot: AutopilotConfig = field(default_factory=AutopilotConfig)

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


def _resolve_youtube_key(y: "YouTubeConfig", root: Path) -> "YouTubeConfig":
    """api_key が空で api_key_file があれば、そこから読む（チャットや git を通さずに秘密を渡すため）。"""
    if y.api_key or not y.api_key_file:
        return y
    p = Path(y.api_key_file).expanduser()
    p = p if p.is_absolute() else root / p
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        try:
            y.api_key = str(tomllib.loads(text).get("api_key", "")).strip()
        except tomllib.TOMLDecodeError:
            y.api_key = text.splitlines()[0].strip() if text else ""  # キーだけを書いた場合
    return y


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
        youtube=_resolve_youtube_key(_build(YouTubeConfig, raw.get("youtube")), root),
        voice=_build(VoiceConfig, raw.get("voice")),
        api=_build(ApiConfig, raw.get("api")),
        avatar=_build(AvatarConfig, raw.get("avatar")),
        learning=_build(LearningConfig, raw.get("learning")),
        stream=_build(StreamConfig, raw.get("stream")),
        discord=_build(DiscordConfig, raw.get("discord")),
        autopilot=_build(AutopilotConfig, raw.get("autopilot")),
    )
