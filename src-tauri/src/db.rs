use rusqlite::{params, Connection, Result as SqlResult};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

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
    let conn = Connection::open(path)?;
    let _ = conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;");
    Ok(conn)
}

pub fn get_active_watched_folders() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(conn) = open_db() {
        if let Ok(mut stmt) =
            conn.prepare("SELECT path FROM watched_folders WHERE active = 1")
        {
            if let Ok(rows) = stmt.query_map([], |row| row.get::<_, String>(0)) {
                for p in rows.flatten() {
                    let pb = PathBuf::from(p);
                    if pb.exists() && pb.is_dir() {
                        out.push(pb);
                    }
                }
            }
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

pub fn is_realtime_watch_enabled() -> bool {
    if let Ok(conn) = open_db() {
        if let Ok(val) = conn.query_row(
            "SELECT value FROM settings WHERE key = 'realtime_watch'",
            params![],
            |r| r.get::<_, String>(0),
        ) {
            return val == "true";
        }
    }
    true
}
