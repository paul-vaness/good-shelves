#!/usr/bin/env python3
"""
Good Shelves Local Multimodal AI Sidecar Server (FastAPI + SSE + JSON-RPC over 127.0.0.1).
Provides real-time file parsing, OCR, 512-d ONNX embedding, SQLite-vec search,
UMAP+HDBSCAN shelf discovery, lookalike diffing, and live ingestion telemetry.
"""

from __future__ import annotations

import argparse
import asyncio
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
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from vector_store import VectorStore, get_user_special_dir
from devices import devices_signature, list_connected_devices


# ============================================================================
# FastAPI Application & Real-Time Event Broadcaster
# ============================================================================

_shutdown_event = threading.Event()
_store_lock = threading.Lock()


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


app = FastAPI(title="Good Shelves Multimodal Engine", version="1.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^tauri://localhost$|^https?://tauri\.localhost$",
    allow_methods=["*"],
    allow_headers=["*"],
)

store: Optional[VectorStore] = None
event_subscribers: list[queue.Queue[dict[str, Any]]] = []


def broadcast_event(evt: dict[str, Any]) -> None:
    for q in list(event_subscribers):
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
    settings = st.get_settings()
    if not settings.get("setup_completed", True):
        return
    if st.is_paused_on_battery():
        return

    max_files = int(settings.get("max_scan_files", 500))
    for folder in st.get_watched_folders():
        if not folder["active"]:
            continue
        try:
            st.scan_folder(Path(folder["path"]), max_files=max_files, recursive=True)
        except OSError:
            continue

    st.verify_integrity()
    st.recluster_all()


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
    force: bool = True


class DeviceScanRequest(BaseModel):
    path: str
    addWatch: bool = False


class SetupCompleteRequest(BaseModel):
    folders: list[str]


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
def health_check() -> dict[str, Any]:
    st = get_store()
    return {"app": "good-shelves", "status": "ok", "dbPath": str(st.db_path), "silhouette": st.silhouette_score}


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


@app.post("/lookalikes/resolve")
def api_resolve_lookalike(req: PairIdRequest) -> dict[str, Any]:
    try:
        res = get_store().toggle_archive_lookalike(req.pairId)
        get_store().recluster_all()
        return {"ok": True, **res, "state": get_store().get_all_state()}
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


@app.post("/settings/update")
def api_update_setting(req: SettingUpdateRequest) -> dict[str, Any]:
    get_store().update_setting(req.key, req.value)
    return {"ok": True, "state": get_store().get_all_state()}


@app.post("/index/rescan")
def api_rescan_all(all_files: bool = Query(False)) -> dict[str, Any]:
    st = get_store()
    max_f = 0 if all_files else int(st.get_settings().get("max_scan_files", 500))
    total = 0
    for wf in st.get_watched_folders():
        if wf["active"]:
            total += st.scan_folder(Path(wf["path"]), max_files=max_f, recursive=True)
    st.verify_integrity()
    st.recluster_all()
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
    item = st.index_single_file(Path(req.path), force=req.force, emit_telemetry=True, recluster=True)
    if not item:
        raise HTTPException(status_code=400, detail=f"Could not index {req.path}")
    dup = st.check_duplicate_for_file(item["id"])
    return {"ok": True, "file": item, "duplicateOf": dup, "state": st.get_all_state()}


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
    content = await file.read()
    target_path.write_bytes(content)

    st = get_store()
    item = st.index_single_file(target_path, force=True, emit_telemetry=True, recluster=True)
    if not item:
        raise HTTPException(status_code=400, detail=f"Unsupported or empty file: {safe_name}")
    dup = st.check_duplicate_for_file(item["id"])
    return {"ok": True, "file": item, "duplicateOf": dup, "state": st.get_all_state()}


@app.post("/os/open")
def api_os_open_file(req: FolderRequest) -> dict[str, Any]:
    target = Path(os.path.expanduser(req.path)).resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
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
    target = Path(os.path.expanduser(req.path)).resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
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
    res = get_store().complete_setup(req.folders)
    return {"ok": True, **res, "state": get_store().get_all_state()}


@app.get("/file/raw")
def api_file_raw(file_id: str = Query(...)):
    f_dict = get_store().get_file_by_id(file_id)
    if not f_dict:
        raise HTTPException(status_code=404, detail="File not found")
    p = Path(f_dict["path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(str(p))


@app.get("/file/pdf-page")
def api_pdf_page(file_id: str = Query(...), page: int = Query(0)):
    f_dict = get_store().get_file_by_id(file_id)
    if not f_dict:
        raise HTTPException(status_code=404, detail="File not found")
    p = Path(f_dict["path"])
    if not p.exists() or p.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Not a PDF file")
    import pymupdf

    doc = pymupdf.open(str(p))
    try:
        if len(doc) == 0:
            raise HTTPException(status_code=400, detail="Empty PDF")
        idx = max(0, min(page, len(doc) - 1))
        pix = doc[idx].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        img_bytes = pix.tobytes("jpeg")
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
    return get_store().ask_your_shelves(req.question, max_citations=req.maxCitations)


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


@app.get("/insights")
def api_workspace_insights() -> dict[str, Any]:
    return get_store().get_workspace_insights()


@app.get("/events")
async def sse_pipeline_events(request: Request):
    """Server-Sent Events stream for real-time indexing stage telemetry."""
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
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
            if q in event_subscribers:
                event_subscribers.remove(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Good Shelves Local Sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18420)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
