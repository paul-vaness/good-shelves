import React, { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { X, ChevronDown, ChevronRight, MoreHorizontal } from "lucide-react";
import { ShelfFile, ShelfGroup, cleanPreviewText, resolveLocalAssetSrc } from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";

type SortMode = "default" | "name" | "date" | "size" | "format";

const SHELF_CATEGORIES = [
  { id: "all", label: "All" },
  { id: "docs", label: "Docs" },
  { id: "pdf", label: "PDF" },
  { id: "images", label: "Images" },
  { id: "sheets", label: "Sheets" },
  { id: "code", label: "Code" },
  { id: "media", label: "Media" },
];

function matchesShelfCategory(f: ShelfFile, cat: string): boolean {
  if (cat === "all") return true;
  const ext = (f.format || "").toLowerCase();
  if (cat === "pdf") return ext === "pdf";
  if (cat === "images") return f.modality === "image";
  if (cat === "docs") return ["docx", "doc", "odt", "rtf", "txt", "md", "pptx", "ppt"].includes(ext);
  if (cat === "sheets") return ["xlsx", "xlsm", "xls", "csv", "tsv"].includes(ext);
  if (cat === "code") return f.modality === "code";
  if (cat === "media") return f.modality === "video" || f.modality === "audio";
  return true;
}

function inferSubShelf(f: ShelfFile): string {
  if (f.subShelf && f.subShelf.trim()) return f.subShelf.trim();
  const blob = `${f.name} ${f.title} ${f.extractedText || ""} ${f.ocrText || ""}`.toLowerCase();
  const ext = (f.format || "").toLowerCase();
  if (f.specimen === "receipt") {
    if (/(coffee|latte|cafe|restaurant|food|grocery|market)/.test(blob)) return "Food & Dining";
    if (/(invoice|billing|statement)/.test(blob)) return "Invoices & Statements";
    return "General Receipts";
  }
  if (f.specimen === "diagram" || ext === "svg") {
    if (ext === "svg") return "Vector & SVG Assets";
    return "Architecture & Diagrams";
  }
  if (f.modality === "code") {
    if (ext === "py") return "Python Scripts";
    if (["ts", "tsx", "js", "jsx", "html", "css"].includes(ext)) return "Web & TypeScript";
    if (["csv", "xlsx", "xlsm"].includes(ext)) return "Spreadsheets & CSV";
    return "Source Code";
  }
  if (f.modality === "image") {
    if (/(screenshot|screen shot|capture)/.test(blob)) return "Screen Captures";
    if (/(portrait|selfie|girl|men|woman|person|people|family)/.test(blob)) return "Portraits & People";
    return "Camera Photos";
  }
  if (["pptx", "ppt"].includes(ext)) return "Slide Decks";
  if (["md", "txt"].includes(ext)) return "Notes & Markdown";
  if (ext === "pdf") {
    return /(report|research|study|paper|evaluation|imrad)/.test(blob) ? "Research & Reports" : "PDF Documents";
  }
  return "General Files";
}

function formatShelfBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

interface ShelvesViewProps {
  active: boolean;
  files: ShelfFile[];
  shelves: ShelfGroup[];
  selectedFileId: string;
  activeShelfFilter: string;
  activeSubShelfFilter?: string;
  hiddenShelfIds?: string[];
  silhouetteScore: number;
  submode: "list" | "all" | "grid" | "map";
  onSetSubmode: (mode: "list" | "all" | "grid" | "map") => void;
  onSetSubShelfFilter?: (subShelf: string) => void;
  onSelectShelfFilter?: (shelfId: string) => void;
  onToggleHideShelf?: (shelfId: string) => void;
  onRestoreAllHiddenShelves?: () => void;
  onSelectFile: (fileId: string) => void;
  onOpenQuickLook: () => void;
  onClearShelfFilter?: () => void;
  onTogglePinShelf?: (shelfId: string) => void;
  onRenameShelf?: (shelf: ShelfGroup) => void;
  onDeleteCustomShelf?: (shelfId: string) => void;
  onCreateSmartShelfFromSelection?: (fileIds: string[]) => void;
  onAskAboutShelf?: (shelf: ShelfGroup) => void;
  onBulkAction?: (fileIds: string[], action: "move" | "trash" | "tag" | "pin" | "unpin") => Promise<boolean> | boolean | void;
  clearSelectionSignal?: number;
  clipboard?: { fileIds: string[]; mode: "copy" | "cut" } | null;
  onSetClipboard?: (fileIds: string[], mode: "copy" | "cut") => void;
  onPasteClipboard?: () => void;
  onShowToast?: (msg: string) => void;
  onRenameFile?: (file: ShelfFile) => void;
  onMoveFile?: (file: ShelfFile) => void;
  onDeleteFile?: (file: ShelfFile) => void;
  onContextMenuFile?: (e: React.MouseEvent, file: ShelfFile) => void;
}

interface ShelfFileRowProps {
  file: ShelfFile;
  isSelected: boolean;
  isChecked: boolean;
  isCut?: boolean;
  selectMode?: boolean;
  onSelect: (id: string) => void;
  onOpenQuickLook: () => void;
  onToggleChecked: (id: string, shiftKey: boolean) => void;
  onContextMenuFile?: (e: React.MouseEvent, file: ShelfFile) => void;
}

const ShelfFileRow = React.memo<ShelfFileRowProps>(({
  file: f,
  isSelected,
  isChecked,
  isCut = false,
  selectMode = false,
  onSelect,
  onOpenQuickLook,
  onToggleChecked,
  onContextMenuFile,
}) => (
  <div
    data-shelf-file-id={f.id}
    className={`file-item-row ${isSelected || isChecked ? "selected" : ""} ${isCut ? "file-cut-dimmed" : ""}`}
    onClick={(e) => {
      if (selectMode || e.ctrlKey || e.metaKey) {
        onToggleChecked(f.id, e.shiftKey);
      } else {
        onSelect(f.id);
      }
    }}
    onDoubleClick={onOpenQuickLook}
    onContextMenu={(e) => onContextMenuFile?.(e, f)}
  >
    <div style={{ minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: "10px", minWidth: 0 }}>
        <input
          type="checkbox"
          className="row-checkbox"
          style={selectMode ? { opacity: 1 } : undefined}
          checked={isChecked}
          aria-label={`Select ${f.name}`}
          onClick={(e) => {
            e.stopPropagation();
            onToggleChecked(f.id, e.shiftKey);
          }}
          onChange={() => {}}
          title="Select for bulk action (Shift+Click for range)"
        />
        {f.pinned && <span style={{ fontSize: "11px", color: "var(--ink)" }}>★</span>}
        <span
          className="font-display"
          style={{
            fontSize: "16px",
            fontWeight: 400,
            color: "var(--ink)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {f.title}
        </span>
      </div>
      <div
        style={{
          fontSize: "12px",
          color: "var(--ink-3)",
          marginTop: "2px",
          paddingLeft: "24px",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {f.format.toLowerCase()} · {f.size} · {f.date}
      </div>
    </div>

    <div style={{ display: "flex", alignItems: "center", gap: "6px", flexShrink: 0 }}>
      <div className="row-quick-actions" onClick={(e) => e.stopPropagation()}>
        <button
          type="button"
          className="row-icon-btn"
          title="Actions"
          aria-label={`Actions for ${f.name}`}
          onClick={(e) => {
            if (onContextMenuFile) {
              e.stopPropagation();
              onContextMenuFile(e, f);
            } else {
              onSelect(f.id);
              onOpenQuickLook();
            }
          }}
        >
          <MoreHorizontal size={14} />
        </button>
      </div>
    </div>
  </div>
));

const INITIAL_SHELF_LIMIT = 30;

export const ShelvesView = memoView<ShelvesViewProps>(({
  active,
  files,
  shelves,
  selectedFileId,
  activeShelfFilter,
  activeSubShelfFilter = "all",
  hiddenShelfIds,
  submode,
  onSetSubmode,
  onSetSubShelfFilter,
  onToggleHideShelf,
  onRestoreAllHiddenShelves,
  onSelectFile,
  onOpenQuickLook,
  onClearShelfFilter,
  onTogglePinShelf,
  onRenameShelf,
  onDeleteCustomShelf,
  onCreateSmartShelfFromSelection,
  onAskAboutShelf,
  onBulkAction,
  clearSelectionSignal = 0,
  clipboard,
  onSetClipboard,
  onPasteClipboard,
  onShowToast,
  onContextMenuFile,
}) => {
  const [mapTopicFilter, setMapTopicFilter] = useState<string>("all");
  const [mapLabelMode, setMapLabelMode] = useState<"auto" | "compact" | "all">("auto");
  const [hoveredFileId, setHoveredFileId] = useState<string | null>(null);
  const [zoom, setZoom] = useState<number>(1.0);
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState<boolean>(false);
  const [panStart, setPanStart] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [lassoBox, setLassoBox] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null);
  const mapBoxRef = useRef<HTMLDivElement | null>(null);
  const sheetRef = useRef<HTMLElement | null>(null);
  const filterInputRef = useRef<HTMLInputElement | null>(null);
  const [sortBy, setSortBy] = useState<SortMode>("default");
  const [allFilesQuery, setAllFilesQuery] = useState<string>("");
  const [allFilesCategory, setAllFilesCategory] = useState<string>("all");
  const [checkedIds, setCheckedIds] = useState<string[]>([]);
  const [lastCheckedId, setLastCheckedId] = useState<string | null>(null);
  const [selectMode, setSelectMode] = useState<boolean>(false);
  const [shelfLimits, setShelfLimits] = useState<Record<string, number>>({});
  const cutIdSet = useMemo(
    () => new Set(clipboard?.mode === "cut" ? clipboard.fileIds : []),
    [clipboard]
  );

  const [collapsedShelves, setCollapsedShelves] = useState<Record<string, boolean>>(() => {
    try {
      return JSON.parse(window.localStorage.getItem("good-shelves:collapsed-shelves") || "{}");
    } catch {
      return {};
    }
  });
  const [localHiddenShelves, setLocalHiddenShelves] = useState<string[]>(() => {
    try {
      const parsed = JSON.parse(window.localStorage.getItem("good-shelves:hidden-shelves") || "[]");
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });
  const hiddenShelves = hiddenShelfIds ?? localHiddenShelves;
  const [activeSubShelfByShelf, setActiveSubShelfByShelf] = useState<Record<string, string>>({});
  const [showHiddenPanel, setShowHiddenPanel] = useState<boolean>(false);
  const [filtersOpen, setFiltersOpen] = useState<boolean>(false);

  useEffect(() => {
    if (submode !== "map") return;
    const el = mapBoxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const delta = e.deltaY < 0 ? 0.15 : -0.15;
      setZoom((z) => Math.max(0.7, Math.min(2.4, +(z + delta).toFixed(2))));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [submode]);

  useEffect(() => {
    if (activeShelfFilter !== "all" && activeSubShelfFilter && activeSubShelfFilter !== "all") {
      setActiveSubShelfByShelf((prev) => ({ ...prev, [activeShelfFilter]: activeSubShelfFilter }));
    } else if (activeShelfFilter !== "all" && activeSubShelfFilter === "all") {
      setActiveSubShelfByShelf((prev) => ({ ...prev, [activeShelfFilter]: "all" }));
    }
  }, [activeShelfFilter, activeSubShelfFilter]);

  const toggleCollapseShelf = (shelfId: string) => {
    setCollapsedShelves((prev) => {
      const next = { ...prev, [shelfId]: !prev[shelfId] };
      try {
        window.localStorage.setItem("good-shelves:collapsed-shelves", JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  };

  const hideShelf = (shelfId: string) => {
    if (onToggleHideShelf) {
      if (!hiddenShelves.includes(shelfId)) onToggleHideShelf(shelfId);
      return;
    }
    setLocalHiddenShelves((prev) => {
      if (prev.includes(shelfId)) return prev;
      const next = [...prev, shelfId];
      try {
        window.localStorage.setItem("good-shelves:hidden-shelves", JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  };

  const unhideShelf = (shelfId: string) => {
    if (onToggleHideShelf) {
      if (hiddenShelves.includes(shelfId)) onToggleHideShelf(shelfId);
      return;
    }
    setLocalHiddenShelves((prev) => {
      const next = prev.filter((id) => id !== shelfId);
      try {
        window.localStorage.setItem("good-shelves:hidden-shelves", JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  };

  const restoreAllHiddenShelves = () => {
    if (onRestoreAllHiddenShelves) {
      onRestoreAllHiddenShelves();
      setShowHiddenPanel(false);
      return;
    }
    setLocalHiddenShelves([]);
    setShowHiddenPanel(false);
    try {
      window.localStorage.setItem("good-shelves:hidden-shelves", "[]");
    } catch {
      // ignore
    }
  };

  const hiddenShelfObjects = useMemo(
    () => shelves.filter((s) => hiddenShelves.includes(s.id)),
    [shelves, hiddenShelves]
  );

  const visibleShelves = useMemo(() => {
    const filtered = shelves.filter((s) => {
      if (hiddenShelves.includes(s.id)) return false;
      if (activeShelfFilter !== "all") return s.id === activeShelfFilter;
      return true;
    });
    return [...filtered].sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)));
  }, [shelves, activeShelfFilter, hiddenShelves]);

  const filterQueryNorm = allFilesQuery.trim().toLowerCase();

  const matchesGlobalFilters = React.useCallback(
    (f: ShelfFile): boolean => {
      if (!matchesShelfCategory(f, allFilesCategory)) return false;
      if (!filterQueryNorm) return true;
      return `${f.title} ${f.name} ${f.format} ${f.folder} ${f.shelfName} ${f.subShelf || ""} ${(f.tags || []).join(" ")}`
        .toLowerCase()
        .includes(filterQueryNorm);
    },
    [allFilesCategory, filterQueryNorm]
  );

  const allFilesFiltered = useMemo(() => {
    let list = (activeShelfFilter === "all" ? files : files.filter((f) => f.shelfId === activeShelfFilter)).filter(
      matchesGlobalFilters
    );
    if (sortBy !== "default") {
      list = [...list].sort((a, b) => {
        if (sortBy === "name") return a.name.localeCompare(b.name);
        if (sortBy === "date") return b.mtime - a.mtime;
        if (sortBy === "size") return b.sizeBytes - a.sizeBytes;
        if (sortBy === "format") return a.format.localeCompare(b.format);
        return 0;
      });
    }
    return list;
  }, [files, activeShelfFilter, matchesGlobalFilters, sortBy]);

  const allCollapsed =
    visibleShelves.length > 0 && visibleShelves.every((s) => Boolean(collapsedShelves[s.id]));

  const toggleCollapseAll = () => {
    const targetState = !allCollapsed;
    setCollapsedShelves((prev) => {
      const next = { ...prev };
      for (const sh of visibleShelves) next[sh.id] = targetState;
      try {
        window.localStorage.setItem("good-shelves:collapsed-shelves", JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  };

  const filesByShelf = useMemo(() => {
    const map = new Map<string, ShelfFile[]>();
    const fileMap = new Map(files.map((f) => [f.id, f]));
    for (const sh of shelves) {
      if (sh.isCustom && sh.fileIds) {
        const list = sh.fileIds
          .map((id) => fileMap.get(id))
          .filter((item): item is ShelfFile => item !== undefined && matchesGlobalFilters(item));
        map.set(sh.id, list);
      }
    }
    for (const f of files) {
      if (!matchesGlobalFilters(f)) continue;
      const arr = map.get(f.shelfId);
      if (arr) {
        if (!arr.some((x) => x.id === f.id)) arr.push(f);
      } else {
        map.set(f.shelfId, [f]);
      }
    }
    if (sortBy !== "default") {
      for (const [k, list] of map.entries()) {
        const sorted = [...list].sort((a, b) => {
          if (sortBy === "name") return a.name.localeCompare(b.name);
          if (sortBy === "date") return b.mtime - a.mtime;
          if (sortBy === "size") return b.sizeBytes - a.sizeBytes;
          if (sortBy === "format") return a.format.localeCompare(b.format);
          return 0;
        });
        map.set(k, sorted);
      }
    }
    return map;
  }, [files, shelves, sortBy, matchesGlobalFilters]);

  const hasContentFilter = allFilesCategory !== "all" || filterQueryNorm.length > 0;
  const displayShelves = useMemo(
    () =>
      hasContentFilter
        ? visibleShelves.filter((sh) => (filesByShelf.get(sh.id)?.length || 0) > 0)
        : visibleShelves,
    [visibleShelves, hasContentFilter, filesByShelf]
  );

  const hasAnyActiveFilter =
    activeShelfFilter !== "all" ||
    activeSubShelfFilter !== "all" ||
    allFilesCategory !== "all" ||
    filterQueryNorm.length > 0 ||
    mapTopicFilter !== "all" ||
    sortBy !== "default";

  const resetAllShelfFilters = () => {
    onClearShelfFilter?.();
    onSetSubShelfFilter?.("all");
    if (activeShelfFilter !== "all") {
      setActiveSubShelfByShelf((prev) => ({ ...prev, [activeShelfFilter]: "all" }));
    }
    setAllFilesCategory("all");
    setAllFilesQuery("");
    setMapTopicFilter("all");
    setSortBy("default");
  };

  useEffect(() => {
    const valid = new Set(files.map((f) => f.id));
    setCheckedIds((prev) => {
      const next = prev.filter((id) => valid.has(id));
      return next.length === prev.length ? prev : next;
    });
  }, [files]);

  useEffect(() => {
    if (clearSelectionSignal > 0) {
      setCheckedIds([]);
      setSelectMode(false);
    }
  }, [clearSelectionSignal]);

  const flatVisibleFiles = useMemo(
    () =>
      submode === "all"
        ? allFilesFiltered
        : visibleShelves.flatMap((sh) => filesByShelf.get(sh.id) || []),
    [submode, allFilesFiltered, visibleShelves, filesByShelf]
  );

  const toggleChecked = (fileId: string, shiftKey: boolean) => {
    if (shiftKey && lastCheckedId) {
      const idxA = flatVisibleFiles.findIndex((f) => f.id === lastCheckedId);
      const idxB = flatVisibleFiles.findIndex((f) => f.id === fileId);
      if (idxA !== -1 && idxB !== -1) {
        const [lo, hi] = idxA < idxB ? [idxA, idxB] : [idxB, idxA];
        const rangeIds = flatVisibleFiles.slice(lo, hi + 1).map((f) => f.id);
        setCheckedIds((prev) => Array.from(new Set([...prev, ...rangeIds])));
        setLastCheckedId(fileId);
        return;
      }
    }
    setCheckedIds((prev) =>
      prev.includes(fileId) ? prev.filter((id) => id !== fileId) : [...prev, fileId]
    );
    setLastCheckedId(fileId);
  };

  const handleListKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
    const target = e.target as HTMLElement | null;
    const isInput = Boolean(
      target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT")
    );
    const mod = e.ctrlKey || e.metaKey;
    const lower = e.key.toLowerCase();

    if (mod && lower === "f") {
      e.preventDefault();
      e.stopPropagation();
      setFiltersOpen(true);
      requestAnimationFrame(() => {
        filterInputRef.current?.focus();
        filterInputRef.current?.select();
      });
      return;
    }
    if (isInput) return;

    if (e.key === "Escape" && (checkedIds.length > 0 || selectMode)) {
      e.preventDefault();
      e.stopPropagation();
      setCheckedIds([]);
      setSelectMode(false);
      return;
    }
    if (!flatVisibleFiles.length) return;

    if (mod && lower === "a") {
      e.preventDefault();
      e.stopPropagation();
      const allIds = flatVisibleFiles.map((f) => f.id);
      setCheckedIds(allIds);
      onShowToast?.(`Selected ${allIds.length} file${allIds.length === 1 ? "" : "s"}`);
      return;
    }

    if (mod && (lower === "c" || lower === "x")) {
      const textSel = window.getSelection()?.toString();
      if (textSel) return;
      const targetIds = checkedIds.length > 0 ? checkedIds : selectedFileId ? [selectedFileId] : [];
      if (targetIds.length > 0 && onSetClipboard) {
        e.preventDefault();
        e.stopPropagation();
        onSetClipboard(targetIds, lower === "x" ? "cut" : "copy");
      }
      return;
    }

    if (mod && lower === "v" && clipboard?.fileIds.length && onPasteClipboard) {
      e.preventDefault();
      e.stopPropagation();
      onPasteClipboard();
      return;
    }

    if (e.key === "Delete" && checkedIds.length > 1 && onBulkAction) {
      e.preventDefault();
      e.stopPropagation();
      void Promise.resolve(onBulkAction(checkedIds, "trash")).then((ok) => {
        if (ok) {
          setCheckedIds([]);
          setSelectMode(false);
        }
      });
      return;
    }

    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      const idx = flatVisibleFiles.findIndex((f) => f.id === selectedFileId);
      const nextIdx =
        e.key === "ArrowDown"
          ? Math.min(flatVisibleFiles.length - 1, idx + 1)
          : Math.max(0, idx <= 0 ? 0 : idx - 1);
      const nextId = flatVisibleFiles[nextIdx].id;
      onSelectFile(nextId);
      requestAnimationFrame(() => {
        document.querySelector(`[data-shelf-file-id="${CSS.escape(nextId)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
    } else if (e.key === "Enter" && selectedFileId) {
      if (target && (target.tagName === "BUTTON" || target.tagName === "A")) return;
      onOpenQuickLook();
    }
  };

  const { focusFile, neighbors, neighborIdSet, shelfHullRadii, shelfAnchorIds } = useMemo(() => {
    if (submode !== "map") {
      return {
        focusFile: undefined,
        neighbors: [] as ShelfFile[],
        neighborIdSet: new Set<string>(),
        shelfHullRadii: new Map<string, number>(),
        shelfAnchorIds: new Set<string>(),
      };
    }
    const activeFocusId = hoveredFileId || selectedFileId;
    const target = activeFocusId ? files.find((f) => f.id === activeFocusId) : undefined;
    const rel = target
      ? files
          .filter((f) => f.id !== target.id && f.shelfId === target.shelfId)
          .sort(
            (a, b) =>
              Math.hypot(a.mapX - target.mapX, a.mapY - target.mapY) -
              Math.hypot(b.mapX - target.mapX, b.mapY - target.mapY)
          )
          .slice(0, 3)
      : [];
    const nSet = new Set(rel.map((n) => n.id));
    const radii = new Map<string, number>();
    const anchors = new Set<string>();
    for (const sh of shelves) {
      const members = files.filter((f) => f.shelfId === sh.id);
      if (members.length === 0) {
        radii.set(sh.id, 52);
        continue;
      }
      let bestAnchor = members[0];
      let bestAnchorDist = Math.hypot(members[0].mapX - sh.centroidX, members[0].mapY - sh.centroidY);
      const maxSpread = members.reduce((mx, m) => {
        const rawDist = Math.hypot(m.mapX - sh.centroidX, m.mapY - sh.centroidY);
        if (rawDist < bestAnchorDist) {
          bestAnchorDist = rawDist;
          bestAnchor = m;
        }
        const d = Math.hypot((m.mapX - sh.centroidX) * 7.2, (m.mapY - sh.centroidY) * 4.8);
        return Math.max(mx, d);
      }, 0);
      anchors.add(bestAnchor.id);
      radii.set(sh.id, Math.max(44, Math.min(96, Math.round(maxSpread + 24))));
    }
    return {
      focusFile: target,
      neighbors: rel,
      neighborIdSet: nSet,
      shelfHullRadii: radii,
      shelfAnchorIds: anchors,
    };
  }, [submode, files, shelves, hoveredFileId, selectedFileId]);

  const handleMouseDownMap = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest("button")) return;
    if (e.shiftKey && mapBoxRef.current) {
      const rect = mapBoxRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      setLassoBox({ x1: x, y1: y, x2: x, y2: y });
      return;
    }
    setIsPanning(true);
    setPanStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
  };

  const handleMouseMoveMap = (e: React.MouseEvent<HTMLDivElement>) => {
    if (lassoBox && mapBoxRef.current) {
      const rect = mapBoxRef.current.getBoundingClientRect();
      setLassoBox((prev) =>
        prev ? { ...prev, x2: e.clientX - rect.left, y2: e.clientY - rect.top } : null
      );
      return;
    }
    if (!isPanning) return;
    setPan({ x: e.clientX - panStart.x, y: e.clientY - panStart.y });
  };

  const handleMouseUpMap = () => {
    if (lassoBox && mapBoxRef.current) {
      const rect = mapBoxRef.current.getBoundingClientRect();
      const minX = Math.min(lassoBox.x1, lassoBox.x2);
      const maxX = Math.max(lassoBox.x1, lassoBox.x2);
      const minY = Math.min(lassoBox.y1, lassoBox.y2);
      const maxY = Math.max(lassoBox.y1, lassoBox.y2);
      if (maxX - minX > 12 && maxY - minY > 12) {
        const cx = rect.width / 2;
        const cy = rect.height / 2;
        const selected = files.filter((f) => {
          if (mapTopicFilter !== "all" && f.shelfId !== mapTopicFilter) return false;
          const baseX = (f.mapX / 100) * rect.width;
          const baseY = (f.mapY / 100) * rect.height;
          const px = cx + pan.x + (baseX - cx) * zoom;
          const py = cy + pan.y + (baseY - cy) * zoom;
          return px >= minX && px <= maxX && py >= minY && py <= maxY;
        });
        if (selected.length > 0) {
          setCheckedIds(selected.map((f) => f.id));
        }
      }
      setLassoBox(null);
      return;
    }
    setIsPanning(false);
  };

  const resetMapTransform = () => {
    setZoom(1.0);
    setPan({ x: 0, y: 0 });
  };

  const allFilesVirtualizer = useVirtualizer({
    count: submode === "all" ? allFilesFiltered.length : 0,
    getScrollElement: () => sheetRef.current,
    estimateSize: () => 58,
    overscan: 12,
  });

  const activeFilterShelf = useMemo(
    () => (activeShelfFilter === "all" ? null : shelves.find((s) => s.id === activeShelfFilter) || null),
    [shelves, activeShelfFilter]
  );

  return (
    <section
      ref={sheetRef}
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-shelves"
      tabIndex={active ? 0 : -1}
      aria-hidden={!active}
      inert={!active ? true : undefined}
      onKeyDown={handleListKeyDown}
    >
      <div className="page-container">
        <div className="page-header">
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
              <h1 className="page-title">
                {activeFilterShelf ? activeFilterShelf.name : "Shelves"}
              </h1>
              {activeFilterShelf && onClearShelfFilter && (
                <button
                  type="button"
                  className="tag-pill"
                  onClick={onClearShelfFilter}
                  title="Show all shelves"
                >
                  <span>All shelves</span>
                  <X size={12} aria-hidden="true" />
                </button>
              )}
            </div>
            {activeFilterShelf?.desc && (
              <p className="page-subtitle">{activeFilterShelf.desc}</p>
            )}
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            {submode !== "map" && (
              <>
                <button
                  type="button"
                  className={`btn-tactile ${filtersOpen || allFilesCategory !== "all" || allFilesQuery.trim() || sortBy !== "default" ? "active-tab" : ""}`}
                  onClick={() => setFiltersOpen((v) => !v)}
                >
                  Filter &amp; Sort
                </button>
                <button
                  type="button"
                  className={`btn-tactile ${selectMode ? "active-tab" : ""}`}
                  onClick={() => {
                    if (selectMode) setCheckedIds([]);
                    setSelectMode((v) => !v);
                  }}
                >
                  {selectMode ? "Done" : "Select"}
                </button>
                {hiddenShelfObjects.length > 0 && (
                  <button
                    type="button"
                    className={`btn-tactile ${showHiddenPanel ? "active-tab" : ""}`}
                    onClick={() => setShowHiddenPanel((v) => !v)}
                  >
                    Hidden ({hiddenShelfObjects.length})
                  </button>
                )}
              </>
            )}
            <div className="seg-group" role="tablist" aria-label="Shelf view">
              <button
                type="button"
                role="tab"
                aria-selected={submode === "list"}
                className={`seg-btn ${submode === "list" ? "active" : ""}`}
                onClick={() => onSetSubmode("list")}
              >
                Shelves
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={submode === "all"}
                className={`seg-btn ${submode === "all" ? "active" : ""}`}
                onClick={() => onSetSubmode("all")}
              >
                All files
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={submode === "grid"}
                className={`seg-btn ${submode === "grid" ? "active" : ""}`}
                onClick={() => onSetSubmode("grid")}
              >
                Grid
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={submode === "map"}
                className={`seg-btn ${submode === "map" ? "active" : ""}`}
                onClick={() => onSetSubmode("map")}
              >
                Cluster map
              </button>
            </div>
          </div>
        </div>

        {/* Restore Hidden Shelves Strip */}
        {showHiddenPanel && hiddenShelfObjects.length > 0 && submode !== "map" && (
          <div
            className="shelf-section"
            style={{
              padding: "12px 18px",
              marginBottom: "20px",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              flexWrap: "wrap",
              gap: "10px",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
              <span style={{ fontSize: "12.5px", color: "var(--ink-2)" }}>Hidden shelves:</span>
              {hiddenShelfObjects.map((hs) => (
                <button
                  key={`unhide-${hs.id}`}
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "3px 10px", minHeight: "26px", fontSize: "12px" }}
                  onClick={() => unhideShelf(hs.id)}
                >
                  + {hs.name}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="btn-tactile"
              style={{ padding: "3px 10px", minHeight: "26px", fontSize: "12px" }}
              onClick={restoreAllHiddenShelves}
            >
              Restore all
            </button>
          </div>
        )}

        {/* Collapsible Filter & Sort Bar */}
        {files.length > 0 && (filtersOpen || hasAnyActiveFilter) && (
          <div style={{ marginBottom: "18px" }}>
            {filtersOpen && (
              <div className="folder-filter-strip" style={{ marginBottom: hasAnyActiveFilter ? "10px" : 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: "4px", flexWrap: "wrap" }} role="group" aria-label="Filter files by type">
                  {SHELF_CATEGORIES.map((cat) => (
                    <button
                      key={cat.id}
                      type="button"
                      aria-pressed={allFilesCategory === cat.id}
                      className={`folder-filter-tab ${allFilesCategory === cat.id ? "active" : ""}`}
                      onClick={() => setAllFilesCategory(cat.id)}
                    >
                      {cat.label}
                    </button>
                  ))}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                  <select
                    aria-label="Sort files"
                    className="editorial-select"
                    value={sortBy}
                    onChange={(e) => setSortBy(e.target.value as SortMode)}
                  >
                    <option value="default">Shelf order</option>
                    <option value="name">Name</option>
                    <option value="date">Newest</option>
                    <option value="size">Size</option>
                    <option value="format">Format</option>
                  </select>
                  {submode === "list" && visibleShelves.length > 1 && (
                    <button type="button" className="btn-tactile" onClick={toggleCollapseAll}>
                      {allCollapsed ? "Expand all" : "Collapse all"}
                    </button>
                  )}
                  <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
                    <input
                      ref={filterInputRef}
                      type="text"
                      aria-label="Filter visible shelves"
                      value={allFilesQuery}
                      onChange={(e) => setAllFilesQuery(e.target.value)}
                      placeholder="Filter shelves… (Ctrl+F)"
                      className="dialog-input"
                      style={{
                        width: "190px",
                        padding: allFilesQuery ? "5px 28px 5px 11px" : "5px 11px",
                        fontSize: "13px",
                        minHeight: "32px",
                      }}
                    />
                    {allFilesQuery.length > 0 && (
                      <button
                        type="button"
                        aria-label="Clear text filter"
                        title="Clear text filter"
                        onClick={() => setAllFilesQuery("")}
                        style={{
                          position: "absolute",
                          right: "7px",
                          color: "var(--ink-3)",
                          display: "inline-flex",
                          padding: "2px",
                        }}
                      >
                        <X size={13} />
                    </button>
                  )}
                </div>
              </div>
            </div>
            )}

            {hasAnyActiveFilter && (
              <div
                role="region"
                aria-label="Active filters"
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  alignItems: "center",
                  gap: "6px",
                  marginTop: "10px",
                }}
              >
                {activeFilterShelf && onClearShelfFilter && (
                  <button type="button" className="tag-pill" onClick={onClearShelfFilter}>
                    <span>Shelf: {activeFilterShelf.name}</span>
                    <X size={11} aria-hidden="true" />
                  </button>
                )}
                {activeSubShelfFilter !== "all" && (
                  <button
                    type="button"
                    className="tag-pill"
                    onClick={() => {
                      onSetSubShelfFilter?.("all");
                      if (activeShelfFilter !== "all") {
                        setActiveSubShelfByShelf((prev) => ({ ...prev, [activeShelfFilter]: "all" }));
                      }
                    }}
                  >
                    <span>Topic: {activeSubShelfFilter}</span>
                    <X size={11} aria-hidden="true" />
                  </button>
                )}
                {allFilesCategory !== "all" && (
                  <button type="button" className="tag-pill" onClick={() => setAllFilesCategory("all")}>
                    <span>Type: {allFilesCategory}</span>
                    <X size={11} aria-hidden="true" />
                  </button>
                )}
                {allFilesQuery.trim().length > 0 && (
                  <button type="button" className="tag-pill" onClick={() => setAllFilesQuery("")}>
                    <span>Filter: “{allFilesQuery.trim()}”</span>
                    <X size={11} aria-hidden="true" />
                  </button>
                )}
                {mapTopicFilter !== "all" && (
                  <button type="button" className="tag-pill" onClick={() => setMapTopicFilter("all")}>
                    <span>Map: {shelves.find((s) => s.id === mapTopicFilter)?.name || mapTopicFilter}</span>
                    <X size={11} aria-hidden="true" />
                  </button>
                )}
                {sortBy !== "default" && (
                  <button type="button" className="tag-pill" onClick={() => setSortBy("default")}>
                    <span>Sort: {sortBy}</span>
                    <X size={11} aria-hidden="true" />
                  </button>
                )}
                <button
                  type="button"
                  style={{ fontSize: "12px", color: "var(--ink-2)", textDecoration: "underline", marginLeft: "4px" }}
                  onClick={resetAllShelfFilters}
                >
                  Reset
                </button>
              </div>
            )}
          </div>
        )}

        {/* Submode ALL FILES */}
        {submode === "all" && (
          <div id="allFilesManagerWrap">
            <div className="shelf-section">
              <div className="shelf-section-header">
                <span className="font-display" style={{ fontSize: "18px", fontWeight: 500, color: "var(--ink)" }}>
                  All indexed files
                </span>
                <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                  {allFilesFiltered.length} {allFilesFiltered.length === 1 ? "file" : "files"}
                </span>
              </div>
              {allFilesFiltered.length === 0 ? (
                <div style={{ padding: "24px 22px", display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "12px", fontSize: "13.5px", color: "var(--ink-3)" }}>
                  <span>No files match the current filter.</span>
                  {hasAnyActiveFilter && (
                    <button type="button" className="btn-tactile" onClick={resetAllShelfFilters}>
                      Reset filters
                    </button>
                  )}
                </div>
              ) : (
                <div
                  className="shelf-list-surface"
                  style={{
                    height: `${allFilesVirtualizer.getTotalSize()}px`,
                    width: "100%",
                    position: "relative",
                  }}
                >
                  {allFilesVirtualizer.getVirtualItems().map((vRow) => {
                    const f = allFilesFiltered[vRow.index];
                    if (!f) return null;
                    return (
                      <div
                        key={`all-${f.id}`}
                        data-index={vRow.index}
                        ref={allFilesVirtualizer.measureElement}
                        style={{
                          position: "absolute",
                          top: 0,
                          left: 0,
                          width: "100%",
                          transform: `translateY(${vRow.start}px)`,
                        }}
                      >
                        <ShelfFileRow
                          file={f}
                          selectMode={selectMode}
                          isSelected={f.id === selectedFileId}
                          isChecked={checkedIds.includes(f.id)}
                          isCut={cutIdSet.has(f.id)}
                          onSelect={onSelectFile}
                          onOpenQuickLook={onOpenQuickLook}
                          onToggleChecked={toggleChecked}
                          onContextMenuFile={onContextMenuFile}
                        />
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Submode LIST or GRID */}
        {(submode === "list" || submode === "grid") && (
          <div id="shelvesListWrap">
            {displayShelves.length === 0 ? (
              <div className="shelf-section" style={{ padding: "36px 24px", textAlign: "center" }}>
                <div className="font-display" style={{ fontSize: "20px", fontWeight: 400, color: "var(--ink)" }}>
                  {visibleShelves.length > 0 && hasContentFilter
                    ? "No files match your active filters"
                    : hiddenShelfObjects.length > 0
                    ? "All shelves are currently hidden"
                    : "No shelves indexed yet"}
                </div>
                <p style={{ fontSize: "13.5px", color: "var(--ink-2)", marginTop: "6px", maxWidth: "46ch", marginInline: "auto" }}>
                  {visibleShelves.length > 0 && hasContentFilter
                    ? "Clear or adjust the filters above to see your shelves again."
                    : hiddenShelfObjects.length > 0
                    ? "Restore any hidden shelf below to view its files."
                    : "Drop any file on this window, or add a watched folder in Settings."}
                </p>
                {visibleShelves.length > 0 && hasContentFilter && (
                  <button type="button" className="btn-tactile solid" style={{ marginTop: "14px" }} onClick={resetAllShelfFilters}>
                    Reset filters
                  </button>
                )}
                {hiddenShelfObjects.length > 0 && !hasContentFilter && (
                  <button type="button" className="btn-tactile solid" style={{ marginTop: "14px" }} onClick={restoreAllHiddenShelves}>
                    Restore all shelves
                  </button>
                )}
                {activeFilterShelf && onClearShelfFilter && !hasContentFilter && (
                  <button type="button" className="btn-tactile" style={{ marginTop: "14px" }} onClick={onClearShelfFilter}>
                    Show all shelves
                  </button>
                )}
              </div>
            ) : displayShelves.map((shelf) => {
              const items = filesByShelf.get(shelf.id) || [];
              const isCollapsed = Boolean(collapsedShelves[shelf.id]);
              const subBuckets = new Map<string, number>();
              for (const it of items) {
                const subName = inferSubShelf(it);
                subBuckets.set(subName, (subBuckets.get(subName) || 0) + 1);
              }
              const subShelvesList = Array.from(subBuckets.entries())
                .map(([name, count]) => ({ name, count }))
                .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
              const selectedSubShelf = activeSubShelfByShelf[shelf.id] || "all";
              const effectiveSubShelf =
                selectedSubShelf !== "all" && subBuckets.has(selectedSubShelf)
                  ? selectedSubShelf
                  : "all";
              const displayItems =
                effectiveSubShelf === "all"
                  ? items
                  : items.filter((it) => inferSubShelf(it) === effectiveSubShelf);
              const limit = shelfLimits[shelf.id] ?? INITIAL_SHELF_LIMIT;
              const visibleItems = displayItems.slice(0, limit);

              const renderFileRow = (f: ShelfFile) => (
                <ShelfFileRow
                  key={f.id}
                  file={f}
                  selectMode={selectMode}
                  isSelected={f.id === selectedFileId}
                  isChecked={checkedIds.includes(f.id)}
                  isCut={cutIdSet.has(f.id)}
                  onSelect={onSelectFile}
                  onOpenQuickLook={onOpenQuickLook}
                  onToggleChecked={toggleChecked}
                  onContextMenuFile={onContextMenuFile}
                />
              );

              return (
                <div className="shelf-section" key={shelf.id}>
                  <div className="shelf-section-header">
                    <div style={{ display: "flex", alignItems: "center", gap: "10px", minWidth: 0 }}>
                      <button
                        type="button"
                        className="row-icon-btn"
                        aria-expanded={!isCollapsed}
                        aria-label={isCollapsed ? `Expand ${shelf.name}` : `Collapse ${shelf.name}`}
                        onClick={() => toggleCollapseShelf(shelf.id)}
                      >
                        {isCollapsed ? <ChevronRight size={15} /> : <ChevronDown size={15} />}
                      </button>
                      <button
                        type="button"
                        onClick={() => toggleCollapseShelf(shelf.id)}
                        style={{ display: "flex", alignItems: "baseline", gap: "10px", textAlign: "left", minWidth: 0, flexWrap: "wrap" }}
                      >
                        <h2
                          className="font-display"
                          style={{ fontSize: "18.5px", fontWeight: 500, letterSpacing: "-0.02em" }}
                        >
                          {shelf.name}
                        </h2>
                        <span className="shelf-ledger-count">
                          {items.length} {items.length === 1 ? "item" : "items"}
                          {(() => {
                            const totalB = items.reduce((acc, it) => acc + (it.sizeBytes || 0), 0);
                            const fmt = formatShelfBytes(totalB);
                            return fmt ? ` · ${fmt}` : "";
                          })()}
                        </span>
                      </button>
                    </div>
                    <div className="shelf-header-actions">
                      {onAskAboutShelf && (
                        <button
                          type="button"
                          className="shelf-header-subtle-btn"
                          onClick={() => onAskAboutShelf(shelf)}
                        >
                          Ask AI
                        </button>
                      )}
                      {onRenameShelf && shelf.id !== "tray" && (
                        <button
                          type="button"
                          className="shelf-header-subtle-btn"
                          onClick={() => onRenameShelf(shelf)}
                        >
                          Rename
                        </button>
                      )}
                      {shelf.isCustom && onDeleteCustomShelf && (
                        <button
                          type="button"
                          className="shelf-header-subtle-btn danger"
                          onClick={() => onDeleteCustomShelf(shelf.id)}
                        >
                          Remove
                        </button>
                      )}
                      {onTogglePinShelf && (
                        <button
                          type="button"
                          className={`shelf-header-subtle-btn ${shelf.pinned ? "active" : ""}`}
                          onClick={() => onTogglePinShelf(shelf.id)}
                        >
                          {shelf.pinned ? "★ Pinned" : "☆ Pin"}
                        </button>
                      )}
                      <button
                        type="button"
                        className="shelf-header-subtle-btn"
                        onClick={() => {
                          hideShelf(shelf.id);
                          if (activeShelfFilter === shelf.id) {
                            onClearShelfFilter?.();
                          }
                        }}
                        aria-label={`Hide ${shelf.name}`}
                      >
                        Hide
                      </button>
                    </div>
                  </div>

                  {!isCollapsed && (
                    <div>
                      {subShelvesList.length > 1 && (
                        <div
                          style={{
                            padding: "10px 2px 2px",
                            display: "flex",
                            alignItems: "center",
                            gap: "6px",
                            flexWrap: "wrap",
                          }}
                        >
                          {subShelvesList.length > 5 ? (
                            <select
                              aria-label={`Filter ${shelf.name} sub-shelf`}
                              className="editorial-select"
                              style={{ minHeight: "26px", padding: "2px 26px 2px 8px", fontSize: "11.5px" }}
                              value={effectiveSubShelf}
                              onChange={(e) => {
                                const nextSub = e.target.value;
                                setActiveSubShelfByShelf((prev) => ({ ...prev, [shelf.id]: nextSub }));
                                if (activeShelfFilter === shelf.id) onSetSubShelfFilter?.(nextSub);
                              }}
                            >
                              <option value="all">All ({items.length})</option>
                              {subShelvesList.map((sub) => (
                                <option key={`${shelf.id}-sub-${sub.name}`} value={sub.name}>
                                  {sub.name} ({sub.count})
                                </option>
                              ))}
                            </select>
                          ) : (
                            <>
                              <button
                                type="button"
                                className={`btn-tactile ${effectiveSubShelf === "all" ? "solid" : ""}`}
                                style={{ padding: "2px 9px", minHeight: "24px", fontSize: "11.5px" }}
                                onClick={() => {
                                  setActiveSubShelfByShelf((prev) => ({ ...prev, [shelf.id]: "all" }));
                                  if (activeShelfFilter === shelf.id) onSetSubShelfFilter?.("all");
                                }}
                              >
                                All ({items.length})
                              </button>
                              {subShelvesList.map((sub) => (
                                <button
                                  key={`${shelf.id}-sub-${sub.name}`}
                                  type="button"
                                  className={`btn-tactile ${effectiveSubShelf === sub.name ? "solid" : ""}`}
                                  style={{ padding: "2px 9px", minHeight: "24px", fontSize: "11.5px" }}
                                  onClick={() => {
                                    const nextSub = activeSubShelfByShelf[shelf.id] === sub.name ? "all" : sub.name;
                                    setActiveSubShelfByShelf((prev) => ({ ...prev, [shelf.id]: nextSub }));
                                    if (activeShelfFilter === shelf.id) onSetSubShelfFilter?.(nextSub);
                                  }}
                                >
                                  {sub.name} ({sub.count})
                                </button>
                              ))}
                            </>
                          )}
                        </div>
                      )}

                      {displayItems.length === 0 ? (
                        <div style={{ padding: "18px 4px", fontSize: "13.5px", color: "var(--ink-3)" }}>
                          No files currently placed in this shelf.
                        </div>
                      ) : submode === "grid" ? (
                        <div className="file-grid-wrap" style={{ padding: "14px 0 4px" }}>
                          {visibleItems.map((f) => {
                            const isSel = f.id === selectedFileId;
                            const isChecked = checkedIds.includes(f.id);
                            const isCut = cutIdSet.has(f.id);
                            const thumb = resolveLocalAssetSrc(f);
                            return (
                              <div
                                key={f.id}
                                className={`file-grid-card ${isSel || isChecked ? "selected" : ""} ${isCut ? "file-cut-dimmed" : ""}`}
                                onClick={(e) => {
                                  if (selectMode || e.ctrlKey || e.metaKey) {
                                    toggleChecked(f.id, e.shiftKey);
                                  } else {
                                    onSelectFile(f.id);
                                  }
                                }}
                                onDoubleClick={onOpenQuickLook}
                                onContextMenu={(e) => onContextMenuFile?.(e, f)}
                              >
                                <div className="file-grid-thumb">
                                  <input
                                    type="checkbox"
                                    className="row-checkbox"
                                    checked={isChecked}
                                    aria-label={`Select ${f.name}`}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      toggleChecked(f.id, e.shiftKey);
                                    }}
                                    onChange={() => {}}
                                    style={{ position: "absolute", top: "8px", left: "8px", zIndex: 2, ...(selectMode ? { opacity: 1 } : {}) }}
                                  />
                                  {thumb ? (
                                    <img
                                      src={thumb}
                                      alt={f.title}
                                      loading="lazy"
                                      decoding="async"
                                      style={{ width: "100%", height: "100%", objectFit: "cover" }}
                                    />
                                  ) : (
                                    <div
                                      style={{
                                        width: "100%",
                                        height: "100%",
                                        padding: "10px 12px",
                                        display: "flex",
                                        flexDirection: "column",
                                        justifyContent: "space-between",
                                        alignItems: "flex-start",
                                      }}
                                    >
                                      <div style={{ alignSelf: "flex-end" }}>
                                        <span className="file-type-tag">{f.format}</span>
                                      </div>
                                      <div
                                        style={{
                                          fontSize: "10.5px",
                                          lineHeight: 1.4,
                                          color: "var(--ink-3)",
                                          overflow: "hidden",
                                          display: "-webkit-box",
                                          WebkitLineClamp: 4,
                                          WebkitBoxOrient: "vertical",
                                          wordBreak: "break-word",
                                        }}
                                      >
                                        {cleanPreviewText(f.extractedText || f.ocrText || f.whyMatched || f.name).slice(0, 110)}
                                      </div>
                                    </div>
                                  )}
                                </div>
                                <div style={{ padding: "11px 12px" }}>
                                  <div className="font-display" style={{ fontSize: "14px", fontWeight: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                    {f.title}
                                  </div>
                                  <div style={{ fontSize: "11.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                                    {f.format.toLowerCase()} · {f.size}
                                  </div>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="shelf-list-surface">
                          {visibleItems.map(renderFileRow)}
                        </div>
                      )}

                      {displayItems.length > limit && (
                        <div style={{ padding: "10px 4px", marginTop: "6px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                            Showing {visibleItems.length} of {displayItems.length} files
                          </span>
                          <button
                            type="button"
                            className="btn-tactile"
                            style={{ padding: "4px 10px", minHeight: "26px", fontSize: "12px" }}
                            onClick={() =>
                              setShelfLimits((prev) => ({
                                ...prev,
                                [shelf.id]: (prev[shelf.id] ?? INITIAL_SHELF_LIMIT) + 40,
                              }))
                            }
                          >
                            Show {Math.min(40, displayItems.length - visibleItems.length)} more
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Submode MAP */}
        {submode === "map" && (
          <div id="shelvesMapWrap">
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "12px",
                marginBottom: "14px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
                <select
                  aria-label="Filter cluster map by shelf"
                  className="editorial-select"
                  value={mapTopicFilter}
                  onChange={(e) => setMapTopicFilter(e.target.value)}
                >
                  <option value="all">All shelves ({shelves.filter((s) => s.id !== "tray").length})</option>
                  {shelves
                    .filter((s) => s.id !== "tray")
                    .map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                </select>
                <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                  Drag to pan · Scroll to zoom · Shift+Drag to select
                </span>
              </div>

              <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "4px 9px", minHeight: "28px", fontSize: "11.5px" }}
                  onClick={() =>
                    setMapLabelMode((m) => (m === "auto" ? "compact" : m === "compact" ? "all" : "auto"))
                  }
                >
                  Labels: {mapLabelMode}
                </button>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "4px 9px", minHeight: "28px", fontSize: "12px" }}
                  onClick={() => setZoom((z) => Math.max(0.7, +(z - 0.2).toFixed(1)))}
                  aria-label="Zoom out map"
                >
                  −
                </button>
                <span style={{ fontSize: "11.5px", color: "var(--ink-2)", minWidth: "40px", textAlign: "center" }}>
                  {Math.round(zoom * 100)}%
                </span>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "4px 9px", minHeight: "28px", fontSize: "12px" }}
                  onClick={() => setZoom((z) => Math.min(2.4, +(z + 0.2).toFixed(1)))}
                  aria-label="Zoom in map"
                >
                  +
                </button>
                {(zoom !== 1.0 || pan.x !== 0 || pan.y !== 0) && (
                  <button
                    type="button"
                    className="btn-tactile"
                    style={{ padding: "4px 9px", minHeight: "28px", fontSize: "11.5px" }}
                    onClick={resetMapTransform}
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>

            <div
              ref={mapBoxRef}
              className="meaning-map-box"
              id="meaningMapCanvas"
              onMouseDown={handleMouseDownMap}
              onMouseMove={handleMouseMoveMap}
              onMouseUp={handleMouseUpMap}
              onMouseLeave={handleMouseUpMap}
              style={{ cursor: isPanning ? "grabbing" : "grab" }}
            >
              {lassoBox && (
                <div
                  style={{
                    position: "absolute",
                    left: `${Math.min(lassoBox.x1, lassoBox.x2)}px`,
                    top: `${Math.min(lassoBox.y1, lassoBox.y2)}px`,
                    width: `${Math.abs(lassoBox.x2 - lassoBox.x1)}px`,
                    height: `${Math.abs(lassoBox.y2 - lassoBox.y1)}px`,
                    border: "1.5px dashed var(--ink)",
                    background: "var(--focus-ring)",
                    zIndex: 40,
                    pointerEvents: "none",
                  }}
                />
              )}
              <div
                style={{
                  position: "absolute",
                  inset: 0,
                  transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                  transformOrigin: "center center",
                  transition: isPanning ? "none" : "transform 0.22s var(--ease)",
                }}
              >
                <svg width="100%" height="100%" style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
                  <line x1="50%" y1="0" x2="50%" y2="100%" stroke="var(--rule)" strokeWidth="1" strokeDasharray="2 5" />
                  <line x1="0" y1="50%" x2="100%" y2="50%" stroke="var(--rule)" strokeWidth="1" strokeDasharray="2 5" />

                  {shelves
                    .filter((s) => s.id !== "tray")
                    .map((s) => (
                      <circle
                        key={`hull-${s.id}`}
                        cx={`${s.centroidX}%`}
                        cy={`${s.centroidY}%`}
                        r={shelfHullRadii.get(s.id) ?? 64}
                        fill="none"
                        stroke="var(--rule-strong)"
                        strokeWidth="1"
                        strokeDasharray="4 4"
                      />
                    ))}

                  {focusFile &&
                    neighbors.map((n) => (
                      <line
                        key={`edge-${focusFile.id}-${n.id}`}
                        x1={`${focusFile.mapX}%`}
                        y1={`${focusFile.mapY}%`}
                        x2={`${n.mapX}%`}
                        y2={`${n.mapY}%`}
                        stroke="var(--ink)"
                        strokeWidth="1.2"
                        strokeDasharray="3 4"
                        opacity="0.65"
                      />
                    ))}
                </svg>

                {shelves
                  .filter((s) => s.id !== "tray")
                  .map((s) => (
                    <span
                      key={`lbl-${s.id}`}
                      style={{
                        position: "absolute",
                        left: `${s.centroidX}%`,
                        top: `${Math.max(5, s.centroidY - Math.round((shelfHullRadii.get(s.id) ?? 56) / 5.4))}%`,
                        transform: "translate(-50%, -100%)",
                        fontSize: "11.5px",
                        fontWeight: 500,
                        color: "var(--ink-2)",
                        background: "var(--bg-canvas)",
                        padding: "2px 8px",
                        borderRadius: "6px",
                        border: "1px solid var(--rule)",
                        pointerEvents: "none",
                        whiteSpace: "nowrap",
                        zIndex: 5,
                      }}
                    >
                      {s.name}
                    </span>
                  ))}

                {files.map((f) => {
                  const isSel = f.id === selectedFileId;
                  const isChecked = checkedIds.includes(f.id);
                  const isNeighbor = neighborIdSet.has(f.id);
                  const isHovered = hoveredFileId === f.id;
                  const showLabel =
                    mapLabelMode === "all"
                      ? true
                      : mapLabelMode === "compact"
                      ? isSel || isChecked || isHovered || isNeighbor
                      : files.length <= 14 ||
                        zoom >= 1.35 ||
                        isSel ||
                        isChecked ||
                        isHovered ||
                        isNeighbor ||
                        shelfAnchorIds.has(f.id);
                  const dimmed =
                    (mapTopicFilter !== "all" && f.shelfId !== mapTopicFilter) ||
                    !matchesGlobalFilters(f);
                  const shortLabel = f.title.split(" ").slice(0, 3).join(" ");
                  const thumb = zoom >= 1.3 ? resolveLocalAssetSrc(f) : null;
                  return (
                    <button
                      key={f.id}
                      type="button"
                      title={f.title}
                      aria-label={f.title}
                      className={`map-point-btn ${isSel || isChecked ? "selected" : ""}`}
                      style={{
                        left: `${f.mapX}%`,
                        top: `${f.mapY}%`,
                        opacity: dimmed ? 0.24 : 1,
                        padding: showLabel ? "5px 10px" : "5px",
                        borderRadius: showLabel ? "6px" : "999px",
                      }}
                      onMouseEnter={() => setHoveredFileId(f.id)}
                      onMouseLeave={() => setHoveredFileId(null)}
                      onClick={() => onSelectFile(f.id)}
                      onDoubleClick={onOpenQuickLook}
                    >
                      {thumb ? (
                        <img
                          src={thumb}
                          alt=""
                          decoding="async"
                          style={{ width: "18px", height: "18px", borderRadius: "3px", objectFit: "cover" }}
                        />
                      ) : (
                        <span
                          style={{
                            width: "6px",
                            height: "6px",
                            borderRadius: "50%",
                            background: "currentColor",
                          }}
                        />
                      )}
                      {showLabel && <span>{shortLabel}</span>}
                    </button>
                  );
                })}
              </div>
            </div>

            <div
              style={{
                marginTop: "12px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                fontSize: "12.5px",
                color: "var(--ink-2)",
              }}
            >
              <span id="mapFooterStatus">
                {focusFile
                  ? `Focused: ${focusFile.title} · ${neighbors.length} nearby files.`
                  : `Showing ${files.length} files grouped by topic.`}
              </span>
            </div>
          </div>
        )}
      </div>

      {checkedIds.length > 0 && onBulkAction && (
        <div className="bulk-action-bar" role="region" aria-label="Bulk actions for selected files">
          <span style={{ fontSize: "12.5px", fontWeight: 600 }}>
            {checkedIds.length} selected
          </span>
          <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
            {onSetClipboard && (
              <>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ fontSize: "12px", padding: "4px 10px" }}
                  onClick={() => onSetClipboard(checkedIds, "copy")}
                >
                  Copy
                </button>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ fontSize: "12px", padding: "4px 10px" }}
                  onClick={() => onSetClipboard(checkedIds, "cut")}
                >
                  Cut
                </button>
              </>
            )}
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "12px", padding: "4px 10px" }}
              onClick={() => onBulkAction(checkedIds, "tag")}
            >
              Tag…
            </button>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "12px", padding: "4px 10px" }}
              onClick={async () => {
                const ok = await onBulkAction(checkedIds, "move");
                if (ok) {
                  setCheckedIds([]);
                  setSelectMode(false);
                }
              }}
            >
              Move…
            </button>
            {onCreateSmartShelfFromSelection && (
              <button
                type="button"
                className="btn-tactile solid"
                style={{ fontSize: "12px", padding: "4px 10px" }}
                onClick={() => onCreateSmartShelfFromSelection(checkedIds)}
              >
                Save as shelf
              </button>
            )}
            <button
              type="button"
              className="btn-tactile danger"
              style={{ fontSize: "12px", padding: "4px 10px" }}
              onClick={async () => {
                const ok = await onBulkAction(checkedIds, "trash");
                if (ok) {
                  setCheckedIds([]);
                  setSelectMode(false);
                }
              }}
            >
              Trash
            </button>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "12px", padding: "4px 10px" }}
              onClick={() => {
                setCheckedIds([]);
                setSelectMode(false);
              }}
            >
              Clear
            </button>
          </div>
        </div>
      )}
    </section>
  );
});
