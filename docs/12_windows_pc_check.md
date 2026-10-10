# Windows PC の確認依頼（Atena の AI 計算を任せられるか）

Windows PC で Claude Code のセッションを開き（Claude デスクトップアプリ、または PowerShell で `claude`）、下の「依頼文」から下を貼ってください。

---

## 依頼文（ここから下を貼る）

Atena project（AI タレント事務所システム、https://github.com/Makinggamer/test の atena-phase1 ブランチ）のための確認です。オーナー承認済みの依頼です。
いまは Mac（M3・24GB）で、配信と AI キャラの会話（Ollama）を両方動かしています。キャラ同士の会話（ラウンジ）や日次の処理の AI 計算を、この Windows PC の Ollama に任せられるかを調べてください。

### 1. 確認すること（読み取りだけ。設定は変えない）

| 項目 | 調べ方の例 |
|------|-----------|
| GPU の型番と VRAM 容量 | `nvidia-smi`（無ければ `Get-CimInstance Win32_VideoController`） |
| メモリ（RAM）容量 | `Get-CimInstance Win32_ComputerSystem` |
| CPU | `Get-CimInstance Win32_Processor` |
| 各ドライブの空き容量 | `Get-PSDrive -PSProvider FileSystem` |
| Ollama が入っているか、バージョン、入っているモデル | `ollama --version` / `ollama list` |
| Python のバージョン | `py --version` |
| この PC の家の中の IP アドレス | `ipconfig`（Wi-Fi か有線のアダプタの IPv4） |
| スリープの設定 | `powercfg /query SCHEME_CURRENT SUB_SLEEP` |

### 2. 空き容量が足りない場合だけ: 古いゲームの削除

Ollama のモデル置き場には **20GB 以上の空き**が必要です（qwen2.5 の 7B・3B・14B で約 16GB と余裕分）。足りない場合だけ、次の手順で空けてください。

1. ゲームの一覧を作る: Steam・Epic・その他ランチャーごとに、ゲーム名・容量・**最後に遊んだ日**
   - Steam は各ライブラリの `steamapps\appmanifest_*.acf` の `LastPlayed`（UNIX 時刻。0 は不明）や `userdata\<ID>\config\localconfig.vdf` の `LastPlayed` で確認
   - 最後に遊んだ日が分からないゲームは「不明」とし、**削除しない**
2. 削除してよいのは、**最後に遊んだ日が 1 年以上前だと確認できたゲームだけ**です（オーナー承認済み）。必要な空き容量に届くまで、容量の大きいものから順に
3. 削除は**ランチャーのアンインストール機能**で行う（Steam なら `steam://uninstall/<AppID>`、またはライブラリから「アンインストール」）。フォルダを直接消さない
4. セーブデータのフォルダ（`ドキュメント\My Games`、`AppData` 内など）には触れない
5. 削除したゲームの一覧（名前・容量・最後に遊んだ日）を記録する

### 3. まだやらないこと

- Ollama のインストールやモデルのダウンロード、ネットワーク・ファイアウォールの設定変更はしないでください（結果を見て Atena 側で方式を決めてから、別途依頼します）
- Atena のリポジトリの変更はしないでください

### 4. オーナーに報告

- 1 の表（GPU・VRAM・RAM・CPU・空き容量・Ollama・Python・IP・スリープ設定）
- 2 を行った場合: 削除したゲームの一覧と、空いた容量。行わなかった場合はその理由（空きが足りていた等）
- 1 年以上遊んでいないか分からなかったゲームがあれば、その一覧（削除していないもの）
