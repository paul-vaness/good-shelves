import React, { useEffect, useMemo, useRef, useState } from "react";
import { ShelfFile, ShelfGroup } from "../lib/tauri-api";

export interface CommandPaletteAction {
  id: string;
  label: string;
  hint: string;
  onRun: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  files: ShelfFile[];
  shelves: ShelfGroup[];
  actions: CommandPaletteAction[];
  onClose: () => void;
  onSelectFile: (fileId: string) => void;
  onSelectShelf: (shelfId: string) => void;
}

export const CommandPalette: React.FC<CommandPaletteProps> = ({
  open,
  files,
  shelves,
  actions,
  onClose,
  onSelectFile,
  onSelectShelf,
}) => {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      setTimeout(() => inputRef.current?.focus(), 20);
    }
  }, [open]);

  const items = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out: Array<{
      id: string;
      kind: "action" | "shelf" | "file";
      label: string;
      hint: string;
      onRun: () => void;
    }> = [];

    for (const act of actions) {
      if (!q || act.label.toLowerCase().includes(q) || act.hint.toLowerCase().includes(q)) {
        out.push({ id: `act:${act.id}`, kind: "action", label: act.label, hint: act.hint, onRun: act.onRun });
      }
    }

    for (const sh of shelves) {
      if (!q || sh.name.toLowerCase().includes(q)) {
        out.push({
          id: `shelf:${sh.id}`,
          kind: "shelf",
          label: `shelf: ${sh.name}`,
          hint: sh.pinned ? "pinned shelf" : "open shelf",
          onRun: () => onSelectShelf(sh.id),
        });
      }
    }

    const matchedFiles = q
      ? files.filter(
          (f) =>
            f.name.toLowerCase().includes(q) ||
            f.title.toLowerCase().includes(q) ||
            (f.tags || []).some((t) => t.toLowerCase().includes(q))
        )
      : files.filter((f) => f.pinned).concat(files.filter((f) => !f.pinned));

    for (const f of matchedFiles.slice(0, 12)) {
      out.push({
        id: `file:${f.id}`,
        kind: "file",
        label: f.title || f.name,
        hint: `${f.format} · ${f.folder}`,
        onRun: () => onSelectFile(f.id),
      });
    }
    return out.slice(0, 18);
  }, [query, actions, shelves, files, onSelectFile, onSelectShelf]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  if (!open) return null;

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((prev) => (items.length ? (prev + 1) % items.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((prev) => (items.length ? (prev - 1 + items.length) % items.length : 0));
    } else if (e.key === "Enter" && items[activeIndex]) {
      e.preventDefault();
      items[activeIndex].onRun();
      onClose();
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 95,
        background: "rgba(18, 17, 15, 0.36)",
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "center",
        paddingTop: "12vh",
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: "min(620px, 92vw)",
          background: "var(--bg-canvas)",
          border: "1px solid var(--ink)",
          borderRadius: "6px",
          boxShadow: "0 18px 48px rgba(0,0,0,0.22)",
          overflow: "hidden",
        }}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--rule)" }}>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="type a file, tag, shelf, or command (ctrl/cmd+k)..."
            style={{
              width: "100%",
              background: "transparent",
              border: "none",
              outline: "none",
              fontSize: "14px",
              color: "var(--ink)",
            }}
          />
        </div>
        <div style={{ maxHeight: "360px", overflowY: "auto" }}>
          {items.length === 0 ? (
            <div style={{ padding: "18px 16px", fontSize: "13px", color: "var(--ink-3)" }}>
              no matching commands, shelves, or files.
            </div>
          ) : (
            items.map((item, idx) => (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  item.onRun();
                  onClose();
                }}
                onMouseEnter={() => setActiveIndex(idx)}
                style={{
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "12px",
                  padding: "9px 16px",
                  border: "none",
                  borderBottom: "1px solid var(--rule)",
                  background: idx === activeIndex ? "var(--bg-hover)" : "transparent",
                  color: "var(--ink)",
                  textAlign: "left",
                  cursor: "pointer",
                  fontSize: "13px",
                }}
              >
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {item.label}
                </span>
                <span style={{ fontSize: "11px", color: "var(--ink-3)", flexShrink: 0 }}>
                  {item.hint}
                </span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
