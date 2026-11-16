import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import {
  ActionHistoryItem,
  AppSettings,
  ConnectedDevice,
  LookalikePair,
  PipelineStageEvent,
  RenameHistoryItem,
  SavedSearch,
  ShelfFile,
  ShelfGroup,
  SmartCleanPreview,
  StorageStats,
  TidyMovePreview,
  WatchedFolder,
  addWatchedFolderPath,
  applyAllSuggestedRenames,
  completeFirstRunSetup,
  copyFileToFolder,
  createFolder,
  deleteFilePermanently,
  deleteSearchPreset,
  executeSmartClean,
  executeTidyFoldersOnDisk,
  fetchSmartCleanPreview,
  fetchTidyMovesPreview,
  fetchWorkspaceState,
  importConnectedDevice,
  ingestFilePath,
  isTauriRuntime,
  moveFileToFolder,
  pickFolderNative,
  redoHistoryAction,
  reloadWatcherNative,
  renameFileCustom,
  rescanAllWatchedFolders,
  rollbackTidyFoldersOnDisk,
  runBulkFileAction,
  runSemanticSearch,
  saveSearchPreset,
  scanConnectedDevice,
  subscribeToPipelineEvents,
  toggleFileRename,
  togglePinShelf,
  toggleResolveLookalikePair,
  toggleWatchedFolderPath,
  trashFile,
  undoHistoryAction,
  updateAppSetting,
  uploadRealDroppedFile,
  verifyLocalIndexIntegrity,
} from "./lib/tauri-api";
import { LeftRail, ViewId } from "./components/LeftRail";
import { TopMemoryBar } from "./components/TopMemoryBar";
import { ShelvesView } from "./components/ShelvesView";
import { FindView } from "./components/FindView";
import { TidyNamesView } from "./components/TidyNamesView";
import { LookalikesView } from "./components/LookalikesView";
import { ActivityView } from "./components/ActivityView";
import { InspectorSheet } from "./components/InspectorSheet";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { OnboardingModal } from "./components/OnboardingModal";
import { SmartCleanModal } from "./components/SmartCleanModal";
import { CommandPalette } from "./components/CommandPalette";
import { FileActionDialogs, FileDialogState } from "./components/FileActionDialogs";

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
  dbPath: "~/.local/share/good-shelves/index.db",
  dbPathFull: "",
  dbSizeMB: 0,
  indexedCount: 0,
};

