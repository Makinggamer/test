# Atena project

ローカルLLM（Ollama）で動く、2次元 AI キャラクターの IP 活動を運営する「AI タレント事務所」システムです。

- [役職一覧](docs/01_roles.md)
- [企画書（ローカルLLMの現実ライン・開発フェーズ）](docs/02_proposal.md)
- [要件定義書](docs/03_requirements.md)

## 現在できること（Phase 1）

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

## セットアップ

```bash
# Python 3.11+。依存ライブラリなし（psutil を入れると Windows/macOS でも CPU/RAM 計測が正確になります）
pip install -e ".[monitor]"     # もしくは python -m atena ... で直接実行

atena init                      # config/ と data/ を準備
# 1. config/owner_secrets.toml にオーナー個人情報を記入（ガーディアンの検出用。git 管理外・キャラには渡りません）
# 2. config/atena.toml でモデル名・PC の閾値・配信禁止時間帯を設定
# 3. キャラを取り込む（サンプル: config/characters/sample_*.toml）
atena character import-ollama <ollamaのモデル名> --id <英字ID> --name <表示名>

ollama pull qwen2.5:7b && ollama pull qwen2.5:3b   # 既定モデル（変更可）
atena stream hikari             # コンソールで模擬配信
atena lounge                    # キャラ同士の休憩所セッション
atena daily                     # 日次サイクル（企画提案〜スケジュール仮押さえ〜記憶整理）
atena report
```

VRAM 8GB 程度の PC では、配信中は `atena stream <id> --no-judge`（LLM 二次判定を省略しルール検査のみ）を推奨します。

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
  stream/        配信チャット連携（Phase 2 で Twitch / YouTube を追加）
config/          設定・NG ワード・キャラ定義
docs/            役職・企画書・要件定義
```
