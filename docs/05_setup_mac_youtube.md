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

## 3. VOICEVOX（読み上げ）

1. VOICEVOX の Mac 版をインストールして起動します（エンジンが `http://127.0.0.1:50021` で動きます）。
2. 話者 ID を調べ、キャラ定義に設定します。
   ```bash
   atena voice speakers
   # config/characters/hikari.toml に voice_speaker = 3 のように追記（アプリ連携なら API で設定）
   atena voice test hikari "テストです"
   ```
3. **クレジット表記**: VOICEVOX の音声を使う動画・配信には、各キャラクターの利用規約に従ったクレジット（例: 「VOICEVOX:ずんだもん」）が必要です。キャラごとに商用利用の条件が違うので、収益化前に権利・規約チェッカー（オーナー）が確認してください。

## 4. OBS

- 配信設定のエンコーダは **Apple VT H264 ハードウェアエンコーダ** を選びます（CPU 負荷を下げるため）。
- 字幕: ソース追加 → テキスト → 「ファイルから読み取り」→ `data/obs/subtitle.txt`
- 読み上げ中のコメント: 同様に `data/obs/comment.txt`
- 音声: VOICEVOX の再生は Mac の標準出力から出ます。OBS で「macOS 音声キャプチャ」等でデスクトップ音声を取り込みます。

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

## 6. 配信の流れ

```bash
atena daily                         # キャラが企画を出し、マネージャーが翌日の枠を仮押さえ
atena approvals list                # 承認待ちを確認
atena approvals approve 3           # 配信枠を確定
# 当日
atena youtube live hikari --schedule 5 --tts --hours 2
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
