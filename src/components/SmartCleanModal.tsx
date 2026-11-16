import React, { useEffect, useState } from "react";
import { SmartCleanPreview } from "../lib/tauri-api";

interface SmartCleanModalProps {
  open: boolean;
  preview: SmartCleanPreview | null;
  running: boolean;
  onClose: () => void;
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
  onExecute,
}) => {
  const [applyRenames, setApplyRenames] = useState(true);
  const [archiveDuplicates, setArchiveDuplicates] = useState(true);
  const [organizeFolders, setOrganizeFolders] = useState(true);

  useEffect(() => {
    if (open) {
      setApplyRenames(true);
      setArchiveDuplicates(true);
      setOrganizeFolders(true);
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
      aria-label="Batch workspace cleanup"
      onClick={(e) => {
        if (e.target === e.currentTarget && !running) onClose();
      }}
    >
      <div className="modal-card" style={{ maxWidth: "600px" }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            paddingBottom: "14px",
            borderBottom: "1px solid var(--rule)",
          }}
        >
          <div>
            <div
              style={{
                fontSize: "11px",
                fontWeight: 500,
                letterSpacing: "0.06em",
                color: "var(--ink-3)",
              }}
            >
              batch cleanup
            </div>
            <h2
              className="font-display"
              style={{ fontSize: "24px", fontWeight: 400, letterSpacing: "-0.02em" }}
            >
              clean up
            </h2>
          </div>
          <button type="button" className="btn-tactile" onClick={onClose} disabled={running}>
            close
          </button>
        </div>

        <p style={{ fontSize: "13.5px", color: "var(--ink-2)", marginTop: "12px" }}>
          Review what will happen below. Every change is recorded so you can undo
          or redo the entire batch anytime.
        </p>

        <div style={{ display: "flex", flexDirection: "column", gap: "10px", marginTop: "16px" }}>
          <label
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "12px 14px",
              border: "1px solid var(--rule)",
              borderRadius: "7px",
              background: applyRenames ? "var(--bg-subtle)" : "var(--bg-canvas)",
              cursor: "pointer",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              <input
                type="checkbox"
                checked={applyRenames}
                onChange={(e) => setApplyRenames(e.target.checked)}
                style={{ width: "15px", height: "15px", accentColor: "var(--ink)" }}
              />
              <div>
                <div style={{ fontSize: "13.5px", fontWeight: 500 }}>
                  rename cryptic files ({renameCount})
                </div>
                <div style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                  {renameCount > 0
                    ? `e.g. ${preview?.renames[0]?.fromName} → ${preview?.renames[0]?.toName}`
                    : "all filenames are already descriptive"}
                </div>
              </div>
            </div>
          </label>

          <label
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "12px 14px",
              border: "1px solid var(--rule)",
              borderRadius: "7px",
              background: archiveDuplicates ? "var(--bg-subtle)" : "var(--bg-canvas)",
              cursor: "pointer",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              <input
                type="checkbox"
                checked={archiveDuplicates}
                onChange={(e) => setArchiveDuplicates(e.target.checked)}
                style={{ width: "15px", height: "15px", accentColor: "var(--ink)" }}
              />
              <div>
                <div style={{ fontSize: "13.5px", fontWeight: 500 }}>
                  archive duplicate copies ({dupCount} · {preview?.totalSavedMB ?? 0} MB)
                </div>
                <div style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                  moves redundant copies to ~/Documents/GoodShelves_Archive
                </div>
              </div>
            </div>
          </label>

          <label
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "12px 14px",
              border: "1px solid var(--rule)",
              borderRadius: "7px",
              background: organizeFolders ? "var(--bg-subtle)" : "var(--bg-canvas)",
              cursor: "pointer",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              <input
                type="checkbox"
                checked={organizeFolders}
                onChange={(e) => setOrganizeFolders(e.target.checked)}
                style={{ width: "15px", height: "15px", accentColor: "var(--ink)" }}
              />
              <div>
                <div style={{ fontSize: "13.5px", fontWeight: 500 }}>
                  group files into ~/Shelves/ ({moveCount})
                </div>
                <div style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                  moves shelved files into topic folders on disk
                </div>
              </div>
            </div>
          </label>
        </div>

        <div
          style={{
            marginTop: "20px",
            display: "flex",
            justifyContent: "flex-end",
            gap: "10px",
          }}
        >
          <button type="button" className="btn-tactile" onClick={onClose} disabled={running}>
            cancel
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
            {running ? "cleaning…" : `apply ${totalSelected} changes`}
          </button>
        </div>
      </div>
    </div>
  );
};