export const App: React.FC = () => {
  const [activeView, setActiveView] = useState<ViewId>("shelves");
  const [shelfSubmode, setShelfSubmode] = useState<"list" | "map">("list");
  const [activeShelfFilter, setActiveShelfFilter] = useState<string>("all");
  const [selectedFileId, setSelectedFileId] = useState<string>("");
  const [inspectorVisible, setInspectorVisible] = useState<boolean>(true);
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

  // Live Pipeline Telemetry State
  const [activePipeStage, setActivePipeStage] = useState<number>(1);
  const [pipeStatusText, setPipeStatusText] = useState<string>(
    "ready — drop a file or save into a watched folder."
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
  const [organizeModalOpen, setOrganizeModalOpen] = useState<boolean>(false);
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
  const [tidyMoves, setTidyMoves] = useState<TidyMovePreview[]>([]);
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
      setSelectedFileId((prev) => {
        if (prev && state.files.some((f) => f.id === prev)) return prev;
        return state.files[0]?.id || "";
      });
    },
    []
  );

  const loadInitial = useCallback(async (isCancelled?: () => boolean) => {
    setEngineStatus("connecting");
    for (let attempt = 0; attempt < 25; attempt++) {
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
      await new Promise((r) => setTimeout(r, 500));
    }
    if (!isCancelled?.()) {
      setEngineStatus("error");
    }
  }, [applyWorkspaceState]);

  // Initial load
  useEffect(() => {
    let mounted = true;
    loadInitial(() => !mounted);
    return () => {
      mounted = false;
    };
  }, [loadInitial]);

  // Subscribe to real-time indexing stage events, live progress, and device hot-plug
  useEffect(() => {
    const unsub = subscribeToPipelineEvents((evt) => {
      if (evt.type === "progress") {
        const cur = evt.current ?? 0;
        const tot = evt.total ?? 1;
        setLiveProgress(
          cur >= tot
            ? null
            : {
                operation: evt.operation || "scanning",
                current: cur,
                total: tot,
                file_name: evt.file_name || "",
              }
        );
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

  // Execute real semantic search when query or modality changes
  useEffect(() => {
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
  }, [searchQuery, searchModality, files]);

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

  // Global keyboard shortcuts (Escape, Space for Quick Look, Up/Down arrows)
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
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
        quickLookOpen ||
        organizeModalOpen ||
        commandPaletteOpen ||
        smartCleanOpen ||
        fileDialog.type !== "closed" ||
        settings.setup_completed === false;

      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        if (settings.setup_completed === false) return;
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
          handleRedoAction();
        } else {
          handleUndoAction();
        }
        return;
      }

      if (!isInput && !anyModalOpen && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
        e.preventDefault();
        handleRedoAction();
        return;
      }

      if (e.key === "Escape") {
        if (settings.setup_completed === false) {
          return;
        }
        if (anyModalOpen) {
          setQuickLookOpen(false);
          setOrganizeModalOpen(false);
          setCommandPaletteOpen(false);
          if (!smartCleanRunning) setSmartCleanOpen(false);
          setFileDialog({ type: "closed" });
        } else if (isInput && target) {
          target.blur();
        } else if (inspectorVisible) {
          setInspectorVisible(false);
        }
        return;
      }

      if (anyModalOpen) return;

      if (!isInteractive && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const lower = e.key.toLowerCase();
        if (lower === "i") {
          e.preventDefault();
          setInspectorVisible((v) => !v);
          return;
        }
        const curFile = files.find((f) => f.id === selectedFileId);
        if (curFile && lower === "r") {
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
        const list = activeView === "find" ? searchResults : files;
        if (list.length === 0) return;
        e.preventDefault();
        const idx = list.findIndex((f) => f.id === selectedFileId);
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
  }, [
    activeView,
    commandPaletteOpen,
    fileDialog.type,
    files,
    handleRedoAction,
    handleUndoAction,
    inspectorVisible,
    organizeModalOpen,
    quickLookOpen,
    searchResults,
    selectedFileId,
    settings.setup_completed,
    smartCleanOpen,
    smartCleanRunning,
  ]);

  const handleGlobalSearch = (val: string) => {
    setSearchQuery(val);
    if (activeView !== "find" && val.trim().length > 0) {
      setActiveView("find");
    }
  };

  const handleClearSearch = () => {
    setSearchQuery("");
    showToast("Cleared search filter");
  };

  const handleTriggerPrompt = (q: string) => {
    setSearchQuery(q);
    if (activeView !== "find") setActiveView("find");
  };

  const handleJumpToShelf = (shelfId: string) => {
    setActiveShelfFilter(shelfId);
    if (activeView !== "shelves") setActiveView("shelves");
    setShelfSubmode("list");
    const targetShelf = shelves.find((s) => s.id === shelfId);
    showToast(
      shelfId === "all"
        ? `Showing all ${shelves.length} shelves`
        : `Filtered to shelf: ${targetShelf?.name || shelfId}`
    );
  };

  const handleSelectFile = (fileId: string) => {
    setSelectedFileId(fileId);
    if (!inspectorVisible) {
      setInspectorVisible(true);
    }
  };

  const handleToggleTheme = () => {
    const next = !isNightMode;
    setIsNightMode(next);
    document.documentElement.classList.toggle("dark", next);
    try {
      window.localStorage.setItem("good-shelves:theme", next ? "dark" : "light");
    } catch {
      // ignore storage failure
    }
    showToast(
      `Switched to ${next ? "dark" : "light"} theme`
    );
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
          : "Restored archived file from ~/Documents/GoodShelves_Archive",
        () => {
          handleResolveLookalike(pairId);
        }
      );
    } catch (err) {
      showToast(`Archive action failed: ${err instanceof Error ? err.message : ""}`);
    }
  };

  const handleOpenOrganizeModal = async () => {
    try {
      const prev = await fetchTidyMovesPreview();
      setTidyMoves(prev.moves);
    } catch {
      setTidyMoves([]);
    }
    setOrganizeModalOpen(true);
  };

  const handleConfirmOrganizeDisk = async () => {
    try {
      const res = await executeTidyFoldersOnDisk();
      applyWorkspaceState(res.state);
      setOrganizeModalOpen(false);
      showToast(
        `Organized ${res.movedCount} files into ~/Shelves directories on disk`,
        () => {
          handleRollbackOrganizeDisk();
        }
      );
    } catch {
      showToast("Failed to organize folders on disk");
    }
  };

  const handleRollbackOrganizeDisk = async () => {
    try {
      const res = await rollbackTidyFoldersOnDisk();
      applyWorkspaceState(res.state);
      setOrganizeModalOpen(false);
      showToast(`Restored ${res.restoredCount} files to original folders`);
    } catch {
      showToast("No pending folder moves to rollback");
    }
  };

  const animateAndRunIngestion = async (
    runner: () => Promise<{ ok: boolean; file: ShelfFile; state: any }>
  ) => {
    setIsProcessingFile(true);
    setStageBadges(["running…", "waiting", "waiting", "waiting"]);
    try {
      const res = await runner();
      applyWorkspaceState(res.state);
      if (res.file) {
        setSelectedFileId(res.file.id);
        setActivePipeStage(4);
        setStageBadges(["detected", "parsed + ocr", "512-d vector", "shelved"]);
        setPipeStatusText(
          `04 · Indexed "${res.file.title}" onto ${res.file.shelfName}`
        );
        showToast(`Indexed "${res.file.title}"`);
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
    try {
      const res = await addWatchedFolderPath(path);
      applyWorkspaceState(res.state);
      showToast(`Added ${path} to watched folders`);
    } catch (err) {
      showToast(err instanceof Error ? err.message : `Could not watch folder: ${path}`);
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

  const handleRescanFolders = async () => {
    showToast("Refreshing watched folders in background…");
    try {
      const res = await rescanAllWatchedFolders();
      applyWorkspaceState(res.state);
      showToast(
        `Rescanned watched folders · ${res.state.files.length} active files indexed`
      );
    } catch {
      showToast("Rescan failed");
    }
  };

  const handleVerifyIndex = async () => {
    try {
      const res = await verifyLocalIndexIntegrity();
      applyWorkspaceState(res.state);
      showToast(
        `Local SQLite-vec index verified · ${res.integrity.vectorRows} vectors aligned (${res.integrity.orphansRemoved} orphans cleaned)`
      );
    } catch {
      showToast("Index verification failed");
    }
  };

  const handleCopyPath = (f: ShelfFile) => {
    const full = `${f.folder}/${f.name}`;
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

  const handleCompleteOnboarding = async (folders: string[]) => {
    try {
      const res = await completeFirstRunSetup(folders);
      applyWorkspaceState(res.state);
      await reloadWatcherNative();
      showToast(`Indexed ${res.scannedCount} files from selected folders`);
    } catch {
      showToast("Could not complete initial setup");
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

  const handleCreateFolder = () => {
    setFileDialog({ type: "new-folder" });
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
    const res = await trashFile(file.id);
    applyWorkspaceState(res.state);
    setQuickLookOpen(false);
    showToast(`Moved ${file.name} to Trash`, () => {
      handleUndoAction();
    });
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

  const relatedFiles = useMemo(() => {
    if (!selectedFile) return [];
    return files
      .filter((f) => f.id !== selectedFile.id && f.shelfId === selectedFile.shelfId)
      .slice(0, 3);
  }, [files, selectedFile]);

  return (
    <>
      <div
        className="desktop-workspace"
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
        {/* COLUMN 1: LEFT ARCHITECTURAL INDEX RAIL */}
        <LeftRail
          activeView={activeView}
          activeShelfFilter={activeShelfFilter}
          files={files}
          shelves={shelves}
          lookalikes={lookalikes}
          watchedFolders={watchedFolders}
          connectedDevices={connectedDevices}
          isNightMode={isNightMode}
          isIndexing={isProcessingFile}
          engineStatus={engineStatus}
          onSwitchView={setActiveView}
          onJumpToShelf={handleJumpToShelf}
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
            inspectorVisible={inspectorVisible}
            liveProgress={liveProgress}
            onQueryChange={handleGlobalSearch}
            onClearQuery={handleClearSearch}
            onOpenOrganizeModal={handleOpenOrganizeModal}
            onOpenSmartClean={handleOpenSmartClean}
            onOpenCommandPalette={() => setCommandPaletteOpen(true)}
            onToggleInspector={() => setInspectorVisible((v) => !v)}
          />

          <div className="stage-body">
            {engineStatus === "error" && (
              <div
                role="alert"
                style={{
                  maxWidth: "820px",
                  margin: "0 auto 16px",
                  padding: "12px 16px",
                  borderRadius: "7px",
                  border: "1px solid var(--rule-strong)",
                  background: "var(--bg-subtle)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "12px",
                  fontSize: "13px",
                }}
              >
                <span>
                  Local AI sidecar is not responding yet. Make sure the engine started cleanly or retry.
                </span>
                <button
                  type="button"
                  className="btn-tactile solid"
                  onClick={() => loadInitial()}
                >
                  retry connection
                </button>
              </div>
            )}
            <ShelvesView
              active={activeView === "shelves"}
              files={files}
              shelves={shelves}
              selectedFileId={selectedFileId}
              activeShelfFilter={activeShelfFilter}
              silhouetteScore={silhouetteScore}
              submode={shelfSubmode}
              onSetSubmode={setShelfSubmode}
              onSelectFile={handleSelectFile}
              onOpenQuickLook={() => setQuickLookOpen(true)}
              onTogglePinShelf={handleTogglePinShelf}
              onBulkAction={handleBulkAction}
              clearSelectionSignal={bulkClearSignal}
              onRenameFile={(f) => setFileDialog({ type: "rename", file: f })}
              onMoveFile={(f) => setFileDialog({ type: "move", file: f, initialMode: "move" })}
              onDeleteFile={(f) => setFileDialog({ type: "delete", file: f })}
            />

            <FindView
              active={activeView === "find"}
              query={searchQuery}
              modalityFilter={searchModality}
              results={searchResults}
              allFiles={files}
              savedSearches={savedSearches}
              suggestions={querySuggestions}
              latencyMs={searchLatencyMs}
              selectedFileId={selectedFileId}
              onQueryChange={handleGlobalSearch}
              onClearQuery={handleClearSearch}
              onTriggerPrompt={handleTriggerPrompt}
              onSetModalityFilter={setSearchModality}
              onSelectFile={handleSelectFile}
              onOpenQuickLook={() => setQuickLookOpen(true)}
              onSaveSearch={handleSaveSearch}
              onDeleteSavedSearch={handleDeleteSavedSearch}
              onRenameFile={(f) => setFileDialog({ type: "rename", file: f })}
              onMoveFile={(f) => setFileDialog({ type: "move", file: f, initialMode: "move" })}
              onDeleteFile={(f) => setFileDialog({ type: "delete", file: f })}
            />

            <TidyNamesView
              active={activeView === "tidy"}
              files={files}
              renameHistory={renameHistory}
              selectedFileId={selectedFileId}
              onSelectFile={handleSelectFile}
              onToggleRename={handleToggleRename}
              onApplyAllRenames={handleApplyAllRenames}
            />

            <LookalikesView
              active={activeView === "lookalikes"}
              lookalikes={lookalikes}
              onResolveLookalike={handleResolveLookalike}
              onSelectFile={handleSelectFile}
            />

            <ActivityView
              active={activeView === "learns"}
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
            />

            <SettingsDrawer
              active={activeView === "settings"}
              watchedFolders={watchedFolders}
              settings={settings}
              storageStats={storageStats}
              onToggleWatchFolder={handleToggleWatchFolder}
              onAddWatchFolder={handleAddWatchFolder}
              onUpdateSetting={handleUpdateSetting}
              onRescanFolders={handleRescanFolders}
              onVerifyIndex={handleVerifyIndex}
              onRefreshState={applyWorkspaceState}
              onShowToast={showToast}
            />
          </div>
        </main>

        {/* COLUMN 3: RIGHT DOCKED LIVE FILE INSPECTOR SHEET + MODALS */}
        <InspectorSheet
          file={selectedFile}
          relatedFiles={relatedFiles}
          visible={inspectorVisible}
          silhouetteScore={silhouetteScore}
          quickLookOpen={quickLookOpen}
          organizeModalOpen={organizeModalOpen}
          tidyMoves={tidyMoves}
          onCloseQuickLook={() => setQuickLookOpen(false)}
          onOpenQuickLook={() => setQuickLookOpen(true)}
          onHideInspector={() => setInspectorVisible(false)}
          onShowInspector={() => setInspectorVisible(true)}
          onSelectFile={handleSelectFile}
          onOpenRenameDialog={(f) => setFileDialog({ type: "rename", file: f })}
          onOpenMoveDialog={(f, mode) =>
            setFileDialog({ type: "move", file: f, initialMode: mode || "move" })
          }
          onOpenDeleteDialog={(f) => setFileDialog({ type: "delete", file: f })}
          onCloseOrganizeModal={() => setOrganizeModalOpen(false)}
          onConfirmOrganizeDisk={handleConfirmOrganizeDisk}
          onRollbackOrganizeDisk={handleRollbackOrganizeDisk}
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
      />

      <OnboardingModal
        open={settings.setup_completed === false}
        watchedFolders={watchedFolders}
        connectedDevices={connectedDevices}
        onComplete={handleCompleteOnboarding}
      />

      <SmartCleanModal
        open={smartCleanOpen}
        preview={smartCleanPreview}
        running={smartCleanRunning}
        onClose={() => setSmartCleanOpen(false)}
        onExecute={handleExecuteSmartClean}
      />

      <CommandPalette
        open={commandPaletteOpen}
        files={files}
        shelves={shelves}
        actions={[
          { id: "smart-clean", label: "Clean up workspace", hint: "action", onRun: handleOpenSmartClean },
          { id: "new-folder", label: "New Folder", hint: "action", onRun: handleCreateFolder },
          { id: "undo", label: "Undo last change", hint: "ctrl/cmd+z", onRun: () => handleUndoAction() },
          { id: "redo", label: "Redo last undone change", hint: "ctrl/cmd+shift+z", onRun: () => handleRedoAction() },
          { id: "view-shelves", label: "Go to Shelves", hint: "view", onRun: () => setActiveView("shelves") },
          { id: "view-find", label: "Go to Find / Search", hint: "view", onRun: () => setActiveView("find") },
          { id: "view-activity", label: "Go to Activity & History", hint: "view", onRun: () => setActiveView("learns") },
          { id: "view-settings", label: "Go to Settings & Folders", hint: "view", onRun: () => setActiveView("settings") },
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
