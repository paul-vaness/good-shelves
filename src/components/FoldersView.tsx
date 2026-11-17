import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowUp,
  ChevronRight,
  Folder,
  FolderPlus,
  LayoutGrid,
  List,
  Pencil,
  Trash2,
  ClipboardPaste,
  RefreshCw,
} from "lucide-react";
import {
  FolderBrowseResponse,
  ShelfFile,
  WatchedFolder,
  browseFolder,
  deleteFolderOnDisk,
  ingestFilePath,
  pasteFilesToFolder,
  renameFolderOnDisk,
  resolveLocalAssetSrc,
} from "../lib/tauri-api";

interface FoldersViewProps {
  active: boolean;
  watchedFolders: WatchedFolder[];
  selectedFileId: string;
  clipboard: { fileIds: string[]; mode: "copy" | "cut" } | null;
  onClearClipboard: () => void;
  onSelectFile: (fileId: string) => void;
  onOpenQuickLook: () => void;
  onCreateFolder: () => void;
  onContextMenuFile: (e: React.MouseEvent, file: ShelfFile) => void;
  onRefreshState: (state: any) => void;
  onShowToast: (msg: string) => void;
}

type SortKey = "name" | "date" | "size" | "format";

export const FoldersView: React.FC<FoldersViewProps> = ({
  active,
  watchedFolders,
  selectedFileId,
  clipboard,
  onClearClipboard,
  onSelectFile,
  onOpenQuickLook,
  onCreateFolder,
  onContextMenuFile,
  onRefreshState,
  onShowToast,
}) => {
  const [currentPath, setCurrentPath] = useState<string>("");
  const [data, setData] = useState<FolderBrowseResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"list" | "grid">("list");
  const [sortBy, setSortBy] = useState<SortKey>("name");
  const [sortAsc, setSortAsc] = useState<boolean>(true);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renameFolderDraft, setRenameFolderDraft] = useState<string>("");

  const loadDir = useCallback(async (target?: string) => {
    setLoading(true);
    setError(null);
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
    if (active && !data) {
      loadDir(watchedFolders.find((w) => w.active)?.path || watchedFolders[0]?.path);
    }
  }, [active, data, loadDir, watchedFolders]);

  const sortedFiles = useMemo(() => {
    const list = [...(data?.files || [])];
    list.sort((a, b) => {
      let cmp = 0;
      if (sortBy === "name") cmp = a.name.localeCompare(b.name);
      else if (sortBy === "date") cmp = a.mtime - b.mtime;
      else if (sortBy === "size") cmp = a.sizeBytes - b.sizeBytes;
      else if (sortBy === "format") cmp = a.format.localeCompare(b.format);
      return sortAsc ? cmp : -cmp;
    });
    return list;
  }, [data?.files, sortBy, sortAsc]);

  const handlePaste = async () => {
    if (!clipboard || !data?.path) return;
    try {
      const mode = clipboard.mode === "cut" ? "move" : "copy";
      const res = await pasteFilesToFolder(clipboard.fileIds, data.path, mode);
      onRefreshState(res.state);
      if (clipboard.mode === "cut") onClearClipboard();
      await loadDir(data.path);
      onShowToast(
        `${clipboard.mode === "cut" ? "Moved" : "Copied"} ${res.count} file(s) into ${data.displayPath}`
      );
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Paste failed");
    }
  };

  const handleRenameFolder = async (folderPath: string) => {
    const clean = renameFolderDraft.trim();
    if (!clean) return;
    try {
      const res = await renameFolderOnDisk(folderPath, clean);
      onRefreshState(res.state);
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
      await loadDir(data?.path);
      onShowToast(`Moved folder “${folderName}” to GoodShelves_Trash`);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not trash folder");
    }
  };

  return (
    <section className={`view-sheet ${active ? "active" : ""}`} id="sheet-folders">
      <div style={{ maxWidth: "880px", margin: "0 auto" }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "flex-end",
            justifyContent: "space-between",
            gap: "14px",
            paddingBottom: "18px",
            borderBottom: "1.5px solid var(--ink)",
            marginBottom: "16px",
          }}
        >
          <div>
            <div style={{ fontSize: "11.5px", fontWeight: 500, letterSpacing: "0.06em", color: "var(--ink-3)" }}>
              directory navigator
            </div>
            <h1
              className="font-display"
              style={{ fontSize: "clamp(30px, 4vw, 44px)", fontWeight: 300, letterSpacing: "-0.03em", lineHeight: 1.02 }}
            >
              folders
            </h1>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            {clipboard && clipboard.fileIds.length > 0 && (
              <button type="button" className="btn-tactile solid" onClick={handlePaste}>
                <ClipboardPaste size={13} />
                <span>
                  paste {clipboard.fileIds.length} ({clipboard.mode === "cut" ? "move" : "copy"})
                </span>
              </button>
            )}
            <button type="button" className="btn-tactile" onClick={onCreateFolder}>
              <FolderPlus size={13} />
              <span>new folder</span>
            </button>
            <button type="button" className="btn-tactile" onClick={() => loadDir(currentPath || undefined)}>
              <RefreshCw size={13} />
            </button>
            <div className="seg-group" role="tablist">
              <button
                type="button"
                className={`seg-btn ${viewMode === "list" ? "active" : ""}`}
                onClick={() => setViewMode("list")}
                aria-label="List view"
              >
                <List size={13} />
              </button>
              <button
                type="button"
                className={`seg-btn ${viewMode === "grid" ? "active" : ""}`}
                onClick={() => setViewMode("grid")}
                aria-label="Grid view"
              >
                <LayoutGrid size={13} />
              </button>
            </div>
          </div>
        </div>

        {/* Watched Root Shortcuts */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "12px" }}>
          {watchedFolders.map((wf) => (
            <button
              key={wf.path}
              type="button"
              className={`btn-tactile ${currentPath === wf.path ? "active-tab" : ""}`}
              style={{ fontSize: "11.5px", padding: "4px 10px" }}
              onClick={() => loadDir(wf.path)}
            >
              {wf.display_path}
            </button>
          ))}
        </div>

        {/* Breadcrumbs & Sort Controls */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "10px",
            padding: "8px 12px",
            borderRadius: "7px",
            border: "1px solid var(--rule)",
            background: "var(--bg-subtle)",
            marginBottom: "16px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "4px", flexWrap: "wrap", minWidth: 0 }}>
            {data?.parentPath && (
              <button
                type="button"
                className="row-icon-btn"
                title="Go to parent folder"
                aria-label="Parent folder"
                onClick={() => loadDir(data.parentPath!)}
              >
                <ArrowUp size={13} />
              </button>
            )}
            {data?.breadcrumbs.slice(-5).map((bc, idx, arr) => (
              <React.Fragment key={bc.path}>
                <button
                  type="button"
                  className="font-mono-code"
                  style={{
                    fontSize: "12px",
                    padding: "2px 6px",
                    borderRadius: "4px",
                    fontWeight: idx === arr.length - 1 ? 600 : 400,
                    color: idx === arr.length - 1 ? "var(--ink)" : "var(--ink-2)",
                  }}
                  onClick={() => loadDir(bc.path)}
                >
                  {bc.name}
                </button>
                {idx < arr.length - 1 && <ChevronRight size={12} style={{ color: "var(--ink-3)" }} />}
              </React.Fragment>
            ))}
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "11.5px" }}>
            <span style={{ color: "var(--ink-3)" }}>sort:</span>
            {(["name", "date", "size", "format"] as SortKey[]).map((key) => (
              <button
                key={key}
                type="button"
                className={`seg-btn ${sortBy === key ? "active" : ""}`}
                style={{ padding: "3px 8px", fontSize: "11px" }}
                onClick={() => {
                  if (sortBy === key) setSortAsc((a) => !a);
                  else {
                    setSortBy(key);
                    setSortAsc(key === "name" || key === "format");
                  }
                }}
              >
                {key} {sortBy === key ? (sortAsc ? "↑" : "↓") : ""}
              </button>
            ))}
          </div>
        </div>

        {error && <div className="inline-error-banner" role="alert" style={{ marginBottom: "14px" }}>{error}</div>}

        {loading && !data ? (
          <div style={{ padding: "24px", color: "var(--ink-3)", fontSize: "13.5px" }}>Loading folder…</div>
        ) : (
          <>
            {/* Subfolders */}
            {data && data.folders.length > 0 && (
              <div style={{ marginBottom: "18px" }}>
                <div style={{ fontSize: "11px", fontWeight: 600, letterSpacing: "0.05em", color: "var(--ink-3)", marginBottom: "8px" }}>
                  FOLDERS ({data.folders.length})
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(210px, 1fr))", gap: "8px" }}>
                  {data.folders.map((sub) => (
                    <div
                      key={sub.path}
                      className="folder-choice-card"
                      style={{ padding: "9px 11px", cursor: "pointer" }}
                      onClick={() => {
                        if (renamingPath !== sub.path) loadDir(sub.path);
                      }}
                    >
                      {renamingPath === sub.path ? (
                        <form
                          style={{ display: "flex", gap: "4px", width: "100%" }}
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
                            className="dialog-input"
                            style={{ padding: "3px 6px", fontSize: "12px" }}
                          />
                          <button type="submit" className="btn-tactile solid" style={{ padding: "2px 7px", fontSize: "11px" }}>
                            ok
                          </button>
                        </form>
                      ) : (
                        <>
                          <div style={{ display: "flex", alignItems: "center", gap: "8px", minWidth: 0 }}>
                            <Folder size={15} style={{ flexShrink: 0, color: "var(--ink-2)" }} />
                            <div style={{ minWidth: 0 }}>
                              <div style={{ fontSize: "13px", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                {sub.name}
                              </div>
                              <div style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                                {sub.itemCount} items{sub.watched ? " · watched" : ""}
                              </div>
                            </div>
                          </div>
                          <div style={{ display: "flex", gap: "2px" }} onClick={(e) => e.stopPropagation()}>
                            <button
                              type="button"
                              className="row-icon-btn"
                              title="Rename folder"
                              onClick={() => {
                                setRenamingPath(sub.path);
                                setRenameFolderDraft(sub.name);
                              }}
                            >
                              <Pencil size={12} />
                            </button>
                            <button
                              type="button"
                              className="row-icon-btn danger"
                              title="Trash folder"
                              onClick={() => handleDeleteFolder(sub.path, sub.name)}
                            >
                              <Trash2 size={12} />
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Files */}
            <div style={{ fontSize: "11px", fontWeight: 600, letterSpacing: "0.05em", color: "var(--ink-3)", marginBottom: "8px" }}>
              FILES ({sortedFiles.length})
            </div>

            {sortedFiles.length === 0 ? (
              <div className="shelf-section" style={{ padding: "24px 20px", fontSize: "13.5px", color: "var(--ink-3)" }}>
                No files in this directory.
              </div>
            ) : viewMode === "grid" ? (
              <div className="file-grid-wrap">
                {sortedFiles.map((f) => {
                  const thumb = resolveLocalAssetSrc(f);
                  const isSel = f.id === selectedFileId;
                  return (
                    <div
                      key={f.id}
                      className={`file-grid-card ${isSel ? "selected" : ""}`}
                      onClick={async () => {
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
                        if (!f.unindexed) onSelectFile(f.id);
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
                          <img src={thumb} alt={f.title} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                        ) : (
                          <span className="file-type-tag">{f.format}</span>
                        )}
                      </div>
                      <div style={{ padding: "10px 12px" }}>
                        <div style={{ fontSize: "13.5px", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {f.title}
                        </div>
                        <div className="font-mono-code" style={{ fontSize: "11px", color: "var(--ink-3)", marginTop: "2px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {f.size} · {f.date}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="shelf-section">
                {sortedFiles.map((f) => {
                  const isSel = f.id === selectedFileId;
                  return (
                    <div
                      key={f.id}
                      className={`file-item-row ${isSel ? "selected" : ""}`}
                      onClick={async () => {
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
                        if (!f.unindexed) onSelectFile(f.id);
                      }}
                      onDoubleClick={() => {
                        if (!f.unindexed) onOpenQuickLook();
                      }}
                      onContextMenu={(e) => {
                        if (!f.unindexed) onContextMenuFile(e, f);
                      }}
                    >
                      <div style={{ minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                          <span className="file-type-tag">{f.format}</span>
                          <span className="font-display" style={{ fontSize: "16.5px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {f.name}
                          </span>
                          {f.unindexed && (
                            <span style={{ fontSize: "11px", color: "var(--ink-3)", border: "1px dashed var(--rule-strong)", padding: "1px 6px", borderRadius: "4px" }}>
                              {f.supported ? "click to index" : "unsupported"}
                            </span>
                          )}
                        </div>
                        <div className="font-mono-code" style={{ fontSize: "11.5px", color: "var(--ink-3)", marginTop: "3px" }}>
                          {f.size} · {f.date} · {f.shelfName}
                        </div>
                      </div>
                      <span className="row-action-cue">{f.unindexed ? "index →" : "inspect →"}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
};
