# 常時運転・Misaki の加入・「加入」チェック（Mac に貼る依頼文 2 つ）

このクラウドのセッションからは Mac を操作できないため、依頼文を 2 つに分けています。

| 依頼 | 貼る先 | 内容 |
|------|-------|------|
| A | Atena を動かしているセッション（キャラクリエイトのセッションなど） | 最新版に更新し、常時運転に切り替え、Misaki を Atena に登録する |
| B | デスクトップアプリ（studio-chat）を開発しているセッション | キャラ管理に「加入」チェックを付け、Atena に同期する |

A だけでも常時運転と Misaki の参加は始まります。B は、チェックでの出し入れをアプリからできるようにするためのものです。

---

## 依頼 A（ここから「依頼 B」の手前までを貼る）

オーナー承認済みの依頼です。Atena project（`~/atena`）を最新版にして、ラウンジを常時運転に切り替え、新しいキャラ Misaki を加入させてください。各ステップで失敗したら、そこで止めて報告してください。

### 1. 最新版にする

```bash
cd ~/atena && git pull origin atena-phase1 && source .venv/bin/activate && pip install -e ".[monitor]" -q
```

`config/atena.toml` を手元で書き換えていて pull が止まった場合は、変更を消さずに、差分（`git diff config/atena.toml`）を報告してください（Webhook の URL や API キーの行があれば伏せる）。

更新後の `config/atena.toml` は、次の設定になっています（確認だけ。書き換え不要）。

- `[autopilot] continuous = true` / `break_min = 5`（1 回終わるごとに約 5 分休んで次の回）
- `active_start = active_end = "00:00"`（一日中）
- `[lounge] review_every = 6`（振り返りは 6 回に 1 回）

### 2. Misaki を Atena に登録する

- **アプリのコードは変更しない**
- デスクトップアプリ（`~/vid2anime`）のキャラ管理にある Misaki の設定から、`config/characters/misaki.toml` を作る
  - 書式は `config/characters/sample_mio.toml` を見本にする
  - 入れる項目: id = "misaki"、name、persona、speaking_style、分かれば specialties / favorites / voice_id / voice_captions
  - 立ち絵: `~/vid2anime/characters/Misaki/avatar/` があれば、`avatar_dir` に絶対パスで入れ、`atena avatar check misaki` で確認する
- アプリに Atena への同期機能（`PUT http://127.0.0.1:8770/api/characters/misaki`）があり動くなら、ファイルを手で作る代わりにそれを使ってよい
- 確認:

```bash
atena character list                    # sora / mio / misaki が「加入」と出ること
atena character deepen misaki           # Misaki の設計書（一人称・口調など）を作る。結果は #運営報告 にも出る
atena lounge sora mio misaki --turns 9  # 3 人で 1 回話させて確認
```

### 3. 自動運転を再起動する（常時運転を反映）

```bash
launchctl unload -w ~/Library/LaunchAgents/com.atena.autopilot.plist
launchctl load -w ~/Library/LaunchAgents/com.atena.autopilot.plist
sleep 90; tail -n 20 ~/atena/data/autopilot.log
```

ログに「lounge: …」と出て、Discord の #ラウンジ に会話が流れ始めれば成功です。会話が終わってから約 5 分後に、次の回が始まります。

### 4. オーナーに報告

- 各ステップの成否
- `atena character list` の結果
- Misaki の設計書（deepen の出力）
- 3 人のラウンジの会話（画面に出たものをそのまま）
- `autopilot.log` の最後の 20 行（URL やキーが含まれる行は伏せる）

**注意**

- Discord の `discord_webhooks.toml` や `youtube.toml` の中身は、表示も出力もしないでください。
- Misaki 専用の Webhook がまだ無くても、Misaki の発言は共用の Webhook から「Misaki の名前」で投稿されます。

---

## 依頼 B（ここから下を、アプリ開発のセッションに貼る）

オーナー承認済みの依頼です。デスクトップアプリ（studio-chat、`~/vid2anime`）のキャラクター管理画面に、**「Atena project に加入」チェックボックス**を追加してください。

### 仕様

- キャラごとにチェックボックスを 1 つ追加する。ラベルは「Atena project に加入（ラウンジ・Discord に参加）」
- 状態はアプリのキャラ設定に保存する
- 既存のキャラの初期値:
  - Sora、Mio、Misaki はチェックあり
  - それ以外のキャラはチェックなし
- 同期: Atena への同期（`PUT http://127.0.0.1:8770/api/characters/<id>`、詳しくは `docs/08_desktop_app_request.md`）の本文に、`"member": true / false` を入れる
  - チェックを切り替えたら、その場で同期する
  - Atena 側では、false のキャラはラウンジ・Discord・自動運転に出なくなる（キャラの登録は残る）
  - チェックを入れたキャラが Atena にまだ登録されていなければ、同じ PUT で登録される（name は必須。persona など他の項目も一緒に送る）
- 同期に失敗しても、アプリの保存は成功させ、「未同期」と表示する（既存の同期と同じ扱い）
- Atena が動いていないときの同期エラーで、アプリが止まらないようにする

### 確認とオーナーへの報告

1. Misaki のチェックを外して同期し、`curl -s -H "Authorization: Bearer $(cat ~/atena/data/api_token)" http://127.0.0.1:8770/api/characters` で `"member": false` になっていることを確かめる
   - トークンの中身は表示しない
   - Atena の API が起動していなければ、`cd ~/atena && .venv/bin/atena serve &` で起動する
2. チェックを戻して `"member": true` に戻ることを確かめる
3. オーナーに報告する:
   - 変更したファイル
   - 画面のどこにチェックが付いたか
   - 確認の結果
