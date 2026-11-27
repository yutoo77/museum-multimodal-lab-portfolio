# Windowsでの準備・起動

このページは **Museum Multimodal Lab** のローカル実行手順です。既存の Museum Evidence Lab の環境・DB・プロセスは共用しません。まず自作の架空資料だけで試してください。

## 必要なもの

- Windows 11、64bit の Python 3.11（`py -3.11` が使えること）
- Node.js / npm、Ollama（この公開版はNode.js 24で検証）
- モデル重みを保存できる空き容量。ダウンロードは初回の**準備時**に行い、アプリ実行時の外部AI API送信とは区別します。

既定のモデルは [`.env.example`](../.env.example) に記載されています。このファイルは自動読み込みされません。モデル名は起動前の環境変数で上書きします。**回答モデルだけを変更しても再索引は不要**です。埋め込みモデルを変更した場合は既存資料を再索引してください。

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

専用Ollamaの起動時に `OLLAMA_NO_CLOUD=1` を設定します。準備時の依存・モデル取得と、実行時のローカル推論は別です。ネットワーク切断下での動作は未検証です。

原本・SQLite DBなどはGit管理外の `data/` に残り、停止しても自動削除されません。再起動時にも保存済み資料を使えます。GitHubからページを開くだけではアプリは動かず、モデルがあるローカルPCで上記コマンドを実行します。

質問画面はBM25が既定です。比較したいときだけ「検索方法」を開きます。Dense・文字Hybrid・視覚を含む方式では、Qdrantクライアントの**永続ローカルモード**が `data/qdrant/`（`MML_DATA_DIR` 指定時はその配下）を使います。別のサーバーやDockerは不要です。同じデータディレクトリを複数のBackendから同時に開かないでください。

### 1.7Bで軽量デモを行う（任意）

準備時に、利用するローカルOllamaに未導入なら `ollama pull qwen3:1.7b` を実行します。起動中なら停止した後、次のPowerShellの同じセッションで起動します。

```powershell
$env:MML_ANSWER_MODEL = 'qwen3:1.7b'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_demo.ps1
```

4Bに戻すには停止し、`Remove-Item Env:MML_ANSWER_MODEL` を実行するか、このPowerShellを閉じて環境変数を設定していないセッションから再起動します。`/api/health` の `models.answer` で使用する設定名を確認できます。1.7Bは待ち時間を抑えたい場合の選択肢で、速度・回答品質は機材と質問に依存します。

### 画像・PDFページの直接検索を有効にする（任意）

基本構成・画像のVLM読み取りには不要です。[固定版の日本語CLIP ONNX](https://huggingface.co/AUXOUT-TEAM/clip-japanese-base-v2-onnx/tree/c924148be2e25b6e4d98e66d8dc1768adb72d079)を準備時に取得します。実行時の自動ダウンロードはしません。モデル2ファイルとトークナイザーは計約138 MiB。CPUで実行し、サイズとSHA-256を確認してから読み込みます。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-visual.txt
$mmlModelPath = Join-Path (Get-Location) 'outputs/models/clip-japanese-base-v2'
New-Item -ItemType Directory -Force -Path (Join-Path $mmlModelPath 'onnx') | Out-Null
$mmlModelUrl = 'https://huggingface.co/AUXOUT-TEAM/clip-japanese-base-v2-onnx/resolve/c924148be2e25b6e4d98e66d8dc1768adb72d079'
foreach ($mmlArtifact in @('onnx/vision_model_q4f16.onnx','onnx/text_model_q4f16.onnx','tokenizer.json','LICENSE','LICENSE.rinna-MIT','NOTICE')) {
    curl.exe --fail --silent --show-error --location --max-time 120 --max-filesize 95000000 --output (Join-Path $mmlModelPath $mmlArtifact) "$mmlModelUrl/$mmlArtifact"
    if ($LASTEXITCODE -ne 0) { throw "モデル取得に失敗: $mmlArtifact" }
}
$env:MML_VISUAL_MODEL_DIR = (Resolve-Path $mmlModelPath).Path
# 停止済みの状態から起動
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_demo.ps1
```

`/api/health` の `visual_retrieval` が `ready` なら「検索方法」に視覚の2方式が出ます。既存の承認済み画像・PDFに視覚索引がなければ `needs_reindex` と表示されます。資料詳細で対象を「検索情報を更新」してください。一括変更はしません。依存や固定ファイルの不一致は `setup_failed` となり、基本の文字検索は利用できます。視覚検索をオフに戻すときは停止後に `Remove-Item Env:MML_VISUAL_MODEL_DIR` を実行し、再起動します。

## 架空資料を作って操作する

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_demo_fixtures.py
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\generate_demo_audio.ps1
```

表画像・文字PDFは `data/demo/` に生成されます。短い日本語WAVの生成は Windows の「Microsoft Haruka Desktop」が使える場合に限ります。生成コマンドの終了結果とファイルの存在を確認してください。これらは実在の科学館の資料ではありません。3〜5分の操作順は [DEMO_GUIDE.md](DEMO_GUIDE.md) を参照してください。

## 開発者向けの確認

次のテストはアプリ専用の環境で実行します。Pythonのテストにはフェイクモデルを用いたものが含まれ、実モデルの品質を証明しません。画面のフェイクAPIテストも同様です。実モデルの少数例と未検証事項は [TEST_REPORT.md](TEST_REPORT.md) に記録しています。

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\ruff.exe check backend tests scripts
.\.venv\Scripts\python.exe -m compileall -q backend tests scripts
cd frontend
npm run typecheck
npm run lint
npm run build
```

起動済みのローカル画面に対し、`frontend/` で `npm run state:ui`、`npm run polish:ui`、`npm run expanded:ui`、`npm run preview:ui` を実行できます。URLが既定と異なる場合は `MML_FRONTEND_URL` を指定します。これらのAPI応答はフェイクです。実際に登録するブラウザーテストの準備は [TEST_REPORT.md](TEST_REPORT.md) に記載しています。

## 起動できないとき

- 5174 / 8765 / 11436 番が使用中なら、起動は失敗します。占有元を調べ、別アプリのプロセスを無差別に終了しないでください。
- モデル不足・Ollama未接続なら、専用サーバーの `ollama list` と `data/logs/` を確認します。未導入モデルをデモ中に取得しようとしないでください。
- 音声認識の重みがない場合は、ネット接続できる準備時に上記の `WhisperModel('tiny', ...)` を実行します。
- PDF・画像・音声の読み取り結果はモデルの推定を含みます。誤りを修正・確認してから登録してください。登録できない・回答が保留される場合は、資料状態、モデル名、索引状態を先に確認します。

接続・保存・再解析・再索引の厳密な仕様は [ARCHITECTURE.md](ARCHITECTURE.md) にあります。このアプリは認証なしの単一PC向け研究デモで、実館資料の保管ルールやOS全体の通信遮断を代替しません。
