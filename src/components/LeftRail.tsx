import React, { useState } from "react";
import { X, ChevronDown, ChevronRight, EyeOff, Folder, Sparkles } from "lucide-react";
import {
  ConnectedDevice,
  LookalikePair,
  PinnedFolder,
  ShelfFile,
  ShelfGroup,
  WatchedFolder,
} from "../lib/tauri-api";

export type ViewId = "shelves" | "folders" | "find" | "tidy" | "lookalikes" | "trash" | "learns" | "settings";

interface LeftRailProps {
  activeView: ViewId;
  activeShelfFilter: string;
  userName?: string;
  hiddenShelfIds?: string[];
  files: ShelfFile[];
  shelves: ShelfGroup[];
  lookalikes: LookalikePair[];
  watchedFolders: WatchedFolder[];
  pinnedFolders?: PinnedFolder[];
  connectedDevices?: ConnectedDevice[];
  trashCount?: number;
  pausedOnBattery?: boolean;
  isNightMode: boolean;
  isIndexing: boolean;
  engineStatus?: "connecting" | "ready" | "error";
  sidebarHidden?: boolean;
  mobileOpen?: boolean;
  onCloseMobile?: () => void;
  onSwitchView: (view: ViewId) => void;
  onJumpToShelf: (shelfId: string, subShelf?: string) => void;
  onToggleHideShelf?: (shelfId: string) => void;
  onRestoreAllHiddenShelves?: () => void;
  onSelectFile?: (fileId: string) => void;
  onOpenPinnedFolder?: (path: string) => void;
  onAskFolder?: (path: string) => void;
  onUnpinFolder?: (path: string) => void;
  onScanDevice?: (path: string, addWatch: boolean) => void;
  onImportDevice?: (path: string) => void;
  onCreateFolder?: () => void;
  onCreateSmartShelf?: () => void;
  onToggleTheme: () => void;
}

