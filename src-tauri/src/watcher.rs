use crate::db::{get_active_watched_folders, is_realtime_watch_enabled};
use crate::sidecar::{notify_file_changed, notify_file_removed};
use notify::{
    event::ModifyKind, Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher,
};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::mpsc::channel;
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const SUPPORTED_EXTS: &[&str] = &[
    // Images
    "png", "jpg", "jpeg", "webp", "bmp", "gif", "tiff", "tif", "heic", "avif", "svg",
    // Documents & spreadsheets & presentations
    "pdf", "docx", "doc", "txt", "md", "markdown", "log", "csv", "tsv", "xlsx", "xlsm", "xls",
    "pptx", "odt", "rtf",
    // Video
    "mp4", "mov", "mkv", "avi", "webm", "m4v",
    // Audio
    "mp3", "wav", "m4a", "flac", "ogg", "aac",
    // Archives
    "zip", "tar", "tgz", "gz",
    // Code & config
    "py", "rs", "ts", "tsx", "js", "jsx", "json", "html", "htm", "css", "sql", "php", "xml",
    "vue", "svelte", "rb", "kt", "swift", "c", "cpp", "h", "java", "go", "ini", "conf", "sh",
    "yaml", "yml", "toml",
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
        let lower = name.to_lowercase();
        if name.starts_with('.')
            || name.starts_with("~$")
            || name.ends_with('~')
            || lower.ends_with(".tmp")
            || lower.ends_with(".crdownload")
            || lower.ends_with(".part")
            || lower == "desktop.ini"
            || lower == "thumbs.db"
        {
            return false;
        }
    }
    for part in path.components() {
        let s = part.as_os_str().to_string_lossy();
        if (s.starts_with('.') && s != "." && s != "..")
            || s == "GoodShelves_Archive"
            || s == "GoodShelves_Trash"
            || s == "node_modules"
            || s == "__pycache__"
            || s == "target"
            || s == "dist"
            || s == "build"
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

#[derive(Clone, Copy, PartialEq, Eq)]
enum PendingKind {
    Change,
    Remove,
}

/// Start the cross-platform `notify` recursive folder watcher on a background thread
/// with a bounded trailing-edge debouncer so file bursts never spawn unbounded threads.
pub fn start_folder_watcher() {
    let (work_tx, work_rx) = channel::<(PathBuf, PendingKind)>();

    // Single worker thread serializes sidecar notifications cleanly
    thread::spawn(move || {
        let mut pending: HashMap<PathBuf, (PendingKind, Instant)> = HashMap::new();
        loop {
            while let Ok((p, kind)) = work_rx.try_recv() {
                pending.insert(p, (kind, Instant::now()));
            }
            let now = Instant::now();
            let ready: Vec<(PathBuf, PendingKind)> = pending
                .iter()
                .filter_map(|(p, (kind, ts))| {
                    if now.duration_since(*ts) >= Duration::from_millis(350) {
                        Some((p.clone(), *kind))
                    } else {
                        None
                    }
                })
                .collect();

            for (p, kind) in ready {
                pending.remove(&p);
                match kind {
                    PendingKind::Change if p.exists() => {
                        let _ = notify_file_changed(&p);
                    }
                    PendingKind::Change | PendingKind::Remove => {
                        if !p.exists() {
                            let _ = notify_file_removed(&p);
                        }
                    }
                }
            }

            if pending.is_empty() {
                match work_rx.recv() {
                    Ok((p, kind)) => {
                        pending.insert(p, (kind, Instant::now()));
                    }
                    Err(_) => break,
                }
            } else {
                thread::sleep(Duration::from_millis(120));
            }
        }
    });

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

        for res in rx {
            if !is_realtime_watch_enabled() {
                continue;
            }
            if let Ok(event) = res {
                match event.kind {
                    EventKind::Create(_) | EventKind::Modify(ModifyKind::Data(_)) | EventKind::Modify(ModifyKind::Metadata(_)) | EventKind::Modify(ModifyKind::Any) => {
                        for path in event.paths {
                            if path.exists() && is_supported_file(&path) {
                                let _ = work_tx.send((path, PendingKind::Change));
                            }
                        }
                    }
                    EventKind::Modify(ModifyKind::Name(_)) => {
                        for path in event.paths {
                            if !is_supported_path(&path) {
                                continue;
                            }
                            if path.exists() && path.is_file() {
                                let _ = work_tx.send((path, PendingKind::Change));
                            } else if !path.exists() {
                                let _ = work_tx.send((path, PendingKind::Remove));
                            }
                        }
                    }
                    EventKind::Remove(_) => {
                        for path in event.paths {
                            if is_supported_path(&path) {
                                let _ = work_tx.send((path, PendingKind::Remove));
                            }
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
            "php", "vue", "svelte", "rb", "kt", "swift", "svg", "doc", "log", "go", "java",
        ] {
            assert!(is_supported_extension(ext), "expected {ext} to be supported");
        }
        assert!(!is_supported_extension("exe"));
        assert!(!is_supported_extension("dll"));
    }

    #[test]
    fn ignores_hidden_and_excluded_dirs() {
        assert!(!is_supported_path(Path::new("/tmp/.hidden.py")));
        assert!(!is_supported_path(Path::new("/tmp/.git/hooks/pre-commit.sh")));
        assert!(!is_supported_path(Path::new("/tmp/Documents/~$budget.docx")));
        assert!(!is_supported_path(Path::new("/tmp/Documents/desktop.ini")));
        assert!(!is_supported_path(Path::new("/tmp/node_modules/app.ts")));
        assert!(!is_supported_path(Path::new("/tmp/GoodShelves_Archive/doc.pdf")));
        assert!(is_supported_path(Path::new("/tmp/Documents/report.pptx")));
    }
}
