import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import {
  ActionHistoryItem,
  AppSettings,
  AskResponse,
  ConnectedDevice,
  LookalikePair,
  PipelineStageEvent,
  ProposedAiAction,
  RenameHistoryItem,
  SavedSearch,
  ShelfFile,
  ShelfGroup,
  SmartCleanPreview,
  StorageStats,
  WatchedFolder,
  addWatchedFolderPath,
  applyAllSuggestedRenames,
  askYourShelves,
  clearActivityLog,
  completeFirstRunSetup,
  copyFileToFolder,
  createCustomShelf,
  createFolder,
  deleteCustomShelf,
  deleteFilePermanently,
  deleteSearchPreset,
  executeAiAction,
  executeSmartClean,
  fetchSmartCleanPreview,
  fetchWorkspaceState,
  findSimilarFiles,
  importConnectedDevice,
  ingestFilePath,
  isTauriRuntime,
  moveFileToFolder,
  openFileExternal,
  openFileInOS,
  pasteFilesToFolder,
  pickFolderNative,
  recordUserFeedback,
  redoHistoryAction,
  reloadWatcherNative,
  removeWatchedFolderPath,
  renameFileCustom,
  renameShelfLabel,
  rescanAllWatchedFolders,
  resetSetupWizard,
  restartSidecarNative,
  revealFileInOS,
  rollbackTidyFoldersOnDisk,
  runBulkFileAction,
  runSemanticSearch,
  saveSearchPreset,
  scanConnectedDevice,
  setFileShelf,
  subscribeToPipelineEvents,
  swapLookalikeKeep,
  toggleFileRename,
  togglePinFile,
  togglePinShelf,
  toggleResolveLookalikePair,
  toggleWatchedFolderPath,
  trashFileUsingSetting,
  undoHistoryAction,
  updateAppSetting,
  uploadRealDroppedFile,
  verifyLocalIndexIntegrity,
} from "./lib/tauri-api";
import { formatShortcut, isWindowsOS, joinDisplayPath } from "./lib/platform";
import { LeftRail, ViewId } from "./components/LeftRail";
import { TopMemoryBar } from "./components/TopMemoryBar";
import { ShelvesView } from "./components/ShelvesView";
import { FoldersView } from "./components/FoldersView";
import { FindView } from "./components/FindView";
import { TidyNamesView } from "./components/TidyNamesView";
import { LookalikesView } from "./components/LookalikesView";
import { TrashArchiveView } from "./components/TrashArchiveView";
import { ActivityView } from "./components/ActivityView";
import { InspectorSheet } from "./components/InspectorSheet";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { OnboardingModal, OnboardingOptions } from "./components/OnboardingModal";
import { SmartCleanModal } from "./components/SmartCleanModal";
import { CommandPalette } from "./components/CommandPalette";
import { FileActionDialogs, FileDialogState } from "./components/FileActionDialogs";
import { ContextMenuState, FileContextMenu } from "./components/FileContextMenu";

const EMPTY_FILES: ShelfFile[] = [];

const DEFAULT_SETTINGS: AppSettings = {
  realtime_watch: true,
  ocr_enabled: true,
  code_indexing: true,
  battery_pause: false,
  max_results: 50,
  min_similarity: 0.22,
  silhouette_score: 0,
};

const DEFAULT_STORAGE: StorageStats = {
  dbPath: isWindowsOS() ? "%APPDATA%\\good-shelves\\index.db" : "~/.local/share/good-shelves/index.db",
  dbPathFull: "",
  dbSizeMB: 0,
  indexedCount: 0,
};

