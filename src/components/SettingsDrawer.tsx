import React, { useEffect, useState } from "react";
import {
  AppSettings,
  StorageStats,
  VisionModelInfo,
  WatchedFolder,
  fetchVisionModels,
  pickFolderNative,
  reindexImagesOnly,
  selectVisionModel,
} from "../lib/tauri-api";

interface SettingsDrawerProps {
  active: boolean;
  watchedFolders: WatchedFolder[];
  settings: AppSettings;
  storageStats: StorageStats;
  onToggleWatchFolder: (path: string) => Promise<void> | void;
  onAddWatchFolder: (path: string) => Promise<void> | void;
  onUpdateSetting: (key: string, value: unknown) => void;
  onRescanFolders: (allFiles?: boolean) => Promise<void> | void;
  onVerifyIndex: () => Promise<void> | void;
  onRefreshState?: (state: any) => void;
  onShowToast?: (msg: string) => void;
}

export const SettingsDrawer: React.FC<SettingsDrawerProps> = ({
  active,
  watchedFolders,
  settings,
  storageStats,
  onToggleWatchFolder,
  onAddWatchFolder,
  onUpdateSetting,
  onRescanFolders,
  onVerifyIndex,
  onRefreshState,
  onShowToast,
}) => {
  const [newFolderInput, setNewFolderInput] = useState<string>("");
  const [addingFolder, setAddingFolder] = useState<boolean>(false);
  const [visionModels, setVisionModels] = useState<VisionModelInfo[]>([]);
  const [visionBusyId, setVisionBusyId] = useState<string | null>(null);
  const [reindexingImages, setReindexingImages] = useState<boolean>(false);
  const [rescanning, setRescanning] = useState<boolean>(false);
  const [verifying, setVerifying] = useState<boolean>(false);
  const [togglingFolderPath, setTogglingFolderPath] = useState<string | null>(null);
  const [draftSimilarity, setDraftSimilarity] = useState<number>(settings.min_similarity);
  const [draftMaxResults, setDraftMaxResults] = useState<number>(settings.max_results);

  useEffect(() => {
    setDraftSimilarity(settings.min_similarity);
  }, [settings.min_similarity]);

  useEffect(() => {
    setDraftMaxResults(settings.max_results);
  }, [settings.max_results]);

  useEffect(() => {
    if (!active) return;
    fetchVisionModels()
      .then((res) => setVisionModels(res.models || []))
      .catch(() => {});
  }, [active]);

  const handleSelectVisionModel = async (model: VisionModelInfo) => {
    if (visionBusyId) return;
    setVisionBusyId(model.id);
    try {
      const res = await selectVisionModel(model.id, true);
      setVisionModels(res.models || []);
      onRefreshState?.(res.state);
      onShowToast?.(`Active vision model: ${model.name}`);
    } catch (err) {
      onShowToast?.(err instanceof Error ? err.message : "Could not switch vision model");
    } finally {
      setVisionBusyId(null);
    }
  };

  const handleReindexImages = async () => {
    if (reindexingImages) return;
    setReindexingImages(true);
    try {
      const res = await reindexImagesOnly();
      onRefreshState?.(res.state);
      onShowToast?.(`Re-indexed ${res.reindexed} photo(s) with current vision model`);
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
      onShowToast?.("Enter a folder path or click browse");
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
    >
      <div style={{ maxWidth: "820px", margin: "0 auto" }}>
        <div
          style={{
            paddingBottom: "20px",
            borderBottom: "1.5px solid var(--ink)",
            marginBottom: "8px",
          }}
        >
          <div
            style={{
              fontSize: "11.5px",
              fontWeight: 500,
              letterSpacing: "0.06em",
              color: "var(--ink-3)",
            }}
          >
            local preferences
          </div>
          <h1
            className="font-display"
            style={{
              fontSize: "clamp(32px, 4.2vw, 48px)",
              fontWeight: 300,
              letterSpacing: "-0.03em",
              lineHeight: 1.02,
            }}
          >
            settings
          </h1>
          <p
            style={{
              fontSize: "15px",
              color: "var(--ink-2)",
              marginTop: "8px",
            }}
          >
            everything runs 100% offline on your computer. no files, filenames,
            or searches ever leave your device.
          </p>
        </div>

        {/* 1. Watched Folders */}
        <div className="skill-row-grid">
          <div
            style={{
              fontSize: "12px",
              fontWeight: 500,
              letterSpacing: "0.05em",
              color: "var(--ink-3)",
            }}
          >
            watched folders
          </div>
          <div>
            <div
              id="settingsFoldersWrap"
              style={{
                border: "1px solid var(--rule)",
                borderRadius: "7px",
                overflow: "hidden",
                marginBottom: "12px",
              }}
            >
              {watchedFolders.map((f, i) => (
                <div
                  key={f.path}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    padding: "12px 16px",
                    background: "var(--bg-subtle)",
                    borderTop: i > 0 ? "1px solid var(--rule)" : "none",
                  }}
                >
                  <div>
                    <span
                      className="font-mono-code"
                      style={{ fontSize: "13.5px", fontWeight: 500 }}
                    >
                      {f.display_path}
                    </span>
                    <span
                      style={{
                        fontSize: "12px",
                        color: "var(--ink-3)",
                        marginLeft: "10px",
                      }}
                    >
                      {f.files} files · {f.active ? "watching" : "paused"}
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: "8px" }}>
                    <button
                      type="button"
                      className="btn-tactile"
                      style={{ padding: "4px 10px", fontSize: "11.5px" }}
                      disabled={togglingFolderPath === f.path}
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
                      {togglingFolderPath === f.path ? "updating…" : f.active ? "pause" : "resume"}
                    </button>
                  </div>
                </div>
              ))}
            </div>

            {addingFolder ? (
              <form
                onSubmit={handleAddFolderSubmit}
                style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}
              >
                <input
                  type="text"
                  value={newFolderInput}
                  onChange={(e) => setNewFolderInput(e.target.value)}
                  placeholder="Enter folder path (e.g. ~/Pictures or ~/Projects)..."
                  className="font-mono-code"
                  style={{
                    flex: 1,
                    padding: "7px 12px",
                    border: "1px solid var(--ink)",
                    borderRadius: "6px",
                    fontSize: "12.5px",
                    background: "var(--bg-canvas)",
                  }}
                  autoFocus
                />
                <button
                  type="button"
                  className="btn-tactile"
                  onClick={async () => {
                    const picked = await pickFolderNative();
                    if (picked) {
                      onAddWatchFolder(picked);
                      setAddingFolder(false);
                    }
                  }}
                >
                  browse…
                </button>
                <button type="submit" className="btn-tactile solid">
                  add
                </button>
                <button
                  type="button"
                  className="btn-tactile"
                  onClick={() => setAddingFolder(false)}
                >
                  cancel
                </button>
              </form>
            ) : (
              <div style={{ display: "flex", gap: "8px" }}>
                <button
                  type="button"
                  className="btn-tactile"
                  onClick={() => setAddingFolder(true)}
                >
                  + add folder to watch
                </button>
                {!watchedFolders.some((w) =>
                  w.display_path.includes("Pictures")
                ) && (
                  <button
                    type="button"
                    className="btn-tactile"
                    onClick={() => onAddWatchFolder("~/Pictures")}
                  >
                    + watch Pictures
                  </button>
                )}
              </div>
            )}
          </div>
        </div>

        {/* 2. Reading & Index Preferences */}
        <div className="skill-row-grid">
          <div
            style={{
              fontSize: "12px",
              fontWeight: 500,
              letterSpacing: "0.05em",
              color: "var(--ink-3)",
            }}
          >
            reading &amp; index
          </div>
          <div
            style={{ display: "flex", flexDirection: "column", gap: "16px" }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "16px",
              }}
            >
              <div>
                <div style={{ fontWeight: 500, fontSize: "14px" }}>
                  watch folders in real time
                </div>
                <div style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  index new files immediately when saved to disk.
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.realtime_watch}
                aria-label="watch folders in real time"
                className={`mono-switch ${
                  settings.realtime_watch ? "on" : ""
                }`}
                onClick={() =>
                  onUpdateSetting("realtime_watch", !settings.realtime_watch)
                }
              />
            </div>

            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "16px",
                paddingTop: "14px",
                borderTop: "1px solid var(--rule)",
              }}
            >
              <div>
                <div style={{ fontWeight: 500, fontSize: "14px" }}>
                  read words inside images (ocr)
                </div>
                <div style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  extract text from receipts, screenshots, and scanned id cards.
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.ocr_enabled}
                aria-label="read words inside images (ocr)"
                className={`mono-switch ${settings.ocr_enabled ? "on" : ""}`}
                onClick={() =>
                  onUpdateSetting("ocr_enabled", !settings.ocr_enabled)
                }
              />
            </div>

            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "16px",
                paddingTop: "14px",
                borderTop: "1px solid var(--rule)",
              }}
            >
              <div>
                <div style={{ fontWeight: 500, fontSize: "14px" }}>
                  index source code scripts
                </div>
                <div style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  parse python, typescript, rust, and csv structures.
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.code_indexing}
                aria-label="index source code scripts"
                className={`mono-switch ${settings.code_indexing ? "on" : ""}`}
                onClick={() =>
                  onUpdateSetting("code_indexing", !settings.code_indexing)
                }
              />
            </div>

            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "16px",
                paddingTop: "14px",
                borderTop: "1px solid var(--rule)",
              }}
            >
              <div>
                <div style={{ fontWeight: 500, fontSize: "14px" }}>
                  pause indexing on battery power
                </div>
                <div style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  save laptop battery while unplugged.
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.battery_pause}
                aria-label="pause indexing on battery power"
                className={`mono-switch ${settings.battery_pause ? "on" : ""}`}
                onClick={() =>
                  onUpdateSetting("battery_pause", !settings.battery_pause)
                }
              />
            </div>

            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "16px",
                paddingTop: "14px",
                borderTop: "1px solid var(--rule)",
              }}
            >
              <div>
                <div style={{ fontWeight: 500, fontSize: "14px" }}>
                  use native OS recycle bin / trash
                </div>
                <div style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  off keeps files in GoodShelves_Trash with instant Ctrl+Z undo &amp; recovery tab.
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.trash_mode === "system"}
                aria-label="use native OS recycle bin"
                className={`mono-switch ${settings.trash_mode === "system" ? "on" : ""}`}
                onClick={() =>
                  onUpdateSetting("trash_mode", settings.trash_mode === "system" ? "reversible" : "system")
                }
              />
            </div>

            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "16px",
                paddingTop: "14px",
                borderTop: "1px solid var(--rule)",
              }}
            >
              <div>
                <div style={{ fontWeight: 500, fontSize: "14px" }}>
                  enhance &ldquo;ask your shelves&rdquo; with local Ollama (optional)
                </div>
                <div style={{ fontSize: "13px", color: "var(--ink-2)" }}>
                  uses local Ollama on 127.0.0.1:11434 when running; otherwise 100% deterministic offline extraction.
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={Boolean(settings.ollama_enabled)}
                aria-label="enhance ask with local Ollama"
                className={`mono-switch ${settings.ollama_enabled ? "on" : ""}`}
                onClick={() =>
                  onUpdateSetting("ollama_enabled", !settings.ollama_enabled)
                }
              />
            </div>

            {/* Sensitivity Sliders */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                gap: "16px",
                paddingTop: "14px",
                borderTop: "1px solid var(--rule)",
              }}
            >
              <div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: "13px",
                    fontWeight: 500,
                    marginBottom: "6px",
                  }}
                >
                  <span>minimum similarity threshold</span>
                  <span className="font-mono-code">
                    {Math.round(draftSimilarity * 100)}%
                  </span>
                </div>
                <input
                  type="range"
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
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: "13px",
                    fontWeight: 500,
                    marginBottom: "6px",
                  }}
                >
                  <span>max search results</span>
                  <span className="font-mono-code">{draftMaxResults}</span>
                </div>
                <input
                  type="range"
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

        {/* 2.5. Vision & Image Recognition Model */}
        <div className="skill-row-grid">
          <div
            style={{
              fontSize: "12px",
              fontWeight: 500,
              letterSpacing: "0.05em",
              color: "var(--ink-3)",
            }}
          >
            image recognition
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
            <div style={{ fontSize: "13px", color: "var(--ink-2)", marginBottom: "2px" }}>
              choose a local vision-language model to search photos by scene, mood, or object (e.g. “sunset last sunday”, “cinema”, “mall”).
            </div>
            {visionModels.map((vm) => {
              const isBusy = visionBusyId === vm.id;
              return (
                <div
                  key={vm.id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "12px",
                    padding: "12px 14px",
                    borderRadius: "7px",
                    border: vm.active ? "1px solid var(--ink)" : "1px solid var(--rule)",
                    background: vm.active ? "var(--bg-subtle)" : "var(--bg-canvas)",
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                      <span style={{ fontWeight: 500, fontSize: "13.5px" }}>{vm.name}</span>
                      <span className="font-mono-code" style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                        {vm.sizeLabel} · {vm.speedLabel}
                      </span>
                      {(vm.jointTextReady || (vm.installed && vm.active)) && (
                        <span
                          className="font-mono-code"
                          style={{
                            fontSize: "10.5px",
                            padding: "1px 6px",
                            borderRadius: "4px",
                            border: "1px solid var(--rule-strong)",
                            color: "var(--ink-2)",
                          }}
                        >
                          {vm.jointTextReady ? "ready offline" : "vision bundled"}
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: "12.5px", color: "var(--ink-2)", marginTop: "2px" }}>
                      {vm.description}
                    </div>
                  </div>
                  <button
                    type="button"
                    className={`btn-tactile ${vm.active ? "solid" : ""}`}
                    disabled={Boolean(visionBusyId) || (vm.active && vm.jointTextReady)}
                    onClick={() => handleSelectVisionModel(vm)}
                    style={{ flexShrink: 0 }}
                  >
                    {isBusy
                      ? vm.jointTextReady
                        ? "activating…"
                        : "downloading…"
                      : vm.active && vm.jointTextReady
                      ? "active"
                      : vm.active && vm.installed
                      ? "active · add text pair"
                      : vm.jointTextReady
                      ? "activate"
                      : "download & use"}
                  </button>
                </div>
              );
            })}
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", paddingTop: "4px" }}>
              <span style={{ fontSize: "12px", color: "var(--ink-3)" }}>
                after switching models, re-index photos to update visual vectors
              </span>
              <button
                type="button"
                className="btn-tactile"
                disabled={reindexingImages}
                onClick={handleReindexImages}
              >
                {reindexingImages ? "re-indexing photos…" : "re-index photos"}
              </button>
            </div>
          </div>
        </div>

        {/* 3. Local Index Storage */}
        <div className="skill-row-grid">
          <div
            style={{
              fontSize: "12px",
              fontWeight: 500,
              letterSpacing: "0.05em",
              color: "var(--ink-3)",
            }}
          >
            local storage
          </div>
          <div>
            <p
              style={{
                fontSize: "14px",
                color: "var(--ink-2)",
                marginBottom: "14px",
              }}
            >
              local vector store uses{" "}
              <strong style={{ color: "var(--ink)" }}>
                {storageStats.dbSizeMB} MB
              </strong>{" "}
              across{" "}
              <strong style={{ color: "var(--ink)" }}>
                {storageStats.indexedCount} indexed files
              </strong>{" "}
              at{" "}
              <span
                className="font-mono-code"
                style={{ fontSize: "12.5px", color: "var(--ink)" }}
              >
                {storageStats.dbPath}
              </span>
            </p>
            <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
              <button
                type="button"
                className="btn-tactile"
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
                {rescanning ? "rescanning folders…" : "rescan watched folders"}
              </button>
              <button
                type="button"
                className="btn-tactile solid"
                disabled={rescanning || verifying}
                onClick={async () => {
                  if (rescanning) return;
                  setRescanning(true);
                  try {
                    await onRescanFolders(true);
                  } finally {
                    setRescanning(false);
                  }
                }}
              >
                {rescanning ? "indexing all…" : "index all files (no cap)"}
              </button>
              <button
                type="button"
                className="btn-tactile"
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
                {verifying ? "verifying index…" : "verify index integrity"}
              </button>
            </div>
          </div>
        </div>

        {/* 4. Team & Project Credits */}
        <div className="skill-row-grid">
          <div
            style={{
              fontSize: "12px",
              fontWeight: 500,
              letterSpacing: "0.05em",
              color: "var(--ink-3)",
            }}
          >
            team &amp; credits
          </div>
          <div>
            <div
              style={{
                fontFamily: "var(--display)",
                fontSize: "18px",
                fontWeight: 400,
                color: "var(--ink)",
                marginBottom: "4px",
              }}
            >
              multimodal embedding-based semantic search and organization system
              for personal file management
            </div>
            <p
              style={{
                fontSize: "13.5px",
                color: "var(--ink-2)",
                marginBottom: "18px",
              }}
            >
              calaca city global college · college of business administration
              and computer studies · computer science department · prol301
              (adviser: prof. clark adrian wick)
            </p>

            <div
              style={{
                border: "1px solid var(--rule)",
                borderRadius: "7px",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                  padding: "12px 16px",
                  borderBottom: "1px solid var(--rule)",
                  background: "var(--bg-subtle)",
                }}
              >
                <span
                  className="font-display"
                  style={{ fontSize: "16px", fontWeight: 400 }}
                >
                  paul vaness b. ramos
                </span>
                <span
                  style={{
                    fontSize: "12px",
                    color: "var(--ink-2)",
                    fontWeight: 500,
                  }}
                >
                  system developer
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                  padding: "12px 16px",
                  borderBottom: "1px solid var(--rule)",
                  background: "var(--bg-subtle)",
                }}
              >
                <span
                  className="font-display"
                  style={{ fontSize: "16px", fontWeight: 400 }}
                >
                  ishee margareth b. esteleydes
                </span>
                <span
                  style={{
                    fontSize: "12px",
                    color: "var(--ink-2)",
                    fontWeight: 500,
                  }}
                >
                  project manager
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                  padding: "12px 16px",
                  background: "var(--bg-subtle)",
                }}
              >
                <span
                  className="font-display"
                  style={{ fontSize: "16px", fontWeight: 400 }}
                >
                  ayessa s. digno
                </span>
                <span
                  style={{
                    fontSize: "12px",
                    color: "var(--ink-2)",
                    fontWeight: 500,
                  }}
                >
                  documentation &amp; quality assurance
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
};
