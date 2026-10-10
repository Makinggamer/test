"""導入・接続の一括診断 (`atena doctor`)。

Mac で初めて動かすときや、うまく動かないときに、何が足りないかと直し方をまとめて表示する。
読み取りと接続確認だけで、設定やファイルは変更しない。
"""

from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

OK, WARN, NG, INFO = "ok", "warn", "ng", "info"
MARK = {OK: "○", WARN: "△", NG: "✗", INFO: "・"}


@dataclass
class Check:
    level: str
    item: str
    detail: str = ""
    fix: str = ""


def _model_in(models: list[str], name: str) -> bool:
    return name in models or (":" not in name and f"{name}:latest" in models)


def _voice_ids(voices: list) -> set[str]:
    out = set()
    for v in voices:
        if isinstance(v, dict):
            out.update(str(v.get(k)) for k in ("id", "name", "voice_id") if v.get(k))
        else:
            out.add(str(v))
    return out


class Doctor:
    def __init__(self, office, *, ollama_factory=None, irodori=None, which=shutil.which,
                 is_mac: bool | None = None):
        from .llm import OllamaClient
        from .stream.voice import IrodoriTTS
        self.o = office
        self.cfg = office.cfg
        self.ollama_factory = ollama_factory or (lambda host: OllamaClient(host, 5))
        self.irodori = irodori or IrodoriTTS(self.cfg.voice.irodori_host, timeout=5)
        self.which = which
        self.is_mac = platform.system() == "Darwin" if is_mac is None else is_mac
        self.checks: list[Check] = []

    def add(self, level, item, detail="", fix=""):
        self.checks.append(Check(level, item, detail, fix))

    # ---- 個別の点検 -----------------------------------------------------
    def basics(self):
        v = sys.version_info
        self.add(OK if v >= (3, 11) else NG, "Python", f"{v.major}.{v.minor}.{v.micro}",
                 "" if v >= (3, 11) else "brew install python@3.12 で 3.11 以上を入れる")
        cfg_file = self.cfg.root / "config" / "atena.toml"
        self.add(OK if cfg_file.exists() else NG, "設定ファイル", str(cfg_file),
                 "" if cfg_file.exists() else "atena init を実行")
        secrets = self.cfg.root / "config" / "owner_secrets.toml"
        filled = secrets.exists() and any("=" in ln and '""' not in ln and not ln.strip().startswith("#")
                                          for ln in secrets.read_text(encoding="utf-8").splitlines())
        self.add(OK if filled else WARN, "オーナー個人情報（ガーディアンの検出用）",
                 "記入あり" if filled else "未記入",
                 "" if filled else f"{secrets} に名前・住所などを記入（キャラには渡りません）")
        has_psutil = importlib.util.find_spec("psutil") is not None
        self.add(OK if has_psutil else WARN, "PC 負荷の計測（psutil）", "あり" if has_psutil else "なし",
                 "" if has_psutil else 'pip install -e ".[monitor]"')

    def characters(self):
        from .avatar.placeholder import check as check_avatar
        chars = list(self.o.characters.values())
        n = len(chars)
        self.add(OK if n >= 2 else (WARN if n == 1 else NG), "キャラ", f"{n} 人: " + "、".join(c.name for c in chars),
                 "" if n >= 2 else "アプリから同期するか config/characters/ に追加（ラウンジは 2 人以上）")
        for c in chars:
            if not c.persona.strip():
                self.add(WARN, f"{c.name}: 人格", "未設定", "アプリのキャラ管理で人格を入力")
            if not (c.specialties or c.favorites):
                self.add(INFO, f"{c.name}: 好きなもの・仕事", "未設定（場繋ぎ・雑談回の話題が減ります）")
            if c.avatar_dir:
                r = check_avatar(Path(c.avatar_dir).expanduser())
                if not r["ok"]:
                    self.add(NG, f"{c.name}: 立ち絵", f"{c.avatar_dir} に neutral.png がありません")
                elif r["missing_closed"] or r["missing_open"] or len(r["sizes"]) > 1:
                    self.add(WARN, f"{c.name}: 立ち絵", "不足またはサイズ違いあり", f"atena avatar check {c.id}")
                else:
                    self.add(OK, f"{c.name}: 立ち絵", c.avatar_dir)

    def ollama(self):
        o = self.cfg.ollama
        from .llm import LLMError
        try:
            models = self.ollama_factory(o.host).tags()
        except LLMError as e:
            self.add(NG, "Ollama（この PC）", str(e), "Ollama アプリを起動")
            return
        self.add(OK, "Ollama（この PC）", f"{o.host}（モデル {len(models)} 個）")
        need = {o.character_model: "キャラ既定", o.judge_model: "ガーディアン判定", o.staff_model: "裏方（記憶要約など）"}
        for c in self.o.characters.values():
            if c.model:
                need.setdefault(c.model, f"{c.name} 専用")
        for m, why in need.items():
            ok = _model_in(models, m)
            self.add(OK if ok else NG, f"モデル {m}", why, "" if ok else f"ollama pull {m}")
        if o.batch_host:
            try:
                remote = self.ollama_factory(o.batch_host).tags()
            except LLMError as e:
                self.add(WARN, "Ollama（別 PC）", f"{o.batch_host} に繋がりません: {e}",
                         "Windows の Ollama が起動しているか、ファイアウォールで Mac の IP を許可しているか確認"
                         "（繋がらない間は Mac で続けるか見送り）")
            else:
                ok = _model_in(remote, o.batch_model)
                self.add(OK if ok else NG, "Ollama（別 PC）", f"{o.batch_host}（{o.batch_model} {'あり' if ok else 'なし'}）",
                         "" if ok else f"Windows で ollama pull {o.batch_model}")

    def voice(self):
        from .stream.voice import TTSError
        try:
            ids = _voice_ids(self.irodori.voices())
        except TTSError as e:
            self.add(WARN, "Irodori-TTS-Server", str(e), "サーバーを起動（無くても字幕だけで配信できます）")
            return
        self.add(OK, "Irodori-TTS-Server", f"{self.cfg.voice.irodori_host}（声 {len(ids)} 個）")
        for c in self.o.characters.values():
            if not c.voice_id:
                self.add(WARN, f"{c.name}: 声", "voice_id 未設定（字幕のみになります）", "アプリのキャラ管理で voice_id を設定")
            elif c.voice_id not in ids:
                self.add(NG, f"{c.name}: 声", f"voice_id「{c.voice_id}」がサーバーにありません",
                         f"参照音声をサーバーの voices/{c.voice_id}.wav に置く")
            else:
                self.add(OK, f"{c.name}: 声", c.voice_id)

    def tools(self):
        ff = self.which("ffmpeg")
        self.add(OK if ff else WARN, "ffmpeg（切り抜きの書き出し）", ff or "なし", "" if ff else "brew install ffmpeg")

    def integrations(self):
        y = self.cfg.youtube
        if y.client_secret_file:
            token = self.cfg.path(y.token_file).exists()
            self.add(OK if token else WARN, "YouTube（OAuth）", "認証済み" if token else "未認証",
                     "" if token else "atena youtube auth")
        elif y.api_key:
            self.add(OK, "YouTube（API キー）", "設定あり（配信ごとに動画 ID を指定）")
        elif y.api_key_file:
            self.add(WARN, "YouTube（API キー）", f"{y.api_key_file} が無いか、api_key が書かれていません",
                     'スマホで iCloud Drive/Atena/youtube.toml に api_key = "..." と保存（iCloud の同期を待つ）')
        else:
            self.add(INFO, "YouTube", "未設定（docs/05_setup_mac_youtube.md の 5）")
        d = self.cfg.discord
        if not d.enabled:
            self.add(INFO, "Discord", "無効（docs/10_discord_lounge.md）")
        else:
            from .discord import load_webhooks
            try:
                hooks = load_webhooks(self.cfg.path(d.webhooks_file))
            except (ValueError, OSError) as e:
                self.add(NG, "Discord", str(e), f"{d.webhooks_file} を直す")
                hooks = None
            if hooks is not None:
                if not hooks:
                    self.add(NG, "Discord", f"{d.webhooks_file} が無いか、Webhook がありません",
                             "iCloud Drive/Atena/discord_webhooks.toml を確認（Mac の Finder で iCloud Drive の同期が"
                             "済んでいるか。ファイル名の拡張子が .txt になっていないか）")
                else:
                    ids = set(self.o.characters)
                    missing = [self.o.characters[i].name for i in ids if i not in hooks]
                    unknown = [k for k in hooks if k not in ids | {"room_master", "manager", "default"}]
                    shared = "default" in hooks or "room_master" in hooks
                    lvl = WARN if unknown or (missing and not shared) else OK
                    det = f"Webhook {len(hooks)} 個"
                    if missing:
                        det += f" / 専用なし: {'、'.join(missing)}" + ("（共用で投稿）" if shared else "")
                    if unknown:
                        det += f" / キャラ ID に無い名前: {', '.join(unknown)}"
                    self.add(lvl, "Discord", det, "atena discord test で確認" if lvl == OK else
                             "discord_webhooks.toml のキーをキャラ ID（atena character list）に合わせる")

    def runtime(self):
        from .monitor import read_lock
        locks = [d for p in self.cfg.resources.external_locks if (d := read_lock(Path(p).expanduser()))]
        self.add(INFO if not locks else WARN, "重い処理のロック", "なし" if not locks else " / ".join(locks))
        if self.is_mac:
            from .autopilot import LAUNCHD_LABEL
            plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
            self.add(OK if plist.exists() else INFO, "自動運転（ログイン時に起動）",
                     "設定あり" if plist.exists() else "未設定", "" if plist.exists() else "atena autopilot-install")

    def run(self) -> list[Check]:
        self.checks = []
        for step in (self.basics, self.characters, self.ollama, self.voice, self.tools, self.integrations,
                     self.runtime):
            try:
                step()
            except Exception as e:  # noqa: BLE001 - 1 項目の失敗で診断全体を止めない
                self.add(WARN, step.__name__, f"点検中にエラー: {e}")
        return self.checks


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{MARK[c.level]} {c.item}: {c.detail}")
        if c.fix and c.level in (NG, WARN):
            lines.append(f"    → {c.fix}")
    ng = sum(c.level == NG for c in checks)
    warn = sum(c.level == WARN for c in checks)
    lines.append("")
    lines.append("問題なし" if not ng and not warn else f"要対応 {ng} 件 / 確認 {warn} 件（✗ から順に直してください）")
    return "\n".join(lines)
