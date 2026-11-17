import React, { useState, useEffect, useMemo } from "react";
import { PanelRightClose, PanelRightOpen, Sparkles, Tag, Plus, X, ChevronLeft, ChevronRight, ZoomIn, ZoomOut } from "lucide-react";
import {
  ShelfFile,
  TidyMovePreview,
  copyFileToFolder,
  fetchFileFullExcerpt,
  getFileRawUrl,
  getPdfPageUrl,
  moveFileToFolder,
  openFileExternal,
  openFileInOS,
  pickFolderNative,
  revealFileInOS,
  resolveLocalAssetSrc,
  runFileAction,
  setFileTags,
  togglePinFile,
  trashFile,
} from "../lib/tauri-api";

interface InspectorSheetProps {
  file: ShelfFile | undefined;
  relatedFiles?: ShelfFile[];
  visible: boolean;
  silhouetteScore: number;
  quickLookOpen: boolean;
  organizeModalOpen: boolean;
  tidyMoves: TidyMovePreview[];
  onCloseQuickLook: () => void;
  onOpenQuickLook: () => void;
  onStepQuickLook?: (dir: -1 | 1) => void;
  onFindSimilar?: (file: ShelfFile) => void;
  onHideInspector?: () => void;
  onShowInspector?: () => void;
  onSelectFile?: (fileId: string) => void;
  onOpenRenameDialog?: (file: ShelfFile) => void;
  onOpenMoveDialog?: (file: ShelfFile, mode?: "move" | "copy") => void;
  onOpenDeleteDialog?: (file: ShelfFile) => void;
  onCloseOrganizeModal: () => void;
  onConfirmOrganizeDisk: () => void;
  onRollbackOrganizeDisk: () => void;
  onToggleRename: (fileId: string) => void;
  onCopyPath: (file: ShelfFile) => void;
  onShowToast: (msg: string) => void;
  onRefreshState?: (state: any) => void;
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
          className="font-mono-code"
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

export const SpecimenPreview: React.FC<{ file: ShelfFile; expanded?: boolean; onCopyText?: (text: string, label: string) => void }> = ({
  file,
  expanded = false,
  onCopyText,
}) => {
  const imgSrc = resolveLocalAssetSrc(file);
  const meta = file.metadata || {};
  const [pdfPage, setPdfPage] = useState(0);
  const [pdfMode, setPdfMode] = useState<"image" | "text">("image");
  const [htmlPreviewMode, setHtmlPreviewMode] = useState<"code" | "render">("code");
  const [mdMode, setMdMode] = useState<"render" | "source">("render");
  const [imgFailed, setImgFailed] = useState(false);
  const [imgZoom, setImgZoom] = useState<number>(1);
  const [showOcrOverlay, setShowOcrOverlay] = useState<boolean>(false);
  const [activeSheet, setActiveSheet] = useState<string>(String(meta.activeSheet || "Sheet1"));
  const [tableFilter, setTableFilter] = useState<string>("");
  const [slideIdx, setSlideIdx] = useState<number>(0);
  const [archiveFilter, setArchiveFilter] = useState<string>("");
  const [fullExcerpt, setFullExcerpt] = useState<string | null>(null);
  const [prettyJson, setPrettyJson] = useState<boolean>(false);

  useEffect(() => {
    setPdfPage(0);
    setPdfMode("image");
    setHtmlPreviewMode("code");
    setMdMode("render");
    setImgFailed(false);
    setImgZoom(1);
    setShowOcrOverlay(false);
    setActiveSheet(String((file.metadata || {}).activeSheet || "Sheet1"));
    setTableFilter("");
    setSlideIdx(0);
    setArchiveFilter("");
    setFullExcerpt(null);
    setPrettyJson(false);
  }, [file.id]);

  useEffect(() => {
    const needsExcerpt =
      expanded &&
      (file.specimen === "code" || meta.kind === "markdown" || meta.kind === "text" || file.format === "json");
    if (!needsExcerpt) return;
    let cancelled = false;
    fetchFileFullExcerpt(file, 400)
      .then((res) => {
        if (!cancelled && res.content) setFullExcerpt(res.content);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [expanded, file, meta.kind]);

  if (meta.kind === "archive" && Array.isArray(meta.entries)) {
    const entries = (meta.entries as Array<{ name: string; sizeBytes: number; isDir: boolean; ext: string }>).filter(
      (e) => !archiveFilter.trim() || e.name.toLowerCase().includes(archiveFilter.trim().toLowerCase())
    );
    return (
      <div className="specimen-card">
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
          <span>ARCHIVE · {String(meta.format || file.format).toUpperCase()}</span>
          <span>{meta.totalEntries ?? entries.length} entries</span>
        </div>
        {expanded && (
          <input
            type="text"
            value={archiveFilter}
            onChange={(e) => setArchiveFilter(e.target.value)}
            placeholder="filter files inside archive…"
            className="dialog-input font-mono-code"
            style={{ padding: "5px 9px", fontSize: "11.5px", marginBottom: "8px" }}
          />
        )}
        <div style={{ maxHeight: expanded ? "360px" : "200px", overflowY: "auto", fontFamily: "var(--mono)", fontSize: "11.5px" }}>
          {entries.slice(0, expanded ? 120 : 30).map((e, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: "10px",
                padding: "4px 6px",
                borderBottom: "1px solid var(--rule)",
                color: e.isDir ? "var(--ink-3)" : "var(--ink)",
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {e.isDir ? `📁 ${e.name}` : e.name}
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
          <span>SLIDES · {slides.length} DECK</span>
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 6px", fontSize: "10.5px" }}
              disabled={slideIdx <= 0}
              onClick={() => setSlideIdx((s) => Math.max(0, s - 1))}
            >
              prev
            </button>
            <span>{slideIdx + 1}/{slides.length}</span>
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 6px", fontSize: "10.5px" }}
              disabled={slideIdx >= slides.length - 1}
              onClick={() => setSlideIdx((s) => Math.min(slides.length - 1, s + 1))}
            >
              next
            </button>
          </div>
        </div>
        <div style={{ padding: "14px", borderRadius: "6px", border: "1px solid var(--rule)", background: "var(--bg-subtle)" }}>
          <div className="font-display" style={{ fontSize: "17px", fontWeight: 500, marginBottom: "6px" }}>
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
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
            borderRadius: "5px",
            background: "#000",
          }}
        />
        {meta.codec && (
          <div style={{ fontSize: "11px", color: "var(--ink-3)", marginTop: "6px", fontFamily: "var(--mono)" }}>
            codec: {meta.codec} {meta.bitrateKbps ? `· ${meta.bitrateKbps} kbps` : ""}
          </div>
        )}
      </div>
    );
  }

  if (file.modality === "audio") {
    return (
      <div className="specimen-card">
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
          <span>AUDIO · {file.format.toUpperCase()}</span>
          <span>{meta.durationLabel || file.size}</span>
        </div>
        <audio key={file.id} controls src={getFileRawUrl(file.id)} style={{ width: "100%" }} />
        {meta.codec && (
          <div style={{ fontSize: "11px", color: "var(--ink-3)", marginTop: "8px", fontFamily: "var(--mono)" }}>
            codec: {meta.codec} {meta.bitrateKbps ? `· ${meta.bitrateKbps} kbps` : ""}
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "6px",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
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
        {expanded && (
          <input
            type="text"
            value={tableFilter}
            onChange={(e) => setTableFilter(e.target.value)}
            placeholder="filter rows…"
            className="dialog-input font-mono-code"
            style={{ padding: "5px 9px", fontSize: "11.5px", marginBottom: "8px" }}
          />
        )}
        <div style={{ overflowX: "auto", maxHeight: expanded ? "400px" : "220px" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "11.5px", fontFamily: "var(--mono)" }}>
            <thead>
              <tr>
                {headers.map((h, i) => (
                  <th
                    key={i}
                    style={{
                      textAlign: "left",
                      padding: "5px 8px",
                      borderBottom: "1px solid var(--rule-strong)",
                      background: "var(--bg-subtle)",
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
                        borderBottom: "1px solid var(--rule)",
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
    return (
      <div className="specimen-card">
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "6px",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
          <span>PDF · {totalPages} {totalPages === 1 ? "PAGE" : "PAGES"}</span>
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 7px", fontSize: "10.5px" }}
              onClick={() => setPdfMode((m) => (m === "image" ? "text" : "image"))}
            >
              {pdfMode === "image" ? "read text" : "page view"}
            </button>
            {totalPages > 1 && pdfMode === "image" && (
              <>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "2px 6px", fontSize: "10.5px" }}
                  disabled={pdfPage <= 0}
                  onClick={() => setPdfPage((p) => Math.max(0, p - 1))}
                >
                  prev
                </button>
                <span>{pdfPage + 1}/{totalPages}</span>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "2px 6px", fontSize: "10.5px" }}
                  disabled={pdfPage >= totalPages - 1}
                  onClick={() => setPdfPage((p) => Math.min(totalPages - 1, p + 1))}
                >
                  next
                </button>
              </>
            )}
          </div>
        </div>
        {pdfMode === "text" ? (
          <div style={{ maxHeight: expanded ? "440px" : "240px", overflowY: "auto", fontSize: "13px", lineHeight: 1.6, color: "var(--ink-2)", whiteSpace: "pre-wrap" }}>
            {file.extractedText || "(no embedded text found in PDF)"}
          </div>
        ) : (
          <>
            <div
              style={{
                marginBottom: "10px",
                borderRadius: "5px",
                overflow: "hidden",
                border: "1px solid var(--rule)",
                background: "var(--bg-subtle)",
                maxHeight: expanded ? "520px" : "220px",
              }}
            >
              {imgFailed ? (
                <div style={{ height: "140px", display: "flex", alignItems: "center", justifyContent: "center", fontSize: "12px", color: "var(--ink-3)" }}>
                  PDF page preview unavailable
                </div>
              ) : (
                <img
                  src={pdfPage === 0 && !expanded && imgSrc ? imgSrc : getPdfPageUrl(file.id, pdfPage)}
                  alt={`${file.title} page ${pdfPage + 1}`}
                  onError={() => setImgFailed(true)}
                  style={{ width: "100%", height: expanded ? "500px" : "220px", objectFit: "contain", display: "block" }}
                />
              )}
            </div>
            {file.extractedText && (
              <p style={{ fontSize: "12.5px", color: "var(--ink-2)", lineHeight: 1.5, maxHeight: "90px", overflow: "hidden" }}>
                {file.extractedText.slice(0, 260)}
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
          <span>RECEIPT</span>
          <span>{file.date}</span>
        </div>

        {imgSrc && file.modality === "image" && !imgFailed && (
          <div
            style={{
              marginTop: "10px",
              borderRadius: "5px",
              overflow: "hidden",
              border: "1px solid var(--rule)",
              maxHeight: "150px",
              background: "var(--bg-subtle)",
            }}
          >
            <img
              src={imgSrc}
              alt={file.title}
              onError={() => setImgFailed(true)}
              style={{
                width: "100%",
                height: "150px",
                objectFit: "cover",
                display: "block",
              }}
            />
          </div>
        )}

        <div
          style={{
            padding: "14px 0",
            borderBottom: "1px solid var(--rule)",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
          }}
        >
          <div>
            <div
              className="font-display"
              style={{ fontSize: "17px", fontWeight: 500 }}
            >
              {file.title}
            </div>
            <div
              style={{
                fontSize: "12px",
                color: "var(--ink-2)",
                marginTop: "2px",
              }}
            >
              {file.extractedText}
            </div>
          </div>
          <span
            className="font-display"
            style={{
              fontSize: "18px",
              fontWeight: 400,
              whiteSpace: "nowrap",
              marginLeft: "12px",
            }}
          >
            {file.extractedText.match(/(?:PHP|USD|EUR|GBP|JPY|₱|\$|€|£|¥)\s?[\d,.]+/i)?.[0] || "VERIFIED"}
          </span>
        </div>

        <div
          style={{
            paddingTop: "8px",
            display: "flex",
            justifyContent: "space-between",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
          <span>
            {file.ocrConfidence > 0
              ? `OCR CONFIDENCE: ${file.ocrConfidence.toFixed(1)}%`
              : "OCR TEXT"}
          </span>
          <span>LOCAL OCR</span>
        </div>
      </div>
    );
  }

  if (file.specimen === "diagram") {
    return (
      <div className="specimen-card">
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
          <span>DIAGRAM</span>
          <span>
            {file.format.toUpperCase()} · {file.size}
          </span>
        </div>
        {imgSrc && !imgFailed && (
          <div
            style={{
              marginBottom: "10px",
              borderRadius: "5px",
              overflow: "hidden",
              border: "1px solid var(--rule)",
              maxHeight: "170px",
              background: "var(--bg-subtle)",
            }}
          >
            <img
              src={imgSrc}
              alt={file.title}
              onError={() => setImgFailed(true)}
              style={{
                width: "100%",
                height: "170px",
                objectFit: "contain",
                display: "block",
              }}
            />
          </div>
        )}
        <div style={{ fontSize: "12px", color: "var(--ink-2)" }}>
          {file.extractedText}
        </div>
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            color: "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}
        >
          <span>{file.name} · {meta.lineCount ? `${meta.lineCount} lines` : file.format.toUpperCase()}</span>
          {isHtml ? (
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 8px", fontSize: "10.5px" }}
              onClick={() => setHtmlPreviewMode((m) => (m === "code" ? "render" : "code"))}
            >
              {htmlPreviewMode === "code" ? "live preview" : "view source"}
            </button>
          ) : isMd ? (
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 8px", fontSize: "10.5px" }}
              onClick={() => setMdMode((m) => (m === "render" ? "source" : "render"))}
            >
              {mdMode === "render" ? "view source" : "rendered"}
            </button>
          ) : isJson ? (
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 8px", fontSize: "10.5px" }}
              onClick={() => setPrettyJson((p) => !p)}
            >
              {prettyJson ? "raw json" : "pretty json"}
            </button>
          ) : (
            <span>{file.format.toUpperCase()}</span>
          )}
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
              borderRadius: "4px",
              background: "#fff",
            }}
          />
        ) : isMd && mdMode === "render" ? (
          <MarkdownPreviewBlock source={codeText} expanded={expanded} />
        ) : (
          <pre
            className="font-mono-code"
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

  const receiptFields = (meta.receiptFields || {}) as Record<string, string>;
  const hasReceiptFields = Object.keys(receiptFields).length > 0;
  const fullImgUrl = file.modality === "image" ? (expanded ? getFileRawUrl(file.id) : imgSrc || getFileRawUrl(file.id)) : imgSrc;

  return (
    <div className="specimen-card">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: "6px",
          borderBottom: "1px solid var(--rule)",
          paddingBottom: "8px",
          marginBottom: "10px",
          fontSize: "11px",
          color: "var(--ink-3)",
          fontFamily: "var(--mono)",
        }}
      >
        <span>
          {file.specimen === "idcard" ? "ID / FORM" : file.modality === "image" ? "IMAGE" : "DOCUMENT"}
          {meta.camera ? ` · ${meta.camera}` : ""}
          {file.width > 0 ? ` · ${file.width}×${file.height}` : ""}
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          {expanded && file.modality === "image" && (
            <>
              <button
                type="button"
                className="btn-tactile"
                style={{ padding: "2px 6px", fontSize: "10.5px" }}
                onClick={() => setImgZoom((z) => Math.max(1, +(z - 0.5).toFixed(1)))}
                title="Zoom out"
              >
                <ZoomOut size={11} />
              </button>
              <span>{Math.round(imgZoom * 100)}%</span>
              <button
                type="button"
                className="btn-tactile"
                style={{ padding: "2px 6px", fontSize: "10.5px" }}
                onClick={() => setImgZoom((z) => Math.min(3, +(z + 0.5).toFixed(1)))}
                title="Zoom in"
              >
                <ZoomIn size={11} />
              </button>
            </>
          )}
          {file.ocrText && (
            <button
              type="button"
              className={`btn-tactile ${showOcrOverlay ? "solid" : ""}`}
              style={{ padding: "2px 7px", fontSize: "10.5px" }}
              onClick={() => setShowOcrOverlay((v) => !v)}
            >
              {showOcrOverlay ? "hide ocr" : "ocr text"}
            </button>
          )}
          <span>
            {file.format.toUpperCase()} · {file.size}
          </span>
        </div>
      </div>

      {fullImgUrl && file.modality === "image" && !imgFailed && (
        <div
          style={{
            marginBottom: "10px",
            borderRadius: "5px",
            overflow: "auto",
            border: "1px solid var(--rule)",
            maxHeight: expanded ? "480px" : "180px",
            background: "var(--bg-subtle)",
            position: "relative",
          }}
        >
          <img
            src={fullImgUrl}
            alt={file.title}
            onError={() => setImgFailed(true)}
            style={{
              width: `${imgZoom * 100}%`,
              height: expanded ? `${Math.round(440 * imgZoom)}px` : "180px",
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
                background: "rgba(18, 17, 15, 0.86)",
                color: "#F7F6F2",
                fontSize: "12px",
                fontFamily: "var(--mono)",
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
            borderRadius: "5px",
            border: "1px dashed var(--rule-strong)",
            background: "var(--bg-subtle)",
            fontSize: "12px",
            color: "var(--ink-3)",
            textAlign: "center",
          }}
        >
          Image preview unavailable (file may be unreadable or corrupt)
        </div>
      )}

      {hasReceiptFields && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "6px",
            marginBottom: "10px",
          }}
        >
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
        className="font-display"
        style={{ fontSize: "15px", lineHeight: 1.55, color: "var(--ink)", whiteSpace: "pre-wrap", maxHeight: expanded ? "300px" : "130px", overflowY: "auto" }}
      >
        {fullExcerpt || (file.extractedText ? `“${file.extractedText}”` : "(no text excerpt)")}
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

  const smartSuggestions = useMemo(() => {
    const candidates = new Set<string>();
    if (file.modality) candidates.add(file.modality.toLowerCase());
    if (file.format) candidates.add(file.format.toLowerCase());
    if (file.shelfName) {
      file.shelfName
        .toLowerCase()
        .split(/[\s/&,_.-]+/)
        .filter((w) => w.length >= 3 && w.length <= 16)
        .slice(0, 2)
        .forEach((w) => candidates.add(w));
    }
    if (file.specimen === "receipt") candidates.add("receipt");
    return Array.from(candidates)
      .filter((c) => !currentTags.includes(c))
      .slice(0, 4);
  }, [file.id, file.modality, file.format, file.shelfName, file.specimen, currentTags]);

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
    <div className="tags-panel">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            fontSize: "11px",
            fontWeight: 600,
            letterSpacing: "0.06em",
            color: "var(--ink-3)",
            textTransform: "uppercase",
          }}
        >
          <Tag size={11} /> tags
        </span>
        {currentTags.length > 0 && (
          <span className="font-mono-code" style={{ fontSize: "10.5px", color: "var(--ink-3)" }}>
            {currentTags.length}
          </span>
        )}
      </div>

      {currentTags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
          {currentTags.map((t) => (
            <span key={t} className="tag-pill">
              <span>#{t}</span>
              <button
                type="button"
                aria-label={`Remove tag ${t}`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  color: "var(--ink-3)",
                  padding: "1px",
                }}
                onClick={() => removeTag(t)}
              >
                <X size={11} />
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
        <Plus size={13} style={{ color: "var(--ink-3)", flexShrink: 0 }} />
        <input
          type="text"
          value={tagInput}
          onChange={(e) => setTagInput(e.target.value)}
          placeholder="add tag and press enter…"
          style={{
            flex: 1,
            minWidth: 0,
            fontSize: "12px",
            color: "var(--ink)",
          }}
        />
        {tagInput.trim().length > 0 && (
          <button
            type="submit"
            className="btn-tactile solid"
            style={{ padding: "2px 8px", fontSize: "11px", borderRadius: "5px" }}
          >
            add
          </button>
        )}
      </form>

      {smartSuggestions.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px", paddingTop: "2px" }}>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "4px",
              fontSize: "10.5px",
              color: "var(--ink-3)",
            }}
          >
            <Sparkles size={10} /> suggest:
          </span>
          {smartSuggestions.map((s) => (
            <button
              key={s}
              type="button"
              className="tag-pill-suggest"
              onClick={() => addTag(s)}
            >
              + #{s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

const FileQuickActions: React.FC<{
  file: ShelfFile;
  actionBusy: boolean;
  onFormatAction: (action: string) => void;
  onCopyText: (text: string, label: string) => void;
  onFindSimilar?: (file: ShelfFile) => void;
  onOpenRenameDialog?: (file: ShelfFile) => void;
  onOpenMoveDialog?: (file: ShelfFile, mode?: "move" | "copy") => void;
  onOpenDeleteDialog?: (file: ShelfFile) => void;
  onRefreshState?: (state: any) => void;
  onShowToast: (msg: string) => void;
}> = ({
  file,
  actionBusy,
  onFormatAction,
  onCopyText,
  onFindSimilar,
  onOpenRenameDialog,
  onOpenMoveDialog,
  onOpenDeleteDialog,
  onRefreshState,
  onShowToast,
}) => (
  <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
    {onFindSimilar && (
      <button
        type="button"
        className="btn-tactile solid"
        style={{ fontSize: "11.5px", padding: "5px 10px" }}
        onClick={() => onFindSimilar(file)}
      >
        <Sparkles size={11} />
        <span>find similar</span>
      </button>
    )}
    {file.modality === "video" && (
      <button
        type="button"
        className="btn-tactile"
        style={{ fontSize: "11.5px", padding: "5px 10px" }}
        disabled={actionBusy}
        onClick={() => onFormatAction("extract_frame")}
      >
        save video frame (.png)
      </button>
    )}
    {(file.format === "xlsx" || file.format === "xlsm") && (
      <button
        type="button"
        className="btn-tactile"
        style={{ fontSize: "11.5px", padding: "5px 10px" }}
        disabled={actionBusy}
        onClick={() => onFormatAction("export_csv")}
      >
        export as .csv
      </button>
    )}
    {file.format === "pdf" && (
      <button
        type="button"
        className="btn-tactile"
        style={{ fontSize: "11.5px", padding: "5px 10px" }}
        disabled={actionBusy}
        onClick={() => onFormatAction("extract_pdf_text")}
      >
        extract text (.txt)
      </button>
    )}
    {file.modality === "image" && (
      <button
        type="button"
        className="btn-tactile"
        style={{ fontSize: "11.5px", padding: "5px 10px" }}
        disabled={actionBusy}
        onClick={() => onFormatAction("run_ocr")}
      >
        re-run ocr
      </button>
    )}
    {(file.extractedText || file.ocrText) && (
      <button
        type="button"
        className="btn-tactile"
        style={{ fontSize: "11.5px", padding: "5px 10px" }}
        onClick={() => onCopyText(file.extractedText || file.ocrText, "extracted content")}
      >
        copy text
      </button>
    )}
    <button
      type="button"
      className={`btn-tactile ${file.pinned ? "solid" : ""}`}
      style={{ fontSize: "11.5px", padding: "5px 10px" }}
      onClick={async () => {
        try {
          const res = await togglePinFile(file.id);
          onRefreshState?.(res.state);
          onShowToast(res.pinned ? `pinned ${file.name}` : `unpinned ${file.name}`);
        } catch (err) {
          onShowToast(err instanceof Error ? err.message : "pin failed");
        }
      }}
    >
      {file.pinned ? "★ pinned" : "☆ pin"}
    </button>
    <button
      type="button"
      className="btn-tactile"
      style={{ fontSize: "11.5px", padding: "5px 10px" }}
      onClick={async () => {
        try {
          await openFileExternal(file.id, false);
        } catch {
          try {
            await openFileInOS(file.path);
          } catch (err) {
            onShowToast(err instanceof Error ? err.message : "Could not open file");
          }
        }
      }}
    >
      open
    </button>
    <button
      type="button"
      className="btn-tactile"
      style={{ fontSize: "11.5px", padding: "5px 10px" }}
      onClick={async () => {
        try {
          await openFileExternal(file.id, true);
        } catch {
          try {
            await revealFileInOS(file.path);
          } catch (err) {
            onShowToast(err instanceof Error ? err.message : "Could not reveal file");
          }
        }
      }}
    >
      reveal
    </button>
    <button
      type="button"
      className="btn-tactile"
      style={{ fontSize: "11.5px", padding: "5px 10px" }}
      onClick={() => onOpenRenameDialog?.(file)}
    >
      rename…
    </button>
    <button
      type="button"
      className="btn-tactile"
      style={{ fontSize: "11.5px", padding: "5px 10px" }}
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
          onShowToast(`moved ${file.name}`);
        } catch (err) {
          onShowToast(err instanceof Error ? err.message : "move failed");
        }
      }}
    >
      move…
    </button>
    <button
      type="button"
      className="btn-tactile"
      style={{ fontSize: "11.5px", padding: "5px 10px" }}
      onClick={async () => {
        if (onOpenMoveDialog) {
          onOpenMoveDialog(file, "copy");
          return;
        }
        const dest = await pickFolderNative();
        if (!dest) return;
        try {
          const res = await copyFileToFolder(file.id, dest);
          onRefreshState?.(res.state);
          onShowToast(`copied ${file.name}`);
        } catch (err) {
          onShowToast(err instanceof Error ? err.message : "copy failed");
        }
      }}
    >
      copy…
    </button>
    <button
      type="button"
      className="btn-tactile danger"
      style={{ fontSize: "11.5px", padding: "5px 10px" }}
      onClick={async () => {
        if (onOpenDeleteDialog) {
          onOpenDeleteDialog(file);
          return;
        }
        try {
          const res = await trashFile(file.id);
          onRefreshState?.(res.state);
          onShowToast(`moved ${file.name} to trash (undo with ctrl+z)`);
        } catch (err) {
          onShowToast(err instanceof Error ? err.message : "trash failed");
        }
      }}
    >
      delete…
    </button>
  </div>
);

export const InspectorSheet: React.FC<InspectorSheetProps> = React.memo(({
  file,
  relatedFiles = [],
  visible,
  silhouetteScore,
  quickLookOpen,
  organizeModalOpen,
  tidyMoves,
  onCloseQuickLook,
  onOpenQuickLook,
  onStepQuickLook,
  onFindSimilar,
  onHideInspector,
  onShowInspector,
  onSelectFile,
  onOpenRenameDialog,
  onOpenMoveDialog,
  onOpenDeleteDialog,
  onCloseOrganizeModal,
  onConfirmOrganizeDisk,
  onRollbackOrganizeDisk,
  onToggleRename,
  onCopyPath,
  onShowToast,
  onRefreshState,
}) => {
  const [techDetailsOpen, setTechDetailsOpen] = useState<boolean>(false);
  const [width, setWidth] = useState<number>(400);
  const [isResizing, setIsResizing] = useState<boolean>(false);
  const [actionBusy, setActionBusy] = useState<boolean>(false);

  const handleFormatAction = async (action: string) => {
    if (!file || actionBusy) return;
    setActionBusy(true);
    try {
      const res = await runFileAction(file.id, action);
      onShowToast(res.message);
      if (res.state && onRefreshState) {
        onRefreshState(res.state);
      }
    } catch (err) {
      onShowToast(`Action failed: ${err instanceof Error ? err.message : "error"}`);
    } finally {
      setActionBusy(false);
    }
  };

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

  useEffect(() => {
    if (!isResizing) return;
    const onMove = (e: MouseEvent) => {
      const nextWidth = Math.max(
        320,
        Math.min(580, window.innerWidth - e.clientX)
      );
      setWidth(nextWidth);
    };
    const onUp = () => setIsResizing(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [isResizing]);

  const handleOpenOS = async () => {
    if (!file) return;
    try {
      await openFileInOS(file.path);
      onShowToast(`Opened ${file.name}`);
    } catch {
      onShowToast(`File located at ${file.folder}/${file.name}`);
    }
  };

  const handleRevealOS = async () => {
    if (!file) return;
    try {
      await revealFileInOS(file.path);
      onShowToast(`Revealed ${file.name} in file manager`);
    } catch {
      onShowToast(`Folder: ${file.folderFull}`);
    }
  };

  return (
    <>
      <aside
        className={`right-inspector ${!visible ? "collapsed" : ""} ${
          isResizing ? "resizing" : ""
        }`}
        style={{ width: visible ? `${width}px` : "0px" }}
        id="rightInspector"
        aria-label="Selected file inspector"
        aria-hidden={!visible}
      >
        <div
          className={`inspector-resizer ${isResizing ? "dragging" : ""}`}
          onMouseDown={() => setIsResizing(true)}
          title="Drag to resize inspector"
        />

        <div
          style={{
            height: "56px",
            padding: "0 16px 0 20px",
            borderBottom: "1px solid var(--rule)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexShrink: 0,
            gap: "8px",
          }}
        >
          <span
            style={{
              fontSize: "11.5px",
              fontWeight: 500,
              letterSpacing: "0.06em",
              color: "var(--ink-3)",
            }}
          >
            inspector
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "4px 9px", fontSize: "11.5px" }}
              onClick={onOpenQuickLook}
            >
              expand (space)
            </button>
            {onHideInspector && (
              <button
                type="button"
                className="btn-tactile"
                style={{ padding: "4px 8px", fontSize: "11.5px" }}
                onClick={onHideInspector}
                title="Hide inspector (Esc or I)"
                aria-label="Hide inspector"
              >
                <PanelRightClose size={13} />
                <span>hide (esc)</span>
              </button>
            )}
          </div>
        </div>

        {file ? (
          <div
            id="inspectorContent"
            style={{
              flex: 1,
              overflowY: "auto",
              padding: "22px 20px",
              display: "flex",
              flexDirection: "column",
              gap: "20px",
            }}
          >
            <div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "8px",
                  marginBottom: "6px",
                }}
              >
                <span className="file-type-tag">{file.format}</span>
                <span style={{ fontSize: "12px", color: "var(--ink-2)" }}>
                  {file.shelfName}
                </span>
              </div>
              <h3
                className="font-display"
                style={{
                  fontSize: "22px",
                  fontWeight: 400,
                  lineHeight: 1.15,
                  letterSpacing: "-0.02em",
                  overflowWrap: "anywhere",
                }}
              >
                {file.title}
              </h3>
              <div
                className="font-mono-code"
                style={{
                  fontSize: "11.5px",
                  color: "var(--ink-3)",
                  marginTop: "6px",
                  wordBreak: "break-all",
                }}
              >
                {file.folder}/{file.name} · {file.size}
              </div>
            </div>

            <SpecimenPreview file={file} onCopyText={handleCopyText} />

            {/* Format-Specific Quick Actions */}
            <FileQuickActions
              file={file}
              actionBusy={actionBusy}
              onFormatAction={handleFormatAction}
              onCopyText={handleCopyText}
              onFindSimilar={onFindSimilar}
              onOpenRenameDialog={onOpenRenameDialog}
              onOpenMoveDialog={onOpenMoveDialog}
              onOpenDeleteDialog={onOpenDeleteDialog}
              onRefreshState={onRefreshState}
              onShowToast={onShowToast}
            />

            {/* Custom Tags */}
            <TagsEditor file={file} onRefreshState={onRefreshState} onShowToast={onShowToast} />

            {/* Auto-Rename Suggestion Card */}
            {(file.suggestedName || file.renamed) && (
              <div
                style={{
                  padding: "14px",
                  border: "1px solid var(--rule-strong)",
                  borderRadius: "7px",
                  background: "var(--bg-canvas)",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    marginBottom: "6px",
                  }}
                >
                  <span
                    style={{
                      fontSize: "11px",
                      fontWeight: 600,
                      letterSpacing: "0.05em",
                      color: "var(--ink-3)",
                    }}
                  >
                    SUGGESTED FILENAME
                  </span>
                  <span style={{ fontSize: "11px", color: "var(--ink-2)" }}>
                    {file.renamed ? "applied" : "ready"}
                  </span>
                </div>
                <div
                  className="font-mono-code"
                  style={{
                    fontSize: "12px",
                    color: "var(--ink)",
                    wordBreak: "break-all",
                    marginBottom: "10px",
                  }}
                >
                  {file.renamed
                    ? `${file.originalName} → ${file.name}`
                    : file.suggestedName}
                </div>
                <button
                  type="button"
                  className={`btn-tactile ${file.renamed ? "" : "solid"}`}
                  style={{ width: "100%" }}
                  onClick={() => onToggleRename(file.id)}
                >
                  {file.renamed ? "undo rename" : "apply clean filename"}
                </button>
              </div>
            )}

            {/* Why It Belongs & Modality Balance Bar */}
            <div
              style={{
                paddingTop: "14px",
                borderTop: "1px solid var(--rule)",
              }}
            >
              <div
                style={{
                  fontSize: "11px",
                  fontWeight: 600,
                  letterSpacing: "0.05em",
                  color: "var(--ink-3)",
                  marginBottom: "6px",
                }}
              >
                MATCH REASON
              </div>
              <p
                style={{
                  fontSize: "13px",
                  color: "var(--ink-2)",
                  lineHeight: 1.55,
                }}
              >
                {file.whyMatched}
              </p>

              <div style={{ marginTop: "12px" }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: "11px",
                    color: "var(--ink-3)",
                    marginBottom: "4px",
                  }}
                >
                  <span>visual cues ({file.visualShare}%)</span>
                  <span>words &amp; ocr ({file.textShare}%)</span>
                </div>
                <div
                  style={{
                    height: "5px",
                    width: "100%",
                    background: "var(--rule)",
                    borderRadius: "3px",
                    overflow: "hidden",
                    display: "flex",
                  }}
                >
                  <div
                    style={{
                      width: `${file.visualShare}%`,
                      background: "var(--ink)",
                    }}
                  />
                  <div
                    style={{
                      width: `${file.textShare}%`,
                      background: "var(--ink-3)",
                      opacity: 0.4,
                    }}
                  />
                </div>
              </div>
            </div>

            {/* Technical Details Drawer */}
            <div
              style={{
                paddingTop: "12px",
                borderTop: "1px solid var(--rule)",
              }}
            >
              <button
                type="button"
                onClick={() => setTechDetailsOpen((v) => !v)}
                style={{
                  width: "100%",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  fontSize: "12px",
                  color: "var(--ink-3)",
                }}
              >
                <span>technical details</span>
                <span>{techDetailsOpen ? "hide −" : "show +"}</span>
              </button>
              <div
                className={`accordion-drawer ${techDetailsOpen ? "open" : ""}`}
              >
                <div className="accordion-inner">
                  <div
                    className="font-mono-code"
                    style={{
                      marginTop: "10px",
                      padding: "10px",
                      borderRadius: "6px",
                      background: "var(--bg-canvas)",
                      border: "1px solid var(--rule)",
                      fontSize: "11px",
                      color: "var(--ink-2)",
                      lineHeight: 1.65,
                    }}
                  >
                    <div>encoder: quantized onnx multimodal (512-d)</div>
                    <div>
                      cosine sim: {file.cosine} · norm: {file.vectorNorm}
                    </div>
                    <div>sha-256: {file.sha256.slice(0, 16)}…</div>
                    <div>
                      cluster: hdbscan #{file.clusterId} ({file.shelfName})
                    </div>
                    <div>
                      silhouette: {silhouetteScore.toFixed(3)} · store:
                      sqlite-vec
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* Smart Related Files on Same Shelf */}
            {relatedFiles.length > 0 && onSelectFile && (
              <div
                style={{
                  paddingTop: "14px",
                  borderTop: "1px solid var(--rule)",
                }}
              >
                <div
                  style={{
                    fontSize: "11px",
                    fontWeight: 600,
                    letterSpacing: "0.05em",
                    color: "var(--ink-3)",
                    marginBottom: "8px",
                  }}
                >
                  RELATED IN “{file.shelfName.toUpperCase()}”
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: "5px" }}>
                  {relatedFiles.map((rf) => (
                    <button
                      key={rf.id}
                      type="button"
                      className="folder-choice-card"
                      style={{ padding: "7px 10px" }}
                      onClick={() => onSelectFile(rf.id)}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: "8px", minWidth: 0 }}>
                        <span className="file-type-tag">{rf.format}</span>
                        <span
                          style={{
                            fontSize: "12.5px",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {rf.title}
                        </span>
                      </div>
                      <span style={{ fontSize: "11px", color: "var(--ink-3)", flexShrink: 0 }}>→</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div
            style={{
              flex: 1,
              padding: "24px 20px",
              color: "var(--ink-3)",
              fontSize: "13.5px",
            }}
          >
            Select any file on the left to inspect its preview and semantic
            vector signature.
          </div>
        )}

        {/* Bottom Native OS Actions */}
        {file && (
          <div
            style={{
              padding: "12px 20px",
              borderTop: "1px solid var(--rule)",
              display: "flex",
              flexWrap: "wrap",
              gap: "6px",
              flexShrink: 0,
              background: "var(--bg-rail)",
            }}
          >
            <button
              type="button"
              className="btn-tactile solid"
              style={{ flex: 1 }}
              onClick={onOpenQuickLook}
            >
              quick look
            </button>
            <button
              type="button"
              className="btn-tactile"
              onClick={handleOpenOS}
              title="Open with default OS application"
            >
              open
            </button>
            <button
              type="button"
              className="btn-tactile"
              onClick={handleRevealOS}
              title="Reveal in File Explorer / File Manager"
            >
              reveal
            </button>
            <button
              type="button"
              className="btn-tactile"
              onClick={() => onCopyPath(file)}
            >
              copy path
            </button>
          </div>
        )}
      </aside>

      {!visible && file && onShowInspector && (
        <button
          type="button"
          className="inspector-reveal-fab"
          onClick={onShowInspector}
          title="Show inspector (I)"
          aria-label="Show inspector"
        >
          <PanelRightOpen size={14} />
          <span>inspect</span>
        </button>
      )}

      {/* MODAL 1: EXPANDED QUICK LOOK SPECIMEN */}
      <div
        className={`modal-wrap ${quickLookOpen ? "open" : ""}`}
        id="quickLookModal"
        role="dialog"
        aria-modal="true"
        aria-label={file ? `Quick Look: ${file.title}` : "Quick Look"}
        aria-hidden={!quickLookOpen}
        onClick={(e) => {
          if (e.target === e.currentTarget) onCloseQuickLook();
        }}
      >
        {file && (
          <div className="modal-card" style={{ maxWidth: "920px" }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "flex-start",
                gap: "16px",
                paddingBottom: "16px",
                borderBottom: "1px solid var(--rule)",
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div
                  style={{
                    fontSize: "12px",
                    color: "var(--ink-3)",
                    fontFamily: "var(--mono)",
                    wordBreak: "break-all",
                  }}
                >
                  {file.folder}/{file.name} · {file.size} · {file.shelfName}
                </div>
                <h2
                  className="font-display"
                  style={{
                    fontSize: "26px",
                    fontWeight: 400,
                    letterSpacing: "-0.02em",
                    marginTop: "4px",
                    overflowWrap: "anywhere",
                  }}
                >
                  {file.title}
                </h2>
              </div>
              <div style={{ display: "flex", gap: "6px", flexShrink: 0, flexWrap: "wrap" }}>
                {onStepQuickLook && (
                  <>
                    <button
                      type="button"
                      className="btn-tactile"
                      onClick={() => onStepQuickLook(-1)}
                      title="Previous file (←)"
                      aria-label="Previous file"
                    >
                      <ChevronLeft size={14} />
                    </button>
                    <button
                      type="button"
                      className="btn-tactile"
                      onClick={() => onStepQuickLook(1)}
                      title="Next file (→)"
                      aria-label="Next file"
                    >
                      <ChevronRight size={14} />
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="btn-tactile"
                  onClick={handleOpenOS}
                >
                  open in os
                </button>
                <button
                  type="button"
                  className="btn-tactile"
                  onClick={onCloseQuickLook}
                >
                  close (esc)
                </button>
              </div>
            </div>

            <div style={{ margin: "20px 0", display: "flex", flexDirection: "column", gap: "16px" }}>
              <SpecimenPreview file={file} expanded onCopyText={handleCopyText} />

              <FileQuickActions
                file={file}
                actionBusy={actionBusy}
                onFormatAction={handleFormatAction}
                onCopyText={handleCopyText}
                onFindSimilar={(f) => {
                  onCloseQuickLook();
                  onFindSimilar?.(f);
                }}
                onOpenRenameDialog={onOpenRenameDialog}
                onOpenMoveDialog={onOpenMoveDialog}
                onOpenDeleteDialog={onOpenDeleteDialog}
                onRefreshState={onRefreshState}
                onShowToast={onShowToast}
              />

              {/* Custom Tags */}
              <TagsEditor file={file} onRefreshState={onRefreshState} onShowToast={onShowToast} />
            </div>

            <div
              style={{
                padding: "14px 16px",
                borderRadius: "6px",
                background: "var(--bg-subtle)",
                border: "1px solid var(--rule)",
                fontSize: "13.5px",
                color: "var(--ink-2)",
              }}
            >
              <strong>Why it matched:</strong> {file.whyMatched}
            </div>
          </div>
        )}
      </div>

      {/* MODAL 2: SAFE PHYSICAL FOLDER TIDY PREVIEW */}
      <div
        className={`modal-wrap ${organizeModalOpen ? "open" : ""}`}
        id="organizeModal"
        role="dialog"
        aria-modal="true"
        aria-label="Organize physical folders from your shelves"
        aria-hidden={!organizeModalOpen}
        onClick={(e) => {
          if (e.target === e.currentTarget) onCloseOrganizeModal();
        }}
      >
        <div className="modal-card">
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "flex-start",
              gap: "16px",
              paddingBottom: "16px",
              borderBottom: "1px solid var(--rule)",
            }}
          >
            <div>
              <div
                style={{
                  fontSize: "11.5px",
                  fontWeight: 500,
                  letterSpacing: "0.06em",
                  color: "var(--ink-3)",
                }}
              >
                safe disk preview
              </div>
              <h2
                className="font-display"
                style={{
                  fontSize: "26px",
                  fontWeight: 400,
                  letterSpacing: "-0.02em",
                  marginTop: "2px",
                }}
              >
                organize physical folders from your shelves?
              </h2>
            </div>
            <button
              type="button"
              className="btn-tactile"
              onClick={onCloseOrganizeModal}
            >
              cancel
            </button>
          </div>

          <p
            style={{
              fontSize: "14px",
              color: "var(--ink-2)",
              margin: "16px 0",
            }}
          >
            by default,{" "}
            <strong style={{ color: "var(--ink)" }}>good shelves</strong> keeps
            your shelves virtual so existing project paths never break. if you
            want to tidy your physical folders, review the real filesystem moves
            below first:
          </p>

          <div
            style={{
              border: "1px solid var(--rule)",
              borderRadius: "7px",
              overflow: "hidden",
              marginBottom: "20px",
              fontSize: "13px",
              maxHeight: "260px",
              overflowY: "auto",
            }}
          >
            {tidyMoves.length === 0 ? (
              <div
                style={{
                  padding: "16px",
                  background: "var(--bg-subtle)",
                  color: "var(--ink-2)",
                }}
              >
                All files are already organized into their target ~/Shelves
                directories.
              </div>
            ) : (
              tidyMoves.map((mv, idx) => (
                <div
                  key={mv.fileId}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: "12px",
                    padding: "11px 14px",
                    borderBottom:
                      idx < tidyMoves.length - 1
                        ? "1px solid var(--rule)"
                        : "none",
                    background: "var(--bg-subtle)",
                  }}
                >
                  <span
                    className="font-mono-code"
                    style={{
                      color: "var(--ink-2)",
                      fontSize: "12px",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {mv.fromShort}
                  </span>
                  <span
                    className="font-mono-code"
                    style={{
                      fontWeight: 500,
                      fontSize: "12px",
                      flexShrink: 0,
                    }}
                  >
                    → {mv.toShort}
                  </span>
                </div>
              ))
            )}
          </div>

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              flexWrap: "wrap",
              gap: "10px",
            }}
          >
            <button
              type="button"
              className="btn-tactile"
              onClick={onRollbackOrganizeDisk}
            >
              rollback last move batch
            </button>
            <div style={{ display: "flex", gap: "10px" }}>
              <button
                type="button"
                className="btn-tactile"
                onClick={onCloseOrganizeModal}
              >
                keep virtual shelves only
              </button>
              <button
                type="button"
                className="btn-tactile solid"
                disabled={tidyMoves.length === 0}
                onClick={onConfirmOrganizeDisk}
              >
                move {tidyMoves.length} files safely
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
});
