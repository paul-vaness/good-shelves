import React, { useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import {
  AiMemoryItem,
  AiTrainCalibrationReport,
  AppSettings,
  StorageStats,
  VisionDownloadProgress,
  VisionModelInfo,
  WatchedFolder,
  fetchVisionDownloadProgress,
  fetchVisionModels,
  pickFolderNative,
  reindexImagesOnly,
  revealFileInOS,
  selectVisionModel,
  trainAndCalibrateAi,
  exportIndex,
} from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";
import { defaultExamplePath } from "../lib/platform";

const APP_VERSION = "1.0.4";

interface SettingsDrawerProps {
  active: boolean;
  watchedFolders: WatchedFolder[];
  aiMemory?: AiMemoryItem[];
  settings: AppSettings;
  storageStats: StorageStats;
  liveProgress?: { operation: string; current: number; total: number; file_name: string } | null;
  onBackToHome?: () => void;
  onToggleWatchFolder: (path: string) => Promise<void> | void;
  onRemoveWatchFolder?: (path: string) => Promise<void> | void;
  onAddWatchFolder: (path: string) => Promise<void> | void;
  onUpdateSetting: (key: string, value: unknown) => void;
  onRescanFolders: (allFiles?: boolean) => Promise<void> | void;
  onVerifyIndex: () => Promise<void> | void;
  onDeleteAiMemory?: (id: number) => Promise<void> | void;
  onClearAiMemory?: () => Promise<void> | void;
  onResetSetup?: () => Promise<void> | void;
  onRefreshState?: (state: any) => void;
  onShowToast?: (msg: string) => void;
}

type SettingsTab = "folders" | "indexing" | "vision" | "storage";

export const SettingsDrawer = memoView<SettingsDrawerProps>(({
  active,
  watchedFolders,
  aiMemory = [],
  settings,
  storageStats,
  liveProgress,
  onBackToHome,
  onToggleWatchFolder,
  onRemoveWatchFolder,
  onAddWatchFolder,
  onUpdateSetting,
  onRescanFolders,
  onVerifyIndex,
  onDeleteAiMemory,
  onClearAiMemory,
  onResetSetup,
  onRefreshState,
  onShowToast,
}) => {
  const [tab, setTab] = useState<SettingsTab>("folders");
  const [newFolderInput, setNewFolderInput] = useState<string>("");
  const [addingFolder, setAddingFolder] = useState<boolean>(false);
  const [visionModels, setVisionModels] = useState<VisionModelInfo[]>([]);
  const [visionBusyId, setVisionBusyId] = useState<string | null>(null);
  const [downloadProgress, setDownloadProgress] = useState<VisionDownloadProgress | null>(null);
  const [reindexingImages, setReindexingImages] = useState<boolean>(false);
  const [rescanning, setRescanning] = useState<boolean>(false);
  const [verifying, setVerifying] = useState<boolean>(false);
  const [exporting, setExporting] = useState<boolean>(false);
  const [trainingAi, setTrainingAi] = useState<boolean>(false);
  const [calibrationReport, setCalibrationReport] = useState<AiTrainCalibrationReport | null>(null);
  const [togglingFolderPath, setTogglingFolderPath] = useState<string | null>(null);
  const [removingFolderPath, setRemovingFolderPath] = useState<string | null>(null);
  const [confirmKey, setConfirmKey] = useState<string | null>(null);
  const confirmTimerRef = useRef<number | null>(null);
  const [draftSimilarity, setDraftSimilarity] = useState<number>(settings.min_similarity);
  const [draftMaxResults, setDraftMaxResults] = useState<number>(settings.max_results);
  const [draftMaxScan, setDraftMaxScan] = useState<number>(settings.max_scan_files ?? 500);
  const [draftIgnores, setDraftIgnores] = useState<string>(
    settings.ignore_patterns ?? ".git,node_modules,target,dist,build,.venv,__pycache__"
  );

  useEffect(() => {
    return () => {
      if (confirmTimerRef.current) window.clearTimeout(confirmTimerRef.current);
    };
  }, []);

  const requestConfirm = (key: string) => {
    setConfirmKey(key);
    if (confirmTimerRef.current) window.clearTimeout(confirmTimerRef.current);
    confirmTimerRef.current = window.setTimeout(() => {
      confirmTimerRef.current = null;
      setConfirmKey((k) => (k === key ? null : k));
    }, 4000);
  };

  const runConfirmed = (key: string, action: () => Promise<void> | void, label: string) => {
    if (confirmKey === key) {
      if (confirmTimerRef.current) {
        window.clearTimeout(confirmTimerRef.current);
        confirmTimerRef.current = null;
      }
      setConfirmKey(null);
      void action();
    } else {
      requestConfirm(key);
      onShowToast?.(`Press "${label}" again to confirm.`);
    }
  };

  useEffect(() => {
    setDraftSimilarity(settings.min_similarity);
  }, [settings.min_similarity]);

  useEffect(() => {
    setDraftMaxResults(settings.max_results);
  }, [settings.max_results]);

  useEffect(() => {
    setDraftMaxScan(settings.max_scan_files ?? 500);
  }, [settings.max_scan_files]);

  useEffect(() => {
    setDraftIgnores(
      settings.ignore_patterns ?? ".git,node_modules,target,dist,build,.venv,__pycache__"
    );
  }, [settings.ignore_patterns]);

  useEffect(() => {
    if (!active) return;
    fetchVisionModels()
      .then((res) => setVisionModels(res.models || []))
      .catch(() => {});
  }, [active]);

  useEffect(() => {
    if (!visionBusyId) {
      setDownloadProgress(null);
      return;
    }
    let cancelled = false;
    const poll = () => {
      fetchVisionDownloadProgress()
        .then((prog) => {
          if (!cancelled && prog.active) {
            setDownloadProgress(prog);
          }
        })
        .catch(() => {});
    };
    poll();
    const timer = window.setInterval(poll, 250);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [visionBusyId]);

  const handleSelectVisionModel = async (model: VisionModelInfo) => {
    if (visionBusyId) return;
    setVisionBusyId(model.id);
    setDownloadProgress({
      active: true,
      modelId: model.id,
      modelName: model.name,
      status: model.jointTextReady ? "activating" : "downloading",
      stage: model.jointTextReady ? "activating" : "preparing",
      fileName: "",
      downloadedBytes: 0,
      totalBytes: 0,
      percent: 0,
      message: model.jointTextReady ? `Activating ${model.name}…` : `Preparing download for ${model.name}…`,
    });
    try {
      const res = await selectVisionModel(model.id, true);
      setVisionModels(res.models || []);
      onRefreshState?.(res.state);
      onShowToast?.(`Active vision model: ${model.name}`);
    } catch (err) {
      onShowToast?.(err instanceof Error ? err.message : "Could not switch vision model");
    } finally {
      setVisionBusyId(null);
      setDownloadProgress(null);
    }
  };

  const handleReindexImages = async () => {
    if (reindexingImages) return;
    setReindexingImages(true);
    try {
      const res = await reindexImagesOnly();
      onRefreshState?.(res.state);
      onShowToast?.(`Re-indexed ${res.reindexed} ${res.reindexed === 1 ? "photo" : "photos"} with current vision model`);
    } catch (err) {
      onShowToast?.(err instanceof Error ? err.message : "Image re-index failed");
    } finally {
      setReindexingImages(false);
    }
  };

  const handleAddFolderSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const target = newFolderInput.trim();
    if (!target) {
      onShowToast?.("Enter a folder path or click Browse");
      return;
    }
    onAddWatchFolder(target);
    setNewFolderInput("");
    setAddingFolder(false);
  };

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-settings"
      aria-hidden={!active}
      inert={!active ? true : undefined}
    >
      <div className="page-container">
        <div className="page-header">
          <div>
            {onBackToHome && (
              <button
                type="button"
                className="page-back-btn"
                onClick={onBackToHome}
                title="Back to Home (Esc)"
                aria-label="Back to Home"
              >
                <ArrowLeft size={13} strokeWidth={1.75} aria-hidden="true" />
                <span>Home</span>
              </button>
            )}
            <h1 className="page-title">Settings</h1>
            <p className="page-subtitle">
              All settings are saved automatically on your local device.
            </p>
          </div>
          <div className="seg-group" role="tablist" aria-label="Settings sections">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "folders"}
              className={`seg-btn ${tab === "folders" ? "active" : ""}`}
              onClick={() => setTab("folders")}
            >
              <span>Folders</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "indexing"}
              className={`seg-btn ${tab === "indexing" ? "active" : ""}`}
              onClick={() => setTab("indexing")}
            >
              <span>Search &amp; Indexing</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "vision"}
              className={`seg-btn ${tab === "vision" ? "active" : ""}`}
              onClick={() => setTab("vision")}
            >
              <span>Photos &amp; Vision</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "storage"}
              className={`seg-btn ${tab === "storage" ? "active" : ""}`}
              onClick={() => setTab("storage")}
            >
              <span>Storage &amp; About</span>
            </button>
          </div>
        </div>

        {/* TAB 1: WATCHED FOLDERS */}
        {tab === "folders" && (
          <div className="shelf-section">
            <div className="shelf-section-header">
              <div>
                <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                  Watched folders
                </h2>
                <p style={{ fontSize: "13px", color: "var(--ink-2)", marginTop: "2px" }}>
                  Good Shelves monitors these folders locally and keeps your shelves up to date automatically.
                </p>
              </div>
              <span className="shelf-ledger-count">{watchedFolders.length} folders</span>
            </div>

            {liveProgress && (
              <div
                role="status"
                aria-live="polite"
                className="surface-card"
                style={{
                  padding: "12px 16px",
                  marginBottom: "14px",
                  display: "flex",
                  flexDirection: "column",
                  gap: "6px",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "10px", fontSize: "12.5px" }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: "7px", fontWeight: 500, minWidth: 0 }}>
                    <span className="status-dot pulse" aria-hidden="true" />
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {liveProgress.operation}: {liveProgress.file_name || "Processing files…"}
                    </span>
                  </span>
                  <span className="tabular-nums" style={{ fontSize: "11.5px", color: "var(--ink-2)", flexShrink: 0 }}>
                    {liveProgress.total > 1
                      ? `${liveProgress.current} / ${liveProgress.total} (${Math.min(100, Math.round((liveProgress.current / liveProgress.total) * 100))}%)`
                      : "Working…"}
                  </span>
                </div>
                <div className="live-indexing-track">
                  <div
                    className="live-indexing-fill"
                    style={{
                      width: `${liveProgress.total > 1 ? Math.max(6, Math.min(100, Math.round((liveProgress.current / liveProgress.total) * 100))) : 35}%`,
                    }}
                  />
                </div>
              </div>
            )}

            <div id="settingsFoldersWrap" className="shelf-list-surface" style={{ marginBottom: "14px" }}>
              {watchedFolders.map((f, i) => (
                <div
                  key={f.path}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: "12px",
                    padding: "12px 16px",
                    background: "var(--bg-surface)",
                    borderTop: i > 0 ? "1px solid var(--rule-soft)" : "none",
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: "13.5px", fontWeight: 500, wordBreak: "break-all" }}>
                      {f.display_path}
                    </div>
                    <div className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "2px" }}>
                      {f.files} files ·{" "}
                      <span style={{ color: f.active ? "var(--success)" : "var(--warning)", fontWeight: 500 }}>
                        {f.active ? "Watching" : "Paused"}
                      </span>
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: "8px", flexShrink: 0 }}>
                    <button
                      type="button"
                      className="btn-tactile sm"
                      disabled={togglingFolderPath === f.path || removingFolderPath === f.path}
                      onClick={async () => {
                        if (togglingFolderPath) return;
                        setTogglingFolderPath(f.path);
                        try {
                          await onToggleWatchFolder(f.path);
                        } finally {
                          setTogglingFolderPath(null);
                        }
                      }}
                    >
                      {togglingFolderPath === f.path ? "Updating…" : f.active ? "Pause" : "Resume"}
                    </button>
                    {onRemoveWatchFolder && (
                      <button
                        type="button"
                        className="btn-tactile sm danger"
                        disabled={removingFolderPath === f.path || togglingFolderPath === f.path}
                        onClick={async () => {
                          if (removingFolderPath) return;
                          setRemovingFolderPath(f.path);
                          try {
                            await onRemoveWatchFolder(f.path);
                          } finally {
                            setRemovingFolderPath(null);
                          }
                        }}
                      >
                        {removingFolderPath === f.path ? "Removing…" : "Remove"}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>

            {addingFolder ? (
              <form onSubmit={handleAddFolderSubmit} style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
                <input
                  type="text"
                  aria-label="Folder path to watch"
                  value={newFolderInput}
                  onChange={(e) => setNewFolderInput(e.target.value)}
                  placeholder={`Enter folder path (e.g. ${defaultExamplePath("Pictures")})…`}
                  className="dialog-input"
                  style={{ flex: 1, fontSize: "13px" }}
                  autoFocus
                />
                <button
                  type="button"
                  className="btn-tactile sm"
                  onClick={async () => {
                    const picked = await pickFolderNative();
                    if (picked) {
                      onAddWatchFolder(picked);
                      setAddingFolder(false);
                    }
                  }}
                >
                  <span>Browse…</span>
                </button>
                <button type="submit" className="btn-tactile sm solid">Add</button>
                <button type="button" className="btn-tactile sm" onClick={() => setAddingFolder(false)}>Cancel</button>
              </form>
            ) : (
              <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                <button type="button" className="btn-tactile solid" onClick={() => setAddingFolder(true)}>
                  + Add folder to watch
                </button>
                {!watchedFolders.some((w) => w.display_path.includes("Pictures")) && (
                  <button type="button" className="btn-tactile" onClick={() => onAddWatchFolder("~/Pictures")}>
                    + Watch Pictures folder
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {/* TAB 2: SEARCH & INDEXING */}
        {tab === "indexing" && (
          <div className="shelf-section">
            <div className="surface-card" style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              {[
                {
                  key: "realtime_watch",
                  title: "Watch folders in real time",
                  desc: "Index new or updated files immediately when saved to disk.",
                  checked: settings.realtime_watch,
                  onToggle: () => onUpdateSetting("realtime_watch", !settings.realtime_watch),
                },
                {
                  key: "ocr_enabled",
                  title: "Read text inside images (OCR)",
                  desc: "Extract words from receipts, screenshots, and scanned documents.",
                  checked: settings.ocr_enabled,
                  onToggle: () => onUpdateSetting("ocr_enabled", !settings.ocr_enabled),
                },
                {
                  key: "code_indexing",
                  title: "Index source code & data files",
                  desc: "Parse Python, TypeScript, Rust, JSON, and CSV structures.",
                  checked: settings.code_indexing,
                  onToggle: () => onUpdateSetting("code_indexing", !settings.code_indexing),
                },
                {
                  key: "battery_pause",
                  title: "Pause indexing on battery power",
                  desc: "Save laptop battery while unplugged.",
                  checked: settings.battery_pause,
                  onToggle: () => onUpdateSetting("battery_pause", !settings.battery_pause),
                },
                {
                  key: "trash_mode",
                  title: "Use native OS Recycle Bin / Trash",
                  desc: "When off, removed files go to local reversible trash for instant undo & recovery.",
                  checked: settings.trash_mode === "system",
                  onToggle: () =>
                    onUpdateSetting("trash_mode", settings.trash_mode === "system" ? "reversible" : "system"),
                },
                {
                  key: "ollama_enabled",
                  title: "Use local Ollama for Ask AI answers",
                  desc: "Optional: route file excerpts to a local Ollama server when running.",
                  checked: Boolean(settings.ollama_enabled),
                  onToggle: () => onUpdateSetting("ollama_enabled", !settings.ollama_enabled),
                },
                {
                  key: "local_telemetry",
                  title: "Local performance log",
                  desc: "Records only search latency and action counts on this device to guide improvements. Nothing leaves your computer.",
                  checked: Boolean(settings.local_telemetry),
                  onToggle: () => onUpdateSetting("local_telemetry", !settings.local_telemetry),
                },
              ].map((item, idx) => (
                <div
                  key={item.key}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "16px",
                    paddingTop: idx > 0 ? "14px" : "0",
                    borderTop: idx > 0 ? "1px solid var(--rule-soft)" : "none",
                  }}
                >
                  <div>
                    <div style={{ fontWeight: 500, fontSize: "14px" }}>{item.title}</div>
                    <div style={{ fontSize: "13px", color: "var(--ink-2)" }}>{item.desc}</div>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={item.checked}
                    aria-label={item.title}
                    className={`mono-switch ${item.checked ? "on" : ""}`}
                    onClick={item.onToggle}
                  />
                </div>
              ))}

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                  gap: "16px",
                  paddingTop: "14px",
                  borderTop: "1px solid var(--rule-soft)",
                }}
              >
                <div>
                  <label htmlFor="settingMaxScanFiles" style={{ display: "block", fontSize: "13px", fontWeight: 500, marginBottom: "6px" }}>
                    Initial folder scan cap
                  </label>
                  <input
                    id="settingMaxScanFiles"
                    type="number"
                    min={10}
                    max={5000}
                    step={50}
                    value={draftMaxScan}
                    onChange={(e) => {
                      const nextVal = parseInt(e.target.value, 10);
                      setDraftMaxScan(Number.isNaN(nextVal) ? 10 : nextVal);
                    }}
                    onBlur={() => {
                      const clamped = Math.max(10, Math.min(5000, Math.round(draftMaxScan || 500)));
                      setDraftMaxScan(clamped);
                      if (clamped !== (settings.max_scan_files ?? 500)) {
                        onUpdateSetting("max_scan_files", clamped);
                      }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") e.currentTarget.blur();
                    }}
                    className="dialog-input"
                    style={{ fontSize: "13px" }}
                  />
                </div>

                <div>
                  <label htmlFor="settingIgnorePatterns" style={{ display: "block", fontSize: "13px", fontWeight: 500, marginBottom: "6px" }}>
                    Ignored folder names
                  </label>
                  <input
                    id="settingIgnorePatterns"
                    type="text"
                    value={draftIgnores}
                    onChange={(e) => setDraftIgnores(e.target.value)}
                    onBlur={() => {
                      const trimmed = draftIgnores.trim();
                      setDraftIgnores(trimmed);
                      if (trimmed !== (settings.ignore_patterns ?? ".git,node_modules,target,dist,build,.venv,__pycache__")) {
                        onUpdateSetting("ignore_patterns", trimmed);
                      }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") e.currentTarget.blur();
                    }}
                    placeholder=".git,node_modules,target,dist,build"
                    className="dialog-input"
                    style={{ fontSize: "13px" }}
                  />
                </div>
              </div>

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                  gap: "16px",
                  paddingTop: "14px",
                  borderTop: "1px solid var(--rule-soft)",
                }}
              >
                <div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", fontWeight: 500, marginBottom: "6px" }}>
                    <span>Minimum similarity threshold</span>
                    <span className="tabular-nums">{Math.round(draftSimilarity * 100)}%</span>
                  </div>
                  <input
                    type="range"
                    aria-label="Minimum similarity threshold"
                    min={0.1}
                    max={0.75}
                    step={0.02}
                    value={draftSimilarity}
                    onChange={(e) => setDraftSimilarity(parseFloat(e.target.value))}
                    onMouseUp={() => {
                      if (draftSimilarity !== settings.min_similarity) {
                        onUpdateSetting("min_similarity", draftSimilarity);
                      }
                    }}
                    onTouchEnd={() => {
                      if (draftSimilarity !== settings.min_similarity) {
                        onUpdateSetting("min_similarity", draftSimilarity);
                      }
                    }}
                    onKeyUp={() => {
                      if (draftSimilarity !== settings.min_similarity) {
                        onUpdateSetting("min_similarity", draftSimilarity);
                      }
                    }}
                    style={{ width: "100%", accentColor: "var(--ink)" }}
                  />
                </div>

                <div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", fontWeight: 500, marginBottom: "6px" }}>
                    <span>Max search results</span>
                    <span className="tabular-nums">{draftMaxResults}</span>
                  </div>
                  <input
                    type="range"
                    aria-label="Max search results"
                    min={10}
                    max={100}
                    step={5}
                    value={draftMaxResults}
                    onChange={(e) => setDraftMaxResults(parseInt(e.target.value, 10))}
                    onMouseUp={() => {
                      if (draftMaxResults !== settings.max_results) {
                        onUpdateSetting("max_results", draftMaxResults);
                      }
                    }}
                    onTouchEnd={() => {
                      if (draftMaxResults !== settings.max_results) {
                        onUpdateSetting("max_results", draftMaxResults);
                      }
                    }}
                    onKeyUp={() => {
                      if (draftMaxResults !== settings.max_results) {
                        onUpdateSetting("max_results", draftMaxResults);
                      }
                    }}
                    style={{ width: "100%", accentColor: "var(--ink)" }}
                  />
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 3: PHOTOS & VISION */}
        {tab === "vision" && (
          <div className="shelf-section">
            <div className="shelf-section-header">
              <div>
                <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                  Photos &amp; Vision
                </h2>
                <p style={{ fontSize: "13px", color: "var(--ink-2)", marginTop: "2px" }}>
                  Choose how deeply photos are scanned for scenes and objects (e.g. “sunset”, “receipt”, “portrait”).
                </p>
              </div>
            </div>

            <div className="shelf-list-surface" style={{ marginBottom: "14px" }}>
              {visionModels.map((vm, idx) => {
                const isBusy = visionBusyId === vm.id;
                const prog = isBusy && downloadProgress?.modelId === vm.id ? downloadProgress : null;
                const hasByteTotal = Boolean(prog && prog.totalBytes > 1024 * 1024);
                const pct = prog ? Math.max(0, Math.min(100, Math.round(prog.percent || 0))) : 0;
                return (
                  <div
                    key={vm.id}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: "10px",
                      padding: "14px 16px",
                      borderTop: idx > 0 ? "1px solid var(--rule-soft)" : "none",
                      background: vm.active ? "var(--bg-subtle)" : "var(--bg-surface)",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: "12px",
                      }}
                    >
                      <div style={{ minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                          <span style={{ fontWeight: 600, fontSize: "14px" }}>{vm.name}</span>
                          <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                            {vm.sizeLabel} · {vm.speedLabel}
                          </span>
                          {(vm.jointTextReady || (vm.installed && vm.active)) && (
                            <span
                              style={{
                                fontSize: "11px",
                                padding: "1px 7px",
                                borderRadius: "var(--radius-xs)",
                                border: "1px solid var(--success-border)",
                                background: "var(--success-bg)",
                                color: "var(--success)",
                                fontWeight: 600,
                              }}
                            >
                              {vm.jointTextReady ? "Ready offline" : "Bundled"}
                            </span>
                          )}
                        </div>
                        <div style={{ fontSize: "12.5px", color: "var(--ink-2)", marginTop: "3px" }}>{vm.description}</div>
                      </div>
                      <button
                        type="button"
                        className={`btn-tactile sm ${vm.active ? "solid" : ""}`}
                        disabled={Boolean(visionBusyId) || (vm.active && vm.jointTextReady)}
                        onClick={() => handleSelectVisionModel(vm)}
                        style={{ flexShrink: 0 }}
                      >
                        {isBusy
                          ? vm.jointTextReady
                            ? "Activating…"
                            : pct > 0
                            ? `Downloading ${pct}%`
                            : "Downloading…"
                          : vm.active && vm.jointTextReady
                          ? "Active"
                          : vm.active && vm.installed
                          ? "Add text search"
                          : vm.jointTextReady
                          ? "Activate"
                          : "Download & use"}
                      </button>
                    </div>

                    {prog && (
                      <div
                        role="progressbar"
                        aria-label={`Download progress for ${vm.name}`}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={pct}
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: "6px",
                          paddingTop: "8px",
                          borderTop: "1px solid var(--rule-soft)",
                        }}
                      >
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "10px", fontSize: "12px" }}>
                          <span style={{ display: "inline-flex", alignItems: "center", gap: "7px", color: "var(--ink)", fontWeight: 500, minWidth: 0 }}>
                            <span className="status-dot pulse" aria-hidden="true" />
                            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              {prog.message || `Downloading ${vm.name}…`}
                            </span>
                          </span>
                          <span className="tabular-nums" style={{ color: "var(--ink-2)", flexShrink: 0 }}>
                            {hasByteTotal
                              ? `${(prog.downloadedBytes / (1024 * 1024)).toFixed(1)} / ${(prog.totalBytes / (1024 * 1024)).toFixed(1)} MB (${pct}%)`
                              : pct > 0
                              ? `${pct}%`
                              : "Connecting…"}
                          </span>
                        </div>
                        <div className="live-indexing-track" style={{ height: "6px" }}>
                          <div
                            className={`live-indexing-fill ${pct === 0 ? "indeterminate" : ""}`}
                            style={{
                              width: pct > 0 ? `${Math.max(4, pct)}%` : "35%",
                            }}
                          />
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="surface-card" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "10px" }}>
              <span style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                After switching models, re-index photos to refresh visual search.
              </span>
              <button type="button" className="btn-tactile sm" disabled={reindexingImages} onClick={handleReindexImages}>
                {reindexingImages ? "Re-indexing photos…" : "Re-index photos"}
              </button>
            </div>
          </div>
        )}

        {/* TAB 4: STORAGE & ABOUT */}
        {tab === "storage" && (
          <div>
            <div className="shelf-section">
              <div className="shelf-section-header">
                <div>
                  <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                    Saved routing rules ({aiMemory.length})
                  </h2>
                  <p style={{ fontSize: "13px", color: "var(--ink-2)", marginTop: "2px" }}>
                    Routing preferences saved locally from your confirmed moves, tags, and remembered instructions.
                  </p>
                </div>
                {aiMemory.length > 0 && onClearAiMemory && (
                  <button
                    type="button"
                    className="btn-tactile sm danger"
                    onClick={() =>
                      runConfirmed(
                        "clear-memory",
                        () => onClearAiMemory(),
                        "Clear rules"
                      )
                    }
                  >
                    {confirmKey === "clear-memory" ? "Confirm Clear" : "Clear rules"}
                  </button>
                )}
              </div>
              <div className="shelf-list-surface">
                {aiMemory.length === 0 ? (
                  <div style={{ padding: "20px", fontSize: "13px", color: "var(--ink-3)" }}>
                    No saved rules yet. Confirm file actions or tell Ask AI what to remember.
                  </div>
                ) : (
                  aiMemory.slice(0, 24).map((mem, idx) => (
                    <div
                      key={mem.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: "10px",
                        padding: "10px 14px",
                        borderTop: idx > 0 ? "1px solid var(--rule-soft)" : "none",
                        background: "var(--bg-surface)",
                      }}
                    >
                      <div style={{ minWidth: 0, fontSize: "12.5px" }}>
                        <span style={{ fontWeight: 600, color: "var(--ink-3)", marginRight: "6px" }}>{mem.kind}</span>
                        <span style={{ color: "var(--ink)" }}>{mem.keyPattern}</span>
                        <span style={{ color: "var(--ink-3)", margin: "0 6px" }}>→</span>
                        <span style={{ color: "var(--ink-2)" }}>{mem.value}</span>
                      </div>
                      {onDeleteAiMemory && (
                        <button
                          type="button"
                          className="btn-tactile sm"
                          onClick={() => void onDeleteAiMemory(mem.id)}
                        >
                          Remove
                        </button>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>

            <div className="shelf-section">
              <div className="shelf-section-header">
                <div>
                  <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                    Continuous AI Data Feeder &amp; Calibration
                  </h2>
                  <p style={{ fontSize: "13px", color: "var(--ink-2)", marginTop: "2px" }}>
                    Seed foundational domain preferences and run the 66-case multimodal benchmark (Search, Entities, Actions, Deep File Context, and Adaptive Learning).
                  </p>
                </div>
                <button
                  type="button"
                  className="btn-tactile sm solid"
                  disabled={trainingAi}
                  onClick={async () => {
                    if (trainingAi) return;
                    setTrainingAi(true);
                    try {
                      const res = await trainAndCalibrateAi(false);
                      setCalibrationReport(res.report);
                      onRefreshState?.(res.state);
                      onShowToast?.(
                        `AI Calibrated: ${res.report.overall_score_pct.toFixed(1)}% (${res.report.passed_cases}/${res.report.total_cases} cases passed)`
                      );
                    } catch (err) {
                      onShowToast?.(err instanceof Error ? err.message : "Calibration failed");
                    } finally {
                      setTrainingAi(false);
                    }
                  }}
                >
                  {trainingAi ? "Feeding & evaluating…" : "Run AI calibration"}
                </button>
              </div>
              {calibrationReport && (
                <div className="surface-card" style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "10px" }}>
                    <div>
                      <span className="font-display" style={{ fontSize: "16px", fontWeight: 600, color: "var(--ink)" }}>
                        {calibrationReport.overall_score_pct.toFixed(1)}% Overall Accuracy ({calibrationReport.passed_cases}/{calibrationReport.total_cases} passed)
                      </span>
                    </div>
                    <span className="tabular-nums" style={{ fontSize: "12px", color: "var(--ink-2)" }}>
                      P@1: {calibrationReport.retrieval_metrics["P@1"].toFixed(3)} · P@5: {calibrationReport.retrieval_metrics["P@5"].toFixed(3)} · MRR: {calibrationReport.retrieval_metrics.MRR.toFixed(3)} · {Math.round(calibrationReport.elapsed_ms)} ms
                    </span>
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(175px, 1fr))", gap: "8px" }}>
                    {Object.entries(calibrationReport.category_scores).map(([cat, info]) => (
                      <div
                        key={cat}
                        style={{
                          padding: "8px 10px",
                          borderRadius: "6px",
                          border: "1px solid var(--rule)",
                          background: "var(--bg-subtle)",
                        }}
                      >
                        <div style={{ fontSize: "11.5px", color: "var(--ink-3)", textTransform: "capitalize" }}>
                          {cat.replace(/_/g, " ")}
                        </div>
                        <div className="tabular-nums" style={{ fontSize: "13.5px", fontWeight: 600, color: "var(--ink)", marginTop: "2px" }}>
                          {info.score_pct.toFixed(1)}% ({info.passed}/{info.total})
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className="shelf-section">
              <div className="shelf-section-header">
                <div>
                  <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                    Local index storage
                  </h2>
                  <p style={{ fontSize: "13px", color: "var(--ink-2)", marginTop: "2px" }}>
                    Your local index uses <strong className="tabular-nums" style={{ color: "var(--ink)" }}>{storageStats.dbSizeMB} MB</strong> across{" "}
                    <strong className="tabular-nums" style={{ color: "var(--ink)" }}>{storageStats.indexedCount} indexed files</strong> at{" "}
                    <span style={{ fontSize: "12.5px", color: "var(--ink)", wordBreak: "break-all" }}>
                      {storageStats.dbPath}
                    </span>
                  </p>
                </div>
              </div>

              <div className="surface-card" style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
                <div>
                  <div style={{ fontSize: "11px", fontWeight: 600, letterSpacing: "0.05em", textTransform: "uppercase", color: "var(--ink-3)", marginBottom: "10px" }}>
                    Index
                  </div>
                  <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                    <button
                      type="button"
                      className="btn-tactile sm"
                      disabled={rescanning || verifying}
                      onClick={async () => {
                        if (rescanning) return;
                        setRescanning(true);
                        try {
                          await onRescanFolders(false);
                        } finally {
                          setRescanning(false);
                        }
                      }}
                    >
                      {rescanning ? "Rescanning…" : "Rescan watched folders"}
                    </button>
                    <button
                      type="button"
                      className="btn-tactile sm"
                      disabled={verifying || rescanning}
                      onClick={async () => {
                        if (verifying) return;
                        setVerifying(true);
                        try {
                          await onVerifyIndex();
                        } finally {
                          setVerifying(false);
                        }
                      }}
                    >
                      {verifying ? "Verifying…" : "Verify index"}
                    </button>
                    <button
                      type="button"
                      className="btn-tactile sm solid"
                      disabled={rescanning || verifying}
                      onClick={() =>
                        runConfirmed(
                          "index-all",
                          async () => {
                            if (rescanning) return;
                            setRescanning(true);
                            try {
                              await onRescanFolders(true);
                            } finally {
                              setRescanning(false);
                            }
                          },
                          "Index all files"
                        )
                      }
                    >
                      {rescanning ? "Indexing all…" : "Index all files"}
                    </button>
                  </div>
                </div>

                <div style={{ borderTop: "1px solid var(--rule-soft)", paddingTop: "14px" }}>
                  <div style={{ fontSize: "11px", fontWeight: 600, letterSpacing: "0.05em", textTransform: "uppercase", color: "var(--ink-3)", marginBottom: "10px" }}>
                    Backup &amp; reset
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                    <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                      <button
                        type="button"
                        className="btn-tactile sm"
                        disabled={exporting || rescanning || verifying}
                        onClick={async () => {
                          if (exporting) return;
                          setExporting(true);
                          try {
                            const res = await exportIndex();
                            onShowToast?.(`Index exported · ${res.size_human} → ${res.path}`);
                          } catch {
                            onShowToast?.("Could not export index");
                          } finally {
                            setExporting(false);
                          }
                        }}
                      >
                        {exporting ? "Exporting…" : "Export backup"}
                      </button>
                      {storageStats.dbPathFull && (
                        <button
                          type="button"
                          className="btn-tactile sm"
                          onClick={() =>
                            revealFileInOS(storageStats.dbPathFull).catch(() =>
                              onShowToast?.(`Data folder: ${storageStats.dbPath}`)
                            )
                          }
                        >
                          Open data folder
                        </button>
                      )}
                    </div>
                    {onResetSetup && (
                      <button
                        type="button"
                        className="btn-tactile sm danger"
                        disabled={rescanning || verifying}
                        onClick={() =>
                          runConfirmed(
                            "reset-setup",
                            () => onResetSetup(),
                            "Reset setup"
                          )
                        }
                      >
                        {confirmKey === "reset-setup" ? "Confirm Reset" : "Reset setup"}
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </div>

            <div className="shelf-section">
              <div className="surface-card">
                <div className="font-display" style={{ fontSize: "17px", fontWeight: 500, color: "var(--ink)", marginBottom: "4px" }}>
                  Good Shelves v{APP_VERSION} · Local Semantic File Manager
                </div>
                <p style={{ fontSize: "13px", color: "var(--ink-2)", marginBottom: "14px" }}>
                  Multimodal Embedding-Based Semantic Search and Organization System for Personal File Management.
                  <br />
                  Calaca City Global College · College of Business Administration and Computer Studies · Computer Science Department (Adviser: Prof. Clark Adrian Wick)
                </p>

                <div className="shelf-list-surface">
                  {[
                    { name: "Paul Vaness B. Ramos", role: "System Developer" },
                    { name: "Ishee Margareth B. Esteleydes", role: "Project Manager" },
                    { name: "Ayessa S. Digno", role: "Documentation & Quality Assurance" },
                  ].map((member, idx) => (
                    <div
                      key={member.name}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "baseline",
                        padding: "10px 14px",
                        borderTop: idx > 0 ? "1px solid var(--rule-soft)" : "none",
                        background: "var(--bg-surface)",
                      }}
                    >
                      <span style={{ fontSize: "14px", fontWeight: 500 }}>{member.name}</span>
                      <span style={{ fontSize: "12px", color: "var(--ink-2)" }}>{member.role}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </section>
  );
});
