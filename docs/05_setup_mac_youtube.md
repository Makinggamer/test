# セットアップ手順（M3 Mac + YouTube）

## 1. Atena 本体

```bash
# macOS 標準の python3 は古い（3.9）ので Homebrew で 3.12 を入れる
brew install python@3.12
cd ~/atena && python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[monitor]"     # psutil で CPU・メモリ計測を正確にする
atena init
```

`config/owner_secrets.toml` にオーナーの個人情報を記入します。これはガーディアンの検出リストとしてだけ使われ、キャラには渡りません。

## 2. Ollama

```bash
ollama pull qwen2.5:7b      # キャラ用（配信中に常駐）
ollama pull qwen2.5:3b      # ガーディアン判定用（配信中に常駐）
ollama pull qwen2.5:14b     # 配信していない時間のバッチ処理用

# 配信中にモデルが入れ替わってカクつかないように（Ollama アプリ再起動で反映）
launchctl setenv OLLAMA_MAX_LOADED_MODELS 2
launchctl setenv OLLAMA_KEEP_ALIVE 30m
```

モデルは `config/atena.toml` の `[ollama]` で変更できます。デスクトップアプリで作ったキャラ専用モデルを使う場合は、キャラ定義の `model` に指定します。

## 3. 読み上げ（Irodori-TTS）

デスクトップアプリで使っている Irodori-TTS を、生配信用に **Irodori-TTS-Server**（Aratako 氏公開、OpenAI 互換 API）経由で呼びます。
アプリ内の声の生成（感情分析・声の加工込み）は1台詞に数十秒〜数分かかるため、生配信では加工を省いた軽い経路を使います。

1. Irodori-TTS-Server を導入して起動します（既定ポート 8088）。Mac では `IRODORI_MODEL_DEVICE=mps` を指定します。
   ```bash
   # 手順の詳細はサーバーの README を参照
   IRODORI_MODEL_DEVICE=mps IRODORI_CODEC_DEVICE=mps \
     uv run --no-sync python -m irodori_openai_tts --host 127.0.0.1 --port 8088
   ```
   README の例は `--host 0.0.0.0` ですが、同じ Mac からしか使わないので `127.0.0.1` にしてください（家のネットワークに公開しない）。
2. キャラの参照音声をサーバーの `voices/` に置きます。デスクトップアプリの `~/vid2anime/characters/Sora/voice.wav` なら `voices/Sora.wav` としてコピー（ファイル名が voice_id になります）。
3. キャラ定義に `voice_id` と `voice_caption` を設定します（アプリから API で同期する場合はアプリ側で設定）。
   ```bash
   atena voice list                 # サーバーが認識している voice_id
   atena voice test sora "テストです"
   atena voice bench sora           # 生配信に使える速さか計測
   ```
4. `voice bench` の結果で使い方を決めます。
   - **3秒以内**: そのまま生配信で使えます。
   - **3〜8秒**: 使えますが返答に間が空きます。`irodori_num_steps` を下げる（音質と引き換え）か、返答を短くします。
   - **8秒超**: 返答の多くが字幕のみになります。`irodori_num_steps` を下げる、返答を短くする、配信中に重い処理を動かさない、で詰めてください。
   - Ollama と同じ GPU を使うので、**配信と同じ状態（Ollama でモデルを読み込んだ状態）で測ってください**。
5. **ボイストラブル時の動作**: 途中で声が変わらないよう、予備の声は使いません。合成に失敗するか `irodori_timeout_sec`（既定30秒）を超えたら、
   画面に「ただいまボイストラブル中のため、字幕でお届けしています」を出して字幕のみで続け、60秒ごとに声を再挑戦します。復帰したら注意書きは消えます。
   文言は `[voice] trouble_notice` で変えられます。

**ライセンスの確認（収益化前に必須）**: サーバーのコードは MIT ですが、**モデルの重みは別ライセンス**で、Hugging Face のモデルカードで確認するよう案内されています。版によって非商用の条件が付いているという情報もあります。投げ銭・広告収益のある配信や、ボイス・ASMR の販売に使えるか、使っている版のモデルカードで必ず確認してください。参照音声（声のもと）についても、本人の許可がある声か、商用利用できる素材かを確認してください。

## 4. OBS

- 配信設定のエンコーダは **Apple VT H264 ハードウェアエンコーダ** を選びます（CPU 負荷を下げるため）。
- 字幕: ソース追加 → テキスト → 「ファイルから読み取り」→ `data/obs/subtitle.txt`
- 読み上げ中のコメント: 同様に `data/obs/comment.txt`
- 注意書き（ボイストラブル中など）: 同様に `data/obs/notice.txt`。普段は空なので何も表示されません。画面上部など目立つ位置に置いてください
- 配信の字幕は終了時に `data/streams/<日時>-<キャラ>.srt` に保存されます。切り抜き動画の字幕に使えます
- 音声: 読み上げは Mac の標準出力から再生されます。OBS で「macOS 音声キャプチャ」等でデスクトップ音声を取り込みます。

## 4.5 アバター

Live2D モデルがあれば VTube Studio、無ければ内蔵の PNGTuber 表示を使います（詳細と採用理由は [06_existing_systems.md](06_existing_systems.md)）。

