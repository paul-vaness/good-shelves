import React, { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, MoreHorizontal, Search, ThumbsDown, ThumbsUp, X } from "lucide-react";
import {
  AiChoice,
  AiProcessStage,
  AskCitation,
  AskResponse,
  PinnedFolder,
  ProposedAiAction,
  SavedSearch,
  ShelfFile,
  WatchedFolder,
  cleanPreviewText,
  getFileThumbnailUrl,
  openFileInOS,
  recordUserFeedback,
  revealFileInOS,
} from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";
import { joinDisplayPath } from "../lib/platform";

interface FindViewProps {
  active: boolean;
  query: string;
  modalityFilter: string;
  results: ShelfFile[];
  allFiles?: ShelfFile[];
  savedSearches?: SavedSearch[];
  suggestions?: string[];
  pinnedFolders?: PinnedFolder[];
  watchedFolders?: WatchedFolder[];
  scopeFolder?: string | null;
  latencyMs: number;
  selectedFileId: string;
  similarSource?: ShelfFile | null;
  askResult?: AskResponse | null;
  asking?: boolean;
  pendingAskRequest?: { prompt: string; seq: number } | null;
  liveProgress?: { operation: string; current: number; total: number; file_name: string } | null;
  findMode?: "search" | "ask";
  userName?: string;
  onBackToHome?: () => void;
  onSetFindMode?: (mode: "search" | "ask") => void;
  onSetScopeFolder?: (folder: string | null) => void;
  onTogglePinFolder?: (path: string) => void;
  onQueryChange: (q: string) => void;
  onClearQuery?: () => void;
  onClearSimilar?: () => void;
  onAskShelves?: (
    question: string,
    context?: {
      recentFileIds?: string[];
      history?: Array<{ role: string; text: string }>;
      pendingAction?: ProposedAiAction | null;
      scopeFolder?: string | null;
    }
  ) => void;
  onExecuteAiAction?: (
    action: ProposedAiAction,
    selectedIds: string[],
    unselectedIds?: string[]
  ) => Promise<{ ok: boolean; count: number; summary: string }>;
  onUndoAction?: () => void;
  onTriggerPrompt?: (q: string) => void;
  onSetModalityFilter: (mod: string) => void;
  onSelectFile: (fileId: string) => void;
  onOpenQuickLook: (anchor?: {
    pageNumber?: number;
    slideNumber?: number;
    lineNumber?: number;
    sheetName?: string;
  }) => void;
  onSaveSearch?: (name: string, query: string, modality: string, tag: string) => void;
  onDeleteSavedSearch?: (searchId: string) => void;
  onShowToast?: (msg: string) => void;
  clipboard?: { fileIds: string[]; mode: "copy" | "cut" } | null;
  onSetClipboard?: (fileIds: string[], mode: "copy" | "cut") => void;
  onPasteClipboard?: () => void;
  onRenameFile?: (file: ShelfFile) => void;
  onContextMenuFile?: (e: React.MouseEvent, file: ShelfFile) => void;
  onJumpToShelf?: (shelfId: string, subShelf?: string) => void;
  onVisibleFileIdsChange?: (ids: string[]) => void;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  questionText?: string;
  feedback?: "up" | "down" | null;
  latencyMs?: number;
  appliedFilters?: string[];
  trace?: string[];
  processSummary?: AiProcessStage[];
  scopeFolder?: string | null;
  totalMatched?: number;
  citations?: Array<{
    file: ShelfFile;
    snippet: string;
    score: number;
    pageNumber?: number;
    slideNumber?: number;
    lineNumber?: number;
    sheetName?: string;
  }>;
  choices?: AiChoice[];
  proposedAction?: ProposedAiAction | null;
  actionStatus?: "pending" | "executing" | "executed" | "cancelled";
  actionResultSummary?: string;
  selectedActionIds?: Record<string, boolean>;
}

const ASK_SLASH_COMMANDS: Array<{ cmd: string; desc: string; template: string }> = [
  { cmd: "/audit", desc: "Inspect everything ready to process", template: "/audit" },
  { cmd: "/explain", desc: "Deep context dossier for a file (structure, entities, takeaways)", template: "/explain " },
  { cmd: "/context", desc: "Inspect full context, columns, slides, or code symbols of a file", template: "/context " },
  { cmd: "/graph", desc: "Connect the dots: find linked invoices, spreadsheets, versions & code", template: "/graph " },
  { cmd: "/reconcile", desc: "Reconcile invoices & receipts against your expense spreadsheets", template: "/reconcile " },
  { cmd: "/organize", desc: "Audit & organize files in scope", template: "/organize " },
  { cmd: "/list", desc: "List everything indexed in scope", template: "/list" },
  { cmd: "/duplicates", desc: "List & archive duplicate copies", template: "/duplicates" },
  { cmd: "/rename", desc: "Smart rename or custom format batch rename", template: "/rename " },
  { cmd: "/ocr", desc: "Run RapidOCR on images or scans", template: "/ocr " },
  { cmd: "/summarize", desc: "Key points from files in scope", template: "/summarize " },
  { cmd: "/compare", desc: "Compare two files side by side", template: "/compare " },
  { cmd: "/cleanup", desc: "Review cleanup & organization steps", template: "/cleanup" },
  { cmd: "/memory", desc: "Show learned AI rules & routes", template: "/memory" },
  { cmd: "/train", desc: "Run continuous AI data feeder & calibration benchmark", template: "/train" },
  { cmd: "/scope", desc: "Scope Ask AI to a folder or all", template: "/scope " },
  { cmd: "/help", desc: "List everything Good Shelves AI can do", template: "/help" },
];

function renderInlineMd(line: string, keyPrefix: string): React.ReactNode[] {
  const parts = line.split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)/g);
  return parts.map((part, idx) => {
    const k = `${keyPrefix}-${idx}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={k} style={{ fontWeight: 600 }}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return <code key={k} className="ai-inline-code">{part.slice(1, -1)}</code>;
    }
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      return <em key={k}>{part.slice(1, -1)}</em>;
    }
    return <React.Fragment key={k}>{part}</React.Fragment>;
  });
}

function renderAiMarkdown(rawText: string): React.ReactNode {
  const clean = cleanPreviewText(rawText) || rawText;
  const lines = clean.split("\n");
  const blocks: React.ReactNode[] = [];
  let listBuf: Array<{ ordered: boolean; text: string }> = [];
  let inCode = false;
  let codeBuf: string[] = [];

  const flushList = (idxKey: number) => {
    if (!listBuf.length) return;
    const isOrdered = listBuf[0].ordered;
    const items = listBuf.map((it, li) => (
      <li key={`li-${idxKey}-${li}`}>{renderInlineMd(it.text, `li-${idxKey}-${li}`)}</li>
    ));
    blocks.push(
      isOrdered ? (
        <ol key={`ol-${idxKey}`} className="ai-md-list ordered">{items}</ol>
      ) : (
        <ul key={`ul-${idxKey}`} className="ai-md-list">{items}</ul>
      )
    );
    listBuf = [];
  };

  lines.forEach((rawLine, idx) => {
    const line = rawLine.trimEnd();
    if (line.trim().startsWith("```")) {
      flushList(idx);
      if (inCode) {
        blocks.push(
          <pre key={`code-${idx}`} className="ai-md-codeblock">
            <code>{codeBuf.join("\n")}</code>
          </pre>
        );
        codeBuf = [];
        inCode = false;
      } else {
        inCode = true;
      }
      return;
    }
    if (inCode) {
      codeBuf.push(rawLine);
      return;
    }

    const bulletM = line.match(/^\s*[-•*]\s+(.+)$/);
    const numM = line.match(/^\s*\d+\.\s+(.+)$/);
    if (bulletM) {
      if (listBuf.length && listBuf[0].ordered) flushList(idx);
      listBuf.push({ ordered: false, text: bulletM[1] });
      return;
    }
    if (numM) {
      if (listBuf.length && !listBuf[0].ordered) flushList(idx);
      listBuf.push({ ordered: true, text: numM[1] });
      return;
    }
    flushList(idx);

    const headM = line.match(/^#{1,3}\s+(.+)$/);
    if (headM) {
      blocks.push(
        <div key={`h-${idx}`} className="font-display" style={{ fontWeight: 600, fontSize: "15px", marginTop: "4px" }}>
          {renderInlineMd(headM[1], `h-${idx}`)}
        </div>
      );
      return;
    }
    if (!line.trim()) {
      blocks.push(<div key={`sp-${idx}`} style={{ height: "6px" }} />);
      return;
    }
    blocks.push(
      <div key={`p-${idx}`}>{renderInlineMd(line, `p-${idx}`)}</div>
    );
  });
  flushList(lines.length);
  if (codeBuf.length) {
    blocks.push(
      <pre key="code-tail" className="ai-md-codeblock">
        <code>{codeBuf.join("\n")}</code>
      </pre>
    );
  }
  return <div className="ai-md-body">{blocks}</div>;
}

const OPERATOR_CHIPS = [
  "type:pdf",
  "type:image",
  "doctype:receipt",
  "has:ocr",
  "unnamed:true",
  "after:2026-01-01",
  "-draft",
];

const CHAT_FILLER_WORDS = new Set([
  "the", "my", "a", "an", "is", "are", "was", "were", "what", "where",
  "who", "when", "which", "how", "much", "did", "do", "does", "i", "me",
  "find", "show", "locate", "looking", "for", "bring", "move", "copy",
  "send", "get", "open", "inside", "about", "in", "on", "to", "from",
  "of", "with", "that", "this", "file", "files", "document", "documents",
  "please", "can", "you", "there", "any", "latest", "newest", "oldest",
  "largest", "biggest", "recent", "have",
]);

function formatDisplayName(raw?: string): string {
  const clean = (raw || "").trim();
  if (!clean) return "";
  return clean.charAt(0).toUpperCase() + clean.slice(1);
}

