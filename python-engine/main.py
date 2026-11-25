#!/usr/bin/env python3
"""
Good Shelves Local Multimodal AI Sidecar Server (FastAPI + SSE + JSON-RPC over 127.0.0.1).
Provides real-time file parsing, OCR, 512-d ONNX embedding, SQLite-vec search,
UMAP+HDBSCAN shelf discovery, lookalike diffing, and live ingestion telemetry.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import platform
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from contextlib import asynccontextmanager
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from vector_store import VectorStore, get_user_special_dir
from devices import devices_signature, list_connected_devices


# ============================================================================
# FastAPI Application & Real-Time Event Broadcaster
# ============================================================================

_shutdown_event = threading.Event()
_store_lock = threading.Lock()

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_BLOCKED_OPEN_SUFFIXES = {
    ".appimage",
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".desktop",
    ".exe",
    ".jar",
    ".lnk",
    ".msi",
    ".ps1",
    ".scr",
    ".sh",
    ".so",
    ".vbs",
}


def _device_hotplug_monitor() -> None:
    """Poll for newly plugged-in or disconnected phones/USB drives and push SSE updates."""
    last_sig = None
    while not _shutdown_event.is_set():
        try:
            st = get_store()
            watched = {w["path"] for w in st.get_watched_folders()}
            devs = list_connected_devices(watched)
            sig = devices_signature(devs)
            if last_sig is not None and sig != last_sig:
                st._invalidate_cache()
                broadcast_event({"type": "devices_changed", "devices": devs, "timestamp": time.time()})
            last_sig = sig
        except Exception:
            pass
        _shutdown_event.wait(5.0)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _shutdown_event.clear()
    threading.Thread(target=bootstrap_initial_index, daemon=True).start()
    threading.Thread(target=_device_hotplug_monitor, daemon=True).start()
    try:
        yield
    finally:
        _shutdown_event.set()
        if store is not None:
            try:
                store.request_shutdown()
            except Exception:
                pass


app = FastAPI(title="Good Shelves Multimodal Engine", version="1.0.4", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^tauri://localhost$|^https?://tauri\.localhost$",
    allow_methods=["*"],
    allow_headers=["Content-Type", "X-GS-Token"],
)

_uvicorn_server: Optional[uvicorn.Server] = None


def _expected_token() -> str:
    return os.environ.get("GOOD_SHELVES_TOKEN", "").strip()


def _is_loopback_host(request: Request) -> bool:
    """Return True when the Host header names the local loopback interface."""
    host_header = (request.headers.get("host") or "").strip().lower()
    if not host_header:
        return True
    if host_header.startswith("["):
        host_name = host_header[1:].split("]", 1)[0]
    elif host_header.count(":") == 1:
        host_name = host_header.rsplit(":", 1)[0]
    else:
        host_name = host_header
    return host_name in ("127.0.0.1", "localhost", "::1")


@app.middleware("http")
async def _sidecar_auth_middleware(request: Request, call_next):
    expected = _expected_token()
    # Fail closed: a production sidecar with no token must never accept mutations.
    # (pytest sets PYTEST_CURRENT_TEST; GOOD_SHELVES_DEV=1 enables the browser dev server.)
    in_tests = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    dev_mode = os.environ.get("GOOD_SHELVES_DEV") == "1"
    # Reject non-loopback Host headers to mitigate DNS-rebinding against the local sidecar.
    if not in_tests and not _is_loopback_host(request):
        return JSONResponse({"detail": "Forbidden host header"}, status_code=403)
    # Read-only media endpoints are token-free: they are loopback-only, Host-validated,
    # and keyed by unguessable file IDs ( sha256 + mtime ), so the powerful bearer
    # token never has to reach the renderer just to paint a thumbnail.
    open_media_get = request.method == "GET" and request.url.path in (
        "/file/thumbnail",
        "/file/raw",
        "/file/pdf-page",
    )
    if not expected and not in_tests and not dev_mode:
        if request.method not in ("OPTIONS", "GET", "HEAD"):
            return JSONResponse(
                {"detail": "Sidecar is not configured for authenticated access"},
                status_code=503,
            )
        return await call_next(request)
    if (
        expected
        and request.method != "OPTIONS"
        and request.url.path != "/health"
        and not open_media_get
    ):
        supplied = request.headers.get("x-gs-token") or ""
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized sidecar request"}, status_code=401)
    return await call_next(request)

store: Optional[VectorStore] = None
event_subscribers: list[queue.Queue[dict[str, Any]]] = []
_subscribers_lock = threading.Lock()


def broadcast_event(evt: dict[str, Any]) -> None:
    with _subscribers_lock:
        subs = list(event_subscribers)
    for q in subs:
        try:
            q.put_nowait(evt)
        except Exception:
            pass


def get_store() -> VectorStore:
    global store
    if store is None:
        with _store_lock:
            if store is None:
                created = VectorStore()
                created.register_stage_callback(broadcast_event)
                store = created
    return store


def bootstrap_initial_index() -> None:
    """Index watched folders on startup once first-run folder selection is complete."""
    st = get_store()
    st.get_all_state()
    if _shutdown_event.wait(0.15):
        return
    settings = st.get_settings()
    if not settings.get("setup_completed", True):
        return
    if st.is_paused_on_battery():
        return

    max_files = int(settings.get("max_scan_files", 500))
    active_folders = [f for f in st.get_watched_folders() if f["active"]]
    if active_folders:
        st.emit_progress("indexing", 0, 1, "Scanning watched folders…")
    total_indexed = 0
    try:
        for folder in active_folders:
            try:
                total_indexed += st.scan_folder(Path(folder["path"]), max_files=max_files, recursive=True, emit_idle=False)
            except OSError:
                continue

        integrity = st.verify_integrity()
        orphans = int(integrity.get("orphansRemoved", 0))
        if total_indexed > 0 or orphans > 0:
            st.recluster_all()
            broadcast_event(
                {"type": "index_updated", "indexedCount": total_indexed, "timestamp": time.time()}
            )
    finally:
        if active_folders:
            st.emit_progress("idle", 1, 1, "")


class SearchRequest(BaseModel):
    query: str = ""
    modality: str = "all"
    limit: Optional[int] = None
    minSimilarity: Optional[float] = None


class FileIdRequest(BaseModel):
    fileId: str
    reveal: bool = False


class PairIdRequest(BaseModel):
    pairId: str


class FolderRequest(BaseModel):
    path: str


class SettingUpdateRequest(BaseModel):
    key: str
    value: Any


class IngestPathRequest(BaseModel):
    path: str
    force: bool = False
    includeState: bool = True


class BatchIngestRequest(BaseModel):
    changed: list[str] = []
    removed: list[str] = []


class DeviceScanRequest(BaseModel):
    path: str
    addWatch: bool = False


class SetupCompleteRequest(BaseModel):
    folders: list[str]
    userName: str = ""
    focusAreas: list[str] = []
    adaptiveLearning: bool = True
    termsAccepted: bool = True


class FeedbackInteractionRequest(BaseModel):
    query: str = ""
    fileId: str = ""
    shelfId: str = ""
    action: str = "select"


class FileActionRequest(BaseModel):
    fileId: str
    action: str


class FileRenameRequest(BaseModel):
    fileId: str
    newName: str


class ActionIdRequest(BaseModel):
    actionId: Optional[int] = None


class SmartCleanRequest(BaseModel):
    applyRenames: bool = True
    archiveDuplicates: bool = True
    organizeFolders: bool = True


@app.get("/health")
def health_check(request: Request) -> dict[str, Any]:
    st = get_store()
    expected = _expected_token()
    supplied = request.headers.get("x-gs-token") or ""
    token_valid = True if not expected else hmac.compare_digest(supplied, expected)
    return {
        "app": "good-shelves",
        "status": "ok",
        "dbPath": str(st.db_path),
        "silhouette": st.silhouette_score,
        "authRequired": bool(expected),
        "tokenValid": token_valid,
    }


@app.post("/shutdown")
def api_shutdown() -> dict[str, Any]:
    _shutdown_event.set()
    if store is not None:
        try:
            store.request_shutdown()
        except Exception:
            pass

    def _trigger_exit() -> None:
        time.sleep(0.08)
        if _uvicorn_server is not None:
            _uvicorn_server.should_exit = True
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    threading.Thread(target=_trigger_exit, daemon=True).start()
    return {"ok": True}


@app.get("/state")
def api_get_state() -> dict[str, Any]:
    return get_store().get_all_state()


@app.post("/search")
def api_search(req: SearchRequest) -> dict[str, Any]:
    return get_store().search_semantic(
        query=req.query,
        modality_filter=req.modality,
        limit=req.limit,
        min_similarity=req.minSimilarity,
    )


@app.post("/rename/toggle")
def api_toggle_rename(req: FileIdRequest) -> dict[str, Any]:
    try:
        res = get_store().toggle_rename_file(req.fileId)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/rename/apply-all")
def api_apply_all_renames() -> dict[str, Any]:
    res = get_store().apply_all_renames()
    return {"ok": True, **res, "state": get_store().get_all_state()}


class FileShelfRequest(BaseModel):
    fileId: str
    shelfId: str


@app.post("/lookalikes/resolve")
def api_resolve_lookalike(req: PairIdRequest) -> dict[str, Any]:
    try:
        res = get_store().toggle_archive_lookalike(req.pairId)
        get_store().recluster_all()
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/lookalikes/swap")
def api_swap_lookalike(req: PairIdRequest) -> dict[str, Any]:
    try:
        res = get_store().swap_lookalike_pair(req.pairId)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/tidy-folders/preview")
def api_preview_tidy() -> dict[str, Any]:
    return {"moves": get_store().preview_tidy_folders()}


@app.post("/tidy-folders/execute")
def api_execute_tidy() -> dict[str, Any]:
    res = get_store().execute_tidy_folders()
    return {"ok": True, **res, "state": get_store().get_all_state()}


@app.post("/tidy-folders/rollback")
def api_rollback_tidy() -> dict[str, Any]:
    res = get_store().rollback_tidy_folders()
    return {"ok": True, **res, "state": get_store().get_all_state()}


@app.post("/folders/add")
def api_add_folder(req: FolderRequest) -> dict[str, Any]:
    try:
        get_store().add_watched_folder(req.path)
        return {"ok": True, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/folders/toggle")
def api_toggle_folder(req: FolderRequest) -> dict[str, Any]:
    try:
        get_store().toggle_watched_folder(req.path)
        return {"ok": True, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/folders/remove")
def api_remove_folder(req: FolderRequest) -> dict[str, Any]:
    try:
        get_store().remove_watched_folder(req.path)
        return {"ok": True, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/settings/update")
def api_update_setting(req: SettingUpdateRequest) -> dict[str, Any]:
    get_store().update_setting(req.key, req.value)
    return {"ok": True, "state": get_store().get_all_state()}


@app.post("/index/rescan")
def api_rescan_all(
    all_files: bool = Query(False),
    body: Optional[dict[str, Any]] = Body(default=None),
) -> dict[str, Any]:
    st = get_store()
    use_all = all_files or bool((body or {}).get("all_files"))
    max_f = 0 if use_all else int(st.get_settings().get("max_scan_files", 500))
    total = 0
    for wf in st.get_watched_folders():
        if wf["active"]:
            total += st.scan_folder(Path(wf["path"]), max_files=max_f, recursive=True, emit_idle=False)
    st.verify_integrity()
    st.recluster_all()
    st.emit_progress("idle", 1, 1, "")
    return {"ok": True, "scannedCount": total, "state": st.get_all_state()}


@app.post("/index/verify")
def api_verify_index() -> dict[str, Any]:
    st = get_store()
    integrity = st.verify_integrity()
    st.recluster_all()
    return {"ok": True, "integrity": integrity, "state": st.get_all_state()}


@app.post("/index/file")
def api_index_file(req: IngestPathRequest) -> dict[str, Any]:
    st = get_store()
    item = st.index_single_file(Path(req.path), force=req.force, emit_telemetry=True, recluster=False)
    if not item:
        raise HTTPException(status_code=400, detail=f"Could not index {req.path}")
    newly_indexed = bool(item.pop("_newlyIndexed", False))
    if req.force or newly_indexed:
        st.recluster_all()
        item = st.get_file_by_id(item["id"]) or item
    report = st.build_ai_ingestion_report(item["id"], apply_auto_rules=True)
    item = st.get_file_by_id(item["id"]) or item
    dup = st.check_duplicate_for_file(item["id"])
    resp: dict[str, Any] = {"ok": True, "file": item, "duplicateOf": dup, "aiIngestionReport": report}
    if req.includeState:
        resp["state"] = st.get_all_state()
    return resp


@app.post("/index/batch")
def api_index_batch(req: BatchIngestRequest) -> dict[str, Any]:
    """Batch ingest/remove filesystem watcher changes with a single recluster + SSE update."""
    st = get_store()
    indexed_count = 0
    removed_count = 0
    needs_recluster = False

    for raw_p in dict.fromkeys(req.removed):
        try:
            res = st.remove_file_by_path(raw_p)
            if res.get("removed"):
                removed_count += 1
                needs_recluster = True
        except Exception:
            continue

    changed_paths = list(dict.fromkeys(req.changed))
    emit_tel = len(changed_paths) == 1
    try:
        for raw_p in changed_paths:
            try:
                item = st.index_single_file(
                    Path(raw_p),
                    force=False,
                    emit_telemetry=emit_tel,
                    recluster=False,
                    commit=len(changed_paths) == 1,
                    return_record=emit_tel,
                )
                if item:
                    indexed_count += 1
                    if item.get("_newlyIndexed"):
                        needs_recluster = True
            except Exception:
                continue
    finally:
        if len(changed_paths) > 1:
            with st._lock:
                st._connect().commit()

    if needs_recluster:
        st.recluster_all()
        broadcast_event(
            {"type": "index_updated", "indexedCount": indexed_count, "timestamp": time.time()}
        )

    return {"ok": True, "indexed": indexed_count, "removed": removed_count}


@app.post("/index/upload")
async def api_upload_and_index_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """Save a dropped file into Downloads and run the live 4-stage ingestion pipeline on it."""
    dl_dir = get_user_special_dir("Downloads")
    dl_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or f"dropped_{int(time.time())}.txt").name
    target_path = dl_dir / safe_name
    if target_path.exists():
        stem, suf = target_path.stem, target_path.suffix
        counter = 1
        while target_path.exists():
            target_path = dl_dir / f"{stem}_{counter}{suf}"
            counter += 1
    written = 0
    with target_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                out.close()
                target_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Uploaded file exceeds the 512 MB limit")
            out.write(chunk)

    st = get_store()
    item = await run_in_threadpool(
        st.index_single_file,
        target_path,
        force=True,
        emit_telemetry=True,
        recluster=True,
    )
    if not item:
        raise HTTPException(status_code=400, detail=f"Unsupported or empty file: {safe_name}")
    report = await asyncio.to_thread(st.build_ai_ingestion_report, item["id"], True)
    item = await asyncio.to_thread(st.get_file_by_id, item["id"]) or item
    dup = await asyncio.to_thread(st.check_duplicate_for_file, item["id"])
    state = await asyncio.to_thread(st.get_all_state)
    return {"ok": True, "file": item, "duplicateOf": dup, "aiIngestionReport": report, "state": state}


class DropPayloadRequest(BaseModel):
    url: str = ""
    dataUrl: str = ""
    html: str = ""
    text: str = ""
    filename: str = ""
    title: str = ""


def _unique_download_target(dl_dir: Path, filename: str) -> Path:
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", Path(filename or f"dropped_{int(time.time())}.txt").name).strip(" ._")
    if not safe_name:
        safe_name = f"dropped_{int(time.time())}.txt"
    target = dl_dir / safe_name
    if target.exists():
        stem, suf = target.stem, target.suffix
        counter = 1
        while target.exists():
            target = dl_dir / f"{stem}_{counter}{suf}"
            counter += 1
    return target


@app.post("/index/drop-payload")
def api_index_drop_payload(req: DropPayloadRequest) -> dict[str, Any]:
    """
    Universal drag-and-drop payload ingestion endpoint:
    Accepts browser image drops (`dataUrl` or `<img src>`), URL/link drops (`http://`, `https://`, `file://`),
    and rich HTML/text snippet drops, saves them into Downloads, indexes them, and runs the
    Autonomous File-Drop Copilot (`build_ai_ingestion_report`).
    """
    import base64
    import re
    import urllib.parse
    import urllib.request

    st = get_store()
    dl_dir = get_user_special_dir("Downloads")
    dl_dir.mkdir(parents=True, exist_ok=True)
    target_path: Optional[Path] = None

    raw_url = (req.url or "").strip()
    raw_data_url = (req.dataUrl or "").strip()
    raw_html = (req.html or "").strip()
    raw_text = (req.text or "").strip()

    # Extract URL or dataUrl from HTML if not explicitly provided
    if not raw_data_url and not raw_url and raw_html:
        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
        m_href = re.search(r'<a[^>]+href=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
        extracted_src = (m_img.group(1) if m_img else (m_href.group(1) if m_href else "")).strip()
        if extracted_src.startswith("data:"):
            raw_data_url = extracted_src
        elif extracted_src.startswith(("http://", "https://", "file://")):
            raw_url = extracted_src

    if raw_url.startswith("data:") and not raw_data_url:
        raw_data_url = raw_url
        raw_url = ""

    # 1. Base64 data: URL drop
    if raw_data_url.startswith("data:"):
        m_du = re.match(r"^data:([^;,]+)?(?:;base64)?,(.*)$", raw_data_url, re.DOTALL)
        if not m_du:
            raise HTTPException(status_code=400, detail="Malformed dataUrl payload")
        mime = (m_du.group(1) or "application/octet-stream").lower().strip()
        b64_part = m_du.group(2).strip()
        is_b64 = ";base64," in raw_data_url[:80].lower()
        try:
            raw_bytes = base64.b64decode(b64_part) if is_b64 else urllib.parse.unquote_to_bytes(b64_part)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to decode dataUrl: {exc}")
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/svg+xml": ".svg",
            "application/pdf": ".pdf",
            "text/plain": ".txt",
            "text/html": ".html",
            "text/markdown": ".md",
            "text/csv": ".csv",
            "application/json": ".json",
        }
        ext = ext_map.get(mime, ".png" if mime.startswith("image/") else ".txt")
        base_name = req.filename.strip() or req.title.strip() or f"dropped_{int(time.time())}{ext}"
        if not Path(base_name).suffix:
            base_name = f"{base_name}{ext}"
        target_path = _unique_download_target(dl_dir, base_name)
        target_path.write_bytes(raw_bytes)

    # 2. Local file:// URL drop
    elif raw_url.startswith("file://"):
        parsed_u = urllib.parse.urlparse(raw_url)
        local_p = Path(urllib.parse.unquote(parsed_u.path)).expanduser().resolve()
        if not local_p.exists() or not local_p.is_file():
            raise HTTPException(status_code=404, detail=f"Dropped local file not found: {local_p}")
        target_path = local_p

    # 3. HTTP / HTTPS URL drop (attempts safe direct download; falls back to rich .md web clipping offline!)
    elif raw_url.startswith(("http://", "https://")):
        parsed_u = urllib.parse.urlparse(raw_url)
        url_path_name = Path(urllib.parse.unquote(parsed_u.path)).name
        url_ext = Path(url_path_name).suffix.lower()
        direct_exts = {
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".pdf",
            ".csv", ".tsv", ".xlsx", ".docx", ".pptx", ".txt", ".md", ".json", ".py", ".zip",
        }
        downloaded_ok = False
        if url_ext in direct_exts:
            try:
                req_obj = urllib.request.Request(raw_url, headers={"User-Agent": "GoodShelves/1.0"})
                with urllib.request.urlopen(req_obj, timeout=6) as resp:
                    data = resp.read(50 * 1024 * 1024)
                if data:
                    cand_name = req.filename.strip() or url_path_name or f"web_drop_{int(time.time())}{url_ext}"
                    target_path = _unique_download_target(dl_dir, cand_name)
                    target_path.write_bytes(data)
                    downloaded_ok = True
            except Exception:
                downloaded_ok = False

        if not downloaded_ok:
            # Save a rich Markdown web clipping / bookmark file so browser link drops always succeed (even 100% offline!)
            host = parsed_u.netloc or "web"
            clip_title = (
                req.title.strip()
                or (re.sub(r"<[^>]+>", " ", raw_html).strip()[:80] if raw_html else "")
                or url_path_name
                or f"Web Link from {host}"
            )
            slug = re.sub(r"[^a-z0-9]+", "-", clip_title.lower()).strip("-")[:42] or "web-link"
            target_path = _unique_download_target(dl_dir, f"{slug}.md")
            html_plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_html)).strip() if raw_html else ""
            body_lines = [
                f"# {clip_title}",
                "",
                f"- **Source URL:** {raw_url}",
                f"- **Domain:** {host}",
                f"- **Saved Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
            ]
            if raw_text and raw_text != raw_url:
                body_lines.extend(["", "## Notes & Excerpt", raw_text])
            elif html_plain:
                body_lines.extend(["", "## Web Clipping Excerpt", html_plain[:4000]])
            target_path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")

    # 4. Plain text or HTML snippet drop
    elif raw_text or raw_html:
        content_txt = raw_text or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_html)).strip()
        if len(content_txt) < 16:
            content_txt = f"# Dropped Note\n\n{content_txt}\n\nSaved to Good Shelves on {time.strftime('%Y-%m-%d')}."
        first_line = next((ln.strip().lstrip("# ") for ln in content_txt.splitlines() if ln.strip()), "Dropped Note")
        title_str = req.title.strip() or first_line[:60]
        slug = re.sub(r"[^a-z0-9]+", "-", title_str.lower()).strip("-")[:40] or f"note-{int(time.time())}"
        fname = req.filename.strip() or f"{slug}.md"
        if not Path(fname).suffix:
            fname = f"{fname}.md"
        target_path = _unique_download_target(dl_dir, fname)
        target_path.write_text(content_txt, encoding="utf-8")
    else:
        raise HTTPException(status_code=400, detail="Empty drop payload")

    item = st.index_single_file(target_path, force=True, emit_telemetry=True, recluster=True)
    if not item:
        raise HTTPException(status_code=400, detail=f"Could not index dropped item: {target_path.name}")
    report = st.build_ai_ingestion_report(item["id"], apply_auto_rules=True)
    item = st.get_file_by_id(item["id"]) or item
    dup = st.check_duplicate_for_file(item["id"])
    return {
        "ok": True,
        "file": item,
        "duplicateOf": dup,
        "aiIngestionReport": report,
        "state": st.get_all_state(),
    }


def _resolve_os_target(raw_path: str) -> Path:
    if any(ch in raw_path for ch in ("\0", "\r", "\n")):
        raise HTTPException(status_code=400, detail="Invalid characters in path")
    target = Path(os.path.expanduser(raw_path)).resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file() and not target.is_dir():
        raise HTTPException(status_code=400, detail="Unsupported file type")
    return target


@app.post("/os/open")
def api_os_open_file(req: FolderRequest) -> dict[str, Any]:
    target = _resolve_os_target(req.path)
    if target.is_file() and target.suffix.lower() in _BLOCKED_OPEN_SUFFIXES:
        raise HTTPException(status_code=403, detail="Refusing to launch executable file types")
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(target))  # type: ignore
        elif system == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "path": str(target)}


@app.post("/os/reveal")
def api_os_reveal_file(req: FolderRequest) -> dict[str, Any]:
    target = _resolve_os_target(req.path)
    folder = target.parent if target.is_file() else target
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer", f"/select,{target}"])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "path": str(target)}


@app.get("/devices")
def api_list_devices() -> dict[str, Any]:
    st = get_store()
    watched = {w["path"] for w in st.get_watched_folders()}
    from devices import list_connected_devices

    return {"devices": list_connected_devices(watched)}


@app.post("/devices/scan")
def api_scan_device(req: DeviceScanRequest) -> dict[str, Any]:
    try:
        res = get_store().scan_device(req.path, add_watch=req.addWatch)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/setup/complete")
def api_complete_setup(req: SetupCompleteRequest) -> dict[str, Any]:
    st = get_store()
    res = st.complete_setup(
        req.folders,
        user_name=req.userName,
        focus_areas=req.focusAreas,
        adaptive_learning=req.adaptiveLearning,
        terms_accepted=req.termsAccepted,
        scan_now=False,
    )
    active_folders = [wf for wf in st.get_watched_folders() if wf["active"]]
    if active_folders and not st.is_paused_on_battery():
        st.emit_progress("indexing", 0, 1, "Scanning selected folders…")

        def _bg_setup_scan() -> None:
            total = 0
            try:
                max_files = int(st.get_settings().get("max_scan_files", 500))
                for wf in active_folders:
                    if _shutdown_event.is_set() or st.is_paused_on_battery():
                        break
                    try:
                        total += st.scan_folder(
                            Path(wf["path"]),
                            max_files=min(max_files, 150) if max_files > 0 else 150,
                            recursive=True,
                            emit_idle=False,
                        )
                    except OSError:
                        continue
                st.verify_integrity()
                st.recluster_all()
            finally:
                st.emit_progress("idle", 1, 1, "")
                broadcast_event(
                    {"type": "index_updated", "indexedCount": total, "timestamp": time.time()}
                )

        threading.Thread(target=_bg_setup_scan, name="setup-initial-scan", daemon=True).start()
    return {"ok": True, **res, "state": st.get_all_state()}


@app.post("/setup/reset")
def api_reset_setup() -> dict[str, Any]:
    res = get_store().reset_setup()
    return {**res, "state": get_store().get_all_state()}


@app.post("/activity/clear")
def api_clear_activity() -> dict[str, Any]:
    res = get_store().clear_activity()
    return {**res, "state": get_store().get_all_state()}


@app.post("/feedback/interaction")
def api_feedback_interaction(req: FeedbackInteractionRequest) -> dict[str, Any]:
    return get_store().record_user_feedback(
        query=req.query,
        file_id=req.fileId,
        shelf_id=req.shelfId,
        action=req.action,
    )


@app.get("/file/raw")
def api_file_raw(file_id: str = Query(...)):
    f_dict = get_store().get_file_by_id(file_id)
    if not f_dict:
        raise HTTPException(status_code=404, detail="File not found")
    p = Path(f_dict["path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(str(p))


@app.get("/file/thumbnail")
def api_file_thumbnail(file_id: str = Query(...)):
    img_bytes = get_store().get_file_thumbnail_bytes(file_id)
    if not img_bytes:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return Response(
        content=img_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/file/pdf-page")
def api_pdf_page(file_id: str = Query(...), page: int = Query(0)):
    f_dict = get_store().get_file_by_id(file_id)
    if not f_dict:
        raise HTTPException(status_code=404, detail="File not found")
    p = Path(f_dict["path"])
    if not p.exists() or p.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Not a PDF file")
    import pymupdf
    from parsers import _FITZ_LOCK

    with _FITZ_LOCK:
        try:
            doc = pymupdf.open(str(p))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Corrupt or unreadable PDF: {exc}")
        try:
            if len(doc) == 0:
                raise HTTPException(status_code=400, detail="Empty PDF")
            idx = max(0, min(page, len(doc) - 1))
            pix = doc[idx].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
            img_bytes = pix.tobytes("jpeg")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Failed to render PDF page: {exc}")
        finally:
            doc.close()
    return Response(content=img_bytes, media_type="image/jpeg")


@app.post("/file/action")
def api_file_action(req: FileActionRequest) -> dict[str, Any]:
    try:
        res = get_store().execute_file_action(req.fileId, req.action)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/history/undo")
def api_undo_action(req: ActionIdRequest) -> dict[str, Any]:
    try:
        res = get_store().undo_action(req.actionId)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/history/redo")
def api_redo_action(req: ActionIdRequest) -> dict[str, Any]:
    try:
        res = get_store().redo_action(req.actionId)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/smart-clean/preview")
def api_smart_clean_preview() -> dict[str, Any]:
    return get_store().preview_smart_clean()


@app.post("/smart-clean/execute")
def api_smart_clean_execute(req: SmartCleanRequest) -> dict[str, Any]:
    res = get_store().execute_smart_clean(
        apply_renames=req.applyRenames,
        archive_duplicates=req.archiveDuplicates,
        organize_folders=req.organizeFolders,
    )
    return {"ok": True, **res, "state": get_store().get_all_state()}


class RemoveIndexRequest(BaseModel):
    path: str


class DeviceImportRequest(BaseModel):
    path: str
    destination: Optional[str] = None
    maxFiles: int = 120


class CreateFolderRequest(BaseModel):
    parentPath: str
    folderName: str


class FileTargetFolderRequest(BaseModel):
    fileId: str
    targetFolder: str


class BulkFileActionRequest(BaseModel):
    fileIds: list[str]
    action: str
    targetFolder: Optional[str] = None
    tag: Optional[str] = None


class FileTagsRequest(BaseModel):
    fileId: str
    tags: list[str]


class ShelfIdRequest(BaseModel):
    shelfId: str


class SaveSearchRequest(BaseModel):
    name: str
    query: str
    modality: str = "all"
    tag: str = ""


@app.post("/index/remove")
def api_remove_index(req: RemoveIndexRequest) -> dict[str, Any]:
    res = get_store().remove_file_by_path(req.path)
    return {"ok": True, **res, "state": get_store().get_all_state()}


@app.post("/devices/import")
def api_import_device(req: DeviceImportRequest) -> dict[str, Any]:
    try:
        res = get_store().import_from_device(req.path, req.destination, req.maxFiles)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/folders/create")
def api_create_folder(req: CreateFolderRequest) -> dict[str, Any]:
    try:
        return get_store().create_folder(req.parentPath, req.folderName)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/copy")
def api_copy_file(req: FileTargetFolderRequest) -> dict[str, Any]:
    try:
        res = get_store().copy_file(req.fileId, req.targetFolder)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/move")
def api_move_file(req: FileTargetFolderRequest) -> dict[str, Any]:
    try:
        res = get_store().move_file(req.fileId, req.targetFolder)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/trash")
def api_trash_file(req: FileIdRequest) -> dict[str, Any]:
    try:
        res = get_store().trash_file(req.fileId)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/rename")
def api_rename_file_custom(req: FileRenameRequest) -> dict[str, Any]:
    try:
        res = get_store().rename_file_custom(req.fileId, req.newName)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/delete")
def api_delete_file_permanently(req: FileIdRequest) -> dict[str, Any]:
    try:
        res = get_store().delete_file_permanently(req.fileId)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/open")
def api_open_file(req: FileIdRequest) -> dict[str, Any]:
    try:
        return get_store().open_file_external(req.fileId, reveal=req.reveal)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/files/bulk")
def api_bulk_files(req: BulkFileActionRequest) -> dict[str, Any]:
    try:
        res = get_store().bulk_file_action(req.fileIds, req.action, req.targetFolder, req.tag)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/pin")
def api_pin_file(req: FileIdRequest) -> dict[str, Any]:
    try:
        res = get_store().toggle_pin_file(req.fileId)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/tags")
def api_set_file_tags(req: FileTagsRequest) -> dict[str, Any]:
    try:
        res = get_store().set_file_tags(req.fileId, req.tags)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/file/shelf")
def api_set_file_shelf(req: FileShelfRequest) -> dict[str, Any]:
    try:
        res = get_store().set_file_shelf(req.fileId, req.shelfId)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/shelf/pin")
def api_pin_shelf(req: ShelfIdRequest) -> dict[str, Any]:
    try:
        res = get_store().toggle_pin_shelf(req.shelfId)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/searches/saved")
def api_list_saved_searches() -> list[dict[str, Any]]:
    return get_store().list_saved_searches()


@app.post("/searches/saved")
def api_save_search(req: SaveSearchRequest) -> dict[str, Any]:
    res = get_store().save_search(req.name, req.query, req.modality, req.tag)
    return {"ok": True, "item": res, "state": get_store().get_all_state()}


@app.delete("/searches/saved/{search_id}")
def api_delete_saved_search(search_id: str) -> dict[str, Any]:
    res = get_store().delete_saved_search(search_id)
    return {"ok": True, **res, "state": get_store().get_all_state()}


class VisionModelSelectRequest(BaseModel):
    modelId: str
    download: bool = True


@app.get("/vision/models")
def api_get_vision_models() -> dict[str, Any]:
    return get_store().get_vision_models()


@app.get("/vision/download-progress")
def api_get_vision_download_progress() -> dict[str, Any]:
    return get_store().get_vision_download_progress()


@app.post("/vision/select")
def api_select_vision_model(req: VisionModelSelectRequest) -> dict[str, Any]:
    try:
        res = get_store().select_vision_model(req.modelId, download=req.download)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/vision/reindex-images")
def api_reindex_images() -> dict[str, Any]:
    try:
        res = get_store().reindex_images_only()
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class FindSimilarRequest(BaseModel):
    fileId: str
    limit: int = 24


class AskRequest(BaseModel):
    question: str
    maxCitations: int = 6
    selectedFileId: Optional[str] = None
    recentFileIds: list[str] = []
    history: list[dict[str, str]] = []
    pendingAction: Optional[dict[str, Any]] = None
    scopeFolder: Optional[str] = None
    scopeRecursive: bool = True


class ExecuteAskActionRequest(BaseModel):
    actionType: str
    fileIds: list[str] = []
    unselectedIds: list[str] = []
    sourceQuery: Optional[str] = None
    targetFolder: Optional[str] = None
    createTargetFolder: bool = False
    folderPath: Optional[str] = None
    folderPaths: list[str] = []
    parentPath: Optional[str] = None
    folderName: Optional[str] = None
    shelfId: Optional[str] = None
    shelfName: Optional[str] = None
    tag: Optional[str] = None
    template: Optional[str] = None
    newName: Optional[str] = None
    renameMap: dict[str, str] = {}
    actionId: Optional[int] = None
    steps: list[dict[str, Any]] = []
    pairIds: list[str] = []
    kind: Optional[str] = None
    keyPattern: Optional[str] = None
    value: Optional[str] = None
    ruleText: Optional[str] = None
    # Phase 2/3 optional backward-compatible action payload fields
    moveMap: dict[str, str] = {}
    shelfMap: dict[str, str] = {}
    tagMap: dict[str, list[str]] = {}
    oldTag: Optional[str] = None
    newTag: Optional[str] = None
    fileAction: Optional[str] = None
    reveal: bool = False
    memoryId: Optional[int] = None
    searchName: Optional[str] = None
    searchQuery: Optional[str] = None
    keepStrategy: Optional[str] = None
    applyRenames: bool = True
    archiveDuplicates: bool = True
    organizeFolders: bool = True


class RenameFolderRequest(BaseModel):
    path: str
    newName: str


class PasteFilesRequest(BaseModel):
    fileIds: list[str]
    targetFolder: str
    mode: str = "copy"


class CustomShelfRequest(BaseModel):
    name: str
    desc: str = ""
    query: str = ""
    modality: str = "all"
    tag: str = ""
    fileIds: list[str] = []


class RenameShelfRequest(BaseModel):
    shelfId: str
    newName: str


class BatchRenameTemplateRequest(BaseModel):
    fileIds: list[str]
    template: str


@app.post("/search/similar")
def api_find_similar(req: FindSimilarRequest) -> dict[str, Any]:
    try:
        return get_store().find_similar_files(req.fileId, limit=req.limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/ask")
def api_ask_shelves(req: AskRequest) -> dict[str, Any]:
    return get_store().ask_your_shelves(
        req.question,
        max_citations=req.maxCitations,
        selected_file_id=req.selectedFileId,
        recent_file_ids=req.recentFileIds,
        history=req.history,
        pending_action=req.pendingAction,
        scope_folder=req.scopeFolder,
        scope_recursive=req.scopeRecursive,
    )


@app.post("/ask/execute")
def api_execute_ask_action(req: ExecuteAskActionRequest) -> dict[str, Any]:
    try:
        res = get_store().execute_ai_action(req.model_dump())
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/folders/pin")
def api_toggle_pin_folder(req: FolderRequest) -> dict[str, Any]:
    try:
        res = get_store().toggle_pin_folder(req.path)
        return {"ok": True, **res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/ai/memory/delete")
def api_delete_ai_memory(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        mem_id = int(body.get("id"))
        res = get_store().delete_ai_memory_entry(mem_id)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/ai/memory/clear")
def api_clear_ai_memory() -> dict[str, Any]:
    res = get_store().clear_ai_memory()
    return {**res, "state": get_store().get_all_state()}


@app.get("/folders/browse")
def api_browse_folder(path: Optional[str] = Query(None)) -> dict[str, Any]:
    try:
        return get_store().browse_folder(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/folders/rename")
def api_rename_folder(req: RenameFolderRequest) -> dict[str, Any]:
    try:
        res = get_store().rename_folder(req.path, req.newName)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/folders/delete")
def api_delete_folder(req: FolderRequest) -> dict[str, Any]:
    try:
        res = get_store().delete_folder(req.path)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/files/paste")
def api_paste_files(req: PasteFilesRequest) -> dict[str, Any]:
    try:
        res = get_store().paste_files(req.fileIds, req.targetFolder, req.mode)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/shelves/custom")
def api_create_custom_shelf(req: CustomShelfRequest) -> dict[str, Any]:
    try:
        res = get_store().create_custom_shelf(req.name, req.desc, req.query, req.modality, req.tag, req.fileIds)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/shelves/rename")
def api_rename_shelf(req: RenameShelfRequest) -> dict[str, Any]:
    try:
        res = get_store().rename_shelf(req.shelfId, req.newName)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/shelves/custom/{shelf_id}")
def api_delete_custom_shelf(shelf_id: str) -> dict[str, Any]:
    res = get_store().delete_custom_shelf(shelf_id)
    return {**res, "state": get_store().get_all_state()}


@app.get("/trash/list")
def api_list_trash() -> dict[str, Any]:
    return get_store().list_trash_and_archive()


@app.post("/trash/restore")
def api_restore_trash(req: FileIdRequest) -> dict[str, Any]:
    try:
        res = get_store().restore_trashed_file(req.fileId)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/trash/empty")
def api_empty_trash() -> dict[str, Any]:
    res = get_store().empty_trash()
    return {**res, "state": get_store().get_all_state()}


@app.post("/rename/template/preview")
def api_preview_rename_template(req: BatchRenameTemplateRequest) -> dict[str, Any]:
    return {"previews": get_store().preview_batch_rename_template(req.fileIds, req.template)}


@app.post("/rename/template/execute")
def api_execute_rename_template(req: BatchRenameTemplateRequest) -> dict[str, Any]:
    try:
        res = get_store().execute_batch_rename_template(req.fileIds, req.template)
        return {**res, "state": get_store().get_all_state()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/file/excerpt")
def api_file_excerpt(file_id: str = Query(...), max_lines: int = Query(500)) -> dict[str, Any]:
    try:
        return get_store().get_file_excerpt(file_id, max_lines=max_lines)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/file/context")
def api_file_context_dossier(file_id: str = Query(...)) -> dict[str, Any]:
    st = get_store()
    f_dict = st.get_file_by_id(file_id)
    if not f_dict:
        raise HTTPException(status_code=404, detail="File not found")
    from qa_engine import build_file_context_dossier, get_full_texts_batch
    full_map = get_full_texts_batch(st, [f_dict], fallback_excerpt=True)
    ft = full_map.get(str(file_id), {}).get("combined", "")
    dossier = build_file_context_dossier(st, f_dict, full_text_override=ft)
    return {"ok": True, "dossier": dossier}


@app.post("/ai/train")
def api_train_and_calibrate_ai(body: Optional[dict[str, Any]] = Body(default=None)) -> dict[str, Any]:
    st = get_store()
    from ai_training_feeder import run_continuous_ai_evaluation_and_feed, seed_foundational_ai_knowledge
    include_routes = bool((body or {}).get("includeDefaultRoutes", False))
    seeded = seed_foundational_ai_knowledge(st, include_default_routes=include_routes)
    report = run_continuous_ai_evaluation_and_feed(store=None, auto_learn=True)
    return {"ok": True, "seeded": seeded, "report": report, "state": st.get_all_state()}


@app.get("/insights")
def api_workspace_insights() -> dict[str, Any]:
    return get_store().get_workspace_insights()


@app.get("/insights/proactive")
def api_proactive_insights() -> dict[str, Any]:
    return {"insights": get_store().get_proactive_insights()}


@app.get("/telemetry")
def api_telemetry_summary() -> dict[str, Any]:
    return get_store().get_telemetry_summary()


@app.post("/telemetry/search")
def api_record_search(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        latency_ms = float(body.get("latency_ms", 0.0) or 0.0)
        result_count = int(body.get("result_count", 0) or 0)
        clicked_rank = int(body.get("clicked_rank", -1) if body.get("clicked_rank") is not None else -1)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid telemetry numeric value: {exc}")
    get_store().record_search_telemetry(
        query=str(body.get("query", "")),
        latency_ms=latency_ms,
        result_count=result_count,
        clicked_file_id=str(body.get("clicked_file_id", "")),
        clicked_rank=clicked_rank,
    )
    return {"ok": True}


@app.post("/telemetry/action")
def api_record_action(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        file_count = int(body.get("file_count", 0) or 0)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid telemetry numeric value: {exc}")
    get_store().record_action_feedback(
        action_type=str(body.get("action_type", "")),
        file_count=file_count,
        accepted=bool(body.get("accepted", False)),
    )
    return {"ok": True}


@app.post("/index/export")
def api_export_index(body: Optional[dict[str, Any]] = Body(default=None)) -> dict[str, Any]:
    st = get_store()
    raw_path = (body or {}).get("path") or None
    export_path = None
    if raw_path:
        candidate = Path(str(raw_path)).expanduser().resolve()
        try:
            candidate.relative_to(st.data_dir.resolve())
        except ValueError:
            raise HTTPException(
                status_code=403,
                detail="Exports must be written inside the app data directory",
            )
        export_path = str(candidate)
    res = st.export_index(export_path)
    return {"ok": True, **res}


@app.get("/events")
async def sse_pipeline_events(request: Request):
    """Server-Sent Events stream for real-time indexing stage telemetry."""
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
    with _subscribers_lock:
        event_subscribers.append(q)

    async def event_generator():
        try:
            while not _shutdown_event.is_set():
                if await request.is_disconnected():
                    break
                try:
                    evt = q.get_nowait()
                    yield f"data: {json.dumps(evt)}\n\n"
                except queue.Empty:
                    await asyncio.sleep(0.15)
        finally:
            with _subscribers_lock:
                if q in event_subscribers:
                    event_subscribers.remove(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Good Shelves Local Sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18420)
    args = parser.parse_args()

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    _uvicorn_server = uvicorn.Server(config)
    _uvicorn_server.run()
