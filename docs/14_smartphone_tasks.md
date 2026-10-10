# スマホからできること・PC が必要なこと

いまの残作業を「スマホだけでできる」「PC が必要」に分けました。上から順に進めると、PC に向かったときの作業が短くなります。

## A. スマホだけでできること

| # | やること | どこで | メモ |
|---|---------|-------|------|
| 1 | **このセッションへの指示・判断**（改善案、人格の見直し案への返事、開発の追加依頼） | Claude アプリ | コードの変更・資料作りはすべてこちらで進められます |
| 2 | **ルーターで IP を固定**（Mac 192.168.0.105 / Windows 192.168.0.112 を DHCP 予約） | ルーターのアプリ、またはスマホのブラウザで管理画面（多くは http://192.168.0.1） | Windows の Ollama 設定の前に済ませておくと安心 |
| 3 | ✅ **Discord の準備**: 自分だけのサーバーと `#atena-lounge` を作る | Discord アプリ | フォーラムにするかもここで決める（ラウンジ1回＝1投稿にしたいならフォーラム） |
| 4 | ✅ **Discord の Webhook 作成**（詳しい手順: [15_discord_setup_phone.md](15_discord_setup_phone.md)）: ルームマスター・プロジェクトマネージャー・各キャラ（名前とアイコン） | Discord アプリ: チャンネルの設定 → 連携サービス → ウェブフック | アイコンは iCloud の立ち絵を選べます。URL はメモ（パスワード管理アプリ推奨）に保存。**URL はこのチャットに貼らない**（知っていれば誰でも投稿できるため） |
| 5 | **YouTube Data API の準備**: Google Cloud でプロジェクト作成 → YouTube Data API v3 を有効化 → API キー作成（API 制限を YouTube Data API v3 のみに） | スマホのブラウザ（PC 版表示にすると操作しやすい） | キーはメモに保存。OAuth 方式にする場合の JSON のダウンロードは PC で |
| 6 | **YouTube Studio の確認**: チャンネル説明に「AI キャラクターによる配信」の明記、パートナープログラムの状況 | YouTube Studio アプリ | 収益化の規約はオーナー確認済み |
| 7 | **他のセッションへの依頼文の受け渡し** | Claude アプリ | Mac / Windows で動いているセッションが「Remote Control」で開かれていれば、スマホの Claude アプリから依頼文を貼れます。開かれていなければ PC で |
| 8 | **報告の読み取り・転送**（キャラクリエイト、アプリ開発、Windows の各セッションからの報告） | Claude アプリ / iCloud | 報告をこのセッションに貼ってもらえれば、こちらで次の手を決めます |
| 9 | **資料を読む** | GitHub アプリ（Makinggamer/test の `atena-phase1` ブランチの `docs/`）または iCloud | 進捗一覧は `docs/00_status.md` |

## B. PC が必要なこと（PC に向かったときにまとめて）

所要時間の目安つき。上から順に。

| # | やること | PC | 時間 | 手順 |
|---|---------|----|------|------|
| 1 | Windows の Ollama 設定（依頼文を Windows の Claude Code に貼る） | Windows | 15 分 | `docs/13_windows_ollama.md`（Mac の IP 記入済み） |
| 2 | Atena を Mac に入れる → `atena doctor` | Mac | 20 分 | `docs/05_setup_mac_youtube.md` の 1。足りないものは doctor が教えます |
| 3 | Ollama のモデルを入れる（doctor に出たもの） | Mac | 待ち時間 | `ollama pull qwen2.5:7b` など |
| 4 | 設定ファイルに書き込む: オーナー情報、Discord の Webhook（A-4 のメモから）、YouTube の API キー（A-5）、Windows の Ollama（`batch_host`） | Mac | 10 分 | `config/owner_secrets.toml` / `config/discord_webhooks.toml` / `config/atena.toml` |
| 5 | Discord の接続テスト → ラウンジを 1 回 | Mac | 5 分 | `atena discord test` → `atena lounge` |
| 6 | 自動運転を有効にする（以後は見るだけ） | Mac | 5 分 | `atena autopilot-install` → `launchctl load -w ...` |
| 7 | Irodori の声の速度を測る・OBS に字幕と立ち絵を置く | Mac | 30 分 | `docs/05_setup_mac_youtube.md` の 3〜4.5 |
| 8 | YouTube で試験配信 | Mac | 30 分〜 | 同 5〜6 |

B-1〜6 が終われば、キャラ達が自動でラウンジを開いて Discord に流れ、毎朝の運営報告も届くようになります（以後はスマホで見るだけ）。

## C. 返事待ちのもの（届いたらこのセッションに貼るだけ）

- デスクトップアプリのセッション: 連携依頼（`docs/08`）の報告 (a)〜(d)
- キャラクリエイトのセッション: Sora・Mio の立ち絵の報告（立ち絵チェックの結果）
- Windows のセッション: Ollama の設定結果（生成速度・VRAM 使用量）
