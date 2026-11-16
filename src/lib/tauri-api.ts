import { invoke, convertFileSrc } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export interface ShelfFile {
  id: string;
  path: string;
  name: string;
  originalName: string;
  suggestedName: string | null;
  renameReason: string | null;
  renamed: boolean;
  title: string;
  format: string;
  modality: "image" | "document" | "code" | "video" | "audio";
  folder: string;
  folderFull: string;
  size: string;
  sizeBytes: number;
  date: string;
  createdDate: string;
  mtime: number;
  sha256: string;
  shelfId: string;
  shelfName: string;
  clusterId: number;
  matchScore: number;
  cosine: string;
  vectorNorm: string;
  whyMatched: string;
  visualShare: number;
  textShare: number;
  extractedText: string;
  ocrText: string;
  ocrConfidence: number;
  specimen: "receipt" | "diagram" | "code" | "idcard" | "text";
  width: number;
  height: number;
  pageCount: number;
  mapX: number;
  mapY: number;
  thumbnailB64?: string | null;
  metadata?: Record<string, any>;
  tags?: string[];
  pinned?: boolean;
}

export interface ShelfGroup {
  id: string;
  clusterId: number;
  num: string;
  name: string;
  desc: string;
  centroidX: number;
  centroidY: number;
  pinned?: boolean;
}

export interface SavedSearch {
  id: string;
  name: string;
  query: string;
  modality: string;
  tag: string;
  createdAt: number;
}

export interface LookalikeDiffItem {
  label: string;
  keep: string;
  copy: string;
}

export interface LookalikePair {
  id: string;
  title: string;
  similarity: number;
  similarity_pct: number;
  badge: string;
  desc: string;
  keep: {
    id: string;
    name: string;
    path: string;
    meta: string;
  };
  copy: {
    id: string;
    name: string;
    path: string;
    meta: string;
  };
  saved_bytes: number;
  saved_mb: number;
  resolved: boolean;
  archived_path?: string | null;
  diffs: LookalikeDiffItem[];
  unified_diff?: string[];
}

export interface WatchedFolder {
  path: string;
  display_path: string;
  files: number;
  active: boolean;
}

export interface ConnectedDevice {
  id: string;
  name: string;
  path: string;
  kind: "phone" | "usb" | "sd" | "external";
  sizeLabel: string;
  alreadyWatched: boolean;
  mtpVirtual?: boolean;
}

export interface ActionHistoryItem {
  id: number;
  actionType: string;
  summary: string;
  detail: Record<string, any>;
  timestamp: number;
  undone: boolean;
}

export interface SmartCleanPreview {
  renames: Array<{
    fileId: string;
    fromName: string;
    toName: string;
    folder: string;
    reason: string;
  }>;
  duplicates: Array<{
    pairId: string;
    keepName: string;
    copyName: string;
    savedMB: number;
    badge: string;
  }>;
  moves: TidyMovePreview[];
  totalSavedMB: number;
}

export interface AppSettings {
  realtime_watch: boolean;
  ocr_enabled: boolean;
  code_indexing: boolean;
  battery_pause: boolean;
  max_results: number;
  min_similarity: number;
  silhouette_score: number;
  setup_completed?: boolean;
  vision_model?: string;
}

export interface PipelineStageEvent {
  type?: "stage" | "progress" | "devices_changed";
  stage?: number;
  stage_name?: string;
  status?: string;
  file_name?: string;
  file_id?: string | null;
  shelf_name?: string | null;
  message?: string;
  elapsed_ms?: number;
  operation?: string;
  current?: number;
  total?: number;
  devices?: ConnectedDevice[];
  timestamp: number;
}

export interface RenameHistoryItem {
  id: number;
  file_id: string;
  old_path: string;
  new_path: string;
  old_name: string;
  new_name: string;
  timestamp: number;
  undone: number;
}

export interface StorageStats {
  dbPath: string;
  dbPathFull: string;
  dbSizeMB: number;
  indexedCount: number;
}

export interface WorkspaceState {
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
  querySuggestions: string[];
  recentEvents: PipelineStageEvent[];
  storageStats: StorageStats;
}

export interface SearchResponse {
  results: ShelfFile[];
  latencyMs: number;
  query: string;
}

export interface TidyMovePreview {
  fileId: string;
  fileName: string;
  fromShort: string;
  toShort: string;
  fromPath: string;
  toPath: string;
}

