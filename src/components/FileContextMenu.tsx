import React, { useEffect, useRef } from "react";
import { ShelfFile } from "../lib/tauri-api";
import { formatShortcut } from "../lib/platform";

export interface ContextMenuState {
  file: ShelfFile;
  x: number;
  y: number;
}

interface FileContextMenuProps {
  menu: ContextMenuState | null;
  onClose: () => void;
  onQuickLook: (file: ShelfFile) => void;
  onFindSimilar: (file: ShelfFile) => void;
  onAskAboutFile?: (file: ShelfFile) => void;
  onOpenExternal: (file: ShelfFile) => void;
  onRevealExternal: (file: ShelfFile) => void;
  onCopyToClipboard: (file: ShelfFile, mode: "copy" | "cut") => void;
  onRename: (file: ShelfFile) => void;
  onMove: (file: ShelfFile) => void;
  onTogglePin: (file: ShelfFile) => void;
  onDelete: (file: ShelfFile) => void;
}

export const FileContextMenu: React.FC<FileContextMenuProps> = ({
  menu,
  onClose,
  onQuickLook,
  onFindSimilar,
  onAskAboutFile,
  onOpenExternal,
  onRevealExternal,
  onCopyToClipboard,
  onRename,
  onMove,
  onTogglePin,
  onDelete,
}) => {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!menu) return;
    requestAnimationFrame(() => {
      const first = ref.current?.querySelector<HTMLButtonElement>('[role="menuitem"]');
      first?.focus();
    });
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        onClose();
        return;
      }
      if (!ref.current) return;
      const items = Array.from(ref.current.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'));
      if (!items.length) return;
      const curIdx = items.indexOf(document.activeElement as HTMLButtonElement);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        items[(curIdx + 1) % items.length]?.focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        items[(curIdx - 1 + items.length) % items.length]?.focus();
      } else if (e.key === "Home") {
        e.preventDefault();
        items[0]?.focus();
      } else if (e.key === "End") {
        e.preventDefault();
        items[items.length - 1]?.focus();
      }
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [menu, onClose]);

  if (!menu) return null;

  const f = menu.file;
  const itemWidth = 232;
  const itemHeight = 30;
  const headerHeight = 30;
  const padding = 12;

  const actions: Array<{
    label: string;
    shortcut?: string;
    danger?: boolean;
    dividerAfter?: boolean;
    onClick: () => void;
  }> = [
    { label: "Preview", shortcut: "Space", onClick: () => onQuickLook(f) },
    { label: "Open", onClick: () => onOpenExternal(f) },
    {
      label: "Show in folder",
      dividerAfter: true,
      onClick: () => onRevealExternal(f),
    },
    { label: "Copy", shortcut: formatShortcut("C"), onClick: () => onCopyToClipboard(f, "copy") },
    { label: "Cut", shortcut: formatShortcut("X"), onClick: () => onCopyToClipboard(f, "cut") },
    { label: "Rename…", shortcut: "F2", onClick: () => onRename(f) },
    {
      label: "Move or copy to…",
      shortcut: "M",
      dividerAfter: true,
      onClick: () => onMove(f),
    },
    { label: "Find similar", onClick: () => onFindSimilar(f) },
    ...(onAskAboutFile
      ? [{ label: "Ask AI about this file", onClick: () => onAskAboutFile(f) }]
      : []),
    {
      label: f.pinned ? "Unpin file" : "Pin file",
      dividerAfter: true,
      onClick: () => onTogglePin(f),
    },
    { label: "Move to Trash", shortcut: "Del", danger: true, onClick: () => onDelete(f) },
  ];

  const menuHeight = headerHeight + actions.length * itemHeight + padding;
  const vw = typeof window !== "undefined" ? window.innerWidth : 1200;
  const vh = typeof window !== "undefined" ? window.innerHeight : 800;
  const left = Math.max(8, Math.min(menu.x, vw - itemWidth - 8));
  const top = Math.max(8, Math.min(menu.y, vh - menuHeight - 8));

  return (
    <div
      ref={ref}
      className="file-context-menu"
      role="menu"
      aria-label={`Actions for ${f.name}`}
      style={{ left: `${Math.max(8, left)}px`, top: `${Math.max(8, top)}px` }}
    >
      <div
        style={{
          padding: "6px 10px",
          fontSize: "12px",
          fontWeight: 500,
          color: "var(--ink-3)",
          borderBottom: "1px solid var(--rule)",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {f.name}
      </div>
      {actions.map((act) => (
        <React.Fragment key={act.label}>
          <button
            type="button"
            role="menuitem"
            className={`context-menu-item ${act.danger ? "danger" : ""}`}
            onClick={() => {
              act.onClick();
              onClose();
            }}
          >
            <span>{act.label}</span>
            {act.shortcut && (
              <span style={{ fontSize: "11px", color: "var(--ink-3)", marginLeft: "auto", paddingLeft: "14px" }}>{act.shortcut}</span>
            )}
          </button>
          {act.dividerAfter && <div style={{ height: "1px", background: "var(--rule-soft)", margin: "3px 0" }} />}
        </React.Fragment>
      ))}
    </div>
  );
};
