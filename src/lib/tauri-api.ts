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
  modality: "image" | "document" | "code" | "video" | "audio" | "archive";
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
  subShelf?: string;
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
  hasThumbnail?: boolean;
  metadata?: Record<string, any>;
  tags?: string[];
  pinned?: boolean;
  unindexed?: boolean;
  supported?: boolean;
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
  isCustom?: boolean;
  fileIds?: string[];
  subShelves?: Array<{ name: string; count: number }>;
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
  swapped?: boolean;
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
  trash_mode?: "reversible" | "system";
  max_scan_files?: number;
  ignore_patterns?: string;
  ollama_enabled?: boolean;
  max_results: number;
  min_similarity: number;
  silhouette_score: number;
  setup_completed?: boolean;
  vision_model?: string;
  user_name?: string;
  user_focus_areas?: string[];
  adaptive_learning?: boolean;
  terms_accepted?: boolean;
}

export interface DuplicateMatchInfo {
  id: string;
  name: string;
  folder: string;
  path: string;
  similarityPct: number;
  kind: "exact" | "similar";
}

export interface FolderEntryItem {
  name: string;
  path: string;
  itemCount: number;
  watched: boolean;
}

export interface FolderBrowseResponse {
  path: string;
  displayPath: string;
  parentPath: string | null;
  breadcrumbs: Array<{ name: string; path: string }>;
  folders: FolderEntryItem[];
  files: ShelfFile[];
}

export interface AskCitation {
  fileId: string;
  fileName: string;
  title: string;
  format: string;
  modality?: string;
  folder?: string;
  folderFull?: string;
  path?: string;
  size?: string;
  date: string;
  shelfName: string;
  subShelf?: string;
  snippet: string;
  hasThumbnail?: boolean;
  score: number;
}

export interface ProposedAiActionItem {
  fileId: string;
  fileName: string;
  title: string;
  format: string;
  modality?: string;
  folder?: string;
  folderFull?: string;
  path?: string;
  size?: string;
  date?: string;
  shelfName?: string;
  detail?: string;
  previewText?: string;
  hasThumbnail?: boolean;
  selectedByDefault?: boolean;
}

export interface ProposedAiAction {
  actionId: string;
  actionType: string;
  title: string;
  summary: string;
  targetLabel?: string;
  reversible?: boolean;
  destructive?: boolean;
  requiresSelection?: boolean;
  steps?: Array<{ actionType: string; title: string; payload?: Record<string, unknown> }>;
  items: ProposedAiActionItem[];
  payload?: Record<string, unknown>;
}

export interface AiChoice {
  label: string;
  prompt: string;
  kind?: "action" | "followup" | "clarify" | string;
}

export interface AskResponse {
  question: string;
  answer: string;
  citations: AskCitation[];
  choices?: AiChoice[];
  mode: string;
  appliedFilters?: string[];
  intentMode?: string;
  totalMatched?: number;
  proposedAction?: ProposedAiAction | null;
  latencyMs: number;
}

export interface ExecuteAiActionRequest {
  actionType: string;
  fileIds?: string[];
  targetFolder?: string;
  createTargetFolder?: boolean;
  folderPath?: string;
  folderPaths?: string[];
  parentPath?: string;
  folderName?: string;
  shelfId?: string;
  shelfName?: string;
  tag?: string;
  template?: string;
  newName?: string;
  renameMap?: Record<string, string>;
  actionId?: number;
  steps?: Array<{ actionType: string; title: string; payload?: Record<string, unknown> }>;
  pairIds?: string[];
}

