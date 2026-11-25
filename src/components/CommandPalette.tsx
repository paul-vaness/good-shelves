import React, { useEffect, useMemo, useRef, useState } from "react";
import { ShelfFile, ShelfGroup } from "../lib/tauri-api";
import { formatShortcut } from "../lib/platform";
import { useFocusTrap } from "../lib/use-focus-trap";

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
  onAskAi?: (query: string) => void;
}

interface PaletteEntry {
  id: string;
  kind: "action" | "shelf" | "file";
  label: string;
  fillText?: string;
  hint: string;
  file?: ShelfFile;
  onRun: () => void;
}

interface ParsedPaletteQuery {
  rawTrimmed: string;
  terms: string[];
  typeFilters: string[];
  tagFilters: string[];
  shelfFilters: string[];
  folderFilters: string[];
  pinnedOnly: boolean;
}

function parsePaletteQuery(raw: string): ParsedPaletteQuery {
  const rawTrimmed = raw.trim();
  const tokens = rawTrimmed.split(/\s+/).filter(Boolean);
  const terms: string[] = [];
  const typeFilters: string[] = [];
  const tagFilters: string[] = [];
  const shelfFilters: string[] = [];
  const folderFilters: string[] = [];
  let pinnedOnly = false;

  for (const tok of tokens) {
    const lower = tok.toLowerCase();
    if (lower.startsWith("type:") || lower.startsWith("ext:") || lower.startsWith("format:")) {
      const val = lower.split(":").slice(1).join(":").trim();
      if (val) typeFilters.push(val);
    } else if (lower.startsWith("tag:") || lower.startsWith("#")) {
      const val = lower.startsWith("#") ? lower.slice(1).trim() : lower.slice(4).trim();
      if (val) tagFilters.push(val);
    } else if (lower.startsWith("shelf:")) {
      const val = lower.slice(6).trim();
      if (val) shelfFilters.push(val);
    } else if (lower.startsWith("folder:") || lower.startsWith("in:")) {
      const val = lower.split(":").slice(1).join(":").trim();
      if (val) folderFilters.push(val);
    } else if (lower.startsWith("@") && lower.length > 1) {
      const val = lower.slice(1).trim();
      if (val === "pinned" || val === "star" || val === "starred") {
        pinnedOnly = true;
      } else if (val) {
        folderFilters.push(val);
      }
    } else if (lower === "is:pinned" || lower === "pinned:true") {
      pinnedOnly = true;
    } else {
      terms.push(lower);
    }
  }

  return {
    rawTrimmed,
    terms,
    typeFilters,
    tagFilters,
    shelfFilters,
    folderFilters,
    pinnedOnly,
  };
}

function scoreActionEntry(act: CommandPaletteAction, terms: string[], rawLower: string): number {
  if (terms.length === 0) return 10;
  const label = act.label.toLowerCase();
  const hint = act.hint.toLowerCase();
  const words = `${label} ${hint}`.split(/[\s._\-/()]+/);

  let score = 0;
  if (label === rawLower) score += 160;
  else if (label.startsWith(rawLower)) score += 110;
  else if (label.includes(rawLower)) score += 70;

  for (const t of terms) {
    let ts = 0;
    if (label === t) ts = 80;
    else if (label.startsWith(t)) ts = 65;
    else if (words.some((w) => w.startsWith(t))) ts = 50;
    else if (label.includes(t)) ts = 38;
    else if (hint.includes(t)) ts = 24;
    if (ts === 0) return 0;
    score += ts;
  }
  return score;
}

function scoreShelfEntry(sh: ShelfGroup, parsed: ParsedPaletteQuery, rawLower: string): number {
  if (
    parsed.typeFilters.length > 0 ||
    parsed.tagFilters.length > 0
  ) {
    return 0;
  }
  const name = sh.name.toLowerCase();
  const desc = (sh.desc || "").toLowerCase();
  const subs = (sh.subShelves || []).map((s) => s.name.toLowerCase());

  if (parsed.pinnedOnly && !sh.pinned) return 0;

  for (const sf of parsed.shelfFilters) {
    if (!name.includes(sf) && !subs.some((s) => s.includes(sf))) return 0;
  }
  for (const ff of parsed.folderFilters) {
    if (!name.includes(ff) && !subs.some((s) => s.includes(ff))) return 0;
  }

  if (parsed.terms.length === 0) {
    return sh.pinned ? 30 : 15;
  }

  let score = 0;
  if (name === rawLower) score += 160;
  else if (name.startsWith(rawLower)) score += 110;
  else if (name.includes(rawLower)) score += 75;

  const words = name.split(/[\s._\-/&]+/);
  for (const t of parsed.terms) {
    let ts = 0;
    if (name === t) ts = 85;
    else if (name.startsWith(t)) ts = 68;
    else if (words.some((w) => w.startsWith(t))) ts = 52;
    else if (name.includes(t)) ts = 38;
    else if (subs.some((s) => s.includes(t))) ts = 28;
    else if (desc.includes(t)) ts = 18;
    if (ts === 0) return 0;
    score += ts;
  }

  if (sh.pinned) score += 10;
  return score;
}