function citationToShelfFile(cit: AskCitation, fileMap: Map<string, ShelfFile>): ShelfFile {
  const existing = fileMap.get(cit.fileId);
  if (existing) return existing;
  return {
    id: cit.fileId,
    path: cit.path || cit.fileName,
    name: cit.fileName,
    originalName: cit.fileName,
    suggestedName: null,
    renameReason: null,
    renamed: false,
    title: cit.title || cit.fileName,
    format: cit.format || "file",
    modality: (cit.modality as ShelfFile["modality"]) || "document",
    folder: cit.folder || "",
    folderFull: cit.folderFull || "",
    size: cit.size || "",
    sizeBytes: 0,
    date: cit.date || "",
    createdDate: cit.date || "",
    mtime: 0,
    sha256: "",
    shelfId: "",
    shelfName: cit.shelfName || "",
    subShelf: cit.subShelf || "",
    clusterId: -1,
    matchScore: cit.score || 0,
    cosine: "0.00",
    vectorNorm: "1.00",
    whyMatched: cit.snippet || "",
    visualShare: 0,
    textShare: 100,
    extractedText: cit.snippet || "",
    ocrText: "",
    ocrConfidence: 0,
    specimen: "text",
    width: 0,
    height: 0,
    pageCount: 1,
    mapX: 50,
    mapY: 50,
  };
}

function rankInstantMatches(rawText: string, pool: ShelfFile[]): ShelfFile[] {
  const clean = rawText.trim().toLowerCase();
  if (clean.length < 2 || pool.length === 0) return [];

  const tokens = clean
    .split(/[^a-z0-9.:_-]+/)
    .map((t) => t.trim())
    .filter((t) => t.length >= 2 && !CHAT_FILLER_WORDS.has(t));

  const wantNewest = /\b(latest|newest|recent|last)\b/.test(clean);
  const wantLargest = /\b(largest|biggest)\b/.test(clean);

  const scored: Array<{ score: number; file: ShelfFile }> = [];
  for (const f of pool) {
    const title = (f.title || "").toLowerCase();
    const name = (f.name || "").toLowerCase();
    const shelf = `${f.shelfName || ""} ${f.subShelf || ""}`.toLowerCase();
    const tags = (f.tags || []).join(" ").toLowerCase();
    const body = `${f.extractedText || ""} ${f.ocrText || ""}`.toLowerCase();
    const format = (f.format || "").toLowerCase();

    let s = 0;
    if (tokens.length === 0) {
      if (wantNewest || wantLargest) s = 1;
    } else {
      for (const tok of tokens) {
        if (tok.startsWith("type:") || tok.startsWith("ext:")) {
          const wantedExt = tok.split(":")[1];
          if (wantedExt && (format === wantedExt || f.modality === wantedExt)) s += 8;
          continue;
        }
        if (title.includes(tok)) s += 5;
        if (name.includes(tok)) s += 4;
        if (tags.includes(tok)) s += 3.5;
        if (shelf.includes(tok)) s += 2.5;
        if (format === tok) s += 3;
        if (body.includes(tok)) s += 1.8;
      }
    }

    if (s > 0) {
      if (wantNewest) s += f.mtime / 1e12;
      if (wantLargest) s += f.sizeBytes / 1e10;
      scored.push({ score: s, file: f });
    }
  }

  scored.sort((a, b) => {
    if (wantNewest && tokens.length === 0) return b.file.mtime - a.file.mtime;
    if (wantLargest && tokens.length === 0) return b.file.sizeBytes - a.file.sizeBytes;
    return b.score - a.score;
  });

  return scored.slice(0, 3).map((x) => x.file);
}

