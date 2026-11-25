import React, { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import {
  RenameHistoryItem,
  ShelfFile,
  executeBatchRenameTemplate,
  previewBatchRenameTemplate,
  renameFileCustom,
} from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";

interface TidyNamesViewProps {
  active: boolean;
  files: ShelfFile[];
  renameHistory: RenameHistoryItem[];
  selectedFileId: string;
  onBackToHome?: () => void;
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

export function buildAutoSuggestion(f: ShelfFile): string {
  if (f.suggestedName && f.suggestedName.trim()) {
    return f.suggestedName.trim();
  }
  const rawDate = (f.createdDate || f.date || "2026-01-01").trim();
  const dateMatch = rawDate.match(/\d{4}-\d{2}-\d{2}/);
  const datePart = dateMatch ? dateMatch[0] : "2026-01-01";
  const ext = (f.format || "txt").toLowerCase().replace(/^\.+/, "");

  const baseSource = (f.title || f.name || "").replace(/\.[a-z0-9]{1,6}$/i, "");
  let slug = baseSource
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48)
    .replace(/-+$/g, "");

  if (!slug || slug.length < 3) {
    const fallbackTopic = `${f.subShelf || ""} ${f.shelfName || ""} ${f.specimen || f.modality || "file"}`
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 32);
    const shortId = (f.id || "01").replace(/[^a-z0-9]/gi, "").slice(-4).toLowerCase() || "01";
    slug = `${fallbackTopic || "document"}-${shortId}`;
  }

  return `${datePart}_${slug}.${ext}`;
}