function scoreFileEntry(f: ShelfFile, parsed: ParsedPaletteQuery, rawLower: string): number {
  const title = (f.title || "").toLowerCase();
  const name = (f.name || "").toLowerCase();
  const format = (f.format || "").toLowerCase();
  const modality = (f.modality || "").toLowerCase();
  const shelfName = (f.shelfName || "").toLowerCase();
  const subShelf = (f.subShelf || "").toLowerCase();
  const folder = (f.folder || "").toLowerCase();
  const tags = (f.tags || []).map((t) => t.toLowerCase());
  const merchant = String(f.metadata?.merchant || "").toLowerCase();
  const docType = String(f.metadata?.doc_type || "").toLowerCase();
  const invoiceNum = String(f.metadata?.invoice_number || "").toLowerCase();
  const dueDate = String(f.metadata?.due_date || "").toLowerCase();
  const amount = String(f.metadata?.amount || "").toLowerCase();

  if (parsed.pinnedOnly && !f.pinned) return 0;

  for (const tf of parsed.typeFilters) {
    if (
      !format.includes(tf) &&
      !modality.includes(tf) &&
      !docType.includes(tf) &&
      !(f.specimen || "").toLowerCase().includes(tf)
    ) {
      return 0;
    }
  }

  for (const tagF of parsed.tagFilters) {
    if (!tags.some((t) => t.includes(tagF)) && !docType.includes(tagF)) {
      return 0;
    }
  }

  for (const sf of parsed.shelfFilters) {
    if (!shelfName.includes(sf) && !subShelf.includes(sf)) {
      return 0;
    }
  }

  for (const ff of parsed.folderFilters) {
    if (!folder.includes(ff) && !shelfName.includes(ff) && !subShelf.includes(ff)) {
      return 0;
    }
  }

  if (parsed.terms.length === 0) {
    return (f.pinned ? 50 : 20) + Math.min(10, (f.mtime || 0) > 0 ? 5 : 0);
  }

  let score = 0;
  if (title === rawLower || name === rawLower) score += 180;
  else if (title.startsWith(rawLower) || name.startsWith(rawLower)) score += 120;
  else if (title.includes(rawLower) || name.includes(rawLower)) score += 75;

  if (merchant && merchant.includes(rawLower)) score += 65;
  if (invoiceNum && invoiceNum.includes(rawLower)) score += 70;

  const titleWords = title.split(/[\s._\-/()]+/);
  const nameWords = name.split(/[\s._\-/()]+/);

  for (const t of parsed.terms) {
    let ts = 0;
    if (title === t || name === t) ts = Math.max(ts, 95);
    else if (title.startsWith(t) || name.startsWith(t)) ts = Math.max(ts, 75);
    else if (titleWords.some((w) => w.startsWith(t)) || nameWords.some((w) => w.startsWith(t))) {
      ts = Math.max(ts, 58);
    } else if (title.includes(t) || name.includes(t)) {
      ts = Math.max(ts, 42);
    }

    if (tags.some((tg) => tg === t)) ts = Math.max(ts, 70);
    else if (tags.some((tg) => tg.startsWith(t) || tg.includes(t))) ts = Math.max(ts, 48);

    if (merchant) {
      if (merchant.startsWith(t)) ts = Math.max(ts, 68);
      else if (merchant.includes(t)) ts = Math.max(ts, 50);
    }
    if (docType) {
      if (docType.startsWith(t)) ts = Math.max(ts, 54);
      else if (docType.includes(t)) ts = Math.max(ts, 38);
    }
    if (invoiceNum && invoiceNum.includes(t)) ts = Math.max(ts, 62);
    if (dueDate && dueDate.includes(t)) ts = Math.max(ts, 48);
    if (amount && amount.includes(t)) ts = Math.max(ts, 46);

    if (format === t || modality === t) ts = Math.max(ts, 46);
    else if (format.startsWith(t)) ts = Math.max(ts, 34);

    if (shelfName.startsWith(t) || folder.startsWith(t)) ts = Math.max(ts, 38);
    else if (shelfName.includes(t) || subShelf.includes(t) || folder.includes(t)) {
      ts = Math.max(ts, 28);
    }

    if (ts === 0) return 0;
    score += ts;
  }

  if (f.pinned) score += 12;
  return score;
}

