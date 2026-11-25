import React, { useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { ActionHistoryItem, WorkspaceInsights, fetchWorkspaceInsights } from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";
import { pathBaseName } from "../lib/platform";

interface ActivityViewProps {
  active: boolean;
  actionHistory?: ActionHistoryItem[];
  isProcessing: boolean;
  onBackToHome?: () => void;
  onUploadRealFile: (files: File[]) => void;
  onSelectFile: (fileId: string) => void;
  onUndoAction?: (actionId?: number) => void;
  onRedoAction?: (actionId?: number) => void;
  onClearActivity?: () => Promise<void> | void;
}

export const ActivityView = memoView<ActivityViewProps>(({
  active,
  actionHistory = [],
  isProcessing,
  onBackToHome,
  onUploadRealFile,
  onSelectFile,
  onUndoAction,
  onRedoAction,
  onClearActivity,
}) => {
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const [insights, setInsights] = useState<WorkspaceInsights | null>(null);
  const [confirmClear, setConfirmClear] = useState<boolean>(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!active) {
      setConfirmClear(false);
      return;
    }
    fetchWorkspaceInsights()
      .then(setInsights)
      .catch(() => {});
  }, [active]);

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
    const droppedFiles = Array.from(e.dataTransfer?.files || []).filter(Boolean) as File[];
    if (droppedFiles.length > 0) {
      onUploadRealFile(droppedFiles);
    }
  };

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files || []).filter(Boolean) as File[];
    if (picked.length > 0) {
      onUploadRealFile(picked);
      e.target.value = "";
    }
  };

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-learns"
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
            <h1 className="page-title">Activity &amp; Undo</h1>
            <p className="page-subtitle">
              Review reversible file changes, storage breakdown, and import new files.
            </p>
          </div>
          {onClearActivity && actionHistory.length > 0 && (
            confirmClear ? (
              <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                <button
                  type="button"
                  className="btn-tactile danger"
                  onClick={() => {
                    setConfirmClear(false);
                    void onClearActivity();
                  }}
                >
                  Confirm clear
                </button>
                <button type="button" className="btn-tactile" onClick={() => setConfirmClear(false)}>
                  Cancel
                </button>
              </div>
            ) : (
              <button type="button" className="btn-tactile" onClick={() => setConfirmClear(true)}>
                Clear history
              </button>
            )
          )}
        </div>

        {/* 1. Action History (Undo & Redo) */}
        <div className="shelf-section">
          <div className="shelf-section-header">
            <div>
              <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                Change history
              </h2>
              <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                Every rename, archive, and folder move can be undone or redone
              </p>
            </div>
            <span className="shelf-ledger-count">{actionHistory.length} actions</span>
          </div>

          <div className="shelf-list-surface">
            {actionHistory.length === 0 ? (
              <div style={{ padding: "24px 20px", fontSize: "13.5px", color: "var(--ink-3)" }}>
                No file changes recorded yet. Actions like Rename, Move, Clean up, and Trash will appear here.
              </div>
            ) : (
              actionHistory.slice(0, 14).map((act) => {
                const detail = act.detail || {};
                const previewFileId: string | undefined =
                  detail.file_id ||
                  detail.fileId ||
                  (Array.isArray(detail.moves) && detail.moves[0]?.fileId) ||
                  undefined;
                const oldName = detail.old_name || detail.oldName || (detail.old_path ? pathBaseName(detail.old_path) : "");
                const newName = detail.new_name || detail.newName || (detail.new_path ? pathBaseName(detail.new_path) : "");
                const fromFolder = detail.from_folder || detail.fromFolder || "";
                const toFolder = detail.to_folder || detail.toFolder || "";
                return (
                  <div
                    key={act.id}
                    className="file-item-row"
                    style={{ cursor: "default", opacity: act.undone ? 0.65 : 1 }}
                  >
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div style={{ fontSize: "13.5px", fontWeight: 500, color: "var(--ink)" }}>
                        {act.summary}
                        {act.undone && (
                          <span style={{ marginLeft: "8px", fontSize: "11px", color: "var(--ink-3)" }}>
                            (Undone)
                          </span>
                        )}
                      </div>
                      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px", marginTop: "4px" }}>
                        {oldName && newName && (
                          <span className="font-code-raw" style={{ fontSize: "12px", color: "var(--ink-2)" }}>
                            {oldName} → <strong>{newName}</strong>
                          </span>
                        )}
                        {fromFolder && toFolder && (
                          <span className="font-code-raw" style={{ fontSize: "12px", color: "var(--ink-2)" }}>
                            {fromFolder} → <strong>{toFolder}</strong>
                          </span>
                        )}
                        {Array.isArray(detail.moves) && detail.moves.length > 0 && (
                          <span
                            title={`${detail.moves[0].fromPath} → ${detail.moves[0].toPath}`}
                            style={{
                              fontSize: "12px",
                              color: "var(--ink-3)",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {pathBaseName(detail.moves[0].fromPath)} → {pathBaseName(detail.moves[0].toPath)}
                            {detail.moves.length > 1 ? ` (+${detail.moves.length - 1} more)` : ""}
                          </span>
                        )}
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 }}>
                      {previewFileId && (
                        <button
                          type="button"
                          className="btn-ghost"
                          style={{ fontSize: "12px", padding: "3px 8px", minHeight: "26px" }}
                          onClick={() => onSelectFile(previewFileId)}
                        >
                          Preview file
                        </button>
                      )}
                      {act.undone
                        ? onRedoAction && (
                            <button
                              type="button"
                              className="btn-tactile sm solid"
                              onClick={() => onRedoAction(act.id)}
                            >
                              <span>Redo</span>
                            </button>
                          )
                        : onUndoAction && (
                            <button
                              type="button"
                              className="btn-tactile sm"
                              onClick={() => onUndoAction(act.id)}
                            >
                              <span>Undo</span>
                            </button>
                          )}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* 2. Workspace Storage & Insights */}
        {insights && insights.totalFiles > 0 && (
          <div className="shelf-section">
            <div className="shelf-section-header">
              <div>
                <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                  Workspace storage insights
                </h2>
                <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  {insights.totalFiles} indexed files · {insights.totalHuman} total · {insights.reclaimableMB} MB reclaimable duplicates
                </p>
              </div>
              <span className="shelf-ledger-count">{insights.unnamedCount} unclear names</span>
            </div>
            <div className="shelf-list-surface" style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: "14px" }}>
              <div style={{ display: "flex", flexDirection: "column", gap: "7px" }}>
                {insights.byModality.map((m) => {
                  const pct = insights.totalBytes > 0 ? Math.max(3, Math.round((m.bytes / insights.totalBytes) * 100)) : 0;
                  const mb = (m.bytes / (1024 * 1024)).toFixed(1);
                  return (
                    <div key={m.modality} style={{ display: "grid", gridTemplateColumns: "110px 1fr 78px", alignItems: "center", gap: "10px", fontSize: "12.5px" }}>
                      <span style={{ color: "var(--ink-2)", textTransform: "capitalize" }}>
                        {m.modality} ({m.count})
                      </span>
                      <div style={{ height: "7px", background: "var(--bg-subtle)", borderRadius: "4px", overflow: "hidden", border: "1px solid var(--rule-soft)" }}>
                        <div style={{ width: `${pct}%`, height: "100%", background: "var(--ink)", borderRadius: "4px" }} />
                      </div>
                      <span className="tabular-nums" style={{ textAlign: "right", color: "var(--ink-3)", fontSize: "12px" }}>{mb} MB</span>
                    </div>
                  );
                })}
              </div>
              {insights.largestFiles.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", alignItems: "center" }}>
                  <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>Largest files:</span>
                  {insights.largestFiles.slice(0, 4).map((lf) => (
                    <button
                      key={lf.id}
                      type="button"
                      className="tag-pill-suggest"
                      onClick={() => onSelectFile(lf.id)}
                    >
                      {lf.name} ({lf.size})
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* 3. Quick Drop Zone */}
        <div
          className={`live-drop-zone ${isDragging ? "dragging" : ""}`}
          style={{ marginBottom: "20px" }}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "12px" }}>
            <div>
              <div className="font-display" style={{ fontSize: "16.5px", fontWeight: 500 }}>
                Add or drop a file to index immediately
              </div>
              <div style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "2px" }}>
                Supports PDF, Word, Excel, PowerPoint, images, code, CSV, audio, video, and ZIP archives
              </div>
            </div>
            <div>
              <input ref={fileInputRef} type="file" style={{ display: "none" }} onChange={handleFileInputChange} />
              <button
                type="button"
                className="btn-tactile solid"
                onClick={() => fileInputRef.current?.click()}
                disabled={isProcessing}
              >
                <span>Choose file…</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
});