/** Sanitize extracted document text in the UI so ligatures, PUA bullets, and glyph-fallback `?` never show raw artifacts. */
export function cleanPreviewText(raw?: string): string {
  if (!raw) return "";
  return raw
    .replace(/\r\n/g, "\n")
    .replace(/[\u200B-\u200F\u202A-\u202E\u2060\uFEFF\u00AD\uFFFD]/g, " ")
    .replace(/[\uE000-\uF8FF]/g, "•")
    .replace(/^[ \t]*\?[ \t]+(?=[A-Za-z0-9("'“])/gm, "• ")
    .replace(/([a-z])\?(s\b)/g, "$1'$2")
    .replace(/\s+\?\s+/g, " ")
    .replace(/([a-z]{2,})-\n([a-z]{2,})/g, "$1$2")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

export interface TrashArchiveResponse {
  trashed: ShelfFile[];
  archived: Array<{
    pairId: string;
    fileId: string;
    originalPath: string;
    archivedPath: string;
    name: string;
    timestamp: number;
  }>;
}

export interface WorkspaceInsights {
  totalFiles: number;
  totalBytes: number;
  totalHuman: string;
  byModality: Array<{ modality: string; count: number; bytes: number }>;
  byShelf: Array<{ shelfName: string; shelfId: string; count: number; bytes: number }>;
  reclaimableMB: number;
  duplicatePairsCount: number;
  unnamedCount: number;
  largestFiles: Array<{ id: string; name: string; title: string; size: string; format: string }>;
  staleDownloads: Array<{ id: string; name: string; size: string; date: string }>;
}

export interface PipelineStageEvent {
  type?: "stage" | "progress" | "devices_changed" | "index_updated";
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
  pausedOnBattery?: boolean;
  trashCount?: number;
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
  appliedFilters?: string[];
  intentMode?: string;
  totalMatched?: number;
}

export interface TidyMovePreview {
  fileId: string;
  fileName: string;
  fromShort: string;
  toShort: string;
  fromPath: string;
  toPath: string;
}

let SIDECAR_BASE = "http://127.0.0.1:18420";
let SIDECAR_TOKEN = "";
let sidecarUrlSynced = false;

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function syncSidecarBase(): Promise<string> {
  if (sidecarUrlSynced) return SIDECAR_BASE;
  if (isTauriRuntime()) {
    const [urlRes, tokRes] = await Promise.allSettled([
      invoke<string>("get_sidecar_url"),
      invoke<string>("get_sidecar_token"),
    ]);
    if (urlRes.status === "fulfilled" && urlRes.value) {
      SIDECAR_BASE = urlRes.value;
    }
    if (tokRes.status === "fulfilled" && tokRes.value) {
      SIDECAR_TOKEN = tokRes.value;
    }
  } else {
    sidecarUrlSynced = true;
  }
  return SIDECAR_BASE;
}

function buildAuthHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  if (SIDECAR_TOKEN) {
    headers["X-GS-Token"] = SIDECAR_TOKEN;
  }
  return headers;
}

function appendTokenQuery(url: string): string {
  if (!SIDECAR_TOKEN) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(SIDECAR_TOKEN)}`;
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number
): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error("Local engine request timed out");
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
}

async function readSidecarError(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json();
    if (data && typeof data.detail === "string" && data.detail.trim()) {
      return data.detail;
    }
  } catch {
    // ignore non-JSON body
  }
  return fallback;
}

async function sidecarGet<T>(endpoint: string, timeoutMs: number = 30000): Promise<T> {
  await syncSidecarBase();
  let res: Response;
  try {
    res = await fetchWithTimeout(
      `${SIDECAR_BASE}${endpoint}`,
      { headers: buildAuthHeaders() },
      timeoutMs
    );
  } catch (err) {
    sidecarUrlSynced = false;
    throw err;
  }
  if (!res.ok) {
    throw new Error(await readSidecarError(res, `Sidecar GET ${endpoint} failed: ${res.status}`));
  }
  if (!isTauriRuntime() || SIDECAR_TOKEN) {
    sidecarUrlSynced = true;
  }
  return res.json() as Promise<T>;
}

async function sidecarPost<T>(
  endpoint: string,
  body: Record<string, unknown> = {},
  timeoutMs?: number
): Promise<T> {
  await syncSidecarBase();
  const longOp =
    endpoint.startsWith("/index/rescan") ||
    endpoint.startsWith("/vision/") ||
    endpoint.startsWith("/smart-clean/") ||
    endpoint.startsWith("/setup/complete") ||
    endpoint.startsWith("/devices/");
  let res: Response;
  try {
    res = await fetchWithTimeout(
      `${SIDECAR_BASE}${endpoint}`,
      {
        method: "POST",
        headers: buildAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
      },
      timeoutMs ?? (longOp ? 180000 : 30000)
    );
  } catch (err) {
    sidecarUrlSynced = false;
    throw err;
  }
  if (!res.ok) {
    throw new Error(await readSidecarError(res, `Sidecar POST ${endpoint} failed: ${res.status}`));
  }
  if (!isTauriRuntime() || SIDECAR_TOKEN) {
    sidecarUrlSynced = true;
  }
  return res.json() as Promise<T>;
}

export async function restartSidecarNative(): Promise<boolean> {
  sidecarUrlSynced = false;
  if (isTauriRuntime()) {
    try {
      return await invoke<boolean>("restart_sidecar");
    } catch {
      return false;
    }
  }
  return false;
}

export async function fetchWorkspaceState(): Promise<WorkspaceState> {
  await syncSidecarBase();
  if (isTauriRuntime()) {
    const state = await invoke<WorkspaceState>("get_workspace_state");
    await syncSidecarBase();
    if (SIDECAR_TOKEN) {
      sidecarUrlSynced = true;
    }
    return state;
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
    return invoke<SearchResponse>("semantic_search", {
      query,
      modality,
      limit,
      minSimilarity,
    });
  }
  return sidecarPost<SearchResponse>("/search", { query, modality, limit, minSimilarity });
}

export async function toggleFileRename(
  fileId: string
): Promise<{ ok: boolean; action: string; file: ShelfFile; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    return invoke("toggle_rename", { fileId });
  }
  return sidecarPost("/rename/toggle", { fileId });
}

export async function applyAllSuggestedRenames(): Promise<{
  ok: boolean;
  appliedCount: number;
  state: WorkspaceState;
}> {
  if (isTauriRuntime()) {
    return invoke("apply_all_renames");
  }
  return sidecarPost("/rename/apply-all", {});
}

export async function toggleResolveLookalikePair(
  pairId: string
): Promise<{ ok: boolean; resolved: boolean; savedMB?: number; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    return invoke("resolve_lookalike", { pairId });
  }
  return sidecarPost("/lookalikes/resolve", { pairId });
}

export async function fetchTidyMovesPreview(): Promise<{ moves: TidyMovePreview[] }> {
  if (isTauriRuntime()) {
    return invoke("preview_tidy_folders");
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
    return invoke("execute_tidy_folders");
  }
  return sidecarPost("/tidy-folders/execute", {});
}

export async function rollbackTidyFoldersOnDisk(): Promise<{
  ok: boolean;
  restoredCount: number;
  state: WorkspaceState;
}> {
  if (isTauriRuntime()) {
    return invoke("rollback_tidy_folders");
  }
  return sidecarPost("/tidy-folders/rollback", {});
}

export async function addWatchedFolderPath(path: string): Promise<{ ok: boolean; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    return invoke("add_watched_folder", { path });
  }
  return sidecarPost("/folders/add", { path });
}

export async function toggleWatchedFolderPath(
  path: string
): Promise<{ ok: boolean; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    return invoke("toggle_watched_folder", { path });
  }
  return sidecarPost("/folders/toggle", { path });
}

export async function removeWatchedFolderPath(
  path: string
): Promise<{ ok: boolean; state: WorkspaceState }> {
  return sidecarPost("/folders/remove", { path });
}

export async function updateAppSetting(
  key: string,
  value: unknown
): Promise<{ ok: boolean; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    return invoke("update_setting", { key, value });
  }
  return sidecarPost("/settings/update", { key, value });
}

export async function rescanAllWatchedFolders(allFiles: boolean = false): Promise<{
  ok: boolean;
  scannedCount: number;
  state: WorkspaceState;
}> {
  if (!allFiles && isTauriRuntime()) {
    return invoke("rescan_folders");
  }
  return sidecarPost(`/index/rescan?all_files=${allFiles ? "true" : "false"}`, {}, 180000);
}

export async function verifyLocalIndexIntegrity(): Promise<{
  ok: boolean;
  integrity: { activeFiles: number; vectorRows: number; orphansRemoved: number };
  state: WorkspaceState;
}> {
  if (isTauriRuntime()) {
    return invoke("verify_index");
  }
  return sidecarPost("/index/verify", {});
}

export async function ingestFilePath(
  path: string
): Promise<{ ok: boolean; file: ShelfFile; duplicateOf?: DuplicateMatchInfo | null; state: WorkspaceState }> {
  if (isTauriRuntime()) {
    return invoke("ingest_file_path", { path });
  }
  return sidecarPost("/index/file", { path, force: true });
}

export async function uploadRealDroppedFile(
  file: File
): Promise<{ ok: boolean; file: ShelfFile; duplicateOf?: DuplicateMatchInfo | null; state: WorkspaceState }> {
  await syncSidecarBase();
  const form = new FormData();
  form.append("file", file);
  const res = await fetchWithTimeout(
    `${SIDECAR_BASE}/index/upload`,
    {
      method: "POST",
      headers: buildAuthHeaders(),
      body: form,
    },
    60000
  );
  if (!res.ok) {
    throw new Error(await readSidecarError(res, `Upload failed: ${res.status}`));
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
  if (file.hasThumbnail) {
    return getFileThumbnailUrl(file.id);
  }
  if (file.modality === "image") {
    if (isTauriRuntime()) {
      try {
        return convertFileSrc(file.path);
      } catch {
        return getFileRawUrl(file.id);
      }
    }
    return getFileRawUrl(file.id);
  }
  return null;
}

export function getFileThumbnailUrl(fileId: string): string {
  return appendTokenQuery(`${SIDECAR_BASE}/file/thumbnail?file_id=${encodeURIComponent(fileId)}`);
}

export function getFileRawUrl(fileId: string): string {
  return appendTokenQuery(`${SIDECAR_BASE}/file/raw?file_id=${encodeURIComponent(fileId)}`);
}

export function getPdfPageUrl(fileId: string, page: number): string {
  return appendTokenQuery(`${SIDECAR_BASE}/file/pdf-page?file_id=${encodeURIComponent(fileId)}&page=${page}`);
}

export async function scanConnectedDevice(
  path: string,
  addWatch: boolean = false
): Promise<{ ok: boolean; scannedCount: number; path: string; state: WorkspaceState }> {
  return sidecarPost("/devices/scan", { path, addWatch });
}

export async function completeFirstRunSetup(
  folders: string[],
  options?: {
    userName?: string;
    focusAreas?: string[];
    adaptiveLearning?: boolean;
    termsAccepted?: boolean;
  }
): Promise<{ ok: boolean; scannedCount: number; scanning?: boolean; state: WorkspaceState }> {
  return sidecarPost("/setup/complete", {
    folders,
    userName: options?.userName || "",
    focusAreas: options?.focusAreas || [],
    adaptiveLearning: options?.adaptiveLearning ?? true,
    termsAccepted: options?.termsAccepted ?? true,
  });
}

export async function clearActivityLog(): Promise<{ ok: boolean; state: WorkspaceState }> {
  return sidecarPost("/activity/clear", {});
}

export async function resetSetupWizard(): Promise<{ ok: boolean; state: WorkspaceState }> {
  return sidecarPost("/setup/reset", {});
}

export async function recordUserFeedback(payload: {
  query?: string;
  fileId?: string;
  shelfId?: string;
  action?: string;
}): Promise<{ ok: boolean }> {
  return sidecarPost("/feedback/interaction", payload);
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
      es = new EventSource(appendTokenQuery(`${SIDECAR_BASE}/events`));
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

export async function setFileShelf(
  fileId: string,
  shelfId: string
): Promise<{ ok: boolean; fileId: string; shelfId: string; shelfName: string; state: WorkspaceState }> {
  return sidecarPost("/file/shelf", { fileId, shelfId });
}

export async function swapLookalikeKeep(
  pairId: string
): Promise<{ ok: boolean; pairId: string; swapped: boolean; state: WorkspaceState }> {
  return sidecarPost("/lookalikes/swap", { pairId });
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
  await syncSidecarBase();
  const res = await fetchWithTimeout(
    `${SIDECAR_BASE}/searches/saved/${encodeURIComponent(searchId)}`,
    {
      method: "DELETE",
      headers: buildAuthHeaders(),
    },
    30000
  );
  if (!res.ok) {
    throw new Error(await readSidecarError(res, `Sidecar DELETE /searches/saved failed: ${res.status}`));
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

export interface VisionDownloadProgress {
  active: boolean;
  modelId: string;
  modelName: string;
  status: "idle" | "downloading" | "activating" | "done" | "error";
  stage: string;
  fileName: string;
  downloadedBytes: number;
  totalBytes: number;
  percent: number;
  message: string;
}

export async function fetchVisionModels(): Promise<{
  activeModel: string;
  models: VisionModelInfo[];
}> {
  return sidecarGet("/vision/models");
}

export async function fetchVisionDownloadProgress(): Promise<VisionDownloadProgress> {
  return sidecarGet("/vision/download-progress");
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

export async function findSimilarFiles(
  fileId: string,
  limit: number = 24
): Promise<{ sourceFile: ShelfFile; results: ShelfFile[]; latencyMs: number }> {
  return sidecarPost("/search/similar", { fileId, limit });
}

export async function askYourShelves(
  question: string,
  maxCitations: number = 25,
  context?: {
    selectedFileId?: string;
    recentFileIds?: string[];
    history?: Array<{ role: string; text: string }>;
    pendingAction?: ProposedAiAction | null;
  }
): Promise<AskResponse> {
  return sidecarPost("/ask", {
    question,
    maxCitations,
    selectedFileId: context?.selectedFileId ?? null,
    recentFileIds: context?.recentFileIds ?? [],
    history: context?.history ?? [],
    pendingAction: context?.pendingAction ?? null,
  });
}

export async function executeAiAction(
  req: ExecuteAiActionRequest
): Promise<{ ok: boolean; count: number; summary: string; state: WorkspaceState }> {
  return sidecarPost("/ask/execute", { ...req });
}

export async function browseFolder(path?: string): Promise<FolderBrowseResponse> {
  const qs = path ? `?path=${encodeURIComponent(path)}` : "";
  return sidecarGet(`/folders/browse${qs}`);
}

export async function renameFolderOnDisk(
  path: string,
  newName: string
): Promise<{ ok: boolean; path: string; name: string; state: WorkspaceState }> {
  return sidecarPost("/folders/rename", { path, newName });
}

export async function deleteFolderOnDisk(
  path: string
): Promise<{ ok: boolean; trashedPath: string; state: WorkspaceState }> {
  return sidecarPost("/folders/delete", { path });
}

export async function pasteFilesToFolder(
  fileIds: string[],
  targetFolder: string,
  mode: "copy" | "move"
): Promise<{ ok: boolean; count: number; state: WorkspaceState }> {
  return sidecarPost("/files/paste", { fileIds, targetFolder, mode });
}

export async function createCustomShelf(opts: {
  name: string;
  desc?: string;
  query?: string;
  modality?: string;
  tag?: string;
  fileIds?: string[];
}): Promise<{ ok: boolean; shelfId: string; state: WorkspaceState }> {
  return sidecarPost("/shelves/custom", opts);
}

export async function renameShelfLabel(
  shelfId: string,
  newName: string
): Promise<{ ok: boolean; shelfId: string; name: string; state: WorkspaceState }> {
  return sidecarPost("/shelves/rename", { shelfId, newName });
}

export async function deleteCustomShelf(
  shelfId: string
): Promise<{ ok: boolean; shelfId: string; state: WorkspaceState }> {
  await syncSidecarBase();
  const res = await fetchWithTimeout(
    `${SIDECAR_BASE}/shelves/custom/${encodeURIComponent(shelfId)}`,
    {
      method: "DELETE",
      headers: buildAuthHeaders(),
    },
    30000
  );
  if (!res.ok) {
    throw new Error(await readSidecarError(res, `Sidecar DELETE /shelves/custom failed: ${res.status}`));
  }
  return res.json();
}

export async function fetchTrashAndArchive(): Promise<TrashArchiveResponse> {
  return sidecarGet("/trash/list");
}

export async function restoreTrashedFile(
  fileId: string
): Promise<{ ok: boolean; restoredPath: string; state: WorkspaceState }> {
  return sidecarPost("/trash/restore", { fileId });
}

export async function emptyTrashPermanently(): Promise<{
  ok: boolean;
  removedCount: number;
  state: WorkspaceState;
}> {
  return sidecarPost("/trash/empty", {});
}

export async function trashFileUsingSetting(
  file: ShelfFile,
  trashMode?: "reversible" | "system"
): Promise<{ ok: boolean; state?: WorkspaceState }> {
  if (trashMode === "system" && isTauriRuntime()) {
    await invoke("trash_file_native", { path: file.path });
    const res = await sidecarPost<{ ok: boolean; state: WorkspaceState }>("/index/remove", { path: file.path });
    return { ok: true, state: res.state };
  }
  const res = await trashFile(file.id);
  return { ok: true, state: res.state };
}

export async function previewBatchRenameTemplate(
  fileIds: string[],
  template: string
): Promise<{ previews: Array<{ fileId: string; fromName: string; toName: string; folder: string }> }> {
  return sidecarPost("/rename/template/preview", { fileIds, template });
}

export async function executeBatchRenameTemplate(
  fileIds: string[],
  template: string
): Promise<{ ok: boolean; renamedCount: number; state: WorkspaceState }> {
  return sidecarPost("/rename/template/execute", { fileIds, template });
}

export async function fetchFileFullExcerpt(
  file: ShelfFile,
  maxLines: number = 500
): Promise<{ content: string; totalLines: number; truncated: boolean }> {
  if (isTauriRuntime()) {
    try {
      const ex = await invoke<{ content: string; total_lines: number; truncated: boolean }>("read_file_excerpt", {
        path: file.path,
        maxLines,
      });
      return { content: ex.content, totalLines: ex.total_lines, truncated: ex.truncated };
    } catch {
      // fall back to sidecar
    }
  }
  return sidecarGet(`/file/excerpt?file_id=${encodeURIComponent(file.id)}&max_lines=${maxLines}`);
}

export async function fetchWorkspaceInsights(): Promise<WorkspaceInsights> {
  return sidecarGet("/insights");
}
