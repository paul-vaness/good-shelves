use crate::db::{
    get_active_watched_folders, get_ignored_folder_names, invalidate_watch_cache,
    is_realtime_watch_enabled,
};
use crate::sidecar::notify_files_batch;
use notify::{
    event::ModifyKind, Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher,
};
use std::collections::{HashMap, HashSet};
use std::path::{Component, Path, PathBuf};
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

const BUILTIN_EXCLUDED_DIRS: &[&str] = &[
    "GoodShelves_Archive",
    "GoodShelves_Trash",
    "node_modules",
    "__pycache__",
    "target",
    "dist",
    "build",
];

const MAX_BATCH_SIZE: usize = 100;
const MAX_DIR_WALK_DEPTH: usize = 32;

fn component_matches_name(component: &str, name: &str) -> bool {
    if cfg!(any(target_os = "windows", target_os = "macos")) {
        component.eq_ignore_ascii_case(name)
    } else {
        component == name
    }
}

fn strip_watched_root<'a>(path: &'a Path, watched_roots: &[PathBuf]) -> &'a Path {
    let mut best: Option<&'a Path> = None;
    let mut best_root_len = 0usize;
    for root in watched_roots {
        if let Ok(rel) = path.strip_prefix(root) {
            let root_len = root.as_os_str().len();
            if root_len >= best_root_len {
                best_root_len = root_len;
                best = Some(rel);
            }
        }
    }
    best.unwrap_or(path)
}

pub fn is_path_excluded_under_roots(
    path: &Path,
    watched_roots: &[PathBuf],
    extra_ignores: &HashSet<String>,
) -> bool {
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
            return true;
        }
    }
    let rel_path = strip_watched_root(path, watched_roots);
    for part in rel_path.components() {
        let Component::Normal(os_part) = part else {
            continue;
        };
        let s = os_part.to_string_lossy();
        if s.starts_with('.')
            || BUILTIN_EXCLUDED_DIRS
                .iter()
                .any(|dir| component_matches_name(s.as_ref(), dir))
            || if cfg!(any(target_os = "windows", target_os = "macos")) {
                extra_ignores
                    .iter()
                    .any(|ig| s.eq_ignore_ascii_case(ig))
            } else {
                extra_ignores.contains(s.as_ref())
            }
        {
            return true;
        }
    }
    false
}

pub fn is_path_excluded(path: &Path, extra_ignores: &HashSet<String>) -> bool {
    is_path_excluded_under_roots(path, &[], extra_ignores)
}

pub fn is_supported_path_under_roots(
    path: &Path,
    watched_roots: &[PathBuf],
    extra_ignores: &HashSet<String>,
) -> bool {
    if is_path_excluded_under_roots(path, watched_roots, extra_ignores) {
        return false;
    }
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        return is_supported_extension(ext);
    }
    false
}

pub fn is_supported_path_with_ignores(path: &Path, extra_ignores: &HashSet<String>) -> bool {
    is_supported_path_under_roots(path, &[], extra_ignores)
}

pub fn is_supported_path(path: &Path) -> bool {
    let empty = HashSet::new();
    is_supported_path_with_ignores(path, &empty)
}

fn is_supported_file(
    path: &Path,
    watched_roots: &[PathBuf],
    extra_ignores: &HashSet<String>,
) -> bool {
    path.is_file() && is_supported_path_under_roots(path, watched_roots, extra_ignores)
}

fn is_removable_dir_or_file(
    path: &Path,
    watched_roots: &[PathBuf],
    extra_ignores: &HashSet<String>,
) -> bool {
    if is_path_excluded_under_roots(path, watched_roots, extra_ignores) {
        return false;
    }
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        return is_supported_extension(ext);
    }
    // Extensionless removed path is likely a moved/deleted subfolder
    true
}

fn collect_supported_files_in_dir(
    dir: &Path,
    watched_roots: &[PathBuf],
    extra_ignores: &HashSet<String>,
    depth: usize,
    out: &mut Vec<PathBuf>,
) {
    if depth > MAX_DIR_WALK_DEPTH || is_path_excluded_under_roots(dir, watched_roots, extra_ignores)
    {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let Ok(ft) = entry.file_type() else {
            continue;
        };
        if ft.is_symlink() {
            continue;
        }
        let p = entry.path();
        if ft.is_dir() {
            collect_supported_files_in_dir(&p, watched_roots, extra_ignores, depth + 1, out);
        } else if ft.is_file() && is_supported_path_under_roots(&p, watched_roots, extra_ignores) {
            out.push(p);
        }
    }
}

