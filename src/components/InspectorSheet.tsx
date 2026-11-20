import React, { useState, useEffect } from "react";
import {
  ShelfFile,
  ShelfGroup,
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

interface InspectorSheetProps {
  file: ShelfFile | undefined;
  relatedFiles?: ShelfFile[];
  shelves?: ShelfGroup[];
  visible: boolean;
  silhouetteScore: number;
  quickLookOpen: boolean;
  onCloseQuickLook: () => void;
  onOpenQuickLook: () => void;
  onStepQuickLook?: (dir: -1 | 1) => void;
  onFindSimilar?: (file: ShelfFile) => void;
  onAssignShelf?: (fileId: string, shelfId: string) => Promise<void> | void;
  onHideInspector?: () => void;
  onSelectFile?: (fileId: string) => void;
  onOpenRenameDialog?: (file: ShelfFile) => void;
  onOpenMoveDialog?: (file: ShelfFile, mode?: "move" | "copy") => void;
  onOpenDeleteDialog?: (file: ShelfFile) => void;
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
    const extLower = (file.format || "").toLowerCase();
    const kindLower = String(meta.kind || "").toLowerCase();
    const needsExcerpt =
      file.specimen === "code" ||
      ["markdown", "text", "docx", "doc", "odt", "rtf", "pdf"].includes(kindLower) ||
      ["json", "txt", "docx", "doc", "odt", "rtf", "log", "xml", "yaml", "yml", "toml", "csv", "tsv", "md"].includes(extLower);
    if (!needsExcerpt) return;
    let cancelled = false;
    fetchFileFullExcerpt(file, expanded ? 450 : 120)
      .then((res) => {
        if (!cancelled && res.content) setFullExcerpt(res.content);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [expanded, file, meta.kind]);

  if (meta.kind === "archive" && Array.isArray(meta.entries)) {
    const rawEntries = meta.entries as Array<{ name: string; sizeBytes: number; isDir: boolean; ext: string }>;
    const entries = rawEntries.filter(
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
            fontWeight: 600,
            color: "var(--ink-3)",
          }}
        >
          <span>ARCHIVE · {String(meta.format || file.format).toUpperCase()}</span>
          <span>{meta.totalEntries ?? entries.length} entries</span>
        </div>
        {(expanded || rawEntries.length > 5 || archiveFilter.trim().length > 0) && (
          <input
            type="text"
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
            fontWeight: 600,
            color: "var(--ink-3)",
          }}
        >
          <span>SLIDES · {slides.length} DECK</span>
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 7px", minHeight: "24px", fontSize: "11px" }}
              disabled={slideIdx <= 0}
              onClick={() => setSlideIdx((s) => Math.max(0, s - 1))}
            >
              Prev
            </button>
            <span>{slideIdx + 1}/{slides.length}</span>
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 7px", minHeight: "24px", fontSize: "11px" }}
              disabled={slideIdx >= slides.length - 1}
              onClick={() => setSlideIdx((s) => Math.min(slides.length - 1, s + 1))}
            >
              Next
            </button>
          </div>
        </div>
        <div style={{ padding: "12px", borderRadius: "7px", border: "1px solid var(--rule)", background: "var(--bg-surface)" }}>
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            fontWeight: 600,
            color: "var(--ink-3)",
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
            borderRadius: "6px",
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            fontWeight: 600,
            color: "var(--ink-3)",
          }}
        >
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
            fontWeight: 600,
            color: "var(--ink-3)",
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
        {(expanded || allRows.length > 5 || tableFilter.trim().length > 0) && (
          <input
            type="text"
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
            fontWeight: 600,
            color: "var(--ink-3)",
          }}
        >
          <span>PDF · {totalPages} {totalPages === 1 ? "PAGE" : "PAGES"}</span>
          <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 7px", minHeight: "24px", fontSize: "11px" }}
              onClick={() => setPdfMode((m) => (m === "image" ? "text" : "image"))}
            >
              {pdfMode === "image" ? "Read text" : "Page view"}
            </button>
            {totalPages > 1 && pdfMode === "image" && (
              <>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "2px 6px", minHeight: "24px", fontSize: "11px" }}
                  disabled={pdfPage <= 0}
                  onClick={() => setPdfPage((p) => Math.max(0, p - 1))}
                >
                  Prev
                </button>
                <span>{pdfPage + 1}/{totalPages}</span>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "2px 6px", minHeight: "24px", fontSize: "11px" }}
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
                borderRadius: "6px",
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            fontSize: "11px",
            fontWeight: 600,
            color: "var(--ink-3)",
          }}
        >
          <span>RECEIPT</span>
          <span>{file.date}</span>
        </div>

        {imgSrc && file.modality === "image" && !imgFailed && (
          <div
            style={{
              marginTop: "10px",
              borderRadius: "6px",
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
            padding: "12px 0 4px",
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            fontWeight: 600,
            color: "var(--ink-3)",
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
              borderRadius: "6px",
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
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            borderBottom: "1px solid var(--rule)",
            paddingBottom: "8px",
            marginBottom: "10px",
            fontSize: "11px",
            fontWeight: 600,
            color: "var(--ink-3)",
          }}
        >
          <span>{file.name} · {meta.lineCount ? `${meta.lineCount} lines` : file.format.toUpperCase()}</span>
          {isHtml ? (
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 8px", minHeight: "24px", fontSize: "11px" }}
              onClick={() => setHtmlPreviewMode((m) => (m === "code" ? "render" : "code"))}
            >
              {htmlPreviewMode === "code" ? "Live preview" : "View source"}
            </button>
          ) : isMd ? (
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 8px", minHeight: "24px", fontSize: "11px" }}
              onClick={() => setMdMode((m) => (m === "render" ? "source" : "render"))}
            >
              {mdMode === "render" ? "View source" : "Rendered"}
            </button>
          ) : isJson ? (
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 8px", minHeight: "24px", fontSize: "11px" }}
              onClick={() => setPrettyJson((p) => !p)}
            >
              {prettyJson ? "Raw JSON" : "Pretty JSON"}
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
              borderRadius: "6px",
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
            fontWeight: 600,
            color: "var(--ink-3)",
          }}
        >
          <span>
            DOCUMENT · {extLow.toUpperCase()} · {wordCount} words
          </span>
          {onCopyText && (fullExcerpt || file.extractedText) && (
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "2px 8px", minHeight: "24px", fontSize: "11px" }}
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
          fontWeight: 600,
          color: "var(--ink-3)",
        }}
      >
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
                className="btn-tactile"
                style={{ padding: "2px 7px", minHeight: "22px", fontSize: "11px" }}
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
                style={{ fontSize: "11px", padding: "0 4px", color: "var(--ink-2)" }}
              >
                {Math.round(imgZoom * 100)}%
              </button>
              <button
                type="button"
                className="btn-tactile"
                style={{ padding: "2px 7px", minHeight: "22px", fontSize: "11px" }}
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
              className={`btn-tactile ${showOcrOverlay ? "active-tab" : ""}`}
              style={{ padding: "2px 7px", minHeight: "22px", fontSize: "10.5px" }}
              onClick={() => setShowOcrOverlay((v) => !v)}
            >
              {showOcrOverlay ? "Hide OCR" : "OCR text"}
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
          value={tagInput}
          onChange={(e) => setTagInput(e.target.value)}
          placeholder="Add tag and press Enter…"
          style={{ flex: 1, minWidth: 0, fontSize: "12.5px", color: "var(--ink)" }}
        />
        {tagInput.trim().length > 0 && (
          <button
            type="submit"
            className="btn-tactile solid"
            style={{ padding: "2px 8px", minHeight: "24px", fontSize: "11px", borderRadius: "6px" }}
          >
            Add
          </button>
        )}
      </form>
    </div>
  );
};

