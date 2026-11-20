import React, { useRef, useEffect, useState } from "react";
import { Menu, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Search, X } from "lucide-react";
import { ViewId } from "./LeftRail";
import { formatShortcut } from "../lib/platform";

interface TopMemoryBarProps {
  query: string;
  activeView: ViewId;
  sidebarVisible?: boolean;
  inspectorVisible: boolean;
  cleanupCount?: number;
  liveProgress?: { operation: string; current: number; total: number; file_name: string } | null;
  onQueryChange: (val: string) => void;
  onClearQuery: () => void;
  onSwitchView: (view: ViewId) => void;
  onOpenAskMode: () => void;
  onOpenSmartClean: () => void;
  onOpenCommandPalette: () => void;
  onToggleSidebar?: () => void;
  onToggleInspector: () => void;
  onToggleMobileRail?: () => void;
  inputRef?: React.RefObject<HTMLInputElement | null>;
}

export const TopMemoryBar: React.FC<TopMemoryBarProps> = ({
  query,
  activeView,
  sidebarVisible = true,
  inspectorVisible,
  cleanupCount = 0,
  onQueryChange,
  onClearQuery,
  onSwitchView,
  onOpenAskMode,
  onOpenSmartClean,
  onOpenCommandPalette,
  onToggleSidebar,
  onToggleInspector,
  onToggleMobileRail,
  inputRef,
}) => {
  const localRef = useRef<HTMLInputElement | null>(null);
  const effectiveRef = inputRef || localRef;
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const shortcutsRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const isInput =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable);
      if (e.key === "Escape" && shortcutsOpen) {
        e.preventDefault();
        e.stopImmediatePropagation();
        setShortcutsOpen(false);
        return;
      }
      if (!isInput && e.key === "/") {
        e.preventDefault();
        if (activeView === "find") {
          const findInput = document.getElementById("findSearchInput") as HTMLInputElement | null;
          findInput?.focus();
          findInput?.select();
        } else {
          effectiveRef.current?.focus();
        }
      }
    };
    const onPointerDown = (e: MouseEvent) => {
      if (shortcutsOpen && shortcutsRef.current && !shortcutsRef.current.contains(e.target as Node)) {
        setShortcutsOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("mousedown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("mousedown", onPointerDown);
    };
  }, [activeView, effectiveRef, shortcutsOpen]);

  return (
    <header className="stage-command-bar">
      <div style={{ display: "flex", alignItems: "center", gap: "8px", flex: 1, minWidth: 0 }}>
        {onToggleSidebar && (
          <button
            type="button"
            className="btn-tactile desktop-rail-toggle"
            style={{ padding: "6px 8px" }}
            onClick={onToggleSidebar}
            title={sidebarVisible ? "Hide sidebar" : "Show sidebar"}
            aria-label={sidebarVisible ? "Hide sidebar" : "Show sidebar"}
            aria-pressed={!sidebarVisible}
          >
            {sidebarVisible ? (
              <PanelLeftClose size={15} aria-hidden="true" />
            ) : (
              <PanelLeftOpen size={15} aria-hidden="true" />
            )}
          </button>
        )}
        {onToggleMobileRail && (
          <button
            type="button"
            className="btn-tactile mobile-rail-toggle"
            style={{ padding: "6px 8px" }}
            onClick={onToggleMobileRail}
            aria-label="Open workspace navigation"
          >
            <Menu size={15} />
          </button>
        )}

        {activeView === "find" ? (
          <div style={{ display: "flex", alignItems: "center", gap: "8px", minWidth: 0 }}>
            <button
              type="button"
              className="btn-tactile"
              onClick={() => onSwitchView("shelves")}
            >
              ← Shelves
            </button>
          </div>
        ) : (
          <div className="quick-memory-box">
            <Search size={15} strokeWidth={1.7} style={{ color: "var(--ink-3)", flexShrink: 0 }} aria-hidden="true" />
            <input
              ref={effectiveRef}
              id="globalMemoryInput"
              type="text"
              autoComplete="off"
              spellCheck={false}
              aria-label="Search files by content, name, or topic"
              value={query}
              placeholder="Search files… (/ or Cmd/Ctrl+K)"
              style={{ width: "100%", minWidth: 0, fontSize: "13.5px", color: "var(--ink)" }}
              onChange={(e) => onQueryChange(e.target.value)}
            />
            {query.trim().length > 0 && (
              <button
                type="button"
                onClick={onClearQuery}
                className="row-icon-btn"
                style={{ width: "22px", height: "22px", border: 0 }}
                title="Clear search"
                aria-label="Clear search"
              >
                <X size={13} />
              </button>
            )}
            <button
              type="button"
              className="btn-tactile top-bar-ask-btn"
              style={{ padding: "2px 9px", minHeight: "24px", fontSize: "12px", borderRadius: "5px", flexShrink: 0 }}
              onClick={onOpenAskMode}
            >
              Ask AI
            </button>
          </div>
        )}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 }}>
        <button
          type="button"
          className="btn-tactile"
          onClick={onOpenSmartClean}
          title="Review renames, duplicates, and folder organization"
        >
          <span>Clean up</span>
          {cleanupCount > 0 && <span className="rail-badge">{cleanupCount}</span>}
        </button>

        <div ref={shortcutsRef} style={{ position: "relative" }}>
          <button
            type="button"
            className="btn-tactile"
            style={{ padding: "5px 10px", fontWeight: 600 }}
            onClick={() => setShortcutsOpen((o) => !o)}
            title="Keyboard shortcuts"
            aria-label="Keyboard shortcuts"
            aria-expanded={shortcutsOpen}
            aria-haspopup="true"
          >
            ?
          </button>
          {shortcutsOpen && (
            <div className="popover-menu" role="dialog" aria-label="Keyboard shortcuts">
              <div style={{ fontWeight: 600, marginBottom: "10px", color: "var(--ink)", fontSize: "13px" }}>
                Keyboard shortcuts
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: "7px", color: "var(--ink-2)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>Focus search / Filter</span>
                  <span className="kbd-pill">/ · {formatShortcut("F")}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>Quick jump</span>
                  <span className="kbd-pill">{formatShortcut("K")}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>Select all</span>
                  <span className="kbd-pill">{formatShortcut("A")}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>Copy / Cut / Paste</span>
                  <span className="kbd-pill">{formatShortcut("C")} · X · V</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>Preview (or double-click)</span>
                  <span className="kbd-pill">Space / Enter</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>Rename / Move / Trash</span>
                  <span className="kbd-pill">F2 · M · Del</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>New folder (Folders)</span>
                  <span className="kbd-pill">{formatShortcut("N", { shift: true })}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>Undo / Redo</span>
                  <span className="kbd-pill">{formatShortcut("Z")} · {formatShortcut("Z", { shift: true })}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>File info panel</span>
                  <span className="kbd-pill">I</span>
                </div>
              </div>
              <div style={{ marginTop: "12px", paddingTop: "10px", borderTop: "1px solid var(--rule)" }}>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ width: "100%", justifyContent: "space-between", fontSize: "12px" }}
                  onClick={() => {
                    setShortcutsOpen(false);
                    onOpenCommandPalette();
                  }}
                >
                  <span>Command palette…</span>
                  <span className="kbd-pill">{formatShortcut("K")}</span>
                </button>
              </div>
            </div>
          )}
        </div>

        <button
          type="button"
          className={`btn-tactile ${inspectorVisible ? "active-tab" : ""}`}
          onClick={onToggleInspector}
          title={inspectorVisible ? "Hide inspector (I)" : "Show inspector (I)"}
          aria-pressed={inspectorVisible}
        >
          {inspectorVisible ? (
            <PanelRightClose size={14} aria-hidden="true" />
          ) : (
            <PanelRightOpen size={14} aria-hidden="true" />
          )}
          <span className="top-bar-inspector-label">Inspector</span>
        </button>
      </div>
    </header>
  );
};
