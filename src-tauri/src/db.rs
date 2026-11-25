use rusqlite::{params, Connection, OpenFlags, Result as SqlResult};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

static WATCH_ENABLED_CACHE: AtomicBool = AtomicBool::new(true);
static WATCH_ENABLED_TS_MS: AtomicU64 = AtomicU64::new(0);
static IGNORE_PATTERNS_CACHE: OnceLock<Mutex<(u64, HashSet<String>)>> = OnceLock::new();

/// Resolve platform-native application data directory:
/// - Windows: `%APPDATA%\good-shelves`
/// - Linux: `~/.local/share/good-shelves`
/// - macOS: `~/Library/Application Support/good-shelves`
pub fn app_data_dir() -> PathBuf {
    if let Ok(custom) = std::env::var("GOOD_SHELVES_DATA_DIR") {
        let p = PathBuf::from(custom);
        let _ = std::fs::create_dir_all(&p);
        return p;
    }
    let base = dirs::data_dir().unwrap_or_else(|| {
        let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
        home.join(".local").join("share")
    });
    let dir = base.join("good-shelves");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

pub fn db_path() -> PathBuf {
    app_data_dir().join("index.db")
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WatchedFolderRecord {
    pub path: String,
    pub display_path: String,
    pub active: bool,
}

pub fn open_db() -> SqlResult<Connection> {
    let path = db_path();
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    let _ = conn.busy_timeout(std::time::Duration::from_millis(5000));
    Ok(conn)
}

fn default_watched_folders() -> Vec<PathBuf> {
    let mut out = Vec::new();
    for p in [dirs::document_dir(), dirs::download_dir(), dirs::desktop_dir()]
        .into_iter()
        .flatten()
    {
        if p.exists() {
            out.push(p);
        }
    }
    if out.is_empty() {
        if let Some(home) = dirs::home_dir() {
            for name in ["Documents", "Downloads", "Desktop"] {
                let p = home.join(name);
                if p.exists() {
                    out.push(p);
                }
            }
        }
    }
    out
}

pub fn get_active_watched_folders() -> Vec<PathBuf> {
    let path = db_path();
    if !path.exists() {
        return default_watched_folders();
    }
    let Ok(conn) = open_db() else {
        // DB exists but is busy or unreadable; do not override user settings with defaults.
        return Vec::new();
    };
    let count = match conn.query_row("SELECT COUNT(*) FROM watched_folders", [], |r| {
        r.get::<_, i64>(0)
    }) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };
    if count == 0 {
        return default_watched_folders();
    }

    let mut out = Vec::new();
    if let Ok(mut stmt) = conn.prepare("SELECT path FROM watched_folders WHERE active = 1") {
        if let Ok(rows) = stmt.query_map([], |row| row.get::<_, String>(0)) {
            for p in rows.flatten() {
                let pb = PathBuf::from(p);
                if pb.exists() && pb.is_dir() {
                    out.push(pb);
                }
            }
        }
    }
    out
}

pub fn invalidate_watch_cache() {
    WATCH_ENABLED_TS_MS.store(0, Ordering::Relaxed);
    if let Some(lock) = IGNORE_PATTERNS_CACHE.get() {
        if let Ok(mut g) = lock.lock() {
            g.0 = 0;
        }
    }
}

pub fn get_ignored_folder_names() -> HashSet<String> {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    let lock = IGNORE_PATTERNS_CACHE.get_or_init(|| Mutex::new((0, HashSet::new())));
    if let Ok(guard) = lock.lock() {
        if guard.0 > 0 && now_ms.saturating_sub(guard.0) < 2000 {
            return guard.1.clone();
        }
    }
    let mut set: HashSet<String> = [
        "GoodShelves_Archive",
        "GoodShelves_Trash",
        "node_modules",
        "__pycache__",
        "target",
        "dist",
        "build",
        ".git",
        ".venv",
    ]
    .into_iter()
    .map(String::from)
    .collect();

    if let Ok(conn) = open_db() {
        if let Ok(raw) = conn.query_row(
            "SELECT value FROM settings WHERE key = 'ignore_patterns'",
            params![],
            |r| r.get::<_, String>(0),
        ) {
            for pat in raw.split(',').map(str::trim).filter(|s| !s.is_empty()) {
                set.insert(pat.to_string());
            }
        }
    }

    if let Ok(mut guard) = lock.lock() {
        guard.0 = now_ms.max(1);
        guard.1 = set.clone();
    }
    set
}

pub fn is_realtime_watch_enabled() -> bool {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    let last_ms = WATCH_ENABLED_TS_MS.load(Ordering::Relaxed);
    if last_ms > 0 && now_ms.saturating_sub(last_ms) < 2000 {
        return WATCH_ENABLED_CACHE.load(Ordering::Relaxed);
    }
    let fallback = if last_ms > 0 {
        WATCH_ENABLED_CACHE.load(Ordering::Relaxed)
    } else {
        true
    };
    let enabled = if let Ok(conn) = open_db() {
        conn.query_row(
            "SELECT value FROM settings WHERE key = 'realtime_watch'",
            params![],
            |r| r.get::<_, String>(0),
        )
        .map(|val| val == "true")
        .unwrap_or(fallback)
    } else {
        fallback
    };
    WATCH_ENABLED_CACHE.store(enabled, Ordering::Relaxed);
    WATCH_ENABLED_TS_MS.store(now_ms.max(1), Ordering::Relaxed);
    enabled
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn db_path_respects_data_dir_env() {
        let dir = std::env::temp_dir().join("gs_test_db_dir");
        std::env::set_var("GOOD_SHELVES_DATA_DIR", &dir);
        let p = db_path();
        assert!(p.starts_with(&dir));
        assert_eq!(p.file_name().and_then(|n| n.to_str()), Some("index.db"));
        std::env::remove_var("GOOD_SHELVES_DATA_DIR");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