const FileQuickActions: React.FC<{
  file: ShelfFile;
  compact?: boolean;
  onOpenOS?: () => void;
  onRevealOS?: () => void;
  onFindSimilar?: (file: ShelfFile) => void;
  onOpenRenameDialog?: (file: ShelfFile) => void;
  onOpenMoveDialog?: (file: ShelfFile, mode?: "move" | "copy") => void;
  onOpenDeleteDialog?: (file: ShelfFile) => void;
  onRefreshState?: (state: any) => void;
  onShowToast: (msg: string) => void;
}> = ({
  file,
  compact = false,
  onOpenOS,
  onRevealOS,
  onFindSimilar,
  onOpenRenameDialog,
  onOpenMoveDialog,
  onOpenDeleteDialog,
  onRefreshState,
  onShowToast,
}) => {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
      {/* Primary Actions */}
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

      {/* Quiet Secondary Actions Strip */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "4px 6px",
          paddingTop: "4px",
        }}
      >
        <button
          type="button"
          className="shelf-header-subtle-btn"
          onClick={() => onOpenRenameDialog?.(file)}
          title="Rename file (F2 or R)"
        >
          Rename
        </button>
        <button
          type="button"
          className="shelf-header-subtle-btn"
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
        {onFindSimilar && (
          <button
            type="button"
            className="shelf-header-subtle-btn"
            onClick={() => onFindSimilar(file)}
          >
            Find similar
          </button>
        )}
        <button
          type="button"
          className={`shelf-header-subtle-btn ${file.pinned ? "active" : ""}`}
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
        <button
          type="button"
          className="shelf-header-subtle-btn danger"
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

