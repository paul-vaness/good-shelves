import React, { useEffect, useMemo, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { ArrowUpRight, HardDrive, Moon, Search, Sparkles, Sun, Undo2, X } from "lucide-react";
import {
  ActionHistoryItem,
  AiIngestionReport,
  ConnectedDevice,
  LookalikePair,
  PinnedFolder,
  PipelineStageEvent,
  ShelfFile,
  ShelfGroup,
  StorageStats,
  WatchedFolder,
  isTauriRuntime,
} from "../lib/tauri-api";
import { formatShortcut } from "../lib/platform";
import type { ViewId } from "./TopMemoryBar";

function scoreFileForInstantJump(f: ShelfFile, qLower: string, tokens: string[]): number {
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
  const amount = String(f.metadata?.amount || "").toLowerCase();
  const invoiceNum = String(f.metadata?.invoice_number || "").toLowerCase();
  const textBody = `${f.extractedText || ""} ${f.ocrText || ""}`.toLowerCase();

  let totalScore = 0;

  // Phrase bonus
  if (title === qLower || name === qLower) {
    totalScore += 180;
  } else if (title.startsWith(qLower) || name.startsWith(qLower)) {
    totalScore += 120;
  } else if (title.includes(qLower) || name.includes(qLower)) {
    totalScore += 75;
  }

  if (merchant && merchant.includes(qLower)) totalScore += 65;
  if (docType && docType.includes(qLower)) totalScore += 50;

  for (const tok of tokens) {
    let tokScore = 0;

    if (title === tok || name === tok) {
      tokScore = Math.max(tokScore, 90);
    } else if (title.startsWith(tok) || name.startsWith(tok)) {
      tokScore = Math.max(tokScore, 70);
    } else if (
      title.split(/[\s._\-/]+/).some((w) => w.startsWith(tok)) ||
      name.split(/[\s._\-/]+/).some((w) => w.startsWith(tok))
    ) {
      tokScore = Math.max(tokScore, 55);
    } else if (title.includes(tok) || name.includes(tok)) {
      tokScore = Math.max(tokScore, 40);
    }

    if (tags.some((t) => t === tok)) {
      tokScore = Math.max(tokScore, 65);
    } else if (tags.some((t) => t.startsWith(tok) || t.includes(tok))) {
      tokScore = Math.max(tokScore, 45);
    }

    if (merchant) {
      if (merchant.startsWith(tok)) tokScore = Math.max(tokScore, 65);
      else if (merchant.includes(tok)) tokScore = Math.max(tokScore, 48);
    }

    if (docType) {
      if (docType.startsWith(tok)) tokScore = Math.max(tokScore, 50);
      else if (docType.includes(tok)) tokScore = Math.max(tokScore, 36);
    }

    if (amount && amount.includes(tok)) {
      tokScore = Math.max(tokScore, 45);
    }

    if (invoiceNum && invoiceNum.includes(tok)) {
      tokScore = Math.max(tokScore, 55);
    }

    if (format === tok || modality === tok) {
      tokScore = Math.max(tokScore, 45);
    } else if (format.startsWith(tok)) {
      tokScore = Math.max(tokScore, 32);
    }

    if (shelfName.includes(tok) || subShelf.includes(tok) || folder.includes(tok)) {
      tokScore = Math.max(tokScore, 30);
    }

    if (tokScore === 0 && textBody.includes(tok)) {
      tokScore = 18;
    } else if (tokScore > 0 && textBody.includes(tok)) {
      tokScore += 6;
    }

    if (tokScore === 0) {
      return 0; // Require all tokens to match at least one signal
    }
    totalScore += tokScore;
  }

  if (totalScore > 0 && f.pinned) {
    totalScore += 8;
  }

  return totalScore;
}

function scoreShelfForInstantJump(
  shelf: ShelfGroup,
  qLower: string,
  tokens: string[]
): number {
  const name = (shelf.name || "").toLowerCase();
  const desc = (shelf.desc || "").toLowerCase();
  const subs = (shelf.subShelves || []).map((s) => s.name.toLowerCase());

  let score = 0;
  if (name === qLower) score += 140;
  else if (name.startsWith(qLower)) score += 100;
  else if (name.includes(qLower)) score += 70;

  for (const tok of tokens) {
    let tokScore = 0;
    if (name === tok) tokScore = 80;
    else if (name.startsWith(tok)) tokScore = 65;
    else if (name.split(/[\s._\-/&]+/).some((w) => w.startsWith(tok))) tokScore = 52;
    else if (name.includes(tok)) tokScore = 38;
    else if (subs.some((s) => s.includes(tok))) tokScore = 30;
    else if (desc.includes(tok)) tokScore = 20;

    if (tokScore === 0) return 0;
    score += tokScore;
  }

  if (score > 0 && shelf.pinned) score += 6;
  return score;
}

export interface LivePipelineTrace {
  stage: number;
  stageName: string;
  status: string;
  fileName: string;
  fileId?: string | null;
  shelfName?: string | null;
  message: string;
  elapsedMs?: number;
  timestamp: number;
  isLive: boolean;
}

export interface DragHoverDescriptor {
  active: boolean;
  kind: "files" | "browser-asset" | "text" | "unknown";
  count: number;
  label: string;
}

export function extractBrowserDropPayload(dt: DataTransfer): {
  files: File[];
  browserPayload: {
    url?: string;
    dataUrl?: string;
    html?: string;
    text?: string;
    filename?: string;
    title?: string;
  } | null;
} {
  const files = Array.from(dt.files || []);
  if (files.length > 0) {
    return { files, browserPayload: null };
  }
  const uriList = (dt.getData("text/uri-list") || "").trim();
  const html = (dt.getData("text/html") || "").trim();
  const plain = (dt.getData("text/plain") || "").trim();

  let url = "";
  let dataUrl = "";
  let title = "";
  let filename = "";

  if (uriList) {
    const firstUri = uriList
      .split(/\r?\n/)
      .map((l) => l.trim())
      .find((l) => l && !l.startsWith("#"));
    if (firstUri) {
      if (firstUri.startsWith("data:")) {
        dataUrl = firstUri;
      } else {
        url = firstUri;
      }
    }
  }

  if (html) {
    const imgMatch = html.match(/<img[^>]+src=["']([^"']+)["'][^>]*>/i);
    const altMatch = html.match(/<img[^>]+alt=["']([^"']+)["'][^>]*>/i);
    const linkMatch = html.match(/<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/i);
    if (imgMatch && imgMatch[1]) {
      const src = imgMatch[1].trim();
      if (src.startsWith("data:")) dataUrl = dataUrl || src;
      else url = url || src;
    } else if (linkMatch && linkMatch[1]) {
      url = url || linkMatch[1].trim();
      const linkText = linkMatch[2].replace(/<[^>]+>/g, "").trim();
      if (linkText) title = linkText;
    }
    if (altMatch && altMatch[1] && !title) {
      title = altMatch[1].trim();
    }
  }

  if (!url && !dataUrl && plain) {
    if (plain.startsWith("data:")) {
      dataUrl = plain;
    } else if (/^(?:https?|file):\/\/\S+$/i.test(plain)) {
      url = plain;
    }
  }

  if (url) {
    try {
      const parsedUrl = new URL(url);
      const base = decodeURIComponent(parsedUrl.pathname.split("/").filter(Boolean).pop() || "");
      if (base && base.length <= 120) filename = base;
    } catch {
      // ignore invalid URL
    }
  }

  if (url || dataUrl || html || plain) {
    return {
      files: [],
      browserPayload: {
        url: url || undefined,
        dataUrl: dataUrl || undefined,
        html: html || undefined,
        text: plain || undefined,
        filename: filename || undefined,
        title: title || undefined,
      },
    };
  }
  return { files: [], browserPayload: null };
}

export function describeDataTransferHover(dt: DataTransfer | null): DragHoverDescriptor {
  if (!dt) {
    return { active: true, kind: "files", count: 1, label: "Drop file to run full 4-stage AI indexing" };
  }
  const items = Array.from(dt.items || []);
  const types = Array.from(dt.types || []);
  const fileItems = items.filter((it) => it.kind === "file");
  if (fileItems.length > 0 || types.includes("Files")) {
    const count = Math.max(1, fileItems.length || dt.files?.length || 1);
    const mimeHints = fileItems
      .map((it) => it.type)
      .filter(Boolean)
      .map((m) => m.split("/").pop()?.toUpperCase())
      .filter(Boolean);
    const hintStr = mimeHints.length > 0 ? ` (${Array.from(new Set(mimeHints)).slice(0, 3).join(", ")})` : "";
    return {
      active: true,
      kind: "files",
      count,
      label: `Drop ${count} ${count === 1 ? "file" : "files"}${hintStr} to run Parse/OCR → 512-d Embed → Auto-Shelve & Tag → Context Dossier`,
    };
  }
  if (types.includes("text/uri-list") || types.includes("text/html")) {
    return {
      active: true,
      kind: "browser-asset",
      count: 1,
      label: "Drop browser image, link, or web asset to download, OCR, embed, auto-tag & build context dossier",
    };
  }
  if (types.includes("text/plain")) {
    return {
      active: true,
      kind: "text",
      count: 1,
      label: "Drop text snippet from browser to save as a Markdown note and index into your shelves",
    };
  }
  return {
    active: true,
    kind: "unknown",
    count: 1,
    label: "Drop item here to automatically parse, embed, shelve & analyze with local AI",
  };
}

interface HomeLauncherViewProps {
  active: boolean;
  files: ShelfFile[];
  shelves: ShelfGroup[];
  hiddenShelfIds: string[];
  lookalikes: LookalikePair[];
  watchedFolders: WatchedFolder[];
  pinnedFolders: PinnedFolder[];
  connectedDevices: ConnectedDevice[];
  actionHistory: ActionHistoryItem[];
  storageStats: StorageStats;
  silhouetteScore: number;
  trashCount: number;
  cleanupCount: number;
  isIndexing: boolean;
  isNightMode: boolean;
  globalDragOver?: boolean;
  latestIngestionReport?: AiIngestionReport | null;
  liveProgress: {
    operation: string;
    current: number;
    total: number;
    file_name: string;
  } | null;
  pipelineTrace: LivePipelineTrace | null;
  recentEvents?: PipelineStageEvent[];
  onSwitchView: (view: ViewId) => void;
  onJumpToShelf: (shelfId: string) => void;
  onCreateSmartShelf: () => void;
  onSearchSubmit: (query: string) => void;
  onAskSubmit: (query: string) => void;
  onOpenSmartClean: () => void;
  onAddWatchFolder: () => void;
  onUploadRealFile: (files: File | File[]) => void;
  onDropBrowserPayload?: (payload: {
    url?: string;
    dataUrl?: string;
    html?: string;
    text?: string;
    filename?: string;
    title?: string;
  }) => void;
  onApplySuggestedRename?: (fileId: string) => void;
  onDismissIngestionReport?: () => void;
  onInspectFile: (fileId: string) => void;
  onOpenPinnedFolder: (path: string) => void;
  onScanDevice: (path: string, addWatch: boolean) => void;
  onImportDevice: (path: string) => void;
  onUndo?: () => void;
  onToggleTheme: () => void;
}

export const HomeLauncherView: React.FC<HomeLauncherViewProps> = React.memo(
  ({
    active,
    files,
    shelves,
    hiddenShelfIds,
    lookalikes,
    watchedFolders,
    pinnedFolders,
    connectedDevices,
    actionHistory,
    trashCount,
    cleanupCount,
    isIndexing,
    isNightMode,
    globalDragOver = false,
    latestIngestionReport = null,
    liveProgress,
    pipelineTrace,
    onSwitchView,
    onJumpToShelf,
    onCreateSmartShelf,
    onSearchSubmit,
    onAskSubmit,
    onOpenSmartClean,
    onAddWatchFolder,
    onUploadRealFile,
    onDropBrowserPayload,
    onApplySuggestedRename,
    onDismissIngestionReport,
    onInspectFile,
    onOpenPinnedFolder,
    onScanDevice,
    onImportDevice,
    onUndo,
    onToggleTheme,
  }) => {
    const [homeQuery, setHomeQuery] = useState("");
    const [dragOver, setDragOver] = useState(false);
    const [hoverDesc, setHoverDesc] = useState<DragHoverDescriptor | null>(null);
    const [searchFocused, setSearchFocused] = useState(false);
    const [activeResultIndex, setActiveResultIndex] = useState(-1);
    const searchInputRef = useRef<HTMLInputElement | null>(null);
    const searchAreaRef = useRef<HTMLDivElement | null>(null);
    const fileInputRef = useRef<HTMLInputElement | null>(null);

    useEffect(() => {
      if (!active) return;
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
          searchInputRef.current?.focus();
          searchInputRef.current?.select();
        }
      };
      window.addEventListener("keydown", onKeyDown);
      return () => window.removeEventListener("keydown", onKeyDown);
    }, [active]);

    useEffect(() => {
      const handlePointerDown = (e: MouseEvent) => {
        if (
          searchAreaRef.current &&
          !searchAreaRef.current.contains(e.target as Node | null)
        ) {
          setSearchFocused(false);
        }
      };
      window.addEventListener("mousedown", handlePointerDown);
      return () => window.removeEventListener("mousedown", handlePointerDown);
    }, []);

    const {
      visibleShelves,
      shelfCounts,
      tidyCount,
      dupCount,
      activeWatchedCount,
      undoableCount,
    } = useMemo(() => {
      const hiddenSet = new Set(hiddenShelfIds);
      const counts = new Map<string, number>();
      let tidy = 0;
      for (const f of files) {
        if (f.suggestedName && !f.renamed) tidy += 1;
        counts.set(f.shelfId, (counts.get(f.shelfId) || 0) + 1);
      }
      const vis = shelves
        .filter((s) => !hiddenSet.has(s.id) && s.id !== "tray")
        .sort((a, b) => {
          const pinDiff = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
          if (pinDiff !== 0) return pinDiff;
          const ca = a.isCustom && a.fileIds ? a.fileIds.length : counts.get(a.id) || 0;
          const cb = b.isCustom && b.fileIds ? b.fileIds.length : counts.get(b.id) || 0;
          return cb - ca;
        });

      return {
        visibleShelves: vis,
        shelfCounts: counts,
        tidyCount: tidy,
        dupCount: lookalikes.filter((d) => !d.resolved).length,
        activeWatchedCount: watchedFolders.filter((w) => w.active).length,
        undoableCount: actionHistory.filter((a) => !a.undone).length,
      };
    }, [files, shelves, hiddenShelfIds, lookalikes, watchedFolders, actionHistory]);

    const isBusy = Boolean(isIndexing || liveProgress || pipelineTrace?.isLive);

    const trimmedQuery = homeQuery.trim();
    const { instantShelves, instantFiles } = useMemo(() => {
      if (trimmedQuery.length < 2) {
        return { instantShelves: [] as ShelfGroup[], instantFiles: [] as ShelfFile[] };
      }
      const qLower = trimmedQuery.toLowerCase();
      const tokens = qLower.split(/\s+/).filter(Boolean);

      const scoredShelves = visibleShelves
        .map((s) => ({ shelf: s, score: scoreShelfForInstantJump(s, qLower, tokens) }))
        .filter((x) => x.score > 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, 2)
        .map((x) => x.shelf);

      const scoredFiles = files
        .map((f) => ({ file: f, score: scoreFileForInstantJump(f, qLower, tokens) }))
        .filter((x) => x.score > 0)
        .sort((a, b) => {
          if (b.score !== a.score) return b.score - a.score;
          return (b.file.mtime || 0) - (a.file.mtime || 0);
        })
        .slice(0, 4)
        .map((x) => x.file);

      return { instantShelves: scoredShelves, instantFiles: scoredFiles };
    }, [trimmedQuery, visibleShelves, files]);

    const showInstantPreview = searchFocused && trimmedQuery.length >= 2;
    const totalNavItems = instantFiles.length + 2; // files + [Search all, Ask AI]

    useEffect(() => {
      setActiveResultIndex(-1);
    }, [trimmedQuery]);

    const handleTopDragDoubleClick = (e: React.MouseEvent<HTMLDivElement>) => {
      if (!isTauriRuntime()) return;
      const target = e.target as HTMLElement | null;
      if (target?.closest("button")) return;
      void getCurrentWindow().toggleMaximize();
    };

    const handleSearchFormSubmit = (e: React.FormEvent) => {
      e.preventDefault();
      if (!trimmedQuery) {
        onSearchSubmit("");
        return;
      }
      if (showInstantPreview && activeResultIndex >= 0) {
        if (activeResultIndex < instantFiles.length) {
          onInspectFile(instantFiles[activeResultIndex].id);
          setSearchFocused(false);
          return;
        }
        if (activeResultIndex === instantFiles.length) {
          onSearchSubmit(trimmedQuery);
          setSearchFocused(false);
          return;
        }
        if (activeResultIndex === instantFiles.length + 1) {
          onAskSubmit(trimmedQuery);
          setSearchFocused(false);
          return;
        }
      }
      onSearchSubmit(trimmedQuery);
      setSearchFocused(false);
    };

    const handleAskClick = () => {
      onAskSubmit(trimmedQuery);
      setSearchFocused(false);
    };

    const handleSearchInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        if (trimmedQuery) {
          onAskSubmit(trimmedQuery);
          setSearchFocused(false);
        }
        return;
      }

      if (e.key === "Escape") {
        e.preventDefault();
        if (homeQuery.length > 0) {
          setHomeQuery("");
          setActiveResultIndex(-1);
        } else {
          setSearchFocused(false);
          searchInputRef.current?.blur();
        }
        return;
      }

      if (!showInstantPreview) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveResultIndex((prev) => {
          if (prev < 0) return 0;
          return (prev + 1) % totalNavItems;
        });
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveResultIndex((prev) => {
          if (prev <= 0) return totalNavItems - 1;
          return prev - 1;
        });
      }
    };

    const handleFilePick = (e: React.ChangeEvent<HTMLInputElement>) => {
      const picked = Array.from(e.target.files || []);
      e.target.value = "";
      if (picked.length > 0) {
        onUploadRealFile(picked);
      }
    };

    const traceFileExists = Boolean(
      pipelineTrace?.fileId && files.some((f) => f.id === pipelineTrace.fileId)
    );

    const liveLabel = liveProgress
      ? liveProgress.operation
      : pipelineTrace?.stageName || "Indexing";

    const liveFileName =
      liveProgress?.file_name || pipelineTrace?.fileName || "Scanning files…";

    const liveRightMetric = liveProgress
      ? liveProgress.total > 1
        ? `${liveProgress.current}/${liveProgress.total}`
        : "Working…"
      : typeof pipelineTrace?.elapsedMs === "number" && pipelineTrace.elapsedMs > 0
      ? `${Math.round(pipelineTrace.elapsedMs)} ms`
      : "";

    const isHoverActive = Boolean(dragOver || globalDragOver);
    const currentStage = pipelineTrace?.isLive ? pipelineTrace.stage : isIndexing ? 2 : 0;

    return (
      <section
        className={`view-sheet home-launcher-sheet ${active ? "active" : ""}`}
        id="sheet-home"
        aria-hidden={!active}
        inert={!active ? true : undefined}
        onDragEnter={(e) => {
          e.preventDefault();
          setDragOver(true);
          setHoverDesc(describeDataTransferHover(e.dataTransfer));
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (e.dataTransfer) {
            e.dataTransfer.dropEffect = "copy";
          }
          if (!dragOver) {
            setDragOver(true);
            setHoverDesc(describeDataTransferHover(e.dataTransfer));
          }
        }}
        onDragLeave={(e) => {
          if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
          setDragOver(false);
          setHoverDesc(null);
        }}
        onDrop={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setDragOver(false);
          setHoverDesc(null);
          if (!e.dataTransfer) return;
          const { files: droppedFiles, browserPayload } = extractBrowserDropPayload(e.dataTransfer);
          if (droppedFiles.length > 0) {
            onUploadRealFile(droppedFiles.slice(0, 10));
          } else if (browserPayload && onDropBrowserPayload) {
            onDropBrowserPayload(browserPayload);
          }
        }}
      >
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={handleFilePick}
          aria-hidden="true"
        />

        {/* Invisible Top Drag Strip for Tauri Window Dragging */}
        <div
          className="home-top-drag-strip"
          data-tauri-drag-region
          onDoubleClick={handleTopDragDoubleClick}
        />

        {/* Calm 960px Editorial Home Canvas */}
        <div
          className="home-launcher-canvas"
          style={
            dragOver
              ? { outline: "1.5px dashed var(--ink)", outlineOffset: "-8px", borderRadius: "10px" }
              : undefined
          }
        >
          {/* Eye-Level Brand Header & Theme / Undo Actions */}
          <header className="home-brand-header">
            <div>
              <h1 className="home-brand-title font-display">good shelves</h1>
              <p className="home-brand-sub tabular-nums">
                {files.length} {files.length === 1 ? "file" : "files"} ·{" "}
                {visibleShelves.length}{" "}
                {visibleShelves.length === 1 ? "shelf" : "shelves"} ·{" "}
                {activeWatchedCount} watched{" "}
                {activeWatchedCount === 1 ? "folder" : "folders"}
              </p>
            </div>
            <div className="home-brand-actions">
              {undoableCount > 0 && onUndo && (
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
                {isNightMode ? <Sun size={13} aria-hidden="true" /> : <Moon size={13} aria-hidden="true" />}
                <span>{isNightMode ? "Light" : "Dark"}</span>
              </button>
            </div>
          </header>

          {/* Search & Ask Bar + Calm Instant-Jump Preview */}
          <div
            ref={searchAreaRef}
            style={{ display: "flex", flexDirection: "column", gap: "8px" }}
            onFocusCapture={() => setSearchFocused(true)}
          >
            <form
              className="home-search-bar"
              onSubmit={handleSearchFormSubmit}
              role="search"
              aria-label="Search or ask your shelves"
            >
              <Search
                size={16}
                strokeWidth={1.75}
                style={{ color: "var(--ink-3)", flexShrink: 0 }}
                aria-hidden="true"
              />
              <input
                ref={searchInputRef}
                id="homeLauncherSearchInput"
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={homeQuery}
                onChange={(e) => {
                  setHomeQuery(e.target.value);
                  setSearchFocused(true);
                }}
                onKeyDown={handleSearchInputKeyDown}
                placeholder="Search files, OCR text, or ask a question…"
                aria-label="Search files or ask AI"
                aria-expanded={showInstantPreview}
                aria-controls={showInstantPreview ? "homeInstantPreviewList" : undefined}
                className="home-search-input"
              />
              {homeQuery.trim().length > 0 ? (
                <button
                  type="button"
                  className="btn-ghost icon-only"
                  onClick={() => {
                    setHomeQuery("");
                    setActiveResultIndex(-1);
                    searchInputRef.current?.focus();
                  }}
                  title="Clear search (Esc)"
                  aria-label="Clear search"
                >
                  <X size={13} aria-hidden="true" />
                </button>
              ) : (
                <kbd className="kbd-pill" aria-hidden="true">
                  /
                </kbd>
              )}
              <button
                type="button"
                className="btn-tactile sm solid"
                onClick={handleAskClick}
                title={`Ask AI about your files (${formatShortcut("Enter")})`}
              >
                Ask
              </button>
            </form>

            {showInstantPreview && (
              <div
                id="homeInstantPreviewList"
                className="shelf-list-surface"
                role="listbox"
                aria-label="Instant search matches"
                style={{ marginTop: 0 }}
              >
                {instantShelves.length > 0 && (
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "6px",
                      flexWrap: "wrap",
                      padding: "8px 14px",
                      borderBottom: "1px solid var(--rule-soft)",
                      background: "var(--bg-subtle)",
                    }}
                  >
                    <span
                      style={{
                        fontSize: "11px",
                        fontWeight: 600,
                        letterSpacing: "0.05em",
                        textTransform: "uppercase",
                        color: "var(--ink-3)",
                        marginRight: "4px",
                      }}
                    >
                      Shelves
                    </span>
                    {instantShelves.map((shelf) => {
                      const count =
                        shelf.isCustom && shelf.fileIds
                          ? shelf.fileIds.length
                          : shelfCounts.get(shelf.id) || 0;
                      return (
                        <button
                          key={shelf.id}
                          type="button"
                          className="flat-chip"
                          style={{ padding: "3px 9px", fontSize: "12px" }}
                          onClick={() => {
                            onJumpToShelf(shelf.id);
                            setSearchFocused(false);
                          }}
                        >
                          <span>{shelf.name}</span>
                          <span
                            className="tabular-nums"
                            style={{ color: "var(--ink-3)", fontSize: "11px" }}
                          >
                            {count}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                )}

                {instantFiles.map((f, idx) => {
                  const isSelected = activeResultIndex === idx;
                  const merchantBadge = f.metadata?.merchant
                    ? String(f.metadata.merchant)
                    : null;
                  const amountBadge = f.metadata?.amount
                    ? String(f.metadata.amount)
                    : null;
                  return (
                    <div
                      key={f.id}
                      role="option"
                      aria-selected={isSelected}
                      className={`file-item-row ${isSelected ? "selected" : ""}`}
                      style={{ minHeight: "44px", padding: "9px 14px" }}
                      onMouseEnter={() => setActiveResultIndex(idx)}
                      onClick={() => {
                        onInspectFile(f.id);
                        setSearchFocused(false);
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "8px",
                          minWidth: 0,
                        }}
                      >
                        <span className="file-type-tag">{f.format}</span>
                        <span
                          style={{
                            fontWeight: 500,
                            fontSize: "13.5px",
                            color: "var(--ink)",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {f.title || f.name}
                        </span>
                        {(merchantBadge || amountBadge) && (
                          <span
                            className="tabular-nums"
                            style={{
                              fontSize: "11.5px",
                              color: "var(--ink-2)",
                              background: "var(--bg-subtle)",
                              border: "1px solid var(--rule)",
                              borderRadius: "4px",
                              padding: "1px 6px",
                              flexShrink: 0,
                            }}
                          >
                            {[merchantBadge, amountBadge].filter(Boolean).join(" · ")}
                          </span>
                        )}
                      </div>
                      <div
                        className="tabular-nums"
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "8px",
                          fontSize: "12px",
                          color: "var(--ink-3)",
                          flexShrink: 0,
                        }}
                      >
                        <span>{f.shelfName || f.folder}</span>
                        <span>·</span>
                        <span>{f.size || f.date}</span>
                      </div>
                    </div>
                  );
                })}

                {/* Footer Action Row 1: Search all results */}
                <button
                  type="button"
                  role="option"
                  aria-selected={
                    activeResultIndex === instantFiles.length || activeResultIndex === -1
                  }
                  className="home-dir-row"
                  style={{
                    padding: "9px 14px",
                    fontSize: "13px",
                    background:
                      activeResultIndex === instantFiles.length
                        ? "var(--bg-active)"
                        : "var(--bg-subtle)",
                    borderTop:
                      instantFiles.length > 0 ? "1px solid var(--rule)" : undefined,
                  }}
                  onMouseEnter={() => setActiveResultIndex(instantFiles.length)}
                  onClick={() => {
                    onSearchSubmit(trimmedQuery);
                    setSearchFocused(false);
                  }}
                >
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "8px",
                      minWidth: 0,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    <Search size={13} style={{ color: "var(--ink-3)", flexShrink: 0 }} />
                    <span>Search all results for “{trimmedQuery}”</span>
                  </span>
                  <kbd className="kbd-pill">Enter</kbd>
                </button>

                {/* Footer Action Row 2: Ask AI */}
                <button
                  type="button"
                  role="option"
                  aria-selected={activeResultIndex === instantFiles.length + 1}
                  className="home-dir-row"
                  style={{
                    padding: "9px 14px",
                    fontSize: "13px",
                    background:
                      activeResultIndex === instantFiles.length + 1
                        ? "var(--bg-active)"
                        : "var(--bg-subtle)",
                  }}
                  onMouseEnter={() => setActiveResultIndex(instantFiles.length + 1)}
                  onClick={() => {
                    onAskSubmit(trimmedQuery);
                    setSearchFocused(false);
                  }}
                >
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "8px",
                      minWidth: 0,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    <Sparkles size={13} style={{ color: "var(--ink-3)", flexShrink: 0 }} />
                    <span>Ask AI: “{trimmedQuery}”</span>
                  </span>
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "6px",
                      flexShrink: 0,
                    }}
                  >
                    <kbd className="kbd-pill">{formatShortcut("Enter")}</kbd>
                    <ArrowUpRight size={13} style={{ color: "var(--ink-3)" }} />
                  </span>
                </button>
              </div>
            )}
          </div>

          {/* Live Browser & OS Drag-Hover / 4-Stage AI Ingestion Pipeline & Post-Drop Dossier Card */}
          {(isHoverActive || isBusy || latestIngestionReport) && (
            <div
              className="surface-card"
              role="region"
              aria-label="Live AI drop zone and ingestion pipeline"
              style={{
                padding: "14px 16px",
                border: isHoverActive
                  ? "1.5px dashed var(--ink)"
                  : "1px solid var(--rule-strong)",
                background: isHoverActive ? "var(--bg-active)" : "var(--bg-surface)",
                display: "flex",
                flexDirection: "column",
                gap: "12px",
                transition: "all 0.16s ease",
              }}
            >
              {/* 1. Drag Hover or Live Pipeline Header */}
              {(isHoverActive || isBusy) && (
                <>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: "10px",
                      flexWrap: "wrap",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", minWidth: 0 }}>
                      <span className="status-dot pulse" aria-hidden="true" />
                      <span style={{ fontWeight: 600, fontSize: "13.5px", color: "var(--ink)" }}>
                        {isHoverActive
                          ? hoverDesc?.kind === "browser-asset"
                            ? "Ready to ingest browser asset"
                            : hoverDesc?.kind === "text"
                            ? "Ready to index browser text note"
                            : "Ready to run 4-stage AI pipeline"
                          : `${liveLabel}: ${liveFileName}`}
                      </span>
                    </div>
                    <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                      {isHoverActive
                        ? "Release to drop & index"
                        : liveRightMetric || "Running locally…"}
                    </span>
                  </div>

                  {isHoverActive && hoverDesc?.label && (
                    <div style={{ fontSize: "12.5px", color: "var(--ink-2)", lineHeight: 1.45 }}>
                      {hoverDesc.label}
                    </div>
                  )}

                  {/* 4-Stage Live Pipeline Stepper */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fit, minmax(155px, 1fr))",
                      gap: "8px",
                    }}
                  >
                    {[
                      { stage: 1, title: "1. Parse & RapidOCR", sub: "Text, tables, slides, EXIF" },
                      { stage: 2, title: "2. 512-d ONNX Embed", sub: "384-d text + 128-d vision" },
                      { stage: 3, title: "3. Auto-Shelve & Rules", sub: "UMAP+HDBSCAN & watch rules" },
                      { stage: 4, title: "4. Context Dossier", sub: "Entities, graph & duplicate check" },
                    ].map((st) => {
                      const isDone = !isHoverActive && currentStage > st.stage;
                      const isCurrent = isHoverActive || currentStage === st.stage;
                      return (
                        <div
                          key={st.stage}
                          style={{
                            padding: "8px 10px",
                            borderRadius: "6px",
                            border: isCurrent
                              ? "1px solid var(--ink)"
                              : "1px solid var(--rule)",
                            background: isDone
                              ? "var(--bg-subtle)"
                              : isCurrent
                              ? "var(--bg-surface)"
                              : "transparent",
                            opacity: !isHoverActive && currentStage > 0 && currentStage < st.stage ? 0.65 : 1,
                          }}
                        >
                          <div style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink)" }}>
                            {isDone ? `✓ ${st.title}` : st.title}
                          </div>
                          <div style={{ fontSize: "11px", color: "var(--ink-3)", marginTop: "2px" }}>
                            {st.sub}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}

              {/* 2. Post-Drop AI Context Dossier Card */}
              {!isHoverActive && latestIngestionReport && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "10px",
                    paddingTop: isBusy ? "10px" : "0",
                    borderTop: isBusy ? "1px solid var(--rule-soft)" : "none",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "flex-start",
                      justifyContent: "space-between",
                      gap: "12px",
                    }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "7px",
                          flexWrap: "wrap",
                        }}
                      >
                        <span
                          style={{
                            fontSize: "10.5px",
                            fontWeight: 600,
                            letterSpacing: "0.05em",
                            textTransform: "uppercase",
                            color: "var(--ink-3)",
                          }}
                        >
                          AI Ingestion Complete
                        </span>
                        <span className="file-type-tag">{latestIngestionReport.format}</span>
                        <span style={{ fontSize: "12px", color: "var(--ink-2)" }}>
                          → Shelf: <strong>{latestIngestionReport.shelfName}</strong>
                          {latestIngestionReport.subShelf ? ` / ${latestIngestionReport.subShelf}` : ""}
                        </span>
                      </div>
                      <div
                        className="font-display"
                        style={{
                          fontSize: "16px",
                          fontWeight: 500,
                          color: "var(--ink)",
                          marginTop: "3px",
                        }}
                      >
                        {latestIngestionReport.title || latestIngestionReport.fileName}
                      </div>
                      {latestIngestionReport.summaryLine && (
                        <div style={{ fontSize: "12.5px", color: "var(--ink-2)", marginTop: "3px" }}>
                          {latestIngestionReport.summaryLine}
                        </div>
                      )}
                    </div>
                    {onDismissIngestionReport && (
                      <button
                        type="button"
                        className="btn-ghost icon-only"
                        onClick={onDismissIngestionReport}
                        title="Dismiss card"
                        aria-label="Dismiss ingestion report"
                      >
                        <X size={13} />
                      </button>
                    )}
                  </div>

                  {/* Extracted Entities & Auto-Tags Pills */}
                  {((latestIngestionReport.autoTagsApplied &&
                    latestIngestionReport.autoTagsApplied.length > 0) ||
                    (latestIngestionReport.entities &&
                      Object.keys(latestIngestionReport.entities).length > 0) ||
                    latestIngestionReport.duplicateOf) && (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                      {latestIngestionReport.entities?.doc_type && (
                        <span className="tag-pill">
                          <span style={{ color: "var(--ink-3)" }}>Type:</span>
                          <strong>{String(latestIngestionReport.entities.doc_type)}</strong>
                        </span>
                      )}
                      {latestIngestionReport.entities?.merchant && (
                        <span className="tag-pill">
                          <span style={{ color: "var(--ink-3)" }}>Merchant:</span>
                          <strong>{String(latestIngestionReport.entities.merchant)}</strong>
                        </span>
                      )}
                      {latestIngestionReport.entities?.amount !== undefined &&
                        latestIngestionReport.entities?.amount !== null && (
                          <span className="tag-pill">
                            <span style={{ color: "var(--ink-3)" }}>Amount:</span>
                            <strong>
                              {latestIngestionReport.entities?.currency
                                ? `${latestIngestionReport.entities.currency} `
                                : ""}
                              {String(latestIngestionReport.entities.amount)}
                            </strong>
                          </span>
                        )}
                      {latestIngestionReport.entities?.author_or_person && (
                        <span className="tag-pill">
                          <span style={{ color: "var(--ink-3)" }}>Author:</span>
                          <strong>{String(latestIngestionReport.entities.author_or_person)}</strong>
                        </span>
                      )}
                      {(latestIngestionReport.autoTagsApplied || []).map((t) => (
                        <span key={t} className="tag-pill">
                          #{t}
                        </span>
                      ))}
                      {latestIngestionReport.duplicateOf && (
                        <span className="tag-pill" style={{ borderColor: "var(--warning)" }}>
                          {latestIngestionReport.duplicateOf.similarityPct}% match to{" "}
                          {latestIngestionReport.duplicateOf.name}
                        </span>
                      )}
                    </div>
                  )}

                  {/* Key Takeaways */}
                  {latestIngestionReport.keyTakeaways &&
                    latestIngestionReport.keyTakeaways.length > 0 && (
                      <div style={{ fontSize: "12.5px", color: "var(--ink-2)", lineHeight: 1.5 }}>
                        • {latestIngestionReport.keyTakeaways[0]}
                      </div>
                    )}

                  {/* Connected Files from Knowledge Graph */}
                  {latestIngestionReport.connectedFiles &&
                    latestIngestionReport.connectedFiles.length > 0 && (
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "6px",
                          flexWrap: "wrap",
                          fontSize: "12px",
                          color: "var(--ink-3)",
                        }}
                      >
                        <span>Connected files:</span>
                        {latestIngestionReport.connectedFiles.slice(0, 3).map((cf) => (
                          <button
                            key={cf.id}
                            type="button"
                            className="flat-chip"
                            style={{ padding: "2px 8px", fontSize: "11.5px" }}
                            onClick={() => onInspectFile(cf.id)}
                            title={cf.detail}
                          >
                            {cf.name} ({cf.relation})
                          </button>
                        ))}
                      </div>
                    )}

                  {/* 1-Click Actions */}
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                    <button
                      type="button"
                      className="btn-tactile sm solid"
                      onClick={() => onInspectFile(latestIngestionReport.fileId)}
                    >
                      Preview in Quick Look
                    </button>
                    <button
                      type="button"
                      className="btn-tactile sm"
                      onClick={() =>
                        onAskSubmit(`/explain ${latestIngestionReport.fileName}`)
                      }
                    >
                      Explain full context
                    </button>
                    {latestIngestionReport.suggestedName && onApplySuggestedRename && (
                      <button
                        type="button"
                        className="btn-tactile sm"
                        onClick={() => onApplySuggestedRename(latestIngestionReport.fileId)}
                      >
                        Rename → {latestIngestionReport.suggestedName}
                      </button>
                    )}
                    {latestIngestionReport.shelfId && (
                      <button
                        type="button"
                        className="btn-ghost"
                        style={{ minHeight: "26px", padding: "2px 8px", fontSize: "12px" }}
                        onClick={() => onJumpToShelf(latestIngestionReport.shelfId)}
                      >
                        Open shelf
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Conditional Connected Devices */}
          {connectedDevices.length > 0 && (
            <div className="home-quick-section" role="region" aria-label="Connected devices">
              <div className="home-dir-kicker">Connected devices</div>
              <div className="shelf-list-surface" style={{ marginTop: 0 }}>
                {connectedDevices.map((dev) => (
                  <div
                    key={dev.id}
                    className="home-dir-row"
                    style={{ cursor: "default" }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "10px",
                        minWidth: 0,
                        flex: 1,
                      }}
                    >
                      <HardDrive
                        size={14}
                        style={{ color: "var(--ink-3)", flexShrink: 0 }}
                        aria-hidden="true"
                      />
                      <span
                        style={{
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                        title={dev.path}
                      >
                        {dev.name}
                      </span>
                      {dev.sizeLabel && (
                        <span className="home-dir-meta tabular-nums">{dev.sizeLabel}</span>
                      )}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "6px", flexShrink: 0 }}>
                      <button
                        type="button"
                        className="btn-tactile sm solid"
                        onClick={() => onImportDevice(dev.path)}
                      >
                        Import
                      </button>
                      <button
                        type="button"
                        className="btn-tactile sm"
                        onClick={() => onScanDevice(dev.path, true)}
                      >
                        {dev.alreadyWatched ? "Rescan" : "Scan"}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Symmetric 2-Column Editorial Directory */}
          <div className="home-directory-grid">
            <div className="home-dir-col">
              <div className="home-dir-kicker">Workspace</div>
              <div
                className="shelf-list-surface"
                style={{ marginTop: 0 }}
                role="navigation"
                aria-label="Workspace"
              >
                <button
                  type="button"
                  className="home-dir-row"
                  onClick={() => onSwitchView("shelves")}
                >
                  <span>Shelves</span>
                  <span className="home-dir-meta tabular-nums">{files.length}</span>
                </button>
                <button
                  type="button"
                  className="home-dir-row"
                  onClick={() => onSwitchView("folders")}
                >
                  <span>Folders</span>
                  <span className="home-dir-meta tabular-nums">{activeWatchedCount}</span>
                </button>
                <button
                  type="button"
                  className="home-dir-row"
                  onClick={() => onSwitchView("find")}
                >
                  <span>Search &amp; Ask</span>
                  <kbd className="kbd-pill">{formatShortcut("K")}</kbd>
                </button>
                <button
                  type="button"
                  className="home-dir-row"
                  onClick={() => onSwitchView("learns")}
                >
                  <span>Activity &amp; Undo</span>
                  <span className="home-dir-meta tabular-nums">
                    {undoableCount > 0 ? undoableCount : ""}
                  </span>
                </button>
              </div>
            </div>

            <div className="home-dir-col">
              <div className="home-dir-kicker">Maintain</div>
              <div
                className="shelf-list-surface"
                style={{ marginTop: 0 }}
                role="navigation"
                aria-label="Maintain"
              >
                <button
                  type="button"
                  className="home-dir-row"
                  onClick={onOpenSmartClean}
                >
                  <span>Smart Clean</span>
                  <span className="home-dir-meta tabular-nums">
                    {cleanupCount > 0 ? cleanupCount : "Clean"}
                  </span>
                </button>
                <button
                  type="button"
                  className="home-dir-row"
                  onClick={() => onSwitchView("tidy")}
                >
                  <span>Tidy Names</span>
                  <span className="home-dir-meta tabular-nums">
                    {tidyCount > 0 ? tidyCount : ""}
                  </span>
                </button>
                <button
                  type="button"
                  className="home-dir-row"
                  onClick={() => onSwitchView("lookalikes")}
                >
                  <span>Duplicates</span>
                  <span className="home-dir-meta tabular-nums">
                    {dupCount > 0 ? dupCount : ""}
                  </span>
                </button>
                <button
                  type="button"
                  className="home-dir-row"
                  onClick={() => onSwitchView("trash")}
                >
                  <span>Trash &amp; Archive</span>
                  <span className="home-dir-meta tabular-nums">
                    {trashCount > 0 ? trashCount : ""}
                  </span>
                </button>
              </div>
            </div>
          </div>

          {/* Quick Jump: Shelves & Pinned Folders */}
          {(visibleShelves.length > 0 || pinnedFolders.length > 0) && (
            <>
              {visibleShelves.length > 0 && (
                <div className="home-quick-section" aria-label="Shelves quick jump">
                  <div className="home-quick-header">
                    <span className="home-dir-kicker">Shelves</span>
                    <button
                      type="button"
                      className="btn-ghost"
                      onClick={onCreateSmartShelf}
                    >
                      + New shelf
                    </button>
                  </div>
                  <div className="home-quick-row">
                    {visibleShelves.slice(0, 6).map((shelf) => {
                      const count =
                        shelf.isCustom && shelf.fileIds
                          ? shelf.fileIds.length
                          : shelfCounts.get(shelf.id) || 0;
                      return (
                        <button
                          key={shelf.id}
                          type="button"
                          className="flat-chip"
                          onClick={() => onJumpToShelf(shelf.id)}
                        >
                          <span
                            style={{
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                              maxWidth: "180px",
                            }}
                          >
                            {shelf.name}
                          </span>
                          <span
                            className="tabular-nums"
                            style={{ color: "var(--ink-3)", fontSize: "11.5px" }}
                          >
                            {count}
                          </span>
                        </button>
                      );
                    })}
                    <button
                      type="button"
                      className="flat-chip"
                      onClick={() => onJumpToShelf("all")}
                    >
                      All shelves
                    </button>
                  </div>
                </div>
              )}

              {pinnedFolders.length > 0 && (
                <div className="home-quick-section" aria-label="Pinned folders">
                  <div className="home-dir-kicker">Pinned folders</div>
                  <div className="home-quick-row">
                    {pinnedFolders.map((pf) => (
                      <button
                        key={pf.path}
                        type="button"
                        className="flat-chip"
                        onClick={() => onOpenPinnedFolder(pf.path)}
                        title={pf.displayPath}
                      >
                        {pf.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}

          {/* Minimal Hairline Footer */}
          <footer className="home-footer-bar" aria-label="Workspace utilities">
            <div style={{ display: "flex", alignItems: "center", gap: "4px", flexWrap: "wrap" }}>
              <button
                type="button"
                className="btn-ghost"
                onClick={onAddWatchFolder}
              >
                Watch folder
              </button>
              <button
                type="button"
                className="btn-ghost"
                onClick={() => fileInputRef.current?.click()}
              >
                Add files
              </button>
              <button
                type="button"
                className="btn-ghost"
                onClick={() => onSwitchView("settings")}
              >
                Settings
              </button>
            </div>
            <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>
              Drop files anywhere to index
            </span>
          </footer>
        </div>
      </section>
    );
  }
);
