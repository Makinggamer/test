# 既存システムの調査と採用方針

AITuber 向けの既存システムを調べ、Atena に取り込むものを決めました（2026-10 時点）。

## 結論

| システム | 判断 | 理由 |
|---------|------|------|
| **VTube Studio**（公開 API） | ✅ 採用（Live2D モデルがある場合） | 口パク・表情を外部から操作できる WebSocket API がある。Atena は合成した音声の波形から口パクを作って送るので、Mac で内部音声を VTube Studio に流す仕組み（仮想オーディオ）が不要 |
| **内蔵 PNGTuber 表示** | ✅ 新規作成 | Live2D モデルが無くても、デスクトップアプリで作った立ち絵（感情差分＋口開き差分）で配信できる。OBS のブラウザソースで表示。外部ライセンス不要 |
| **AITuberKit** | ❌ 中核には不採用 | ①v2.0 以降は独自ライセンスで、**収益を伴う利用には別途商用ライセンスが必要**。②対応 TTS に Irodori が無く、声が変わってしまう。③コメント応答を AITuberKit 側で行うと、Atena のガーディアン・記憶・ランキングを通らない。アバター表示だけ使う場合も声は AITuberKit 側の TTS になるため不採用 |
| **Open-LLM-VTuber** | ❌ 不採用 | 音声対話向けの統合アプリで、Atena と役割（LLM・TTS・コメント処理）が重複する。開発初期段階とされている |
| **わんコメ（OneComme）** | ❌ 不採用 | 複数プラットフォームのコメントを集約できるが、外部連携用の WebSocket API は提供終了。公式もコメント取得は各プラットフォームの API を使うよう案内している |
| **OBS WebSocket**（OBS 28 以降内蔵） | 🔜 保留 | シーン切替（開始・終了・ボイストラブル画面）に使える。現状はテキストファイル方式の字幕・注意書きで足りているので、必要になったら追加 |
| **Whisper（mlx-whisper）** | 🔜 Phase 3 | 切り抜き用の文字起こし。配信の字幕は Atena が SRT で保存済みなので、配信以外の素材に使う |

## 採用した仕組みの全体像

```
キャラの返答（LLM）
  │  先頭に感情タグ [うれしい] 等（デスクトップアプリと同じ7感情）
  ▼
ガーディアン検査（タグは外してから検査）
  ▼
読み上げキュー ──▶ Irodori（感情ごとの喋り方 voice_captions を caption に指定）
  │                    │ WAV
  │                    ▼
  │               再生 + 波形から口の開きを 50ms ごとに計算
  │                    │
  ├── 表情切替 ───────┼──▶ VTube Studio（ホットキー / MouthOpen パラメータ）
  │                    └──▶ PNGTuber（感情差分画像 / 口開き差分）
  └── 字幕・SRT・ボイストラブル表示（OBS テキストソース）
```

## 使い方

```toml
# config/atena.toml
[avatar]
engines = ["pngtuber"]        # Live2D モデルがあれば ["vtube_studio"]、両方も可
```

```toml
# キャラ定義（デスクトップアプリから API で同期も可）
voice_captions = { joy = "明るく弾んだ声で", shy = "少し小さな声で恥ずかしそうに", sad = "静かに沈んだ声で" }
avatar_dir = "~/vid2anime/characters/Sora/avatar"   # neutral.png, neutral_open.png, joy.png, joy_open.png ...
vts_hotkeys = { joy = "Smile", sad = "Sad" }         # VTube Studio のホットキー名
```

```bash
atena avatar vts-auth          # VTube Studio の場合: 初回認証とホットキー名の確認
atena avatar test sora --hold  # 表情と口パクの確認（OBS の配置調整用に表示を残す）
atena youtube live sora --tts --avatar --hours 2
```

## ライセンス・権利の確認事項

- **VTube Studio**: 利用条件（無料版の透かし、商用利用の扱い）を公式で確認してください。
- **Live2D モデル**: モデルごとに商用利用・改変の可否が違います。購入・依頼したモデルの規約を確認してください。
- **立ち絵（PNGTuber）**: デスクトップアプリで生成した画像を使う場合、元にしたモデル・LoRA の学習データや利用規約で商用利用できるかを確認してください。
