import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FolderBrowseResponse,
  ShelfFile,
  WatchedFolder,
  browseFolder,
  cleanPreviewText,
  deleteFolderOnDisk,
  ingestFilePath,
  openFileInOS,
  pasteFilesToFolder,
  reloadWatcherNative,
  renameFolderOnDisk,
  resolveLocalAssetSrc,
} from "../lib/tauri-api";

interface FoldersViewProps {
  active: boolean;
  watchedFolders: WatchedFolder[];
  selectedFileId: string;
  clipboard: { fileIds: string[]; mode: "copy" | "cut" } | null;
  onSetClipboard: (fileIds: string[], mode: "copy" | "cut") => void;
  onClearClipboard: () => void;
  onSelectFile: (fileId: string) => void;
  onOpenQuickLook: () => void;
  onCreateFolder: (parentPath?: string) => void;
  onRenameFile?: (file: ShelfFile) => void;
  onMoveFile?: (file: ShelfFile) => void;
  onDeleteFile?: (file: ShelfFile) => void;
  onBulkAction?: (fileIds: string[], action: "move" | "trash" | "tag" | "pin" | "unpin") => Promise<boolean> | boolean | void;
  onContextMenuFile: (e: React.MouseEvent, file: ShelfFile) => void;
  onRefreshState: (state: any) => void;
  onShowToast: (msg: string, undoFn?: () => void) => void;
  onUndo?: () => void;
}

type SortKey = "name" | "date" | "size" | "format";
type FolderTypeFilter = "all" | "docs" | "images" | "code" | "unindexed";

const FOLDER_TYPE_FILTERS: Array<{ id: FolderTypeFilter; label: string }> = [
  { id: "all", label: "All" },
  { id: "docs", label: "Docs" },
  { id: "images", label: "Images" },
  { id: "code", label: "Code" },
  { id: "unindexed", label: "Unindexed" },
];