export const InspectorSheet: React.FC<InspectorSheetProps> = React.memo(({
  file,
  shelves = [],
  visible,
  quickLookOpen,
  onCloseQuickLook,
  onOpenQuickLook,
  onStepQuickLook,
  onFindSimilar,
  onAssignShelf,
  onHideInspector,
  onOpenRenameDialog,
  onOpenMoveDialog,
  onOpenDeleteDialog,
  onToggleRename,
  onCopyPath,
  onShowToast,
  onRefreshState,
}) => {
  const [width, setWidth] = useState<number>(348);
  const [isResizing, setIsResizing] = useState<boolean>(false);

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
    const prevSelect = document.body.style.userSelect;
    const prevCursor = document.body.style.cursor;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    const onMove = (e: MouseEvent) => {
      const nextWidth = Math.max(320, Math.min(540, window.innerWidth - e.clientX));
      setWidth(nextWidth);
    };
    const onUp = () => setIsResizing(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      document.body.style.userSelect = prevSelect;
      document.body.style.cursor = prevCursor;
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
    <>
      {visible && onHideInspector && (
        <div
          className="inspector-backdrop"
          onClick={onHideInspector}
          aria-hidden="true"
        />
      )}
      <aside
        className={`right-inspector ${!visible ? "collapsed" : ""} ${isResizing ? "resizing" : ""}`}
        style={{ width: visible ? `${width}px` : "0px" }}
        id="rightInspector"
        aria-label="Selected file inspector"
        aria-hidden={!visible}
        inert={!visible ? true : undefined}
      >
        <div
          className={`inspector-resizer ${isResizing ? "dragging" : ""}`}
          onMouseDown={() => setIsResizing(true)}
          title="Drag to resize inspector"
        />

        <div className="right-inspector-inner" style={{ width: `${width}px` }}>
          <div
            className="right-inspector-header"
            data-tauri-drag-region
            style={{
              height: "58px",
              padding: "0 14px 0 18px",
              borderBottom: "1px solid var(--rule)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              flexShrink: 0,
              gap: "8px",
            }}
          >
            <span style={{ fontSize: "12px", fontWeight: 500, color: "var(--ink-3)" }}>
              File info
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
              {file && (
                <button
                  type="button"
                  className="shelf-header-subtle-btn"
                  onClick={onOpenQuickLook}
                  title="Open full preview (Double-click or Space)"
                >
                  Preview
                </button>
              )}
              {onHideInspector && (
                <button
                  type="button"
                  className="shelf-header-subtle-btn"
                  onClick={onHideInspector}
                  title="Hide panel (I)"
                  aria-label="Hide file info"
                >
                  Hide
                </button>
              )}
            </div>
          </div>

          {visible && file ? (
            <div
              id="inspectorContent"
              style={{
                flex: 1,
                overflowY: "auto",
                padding: "20px 20px 28px",
                display: "flex",
                flexDirection: "column",
                gap: "16px",
              }}
            >
              {/* File Identity Header */}
              <div style={{ minWidth: 0 }}>
                <h3
                  className="font-display"
                  style={{
                    fontSize: "20px",
                    fontWeight: 500,
                    lineHeight: 1.25,
                    letterSpacing: "-0.02em",
                    overflowWrap: "anywhere",
                  }}
                >
                  {file.title}
                </h3>
                <div
                  style={{
                    fontSize: "12.5px",
                    color: "var(--ink-3)",
                    marginTop: "5px",
                  }}
                >
                  {file.format.toLowerCase()} · {file.size} · {file.date}
                </div>
              </div>

              {/* Clean Actions */}
              <FileQuickActions
                file={file}
                onOpenOS={handleOpenOS}
                onRevealOS={handleRevealOS}
                onFindSimilar={onFindSimilar}
                onOpenRenameDialog={onOpenRenameDialog}
                onOpenMoveDialog={onOpenMoveDialog}
                onOpenDeleteDialog={onOpenDeleteDialog}
                onRefreshState={onRefreshState}
                onShowToast={onShowToast}
              />

              {/* Suggested Clean Filename (only when available) */}
              {file.suggestedName && !file.renamed && (
                <div
                  style={{
                    paddingTop: "12px",
                    borderTop: "1px solid var(--rule)",
                    display: "flex",
                    flexDirection: "column",
                    gap: "8px",
                  }}
                >
                  <div style={{ fontSize: "11px", fontWeight: 600, letterSpacing: "0.05em", color: "var(--ink-3)", textTransform: "uppercase" }}>
                    Suggested rename
                  </div>
                  <div style={{ fontSize: "13px", color: "var(--ink)", wordBreak: "break-all" }}>
                    {file.suggestedName}
                  </div>
                  <button
                    type="button"
                    className="btn-tactile"
                    style={{ alignSelf: "flex-start" }}
                    onClick={() => onToggleRename(file.id)}
                  >
                    Apply rename
                  </button>
                </div>
              )}

              {/* Essential Flat Metadata Rows */}
              <div className="inspector-meta-list">
                <div className="inspector-meta-row">
                  <span className="inspector-meta-label">Location</span>
                  <button
                    type="button"
                    onClick={() => onCopyPath(file)}
                    title={`Copy full path: ${file.path}`}
                    style={{
                      textAlign: "right",
                      color: "var(--ink)",
                      fontSize: "12.5px",
                      overflowWrap: "anywhere",
                      wordBreak: "break-word",
                    }}
                  >
                    {file.folderFull || file.folder || file.path}
                  </button>
                </div>

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
                        minHeight: "26px",
                        maxWidth: "185px",
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
                  <span className="inspector-meta-label">Modified</span>
                  <span style={{ fontSize: "12.5px", color: "var(--ink)" }}>{file.date}</span>
                </div>

                <div className="inspector-meta-row">
                  <span className="inspector-meta-label">Size</span>
                  <span className="tabular-nums" style={{ fontSize: "12.5px", color: "var(--ink)" }}>{file.size}</span>
                </div>

                {(file.width > 0 || file.pageCount > 1) && (
                  <div className="inspector-meta-row">
                    <span className="inspector-meta-label">{file.width > 0 ? "Dimensions" : "Pages"}</span>
                    <span className="tabular-nums" style={{ fontSize: "12.5px", color: "var(--ink)" }}>
                      {file.width > 0 ? `${file.width} × ${file.height}` : `${file.pageCount} pages`}
                    </span>
                  </div>
                )}
              </div>

              {/* Minimal Tags */}
              <TagsEditor file={file} onRefreshState={onRefreshState} onShowToast={onShowToast} />

              <p style={{ fontSize: "12px", color: "var(--ink-3)", paddingTop: "6px" }}>
                Double-click any file or press Space for a full preview.
              </p>
            </div>
          ) : !visible ? null : (
            <div style={{ flex: 1, padding: "28px 20px", color: "var(--ink-3)", fontSize: "13.5px", textAlign: "center" }}>
              Select a file to view details and quick actions.
            </div>
          )}
        </div>
      </aside>

      {/* EXPANDED QUICK LOOK MODAL */}
      <div
        className={`modal-wrap ${quickLookOpen ? "open" : ""}`}
        id="quickLookModal"
        role="dialog"
        aria-modal="true"
        aria-label={file ? `Quick Look: ${file.title}` : "Quick Look"}
        aria-hidden={!quickLookOpen}
        inert={!quickLookOpen ? true : undefined}
        onClick={(e) => {
          if (e.target === e.currentTarget) onCloseQuickLook();
        }}
      >
        {quickLookOpen && file && (
          <div className="modal-card" style={{ maxWidth: "980px" }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "flex-start",
                gap: "16px",
                paddingBottom: "14px",
                borderBottom: "1px solid var(--rule)",
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  {file.format.toLowerCase()} · {file.size} · {file.shelfName}
                </div>
                <h2
                  className="font-display"
                  style={{ fontSize: "24px", fontWeight: 500, letterSpacing: "-0.02em", marginTop: "4px", overflowWrap: "anywhere" }}
                >
                  {file.title}
                </h2>
              </div>
              <div style={{ display: "flex", gap: "8px", flexShrink: 0, flexWrap: "wrap" }}>
                {onStepQuickLook && (
                  <>
                    <button
                      type="button"
                      className="btn-tactile"
                      onClick={() => onStepQuickLook(-1)}
                      title="Previous file (←)"
                      aria-label="Previous file"
                    >
                      ←
                    </button>
                    <button
                      type="button"
                      className="btn-tactile"
                      onClick={() => onStepQuickLook(1)}
                      title="Next file (→)"
                      aria-label="Next file"
                    >
                      →
                    </button>
                  </>
                )}
                <button type="button" className="btn-tactile solid" onClick={handleOpenOS}>
                  <span>Open in app</span>
                </button>
                <button type="button" className="btn-tactile" onClick={onCloseQuickLook}>
                  Close (Esc)
                </button>
              </div>
            </div>

            <div style={{ margin: "18px 0", display: "flex", flexDirection: "column", gap: "14px" }}>
              <SpecimenPreview file={file} expanded onCopyText={handleCopyText} />

              <FileQuickActions
                file={file}
                compact
                onOpenOS={handleOpenOS}
                onRevealOS={handleRevealOS}
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

              <TagsEditor file={file} onRefreshState={onRefreshState} onShowToast={onShowToast} />
            </div>
          </div>
        )}
      </div>
    </>
  );
});
