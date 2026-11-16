import React, { useRef, useEffect, useState } from "react";
import { PanelRightClose, PanelRightOpen } from "lucide-react";

interface TopMemoryBarProps {
  query: string;
  inspectorVisible: boolean;
  liveProgress?: { operation: string; current: number; total: number; file_name: string } | null;
  onQueryChange: (val: string) => void;
  onClearQuery: () => void;
  onOpenOrganizeModal: () => void;
  onOpenSmartClean: () => void;
  onOpenCommandPalette: () => void;
  onToggleInspector: () => void;
  inputRef?: React.RefObject<HTMLInputElement | null>;
}

export const TopMemoryBar: React.FC<TopMemoryBarProps> = ({
  query,
  inspectorVisible,
  liveProgress,
  onQueryChange,
  onClearQuery,
  onOpenOrganizeModal,
  onOpenSmartClean,
  onOpenCommandPalette,
  onToggleInspector,
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
        setShortcutsOpen(false);
        return;
      }
      if (!isInput && e.key === "/") {
        e.preventDefault();
        effectiveRef.current?.focus();
      }
    };
    const onPointerDown = (e: MouseEvent) => {
      if (shortcutsOpen && shortcutsRef.current && !shortcutsRef.current.contains(e.target as Node)) {
        setShortcutsOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("mousedown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("mousedown", onPointerDown);
    };
  }, [effectiveRef, shortcutsOpen]);

  return (
    <header className="stage-command-bar">
      <div className="quick-memory-box">
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          style={{ color: "var(--ink-3)", flexShrink: 0 }}
        >
          <circle cx="11" cy="11" r="7"></circle>
          <line x1="20" y1="20" x2="16.2" y2="16.2"></line>
        </svg>
        <input
          ref={effectiveRef}
          id="globalMemoryInput"
          type="text"
          autoComplete="off"
          spellCheck={false}
          value={query}
          placeholder="Search files by content, filename, or topic… (/)"
          style={{ width: "100%", fontSize: "13.5px", color: "var(--ink)" }}
          onChange={(e) => onQueryChange(e.target.value)}
        />
        {query.trim().length > 0 && (
          <button
            type="button"
            onClick={onClearQuery}
            style={{
              fontSize: "11.5px",
              color: "var(--ink-3)",
              padding: "2px 6px",
            }}
          >
            clear
          </button>
        )}
      </div>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "10px",
          flexShrink: 0,
        }}
      >
        <button
          type="button"
          className="btn-tactile solid"
          onClick={onOpenSmartClean}
        >
          <span>clean up</span>
        </button>
        <button
          type="button"
          className="btn-tactile"
          onClick={onOpenOrganizeModal}
        >
          <span>organize folders…</span>
        </button>
        <div ref={shortcutsRef} style={{ position: "relative" }}>
          <button
            type="button"
            className="btn-tactile"
            style={{ padding: "5px 9px", fontWeight: 500 }}
            onClick={() => setShortcutsOpen((o) => !o)}
            title="Keyboard shortcuts"
            aria-label="Keyboard shortcuts"
          >
            ?
          </button>
          {shortcutsOpen && (
            <div
              style={{
                position: "absolute",
                right: 0,
                top: "calc(100% + 8px)",
                width: "290px",
                background: "var(--bg-canvas)",
                border: "1px solid var(--ink)",
                borderRadius: "8px",
                padding: "14px 16px",
                boxShadow: "0 14px 36px rgba(0,0,0,0.2)",
                zIndex: 80,
                fontSize: "12.5px",
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: "8px", color: "var(--ink)" }}>
                shortcuts &amp; quick actions
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: "6px", color: "var(--ink-2)" }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>focus search</span>
                  <span className="font-mono-code">/</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>quick jump &amp; search</span>
                  <span className="font-mono-code">Ctrl/⌘ + K</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>quick look preview</span>
                  <span className="font-mono-code">Space</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>undo / redo</span>
                  <span className="font-mono-code">Ctrl/⌘ + Z / ⇧Z</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>toggle / close inspector</span>
                  <span className="font-mono-code">I · Esc</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>rename / move / delete</span>
                  <span className="font-mono-code">R · M · Del</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>navigate files</span>
                  <span className="font-mono-code">↑ / ↓ / Enter</span>
                </div>
              </div>
              <div
                style={{
                  marginTop: "12px",
                  paddingTop: "10px",
                  borderTop: "1px solid var(--rule)",
                  display: "flex",
                  flexDirection: "column",
                  gap: "6px",
                }}
              >
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ width: "100%", justifyContent: "space-between", fontSize: "12px" }}
                  onClick={() => {
                    setShortcutsOpen(false);
                    onOpenCommandPalette();
                  }}
                >
                  <span>quick jump…</span>
                  <span className="font-mono-code" style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                    ⌘K
                  </span>
                </button>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ width: "100%", justifyContent: "space-between", fontSize: "12px" }}
                  onClick={() => {
                    setShortcutsOpen(false);
                    onOpenSmartClean();
                  }}
                >
                  <span>clean up workspace…</span>
                </button>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ width: "100%", justifyContent: "space-between", fontSize: "12px" }}
                  onClick={() => {
                    setShortcutsOpen(false);
                    onOpenOrganizeModal();
                  }}
                >
                  <span>organize folders…</span>
                </button>
              </div>
            </div>
          )}
        </div>
        <button
          type="button"
          className={`btn-tactile ${inspectorVisible ? "solid" : ""}`}
          onClick={onToggleInspector}
          title={inspectorVisible ? "Hide inspector (I or Esc)" : "Show inspector (I)"}
          style={{ gap: "6px" }}
        >
          {inspectorVisible ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
          <span>inspector</span>
          <span
            className="font-mono-code"
            style={{
              fontSize: "10.5px",
              opacity: 0.72,
              padding: "0 4px",
              borderRadius: "3px",
              border: "1px solid currentColor",
            }}
          >
            I
          </span>
        </button>
      </div>
      {liveProgress && (
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 0,
            height: "3px",
            background: "var(--rule-soft)",
            overflow: "hidden",
          }}
          title={`${liveProgress.operation}: ${liveProgress.current}/${liveProgress.total} ${liveProgress.file_name}`}
        >
          <div
            style={{
              height: "100%",
              width: `${Math.min(100, Math.round((liveProgress.current / Math.max(1, liveProgress.total)) * 100))}%`,
              background: "var(--ink)",
              transition: "width 180ms ease",
            }}
          />
        </div>
      )}
    </header>
  );
};
