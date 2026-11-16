import React, { useRef, useState } from "react";
import { ActionHistoryItem, PipelineStageEvent } from "../lib/tauri-api";

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
}

export const ActivityView: React.FC<ActivityViewProps> = ({
  active,
  activeStage,
  statusText,
  timerBadge,
  stageBadges,
  recentEvents,
  actionHistory = [],
  isProcessing,
  onUploadRealFile,
  onSelectFile,
  onUndoAction,
  onRedoAction,
}) => {
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

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

  const steps = [
    {
      num: 1,
      tag: "STEP 01 · WATCH",
      title: "detect file change",
      desc: "monitors watched folders via OS notify events and SHA-256 content hashing.",
      badge: stageBadges[0],
    },
    {
      num: 2,
      tag: "STEP 02 · PARSE & OCR",
      title: "extract text & visual features",
      desc: "extracts PDF/DOCX text, code symbols, and runs local OCR on images.",
      badge: stageBadges[1],
    },
    {
      num: 3,
      tag: "STEP 03 · EMBED",
      title: "generate 512-d vector",
      desc: "encodes text and visual features into a 512-dimensional L2-normalized vector via ONNX Runtime.",
      badge: stageBadges[2],
    },
    {
      num: 4,
      tag: "STEP 04 · INDEX",
      title: "cluster & index",
      desc: "stores the vector in SQLite-vec, updates UMAP+HDBSCAN shelves, and checks filename suggestions.",
      badge: stageBadges[3],
    },
  ];

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-learns"
    >
      <div style={{ maxWidth: "820px", margin: "0 auto" }}>
        <div
          style={{
            paddingBottom: "20px",
            borderBottom: "1.5px solid var(--ink)",
            marginBottom: "24px",
          }}
        >
          <div
            style={{
              fontSize: "11.5px",
              fontWeight: 500,
              letterSpacing: "0.06em",
              color: "var(--ink-3)",
            }}
          >
            indexing pipeline
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
            activity
          </h1>
          <p
            style={{
              fontSize: "15px",
              color: "var(--ink-2)",
              marginTop: "8px",
              maxWidth: "58ch",
            }}
          >
            drop a file below or save one into a watched folder to run the local
            indexing pipeline.
          </p>

          {/* Real File Drop Zone + Instant Real Disk File Generators */}
          <div
            className={`live-drop-zone ${isDragging ? "dragging" : ""}`}
            style={{ marginTop: "18px" }}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "12px",
                marginBottom: "14px",
              }}
            >
              <div>
                <div
                  className="font-display"
                  style={{ fontSize: "17px", fontWeight: 400 }}
                >
                  drop any file here to ingest into ~/Downloads &amp; index live
                </div>
                <div style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  supports .png, .jpg, .webp, .pdf, .docx, .csv, .txt, .md, .py,
                  .ts, .rs
                </div>
              </div>
              <div>
                <input
                  ref={fileInputRef}
                  type="file"
                  style={{ display: "none" }}
                  onChange={handleFileInputChange}
                />
                <button
                  type="button"
                  className="btn-tactile"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isProcessing}
                >
                  choose local file…
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Live Pipeline Status Box */}
        <div
          id="pipelineLiveBanner"
          style={{
            padding: "16px 20px",
            border: "1px solid var(--ink)",
            borderRadius: "7px",
            background: "var(--bg-subtle)",
            marginBottom: "24px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: "12px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span
              className={`status-dot ${isProcessing ? "pulse" : ""}`}
            ></span>
            <span
              id="pipelineStatusText"
              style={{ fontSize: "13.5px", fontWeight: 500 }}
            >
              {statusText}
            </span>
          </div>
          <span
            id="pipelineTimerBadge"
            className="font-mono-code"
            style={{ fontSize: "11.5px", color: "var(--ink-3)" }}
          >
            {timerBadge}
          </span>
        </div>

        {/* 4 Pipeline Steps — Responsive Telemetry Grid */}
        <div className="pipeline-stepper-grid">
          {steps.map((st) => {
            const isActiveStep = activeStage === st.num;
            const isCompleted = activeStage > st.num || (!isProcessing && st.badge === "done");
            return (
              <div
                key={st.num}
                title={st.desc}
                className={`pipeline-step-card ${isActiveStep ? "active" : isCompleted ? "completed" : ""}`}
              >
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" }}>
                  <span className="font-mono-code" style={{ fontSize: "10.5px", color: "var(--ink-3)" }}>
                    0{st.num}
                  </span>
                  <span
                    className="font-mono-code"
                    style={{
                      fontSize: "10px",
                      color: isActiveStep || isCompleted ? "var(--ink)" : "var(--ink-3)",
                      fontWeight: isActiveStep ? 600 : 400,
                    }}
                  >
                    {isCompleted ? "done" : st.badge}
                  </span>
                </div>
                <div
                  style={{
                    fontSize: "13px",
                    fontWeight: isActiveStep ? 600 : 500,
                    color: "var(--ink)",
                    marginTop: "4px",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {st.title}
                </div>
              </div>
            );
          })}
        </div>

        {/* Action History (Undo & Redo) */}
        {actionHistory.length > 0 && (
          <div className="shelf-section" style={{ marginBottom: "24px" }}>
            <div className="shelf-section-header">
              <div>
                <h3
                  className="font-display"
                  style={{ fontSize: "18px", fontWeight: 400 }}
                >
                  change history (undo &amp; redo)
                </h3>
                <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  every rename, archive, and folder move is reversible
                </p>
              </div>
              <span
                className="font-mono-code"
                style={{ fontSize: "11.5px", color: "var(--ink-3)" }}
              >
                {actionHistory.length} actions
              </span>
            </div>
            <div>
              {actionHistory.slice(0, 12).map((act) => (
                <div
                  key={act.id}
                  className="file-item-row"
                  style={{
                    padding: "12px 20px",
                    cursor: "default",
                    opacity: act.undone ? 0.65 : 1,
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: "13.5px", fontWeight: 500, color: "var(--ink)" }}>
                      {act.summary}
                      {act.undone && (
                        <span
                          style={{
                            marginLeft: "8px",
                            fontSize: "11px",
                            color: "var(--ink-3)",
                            fontFamily: "var(--mono)",
                          }}
                        >
                          (undone)
                        </span>
                      )}
                    </div>
                    {Array.isArray(act.detail?.moves) && act.detail.moves.length > 0 && (
                      <div
                        className="font-mono-code"
                        style={{
                          fontSize: "11px",
                          color: "var(--ink-3)",
                          marginTop: "3px",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {act.detail.moves[0].fromPath} → {act.detail.moves[0].toPath}
                        {act.detail.moves.length > 1
                          ? ` (+${act.detail.moves.length - 1} more)`
                          : ""}
                      </div>
                    )}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    {act.undone ? (
                      onRedoAction && (
                        <button
                          type="button"
                          className="btn-tactile solid"
                          style={{ padding: "4px 10px", fontSize: "11.5px" }}
                          onClick={() => onRedoAction(act.id)}
                        >
                          redo
                        </button>
                      )
                    ) : (
                      onUndoAction && (
                        <button
                          type="button"
                          className="btn-tactile"
                          style={{ padding: "4px 10px", fontSize: "11.5px" }}
                          onClick={() => onUndoAction(act.id)}
                        >
                          undo
                        </button>
                      )
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Live Telemetry Event Log */}
        {recentEvents.length > 0 && (
          <div className="shelf-section">
            <div className="shelf-section-header">
              <div>
                <h3
                  className="font-display"
                  style={{ fontSize: "18px", fontWeight: 400 }}
                >
                  recent activity
                </h3>
                <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  live events from the file watcher and onnx worker
                </p>
              </div>
              <span
                className="font-mono-code"
                style={{ fontSize: "11.5px", color: "var(--ink-3)" }}
              >
                {recentEvents.length} events
              </span>
            </div>
            <div>
              {recentEvents.slice(0, 8).map((ev, idx) => (
                <div
                  key={`${ev.timestamp}-${idx}`}
                  className="file-item-row"
                  style={{
                    padding: "10px 20px",
                    cursor: ev.file_id ? "pointer" : "default",
                  }}
                  onClick={() => {
                    if (ev.file_id) onSelectFile(ev.file_id);
                  }}
                >
                  <div
                    style={{
                      fontSize: "13px",
                      color: "var(--ink-2)",
                    }}
                  >
                    {ev.message}
                  </div>
                  <span
                    className="font-mono-code"
                    style={{ fontSize: "11.5px", color: "var(--ink-3)" }}
                  >
                    {(ev.elapsed_ms ?? 0).toFixed(0)} ms
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
};