export const FindView = memoView<FindViewProps>(({
  active,
  query,
  modalityFilter,
  results,
  allFiles = [],
  savedSearches = [],
  pinnedFolders = [],
  watchedFolders = [],
  scopeFolder = null,
  latencyMs,
  selectedFileId,
  similarSource = null,
  askResult = null,
  asking = false,
  pendingAskRequest = null,
  liveProgress = null,
  findMode: controlledFindMode,
  userName = "",
  onBackToHome,
  onSetFindMode,
  onSetScopeFolder,
  onTogglePinFolder,
  onQueryChange,
  onClearQuery,
  onClearSimilar,
  onAskShelves,
  onExecuteAiAction,
  onUndoAction,
  onTriggerPrompt,
  onSetModalityFilter,
  onSelectFile,
  onOpenQuickLook,
  onSaveSearch,
  onDeleteSavedSearch,
  onShowToast,
  clipboard,
  onSetClipboard,
  onPasteClipboard,
  onRenameFile,
  onContextMenuFile,
  onJumpToShelf,
  onVisibleFileIdsChange,
}) => {
  const cutIdSet = useMemo(
    () => new Set(clipboard?.mode === "cut" ? clipboard.fileIds : []),
    [clipboard]
  );
  const [activeTag, setActiveTag] = useState<string>("");
  const [internalFindMode, setInternalFindMode] = useState<"search" | "ask">("search");
  const findMode = controlledFindMode ?? internalFindMode;
  const setFindMode = (m: "search" | "ask") => {
    setInternalFindMode(m);
    onSetFindMode?.(m);
  };
  const [filtersOpen, setFiltersOpen] = useState<boolean>(false);
  const [resultsLimit, setResultsLimit] = useState<number>(60);
  const [chatInput, setChatInput] = useState<string>("");
  const [pickerIndex, setPickerIndex] = useState<number>(0);
  const [pickerDismissed, setPickerDismissed] = useState<boolean>(false);
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [expandedChatIds, setExpandedChatIds] = useState<Record<string, boolean>>({});
  const lastAskRef = useRef<AskResponse | null>(null);
  const lastHandledAskSeqRef = useRef<number>(0);
  const submitQuestionRef = useRef<(q: string) => void>(() => {});
  const chatScrollRef = useRef<HTMLDivElement | null>(null);

  const displayName = useMemo(() => formatDisplayName(userName), [userName]);

  const fileLookup = useMemo(() => {
    const m = new Map<string, ShelfFile>();
    for (const f of allFiles) m.set(f.id, f);
    for (const f of results) if (!m.has(f.id)) m.set(f.id, f);
    return m;
  }, [allFiles, results]);

  useEffect(() => {
    if (!askResult || askResult === lastAskRef.current) return;
    lastAskRef.current = askResult;
    const resolvedCitations = (askResult.citations || []).slice(0, 25).map((c) => {
      const mPage = c.snippet?.match(/\[Page\s+(\d+)\]/i);
      const mSlide = c.snippet?.match(/\[Slide\s+(\d+)\]/i);
      const mLine = c.snippet?.match(/\[Line\s+(\d+)\]/i);
      return {
        file: citationToShelfFile(c, fileLookup),
        snippet: c.snippet,
        score: c.score,
        pageNumber: c.pageNumber ?? (mPage ? parseInt(mPage[1], 10) : undefined),
        slideNumber: c.slideNumber ?? (mSlide ? parseInt(mSlide[1], 10) : undefined),
        lineNumber: c.lineNumber ?? (mLine ? parseInt(mLine[1], 10) : undefined),
        sheetName: c.sheetName,
      };
    });
    const proposed = askResult.proposedAction ?? null;
    const defaultSelected: Record<string, boolean> = {};
    if (proposed?.items) {
      for (const it of proposed.items) {
        defaultSelected[it.fileId] = it.selectedByDefault !== false;
      }
    }
    if (askResult.scopeFolder !== undefined && askResult.scopeFolder !== scopeFolder) {
      onSetScopeFolder?.(askResult.scopeFolder ?? null);
    }
    const shouldAutoExecute = Boolean(proposed?.autoExecute && onExecuteAiAction);
    const msgId = `a-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    setChatHistory((prev) => [
      ...prev,
      {
        id: msgId,
        role: "assistant",
        text: askResult.answer,
        questionText: askResult.question,
        latencyMs: askResult.latencyMs,
        appliedFilters: askResult.appliedFilters,
        trace: askResult.trace,
        processSummary: askResult.processSummary,
        scopeFolder: askResult.scopeFolder,
        totalMatched: askResult.totalMatched,
        citations: resolvedCitations,
        choices: askResult.choices || [],
        proposedAction: proposed,
        actionStatus: proposed ? (shouldAutoExecute ? "executing" : "pending") : undefined,
        selectedActionIds: proposed ? defaultSelected : undefined,
      },
    ]);
    if (shouldAutoExecute && proposed && onExecuteAiAction) {
      const selectedIds = (proposed.items || [])
        .filter((it) => defaultSelected[it.fileId] !== false)
        .map((it) => it.fileId);
      const unselectedIds = (proposed.items || [])
        .filter((it) => defaultSelected[it.fileId] === false)
        .map((it) => it.fileId);
      void onExecuteAiAction(proposed, selectedIds, unselectedIds)
        .then((res) => {
          setChatHistory((prev) =>
            prev.map((m) =>
              m.id === msgId
                ? { ...m, actionStatus: "executed", actionResultSummary: res.summary }
                : m
            )
          );
        })
        .catch((err) => {
          onShowToast?.(err instanceof Error ? err.message : "Action failed");
          setChatHistory((prev) =>
            prev.map((m) => (m.id === msgId ? { ...m, actionStatus: "pending" } : m))
          );
        });
    }
  }, [askResult, fileLookup, onExecuteAiAction, onSetScopeFolder, onShowToast, scopeFolder]);

  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [chatHistory, asking]);

  const instantMatches = useMemo(() => {
    if (findMode !== "ask") return [];
    const pool = allFiles.length > 0 ? allFiles : results;
    return rankInstantMatches(chatInput, pool);
  }, [findMode, chatInput, allFiles, results]);

  const slashMatches = useMemo(() => {
    if (findMode !== "ask") return [];
    const trimmed = chatInput.trimStart();
    if (!trimmed.startsWith("/") || trimmed.includes(" ")) return [];
    const low = trimmed.toLowerCase();
    return ASK_SLASH_COMMANDS.filter((c) => c.cmd.startsWith(low));
  }, [findMode, chatInput]);

  const scopeFoldersList = useMemo(() => {
    const map = new Map<string, { path: string; label: string; pinned: boolean }>();
    for (const pf of pinnedFolders) {
      map.set(pf.path, { path: pf.path, label: pf.name, pinned: true });
    }
    for (const wf of watchedFolders) {
      if (!map.has(wf.path)) {
        map.set(wf.path, { path: wf.path, label: wf.display_path, pinned: false });
      }
    }
    if (scopeFolder && !map.has(scopeFolder)) {
      const baseName = scopeFolder.split(/[\\/]/).filter(Boolean).pop() || scopeFolder;
      map.set(scopeFolder, { path: scopeFolder, label: baseName, pinned: false });
    }
    return Array.from(map.values());
  }, [pinnedFolders, watchedFolders, scopeFolder]);

  const activeScopeLabel = useMemo(() => {
    if (!scopeFolder) return null;
    const hit = scopeFoldersList.find((s) => s.path === scopeFolder);
    return hit?.label || scopeFolder.split(/[\\/]/).filter(Boolean).pop() || scopeFolder;
  }, [scopeFolder, scopeFoldersList]);

  const scopedFilesCount = useMemo(() => {
    const pool = allFiles.length > 0 ? allFiles : results;
    if (!scopeFolder) return pool.length;
    const normScope = scopeFolder.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
    return pool.filter((f) => {
      const fp = (f.folderFull || f.path || "").replace(/\\/g, "/").toLowerCase();
      return fp === normScope || fp.startsWith(`${normScope}/`);
    }).length;
  }, [allFiles, results, scopeFolder]);

  const atMentionMatches = useMemo(() => {
    if (findMode !== "ask") return [];
    const m = chatInput.match(/(?:^|\s)@([^\s@]{0,40})$/);
    if (!m) return [];
    const needle = m[1].toLowerCase();
    const pool = allFiles.length > 0 ? allFiles : results;
    return pool
      .filter((f) => !needle || f.name.toLowerCase().includes(needle) || f.title.toLowerCase().includes(needle))
      .slice(0, 6);
  }, [findMode, chatInput, allFiles, results]);

  const applyAtMention = (file: ShelfFile) => {
    const next = chatInput.replace(/(^|\s)@[^\s@]*$/, `$1${file.name} `);
    setChatInput(next);
    setPickerIndex(0);
    setPickerDismissed(false);
  };

  const starterPrompts = useMemo(() => {
    if (activeScopeLabel) {
      return [
        { label: `Summarize key files in ${activeScopeLabel}`, prompt: `Summarize key files in ${activeScopeLabel}` },
        { label: `Audit & organize ${activeScopeLabel}`, prompt: `Audit & organize ${activeScopeLabel}` },
        { label: `List all files in ${activeScopeLabel}`, prompt: `List all files in ${activeScopeLabel}` },
        { label: `Find duplicates in ${activeScopeLabel}`, prompt: `Find duplicates in ${activeScopeLabel}` },
      ];
    }
    const pool = allFiles.length > 0 ? allFiles : results;
    const hasReceipts = pool.some(
      (f) => f.specimen === "receipt" || f.metadata?.doc_type === "receipt" || f.metadata?.amount
    );
    const hasUnnamed = pool.some((f) => Boolean(f.suggestedName) && !f.renamed);
    return [
      { label: "Audit workspace readiness", prompt: "/audit" },
      { label: "Summarize my recent documents", prompt: "Summarize my recent documents" },
      hasReceipts
        ? { label: "Find receipts and total amounts", prompt: "Find receipts and total amounts" }
        : { label: "List all indexed files by folder", prompt: "/list" },
      { label: "Review duplicate files", prompt: "Review duplicate files" },
      hasUnnamed
        ? { label: "Rename unclear filenames", prompt: "Rename unclear filenames" }
        : { label: "Organize messy files into shelves", prompt: "/organize " },
    ];
  }, [activeScopeLabel, allFiles, results]);

  const availableTags = useMemo(() => {
    const set = new Set<string>();
    for (const f of allFiles.length ? allFiles : results) {
      for (const t of f.tags || []) {
        set.add(t);
      }
    }
    return Array.from(set).sort();
  }, [allFiles, results]);

  const filteredResults = useMemo(() => {
    const base = activeTag
      ? results.filter((f) => (f.tags || []).includes(activeTag))
      : results;
    const q = query.trim().toLowerCase();
    if (!q) return base;
    const wantLargest = /\b(largest|biggest)\b/.test(q);
    const wantOldest = /\b(oldest|earliest)\b/.test(q);
    const wantNewest = /\b(newest|latest|most\s+recent)\b/.test(q);
    if (!wantLargest && !wantOldest && !wantNewest) return base;
    const copy = [...base];
    copy.sort((a, b) => {
      if (wantLargest) return b.sizeBytes - a.sizeBytes || b.mtime - a.mtime;
      if (wantOldest) return a.mtime - b.mtime;
      return b.mtime - a.mtime;
    });
    return copy;
  }, [results, activeTag, query]);

  const visibleFindIds = useMemo(
    () => filteredResults.slice(0, resultsLimit).map((f) => f.id),
    [filteredResults, resultsLimit]
  );

  useEffect(() => {
    if (active) onVisibleFileIdsChange?.(visibleFindIds);
  }, [active, visibleFindIds, onVisibleFileIdsChange]);

  const activeOperators = useMemo(() => {
    const matches = query.match(
      /(?:^|\s)(-(?:[a-z]+):(?:"[^"]+"|[^\s]+)|-[^\s"]+|(?:type|ext|folder|shelf|tag|has|after|before|unnamed|doctype|merchant|amount|due|invoice|date|phone|email):(?:"[^"]+"|[^\s]+))/gi
    );
    return (matches || []).map((m) => m.trim());
  }, [query]);

  const removeOperatorToken = (token: string) => {
    const next = query.replace(token, "").replace(/\s{2,}/g, " ").trim();
    onQueryChange(next);
  };

  const resetAllFindFilters = () => {
    onSetModalityFilter("all");
    setActiveTag("");
    onClearSimilar?.();
    if (query.trim().length > 0) {
      let cleaned = query;
      for (const op of activeOperators) {
        cleaned = cleaned.replace(op, "");
      }
      onQueryChange(cleaned.replace(/\s{2,}/g, " ").trim());
    }
  };

  const hasActiveFindFilters =
    modalityFilter !== "all" ||
    activeTag !== "" ||
    Boolean(similarSource) ||
    activeOperators.length > 0;

  const appendOperator = (op: string) => {
    const trimmed = query.trim();
    if (trimmed.includes(op)) return;
    onQueryChange(trimmed ? `${trimmed} ${op}` : op);
  };

  const handleOpenFile = async (f: ShelfFile) => {
    try {
      await openFileInOS(f.path);
      onShowToast?.(`Opened ${f.name}`);
    } catch {
      onSelectFile(f.id);
      onOpenQuickLook();
    }
  };

  const handleRevealFile = async (f: ShelfFile) => {
    try {
      await revealFileInOS(f.path);
      onShowToast?.(`Revealed ${f.name} in folder`);
    } catch {
      onShowToast?.(joinDisplayPath(f.folder, f.name));
    }
  };

  const submitQuestion = (questionText: string) => {
    const q = questionText.trim();
    if (!q || asking) return;
    const recentIds: string[] = [];
    for (let i = chatHistory.length - 1; i >= 0 && recentIds.length < 30; i--) {
      const m = chatHistory[i];
      if (m.role === "assistant") {
        for (const cit of m.citations || []) {
          if (!recentIds.includes(cit.file.id)) recentIds.push(cit.file.id);
        }
        for (const it of m.proposedAction?.items || []) {
          if (!it.fileId.startsWith("folder::") && !recentIds.includes(it.fileId)) {
            recentIds.push(it.fileId);
          }
        }
        if (recentIds.length > 0) break;
      }
    }
    if (recentIds.length === 0 && (query.trim().length > 0 || hasActiveFindFilters) && filteredResults.length > 0) {
      for (const rf of filteredResults.slice(0, 20)) {
        recentIds.push(rf.id);
      }
    }
    const recentTurns = chatHistory.slice(-8).map((m) => ({ role: m.role, text: m.text }));
    const pendingMsg = [...chatHistory].reverse().find((m) => m.role === "assistant" && m.actionStatus === "pending" && m.proposedAction);
    const qNorm = q.replace(/[?!.,\s]+$/g, "").trim();
    const isCancelReply = Boolean(pendingMsg && /^(?:cancel|cancel plan|never ?mind|abort|stop|forget it)$/i.test(qNorm));
    const isConfirmReply = Boolean(
      pendingMsg &&
        /^(?:(?:yes|yeah|yep|yup|sure|ok|okay)\s*)?(?:do\s+it|do\s+that|run\s+it|apply(?:\s+it|\s+that)?|confirm(?:\s+it|\s+plan)?|execute(?:\s+it)?|go\s+ahead|proceed|make\s+it\s+happen|yes|yeah|yep|yup|sure|ok|okay)(?:\s+now|\s+please)?$/i.test(
          qNorm
        )
    );
    if (isConfirmReply && pendingMsg) {
      setChatHistory((prev) => [
        ...prev,
        { id: `u-${Date.now()}-${prev.length}`, role: "user", text: q },
      ]);
      setChatInput("");
      void handleConfirmAiAction(pendingMsg.id);
      return;
    }
    setChatHistory((prev) => [
      ...prev.map((m) =>
        isCancelReply && m.id === pendingMsg?.id ? { ...m, actionStatus: "cancelled" as const } : m
      ),
      { id: `u-${Date.now()}-${prev.length}`, role: "user", text: q },
    ]);
    setChatInput("");
    onAskShelves?.(q, {
      recentFileIds: recentIds,
      history: recentTurns,
      pendingAction: pendingMsg?.proposedAction ?? null,
      scopeFolder,
    });
  };
  submitQuestionRef.current = submitQuestion;

  useEffect(() => {
    if (!pendingAskRequest || pendingAskRequest.seq === lastHandledAskSeqRef.current) return;
    lastHandledAskSeqRef.current = pendingAskRequest.seq;
    submitQuestionRef.current(pendingAskRequest.prompt);
  }, [pendingAskRequest]);

  const toggleActionItemChecked = (msgId: string, fileId: string) => {
    setChatHistory((prev) =>
      prev.map((m) => {
        if (m.id !== msgId || m.actionStatus !== "pending") return m;
        const curr = m.selectedActionIds || {};
        return {
          ...m,
          selectedActionIds: { ...curr, [fileId]: !curr[fileId] },
        };
      })
    );
  };

  const setAllActionItemsChecked = (msgId: string, checked: boolean) => {
    setChatHistory((prev) =>
      prev.map((m) => {
        if (m.id !== msgId || m.actionStatus !== "pending" || !m.proposedAction) return m;
        const next: Record<string, boolean> = {};
        for (const it of m.proposedAction.items) next[it.fileId] = checked;
        return { ...m, selectedActionIds: next };
      })
    );
  };

  const handleConfirmAiAction = async (msgId: string) => {
    const msg = chatHistory.find((m) => m.id === msgId);
    if (!msg?.proposedAction || !onExecuteAiAction || msg.actionStatus !== "pending") return;
    const action = msg.proposedAction;
    const selectedIds = (action.items || [])
      .filter((it) => (msg.selectedActionIds?.[it.fileId] ?? true))
      .map((it) => it.fileId);
    const unselectedIds = (action.items || [])
      .filter((it) => !(msg.selectedActionIds?.[it.fileId] ?? true))
      .map((it) => it.fileId);
    if (action.requiresSelection && selectedIds.length === 0) {
      onShowToast?.("Select at least one item to confirm.");
      return;
    }

    setChatHistory((prev) =>
      prev.map((m) => (m.id === msgId ? { ...m, actionStatus: "executing" } : m))
    );
    try {
      const res = await onExecuteAiAction(action, selectedIds, unselectedIds);
      setChatHistory((prev) =>
        prev.map((m) =>
          m.id === msgId
            ? { ...m, actionStatus: "executed", actionResultSummary: res.summary }
            : m
        )
      );
    } catch (err) {
      onShowToast?.(err instanceof Error ? err.message : "Action failed");
      setChatHistory((prev) =>
        prev.map((m) => (m.id === msgId ? { ...m, actionStatus: "pending" } : m))
      );
    }
  };

  const handleCancelAiAction = (msgId: string) => {
    setChatHistory((prev) =>
      prev.map((m) =>
        m.id === msgId && m.actionStatus === "pending"
          ? { ...m, actionStatus: "cancelled", actionResultSummary: "Cancelled. No files were changed." }
          : m
      )
    );
  };

  const handleSubmitChat = (e: React.FormEvent) => {
    e.preventDefault();
    submitQuestion(chatInput);
  };

  const handleListKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
    if (findMode !== "search") return;
    const target = e.target as HTMLElement | null;
    const isInput = Boolean(
      target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT")
    );
    const mod = e.ctrlKey || e.metaKey;
    const lower = e.key.toLowerCase();

    if (mod && lower === "f") {
      e.preventDefault();
      e.stopPropagation();
      const input = document.getElementById("findSearchInput") as HTMLInputElement | null;
      input?.focus();
      input?.select();
      return;
    }
    if (isInput) return;
    if (!filteredResults.length) return;

    if (mod && (lower === "c" || lower === "x") && selectedFileId && onSetClipboard) {
      const textSel = window.getSelection()?.toString();
      if (textSel) return;
      e.preventDefault();
      e.stopPropagation();
      onSetClipboard([selectedFileId], lower === "x" ? "cut" : "copy");
      return;
    }

    if (mod && lower === "v" && clipboard?.fileIds.length && onPasteClipboard) {
      e.preventDefault();
      e.stopPropagation();
      onPasteClipboard();
      return;
    }

    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      const idx = visibleFindIds.indexOf(selectedFileId);
      const nextIdx =
        e.key === "ArrowDown"
          ? Math.min(visibleFindIds.length - 1, idx + 1)
          : Math.max(0, idx <= 0 ? 0 : idx - 1);
      const nextId = visibleFindIds[nextIdx];
      if (!nextId) return;
      onSelectFile(nextId);
      requestAnimationFrame(() => {
        document.querySelector(`[data-find-file-id="${CSS.escape(nextId)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
    } else if (e.key === "Enter" && selectedFileId) {
      if (target && (target.tagName === "BUTTON" || target.tagName === "A")) return;
      onOpenQuickLook();
    }
  };

  const renderCalmCitationActions = (f: ShelfFile) => (
    <div className="row-actions-inline" style={{ display: "flex", alignItems: "center", gap: "4px" }} onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className="btn-tactile sm"
        onClick={() => {
          onSelectFile(f.id);
          onOpenQuickLook();
        }}
      >
        Preview
      </button>
      {onRenameFile && (
        <button
          type="button"
          className="btn-ghost"
          style={{ minHeight: "26px", padding: "2px 8px", fontSize: "12px" }}
          onClick={() => onRenameFile(f)}
        >
          Rename
        </button>
      )}
      <button
        type="button"
        className="btn-ghost"
        style={{ minHeight: "26px", padding: "2px 8px", fontSize: "12px" }}
        onClick={() => handleOpenFile(f)}
      >
        Open
      </button>
      <button
        type="button"
        className="btn-ghost"
        style={{ minHeight: "26px", padding: "2px 8px", fontSize: "12px" }}
        onClick={() => handleRevealFile(f)}
      >
        Reveal
      </button>
      {onContextMenuFile && (
        <button
          type="button"
          className="row-icon-btn"
          title="Actions"
          aria-label={`More actions for ${f.title}`}
          onClick={(e) => onContextMenuFile(e, f)}
        >
          <MoreHorizontal size={14} aria-hidden="true" />
        </button>
      )}
    </div>
  );

  return (
    <section
      className={`view-sheet${active ? " active" : ""}${findMode === "ask" ? " find-ask-active" : ""}`}
      id="viewFind"
      aria-label="Search & Ask"
      aria-hidden={!active}
      inert={!active ? true : undefined}
      tabIndex={active ? 0 : -1}
      onKeyDown={handleListKeyDown}
    >
      <div className="page-container find-full-container">
        <div className="find-compact-topbar">
          <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
            {onBackToHome && (
              <button
                type="button"
                className="page-back-btn inline"
                onClick={onBackToHome}
                title="Back to Home (Esc)"
                aria-label="Back to Home"
              >
                <ArrowLeft size={13} strokeWidth={1.75} aria-hidden="true" />
                <span>Home</span>
              </button>
            )}
            <div className="seg-group" role="tablist" aria-label="Search or Ask mode">
              <button
                type="button"
                id="findModeSearchBtn"
                role="tab"
                aria-selected={findMode === "search"}
                className={`seg-btn${findMode === "search" ? " active" : ""}`}
                onClick={() => setFindMode("search")}
              >
                Search
              </button>
              <button
                type="button"
                id="findModeAskBtn"
                role="tab"
                aria-selected={findMode === "ask"}
                className={`seg-btn${findMode === "ask" ? " active" : ""}`}
                onClick={() => setFindMode("ask")}
              >
                Ask AI
              </button>
            </div>
          </div>
          {findMode === "ask" && chatHistory.length > 0 && (
            <button
              type="button"
              className="btn-tactile"
              onClick={() => setChatHistory([])}
            >
              + New chat
            </button>
          )}
          {findMode === "search" && (
            <button
              type="button"
              className={`btn-tactile${filtersOpen || hasActiveFindFilters ? " active-tab" : ""}`}
              onClick={() => setFiltersOpen((p) => !p)}
            >
              Filters
              {hasActiveFindFilters
                ? ` (${(modalityFilter !== "all" ? 1 : 0) + (activeTag ? 1 : 0) + (similarSource ? 1 : 0) + activeOperators.length})`
                : ""}
            </button>
          )}
        </div>

        {/* SEARCH MODE */}
        {findMode === "search" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
            {/* Search Input Bar */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "10px",
                padding: "10px 14px",
                borderRadius: "10px",
                border: "1px solid var(--rule-strong)",
                background: "var(--bg-surface)",
              }}
            >
              <Search
                size={15}
                strokeWidth={1.75}
                style={{ color: "var(--ink-3)", flexShrink: 0 }}
                aria-hidden="true"
              />
              <input
                id="findSearchInput"
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={query}
                onChange={(e) => onQueryChange(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape" && query) {
                    e.stopPropagation();
                    onQueryChange("");
                    onClearQuery?.();
                  }
                }}
                placeholder="Search documents, notes, topics, or type:pdf…"
                aria-label="Search your files"
                style={{
                  flex: 1,
                  minWidth: 0,
                  border: 0,
                  background: "transparent",
                  fontSize: "15px",
                  color: "var(--ink)",
                }}
              />
              {query && (
                <button
                  type="button"
                  className="btn-ghost icon-only"
                  title="Clear search"
                  aria-label="Clear search"
                  onClick={() => {
                    onQueryChange("");
                    onClearQuery?.();
                  }}
                >
                  <X size={14} aria-hidden="true" />
                </button>
              )}
              {query.trim().length > 0 && onSaveSearch && (
                <button
                  type="button"
                  className="btn-tactile sm"
                  onClick={() => onSaveSearch(query.trim(), query.trim(), modalityFilter, activeTag)}
                >
                  Save search
                </button>
              )}
            </div>

            {/* Active Similar / Filter Pills */}
            {similarSource && (
              <div
                className="surface-card"
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "12px",
                  padding: "11px 16px",
                }}
              >
                <span style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  Showing files similar to <strong style={{ color: "var(--ink)", fontWeight: 500 }}>{similarSource.title}</strong>
                </span>
                <button type="button" className="btn-ghost" onClick={onClearSimilar}>
                  Clear
                </button>
              </div>
            )}

            {/* Collapsible Filter Controls */}
            {filtersOpen && (
              <div
                className="surface-card"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "12px",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "10px" }}>
                  <div
                    style={{ display: "flex", alignItems: "center", gap: "4px", flexWrap: "wrap" }}
                    role="group"
                    aria-label="File type filter"
                  >
                    {(
                      [
                        { id: "all", label: "All" },
                        { id: "document", label: "Documents" },
                        { id: "image", label: "Images" },
                        { id: "code", label: "Code" },
                        { id: "video", label: "Video" },
                        { id: "audio", label: "Audio" },
                        { id: "archive", label: "Archives" },
                      ] as const
                    ).map((mod) => (
                      <button
                        key={mod.id}
                        type="button"
                        aria-pressed={modalityFilter === mod.id}
                        className={`folder-filter-tab${modalityFilter === mod.id ? " active" : ""}`}
                        onClick={() => onSetModalityFilter(mod.id)}
                      >
                        {mod.label}
                      </button>
                    ))}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                    {availableTags.length > 0 && (
                      <select
                        className="editorial-select"
                        aria-label="Filter by tag"
                        value={activeTag}
                        onChange={(e) => setActiveTag(e.target.value)}
                      >
                        <option value="">All tags</option>
                        {availableTags.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                    )}
                    {hasActiveFindFilters && (
                      <button type="button" className="btn-ghost" onClick={resetAllFindFilters}>
                        Reset filters
                      </button>
                    )}
                  </div>
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                  <span style={{ fontSize: "12px", color: "var(--ink-3)", marginRight: "4px" }}>Quick operators</span>
                  {OPERATOR_CHIPS.map((op) => {
                    const isUsed = activeOperators.some((tok) => tok.toLowerCase() === op.toLowerCase());
                    return (
                      <button
                        key={op}
                        type="button"
                        className={`flat-chip${isUsed ? " active" : ""}`}
                        onClick={() => (isUsed ? removeOperatorToken(op) : appendOperator(op))}
                      >
                        {op}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Saved Searches when query is empty */}
            {!query.trim() && savedSearches.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px" }}>
                {savedSearches.map((ss) => (
                  <div key={ss.id} className="flat-chip" style={{ paddingRight: onDeleteSavedSearch ? "6px" : undefined }}>
                    <button
                      type="button"
                      onClick={() => {
                        onQueryChange(ss.query);
                        onSetModalityFilter(ss.modality || "all");
                        setActiveTag(ss.tag || "");
                      }}
                    >
                      {ss.name}
                    </button>
                    {onDeleteSavedSearch && (
                      <button
                        type="button"
                        aria-label={`Remove saved search ${ss.name}`}
                        style={{
                          color: "var(--ink-3)",
                          display: "inline-flex",
                          alignItems: "center",
                          padding: "2px",
                        }}
                        onClick={() => onDeleteSavedSearch(ss.id)}
                      >
                        <X size={12} aria-hidden="true" />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}

            {/* Results */}
            <div className="shelf-section">
              <div className="shelf-section-header">
                <span className="tabular-nums" style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  {filteredResults.length} {filteredResults.length === 1 ? "match" : "matches"}
                  {latencyMs > 0 ? ` · ${latencyMs} ms` : ""}
                </span>
                {selectedFileId && (
                  <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                    Space to preview · Enter to inspect
                  </span>
                )}
              </div>

              {filteredResults.length === 0 ? (
                <div className="surface-card" style={{ marginTop: "12px", padding: "40px 24px", textAlign: "center", color: "var(--ink-3)" }}>
                  <p style={{ fontSize: "15px", color: "var(--ink-2)", fontWeight: 500 }}>No matching files</p>
                  <p style={{ fontSize: "13.5px", marginTop: "6px" }}>
                    Try broader words or ask AI in plain language.
                  </p>
                  <div style={{ display: "flex", justifyContent: "center", gap: "10px", marginTop: "16px" }}>
                    {hasActiveFindFilters && (
                      <button type="button" className="btn-tactile" onClick={resetAllFindFilters}>
                        Reset filters
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn-tactile solid"
                      onClick={() => {
                        setFindMode("ask");
                        if (query.trim()) setChatInput(query.trim());
                      }}
                    >
                      Ask AI instead
                    </button>
                  </div>
                </div>
              ) : (
                <div className="shelf-list-surface">
                  {filteredResults.slice(0, resultsLimit).map((f) => {
                    const isSelected = f.id === selectedFileId;
                    return (
                      <div
                        key={f.id}
                        data-find-file-id={f.id}
                        className={`file-item-row${isSelected ? " selected" : ""}${cutIdSet.has(f.id) ? " file-cut-dimmed" : ""}`}
                        onClick={() => onSelectFile(f.id)}
                        onDoubleClick={() => {
                          onSelectFile(f.id);
                          onOpenQuickLook();
                        }}
                        onContextMenu={(e) => onContextMenuFile?.(e, f)}
                      >
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div className="font-display" style={{ fontSize: "15px", fontWeight: 500, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {f.title}
                          </div>
                          <div style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "3px", display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                            <span>{f.format.toLowerCase()}</span>
                            {f.size && (
                              <>
                                <span aria-hidden="true">·</span>
                                <span className="tabular-nums">{f.size}</span>
                              </>
                            )}
                            {f.date && (
                              <>
                                <span aria-hidden="true">·</span>
                                <span className="tabular-nums">{f.date}</span>
                              </>
                            )}
                            {f.shelfName && (
                              <>
                                <span aria-hidden="true">·</span>
                                {onJumpToShelf && f.shelfId ? (
                                  <button
                                    type="button"
                                    style={{ color: "var(--ink-2)" }}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      onJumpToShelf(f.shelfId, f.subShelf);
                                    }}
                                  >
                                    {f.shelfName}
                                  </button>
                                ) : (
                                  <span>{f.shelfName}</span>
                                )}
                              </>
                            )}
                            {f.metadata?.doc_type && (
                              <>
                                <span aria-hidden="true">·</span>
                                <span style={{ color: "var(--ink-2)", textTransform: "capitalize" }}>
                                  {String(f.metadata.doc_type)}
                                </span>
                              </>
                            )}
                            {f.metadata?.merchant && (
                              <>
                                <span aria-hidden="true">·</span>
                                <span style={{ color: "var(--ink-2)", fontWeight: 500 }}>
                                  {String(f.metadata.merchant)}
                                </span>
                              </>
                            )}
                            {f.metadata?.amount !== undefined && f.metadata?.amount !== null && f.metadata?.amount !== "" && (
                              <>
                                <span aria-hidden="true">·</span>
                                <span className="tabular-nums" style={{ color: "var(--ink-2)", fontWeight: 500 }}>
                                  {f.metadata.currency === "PHP"
                                    ? "₱"
                                    : f.metadata.currency === "USD"
                                    ? "$"
                                    : f.metadata.currency
                                    ? `${String(f.metadata.currency)} `
                                    : ""}
                                  {String(f.metadata.amount)}
                                </span>
                              </>
                            )}
                            {f.metadata?.due_date && (
                              <>
                                <span aria-hidden="true">·</span>
                                <span className="tabular-nums" style={{ color: "var(--ink-2)" }}>
                                  Due {String(f.metadata.due_date)}
                                </span>
                              </>
                            )}
                            {f.metadata?.invoice_number && (
                              <>
                                <span aria-hidden="true">·</span>
                                <span className="tabular-nums" style={{ color: "var(--ink-3)" }}>
                                  #{String(f.metadata.invoice_number)}
                                </span>
                              </>
                            )}
                            {f.tags && f.tags.length > 0 && (
                              <span style={{ display: "inline-flex", alignItems: "center", gap: "4px", marginLeft: "2px" }}>
                                {f.tags.slice(0, 3).map((tag) => (
                                  <button
                                    key={`${f.id}-tag-${tag}`}
                                    type="button"
                                    className={`flat-chip${activeTag === tag ? " active" : ""}`}
                                    style={{ minHeight: "20px", padding: "1px 7px", fontSize: "11px" }}
                                    title={`Filter by #${tag}`}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setActiveTag((prev) => (prev === tag ? "" : tag));
                                    }}
                                  >
                                    #{tag}
                                  </button>
                                ))}
                              </span>
                            )}
                          </div>
                          {f.whyMatched && f.whyMatched.trim() && (
                            <div
                              style={{
                                fontSize: "12px",
                                color: "var(--ink-2)",
                                marginTop: "3px",
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {cleanPreviewText(f.whyMatched)}
                            </div>
                          )}
                        </div>
                        <div className="row-actions-inline" style={{ display: "flex", alignItems: "center", gap: "4px", flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
                          <button
                            type="button"
                            className="btn-tactile sm"
                            title="Preview file"
                            onClick={() => {
                              onSelectFile(f.id);
                              onOpenQuickLook();
                            }}
                          >
                            Preview
                          </button>
                          {onRenameFile && (
                            <button
                              type="button"
                              className="btn-ghost"
                              style={{ minHeight: "26px", padding: "2px 8px", fontSize: "12px" }}
                              title="Rename file"
                              onClick={() => onRenameFile(f)}
                            >
                              Rename
                            </button>
                          )}
                          {onContextMenuFile && (
                            <button
                              type="button"
                              className="row-icon-btn"
                              title="Actions"
                              aria-label={`Actions for ${f.title}`}
                              onClick={(e) => onContextMenuFile(e, f)}
                            >
                              <MoreHorizontal size={14} aria-hidden="true" />
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                  {filteredResults.length > resultsLimit && (
                    <div style={{ padding: "16px 20px", textAlign: "center", borderTop: "1px solid var(--rule)" }}>
                      <button
                        type="button"
                        className="btn-tactile"
                        onClick={() => setResultsLimit((p) => p + 60)}
                      >
                        Show more ({filteredResults.length - resultsLimit} remaining)
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}

        {/* ASK AI CHAT MODE */}
        {findMode === "ask" && (
          <div
            id="askChatShell"
            className="ask-chat-canvas"
          >
            {/* Flat Hairline Scope Strip */}
            <div className="ask-scope-strip">
              <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap", minWidth: 0 }}>
                <span style={{ fontSize: "12px", fontWeight: 500, color: "var(--ink-3)" }}>
                  Scope
                </span>
                <select
                  aria-label="Ask AI folder scope"
                  className="editorial-select"
                  style={{ minHeight: "28px", padding: "3px 26px 3px 9px", fontSize: "12px", maxWidth: "240px" }}
                  value={scopeFolder || ""}
                  onChange={(e) => onSetScopeFolder?.(e.target.value || null)}
                >
                  <option value="">All workspace ({allFiles.length || results.length})</option>
                  {scopeFoldersList.map((sf) => (
                    <option key={sf.path} value={sf.path}>
                      {sf.pinned ? `★ ${sf.label}` : sf.label}
                    </option>
                  ))}
                </select>
                {scopeFolder && (
                  <>
                    <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                      {scopedFilesCount} {scopedFilesCount === 1 ? "file" : "files"}
                    </span>
                    {onTogglePinFolder && (
                      <button
                        type="button"
                        className={`btn-ghost${pinnedFolders.some((p) => p.path === scopeFolder) ? " active" : ""}`}
                        style={{ minHeight: "26px", padding: "2px 8px", fontSize: "12px" }}
                        onClick={() => onTogglePinFolder(scopeFolder)}
                      >
                        {pinnedFolders.some((p) => p.path === scopeFolder) ? "Pinned" : "Pin folder"}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn-ghost"
                      style={{ minHeight: "26px", padding: "2px 8px", fontSize: "12px" }}
                      onClick={() => onSetScopeFolder?.(null)}
                    >
                      Clear scope
                    </button>
                  </>
                )}
              </div>
            </div>

            {/* Messages Thread */}
            <div
              ref={chatScrollRef}
              style={{
                flex: 1,
                minHeight: 0,
                minWidth: 0,
                padding: "8px 0 20px",
                display: "flex",
                flexDirection: "column",
                gap: "20px",
                overflowY: "auto",
                overflowX: "hidden",
              }}
            >
              {chatHistory.length === 0 ? (
                <div style={{ padding: "24px 0", display: "flex", flexDirection: "column", gap: "18px" }}>
                  <div style={{ maxWidth: "600px" }}>
                    <h2 className="font-display" style={{ fontSize: "22px", fontWeight: 500, letterSpacing: "-0.02em", color: "var(--ink)" }}>
                      {activeScopeLabel
                        ? `Ask anything in ${activeScopeLabel}`
                        : displayName
                        ? `Hi ${displayName}, ask anything about your files.`
                        : "Ask anything about your files."}
                    </h2>
                    <p style={{ fontSize: "13.5px", color: "var(--ink-2)", marginTop: "8px", lineHeight: 1.6 }}>
                      Ask about your files, summarize documents, or organize with reversible plans.
                    </p>
                  </div>
                  <div
                    style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px", maxWidth: "680px" }}
                    role="group"
                    aria-label="Starter prompts"
                  >
                    {starterPrompts.map((sp) => (
                      <button
                        key={sp.label}
                        type="button"
                        className="flat-chip"
                        disabled={asking}
                        onClick={() => {
                          if (sp.prompt.endsWith(" ")) {
                            setChatInput(sp.prompt);
                            const input = document.getElementById("askChatInput") as HTMLInputElement | null;
                            input?.focus();
                          } else {
                            submitQuestion(sp.prompt);
                          }
                        }}
                      >
                        {sp.label}
                      </button>
                    ))}
                  </div>
                  <p style={{ fontSize: "12px", color: "var(--ink-3)", lineHeight: 1.5 }}>
                    Tip: Type <span className="kbd-pill">/</span> for commands, <span className="kbd-pill">@</span> to reference a specific file, or chain actions with &ldquo;and then&rdquo;.
                  </p>
                  {pinnedFolders.length > 0 && (
                    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px" }}>
                      <span style={{ fontSize: "11.5px", color: "var(--ink-3)", marginRight: "4px" }}>Pinned folders:</span>
                      {pinnedFolders.map((pf) => (
                        <button
                          key={`empty-pin-${pf.path}`}
                          type="button"
                          className={`flat-chip${scopeFolder === pf.path ? " active" : ""}`}
                          onClick={() => onSetScopeFolder?.(scopeFolder === pf.path ? null : pf.path)}
                        >
                          {pf.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                chatHistory.map((msg) => {
                  const metaTokens =
                    msg.role === "assistant"
                      ? [...(msg.trace || []), ...(msg.appliedFilters || [])].filter(Boolean)
                      : [];
                  return (
                  <div
                    key={msg.id}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      alignItems: msg.role === "user" ? "flex-end" : "flex-start",
                      gap: "10px",
                      width: "100%",
                      minWidth: 0,
                    }}
                  >
                    <div
                      className={`ai-chat-bubble ${msg.role}`}
                      style={
                        msg.role === "user"
                          ? {
                              padding: "12px 17px",
                              borderRadius: "14px",
                              background: "var(--ink)",
                              color: "var(--bg-surface)",
                              fontSize: "15px",
                              lineHeight: 1.65,
                              whiteSpace: "pre-wrap",
                            }
                          : {
                              padding: "2px 0",
                              background: "transparent",
                              color: "var(--ink)",
                              fontSize: "15px",
                              lineHeight: 1.7,
                              whiteSpace: "pre-wrap",
                            }
                      }
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: "10px",
                          marginBottom: "4px",
                        }}
                      >
                        <div
                          style={{
                            fontSize: "11px",
                            fontWeight: 500,
                            opacity: 0.65,
                          }}
                        >
                          {msg.role === "user" ? (displayName || "You") : "good shelves"}
                          {msg.latencyMs ? ` · ${msg.latencyMs} ms` : ""}
                        </div>
                        {msg.role === "assistant" && msg.text && (
                          <div style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
                            <button
                              type="button"
                              className={`btn-ghost${msg.feedback === "up" ? " active" : ""}`}
                              style={{ minHeight: "22px", padding: "1px 6px", fontSize: "11px" }}
                              title="Helpful answer (calibrates local AI ranking)"
                              aria-label="Helpful answer"
                              onClick={() => {
                                const topFile = msg.citations?.[0]?.file;
                                setChatHistory((prev) =>
                                  prev.map((m) => (m.id === msg.id ? { ...m, feedback: "up" } : m))
                                );
                                void recordUserFeedback({
                                  query: msg.questionText || query,
                                  fileId: topFile?.id || "",
                                  shelfId: topFile?.shelfId || "",
                                  action: "thumbs_up",
                                }).catch(() => {});
                                onShowToast?.("Recorded positive feedback for local AI calibration");
                              }}
                            >
                              <ThumbsUp size={11} aria-hidden="true" />
                            </button>
                            <button
                              type="button"
                              className={`btn-ghost${msg.feedback === "down" ? " active" : ""}`}
                              style={{ minHeight: "22px", padding: "1px 6px", fontSize: "11px" }}
                              title="Unhelpful answer (adjusts local AI affinity)"
                              aria-label="Unhelpful answer"
                              onClick={() => {
                                const topFile = msg.citations?.[0]?.file;
                                setChatHistory((prev) =>
                                  prev.map((m) => (m.id === msg.id ? { ...m, feedback: "down" } : m))
                                );
                                void recordUserFeedback({
                                  query: msg.questionText || query,
                                  fileId: topFile?.id || "",
                                  shelfId: topFile?.shelfId || "",
                                  action: "thumbs_down",
                                }).catch(() => {});
                                onShowToast?.("Adjusted local AI affinity from your feedback");
                              }}
                            >
                              <ThumbsDown size={11} aria-hidden="true" />
                            </button>
                            <button
                              type="button"
                              className="btn-ghost"
                              style={{ minHeight: "22px", padding: "1px 7px", fontSize: "11px" }}
                              title="Copy response"
                              aria-label="Copy response"
                              onClick={() => {
                                navigator.clipboard?.writeText(msg.text);
                                onShowToast?.("Copied response");
                              }}
                            >
                              Copy
                            </button>
                          </div>
                        )}
                      </div>
                      <div>{msg.role === "assistant" ? renderAiMarkdown(msg.text) : msg.text}</div>
                      {metaTokens.length > 0 && (
                        <div
                          style={{
                            fontSize: "11.5px",
                            color: "var(--ink-3)",
                            marginTop: "6px",
                            lineHeight: 1.45,
                          }}
                        >
                          {metaTokens.join(" · ")}
                        </div>
                      )}
                    </div>

                    {msg.role === "assistant" && msg.processSummary && msg.processSummary.length > 0 && (
                      <div className="ai-process-panel" role="region" aria-label="Workspace process status">
                        <div className="ai-process-header">
                          <span>Available System Processes</span>
                          <span style={{ fontWeight: 400, color: "var(--ink-3)" }}>
                            {msg.processSummary.reduce((acc, s) => acc + s.pending, 0)} pending item(s)
                          </span>
                        </div>
                        <div className="ai-process-rows">
                          {msg.processSummary.map((st) => (
                            <div key={`${msg.id}-proc-${st.key}`} className="ai-process-row">
                              <div className="ai-process-bar-wrap">
                                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px", fontSize: "12.5px" }}>
                                  <span style={{ fontWeight: 500, color: "var(--ink)" }}>{st.label}</span>
                                  <span className="tabular-nums" style={{ fontSize: "11.5px", color: st.pending > 0 ? "var(--ink-2)" : "var(--ink-3)" }}>
                                    {st.pending > 0 ? `${st.pending} ready · ${st.percent}% clean` : "100% clean"}
                                  </span>
                                </div>
                                <div className="live-indexing-track" style={{ height: "5px" }}>
                                  <div
                                    className="live-indexing-fill"
                                    style={{ width: `${Math.max(st.pending > 0 ? 6 : 100, st.percent)}%` }}
                                  />
                                </div>
                                <div style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>{st.detail}</div>
                              </div>
                              {st.pending > 0 && st.actionPrompt && (
                                <button
                                  type="button"
                                  className="btn-tactile"
                                  style={{ padding: "4px 10px", minHeight: "28px", fontSize: "12px", flexShrink: 0 }}
                                  disabled={asking}
                                  onClick={() => submitQuestion(st.actionPrompt!)}
                                >
                                  Inspect
                                </button>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {msg.proposedAction && (() => {
                      const act = msg.proposedAction;
                      const status = msg.actionStatus || "pending";
                      const items = act.items || [];
                      const checkedMap = msg.selectedActionIds || {};
                      const checkedCount = items.filter((it) => checkedMap[it.fileId] ?? true).length;
                      const isDestructive = Boolean(act.destructive);
                      const isActionDisabled =
                        status !== "pending" || (Boolean(act.requiresSelection) && checkedCount === 0);

                      return (
                        <div
                          className={`ai-action-card ${isDestructive ? "destructive" : ""} ${status === "executed" ? "executed" : ""}`}
                          role="region"
                          aria-label={`Action confirmation: ${act.title}`}
                        >
                          <div className="ai-action-card-header">
                            <div style={{ minWidth: 0 }}>
                              <div className="ai-action-title-row">
                                <span className={`ai-action-pill ${isDestructive ? "danger" : ""}`}>
                                  {status === "executed"
                                    ? "Applied"
                                    : status === "cancelled"
                                    ? "Cancelled"
                                    : isDestructive
                                    ? act.reversible
                                      ? "Trash · Undoable"
                                      : "Permanent Delete"
                                    : act.reversible
                                    ? "Undoable Plan"
                                    : "Needs Confirmation"}
                                </span>
                                {act.targetLabel && (
                                  <span style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>
                                    → {act.targetLabel}
                                  </span>
                                )}
                              </div>
                              <div className="font-display" style={{ fontSize: "15.5px", fontWeight: 500, color: "var(--ink)", marginTop: "4px" }}>
                                {act.title}
                              </div>
                              <div style={{ fontSize: "12.5px", color: "var(--ink-2)", marginTop: "2px", lineHeight: 1.45 }}>
                                {act.summary}
                              </div>
                              {act.steps && act.steps.length > 1 && (
                                <div className="ai-action-steps">
                                  {act.steps.map((st, sIdx) => (
                                    <span key={`step-${msg.id}-${sIdx}`} className="ai-action-step-pill">
                                      {sIdx + 1}. {st.title}
                                    </span>
                                  ))}
                                </div>
                              )}
                            </div>
                          </div>

                          {items.length > 0 && status !== "executed" && status !== "cancelled" && (
                            <div className="ai-action-list-wrap">
                              {items.length > 1 && (
                                <div className="ai-action-list-bar">
                                  <span style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>
                                    {checkedCount} of {items.length} selected
                                  </span>
                                  <div style={{ display: "flex", gap: "8px" }}>
                                    <button
                                      type="button"
                                      className="ai-action-link-btn"
                                      onClick={() => setAllActionItemsChecked(msg.id, true)}
                                    >
                                      Select all
                                    </button>
                                    <button
                                      type="button"
                                      className="ai-action-link-btn"
                                      onClick={() => setAllActionItemsChecked(msg.id, false)}
                                    >
                                      Clear
                                    </button>
                                  </div>
                                </div>
                              )}
                              <div className="ai-action-items">
                                {items.map((it) => {
                                  const isChecked = checkedMap[it.fileId] ?? true;
                                  const isFolderItem = it.fileId.startsWith("folder::");
                                  return (
                                    <label
                                      key={`${msg.id}-act-${it.fileId}`}
                                      className={`ai-action-item ${isChecked ? "checked" : ""}`}
                                    >
                                      <input
                                        type="checkbox"
                                        checked={isChecked}
                                        disabled={status !== "pending" || !act.requiresSelection}
                                        onChange={() => toggleActionItemChecked(msg.id, it.fileId)}
                                      />
                                      {!isFolderItem && it.hasThumbnail && (
                                        <img
                                          src={getFileThumbnailUrl(it.fileId)}
                                          alt=""
                                          className="ai-thumb-mini"
                                          loading="lazy"
                                          onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                                        />
                                      )}
                                      <div style={{ minWidth: 0, flex: 1 }}>
                                        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                                          {!isFolderItem ? (
                                            <button
                                              type="button"
                                              className="font-display ai-wrap-text"
                                              style={{
                                                fontSize: "13.5px",
                                                fontWeight: 500,
                                                color: "var(--ink)",
                                                textAlign: "left",
                                                maxWidth: "100%",
                                              }}
                                              onClick={(e) => {
                                                e.preventDefault();
                                                onSelectFile(it.fileId);
                                                onOpenQuickLook();
                                              }}
                                            >
                                              {it.fileName}
                                            </button>
                                          ) : (
                                            <span className="font-display ai-wrap-text" style={{ fontSize: "13.5px", fontWeight: 500, color: "var(--ink)" }}>
                                              {it.title || it.fileName}
                                            </span>
                                          )}
                                          <span className="file-type-tag">{it.format}</span>
                                          {it.size && (
                                            <span style={{ fontSize: "11px", color: "var(--ink-3)" }}>{it.size}</span>
                                          )}
                                        </div>
                                        {it.detail && (
                                          <div style={{ fontSize: "11.5px", color: "var(--ink-2)", marginTop: "2px" }}>
                                            {cleanPreviewText(it.detail)}
                                          </div>
                                        )}
                                        {it.previewText && (
                                          <div style={{ fontSize: "11px", color: "var(--ink-3)", marginTop: "2px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                            {cleanPreviewText(it.previewText)}
                                          </div>
                                        )}
                                      </div>
                                    </label>
                                  );
                                })}
                              </div>
                            </div>
                          )}

                          {status === "executing" && (
                            <div className="ai-action-progress-box" role="status" aria-live="polite">
                              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px", fontSize: "12px" }}>
                                <span style={{ display: "inline-flex", alignItems: "center", gap: "7px", fontWeight: 500, color: "var(--ink)", minWidth: 0 }}>
                                  <span className="status-dot pulse" aria-hidden="true" />
                                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                    {liveProgress ? `${liveProgress.operation}${liveProgress.file_name ? ` · ${liveProgress.file_name}` : ""}` : "Applying action…"}
                                  </span>
                                </span>
                                <span className="tabular-nums" style={{ color: "var(--ink-2)", flexShrink: 0 }}>
                                  {liveProgress && liveProgress.total > 1
                                    ? `${liveProgress.current}/${liveProgress.total} (${Math.min(100, Math.round((liveProgress.current / liveProgress.total) * 100))}%)`
                                    : `${checkedCount} item(s)`}
                                </span>
                              </div>
                              <div className="live-indexing-track">
                                <div
                                  className={`live-indexing-fill ${!liveProgress || liveProgress.total <= 1 ? "indeterminate" : ""}`}
                                  style={
                                    !liveProgress || liveProgress.total <= 1
                                      ? { width: "36%" }
                                      : { width: `${Math.max(5, Math.min(100, Math.round((liveProgress.current / liveProgress.total) * 100)))}%` }
                                  }
                                />
                              </div>
                            </div>
                          )}

                          {status === "pending" || status === "executing" ? (
                            <div className="ai-action-footer">
                              <button
                                type="button"
                                className="btn-tactile"
                                disabled={status === "executing"}
                                onClick={() => handleCancelAiAction(msg.id)}
                              >
                                Cancel
                              </button>
                              <button
                                type="button"
                                className={`btn-tactile ${isDestructive ? "danger-solid" : "solid"}`}
                                disabled={isActionDisabled}
                                onClick={() => handleConfirmAiAction(msg.id)}
                              >
                                {status === "executing"
                                  ? "Applying…"
                                  : act.requiresSelection && items.length > 0
                                  ? `Confirm (${checkedCount})`
                                  : "Confirm"}
                              </button>
                            </div>
                          ) : (
                            <div className="ai-action-done-note" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px", flexWrap: "wrap" }}>
                              <span>{msg.actionResultSummary}</span>
                              {status === "executed" && act.reversible && act.actionType !== "undo" && onUndoAction && (
                                <button
                                  type="button"
                                  className="ai-action-link-btn"
                                  onClick={onUndoAction}
                                >
                                  Undo
                                </button>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })()}

                    {!msg.proposedAction && msg.citations && msg.citations.length > 0 && (() => {
                      const isExpanded = Boolean(expandedChatIds[msg.id]);
                      const visibleCitations = isExpanded ? msg.citations : msg.citations.slice(0, 4);
                      return (
                        <div className="ai-citations-col">
                          <div className="ai-citation-list">
                            {visibleCitations.map(({ file: f, snippet, pageNumber, slideNumber, lineNumber, sheetName }) => {
                              const anchorObj =
                                pageNumber || slideNumber || lineNumber || sheetName
                                  ? { pageNumber, slideNumber, lineNumber, sheetName }
                                  : undefined;
                              const anchorLabel = pageNumber
                                ? `Page ${pageNumber}`
                                : slideNumber
                                ? `Slide ${slideNumber}`
                                : lineNumber
                                ? `Line ${lineNumber}`
                                : sheetName
                                ? `Sheet ${sheetName}`
                                : null;
                              return (
                              <div
                                key={`${msg.id}-${f.id}`}
                                onClick={() => onSelectFile(f.id)}
                                onContextMenu={(e) => onContextMenuFile?.(e, f)}
                                className={`ai-citation-row${f.id === selectedFileId ? " selected" : ""}`}
                              >
                                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px", minWidth: 0 }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: "10px", minWidth: 0, flex: 1 }}>
                                    {f.hasThumbnail && (
                                      <img
                                        src={getFileThumbnailUrl(f.id)}
                                        alt=""
                                        className="ai-thumb-mini"
                                        loading="lazy"
                                        onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                                      />
                                    )}
                                    <div style={{ minWidth: 0, flex: 1 }}>
                                      <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                                        <button
                                          type="button"
                                          className="font-display ai-wrap-text"
                                          style={{ fontSize: "14px", fontWeight: 500, color: "var(--ink)", textAlign: "left", maxWidth: "100%" }}
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            onSelectFile(f.id);
                                            onOpenQuickLook(anchorObj);
                                          }}
                                        >
                                          {f.title}
                                        </button>
                                        {anchorLabel && (
                                          <button
                                            type="button"
                                            className="tag-pill"
                                            style={{ padding: "1px 6px", fontSize: "11px" }}
                                            title={`Jump directly to ${anchorLabel} in Quick Look`}
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              onSelectFile(f.id);
                                              onOpenQuickLook(anchorObj);
                                            }}
                                          >
                                            ↗ {anchorLabel}
                                          </button>
                                        )}
                                      </div>
                                      <div className="ai-wrap-text" style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "1px" }}>
                                        {f.format.toLowerCase()}
                                        {f.size ? ` · ${f.size}` : ""}
                                        {f.date ? ` · ${f.date}` : ""}
                                        {f.shelfName ? ` · ${f.shelfName}` : ""}
                                      </div>
                                    </div>
                                  </div>
                                  <div className="citation-hover-actions">
                                    {renderCalmCitationActions(f)}
                                  </div>
                                </div>
                                {snippet && (
                                  <div className="ai-citation-snippet ai-wrap-text">
                                    {cleanPreviewText(snippet)}
                                  </div>
                                )}
                              </div>
                            );
                            })}
                            {msg.citations.length > 4 && (
                              <div
                                style={{
                                  padding: "6px 12px",
                                  borderTop: "1px solid var(--rule-soft)",
                                  display: "flex",
                                  alignItems: "center",
                                }}
                              >
                                <button
                                  type="button"
                                  className="btn-ghost"
                                  style={{ minHeight: "26px", padding: "2px 8px", fontSize: "12px" }}
                                  onClick={() =>
                                    setExpandedChatIds((prev) => ({ ...prev, [msg.id]: !prev[msg.id] }))
                                  }
                                >
                                  {isExpanded ? "Show fewer sources" : `Show all ${msg.citations.length} sources`}
                                </button>
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })()}

                    {msg.role === "assistant" && msg.choices && msg.choices.length > 0 && (
                      <div className="ai-choice-chips" role="group" aria-label="Suggested next actions">
                        {msg.choices.slice(0, 3).map((ch, cIdx) => (
                          <button
                            key={`${msg.id}-choice-${cIdx}`}
                            type="button"
                            className="flat-chip"
                            disabled={asking}
                            onClick={() => submitQuestion(ch.prompt)}
                          >
                            {ch.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  );
                })
              )}

              {asking && (
                <div
                  role="status"
                  aria-live="polite"
                  style={{
                    padding: "10px 14px",
                    borderRadius: "10px",
                    border: "1px solid var(--rule)",
                    background: "var(--bg-subtle)",
                    fontSize: "13px",
                    color: "var(--ink-2)",
                    display: "inline-flex",
                    flexDirection: "column",
                    gap: "6px",
                    alignSelf: "flex-start",
                    minWidth: liveProgress ? "240px" : undefined,
                  }}
                >
                  <div style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
                    <span className="status-dot pulse" aria-hidden="true" />
                    <span>
                      {liveProgress
                        ? `${liveProgress.operation}${liveProgress.file_name ? ` · ${liveProgress.file_name}` : ""}`
                        : "Inspecting your shelves…"}
                    </span>
                  </div>
                  {liveProgress && liveProgress.total > 1 && (
                    <div className="live-indexing-track">
                      <div
                        className="live-indexing-fill"
                        style={{ width: `${Math.max(6, Math.min(100, Math.round((liveProgress.current / liveProgress.total) * 100)))}%` }}
                      />
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* / Slash Command Quick-Picker */}
            {!pickerDismissed && slashMatches.length > 0 && (
              <div className="ai-at-picker" role="listbox" aria-label="AI slash commands">
                {slashMatches.map((sc, idx) => {
                  const isHighlighted = idx === (pickerIndex % slashMatches.length);
                  return (
                    <button
                      key={sc.cmd}
                      type="button"
                      role="option"
                      aria-selected={isHighlighted}
                      className={`flat-chip${isHighlighted ? " active" : ""}`}
                      title={sc.desc}
                      onClick={() => {
                        setChatInput(sc.template);
                        setPickerIndex(0);
                        setPickerDismissed(false);
                      }}
                    >
                      <strong>{sc.cmd}</strong> · {sc.desc}
                    </button>
                  );
                })}
              </div>
            )}

            {/* @ File Mention Quick-Picker */}
            {!pickerDismissed && atMentionMatches.length > 0 && (
              <div className="ai-at-picker" role="listbox" aria-label="Insert file reference">
                <span style={{ fontSize: "11px", color: "var(--ink-3)", marginRight: "6px" }}>Reference file:</span>
                {atMentionMatches.map((mf, idx) => {
                  const isHighlighted = idx === (pickerIndex % atMentionMatches.length);
                  return (
                    <button
                      key={`at-${mf.id}`}
                      type="button"
                      role="option"
                      aria-selected={isHighlighted}
                      className={`flat-chip${isHighlighted ? " active" : ""}`}
                      onClick={() => applyAtMention(mf)}
                    >
                      @{mf.name}
                    </button>
                  );
                })}
              </div>
            )}

            {/* Instant Match Jump Strip */}
            {instantMatches.length > 0 && (pickerDismissed || (slashMatches.length === 0 && atMentionMatches.length === 0)) && (
              <div className="ai-instant-strip" role="region" aria-label="Instant file matches">
                <span style={{ fontSize: "11px", color: "var(--ink-3)", flexShrink: 0 }}>Jump to:</span>
                {instantMatches.map((im) => (
                  <button
                    key={`inst-${im.id}`}
                    type="button"
                    className="flat-chip"
                    style={{ maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                    title={`Preview ${im.title}`}
                    onClick={() => {
                      onSelectFile(im.id);
                      onOpenQuickLook();
                    }}
                  >
                    {im.title}
                  </button>
                ))}
              </div>
            )}

            {/* Chat Compose Box */}
            <form
              onSubmit={handleSubmitChat}
              className="ask-compose-bar"
            >
              <input
                id="askChatInput"
                type="text"
                value={chatInput}
                onChange={(e) => {
                  setChatInput(e.target.value);
                  setPickerIndex(0);
                  setPickerDismissed(false);
                }}
                onKeyDown={(e) => {
                  const activeSlash = pickerDismissed ? [] : slashMatches;
                  const activeAt = pickerDismissed ? [] : atMentionMatches;

                  if (activeSlash.length > 0) {
                    const count = activeSlash.length;
                    const safeIdx = ((pickerIndex % count) + count) % count;
                    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
                      e.preventDefault();
                      setPickerIndex((safeIdx + 1) % count);
                      return;
                    }
                    if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
                      e.preventDefault();
                      setPickerIndex((safeIdx - 1 + count) % count);
                      return;
                    }
                    if (e.key === "Tab") {
                      e.preventDefault();
                      setChatInput(activeSlash[safeIdx].template);
                      setPickerIndex(0);
                      return;
                    }
                    if (e.key === "Enter") {
                      const picked = activeSlash[safeIdx];
                      if (picked.template.endsWith(" ")) {
                        e.preventDefault();
                        setChatInput(picked.template);
                        setPickerIndex(0);
                        return;
                      }
                      if (chatInput.trim().toLowerCase() !== picked.cmd) {
                        e.preventDefault();
                        submitQuestion(picked.template);
                        setPickerIndex(0);
                        return;
                      }
                    }
                    if (e.key === "Escape") {
                      e.preventDefault();
                      setPickerDismissed(true);
                      return;
                    }
                  } else if (activeAt.length > 0) {
                    const count = activeAt.length;
                    const safeIdx = ((pickerIndex % count) + count) % count;
                    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
                      e.preventDefault();
                      setPickerIndex((safeIdx + 1) % count);
                      return;
                    }
                    if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
                      e.preventDefault();
                      setPickerIndex((safeIdx - 1 + count) % count);
                      return;
                    }
                    if (e.key === "Tab" || e.key === "Enter") {
                      e.preventDefault();
                      applyAtMention(activeAt[safeIdx]);
                      return;
                    }
                    if (e.key === "Escape") {
                      e.preventDefault();
                      setPickerDismissed(true);
                      return;
                    }
                  } else if (e.key === "ArrowUp" && !chatInput.trim()) {
                    const lastUserMsg = [...chatHistory].reverse().find((m) => m.role === "user");
                    if (lastUserMsg) {
                      e.preventDefault();
                      setChatInput(lastUserMsg.text);
                    }
                  } else if (e.key === "Escape" && chatInput) {
                    e.preventDefault();
                    setChatInput("");
                  }
                }}
                placeholder={
                  activeScopeLabel
                    ? `Ask or run actions inside ${activeScopeLabel} (/ for commands, @ for files)…`
                    : displayName
                    ? `Ask anything, chain actions, /commands, or @file, ${displayName}…`
                    : 'Ask anything, /commands, @file, or chain actions…'
                }
                aria-label="Ask a question"
                style={{
                  flex: 1,
                  minWidth: 0,
                  border: 0,
                  padding: "6px 4px",
                  fontSize: "15px",
                  background: "transparent",
                  color: "var(--ink)",
                }}
              />
              <button
                type="submit"
                id="askChatSendBtn"
                className="btn-tactile solid"
                disabled={asking || !chatInput.trim()}
              >
                Send
              </button>
            </form>
          </div>
        )}
      </div>
    </section>
  );
});
