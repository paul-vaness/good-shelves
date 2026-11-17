import React, { useEffect, useMemo, useState } from "react";
import {
  FolderPlus,
  Pencil,
  FolderInput,
  Trash2,
  Sparkles,
  FolderOpen,
  Copy,
  Check,
  AlertTriangle,
  X,
} from "lucide-react";
import { ShelfFile, ShelfGroup, WatchedFolder, pickFolderNative } from "../lib/tauri-api";

export type FileDialogState =
  | { type: "closed" }
  | { type: "new-folder" }
  | { type: "rename"; file: ShelfFile }
  | { type: "move"; file: ShelfFile; initialMode?: "move" | "copy" }
  | { type: "delete"; file: ShelfFile }
  | { type: "bulk-tag"; fileIds: string[] }
  | { type: "create-smart-shelf"; initialFileIds?: string[]; initialQuery?: string }
  | { type: "rename-shelf"; shelf: ShelfGroup };

interface FileActionDialogsProps {
  dialog: FileDialogState;
  watchedFolders: WatchedFolder[];
  allFiles: ShelfFile[];
  onClose: () => void;
  onCreateFolder: (parentPath: string, folderName: string, watch: boolean) => Promise<void>;
  onRenameFile: (file: ShelfFile, newName: string) => Promise<void>;
  onMoveOrCopyFile: (file: ShelfFile, targetFolder: string, mode: "move" | "copy") => Promise<void>;
  onTrashFile: (file: ShelfFile) => Promise<void>;
  onDeletePermanent: (file: ShelfFile) => Promise<void>;
  onBulkTag?: (fileIds: string[], tag: string) => Promise<void>;
  onCreateSmartShelf?: (opts: { name: string; query: string; tag: string; fileIds: string[] }) => Promise<void>;
  onRenameShelf?: (shelfId: string, newName: string) => Promise<void>;
}

