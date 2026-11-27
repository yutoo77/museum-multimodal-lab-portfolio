// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

import { forwardRef, useCallback, useEffect, useId, useRef, useState } from "react";
import type { ButtonHTMLAttributes, PointerEvent as ReactPointerEvent } from "react";
import { api, DEFAULT_RETRIEVAL_MODE } from "./api";
import type { Answer, ExtractedUnit, Health, Job, MuseumDocument, Page, RetrievalMode, Source, UnitKind } from "./types";

const nav: { id: Page; label: string }[] = [
  { id: "import", label: "資料を取り込む" },
  { id: "documents", label: "保存した資料" },
  { id: "ask", label: "質問する" },
];

type IconName = Page | "help" | "close" | "external" | "zoomIn" | "zoomOut";

function Icon({ name }: { name: IconName }) {
  const paths: Record<IconName, React.ReactNode> = {
    import: <><path d="M12 3v11m0 0 4-4m-4 4-4-4" /><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" /></>,
    documents: <><path d="M7 3h7l4 4v14H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z" /><path d="M14 3v5h4M9 12h6M9 16h6" /></>,
    ask: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 5 5" /></>,
    help: <><circle cx="12" cy="12" r="9" /><path d="M9.7 9a2.5 2.5 0 1 1 4.4 1.6c-.6.7-2.1 1.2-2.1 2.7" /><path d="M12 17h.01" /></>,
    close: <path d="M5 5l14 14M19 5 5 19" />,
    external: <><path d="M13 5h6v6M19 5l-9 9" /><path d="M19 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" /></>,
    zoomIn: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 5 5M10.5 8v5M8 10.5h5" /></>,
    zoomOut: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 5 5M8 10.5h5" /></>,
  };
  return <svg className="app-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { icon: IconName; tooltip: string };

const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton({ icon, tooltip, className = "", onKeyDown, onBlur, ...props }, ref) {
  const tooltipId = useId();
  const [dismissed, setDismissed] = useState(false);
  return <span className="icon-action" data-dismissed={dismissed} onPointerEnter={() => setDismissed(false)}>
    <button {...props} ref={ref} className={`icon-button ${className}`} aria-label={tooltip} aria-describedby={tooltipId} onBlur={(event) => { setDismissed(false); onBlur?.(event); }} onKeyDown={(event) => { if (event.key === "Escape") setDismissed(true); onKeyDown?.(event); }}><Icon name={icon} /></button>
    <span className="icon-tooltip" id={tooltipId} role="tooltip">{tooltip}</span>
  </span>;
});

const kindLabels: Record<UnitKind, string> = {
  pdf_text: "PDFの文字",
  table: "表の構造",
  image_interpretation: "画像のAI解釈",
  audio_transcript: "音声認識",
};

const defaultRetrievalModes: RetrievalMode[] = [DEFAULT_RETRIEVAL_MODE, "legacy", "dense", "hybrid"];
const retrievalModeLabels: Record<RetrievalMode, string> = {
  legacy: "従来方式",
  bm25: "語句の一致（BM25）",
  dense: "意味の近さ（Qdrant）",
  hybrid: "語句＋意味（Hybrid）",
  visual: "ページ画像から探す",
  multimodal: "文字＋画像から探す",
  agentic: "探し方を自動選択",
};

const availableRetrievalModes = (health: Health | null): RetrievalMode[] => {
  const advertised = health?.retrieval_modes?.filter((mode) => Object.hasOwn(retrievalModeLabels, mode));
  return advertised?.length ? [...new Set(advertised)] : defaultRetrievalModes;
};

const statusLabels: Record<string, string> = {
  draft: "確認待ち",
  reviewed: "確認済み",
  indexed: "検索に登録済み",
  failed: "処理失敗",
  superseded: "旧版",
};

const documentStatus = (doc: MuseumDocument): string =>
  doc.original_available === false ? "原本が見つかりません" : doc.reindex_required ? "検索情報の更新が必要" : statusLabels[doc.status] ?? doc.status;

const documentStatusClass = (doc: MuseumDocument): string =>
  doc.original_available === false ? "status-failed" : doc.reindex_required ? "status-warning" : `status-${doc.status}`;

const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : "処理を完了できませんでした。";

const dateText = (value?: string): string => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ja-JP", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
};

const dateTimeText = (value: string): string => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("ja-JP", { dateStyle: "short", timeStyle: "short" }).format(date);
};

const secondText = (value: number | null | undefined): string =>
  value == null ? "時刻未確定" : `${value.toFixed(1)}秒`;

const locationText = (unit: { kind?: UnitKind; page_number: number | null; start_sec: number | null; end_sec: number | null }): string => {
  if (unit.page_number != null) return `${unit.page_number}ページ`;
  if (unit.start_sec != null) return `${secondText(unit.start_sec)}–${secondText(unit.end_sec)}`;
  if (unit.kind === "image_interpretation") return "画像全体";
  return "位置未確定";
};

const fileKind = (doc: MuseumDocument): string => {
  if (doc.mime_type === "application/pdf") return "PDF";
  if (doc.mime_type.startsWith("image/")) return "画像";
  if (doc.mime_type.startsWith("audio/")) return "音声";
  return "資料";
};

const jobStage = (job: Job): string => {
  if (job.phase) return job.phase;
  if (job.status === "pending") return "処理の順番を待っています";
  if (job.status === "failed") return "処理に失敗しました";
  if (job.status === "completed") return job.kind === "extract" ? "読み取りが完了しました" : "知識への登録が完了しました";
  return job.kind === "extract" ? "資料を読み取っています" : "検索用の情報を作っています";
};

const jobStatusLabel = (job: Job, doc: MuseumDocument): string =>
  job.kind === "extract" ? "読み取り中" : doc.status === "indexed" ? "検索情報を更新中" : "登録中";

const storedId = (key: string): string | null => {
  try { return window.sessionStorage.getItem(key); } catch { return null; }
};

type ComparisonTarget = {
  documentId: string;
  unitId: string;
  page: number | null;
  time: number | null;
  fromAnswer: boolean;
  fromClarification?: boolean;
  snippet?: string;
};

type ScrollTarget = { area: "job" | "review" | "answer" | "upload" | "comparison"; documentId?: string };

