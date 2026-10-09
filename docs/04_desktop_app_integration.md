# デスクトップアプリ連携仕様（Atena API）

Ollama で作成中のデスクトップアプリ（別プロジェクト）と Atena を連携するための仕様です。
**この文書をデスクトップアプリ側の開発セッションにそのまま渡せば、連携部分を実装できる**ように書いています。

## 役割分担

```
┌───────────────────────────┐        HTTP (127.0.0.1:8770)       ┌──────────────────────────────┐
│ デスクトップアプリ           │ ─── キャラ登録・更新 ───────────▶ │ Atena（事務所）                 │
│ ・キャラの作成・編集 UI      │ ─── 発言の安全検査 ─────────────▶ │ ・ガーディアン / モデレーター    │
│ ・オーナーとの会話           │ ─── 返答の依頼 ─────────────────▶ │ ・記憶 / スケジュール / 収益     │
│ ・事務所ダッシュボード表示   │ ◀── ランキング・予定・承認待ち ── │ ・YouTube 配信 / ラウンジ        │
└────────────┬──────────────┘                                     └──────────────┬───────────────┘
             └──────────────────── 同じ Ollama (localhost:11434) を共有 ───────────┘
```

- **キャラの「正」はアプリ側**。アプリで作成・編集したら `PUT /api/characters/{id}` で Atena に同期します。
- アプリでキャラが話す内容を**公開する（配信・SNS・切り抜き等）前**は、必ず `POST /api/guardian/check` を通してください。
- オーナーとの私的な会話はアプリ内で完結して構いません。ただし、その会話を Atena の記憶に入れたい場合は `reply` API を使います（個人情報は自動で伏せ字になります）。

## 起動

```bash
atena serve            # http://127.0.0.1:8770（8765 はアプリ自身が使っているため）
cat data/api_token     # アクセストークン
```

## 認証

全リクエストに `Authorization: Bearer <token>` ヘッダが必要です（無い・違う場合は 401）。
トークンは `data/api_token`（Atena のプロジェクトディレクトリ内）から読み込むか、`config/atena.toml` の `[api] token` に固定値を設定してアプリ側にも同じ値を設定してください。

## エンドポイント

すべて JSON。エラー時は `{"error": "..."}` と 400 / 401 / 404 / 413 / 500。

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/health` | 死活確認 `{"ok": true, "characters": 2}` |
| GET | `/api/characters` | 登録済みキャラ一覧 |
| PUT | `/api/characters/{id}` | キャラの登録・更新 |
| POST | `/api/characters/{id}/reply` | キャラにコメントへ返答させる |
| POST | `/api/guardian/check` | 発言の安全検査 |
| GET | `/api/monitor` | PC 状態 |
| GET | `/api/schedule?since=YYYY-MM-DD` | 配信スケジュール |
| GET | `/api/ranking` | 直近30日の収益ランキング |
| GET | `/api/approvals` | 承認待ち一覧 |
| POST | `/api/approvals/{id}` | 承認・却下 `{"approve": true}` |
| POST | `/api/lounge` | ラウンジ実行 `{"participants": ["a","b"], "topic": "...", "turns": 6}` |
| GET | `/api/report` | 状況レポート（Markdown） |

### PUT /api/characters/{id}

`id` は英小文字・数字・`_` `-` の40文字以内。

```json
{
  "name": "ひかり",
  "model": "hikari:latest",
  "persona": "ゲームが大好きな元気系AIキャラクター…",
  "speaking_style": "明るくテンポのよい口調",
  "goals": ["月間スパチャ10万円"],
  "autonomy_level": 1,
  "tags": ["ゲーム"],
  "voice_id": "Sora",
  "voice_caption": "落ち着いた優しい声で、ゆっくり話す",
  "voice_speaker": null
}
```

- `model`: アプリが `ollama create` したモデル名を指定すると、そのモデルで話します。空なら Atena の既定モデル。
- `persona`: アプリのシステムプロンプト（人格設定）。**オーナーの個人情報やPC環境は書かないでください**（Atena が事務所憲章を自動で前置きします）。
- `autonomy_level`: 0〜3。3 は「お金・契約」で常にオーナー承認。
- `voice_id`: Irodori-TTS-Server の voices/ に置いた参照音声の ID（アプリの `characters/<名前>/voice.wav` を `voices/<名前>.wav` として置く想定）。
- `voice_caption`: 話し方の説明（例: 「明るく元気で、楽しそうな話し方」）。アプリの感情タグ「ふつう」の喋り方を入れるのがおすすめ。
- `voice_speaker`: 予備の VOICEVOX の話者 ID。不要なら `null`。

### POST /api/guardian/check

```json
// リクエスト
{"text": "公開予定の発言", "speaker": "hikari", "use_llm_judge": true, "record": true}
// レスポンス
{"action": "allow" | "redact" | "block", "text": "公開してよい文（redact 時は伏せ字済み）",
 "categories": ["environment_leak"], "reasons": ["内部情報パターン: ipv4"]}
```

- `action` が `block` なら公開しない。`redact` なら返ってきた `text` を使う。
- `record: false` にすると違反として記録しない（入力中のプレビュー等に使用）。

### POST /api/characters/{id}/reply

```json
// リクエスト
{"comment": "こんばんは！", "author": "視聴者名", "platform": "app", "use_llm_judge": true}
// レスポンス（コメントが荒らし・NG で除外された場合や LLM 停止時は null）
{"reply": "こんばんは！今日も来てくれてありがとう！"}
```

## アプリ側の実装チェックリスト

- [ ] 設定画面に「Atena 連携」: URL（既定 `http://127.0.0.1:8770`）とトークン
- [ ] キャラ保存時に `PUT /api/characters/{id}`（失敗してもアプリの保存は成功させ、未同期マークを出す）
- [ ] 公開系の機能の前に `POST /api/guardian/check`
- [ ] ダッシュボード: `/api/ranking`、`/api/schedule`、`/api/approvals`、`/api/monitor` を表示
- [ ] 承認待ちに「承認 / 却下」ボタン → `POST /api/approvals/{id}`
- [ ] Atena が起動していない時は連携機能を無効表示にする（`/api/health` で確認）

## GPU の取り合いを避けるロック

アプリは重い処理（LoRA 学習など）の間 `~/vid2anime/.heavy.lock`（`{"pid", "what", "started"}`）を置いています。Atena はこれを次のように扱います。

- ロックがあり、持ち主のプロセスが生きている間は「PC 負荷 critical」とみなし、配信を開始しません。
- 配信中は Atena が同じ形式でロックを置きます（`what` は「Atena 配信中（キャラ名）」）。**アプリ側のジョブキューは、このロックがある間、重い処理を開始しないでください。**
- すでにロックがある場合、Atena は上書きしません。終了時は自分の pid のロックだけを消します。

## 注意

- API は `127.0.0.1` のみで待ち受けます。ほかの PC やインターネットからは使えません。
- 処理は1件ずつ順番に行います。返答やラウンジは LLM の生成時間ぶん待つので、アプリ側は非同期で呼び出し、タイムアウトを長め（60秒以上）にしてください。