export const LeftRail: React.FC<LeftRailProps> = React.memo(({
  activeView,
  activeShelfFilter,
  hiddenShelfIds = [],
  files,
  shelves,
  lookalikes,
  watchedFolders,
  pinnedFolders = [],
  connectedDevices = [],
  trashCount = 0,
  pausedOnBattery = false,
  isNightMode,
  isIndexing,
  engineStatus = "ready",
  sidebarHidden = false,
  mobileOpen = false,
  onCloseMobile,
  onSwitchView,
  onJumpToShelf,
  onToggleHideShelf,
  onRestoreAllHiddenShelves,
  onSelectFile,
  onOpenPinnedFolder,
  onAskFolder,
  onUnpinFolder,
  onScanDevice,
  onImportDevice,
  onCreateSmartShelf,
  onToggleTheme,
}) => {
  const [expandedRailShelves, setExpandedRailShelves] = useState<Record<string, boolean>>({});
  const [collectionsOpen, setCollectionsOpen] = useState<boolean>(false);
  const [pinnedOpen, setPinnedOpen] = useState<boolean>(true);

  const hiddenSet = React.useMemo(() => new Set(hiddenShelfIds), [hiddenShelfIds]);

  const { tidyCount, dupCount, activeWatchedCount, shelfCounts, subShelvesByShelf, pinnedShelves, pinnedFiles } = React.useMemo(() => {
    let tidy = 0;
    const counts = new Map<string, number>();
    const subMap = new Map<string, Array<{ name: string; count: number }>>();
    const subTemp = new Map<string, Map<string, number>>();
    const pFiles: ShelfFile[] = [];
    for (const f of files) {
      if (f.suggestedName && !f.renamed) tidy += 1;
      counts.set(f.shelfId, (counts.get(f.shelfId) || 0) + 1);
      if (f.subShelf) {
        let b = subTemp.get(f.shelfId);
        if (!b) {
          b = new Map<string, number>();
          subTemp.set(f.shelfId, b);
        }
        b.set(f.subShelf, (b.get(f.subShelf) || 0) + 1);
      }
      if (f.pinned && pFiles.length < 6) pFiles.push(f);
    }
    for (const sh of shelves) {
      if (sh.subShelves && sh.subShelves.length > 0) {
        subMap.set(sh.id, sh.subShelves);
      } else {
        const b = subTemp.get(sh.id);
        if (b && b.size > 0) {
          subMap.set(
            sh.id,
            Array.from(b.entries())
              .map(([name, count]) => ({ name, count }))
              .sort((a, b2) => b2.count - a.count)
          );
        }
      }
    }
    return {
      tidyCount: tidy,
      dupCount: lookalikes.filter((d) => !d.resolved).length,
      activeWatchedCount: watchedFolders.filter((w) => w.active).length,
      shelfCounts: counts,
      subShelvesByShelf: subMap,
      pinnedShelves: shelves.filter((s) => s.pinned),
      pinnedFiles: pFiles,
    };
  }, [files, lookalikes, watchedFolders, shelves]);

  const handleNavSwitch = (view: ViewId) => {
    onSwitchView(view);
    onCloseMobile?.();
  };

  const handleShelfJump = (shelfId: string, subShelf?: string) => {
    onJumpToShelf(shelfId, subShelf);
    onCloseMobile?.();
  };

  const visibleShelfCount = shelves.filter((s) => !hiddenSet.has(s.id)).length;
  const statusClass =
    engineStatus === "error"
      ? "error"
      : isIndexing
      ? "pulse"
      : pausedOnBattery
      ? "warning"
      : "";

  return (
    <>
      <div
        className={`rail-backdrop ${mobileOpen ? "open" : ""}`}
        onClick={onCloseMobile}
        aria-hidden="true"
      />
      <aside
        className={`left-rail ${sidebarHidden ? "desktop-hidden" : ""} ${mobileOpen ? "mobile-open" : ""}`}
        aria-label="Workspace navigation"
        aria-hidden={sidebarHidden ? true : undefined}
      >
        <div style={{ overflowY: "auto", flex: 1, paddingBottom: "16px" }}>
          {/* Title Card: strictly good shelves */}
          <div className="rail-brand" data-tauri-drag-region>
            <button
              type="button"
              className="brand-mark"
              onClick={() => handleNavSwitch("shelves")}
              title="good shelves"
            >
              <span className={`status-dot ${statusClass}`} aria-hidden="true" />
              <span>good shelves</span>
            </button>
            {onCloseMobile && (
              <button
                type="button"
                className="row-icon-btn mobile-rail-toggle"
                onClick={onCloseMobile}
                aria-label="Close navigation menu"
              >
                <X size={15} />
              </button>
            )}
          </div>

          {/* Primary Browse Views */}
          <div className="rail-section-label">Workspace</div>
          <nav className="rail-nav" id="primaryRailNav" aria-label="Primary views">
            <button
              type="button"
              className={`rail-btn ${activeView === "shelves" ? "active" : ""}`}
              aria-current={activeView === "shelves" ? "page" : undefined}
              onClick={() => handleNavSwitch("shelves")}
            >
              <span>Shelves</span>
              <span className="rail-badge">{files.length}</span>
            </button>

            <button
              type="button"
              className={`rail-btn ${activeView === "folders" ? "active" : ""}`}
              aria-current={activeView === "folders" ? "page" : undefined}
              onClick={() => handleNavSwitch("folders")}
            >
              <span>Folders</span>
              <span className="rail-badge">{activeWatchedCount}</span>
            </button>

            <button
              type="button"
              className={`rail-btn ${activeView === "find" ? "active" : ""}`}
              aria-current={activeView === "find" ? "page" : undefined}
              onClick={() => handleNavSwitch("find")}
            >
              <span>Search &amp; Ask</span>
            </button>
          </nav>

          {/* Unified Cleanup Link */}
          <div className="rail-section-label">Organize</div>
          <nav className="rail-nav" aria-label="Organization tools">
            <button
              type="button"
              className={`rail-btn ${(activeView === "tidy" || activeView === "lookalikes" || activeView === "trash") ? "active" : ""}`}
              aria-current={(activeView === "tidy" || activeView === "lookalikes" || activeView === "trash") ? "page" : undefined}
              onClick={() => handleNavSwitch("tidy")}
            >
              <span>Clean &amp; Trash</span>
              {(tidyCount + dupCount + trashCount) > 0 && (
                <span className="rail-badge">{tidyCount + dupCount + trashCount}</span>
              )}
            </button>
          </nav>

          {/* Collapsible Shelves */}
          <button
            type="button"
            className="rail-section-label"
            aria-expanded={collectionsOpen}
            onClick={() => setCollectionsOpen((o) => !o)}
            style={{
              width: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              cursor: "pointer",
            }}
          >
            <span>Shelves ({visibleShelfCount})</span>
            {collectionsOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </button>

          {collectionsOpen && (
            <div className="rail-nav" id="shelfQuickNav">
              <button
                type="button"
                className={`rail-btn ${
                  activeView === "shelves" && activeShelfFilter === "all" ? "active" : ""
                }`}
                onClick={() => handleShelfJump("all")}
              >
                <span>All shelves</span>
                <span className="rail-badge">{visibleShelfCount}</span>
              </button>

              {shelves
                .filter((shelf) => !hiddenSet.has(shelf.id))
                .map((shelf) => {
                  const count =
                    shelf.isCustom && shelf.fileIds
                      ? shelf.fileIds.length
                      : shelfCounts.get(shelf.id) || 0;
                  const isSelectedShelf =
                    activeView === "shelves" && activeShelfFilter === shelf.id;
                  const subList = subShelvesByShelf.get(shelf.id) || [];
                  const isExpanded = Boolean(expandedRailShelves[shelf.id]);
                  return (
                    <div key={shelf.id}>
                      <div className="rail-shelf-row">
                        {subList.length > 0 && (
                          <button
                            type="button"
                            aria-label={isExpanded ? `Collapse ${shelf.name}` : `Expand ${shelf.name}`}
                            aria-expanded={isExpanded}
                            onClick={() =>
                              setExpandedRailShelves((prev) => ({
                                ...prev,
                                [shelf.id]: !prev[shelf.id],
                              }))
                            }
                            style={{
                              width: "18px",
                              height: "24px",
                              display: "inline-flex",
                              alignItems: "center",
                              justifyContent: "center",
                              color: "var(--ink-3)",
                              flexShrink: 0,
                              borderRadius: "4px",
                            }}
                          >
                            {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                          </button>
                        )}
                        <button
                          type="button"
                          className={`rail-btn ${isSelectedShelf ? "active" : ""}`}
                          style={{
                            ...(subList.length === 0 ? {} : { paddingLeft: "6px" }),
                            flex: 1,
                            minWidth: 0,
                          }}
                          onClick={() => handleShelfJump(shelf.id)}
                        >
                          <span
                            style={{
                              whiteSpace: "nowrap",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              marginRight: "6px",
                            }}
                          >
                            {shelf.name}
                          </span>
                          <span className="rail-badge">{count}</span>
                        </button>
                        {onToggleHideShelf && (
                          <button
                            type="button"
                            className="rail-shelf-hide"
                            aria-label={`Hide ${shelf.name} from shelves`}
                            title={`Hide ${shelf.name}`}
                            onClick={() => onToggleHideShelf(shelf.id)}
                          >
                            <EyeOff size={12} />
                          </button>
                        )}
                      </div>
                      {isExpanded && subList.length > 0 && (
                        <div
                          style={{
                            paddingLeft: "22px",
                            display: "flex",
                            flexDirection: "column",
                            gap: "1px",
                            marginTop: "2px",
                            marginBottom: "4px",
                          }}
                        >
                          {subList.map((sub) => (
                            <button
                              key={`${shelf.id}-${sub.name}`}
                              type="button"
                              className="rail-btn"
                              style={{ padding: "4px 10px", fontSize: "12px", color: "var(--ink-3)" }}
                              onClick={() => handleShelfJump(shelf.id, sub.name)}
                            >
                              <span
                                style={{
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                }}
                              >
                                {sub.name}
                              </span>
                              <span style={{ fontSize: "10.5px" }}>{sub.count}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}

              {hiddenSet.size > 0 && (
                <button
                  type="button"
                  className="rail-btn"
                  style={{ color: "var(--ink-3)", fontSize: "12px" }}
                  onClick={() => {
                    if (onRestoreAllHiddenShelves) {
                      onRestoreAllHiddenShelves();
                    } else if (onToggleHideShelf) {
                      for (const id of hiddenSet) onToggleHideShelf(id);
                    }
                  }}
                >
                  <span>{hiddenSet.size} hidden · Restore</span>
                </button>
              )}

              {onCreateSmartShelf && (
                <button
                  type="button"
                  className="rail-btn"
                  style={{ color: "var(--ink-3)", fontSize: "12.5px" }}
                  onClick={() => {
                    onCreateSmartShelf();
                    onCloseMobile?.();
                  }}
                >
                  <span>+ New shelf</span>
                </button>
              )}
            </div>
          )}

          {/* Connected Devices */}
          {connectedDevices.length > 0 && (
            <>
              <div className="rail-section-label">Devices</div>
              <div className="rail-nav">
                {connectedDevices.map((dev) => (
                  <div
                    key={dev.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      padding: "6px 12px",
                      borderRadius: "6px",
                      fontSize: "12.5px",
                      color: "var(--ink-2)",
                      gap: "6px",
                    }}
                  >
                    <span
                      style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      title={`${dev.path} (${dev.sizeLabel})`}
                    >
                      {dev.name}
                    </span>
                    <div style={{ display: "flex", gap: "4px", flexShrink: 0 }}>
                      {onImportDevice && (
                        <button
                          type="button"
                          className="btn-tactile solid"
                          style={{ padding: "2px 7px", minHeight: "24px", fontSize: "11px" }}
                          onClick={() => onImportDevice(dev.path)}
                        >
                          Import
                        </button>
                      )}
                      {onScanDevice && (
                        <button
                          type="button"
                          className="btn-tactile"
                          style={{ padding: "2px 7px", minHeight: "24px", fontSize: "11px" }}
                          onClick={() => onScanDevice(dev.path, true)}
                        >
                          {dev.alreadyWatched ? "Rescan" : "Scan"}
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          {/* Pinned */}
          {(pinnedFolders.length > 0 || pinnedFiles.length > 0 || pinnedShelves.length > 0) && (
            <>
              <button
                type="button"
                className="rail-section-label"
                aria-expanded={pinnedOpen}
                onClick={() => setPinnedOpen((o) => !o)}
                style={{
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  cursor: "pointer",
                }}
              >
                <span>Pinned ({pinnedFolders.length + pinnedShelves.length + pinnedFiles.length})</span>
                {pinnedOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              </button>
              {pinnedOpen && (
                <div className="rail-nav">
                  {pinnedFolders.map((pf) => (
                    <div key={`pin-dir-${pf.path}`} className="rail-shelf-row">
                      <button
                        type="button"
                        className="rail-btn"
                        style={{ flex: 1, minWidth: 0, gap: "7px" }}
                        title={pf.displayPath}
                        onClick={() => {
                          onOpenPinnedFolder?.(pf.path);
                          onCloseMobile?.();
                        }}
                      >
                        <Folder size={13} style={{ flexShrink: 0, color: "var(--ink-3)" }} aria-hidden="true" />
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {pf.name}
                        </span>
                      </button>
                      {onAskFolder && (
                        <button
                          type="button"
                          className="rail-shelf-hide"
                          title={`Ask AI about ${pf.name}`}
                          aria-label={`Ask AI about ${pf.name}`}
                          onClick={() => {
                            onAskFolder(pf.path);
                            onCloseMobile?.();
                          }}
                        >
                          <Sparkles size={11} />
                        </button>
                      )}
                      {onUnpinFolder && (
                        <button
                          type="button"
                          className="rail-shelf-hide"
                          title={`Unpin ${pf.name}`}
                          aria-label={`Unpin ${pf.name}`}
                          onClick={() => onUnpinFolder(pf.path)}
                        >
                          <X size={11} />
                        </button>
                      )}
                    </div>
                  ))}
                  {pinnedShelves.map((s) => (
                    <button
                      key={`pin-s-${s.id}`}
                      type="button"
                      className="rail-btn"
                      onClick={() => handleShelfJump(s.id)}
                    >
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {s.name}
                      </span>
                    </button>
                  ))}
                  {pinnedFiles.map((f) => (
                    <button
                      key={`pin-f-${f.id}`}
                      type="button"
                      className="rail-btn"
                      onClick={() => {
                        onSelectFile?.(f.id);
                        onCloseMobile?.();
                      }}
                    >
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {f.title || f.name}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        {/* Bottom Rail Controls */}
        <div
          style={{
            padding: "12px",
            borderTop: "1px solid var(--rule)",
            display: "flex",
            flexDirection: "column",
            gap: "3px",
          }}
        >
          <button
            type="button"
            className={`rail-btn ${activeView === "learns" ? "active" : ""}`}
            aria-current={activeView === "learns" ? "page" : undefined}
            onClick={() => handleNavSwitch("learns")}
          >
            <span>Activity &amp; Undo</span>
            {(pausedOnBattery || isIndexing) && (
              <span className="rail-badge">{pausedOnBattery ? "Paused" : "Active"}</span>
            )}
          </button>

          <button
            type="button"
            className={`rail-btn ${activeView === "settings" ? "active" : ""}`}
            aria-current={activeView === "settings" ? "page" : undefined}
            onClick={() => handleNavSwitch("settings")}
          >
            <span>Settings</span>
          </button>

          <button
            type="button"
            role="switch"
            aria-checked={isNightMode}
            aria-label={isNightMode ? "Switch to light mode" : "Switch to dark mode"}
            className="theme-toggle-row"
            onClick={onToggleTheme}
            style={{ marginTop: "4px" }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "8px", minWidth: 0 }}>
              <span className={`theme-status-dot ${isNightMode ? "dark" : ""}`} aria-hidden="true" />
              <span style={{ fontSize: "12.5px", fontWeight: 500 }}>
                {isNightMode ? "Dark mode" : "Light mode"}
              </span>
            </div>
            <span className={`theme-switch-track ${isNightMode ? "dark" : ""}`} aria-hidden="true">
              <span className="theme-switch-thumb" />
            </span>
          </button>
        </div>
      </aside>
    </>
  );
});
