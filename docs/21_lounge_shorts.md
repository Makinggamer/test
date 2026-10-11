# ラウンジの会話から縦型ショートを作る

ラウンジで話しているキャラを左右に立たせ、台詞に合わせて声・口パク・表情をつけた「会話を切り取った風」の縦型ショート（約 1 分）を作ります。

## できあがり

| 場所 | 内容 |
|------|------|
| 上 | 「Atena project ラウンジより」とタイトル（LLM が会話から付ける） |
| 中央 | 2 人の立ち絵。話している方は手前・明るく・少し大きく弾む。聞いている方は少し暗く小さい。口パク（声の音量）・まばたき・台詞ごとの表情 |
| 下 | 名前の札つきの字幕（左のキャラはピンク、右は青） |
| 一番下 | 「※AI キャラクター同士の会話です」 |

- 書き出し: `data/shorts/short-日時-キャラ.mp4`（1080x1920・30fps・H.264/AAC）と、台詞・時刻の `.json`
- 声ありの完成品は、公開をオーナー承認待ち（L3）に出します。投稿はしません

## Mac での準備（1 回だけ）

```bash
cd ~/atena && git pull origin atena-phase1 && source .venv/bin/activate
pip install -e ".[shorts]"        # Pillow（画像の合成）
```

フォントは Mac のヒラギノを自動で使います。背景画像を使うなら `config/atena.toml` の `[shorts] background` に画像のパスを書きます。

## 作り方

```bash
atena shorts make --no-voice      # まず声なしで試作（Irodori 不要・承認にも出さない）
atena shorts make                 # 声あり（Irodori-TTS-Server を起動しておく）
atena shorts make --session 20261010-222514-8ebb --seconds 45   # 会話と長さを指定
atena shorts list
```

- 会話を指定しなければ、まだ使っていない最新の会話から選びます（立ち絵がそろった 2 人の会話を優先）
- 声は各キャラの `voice_id` と感情ごとの喋り方（`voice_captions`）を使います
- PC が重いとき（配信中・高負荷）は始めません。`--force` で強制

## 立ち絵

`avatar_dir` のフォルダ（PNGTuber と同じ `neutral.png` / `joy_open.png` / `neutral_blink.png` …）を使います。無いキャラは仮の立ち絵になります。
Misaki は口を開けた絵の不具合（顎の白い染み）が直るまで、自動では選ばれにくくしてあります。

## これから

- 自動運転で 1 日 1 本作り、#運営報告 に知らせる
- BGM・効果音、話題に合わせた背景
