# Phase 2 基盤監査

対象は**新アプリの実コード**です。旧Museum Evidence Labは参照だけに留め、データ・ソース・Git履歴・起動中のプロセスを変更しません。ここでいう「確認」はコード確認と自動テストであり、実モデル・実画面の結果は[検証記録](TEST_REPORT.md)と分けます。

## 変更前の基準状態（2026-11-09）

- 当時、新アプリはGit履歴を持たないローカルフォルダでした。既存の Museum Evidence Lab は参照のみで、作業ツリーに変更を加えていません。この公開候補も元のPrivate開発履歴は引き継いでいません。
- 新アプリの既存DBには4版（現行3資料、旧版1資料）と原本4ファイルがありました。修正前に`data/backups/phase2-prechange-20261109/`へ原本とSQLiteのオンラインバックアップを保存。作業・テスト用の資料は別データ領域へ分けます。
- 改修前の`pytest -q`は**25 passed / 7.51秒**、Ruff成功、Frontendの型チェック・Lint・ビルド成功。`GET /api/health`は`ok`、5174番のFrontendはHTTP 200。これらだけでは操作性・実モデル品質は証明されません。
- 実機はWindows 11、メモリ約16 GiB、VRAM 4 GiB。当時の空きメモリやモデル常駐状況は[検証記録](TEST_REPORT.md)の測定条件に依存します。Python 3.11、Node.js、Ollama、PyAVを使用しました。専用Ollamaは11436番で、他アプリのOllamaプロセスは変更していません。

## 実コード上の流れ

| 段階 | API・関数 | 保存先・条件 |
| --- | --- | --- |
| 原本受付 | `POST /api/documents` → `api.upload()` → `LabService.upload()` → `validate_upload()` | 拡張子・内容・上限を検査。原本はUUID名で`data/originals/`、メタ情報は`documents`。同名新版の登録時に旧`chunks`を無効化。 |
| 抽出 | `POST /api/documents/{id}/extract` → `_create_job()` → `_run_extract()` → `extract_document()` | `jobs`をSQLiteへ保存し、最大1件のワーカーで処理。PDF文字、VLM画像解釈、ASR時刻付き文字を`extracted_units`へ区別して保存。抽出直後は検索不可。 |
| 照合・登録 | `PATCH /api/extracted-units/{id}` → `edit_unit()`、`POST /api/documents/{id}/approve` → `_run_approve()` | 元の抽出文を残して修正文を別フィールドへ保存。`review_events`に修正/承認の履歴。`chunk_text()`とローカル`embed()`を経て`chunks`へ索引を一括反映。ChunkにはUnit内順序・ベクトル次元も保持。 |
| 質問 | `POST /api/ask` → `LabService.ask()` → `rank_chunks()` → `OllamaProvider.answer()` | `indexed`文書と`reviewed` Unitのみを候補にし、日本語語句＋DenseをRRFで統合。モデルが返す仮出典IDを実Chunkへ照合し、実原本URLを組み立てる。 |
| 原本・更新 | `GET /api/documents/{id}/original`、PDFページプレビュー、`DELETE /api/documents/{id}`、同名再登録 | 原本は資料IDからだけ参照。編集・差替え・削除で旧索引を無効化。再起動時は途中ジョブを失敗として記録。 |

`Database.connect()`は外部キーを有効にし、変更をcommit/rollbackします。モデル呼び出し中に長いDB書き込みトランザクションは保持しません。単一PC・認証なしの研究デモであり、これは複数人の本番運用設計ではありません。

独立したOCRエンジンと`ocr_text`種別は**未実装**です。画像中の文字も現状はローカルVLMによる`image_interpretation`として保存し、PDFテキスト層の`pdf_text`と混同しません。また`bbox_json`は未利用で、図表の厳密な領域座標は得ていません。画像全体・PDF該当ページへの出典はありますが、ページ内の特定領域への精密な移動はできません。

## 発見した不足と補強

