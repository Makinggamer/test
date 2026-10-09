# Atena project

ローカルLLM（Ollama）で動く、2次元 AI キャラクターの IP 活動を運営する「AI タレント事務所」システムです。

- [役職一覧](docs/01_roles.md)
- [企画書（ローカルLLMの現実ライン・開発フェーズ）](docs/02_proposal.md)
- [要件定義書](docs/03_requirements.md)
- [デスクトップアプリ連携仕様（Atena API）](docs/04_desktop_app_integration.md)
- [セットアップ手順（M3 Mac + YouTube + Irodori-TTS + OBS）](docs/05_setup_mac_youtube.md)

## 現在できること（Phase 1〜2）

| 機能 | 役職 | コマンド例 |
|------|------|-----------|
| 既存 Ollama キャラの取り込み | — | `atena character import-ollama mychar:latest --id hikari --name ひかり` |
| 発言の安全検査（差別・暴力／オーナー個人情報／配信環境・内部情報／キャラ同士の喧嘩） | ガーディアン | `atena guardian "テキスト"` |
| コメントの事前検査（プロンプトインジェクション・詮索・スパム） | コメントモデレーター | `atena stream hikari` 内で自動 |
| 記憶の保存・検索・容量管理（自動要約） | 記憶マネージャー | `atena memory stats` / `atena memory maintain` |
| PC 監視と配信可否判定 | リソースモニター | `atena monitor --watch` / `atena schedule preflight 3` |
| 配信枠の制約チェック・空き枠提案 | マネージャー | `atena schedule suggest 2026-10-10 --minutes 90` |
| キャラの企画提案 → 審査 → 仮押さえ → 承認 | マネージャー / 企画P | `atena daily` → `atena approvals list` → `atena approvals approve 1` |
| キャラ休憩所（会話・規制・ナレッジ化・切り抜き候補） | ルームマスター | `atena lounge` / `atena highlights --out clips.md` / `atena knowledge` |
| 収益台帳・ランキング・CSV 出力 | 経理 | `atena revenue add hikari superchat 5000` / `atena revenue rank` |
| タスク割り振り | オーナー / マネージャー | `atena task add "サムネ作成" hikari` |
| 状況レポート | マネージャー | `atena report` |
| 改ざん検知付き監査ログ | 監査ログ係 | `atena audit-verify` |
| YouTube Live 配信（コメント応答・スパチャ自動記録・読み上げ・字幕・負荷で自動終了） | タレント / マネージャー | `atena youtube live hikari --tts --hours 2` |
| 読み上げ（Irodori-TTS / 予備に VOICEVOX）・速度計測 | テクニカルディレクター | `atena voice list` / `atena voice test hikari "テスト"` / `atena voice bench hikari` |
| デスクトップアプリ連携 API | — | `atena serve` |

## セットアップ

M3 Mac + YouTube の手順は [docs/05_setup_mac_youtube.md](docs/05_setup_mac_youtube.md) を参照してください。概要:

```bash
brew install python@3.12 && python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[monitor]"
atena init                                   # config/ と data/ を準備
# config/owner_secrets.toml にオーナー個人情報を記入（ガーディアン検出用・git 管理外・キャラには渡らない）
ollama pull qwen2.5:7b && ollama pull qwen2.5:3b && ollama pull qwen2.5:14b
atena serve                                  # デスクトップアプリからキャラを同期（または character import-*）
atena stream hikari --tts                    # コンソールで模擬配信
atena youtube live hikari --video <動画ID> --tts --hours 2
```

## テスト

```bash
python -m unittest discover -s tests -t .
```

Ollama なしで動きます（LLM は台本応答のスタブに差し替え）。

## 構成

```
atena/
  character.py   キャラ定義・Ollama/Modelfile/WebUI からの取り込み・共通憲章
  agent.py       キャラの発言生成（→ガーディアン検査→言い直し）
  guardian.py    ガーディアン（意識統制官）
  moderator.py   コメントモデレーター
  memory.py      記憶マネージャー
  monitor.py     リソースモニター
  scheduler.py   配信スケジュール
  manager.py     プロジェクトマネージャー（日次サイクル・承認・レポート）
  lounge.py      ラウンジとルームマスター
  knowledge.py   ナレッジベース
  revenue.py     収益台帳・ランキング
  tasks.py       タスク・承認キュー（自律度レベル L0〜L3）
  audit.py       監査ログ（ハッシュチェーン）
  api.py         デスクトップアプリ連携用ローカル API
  http.py        外部 HTTP 呼び出し
  stream/
    session.py   配信セッション（応答・スパチャ・読み上げ・字幕・負荷監視）
    youtube.py   YouTube Live チャット・API 割り当て管理
    google_oauth.py  Google OAuth（読み取り権限のみ）
    voice.py     読み上げ（Irodori / VOICEVOX）・読み上げキュー・OBS 字幕ファイル
config/          設定・NG ワード・キャラ定義
docs/            役職・企画書・要件定義
```
