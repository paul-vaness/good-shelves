import React, { useEffect, useState } from "react";
import { SmartCleanPreview } from "../lib/tauri-api";
import { formatShortcut } from "../lib/platform";

interface SmartCleanModalProps {
  open: boolean;
  preview: SmartCleanPreview | null;
  running: boolean;
  onClose: () => void;
  onRollbackFolders?: () => Promise<void> | void;
  onExecute: (opts: {
    applyRenames: boolean;
    archiveDuplicates: boolean;
    organizeFolders: boolean;
  }) => Promise<void>;
}

export const SmartCleanModal: React.FC<SmartCleanModalProps> = ({
  open,
  preview,
  running,
  onClose,
  onRollbackFolders,
  onExecute,
}) => {
  const [applyRenames, setApplyRenames] = useState(true);
  const [archiveDuplicates, setArchiveDuplicates] = useState(true);
  const [organizeFolders, setOrganizeFolders] = useState(true);
  const [expandedSection, setExpandedSection] = useState<"renames" | "dups" | "moves" | null>(null);

  useEffect(() => {
    if (open) {
      setApplyRenames(true);
      setArchiveDuplicates(true);
      setOrganizeFolders(true);
      setExpandedSection(null);
    }
  }, [open]);

  if (!open) return null;

  const renameCount = preview?.renames.length ?? 0;
  const dupCount = preview?.duplicates.length ?? 0;
  const moveCount = preview?.moves.length ?? 0;
  const totalSelected =
    (applyRenames ? renameCount : 0) +
    (archiveDuplicates ? dupCount : 0) +
    (organizeFolders ? moveCount : 0);

  return (
    <div
      className="modal-wrap open"
      role="dialog"
      aria-modal="true"
      aria-label="Clean up and organize workspace"
      onClick={(e) => {
        if (e.target === e.currentTarget && !running) onClose();
      }}
    >
      <div className="modal-card" style={{ maxWidth: "660px" }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: "12px",
            paddingBottom: "14px",
            borderBottom: "1px solid var(--rule)",
          }}
        >
          <div>
            <h2 className="font-display" style={{ fontSize: "23px", fontWeight: 500, letterSpacing: "-0.02em" }}>
              Clean up &amp; organize workspace
            </h2>
            <p style={{ fontSize: "13px", color: "var(--ink-2)", marginTop: "3px" }}>
              Preview every change before applying. Everything is reversible with {formatShortcut("Z")}.
            </p>
          </div>
          <button type="button" className="shelf-header-subtle-btn" onClick={onClose} disabled={running} aria-label="Close">
            Close
          </button>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "10px", marginTop: "16px" }}>
          {/* 1. Rename Suggestions */}
          <div
            style={{
              border: "1px solid var(--rule)",
              borderRadius: "10px",
              background: applyRenames ? "var(--bg-subtle)" : "var(--bg-surface)",
              overflow: "hidden",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", padding: "12px 14px" }}>
              <label style={{ display: "flex", alignItems: "center", gap: "10px", cursor: "pointer", flex: 1, minWidth: 0 }}>
                <input
                  type="checkbox"
                  checked={applyRenames}
                  onChange={(e) => setApplyRenames(e.target.checked)}
                  style={{ width: "16px", height: "16px", accentColor: "var(--ink)" }}
                />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: "13.5px", fontWeight: 500 }}>
                    Rename unclear filenames ({renameCount})
                  </div>
                  <div style={{ fontSize: "12px", color: "var(--ink-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {renameCount > 0
                      ? `e.g. ${preview?.renames[0]?.fromName} → ${preview?.renames[0]?.toName}`
                      : "All filenames are already descriptive"}
                  </div>
                </div>
              </label>
              {renameCount > 0 && (
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "3px 10px", minHeight: "26px", fontSize: "11.5px" }}
                  onClick={() => setExpandedSection((s) => (s === "renames" ? null : "renames"))}
                >
                  <span>{expandedSection === "renames" ? "Hide" : "Preview"}</span>
                </button>
              )}
            </div>
            {expandedSection === "renames" && renameCount > 0 && (
              <div style={{ borderTop: "1px solid var(--rule)", maxHeight: "160px", overflowY: "auto", background: "var(--bg-surface)" }}>
                {preview?.renames.map((r) => (
                  <div
                    key={r.fileId}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: "10px",
                      padding: "7px 14px",
                      fontSize: "12px",
                      borderBottom: "1px solid var(--rule-soft)",
                    }}
                  >
                    <span style={{ color: "var(--ink-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.fromName}</span>
                    <span style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>→ {r.toName}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 2. Duplicate Copies */}
          <div
            style={{
              border: "1px solid var(--rule)",
              borderRadius: "10px",
              background: archiveDuplicates ? "var(--bg-subtle)" : "var(--bg-surface)",
              overflow: "hidden",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", padding: "12px 14px" }}>
              <label style={{ display: "flex", alignItems: "center", gap: "10px", cursor: "pointer", flex: 1, minWidth: 0 }}>
                <input
                  type="checkbox"
                  checked={archiveDuplicates}
                  onChange={(e) => setArchiveDuplicates(e.target.checked)}
                  style={{ width: "16px", height: "16px", accentColor: "var(--ink)" }}
                />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: "13.5px", fontWeight: 500 }}>
                    Archive duplicate copies ({dupCount} · {preview?.totalSavedMB ?? 0} MB)
                  </div>
                  <div style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                    Moves redundant copies safely to your GoodShelves_Archive folder
                  </div>
                </div>
              </label>
              {dupCount > 0 && (
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "3px 10px", minHeight: "26px", fontSize: "11.5px" }}
                  onClick={() => setExpandedSection((s) => (s === "dups" ? null : "dups"))}
                >
                  <span>{expandedSection === "dups" ? "Hide" : "Preview"}</span>
                </button>
              )}
            </div>
            {expandedSection === "dups" && dupCount > 0 && (
              <div style={{ borderTop: "1px solid var(--rule)", maxHeight: "160px", overflowY: "auto", background: "var(--bg-surface)" }}>
                {preview?.duplicates.map((d) => (
                  <div
                    key={d.pairId}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: "10px",
                      padding: "7px 14px",
                      fontSize: "12px",
                      borderBottom: "1px solid var(--rule-soft)",
                    }}
                  >
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      Archive <strong>{d.copyName}</strong> (keep {d.keepName})
                    </span>
                    <span className="tabular-nums" style={{ color: "var(--ink-3)", flexShrink: 0 }}>{d.savedMB} MB</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 3. Organize Disk Folders */}
          <div
            style={{
              border: "1px solid var(--rule)",
              borderRadius: "10px",
              background: organizeFolders ? "var(--bg-subtle)" : "var(--bg-surface)",
              overflow: "hidden",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", padding: "12px 14px" }}>
              <label style={{ display: "flex", alignItems: "center", gap: "10px", cursor: "pointer", flex: 1, minWidth: 0 }}>
                <input
                  type="checkbox"
                  checked={organizeFolders}
                  onChange={(e) => setOrganizeFolders(e.target.checked)}
                  style={{ width: "16px", height: "16px", accentColor: "var(--ink)" }}
                />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: "13.5px", fontWeight: 500 }}>
                    Organize into Shelves folders on disk ({moveCount})
                  </div>
                  <div style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                    Uncheck to keep your current disk folders unchanged (virtual shelves only)
                  </div>
                </div>
              </label>
              {moveCount > 0 && (
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "3px 10px", minHeight: "26px", fontSize: "11.5px" }}
                  onClick={() => setExpandedSection((s) => (s === "moves" ? null : "moves"))}
                >
                  <span>{expandedSection === "moves" ? "Hide" : "Preview"}</span>
                </button>
              )}
            </div>
            {expandedSection === "moves" && moveCount > 0 && (
              <div style={{ borderTop: "1px solid var(--rule)", maxHeight: "160px", overflowY: "auto", background: "var(--bg-surface)" }}>
                {preview?.moves.map((mv) => (
                  <div
                    key={mv.fileId}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: "10px",
                      padding: "7px 14px",
                      fontSize: "12px",
                      borderBottom: "1px solid var(--rule-soft)",
                    }}
                  >
                    <span style={{ color: "var(--ink-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{mv.fromShort}</span>
                    <span style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>→ {mv.toShort}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div
          style={{
            marginTop: "20px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "10px",
          }}
        >
          <div>
            {onRollbackFolders && (
              <button
                type="button"
                className="btn-tactile"
                disabled={running}
                onClick={onRollbackFolders}
                title="Undo the most recent physical folder organization batch"
              >
                <span>Undo last folder move</span>
              </button>
            )}
          </div>
          <div style={{ display: "flex", gap: "8px" }}>
            <button type="button" className="btn-tactile" onClick={onClose} disabled={running}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-tactile solid"
              disabled={running || totalSelected === 0}
              onClick={() =>
                onExecute({
                  applyRenames,
                  archiveDuplicates,
                  organizeFolders,
                })
              }
            >
              {running ? "Applying…" : `Apply ${totalSelected} changes`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