export const TidyNamesView = memoView<TidyNamesViewProps>(({
  active,
  files,
  renameHistory,
  selectedFileId,
  onBackToHome,
  onSelectFile,
  onToggleRename,
  onApplyAllRenames,
  onRefreshState,
  onShowToast,
  onClearHistory,
}) => {
  const [viewTab, setViewTab] = useState<"suggested" | "all">("suggested");
  const [filterQuery, setFilterQuery] = useState<string>("");
  const [draftNames, setDraftNames] = useState<Record<string, string>>({});
  const [historyOpen, setHistoryOpen] = useState<boolean>(true);
  const [studioOpen, setStudioOpen] = useState<boolean>(false);
  const [templateScope, setTemplateScope] = useState<string>("candidates");
  const [template, setTemplate] = useState<string>("{date}_{title}");
  const [templatePreviews, setTemplatePreviews] = useState<Array<{ fileId: string; fromName: string; toName: string; folder: string }>>([]);
  const [applyingTemplate, setApplyingTemplate] = useState<boolean>(false);
  const [busyFileId, setBusyFileId] = useState<string | null>(null);
  const [applyingAll, setApplyingAll] = useState<boolean>(false);
  const [confirmClearHistory, setConfirmClearHistory] = useState<boolean>(false);

  const pendingCandidates = useMemo(
    () => files.filter((f) => Boolean(f.suggestedName) && !f.renamed),
    [files]
  );
  const pendingCount = pendingCandidates.length;

  const displayedFiles = useMemo(() => {
    const base = viewTab === "suggested" ? pendingCandidates : files;
    const q = filterQuery.trim().toLowerCase();
    if (!q) return base;
    return base.filter((f) =>
      `${f.name} ${f.originalName} ${f.title} ${f.folder} ${f.shelfName || ""} ${f.suggestedName || ""}`
        .toLowerCase()
        .includes(q)
    );
  }, [viewTab, pendingCandidates, files, filterQuery]);

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
      pool = pendingCandidates;
    } else if (templateScope === "all") {
      pool = files;
    } else {
      pool = files.filter((f) => f.shelfId === templateScope);
    }
    return pool.slice(0, 100).map((f) => f.id);
  }, [templateScope, pendingCandidates, files]);

  const previewTimerRef = useRef<number | null>(null);
  useEffect(() => {
    if (previewTimerRef.current) window.clearTimeout(previewTimerRef.current);
    if (!studioOpen || scopedTemplateIds.length === 0) {
      if (scopedTemplateIds.length === 0) {
        setTemplatePreviews((prev) => (prev.length === 0 ? prev : []));
      }
      return;
    }
    let cancelled = false;
    previewTimerRef.current = window.setTimeout(() => {
      previewBatchRenameTemplate(scopedTemplateIds, template)
        .then((res) => {
          if (!cancelled) setTemplatePreviews(res.previews || []);
        })
        .catch(() => {
          if (!cancelled) setTemplatePreviews((prev) => (prev.length === 0 ? prev : []));
        });
    }, 220);
    return () => {
      cancelled = true;
      if (previewTimerRef.current) window.clearTimeout(previewTimerRef.current);
    };
  }, [studioOpen, template, scopedTemplateIds]);

  const handleApplyTemplate = async () => {
    if (applyingTemplate || templatePreviews.length === 0) return;
    setApplyingTemplate(true);
    try {
      const ids = templatePreviews.map((p) => p.fileId);
      const res = await executeBatchRenameTemplate(ids, template);
      onRefreshState?.(res.state);
      onShowToast?.(`Applied template rename to ${res.renamedCount} ${res.renamedCount === 1 ? "file" : "files"}`);
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

  const handleApplySingleRename = async (f: ShelfFile, targetName: string) => {
    const cleanTarget = targetName.trim();
    if (!cleanTarget || busyFileId || applyingAll) return;
    const autoSuggested = buildAutoSuggestion(f);
    const wasEdited = cleanTarget !== autoSuggested;
    if (!wasEdited && f.suggestedName && !f.renamed) {
      await handleToggle(f.id);
      return;
    }
    setBusyFileId(f.id);
    try {
      const res = await renameFileCustom(f.id, cleanTarget);
      onRefreshState?.(res.state);
      setDraftNames((prev) => {
        const next = { ...prev };
        delete next[f.id];
        return next;
      });
      onShowToast?.(`Renamed ${f.name} → ${res.file.name}`);
    } catch (err) {
      onShowToast?.(err instanceof Error ? err.message : "Could not rename file");
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
            {onBackToHome && (
              <button
                type="button"
                className="page-back-btn"
                onClick={onBackToHome}
                title="Back to Home (Esc)"
                aria-label="Back to Home"
              >
                <ArrowLeft size={13} strokeWidth={1.75} aria-hidden="true" />
                <span>Home</span>
              </button>
            )}
            <h1 className="page-title">Rename &amp; Auto-Rename</h1>
            <p className="page-subtitle">
              Turn unclear or foreign filenames into descriptive, searchable names. Edit any target name inline before applying.
            </p>
          </div>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            <button
              type="button"
              className={`btn-tactile ${studioOpen ? "active-tab" : ""}`}
              onClick={() => setStudioOpen((s) => !s)}
            >
              <span>{studioOpen ? "Hide pattern" : "Custom pattern"}</span>
            </button>
            <button
              type="button"
              className="btn-tactile solid"
              disabled={applyingAll || pendingCount === 0}
              onClick={handleApplyAll}
            >
              <span>{applyingAll ? "Applying…" : `Apply all suggested (${pendingCount})`}</span>
            </button>
          </div>
        </div>

        {/* Mode Switcher & Filter Bar */}
        <div
          className="folder-filter-strip"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: "10px",
            marginBottom: "16px",
          }}
        >
          <div className="seg-group" role="tablist" aria-label="Rename view mode">
            <button
              type="button"
              role="tab"
              aria-selected={viewTab === "suggested"}
              className={`seg-btn ${viewTab === "suggested" ? "active" : ""}`}
              onClick={() => setViewTab("suggested")}
            >
              Suggested renames ({pendingCount})
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={viewTab === "all"}
              className={`seg-btn ${viewTab === "all" ? "active" : ""}`}
              onClick={() => setViewTab("all")}
            >
              All files ({files.length})
            </button>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px", flex: "1 1 240px", justifyContent: "flex-end" }}>
            <input
              type="text"
              aria-label="Filter files to rename"
              value={filterQuery}
              onChange={(e) => setFilterQuery(e.target.value)}
              placeholder="Filter files by name, folder, or title…"
              className="dialog-input"
              style={{ maxWidth: "300px", width: "100%", padding: "5px 10px", fontSize: "12.5px", minHeight: "30px" }}
            />
            {filterQuery && (
              <button
                type="button"
                className="btn-ghost"
                style={{ fontSize: "12px", padding: "3px 8px" }}
                onClick={() => setFilterQuery("")}
              >
                Clear
              </button>
            )}
          </div>
        </div>

        {studioOpen && (
          <div className="surface-card" style={{ marginBottom: "20px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "8px", marginBottom: "10px" }}>
              <div>
                <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                  Custom rename pattern
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
                {applyingTemplate
                  ? "Renaming…"
                  : `Apply to ${templatePreviews.length} ${templatePreviews.length === 1 ? "file" : "files"}`}
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
              <div style={{ border: "1px solid var(--rule)", borderRadius: "var(--radius-sm)", maxHeight: "190px", overflowY: "auto" }}>
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
                    <span style={{ color: "var(--ink-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {pv.folder ? `${pv.folder}/` : ""}{pv.fromName}
                    </span>
                    <span style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      → {pv.folder ? `${pv.folder}/` : ""}{pv.toName}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="shelf-list-surface" id="tidyNamesList">
          {displayedFiles.length === 0 ? (
            <div style={{ padding: "28px 20px", textAlign: "center", fontSize: "14px", color: "var(--ink-2)" }}>
              {viewTab === "suggested" && files.length > 0 ? (
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "10px" }}>
                  <span>All automatic rename suggestions have been applied.</span>
                  <button
                    type="button"
                    className="btn-tactile sm"
                    onClick={() => setViewTab("all")}
                  >
                    Browse &amp; rename any of your {files.length} files
                  </button>
                </div>
              ) : (
                "No files match the current filter."
              )}
            </div>
          ) : (
            displayedFiles.map((f) => {
              const isSel = f.id === selectedFileId;
              const autoSuggestion = buildAutoSuggestion(f);
              const currentDraft = draftNames[f.id] ?? autoSuggestion;
              const isEdited = currentDraft.trim() !== autoSuggestion;
              const isUnchangedFromDisk = currentDraft.trim() === f.name;
              return (
                <div
                  key={f.id}
                  className={`file-item-row ${isSel ? "selected" : ""}`}
                  style={{ alignItems: "flex-start", gap: "14px", padding: "12px 16px" }}
                  onClick={() => onSelectFile(f.id)}
                >
                  <div style={{ minWidth: 0, flex: 1, display: "flex", flexDirection: "column", gap: "6px" }}>
                    {/* Top line: Current File & Context */}
                    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px" }}>
                      <span style={{ fontSize: "13.5px", fontWeight: 600, color: "var(--ink)", wordBreak: "break-all" }}>
                        {f.name}
                      </span>
                      {f.folder && (
                        <span className="file-type-tag" style={{ textTransform: "none" }}>
                          {f.folder}
                        </span>
                      )}
                      {f.title && f.title !== f.name && (
                        <span style={{ fontSize: "12px", color: "var(--ink-2)" }}>
                          Title: “{f.title}”
                        </span>
                      )}
                    </div>

                    {/* Middle line: Editable Target Filename & Auto-Suggest */}
                    <div
                      style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}
                      onClick={(e) => e.stopPropagation()}
                    >
                      <span style={{ color: "var(--ink-2)", fontWeight: 600 }}>→</span>
                      <input
                        type="text"
                        aria-label={`Target filename for ${f.name}`}
                        className="dialog-input tidy-inline-input font-display"
                        value={currentDraft}
                        onChange={(e) =>
                          setDraftNames((prev) => ({
                            ...prev,
                            [f.id]: e.target.value,
                          }))
                        }
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void handleApplySingleRename(f, currentDraft);
                          } else if (e.key === "Escape" && isEdited) {
                            e.preventDefault();
                            setDraftNames((prev) => {
                              const next = { ...prev };
                              delete next[f.id];
                              return next;
                            });
                          }
                        }}
                        style={{
                          flex: "1 1 260px",
                          maxWidth: "460px",
                          padding: "4px 9px",
                          fontSize: "14px",
                          fontWeight: 500,
                          minHeight: "30px",
                          color: "var(--ink)",
                        }}
                      />
                      {isEdited && (
                        <button
                          type="button"
                          className="btn-ghost"
                          style={{ fontSize: "11.5px", padding: "2px 8px", minHeight: "26px" }}
                          onClick={() =>
                            setDraftNames((prev) => {
                              const next = { ...prev };
                              delete next[f.id];
                              return next;
                            })
                          }
                        >
                          Reset to auto
                        </button>
                      )}
                    </div>

                    {/* Bottom line: Full Transparency on What Will Change */}
                    <div style={{ fontSize: "12px", color: "var(--ink-3)", lineHeight: 1.45 }}>
                      Will rename on disk:{" "}
                      <span className="font-code-raw" style={{ color: "var(--ink-2)" }}>
                        {f.folder ? `${f.folder}/` : ""}{f.name}
                      </span>{" "}
                      →{" "}
                      <span className="font-code-raw" style={{ color: "var(--ink)", fontWeight: 500 }}>
                        {f.folder ? `${f.folder}/` : ""}{currentDraft}
                      </span>{" "}
                      · {f.renameReason || "Derived from content title & date"} (Reversible via Undo)
                    </div>
                  </div>

                  {/* Right side: Eye-Level Actions */}
                  <div
                    className="row-actions-inline"
                    style={{ display: "flex", alignItems: "center", gap: "6px", flexShrink: 0, paddingTop: "2px" }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      className="btn-tactile sm"
                      onClick={() => onSelectFile(f.id)}
                    >
                      Preview
                    </button>
                    <button
                      type="button"
                      className="btn-tactile sm solid"
                      disabled={busyFileId === f.id || applyingAll || !currentDraft.trim() || isUnchangedFromDisk}
                      onClick={() => handleApplySingleRename(f, currentDraft)}
                    >
                      {busyFileId === f.id ? "Working…" : "Apply rename"}
                    </button>
                    {f.renamed && !isEdited && (
                      <button
                        type="button"
                        className="btn-tactile sm"
                        disabled={busyFileId === f.id || applyingAll}
                        onClick={() => handleToggle(f.id)}
                      >
                        Undo
                      </button>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Persistent Rename History Log */}
        {renameHistory.length > 0 && (
          <div className="surface-card" style={{ marginTop: "24px" }}>
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
                <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>{historyOpen ? "Hide" : "Show"}</span>
              </button>
              {onClearHistory && (
                confirmClearHistory ? (
                  <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                    <button
                      type="button"
                      className="btn-tactile sm danger"
                      onClick={() => {
                        setConfirmClearHistory(false);
                        void onClearHistory();
                      }}
                    >
                      Confirm clear
                    </button>
                    <button
                      type="button"
                      className="btn-tactile sm"
                      onClick={() => setConfirmClearHistory(false)}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    className="btn-tactile sm"
                    onClick={() => setConfirmClearHistory(true)}
                  >
                    Clear history
                  </button>
                )
              )}
            </div>
            <div className={`accordion-drawer ${historyOpen ? "open" : ""}`}>
              <div className="accordion-inner">
                <div style={{ marginTop: "12px", paddingTop: "12px", borderTop: "1px solid var(--rule)", display: "flex", flexDirection: "column", gap: "8px" }}>
                  {renameHistory.map((h) => (
                    <div
                      key={h.id}
                      className="tabular-nums"
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: "12px",
                        fontSize: "12.5px",
                        fontFamily: "var(--body)",
                        opacity: h.undone === 1 ? 0.6 : 1,
                      }}
                    >
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {h.old_name} → {h.new_name}
                      </span>
                      <div style={{ display: "flex", alignItems: "center", gap: "6px", flexShrink: 0 }}>
                        <button
                          type="button"
                          className="btn-ghost"
                          style={{ fontSize: "11.5px", padding: "2px 7px", minHeight: "24px" }}
                          onClick={() => onSelectFile(h.file_id)}
                        >
                          Preview
                        </button>
                        {h.undone === 1 ? (
                          <span style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>
                            Undone
                          </span>
                        ) : (
                          <button
                            type="button"
                            className="btn-tactile sm"
                            disabled={busyFileId === h.file_id || applyingAll}
                            onClick={() => handleToggle(h.file_id)}
                          >
                            Undo
                          </button>
                        )}
                      </div>
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