export const App: React.FC = () => {
  const [activeView, setActiveView] = useState<ViewId>("shelves");
  const [shelfSubmode, setShelfSubmode] = useState<"list" | "all" | "grid" | "map">("list");
  const [activeShelfFilter, setActiveShelfFilter] = useState<string>("all");
  const [activeSubShelfFilter, setActiveSubShelfFilter] = useState<string>("all");
  const [hiddenShelfIds, setHiddenShelfIds] = useState<string[]>(() => {
    try {
      const parsed = JSON.parse(window.localStorage.getItem("good-shelves:hidden-shelves") || "[]");
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });
  const [selectedFileId, setSelectedFileId] = useState<string>("");
  const [inspectorVisible, setInspectorVisible] = useState<boolean>(false);
  const [sidebarVisible, setSidebarVisible] = useState<boolean>(() => {
    try {
      const saved = window.localStorage.getItem("good-shelves:sidebar-visible");
      return saved === null ? true : saved === "true";
    } catch {
      return true;
    }
  });
  const [mobileRailOpen, setMobileRailOpen] = useState<boolean>(false);
  const [pausedOnBattery, setPausedOnBattery] = useState<boolean>(false);
  const [trashCount, setTrashCount] = useState<number>(0);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [clipboard, setClipboard] = useState<{ fileIds: string[]; mode: "copy" | "cut" } | null>(null);
  const [similarSource, setSimilarSource] = useState<ShelfFile | null>(null);
  const [askResult, setAskResult] = useState<AskResponse | null>(null);
  const [asking, setAsking] = useState<boolean>(false);
  const [isNightMode, setIsNightMode] = useState<boolean>(() => {
    try {
      const saved = window.localStorage.getItem("good-shelves:theme") === "dark";
      if (saved) document.documentElement.classList.add("dark");
      return saved;
    } catch {
      return false;
    }
  });
  const [engineStatus, setEngineStatus] = useState<"connecting" | "ready" | "error">("connecting");
  const [retryingEngine, setRetryingEngine] = useState<boolean>(false);
  const [splashProgress, setSplashProgress] = useState<number>(18);
  const [splashHiding, setSplashHiding] = useState<boolean>(false);
  const [splashVisible, setSplashVisible] = useState<boolean>(true);

  // Real Backend State
  const [files, setFiles] = useState<ShelfFile[]>([]);
  const [shelves, setShelves] = useState<ShelfGroup[]>([]);
  const [lookalikes, setLookalikes] = useState<LookalikePair[]>([]);
  const [watchedFolders, setWatchedFolders] = useState<WatchedFolder[]>([]);
  const [connectedDevices, setConnectedDevices] = useState<ConnectedDevice[]>([]);
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS);
  const [silhouetteScore, setSilhouetteScore] = useState<number>(0);
  const [renameHistory, setRenameHistory] = useState<RenameHistoryItem[]>([]);
  const [actionHistory, setActionHistory] = useState<ActionHistoryItem[]>([]);
  const [querySuggestions, setQuerySuggestions] = useState<string[]>([]);
  const [storageStats, setStorageStats] = useState<StorageStats>(DEFAULT_STORAGE);

  // Semantic Search State
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [searchModality, setSearchModality] = useState<string>("all");
  const [searchResults, setSearchResults] = useState<ShelfFile[]>([]);
  const [searchLatencyMs, setSearchLatencyMs] = useState<number>(0);
  const [findMode, setFindMode] = useState<"search" | "ask">("search");

  // Live Pipeline Telemetry State
  const [activePipeStage, setActivePipeStage] = useState<number>(1);
  const [pipeStatusText, setPipeStatusText] = useState<string>(
    "ready. Drop a file or save into a watched folder."
  );
  const [pipeTimerBadge, setPipeTimerBadge] = useState<string>("local cpu · idle");
  const [stageBadges, setStageBadges] = useState<[string, string, string, string]>([
    "ready",
    "waiting",
    "waiting",
    "waiting",
  ]);
  const [recentEvents, setRecentEvents] = useState<PipelineStageEvent[]>([]);
  const [isProcessingFile, setIsProcessingFile] = useState<boolean>(false);

  // Modals & Toasts
  const [quickLookOpen, setQuickLookOpen] = useState<boolean>(false);
  const [smartCleanOpen, setSmartCleanOpen] = useState<boolean>(false);
  const [smartCleanPreview, setSmartCleanPreview] = useState<SmartCleanPreview | null>(null);
  const [smartCleanRunning, setSmartCleanRunning] = useState<boolean>(false);
  const [commandPaletteOpen, setCommandPaletteOpen] = useState<boolean>(false);
  const [fileDialog, setFileDialog] = useState<FileDialogState>({ type: "closed" });
  const [savedSearches, setSavedSearches] = useState<SavedSearch[]>([]);
  const [bulkClearSignal, setBulkClearSignal] = useState<number>(0);
  const [liveProgress, setLiveProgress] = useState<{
    operation: string;
    current: number;
    total: number;
    file_name: string;
  } | null>(null);
  const [dragOver, setDragOver] = useState<boolean>(false);
  const [toastMessage, setToastMessage] = useState<string>("");
  const [toastVisible, setToastVisible] = useState<boolean>(false);
  const [toastUndoFn, setToastUndoFn] = useState<(() => void) | null>(null);
  const toastTimerRef = useRef<number | null>(null);
  const searchDebounceRef = useRef<number | null>(null);
  const searchSeqRef = useRef<number>(0);

  useEffect(() => {
    return () => {
      if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
      if (searchDebounceRef.current) window.clearTimeout(searchDebounceRef.current);
    };
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem("good-shelves:sidebar-visible", String(sidebarVisible));
    } catch {
      // ignore
    }
  }, [sidebarVisible]);

  useEffect(() => {
    let wasWide = typeof window !== "undefined" ? window.innerWidth > 1180 : true;
    const onResize = () => {
      const isWide = window.innerWidth > 1180;
      if (wasWide && !isWide) {
        setInspectorVisible(false);
      }
      wasWide = isWide;
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const showToast = useCallback((msg: string, undoFn?: () => void) => {
    setToastMessage(msg);
    setToastUndoFn(() => (undoFn ? undoFn : null));
    setToastVisible(true);
    if (toastTimerRef.current) {
      window.clearTimeout(toastTimerRef.current);
    }
    toastTimerRef.current = window.setTimeout(() => {
      setToastVisible(false);
    }, 3400);
  }, []);

  const applyWorkspaceState = useCallback(
    (state: {
      files: ShelfFile[];
      shelves: ShelfGroup[];
      lookalikes: LookalikePair[];
      watchedFolders: WatchedFolder[];
      connectedDevices?: ConnectedDevice[];
      settings: AppSettings;
      pausedOnBattery?: boolean;
      trashCount?: number;
      silhouetteScore: number;
      renameHistory: RenameHistoryItem[];
      actionHistory?: ActionHistoryItem[];
      savedSearches?: SavedSearch[];
      querySuggestions?: string[];
      recentEvents?: PipelineStageEvent[];
      storageStats: StorageStats;
    }) => {
      setFiles(state.files);
      setShelves(state.shelves);
      setLookalikes(state.lookalikes);
      setWatchedFolders(state.watchedFolders);
      if (state.connectedDevices) {
        setConnectedDevices(state.connectedDevices);
      }
      setSettings(state.settings);
      setPausedOnBattery(Boolean(state.pausedOnBattery));
      setTrashCount(Number(state.trashCount ?? 0));
      setSilhouetteScore(state.silhouetteScore);
      setRenameHistory(state.renameHistory || []);
      if (state.actionHistory) {
        setActionHistory(state.actionHistory);
      }
      if (state.savedSearches) {
        setSavedSearches(state.savedSearches);
      }
      if (state.querySuggestions) {
        setQuerySuggestions(state.querySuggestions);
      }
      setStorageStats(state.storageStats);
      if (state.recentEvents && state.recentEvents.length > 0) {
        setRecentEvents(state.recentEvents);
      }
      setSearchResults((prev) => {
        if (!prev.length) return state.files;
        const byId = new Map(state.files.map((f) => [f.id, f]));
        const refreshed = prev.map((r) => byId.get(r.id)).filter((r): r is ShelfFile => Boolean(r));
        return refreshed;
      });
      setSelectedFileId((prev) => {
        if (prev && state.files.some((f) => f.id === prev)) return prev;
        return state.files[0]?.id || "";
      });
    },
    []
  );

  const loadInitial = useCallback(async (isCancelled?: () => boolean) => {
    setEngineStatus("connecting");
    for (let attempt = 0; attempt < 135; attempt++) {
      if (isCancelled?.()) return;
      try {
        const st = await fetchWorkspaceState();
        if (isCancelled?.()) return;
        applyWorkspaceState(st);
        setSearchResults(st.files);
        setEngineStatus("ready");
        return;
      } catch {
        // Wait for sidecar startup
      }
      const delayMs = attempt === 0 ? 60 : attempt < 5 ? 100 : 220;
      await new Promise((r) => setTimeout(r, delayMs));
    }
    if (!isCancelled?.()) {
      setEngineStatus("error");
    }
  }, [applyWorkspaceState]);

  const handleRetryConnection = useCallback(async () => {
    if (retryingEngine) return;
    setRetryingEngine(true);
    try {
      await restartSidecarNative();
      await loadInitial();
    } finally {
      setRetryingEngine(false);
    }
  }, [loadInitial, retryingEngine]);

  // Initial load
  useEffect(() => {
    let mounted = true;
    loadInitial(() => !mounted);
    return () => {
      mounted = false;
    };
  }, [loadInitial]);

  // Splash progress animation during startup
  useEffect(() => {
    if (engineStatus === "connecting") {
      const iv = window.setInterval(() => {
        setSplashProgress((p) => (p >= 88 ? 88 : Math.min(88, p + 8)));
      }, 120);
      return () => window.clearInterval(iv);
    }
    setSplashProgress(100);
    const t1 = window.setTimeout(() => setSplashHiding(true), 20);
    const t2 = window.setTimeout(() => setSplashVisible(false), 140);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
    };
  }, [engineStatus]);

  // Subscribe to real-time indexing stage events, live progress, and device hot-plug
  useEffect(() => {
    const unsub = subscribeToPipelineEvents((evt) => {
      if (evt.type === "progress") {
        const cur = evt.current ?? 0;
        const tot = evt.total ?? 1;
        const op = evt.operation || "scanning";
        const isDone = op === "idle" || (cur >= tot && tot > 0 && !evt.file_name);
        setLiveProgress(
          isDone
            ? null
            : {
                operation: op,
                current: cur,
                total: tot,
                file_name: evt.file_name || "",
              }
        );
        return;
      }
      if (evt.type === "index_updated") {
        setLiveProgress(null);
        fetchWorkspaceState()
          .then((st) => applyWorkspaceState(st))
          .catch(() => {});
        return;
      }
      if (evt.type === "devices_changed" && evt.devices) {
        setConnectedDevices(evt.devices);
        showToast("Connected devices updated");
        return;
      }

      const stageNum = evt.stage ?? 0;
      const elapsed = evt.elapsed_ms ?? 0;
      setActivePipeStage(stageNum);
      setPipeStatusText(evt.message || "");
      setPipeTimerBadge(`${elapsed.toFixed(0)} ms`);
      setStageBadges((prev) => {
        const next: [string, string, string, string] = [...prev] as [
          string,
          string,
          string,
          string
        ];
        if (stageNum >= 1 && stageNum <= 4 && evt.status) {
          next[stageNum - 1] = evt.status;
        }
        return next;
      });
      setRecentEvents((prev) => [evt, ...prev.slice(0, 19)]);

      if (stageNum === 4) {
        setLiveProgress(null);
        fetchWorkspaceState()
          .then((st) => {
            applyWorkspaceState(st);
            if (evt.file_id) {
              setSelectedFileId(evt.file_id);
            }
          })
          .catch(() => {});
      }
    });
    return unsub;
  }, [applyWorkspaceState, showToast]);

  // Execute real semantic search only when Find view is active or user typed a query
  useEffect(() => {
    if (similarSource) return;
    if (activeView !== "find" && !searchQuery.trim()) return;
    if (searchDebounceRef.current) {
      window.clearTimeout(searchDebounceRef.current);
    }
    const seq = ++searchSeqRef.current;
    searchDebounceRef.current = window.setTimeout(async () => {
      try {
        const res = await runSemanticSearch(searchQuery, searchModality);
        if (seq !== searchSeqRef.current) return;
        setSearchResults(res.results);
        setSearchLatencyMs(res.latencyMs);
      } catch {
        if (seq !== searchSeqRef.current) return;
        const filtered = files.filter(
          (f) => searchModality === "all" || f.modality === searchModality
        );
        setSearchResults(filtered);
      }
    }, 65);
  }, [activeView, searchQuery, searchModality, files, similarSource]);

  const handleUndoAction = useCallback(async (actionId?: number) => {
    try {
      const res = await undoHistoryAction(actionId);
      applyWorkspaceState(res.state);
      showToast(res.summary);
    } catch {
      showToast("Nothing to undo");
    }
  }, [applyWorkspaceState, showToast]);

  const handleRedoAction = useCallback(async (actionId?: number) => {
    try {
      const res = await redoHistoryAction(actionId);
      applyWorkspaceState(res.state);
      showToast(res.summary);
    } catch {
      showToast("Nothing to redo");
    }
  }, [applyWorkspaceState, showToast]);

  const handleStepQuickLook = useCallback(
    (dir: -1 | 1) => {
      const list =
        activeView === "find"
          ? searchResults
          : activeView === "shelves" && activeShelfFilter !== "all"
          ? files.filter((f) => f.shelfId === activeShelfFilter)
          : files;
      if (list.length === 0) return;
      const idx = list.findIndex((f) => f.id === selectedFileId);
      const nextIdx =
        idx === -1
          ? 0
          : (idx + dir + list.length) % list.length;
      if (list[nextIdx]) setSelectedFileId(list[nextIdx].id);
    },
    [activeView, activeShelfFilter, files, searchResults, selectedFileId]
  );

  const handleSetClipboard = useCallback(
    (fileIds: string[], mode: "copy" | "cut") => {
      if (!fileIds.length) return;
      setClipboard({ fileIds, mode });
      if (fileIds.length === 1) {
        const f = files.find((x) => x.id === fileIds[0]);
        const name = f?.name || "1 file";
        showToast(`${mode === "cut" ? "Cut" : "Copied"} ${name}. Press Ctrl+V in Folders to paste.`);
      } else {
        showToast(`${mode === "cut" ? "Cut" : "Copied"} ${fileIds.length} files. Press Ctrl+V in Folders to paste.`);
      }
    },
    [files, showToast]
  );

  const handlePasteClipboardGlobal = useCallback(async () => {
    if (!clipboard || !clipboard.fileIds.length) return;
    const dest = await pickFolderNative();
    if (!dest) {
      if (!isTauriRuntime()) {
        showToast("Open Folders view and press Ctrl+V to paste into a folder.");
      }
      return;
    }
    try {
      const mode = clipboard.mode === "cut" ? "move" : "copy";
      const res = await pasteFilesToFolder(clipboard.fileIds, dest, mode);
      applyWorkspaceState(res.state);
      if (clipboard.mode === "cut") setClipboard(null);
      showToast(
        `${clipboard.mode === "cut" ? "Moved" : "Copied"} ${res.count} file(s)`,
        clipboard.mode === "cut" ? () => handleUndoAction() : undefined
      );
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Paste failed");
    }
  }, [clipboard, applyWorkspaceState, handleUndoAction, showToast]);

  // Keep volatile keyboard handler inputs in a ref so window listener binds once
  const keyStateRef = useRef({
    activeView,
    clipboard,
    commandPaletteOpen,
    fileDialogType: fileDialog.type,
    files,
    handlePasteClipboardGlobal,
    handleRedoAction,
    handleSetClipboard,
    handleStepQuickLook,
    handleUndoAction,
    inspectorVisible,
    quickLookOpen,
    searchResults,
    selectedFileId,
    setupCompleted: settings.setup_completed,
    smartCleanOpen,
    smartCleanRunning,
  });
  keyStateRef.current = {
    activeView,
    clipboard,
    commandPaletteOpen,
    fileDialogType: fileDialog.type,
    files,
    handlePasteClipboardGlobal,
    handleRedoAction,
    handleSetClipboard,
    handleStepQuickLook,
    handleUndoAction,
    inspectorVisible,
    quickLookOpen,
    searchResults,
    selectedFileId,
    setupCompleted: settings.setup_completed,
    smartCleanOpen,
    smartCleanRunning,
  };

  // Global keyboard shortcuts (Escape, Space for Quick Look, Up/Down arrows, F2/R/M/Del)
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const s = keyStateRef.current;
      const target = e.target as HTMLElement | null;
      const isInput = Boolean(
        target &&
          (target.tagName === "INPUT" ||
            target.tagName === "TEXTAREA" ||
            target.tagName === "SELECT" ||
            target.isContentEditable)
      );
      const isInteractive = Boolean(
        isInput ||
          (target &&
            (target.tagName === "BUTTON" ||
              target.tagName === "A" ||
              target.closest('[role="button"], [role="switch"], [role="tab"]')))
      );
      const anyModalOpen =
        s.quickLookOpen ||
        s.commandPaletteOpen ||
        s.smartCleanOpen ||
        s.fileDialogType !== "closed" ||
        s.setupCompleted === false;

      if (s.quickLookOpen && !isInput && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
        e.preventDefault();
        s.handleStepQuickLook(e.key === "ArrowRight" ? 1 : -1);
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        if (s.setupCompleted === false) return;
        e.preventDefault();
        setCommandPaletteOpen((open) => !open);
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key === "\\") {
        e.preventDefault();
        setInspectorVisible((v) => !v);
        return;
      }

      if (!isInput && !anyModalOpen && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) {
          s.handleRedoAction();
        } else {
          s.handleUndoAction();
        }
        return;
      }

      if (!isInput && !anyModalOpen && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
        e.preventDefault();
        s.handleRedoAction();
        return;
      }

      if (e.key === "Escape") {
        if (s.setupCompleted === false) {
          return;
        }
        if (anyModalOpen) {
          setQuickLookOpen(false);
          setCommandPaletteOpen(false);
          if (!s.smartCleanRunning) setSmartCleanOpen(false);
          setFileDialog({ type: "closed" });
        } else if (isInput && target) {
          target.blur();
        } else if (s.inspectorVisible) {
          setInspectorVisible(false);
        }
        return;
      }

      if (anyModalOpen) return;

      if (!isInput && (e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "c" || e.key.toLowerCase() === "x")) {
        const textSel = window.getSelection()?.toString();
        if (!textSel && s.selectedFileId) {
          e.preventDefault();
          s.handleSetClipboard([s.selectedFileId], e.key.toLowerCase() === "x" ? "cut" : "copy");
          return;
        }
      }

      if (!isInput && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v" && s.clipboard?.fileIds.length) {
        e.preventDefault();
        void s.handlePasteClipboardGlobal();
        return;
      }

      if (!isInteractive && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const lower = e.key.toLowerCase();
        if (lower === "i") {
          e.preventDefault();
          setInspectorVisible((v) => !v);
          return;
        }
        const curFile = s.files.find((f) => f.id === s.selectedFileId);
        if (curFile && (lower === "r" || e.key === "F2")) {
          e.preventDefault();
          setFileDialog({ type: "rename", file: curFile });
          return;
        }
        if (curFile && lower === "m") {
          e.preventDefault();
          setFileDialog({ type: "move", file: curFile, initialMode: "move" });
          return;
        }
        if (curFile && e.key === "Delete") {
          e.preventDefault();
          setFileDialog({ type: "delete", file: curFile });
          return;
        }
      }

      if (!isInteractive && !e.ctrlKey && !e.metaKey && !e.altKey && e.key === " ") {
        e.preventDefault();
        setQuickLookOpen((open) => !open);
        return;
      }

      if (!isInput && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        const list = s.activeView === "find" ? s.searchResults : s.files;
        if (list.length === 0) return;
        e.preventDefault();
        const idx = list.findIndex((f) => f.id === s.selectedFileId);
        const nextIdx =
          e.key === "ArrowDown"
            ? Math.min(list.length - 1, idx + 1)
            : Math.max(0, idx <= 0 ? 0 : idx - 1);
        const nextItem = list[nextIdx];
        if (nextItem) setSelectedFileId(nextItem.id);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const handleGlobalSearch = (val: string) => {
    setSimilarSource(null);
    setSearchQuery(val);
    if (val.trim().length > 0) {
      setFindMode("search");
      if (activeView !== "find") {
        setActiveView("find");
      }
    }
  };

  const handleClearSearch = () => {
    setSimilarSource(null);
    setSearchQuery("");
  };

  const handleFindSimilar = async (file: ShelfFile) => {
    try {
      const res = await findSimilarFiles(file.id, 24);
      setSimilarSource(res.sourceFile);
      setSearchResults(res.results);
      setSearchLatencyMs(res.latencyMs);
      setActiveView("find");
      showToast(`Found ${res.results.length} files similar to ${file.name}`);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Could not find similar files");
    }
  };

  const handleAskShelves = async (
    question: string,
    context?: {
      recentFileIds?: string[];
      history?: Array<{ role: string; text: string }>;
      pendingAction?: ProposedAiAction | null;
    }
  ) => {
    if (!question.trim() || asking) return;
    setAsking(true);
    try {
      const res = await askYourShelves(question, 25, {
        selectedFileId: selectedFileId ?? undefined,
        recentFileIds: context?.recentFileIds,
        history: context?.history,
        pendingAction: context?.pendingAction,
      });
      setAskResult(res);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Could not answer question");
    } finally {
      setAsking(false);
    }
  };

  const handleExecuteAiAction = async (
    action: ProposedAiAction,
    selectedIds: string[]
  ): Promise<{ ok: boolean; count: number; summary: string }> => {
    const res = await executeAiAction({
      actionType: action.actionType,
      fileIds: selectedIds,
      ...(action.payload || {}),
    });
    applyWorkspaceState(res.state);
    if (
      selectedFileId &&
      selectedIds.includes(selectedFileId) &&
      (action.actionType === "trash" || action.actionType === "delete_permanent")
    ) {
      setQuickLookOpen(false);
      setSelectedFileId("");
    }
    const canUndo = Boolean(action.reversible) && action.actionType !== "undo";
    showToast(res.summary, canUndo ? () => handleUndoAction() : undefined);
    return { ok: res.ok, count: res.count, summary: res.summary };
  };

  const handleTriggerPrompt = (q: string) => {
    setFindMode("search");
    setSearchQuery(q);
    if (activeView !== "find") setActiveView("find");
  };

  const handleJumpToShelf = (shelfId: string, subShelf?: string) => {
    setActiveShelfFilter(shelfId);
    setActiveSubShelfFilter(subShelf && subShelf.trim() ? subShelf.trim() : "all");
    if (activeView !== "shelves") setActiveView("shelves");
    setShelfSubmode("list");
  };

  const handleToggleHideShelf = useCallback((shelfId: string) => {
    setHiddenShelfIds((prev) => {
      const next = prev.includes(shelfId)
        ? prev.filter((id) => id !== shelfId)
        : [...prev, shelfId];
      try {
        window.localStorage.setItem("good-shelves:hidden-shelves", JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  const handleRestoreAllHiddenShelves = useCallback(() => {
    setHiddenShelfIds([]);
    try {
      window.localStorage.setItem("good-shelves:hidden-shelves", "[]");
    } catch {
      // ignore
    }
  }, []);

  const handleSelectFile = (fileId: string) => {
    setSelectedFileId(fileId);
    if (!inspectorVisible) {
      setInspectorVisible(true);
    }
    if (activeView === "find" && searchQuery.trim().length >= 2) {
      recordUserFeedback({ query: searchQuery, fileId, action: "select" }).catch(() => {});
    }
  };

  const handleToggleTheme = () => {
    const next = !isNightMode;
    const applyStateAndClass = (withFallbackCss: boolean) => {
      if (withFallbackCss) {
        document.documentElement.classList.add("theme-transitioning");
      }
      setIsNightMode(next);
      document.documentElement.classList.toggle("dark", next);
      try {
        window.localStorage.setItem("good-shelves:theme", next ? "dark" : "light");
      } catch {
        // ignore storage failure
      }
      if (withFallbackCss) {
        window.setTimeout(() => {
          document.documentElement.classList.remove("theme-transitioning");
        }, 180);
      }
    };

    const docWithTransition = document as Document & {
      startViewTransition?: (cb: () => void) => { finished?: Promise<void> };
    };
    const prefersReduced =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (!prefersReduced && typeof docWithTransition.startViewTransition === "function") {
      docWithTransition.startViewTransition(() => {
        applyStateAndClass(false);
      });
    } else {
      applyStateAndClass(true);
    }
  };

  const handleToggleRename = async (fileId: string) => {
    try {
      const res = await toggleFileRename(fileId);
      applyWorkspaceState(res.state);
      showToast(
        res.action === "applied"
          ? `Renamed to ${res.file.name}`
          : `Restored ${res.file.originalName}`,
        () => {
          handleToggleRename(fileId);
        }
      );
    } catch (err) {
      showToast(`Rename error: ${err instanceof Error ? err.message : "failed"}`);
    }
  };

  const handleApplyAllRenames = async () => {
    try {
      const res = await applyAllSuggestedRenames();
      applyWorkspaceState(res.state);
      showToast(
        res.appliedCount > 0
          ? `Applied ${res.appliedCount} clean filenames on disk`
          : "All suggested filenames already applied"
      );
    } catch {
      showToast("Could not apply batch renames");
    }
  };

  const handleResolveLookalike = async (pairId: string) => {
    try {
      const res = await toggleResolveLookalikePair(pairId);
      applyWorkspaceState(res.state);
      showToast(
        res.resolved
          ? `Archived redundant copy (${res.savedMB ?? ""} MB reclaimed)`
          : "Restored archived file from GoodShelves_Archive",
        () => {
          handleResolveLookalike(pairId);
        }
      );
    } catch (err) {
      showToast(`Archive action failed: ${err instanceof Error ? err.message : ""}`);
    }
  };

  const handleAssignShelf = useCallback(
    async (fileId: string, shelfId: string) => {
      const prevShelf = files.find((f) => f.id === fileId)?.shelfId;
      try {
        const res = await setFileShelf(fileId, shelfId);
        applyWorkspaceState(res.state);
        showToast(`Moved file to ${res.shelfName}`, async () => {
          try {
            const restoreTarget = prevShelf && prevShelf !== shelfId ? prevShelf : "auto";
            const undone = await setFileShelf(fileId, restoreTarget);
            applyWorkspaceState(undone.state);
            showToast(`Restored shelf to ${undone.shelfName}`);
          } catch {
            showToast("Could not restore shelf");
          }
        });
      } catch (err) {
        showToast(`Shelf update failed: ${err instanceof Error ? err.message : ""}`);
      }
    },
    [applyWorkspaceState, files, showToast]
  );

  const handleSwapLookalike = useCallback(
    async (pairId: string) => {
      try {
        const res = await swapLookalikeKeep(pairId);
        applyWorkspaceState(res.state);
        showToast("Swapped which copy to keep and which to archive");
      } catch (err) {
        showToast(`Could not swap copies: ${err instanceof Error ? err.message : ""}`);
      }
    },
    [applyWorkspaceState, showToast]
  );

  const handleRollbackOrganizeDisk = async () => {
    try {
      const res = await rollbackTidyFoldersOnDisk();
      applyWorkspaceState(res.state);
      setSmartCleanOpen(false);
      showToast(`Restored ${res.restoredCount} files to original folders`);
    } catch {
      showToast("No recent folder organization moves to undo");
    }
  };

  const animateAndRunIngestion = async (
    runner: () => Promise<{ ok: boolean; file: ShelfFile; duplicateOf?: any; state: any }>
  ) => {
    setIsProcessingFile(true);
    setStageBadges(["running…", "waiting", "waiting", "waiting"]);
    try {
      const res = await runner();
      applyWorkspaceState(res.state);
      if (res.file) {
        setSelectedFileId(res.file.id);
        setActivePipeStage(4);
        setStageBadges(["detected", "read", "analyzed", "shelved"]);
        setPipeStatusText(
          `04 · Placed "${res.file.title}" onto ${res.file.shelfName}`
        );
        if (res.duplicateOf) {
          showToast(
            `Indexed "${res.file.name}" (${res.duplicateOf.similarityPct}% match to existing ${res.duplicateOf.name} in ${res.duplicateOf.folder})`
          );
        } else {
          showToast(`Indexed "${res.file.title}"`);
        }
      }
    } catch (err) {
      showToast(`Ingestion error: ${err instanceof Error ? err.message : "failed"}`);
    } finally {
      setIsProcessingFile(false);
    }
  };

  const handleUploadRealFile = (file: File) => {
    animateAndRunIngestion(() => uploadRealDroppedFile(file));
  };

  const handleToggleWatchFolder = async (path: string) => {
    try {
      const res = await toggleWatchedFolderPath(path);
      applyWorkspaceState(res.state);
      showToast(`Updated folder watch status for ${path}`);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to update watched folder");
    }
  };

  const handleAddWatchFolder = async (path: string) => {
    const folderLabel = path.split(/[\\/]/).filter(Boolean).pop() || path;
    setLiveProgress({
      operation: `watching ${folderLabel}`,
      current: 0,
      total: 1,
      file_name: "discovering supported files…",
    });
    try {
      const res = await addWatchedFolderPath(path);
      applyWorkspaceState(res.state);
      await reloadWatcherNative().catch(() => {});
      showToast(`Watching ${folderLabel} (${res.state.files.length} files indexed)`);
    } catch (err) {
      showToast(err instanceof Error ? err.message : `Could not watch folder: ${path}`);
    } finally {
      setLiveProgress(null);
    }
  };

  const handleRemoveWatchFolder = async (path: string) => {
    try {
      const res = await removeWatchedFolderPath(path);
      applyWorkspaceState(res.state);
      await reloadWatcherNative().catch(() => {});
      showToast(`Removed watched folder ${path}`);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to remove watched folder");
    }
  };

  const handleUpdateSetting = async (key: string, value: unknown) => {
    try {
      const res = await updateAppSetting(key, value);
      applyWorkspaceState(res.state);
      showToast(`Updated preference: ${key}`);
    } catch {
      showToast("Failed to save preference");
    }
  };

  const handleRescanFolders = async (allFiles: boolean = false) => {
    setLiveProgress({
      operation: allFiles ? "full rescan" : "refreshing folders",
      current: 0,
      total: 1,
      file_name: "checking watched folders…",
    });
    showToast(allFiles ? "Indexing all files across watched folders…" : "Refreshing watched folders in background…");
    try {
      const res = await rescanAllWatchedFolders(allFiles);
      applyWorkspaceState(res.state);
      showToast(
        `Rescanned watched folders · ${res.state.files.length} active files indexed`
      );
    } catch {
      showToast("Rescan failed");
    } finally {
      setLiveProgress(null);
    }
  };

  const handleVerifyIndex = async () => {
    try {
      const res = await verifyLocalIndexIntegrity();
      applyWorkspaceState(res.state);
      showToast(
        `Local file index verified · ${res.integrity.vectorRows} files synced (${res.integrity.orphansRemoved} old entries cleaned)`
      );
    } catch {
      showToast("Index verification failed");
    }
  };

  const handleCopyPath = (f: ShelfFile) => {
    const full = joinDisplayPath(f.folder, f.name);
    if (navigator.clipboard?.writeText) {
      navigator.clipboard
        .writeText(f.path)
        .then(() => showToast(`Copied path: ${full}`))
        .catch(() => showToast("Clipboard unavailable"));
    } else {
      showToast("Clipboard unavailable");
    }
  };

  const handleScanDevice = async (path: string, addWatch: boolean) => {
    showToast(`Scanning device ${path}…`);
    try {
      const res = await scanConnectedDevice(path, addWatch);
      applyWorkspaceState(res.state);
      showToast(`Indexed ${res.scannedCount} files from device`);
    } catch (err) {
      showToast(`Device scan failed: ${err instanceof Error ? err.message : "error"}`);
    }
  };

  const handleCompleteOnboarding = async (
    folders: string[],
    options?: OnboardingOptions
  ) => {
    try {
      const res = await completeFirstRunSetup(folders, options);
      applyWorkspaceState(res.state);
      await reloadWatcherNative();
      showToast(
        options?.userName
          ? `Welcome, ${options.userName}! Indexed ${res.scannedCount} files`
          : `Indexed ${res.scannedCount} files from selected folders`
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Could not complete initial setup";
      showToast(msg);
      throw err instanceof Error ? err : new Error(msg);
    }
  };

  const handleClearActivity = async () => {
    try {
      const res = await clearActivityLog();
      setRecentEvents([]);
      applyWorkspaceState(res.state);
      showToast("Cleared recent activity log");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Could not clear activity");
    }
  };

  const handleResetSetup = async () => {
    try {
      const res = await resetSetupWizard();
      setRecentEvents([]);
      applyWorkspaceState(res.state);
      showToast("Setup wizard reset");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Could not reset setup");
    }
  };

  const handleImportDevice = async (devicePath: string) => {
    showToast("Safely importing files from connected device…");
    try {
      const res = await importConnectedDevice(devicePath);
      applyWorkspaceState(res.state);
      await reloadWatcherNative();
      showToast(`Imported ${res.importedCount} file(s) to ${res.destination}`, () => {
        handleUndoAction();
      });
    } catch (err) {
      showToast(`Device import failed: ${err instanceof Error ? err.message : "error"}`);
    }
  };

  const handleCreateFolder = (defaultParentPath?: string) => {
    setFileDialog({ type: "new-folder", defaultParentPath });
  };

  const handleConfirmCreateFolder = async (
    parentPath: string,
    folderName: string,
    watch: boolean
  ) => {
    const res = await createFolder(parentPath, folderName);
    if (watch) {
      await addWatchedFolderPath(res.path);
      await reloadWatcherNative();
    }
    const st = await fetchWorkspaceState();
    applyWorkspaceState(st);
    showToast(`Created folder: ${res.name}`);
  };

  const handleConfirmRenameFile = async (file: ShelfFile, newName: string) => {
    const res = await renameFileCustom(file.id, newName);
    applyWorkspaceState(res.state);
    showToast(`Renamed to ${res.file?.name || newName}`, () => {
      handleUndoAction();
    });
  };

  const handleConfirmMoveOrCopyFile = async (
    file: ShelfFile,
    targetFolder: string,
    mode: "move" | "copy"
  ) => {
    if (mode === "move") {
      const res = await moveFileToFolder(file.id, targetFolder);
      applyWorkspaceState(res.state);
      showToast(`Moved ${file.name}`, () => {
        handleUndoAction();
      });
    } else {
      const res = await copyFileToFolder(file.id, targetFolder);
      applyWorkspaceState(res.state);
      showToast(`Copied ${file.name}`);
    }
  };

  const handleConfirmTrashFile = async (file: ShelfFile) => {
    const res = await trashFileUsingSetting(file, settings.trash_mode);
    if (res.state) applyWorkspaceState(res.state);
    setQuickLookOpen(false);
    showToast(
      settings.trash_mode === "system"
        ? `Moved ${file.name} to OS Trash`
        : `Moved ${file.name} to Trash`,
      settings.trash_mode === "system" ? undefined : () => handleUndoAction()
    );
  };

  const handleConfirmCreateSmartShelf = async (opts: {
    name: string;
    query: string;
    tag: string;
    fileIds: string[];
  }) => {
    const res = await createCustomShelf(opts);
    applyWorkspaceState(res.state);
    setBulkClearSignal((n) => n + 1);
    showToast(`Created custom shelf: ${opts.name}`);
  };

  const handleConfirmRenameShelf = async (shelfId: string, newName: string) => {
    const res = await renameShelfLabel(shelfId, newName);
    applyWorkspaceState(res.state);
    showToast(`Renamed shelf to ${res.name}`);
  };

  const handleDeleteCustomShelf = async (shelfId: string) => {
    try {
      const res = await deleteCustomShelf(shelfId);
      applyWorkspaceState(res.state);
      if (activeShelfFilter === shelfId) setActiveShelfFilter("all");
      showToast("Removed custom shelf");
    } catch {
      showToast("Could not remove custom shelf");
    }
  };

  const handleConfirmDeletePermanent = async (file: ShelfFile) => {
    const res = await deleteFilePermanently(file.id);
    applyWorkspaceState(res.state);
    setQuickLookOpen(false);
    showToast(`Permanently deleted ${file.name}`);
  };

  const handleTogglePinShelf = async (shelfId: string) => {
    try {
      const res = await togglePinShelf(shelfId);
      applyWorkspaceState(res.state);
      showToast(res.pinned ? "Pinned shelf" : "Unpinned shelf");
    } catch {
      showToast("Could not update shelf pin");
    }
  };

  const handleBulkAction = async (
    fileIds: string[],
    action: "move" | "trash" | "tag" | "pin" | "unpin"
  ): Promise<boolean> => {
    if (action === "tag") {
      setFileDialog({ type: "bulk-tag", fileIds });
      return false;
    }
    let targetFolder: string | undefined;
    if (action === "move") {
      targetFolder = (await pickFolderNative()) || undefined;
      if (!targetFolder) {
        if (!isTauriRuntime()) {
          showToast("Bulk move folder picker requires the desktop app");
        }
        return false;
      }
    }
    try {
      const res = await runBulkFileAction(fileIds, action, targetFolder);
      applyWorkspaceState(res.state);
      showToast(
        `Applied ${action} to ${res.count} file(s)`,
        action === "move" || action === "trash" ? () => handleUndoAction() : undefined
      );
      return true;
    } catch (err) {
      showToast(`Bulk ${action} failed: ${err instanceof Error ? err.message : "error"}`);
      return false;
    }
  };

  const handleConfirmBulkTag = async (fileIds: string[], tag: string) => {
    const res = await runBulkFileAction(fileIds, "tag", undefined, tag);
    applyWorkspaceState(res.state);
    setBulkClearSignal((n) => n + 1);
    showToast(`Added #${tag} to ${res.count} file(s)`);
  };

  const handleSaveSearch = async (name: string, query: string, modality: string, tag: string) => {
    try {
      const res = await saveSearchPreset(name, query, modality, tag);
      applyWorkspaceState(res.state);
      showToast(`Saved search: ${name}`);
    } catch {
      showToast("Could not save search");
    }
  };

  const handleDeleteSavedSearch = async (searchId: string) => {
    try {
      const res = await deleteSearchPreset(searchId);
      applyWorkspaceState(res.state);
    } catch {
      showToast("Could not delete saved search");
    }
  };

  const handleOpenSmartClean = async () => {
    try {
      const prev = await fetchSmartCleanPreview();
      setSmartCleanPreview(prev);
    } catch {
      setSmartCleanPreview(null);
    }
    setSmartCleanOpen(true);
  };

  const handleExecuteSmartClean = async (opts: {
    applyRenames: boolean;
    archiveDuplicates: boolean;
    organizeFolders: boolean;
  }) => {
    setSmartCleanRunning(true);
    try {
      const res = await executeSmartClean(opts);
      applyWorkspaceState(res.state);
      setSmartCleanOpen(false);
      showToast(res.summary, () => {
        handleUndoAction();
      });
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Smart Clean failed");
    } finally {
      setSmartCleanRunning(false);
    }
  };

  // Tauri v2 native OS drag-and-drop: index dropped paths in-place without copying to Downloads
  useEffect(() => {
    if (!isTauriRuntime()) return;
    let unlisten: (() => void) | null = null;
    let disposed = false;
    getCurrentWebview()
      .onDragDropEvent(async (event) => {
        if (disposed) return;
        const payload = event.payload;
        if (payload.type === "over" || payload.type === "enter") {
          setDragOver(true);
        } else if (payload.type === "leave") {
          setDragOver(false);
        } else if (payload.type === "drop") {
          setDragOver(false);
          const paths = payload.paths || [];
          for (const p of paths.slice(0, 10)) {
            try {
              const res = await ingestFilePath(p);
              applyWorkspaceState(res.state);
              showToast(`Indexed file: ${res.file?.name || p}`);
            } catch {
              showToast(`Could not index ${p}`);
            }
          }
        }
      })
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch(() => {});
    return () => {
      disposed = true;
      if (unlisten) unlisten();
    };
  }, [applyWorkspaceState, showToast]);

  const selectedFile = useMemo(
    () => files.find((f) => f.id === selectedFileId) || files[0],
    [files, selectedFileId]
  );

  const cleanupCount = useMemo(() => {
    const renames = files.filter((f) => Boolean(f.suggestedName) && !f.renamed).length;
    const dupes = lookalikes.filter((d) => !d.resolved).length;
    return renames + dupes;
  }, [files, lookalikes]);

  const handleCloseQuickLook = useCallback(() => setQuickLookOpen(false), []);
  const handleOpenQuickLook = useCallback(() => setQuickLookOpen(true), []);
  const handleHideInspector = useCallback(() => setInspectorVisible(false), []);
  const handleOpenRenameDialog = useCallback((f: ShelfFile) => setFileDialog({ type: "rename", file: f }), []);
  const handleOpenMoveDialog = useCallback(
    (f: ShelfFile, mode?: "move" | "copy") => setFileDialog({ type: "move", file: f, initialMode: mode || "move" }),
    []
  );
  const handleOpenDeleteDialog = useCallback((f: ShelfFile) => setFileDialog({ type: "delete", file: f }), []);

  return (
    <>
      {splashVisible && (
        <div
          className={`app-splash-screen ${splashHiding ? "hiding" : ""}`}
          id="appSplashScreen"
          aria-live="polite"
        >
          <div className="font-display" style={{ fontSize: "34px", fontWeight: 500, letterSpacing: "-0.03em" }}>
            good shelves
          </div>
          <div className="app-splash-caption">
            {engineStatus === "connecting"
              ? "Preparing your workspace…"
              : engineStatus === "error"
              ? "Opening offline workspace…"
              : "Ready"}
          </div>
          <div className="app-splash-bar-wrap" role="progressbar" aria-valuenow={splashProgress} aria-valuemin={0} aria-valuemax={100}>
            <div className="app-splash-bar-fill" style={{ width: `${splashProgress}%` }} />
          </div>
        </div>
      )}

      <div
        className={`desktop-workspace${!sidebarVisible ? " sidebar-collapsed" : ""}`}
        onDragOver={(e) => {
          if (isTauriRuntime()) return;
          e.preventDefault();
          if (!dragOver) setDragOver(true);
        }}
        onDragLeave={(e) => {
          if (isTauriRuntime()) return;
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
            setDragOver(false);
          }
        }}
        onDrop={async (e) => {
          if (isTauriRuntime()) return;
          e.preventDefault();
          setDragOver(false);
          const dropped = Array.from(e.dataTransfer?.files || []);
          if (!dropped.length) return;
          for (const f of dropped.slice(0, 10)) {
            try {
              const res = await uploadRealDroppedFile(f);
              applyWorkspaceState(res.state);
              showToast(`Indexed dropped file: ${f.name}`);
            } catch {
              showToast(`Could not index ${f.name}`);
            }
          }
        }}
        style={dragOver ? { outline: "2px dashed var(--ink)", outlineOffset: "-4px" } : undefined}
      >
        {/* COLUMN 1: LEFT NAVIGATION RAIL */}
        <LeftRail
          activeView={activeView}
          activeShelfFilter={activeShelfFilter}
          userName={settings.user_name || ""}
          hiddenShelfIds={hiddenShelfIds}
          files={files}
          shelves={shelves}
          lookalikes={lookalikes}
          trashCount={trashCount}
          watchedFolders={watchedFolders}
          connectedDevices={connectedDevices}
          isNightMode={isNightMode}
          isIndexing={isProcessingFile}
          pausedOnBattery={pausedOnBattery}
          engineStatus={engineStatus}
          sidebarHidden={!sidebarVisible}
          mobileOpen={mobileRailOpen}
          onCloseMobile={() => setMobileRailOpen(false)}
          onSwitchView={setActiveView}
          onJumpToShelf={handleJumpToShelf}
          onToggleHideShelf={(id) => {
            handleToggleHideShelf(id);
            if (activeShelfFilter === id) {
              setActiveShelfFilter("all");
              setActiveSubShelfFilter("all");
            }
          }}
          onRestoreAllHiddenShelves={handleRestoreAllHiddenShelves}
          onCreateSmartShelf={() => setFileDialog({ type: "create-smart-shelf" })}
          onSelectFile={handleSelectFile}
          onScanDevice={handleScanDevice}
          onImportDevice={handleImportDevice}
          onCreateFolder={handleCreateFolder}
          onToggleTheme={handleToggleTheme}
        />

        {/* COLUMN 2: CENTER PRIMARY STAGE */}
        <main className="center-stage">
          <TopMemoryBar
            query={searchQuery}
            activeView={activeView}
            sidebarVisible={sidebarVisible}
            inspectorVisible={inspectorVisible}
            cleanupCount={cleanupCount}
            liveProgress={liveProgress}
            onQueryChange={handleGlobalSearch}
            onClearQuery={handleClearSearch}
            onSwitchView={setActiveView}
            onOpenAskMode={() => {
              setFindMode("ask");
              setActiveView("find");
            }}
            onOpenSmartClean={handleOpenSmartClean}
            onOpenCommandPalette={() => setCommandPaletteOpen(true)}
            onToggleSidebar={() => setSidebarVisible((v) => !v)}
            onToggleInspector={() => setInspectorVisible((v) => !v)}
            onToggleMobileRail={() => setMobileRailOpen((o) => !o)}
          />

          <div className="stage-body">
            {liveProgress && (
              <div className="live-indexing-banner" role="status" aria-live="polite">
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px", fontSize: "12.5px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", minWidth: 0 }}>
                    <span className="status-dot pulse" aria-hidden="true" />
                    <span style={{ fontWeight: 600, color: "var(--ink)", whiteSpace: "nowrap" }}>
                      {liveProgress.operation}
                    </span>
                    <span style={{ color: "var(--ink-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {liveProgress.file_name || "Processing files…"}
                    </span>
                  </div>
                  <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-2)", flexShrink: 0 }}>
                    {liveProgress.operation.startsWith("downloading")
                      ? `${Math.min(100, Math.round(liveProgress.current))}%`
                      : liveProgress.total > 1
                      ? `${liveProgress.current} / ${liveProgress.total} (${Math.min(100, Math.round((liveProgress.current / liveProgress.total) * 100))}%)`
                      : "Working…"}
                  </span>
                </div>
                <div className="live-indexing-track">
                  <div
                    className="live-indexing-fill"
                    style={{
                      width: `${liveProgress.total > 1 ? Math.max(4, Math.min(100, Math.round((liveProgress.current / liveProgress.total) * 100))) : 35}%`,
                    }}
                  />
                </div>
              </div>
            )}
            {(engineStatus === "error" || retryingEngine) && (
              <div className="stage-alert-slot">
                <div className="stage-alert-banner" role="alert">
                  <span style={{ flex: "1 1 260px" }}>
                    {retryingEngine
                      ? "Reconnecting to local file index…"
                      : "Local file engine is starting up. Try retrying in a moment."}
                  </span>
                  <button
                    type="button"
                    className="btn-tactile solid"
                    disabled={retryingEngine}
                    onClick={handleRetryConnection}
                  >
                    {retryingEngine ? "Retrying…" : "Retry connection"}
                  </button>
                </div>
              </div>
            )}
            <div className="stage-sheets">
              <div className="stage-scroll-blur-top" aria-hidden="true" />
              {activeView === "shelves" && (
                <ShelvesView
                  active
                  files={files}
                  shelves={shelves}
                  selectedFileId={selectedFileId}
                  activeShelfFilter={activeShelfFilter}
                  activeSubShelfFilter={activeSubShelfFilter}
                  hiddenShelfIds={hiddenShelfIds}
                  silhouetteScore={silhouetteScore}
                  submode={shelfSubmode}
                  onSetSubmode={setShelfSubmode}
                  onSetSubShelfFilter={setActiveSubShelfFilter}
                  onSelectShelfFilter={(id) => handleJumpToShelf(id)}
                  onToggleHideShelf={handleToggleHideShelf}
                  onRestoreAllHiddenShelves={handleRestoreAllHiddenShelves}
                  onSelectFile={handleSelectFile}
                  onOpenQuickLook={() => setQuickLookOpen(true)}
                  onClearShelfFilter={() => {
                    setActiveShelfFilter("all");
                    setActiveSubShelfFilter("all");
                  }}
                  onTogglePinShelf={handleTogglePinShelf}
                  onRenameShelf={(sh) => setFileDialog({ type: "rename-shelf", shelf: sh })}
                  onDeleteCustomShelf={handleDeleteCustomShelf}
                  onCreateSmartShelfFromSelection={(ids) =>
                    setFileDialog({ type: "create-smart-shelf", initialFileIds: ids })
                  }
                  onAskAboutShelf={(sh) => {
                    setFindMode("ask");
                    setActiveView("find");
                    void handleAskShelves(`List items on shelf ${sh.name}`);
                  }}
                  onBulkAction={handleBulkAction}
                  clearSelectionSignal={bulkClearSignal}
                  clipboard={clipboard}
                  onSetClipboard={handleSetClipboard}
                  onPasteClipboard={handlePasteClipboardGlobal}
                  onShowToast={showToast}
                  onRenameFile={(f) => setFileDialog({ type: "rename", file: f })}
                  onMoveFile={(f) => setFileDialog({ type: "move", file: f, initialMode: "move" })}
                  onDeleteFile={(f) => setFileDialog({ type: "delete", file: f })}
                  onContextMenuFile={(e, f) => {
                    e.preventDefault();
                    setContextMenu({ file: f, x: e.clientX, y: e.clientY });
                  }}
                />
              )}

              {activeView === "folders" && (
                <FoldersView
                  active
                  watchedFolders={watchedFolders}
                  selectedFileId={selectedFileId}
                  clipboard={clipboard}
                  onSetClipboard={handleSetClipboard}
                  onClearClipboard={() => setClipboard(null)}
                  onSelectFile={handleSelectFile}
                  onOpenQuickLook={() => setQuickLookOpen(true)}
                  onCreateFolder={handleCreateFolder}
                  onRenameFile={(f) => setFileDialog({ type: "rename", file: f })}
                  onMoveFile={(f) => setFileDialog({ type: "move", file: f, initialMode: "move" })}
                  onDeleteFile={(f) => setFileDialog({ type: "delete", file: f })}
                  onBulkAction={handleBulkAction}
                  onContextMenuFile={(e, f) => {
                    e.preventDefault();
                    setContextMenu({ file: f, x: e.clientX, y: e.clientY });
                  }}
                  onRefreshState={applyWorkspaceState}
                  onShowToast={showToast}
                  onUndo={() => handleUndoAction()}
                />
              )}

              {activeView === "find" && (
                <FindView
                  active
                  query={searchQuery}
                  modalityFilter={searchModality}
                  results={searchResults}
                  allFiles={files}
                  savedSearches={savedSearches}
                  suggestions={querySuggestions}
                  latencyMs={searchLatencyMs}
                  selectedFileId={selectedFileId}
                  similarSource={similarSource}
                  askResult={askResult}
                  asking={asking}
                  findMode={findMode}
                  userName={settings.user_name || ""}
                  onSetFindMode={setFindMode}
                  onQueryChange={handleGlobalSearch}
                  onClearQuery={handleClearSearch}
                  onClearSimilar={() => {
                    setSimilarSource(null);
                    setSearchQuery("");
                  }}
                  onTriggerPrompt={handleTriggerPrompt}
                  onAskShelves={handleAskShelves}
                  onExecuteAiAction={handleExecuteAiAction}
                  onUndoAction={() => void handleUndoAction()}
                  onSetModalityFilter={setSearchModality}
                  onSelectFile={handleSelectFile}
                  onOpenQuickLook={() => setQuickLookOpen(true)}
                  onSaveSearch={handleSaveSearch}
                  onDeleteSavedSearch={handleDeleteSavedSearch}
                  onRenameFile={(f) => setFileDialog({ type: "rename", file: f })}
                  onMoveFile={(f) => setFileDialog({ type: "move", file: f, initialMode: "move" })}
                  onDeleteFile={(f) => setFileDialog({ type: "delete", file: f })}
                  onCopyPath={handleCopyPath}
                  onShowToast={showToast}
                  clipboard={clipboard}
                  onSetClipboard={handleSetClipboard}
                  onPasteClipboard={handlePasteClipboardGlobal}
                  onJumpToShelf={handleJumpToShelf}
                  onContextMenuFile={(e, f) => {
                    e.preventDefault();
                    setContextMenu({ file: f, x: e.clientX, y: e.clientY });
                  }}
                />
              )}

              {(activeView === "tidy" || activeView === "lookalikes" || activeView === "trash") && (
                <div className="clean-hub-bar-wrap">
                  <div className="seg-group" role="tablist" aria-label="Cleanup sections">
                    <button
                      type="button"
                      role="tab"
                      aria-selected={activeView === "tidy"}
                      className={`seg-btn ${activeView === "tidy" ? "active" : ""}`}
                      onClick={() => setActiveView("tidy")}
                    >
                      Rename
                    </button>
                    <button
                      type="button"
                      role="tab"
                      aria-selected={activeView === "lookalikes"}
                      className={`seg-btn ${activeView === "lookalikes" ? "active" : ""}`}
                      onClick={() => setActiveView("lookalikes")}
                    >
                      Duplicates
                    </button>
                    <button
                      type="button"
                      role="tab"
                      aria-selected={activeView === "trash"}
                      className={`seg-btn ${activeView === "trash" ? "active" : ""}`}
                      onClick={() => setActiveView("trash")}
                    >
                      Trash &amp; Archive
                    </button>
                  </div>
                </div>
              )}

              {activeView === "tidy" && (
                <TidyNamesView
                  active
                  files={files}
                  renameHistory={renameHistory}
                  selectedFileId={selectedFileId}
                  onSelectFile={handleSelectFile}
                  onToggleRename={handleToggleRename}
                  onApplyAllRenames={handleApplyAllRenames}
                  onRefreshState={applyWorkspaceState}
                  onShowToast={showToast}
                  onClearHistory={handleClearActivity}
                />
              )}

              {activeView === "lookalikes" && (
                <LookalikesView
                  active
                  lookalikes={lookalikes}
                  files={files}
                  onResolveLookalike={handleResolveLookalike}
                  onSwapLookalike={handleSwapLookalike}
                  onSelectFile={handleSelectFile}
                />
              )}

              {activeView === "trash" && (
                <TrashArchiveView
                  active
                  onRefreshState={applyWorkspaceState}
                  onShowToast={showToast}
                />
              )}

              {activeView === "learns" && (
                <ActivityView
                  active
                  activeStage={activePipeStage}
                  statusText={pipeStatusText}
                  timerBadge={pipeTimerBadge}
                  stageBadges={stageBadges}
                  recentEvents={recentEvents}
                  actionHistory={actionHistory}
                  isProcessing={isProcessingFile}
                  onUploadRealFile={handleUploadRealFile}
                  onSelectFile={handleSelectFile}
                  onUndoAction={handleUndoAction}
                  onRedoAction={handleRedoAction}
                  onClearActivity={handleClearActivity}
                />
              )}

              {activeView === "settings" && (
                <SettingsDrawer
                  active
                  watchedFolders={watchedFolders}
                  settings={settings}
                  storageStats={storageStats}
                  liveProgress={liveProgress}
                  onToggleWatchFolder={handleToggleWatchFolder}
                  onRemoveWatchFolder={handleRemoveWatchFolder}
                  onAddWatchFolder={handleAddWatchFolder}
                  onUpdateSetting={handleUpdateSetting}
                  onRescanFolders={handleRescanFolders}
                  onVerifyIndex={handleVerifyIndex}
                  onResetSetup={handleResetSetup}
                  onRefreshState={applyWorkspaceState}
                  onShowToast={showToast}
                />
              )}
              <div className="stage-scroll-blur-bottom" aria-hidden="true" />
            </div>
          </div>
        </main>

        {/* COLUMN 3: RIGHT DOCKED LIVE FILE INSPECTOR SHEET + MODALS */}
        <InspectorSheet
          file={selectedFile}
          shelves={shelves}
          visible={inspectorVisible}
          silhouetteScore={silhouetteScore}
          quickLookOpen={quickLookOpen}
          onCloseQuickLook={handleCloseQuickLook}
          onOpenQuickLook={handleOpenQuickLook}
          onStepQuickLook={handleStepQuickLook}
          onFindSimilar={handleFindSimilar}
          onAssignShelf={handleAssignShelf}
          onHideInspector={handleHideInspector}
          onSelectFile={handleSelectFile}
          onOpenRenameDialog={handleOpenRenameDialog}
          onOpenMoveDialog={handleOpenMoveDialog}
          onOpenDeleteDialog={handleOpenDeleteDialog}
          onToggleRename={handleToggleRename}
          onCopyPath={handleCopyPath}
          onShowToast={showToast}
          onRefreshState={applyWorkspaceState}
        />
      </div>

      <FileActionDialogs
        dialog={fileDialog}
        watchedFolders={watchedFolders}
        allFiles={files}
        onClose={() => setFileDialog({ type: "closed" })}
        onCreateFolder={handleConfirmCreateFolder}
        onRenameFile={handleConfirmRenameFile}
        onMoveOrCopyFile={handleConfirmMoveOrCopyFile}
        onTrashFile={handleConfirmTrashFile}
        onDeletePermanent={handleConfirmDeletePermanent}
        onBulkTag={handleConfirmBulkTag}
        onCreateSmartShelf={handleConfirmCreateSmartShelf}
        onRenameShelf={handleConfirmRenameShelf}
      />

      <FileContextMenu
        menu={contextMenu}
        onClose={() => setContextMenu(null)}
        onQuickLook={(f) => {
          handleSelectFile(f.id);
          setQuickLookOpen(true);
        }}
        onFindSimilar={handleFindSimilar}
        onOpenExternal={(f) => openFileExternal(f.id, false).catch(() => openFileInOS(f.path))}
        onRevealExternal={(f) => openFileExternal(f.id, true).catch(() => revealFileInOS(f.path))}
        onCopyToClipboard={(f, mode) => handleSetClipboard([f.id], mode)}
        onRename={(f) => setFileDialog({ type: "rename", file: f })}
        onMove={(f) => setFileDialog({ type: "move", file: f, initialMode: "move" })}
        onTogglePin={async (f) => {
          const res = await togglePinFile(f.id);
          applyWorkspaceState(res.state);
          showToast(res.pinned ? `Pinned ${f.name}` : `Unpinned ${f.name}`);
        }}
        onDelete={(f) => setFileDialog({ type: "delete", file: f })}
      />

      <OnboardingModal
        open={settings.setup_completed === false}
        watchedFolders={watchedFolders}
        connectedDevices={connectedDevices}
        initialUserName={settings.user_name || ""}
        onComplete={handleCompleteOnboarding}
      />

      <SmartCleanModal
        open={smartCleanOpen}
        preview={smartCleanPreview}
        running={smartCleanRunning}
        onClose={() => setSmartCleanOpen(false)}
        onRollbackFolders={handleRollbackOrganizeDisk}
        onExecute={handleExecuteSmartClean}
      />

      <CommandPalette
        open={commandPaletteOpen}
        files={files}
        shelves={shelves}
        actions={[
          { id: "smart-clean", label: "Clean up & organize workspace", hint: "Action", onRun: handleOpenSmartClean },
          { id: "new-folder", label: "New Folder", hint: "Action", onRun: () => handleCreateFolder() },
          { id: "new-shelf", label: "New Smart Shelf", hint: "Action", onRun: () => setFileDialog({ type: "create-smart-shelf" }) },
          { id: "undo", label: "Undo last change", hint: formatShortcut("Z"), onRun: () => handleUndoAction() },
          { id: "redo", label: "Redo last undone change", hint: formatShortcut("Z", { shift: true }), onRun: () => handleRedoAction() },
          { id: "view-shelves", label: "Go to Shelves", hint: "View", onRun: () => setActiveView("shelves") },
          { id: "view-folders", label: "Go to Folders", hint: "View", onRun: () => setActiveView("folders") },
          { id: "view-find", label: "Go to Search & Ask", hint: "View", onRun: () => setActiveView("find") },
          { id: "view-tidy", label: "Go to Rename suggestions", hint: "View", onRun: () => setActiveView("tidy") },
          { id: "view-dups", label: "Go to Duplicates", hint: "View", onRun: () => setActiveView("lookalikes") },
          { id: "view-trash", label: "Go to Trash & Archive", hint: "View", onRun: () => setActiveView("trash") },
          { id: "view-activity", label: "Go to Activity & Undo", hint: "View", onRun: () => setActiveView("learns") },
          { id: "view-settings", label: "Go to Settings", hint: "View", onRun: () => setActiveView("settings") },
        ]}
        onClose={() => setCommandPaletteOpen(false)}
        onSelectFile={handleSelectFile}
        onSelectShelf={handleJumpToShelf}
      />

      {/* EDITORIAL FEEDBACK TOAST */}
      <div
        className={`editorial-toast ${toastVisible ? "show" : ""}`}
        id="editorialToast"
        role="status"
        aria-live="polite"
        onMouseEnter={() => {
          if (toastTimerRef.current) {
            window.clearTimeout(toastTimerRef.current);
            toastTimerRef.current = null;
          }
        }}
        onFocus={() => {
          if (toastTimerRef.current) {
            window.clearTimeout(toastTimerRef.current);
            toastTimerRef.current = null;
          }
        }}
        onMouseLeave={() => {
          if (!toastVisible) return;
          if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
          toastTimerRef.current = window.setTimeout(() => {
            setToastVisible(false);
          }, 1800);
        }}
        onBlur={(e) => {
          if (!toastVisible) return;
          if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
          if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
          toastTimerRef.current = window.setTimeout(() => {
            setToastVisible(false);
          }, 1800);
        }}
      >
        <span id="toastText">{toastMessage}</span>
        {toastUndoFn && (
          <button
            type="button"
            id="toastUndoBtn"
            style={{
              textDecoration: "underline",
              fontWeight: 500,
              color: "var(--bg-canvas)",
            }}
            onClick={() => {
              setToastVisible(false);
              toastUndoFn();
            }}
          >
            undo
          </button>
        )}
      </div>
    </>
  );
};
export default App;
