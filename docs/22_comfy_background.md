# ショートの背景を ComfyUI で作る（Mac に貼る依頼文）

ラウンジのショートの背景を、会話の話題に合わせて ComfyUI で自動生成します。
Atena が話題から背景のプロンプト（英語のタグ・人物なし・文字なし）を作り、ComfyUI に送って 1 枚作ります。同じ話題の背景は作り置きを使います。

## 仕組み

| 項目 | 内容 |
|------|------|
| 生成サイズ | 768x1344（9:16 に近い SDXL の解像度）→ 動画では 1080x1920 に広げて使う |
| プロンプト | 共通の画風（アニメ調の背景・人物なし・ラウンジの室内・やわらかい光）＋ 話題に合わせたタグ |
| 除外 | 人物・顔・手・文字・ロゴ・透かし・低画質 |
| ワークフロー | `config/comfy_background.json`（API 形式）。無ければ、標準ノードだけの SDXL 用を使う |
| 差し替える文字 | `%PROMPT%` `%NEGATIVE%` `%SEED%` `%WIDTH%` `%HEIGHT%` `%CKPT%` |
| 重い処理 | `~/vid2anime/.heavy.lock` があれば作らない。作る間は自分がロックを置く |
| 失敗したとき | ComfyUI が止まっていても、ショートはグラデーションの背景で作る |
| 保存先 | `data/shorts/backgrounds/bg-*.png`（プロンプトと seed を同名の .json に残す） |

---

## 依頼文（ここから下を、ComfyUI を使っている Mac のセッションに貼る）

オーナー承認済みの依頼です。Atena（`~/atena`）のショートの背景を ComfyUI で作れるようにしてください。

### 1. 更新

```bash
cd ~/atena && git pull origin atena-phase1 && source .venv/bin/activate && pip install -e ".[shorts]"
```

（手元の変更があれば、いつもどおり控えてから当て直す）

### 2. ComfyUI のつなぎ先とモデル

- ComfyUI の URL（ポート）を確かめ、違えば `config/atena.toml` の `[shorts] comfy_host` を直す
- 背景に向くアニメ調の SDXL モデルを 1 つ選び、`comfy_checkpoint` にファイル名を書く（`models/checkpoints/` にあるもの）
  - キャラの LoRA は使わない（背景に人物を出さないため）
  - 商用利用できるライセンスのモデルを選ぶ。ライセンス名を報告に書く

### 3. ワークフロー（任意。画質を上げたいとき）

標準ワークフローで足りなければ、ComfyUI で背景用のワークフローを組みます。そのうえで「Export (API)」で書き出し、`config/comfy_background.json` に置いてください。

- 正のプロンプトの文字を `%PROMPT%`、負のプロンプトを `%NEGATIVE%` にする
- seed を `"%SEED%"`、幅・高さを `"%WIDTH%"` `"%HEIGHT%"` にする（文字列のまま。Atena が数値に置き換えます）
- 最後に SaveImage ノードを置く
- アップスケーラーなどは自由に足してよい（最後の画像が 1080x1920 以上なら理想）

### 4. 試す

```bash
atena shorts bg "夕焼けが赤い理由"     # 背景を 1 枚作る → data/shorts/backgrounds/
```

できた画像に人物・文字が入っていないか確かめる。入っていたら、モデルかワークフローを見直す。

### 5. ショートで使う

`config/atena.toml` の `[shorts] background = "comfy"` にする。そのうえで次を実行する。

```bash
atena shorts make --no-voice
```

背景つきの試作ができたら、mp4 と背景の画像を iCloud Drive/Atena/shorts/ にコピーする。

### 6. オーナーに報告

- 使ったモデルとライセンス
- 生成にかかった時間
- 背景の画像（iCloud に置いた場所）
- 気になった点（人物が出る、文字が出る、暗すぎる など）
