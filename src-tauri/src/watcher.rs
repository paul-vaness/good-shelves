use crate::db::{get_active_watched_folders, is_realtime_watch_enabled};
use crate::sidecar::{notify_file_changed, notify_file_removed};
use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::mpsc::channel;
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const SUPPORTED_EXTS: &[&str] = &[
    // Images
    "png", "jpg", "jpeg", "webp", "bmp", "gif", "tiff", "tif",
    // Documents & spreadsheets & presentations
    "pdf", "docx", "txt", "md", "markdown", "csv", "tsv", "xlsx", "xlsm", "xls", "pptx", "odt",
    "rtf",
    // Video
    "mp4", "mov", "mkv", "avi", "webm", "m4v",
    // Audio
    "mp3", "wav", "m4a", "flac", "ogg", "aac",
    // Code & config
    "py", "rs", "ts", "tsx", "js", "jsx", "json", "html", "htm", "css", "sql", "php", "xml",
    "vue", "svelte", "rb", "kt", "swift", "ini", "conf", "sh", "yaml", "yml", "toml",
];

struct WatcherHandle {
    watcher: RecommendedWatcher,
    watched: HashSet<PathBuf>,
}

static WATCHER_STATE: OnceLock<Mutex<Option<WatcherHandle>>> = OnceLock::new();

pub fn is_supported_extension(ext: &str) -> bool {
    SUPPORTED_EXTS.contains(&ext.to_lowercase().as_str())
}

pub fn is_supported_path(path: &Path) -> bool {
    if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
        if name.starts_with('.') || name.ends_with('~') || name.ends_with(".tmp") {
            return false;
        }
    }
    for part in path.components() {
        let s = part.as_os_str().to_string_lossy();
        if s == "GoodShelves_Archive"
            || s == "GoodShelves_Trash"
            || s == "node_modules"
            || s == ".venv"
            || s == "target"
        {
            return false;
        }
    }
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        return is_supported_extension(ext);
    }
    false
}

fn is_supported_file(path: &Path) -> bool {
    path.is_file() && is_supported_path(path)
}

/// Synchronize the active `notify` watcher roots with `get_active_watched_folders()`.
pub fn reload_watched_folders() {
    let lock = WATCHER_STATE.get_or_init(|| Mutex::new(None));
    let mut guard = match lock.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    let Some(state) = guard.as_mut() else {
        return;
    };

    let desired: HashSet<PathBuf> = get_active_watched_folders().into_iter().collect();
    let to_remove: Vec<PathBuf> = state.watched.difference(&desired).cloned().collect();
    let to_add: Vec<PathBuf> = desired.difference(&state.watched).cloned().collect();

    for old_dir in to_remove {
        let _ = state.watcher.unwatch(&old_dir);
        state.watched.remove(&old_dir);
    }
    for new_dir in to_add {
        if new_dir.exists() && state.watcher.watch(&new_dir, RecursiveMode::Recursive).is_ok() {
            state.watched.insert(new_dir);
        }
    }
}

/// Start the cross-platform `notify` recursive folder watcher on a background thread.
/// Monitors user-configured folders and triggers the multimodal ingestion or removal pipeline.
pub fn start_folder_watcher() {
    thread::spawn(move || {
        let (tx, rx) = channel::<notify::Result<Event>>();
        let mut watcher: RecommendedWatcher = match Watcher::new(
            tx,
            Config::default().with_poll_interval(Duration::from_secs(2)),
        ) {
            Ok(w) => w,
            Err(_) => return,
        };

        let folders = get_active_watched_folders();
        let mut watched = HashSet::new();
        for folder in folders {
            if folder.exists() && watcher.watch(&folder, RecursiveMode::Recursive).is_ok() {
                watched.insert(folder);
            }
        }

        {
            let lock = WATCHER_STATE.get_or_init(|| Mutex::new(None));
            if let Ok(mut guard) = lock.lock() {
                *guard = Some(WatcherHandle { watcher, watched });
            }
        }

        let mut last_seen: HashMap<PathBuf, Instant> = HashMap::new();

        for res in rx {
            if !is_realtime_watch_enabled() {
                continue;
            }
            if let Ok(event) = res {
                match event.kind {
                    EventKind::Create(_) | EventKind::Modify(_) => {
                        for path in event.paths {
                            let exists = path.exists();
                            if exists && !is_supported_file(&path) {
                                continue;
                            }
                            if !exists && !is_supported_path(&path) {
                                continue;
                            }
                            let now = Instant::now();
                            if let Some(prev) = last_seen.get(&path) {
                                if now.duration_since(*prev) < Duration::from_secs(2) {
                                    continue;
                                }
                            }
                            last_seen.insert(path.clone(), now);
                            let p_clone = path.clone();
                            thread::spawn(move || {
                                thread::sleep(Duration::from_millis(250));
                                if p_clone.exists() {
                                    let _ = notify_file_changed(&p_clone);
                                } else {
                                    let _ = notify_file_removed(&p_clone);
                                }
                            });
                        }
                    }
                    EventKind::Remove(_) => {
                        for path in event.paths {
                            if !is_supported_path(&path) {
                                continue;
                            }
                            let p_clone = path.clone();
                            thread::spawn(move || {
                                let _ = notify_file_removed(&p_clone);
                            });
                        }
                    }
                    _ => {}
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn supports_expanded_extensions() {
        for ext in [
            "mp4", "mov", "mkv", "mp3", "wav", "flac", "xlsx", "xls", "pptx", "odt", "rtf",
            "php", "vue", "svelte", "rb", "kt", "swift",
        ] {
            assert!(is_supported_extension(ext), "expected {ext} to be supported");
        }
        assert!(!is_supported_extension("exe"));
        assert!(!is_supported_extension("dll"));
    }

    #[test]
    fn ignores_hidden_and_excluded_dirs() {
        assert!(!is_supported_path(Path::new("/tmp/.hidden.py")));
        assert!(!is_supported_path(Path::new("/tmp/node_modules/app.ts")));
        assert!(!is_supported_path(Path::new("/tmp/GoodShelves_Archive/doc.pdf")));
        assert!(is_supported_path(Path::new("/tmp/Documents/report.pptx")));
    }
}
