pub mod db;
pub mod fs_ops;
pub mod sidecar;
pub mod watcher;

use fs_ops::{
    compute_unified_line_diff, open_file_os, read_text_excerpt, reveal_file_os, FileExcerpt,
};
use serde_json::{json, Value};
use sidecar::{http_get, http_post, SidecarManager};
use std::path::PathBuf;
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;

#[tauri::command]
fn get_workspace_state() -> Result<Value, String> {
    http_get("/state")
}

#[tauri::command]
fn semantic_search(
    query: String,
    modality: String,
    limit: Option<usize>,
    min_similarity: Option<f64>,
) -> Result<Value, String> {
    http_post(
        "/search",
        &json!({
            "query": query,
            "modality": modality,
            "limit": limit,
            "minSimilarity": min_similarity
        }),
    )
}

#[tauri::command]
fn toggle_rename(file_id: String) -> Result<Value, String> {
    http_post("/rename/toggle", &json!({ "fileId": file_id }))
}

#[tauri::command]
fn apply_all_renames() -> Result<Value, String> {
    http_post("/rename/apply-all", &json!({}))
}

#[tauri::command]
fn resolve_lookalike(pair_id: String) -> Result<Value, String> {
    http_post("/lookalikes/resolve", &json!({ "pairId": pair_id }))
}

#[tauri::command]
fn preview_tidy_folders() -> Result<Value, String> {
    http_get("/tidy-folders/preview")
}

#[tauri::command]
fn execute_tidy_folders() -> Result<Value, String> {
    http_post("/tidy-folders/execute", &json!({}))
}

#[tauri::command]
fn rollback_tidy_folders() -> Result<Value, String> {
    http_post("/tidy-folders/rollback", &json!({}))
}

#[tauri::command]
fn add_watched_folder(path: String) -> Result<Value, String> {
    let res = http_post("/folders/add", &json!({ "path": path }))?;
    watcher::reload_watched_folders();
    Ok(res)
}

#[tauri::command]
fn toggle_watched_folder(path: String) -> Result<Value, String> {
    let res = http_post("/folders/toggle", &json!({ "path": path }))?;
    watcher::reload_watched_folders();
    Ok(res)
}

#[tauri::command]
fn reload_watcher() -> Result<(), String> {
    watcher::reload_watched_folders();
    Ok(())
}

#[tauri::command]
fn pick_folder_native(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let picked = app.dialog().file().blocking_pick_folder();
    Ok(picked.map(|p| p.to_string()))
}

#[tauri::command]
fn trash_file_native(path: String) -> Result<(), String> {
    trash::delete(&path).map_err(|e| format!("failed to move file to trash: {e}"))
}

#[tauri::command]
fn update_setting(key: String, value: Value) -> Result<Value, String> {
    http_post("/settings/update", &json!({ "key": key, "value": value }))
}

#[tauri::command]
fn rescan_folders() -> Result<Value, String> {
    http_post("/index/rescan", &json!({}))
}

#[tauri::command]
fn verify_index() -> Result<Value, String> {
    http_post("/index/verify", &json!({}))
}

#[tauri::command]
fn ingest_file_path(path: String) -> Result<Value, String> {
    http_post("/index/file", &json!({ "path": path, "force": true }))
}

#[tauri::command]
fn open_file_native(path: String) -> Result<(), String> {
    let p = PathBuf::from(path);
    open_file_os(&p)
}

#[tauri::command]
fn reveal_file_native(path: String) -> Result<(), String> {
    let p = PathBuf::from(path);
    reveal_file_os(&p)
}

#[tauri::command]
fn read_file_excerpt(path: String, max_lines: Option<usize>) -> Result<FileExcerpt, String> {
    let p = PathBuf::from(path);
    read_text_excerpt(&p, max_lines.unwrap_or(120))
}

#[tauri::command]
fn diff_texts_native(old_text: String, new_text: String) -> Vec<String> {
    compute_unified_line_diff(&old_text, &new_text)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let sidecar_mgr = SidecarManager::new();
            let handle = app.handle().clone();
            sidecar_mgr.ensure_started(Some(handle));
            app.manage(sidecar_mgr);
            watcher::start_folder_watcher();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_workspace_state,
            semantic_search,
            toggle_rename,
            apply_all_renames,
            resolve_lookalike,
            preview_tidy_folders,
            execute_tidy_folders,
            rollback_tidy_folders,
            add_watched_folder,
            toggle_watched_folder,
            reload_watcher,
            pick_folder_native,
            trash_file_native,
            update_setting,
            rescan_folders,
            verify_index,
            ingest_file_path,
            open_file_native,
            reveal_file_native,
            read_file_excerpt,
            diff_texts_native
        ])
        .run(tauri::generate_context!())
        .expect("error while running good shelves desktop application");
}
