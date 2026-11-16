import React, { useState } from "react";
import { RenameHistoryItem, ShelfFile } from "../lib/tauri-api";

interface TidyNamesViewProps {
  active: boolean;
  files: ShelfFile[];
  renameHistory: RenameHistoryItem[];
  selectedFileId: string;
  onSelectFile: (fileId: string) => void;
  onToggleRename: (fileId: string) => void;
  onApplyAllRenames: () => void;
}

export const TidyNamesView: React.FC<TidyNamesViewProps> = ({
  active,
  files,
  renameHistory,
  selectedFileId,
  onSelectFile,
  onToggleRename,
  onApplyAllRenames,
}) => {
  const [historyOpen, setHistoryOpen] = useState<boolean>(false);
  const renameCandidates = files.filter(
    (f) => Boolean(f.suggestedName) || f.renamed
  );

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
          <button
            type="button"
            className="btn-tactile solid"
            onClick={onApplyAllRenames}
          >
            <span>apply all suggestions</span>
          </button>
        </div>

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
                      onClick={() => onToggleRename(f.id)}
                    >
                      {f.renamed ? "undo" : "apply rename"}
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
                        onClick={() => onToggleRename(h.file_id)}
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
