import React, { useEffect, useRef } from "react";
import {
  Eye,
  Sparkles,
  ExternalLink,
  FolderOpen,
  Copy,
  Scissors,
  Pencil,
  FolderInput,
  Star,
  Trash2,
} from "lucide-react";
import { ShelfFile } from "../lib/tauri-api";

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

  const vw = typeof window !== "undefined" ? window.innerWidth : 1200;
  const vh = typeof window !== "undefined" ? window.innerHeight : 800;
  const left = Math.min(menu.x, vw - 230);
  const top = Math.min(menu.y, vh - 360);
  const f = menu.file;

  const actions: Array<{
    label: string;
    icon: React.ReactNode;
    danger?: boolean;
    dividerAfter?: boolean;
    onClick: () => void;
  }> = [
    { label: "quick look", icon: <Eye size={13} />, onClick: () => onQuickLook(f) },
    {
      label: "find similar files",
      icon: <Sparkles size={13} />,
      dividerAfter: true,
      onClick: () => onFindSimilar(f),
    },
    { label: "open with default app", icon: <ExternalLink size={13} />, onClick: () => onOpenExternal(f) },
    {
      label: "reveal in file manager",
      icon: <FolderOpen size={13} />,
      dividerAfter: true,
      onClick: () => onRevealExternal(f),
    },
    { label: "copy (for paste)", icon: <Copy size={13} />, onClick: () => onCopyToClipboard(f, "copy") },
    { label: "cut (move on paste)", icon: <Scissors size={13} />, onClick: () => onCopyToClipboard(f, "cut") },
    { label: "rename…", icon: <Pencil size={13} />, onClick: () => onRename(f) },
    { label: "move or copy to…", icon: <FolderInput size={13} />, onClick: () => onMove(f) },
    {
      label: f.pinned ? "unpin file" : "pin file",
      icon: <Star size={13} />,
      dividerAfter: true,
      onClick: () => onTogglePin(f),
    },
    { label: "remove / trash…", icon: <Trash2 size={13} />, danger: true, onClick: () => onDelete(f) },
  ];

  return (
    <div
      ref={ref}
      className="file-context-menu"
      role="menu"
      aria-label={`Actions for ${f.name}`}
      style={{ left: `${Math.max(8, left)}px`, top: `${Math.max(8, top)}px` }}
    >
      <div
        className="font-mono-code"
        style={{
          padding: "6px 10px",
          fontSize: "11px",
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
            {act.icon}
            <span>{act.label}</span>
          </button>
          {act.dividerAfter && <div style={{ height: "1px", background: "var(--rule)", margin: "3px 0" }} />}
        </React.Fragment>
      ))}
    </div>
  );
};
