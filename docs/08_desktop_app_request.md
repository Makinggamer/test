# 【依頼】studio-chat（デスクトップアプリ）と Atena project の連携

オーナー承認済みの依頼です。このファイルの内容をそのままデスクトップアプリの開発セッションに貼ってください。
Sora のボイス調整や LoRA 学習など、進行中の作業を優先し、区切りのよいところで着手してください。

---

## 背景

Atena project は、オーナーの AI キャラクター達（YouTube 配信・グッズ等）を管理するローカルの「事務所」システムです。
同じ Mac 上で動き、同じ Ollama を使います。リポジトリ: https://github.com/Makinggamer/test （ブランチ `atena-phase1`）

- Atena の API: `http://127.0.0.1:8770`（Atena で `atena serve` を実行すると起動。8765 はこのアプリが使っているので避けています）
- 認証: すべてのリクエストに `Authorization: Bearer <token>` ヘッダ。トークンは Atena のフォルダの `data/api_token`（または Atena の `config/atena.toml` の `[api] token` に固定値を設定）
- 形式: JSON。エラーは `{"error": "..."}` と 400 / 401 / 404 / 413 / 500
- 死活確認: `GET /api/health` → `{"ok": true, "characters": 2}`
- **Atena が起動していないときは、連携機能を無効表示にするだけで、アプリ本体は普通に動くようにしてください**
- 返答やラウンジは LLM の生成を待つので、呼び出しは非同期・タイムアウト 60 秒以上にしてください

## お願い 1: キャラクターの同期（アプリ → Atena）

キャラの元データはアプリ側（アプリが正）です。キャラを保存したら次を送ってください。

`PUT /api/characters/{id}` （id は英小文字・数字・`_` `-` の 40 文字以内。例: `sora`）

```json
{
  "name": "Sora",
  "model": "<ollama のモデル名。空なら Atena の既定モデル>",
  "persona": "<人格設定（システムプロンプト）>",
  "speaking_style": "<話し方>",
  "goals": ["<目標>"],
  "autonomy_level": 1,
  "tags": [],
  "specialties": ["<仕事・専門>"],
  "favorites": ["<好きなもの>"],
  "learning_sources": ["<学習に使うサイト / RSS の URL>"],
  "voice_id": "Sora",
  "voice_caption": "<感情『ふつう』の喋り方の説明>",
  "voice_captions": {"neutral": "...", "joy": "...", "shy": "...", "sad": "...", "worry": "...", "angry": "...", "surprise": "..."},
  "avatar_dir": "<PNGTuber 用立ち絵フォルダの絶対パス>",
  "vts_hotkeys": {},
  "talkativeness": 0.5
}
```

- 送信に失敗してもアプリの保存は成功させ、「未同期」と表示してください（次回保存時か、手動の再同期ボタンで再送）
- `persona` にはオーナーの個人情報や PC 環境の情報を入れないでください（Atena 側で事務所のルールを自動で前置きします）
- `talkativeness`（ラウンジでの口数 0.0 無口〜1.0 おしゃべり）は任意です。アプリに欄が無ければ送らなくてよく、Atena が人格から推定します
- 値はすべて任意（`name` だけ必須）。型: 文字列 / 文字列の配列 / `{文字列: 文字列}`。`autonomy_level` は 0〜3

### 1-a. キャラクター管理に欄を3つ追加

オーナーの指示で、次の3つはこのアプリの欄で保管し、Atena に同期します（Atena 側では編集しません）。

| 欄 | 送るキー | 例（古書店員の Mio） |
|----|---------|------|
| 仕事・専門（複数可） | `specialties` | 「古書」「日本の近代文学」「本の修復」 |
| 好きなもの（複数可） | `favorites` | 「猫」「純喫茶」 |
| 学習に使うサイト / RSS（複数可・任意。どのサイトでも可） | `learning_sources` | `https://...` |

Atena はこれらの話題について Wikipedia・一般 Web 検索・登録サイト・視聴者コメントから知識を集め、配信でコメントが少ないときの話題にします。

### 1-b. 声（Irodori）

