import React, { useEffect, useMemo, useState } from "react";
import {
  RenameHistoryItem,
  ShelfFile,
  executeBatchRenameTemplate,
  previewBatchRenameTemplate,
} from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";

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
  onClearHistory?: () => Promise<void> | void;
}

const TEMPLATE_PRESETS = [
  { label: "Date + Title", tpl: "{date}_{title}" },
  { label: "Receipt Merchant + Amount", tpl: "{date}_{merchant}_{amount}" },
  { label: "Shelf + Seq + Original", tpl: "{shelf}_{seq}_{original}" },
];

export const TidyNamesView = memoView<TidyNamesViewProps>(({
  active,
  files,
  renameHistory,
  selectedFileId,
  onSelectFile,
  onToggleRename,
  onApplyAllRenames,
  onRefreshState,
  onShowToast,
  onClearHistory,
}) => {
  const [historyOpen, setHistoryOpen] = useState<boolean>(false);
  const [studioOpen, setStudioOpen] = useState<boolean>(false);
  const [templateScope, setTemplateScope] = useState<string>("candidates");
  const [template, setTemplate] = useState<string>("{date}_{title}");
  const [templatePreviews, setTemplatePreviews] = useState<Array<{ fileId: string; fromName: string; toName: string; folder: string }>>([]);
  const [applyingTemplate, setApplyingTemplate] = useState<boolean>(false);
  const [busyFileId, setBusyFileId] = useState<string | null>(null);
  const [applyingAll, setApplyingAll] = useState<boolean>(false);
  const pendingCandidates = files.filter((f) => Boolean(f.suggestedName) && !f.renamed);
  const pendingCount = pendingCandidates.length;

  const availableScopes = useMemo(() => {
    const shelfMap = new Map<string, string>();
    for (const f of files) {
      if (f.shelfId && f.shelfName && !shelfMap.has(f.shelfId)) {
        shelfMap.set(f.shelfId, f.shelfName);
      }
    }
    return Array.from(shelfMap.entries()).map(([id, name]) => ({ id, name }));
  }, [files]);

  const scopedTemplateIds = useMemo(() => {
    let pool: ShelfFile[];
    if (templateScope === "candidates") {
      pool = pendingCandidates.length > 0 ? pendingCandidates : files;
    } else if (templateScope === "all") {
      pool = files;
    } else {
      pool = files.filter((f) => f.shelfId === templateScope);
    }
    return pool.slice(0, 40).map((f) => f.id);
  }, [templateScope, pendingCandidates, files]);

  useEffect(() => {
    if (!studioOpen || scopedTemplateIds.length === 0) {
      if (scopedTemplateIds.length === 0) setTemplatePreviews([]);
      return;
    }
    previewBatchRenameTemplate(scopedTemplateIds, template)
      .then((res) => setTemplatePreviews(res.previews || []))
      .catch(() => setTemplatePreviews([]));
  }, [studioOpen, template, scopedTemplateIds]);

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
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-tidy"
      aria-hidden={!active}
      inert={!active ? true : undefined}
    >
      <div className="page-container">
        <div className="page-header">
          <div>
            <h1 className="page-title">Rename suggestions</h1>
            <p className="page-subtitle">
              Turn cryptic filenames (like IMG_2048.jpg or scan_01.pdf) into clear, searchable names.
            </p>
          </div>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            <button
              type="button"
              className={`btn-tactile ${studioOpen ? "active-tab" : ""}`}
              onClick={() => setStudioOpen((s) => !s)}
            >
              <span>{studioOpen ? "Hide template studio" : "Batch template studio"}</span>
            </button>
            <button
              type="button"
              className="btn-tactile solid"
              disabled={applyingAll || pendingCount === 0}
              onClick={handleApplyAll}
            >
              <span>{applyingAll ? "Applying…" : `Apply all (${pendingCount})`}</span>
            </button>
          </div>
        </div>

        {studioOpen && (
          <div className="shelf-section" style={{ padding: "20px 22px", marginBottom: "20px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "8px", marginBottom: "10px" }}>
              <div>
                <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                  Batch rename template studio
                </h2>
                <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  Available tokens: {"{date} {title} {original} {shelf} {merchant} {amount} {seq}"}
                </p>
              </div>
              <button
                type="button"
                className="btn-tactile solid"
                disabled={applyingTemplate || templatePreviews.length === 0}
                onClick={handleApplyTemplate}
              >
                {applyingTemplate ? "Renaming…" : `Apply to ${templatePreviews.length} file(s)`}
              </button>
            </div>
            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center", marginBottom: "10px" }}>
              <select
                aria-label="Template scope"
                className="editorial-select"
                value={templateScope}
                onChange={(e) => setTemplateScope(e.target.value)}
              >
                <option value="candidates">Rename candidates ({pendingCandidates.length})</option>
                <option value="all">All indexed files ({files.length})</option>
                {availableScopes.map((sc) => (
                  <option key={sc.id} value={sc.id}>
                    Shelf: {sc.name}
                  </option>
                ))}
              </select>
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
              aria-label="Rename template pattern"
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              className="dialog-input"
              style={{ fontSize: "13px", marginBottom: "12px" }}
            />
            {templatePreviews.length > 0 && (
              <div style={{ border: "1px solid var(--rule)", borderRadius: "8px", maxHeight: "190px", overflowY: "auto" }}>
                {templatePreviews.map((pv) => (
                  <div
                    key={pv.fileId}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: "12px",
                      padding: "7px 12px",
                      fontSize: "12.5px",
                      borderBottom: "1px solid var(--rule-soft)",
                    }}
                  >
                    <span style={{ color: "var(--ink-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{pv.fromName}</span>
                    <span style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>→ {pv.toName}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="shelf-list-surface" id="tidyNamesList">
          {pendingCandidates.length === 0 ? (
            <div style={{ padding: "28px 20px", textAlign: "center", fontSize: "14px", color: "var(--ink-2)" }}>
              All indexed filenames are already clean and descriptive.
            </div>
          ) : (
            pendingCandidates.map((f) => {
              const isSel = f.id === selectedFileId;
              const targetDisplay = f.suggestedName || f.name;
              return (
                <div
                  key={f.id}
                  className={`file-item-row ${isSel ? "selected" : ""}`}
                  onClick={() => onSelectFile(f.id)}
                >
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: "8px" }}>
                      <span style={{ fontSize: "13.5px", color: "var(--ink-3)", wordBreak: "break-all" }}>
                        {f.originalName}
                      </span>
                      <span style={{ color: "var(--ink-2)" }}>→</span>
                      <span className="font-display" style={{ fontSize: "15px", fontWeight: 500, color: "var(--ink)", wordBreak: "break-all" }}>
                        {targetDisplay}
                      </span>
                    </div>
                    <div style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "3px" }}>
                      {f.renameReason || "Clean date-stamped filename"} · {f.format.toLowerCase()}
                    </div>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }} onClick={(e) => e.stopPropagation()}>
                    <button
                      type="button"
                      className={`btn-tactile ${f.renamed ? "" : "solid"}`}
                      disabled={busyFileId === f.id || applyingAll}
                      onClick={() => handleToggle(f.id)}
                    >
                      {busyFileId === f.id ? "Working…" : f.renamed ? "Undo" : "Apply rename"}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Persistent Rename History Log */}
        {renameHistory.length > 0 && (
          <div className="shelf-section" style={{ padding: "14px 18px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "12px" }}>
              <button
                type="button"
                onClick={() => setHistoryOpen((v) => !v)}
                style={{
                  flex: 1,
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  fontSize: "13px",
                  fontWeight: 500,
                  color: "var(--ink)",
                }}
              >
                <span>Rename history ({renameHistory.length})</span>
                <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>{historyOpen ? "Hide ↑" : "Show ↓"}</span>
              </button>
              {onClearHistory && (
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "3px 10px", minHeight: "26px", fontSize: "11.5px" }}
                  onClick={() => onClearHistory()}
                >
                  Clear history
                </button>
              )}
            </div>
            <div className={`accordion-drawer ${historyOpen ? "open" : ""}`}>
              <div className="accordion-inner">
                <div style={{ marginTop: "12px", paddingTop: "12px", borderTop: "1px solid var(--rule)", display: "flex", flexDirection: "column", gap: "8px" }}>
                  {renameHistory.map((h) => (
                    <div
                      key={h.id}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: "12px",
                        fontSize: "12px",
                        fontFamily: "var(--mono)",
                      }}
                    >
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {h.old_name} → {h.new_name}
                      </span>
                      <button
                        type="button"
                        className="btn-tactile"
                        style={{ padding: "3px 9px", minHeight: "26px", fontSize: "11.5px" }}
                        disabled={busyFileId === h.file_id || applyingAll}
                        onClick={() => handleToggle(h.file_id)}
                      >
                        Undo
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
});