| 優先 | 再現・影響 | 対応箇所と確認方法 |
| --- | --- | --- |
| P0 | 登録済み資料を再解析すると、モデル失敗前に旧Unit/Chunkを消してしまい、修正済み本文・検索対象を失い得た。再索引失敗でも旧Chunkを先に失う経路があった。 | `service.py`で再解析結果/新Embeddingを先に作り、成功時だけ一つのDBトランザクションで旧内容と入れ替える。失敗・起動途中の中断では旧Unit/Chunk/`indexed`を保持し、Jobと資料エラーを記録するfake回帰テストを追加。隔離実モデルAPIでも、存在しない画像モデルによる失敗後に旧`17℃`回答を保持、正常再解析成功後は新Unitへ置換・承認前保留を確認。初回抽出失敗は`failed`のまま。直接の本文編集は意図的に旧索引を無効化する。 |
| P0 | 埋め込みモデル変更後も旧`embedding_model`のベクトルを質問に使い、次元不一致でも語句スコアだけで回答し得た。 | `db.py`に`embedding_dim`を追加し、旧ChunkのJSONから移行。`service.py`で索引モデル・次元を質問前に検査し、不一致なら保留。資料詳細に`reindex_required`を出し、`POST .../reindex`で再登録する。`tests/test_service_flow.py`のモデル変更・次元変更・再索引失敗/再試行のfakeテストを追加。実モデルを変更しての再索引は未実施。 |
| P0 | 原本が失われた後もChunkだけ残ると、実体のない出典リンクを提示し得た。 | `service.py`で原本の存在とSHA256一致を質問時・出典確定前に確認し、利用不可なら回答を保留。詳細に`original_available`を表示する。原本削除/内容改変のfakeテストを追加。実モデルを使った欠損原本の再現は未実施。 |
| P1 | 元ファイルの削除に失敗してもDB行だけ先に消えると、追跡できない原本が残る。 | `service.py`で索引を先に無効化し、削除失敗なら再試行できる資料記録とエラーを残す。fakeテストで失敗経路を確認する。 |
| P1 | 修正文を再編集すると、最初の機械抽出は残るが修正の経過が追えなかった。 | `db.py`へ追加の`review_events`、`service.py`で修正・承認の履歴を記録。既存表や原本は消さない。fakeテストで履歴を確認する。 |
| P1 | 長いPDFの分割ChunkにはUnit/ページ参照はあったが、Unit内の明示的な順序と次元列がなかった。 | `chunks.ordinal`・`embedding_dim`を追加し、旧DBは`ALTER TABLE`で追加移行。旧スキーマからの補完と長文Chunk順序はfakeテストで確認。既存DBのオンラインバックアップは`integrity_check=ok`、資料4行/Chunk3行。コピーへの移行後も同じ件数・整合で、既存Chunkの次元は768へ補完、順序は0、`review_events`表が追加されました。通常環境の再起動後も`integrity_check=ok`、資料4行・Chunk3行を確認。 |
| P1 | 画像の拡張子と中身が不一致でも登録できた。PDFの図形が少ないページは画像解析から漏れ得た。 | `extraction.py`の内容検査と図形判定を修正。PNG/JPEG不一致・1本の図形を持つPDFのfakeテストを追加。 |
| P1 | MP3/M4Aの処理は偶然の間接依存`av`に頼り、Ollamaの壊れたJSON応答は内部例外になり得た。 | `requirements.txt`へPyAVを直接明記し、`providers.py`で応答形を検査。自作符号化ファイルのデコード＋fake ASR、壊れた応答のfakeテストを追加。実ASR・実音声再生とは別。 |
| P1 | 100秒級の工程で実際の状態・経過時間が見えにくく、画面上の進捗%が根拠のない予測に見えた。再読込時に選択資料や編集中の文章が失われ得た。 | `service.py`のジョブに`phase`・`elapsed_sec`を追加し、Frontendは工程名と経過時間を使う。選択資料/ジョブをセッション内で復元し、未保存テキストを警告・保持する。隔離ブラウザー操作で画像処理中の再読込後にジョブ表示が戻ることを確認。未保存編集の警告はコード確認段階で、人の操作での受け入れは別。 |

実モデルAPIで画像・文字PDF・画像のみPDF・WAVの一往復、類似3資料の検索、削除・差替え・再起動を確認しました。別の空DBでPNG・文字PDF・WAVのブラウザー通し操作も実施しました。通常デモ環境も既存資料を保持して再起動し、質問・出典表示まで確認しました。結果と改修前25件→改修後48件の自動テストは[TEST_REPORT.md](TEST_REPORT.md)に記録します。再解析のステージング修正後の実モデル再確認は同記録の最新欄を参照してください。上記の局所的な対策を「完全オフライン」「出典の意味的正しさ」「同時操作の厳密な原子性」の証明とは扱いません。
