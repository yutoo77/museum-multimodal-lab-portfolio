# Windowsでの準備・起動

このページは **Museum Multimodal Lab** のローカル実行手順です。既存の Museum Evidence Lab の環境・DB・プロセスは共用しません。まず自作の架空資料だけで試してください。

## 必要なもの

- Windows 11、64bit の Python 3.11（`py -3.11` が使えること）
- Node.js / npm、Ollama
- モデル重みを保存できる空き容量。ダウンロードは初回の**準備時**に行い、アプリ実行時の外部AI API送信とは区別します。

既定のモデルは [`.env.example`](../.env.example) に記載されています。`.env.example` は自動読み込みされません。モデル名を変える場合は起動前に `MML_ANSWER_MODEL`、`MML_EMBEDDING_MODEL`、`MML_VISION_MODEL`、`MML_ASR_MODEL` を環境変数として設定します。モデルを変えると保存済みの埋め込み索引が合わなくなる場合があり、資料ごとの再索引が必要です。

## 1. アプリ専用の依存を入れる

PowerShellでこのリポジトリを開きます。`<保存先>`は自分の配置先に読み替えてください。

```powershell
cd "<保存先>\museum-multimodal-lab-portfolio"
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd frontend
npm ci
cd ..
```

テストも実行する場合は追加で `requirements-dev.txt` を入れます。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

`pip` / `npm` の取得にはネット接続が必要なことがあります。既存アプリの仮想環境へインストールしないでください。

## 2. ローカルモデルを準備する

既定では以下を使います。導入済みなら再取得する必要はありません。

| 用途 | モデル |
| --- | --- |
| 画像の解釈 | `qwen3.5:2b`（Ollamaの画像能力が必要） |
| 質問への回答案 | `qwen3:4b-instruct-2507-q4_K_M` |
| 埋め込み | `embeddinggemma:latest` |
| 音声認識 | faster-whisper の `tiny`、CPU INT8 |

`start_demo.ps1` はこのアプリ専用の Ollama を `127.0.0.1:11436` で起動します。起動後に**別のPowerShell**で導入済みモデルを確認してください。モデルが不足している場合のみ、ネット接続のある準備時に取得します。

```powershell
$env:OLLAMA_HOST = '127.0.0.1:11436'
ollama list
# 必要なものだけ取得する例：
ollama pull embeddinggemma
ollama pull qwen3.5:2b
ollama pull qwen3:4b-instruct-2507-q4_K_M
```

音声認識のモデル重みも一度取得する必要があります。次のコマンドは未取得なら Hugging Face からダウンロードし、通常はユーザーのキャッシュへ保存します。デモ実行時の読込は `local_files_only=True` です。会場などネット接続がない場所で初回取得しようとしないでください。

```powershell
.\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"
```

初期構成は4GiB VRAM程度のGPUを持つ開発PCでも試していますが、快適な速度を保証するものではありません。画像モデル・埋め込み・回答モデルを順に使うため、モデル切替には待ち時間があります。音声の固有名詞・数字は実験用WAVでも誤認したため、必ず原音と照合してください。

## 3. 起動・停止する

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_demo.ps1
# ブラウザー: http://127.0.0.1:5174/
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stop_demo.ps1
```

自動でブラウザーを開かない場合は `start_demo.ps1 -NoBrowser` を使えます。起動スクリプトは専用 Ollama `127.0.0.1:11436`、Backend `127.0.0.1:8765`、Frontend `127.0.0.1:5174` を使用します。既存の 11434 / 11435 や別アプリのポートを勝手に停止しません。起動時に設定した Ollama 関連の環境変数は起動処理後に復元します。起動中のログはGit管理外の `data/logs/` にあります。

原本・SQLite DBなどはGit管理外の `data/` に残り、停止しても自動削除されません。再起動時にも保存済み資料を使えます。GitHubからページを開くだけではアプリは動かず、モデルがあるローカルPCで上記コマンドを実行します。

## 架空資料を作って操作する

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_demo_fixtures.py
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\generate_demo_audio.ps1
```

表画像・文字PDFは `data/demo/` に生成されます。短い日本語WAVの生成は Windows の「Microsoft Haruka Desktop」が使える場合に限ります。生成コマンドの終了結果とファイルの存在を確認してください。これらは実在の科学館の資料ではありません。3〜5分の操作順は [DEMO_GUIDE.md](DEMO_GUIDE.md) を参照してください。

## 開発者向けの確認

次のテストはアプリ専用の環境で実行します。Pythonのテストにはフェイクモデルを用いたものが含まれ、実モデルの品質を証明しません。画面のフェイクAPIテストも同様です。実モデルの少数例と未検証事項は [TEST_REPORT.md](TEST_REPORT.md) に記録しています。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check backend tests scripts
.\.venv\Scripts\python.exe -m compileall -q backend tests scripts
cd frontend
npm run typecheck
npm run lint
npm run build
```

## 起動できないとき

- 5174 / 8765 / 11436 番が使用中なら、起動は失敗します。占有元を調べ、別アプリのプロセスを無差別に終了しないでください。
- モデル不足・Ollama未接続なら、専用サーバーの `ollama list` と `data/logs/` を確認します。未導入モデルをデモ中に取得しようとしないでください。
- 音声認識の重みがない場合は、ネット接続できる準備時に上記の `WhisperModel('tiny', ...)` を実行します。
- PDF・画像・音声の読み取り結果はモデルの推定を含みます。誤りを修正・確認してから登録してください。登録できない・回答が保留される場合は、資料状態、モデル名、索引状態を先に確認します。

接続・保存・再解析・再索引の厳密な仕様は [ARCHITECTURE.md](ARCHITECTURE.md) にあります。このアプリは認証なしの単一PC向け研究デモで、実館資料の保管ルールやOS全体の通信遮断を代替しません。