const FoldersViewImpl: React.FC<FoldersViewProps> = ({
  active,
  watchedFolders,
  selectedFileId,
  clipboard,
  onSetClipboard,
  onClearClipboard,
  onSelectFile,
  onOpenQuickLook,
  onCreateFolder,
  onRenameFile,
  onDeleteFile,
  onBulkAction,
  onContextMenuFile,
  onRefreshState,
  onShowToast,
  onUndo,
}) => {
  const [currentPath, setCurrentPath] = useState<string>("");
  const [data, setData] = useState<FolderBrowseResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"list" | "grid">("list");
  const [sortBy, setSortBy] = useState<SortKey>("name");
  const [sortAsc, setSortAsc] = useState<boolean>(true);
  const [dirFilter, setDirFilter] = useState<string>("");
  const [folderTypeFilter, setFolderTypeFilter] = useState<FolderTypeFilter>("all");
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renameFolderDraft, setRenameFolderDraft] = useState<string>("");
  const [confirmDeletePath, setConfirmDeletePath] = useState<string | null>(null);
  const [checkedIds, setCheckedIds] = useState<string[]>([]);
  const [lastCheckedId, setLastCheckedId] = useState<string | null>(null);
  const filterInputRef = useRef<HTMLInputElement | null>(null);

  const loadDir = useCallback(async (target?: string) => {
    setLoading(true);
    setError(null);
    setCheckedIds([]);
    setLastCheckedId(null);
    try {
      const res = await browseFolder(target);
      setData(res);
      setCurrentPath(res.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open folder");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (data || loading) return;
    const initialTarget = watchedFolders.find((w) => w.active)?.path || watchedFolders[0]?.path;
    if (!initialTarget) return;
    if (active) {
      loadDir(initialTarget);
      return;
    }
    const timer = window.setTimeout(() => {
      loadDir(initialTarget);
    }, 120);
    return () => window.clearTimeout(timer);
  }, [active, data, loading, loadDir, watchedFolders]);

  const filteredSubfolders = useMemo(() => {
    const folders = data?.folders || [];
    const q = dirFilter.trim().toLowerCase();
    if (!q) return folders;
    return folders.filter((sub) => sub.name.toLowerCase().includes(q));
  }, [data?.folders, dirFilter]);

  const sortedFiles = useMemo(() => {
    const q = dirFilter.trim().toLowerCase();
    let rawList = data?.files || [];
    if (folderTypeFilter !== "all") {
      rawList = rawList.filter((f) => {
        if (folderTypeFilter === "unindexed") return Boolean(f.unindexed);
        if (folderTypeFilter === "images") return f.modality === "image";
        if (folderTypeFilter === "code") return f.modality === "code";
        if (folderTypeFilter === "docs") {
          const ext = (f.format || "").toLowerCase();
          return (
            f.modality === "document" ||
            ["pdf", "docx", "doc", "odt", "rtf", "txt", "md", "pptx", "ppt", "xlsx", "xlsm", "xls", "csv", "tsv"].includes(ext)
          );
        }
        return true;
      });
    }
    if (q) {
      rawList = rawList.filter((f) =>
        `${f.name} ${f.title} ${f.format} ${f.shelfName || ""}`.toLowerCase().includes(q)
      );
    }
    const list = [...rawList];
    list.sort((a, b) => {
      let cmp = 0;
      if (sortBy === "name") cmp = a.name.localeCompare(b.name);
      else if (sortBy === "date") cmp = a.mtime - b.mtime;
      else if (sortBy === "size") cmp = a.sizeBytes - b.sizeBytes;
      else if (sortBy === "format") cmp = a.format.localeCompare(b.format);
      return sortAsc ? cmp : -cmp;
    });
    return list;
  }, [data?.files, dirFilter, folderTypeFilter, sortBy, sortAsc]);

  const indexedFiles = useMemo(
    () => sortedFiles.filter((f) => !f.unindexed),
    [sortedFiles]
  );

  const cutIdSet = useMemo(
    () => new Set(clipboard?.mode === "cut" ? clipboard.fileIds : []),
    [clipboard]
  );

  const toggleChecked = useCallback(
    (fileId: string, shiftKey: boolean) => {
      if (shiftKey && lastCheckedId) {
        const idxA = indexedFiles.findIndex((f) => f.id === lastCheckedId);
        const idxB = indexedFiles.findIndex((f) => f.id === fileId);
        if (idxA !== -1 && idxB !== -1) {
          const [lo, hi] = idxA < idxB ? [idxA, idxB] : [idxB, idxA];
          const rangeIds = indexedFiles.slice(lo, hi + 1).map((f) => f.id);
          setCheckedIds((prev) => Array.from(new Set([...prev, ...rangeIds])));
          setLastCheckedId(fileId);
          return;
        }
      }
      setCheckedIds((prev) =>
        prev.includes(fileId) ? prev.filter((id) => id !== fileId) : [...prev, fileId]
      );
      setLastCheckedId(fileId);
    },
    [indexedFiles, lastCheckedId]
  );

  const handlePaste = useCallback(async () => {
    if (!clipboard || !clipboard.fileIds.length || !data?.path) return;
    try {
      const mode = clipboard.mode === "cut" ? "move" : "copy";
      const res = await pasteFilesToFolder(clipboard.fileIds, data.path, mode);
      onRefreshState(res.state);
      if (clipboard.mode === "cut") onClearClipboard();
      await loadDir(data.path);
      onShowToast(
        `${clipboard.mode === "cut" ? "Moved" : "Copied"} ${res.count} file(s) into ${data.displayPath}`,
        clipboard.mode === "cut" ? onUndo : undefined
      );
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Paste failed");
    }
  }, [clipboard, data?.path, data?.displayPath, onRefreshState, onClearClipboard, loadDir, onShowToast, onUndo]);

  const activeTargetIds = useMemo(() => {
    if (checkedIds.length > 0) return checkedIds;
    const cur = indexedFiles.find((f) => f.id === selectedFileId);
    return cur ? [cur.id] : [];
  }, [checkedIds, indexedFiles, selectedFileId]);

  const handleRenameFolder = async (folderPath: string) => {
    const clean = renameFolderDraft.trim();
    if (!clean) return;
    try {
      const res = await renameFolderOnDisk(folderPath, clean);
      onRefreshState(res.state);
      await reloadWatcherNative().catch(() => {});
      setRenamingPath(null);
      await loadDir(data?.path);
      onShowToast(`Renamed folder to ${res.name}`);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not rename folder");
    }
  };

  const handleDeleteFolder = async (folderPath: string, folderName: string) => {
    try {
      const res = await deleteFolderOnDisk(folderPath);
      onRefreshState(res.state);
      await reloadWatcherNative().catch(() => {});
      setConfirmDeletePath(null);
      await loadDir(data?.path);
      onShowToast(`Moved folder “${folderName}” to GoodShelves_Trash`, onUndo);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not trash folder");
    }
  };

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-folders"
      aria-hidden={!active}
      inert={!active ? true : undefined}
      tabIndex={active ? 0 : -1}
      onKeyDown={async (e) => {
        if (!active) return;
        const target = e.target as HTMLElement | null;
        const isInput = Boolean(
          target &&
            (target.tagName === "INPUT" ||
              target.tagName === "TEXTAREA" ||
              target.tagName === "SELECT" ||
              target.isContentEditable)
        );
        const mod = e.ctrlKey || e.metaKey;
        const lower = e.key.toLowerCase();

        if (mod && lower === "f") {
          e.preventDefault();
          e.stopPropagation();
          filterInputRef.current?.focus();
          filterInputRef.current?.select();
          return;
        }
        if (isInput) return;

        if (e.key === "Escape") {
          if (checkedIds.length > 0 || clipboard) {
            e.preventDefault();
            e.stopPropagation();
            if (checkedIds.length > 0) setCheckedIds([]);
            else onClearClipboard();
          }
          return;
        }

        if (mod && e.shiftKey && lower === "n") {
          e.preventDefault();
          e.stopPropagation();
          onCreateFolder(currentPath || undefined);
          return;
        }

        if (mod && lower === "a") {
          if (indexedFiles.length === 0) return;
          e.preventDefault();
          e.stopPropagation();
          const allIds = indexedFiles.map((f) => f.id);
          setCheckedIds(allIds);
          onShowToast(`Selected ${allIds.length} file${allIds.length === 1 ? "" : "s"}`);
          return;
        }

        if (mod && (lower === "c" || lower === "x")) {
          const textSel = window.getSelection()?.toString();
          if (textSel) return;
          if (activeTargetIds.length === 0) return;
          e.preventDefault();
          e.stopPropagation();
          onSetClipboard(activeTargetIds, lower === "x" ? "cut" : "copy");
          return;
        }

        if (mod && lower === "v") {
          if (!clipboard || clipboard.fileIds.length === 0) return;
          e.preventDefault();
          e.stopPropagation();
          await handlePaste();
          return;
        }

        if ((e.key === "Delete" || (mod && e.key === "Backspace")) && activeTargetIds.length > 0) {
          e.preventDefault();
          e.stopPropagation();
          if (activeTargetIds.length === 1 && onDeleteFile) {
            const single = indexedFiles.find((f) => f.id === activeTargetIds[0]);
            if (single) onDeleteFile(single);
          } else if (onBulkAction) {
            const ok = await onBulkAction(activeTargetIds, "trash");
            if (ok) {
              setCheckedIds([]);
              await loadDir(currentPath || undefined);
            }
          }
          return;
        }

        if (e.key === "F2" && selectedFileId && onRenameFile) {
          const cur = indexedFiles.find((f) => f.id === selectedFileId);
          if (cur) {
            e.preventDefault();
            e.stopPropagation();
            onRenameFile(cur);
          }
          return;
        }

        if ((e.key === "Backspace" || (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowLeft"))) && data?.parentPath) {
          e.preventDefault();
          e.stopPropagation();
          loadDir(data.parentPath);
          return;
        }
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          if (sortedFiles.length === 0) return;
          e.preventDefault();
          e.stopPropagation();
          const curIdx = sortedFiles.findIndex((f) => f.id === selectedFileId);
          const delta = e.key === "ArrowDown" ? 1 : -1;
          const nextIdx =
            curIdx === -1
              ? delta > 0
                ? 0
                : sortedFiles.length - 1
              : (curIdx + delta + sortedFiles.length) % sortedFiles.length;
          const next = sortedFiles[nextIdx];
          if (next) {
            if (!next.unindexed) onSelectFile(next.id);
            const row = e.currentTarget.querySelector(
              `[data-folder-file-id="${CSS.escape(next.id)}"]`
            ) as HTMLElement | null;
            row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
          }
        } else if (e.key === "Enter" && selectedFileId && !target?.closest("button, a")) {
          const cur = sortedFiles.find((f) => f.id === selectedFileId);
          if (cur && !cur.unindexed) {
            e.preventDefault();
            e.stopPropagation();
            onOpenQuickLook();
          }
        }
      }}
    >
      <div className="page-container">
        <div className="page-header">
          <div>
            <h1 className="page-title">Folders</h1>
            <p className="page-subtitle">
              Browse files directly on disk across your watched folders
            </p>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            {clipboard && clipboard.fileIds.length > 0 && (
              <button type="button" className="btn-tactile solid" onClick={handlePaste}>
                Paste {clipboard.fileIds.length} ({clipboard.mode === "cut" ? "Move" : "Copy"})
              </button>
            )}
            <button
              type="button"
              className="btn-tactile"
              onClick={() => onCreateFolder(currentPath || undefined)}
            >
              New folder
            </button>
            <button
              type="button"
              className="btn-tactile"
              onClick={() => loadDir(currentPath || undefined)}
              title="Refresh folder"
              aria-label="Refresh folder"
            >
              Refresh
            </button>
            <div className="seg-group" role="tablist" aria-label="Folder view layout">
              <button
                type="button"
                className={`seg-btn ${viewMode === "list" ? "active" : ""}`}
                onClick={() => setViewMode("list")}
              >
                List
              </button>
              <button
                type="button"
                className={`seg-btn ${viewMode === "grid" ? "active" : ""}`}
                onClick={() => setViewMode("grid")}
              >
                Grid
              </button>
            </div>
          </div>
        </div>

        {/* Flat Breadcrumb Path Strip */}
        <div className="folder-path-strip">
          <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap", minWidth: 0 }}>
            {watchedFolders.length > 1 && (
              <select
                aria-label="Watched root folder"
                className="editorial-select"
                style={{ minHeight: "30px", padding: "3px 26px 3px 9px", fontSize: "12px" }}
                value={
                  watchedFolders.find(
                    (w) =>
                      currentPath === w.path ||
                      currentPath.startsWith(w.path + "/") ||
                      currentPath.startsWith(w.path + "\\")
                  )?.path || watchedFolders[0]?.path || ""
                }
                onChange={(e) => loadDir(e.target.value)}
              >
                {watchedFolders.map((wf) => (
                  <option key={wf.path} value={wf.path}>
                    {wf.display_path}
                  </option>
                ))}
              </select>
            )}
            {data?.parentPath && (
              <button
                type="button"
                className="shelf-header-subtle-btn"
                title="Up one folder (Backspace / Alt+↑)"
                aria-label="Up one folder"
                onClick={() => loadDir(data.parentPath!)}
              >
                ↑ Up
              </button>
            )}
            {(data?.breadcrumbs || []).slice(-5).map((bc, idx, arr) => (
              <React.Fragment key={bc.path}>
                <button
                  type="button"
                  style={{
                    fontSize: "13px",
                    padding: "3px 5px",
                    borderRadius: "5px",
                    fontWeight: idx === arr.length - 1 ? 600 : 400,
                    color: idx === arr.length - 1 ? "var(--ink)" : "var(--ink-2)",
                  }}
                  onClick={() => loadDir(bc.path)}
                >
                  {bc.name}
                </button>
                {idx < arr.length - 1 && <span style={{ color: "var(--ink-3)", fontSize: "12px" }}>/</span>}
              </React.Fragment>
            ))}
          </div>
        </div>

        {/* Borderless Filter & Sort Toolbar */}
        <div className="folder-filter-strip">
          <div style={{ display: "flex", alignItems: "center", gap: "4px", flexWrap: "wrap" }} role="group" aria-label="Filter folder files by type">
            {FOLDER_TYPE_FILTERS.map((tf) => (
              <button
                key={tf.id}
                type="button"
                aria-pressed={folderTypeFilter === tf.id}
                className={`folder-filter-tab ${folderTypeFilter === tf.id ? "active" : ""}`}
                onClick={() => setFolderTypeFilter(tf.id)}
              >
                {tf.label}
              </button>
            ))}
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
              <input
                ref={filterInputRef}
                type="text"
                aria-label="Filter current folder"
                value={dirFilter}
                onChange={(e) => setDirFilter(e.target.value)}
                placeholder="Filter folder… (Ctrl+F)"
                className="dialog-input"
                style={{
                  width: "180px",
                  padding: dirFilter ? "4px 26px 4px 9px" : "4px 9px",
                  fontSize: "12px",
                  minHeight: "30px",
                }}
              />
              {dirFilter.length > 0 && (
                <button
                  type="button"
                  aria-label="Clear folder filter"
                  title="Clear folder filter"
                  onClick={() => setDirFilter("")}
                  style={{
                    position: "absolute",
                    right: "6px",
                    color: "var(--ink-3)",
                    display: "inline-flex",
                    padding: "2px",
                  }}
                >
                  <span style={{ fontSize: "13px" }}>×</span>
                </button>
              )}
            </div>
            <select
              aria-label="Sort folder files"
              className="editorial-select"
              style={{ minHeight: "30px", padding: "3px 26px 3px 8px", fontSize: "12px" }}
              value={sortBy}
              onChange={(e) => {
                const v = e.target.value as SortKey;
                setSortBy(v);
                setSortAsc(v === "name" || v === "format");
              }}
            >
              <option value="name">Sort: Name</option>
              <option value="date">Sort: Newest</option>
              <option value="size">Sort: Largest</option>
              <option value="format">Sort: Type</option>
            </select>
          </div>
        </div>

        {(dirFilter.trim().length > 0 || folderTypeFilter !== "all") && (
          <div
            role="region"
            aria-label="Active folder filters"
            style={{
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              gap: "6px",
              marginBottom: "14px",
            }}
          >
            <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-2)", marginRight: "4px" }}>
              Showing {sortedFiles.length} of {(data?.files || []).length} files
            </span>
            {folderTypeFilter !== "all" && (
              <button type="button" className="tag-pill" onClick={() => setFolderTypeFilter("all")}>
                <span>Type: {folderTypeFilter}</span>
                ×
              </button>
            )}
            {dirFilter.trim().length > 0 && (
              <button type="button" className="tag-pill" onClick={() => setDirFilter("")}>
                <span>Filter: “{dirFilter.trim()}”</span>
                ×
              </button>
            )}
            <button
              type="button"
              className="shelf-header-subtle-btn"
              onClick={() => {
                setFolderTypeFilter("all");
                setDirFilter("");
              }}
            >
              Reset
            </button>
          </div>
        )}

        {error && <div className="inline-error-banner" role="alert" style={{ marginBottom: "14px" }}>{error}</div>}

        {loading && !data ? (
          <div className="shelf-section" style={{ padding: "20px", display: "flex", flexDirection: "column", gap: "10px" }}>
            <div className="skeleton-line" style={{ width: "42%" }} />
            <div className="skeleton-line" style={{ width: "78%" }} />
            <div className="skeleton-line" style={{ width: "64%" }} />
          </div>
        ) : (
          <>
            {/* Subfolders */}
            {filteredSubfolders.length > 0 && (
              <div style={{ marginBottom: "18px" }}>
                <div style={{ fontSize: "11px", fontWeight: 600, letterSpacing: "0.05em", color: "var(--ink-3)", marginBottom: "8px", textTransform: "uppercase" }}>
                  Folders ({filteredSubfolders.length})
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: "8px" }}>
                  {filteredSubfolders.map((sub) => (
                    <div
                      key={sub.path}
                      className="folder-choice-card"
                      style={{ padding: "10px 12px", cursor: "pointer" }}
                      onClick={() => {
                        if (renamingPath !== sub.path) loadDir(sub.path);
                      }}
                    >
                      {renamingPath === sub.path ? (
                        <form
                          style={{ display: "flex", gap: "6px", width: "100%" }}
                          onClick={(e) => e.stopPropagation()}
                          onSubmit={(e) => {
                            e.preventDefault();
                            handleRenameFolder(sub.path);
                          }}
                        >
                          <input
                            type="text"
                            autoFocus
                            value={renameFolderDraft}
                            onChange={(e) => setRenameFolderDraft(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Escape") {
                                e.preventDefault();
                                e.stopPropagation();
                                setRenamingPath(null);
                              }
                            }}
                            className="dialog-input"
                            style={{ padding: "4px 8px", fontSize: "12.5px" }}
                          />
                          <button type="submit" className="btn-tactile solid" style={{ padding: "3px 10px", minHeight: "28px", fontSize: "12px" }}>
                            Save
                          </button>
                        </form>
                      ) : (
                        <>
                          <div style={{ minWidth: 0 }}>
                            <div className="font-display" style={{ fontSize: "14px", fontWeight: 500, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              {sub.name}
                            </div>
                            <div style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "2px" }}>
                              {sub.itemCount} items{sub.watched ? " · Watched" : ""}
                            </div>
                          </div>
                          <div style={{ display: "flex", gap: "4px", alignItems: "center" }} onClick={(e) => e.stopPropagation()}>
                            {confirmDeletePath === sub.path ? (
                              <>
                                <button
                                  type="button"
                                  className="btn-tactile danger"
                                  style={{ padding: "2px 8px", minHeight: "26px", fontSize: "11px" }}
                                  onClick={() => handleDeleteFolder(sub.path, sub.name)}
                                >
                                  Trash
                                </button>
                                <button
                                  type="button"
                                  className="shelf-header-subtle-btn"
                                  onClick={() => setConfirmDeletePath(null)}
                                >
                                  Cancel
                                </button>
                              </>
                            ) : (
                              <>
                                <button
                                  type="button"
                                  className="shelf-header-subtle-btn"
                                  aria-label={`Rename ${sub.name}`}
                                  onClick={() => {
                                    setConfirmDeletePath(null);
                                    setRenamingPath(sub.path);
                                    setRenameFolderDraft(sub.name);
                                  }}
                                >
                                  Rename
                                </button>
                                <button
                                  type="button"
                                  className="shelf-header-subtle-btn danger"
                                  aria-label={`Trash ${sub.name}`}
                                  onClick={() => setConfirmDeletePath(sub.path)}
                                >
                                  Trash
                                </button>
                              </>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Files */}
            <div style={{ fontSize: "11px", fontWeight: 600, letterSpacing: "0.05em", color: "var(--ink-3)", marginBottom: "8px", textTransform: "uppercase" }}>
              Files ({sortedFiles.length})
            </div>

            {sortedFiles.length === 0 ? (
              <div className="shelf-section" style={{ padding: "24px 20px", fontSize: "13.5px", color: "var(--ink-3)" }}>
                No files in this directory match the current filter.
              </div>
            ) : viewMode === "grid" ? (
              <div className="file-grid-wrap">
                {sortedFiles.map((f) => {
                  const thumb = resolveLocalAssetSrc(f);
                  const isSel = f.id === selectedFileId;
                  const isChecked = checkedIds.includes(f.id);
                  const isCut = cutIdSet.has(f.id);
                  return (
                    <div
                      key={f.id}
                      data-folder-file-id={f.id}
                      className={`file-grid-card ${isSel || isChecked ? "selected" : ""} ${isCut ? "file-cut-dimmed" : ""}`}
                      onClick={async (e) => {
                        if (f.unindexed && f.supported) {
                          try {
                            const res = await ingestFilePath(f.path);
                            onRefreshState(res.state);
                            onSelectFile(res.file.id);
                            await loadDir(currentPath);
                            return;
                          } catch {
                            // ignore
                          }
                        }
                        if (!f.unindexed) {
                          if (e.ctrlKey || e.metaKey || e.shiftKey) {
                            toggleChecked(f.id, e.shiftKey);
                          } else {
                            setCheckedIds([]);
                            onSelectFile(f.id);
                          }
                        }
                      }}
                      onDoubleClick={() => {
                        if (!f.unindexed) onOpenQuickLook();
                      }}
                      onContextMenu={(e) => {
                        if (!f.unindexed) onContextMenuFile(e, f);
                      }}
                    >
                      <div className="file-grid-thumb">
                        {thumb ? (
                          <img src={thumb} alt={f.title} loading="lazy" decoding="async" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                        ) : (
                          <div
                            style={{
                              width: "100%",
                              height: "100%",
                              padding: "10px 11px",
                              display: "flex",
                              flexDirection: "column",
                              justifyContent: "space-between",
                              alignItems: "flex-start",
                            }}
                          >
                            <div style={{ alignSelf: "flex-end" }}>
                              <span className="file-type-tag">{f.format}</span>
                            </div>
                            <div
                              style={{
                                fontSize: "11px",
                                lineHeight: 1.45,
                                color: "var(--ink-3)",
                                overflow: "hidden",
                                display: "-webkit-box",
                                WebkitLineClamp: 4,
                                WebkitBoxOrient: "vertical",
                                wordBreak: "break-word",
                              }}
                            >
                              {cleanPreviewText(f.extractedText || f.ocrText || f.whyMatched || f.name).slice(0, 110)}
                            </div>
                          </div>
                        )}
                      </div>
                      <div style={{ padding: "12px 14px", minWidth: 0 }}>
                        <div className="font-display" style={{ fontSize: "14px", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {f.title}
                        </div>
                        <div style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "2px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {f.format.toLowerCase()} · {f.size} · {f.date}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="shelf-list-surface">
                {sortedFiles.map((f) => {
                  const isSel = f.id === selectedFileId;
                  const isChecked = checkedIds.includes(f.id);
                  const isCut = cutIdSet.has(f.id);
                  return (
                    <div
                      key={f.id}
                      data-folder-file-id={f.id}
                      className={`file-item-row ${isSel || isChecked ? "selected" : ""} ${isCut ? "file-cut-dimmed" : ""}`}
                      onClick={async (e) => {
                        if (f.unindexed && f.supported) {
                          try {
                            const res = await ingestFilePath(f.path);
                            onRefreshState(res.state);
                            onSelectFile(res.file.id);
                            await loadDir(currentPath);
                            onShowToast(`Indexed ${f.name}`);
                            return;
                          } catch {
                            // ignore
                          }
                        }
                        if (!f.unindexed) {
                          if (e.ctrlKey || e.metaKey || e.shiftKey) {
                            toggleChecked(f.id, e.shiftKey);
                          } else {
                            setCheckedIds([]);
                            onSelectFile(f.id);
                          }
                        }
                      }}
                      onDoubleClick={() => {
                        if (!f.unindexed) onOpenQuickLook();
                      }}
                      onContextMenu={(e) => {
                        if (!f.unindexed) onContextMenuFile(e, f);
                      }}
                    >
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "10px", minWidth: 0 }}>
                          {!f.unindexed && (
                            <input
                              type="checkbox"
                              className="row-checkbox"
                              style={checkedIds.length > 0 ? { opacity: 1 } : undefined}
                              checked={isChecked}
                              aria-label={`Select ${f.name}`}
                              onClick={(e) => {
                                e.stopPropagation();
                                toggleChecked(f.id, e.shiftKey);
                              }}
                              onChange={() => {}}
                            />
                          )}
                          <span className="font-display" style={{ fontSize: "15px", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {f.name}
                          </span>
                          {isCut && (
                            <span style={{ fontSize: "11px", color: "var(--ink-3)", border: "1px dashed var(--rule-strong)", padding: "0 6px", borderRadius: "4px", flexShrink: 0 }}>
                              Cut
                            </span>
                          )}
                          {f.unindexed && (
                            <span style={{ fontSize: "11px", color: "var(--ink-3)", border: "1px dashed var(--rule-strong)", padding: "1px 6px", borderRadius: "4px", flexShrink: 0 }}>
                              {f.supported ? "Click to index" : "Unsupported"}
                            </span>
                          )}
                        </div>
                        <div style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "3px", paddingLeft: f.unindexed ? undefined : "24px" }}>
                          {f.format.toLowerCase()} · {f.size} · {f.date}
                          {f.shelfName ? ` · ${f.shelfName}` : ""}
                        </div>
                      </div>
                      {!f.unindexed ? (
                        <div className="row-action-cue" onClick={(e) => e.stopPropagation()}>
                          <button
                            type="button"
                            className="shelf-header-subtle-btn"
                            title="Actions"
                            aria-label={`More actions for ${f.name}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              onContextMenuFile(e, f);
                            }}
                          >
                            ⋯
                          </button>
                        </div>
                      ) : (
                        <span className="row-action-cue">Index →</span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}
      </div>

      {checkedIds.length > 0 && (
        <div className="bulk-action-bar" role="region" aria-label="Selected files actions">
          <span style={{ fontSize: "12.5px", fontWeight: 600 }}>
            {checkedIds.length} selected
          </span>
          <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "12px", padding: "4px 10px" }}
              onClick={() => onSetClipboard(checkedIds, "copy")}
            >
              Copy
            </button>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "12px", padding: "4px 10px" }}
              onClick={() => onSetClipboard(checkedIds, "cut")}
            >
              Cut
            </button>
            {onBulkAction && (
              <>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ fontSize: "12px", padding: "4px 10px" }}
                  onClick={async () => {
                    const ok = await onBulkAction(checkedIds, "move");
                    if (ok) {
                      setCheckedIds([]);
                      await loadDir(currentPath || undefined);
                    }
                  }}
                >
                  Move…
                </button>
                <button
                  type="button"
                  className="btn-tactile danger"
                  style={{ fontSize: "12px", padding: "4px 10px" }}
                  onClick={async () => {
                    const ok = await onBulkAction(checkedIds, "trash");
                    if (ok) {
                      setCheckedIds([]);
                      await loadDir(currentPath || undefined);
                    }
                  }}
                >
                  Trash
                </button>
              </>
            )}
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "12px", padding: "4px 10px" }}
              onClick={() => setCheckedIds([])}
            >
              Clear
            </button>
          </div>
        </div>
      )}
    </section>
  );
};

export const FoldersView = React.memo(
  FoldersViewImpl,
  (prev, next) =>
    !prev.active &&
    !next.active &&
    (prev.watchedFolders.length > 0 || next.watchedFolders.length === 0)
);