fn snapshot_watched_roots() -> Vec<PathBuf> {
    let Some(lock) = WATCHER_STATE.get() else {
        return Vec::new();
    };
    let Ok(guard) = lock.lock() else {
        return Vec::new();
    };
    guard
        .as_ref()
        .map(|s| s.watched.iter().cloned().collect())
        .unwrap_or_default()
}

/// Synchronize the active `notify` watcher roots with `get_active_watched_folders()`.
pub fn reload_watched_folders() {
    invalidate_watch_cache();
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
    Upsert,
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

            let mut changed_batch: Vec<PathBuf> = Vec::new();
            let mut removed_batch: Vec<PathBuf> = Vec::new();
            for (p, kind) in ready {
                pending.remove(&p);
                match kind {
                    PendingKind::Upsert if p.exists() => changed_batch.push(p),
                    PendingKind::Upsert | PendingKind::Remove => {
                        if !p.exists() {
                            removed_batch.push(p);
                        }
                    }
                }
            }
            let total = changed_batch.len() + removed_batch.len();
            if total > 0 {
                if total <= MAX_BATCH_SIZE {
                    let _ = notify_files_batch(&changed_batch, &removed_batch);
                } else {
                    for chunk in removed_batch.chunks(MAX_BATCH_SIZE) {
                        let _ = notify_files_batch(&[], chunk);
                    }
                    for chunk in changed_batch.chunks(MAX_BATCH_SIZE) {
                        let _ = notify_files_batch(chunk, &[]);
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

    thread::spawn(move || {
        for res in rx {
            if !is_realtime_watch_enabled() {
                continue;
            }
            let ignores = get_ignored_folder_names();
            let watched_roots = snapshot_watched_roots();
            if let Ok(event) = res {
                match event.kind {
                    EventKind::Create(_) => {
                        for path in event.paths {
                            if path.is_dir() {
                                let mut files = Vec::new();
                                collect_supported_files_in_dir(
                                    &path,
                                    &watched_roots,
                                    &ignores,
                                    0,
                                    &mut files,
                                );
                                for f in files {
                                    let _ = work_tx.send((f, PendingKind::Upsert));
                                }
                            } else if path.exists()
                                && is_supported_file(&path, &watched_roots, &ignores)
                            {
                                let _ = work_tx.send((path, PendingKind::Upsert));
                            }
                        }
                    }
                    EventKind::Modify(ModifyKind::Data(_)) | EventKind::Modify(ModifyKind::Any) => {
                        for path in event.paths {
                            if path.exists() && is_supported_file(&path, &watched_roots, &ignores) {
                                let _ = work_tx.send((path, PendingKind::Upsert));
                            }
                        }
                    }
                    EventKind::Modify(ModifyKind::Name(_)) => {
                        for path in event.paths {
                            if path.is_dir() {
                                let mut files = Vec::new();
                                collect_supported_files_in_dir(
                                    &path,
                                    &watched_roots,
                                    &ignores,
                                    0,
                                    &mut files,
                                );
                                for f in files {
                                    let _ = work_tx.send((f, PendingKind::Upsert));
                                }
                            } else if path.exists() && path.is_file() {
                                if is_supported_path_under_roots(&path, &watched_roots, &ignores) {
                                    let _ = work_tx.send((path, PendingKind::Upsert));
                                }
                            } else if !path.exists()
                                && is_removable_dir_or_file(&path, &watched_roots, &ignores)
                            {
                                let _ = work_tx.send((path, PendingKind::Remove));
                            }
                        }
                    }
                    EventKind::Remove(_) => {
                        for path in event.paths {
                            if is_removable_dir_or_file(&path, &watched_roots, &ignores) {
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

    #[test]
    fn ancestor_named_build_outside_watched_root_is_not_excluded() {
        let roots = vec![PathBuf::from("/mnt/build/Documents")];
        let ignores = HashSet::new();
        assert!(!is_path_excluded_under_roots(
            Path::new("/mnt/build/Documents/report.pdf"),
            &roots,
            &ignores
        ));
        assert!(is_path_excluded_under_roots(
            Path::new("/mnt/build/Documents/build/artifact.pdf"),
            &roots,
            &ignores
        ));
    }

    #[test]
    fn collect_supported_files_walks_nested_directory() {
        let dir = std::env::temp_dir().join("gs_test_watcher_walk");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::create_dir_all(dir.join("node_modules")).unwrap();
        std::fs::write(dir.join("sub").join("notes.md"), "hello").unwrap();
        std::fs::write(dir.join("node_modules").join("skip.js"), "ignored").unwrap();

        let roots = vec![dir.clone()];
        let ignores = HashSet::new();
        let mut files = Vec::new();
        collect_supported_files_in_dir(&dir, &roots, &ignores, 0, &mut files);
        assert_eq!(files.len(), 1);
        assert!(files[0].ends_with("notes.md"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