function OriginalPreview({
  doc,
  page,
  time,
  onPageChange,
}: {
  doc: MuseumDocument;
  page: number | null;
  time: number | null;
  onPageChange: (page: number) => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const panStartRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const [pdfError, setPdfError] = useState(false);
  const [pdfLoading, setPdfLoading] = useState(true);
  const [zoom, setZoom] = useState(100);
  const [dragging, setDragging] = useState(false);
  const original = api.originalUrl(doc.id);
  const isPdf = doc.mime_type === "application/pdf";
  const missingOriginal = doc.original_available === false;
  const currentPage = Math.max(1, page ?? 1);
  const maxPage = Math.max(1, doc.page_count ?? 1, ...(doc.units ?? []).map((unit) => unit.page_number ?? 1));

  useEffect(() => { setPdfError(false); setPdfLoading(true); }, [doc.id, currentPage]);

  useEffect(() => {
    setZoom(100);
    setDragging(false);
    panStartRef.current = null;
    viewportRef.current?.scrollTo(0, 0);
  }, [doc.id, currentPage]);

  useEffect(() => {
    if (time != null && audioRef.current) audioRef.current.currentTime = time;
  }, [doc.id, time]);

  const playAtTime = async () => {
    if (!audioRef.current || time == null) return;
    audioRef.current.currentTime = time;
    try {
      await audioRef.current.play();
    } catch {
      // Browser autoplay restrictions leave the controls available for a user click.
    }
  };

  const resetZoom = () => {
    setZoom(100);
    viewportRef.current?.scrollTo(0, 0);
  };

  const startPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (zoom === 100 || event.pointerType !== "mouse" || event.button !== 0) return;
    panStartRef.current = { x: event.clientX, y: event.clientY, left: event.currentTarget.scrollLeft, top: event.currentTarget.scrollTop };
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  };

  const movePan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!panStartRef.current) return;
    event.currentTarget.scrollLeft = panStartRef.current.left - (event.clientX - panStartRef.current.x);
    event.currentTarget.scrollTop = panStartRef.current.top - (event.clientY - panStartRef.current.y);
  };

  const endPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    panStartRef.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const previewClass = `preview-viewport ${zoom > 100 ? "is-zoomed" : ""} ${dragging ? "is-dragging" : ""}`;
  const previewEvents = { onPointerDown: startPan, onPointerMove: movePan, onPointerUp: endPan, onPointerCancel: endPan };

  return (
    <section className="original-panel" aria-label="元資料">
      <div className="panel-heading">
        <div>
          <h3>元資料</h3>
        </div>
        {!missingOriginal && <a className="text-link with-icon" href={isPdf ? `${original}#page=${currentPage}` : original} target="_blank" rel="noopener noreferrer">
          {isPdf ? "元PDFを開く" : doc.mime_type.startsWith("image/") ? "元画像を開く" : doc.mime_type.startsWith("audio/") ? "元音声を開く" : "別画面で開く"}<Icon name="external" />
        </a>}
      </div>
      {!missingOriginal && (isPdf || doc.mime_type.startsWith("image/")) && <div className="preview-controls" role="group" aria-label="元資料の表示倍率">
        <IconButton icon="zoomOut" tooltip="縮小" type="button" disabled={zoom <= 100} onClick={() => setZoom((current) => Math.max(100, current - 50))} />
        <output aria-live="polite">{zoom}%</output>
        <IconButton icon="zoomIn" tooltip="拡大" type="button" disabled={zoom >= 300} onClick={() => setZoom((current) => Math.min(300, current + 50))} />
        <button className="preview-reset" type="button" disabled={zoom === 100} onClick={resetZoom}>全体表示</button>
      </div>}
      {missingOriginal ? <p className="warning-note">元資料が見つかりません。回答の根拠として利用しないでください。</p> : isPdf ? (
        <div className="pdf-page">
          <div className="pdf-page-toolbar">
            <span>{currentPage} / {maxPage} ページ</span>
            {maxPage > 1 && <div>
              <button className="inline-button" type="button" disabled={currentPage <= 1} onClick={() => onPageChange(currentPage - 1)}>前へ</button>
              <button className="inline-button" type="button" disabled={currentPage >= maxPage} onClick={() => onPageChange(currentPage + 1)}>次へ</button>
            </div>}
          </div>
          {pdfError ? <p className="preview-fallback">ページ画像を表示できません。右上の「元PDFを開く」から確認してください。</p> : <>
            {pdfLoading && <p className="pdf-page-loading" role="status">ページを表示しています…</p>}
            <div ref={viewportRef} className={previewClass} tabIndex={zoom > 100 ? 0 : -1} aria-label="PDFページ。拡大後はスクロールまたはドラッグで移動できます" {...previewEvents}>
              <img
                className="pdf-page-preview"
                style={{ width: `${zoom}%` }}
                src={`/api/documents/${encodeURIComponent(doc.id)}/pages/${currentPage}/preview`}
                alt={`${doc.name} の${currentPage}ページのプレビュー`}
                draggable={false}
                onLoad={() => setPdfLoading(false)}
                onError={() => { setPdfError(true); setPdfLoading(false); }}
              />
            </div></>}
        </div>
      ) : doc.mime_type.startsWith("image/") ? (
        <div ref={viewportRef} className={previewClass} tabIndex={zoom > 100 ? 0 : -1} aria-label="元画像。拡大後はスクロールまたはドラッグで移動できます" {...previewEvents}>
          <img className="image-preview" style={{ width: `${zoom}%` }} src={original} alt={`${doc.name} の元画像`} draggable={false} />
        </div>
      ) : doc.mime_type.startsWith("audio/") ? (
        <div className="audio-preview">
          <audio ref={audioRef} controls preload="metadata" src={original} aria-label={`${doc.name} の元音声`} />
          {time != null && (
            <button className="button button-quiet" type="button" onClick={() => void playAtTime()}>
              {secondText(time)}から再生
            </button>
          )}
        </div>
      ) : (
        <p className="muted">この形式は別画面で確認してください。</p>
      )}
    </section>
  );
}

function UnitEditor({
  unit,
  draft,
  onDraft,
  onSave,
  onLocate,
  saving,
  readOnly,
}: {
  unit: ExtractedUnit;
  draft: string;
  onDraft: (value: string) => void;
  onSave: () => void;
  onLocate: () => void;
  saving: boolean;
  readOnly: boolean;
}) {
  const changed = draft !== (unit.corrected_text ?? unit.extracted_text);
  return (
    <article className="unit-card">
      <div className="unit-topline">
        <span className="kind-tag">{kindLabels[unit.kind] ?? unit.kind}</span>
        <button className="inline-button" type="button" onClick={onLocate}>
          {locationText(unit)}を元資料で見る ↗
        </button>
      </div>
      <label className="field-label" htmlFor={`unit-${unit.id}`}>
        確認・修正するテキスト
      </label>
      <textarea
        id={`unit-${unit.id}`}
        className="unit-textarea"
        value={draft}
        onChange={(event) => onDraft(event.target.value)}
        readOnly={readOnly}
        spellCheck={false}
      />
      <div className="unit-footer">
        <span className="muted-small">
          {readOnly ? "旧版のため閲覧のみ" : unit.corrected_text != null ? "人の修正を保存済み" : "未修正の抽出結果"}
        </span>
        <button className="button button-quiet" type="button" disabled={readOnly || !changed || saving} onClick={onSave}>
          {saving ? "保存中…" : "修正を保存"}
        </button>
      </div>
    </article>
  );
}

