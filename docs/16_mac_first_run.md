# Mac での初回実行（Mac の Claude Code に貼る依頼文）

このクラウドのセッションからは Mac を操作できないため、Mac で動く Claude Code のセッションに下の依頼文を貼ってください。
スマホからなら、Mac のセッションを Remote Control で開いて貼れます（開いていなければ Mac の前で）。

---

## 依頼文（ここから下を貼る）

Atena project（AI タレント事務所システム、https://github.com/Makinggamer/test の atena-phase1 ブランチ）を、この Mac で初めて動かしてください。オーナー承認済みの依頼です。各ステップで失敗したら、そこで止めて結果を報告してください。

### 1. 入れる

```bash
python3.12 --version || brew install python@3.12
if [ -d ~/atena/.git ]; then cd ~/atena && git pull origin atena-phase1;
else git clone -b atena-phase1 https://github.com/Makinggamer/test.git ~/atena && cd ~/atena; fi
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[monitor]"
atena init
```

### 2. iCloud の設定ファイルを Mac に下ろす

オーナーがスマホで iCloud Drive/Atena/ に `discord_webhooks.toml` と `youtube.toml` を置いています。**中身は表示・出力しないでください**（Webhook の URL と API キーです）。存在と同期だけ確認します。

```bash
D=~/Library/Mobile\ Documents/com~apple~CloudDocs/Atena
ls -la "$D"
brctl download "$D" 2>/dev/null; sleep 5; ls -la "$D"
```

ファイル名が `.txt` で終わっていたら、`.toml` に名前を変えてください（中身は開かない）。

### 3. キャラを Atena に登録する

Discord の Webhook は `room_master` / `manager` / `sora` / `mio` の 4 つです。Atena のキャラ ID を `sora` と `mio` にそろえます。

- `config/characters/` にあるサンプル（`sample_hikari.toml`, `sample_shizuku.toml`）は `config/characters/_samples/` に移す（ラウンジに出さないため。消さない）
- `sample_mio.toml`（id = "mio"）はそのまま使う。デスクトップアプリ（studio-chat、`~/vid2anime`）に Mio の人格・話し方の設定があれば、その内容で persona / speaking_style を更新する
- Sora は、アプリの Sora の設定（`~/vid2anime/characters/Sora/` やアプリのキャラ管理）から `config/characters/sora.toml` を作る。書式は `sample_mio.toml` を見本に。id = "sora"、name、persona、speaking_style。好きなもの・仕事が分かれば specialties / favorites も。voice_id はアプリの Irodori 設定の名前（分からなければ空）
- 立ち絵: `~/vid2anime/characters/Sora/avatar/` と `~/vid2anime/characters/Mio/avatar/` があれば、それぞれの `avatar_dir` に絶対パスで設定し、`atena avatar check sora` / `atena avatar check mio` で確認
- **アプリのコードは変更しない**（別セッションが作業中）
- 確認: `atena character list`

### 4. 点検とモデル

```bash
atena doctor
```

「✗ モデル …」と出たものを `ollama pull <名前>` で入れる（qwen2.5:14b は約 9GB で時間がかかるので、最後に。`~/vid2anime/.heavy.lock` があり持ち主が動いている間は、ダウンロードだけにして、以下のラウンジは待つ）。入れ終わったらもう一度 `atena doctor`。

### 5. Discord と最初のラウンジ

```bash
atena discord test                 # 各 Webhook からテスト投稿が 1 件ずつ出る
atena lounge sora mio --turns 6    # 会話が Discord の #ラウンジ に流れ、振り返りが #運営報告 に出る
```

### 6. 自動運転を有効にする（5 が成功した場合だけ）

```bash
atena autopilot --once
atena autopilot-install
launchctl load -w ~/Library/LaunchAgents/com.atena.autopilot.plist
```

### 7. オーナーに報告

- 各ステップの成否
- `atena doctor` の最終結果（URL やキーは含まれません）
- `atena discord test` の結果
- `atena lounge` の会話（画面に出たものをそのまま）と、かかった時間
- 動かなかったところのエラー全文（URL・キーが含まれる行は伏せる）