const sanitizeFolderName = (raw: string) =>
  raw.replace(/[<>:"/\\|?*]+/g, "_").trim().replace(/^[. ]+|[. ]+$/g, "");

const splitNameParts = (filename: string): { stem: string; ext: string } => {
  const dot = filename.lastIndexOf(".");
  if (dot <= 0) return { stem: filename, ext: "" };
  return { stem: filename.slice(0, dot), ext: filename.slice(dot) };
};

export const FileActionDialogs: React.FC<FileActionDialogsProps> = ({
  dialog,
  watchedFolders,
  allFiles,
  onClose,
  onCreateFolder,
  onRenameFile,
  onMoveOrCopyFile,
  onTrashFile,
  onDeletePermanent,
  onBulkTag,
  onCreateSmartShelf,
  onRenameShelf,
}) => {
  const isOpen = dialog.type !== "closed";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // New folder state
  const [parentPath, setParentPath] = useState<string>("");
  const [folderName, setFolderName] = useState<string>("");
  const [autoWatch, setAutoWatch] = useState<boolean>(true);

  // Rename state
  const [newName, setNewName] = useState<string>("");

  // Move/copy state
  const [moveMode, setMoveMode] = useState<"move" | "copy">("move");
  const [targetFolder, setTargetFolder] = useState<string>("");

  // Bulk tag state
  const [bulkTagInput, setBulkTagInput] = useState<string>("");

  // Smart shelf & rename shelf state
  const [smartShelfName, setSmartShelfName] = useState<string>("");
  const [smartShelfQuery, setSmartShelfQuery] = useState<string>("");
  const [smartShelfTag, setSmartShelfTag] = useState<string>("");
  const [shelfRenameDraft, setShelfRenameDraft] = useState<string>("");

  // Sync initial state when dialog opens
  useEffect(() => {
    setError(null);
    setBusy(false);
    if (dialog.type === "new-folder") {
      setParentPath(watchedFolders[0]?.path || "~/Documents");
      setFolderName("");
      setAutoWatch(true);
    } else if (dialog.type === "rename") {
      setNewName(dialog.file.name);
    } else if (dialog.type === "move") {
      setMoveMode(dialog.initialMode || "move");
      setTargetFolder(dialog.file.folderFull || watchedFolders[0]?.path || "");
    } else if (dialog.type === "bulk-tag") {
      setBulkTagInput("");
    } else if (dialog.type === "create-smart-shelf") {
      setSmartShelfName("");
      setSmartShelfQuery(dialog.initialQuery || "");
      setSmartShelfTag("");
    } else if (dialog.type === "rename-shelf") {
      setShelfRenameDraft(dialog.shelf.name);
    }
  }, [dialog, watchedFolders]);

  // Escape to close, Enter to confirm safe default in delete dialog
  useEffect(() => {
    if (!isOpen) return;
    const onKey = async (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) {
        e.preventDefault();
        e.stopImmediatePropagation();
        onClose();
      } else if (e.key === "Enter" && dialog.type === "delete" && !busy) {
        const target = e.target as HTMLElement | null;
        if (target && (target.tagName === "BUTTON" || target.tagName === "A")) return;
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await onTrashFile(dialog.file);
          onClose();
        } catch (err) {
          setError(err instanceof Error ? err.message : "Could not trash file");
        } finally {
          setBusy(false);
        }
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [isOpen, busy, onClose, dialog, onTrashFile]);

  // Smart folder recommendations for Move dialog based on shelf similarity
  const smartDestinations = useMemo(() => {
    if (dialog.type !== "move") return [];
    const targetFile = dialog.file;
    const folderCounts = new Map<string, { full: string; short: string; count: number }>();

    for (const f of allFiles) {
      if (f.id === targetFile.id || !f.folderFull) continue;
      if (f.shelfId === targetFile.shelfId) {
        const cur = folderCounts.get(f.folderFull) || {
          full: f.folderFull,
          short: f.folder,
          count: 0,
        };
        cur.count += 1;
        folderCounts.set(f.folderFull, cur);
      }
    }

    return Array.from(folderCounts.values())
      .filter((d) => d.full !== targetFile.folderFull)
      .sort((a, b) => b.count - a.count)
      .slice(0, 4);
  }, [dialog, allFiles]);

  const cleanFolderPreview = sanitizeFolderName(folderName);

  const handleBrowseParent = async () => {
    const picked = await pickFolderNative();
    if (picked) setParentPath(picked);
  };

  const handleBrowseMoveTarget = async () => {
    const picked = await pickFolderNative();
    if (picked) setTargetFolder(picked);
  };

  const handleSubmitNewFolder = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!cleanFolderPreview) {
      setError("Please enter a valid folder name.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onCreateFolder(parentPath || "~/Documents", cleanFolderPreview, autoWatch);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create folder");
    } finally {
      setBusy(false);
    }
  };

  const handleSubmitRename = async (e: React.FormEvent) => {
    if (dialog.type !== "rename") return;
    e.preventDefault();
    const trimmed = newName.trim();
    if (!trimmed) {
      setError("Filename cannot be empty.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onRenameFile(dialog.file, trimmed);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rename failed");
    } finally {
      setBusy(false);
    }
  };

  const handleSubmitMove = async (e: React.FormEvent) => {
    if (dialog.type !== "move") return;
    e.preventDefault();
    const dest = targetFolder.trim();
    if (!dest) {
      setError("Please choose a destination folder.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onMoveOrCopyFile(dialog.file, dest, moveMode);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Operation failed");
    } finally {
      setBusy(false);
    }
  };

  const applyRenamePreset = (preset: "kebab" | "snake" | "date" | "title") => {
    if (dialog.type !== "rename") return;
    const { stem, ext } = splitNameParts(newName || dialog.file.name);
    const cleaned = stem.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
    let nextStem = cleaned;
    if (preset === "kebab") {
      nextStem = cleaned.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    } else if (preset === "snake") {
      nextStem = cleaned.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
    } else if (preset === "date") {
      const today = new Date().toISOString().slice(0, 10);
      const withoutDate = cleaned.replace(/^\d{4}-\d{2}-\d{2}[_\s-]*/, "");
      nextStem = `${today}_${withoutDate.toLowerCase().replace(/[^a-z0-9]+/g, "_")}`;
    } else if (preset === "title") {
      nextStem = cleaned
        .split(" ")
        .map((w) => (w ? w[0].toUpperCase() + w.slice(1).toLowerCase() : ""))
        .join(" ");
    }
    setNewName(`${nextStem || stem}${ext}`);
  };

  return (
    <div
      className={`modal-wrap ${isOpen ? "open" : ""}`}
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
      aria-hidden={!isOpen}
    >
      <div
        className="modal-card"
        style={{ maxWidth: "520px", padding: "24px 26px" }}
        role="dialog"
        aria-modal="true"
      >
        {/* NEW FOLDER DIALOG */}
        {dialog.type === "new-folder" && (
          <form onSubmit={handleSubmitNewFolder} style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                <div
                  style={{
                    width: "34px",
                    height: "34px",
                    borderRadius: "8px",
                    border: "1px solid var(--rule-strong)",
                    background: "var(--bg-subtle)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <FolderPlus size={17} />
                </div>
                <div>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 400, lineHeight: 1.1 }}>
                    create new folder
                  </h2>
                  <p style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                    organize on disk and index automatically
                  </p>
                </div>
              </div>
              <button type="button" className="row-icon-btn" onClick={onClose} aria-label="Close">
                <X size={16} />
              </button>
            </div>

            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "6px" }}>
                folder name
              </label>
              <input
                type="text"
                autoFocus
                className="dialog-input"
                value={folderName}
                onChange={(e) => setFolderName(e.target.value)}
                placeholder="e.g. Q4 Tax Receipts"
              />
              <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" }}>
                {["Receipts", "Projects", "Archive", "Reference", "Screenshots"].map((preset) => (
                  <button
                    key={preset}
                    type="button"
                    className="tag-pill-suggest"
                    onClick={() => setFolderName(preset)}
                  >
                    + {preset}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
                <label style={{ fontSize: "12px", fontWeight: 500, color: "var(--ink-2)" }}>
                  parent location
                </label>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "3px 9px", fontSize: "11.5px" }}
                  onClick={handleBrowseParent}
                >
                  <FolderOpen size={12} /> browse…
                </button>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: "6px", maxHeight: "148px", overflowY: "auto" }}>
                {watchedFolders.map((wf) => {
                  const selected = parentPath === wf.path;
                  return (
                    <button
                      key={wf.path}
                      type="button"
                      className={`folder-choice-card ${selected ? "selected" : ""}`}
                      onClick={() => setParentPath(wf.path)}
                    >
                      <span className="font-mono-code" style={{ fontSize: "12px", overflow: "hidden", textOverflow: "ellipsis" }}>
                        {wf.path}
                      </span>
                      {selected && <Check size={14} />}
                    </button>
                  );
                })}
                {!watchedFolders.some((wf) => wf.path === parentPath) && parentPath && (
                  <div className="folder-choice-card selected">
                    <span className="font-mono-code" style={{ fontSize: "12px" }}>{parentPath}</span>
                    <Check size={14} />
                  </div>
                )}
              </div>
            </div>

            <div
              style={{
                padding: "10px 12px",
                borderRadius: "7px",
                background: "var(--bg-subtle)",
                border: "1px solid var(--rule)",
                fontSize: "12px",
              }}
            >
              <div style={{ color: "var(--ink-3)", marginBottom: "2px" }}>will create:</div>
              <div className="font-mono-code" style={{ color: "var(--ink)", wordBreak: "break-all" }}>
                {parentPath.replace(/[\\/]+$/, "")}
                {parentPath.includes("\\") ? "\\" : "/"}
                {cleanFolderPreview || "…"}
              </div>
            </div>

            <label style={{ display: "flex", alignItems: "center", justifyContent: "space-between", cursor: "pointer" }}>
              <span style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                auto-watch this folder in Good Shelves
              </span>
              <button
                type="button"
                className={`mono-switch ${autoWatch ? "on" : ""}`}
                onClick={() => setAutoWatch((v) => !v)}
                aria-pressed={autoWatch}
              />
            </label>

            {error && <div className="inline-error-banner" role="alert">{error}</div>}

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", paddingTop: "4px" }}>
              <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                cancel
              </button>
              <button type="submit" className="btn-tactile solid" disabled={busy || !cleanFolderPreview}>
                {busy ? "creating…" : "create folder"}
              </button>
            </div>
          </form>
        )}

        {/* RENAME FILE DIALOG */}
        {dialog.type === "rename" && (
          <form onSubmit={handleSubmitRename} style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                <div
                  style={{
                    width: "34px",
                    height: "34px",
                    borderRadius: "8px",
                    border: "1px solid var(--rule-strong)",
                    background: "var(--bg-subtle)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <Pencil size={16} />
                </div>
                <div>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 400, lineHeight: 1.1 }}>
                    rename file
                  </h2>
                  <p className="font-mono-code" style={{ fontSize: "11.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                    {dialog.file.folder}/{dialog.file.name}
                  </p>
                </div>
              </div>
              <button type="button" className="row-icon-btn" onClick={onClose} aria-label="Close">
                <X size={16} />
              </button>
            </div>

            {dialog.file.suggestedName && dialog.file.suggestedName !== dialog.file.name && (
              <button
                type="button"
                className="folder-choice-card"
                onClick={() => setNewName(dialog.file.suggestedName!)}
                style={{ borderStyle: "dashed" }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "8px", minWidth: 0 }}>
                  <Sparkles size={14} style={{ flexShrink: 0 }} />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: "11px", color: "var(--ink-3)" }}>smart suggestion</div>
                    <div className="font-mono-code" style={{ fontSize: "12.5px", color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {dialog.file.suggestedName}
                    </div>
                  </div>
                </div>
                <span style={{ fontSize: "11.5px", fontWeight: 500, flexShrink: 0 }}>use</span>
              </button>
            )}

            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "6px" }}>
                new filename
              </label>
              <input
                type="text"
                autoFocus
                className="dialog-input font-mono-code"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onFocus={(e) => {
                  const dot = e.currentTarget.value.lastIndexOf(".");
                  if (dot > 0) e.currentTarget.setSelectionRange(0, dot);
                }}
              />
              <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" }}>
                <button type="button" className="tag-pill-suggest" onClick={() => applyRenamePreset("kebab")}>
                  kebab-case
                </button>
                <button type="button" className="tag-pill-suggest" onClick={() => applyRenamePreset("snake")}>
                  snake_case
                </button>
                <button type="button" className="tag-pill-suggest" onClick={() => applyRenamePreset("date")}>
                  + date prefix
                </button>
                <button type="button" className="tag-pill-suggest" onClick={() => applyRenamePreset("title")}>
                  Title Case
                </button>
              </div>
            </div>

            {error && <div className="inline-error-banner" role="alert">{error}</div>}

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingTop: "4px" }}>
              <span style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>reversible with Ctrl/⌘ + Z</span>
              <div style={{ display: "flex", gap: "8px" }}>
                <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                  cancel
                </button>
                <button
                  type="submit"
                  className="btn-tactile solid"
                  disabled={busy || !newName.trim() || newName.trim() === dialog.file.name}
                >
                  {busy ? "renaming…" : "rename file"}
                </button>
              </div>
            </div>
          </form>
        )}

        {/* MOVE / COPY FILE DIALOG */}
        {dialog.type === "move" && (
          <form onSubmit={handleSubmitMove} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                <div
                  style={{
                    width: "34px",
                    height: "34px",
                    borderRadius: "8px",
                    border: "1px solid var(--rule-strong)",
                    background: "var(--bg-subtle)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  {moveMode === "move" ? <FolderInput size={17} /> : <Copy size={16} />}
                </div>
                <div>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 400, lineHeight: 1.1 }}>
                    {moveMode === "move" ? "move file" : "copy file"}
                  </h2>
                  <p className="font-mono-code" style={{ fontSize: "11.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                    {dialog.file.name}
                  </p>
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <div className="seg-group">
                  <button
                    type="button"
                    className={`seg-btn ${moveMode === "move" ? "active" : ""}`}
                    onClick={() => setMoveMode("move")}
                  >
                    move
                  </button>
                  <button
                    type="button"
                    className={`seg-btn ${moveMode === "copy" ? "active" : ""}`}
                    onClick={() => setMoveMode("copy")}
                  >
                    copy
                  </button>
                </div>
                <button type="button" className="row-icon-btn" onClick={onClose} aria-label="Close">
                  <X size={16} />
                </button>
              </div>
            </div>

            {smartDestinations.length > 0 && (
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: "5px", fontSize: "11.5px", color: "var(--ink-3)", marginBottom: "6px" }}>
                  <Sparkles size={12} />
                  <span>smart suggestions (from “{dialog.file.shelfName}”)</span>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  {smartDestinations.map((dest) => {
                    const selected = targetFolder === dest.full;
                    return (
                      <button
                        key={dest.full}
                        type="button"
                        className={`folder-choice-card ${selected ? "selected" : ""}`}
                        onClick={() => setTargetFolder(dest.full)}
                      >
                        <div style={{ minWidth: 0 }}>
                          <div className="font-mono-code" style={{ fontSize: "12px", overflow: "hidden", textOverflow: "ellipsis" }}>
                            {dest.short}
                          </div>
                          <div style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                            {dest.count} similar {dest.count === 1 ? "file" : "files"} here
                          </div>
                        </div>
                        {selected && <Check size={14} />}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            <div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
                <label style={{ fontSize: "12px", fontWeight: 500, color: "var(--ink-2)" }}>
                  destination folder
                </label>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "3px 9px", fontSize: "11.5px" }}
                  onClick={handleBrowseMoveTarget}
                >
                  <FolderOpen size={12} /> browse…
                </button>
              </div>
              <input
                type="text"
                className="dialog-input font-mono-code"
                value={targetFolder}
                onChange={(e) => setTargetFolder(e.target.value)}
                placeholder="/path/to/destination"
                style={{ marginBottom: "8px", fontSize: "12.5px" }}
              />
              <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                {watchedFolders.map((wf) => (
                  <button
                    key={wf.path}
                    type="button"
                    className="tag-pill-suggest font-mono-code"
                    onClick={() => setTargetFolder(wf.path)}
                  >
                    {wf.path.split(/[\\/]/).filter(Boolean).pop() || wf.path}
                  </button>
                ))}
              </div>
            </div>

            {error && <div className="inline-error-banner" role="alert">{error}</div>}

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", paddingTop: "4px" }}>
              <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                cancel
              </button>
              <button type="submit" className="btn-tactile solid" disabled={busy || !targetFolder.trim()}>
                {busy ? "working…" : moveMode === "move" ? "move here" : "copy here"}
              </button>
            </div>
          </form>
        )}

        {/* DELETE / TRASH FILE DIALOG */}
        {dialog.type === "delete" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                <div
                  style={{
                    width: "34px",
                    height: "34px",
                    borderRadius: "8px",
                    border: "1px solid var(--danger-border)",
                    background: "var(--danger-bg)",
                    color: "var(--danger)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <Trash2 size={16} />
                </div>
                <div>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 400, lineHeight: 1.1 }}>
                    remove file
                  </h2>
                  <p style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                    choose how to remove this file
                  </p>
                </div>
              </div>
              <button type="button" className="row-icon-btn" onClick={onClose} aria-label="Close">
                <X size={16} />
              </button>
            </div>

            <div
              style={{
                padding: "12px 14px",
                borderRadius: "7px",
                border: "1px solid var(--rule)",
                background: "var(--bg-subtle)",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "4px" }}>
                <span className="file-type-tag">{dialog.file.format}</span>
                <span className="font-display" style={{ fontSize: "16px" }}>{dialog.file.title}</span>
              </div>
              <div className="font-mono-code" style={{ fontSize: "11.5px", color: "var(--ink-3)", wordBreak: "break-all" }}>
                {dialog.file.folder}/{dialog.file.name} · {dialog.file.size}
              </div>
            </div>

            {error && <div className="inline-error-banner" role="alert">{error}</div>}

            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              <button
                type="button"
                className="btn-tactile solid"
                style={{ width: "100%", justifyContent: "space-between", padding: "11px 14px" }}
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    await onTrashFile(dialog.file);
                    onClose();
                  } catch (err) {
                    setError(err instanceof Error ? err.message : "Could not trash file");
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <span>move to trash</span>
                <span style={{ fontSize: "11.5px", opacity: 0.75 }}>reversible (Ctrl/⌘ + Z)</span>
              </button>

              <button
                type="button"
                className="btn-tactile danger"
                style={{ width: "100%", justifyContent: "space-between", padding: "11px 14px" }}
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    await onDeletePermanent(dialog.file);
                    onClose();
                  } catch (err) {
                    setError(err instanceof Error ? err.message : "Could not delete file");
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                  <AlertTriangle size={14} /> delete permanently from disk
                </span>
                <span style={{ fontSize: "11.5px", opacity: 0.8 }}>cannot undo</span>
              </button>
            </div>
          </div>
        )}

        {/* 5. BULK TAG DIALOG */}
        {dialog.type === "bulk-tag" && (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const cleanTag = bulkTagInput.trim().toLowerCase();
              if (!cleanTag) {
                setError("Please enter a tag name");
                return;
              }
              setBusy(true);
              setError(null);
              try {
                await onBulkTag?.(dialog.fileIds, cleanTag);
                onClose();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Could not add tag");
              } finally {
                setBusy(false);
              }
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "14px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <Sparkles size={16} />
                <h2 className="font-display" style={{ fontSize: "20px", fontWeight: 400 }}>
                  tag {dialog.fileIds.length} selected file{dialog.fileIds.length === 1 ? "" : "s"}
                </h2>
              </div>
              <button type="button" className="btn-tactile" onClick={onClose} disabled={busy} style={{ padding: "4px 8px" }}>
                <X size={14} />
              </button>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
              <label style={{ fontSize: "12px", color: "var(--ink-2)" }}>tag name</label>
              <input
                type="text"
                className="font-mono-code"
                value={bulkTagInput}
                autoFocus
                onChange={(e) => setBulkTagInput(e.target.value)}
                placeholder="e.g. taxes-2026, travel, review"
                style={{
                  width: "100%",
                  padding: "9px 10px",
                  borderRadius: "6px",
                  border: "1px solid var(--rule-strong)",
                  background: "var(--bg-subtle)",
                  color: "var(--ink)",
                  fontSize: "13px",
                }}
              />
            </div>

            {error && (
              <div className="inline-error-banner" role="alert" style={{ marginTop: "10px" }}>
                {error}
              </div>
            )}

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "18px" }}>
              <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                cancel
              </button>
              <button type="submit" className="btn-tactile solid" disabled={busy}>
                {busy ? "applying…" : "apply tag"}
              </button>
            </div>
          </form>
        )}

        {/* 6. CREATE SMART SHELF DIALOG */}
        {dialog.type === "create-smart-shelf" && (
          <form
            style={{ display: "flex", flexDirection: "column", gap: "14px" }}
            onSubmit={async (e) => {
              e.preventDefault();
              const clean = smartShelfName.trim();
              if (!clean) {
                setError("Please enter a shelf name.");
                return;
              }
              setBusy(true);
              setError(null);
              try {
                await onCreateSmartShelf?.({
                  name: clean,
                  query: smartShelfQuery.trim(),
                  tag: smartShelfTag.trim(),
                  fileIds: dialog.initialFileIds || [],
                });
                onClose();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Could not create smart shelf");
              } finally {
                setBusy(false);
              }
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 400 }}>
                  create smart shelf
                </h2>
                <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                  {dialog.initialFileIds?.length
                    ? `includes ${dialog.initialFileIds.length} selected file(s) plus optional auto-rule`
                    : "group files automatically using keywords, operators, or tags"}
                </p>
              </div>
              <button type="button" className="row-icon-btn" onClick={onClose} aria-label="Close">
                <X size={16} />
              </button>
            </div>

            <div>
              <label style={{ display: "block", fontSize: "12px", color: "var(--ink-2)", marginBottom: "5px" }}>
                shelf name
              </label>
              <input
                type="text"
                autoFocus
                className="dialog-input"
                value={smartShelfName}
                onChange={(e) => setSmartShelfName(e.target.value)}
                placeholder="e.g. 2026 Tax & Receipts"
              />
            </div>

            <div>
              <label style={{ display: "block", fontSize: "12px", color: "var(--ink-2)", marginBottom: "5px" }}>
                auto-match query / operators (optional)
              </label>
              <input
                type="text"
                className="dialog-input font-mono-code"
                value={smartShelfQuery}
                onChange={(e) => setSmartShelfQuery(e.target.value)}
                placeholder="e.g. receipt type:image after:2026-01-01"
                style={{ fontSize: "12.5px" }}
              />
            </div>

            <div>
              <label style={{ display: "block", fontSize: "12px", color: "var(--ink-2)", marginBottom: "5px" }}>
                auto-match tag (optional)
              </label>
              <input
                type="text"
                className="dialog-input font-mono-code"
                value={smartShelfTag}
                onChange={(e) => setSmartShelfTag(e.target.value)}
                placeholder="e.g. finance"
                style={{ fontSize: "12.5px" }}
              />
            </div>

            {error && <div className="inline-error-banner" role="alert">{error}</div>}

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "6px" }}>
              <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                cancel
              </button>
              <button type="submit" className="btn-tactile solid" disabled={busy || !smartShelfName.trim()}>
                {busy ? "creating…" : "create shelf"}
              </button>
            </div>
          </form>
        )}

        {/* 7. RENAME SHELF DIALOG */}
        {dialog.type === "rename-shelf" && (
          <form
            style={{ display: "flex", flexDirection: "column", gap: "14px" }}
            onSubmit={async (e) => {
              e.preventDefault();
              const clean = shelfRenameDraft.trim();
              if (!clean) {
                setError("Shelf name cannot be empty.");
                return;
              }
              setBusy(true);
              setError(null);
              try {
                await onRenameShelf?.(dialog.shelf.id, clean);
                onClose();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Could not rename shelf");
              } finally {
                setBusy(false);
              }
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 400 }}>
                rename shelf
              </h2>
              <button type="button" className="row-icon-btn" onClick={onClose} aria-label="Close">
                <X size={16} />
              </button>
            </div>
            <input
              type="text"
              autoFocus
              className="dialog-input"
              value={shelfRenameDraft}
              onChange={(e) => setShelfRenameDraft(e.target.value)}
            />
            {error && <div className="inline-error-banner" role="alert">{error}</div>}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
              <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                cancel
              </button>
              <button type="submit" className="btn-tactile solid" disabled={busy || !shelfRenameDraft.trim()}>
                {busy ? "saving…" : "save name"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
};