function buildFileHint(f: ShelfFile): string {
  const parts: string[] = [];
  if (f.pinned) {
    parts.push("★");
  }
  parts.push((f.format || f.modality || "FILE").toUpperCase());
  const location = f.shelfName || f.folder;
  if (location) {
    parts.push(location);
  }
  const merchant = f.metadata?.merchant ? String(f.metadata.merchant).trim() : "";
  const amount = f.metadata?.amount ? String(f.metadata.amount).trim() : "";
  if (amount) {
    parts.push(amount);
  } else if (merchant && !location.toLowerCase().includes(merchant.toLowerCase())) {
    parts.push(merchant);
  }
  return parts.join(" · ");
}

export const CommandPalette: React.FC<CommandPaletteProps> = ({
  open,
  files,
  shelves,
  actions,
  onClose,
  onSelectFile,
  onSelectShelf,
  onAskAi,
}) => {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const paletteCardRef = useFocusTrap<HTMLDivElement>(open);

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
    const parsed = parsePaletteQuery(query);
    const q = parsed.rawTrimmed.toLowerCase();
    const hasOperator =
      parsed.typeFilters.length > 0 ||
      parsed.tagFilters.length > 0 ||
      parsed.shelfFilters.length > 0 ||
      parsed.folderFilters.length > 0 ||
      parsed.pinnedOnly;

    const actList: PaletteEntry[] = [];
    const shList: PaletteEntry[] = [];
    const fList: PaletteEntry[] = [];

    const scoredActions = hasOperator
      ? []
      : actions
          .map((act) => ({ act, score: scoreActionEntry(act, parsed.terms, q) }))
          .filter((x) => x.score > 0)
          .sort((a, b) => b.score - a.score);

    // Route natural-language queries through the AI agent
    if (q && onAskAi) {
      const trimmed = parsed.rawTrimmed;
      const commandVerb = /\b(find|show|list|organize|clean|rename|move|delete|trash|sort|group|filter|categorize|open|search|get|put|create|make|remove|merge|split|tag|what|where|which|how|why|when|who|give|bring)\b/i.test(trimmed);
      const matchedActionCount = scoredActions.length;
      const looksLikeQuestion = /^(what|where|which|how|why|when|who|is|are|do|does|can|could|would|should)\b/i.test(trimmed);
      // Offer Ask AI when the query reads like a command/question or matches no known action.
      if (commandVerb || looksLikeQuestion || matchedActionCount === 0) {
        actList.push({
          id: "ask-ai",
          kind: "action",
          label: `Ask AI: “${trimmed.slice(0, 60)}”`,
          fillText: trimmed,
          hint: formatShortcut("Enter"),
          onRun: () => onAskAi(trimmed),
        });
      }
    }

    for (const { act } of scoredActions) {
      actList.push({
        id: `act:${act.id}`,
        kind: "action",
        label: act.label,
        fillText: act.label,
        hint: act.hint,
        onRun: act.onRun,
      });
    }

    const scoredShelves = shelves
      .map((sh) => ({ sh, score: scoreShelfEntry(sh, parsed, q) }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score);

    for (const { sh } of scoredShelves) {
      shList.push({
        id: `shelf:${sh.id}`,
        kind: "shelf",
        label: sh.name,
        fillText: `shelf:${sh.name.includes(" ") ? sh.name.split(" ")[0].toLowerCase() : sh.name.toLowerCase()} `,
        hint: sh.pinned ? "★ Pinned shelf" : "Open shelf",
        onRun: () => onSelectShelf(sh.id),
      });
    }

    const scoredFiles = files
      .map((f) => ({ f, score: scoreFileEntry(f, parsed, q) }))
      .filter((x) => x.score > 0)
      .sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        return (b.f.mtime || 0) - (a.f.mtime || 0);
      });

    for (const { f } of scoredFiles.slice(0, 10)) {
      const displayLabel = f.title || f.name;
      fList.push({
        id: `file:${f.id}`,
        kind: "file",
        label: displayLabel,
        fillText: displayLabel,
        hint: buildFileHint(f),
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
  }, [query, actions, shelves, files, onSelectFile, onSelectShelf, onAskAi]);

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
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      const trimmed = query.trim();
      if (trimmed && onAskAi) {
        e.preventDefault();
        onAskAi(trimmed);
        onClose();
        return;
      }
    }
    if (e.key === "Tab" && !e.shiftKey && flatItems[activeIndex]) {
      e.preventDefault();
      const current = flatItems[activeIndex];
      const nextVal = current.fillText || current.file?.title || current.file?.name || current.label;
      setQuery(nextVal);
      requestAnimationFrame(() => {
        inputRef.current?.focus();
      });
      return;
    }
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
        ref={paletteCardRef}
        tabIndex={-1}
        className="cmd-palette-card"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--rule)", display: "flex", alignItems: "center", gap: "10px" }}>
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
          <span>↑↓ Navigate · Enter Open · Tab Fill · {formatShortcut("Enter")} Ask AI</span>
          <span>Esc Close</span>
        </div>
      </div>
    </div>
  );
};
