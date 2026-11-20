import React, { useEffect, useRef, useState } from "react";
import { ActionHistoryItem, PipelineStageEvent, WorkspaceInsights, fetchWorkspaceInsights } from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";
import { pathBaseName } from "../lib/platform";

interface ActivityViewProps {
  active: boolean;
  activeStage: number;
  statusText: string;
  timerBadge: string;
  stageBadges: [string, string, string, string];
  recentEvents: PipelineStageEvent[];
  actionHistory?: ActionHistoryItem[];
  isProcessing: boolean;
  onUploadRealFile: (file: File) => void;
  onSelectFile: (fileId: string) => void;
  onUndoAction?: (actionId?: number) => void;
  onRedoAction?: (actionId?: number) => void;
  onClearActivity?: () => Promise<void> | void;
}

export const ActivityView = memoView<ActivityViewProps>(({
  active,
  actionHistory = [],
  isProcessing,
  onUploadRealFile,
  onSelectFile,
  onUndoAction,
  onRedoAction,
  onClearActivity,
}) => {
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const [insights, setInsights] = useState<WorkspaceInsights | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!active) return;
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
    const droppedFiles = e.dataTransfer?.files;
    if (droppedFiles && droppedFiles.length > 0 && droppedFiles[0]) {
      onUploadRealFile(droppedFiles[0]);
    }
  };

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0];
    if (picked) {
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
            <h1 className="page-title">Activity &amp; Undo</h1>
            <p className="page-subtitle">
              Review reversible file changes, storage breakdown, and import new files.
            </p>
          </div>
          {onClearActivity && actionHistory.length > 0 && (
            <button type="button" className="btn-tactile" onClick={onClearActivity}>
              Clear history
            </button>
          )}
        </div>

        {/* 1. Action History (Undo & Redo) */}
        <div className="shelf-section" style={{ marginBottom: "20px" }}>
          <div className="shelf-section-header">
            <div>
              <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                Change history
              </h2>
              <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                Every rename, archive, and folder move can be undone or redone
              </p>
            </div>
            <span className="rail-badge">{actionHistory.length} actions</span>
          </div>

          {actionHistory.length === 0 ? (
            <div style={{ padding: "22px 20px", fontSize: "13.5px", color: "var(--ink-3)" }}>
              No file changes recorded yet. Actions like Rename, Move, Clean up, and Trash will appear here.
            </div>
          ) : (
            <div className="shelf-list-surface">
              {actionHistory.slice(0, 14).map((act) => (
                <div
                  key={act.id}
                  className="file-item-row"
                  style={{ cursor: "default", opacity: act.undone ? 0.65 : 1 }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: "13.5px", fontWeight: 500, color: "var(--ink)" }}>
                      {act.summary}
                      {act.undone && (
                        <span style={{ marginLeft: "8px", fontSize: "11px", color: "var(--ink-3)" }}>
                          (Undone)
                        </span>
                      )}
                    </div>
                    {Array.isArray(act.detail?.moves) && act.detail.moves.length > 0 && (
                      <div
                        title={`${act.detail.moves[0].fromPath} → ${act.detail.moves[0].toPath}`}
                        style={{
                          fontSize: "12px",
                          color: "var(--ink-3)",
                          marginTop: "2px",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {pathBaseName(act.detail.moves[0].fromPath)} → {pathBaseName(act.detail.moves[0].toPath)}
                        {act.detail.moves.length > 1 ? ` (+${act.detail.moves.length - 1} more)` : ""}
                      </div>
                    )}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    {act.undone
                      ? onRedoAction && (
                          <button
                            type="button"
                            className="btn-tactile solid"
                            style={{ padding: "4px 10px", minHeight: "28px", fontSize: "12px" }}
                            onClick={() => onRedoAction(act.id)}
                          >
                            <span>Redo</span>
                          </button>
                        )
                      : onUndoAction && (
                          <button
                            type="button"
                            className="btn-tactile"
                            style={{ padding: "4px 10px", minHeight: "28px", fontSize: "12px" }}
                            onClick={() => onUndoAction(act.id)}
                          >
                            <span>Undo</span>
                          </button>
                        )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 2. Workspace Storage & Insights */}
        {insights && insights.totalFiles > 0 && (
          <div className="shelf-section" style={{ marginBottom: "20px" }}>
            <div className="shelf-section-header">
              <div>
                <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                  Workspace storage insights
                </h2>
                <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  {insights.totalFiles} indexed files · {insights.totalHuman} total · {insights.reclaimableMB} MB reclaimable duplicates
                </p>
              </div>
              <span className="rail-badge">{insights.unnamedCount} unclear names</span>
            </div>
            <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: "14px" }}>
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
