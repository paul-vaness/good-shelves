import React, { useEffect, useState } from "react";
import {
  RenameHistoryItem,
  ShelfFile,
  executeBatchRenameTemplate,
  previewBatchRenameTemplate,
} from "../lib/tauri-api";

interface TidyNamesViewProps {
  active: boolean;
  files: ShelfFile[];
  renameHistory: RenameHistoryItem[];
  selectedFileId: string;
  onSelectFile: (fileId: string) => void;
  onToggleRename: (fileId: string) => Promise<void> | void;
  onApplyAllRenames: () => Promise<void> | void;
  onRefreshState?: (state: any) => void;
  onShowToast?: (msg: string) => void;
}

const TEMPLATE_PRESETS = [
  { label: "date + title", tpl: "{date}_{title}" },
  { label: "receipt merchant + amount", tpl: "{date}_{merchant}_{amount}" },
  { label: "shelf + seq + original", tpl: "{shelf}_{seq}_{original}" },
];

export const TidyNamesView: React.FC<TidyNamesViewProps> = ({
  active,
  files,
  renameHistory,
  selectedFileId,
  onSelectFile,
  onToggleRename,
  onApplyAllRenames,
  onRefreshState,
  onShowToast,
}) => {
  const [historyOpen, setHistoryOpen] = useState<boolean>(false);
  const [studioOpen, setStudioOpen] = useState<boolean>(false);
  const [template, setTemplate] = useState<string>("{date}_{title}");
  const [templatePreviews, setTemplatePreviews] = useState<Array<{ fileId: string; fromName: string; toName: string; folder: string }>>([]);
  const [applyingTemplate, setApplyingTemplate] = useState<boolean>(false);
  const [busyFileId, setBusyFileId] = useState<string | null>(null);
  const [applyingAll, setApplyingAll] = useState<boolean>(false);
  const renameCandidates = files.filter(
    (f) => Boolean(f.suggestedName) || f.renamed
  );
  const pendingCount = renameCandidates.filter((f) => Boolean(f.suggestedName) && !f.renamed).length;

  useEffect(() => {
    if (!studioOpen || files.length === 0) return;
    const ids = (renameCandidates.length > 0 ? renameCandidates : files).slice(0, 12).map((f) => f.id);
    previewBatchRenameTemplate(ids, template)
      .then((res) => setTemplatePreviews(res.previews || []))
      .catch(() => setTemplatePreviews([]));
  }, [studioOpen, template, files, renameCandidates.length]);

  const handleApplyTemplate = async () => {
    if (applyingTemplate || templatePreviews.length === 0) return;
    setApplyingTemplate(true);
    try {
      const ids = templatePreviews.map((p) => p.fileId);
      const res = await executeBatchRenameTemplate(ids, template);
      onRefreshState?.(res.state);
      onShowToast?.(`Applied template rename to ${res.renamedCount} file(s)`);
    } catch (err) {
      onShowToast?.(err instanceof Error ? err.message : "Template rename failed");
    } finally {
      setApplyingTemplate(false);
    }
  };

  const handleToggle = async (fileId: string) => {
    if (busyFileId || applyingAll) return;
    setBusyFileId(fileId);
    try {
      await onToggleRename(fileId);
    } finally {
      setBusyFileId(null);
    }
  };

  const handleApplyAll = async () => {
    if (applyingAll || busyFileId || pendingCount === 0) return;
    setApplyingAll(true);
    try {
      await onApplyAllRenames();
    } finally {
      setApplyingAll(false);
    }
  };

  return (
    <section className={`view-sheet ${active ? "active" : ""}`} id="sheet-tidy">
      <div style={{ maxWidth: "820px", margin: "0 auto" }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "flex-end",
            justifyContent: "space-between",
            gap: "16px",
            paddingBottom: "20px",
            borderBottom: "1.5px solid var(--ink)",
            marginBottom: "24px",
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
              filename cleanup
            </div>
            <h1
              className="font-display"
              style={{
                fontSize: "clamp(32px, 4.2vw, 48px)",
                fontWeight: 300,
                letterSpacing: "-0.03em",
                lineHeight: 1.02,
              }}
            >
              rename files
            </h1>
            <p
              style={{
                fontSize: "15px",
                color: "var(--ink-2)",
                marginTop: "8px",
                maxWidth: "56ch",
              }}
            >
              replaces generic camera, screenshot, and{" "}
              <span className="font-mono-code" style={{ fontSize: "13px" }}>
                final_final_v2
              </span>{" "}
              names with date-stamped titles extracted from each file.
            </p>
          </div>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            <button
              type="button"
              className="btn-tactile"
              onClick={() => setStudioOpen((s) => !s)}
            >
              {studioOpen ? "hide template studio" : "template studio…"}
            </button>
            <button
              type="button"
              className="btn-tactile solid"
              disabled={applyingAll || pendingCount === 0}
              onClick={handleApplyAll}
            >
              <span>{applyingAll ? "applying…" : "apply all suggestions"}</span>
            </button>
          </div>
        </div>

        {studioOpen && (
          <div className="shelf-section" style={{ padding: "18px 20px", marginBottom: "22px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "8px", marginBottom: "10px" }}>
              <div>
                <h2 className="font-display" style={{ fontSize: "20px", fontWeight: 400 }}>batch rename template studio</h2>
                <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  tokens: <span className="font-mono-code">{"{date} {title} {original} {shelf} {merchant} {amount} {seq}"}</span>
                </p>
              </div>
              <button
                type="button"
                className="btn-tactile solid"
                disabled={applyingTemplate || templatePreviews.length === 0}
                onClick={handleApplyTemplate}
              >
                {applyingTemplate ? "renaming…" : `apply to ${templatePreviews.length} file(s)`}
              </button>
            </div>
            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "10px" }}>
              {TEMPLATE_PRESETS.map((p) => (
                <button
                  key={p.tpl}
                  type="button"
                  className={`tag-pill-suggest ${template === p.tpl ? "solid" : ""}`}
                  onClick={() => setTemplate(p.tpl)}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <input
              type="text"
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              className="dialog-input font-mono-code"
              style={{ fontSize: "13px", marginBottom: "12px" }}
            />
            {templatePreviews.length > 0 && (
              <div style={{ border: "1px solid var(--rule)", borderRadius: "6px", maxHeight: "180px", overflowY: "auto" }}>
                {templatePreviews.map((pv) => (
                  <div
                    key={pv.fileId}
                    className="font-mono-code"
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: "12px",
                      padding: "6px 10px",
                      fontSize: "11.5px",
                      borderBottom: "1px solid var(--rule)",
                    }}
                  >
                    <span style={{ color: "var(--ink-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{pv.fromName}</span>
                    <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>→ {pv.toName}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="shelf-section" id="tidyNamesList">
          {renameCandidates.length === 0 ? (
            <div
              style={{
                padding: "24px 20px",
                fontSize: "14px",
                color: "var(--ink-2)",
              }}
            >
              All indexed filenames are clean and descriptive.
            </div>
          ) : (
            renameCandidates.map((f) => {
              const isSel = f.id === selectedFileId;
              const targetDisplay = f.suggestedName || f.name;
              return (
                <div
                  key={f.id}
                  className={`file-item-row ${isSel ? "selected" : ""}`}
                  onClick={() => onSelectFile(f.id)}
                >
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        display: "flex",
                        flexWrap: "wrap",
                        alignItems: "baseline",
                        gap: "8px",
                      }}
                    >
                      <span
                        className="font-mono-code"
                        style={{
                          fontSize: "13px",
                          color: "var(--ink-3)",
                          textDecoration: f.renamed ? "line-through" : "none",
                          wordBreak: "break-all",
                        }}
                      >
                        {f.originalName}
                      </span>
                      <span style={{ color: "var(--ink-3)" }}>→</span>
                      <span
                        className="font-mono-code"
                        style={{
                          fontSize: "13.5px",
                          fontWeight: 600,
                          color: "var(--ink)",
                          wordBreak: "break-all",
                        }}
                      >
                        {targetDisplay}
                      </span>
                    </div>
                    <div
                      style={{
                        fontSize: "13px",
                        color: "var(--ink-2)",
                        marginTop: "4px",
                      }}
                    >
                      {f.renameReason || "Applied clean date-stamped filename"}{" "}
                      ({f.folder})
                    </div>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "8px",
                    }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      className={`btn-tactile ${f.renamed ? "" : "solid"}`}
                      disabled={busyFileId === f.id || applyingAll}
                      onClick={() => handleToggle(f.id)}
                    >
                      {busyFileId === f.id ? "working…" : f.renamed ? "undo" : "apply rename"}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Persistent SQLite Rename History Log */}
        {renameHistory.length > 0 && (
          <div
            style={{
              marginTop: "20px",
              padding: "16px 20px",
              border: "1px solid var(--rule)",
              borderRadius: "8px",
              background: "var(--bg-subtle)",
            }}
          >
            <button
              type="button"
              onClick={() => setHistoryOpen((v) => !v)}
              style={{
                width: "100%",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                fontSize: "12.5px",
                fontWeight: 500,
                color: "var(--ink-2)",
              }}
            >
              <span>
                rename history ({renameHistory.length})
              </span>
              <span>{historyOpen ? "hide ↑" : "show history ↓"}</span>
            </button>
            <div className={`accordion-drawer ${historyOpen ? "open" : ""}`}>
              <div className="accordion-inner">
                <div
                  style={{
                    marginTop: "12px",
                    paddingTop: "12px",
                    borderTop: "1px solid var(--rule)",
                    display: "flex",
                    flexDirection: "column",
                    gap: "8px",
                  }}
                >
                  {renameHistory.map((h) => (
                    <div
                      key={h.id}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        fontSize: "12px",
                        fontFamily: "var(--mono)",
                      }}
                    >
                      <span>
                        {h.old_name} → {h.new_name}
                      </span>
                      <button
                        type="button"
                        className="btn-tactile"
                        style={{ padding: "3px 8px", fontSize: "11px" }}
                        disabled={busyFileId === h.file_id || applyingAll}
                        onClick={() => handleToggle(h.file_id)}
                      >
                        undo
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </section>
  );
};
