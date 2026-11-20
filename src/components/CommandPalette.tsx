import React, { useEffect, useMemo, useRef, useState } from "react";
import { ShelfFile, ShelfGroup } from "../lib/tauri-api";
import { formatShortcut } from "../lib/platform";

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

interface PaletteEntry {
  id: string;
  kind: "action" | "shelf" | "file";
  label: string;
  hint: string;
  file?: ShelfFile;
  onRun: () => void;
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
  const listRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (open) {
      returnFocusRef.current = document.activeElement as HTMLElement | null;
      setQuery("");
      setActiveIndex(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    } else if (returnFocusRef.current) {
      const el = returnFocusRef.current;
      returnFocusRef.current = null;
      requestAnimationFrame(() => el?.focus?.());
    }
  }, [open]);

  const { actionItems, shelfItems, fileItems, flatItems } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const actList: PaletteEntry[] = [];
    const shList: PaletteEntry[] = [];
    const fList: PaletteEntry[] = [];

    for (const act of actions) {
      if (!q || act.label.toLowerCase().includes(q) || act.hint.toLowerCase().includes(q)) {
        actList.push({ id: `act:${act.id}`, kind: "action", label: act.label, hint: act.hint, onRun: act.onRun });
      }
    }

    for (const sh of shelves) {
      if (!q || sh.name.toLowerCase().includes(q)) {
        shList.push({
          id: `shelf:${sh.id}`,
          kind: "shelf",
          label: sh.name,
          hint: sh.pinned ? "Pinned shelf" : "Open shelf",
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

    for (const f of matchedFiles.slice(0, 10)) {
      fList.push({
        id: `file:${f.id}`,
        kind: "file",
        label: f.title || f.name,
        hint: `${f.format.toUpperCase()} · ${f.folder}`,
        file: f,
        onRun: () => onSelectFile(f.id),
      });
    }

    const combined = [...actList.slice(0, 6), ...shList.slice(0, 5), ...fList.slice(0, 8)];
    return {
      actionItems: actList.slice(0, 6),
      shelfItems: shList.slice(0, 5),
      fileItems: fList.slice(0, 8),
      flatItems: combined,
    };
  }, [query, actions, shelves, files, onSelectFile, onSelectShelf]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  useEffect(() => {
    if (!open || !listRef.current) return;
    const activeEl = listRef.current.querySelector<HTMLElement>(`[data-cp-idx="${activeIndex}"]`);
    activeEl?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open]);

  if (!open) return null;

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((prev) => (flatItems.length ? (prev + 1) % flatItems.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((prev) => (flatItems.length ? (prev - 1 + flatItems.length) % flatItems.length : 0));
    } else if (e.key === "Enter" && flatItems[activeIndex]) {
      e.preventDefault();
      flatItems[activeIndex].onRun();
      onClose();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      e.nativeEvent.stopImmediatePropagation();
      onClose();
    }
  };

  let runningIndex = -1;

  const renderGroup = (title: string, entries: PaletteEntry[]) => {
    if (entries.length === 0) return null;
    return (
      <div>
        <div className="cmd-palette-group-label">{title}</div>
        {entries.map((item) => {
          runningIndex += 1;
          const idx = runningIndex;
          return (
            <button
              key={item.id}
              type="button"
              role="option"
              aria-selected={idx === activeIndex}
              data-cp-idx={idx}
              className={`cmd-palette-item ${idx === activeIndex ? "active" : ""}`}
              onClick={() => {
                item.onRun();
                onClose();
              }}
              onMouseEnter={() => setActiveIndex(idx)}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {item.label}
              </span>
              <span style={{ fontSize: "11px", color: "var(--ink-3)", flexShrink: 0 }}>
                {item.hint}
              </span>
            </button>
          );
        })}
      </div>
    );
  };

  return (
    <div
      className="cmd-palette-wrap"
      role="dialog"
      aria-modal="true"
      aria-label="Quick jump and commands"
      onClick={onClose}
    >
      <div
        className="cmd-palette-card"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--rule)", display: "flex", alignItems: "center", gap: "10px" }}>
          <input
            ref={inputRef}
            type="text"
            className="cmd-palette-input"
            aria-label="Type a file, tag, shelf, or command"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`Jump to any file, shelf, or action (${formatShortcut("K")})…`}
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
        <div ref={listRef} role="listbox" aria-label="Quick jump results" style={{ maxHeight: "380px", overflowY: "auto" }}>
          {flatItems.length === 0 ? (
            <div style={{ padding: "22px 16px", fontSize: "13px", color: "var(--ink-3)", textAlign: "center" }}>
              No matching commands, shelves, or files.
            </div>
          ) : (
            <>
              {renderGroup("Actions", actionItems)}
              {renderGroup("Shelves", shelfItems)}
              {renderGroup("Files", fileItems)}
            </>
          )}
        </div>
        <div
          style={{
            padding: "8px 16px",
            borderTop: "1px solid var(--rule)",
            background: "var(--bg-subtle)",
            display: "flex",
            justifyContent: "space-between",
            fontSize: "11px",
            color: "var(--ink-3)",
          }}
        >
          <span>↑↓ Navigate · Enter Open</span>
          <span>Esc Close</span>
        </div>
      </div>
    </div>
  );
};
