import React, { useState, useEffect, useRef } from "react";
import {
  AskResponse,
  ShelfFile,
  ShelfGroup,
  askYourShelves,
  cleanPreviewText,
  fetchFileFullExcerpt,
  getFileRawUrl,
  getPdfPageUrl,
  moveFileToFolder,
  openFileInOS,
  pickFolderNative,
  revealFileInOS,
  resolveLocalAssetSrc,
  setFileTags,
  togglePinFile,
  trashFile,
} from "../lib/tauri-api";
import { formatShortcut, joinDisplayPath } from "../lib/platform";
import { useFocusTrap } from "../lib/use-focus-trap";

interface InspectorSheetProps {
  file: ShelfFile | undefined;
  shelves?: ShelfGroup[];
  quickLookOpen: boolean;
  initialAnchor?: {
    pageNumber?: number;
    slideNumber?: number;
    lineNumber?: number;
    sheetName?: string;
  } | null;
  onCloseQuickLook: () => void;
  onStepQuickLook?: (dir: -1 | 1) => void;
  onFindSimilar?: (file: ShelfFile) => void;
  onAskAboutFile?: (file: ShelfFile) => void;
  onInspectFile?: (fileId: string) => void;
  onAssignShelf?: (fileId: string, shelfId: string) => Promise<void> | void;
  onOpenRenameDialog?: (file: ShelfFile) => void;
  onOpenMoveDialog?: (file: ShelfFile, mode?: "move" | "copy") => void;
  onOpenDeleteDialog?: (file: ShelfFile) => void;
  onToggleRename: (fileId: string) => void;
  onCopyPath: (file: ShelfFile) => void;
  onShowToast: (msg: string) => void;
  onRefreshState?: (state: any) => void;
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  PHP: "₱",
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
};

export function extractFileEntities(file: ShelfFile): Array<{ label: string; value: string }> {
  const meta = file.metadata || {};
  const items: Array<{ label: string; value: string }> = [];
  const seenLabels = new Set<string>();
  const seenValues = new Set<string>();

  const pushEntity = (label: string, rawVal: unknown) => {
    if (rawVal === null || rawVal === undefined) return;
    const val = String(rawVal).trim();
    if (!val) return;
    const labelKey = label.toLowerCase();
    const valKey = `${labelKey}:${val.toLowerCase()}`;
    if (seenLabels.has(labelKey) || seenValues.has(valKey)) return;
    seenLabels.add(labelKey);
    seenValues.add(valKey);
    items.push({ label, value: val });
  };

  if (typeof meta.doc_type === "string" && meta.doc_type.trim()) {
    const dt = meta.doc_type.trim();
    if (!["unknown", "other", "general"].includes(dt.toLowerCase())) {
      const formatted = dt.charAt(0).toUpperCase() + dt.slice(1).replace(/_/g, " ");
      pushEntity("Document type", formatted);
    }
  }

  if (meta.merchant) {
    pushEntity("Merchant", meta.merchant);
  }

  if (meta.amount !== undefined && meta.amount !== null && String(meta.amount).trim() !== "") {
    const rawAmt = String(meta.amount).trim();
    const rawCur = typeof meta.currency === "string" ? meta.currency.trim().toUpperCase() : "";
    const sym = CURRENCY_SYMBOLS[rawCur] || rawCur;
    const hasSymbolAlready = /^[₱$€£¥]|^(?:PHP|USD|EUR|GBP|JPY)\b/i.test(rawAmt);
    const formattedAmt = hasSymbolAlready
      ? rawAmt
      : sym
      ? `${sym}${sym.length > 1 ? " " : ""}${rawAmt}`
      : rawAmt;
    pushEntity("Amount", formattedAmt);
  }

  if (meta.date) {
    pushEntity("Date", meta.date);
  }

  if (meta.due_date) {
    pushEntity("Due date", meta.due_date);
  }

  if (meta.invoice_number) {
    pushEntity("Invoice / Ref #", meta.invoice_number);
  }

  if (meta.email) {
    pushEntity("Email", meta.email);
  }

  if (meta.phone) {
    pushEntity("Phone", meta.phone);
  }

  if (meta.payment_method) {
    pushEntity("Payment method", meta.payment_method);
  }

  if (meta.tax_amount !== undefined && meta.tax_amount !== null && String(meta.tax_amount).trim() !== "") {
    const rawCur = typeof meta.currency === "string" ? meta.currency.trim().toUpperCase() : "";
    const sym = CURRENCY_SYMBOLS[rawCur] || rawCur;
    pushEntity("Tax / VAT", sym ? `${sym}${sym.length > 1 ? " " : ""}${meta.tax_amount}` : String(meta.tax_amount));
  }

  if (meta.author_or_person) {
    pushEntity("Author / Person", meta.author_or_person);
  }

  if (Array.isArray(meta.action_items) && meta.action_items.length > 0) {
    pushEntity("Action items", `${meta.action_items.length} item(s)`);
  }

  const receiptFields = (meta.receiptFields || {}) as Record<string, string>;
  for (const [k, v] of Object.entries(receiptFields)) {
    pushEntity(k, v);
  }

  return items;
}

function renderInlineMd(text: string): React.ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g);
  return parts.map((part, idx) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={idx} style={{ color: "var(--ink)", fontWeight: 600 }}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return (
        <code
          key={idx}
          className="font-code-raw"
          style={{
            background: "var(--bg-subtle)",
            padding: "1px 5px",
            borderRadius: "4px",
            fontSize: "0.9em",
          }}
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      return <em key={idx}>{part.slice(1, -1)}</em>;
    }
    return part;
  });
}

const MarkdownPreviewBlock: React.FC<{ source: string; expanded: boolean }> = ({ source, expanded }) => {
  const lines = source.split("\n").slice(0, expanded ? 300 : 80);
  return (
    <div className="md-preview-body" style={{ maxHeight: expanded ? "440px" : "220px", overflowY: "auto" }}>
      {lines.map((line, i) => {
        const t = line.trim();
        if (t.startsWith("### ")) return <h4 key={i} className="font-display" style={{ fontSize: "14px", fontWeight: 600, margin: "8px 0 3px" }}>{renderInlineMd(t.slice(4))}</h4>;
        if (t.startsWith("## ")) return <h3 key={i} className="font-display" style={{ fontSize: "16px", fontWeight: 500, margin: "10px 0 4px" }}>{renderInlineMd(t.slice(3))}</h3>;
        if (t.startsWith("# ")) return <h2 key={i} className="font-display" style={{ fontSize: "19px", fontWeight: 500, margin: "10px 0 5px" }}>{renderInlineMd(t.slice(2))}</h2>;
        if (t.startsWith("- ") || t.startsWith("* ")) {
          return <div key={i} style={{ paddingLeft: "14px", fontSize: "13px", color: "var(--ink-2)" }}>• {renderInlineMd(t.slice(2))}</div>;
        }
        if (t.startsWith("> ")) {
          return (
            <blockquote key={i} style={{ borderLeft: "2px solid var(--ink-3)", paddingLeft: "10px", color: "var(--ink-2)", fontStyle: "italic", margin: "4px 0", fontSize: "13px" }}>
              {renderInlineMd(t.slice(2))}
            </blockquote>
          );
        }
        if (!t) return <div key={i} style={{ height: "6px" }} />;
        return <p key={i} style={{ fontSize: "13px", color: "var(--ink-2)", lineHeight: 1.55, margin: "2px 0" }}>{renderInlineMd(line)}</p>;
      })}
    </div>
  );
};