- `voice_id`: 生配信では Irodori-TTS-Server（OpenAI 互換、既定ポート 8088）を使います。`characters/<名前>/voice.wav` をサーバーの `voices/<名前>.wav` に置き、その名前を `voice_id` にします
- `voice_captions`: キャラクター管理の「感情ごとの声」の喋り方を、もとになる感情ごとに入れてください。キーは `neutral`（ふつう）`joy`（うれしい）`shy`（照れ）`sad`（悲しい）`worry`（心配）`angry`（怒り）`surprise`（驚き）。同じ感情のタグが複数あるときは、状況の紐づけが無い基本のものを1つ
- 生配信では感情分析・声の加工を省いた軽い経路で読みます（アプリ内の生成は1台詞に数十秒以上かかり、生配信に間に合わないため）

### 1-c. 立ち絵（任意）

- `avatar_dir`: PNGTuber 表示用の立ち絵フォルダ。直下に `neutral.png` / `neutral_open.png`（口を開けた差分）/ `joy.png` / `joy_open.png` … の 14 枚（7 感情 × 口の開閉）。無い感情は neutral を使います
- 立ち絵の制作は別の依頼文（Atena の `docs/07_avatar_assets_request.md`）で行います。作ったらこの欄にフォルダを入れてください
- `vts_hotkeys`: VTube Studio（Live2D）を使う場合のみ。通常は空で構いません

## お願い 2: 公開前のガーディアン検査

配信・SNS・動画など、外に出すキャラの発言は、公開前に必ず検査してください（オーナーとの私的な会話には不要です）。

`POST /api/guardian/check`

```json
{"text": "<公開予定の発言>", "speaker": "sora", "use_llm_judge": true, "record": true}
```

返り値:

```json
{"action": "allow | redact | block", "text": "<redact 時は伏せ字済みの文>", "categories": ["..."], "reasons": ["..."]}
```

- `block` → 公開しない。`redact` → 返ってきた `text` を使う。`allow` → そのまま
- 入力中のプレビューなどは `"record": false`（違反として記録しない）

## お願い 3: Atena 配信中ロックの尊重（GPU の取り合い防止）

- アプリの `~/vid2anime/.heavy.lock`（`{"pid", "what", "started"}`）を Atena も読みます。ロックがあり、その pid のプロセスが生きている間、Atena は配信を開始しません
- Atena は配信中に同じファイルへ同じ形式で書きます: `{"pid": <Atena の pid>, "what": "Atena 配信中（キャラ名）", "started": "HH:MM"}`。既存のロックは上書きせず、終了時は自分の pid のロックだけ消します
- **お願い: アプリのジョブキュー（LoRA 学習・ComfyUI・Irodori 生成などの重い処理）は、他プロセスが置いたこのロックがある間は開始を待ってください。** キューの `outside` 表示に出すと分かりやすいです
- 持ち主の pid が存在しない古いロックは無視してよい、という扱いを両者で揃えてください

## 任意: Atena の情報をアプリで表示

余裕があれば、ダッシュボードやキャラクター管理に表示してください。

| メソッド | パス | 内容 |
|---------|------|------|
| GET | `/api/ranking` | 直近 30 日の収益ランキング |
| GET | `/api/schedule?since=YYYY-MM-DD` | 配信スケジュール |
| GET | `/api/approvals` / POST `/api/approvals/{id}` `{"approve": true}` | 承認待ちと承認・却下 |
| GET | `/api/monitor` | PC 状態 |
| GET | `/api/report` | 状況レポート（Markdown） |
| GET | `/api/characters/{id}/expertise` | キャラが覚えた知識（status: active=確定 / unverified=視聴者情報・未確認 / disputed=要確認） |
| POST | `/api/characters/{id}/expertise` `{"topic": "...", "content": "..."}` | オーナーが知識を直接教える（最優先） |

## 完了したら

オーナーに次を報告してください（オーナーが Atena 側に伝えます）。

- (a) ジョブキューは、もともと他プロセスの `.heavy.lock` を待つ作りでしたか？（Atena が置いたロックで止まるか）
- (b) キャラデータの保存場所と形式（`characters/<名前>/` 以下の構成）、感情ごとの声のデータの保存場所と形式
- (c) お願い 1〜3 それぞれの実装状況と、確認できたこと・できなかったこと
- (d) 立ち絵の感情差分・口開き差分をこのアプリで作れるか
