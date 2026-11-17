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
async fn get_workspace_state() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| http_get("/state"))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
fn get_sidecar_url() -> String {
    sidecar::active_sidecar_url()
}

#[tauri::command]
async fn restart_sidecar(app: tauri::AppHandle) -> Result<bool, String> {
    let mgr = app.state::<SidecarManager>().inner().clone();
    let app_clone = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        mgr.ensure_started(Some(app_clone));
        sidecar::is_sidecar_healthy()
    })
    .await
    .map_err(|e| e.to_string())
}

#[tauri::command]
async fn semantic_search(
    query: String,
    modality: String,
    limit: Option<usize>,
    min_similarity: Option<f64>,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        http_post(
            "/search",
            &json!({
                "query": query,
                "modality": modality,
                "limit": limit,
                "minSimilarity": min_similarity
            }),
        )
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn toggle_rename(file_id: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || http_post("/rename/toggle", &json!({ "fileId": file_id })))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn apply_all_renames() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| http_post("/rename/apply-all", &json!({})))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn resolve_lookalike(pair_id: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || http_post("/lookalikes/resolve", &json!({ "pairId": pair_id })))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn preview_tidy_folders() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| http_get("/tidy-folders/preview"))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn execute_tidy_folders() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| http_post("/tidy-folders/execute", &json!({})))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn rollback_tidy_folders() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| http_post("/tidy-folders/rollback", &json!({})))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn add_watched_folder(path: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let res = http_post("/folders/add", &json!({ "path": path }))?;
        watcher::reload_watched_folders();
        Ok(res)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn toggle_watched_folder(path: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let res = http_post("/folders/toggle", &json!({ "path": path }))?;
        watcher::reload_watched_folders();
        Ok(res)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn reload_watcher() -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(watcher::reload_watched_folders)
        .await
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
async fn pick_folder_native(app: tauri::AppHandle) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let picked = app.dialog().file().blocking_pick_folder();
        Ok(picked.map(|p| p.to_string()))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn trash_file_native(path: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        trash::delete(&path).map_err(|e| format!("failed to move file to trash: {e}"))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn update_setting(key: String, value: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        http_post("/settings/update", &json!({ "key": key, "value": value }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn rescan_folders() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| http_post("/index/rescan", &json!({})))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn verify_index() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| http_post("/index/verify", &json!({})))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn ingest_file_path(path: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        http_post("/index/file", &json!({ "path": path, "force": true }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn open_file_native(path: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || open_file_os(&PathBuf::from(path)))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn reveal_file_native(path: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || reveal_file_os(&PathBuf::from(path)))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn read_file_excerpt(path: String, max_lines: Option<usize>) -> Result<FileExcerpt, String> {
    tauri::async_runtime::spawn_blocking(move || {
        read_text_excerpt(&PathBuf::from(path), max_lines.unwrap_or(120))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn diff_texts_native(old_text: String, new_text: String) -> Vec<String> {
    compute_unified_line_diff(&old_text, &new_text)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let sidecar_mgr = SidecarManager::new();
            let mgr_clone = sidecar_mgr.clone();
            let handle = app.handle().clone();
            app.manage(sidecar_mgr);
            watcher::start_folder_watcher();
            std::thread::spawn(move || {
                mgr_clone.ensure_started(Some(handle));
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_workspace_state,
            get_sidecar_url,
            restart_sidecar,
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
        .build(tauri::generate_context!())
        .expect("error while building good shelves desktop application");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(mgr) = app_handle.try_state::<SidecarManager>() {
                mgr.shutdown();
            }
        }
    });
}
