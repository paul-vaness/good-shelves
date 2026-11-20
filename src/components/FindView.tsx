import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  AiChoice,
  AskCitation,
  AskResponse,
  ProposedAiAction,
  SavedSearch,
  ShelfFile,
  cleanPreviewText,
  getFileThumbnailUrl,
  openFileInOS,
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
  latencyMs: number;
  selectedFileId: string;
  similarSource?: ShelfFile | null;
  askResult?: AskResponse | null;
  asking?: boolean;
  findMode?: "search" | "ask";
  userName?: string;
  onSetFindMode?: (mode: "search" | "ask") => void;
  onQueryChange: (q: string) => void;
  onClearQuery?: () => void;
  onClearSimilar?: () => void;
  onAskShelves?: (
    question: string,
    context?: {
      recentFileIds?: string[];
      history?: Array<{ role: string; text: string }>;
      pendingAction?: ProposedAiAction | null;
    }
  ) => void;
  onExecuteAiAction?: (
    action: ProposedAiAction,
    selectedIds: string[]
  ) => Promise<{ ok: boolean; count: number; summary: string }>;
  onUndoAction?: () => void;
  onTriggerPrompt?: (q: string) => void;
  onSetModalityFilter: (mod: string) => void;
  onSelectFile: (fileId: string) => void;
  onOpenQuickLook: () => void;
  onSaveSearch?: (name: string, query: string, modality: string, tag: string) => void;
  onDeleteSavedSearch?: (searchId: string) => void;
  onRenameFile?: (file: ShelfFile) => void;
  onMoveFile?: (file: ShelfFile) => void;
  onDeleteFile?: (file: ShelfFile) => void;
  onCopyPath?: (file: ShelfFile) => void;
  onShowToast?: (msg: string) => void;
  clipboard?: { fileIds: string[]; mode: "copy" | "cut" } | null;
  onSetClipboard?: (fileIds: string[], mode: "copy" | "cut") => void;
  onPasteClipboard?: () => void;
  onContextMenuFile?: (e: React.MouseEvent, file: ShelfFile) => void;
  onJumpToShelf?: (shelfId: string, subShelf?: string) => void;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  latencyMs?: number;
  appliedFilters?: string[];
  totalMatched?: number;
  citations?: Array<{ file: ShelfFile; snippet: string; score: number }>;
  choices?: AiChoice[];
  proposedAction?: ProposedAiAction | null;
  actionStatus?: "pending" | "executing" | "executed" | "cancelled";
  actionResultSummary?: string;
  selectedActionIds?: Record<string, boolean>;
}

