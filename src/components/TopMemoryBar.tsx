import React, { useEffect } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import {
  ArrowLeft,
  Moon,
  Sun,
  Undo2,
} from "lucide-react";
import { formatShortcut } from "../lib/platform";
import { isTauriRuntime } from "../lib/tauri-api";

export type ViewId =
  | "home"
  | "shelves"
  | "folders"
  | "find"
  | "tidy"
  | "lookalikes"
  | "trash"
  | "learns"
  | "settings";

interface TopMemoryBarProps {
  query: string;
  activeView: ViewId;
  findMode?: "search" | "ask";
  cleanupCount?: number;
  tidyCount?: number;
  dupCount?: number;
  trashCount?: number;
  canUndo?: boolean;
  isNightMode: boolean;
  liveProgress?: { operation: string; current: number; total: number; file_name: string } | null;
  onQueryChange: (val: string) => void;
  onClearQuery: () => void;
  onSwitchView: (view: ViewId) => void;
  onOpenAskMode: () => void;
  onOpenSmartClean: () => void;
  onOpenCommandPalette: () => void;
  onUndo?: () => void;
  onToggleTheme: () => void;
  inputRef?: React.RefObject<HTMLInputElement | null>;
}

export const TopMemoryBar: React.FC<TopMemoryBarProps> = ({
  activeView,
  findMode = "search",
  tidyCount = 0,
  dupCount = 0,
  trashCount = 0,
  canUndo = false,
  isNightMode,
  onSwitchView,
  onUndo,
  onToggleTheme,
}) => {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const isInput =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable);
      if (!isInput && e.key === "/") {
        e.preventDefault();
        if (activeView !== "find") {
          onSwitchView("find");
        }
        requestAnimationFrame(() => {
          const targetId =
            activeView === "find" && findMode === "ask" ? "askChatInput" : "findSearchInput";
          const findInput = document.getElementById(targetId) as HTMLInputElement | null;
          findInput?.focus();
          findInput?.select();
        });
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [activeView, findMode, onSwitchView]);

  const handleHeaderDoubleClick = (e: React.MouseEvent<HTMLElement>) => {
    if (!isTauriRuntime()) return;
    const target = e.target as HTMLElement | null;
    if (
      target?.closest(
        'button, input, a, select, [role="button"], [role="tab"], .eye-nav-pills'
      )
    )
      return;
    void getCurrentWindow().toggleMaximize();
  };

  return (
    <header
      className="page-eye-nav"
      data-tauri-drag-region
      onDoubleClick={handleHeaderDoubleClick}
    >
      <div className="page-eye-nav-inner" data-tauri-drag-region>
        {/* Left: Tactile Home Back Button + Uniform Eye-Level Navigation Pills */}
        <div
          data-tauri-drag-region
          style={{
            display: "flex",
            alignItems: "center",
            gap: "10px",
            minWidth: 0,
            flexWrap: "wrap",
          }}
        >
          <button
            type="button"
            className="btn-tactile sm"
            onClick={() => onSwitchView("home")}
            title="Back to Home (Esc)"
            aria-label="Back to Home"
          >
            <ArrowLeft size={13} aria-hidden="true" />
            <span>Home</span>
          </button>

          <nav
            className="eye-nav-pills"
            aria-label="Workspace pages"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "4px",
              flexWrap: "wrap",
            }}
          >
            <button
              type="button"
              className={`btn-ghost ${activeView === "shelves" ? "active" : ""}`}
              aria-current={activeView === "shelves" ? "page" : undefined}
              onClick={() => onSwitchView("shelves")}
            >
              Shelves
            </button>
            <button
              type="button"
              className={`btn-ghost ${activeView === "folders" ? "active" : ""}`}
              aria-current={activeView === "folders" ? "page" : undefined}
              onClick={() => onSwitchView("folders")}
            >
              Folders
            </button>
            <button
              type="button"
              className={`btn-ghost ${activeView === "find" ? "active" : ""}`}
              aria-current={activeView === "find" ? "page" : undefined}
              onClick={() => onSwitchView("find")}
            >
              Search &amp; Ask
            </button>
            <button
              type="button"
              className={`btn-ghost ${activeView === "tidy" ? "active" : ""}`}
              aria-current={activeView === "tidy" ? "page" : undefined}
              onClick={() => onSwitchView("tidy")}
            >
              <span>Rename</span>
              {tidyCount > 0 && <span className="shelf-ledger-count">{tidyCount}</span>}
            </button>
            <button
              type="button"
              className={`btn-ghost ${activeView === "lookalikes" ? "active" : ""}`}
              aria-current={activeView === "lookalikes" ? "page" : undefined}
              onClick={() => onSwitchView("lookalikes")}
            >
              <span>Duplicates</span>
              {dupCount > 0 && <span className="shelf-ledger-count">{dupCount}</span>}
            </button>
            <button
              type="button"
              className={`btn-ghost ${activeView === "trash" ? "active" : ""}`}
              aria-current={activeView === "trash" ? "page" : undefined}
              onClick={() => onSwitchView("trash")}
            >
              <span>Trash</span>
              {trashCount > 0 && <span className="shelf-ledger-count">{trashCount}</span>}
            </button>
            <button
              type="button"
              className={`btn-ghost ${activeView === "learns" ? "active" : ""}`}
              aria-current={activeView === "learns" ? "page" : undefined}
              onClick={() => onSwitchView("learns")}
            >
              Activity
            </button>
            <button
              type="button"
              className={`btn-ghost ${activeView === "settings" ? "active" : ""}`}
              aria-current={activeView === "settings" ? "page" : undefined}
              onClick={() => onSwitchView("settings")}
            >
              Settings
            </button>
          </nav>
        </div>

        {/* Right: Uniform Eye-Level Undo + Dark/Light Mode Button */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "6px",
            flexShrink: 0,
          }}
        >
          {canUndo && onUndo && (
            <button
              type="button"
              className="btn-tactile sm"
              onClick={onUndo}
              title={`Undo (${formatShortcut("Z")})`}
              aria-label="Undo last action"
            >
              <Undo2 size={13} aria-hidden="true" />
              <span>Undo</span>
            </button>
          )}

          <button
            type="button"
            className="btn-tactile sm"
            onClick={onToggleTheme}
            title={isNightMode ? "Switch to light mode" : "Switch to dark mode"}
            aria-label={isNightMode ? "Switch to light mode" : "Switch to dark mode"}
          >
            {isNightMode ? (
              <Sun size={13} aria-hidden="true" />
            ) : (
              <Moon size={13} aria-hidden="true" />
            )}
            <span>{isNightMode ? "Light" : "Dark"}</span>
          </button>
        </div>
      </div>
    </header>
  );
};