const SIDECAR_BASE = "http://127.0.0.1:18420";

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function sidecarGet<T>(endpoint: string): Promise<T> {
  const res = await fetch(`${SIDECAR_BASE}${endpoint}`);
  if (!res.ok) {
    throw new Error(`Sidecar GET ${endpoint} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function sidecarPost<T>(endpoint: string, body: Record<string, unknown> = {}): Promise<T> {
  const res = await fetch(`${SIDECAR_BASE}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`Sidecar POST ${endpoint} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function fetchWorkspaceState(): Promise<WorkspaceState> {
  if (isTauriRuntime()) {
    try {
      return await invoke<WorkspaceState>("get_workspace_state");
    } catch {
      return sidecarGet<WorkspaceState>("/state");
    }
  }
  return sidecarGet<WorkspaceState>("/state");
}

export async function runSemanticSearch(
  query: string,
  modality: string = "all",
  limit?: number,
  minSimilarity?: number
): Promise<SearchResponse> {
  if (isTauriRuntime()) {
    try {
      return await invoke<SearchResponse>("semantic_search", {
        query,
        modality,
        limit,
        minSimilarity,
      });
    } catch {
      return sidecarPost<SearchResponse>("/search", { query, modality, limit, minSimilarity });
    }
  }
  return sidecarPost<SearchResponse>("/search", { query, modality, limit, minSimilarity });
}

export async function toggleFileRename(
  fileId: string
): Promise<{ ok: boolean; action: string; file: ShelfFile; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    try {
      return await invoke("toggle_rename", { fileId });
    } catch {
      return sidecarPost("/rename/toggle", { fileId });
    }
  }
  return sidecarPost("/rename/toggle", { fileId });
}

export async function applyAllSuggestedRenames(): Promise<{
  ok: boolean;
  appliedCount: number;
  state: WorkspaceState;
}> {
  if (isTauriRuntime()) {
    try {
      return await invoke("apply_all_renames");
    } catch {
      return sidecarPost("/rename/apply-all", {});
    }
  }
  return sidecarPost("/rename/apply-all", {});
}

export async function toggleResolveLookalikePair(
  pairId: string
): Promise<{ ok: boolean; resolved: boolean; savedMB?: number; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    try {
      return await invoke("resolve_lookalike", { pairId });
    } catch {
      return sidecarPost("/lookalikes/resolve", { pairId });
    }
  }
  return sidecarPost("/lookalikes/resolve", { pairId });
}

export async function fetchTidyMovesPreview(): Promise<{ moves: TidyMovePreview[] }> {
  if (isTauriRuntime()) {
    try {
      return await invoke("preview_tidy_folders");
    } catch {
      return sidecarGet("/tidy-folders/preview");
    }
  }
  return sidecarGet("/tidy-folders/preview");
}

export async function executeTidyFoldersOnDisk(): Promise<{
  ok: boolean;
  batchId: string;
  movedCount: number;
  state: WorkspaceState;
}> {
  if (isTauriRuntime()) {
    try {
      return await invoke("execute_tidy_folders");
    } catch {
      return sidecarPost("/tidy-folders/execute", {});
    }
  }
  return sidecarPost("/tidy-folders/execute", {});
}

export async function rollbackTidyFoldersOnDisk(): Promise<{
  ok: boolean;
  restoredCount: number;
  state: WorkspaceState;
}> {
  if (isTauriRuntime()) {
    try {
      return await invoke("rollback_tidy_folders");
    } catch {
      return sidecarPost("/tidy-folders/rollback", {});
    }
  }
  return sidecarPost("/tidy-folders/rollback", {});
}

export async function addWatchedFolderPath(path: string): Promise<{ ok: boolean; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    try {
      return await invoke("add_watched_folder", { path });
    } catch {
      return sidecarPost("/folders/add", { path });
    }
  }
  return sidecarPost("/folders/add", { path });
}

export async function toggleWatchedFolderPath(
  path: string
): Promise<{ ok: boolean; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    try {
      return await invoke("toggle_watched_folder", { path });
    } catch {
      return sidecarPost("/folders/toggle", { path });
    }
  }
  return sidecarPost("/folders/toggle", { path });
}

export async function updateAppSetting(
  key: string,
  value: unknown
): Promise<{ ok: boolean; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    try {
      return await invoke("update_setting", { key, value });
    } catch {
      return sidecarPost("/settings/update", { key, value });
    }
  }
  return sidecarPost("/settings/update", { key, value });
}

export async function rescanAllWatchedFolders(): Promise<{
  ok: boolean;
  scannedCount: number;
  state: WorkspaceState;
}> {
  if (isTauriRuntime()) {
    try {
      return await invoke("rescan_folders");
    } catch {
      return sidecarPost("/index/rescan", {});
    }
  }
  return sidecarPost("/index/rescan", {});
}

export async function verifyLocalIndexIntegrity(): Promise<{
  ok: boolean;
  integrity: { activeFiles: number; vectorRows: number; orphansRemoved: number };
  state: WorkspaceState;
}> {
  if (isTauriRuntime()) {
    try {
      return await invoke("verify_index");
    } catch {
      return sidecarPost("/index/verify", {});
    }
  }
  return sidecarPost("/index/verify", {});
}

export async function ingestFilePath(
  path: string
): Promise<{ ok: boolean; file: ShelfFile; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    try {
      return await invoke("ingest_file_path", { path });
    } catch {
      return sidecarPost("/index/file", { path, force: true });
    }
  }
  return sidecarPost("/index/file", { path, force: true });
}

export async function uploadRealDroppedFile(
  file: File
): Promise<{ ok: boolean; file: ShelfFile; state: WorkspaceState }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${SIDECAR_BASE}/index/upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    throw new Error(`Upload failed: ${res.status}`);
  }
  return res.json();
}

export async function openFileInOS(path: string): Promise<void> {
  if (isTauriRuntime()) {
    try {
      await invoke("open_file_native", { path });
      return;
    } catch {
      // Fallback to sidecar OS open
    }
  }
  await sidecarPost("/os/open", { path });
}

export async function revealFileInOS(path: string): Promise<void> {
  if (isTauriRuntime()) {
    try {
      await invoke("reveal_file_native", { path });
      return;
    } catch {
      // Fallback to sidecar OS reveal
    }
  }
  await sidecarPost("/os/reveal", { path });
}

export function resolveLocalAssetSrc(file: ShelfFile): string | null {
  if (file.thumbnailB64) {
    return file.thumbnailB64;
  }
  if (isTauriRuntime() && file.modality === "image") {
    try {
      return convertFileSrc(file.path);
    } catch {
      return null;
    }
  }
  return null;
}

export function getFileRawUrl(fileId: string): string {
  return `${SIDECAR_BASE}/file/raw?file_id=${encodeURIComponent(fileId)}`;
}

export function getPdfPageUrl(fileId: string, page: number): string {
  return `${SIDECAR_BASE}/file/pdf-page?file_id=${encodeURIComponent(fileId)}&page=${page}`;
}

export async function scanConnectedDevice(
  path: string,
  addWatch: boolean = false
): Promise<{ ok: boolean; scannedCount: number; path: string; state: WorkspaceState }> {
  return sidecarPost("/devices/scan", { path, addWatch });
}

export async function completeFirstRunSetup(
  folders: string[]
): Promise<{ ok: boolean; scannedCount: number; state: WorkspaceState }> {
  return sidecarPost("/setup/complete", { folders });
}

export async function runFileAction(
  fileId: string,
  action: string
): Promise<{ ok: boolean; message: string; state: WorkspaceState }> {
  return sidecarPost("/file/action", { fileId, action });
}

export async function undoHistoryAction(
  actionId?: number
): Promise<{ ok: boolean; actionId: number; summary: string; state: WorkspaceState }> {
  return sidecarPost("/history/undo", { actionId: actionId ?? null });
}

export async function redoHistoryAction(
  actionId?: number
): Promise<{ ok: boolean; actionId: number; summary: string; state: WorkspaceState }> {
  return sidecarPost("/history/redo", { actionId: actionId ?? null });
}

export async function fetchSmartCleanPreview(): Promise<SmartCleanPreview> {
  return sidecarGet("/smart-clean/preview");
}

export async function executeSmartClean(opts: {
  applyRenames: boolean;
  archiveDuplicates: boolean;
  organizeFolders: boolean;
}): Promise<{
  ok: boolean;
  summary: string;
  renamedCount: number;
  archivedCount: number;
  movedCount: number;
  state: WorkspaceState;
}> {
  return sidecarPost("/smart-clean/execute", opts);
}

export function subscribeToPipelineEvents(
  onEvent: (evt: PipelineStageEvent) => void
): () => void {
  let unlistenTauri: (() => void) | null = null;
  let es: EventSource | null = null;
  let disposed = false;

  if (isTauriRuntime()) {
    // In Tauri mode, Rust's SSE bridge forwards sidecar events over `indexing://stage-update`.
    // Avoid opening a second EventSource so every event is delivered once.
    listen<PipelineStageEvent>("indexing://stage-update", (event) => {
      if (!disposed) onEvent(event.payload);
    })
      .then((fn) => {
        if (disposed) {
          fn();
        } else {
          unlistenTauri = fn;
        }
      })
      .catch(() => {});
  } else {
    try {
      es = new EventSource(`${SIDECAR_BASE}/events`);
      es.onmessage = (msg) => {
        try {
          const parsed = JSON.parse(msg.data) as PipelineStageEvent;
          if (!disposed) onEvent(parsed);
        } catch {
          // Ignore malformed frame
        }
      };
    } catch {
      // Ignore SSE connection error
    }
  }

  return () => {
    disposed = true;
    if (unlistenTauri) unlistenTauri();
    if (es) es.close();
  };
}

export async function pickFolderNative(): Promise<string | null> {
  if (isTauriRuntime()) {
    try {
      const picked = await invoke<string | null>("pick_folder_native");
      return picked || null;
    } catch {
      return null;
    }
  }
  return null;
}

export async function reloadWatcherNative(): Promise<void> {
  if (isTauriRuntime()) {
    try {
      await invoke("reload_watcher");
    } catch {
      // ignore in non-tauri mode
    }
  }
}

export async function importConnectedDevice(
  path: string,
  destination?: string
): Promise<{ importedCount: number; destination: string; state: WorkspaceState }> {
  return sidecarPost("/devices/import", { path, destination });
}

export async function createFolder(
  parentPath: string,
  folderName: string
): Promise<{ ok: boolean; path: string; name: string }> {
  return sidecarPost("/folders/create", { parentPath, folderName });
}

export async function copyFileToFolder(
  fileId: string,
  targetFolder: string
): Promise<{ ok: boolean; path: string; state: WorkspaceState }> {
  return sidecarPost("/file/copy", { fileId, targetFolder });
}

export async function moveFileToFolder(
  fileId: string,
  targetFolder: string
): Promise<{ ok: boolean; path: string; state: WorkspaceState }> {
  return sidecarPost("/file/move", { fileId, targetFolder });
}

export async function trashFile(
  fileId: string
): Promise<{ ok: boolean; trashedPath: string; state: WorkspaceState }> {
  return sidecarPost("/file/trash", { fileId });
}

export async function renameFileCustom(
  fileId: string,
  newName: string
): Promise<{ ok: boolean; file: ShelfFile; state: WorkspaceState }> {
  return sidecarPost("/file/rename", { fileId, newName });
}

export async function deleteFilePermanently(
  fileId: string
): Promise<{ ok: boolean; deletedPath: string; state: WorkspaceState }> {
  return sidecarPost("/file/delete", { fileId });
}

export async function openFileExternal(
  fileId: string,
  reveal: boolean = false
): Promise<{ ok: boolean }> {
  return sidecarPost("/file/open", { fileId, reveal });
}

export async function runBulkFileAction(
  fileIds: string[],
  action: "move" | "trash" | "tag" | "pin" | "unpin",
  targetFolder?: string,
  tag?: string
): Promise<{ ok: boolean; count: number; state: WorkspaceState }> {
  return sidecarPost("/files/bulk", { fileIds, action, targetFolder, tag });
}

export async function togglePinFile(
  fileId: string
): Promise<{ ok: boolean; fileId: string; pinned: boolean; state: WorkspaceState }> {
  return sidecarPost("/file/pin", { fileId });
}

export async function setFileTags(
  fileId: string,
  tags: string[]
): Promise<{ ok: boolean; fileId: string; tags: string[]; state: WorkspaceState }> {
  return sidecarPost("/file/tags", { fileId, tags });
}

export async function togglePinShelf(
  shelfId: string
): Promise<{ ok: boolean; shelfId: string; pinned: boolean; state: WorkspaceState }> {
  return sidecarPost("/shelf/pin", { shelfId });
}

export async function saveSearchPreset(
  name: string,
  query: string,
  modality: string = "all",
  tag: string = ""
): Promise<{ ok: boolean; item: SavedSearch; state: WorkspaceState }> {
  return sidecarPost("/searches/saved", { name, query, modality, tag });
}

export async function deleteSearchPreset(
  searchId: string
): Promise<{ ok: boolean; state: WorkspaceState }> {
  const res = await fetch(`${SIDECAR_BASE}/searches/saved/${encodeURIComponent(searchId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw new Error(`Sidecar DELETE /searches/saved failed: ${res.status}`);
  }
  return res.json();
}

export interface VisionModelInfo {
  id: string;
  name: string;
  description: string;
  sizeLabel: string;
  speedLabel: string;
  accuracyLabel: string;
  installed: boolean;
  jointTextReady: boolean;
  active: boolean;
}

export async function fetchVisionModels(): Promise<{
  activeModel: string;
  models: VisionModelInfo[];
}> {
  return sidecarGet("/vision/models");
}

export async function selectVisionModel(
  modelId: string,
  download: boolean = true
): Promise<{ ok: boolean; activeModel: string; models: VisionModelInfo[]; state: WorkspaceState }> {
  return sidecarPost("/vision/select", { modelId, download });
}

export async function reindexImagesOnly(): Promise<{
  ok: boolean;
  reindexed: number;
  total: number;
  state: WorkspaceState;
}> {
  return sidecarPost("/vision/reindex-images", {});
}
