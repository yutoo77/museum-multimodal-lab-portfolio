# 第三者ライセンスと素材

アプリの自作コード・文書・自作のデモ画面には [GNU AGPL-3.0-only](LICENSE) を適用します。第三者のパッケージ・推論エンジン・モデルは同梱せず、各提供元から利用者のPCへ取得します。この表は主な依存先であり、全推移依存の通知や各配布元の条件に代わるものではありません。

| 利用先 | ライセンス・条件 |
| --- | --- |
| [PyMuPDF](https://pymupdf.io/licensing) | AGPLv3またはArtifex商用ライセンス。本ソース公開版はAGPLで利用します。PDF等の著作権・Producer表示と関連する条件を保持してください。 |
| [Qdrant Python client](https://github.com/qdrant/qdrant-client) | Apache-2.0。永続ローカルモードを使い、サーバー用バイナリを再配布しません。 |
| [ONNX Runtime](https://onnxruntime.ai/docs/)・[Hugging Face Tokenizers](https://github.com/huggingface/tokenizers)・[NumPy](https://numpy.org/doc/stable/license.html) | 任意の視覚検索用。ONNX RuntimeはMIT、TokenizersはApache-2.0、NumPyの主ライセンスはBSD-3-Clause。各配布物内の追加通知にも従ってください。 |
| [Ollama](https://github.com/ollama/ollama) | Ollama自体はMIT。モデルごとの条件は別です。 |
| [Qwen3 4B Instruct](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M)・[Qwen3 1.7Bタグ](https://ollama.com/library/qwen3:1.7b)・[Qwen3.5 2B](https://ollama.com/library/qwen3.5:2b) | Apache-2.0。4B/1.7Bは回答、Qwen3.5は画像解釈に使用します。 |
| [EmbeddingGemma](https://ollama.com/library/embeddinggemma) | [Gemmaの利用条件](https://gemma.ai/resources/terms/)が適用されます。 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper)・[Whisper](https://github.com/openai/whisper)・[PyAV](https://github.com/PyAV-Org/PyAV) | faster-whisperおよびWhisperのコード・モデルはMIT、PyAVはBSD-3-Clause。配布物をまとめ直す場合、PyAV wheelに含まれるFFmpeg等の条件は別途確認してください。 |

## 任意の視覚検索モデル

[clip-japanese-base-v2](https://huggingface.co/line-corporation/clip-japanese-base-v2) Copyright 2025 LY Corporation、Apache-2.0。[使用するq4f16 ONNX変換版](https://huggingface.co/AUXOUT-TEAM/clip-japanese-base-v2-onnx/tree/c924148be2e25b6e4d98e66d8dc1768adb72d079)はApache-2.0で、同梱トークナイザーの由来は rinna Co., Ltd. の日本語RoBERTa（MIT）です。[配布元のNOTICE](https://huggingface.co/AUXOUT-TEAM/clip-japanese-base-v2-onnx/raw/c924148be2e25b6e4d98e66d8dc1768adb72d079/NOTICE)、`LICENSE`、`LICENSE.rinna-MIT` をモデルファイルと一緒に保持してください。[取得手順](docs/SETUP.md#画像pdfページの直接検索を有効にする任意)にこれらのファイルを含めています。

## デモ素材

READMEの画面は架空の展示資料を使ったアプリのスクリーンショットです。フォントファイル、合成音声の原本、PDF原本、モデル・バイナリはリポジトリに含めません。Windowsのフォントや音声はローカルでデモ素材を生成するときにだけ使い、第三者素材を再配布する場合の権利は別途確認してください。
