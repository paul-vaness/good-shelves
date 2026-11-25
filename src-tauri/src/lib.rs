pub mod db;
pub mod fs_ops;
pub mod sidecar;
pub mod watcher;

use fs_ops::{
    compute_unified_line_diff, open_file_os, read_text_excerpt, reveal_file_os, FileExcerpt,
};
use serde_json::{json, Value};
use sidecar::{http_get, http_post, SidecarManager};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;

fn sync_asset_protocol_scope(app: &tauri::AppHandle) {
    let scope = app.asset_protocol_scope();
    for dir in db::get_active_watched_folders() {
        let _ = scope.allow_directory(&dir, true);
    }
}

fn try_focus_existing_instance(lock_path: &std::path::Path) -> bool {
    let Ok(raw) = std::fs::read_to_string(lock_path) else {
        return false;
    };
    let Some(port_str) = raw.trim().split(':').nth(1) else {
        return false;
    };
    let Ok(port) = port_str.parse::<u16>() else {
        return false;
    };
    let Ok(addr) = format!("127.0.0.1:{port}").parse::<SocketAddr>() else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(450)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(450)));
    if stream.write_all(b"GS_FOCUS\n").is_err() {
        return false;
    }
    let mut reader = BufReader::new(stream);
    let mut resp = String::new();
    let _ = reader.read_line(&mut resp);
    resp.trim() == "GS_PONG"
}

fn start_single_instance_listener(app: tauri::AppHandle, lock_path: PathBuf) {
    let Ok(listener) = TcpListener::bind(("127.0.0.1", 0)) else {
        return;
    };
    if let Ok(local_addr) = listener.local_addr() {
        let content = format!("{}:{}", std::process::id(), local_addr.port());
        let _ = std::fs::write(&lock_path, content);
    }
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
            let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
            let mut reader = BufReader::new((&stream).take(64));
            let mut line = String::new();
            if reader.read_line(&mut line).is_ok() && line.trim() == "GS_FOCUS" {
                let mut writer = &stream;
                let _ = writer.write_all(b"GS_PONG\n");
                if let Some(win) = app.get_webview_window("main") {
                    let _ = win.unminimize();
                    let _ = win.show();
                    let _ = win.set_focus();
                }
            }
        }
    });
}

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

fn validate_sidecar_request(
    method: &str,
    path: &str,
    timeout_ms: Option<u64>,
) -> Result<(String, Duration), String> {
    let method = method.trim().to_ascii_uppercase();
    if !matches!(method.as_str(), "GET" | "DELETE" | "POST" | "PUT" | "PATCH") {
        return Err(format!("Unsupported sidecar method: {method}"));
    }
    if !path.starts_with('/') {
        return Err("Sidecar path must start with '/'".to_string());
    }
    if path.len() > 4096 {
        return Err("Sidecar path is too long".to_string());
    }
    if path
        .bytes()
        .any(|b| b < 0x20 || b == 0x7f || b == b' ' || b == b'\\')
    {
        return Err("Invalid characters in sidecar path".to_string());
    }
    let route = path.split(['?', '#']).next().unwrap_or("");
    if route.split('/').any(|segment| segment == ".." || segment == ".") {
        return Err("Path traversal is not allowed in sidecar paths".to_string());
    }
    let timeout = Duration::from_millis(timeout_ms.unwrap_or(30_000).clamp(1_000, 300_000));
    Ok((method, timeout))
}

/// Proxy a sidecar HTTP request, attaching the auth token server-side.
/// This keeps the bearer token out of the renderer entirely.
#[tauri::command]
async fn sidecar_http(
    method: String,
    path: String,
    body: Option<Value>,
    timeout_ms: Option<u64>,
) -> Result<Value, String> {
    let (method, timeout) = validate_sidecar_request(&method, &path, timeout_ms)?;
    tauri::async_runtime::spawn_blocking(move || match method.as_str() {
        "GET" => sidecar::http_get_with_timeout(&path, timeout),
        "DELETE" => sidecar::http_delete_with_timeout(&path, timeout),
        "POST" | "PUT" | "PATCH" => {
            let payload = body.unwrap_or(Value::Null);
            sidecar::http_method_with_timeout(&method, &path, Some(&payload), timeout)
        }
        _ => Err(format!("Unsupported sidecar method: {method}")),
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn restart_sidecar(app: tauri::AppHandle) -> Result<bool, String> {
    let mgr = app.state::<SidecarManager>().inner().clone();
    let app_clone = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        mgr.shutdown();
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
async fn add_watched_folder(app: tauri::AppHandle, path: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let res = http_post("/folders/add", &json!({ "path": path }))?;
        watcher::reload_watched_folders();
        sync_asset_protocol_scope(&app);
        Ok(res)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn toggle_watched_folder(app: tauri::AppHandle, path: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let res = http_post("/folders/toggle", &json!({ "path": path }))?;
        watcher::reload_watched_folders();
        sync_asset_protocol_scope(&app);
        Ok(res)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn reload_watcher(app: tauri::AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        watcher::reload_watched_folders();
        sync_asset_protocol_scope(&app);
    })
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
        let raw_path = PathBuf::from(&path);
        let canonical = fs_ops::validate_trash_target(&raw_path)?;
        trash::delete(&canonical).map_err(|e| format!("failed to move file to trash: {e}"))?;
        let _ = sidecar::notify_file_removed(&raw_path);
        if canonical != raw_path {
            let _ = sidecar::notify_file_removed(&canonical);
        }
        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn update_setting(key: String, value: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let res = http_post("/settings/update", &json!({ "key": key, "value": value }))?;
        db::invalidate_watch_cache();
        Ok(res)
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
async fn diff_texts_native(old_text: String, new_text: String) -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(move || compute_unified_line_diff(&old_text, &new_text))
        .await
        .map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let lock_path = db::app_data_dir().join("instance.lock");
    if try_focus_existing_instance(&lock_path) {
        return;
    }

    let lock_path_setup = lock_path.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(move |app| {
            let sidecar_mgr = SidecarManager::new();
            let mgr_clone = sidecar_mgr.clone();
            let handle = app.handle().clone();
            app.manage(sidecar_mgr);
            start_single_instance_listener(handle.clone(), lock_path_setup.clone());
            std::thread::spawn(move || {
                watcher::start_folder_watcher();
                mgr_clone.ensure_started(Some(handle.clone()));
                watcher::reload_watched_folders();
                sync_asset_protocol_scope(&handle);
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_workspace_state,
            get_sidecar_url,
            sidecar_http,
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

    app.run(move |app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            let _ = std::fs::remove_file(&lock_path);
            if let Some(mgr) = app_handle.try_state::<SidecarManager>() {
                mgr.shutdown();
            }
        }
    });
}
