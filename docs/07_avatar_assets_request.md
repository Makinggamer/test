# 本番の立ち絵（PNGTuber 差分）制作の依頼文

立ち絵の生成には Mac 上の ComfyUI・LoRA が必要なため、Atena のクラウドセッションからは作れません。
Sora を作っているキャラクリエイトのセッション（LoRA・ComfyUI の環境を知っているので最適）に、下の「依頼文」から下を貼ってください。
そのセッションが無い場合は、Mac で新しい Claude Code セッションを開いて貼ります。

```bash
cd ~/vid2anime && claude
```

それまでは仮の立ち絵で配置や口パクを確認できます。

```bash
atena avatar placeholder sora     # 仮の立ち絵 28 枚を作って sora に設定
atena avatar test sora --hold     # OBS で表示・口パク・表情切替を確認
```

---

## 依頼文（ここから下を貼る）

Atena project（AI タレント事務所システム、https://github.com/Makinggamer/test の atena-phase1 ブランチ）の配信アバター用に、キャラクター Sora の PNGTuber 用立ち絵セットを作ってください。オーナー承認済みの依頼です。

### 作るもの

`~/vid2anime/characters/Sora/avatar/` に、透過 PNG を 28 枚（7 感情 × 口の開閉 × 目の開閉）。

| 感情 | 基本（目開き・口閉じ） | 口開き | 目閉じ（まばたき） | 目閉じ＋口開き |
|------|------|------|------|------|
| ふつう | neutral.png | neutral_open.png | neutral_blink.png | neutral_blink_open.png |
| うれしい | joy.png | joy_open.png | joy_blink.png | joy_blink_open.png |
| 照れ | shy.png | shy_open.png | shy_blink.png | shy_blink_open.png |
| 悲しい | sad.png | sad_open.png | sad_blink.png | sad_blink_open.png |
| 心配 | worry.png | worry_open.png | worry_blink.png | worry_blink_open.png |
| 怒り | angry.png | angry_open.png | angry_blink.png | angry_blink_open.png |
| 驚き | surprise.png | surprise_open.png | surprise_blink.png | surprise_blink_open.png |

任意（余裕があれば）: 口を半分開けた `<感情>_half.png` と `<感情>_blink_half.png`。小さな声のときに使い、口パクが自然になります。

優先順位: 基本と口開きの 14 枚 → 目閉じの 14 枚 → 半開き。途中まででも使えます（目閉じが無い感情はまばたきしないだけ）。

### 条件（配信で切り替えたときに不自然にならないため）

- すべて**同じキャンバスサイズ・同じ構図・同じ位置**（バストアップ、正面〜やや斜め、背景透過）。切り替えで体や顔の位置がずれないこと
- まず neutral.png を Sora の LoRA で作り、ほかの感情は **neutral.png をもとに顔（表情）だけを変える**（img2img / インペイント）。髪型・服・体はそのまま
- `_open` は、同じ感情の絵の**口だけ**を開けた差分（口の部分だけインペイント）。口以外は 1 ピクセルも動かさないのが理想
- `_blink` は、同じ感情の絵の**目だけ**を閉じた差分（目の部分だけインペイント）。`_blink_open` は `_open` の目だけを閉じたもの。表示側でまばたきの瞬間だけ差し替えるので、目以外が動くとちらつきます
- うれしい（にっこり目）など、もともと目が細い表情は、閉じ目との差が小さくても構いません
- 解像度の目安は 1024×1024 程度。OBS で縮小して使う

### 進め方の注意

- Sora の LoRA 学習が終わってから着手してください。`~/vid2anime/.heavy.lock` があり、持ち主のプロセスが生きている間は重い処理を始めないでください（Atena が配信中に置くロックも同じファイルです）
- studio-chat のコードは別セッション（「Ollama デスクトップアプリ開発」）が作業中です。**アプリのコードは変更せず**、既存の道具（ComfyUI の API、既存のスクリプト）を使うか、独立したスクリプト（例: `scripts/avatar_set.py`）を新しく作ってください
- できた絵は、オーナーが目で見て確認できるよう一覧画像（全部を感情ごとに並べた 1 枚）も作ってください
- 動き（呼吸のゆれ・話すときの弾み・表情のフェード）は Atena の表示側で付けるので、絵には不要です

### 完了したら

1. Atena 側で揃っているか確認: `atena avatar check sora`（Atena の作業ディレクトリで実行）
2. Atena にキャラの立ち絵フォルダを登録（Atena の API が起動していれば）:
   `curl -X PUT -H "Authorization: Bearer $(cat <Atenaのディレクトリ>/data/api_token)" -d '{"name":"Sora","avatar_dir":"'$HOME'/vid2anime/characters/Sora/avatar"}' http://127.0.0.1:8770/api/characters/sora`
   （API が起動していなければ、Atena の `config/characters/` のキャラ定義に `avatar_dir = "..."` を追記）
3. 結果をオーナーに報告（できた枚数、一覧画像の場所、使ったモデル名）

### 権利面

画像生成モデル・LoRA の商用利用はオーナー確認済みです。記録のため、使ったモデル名・LoRA 名だけ報告してください。