- **PNGTuber**: キャラの `avatar_dir` に感情ごとの立ち絵と口開き差分（`neutral.png` / `neutral_open.png` / `joy.png` / …）を置き、`[avatar] engines = ["pngtuber"]`。OBS にブラウザソース `http://127.0.0.1:8771/` を追加します（背景は透過されます）。
- **VTube Studio**: `pip install -e ".[avatar]"`、VTube Studio の設定で API を有効化（ポート 8001）、`[avatar] engines = ["vtube_studio"]`、`atena avatar vts-auth` で許可。表示されたホットキー名をキャラの `vts_hotkeys` に感情ごとに設定します。口パクは Atena が送るので、VTube Studio のマイク口パクは切っておいてください。
- 確認: `atena avatar test <キャラ> --hold`
- 本番の立ち絵ができるまでは `atena avatar placeholder <キャラ>` で仮の立ち絵（7感情×口の開閉）を使えます。本番の立ち絵の作り方は [07_avatar_assets_request.md](07_avatar_assets_request.md)。揃い具合は `atena avatar check <キャラ>`

## 5. YouTube 接続

どちらか一方を選びます。

### A. API キー方式（手軽）

1. Google Cloud Console でプロジェクトを作成し、**YouTube Data API v3** を有効化します。
2. 「認証情報」→「API キー」を作成し、API 制限を YouTube Data API v3 のみにします。
3. `config/atena.toml` の `[youtube] api_key` に設定します。
4. 配信を開始したら、動画 ID（URL の `watch?v=` の後ろ）を指定して接続します。
   ```bash
   atena youtube live hikari --video XXXXXXXXXXX --tts --hours 2
   ```

### B. OAuth 方式（自分の配信を自動検出）

1. 同じプロジェクトで「OAuth 同意画面」を設定し、自分のアカウントをテストユーザーに追加します。
2. 「OAuth クライアント ID」→ 種類「デスクトップアプリ」で作成し、JSON を `config/client_secret.json` に保存します（git 管理外）。
3. `[youtube] client_secret_file = "config/client_secret.json"` を設定します。
4. 初回だけ認証します。要求する権限は**読み取りのみ**（youtube.readonly）です。
   ```bash
   atena youtube auth
   atena youtube live hikari --tts --hours 2      # 配信中の枠を自動検出
   ```

### API 割り当て（クォータ）について

- YouTube Data API は既定で **1日 10,000 ユニット**（太平洋時間の0時にリセット）です。
- チャット取得は1回ごとにユニットを消費します。Atena は「予定配信時間（`--hours`）の間、割り当てが持つ間隔」でポーリングを自動調整します。配信が長いほど、コメントへの反応は少し遅くなります。
- 1回あたりの消費量は `[youtube] poll_cost` で設定します。Google の公式料金表（Quota Calculator）で `liveChatMessages.list` の値を確認して合わせてください。
- 残量確認: `atena youtube quota`

### スーパーチャットの扱い

- 届いたスパチャは自動で収益台帳に記録されます（`[youtube.fx_rates]` で円換算。**YouTube の手数料を引く前の金額**）。
- 換算レートが無い通貨は経理タスクとして起票されるので、手入力してください。
- メンバーシップは API で金額が取れないため、YouTube Studio の収益レポートから月次で手入力します（`atena revenue add <キャラ> membership <金額>`）。

## 5.5 キャラの知識（好きなもの・仕事）

キャラ定義に `specialties`（仕事・専門）と `favorites`（好きなもの）を書くと、そのキャラが自分で知識を集めます（サンプル: `config/characters/sample_mio.toml`）。

```bash
atena learn mio                 # Wikipedia・登録サイトから学習、コメントで教わった内容の抽出と裏付け、容量整理
atena expertise list mio        # 覚えている知識（確定 / 未確認 / 要確認）
atena expertise add mio 古書 "..."   # オーナーが登録（最優先）
atena expertise stats
```

- `atena daily` でも毎日自動で学習します。学習は配信していない時間に実行してください（14B モデルを使うため）。
- 視聴者が教えてくれた内容は「未確認」として覚え、配信では「〜って教えてもらったんだけど」と断定せずに話します。Web で裏付けが取れたら確定、Web と食い違えば Web の情報を優先します。
- 配信中にコメントが 40 秒途切れると、好きなもの・専門の知識から1つ選んで話し、最後に問いかけてコメントを促します（`[stream]` で調整）。

## 6. 配信の流れ

```bash
atena daily                         # キャラが企画を出し、マネージャーが翌日の枠を仮押さえ
atena approvals list                # 承認待ちを確認
atena approvals approve 3           # 配信枠を確定
# 当日
atena youtube live hikari --schedule 5 --tts --avatar --hours 2
#   開始前: PC 状態チェック（ダメなら中止）
#   配信中: コメント応答・スパチャのお礼・字幕・60秒ごとの負荷監視（高負荷が続くと自動で締め）
#   終了後: 記憶に要約を保存、配信枠を done に
atena report
```

## 7. YouTube の規約について（収益化前に必ず確認）

- スーパーチャット等の収益機能を使うには YouTube パートナープログラムへの参加が必要です。
- AI キャラクターであることは概要欄・チャンネル説明に明記してください（事務所憲章でもキャラ自身に偽らせない設定にしています）。
- 同じような内容の量産は収益化の対象外になり得ます。企画プロデューサー（日次サイクルのネタ被りチェック）で企画の多様性を保ちます。
- 規約は変わるので、収益化の申請前にオーナーが最新の YouTube ヘルプを確認してください。
