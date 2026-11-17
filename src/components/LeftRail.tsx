import React from "react";
import {
  ConnectedDevice,
  LookalikePair,
  ShelfFile,
  ShelfGroup,
  WatchedFolder,
} from "../lib/tauri-api";

export type ViewId = "shelves" | "folders" | "find" | "tidy" | "lookalikes" | "trash" | "learns" | "settings";

interface LeftRailProps {
  activeView: ViewId;
  activeShelfFilter: string;
  files: ShelfFile[];
  shelves: ShelfGroup[];
  lookalikes: LookalikePair[];
  watchedFolders: WatchedFolder[];
  connectedDevices?: ConnectedDevice[];
  trashCount?: number;
  pausedOnBattery?: boolean;
  isNightMode: boolean;
  isIndexing: boolean;
  engineStatus?: "connecting" | "ready" | "error";
  onSwitchView: (view: ViewId) => void;
  onJumpToShelf: (shelfId: string) => void;
  onSelectFile?: (fileId: string) => void;
  onScanDevice?: (path: string, addWatch: boolean) => void;
  onImportDevice?: (path: string) => void;
  onCreateFolder?: () => void;
  onCreateSmartShelf?: () => void;
  onToggleTheme: () => void;
}

export const LeftRail: React.FC<LeftRailProps> = ({
  activeView,
  activeShelfFilter,
  files,
  shelves,
  lookalikes,
  watchedFolders,
  connectedDevices = [],
  trashCount = 0,
  pausedOnBattery = false,
  isNightMode,
  isIndexing,
  engineStatus = "ready",
  onSwitchView,
  onJumpToShelf,
  onSelectFile,
  onScanDevice,
  onImportDevice,
  onCreateFolder,
  onCreateSmartShelf,
  onToggleTheme,
}) => {
  const tidyCount = files.filter((f) => Boolean(f.suggestedName) && !f.renamed).length;
  const dupCount = lookalikes.filter((d) => !d.resolved).length;
  const activeWatchedCount = watchedFolders.filter((w) => w.active).length;

  const shelfRangeLabel =
    shelves.length > 0 ? `01–${String(shelves.length).padStart(2, "0")}` : "00";

  return (
    <aside className="left-rail" aria-label="Workspace navigation">
      <div style={{ overflowY: "auto", flex: 1 }}>
        {/* Brand Header */}
        <div className="rail-brand">
          <button
            type="button"
            className="brand-mark"
            onClick={() => onSwitchView("shelves")}
          >
            <span className={`status-dot ${isIndexing ? "pulse" : ""}`}></span>
            <span>good shelves</span>
          </button>
          <span
            style={{
              fontSize: "11px",
              color: "var(--ink-3)",
              fontFamily: "var(--mono)",
            }}
          >
            v1.0
          </span>
        </div>

        {/* Primary Workspace Views */}
        <div className="rail-section-label">workspace</div>
        <nav className="rail-nav" id="primaryRailNav">
          <button
            type="button"
            className={`rail-btn ${activeView === "shelves" ? "active" : ""}`}
            onClick={() => onSwitchView("shelves")}
          >
            <span>shelves</span>
            <span className="rail-badge">{files.length}</span>
          </button>

          <button
            type="button"
            className={`rail-btn ${activeView === "folders" ? "active" : ""}`}
            onClick={() => onSwitchView("folders")}
          >
            <span>folders</span>
            <span className="rail-badge">{activeWatchedCount}</span>
          </button>

          <button
            type="button"
            className={`rail-btn ${activeView === "find" ? "active" : ""}`}
            onClick={() => onSwitchView("find")}
          >
            <span>search &amp; ask</span>
            <span className="rail-badge">/</span>
          </button>

          <button
            type="button"
            className={`rail-btn ${activeView === "tidy" ? "active" : ""}`}
            onClick={() => onSwitchView("tidy")}
          >
            <span>rename</span>
            <span className="rail-badge">{tidyCount}</span>
          </button>

          <button
            type="button"
            className={`rail-btn ${activeView === "lookalikes" ? "active" : ""}`}
            onClick={() => onSwitchView("lookalikes")}
          >
            <span>duplicates</span>
            <span className="rail-badge">{dupCount}</span>
          </button>

          <button
            type="button"
            className={`rail-btn ${activeView === "trash" ? "active" : ""}`}
            onClick={() => onSwitchView("trash")}
          >
            <span>trash &amp; archive</span>
            <span className="rail-badge">{trashCount}</span>
          </button>

          <button
            type="button"
            className={`rail-btn ${activeView === "learns" ? "active" : ""}`}
            onClick={() => onSwitchView("learns")}
          >
            <span>activity</span>
            <span className="rail-badge">{pausedOnBattery ? "battery" : isIndexing ? "active" : "idle"}</span>
          </button>
        </nav>

        {/* Direct HDBSCAN Shelf Jumpers */}
        <div className="rail-section-label" style={{ marginTop: "10px" }}>
          collections
        </div>
        <div className="rail-nav" id="shelfQuickNav">
          <button
            type="button"
            className={`rail-btn ${
              activeView === "shelves" && activeShelfFilter === "all" ? "active" : ""
            }`}
            onClick={() => onJumpToShelf("all")}
          >
            <span>all shelves</span>
            <span style={{ fontSize: "11px", color: "var(--ink-3)" }}>
              {shelfRangeLabel}
            </span>
          </button>

          {shelves.map((shelf) => {
            const count = shelf.isCustom && shelf.fileIds
              ? shelf.fileIds.length
              : files.filter((f) => f.shelfId === shelf.id).length;
            const isSelectedShelf =
              activeView === "shelves" && activeShelfFilter === shelf.id;
            return (
              <button
                key={shelf.id}
                type="button"
                className={`rail-btn ${isSelectedShelf ? "active" : ""}`}
                onClick={() => onJumpToShelf(shelf.id)}
              >
                <span
                  style={{
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    marginRight: "6px",
                  }}
                >
                  {shelf.num} {shelf.name}
                </span>
                <span style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                  {count}
                </span>
              </button>
            );
          })}
          {onCreateSmartShelf && (
            <button
              type="button"
              className="rail-btn"
              style={{ color: "var(--ink-3)", fontSize: "12.5px" }}
              onClick={onCreateSmartShelf}
            >
              <span>+ new smart shelf</span>
            </button>
          )}
        </div>

        {/* Connected Devices (Phones, USB, SD Cards) */}
        {connectedDevices.length > 0 && (
          <>
            <div className="rail-section-label" style={{ marginTop: "10px" }}>
              connected devices
            </div>
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
                    style={{
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={`${dev.path} (${dev.sizeLabel})`}
                  >
                    {dev.kind === "phone" ? "phone · " : ""}
                    {dev.name}
                  </span>
                  <div style={{ display: "flex", gap: "4px", flexShrink: 0 }}>
                    {onImportDevice && (
                      <button
                        type="button"
                        className="btn-tactile solid"
                        style={{ padding: "2px 7px", fontSize: "11px" }}
                        title="Safely copy photos & files from this device into a local folder"
                        onClick={() => onImportDevice(dev.path)}
                      >
                        import
                      </button>
                    )}
                    {onScanDevice && (
                      <button
                        type="button"
                        className="btn-tactile"
                        style={{ padding: "2px 7px", fontSize: "11px" }}
                        onClick={() => onScanDevice(dev.path, true)}
                      >
                        {dev.alreadyWatched ? "rescan" : "scan"}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
        {/* Pinned Files & Shelves */}
        {(files.some((f) => f.pinned) || shelves.some((s) => s.pinned)) && (
          <>
            <div className="rail-section-label" style={{ marginTop: "10px" }}>
              pinned
            </div>
            <div className="rail-nav">
              {shelves
                .filter((s) => s.pinned)
                .map((s) => (
                  <button
                    key={`pin-s-${s.id}`}
                    type="button"
                    className="rail-btn"
                    onClick={() => onJumpToShelf(s.id)}
                  >
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      ★ {s.name}
                    </span>
                  </button>
                ))}
              {files
                .filter((f) => f.pinned)
                .slice(0, 6)
                .map((f) => (
                  <button
                    key={`pin-f-${f.id}`}
                    type="button"
                    className="rail-btn"
                    onClick={() => onSelectFile?.(f.id)}
                  >
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      ★ {f.title || f.name}
                    </span>
                  </button>
                ))}
            </div>
          </>
        )}

        {onCreateFolder && (
          <div style={{ padding: "8px 10px 0" }}>
            <button
              type="button"
              className="btn-tactile"
              style={{ width: "100%", justifyContent: "center", fontSize: "12px" }}
              onClick={onCreateFolder}
            >
              + new folder
            </button>
          </div>
        )}
      </div>

      {/* Bottom Rail Controls */}
      <div
        style={{
          padding: "14px 10px",
          borderTop: "1px solid var(--rule)",
          display: "flex",
          flexDirection: "column",
          gap: "4px",
        }}
      >
        <button
          type="button"
          className={`rail-btn ${activeView === "settings" ? "active" : ""}`}
          onClick={() => onSwitchView("settings")}
        >
          <span>settings &amp; folders</span>
          <span style={{ fontSize: "11px", color: "var(--ink-3)" }}>
            {activeWatchedCount} watched
          </span>
        </button>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "6px 12px",
            fontSize: "12px",
            color: "var(--ink-3)",
          }}
        >
          <span>
            {engineStatus === "connecting"
              ? "starting…"
              : engineStatus === "error"
              ? "engine offline"
              : "local · ready"}
          </span>
          <button
            type="button"
            onClick={onToggleTheme}
            style={{ textDecoration: "underline", color: "var(--ink-2)" }}
          >
            {isNightMode ? "light" : "dark"}
          </button>
        </div>
      </div>
    </aside>
  );
};