export const SpecimenPreview: React.FC<{
  file: ShelfFile;
  expanded?: boolean;
  initialAnchor?: {
    pageNumber?: number;
    slideNumber?: number;
    lineNumber?: number;
    sheetName?: string;
  } | null;
  onCopyText?: (text: string, label: string) => void;
}> = ({
  file,
  expanded = false,
  initialAnchor = null,
  onCopyText,
}) => {
  const imgSrc = resolveLocalAssetSrc(file);
  const meta = file.metadata || {};
  const [pdfPage, setPdfPage] = useState(
    initialAnchor?.pageNumber && initialAnchor.pageNumber > 0 ? initialAnchor.pageNumber - 1 : 0
  );
  const [pdfMode, setPdfMode] = useState<"image" | "text">("image");
  const [htmlPreviewMode, setHtmlPreviewMode] = useState<"code" | "render">("code");
  const [mdMode, setMdMode] = useState<"render" | "source">("render");
  const [imgFailed, setImgFailed] = useState(false);
  const [imgZoom, setImgZoom] = useState<number>(1);
  const [showOcrOverlay, setShowOcrOverlay] = useState<boolean>(false);
  const [activeSheet, setActiveSheet] = useState<string>(
    initialAnchor?.sheetName || String(meta.activeSheet || "Sheet1")
  );
  const [tableFilter, setTableFilter] = useState<string>("");
  const [slideIdx, setSlideIdx] = useState<number>(
    initialAnchor?.slideNumber && initialAnchor.slideNumber > 0 ? initialAnchor.slideNumber - 1 : 0
  );
  const [archiveFilter, setArchiveFilter] = useState<string>("");
  const [fullExcerpt, setFullExcerpt] = useState<string | null>(null);
  const [prettyJson, setPrettyJson] = useState<boolean>(false);

  useEffect(() => {
    setPdfPage(
      initialAnchor?.pageNumber && initialAnchor.pageNumber > 0
        ? Math.max(0, initialAnchor.pageNumber - 1)
        : 0
    );
    setPdfMode("image");
    setHtmlPreviewMode("code");
    setMdMode("render");
    setImgFailed(false);
    setImgZoom(1);
    setShowOcrOverlay(false);
    setActiveSheet(initialAnchor?.sheetName || String((file.metadata || {}).activeSheet || "Sheet1"));
    setTableFilter("");
    setSlideIdx(
      initialAnchor?.slideNumber && initialAnchor.slideNumber > 0
        ? Math.max(0, initialAnchor.slideNumber - 1)
        : 0
    );
    setArchiveFilter("");
    setFullExcerpt(null);
    setPrettyJson(false);
  }, [
    file.id,
    initialAnchor?.pageNumber,
    initialAnchor?.slideNumber,
    initialAnchor?.lineNumber,
    initialAnchor?.sheetName,
  ]);

  const fileRef = useRef(file);
  fileRef.current = file;

  useEffect(() => {
    const curFile = fileRef.current;
    const extLower = (curFile.format || "").toLowerCase();
    const kindLower = String(meta.kind || "").toLowerCase();
    const needsExcerpt =
      curFile.specimen === "code" ||
      ["markdown", "text", "docx", "doc", "odt", "rtf", "pdf"].includes(kindLower) ||
      ["json", "txt", "docx", "doc", "odt", "rtf", "log", "xml", "yaml", "yml", "toml", "csv", "tsv", "md"].includes(extLower);
    if (!needsExcerpt) return;
    let cancelled = false;
    fetchFileFullExcerpt(curFile, expanded ? 450 : 120)
      .then((res) => {
        if (!cancelled && res.content) setFullExcerpt(res.content);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [expanded, file.id, file.mtime, file.format, file.specimen, meta.kind]);

  if (meta.kind === "archive" && Array.isArray(meta.entries)) {
    const rawEntries = meta.entries as Array<{ name: string; sizeBytes: number; isDir: boolean; ext: string }>;
    const entries = rawEntries.filter(
      (e) => !archiveFilter.trim() || e.name.toLowerCase().includes(archiveFilter.trim().toLowerCase())
    );
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>ARCHIVE · {String(meta.format || file.format).toUpperCase()}</span>
          <span>{meta.totalEntries ?? entries.length} entries</span>
        </div>
        {(expanded || rawEntries.length > 5 || archiveFilter.trim().length > 0) && (
          <input
            type="text"
            aria-label="Filter files inside archive"
            value={archiveFilter}
            onChange={(e) => setArchiveFilter(e.target.value)}
            placeholder="Filter files inside archive…"
            className="dialog-input font-mono-code"
            style={{ padding: "5px 9px", fontSize: "11.5px", marginBottom: "8px" }}
          />
        )}
        <div style={{ maxHeight: expanded ? "360px" : "200px", overflowY: "auto", fontSize: "11.5px" }}>
          {entries.slice(0, expanded ? 120 : 30).map((e, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: "10px",
                padding: "4px 6px",
                borderBottom: "1px solid var(--rule-soft)",
                color: e.isDir ? "var(--ink-3)" : "var(--ink)",
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {e.isDir ? `${e.name}/` : e.name}
              </span>
              <span style={{ color: "var(--ink-3)", flexShrink: 0 }}>
                {e.isDir ? "dir" : e.sizeBytes >= 1024 ? `${Math.round(e.sizeBytes / 1024)} KB` : `${e.sizeBytes} B`}
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (meta.kind === "pptx" && Array.isArray(meta.slides) && meta.slides.length > 0) {
    const slides = meta.slides as Array<{ index: number; title: string; text: string }>;
    const cur = slides[Math.min(slideIdx, slides.length - 1)];
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>SLIDES · {slides.length} DECK</span>
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <button
              type="button"
              className="btn-tactile sm"
              disabled={slideIdx <= 0}
              onClick={() => setSlideIdx((s) => Math.max(0, s - 1))}
            >
              Prev
            </button>
            <span className="tabular-nums">{slideIdx + 1}/{slides.length}</span>
            <button
              type="button"
              className="btn-tactile sm"
              disabled={slideIdx >= slides.length - 1}
              onClick={() => setSlideIdx((s) => Math.min(slides.length - 1, s + 1))}
            >
              Next
            </button>
          </div>
        </div>
        <div style={{ padding: "12px", borderRadius: "var(--radius-sm)", border: "1px solid var(--rule)", background: "var(--bg-surface)" }}>
          <div className="font-display" style={{ fontSize: "16px", fontWeight: 500, marginBottom: "6px" }}>
            {cur.title}
          </div>
          <p style={{ fontSize: "13px", color: "var(--ink-2)", lineHeight: 1.55 }}>{cur.text}</p>
        </div>
      </div>
    );
  }

  if (file.modality === "video") {
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>VIDEO · {file.format.toUpperCase()}</span>
          <span>
            {meta.durationLabel || ""} {meta.width ? `· ${meta.width}×${meta.height}` : ""}
          </span>
        </div>
        <video
          key={file.id}
          controls
          preload="metadata"
          poster={imgSrc || undefined}
          src={getFileRawUrl(file.id)}
          style={{
            width: "100%",
            maxHeight: expanded ? "440px" : "220px",
            borderRadius: "var(--radius-sm)",
            background: "#000",
          }}
        />
        {meta.codec && (
          <div style={{ fontSize: "11px", color: "var(--ink-3)", marginTop: "6px" }}>
            Codec: {meta.codec} {meta.bitrateKbps ? `· ${meta.bitrateKbps} kbps` : ""}
          </div>
        )}
      </div>
    );
  }

  if (file.modality === "audio") {
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>AUDIO · {file.format.toUpperCase()}</span>
          <span>{meta.durationLabel || file.size}</span>
        </div>
        <audio key={file.id} controls src={getFileRawUrl(file.id)} style={{ width: "100%" }} />
        {meta.codec && (
          <div style={{ fontSize: "11px", color: "var(--ink-3)", marginTop: "8px" }}>
            Codec: {meta.codec} {meta.bitrateKbps ? `· ${meta.bitrateKbps} kbps` : ""}
          </div>
        )}
      </div>
    );
  }

  if (meta.kind === "spreadsheet" && Array.isArray(meta.headers)) {
    const sheets = Array.isArray(meta.sheets) ? (meta.sheets as string[]) : [String(meta.activeSheet || "Sheet1")];
    const sheetsData = (meta.sheetsData || {}) as Record<string, { headers: string[]; rows: string[][]; totalRows: number; totalCols: number }>;
    const curSheet = sheetsData[activeSheet] || {
      headers: meta.headers as string[],
      rows: Array.isArray(meta.rows) ? (meta.rows as string[][]) : [],
      totalRows: meta.totalRows ?? 0,
      totalCols: meta.totalCols ?? 0,
    };
    const headers = curSheet.headers || [];
    const allRows = curSheet.rows || [];
    const filteredRows = tableFilter.trim()
      ? allRows.filter((r) => r.some((c) => String(c).toLowerCase().includes(tableFilter.trim().toLowerCase())))
      : allRows;

    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>SPREADSHEET · {activeSheet}</span>
          <span>
            {curSheet.totalRows ?? filteredRows.length} rows × {curSheet.totalCols ?? headers.length} cols
          </span>
        </div>
        {sheets.length > 1 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", marginBottom: "8px" }}>
            {sheets.map((s) => (
              <button
                key={s}
                type="button"
                className={`seg-btn ${activeSheet === s ? "active" : ""}`}
                style={{ padding: "2px 8px", fontSize: "11px", border: "1px solid var(--rule)" }}
                onClick={() => setActiveSheet(s)}
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {(expanded || allRows.length > 5 || tableFilter.trim().length > 0) && (
          <input
            type="text"
            aria-label="Filter spreadsheet rows"
            value={tableFilter}
            onChange={(e) => setTableFilter(e.target.value)}
            placeholder="Filter rows…"
            className="dialog-input font-mono-code"
            style={{ padding: "5px 9px", fontSize: "11.5px", marginBottom: "8px" }}
          />
        )}
        <div style={{ overflowX: "auto", maxHeight: expanded ? "400px" : "220px" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "11.5px", fontVariantNumeric: "tabular-nums" }}>
            <thead>
              <tr>
                {headers.map((h, i) => (
                  <th
                    key={i}
                    style={{
                      textAlign: "left",
                      padding: "5px 8px",
                      borderBottom: "1px solid var(--rule-strong)",
                      background: "var(--bg-surface)",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {h || `Col ${i + 1}`}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filteredRows.slice(0, expanded ? 35 : 12).map((r, ri) => (
                <tr key={ri}>
                  {headers.map((_, ci) => (
                    <td
                      key={ci}
                      style={{
                        padding: "4px 8px",
                        borderBottom: "1px solid var(--rule-soft)",
                        color: "var(--ink-2)",
                        whiteSpace: "nowrap",
                        maxWidth: expanded ? "240px" : "140px",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {r[ci] ?? ""}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  if (file.format === "pdf") {
    const totalPages = Math.max(1, file.pageCount || 1);
    const pdfText = cleanPreviewText(fullExcerpt || file.extractedText);
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>PDF · {totalPages} {totalPages === 1 ? "PAGE" : "PAGES"}</span>
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            {pdfMode === "text" && onCopyText && pdfText && (
              <button
                type="button"
                className="btn-tactile sm"
                onClick={() => onCopyText(pdfText, "PDF text")}
              >
                Copy text
              </button>
            )}
            <button
              type="button"
              className="btn-tactile sm"
              onClick={() => setPdfMode((m) => (m === "image" ? "text" : "image"))}
            >
              {pdfMode === "image" ? "Read text" : "Page view"}
            </button>
            {totalPages > 1 && pdfMode === "image" && (
              <>
                <button
                  type="button"
                  className="btn-tactile sm"
                  disabled={pdfPage <= 0}
                  onClick={() => setPdfPage((p) => Math.max(0, p - 1))}
                >
                  Prev
                </button>
                <span className="tabular-nums">{pdfPage + 1}/{totalPages}</span>
                <button
                  type="button"
                  className="btn-tactile sm"
                  disabled={pdfPage >= totalPages - 1}
                  onClick={() => setPdfPage((p) => Math.min(totalPages - 1, p + 1))}
                >
                  Next
                </button>
              </>
            )}
          </div>
        </div>
        {pdfMode === "text" ? (
          <div style={{ maxHeight: expanded ? "440px" : "240px", overflowY: "auto", fontSize: "13px", lineHeight: 1.6, color: "var(--ink-2)", whiteSpace: "pre-wrap" }}>
            {cleanPreviewText(fullExcerpt || file.extractedText) || "(No embedded text found in PDF)"}
          </div>
        ) : (
          <>
            <div
              style={{
                marginBottom: "10px",
                borderRadius: "var(--radius-sm)",
                overflow: "hidden",
                border: "1px solid var(--rule)",
                background: "var(--bg-surface)",
                maxHeight: expanded ? "520px" : "220px",
              }}
            >
              {imgFailed ? (
                <div style={{ padding: "12px", maxHeight: expanded ? "400px" : "200px", overflowY: "auto", fontSize: "12.5px", lineHeight: 1.55, color: "var(--ink-2)", whiteSpace: "pre-wrap" }}>
                  {cleanPreviewText(fullExcerpt || file.extractedText) || "PDF page preview unavailable"}
                </div>
              ) : (
                <img
                  src={pdfPage === 0 && !expanded && imgSrc ? imgSrc : getPdfPageUrl(file.id, pdfPage)}
                  alt={`${file.title} page ${pdfPage + 1}`}
                  decoding="async"
                  loading="lazy"
                  onError={() => {
                    setImgFailed(true);
                    if (fullExcerpt || file.extractedText) setPdfMode("text");
                  }}
                  style={{ width: "100%", height: expanded ? "500px" : "220px", objectFit: "contain", display: "block" }}
                />
              )}
            </div>
            {(fullExcerpt || file.extractedText) && (
              <p style={{ fontSize: "12.5px", color: "var(--ink-2)", lineHeight: 1.5, maxHeight: "90px", overflow: "hidden" }}>
                {cleanPreviewText(fullExcerpt || file.extractedText).slice(0, 260)}
              </p>
            )}
          </>
        )}
      </div>
    );
  }

  if (file.specimen === "receipt") {
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>RECEIPT</span>
          <span>{file.date}</span>
        </div>

        {imgSrc && file.modality === "image" && !imgFailed && (
          <div
            style={{
              marginBottom: "10px",
              borderRadius: "var(--radius-sm)",
              overflow: "hidden",
              border: "1px solid var(--rule)",
              maxHeight: expanded ? "340px" : "160px",
              background: "var(--bg-surface)",
            }}
          >
            <img
              src={expanded ? getFileRawUrl(file.id) : imgSrc}
              alt={file.title}
              decoding="async"
              loading="lazy"
              onError={() => setImgFailed(true)}
              style={{
                width: "100%",
                height: expanded ? "340px" : "160px",
                objectFit: "cover",
                display: "block",
              }}
            />
          </div>
        )}

        <div
          style={{
            padding: "4px 0",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: "12px",
          }}
        >
          <div
            style={{
              fontSize: "12.5px",
              color: "var(--ink-2)",
              lineHeight: 1.5,
              display: "-webkit-box",
              WebkitLineClamp: expanded ? 8 : 3,
              WebkitBoxOrient: "vertical",
              overflow: "hidden",
            }}
          >
            {file.extractedText || "Receipt details indexed"}
          </div>
          <span
            className="font-display"
            style={{ fontSize: "17px", fontWeight: 600, whiteSpace: "nowrap", flexShrink: 0 }}
          >
            {file.extractedText.match(/(?:PHP|USD|EUR|GBP|JPY|₱|\$|€|£|¥)\s?[\d,.]+/i)?.[0] || "RECEIPT"}
          </span>
        </div>
      </div>
    );
  }

  if (file.specimen === "diagram") {
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>DIAGRAM</span>
          <span>
            {file.format.toUpperCase()} · {file.size}
          </span>
        </div>
        {imgSrc && !imgFailed && (
          <div
            style={{
              marginBottom: "10px",
              borderRadius: "var(--radius-sm)",
              overflow: "hidden",
              border: "1px solid var(--rule)",
              maxHeight: "170px",
              background: "var(--bg-surface)",
            }}
          >
            <img
              src={imgSrc}
              alt={file.title}
              decoding="async"
              loading="lazy"
              onError={() => setImgFailed(true)}
              style={{ width: "100%", height: "170px", objectFit: "contain", display: "block" }}
            />
          </div>
        )}
        <div style={{ fontSize: "12px", color: "var(--ink-2)" }}>{file.extractedText}</div>
      </div>
    );
  }

  if (file.specimen === "code" || meta.kind === "markdown") {
    const isHtml = file.format === "html" || file.format === "htm";
    const isMd = meta.kind === "markdown" || file.format === "md" || file.format === "markdown";
    const isJson = file.format === "json";
    let codeText = fullExcerpt || (meta.rawSnippet as string) || file.extractedText || "";
    if (isJson && prettyJson) {
      try {
        codeText = JSON.stringify(JSON.parse(codeText), null, 2);
      } catch {
        // leave unformatted
      }
    }
    const lines = codeText.split("\n").slice(0, expanded ? 400 : 45);
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>{file.name} · {meta.lineCount ? `${meta.lineCount} lines` : file.format.toUpperCase()}</span>
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            {onCopyText && codeText.trim().length > 0 && (
              <button
                type="button"
                className="btn-tactile sm"
                onClick={() => onCopyText(codeText, isMd ? "markdown" : "code")}
              >
                Copy text
              </button>
            )}
            {isHtml ? (
              <button
                type="button"
                className="btn-tactile sm"
                onClick={() => setHtmlPreviewMode((m) => (m === "code" ? "render" : "code"))}
              >
                {htmlPreviewMode === "code" ? "Live preview" : "View source"}
              </button>
            ) : isMd ? (
              <button
                type="button"
                className="btn-tactile sm"
                onClick={() => setMdMode((m) => (m === "render" ? "source" : "render"))}
              >
                {mdMode === "render" ? "View source" : "Rendered"}
              </button>
            ) : isJson ? (
              <button
                type="button"
                className="btn-tactile sm"
                onClick={() => setPrettyJson((p) => !p)}
              >
                {prettyJson ? "Raw JSON" : "Pretty JSON"}
              </button>
            ) : (
              <span>{file.format.toUpperCase()}</span>
            )}
          </div>
        </div>
        {isHtml && htmlPreviewMode === "render" ? (
          <iframe
            title={file.name}
            sandbox=""
            srcDoc={codeText}
            style={{
              width: "100%",
              height: expanded ? "420px" : "200px",
              border: "1px solid var(--rule)",
              borderRadius: "var(--radius-sm)",
              background: "var(--bg-surface)",
            }}
          />
        ) : isMd && mdMode === "render" ? (
          <MarkdownPreviewBlock source={codeText} expanded={expanded} />
        ) : (
          <pre
            className="font-code-raw"
            style={{
              fontSize: "11px",
              lineHeight: 1.55,
              color: "var(--ink)",
              overflowX: "auto",
              maxHeight: expanded ? "460px" : "220px",
              whiteSpace: "pre",
            }}
          >
            {lines.map((ln, idx) => `${String(idx + 1).padStart(3, " ")} │ ${ln}`).join("\n")}
          </pre>
        )}
      </div>
    );
  }

  const extLow = (file.format || "").toLowerCase();
  const kindLow = String(meta.kind || "").toLowerCase();
  const isRichDoc =
    file.specimen !== "idcard" &&
    file.modality !== "image" &&
    (["docx", "doc", "odt", "rtf", "txt", "log"].includes(extLow) ||
      ["docx", "doc", "odt", "rtf", "text"].includes(kindLow));

  if (isRichDoc) {
    const docBody = cleanPreviewText(fullExcerpt || file.extractedText) || "(No readable text extracted)";
    const wordCount =
      typeof meta.wordCount === "number" && meta.wordCount > 0
        ? meta.wordCount
        : docBody.trim().split(/\s+/).filter(Boolean).length;
    return (
      <div className="specimen-card">
        <div className="specimen-header">
          <span>
            DOCUMENT · {extLow.toUpperCase()} · {wordCount} words
          </span>
          {onCopyText && (fullExcerpt || file.extractedText) && (
            <button
              type="button"
              className="btn-tactile sm"
              onClick={() => onCopyText(docBody, "document text")}
            >
              <span>Copy text</span>
            </button>
          )}
        </div>
        <div
          style={{
            maxHeight: expanded ? "440px" : "240px",
            overflowY: "auto",
            fontSize: "13px",
            lineHeight: 1.62,
            color: "var(--ink-2)",
            whiteSpace: "pre-wrap",
          }}
        >
          {docBody}
        </div>
      </div>
    );
  }

  const receiptFields = (meta.receiptFields || {}) as Record<string, string>;
  const hasReceiptFields = Object.keys(receiptFields).length > 0;
  const fullImgUrl = file.modality === "image" ? (expanded ? getFileRawUrl(file.id) : imgSrc || getFileRawUrl(file.id)) : imgSrc;

  return (
    <div className="specimen-card">
      <div className="specimen-header">
        <span>
          {file.specimen === "idcard" ? "ID / FORM" : file.modality === "image" ? "IMAGE" : "DOCUMENT"}
          {meta.camera ? ` · ${meta.camera}` : ""}
          {file.width > 0 ? ` · ${file.width}×${file.height}` : ""}
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: "5px" }}>
          {file.modality === "image" && (
            <>
              <button
                type="button"
                className="btn-tactile sm"
                onClick={() => setImgZoom((z) => Math.max(1, +(z - 0.5).toFixed(1)))}
                title="Zoom out"
                aria-label="Zoom out"
              >
                −
              </button>
              <button
                type="button"
                onClick={() => setImgZoom(1)}
                title="Reset zoom to 100%"
                className="tabular-nums"
                style={{ fontSize: "11px", padding: "0 4px", color: "var(--ink-2)" }}
              >
                {Math.round(imgZoom * 100)}%
              </button>
              <button
                type="button"
                className="btn-tactile sm"
                onClick={() => setImgZoom((z) => Math.min(3, +(z + 0.5).toFixed(1)))}
                title="Zoom in"
                aria-label="Zoom in"
              >
                +
              </button>
            </>
          )}
          {file.ocrText && (
            <button
              type="button"
              className={`btn-tactile sm ${showOcrOverlay ? "active-tab" : ""}`}
              onClick={() => setShowOcrOverlay((v) => !v)}
            >
              {showOcrOverlay ? "Hide OCR" : "OCR text"}
            </button>
          )}
          {onCopyText && (file.ocrText || cleanPreviewText(fullExcerpt || file.extractedText)) && (
            <button
              type="button"
              className="btn-tactile sm"
              onClick={() =>
                onCopyText(
                  showOcrOverlay && file.ocrText
                    ? file.ocrText
                    : cleanPreviewText(fullExcerpt || file.extractedText || file.ocrText),
                  file.ocrText ? "OCR text" : "extracted text"
                )
              }
            >
              Copy text
            </button>
          )}
        </div>
      </div>

      {fullImgUrl && file.modality === "image" && !imgFailed && (
        <div
          style={{
            marginBottom: "10px",
            borderRadius: "6px",
            overflow: "auto",
            border: "1px solid var(--rule)",
            maxHeight: expanded ? "480px" : "190px",
            background: "var(--bg-surface)",
            position: "relative",
          }}
        >
          <img
            src={fullImgUrl}
            alt={file.title}
            decoding="async"
            loading="lazy"
            onError={() => setImgFailed(true)}
            style={{
              width: `${imgZoom * 100}%`,
              height: expanded ? `${Math.round(440 * imgZoom)}px` : "190px",
              objectFit: "contain",
              display: "block",
            }}
          />
          {showOcrOverlay && file.ocrText && (
            <div
              style={{
                position: "sticky",
                bottom: 0,
                left: 0,
                right: 0,
                padding: "10px 12px",
                background: "rgba(18, 17, 15, 0.88)",
                color: "#F7F6F2",
                fontSize: "12px",
                lineHeight: 1.5,
              }}
            >
              {file.ocrText}
            </div>
          )}
        </div>
      )}
      {file.modality === "image" && (!fullImgUrl || imgFailed) && (
        <div
          style={{
            marginBottom: "10px",
            padding: "16px 12px",
            borderRadius: "6px",
            border: "1px dashed var(--rule-strong)",
            background: "var(--bg-surface)",
            fontSize: "12px",
            color: "var(--ink-3)",
            textAlign: "center",
          }}
        >
          Image preview unavailable
        </div>
      )}

      {hasReceiptFields && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "10px" }}>
          {Object.entries(receiptFields).map(([k, v]) => (
            <button
              key={k}
              type="button"
              className="tag-pill"
              title="Click to copy extracted field"
              onClick={() => onCopyText?.(v, k)}
            >
              <span style={{ color: "var(--ink-3)" }}>{k}:</span>
              <span style={{ fontWeight: 600 }}>{v}</span>
            </button>
          ))}
        </div>
      )}

      <p
        style={{
          fontSize: "13.5px",
          lineHeight: 1.55,
          color: "var(--ink-2)",
          whiteSpace: "pre-wrap",
          maxHeight: expanded ? "300px" : "120px",
          overflowY: "auto",
        }}
      >
        {cleanPreviewText(fullExcerpt || file.extractedText || "") || "(No text excerpt indexed)"}
      </p>
    </div>
  );
};

const TagsEditor: React.FC<{
  file: ShelfFile;
  onRefreshState?: (state: any) => void;
  onShowToast: (msg: string) => void;
}> = ({ file, onRefreshState, onShowToast }) => {
  const [tagInput, setTagInput] = useState("");
  const currentTags = file.tags || [];

  useEffect(() => {
    setTagInput("");
  }, [file.id]);

  const suggestedTags = React.useMemo(() => {
    const existing = new Set(currentTags.map((t) => t.toLowerCase()));
    const candidates: string[] = [];
    const meta = file.metadata || {};

    const addCandidate = (raw: unknown) => {
      if (!raw) return;
      const clean = String(raw)
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9-]+/g, "-")
        .replace(/^-+|-+$/g, "");
      if (
        !clean ||
        clean.length < 2 ||
        clean.length > 22 ||
        ["text", "unknown", "other", "general", "file", "files", "document"].includes(clean) ||
        existing.has(clean) ||
        candidates.includes(clean)
      ) {
        return;
      }
      candidates.push(clean);
    };

    if (meta.doc_type) {
      addCandidate(meta.doc_type);
    }
    if (file.specimen && file.specimen !== "text") {
      addCandidate(file.specimen);
    }
    if (meta.merchant) {
      const token = String(meta.merchant)
        .trim()
        .toLowerCase()
        .split(/[^a-z0-9]+/)
        .find((w) => w.length >= 2 && w.length <= 16 && !["inc", "llc", "ltd", "corp", "the", "and"].includes(w));
      if (token) addCandidate(token);
    }
    if (meta.due_date) {
      addCandidate("due");
      addCandidate("bill");
    }
    if (file.subShelf) {
      const subWord = file.subShelf
        .toLowerCase()
        .split(/[^a-z0-9]+/)
        .find((w) => w.length >= 3 && !["and", "the", "for", "general", "other", "files", "all"].includes(w));
      if (subWord) addCandidate(subWord);
    }

    return candidates.slice(0, 4);
  }, [file.id, file.specimen, file.subShelf, file.metadata, currentTags]);

  const addTag = async (raw: string) => {
    const clean = raw.trim().toLowerCase().replace(/^#+/, "");
    if (!clean) return;
    if (currentTags.includes(clean)) {
      setTagInput("");
      return;
    }
    try {
      const next = [...currentTags, clean];
      const res = await setFileTags(file.id, next);
      onRefreshState?.(res.state);
      setTagInput("");
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not add tag");
    }
  };

  const removeTag = async (tag: string) => {
    try {
      const next = currentTags.filter((x) => x !== tag);
      const res = await setFileTags(file.id, next);
      onRefreshState?.(res.state);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not remove tag");
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "8px", paddingTop: "12px", borderTop: "1px solid var(--rule)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span
          style={{
            fontSize: "11px",
            fontWeight: 600,
            letterSpacing: "0.05em",
            color: "var(--ink-3)",
            textTransform: "uppercase",
          }}
        >
          Tags
        </span>
        {currentTags.length > 0 && (
          <span className="tabular-nums" style={{ fontSize: "11px", color: "var(--ink-3)" }}>
            {currentTags.length}
          </span>
        )}
      </div>

      {currentTags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
          {currentTags.map((t) => (
            <span key={t} className="tag-pill">
              <span>{t}</span>
              <button
                type="button"
                aria-label={`Remove tag ${t}`}
                style={{ display: "inline-flex", alignItems: "center", color: "var(--ink-3)", padding: "0 2px", fontSize: "12px" }}
                onClick={() => removeTag(t)}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          addTag(tagInput);
        }}
        className="tag-input-wrap"
      >
        <input
          type="text"
          aria-label="Add tag"
          value={tagInput}
          onChange={(e) => setTagInput(e.target.value)}
          placeholder="Add tag and press Enter…"
          style={{ flex: 1, minWidth: 0, fontSize: "12.5px", color: "var(--ink)" }}
        />
        {tagInput.trim().length > 0 && (
          <button
            type="submit"
            className="btn-tactile sm solid"
          >
            Add
          </button>
        )}
      </form>

      {suggestedTags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
          {suggestedTags.map((tag) => (
            <button
              key={tag}
              type="button"
              className="tag-pill-suggest"
              onClick={() => addTag(tag)}
              title={`Add tag #${tag}`}
            >
              + {tag}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

const FileQuickActions: React.FC<{
  file: ShelfFile;
  compact?: boolean;
  onOpenOS?: () => void;
  onRevealOS?: () => void;
  onFindSimilar?: (file: ShelfFile) => void;
  onAskAboutFile?: (file: ShelfFile) => void;
  onOpenRenameDialog?: (file: ShelfFile) => void;
  onOpenMoveDialog?: (file: ShelfFile, mode?: "move" | "copy") => void;
  onOpenDeleteDialog?: (file: ShelfFile) => void;
  onRefreshState?: (state: any) => void;
  onShowToast: (msg: string) => void;
}> = ({
  file,
  onOpenOS,
  onRevealOS,
  onFindSimilar,
  onAskAboutFile,
  onOpenRenameDialog,
  onOpenMoveDialog,
  onOpenDeleteDialog,
  onRefreshState,
  onShowToast,
}) => {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
      {/* Row 1: Primary Actions */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
        {onOpenOS && (
          <button
            type="button"
            className="btn-tactile solid"
            onClick={onOpenOS}
            title="Open with default app"
          >
            <span>Open</span>
          </button>
        )}
        {onRevealOS && (
          <button
            type="button"
            className="btn-tactile"
            onClick={onRevealOS}
            title="Show file in Explorer / File Manager"
          >
            <span>Show in folder</span>
          </button>
        )}
      </div>

      {/* Row 2: Secondary Actions */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
        <button
          type="button"
          className="btn-tactile sm"
          onClick={() => onOpenRenameDialog?.(file)}
          title="Rename file (F2 or R)"
        >
          Rename
        </button>
        <button
          type="button"
          className="btn-tactile sm"
          onClick={async () => {
            if (onOpenMoveDialog) {
              onOpenMoveDialog(file, "move");
              return;
            }
            const dest = await pickFolderNative();
            if (!dest) return;
            try {
              const res = await moveFileToFolder(file.id, dest);
              onRefreshState?.(res.state);
              onShowToast(`Moved ${file.name}`);
            } catch (err) {
              onShowToast(err instanceof Error ? err.message : "Move failed");
            }
          }}
          title="Move or copy to another folder (M)"
        >
          Move / Copy
        </button>
        <button
          type="button"
          className={`btn-tactile sm ${file.pinned ? "active-tab" : ""}`}
          aria-pressed={Boolean(file.pinned)}
          onClick={async () => {
            try {
              const res = await togglePinFile(file.id);
              onRefreshState?.(res.state);
              onShowToast(res.pinned ? `Pinned ${file.name}` : `Unpinned ${file.name}`);
            } catch (err) {
              onShowToast(err instanceof Error ? err.message : "Pin failed");
            }
          }}
        >
          {file.pinned ? "Pinned" : "Pin"}
        </button>
        {onFindSimilar && (
          <button
            type="button"
            className="btn-tactile sm"
            onClick={() => onFindSimilar(file)}
          >
            Find similar
          </button>
        )}
        {onAskAboutFile && (
          <button
            type="button"
            className="btn-tactile sm"
            onClick={() => onAskAboutFile(file)}
            title="Ask AI to summarize or inspect this file"
          >
            Ask AI
          </button>
        )}
      </div>

      {/* Row 3: Destructive Action */}
      <div style={{ marginTop: "4px" }}>
        <button
          type="button"
          className="btn-tactile sm danger"
          onClick={async () => {
            if (onOpenDeleteDialog) {
              onOpenDeleteDialog(file);
              return;
            }
            try {
              const res = await trashFile(file.id);
              onRefreshState?.(res.state);
              onShowToast(`Moved ${file.name} to Trash (${formatShortcut("Z")} to undo)`);
            } catch (err) {
              onShowToast(err instanceof Error ? err.message : "Trash failed");
            }
          }}
        >
          Move to Trash
        </button>
      </div>
    </div>
  );
};

const IndexingDetailsDisclosure: React.FC<{ file: ShelfFile }> = ({ file }) => {
  const rawVisual = Number.isFinite(file.visualShare) ? file.visualShare : 0;
  const rawText = Number.isFinite(file.textShare) ? file.textShare : 0;
  const sum = rawVisual + rawText;
  const visualPct = sum > 0 ? Math.round((rawVisual / sum) * 100) : file.modality === "image" ? 100 : 0;
  const textPct = 100 - visualPct;
  const hasOcr = Boolean(file.ocrText && file.ocrText.trim()) || (file.ocrConfidence ?? 0) > 0;
  const ocrPct =
    file.ocrConfidence > 1
      ? Math.min(100, Math.round(file.ocrConfidence))
      : Math.round((file.ocrConfidence || 0) * 100);
  const cosineLabel =
    file.cosine && file.cosine !== "0.00" && file.cosine !== "0"
      ? file.cosine
      : file.matchScore > 0
      ? `${Math.round(file.matchScore <= 1 ? file.matchScore * 100 : file.matchScore)}%`
      : null;

  return (
    <details className="inspector-tech-disclosure">
      <summary>Indexing details</summary>
      <div className="inspector-meta-list" style={{ marginTop: "8px" }}>
        <div className="inspector-meta-row">
          <span className="inspector-meta-label">Embedding model</span>
          <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-2)" }}>
            Local ONNX (512-d)
          </span>
        </div>
        <div className="inspector-meta-row">
          <span className="inspector-meta-label">Modality</span>
          <span style={{ fontSize: "12px", color: "var(--ink-2)", textTransform: "capitalize" }}>
            {file.modality}
          </span>
        </div>
        <div className="inspector-meta-row">
          <span className="inspector-meta-label">Visual / text share</span>
          <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-2)" }}>
            {visualPct}% visual · {textPct}% text
          </span>
        </div>
        {cosineLabel && (
          <div className="inspector-meta-row">
            <span className="inspector-meta-label">Match score</span>
            <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-2)" }}>
              {cosineLabel}
            </span>
          </div>
        )}
        {hasOcr && (
          <div className="inspector-meta-row">
            <span className="inspector-meta-label">OCR confidence</span>
            <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-2)" }}>
              {ocrPct > 0 ? `${ocrPct}%` : "Indexed"}
            </span>
          </div>
        )}
        {file.vectorNorm && (
          <div className="inspector-meta-row">
            <span className="inspector-meta-label">Vector norm</span>
            <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-2)" }}>
              {file.vectorNorm}
            </span>
          </div>
        )}
      </div>
    </details>
  );
};

const InlineFileCopilot: React.FC<{
  file: ShelfFile;
  onInspectFile?: (fileId: string) => void;
  onShowToast: (msg: string) => void;
}> = ({ file, onInspectFile, onShowToast }) => {
  const [copilotOpen, setCopilotOpen] = useState<boolean>(false);
  const [promptInput, setPromptInput] = useState<string>("");
  const [asking, setAsking] = useState<boolean>(false);
  const [lastAnswer, setLastAnswer] = useState<AskResponse | null>(null);

  useEffect(() => {
    setLastAnswer(null);
    setPromptInput("");
  }, [file.id]);

  const quickChips = React.useMemo(() => {
    const ext = (file.format || "").toLowerCase();
    const kind = String((file.metadata || {}).kind || "").toLowerCase();
    const chips: Array<{ label: string; prompt: string }> = [
      { label: "Explain full context", prompt: `Explain ${file.name}` },
      { label: "Connect the dots", prompt: `/graph ${file.name}` },
    ];
    if (kind === "spreadsheet" || ["csv", "xlsx", "xls", "tsv"].includes(ext)) {
      chips.push({ label: "Column statistics", prompt: `What columns and totals are in ${file.name}?` });
    } else if (kind === "pptx" || ["pptx", "ppt"].includes(ext)) {
      chips.push({ label: "Slide outline", prompt: `Outline the slides in ${file.name}` });
    } else if (file.modality === "code" || kind === "code") {
      chips.push({ label: "Functions & TODOs", prompt: `What functions, imports, and TODOs are in ${file.name}?` });
    } else if (file.specimen === "receipt" || file.metadata?.doc_type === "invoice" || file.metadata?.amount) {
      chips.push({ label: "Invoice & payment details", prompt: `What is the invoice number, due date, and total in ${file.name}?` });
    } else {
      chips.push({ label: "Key takeaways", prompt: `Summarize key points of ${file.name}` });
    }
    return chips;
  }, [file.id, file.name, file.format, file.modality, file.specimen, file.metadata]);

  const runCopilotQuery = async (q: string) => {
    const clean = q.trim();
    if (!clean || asking) return;
    setCopilotOpen(true);
    setAsking(true);
    try {
      const res = await askYourShelves(clean, 6, { selectedFileId: file.id });
      setLastAnswer(res);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not query file");
    } finally {
      setAsking(false);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "8px",
        paddingTop: "12px",
        borderTop: "1px solid var(--rule)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" }}>
        <span
          style={{
            fontSize: "11px",
            fontWeight: 600,
            letterSpacing: "0.05em",
            color: "var(--ink-3)",
            textTransform: "uppercase",
          }}
        >
          Chat with this file (AI Copilot)
        </span>
        <button
          type="button"
          className="btn-ghost"
          style={{ minHeight: "22px", padding: "1px 6px", fontSize: "11.5px" }}
          onClick={() => setCopilotOpen((o) => !o)}
        >
          {copilotOpen ? "Hide" : "Ask"}
        </button>
      </div>

      {/* Quick Chips */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "5px" }}>
        {quickChips.map((ch) => (
          <button
            key={ch.label}
            type="button"
            className="tag-pill-suggest"
            disabled={asking}
            onClick={() => void runCopilotQuery(ch.prompt)}
          >
            {ch.label}
          </button>
        ))}
      </div>

      {copilotOpen && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "8px",
            padding: "10px",
            borderRadius: "var(--radius-sm)",
            border: "1px solid var(--rule)",
            background: "var(--bg-subtle)",
          }}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void runCopilotQuery(promptInput);
              setPromptInput("");
            }}
            style={{ display: "flex", gap: "6px" }}
          >
            <input
              type="text"
              aria-label="Ask AI about this file"
              value={promptInput}
              onChange={(e) => setPromptInput(e.target.value)}
              placeholder={`Ask anything about ${file.name}…`}
              className="dialog-input"
              style={{ flex: 1, minWidth: 0, padding: "5px 8px", fontSize: "12px" }}
            />
            <button
              type="submit"
              className="btn-tactile sm solid"
              disabled={asking || !promptInput.trim()}
            >
              {asking ? "…" : "Ask"}
            </button>
          </form>

          {asking && (
            <div style={{ fontSize: "12px", color: "var(--ink-2)", display: "flex", alignItems: "center", gap: "6px" }}>
              <span className="status-dot pulse" aria-hidden="true" />
              <span>Analyzing `{file.name}` locally…</span>
            </div>
          )}

          {!asking && lastAnswer && (
            <div
              style={{
                maxHeight: "230px",
                overflowY: "auto",
                fontSize: "12.5px",
                lineHeight: 1.55,
                color: "var(--ink)",
                display: "flex",
                flexDirection: "column",
                gap: "4px",
              }}
            >
              {lastAnswer.answer.split("\n").map((ln, idx) => {
                const t = ln.trim();
                if (!t) return <div key={idx} style={{ height: "4px" }} />;
                if (t.startsWith("- ")) {
                  return (
                    <div key={idx} style={{ paddingLeft: "10px", color: "var(--ink-2)" }}>
                      • {renderInlineMd(t.slice(2))}
                    </div>
                  );
                }
                return <div key={idx}>{renderInlineMd(ln)}</div>;
              })}
              {lastAnswer.citations && lastAnswer.citations.length > 1 && onInspectFile && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: "5px", marginTop: "6px", paddingTop: "6px", borderTop: "1px solid var(--rule-soft)" }}>
                  <span style={{ fontSize: "11px", color: "var(--ink-3)" }}>Related:</span>
                  {lastAnswer.citations
                    .filter((c) => c.fileId !== file.id)
                    .slice(0, 3)
                    .map((c) => (
                      <button
                        key={c.fileId}
                        type="button"
                        className="flat-chip"
                        style={{ padding: "1px 7px", fontSize: "11px" }}
                        onClick={() => onInspectFile(c.fileId)}
                      >
                        {c.fileName}
                      </button>
                    ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export const InspectorSheet: React.FC<InspectorSheetProps> = React.memo(({
  file,
  shelves = [],
  quickLookOpen,
  initialAnchor = null,
  onCloseQuickLook,
  onStepQuickLook,
  onFindSimilar,
  onAskAboutFile,
  onInspectFile,
  onAssignShelf,
  onOpenRenameDialog,
  onOpenMoveDialog,
  onOpenDeleteDialog,
  onToggleRename,
  onCopyPath,
  onShowToast,
  onRefreshState,
}) => {
  const quickLookCardRef = useFocusTrap<HTMLDivElement>(quickLookOpen && Boolean(file));
  const extractedEntities = React.useMemo(() => (file ? extractFileEntities(file) : []), [file]);

  const handleCopyText = (text: string, label: string) => {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard
        .writeText(text)
        .then(() => onShowToast(`Copied ${label}`))
        .catch(() => onShowToast("Clipboard unavailable"));
    } else {
      onShowToast("Clipboard unavailable");
    }
  };

  const handleOpenOS = async () => {
    if (!file) return;
    try {
      await openFileInOS(file.path);
      onShowToast(`Opened ${file.name}`);
    } catch {
      onShowToast(`File located at ${joinDisplayPath(file.folder, file.name)}`);
    }
  };

  const handleRevealOS = async () => {
    if (!file) return;
    try {
      await revealFileInOS(file.path);
      onShowToast(`Revealed ${file.name} in folder`);
    } catch {
      onShowToast(`Folder: ${file.folderFull}`);
    }
  };

  return (
    <div
      className={`modal-wrap ${quickLookOpen ? "open" : ""}`}
      id="quickLookModal"
      role="dialog"
      aria-modal="true"
      aria-label={file ? `File Preview & Details: ${file.title}` : "File Preview & Details"}
      aria-hidden={!quickLookOpen}
      inert={!quickLookOpen ? true : undefined}
      onClick={(e) => {
        if (e.target === e.currentTarget) onCloseQuickLook();
      }}
    >
      {quickLookOpen && file && (
        <div
          ref={quickLookCardRef}
          tabIndex={-1}
          className="modal-card modal-lg file-details-modal"
        >
          {/* Modal Header */}
          <div className="file-modal-header">
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: "12px", color: "var(--ink-3)", display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                <span className="file-type-tag">{file.format.toLowerCase()}</span>
                <span>{file.size}</span>
                <span>·</span>
                <span>{file.shelfName}</span>
                {file.subShelf && (
                  <>
                    <span>·</span>
                    <span>{file.subShelf}</span>
                  </>
                )}
              </div>
              <h2
                className="font-display"
                style={{
                  fontSize: "22px",
                  fontWeight: 500,
                  letterSpacing: "-0.02em",
                  marginTop: "6px",
                  lineHeight: 1.2,
                  overflowWrap: "anywhere",
                }}
              >
                {file.title}
              </h2>
              {file.name !== file.title && (
                <div
                  className="font-code-raw"
                  style={{ fontSize: "11.5px", color: "var(--ink-3)", marginTop: "3px", overflowWrap: "anywhere" }}
                >
                  {file.name}
                </div>
              )}
            </div>

            <div style={{ display: "flex", gap: "6px", flexShrink: 0, flexWrap: "wrap", alignItems: "center" }}>
              {onStepQuickLook && (
                <>
                  <button
                    type="button"
                    className="btn-tactile sm"
                    onClick={() => onStepQuickLook(-1)}
                    title="Previous file (←)"
                    aria-label="Previous file"
                  >
                    ←
                  </button>
                  <button
                    type="button"
                    className="btn-tactile sm"
                    onClick={() => onStepQuickLook(1)}
                    title="Next file (→)"
                    aria-label="Next file"
                  >
                    →
                  </button>
                </>
              )}
              <button
                type="button"
                className="btn-tactile sm"
                onClick={onCloseQuickLook}
                title="Close (Esc)"
              >
                Close
              </button>
            </div>
          </div>

          {/* 2-Column Preview + Details Layout */}
          <div className="file-modal-grid">
            {/* Left: Full Specimen Preview */}
            <div className="file-modal-preview-col">
              <SpecimenPreview file={file} expanded initialAnchor={initialAnchor} onCopyText={handleCopyText} />
            </div>

            {/* Right: Editorial Match Note, Metadata, Quick Actions, Tags, Indexing Details */}
            <div className="file-modal-details-col">
              {file.whyMatched && file.whyMatched.trim().length > 0 && (
                <p
                  style={{
                    borderLeft: "2px solid var(--rule-strong)",
                    paddingLeft: "10px",
                    fontSize: "13px",
                    color: "var(--ink-2)",
                    marginBottom: "14px",
                    lineHeight: 1.5,
                  }}
                >
                  {file.whyMatched}
                </p>
              )}

              {/* Suggested Clean Filename */}
              {file.suggestedName && !file.renamed && (
                <div className="modal-rename-callout">
                  <div
                    style={{
                      fontSize: "10.5px",
                      fontWeight: 600,
                      letterSpacing: "0.06em",
                      color: "var(--ink-3)",
                      textTransform: "uppercase",
                    }}
                  >
                    Suggested rename
                  </div>
                  <div
                    className="font-code-raw"
                    style={{ fontSize: "12.5px", color: "var(--ink)", overflowWrap: "anywhere", wordBreak: "normal" }}
                  >
                    <span style={{ color: "var(--ink-3)" }}>{file.name}</span> → <strong>{file.suggestedName}</strong>
                  </div>
                  {file.renameReason && (
                    <div style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>
                      {file.renameReason}
                    </div>
                  )}
                  <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                    <button
                      type="button"
                      className="btn-tactile sm solid"
                      onClick={() => onToggleRename(file.id)}
                    >
                      Apply suggested rename
                    </button>
                    {onOpenRenameDialog && (
                      <button
                        type="button"
                        className="btn-tactile sm"
                        onClick={() => onOpenRenameDialog(file)}
                      >
                        Edit name…
                      </button>
                    )}
                  </div>
                </div>
              )}

              {/* Metadata & Inline Shelf Assignment */}
              <div className="inspector-meta-list">
                <div className="inspector-meta-row">
                  <span className="inspector-meta-label">Shelf</span>
                  {onAssignShelf && shelves.length > 0 ? (
                    <select
                      aria-label="Shelf assignment"
                      className="editorial-select"
                      value={file.shelfId}
                      onChange={(e) => onAssignShelf(file.id, e.target.value)}
                      title="Move file to another shelf"
                      style={{
                        fontSize: "12px",
                        padding: "3px 24px 3px 8px",
                        minHeight: "28px",
                        maxWidth: "200px",
                      }}
                    >
                      {!shelves.some((s) => s.id === file.shelfId) && (
                        <option value={file.shelfId}>{file.shelfName}</option>
                      )}
                      {shelves.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name}
                        </option>
                      ))}
                      <option value="auto">Auto-place by topic</option>
                    </select>
                  ) : (
                    <span style={{ fontSize: "12.5px", color: "var(--ink)" }}>{file.shelfName}</span>
                  )}
                </div>

                <div className="inspector-meta-row">
                  <span className="inspector-meta-label">Location</span>
                  <button
                    type="button"
                    onClick={() => onCopyPath(file)}
                    title={`Click to copy path: ${file.path}`}
                    style={{
                      textAlign: "right",
                      color: "var(--ink)",
                      fontSize: "12px",
                      overflowWrap: "anywhere",
                      wordBreak: "break-word",
                      textDecoration: "underline",
                      textDecorationColor: "var(--rule-strong)",
                      textUnderlineOffset: "3px",
                    }}
                  >
                    {file.folderFull || file.folder || file.path}
                  </button>
                </div>

                <div className="inspector-meta-row">
                  <span className="inspector-meta-label">Modified</span>
                  <span style={{ fontSize: "12.5px", color: "var(--ink)" }}>{file.date}</span>
                </div>

                <div className="inspector-meta-row">
                  <span className="inspector-meta-label">Size</span>
                  <span className="tabular-nums" style={{ fontSize: "12.5px", color: "var(--ink)" }}>
                    {file.size}
                  </span>
                </div>

                {(file.width > 0 || file.pageCount > 1) && (
                  <div className="inspector-meta-row">
                    <span className="inspector-meta-label">
                      {file.width > 0 ? "Dimensions" : "Pages"}
                    </span>
                    <span className="tabular-nums" style={{ fontSize: "12.5px", color: "var(--ink)" }}>
                      {file.width > 0 ? `${file.width} × ${file.height}` : `${file.pageCount} pages`}
                    </span>
                  </div>
                )}
              </div>

              {/* Extracted Details (1-Click Copy) */}
              {extractedEntities.length > 0 && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "7px",
                    paddingTop: "10px",
                    borderTop: "1px solid var(--rule)",
                  }}
                >
                  <span
                    style={{
                      fontSize: "11px",
                      fontWeight: 600,
                      letterSpacing: "0.05em",
                      color: "var(--ink-3)",
                      textTransform: "uppercase",
                    }}
                  >
                    Extracted details
                  </span>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                    {extractedEntities.map((ent) => (
                      <button
                        key={`${ent.label}-${ent.value}`}
                        type="button"
                        className="tag-pill"
                        title={`Click to copy ${ent.label.toLowerCase()}`}
                        onClick={() => handleCopyText(ent.value, ent.label.toLowerCase())}
                      >
                        <span style={{ color: "var(--ink-3)" }}>{ent.label}:</span>
                        <span style={{ fontWeight: 600 }}>{ent.value}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Actions */}
              <FileQuickActions
                file={file}
                compact
                onOpenOS={handleOpenOS}
                onRevealOS={handleRevealOS}
                onFindSimilar={(f) => {
                  onCloseQuickLook();
                  onFindSimilar?.(f);
                }}
                onAskAboutFile={
                  onAskAboutFile
                    ? (f) => {
                        onCloseQuickLook();
                        onAskAboutFile(f);
                      }
                    : undefined
                }
                onOpenRenameDialog={onOpenRenameDialog}
                onOpenMoveDialog={onOpenMoveDialog}
                onOpenDeleteDialog={onOpenDeleteDialog}
                onRefreshState={onRefreshState}
                onShowToast={onShowToast}
              />

              {/* Inline "Chat with This File" AI Copilot */}
              <InlineFileCopilot
                file={file}
                onInspectFile={onInspectFile}
                onShowToast={onShowToast}
              />

              {/* Tags */}
              <TagsEditor
                file={file}
                onRefreshState={onRefreshState}
                onShowToast={onShowToast}
              />

              {/* Collapsed Technical Diagnostics */}
              <IndexingDetailsDisclosure file={file} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
});