function SourceCard({ source, onOpen, candidate = false }: { source: Source; onOpen: () => void; candidate?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const snippetId = useId();
  const isLong = source.snippet.length > 120 || source.snippet.split(/\r?\n/).length > 4;
  const url = source.original_url || api.originalUrl(source.document_id);
  const fragment = source.start_sec != null ? `#t=${Math.max(0, source.start_sec)}` : source.page_number != null ? `#page=${source.page_number}` : "";
  const originalLink = url.includes("#") ? url : `${url}${fragment}`;
  return (
    <article className="source-card">
      <div className="source-card-head">
        <span className="kind-tag">{kindLabels[source.kind] ?? source.kind}</span>
        {source.retrieval_origin && <span className="retrieval-origin" title={source.retrieval_origin === "visual" ? "ページ画像で資料候補を見つけました" : "文字情報から資料候補を見つけました"}>{source.retrieval_origin === "visual" ? "画像で候補" : "文字で候補"}</span>}
      </div>
      <h3>{source.document_name}</h3>
      <p className="source-location">{locationText(source)}</p>
      {source.retrieval_origin === "visual" && <p className="source-grounding">回答には確認済みの文字情報を使用</p>}
      <blockquote id={snippetId} className={isLong && !expanded ? "is-clamped" : undefined}>{source.snippet}</blockquote>
      {isLong && <button className="inline-button source-expand" type="button" aria-expanded={expanded} aria-controls={snippetId} onClick={() => setExpanded((current) => !current)}>{expanded ? "閉じる" : "全文を見る"}</button>}
      <div className="source-links">
        <button className="inline-button" type="button" onClick={onOpen}>{candidate ? "元資料で確認する" : "元資料と比較する"}</button>
        <a className="text-link with-icon" href={originalLink} target="_blank" rel="noopener noreferrer">
          元資料を開く<Icon name="external" />
        </a>
      </div>
    </article>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>(() => {
    const saved = storedId("mml.page");
    return saved === "documents" || saved === "ask" ? saved : "import";
  });
  const [health, setHealth] = useState<Health | null>(null);
  const [documents, setDocuments] = useState<MuseumDocument[]>([]);
  const [documentsLoaded, setDocumentsLoaded] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(() => storedId("mml.selectedId"));
  const [selectedDocument, setSelectedDocument] = useState<MuseumDocument | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [jobId, setJobId] = useState<string | null>(() => storedId("mml.jobId"));
  const [job, setJob] = useState<Job | null>(null);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  const [question, setQuestion] = useState("");
  const [retrievalMode, setRetrievalMode] = useState<RetrievalMode>(DEFAULT_RETRIEVAL_MODE);
  const retrievalModes = availableRetrievalModes(health);
  const selectedRetrievalMode = retrievalModes.includes(retrievalMode) ? retrievalMode : retrievalModes[0];
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [errorContext, setErrorContext] = useState<"upload" | "extract" | "approve" | "ask" | "reindex" | null>(null);
  const [previewPage, setPreviewPage] = useState<number | null>(null);
  const [previewTime, setPreviewTime] = useState<number | null>(null);
  const [showUpload, setShowUpload] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [comparison, setComparison] = useState<ComparisonTarget | null>(null);
  const [scrollTarget, setScrollTarget] = useState<ScrollTarget | null>(null);
  const uploadRef = useRef<HTMLElement>(null);
  const workflowRef = useRef<HTMLElement>(null);
  const jobRef = useRef<HTMLDivElement>(null);
  const reviewRef = useRef<HTMLDivElement>(null);
  const answerRef = useRef<HTMLElement>(null);
  const comparisonRef = useRef<HTMLDivElement>(null);
  const helpCloseRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    try { window.sessionStorage.setItem("mml.page", page); } catch { /* Private browsing may block storage. */ }
  }, [page]);

  useEffect(() => {
    try {
      if (selectedId) window.sessionStorage.setItem("mml.selectedId", selectedId);
      else window.sessionStorage.removeItem("mml.selectedId");
    } catch { /* Current page remains usable without storage. */ }
  }, [selectedId]);

  useEffect(() => {
    try {
      if (jobId) window.sessionStorage.setItem("mml.jobId", jobId);
      else window.sessionStorage.removeItem("mml.jobId");
    } catch { /* Current job remains visible until navigation. */ }
  }, [jobId]);

  useEffect(() => {
    if (!jobId) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [jobId]);

  const loadDocuments = useCallback(async () => {
    const list = await api.listDocuments();
    setDocuments(list);
    setDocumentsLoaded(true);
    return list;
  }, []);

  const loadDocument = useCallback(async (id: string) => {
    const document = await api.getDocument(id);
    setSelectedDocument(document);
    setDrafts(Object.fromEntries((document.units ?? []).map((unit) => [unit.id, unit.corrected_text ?? unit.extracted_text])));
    setReviewConfirmed(false);
    return document;
  }, []);

  useEffect(() => {
    void api.health().then(setHealth).catch((err: unknown) => setError(errorText(err)));
    void loadDocuments().catch((err: unknown) => setError(errorText(err)));
  }, [loadDocuments]);

  useEffect(() => {
    if (!selectedId) {
      setSelectedDocument(null);
      return;
    }
    void loadDocument(selectedId).catch((err: unknown) => setError(errorText(err)));
  }, [selectedId, loadDocument]);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const current = await api.getJob(jobId);
        if (cancelled) return;
        setJob(current);
        if (current.status === "completed" || current.status === "failed") {
          setJobId(null);
          if (current.status === "failed") {
            setError(current.error || "処理に失敗しました。");
            setErrorContext(current.kind === "extract" ? "extract" : "approve");
            setNotice(null);
          } else if (page !== "import" || current.document_id !== selectedId) {
            setNotice(current.kind === "extract" ? "読み取りが完了しました。内容を確認してください。" : "知識の登録が完了しました。質問できます。");
          } else {
            setNotice(null);
          }
          if (current.status === "completed") setAnswer(null);
          await loadDocuments();
          void api.health().then(setHealth).catch(() => {});
          if (selectedId) await loadDocument(selectedId);
          if (page === "import" && current.document_id === selectedId) {
            setScrollTarget({ area: current.status === "failed" ? "job" : current.kind === "extract" ? "review" : "job", documentId: selectedId });
          }
          return;
        }
        timer = setTimeout(() => void poll(), 1000);
      } catch (err) {
        if (!cancelled) {
          setJobId(null);
          setError(errorText(err));
          setErrorContext(null);
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, selectedId, page, loadDocument, loadDocuments]);

  const clearMessages = () => {
    setError(null);
    setErrorContext(null);
    setNotice(null);
  };

  const upload = async () => {
    if (!file) return;
    if (hasUnsavedChanges && !window.confirm("保存していない修正があります。破棄して別の資料を追加しますか？")) return;
    clearMessages();
    setBusy("upload");
    try {
      const document = await api.uploadDocument(file);
      // A revised upload can supersede the source before the new review begins.
      setAnswer(null);
      await loadDocuments();
      setSelectedId(document.id);
      setFile(null);
      setFileInputKey((current) => current + 1);
      setShowUpload(false);
      setComparison(null);
      setScrollTarget({ area: "job", documentId: document.id });
      setNotice(document.status === "indexed" ? "同じ内容の資料がすでに登録されています。質問画面から利用できます。" : document.units?.length ? "同じ内容の資料があります。確認・登録の状態を確かめてください。" : "資料を受け付けました。次に読み取りを開始してください。");
    } catch (err) {
      setError(errorText(err));
      setErrorContext("upload");
    } finally {
      setBusy(null);
    }
  };

  const extract = async () => {
    if (!selectedDocument || selectedDocument.status === "superseded") return;
    if (selectedDocument.status === "indexed" && !window.confirm("再解析が完了すると、新しい内容を確認・登録するまで質問に使われません。失敗時は今の登録内容を保ちます。続けますか？")) return;
    clearMessages();
    setBusy("extract");
    try {
      const result = await api.extractDocument(selectedDocument.id);
      setJob(null);
      setJobId(result.job_id);
      setShowUpload(false);
      setScrollTarget({ area: "job", documentId: selectedDocument.id });
    } catch (err) {
      setError(errorText(err));
      setErrorContext("extract");
    } finally {
      setBusy(null);
    }
  };

  const saveUnit = async (unit: ExtractedUnit) => {
    if (!selectedDocument || selectedDocument.status === "superseded") return;
    clearMessages();
    setBusy(unit.id);
    try {
      await api.updateUnit(unit.id, drafts[unit.id] ?? "");
      const updated = await api.getDocument(selectedDocument.id);
      setSelectedDocument(updated);
      setDrafts((current) => Object.fromEntries(updated.units.map((item) => [
        item.id,
        item.id === unit.id ? item.corrected_text ?? item.extracted_text : current[item.id] ?? item.corrected_text ?? item.extracted_text,
      ])));
      setReviewConfirmed(false);
      setAnswer(null);
      setNotice("修正を保存しました。知識への登録はまだ行われていません。");
    } catch (err) {
      setError(errorText(err));
      setErrorContext("approve");
    } finally {
      setBusy(null);
    }
  };

  const approve = async () => {
    if (!selectedDocument || selectedDocument.status === "superseded" || !reviewConfirmed) return;
    clearMessages();
    setBusy("approve");
    try {
      const result = await api.approveDocument(selectedDocument.id);
      if ("job_id" in result) {
        setJob(null);
        setJobId(result.job_id);
      } else {
        await loadDocuments();
        await loadDocument(result.id);
        setNotice("確認した内容を知識に登録しました。");
      }
    } catch (err) {
      setError(errorText(err));
      setErrorContext("approve");
    } finally {
      setBusy(null);
    }
  };

  const reindex = async () => {
    if (!selectedDocument || selectedDocument.status !== "indexed") return;
    if (!window.confirm("更新が成功するまで以前の検索情報は保持されます。モデルが変わった場合は、更新まで質問を保留します。続けますか？")) return;
    clearMessages();
    setBusy("reindex");
    try {
      const result = await api.reindexDocument(selectedDocument.id);
      setJob(null);
      setJobId(result.job_id);
      await loadDocuments();
      await loadDocument(selectedDocument.id);
    } catch (err) {
      setError(errorText(err));
      setErrorContext("reindex");
    } finally {
      setBusy(null);
    }
  };

  const deleteDocument = async () => {
    if (!selectedDocument || !window.confirm(`「${selectedDocument.name}」と関連する検索索引を削除します。元に戻せません。よろしいですか？`)) return;
    clearMessages();
    setBusy("delete");
    try {
      await api.deleteDocument(selectedDocument.id);
      setSelectedId(null);
      setSelectedDocument(null);
      setAnswer(null);
      await loadDocuments();
      setNotice("資料と検索索引を削除しました。");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  };

  const ask = async () => {
    if (!question.trim() || busy !== null || Boolean(jobId) || !hasSearchableDocument) return;
    clearMessages();
    setBusy("ask");
    setAnswer(null);
    try {
      setAnswer(await api.ask(question.trim(), selectedRetrievalMode));
      setScrollTarget({ area: "answer" });
    } catch (err) {
      setError(errorText(err));
      setErrorContext("ask");
    } finally {
      setBusy(null);
    }
  };

  const selectDocument = (id: string, target: Page = "documents"): boolean => {
    if (hasUnsavedChanges && (id !== selectedId || (page === "import" && target !== "import")) && !window.confirm("保存していない修正があります。破棄して画面を切り替えますか？")) return false;
    clearMessages();
    setSelectedId(id);
    setPage(target);
    setShowHelp(false);
    setScrollTarget(null);
    setPreviewPage(null);
    setPreviewTime(null);
    setComparison(null);
    return true;
  };

  const openSource = (source: Source) => {
    if (!selectDocument(source.document_id, "documents")) return;
    setPreviewPage(source.page_number);
    setPreviewTime(source.start_sec);
    setComparison({ documentId: source.document_id, unitId: source.unit_id, page: source.page_number, time: source.start_sec, fromAnswer: true, fromClarification: answer?.status === "needs_clarification", snippet: source.snippet });
    setScrollTarget({ area: "comparison", documentId: source.document_id });
  };

  const compareDocument = (doc: MuseumDocument) => {
    const unit = doc.units?.[0];
    if (!unit) return;
    setPreviewPage(unit.page_number);
    setPreviewTime(unit.start_sec);
    setComparison({ documentId: doc.id, unitId: unit.id, page: unit.page_number, time: unit.start_sec, fromAnswer: false });
    setScrollTarget({ area: "comparison", documentId: doc.id });
  };

  const returnFromComparison = () => {
    const fromAnswer = comparison?.fromAnswer;
    setComparison(null);
    if (fromAnswer) {
      setPage("ask");
      setScrollTarget({ area: "answer" });
    } else window.scrollTo({ top: 0, behavior: "instant" });
  };

  const showCurrentJob = () => {
    if (!job?.document_id || !jobId) return;
    if (!selectDocument(job.document_id, "import")) return;
    setShowUpload(false);
    setScrollTarget({ area: "job", documentId: job.document_id });
  };

  const locateUnit = (unit: ExtractedUnit) => {
    setPreviewPage(unit.page_number);
    setPreviewTime(unit.start_sec);
    document.querySelector(".original-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const activeDoc = selectedDocument && selectedDocument.id === selectedId ? selectedDocument : null;
  const visibleDocuments = documents.filter((doc) => doc.status !== "superseded");
  const hasSearchableDocument = visibleDocuments.some((doc) => doc.status === "indexed" && doc.original_available !== false && !doc.reindex_required);
  const hasIndexNeedingUpdate = visibleDocuments.some((doc) => doc.status === "indexed" && doc.reindex_required);
  const pendingDocument = visibleDocuments.find((doc) => doc.status === "draft" || doc.status === "reviewed" || doc.status === "failed");
  const staleDocument = visibleDocuments.find((doc) => doc.status === "indexed" && doc.reindex_required);
  const activeJob = job && activeDoc && job.document_id === activeDoc.id ? job : null;
  const showingJobError = page === "import" && activeJob?.status === "failed" && error === (activeJob.error || "処理に失敗しました。");
  const sourceUnit = comparison && activeDoc?.id === comparison.documentId
    ? activeDoc.units.find((unit) => unit.id === comparison.unitId)
    : null;
  const isOldVersion = activeDoc?.status === "superseded";
  const reviewable = Boolean(activeDoc && activeDoc.original_available !== false && activeDoc.units?.length && activeDoc.units.every((unit) => (drafts[unit.id] ?? "").trim()));
  const hasUnsavedChanges = Boolean(activeDoc?.units?.some((unit) => (drafts[unit.id] ?? "") !== (unit.corrected_text ?? unit.extracted_text)));
  const elapsed = job?.started_at && !Number.isNaN(new Date(job.started_at).getTime())
    ? Math.max(0, Math.floor(((job.finished_at ? new Date(job.finished_at).getTime() : now) - new Date(job.started_at).getTime()) / 1000))
    : null;
  const navigate = (target: Page) => {
    if (target !== page && page === "import" && hasUnsavedChanges && !window.confirm("保存していない修正があります。この画面を離れますか？")) return;
    setPage(target);
    setShowHelp(false);
    setScrollTarget(null);
    setComparison(null);
    clearMessages();
  };

  useEffect(() => {
    if (!showHelp) return;
    helpCloseRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") setShowHelp(false); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [showHelp]);

  useEffect(() => {
    if (!scrollTarget) return;
    if (scrollTarget.documentId && activeDoc?.id !== scrollTarget.documentId) return;
    const element = scrollTarget.area === "upload" ? uploadRef.current
      : scrollTarget.area === "job" ? jobRef.current ?? workflowRef.current
      : scrollTarget.area === "review" ? reviewRef.current
      : scrollTarget.area === "answer" ? answerRef.current
      : comparisonRef.current;
    if (!element) return;
    const frame = window.requestAnimationFrame(() => {
      element.scrollIntoView({ behavior: "instant", block: "start" });
      if (scrollTarget.area === "comparison" || scrollTarget.area === "answer") element.focus({ preventScroll: true });
      setScrollTarget(null);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [scrollTarget, activeDoc, activeJob, answer, page, showUpload, comparison]);

  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasUnsavedChanges]);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /></span>
          <div><strong>Museum<br />Multimodal Lab</strong><small>ローカル研究デモ</small></div>
        </div>
        <nav className="nav" aria-label="画面">
          {nav.map((item) => (
            <button key={item.id} type="button" className={`nav-item ${page === item.id ? "active" : ""}`} onClick={() => navigate(item.id)} aria-current={page === item.id ? "page" : undefined}>
              <Icon name={item.id} />{item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-indicator"><span className="local-dot" /> このPC内で処理</div>
          <p>{health && documentsLoaded ? `現行${visibleDocuments.length}件 · ${health.ollama?.available ? "ローカルAI接続中" : "AI未接続"}${health.stale_index_count ? ` · 要更新${health.stale_index_count}件` : ""}` : "状態を確認中"}</p>
        </div>
      </aside>

      <main className="main-content">
        <div className="topbar">
          <span className="topbar-caption">職員が確認して使う実験アプリ</span>
          <div className="topbar-actions">
            {job && jobId && <button className="topbar-job" type="button" onClick={showCurrentJob}>処理中 · 状況を見る</button>}
            <IconButton icon="help" tooltip={showHelp ? "使い方を閉じる" : "使い方を開く"} type="button" aria-expanded={showHelp} aria-controls="app-help" onClick={() => setShowHelp((current) => !current)} />
          </div>
        </div>
        {showHelp && <aside className="help-panel" id="app-help" aria-label="使い方">
          <div className="panel-heading"><div><p className="eyebrow">使い方</p><h2>6つの操作</h2></div><IconButton ref={helpCloseRef} icon="close" tooltip="使い方を閉じる" type="button" onClick={() => setShowHelp(false)} /></div>
          <ol><li>資料を追加</li><li>内容を読み取る</li><li>原本と照合し、必要なら修正</li><li>確認して知識に登録</li><li>資料に質問</li><li>根拠を元資料で確かめる</li></ol>
          <p>自動の読み取りは誤ることがあります。確認して登録した内容だけを質問に使います。回答は職員が確かめるための案です。</p>
          <details><summary>対応形式・待ち時間</summary><p>PDF（10ページ・20 MBまで）、PNG/JPEG、短いWAV音声に対応。画像や音声の読み取りは時間がかかる場合があります。工程と経過時間を画面に表示します。</p></details>
          <details><summary>再解析・保存について</summary><p>再解析が成功すると新しい結果の確認・登録が必要です。再解析に失敗した場合は以前の登録内容を保持します。検索用情報の更新は成功後に置き換えます。資料と処理結果はこのPC内に保存されます。</p></details>
          <details><summary>技術情報</summary><p>研究用のローカルデモです。外部AI APIは使用しません。画像：{health?.models?.vision ?? "未確認"}／音声：{health?.models?.asr ?? "未確認"}／回答：{health?.models?.answer ?? "未確認"}／検索：{health?.models?.embedding ?? "未確認"}。AIが未接続ならローカルモデルの起動を確認してください。</p></details>
        </aside>}
        {error && !showingJobError && <div className="banner banner-error" role="alert"><span>{error}</span><div className="banner-actions">{errorContext === "upload" && <button className="banner-retry" type="button" onClick={() => { setFile(null); setFileInputKey((current) => current + 1); setShowUpload(true); setPage("import"); setScrollTarget({ area: "upload" }); clearMessages(); }}>ファイルを選び直す</button>}<IconButton icon="close" tooltip="エラーを閉じる" type="button" onClick={() => { setError(null); setErrorContext(null); }} /></div></div>}
        {notice && <div className="banner banner-notice" role="status"><span>{notice}</span><IconButton icon="close" tooltip="通知を閉じる" type="button" onClick={() => setNotice(null)} /></div>}

        {page === "import" && (
          <div className="page-content import-page">
            <header className="page-header"><p className="eyebrow">01 / 取り込み</p><h1>{!activeDoc || activeJob && jobId ? "資料を取り込む" : activeDoc.units?.length ? activeDoc.status === "indexed" ? "資料を登録しました" : "内容を確認する" : "資料を読み取る"}</h1></header>
            {activeDoc && (
              <section className="workflow-section" ref={workflowRef}>
                <div className="section-title"><div><p className="eyebrow">現在の資料</p><h2>{activeDoc.name}</h2></div><span className={`status-pill ${activeJob && jobId ? "status-running" : documentStatusClass(activeDoc)}`}>{activeJob && jobId ? jobStatusLabel(activeJob, activeDoc) : documentStatus(activeDoc)}</span></div>
                <div className="workflow-toolbar"><span>{fileKind(activeDoc)} · 版 {activeDoc.version}</span>{!jobId && busy === null && <button className="inline-button" type="button" onClick={() => { setShowUpload(true); setScrollTarget({ area: "upload" }); }}>別の資料を追加</button>}</div>
                {activeDoc.warning && <p className="warning-note">{activeDoc.warning}</p>}
                {activeDoc.error && !jobId && activeJob?.status !== "failed" && <p className="warning-note">前回の処理: {activeDoc.error}</p>}
                {activeDoc.reindex_required && <p className="warning-note">検索モデルが変わりました。この資料の検索用知識を更新してください。</p>}
                {isOldVersion && <p className="old-version-note">この資料は旧版です。原本と抽出内容を閲覧できますが、検索には使われません。</p>}
                {activeJob && jobId && <div className={`job-state ${activeJob.status}`} role="status" aria-live="polite" ref={jobRef}><div><strong>{jobStage(activeJob)}</strong><span>{elapsed != null ? `経過 ${elapsed} 秒` : "受付中"}</span></div>{activeJob.kind === "approve" && activeDoc.status === "indexed" && <p>更新が成功するまで、以前の検索情報を保持します。</p>}</div>}
                {activeJob?.status === "failed" && !jobId && <div className="job-state failed" role="alert" ref={jobRef}><strong>{jobStage(activeJob)}</strong><p>{activeJob.error || "資料の状態を確認してください。"}</p>{activeJob.kind === "extract" ? <button className="button button-quiet" type="button" disabled={busy !== null} onClick={() => void extract()}>もう一度読み取る</button> : activeDoc.status === "indexed" ? <button className="button button-quiet" type="button" disabled={busy !== null} onClick={() => void reindex()}>検索情報をもう一度更新</button> : <button className="button button-quiet" type="button" onClick={() => setScrollTarget({ area: "review", documentId: activeDoc.id })}>内容を確認する</button>}</div>}
                {!activeDoc.units?.length && !jobId && <div className="step-block" ref={jobRef}><div><h3>内容を読み取る</h3><p>読み取り後に原本と照らし合わせます。</p></div><button className="button button-primary" type="button" disabled={busy !== null || isOldVersion || activeDoc.original_available === false} onClick={() => void extract()}>{busy === "extract" ? "開始中…" : "読み取りを開始"}</button></div>}
                {activeDoc.status === "indexed" && !jobId && <div className={`registered-next ${activeDoc.reindex_required ? "needs-update" : ""}`} ref={activeJob?.status === "failed" ? undefined : jobRef}><div><strong>{activeDoc.reindex_required ? "検索情報の更新が必要です" : "知識に登録済み"}</strong><p>{activeDoc.reindex_required ? "更新後に質問できます。" : "この資料について質問できます。"}</p></div><button className="button button-primary" type="button" disabled={busy !== null || activeDoc.original_available === false} onClick={() => activeDoc.reindex_required ? void reindex() : navigate("ask")}>{activeDoc.reindex_required ? "検索情報を更新" : "資料に質問する"}</button></div>}
                {activeDoc.units?.length > 0 && (
                  <>
                    <div className="step-heading" ref={reviewRef}><span className="step-number">2</span><div><h3>元資料と照らし合わせる</h3><p>数値や固有名詞を確認してください。</p></div></div>
                    <div className="review-grid">
                      <OriginalPreview doc={activeDoc} page={previewPage} time={previewTime} onPageChange={setPreviewPage} />
                      <div className="unit-list">{activeDoc.units.map((unit) => <UnitEditor key={unit.id} unit={unit} draft={drafts[unit.id] ?? unit.corrected_text ?? unit.extracted_text} onDraft={(value) => { setDrafts((current) => ({ ...current, [unit.id]: value })); setReviewConfirmed(false); }} onSave={() => void saveUnit(unit)} onLocate={() => locateUnit(unit)} saving={busy === unit.id} readOnly={isOldVersion || Boolean(jobId)} />)}</div>
                    </div>
                    {activeDoc.status !== "indexed" && <div className="approve-panel">
                      <span className="step-number">3</span>
                      <div>
                        <h3>確認して知識に登録</h3>
                        <p>確認した内容だけを質問に使います。</p>
                        <label className="check-row"><input type="checkbox" disabled={isOldVersion || activeDoc.original_available === false || Boolean(jobId)} checked={reviewConfirmed} onChange={(event) => setReviewConfirmed(event.target.checked)} /> 元資料と抽出内容を確認しました</label>
                        {!isOldVersion && hasUnsavedChanges && <p className="warning-note">保存していない修正があります。先に各テキストを保存してください。</p>}
                      </div>
                      <button className="button button-primary" type="button" disabled={isOldVersion || !reviewConfirmed || !reviewable || hasUnsavedChanges || Boolean(jobId) || busy !== null} onClick={() => void approve()}>{busy === "approve" ? "登録中…" : "知識に登録"}</button>
                    </div>}
                  </>
                )}
                {!isOldVersion && activeDoc.units?.length > 0 && <details className="secondary-operations"><summary>この資料の操作</summary><button className="button button-quiet" type="button" disabled={Boolean(jobId) || busy !== null || activeDoc.original_available === false} onClick={() => void extract()}>再解析する</button><span>再解析が完了すると、新しい内容の確認・登録が必要です。失敗時は以前の登録内容を保持します。</span></details>}
                <details className="technical-details"><summary>資料の詳細</summary><p>版 {activeDoc.version} · 抽出結果 {activeDoc.units?.length ?? 0}件 · 読み取り方法 {activeDoc.extraction_model ?? "未解析"}</p></details>
              </section>
            )}
            {(!activeDoc || showUpload) && !jobId && <section className="panel upload-panel" ref={uploadRef}>
              <div className="panel-heading"><h2>{activeDoc ? "別の資料を追加" : "新しい資料"}</h2>{activeDoc && <button className="inline-button" type="button" onClick={() => setShowUpload(false)}>閉じる</button>}</div>
              <label className="dropzone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); setFile(event.dataTransfer.files[0] ?? null); }}>
                <input key={fileInputKey} type="file" accept=".pdf,.png,.jpg,.jpeg,.wav,application/pdf,image/png,image/jpeg,audio/wav" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
                <span className="dropzone-icon" aria-hidden="true">＋</span><strong>{file ? file.name : "ファイルを選ぶ、またはここへドロップ"}</strong>
                <small>{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB` : "PDF・画像・短いWAV音声（20 MBまで）"}</small>
              </label>
              <div className="actions-right"><button className="button button-primary" type="button" disabled={!file || busy !== null || Boolean(jobId)} onClick={() => void upload()}>{busy === "upload" ? "受け付け中…" : "資料を追加"}</button></div>
            </section>}
          </div>
        )}

        {page === "documents" && (
          <div className="page-content">
            <header className="page-header"><p className="eyebrow">02 / 保存した資料</p><h1>{comparison ? comparison.fromClarification ? "候補の元資料を確認" : "根拠を元資料で確認" : "保存した資料"}</h1>{!comparison && <p>確認して登録した資料だけが質問に使えます。</p>}</header>
            <div className={`library-layout ${comparison ? "comparing" : ""}`}>
              {!comparison && <section className="document-list panel" aria-label="資料一覧">
                <div className="panel-heading"><h2>資料一覧</h2><span className="subtle-label">{documents.length}件</span></div>
                {documents.length === 0 ? <div className="empty-state"><strong>まだ資料がありません</strong><p>「資料を取り込む」から追加できます。</p><button className="button button-quiet" type="button" onClick={() => navigate("import")}>資料を追加する</button></div> : documents.map((doc) => <button className={`document-row ${selectedId === doc.id ? "selected" : ""}`} type="button" key={doc.id} onClick={() => selectDocument(doc.id)}><span className="document-kind">{fileKind(doc)}</span><span className="document-row-body"><strong>{doc.name}</strong><small>{jobId && job?.document_id === doc.id ? jobStatusLabel(job, doc) : documentStatus(doc)} · 版 {doc.version} · {doc.units?.length ?? 0}件 · {dateText(doc.updated_at)}</small></span><span aria-hidden="true">›</span></button>)}
              </section>}
              <section className="document-detail">
                {!activeDoc ? <div className="panel empty-state"><strong>{comparison ? error ? "資料を開けませんでした" : "資料を読み込んでいます" : "資料を選んでください"}</strong><p>原本と抽出内容をここで確認できます。</p>{comparison && error && <button className="button button-quiet" type="button" onClick={returnFromComparison}>戻る</button>}</div> : comparison ? <div className="comparison-view">
                  <div className="comparison-toolbar" ref={comparisonRef} tabIndex={-1}>
                    <button className="inline-button" type="button" onClick={returnFromComparison}>{comparison.fromAnswer ? "← 質問結果へ戻る" : "← 資料一覧へ戻る"}</button>
                    <div><strong>{activeDoc.name}</strong><span>{sourceUnit ? `${fileKind(activeDoc)} · ${locationText(sourceUnit)}` : fileKind(activeDoc)}</span></div>
                  </div>
                  {sourceUnit ? <div className="source-comparison-grid">
                    <OriginalPreview doc={activeDoc} page={previewPage ?? comparison.page} time={previewTime ?? comparison.time} onPageChange={setPreviewPage} />
                    <section className="panel comparison-text" aria-label={comparison.fromClarification ? "候補資料の内容" : comparison.fromAnswer ? "回答に使った資料の内容" : "確認済みの資料の内容"}>
                      <h2>{comparison.fromClarification ? "候補資料の内容" : comparison.fromAnswer ? "回答に使った箇所" : "確認済みの内容"}</h2>
                      <p className="comparison-location">{kindLabels[sourceUnit.kind] ?? sourceUnit.kind} · {locationText(sourceUnit)}</p>
                      <blockquote>{comparison.snippet ?? sourceUnit.corrected_text ?? sourceUnit.extracted_text}</blockquote>
                      {comparison.snippet && comparison.snippet !== (sourceUnit.corrected_text ?? sourceUnit.extracted_text) && <details><summary>登録した全文を見る</summary><p>{sourceUnit.corrected_text ?? sourceUnit.extracted_text}</p></details>}
                      {sourceUnit.kind !== "pdf_text" && <p className="comparison-caution">画像・音声の内容は読み取り結果です。原本と照らし合わせてください。</p>}
                    </section>
                  </div> : <div className="panel comparison-missing" role="alert">参照した内容がこの資料に見つかりません。出典の状態を確認してください。</div>}
                </div> : <>
                  {activeDoc.units?.length > 0 && <div className="detail-compare-action"><button className="button button-quiet" type="button" onClick={() => compareDocument(activeDoc)}>元資料と内容を比較する</button></div>}
                  <div className="panel detail-summary">
                    <div className="panel-heading"><div><p className="eyebrow">資料</p><h2>{activeDoc.name}</h2></div><span className={`status-pill ${documentStatusClass(activeDoc)}`}>{documentStatus(activeDoc)}</span></div>
                    <div className="meta-strip"><span>{fileKind(activeDoc)}</span><span>版 {activeDoc.version}</span><span>{activeDoc.units?.length ?? 0}件の抽出結果</span><span>更新 {dateText(activeDoc.updated_at)}</span></div>
                    {activeDoc.reindex_required && <p className="warning-note">検索情報の更新が必要です。更新後に質問できます。</p>}
                    {activeDoc.error && <p className="warning-note">前回の処理: {activeDoc.error}</p>}
                    <div className="detail-actions">
                      <button className="button button-quiet" type="button" onClick={() => navigate("import")}>{isOldVersion ? "抽出内容を見る" : "確認・編集する"}</button>
                      <details className="management-menu"><summary>資料の操作</summary><div>{activeDoc.status === "indexed" && <button className="button button-quiet" type="button" disabled={busy !== null || Boolean(jobId) || activeDoc.original_available === false} onClick={() => void reindex()}>{busy === "reindex" ? "更新中…" : "検索情報を更新"}</button>}<button className="button button-danger" type="button" disabled={busy !== null || Boolean(jobId)} onClick={() => void deleteDocument()}>資料を削除</button></div></details>
                    </div>
                  </div>
                  <OriginalPreview doc={activeDoc} page={previewPage} time={previewTime} onPageChange={setPreviewPage} />
                  <div className="panel unit-summary"><div className="panel-heading"><h3>抽出した内容</h3><span className="subtle-label">{activeDoc.units?.length ?? 0}件</span></div>{activeDoc.units?.length ? activeDoc.units.map((unit) => <div className="read-unit" key={unit.id}><div><span className="kind-tag">{kindLabels[unit.kind] ?? unit.kind}</span><span className="muted-small">{locationText(unit)}</span></div><p>{unit.corrected_text ?? unit.extracted_text}</p><button className="inline-button" type="button" onClick={() => locateUnit(unit)}>元資料の位置を見る ↗</button></div>) : <p className="muted">まだ抽出結果がありません。「確認・編集する」から処理を始めてください。</p>}</div>
                  {Boolean(activeDoc.review_history?.length) && <details className="panel review-history"><summary>修正・登録の履歴（{activeDoc.review_history?.length}件）</summary><div>{activeDoc.review_history?.map((record) => <article key={record.id}><strong>{record.action === "edited" ? "抽出結果を修正" : "知識に登録"}</strong><time>{dateTimeText(record.created_at)}</time>{record.action === "edited" && <div className="review-change"><p><span>修正前</span>{record.previous_text}</p><p><span>修正後</span>{record.new_text}</p></div>}</article>)}</div></details>}
                </>}
              </section>
            </div>
          </div>
        )}

        {page === "ask" && (
          <div className="page-content ask-page">
            <header className="page-header"><p className="eyebrow">03 / 質問</p><h1>資料に質問する</h1></header>
            {!documentsLoaded ? <div className="panel empty-state ask-empty" role="status">資料を確認しています…</div> : !hasSearchableDocument ? <div className="panel empty-state ask-empty">
              <strong>{staleDocument ? "検索情報の更新が必要です" : "まだ質問に使える資料がありません"}</strong>
              <p>{staleDocument ? "検索情報を更新すると質問できます。" : pendingDocument ? "読み取った内容を確認して、知識に登録してください。" : "資料を追加して、内容を確認してください。"}</p>
              <button className="button button-primary" type="button" onClick={() => pendingDocument ? selectDocument(pendingDocument.id, "import") : staleDocument ? selectDocument(staleDocument.id, "documents") : navigate("import")}>{pendingDocument ? "内容を確認して登録" : staleDocument ? "資料を確認する" : "資料を追加する"}</button>
            </div> : <>
              {hasIndexNeedingUpdate && <p className="warning-note ask-index-warning">一部資料の検索情報は更新が必要です。質問を保留する場合があります。</p>}
              <section className={`panel question-panel ${answer ? "has-answer" : ""}`}>
                <label className="field-label" htmlFor="question">質問</label>
                <textarea id="question" placeholder="例：展示の体験時間は何分ですか？" value={question} maxLength={1500} disabled={busy === "ask"} onChange={(event) => { setQuestion(event.target.value); setAnswer(null); if (errorContext === "ask") { setError(null); setErrorContext(null); } }} onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && busy === null) void ask(); }} />
                <details className="retrieval-options">
                  <summary>検索方法</summary>
                  <div className="retrieval-options-content">
                    <label htmlFor="retrieval-mode">資料の探し方</label>
                    <select id="retrieval-mode" value={selectedRetrievalMode} disabled={busy === "ask"} onChange={(event) => { setRetrievalMode(event.target.value as RetrievalMode); setAnswer(null); if (errorContext === "ask") { setError(null); setErrorContext(null); } }}>
                      {retrievalModes.map((mode) => <option key={mode} value={mode}>{retrievalModeLabels[mode]}</option>)}
                    </select>
                    <p>このアプリで選べる方法を表示します。「意味の近さ」「語句＋意味」にはローカルQdrantが必要です。</p>
                    {health?.visual_retrieval === "needs_reindex" && <p>画像で探すには、以前に登録した画像やPDFの「検索情報を更新」を実行してください。</p>}
                    {health?.visual_retrieval === "setup_failed" && <p>画像検索モデルを読み込めませんでした。文字による検索は利用できます。</p>}
                  </div>
                </details>
                <div className="question-footer"><span className="muted-small">Ctrl + Enter でも質問できます</span><button className="button button-primary" type="button" disabled={!question.trim() || busy !== null || Boolean(jobId)} onClick={() => void ask()}>{busy === "ask" ? "調査中…" : "資料から調べる"}</button></div>
                {busy === "ask" && <p className="question-progress" role="status">登録資料から根拠を探し、回答を作成しています。しばらくお待ちください。</p>}
              </section>
              {answer && <section className="answer-section" id="answer-result" ref={answerRef} tabIndex={-1}>
                <div className="section-title"><h2>{answer.status === "held" ? "登録資料からは確認できません" : answer.status === "needs_clarification" ? "対象を確認してください" : "回答案"}</h2>{answer.status === "answered" && <span className="subtle-label">利用前に根拠を確認</span>}</div>
                <div className={`answer-card ${answer.status === "held" ? "held" : answer.status === "needs_clarification" ? "clarification" : ""}`}><p>{answer.answer || (answer.status === "held" ? "登録資料からは確認できません。" : "回答を表示できませんでした。")}</p></div>
                {answer.status === "held" && <p className="held-guidance">質問を言い換えるか、関連する資料を確認・登録してください。</p>}
                {answer.sources?.length > 0 && <><div className="section-title source-title"><h2>{answer.status === "needs_clarification" ? "候補資料" : "根拠"}</h2><span className="subtle-label">{answer.sources.length}件</span></div><div className="sources-grid">{answer.sources.map((source) => <SourceCard key={source.chunk_id} source={source} candidate={answer.status === "needs_clarification"} onOpen={() => openSource(source)} />)}</div></>}
                {Boolean(answer.retrieval_trace?.length) && <details className="retrieval-trace">
                  <summary>検索の記録</summary>
                  <ol>{answer.retrieval_trace?.map((entry, index) => <li key={`${entry.action}-${index}`}>
                    <strong>{entry.action}</strong>
                    {entry.mode && <span>{Object.hasOwn(retrievalModeLabels, entry.mode) ? retrievalModeLabels[entry.mode as RetrievalMode] : entry.mode}</span>}
                    {entry.candidate_count != null && <span>候補 {entry.candidate_count}件</span>}
                    {entry.elapsed_ms != null && <span>{Math.round(entry.elapsed_ms)} ms</span>}
                    {entry.reason && <p>{entry.reason}</p>}
                  </li>)}</ol>
                </details>}
              </section>}
            </>}
          </div>
        )}
      </main>
    </div>
  );
}
