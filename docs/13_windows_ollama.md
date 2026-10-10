# Windows PC にラウンジの AI 計算を任せる（案2）

## 方針（確認結果から）

| 項目 | 結果 | 判断 |
|------|------|------|
| GPU | GTX 1660 SUPER / VRAM 6GB | 7B モデル（約 4.7GB）が GPU に載る。M3 より速い見込み。3B と 7B を同時には載せられないので、Windows では 7B だけを使う |
| RAM | 16GB | 7B には十分。14B は VRAM に載らず遅いので、14B を使う毎朝の日次サイクルは Mac のまま（配信していない時間） |
| 空き容量 | D:（NVMe SSD）168GB | モデルは **D:** に置く（C: は HDD で読み込みが遅い） |
| スリープ | なし | 常時待ち受けできる |

- **Mac で動かすもの**: Atena 本体（記憶・知識・記録はすべて Mac）、配信、毎朝の日次サイクル、Discord 投稿
- **Windows で動かすもの**: Ollama（qwen2.5:7b）だけ。ラウンジの会話・ガーディアンの判定・振り返りの AI 計算
- Windows に無いモデル（アプリで作ったキャラ専用モデル、判定用 3B、14B）は 7B で代用します。キャラの人格は Atena がプロンプトで渡すので、キャラとして話せます
- Windows に繋がらないとき: Mac が空いていれば Mac で続け、配信中なら見送ります
- 配信中でも、AI の計算は Windows なのでラウンジを開けます（Mac の GPU を取り合わない）
- **Windows でゲームをしている間**は GPU を取り合うので、ゲームが重くなります。遊ぶときはタスクトレイの Ollama を終了してください（その間は Mac で続けるか見送り）

## オーナーの作業

1. ルーターの設定で、Windows PC（192.168.0.112）と Mac の IP を固定（DHCP 予約）にする。変わると繋がらなくなるため
2. Mac の IP を調べる: Mac のターミナルで `ipconfig getifaddr en0`（有線なら `en1` の場合も）
3. 下の「依頼文」の `<MacのIP>` を書き換えて、Windows PC の Claude Code に貼る
4. Windows 側の報告が来たら、Mac の `config/atena.toml` の `[ollama]` に追記:
   ```toml
   batch_host = "http://192.168.0.112:11434"
   ```
5. Mac で確認: `curl http://192.168.0.112:11434/api/tags` でモデル一覧が出れば OK。続けて `atena lounge` を 1 回

---

## 依頼文（ここから下を貼る）

Atena project（https://github.com/Makinggamer/test の atena-phase1 ブランチ）のために、この Windows PC に Ollama を入れて、同じ家の Mac（IP: `<MacのIP>`）からだけ使えるようにしてください。オーナー承認済みの依頼です。

### 1. Ollama のインストールとモデルの置き場所

1. 公式サイト（https://ollama.com/download/windows）から Ollama for Windows を入れる
2. モデルを **D: の NVMe SSD** に置くため、ユーザー環境変数を設定する（C: は HDD で遅い）
   ```powershell
   New-Item -ItemType Directory -Force D:\ollama\models | Out-Null
   setx OLLAMA_MODELS "D:\ollama\models"
   setx OLLAMA_HOST "0.0.0.0:11434"
   setx OLLAMA_KEEP_ALIVE "30m"
   setx OLLAMA_MAX_LOADED_MODELS "1"
   ```
3. タスクトレイの Ollama を終了して起動し直し（環境変数を反映）、モデルを入れる
   ```powershell
   ollama pull qwen2.5:7b
   ```

### 2. ファイアウォール（Mac からだけ許可）

`OLLAMA_HOST=0.0.0.0` で家のネットワークに開くので、Mac 以外からは繋がらないようにします。Ollama には認証が無いため必須です。

1. ネットワークの種類が「プライベート」になっていることを確認（`Get-NetConnectionProfile`）
2. 管理者の PowerShell で、Mac の IP からの 11434 番だけを許可する
   ```powershell
   New-NetFirewallRule -DisplayName "Ollama (Atena Mac only)" -Direction Inbound -Protocol TCP `
     -LocalPort 11434 -RemoteAddress <MacのIP> -Action Allow -Profile Private
   ```
3. Ollama 起動時に Windows のファイアウォールの許可ダイアログが出た場合は**許可しない**（許可するとどの機器からでも繋がるルールができる）。すでにできていれば無効にする
   ```powershell
   Get-NetFirewallRule | Where-Object { $_.DisplayName -like "*ollama*" } |
     Format-Table DisplayName, Enabled, Direction, Action, Profile
   # 上で作ったルール以外で Inbound / Allow のものがあれば:
   # Disable-NetFirewallRule -DisplayName "<そのルール名>"
   ```

### 3. 動作確認

```powershell
ollama list
ollama run qwen2.5:7b --verbose "自己紹介を一文で"   # eval rate（tokens/s）を記録
nvidia-smi                                          # 実行直後に、VRAM 使用量と ollama が GPU を使っているか
netstat -ano | findstr 11434                        # 0.0.0.0:11434 で待ち受けているか
```

### 4. やらないこと

- 上記以外のファイアウォール・ルーターの設定変更
- Python や Atena のインストール（この PC では Ollama だけを使います）
- ゲームの削除（空き容量は足りています）

### 5. オーナーに報告

- Ollama のバージョン、`ollama list` の結果
- `eval rate`（tokens/s）と、実行中の VRAM 使用量（GPU で動いているか）
- 作ったファイアウォールのルールと、無効にした Ollama のルール（あれば）
- 待ち受けの確認結果（`0.0.0.0:11434`）
