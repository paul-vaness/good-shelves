import React, { useEffect, useMemo, useRef, useState } from "react";
import { ShelfFile, ShelfGroup, WatchedFolder, pickFolderNative } from "../lib/tauri-api";
import {
  defaultExamplePath,
  formatShortcut,
  joinDisplayPath,
  pathBaseName,
  sanitizeFolderName,
} from "../lib/platform";

export type FileDialogState =
  | { type: "closed" }
  | { type: "new-folder"; defaultParentPath?: string }
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
  const cardRef = useRef<HTMLDivElement | null>(null);
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

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
    if (dialog.type !== "closed") {
      returnFocusRef.current = document.activeElement as HTMLElement | null;
    } else if (returnFocusRef.current) {
      const el = returnFocusRef.current;
      returnFocusRef.current = null;
      requestAnimationFrame(() => el?.focus?.());
    }

    if (dialog.type === "new-folder") {
      setParentPath(dialog.defaultParentPath || watchedFolders[0]?.path || defaultExamplePath("Documents"));
      setFolderName("");
      setAutoWatch(true);
    } else if (dialog.type === "rename") {
      setNewName(dialog.file.name);
      requestAnimationFrame(() => {
        const el = renameInputRef.current;
        if (!el) return;
        el.focus();
        const dot = dialog.file.name.lastIndexOf(".");
        if (dot > 0) el.setSelectionRange(0, dot);
      });
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

  // Escape to close + Modal Focus Trap
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) {
        e.preventDefault();
        e.stopImmediatePropagation();
        onClose();
        return;
      }
      if (e.key === "Tab" && cardRef.current) {
        const focusables = Array.from(
          cardRef.current.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
          )
        ).filter((el) => el.offsetParent !== null);
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [isOpen, busy, onClose]);

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

  const existingTags = useMemo(() => {
    const s = new Set<string>();
    for (const f of allFiles) {
      for (const t of f.tags || []) s.add(t);
    }
    return Array.from(s).sort().slice(0, 12);
  }, [allFiles]);

  const smartShelfMatchEstimate = useMemo(() => {
    if (dialog.type !== "create-smart-shelf") return 0;
    const baseIds = new Set(dialog.initialFileIds || []);
    const q = smartShelfQuery.trim().toLowerCase();
    const tag = smartShelfTag.trim().toLowerCase().replace(/^#+/, "");
    if (!q && !tag) return baseIds.size;

    const tokens = q.split(/\s+/).filter(Boolean);
    for (const f of allFiles) {
      if (baseIds.has(f.id)) continue;
      if (tag && !(f.tags || []).some((t) => t.toLowerCase() === tag)) continue;
      if (tokens.length > 0) {
        const hay = `${f.title} ${f.name} ${f.extractedText} ${f.ocrText} ${f.shelfName}`.toLowerCase();
        const ok = tokens.every((tok) => {
          if (tok.startsWith("type:")) return f.format.toLowerCase() === tok.slice(5) || f.modality === tok.slice(5);
          if (tok === "has:ocr") return Boolean(f.ocrText);
          if (tok === "unnamed:true") return Boolean(f.suggestedName && !f.renamed);
          if (tok.startsWith("-") && tok.length > 1) return !hay.includes(tok.slice(1));
          return hay.includes(tok);
        });
        if (!ok) continue;
      }
      baseIds.add(f.id);
    }
    return baseIds.size;
  }, [dialog, smartShelfQuery, smartShelfTag, allFiles]);

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
      await onCreateFolder(parentPath || watchedFolders[0]?.path || ".", cleanFolderPreview, autoWatch);
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
      inert={!isOpen ? true : undefined}
    >
      {isOpen && (
        <div
          ref={cardRef}
          className="modal-card"
          style={{ maxWidth: "520px", padding: "24px 26px" }}
          role="dialog"
          aria-modal="true"
        >
          {/* NEW FOLDER DIALOG */}
          {dialog.type === "new-folder" && (
            <form onSubmit={handleSubmitNewFolder} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 500, lineHeight: 1.15 }}>
                    Create new folder
                  </h2>
                  <p style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "3px" }}>
                    Creates a real folder on disk and optionally watches it
                  </p>
                </div>
                <button type="button" className="shelf-header-subtle-btn" onClick={onClose} aria-label="Close">
                  Close
                </button>
              </div>

              <div>
                <label htmlFor="newFolderNameInput" style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "6px" }}>
                  Folder name
                </label>
                <input
                  id="newFolderNameInput"
                  type="text"
                  autoFocus
                  className="dialog-input"
                  value={folderName}
                  onChange={(e) => setFolderName(e.target.value)}
                  placeholder="e.g. Tax Receipts 2026"
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
                  <span style={{ fontSize: "12px", fontWeight: 500, color: "var(--ink-2)" }}>
                    Parent location
                  </span>
                  <button
                    type="button"
                    className="btn-tactile"
                    style={{ padding: "3px 9px", minHeight: "26px", fontSize: "11.5px" }}
                    onClick={handleBrowseParent}
                  >
                    Browse…
                  </button>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: "6px", maxHeight: "148px", overflowY: "auto" }}>
                  {!watchedFolders.some((wf) => wf.path === parentPath) && parentPath && (
                    <div className="folder-choice-card selected">
                      <span style={{ fontSize: "12.5px", overflow: "hidden", textOverflow: "ellipsis" }}>
                        {parentPath}
                      </span>
                      <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink)", flexShrink: 0 }}>✓</span>
                    </div>
                  )}
                  {watchedFolders.map((wf) => {
                    const selected = parentPath === wf.path;
                    return (
                      <button
                        key={wf.path}
                        type="button"
                        className={`folder-choice-card ${selected ? "selected" : ""}`}
                        onClick={() => setParentPath(wf.path)}
                      >
                        <span style={{ fontSize: "12.5px", overflow: "hidden", textOverflow: "ellipsis" }}>
                          {wf.path}
                        </span>
                        {selected && <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink)", flexShrink: 0 }}>✓</span>}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div
                style={{
                  padding: "10px 12px",
                  borderRadius: "8px",
                  background: "var(--bg-subtle)",
                  border: "1px solid var(--rule)",
                  fontSize: "12px",
                }}
              >
                <div style={{ color: "var(--ink-3)", marginBottom: "2px" }}>New folder path:</div>
                <div style={{ color: "var(--ink)", wordBreak: "break-all" }}>
                  {joinDisplayPath(parentPath, cleanFolderPreview || "…")}
                </div>
              </div>

              <label style={{ display: "flex", alignItems: "center", justifyContent: "space-between", cursor: "pointer" }}>
                <span style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  Watch this folder automatically
                </span>
                <button
                  type="button"
                  role="switch"
                  aria-checked={autoWatch}
                  className={`mono-switch ${autoWatch ? "on" : ""}`}
                  onClick={() => setAutoWatch((v) => !v)}
                />
              </label>

              {error && <div className="inline-error-banner" role="alert">{error}</div>}

              <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", paddingTop: "4px" }}>
                <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="btn-tactile solid" disabled={busy || !cleanFolderPreview}>
                  {busy ? "Creating…" : "Create folder"}
                </button>
              </div>
            </form>
          )}

          {/* RENAME FILE DIALOG */}
          {dialog.type === "rename" && (
            <form onSubmit={handleSubmitRename} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div style={{ minWidth: 0 }}>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 500, lineHeight: 1.15 }}>
                    Rename file
                  </h2>
                  <p style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "3px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {dialog.file.name}
                  </p>
                </div>
                <button type="button" className="shelf-header-subtle-btn" onClick={onClose} aria-label="Close">
                  Close
                </button>
              </div>

              {dialog.file.suggestedName && dialog.file.suggestedName !== dialog.file.name && (
                <button
                  type="button"
                  className="folder-choice-card"
                  onClick={() => setNewName(dialog.file.suggestedName!)}
                  style={{ borderStyle: "dashed", borderColor: "var(--rule-strong)", background: "var(--bg-subtle)" }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: "11px", color: "var(--ink-3)", fontWeight: 500 }}>Suggested filename</div>
                    <div style={{ fontSize: "13px", color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {dialog.file.suggestedName}
                    </div>
                  </div>
                  <span style={{ fontSize: "12px", fontWeight: 500, color: "var(--ink)", flexShrink: 0 }}>Use</span>
                </button>
              )}

              <div>
                <label htmlFor="renameFileInput" style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "6px" }}>
                  New filename
                </label>
                <input
                  ref={renameInputRef}
                  id="renameFileInput"
                  type="text"
                  className="dialog-input"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                />
                <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" }}>
                  <button type="button" className="tag-pill-suggest" onClick={() => applyRenamePreset("kebab")}>
                    kebab-case
                  </button>
                  <button type="button" className="tag-pill-suggest" onClick={() => applyRenamePreset("snake")}>
                    snake_case
                  </button>
                  <button type="button" className="tag-pill-suggest" onClick={() => applyRenamePreset("date")}>
                    + Date prefix
                  </button>
                  <button type="button" className="tag-pill-suggest" onClick={() => applyRenamePreset("title")}>
                    Title Case
                  </button>
                </div>
              </div>

              {error && <div className="inline-error-banner" role="alert">{error}</div>}

              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingTop: "4px" }}>
                <span style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>
                  Undo anytime with {formatShortcut("Z")}
                </span>
                <div style={{ display: "flex", gap: "8px" }}>
                  <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="btn-tactile solid"
                    disabled={busy || !newName.trim() || newName.trim() === dialog.file.name}
                  >
                    {busy ? "Renaming…" : "Rename file"}
                  </button>
                </div>
              </div>
            </form>
          )}

          {/* MOVE / COPY FILE DIALOG */}
          {dialog.type === "move" && (
            <form onSubmit={handleSubmitMove} style={{ display: "flex", flexDirection: "column", gap: "15px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div style={{ minWidth: 0 }}>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 500, lineHeight: 1.15 }}>
                    {moveMode === "move" ? "Move file to folder" : "Copy file to folder"}
                  </h2>
                  <p style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "3px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {dialog.file.name}
                  </p>
                </div>
                <button type="button" className="shelf-header-subtle-btn" onClick={onClose} aria-label="Close">
                  Close
                </button>
              </div>

              {/* Clear Move vs Copy choice inside dialog body */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", padding: "8px 12px", borderRadius: "8px", background: "var(--bg-subtle)", border: "1px solid var(--rule)" }}>
                <div className="seg-group" role="tablist" aria-label="Action mode">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={moveMode === "move"}
                    className={`seg-btn ${moveMode === "move" ? "active" : ""}`}
                    onClick={() => setMoveMode("move")}
                  >
                    Move
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={moveMode === "copy"}
                    className={`seg-btn ${moveMode === "copy" ? "active" : ""}`}
                    onClick={() => setMoveMode("copy")}
                  >
                    Copy
                  </button>
                </div>
                <span style={{ fontSize: "12px", color: "var(--ink-2)" }}>
                  {moveMode === "move" ? "Removes from current folder" : "Keeps original file"}
                </span>
              </div>

              {smartDestinations.length > 0 && (
                <div>
                  <div style={{ fontSize: "11.5px", color: "var(--ink-3)", marginBottom: "6px" }}>
                    Suggested folders (from “{dialog.file.shelfName}”)
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
                            <div style={{ fontSize: "12.5px", overflow: "hidden", textOverflow: "ellipsis" }}>
                              {dest.short}
                            </div>
                            <div style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                              {dest.count} similar {dest.count === 1 ? "file" : "files"} here
                            </div>
                          </div>
                          {selected && <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink)", flexShrink: 0 }}>✓</span>}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              <div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
                  <label htmlFor="moveDestInput" style={{ fontSize: "12px", fontWeight: 500, color: "var(--ink-2)" }}>
                    Destination folder
                  </label>
                  <button
                    type="button"
                    className="btn-tactile"
                    style={{ padding: "3px 9px", minHeight: "26px", fontSize: "11.5px" }}
                    onClick={handleBrowseMoveTarget}
                  >
                    Browse…
                  </button>
                </div>
                <input
                  id="moveDestInput"
                  type="text"
                  className="dialog-input"
                  value={targetFolder}
                  onChange={(e) => setTargetFolder(e.target.value)}
                  placeholder={defaultExamplePath("Documents")}
                  style={{ marginBottom: "8px", fontSize: "13px" }}
                />
                <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                  {watchedFolders.map((wf) => (
                    <button
                      key={wf.path}
                      type="button"
                      className="tag-pill-suggest"
                      onClick={() => setTargetFolder(wf.path)}
                    >
                      {pathBaseName(wf.path)}
                    </button>
                  ))}
                </div>
              </div>

              {error && <div className="inline-error-banner" role="alert">{error}</div>}

              <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", paddingTop: "4px" }}>
                <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="btn-tactile solid" disabled={busy || !targetFolder.trim()}>
                  {busy ? "Working…" : moveMode === "move" ? "Move file" : "Copy file"}
                </button>
              </div>
            </form>
          )}

          {/* DELETE / TRASH FILE DIALOG */}
          {dialog.type === "delete" && (
            <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 500, lineHeight: 1.15 }}>
                    Remove file?
                  </h2>
                  <p style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "3px" }}>
                    Choose whether to move to Trash or delete permanently
                  </p>
                </div>
                <button type="button" className="shelf-header-subtle-btn" onClick={onClose} aria-label="Close">
                  Close
                </button>
              </div>

              <div
                style={{
                  padding: "12px 14px",
                  borderRadius: "8px",
                  border: "1px solid var(--rule)",
                  background: "var(--bg-subtle)",
                }}
              >
                <div className="font-display" style={{ fontSize: "15px", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {dialog.file.title}
                </div>
                <div style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "2px" }}>
                  {dialog.file.format.toLowerCase()} · {dialog.file.size} · {dialog.file.date}
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
                  <span>Move to Trash</span>
                  <span style={{ fontSize: "11.5px", opacity: 0.8 }}>Reversible ({formatShortcut("Z")})</span>
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
                  <span>
                    Delete permanently from disk
                  </span>
                  <span style={{ fontSize: "11.5px", opacity: 0.85 }}>Cannot undo</span>
                </button>
              </div>
            </div>
          )}

          {/* BULK TAG DIALOG */}
          {dialog.type === "bulk-tag" && (
            <form
              style={{ display: "flex", flexDirection: "column", gap: "15px" }}
              onSubmit={async (e) => {
                e.preventDefault();
                const cleanTag = bulkTagInput.trim().toLowerCase().replace(/^#+/, "");
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
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 500 }}>
                  Tag {dialog.fileIds.length} selected {dialog.fileIds.length === 1 ? "file" : "files"}
                </h2>
                <button type="button" className="shelf-header-subtle-btn" onClick={onClose} disabled={busy} aria-label="Close">
                  Close
                </button>
              </div>

              <div>
                <label htmlFor="bulkTagInput" style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "6px" }}>
                  Tag name
                </label>
                <input
                  id="bulkTagInput"
                  type="text"
                  className="dialog-input"
                  value={bulkTagInput}
                  autoFocus
                  onChange={(e) => setBulkTagInput(e.target.value)}
                  placeholder="e.g. taxes-2026, travel, review"
                />
                {existingTags.length > 0 && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" }}>
                    {existingTags.map((t) => (
                      <button
                        key={t}
                        type="button"
                        className="tag-pill-suggest"
                        onClick={() => setBulkTagInput(t)}
                      >
                        #{t}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {error && <div className="inline-error-banner" role="alert">{error}</div>}

              <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
                <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="btn-tactile solid" disabled={busy}>
                  {busy ? "Applying…" : "Apply tag"}
                </button>
              </div>
            </form>
          )}

          {/* CREATE SMART SHELF DIALOG */}
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
                  setError(err instanceof Error ? err.message : "Could not create shelf");
                } finally {
                  setBusy(false);
                }
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div>
                  <h2 className="font-display" style={{ fontSize: "21px", fontWeight: 500 }}>
                    New smart shelf
                  </h2>
                  <p style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                    {dialog.initialFileIds?.length
                      ? `Includes ${dialog.initialFileIds.length} selected file(s) plus optional auto-rules`
                      : "Group files automatically by keyword, type, or tag"}
                  </p>
                </div>
                <button type="button" className="shelf-header-subtle-btn" onClick={onClose} aria-label="Close">
                  Close
                </button>
              </div>

              <div>
                <label htmlFor="smartShelfNameInput" style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "5px" }}>
                  Shelf name
                </label>
                <input
                  id="smartShelfNameInput"
                  type="text"
                  autoFocus
                  className="dialog-input"
                  value={smartShelfName}
                  onChange={(e) => setSmartShelfName(e.target.value)}
                  placeholder="e.g. 2026 Tax & Receipts"
                />
              </div>

              <div>
                <label htmlFor="smartShelfQueryInput" style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "5px" }}>
                  Auto-match keywords or filters (optional)
                </label>
                <input
                  id="smartShelfQueryInput"
                  type="text"
                  className="dialog-input"
                  value={smartShelfQuery}
                  onChange={(e) => setSmartShelfQuery(e.target.value)}
                  placeholder="e.g. receipt type:image"
                  style={{ fontSize: "13px" }}
                />
                <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "7px" }}>
                  {[
                    { label: "+ PDFs", token: "type:pdf" },
                    { label: "+ Images", token: "type:image" },
                    { label: "+ OCR text", token: "has:ocr" },
                    { label: "+ Unnamed", token: "unnamed:true" },
                  ].map((chip) => (
                    <button
                      key={chip.token}
                      type="button"
                      className="tag-pill-suggest"
                      onClick={() => {
                        if (!smartShelfQuery.includes(chip.token)) {
                          setSmartShelfQuery((q) => (q.trim() ? `${q.trim()} ${chip.token}` : chip.token));
                        }
                      }}
                    >
                      {chip.label}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label htmlFor="smartShelfTagInput" style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "5px" }}>
                  Auto-match tag (optional)
                </label>
                <input
                  id="smartShelfTagInput"
                  type="text"
                  className="dialog-input"
                  value={smartShelfTag}
                  onChange={(e) => setSmartShelfTag(e.target.value)}
                  placeholder="e.g. finance"
                  style={{ fontSize: "13px" }}
                />
              </div>

              <div
                style={{
                  padding: "8px 12px",
                  borderRadius: "7px",
                  background: "var(--bg-subtle)",
                  border: "1px solid var(--rule)",
                  fontSize: "12px",
                  color: "var(--ink-2)",
                }}
              >
                Live preview: <strong>{smartShelfMatchEstimate}</strong>{" "}
                {smartShelfMatchEstimate === 1 ? "file matches" : "files match"} right now
              </div>

              {error && <div className="inline-error-banner" role="alert">{error}</div>}

              <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "4px" }}>
                <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="btn-tactile solid" disabled={busy || !smartShelfName.trim()}>
                  {busy ? "Creating…" : "Create shelf"}
                </button>
              </div>
            </form>
          )}

          {/* RENAME SHELF DIALOG */}
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
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div>
                  <h2 className="font-display" style={{ fontSize: "20px", fontWeight: 500 }}>
                    Rename shelf
                  </h2>
                  <p style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                    Renames this virtual shelf label only; folders on disk are unchanged.
                  </p>
                </div>
                <button type="button" className="shelf-header-subtle-btn" onClick={onClose} aria-label="Close">
                  Close
                </button>
              </div>
              <div>
                <label htmlFor="renameShelfInput" style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "6px" }}>
                  Shelf label
                </label>
                <input
                  id="renameShelfInput"
                  type="text"
                  autoFocus
                  className="dialog-input"
                  value={shelfRenameDraft}
                  onChange={(e) => setShelfRenameDraft(e.target.value)}
                />
              </div>
              {error && <div className="inline-error-banner" role="alert">{error}</div>}
              <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
                <button type="button" className="btn-tactile" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="btn-tactile solid" disabled={busy || !shelfRenameDraft.trim()}>
                  {busy ? "Saving…" : "Save name"}
                </button>
              </div>
            </form>
          )}
        </div>
      )}
    </div>
  );
};