const ASK_STARTER_PROMPTS = [
  "What's on my shelves?",
  "Clean up my files",
  "Create folder Tax 2026, move receipts to it, and pin them",
  "Delete this picture from Downloads",
  "Summarize my research PDFs",
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
  suggestions = [],
  latencyMs,
  selectedFileId,
  similarSource = null,
  askResult = null,
  asking = false,
  findMode: controlledFindMode,
  userName = "",
  onSetFindMode,
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
  onContextMenuFile,
  onJumpToShelf,
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
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [expandedChatIds, setExpandedChatIds] = useState<Record<string, boolean>>({});
  const lastAskRef = useRef<AskResponse | null>(null);
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
    const resolvedCitations = (askResult.citations || []).slice(0, 25).map((c) => ({
      file: citationToShelfFile(c, fileLookup),
      snippet: c.snippet,
      score: c.score,
    }));
    const proposed = askResult.proposedAction ?? null;
    const defaultSelected: Record<string, boolean> = {};
    if (proposed?.items) {
      for (const it of proposed.items) {
        defaultSelected[it.fileId] = it.selectedByDefault !== false;
      }
    }
    setChatHistory((prev) => [
      ...prev,
      {
        id: `a-${Date.now()}-${prev.length}`,
        role: "assistant",
        text: askResult.answer,
        latencyMs: askResult.latencyMs,
        appliedFilters: askResult.appliedFilters,
        totalMatched: askResult.totalMatched,
        citations: resolvedCitations,
        choices: askResult.choices || [],
        proposedAction: proposed,
        actionStatus: proposed ? "pending" : undefined,
        selectedActionIds: proposed ? defaultSelected : undefined,
      },
    ]);
  }, [askResult, fileLookup]);

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
  };

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

  const activeOperators = useMemo(() => {
    const matches = query.match(
      /(?:^|\s)(-(?:[a-z]+):(?:"[^"]+"|[^\s]+)|-[^\s"]+|(?:type|ext|folder|shelf|tag|has|after|before|unnamed):(?:"[^"]+"|[^\s]+))/gi
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
    const recentTurns = chatHistory.slice(-6).map((m) => ({ role: m.role, text: m.text }));
    const pendingMsg = [...chatHistory].reverse().find((m) => m.role === "assistant" && m.actionStatus === "pending" && m.proposedAction);
    const isCancelReply = Boolean(pendingMsg && /^(?:cancel|cancel plan|never ?mind|abort|stop|forget it)$/i.test(q));
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
    });
  };

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
    if (action.requiresSelection && selectedIds.length === 0) {
      onShowToast?.("Select at least one item to confirm.");
      return;
    }

    setChatHistory((prev) =>
      prev.map((m) => (m.id === msgId ? { ...m, actionStatus: "executing" } : m))
    );
    try {
      const res = await onExecuteAiAction(action, selectedIds);
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
      const idx = filteredResults.findIndex((f) => f.id === selectedFileId);
      const nextIdx =
        e.key === "ArrowDown"
          ? Math.min(filteredResults.length - 1, idx + 1)
          : Math.max(0, idx <= 0 ? 0 : idx - 1);
      const nextId = filteredResults[nextIdx].id;
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
    <div style={{ display: "flex", alignItems: "center", gap: "8px" }} onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className="shelf-header-subtle-btn"
        onClick={() => handleOpenFile(f)}
      >
        Open
      </button>
      <button
        type="button"
        className="shelf-header-subtle-btn"
        onClick={() => handleRevealFile(f)}
      >
        Show in folder
      </button>
      {onContextMenuFile && (
        <button
          type="button"
          className="shelf-header-subtle-btn"
          aria-label={`More actions for ${f.title}`}
          onClick={(e) => onContextMenuFile(e, f)}
        >
          ⋯
        </button>
      )}
    </div>
  );

  return (
    <section
      className={`view-sheet${active ? " active" : ""}`}
      id="viewFind"
      aria-label="Search & Ask"
      tabIndex={-1}
      onKeyDown={handleListKeyDown}
    >
      <div className="page-container find-full-container">
        <div className="find-compact-topbar">
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
              {hasActiveFindFilters ? ` (${(modalityFilter !== "all" ? 1 : 0) + (activeTag ? 1 : 0) + activeOperators.length})` : ""}
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
                gap: "12px",
                padding: "12px 18px",
                borderRadius: "14px",
                border: "1px solid var(--rule-strong)",
                background: "var(--bg-surface)",
              }}
            >
              <input
                id="findSearchInput"
                type="search"
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
                  className="shelf-header-subtle-btn"
                  onClick={() => {
                    onQueryChange("");
                    onClearQuery?.();
                  }}
                >
                  Clear
                </button>
              )}
              {query.trim().length > 0 && onSaveSearch && (
                <button
                  type="button"
                  className="btn-tactile"
                  onClick={() => onSaveSearch(query.trim(), query.trim(), modalityFilter, activeTag)}
                >
                  Save search
                </button>
              )}
              <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-3)", whiteSpace: "nowrap" }}>
                {latencyMs > 0 ? `${filteredResults.length} · ${latencyMs} ms` : `${filteredResults.length}`}
              </span>
            </div>

            {/* Active Similar / Filter Pills */}
            {similarSource && (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px", padding: "12px 16px", borderRadius: "12px", background: "var(--bg-subtle)", border: "1px solid var(--rule)" }}>
                <span style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  Showing files similar to <strong style={{ color: "var(--ink)", fontWeight: 500 }}>{similarSource.title}</strong>
                </span>
                <button type="button" className="shelf-header-subtle-btn" onClick={onClearSimilar}>
                  Clear
                </button>
              </div>
            )}

            {/* Collapsible Filter Controls */}
            {filtersOpen && (
              <div
                style={{
                  padding: "16px 18px",
                  borderRadius: "14px",
                  border: "1px solid var(--rule)",
                  background: "var(--bg-surface)",
                  display: "flex",
                  flexDirection: "column",
                  gap: "12px",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "10px" }}>
                  <div className="seg-group" role="group" aria-label="File type filter">
                    {(["all", "document", "image", "text"] as const).map((mod) => (
                      <button
                        key={mod}
                        type="button"
                        className={`seg-btn${modalityFilter === mod ? " active" : ""}`}
                        onClick={() => onSetModalityFilter(mod)}
                      >
                        {mod === "all" ? "All" : mod === "document" ? "Documents" : mod === "image" ? "Images" : "Text"}
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
                      <button type="button" className="shelf-header-subtle-btn" onClick={resetAllFindFilters}>
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
                        className={`shelf-header-subtle-btn${isUsed ? " active" : ""}`}
                        onClick={() => (isUsed ? removeOperatorToken(op) : appendOperator(op))}
                      >
                        {op}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Saved Searches & Suggestions when query is empty */}
            {!query.trim() && (savedSearches.length > 0 || suggestions.length > 0) && (
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px" }}>
                {savedSearches.map((ss) => (
                  <div
                    key={ss.id}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "6px",
                      padding: "4px 10px",
                      borderRadius: "999px",
                      border: "1px solid var(--rule)",
                      background: "var(--bg-surface)",
                      fontSize: "12.5px",
                    }}
                  >
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
                        style={{ color: "var(--ink-3)", fontSize: "12px" }}
                        onClick={() => onDeleteSavedSearch(ss.id)}
                      >
                        ×
                      </button>
                    )}
                  </div>
                ))}
                {suggestions.slice(0, 4).map((sug) => (
                  <button
                    key={sug}
                    type="button"
                    className="btn-tactile"
                    style={{ borderRadius: "999px", fontSize: "12.5px", color: "var(--ink-2)" }}
                    onClick={() => onTriggerPrompt ? onTriggerPrompt(sug) : onQueryChange(sug)}
                  >
                    {sug}
                  </button>
                ))}
              </div>
            )}

            {/* Results */}
            <div className="shelf-section">
              <div className="shelf-section-header">
                <span style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  {filteredResults.length} {filteredResults.length === 1 ? "match" : "matches"}
                </span>
                {selectedFileId && (
                  <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                    Space to preview · Enter to inspect
                  </span>
                )}
              </div>

              {filteredResults.length === 0 ? (
                <div style={{ padding: "52px 24px", textAlign: "center", color: "var(--ink-3)" }}>
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
                          </div>
                        </div>
                        <div className="row-action-cue" onClick={(e) => e.stopPropagation()}>
                          {onContextMenuFile && (
                            <button
                              type="button"
                              className="shelf-header-subtle-btn"
                              aria-label={`Actions for ${f.title}`}
                              onClick={(e) => onContextMenuFile(e, f)}
                            >
                              ⋯
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
            {/* Messages Thread */}
            <div
              ref={chatScrollRef}
              style={{
                flex: 1,
                minHeight: 0,
                minWidth: 0,
                padding: "8px 4px 20px",
                display: "flex",
                flexDirection: "column",
                gap: "20px",
                overflowY: "auto",
                overflowX: "hidden",
              }}
            >
              {chatHistory.length === 0 ? (
                <div style={{ padding: "24px 8px", display: "flex", flexDirection: "column", gap: "18px" }}>
                  <div style={{ maxWidth: "560px" }}>
                    <h2 className="font-display" style={{ fontSize: "22px", fontWeight: 500, letterSpacing: "-0.02em", color: "var(--ink)" }}>
                      {displayName ? `Hi ${displayName}, ask anything about your files.` : "Ask anything about your files."}
                    </h2>
                    <p style={{ fontSize: "14px", color: "var(--ink-2)", marginTop: "8px", lineHeight: 1.6 }}>
                      Search by meaning, ask about storage or topics, or tell me to move, copy, tag, pin, shelve, rename, or delete files &amp; folders. I&apos;ll always show you a confirmation list first.
                    </p>
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                    {ASK_STARTER_PROMPTS.map((prompt) => (
                      <button
                        key={prompt}
                        type="button"
                        className="btn-tactile"
                        style={{ borderRadius: "999px" }}
                        onClick={() => submitQuestion(prompt)}
                      >
                        {prompt}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                chatHistory.map((msg) => (
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
                          fontSize: "11px",
                          fontWeight: 500,
                          opacity: 0.65,
                          marginBottom: "4px",
                        }}
                      >
                        {msg.role === "user" ? (displayName || "You") : "good shelves"}
                        {msg.latencyMs ? ` · ${msg.latencyMs} ms` : ""}
                      </div>
                      <div>{msg.role === "assistant" ? renderAiMarkdown(msg.text) : msg.text}</div>
                      {msg.role === "assistant" && msg.appliedFilters && msg.appliedFilters.length > 0 && (
                        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" }}>
                          {msg.appliedFilters.map((flt) => (
                            <span
                              key={flt}
                              style={{
                                fontSize: "11px",
                                padding: "2px 8px",
                                borderRadius: "999px",
                                border: "1px solid var(--rule)",
                                background: "var(--bg-surface)",
                                color: "var(--ink-2)",
                              }}
                            >
                              {flt}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>

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
                      const visibleCitations = isExpanded ? msg.citations : msg.citations.slice(0, 6);
                      return (
                        <div className="ai-citations-col">
                          {visibleCitations.map(({ file: f, snippet }) => (
                            <div
                              key={`${msg.id}-${f.id}`}
                              onClick={() => onSelectFile(f.id)}
                              onContextMenu={(e) => onContextMenuFile?.(e, f)}
                              className={`ai-citation-card ${f.id === selectedFileId ? "selected" : ""}`}
                            >
                              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px", flexWrap: "wrap", minWidth: 0 }}>
                                <div style={{ display: "flex", alignItems: "center", gap: "10px", minWidth: 0, flex: "1 1 200px" }}>
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
                                    <button
                                      type="button"
                                      className="font-display ai-wrap-text"
                                      style={{ fontSize: "14.5px", fontWeight: 500, color: "var(--ink)", textAlign: "left", maxWidth: "100%" }}
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        onSelectFile(f.id);
                                        onOpenQuickLook();
                                      }}
                                    >
                                      {f.title}
                                    </button>
                                    <div className="ai-wrap-text" style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "2px" }}>
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
                                  “{cleanPreviewText(snippet)}”
                                </div>
                              )}
                            </div>
                          ))}
                          {msg.citations.length > 6 && (
                            <button
                              type="button"
                              className="btn-tactile"
                              style={{ alignSelf: "flex-start" }}
                              onClick={() =>
                                setExpandedChatIds((prev) => ({ ...prev, [msg.id]: !prev[msg.id] }))
                              }
                            >
                              {isExpanded ? "Show fewer matches" : `Show all ${msg.citations.length} matches`}
                            </button>
                          )}
                        </div>
                      );
                    })()}

                    {msg.role === "assistant" && msg.choices && msg.choices.length > 0 && (
                      <div className="ai-choice-chips" role="group" aria-label="Suggested next actions">
                        {msg.choices.map((ch, cIdx) => (
                          <button
                            key={`${msg.id}-choice-${cIdx}`}
                            type="button"
                            className={`ai-choice-chip ${ch.kind === "action" || ch.kind === "clarify" ? "emphasized" : ""}`}
                            disabled={asking}
                            onClick={() => submitQuestion(ch.prompt)}
                          >
                            {ch.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                ))
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
                    alignItems: "center",
                    gap: "8px",
                    alignSelf: "flex-start",
                  }}
                >
                  <span className="status-dot pulse" aria-hidden="true" />
                  <span>Thinking…</span>
                </div>
              )}
            </div>

            {/* @ File Mention Quick-Picker */}
            {atMentionMatches.length > 0 && (
              <div className="ai-at-picker" role="listbox" aria-label="Insert file reference">
                <span style={{ fontSize: "11px", color: "var(--ink-3)", marginRight: "6px" }}>Reference file:</span>
                {atMentionMatches.map((mf) => (
                  <button
                    key={`at-${mf.id}`}
                    type="button"
                    className="ai-choice-chip"
                    onClick={() => applyAtMention(mf)}
                  >
                    @{mf.name}
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
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Tab" && atMentionMatches.length > 0) {
                    e.preventDefault();
                    applyAtMention(atMentionMatches[0]);
                  }
                }}
                placeholder={
                  displayName
                    ? `Ask anything, @file, or chain actions, ${displayName}…`
                    : 'Ask anything, type @file, or say "move receipts to Finance and pin them"…'
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
