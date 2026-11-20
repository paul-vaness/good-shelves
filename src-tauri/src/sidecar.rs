use crate::db::app_data_dir;
use serde_json::{json, Value};
use std::collections::hash_map::RandomState;
use std::fs::{self, OpenOptions};
use std::hash::{BuildHasher, Hasher};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter, Manager};

pub const SIDECAR_HOST: &str = "127.0.0.1";
pub const SIDECAR_PORT: u16 = 18420;
const MAX_SIDECAR_LOG_BYTES: u64 = 2 * 1024 * 1024;
static ACTIVE_SIDECAR_PORT: AtomicU16 = AtomicU16::new(SIDECAR_PORT);
static SIDECAR_TOKEN: OnceLock<String> = OnceLock::new();

static SSE_BRIDGE_ACTIVE: AtomicBool = AtomicBool::new(false);

fn generate_hex_token() -> String {
    let mut bytes = [0u8; 32];
    let mut filled = false;
    #[cfg(unix)]
    {
        if let Ok(mut f) = fs::File::open("/dev/urandom") {
            if f.read_exact(&mut bytes).is_ok() {
                filled = true;
            }
        }
    }
    if !filled {
        let mut seed_parts = [0u64; 4];
        for (i, slot) in seed_parts.iter_mut().enumerate() {
            let mut h = RandomState::new().build_hasher();
            let nanos = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_nanos() as u64)
                .unwrap_or(0);
            h.write_u64(nanos ^ (std::process::id() as u64).wrapping_add(i as u64 * 0x9e3779b97f4a7c15));
            *slot = h.finish();
        }
        for (idx, chunk) in bytes.chunks_mut(8).enumerate() {
            chunk.copy_from_slice(&seed_parts[idx].to_le_bytes());
        }
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn active_sidecar_token() -> &'static str {
    SIDECAR_TOKEN.get_or_init(generate_hex_token).as_str()
}

pub fn active_sidecar_port() -> u16 {
    ACTIVE_SIDECAR_PORT.load(Ordering::SeqCst)
}

pub fn active_sidecar_url() -> String {
    format!("http://{SIDECAR_HOST}:{}", active_sidecar_port())
}

fn rotate_log_if_needed(log_path: &Path) {
    if let Ok(meta) = fs::metadata(log_path) {
        if meta.len() >= MAX_SIDECAR_LOG_BYTES {
            let backup = log_path.with_extension("log.1");
            let _ = fs::remove_file(&backup);
            let _ = fs::rename(log_path, &backup);
        }
    }
}

struct SidecarProcess {
    child: Mutex<Option<Child>>,
    startup_lock: Mutex<()>,
}

impl SidecarProcess {
    fn shutdown(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut c) = guard.take() {
                // Attempt graceful HTTP shutdown first so active SQLite WAL writes settle cleanly
                let _ = http_post_with_timeout("/shutdown", &json!({}), Duration::from_millis(800));
                for _ in 0..15 {
                    match c.try_wait() {
                        Ok(Some(_)) => return,
                        Ok(None) => thread::sleep(Duration::from_millis(100)),
                        Err(_) => break,
                    }
                }
                let _ = c.kill();
                let _ = c.wait();
            }
        }
    }
}

impl Drop for SidecarProcess {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[derive(Clone)]
pub struct SidecarManager {
    inner: Arc<SidecarProcess>,
}

impl SidecarManager {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(SidecarProcess {
                child: Mutex::new(None),
                startup_lock: Mutex::new(()),
            }),
        }
    }

    fn log_sidecar_note(msg: &str) {
        let log_path = app_data_dir().join("sidecar.log");
        rotate_log_if_needed(&log_path);
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&log_path) {
            let _ = writeln!(f, "[sidecar-mgr] {msg}");
        }
    }

    /// Locate either a bundled native `good-shelves-engine` sidecar binary (for packaged releases)
    /// or `python-engine/main.py` + virtual environment Python interpreter (for dev mode).
    fn resolve_sidecar_command() -> Option<(PathBuf, Vec<String>)> {
        let bin_name = if cfg!(target_os = "windows") {
            "good-shelves-engine.exe"
        } else {
            "good-shelves-engine"
        };

        if let Ok(exe) = std::env::current_exe() {
            if let Some(exe_dir) = exe.parent() {
                let bundled_candidates = [
                    exe_dir.join(bin_name),
                    exe_dir.join("binaries").join(bin_name),
                    exe_dir.join("..").join("Resources").join(bin_name),
                ];
                for cand in bundled_candidates {
                    if cand.exists() {
                        return Some((cand, Vec::new()));
                    }
                }
            }
        }

        let mut candidates = Vec::new();
        if let Ok(cwd) = std::env::current_dir() {
            candidates.push(cwd.clone());
            if let Some(parent) = cwd.parent() {
                candidates.push(parent.to_path_buf());
            }
        }
        if let Ok(exe) = std::env::current_exe() {
            let mut cur = exe.parent();
            for _ in 0..5 {
                if let Some(dir) = cur {
                    candidates.push(dir.to_path_buf());
                    cur = dir.parent();
                }
            }
        }

        for root in candidates {
            let script = root.join("python-engine").join("main.py");
            if script.exists() {
                #[cfg(target_os = "windows")]
                let venv_py = root.join(".venv").join("Scripts").join("python.exe");
                #[cfg(not(target_os = "windows"))]
                let venv_py = root.join(".venv").join("bin").join("python");

                let py_bin = if venv_py.exists() {
                    venv_py
                } else if cfg!(target_os = "windows") {
                    PathBuf::from("python")
                } else {
                    PathBuf::from("python3")
                };
                return Some((py_bin, vec![script.to_string_lossy().to_string()]));
            }
        }
        None
    }

    fn resolve_models_dir(app_handle: Option<&AppHandle>) -> Option<PathBuf> {
        if let Some(app) = app_handle {
            if let Ok(res_dir) = app.path().resource_dir() {
                for cand in [
                    res_dir.join("models"),
                    res_dir.join("python-engine").join("models"),
                ] {
                    if cand.join("manifest.json").exists() {
                        return Some(cand);
                    }
                }
            }
        }
        if let Ok(exe) = std::env::current_exe() {
            if let Some(exe_dir) = exe.parent() {
                for cand in [
                    exe_dir.join("models"),
                    exe_dir.join("..").join("Resources").join("models"),
                ] {
                    if cand.join("manifest.json").exists() {
                        return Some(cand);
                    }
                }
            }
        }
        None
    }

    fn pick_available_port() -> u16 {
        for port in SIDECAR_PORT..=SIDECAR_PORT + 10 {
            if TcpListener::bind((SIDECAR_HOST, port)).is_ok() {
                return port;
            }
        }
        SIDECAR_PORT
    }

    pub fn ensure_started(&self, app_handle: Option<AppHandle>) {
        let _startup_guard = self.inner.startup_lock.lock().unwrap();

        if is_sidecar_healthy() {
            crate::watcher::reload_watched_folders();
            if let Some(app) = app_handle {
                spawn_sse_event_bridge(app);
            }
            return;
        }

        let target_port = Self::pick_available_port();
        ACTIVE_SIDECAR_PORT.store(target_port, Ordering::SeqCst);

        if let Some((bin_path, base_args)) = Self::resolve_sidecar_command() {
            let mut guard = self.inner.child.lock().unwrap();
            let already_alive = guard
                .as_mut()
                .map(|c| matches!(c.try_wait(), Ok(None)))
                .unwrap_or(false);
            if !already_alive {
                *guard = None;
                let log_path = app_data_dir().join("sidecar.log");
                rotate_log_if_needed(&log_path);
                let stderr_stdio = OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&log_path)
                    .map(Stdio::from)
                    .unwrap_or_else(|_| Stdio::null());

                let mut cmd = Command::new(&bin_path);
                cmd.args(&base_args)
                    .arg("--host")
                    .arg(SIDECAR_HOST)
                    .arg("--port")
                    .arg(target_port.to_string())
                    .env("GOOD_SHELVES_TOKEN", active_sidecar_token())
                    .stdout(Stdio::null())
                    .stderr(stderr_stdio);

                if let Some(models_dir) = Self::resolve_models_dir(app_handle.as_ref()) {
                    cmd.env("GOOD_SHELVES_MODELS_DIR", models_dir);
                }

                #[cfg(target_os = "windows")]
                {
                    use std::os::windows::process::CommandExt;
                    const CREATE_NO_WINDOW: u32 = 0x08000000;
                    cmd.creation_flags(CREATE_NO_WINDOW);
                }

                match cmd.spawn() {
                    Ok(child_proc) => {
                        Self::log_sidecar_note(&format!(
                            "spawned {:?} {:?} on port {} (pid {})",
                            bin_path,
                            base_args,
                            target_port,
                            child_proc.id()
                        ));
                        *guard = Some(child_proc);
                    }
                    Err(err) => {
                        Self::log_sidecar_note(&format!("failed to spawn {:?}: {err}", bin_path));
                    }
                }
            }
        } else {
            Self::log_sidecar_note("could not locate sidecar binary or python-engine/main.py");
        }

        // Wait up to ~30 seconds (40ms initial fast polls, then 100ms polls)
        for attempt in 0..300 {
            if is_sidecar_healthy() {
                crate::watcher::reload_watched_folders();
                break;
            }
            if let Ok(mut guard) = self.inner.child.lock() {
                if let Some(child) = guard.as_mut() {
                    if let Ok(Some(status)) = child.try_wait() {
                        Self::log_sidecar_note(&format!("sidecar exited early with {status}"));
                        *guard = None;
                        break;
                    }
                }
            }
            let wait_ms = if attempt < 25 { 40 } else { 100 };
            thread::sleep(Duration::from_millis(wait_ms));
        }

        if let Some(app) = app_handle {
            spawn_sse_event_bridge(app);
        }
    }

    pub fn shutdown(&self) {
        self.inner.shutdown();
    }
}

impl Default for SidecarManager {
    fn default() -> Self {
        Self::new()
    }
}

pub fn is_sidecar_healthy() -> bool {
    match http_get("/health") {
        Ok(val) => {
            val.get("app").and_then(|v| v.as_str()) == Some("good-shelves")
                && val.get("status").and_then(|v| v.as_str()) == Some("ok")
                && val.get("tokenValid").and_then(|v| v.as_bool()).unwrap_or(true)
        }
        Err(_) => false,
    }
}

/// Perform a local loopback HTTP/1.0 GET request to the active sidecar port.
pub fn http_get(endpoint: &str) -> Result<Value, String> {
    let port = active_sidecar_port();
    let token = active_sidecar_token();
    let addr: SocketAddr = format!("{SIDECAR_HOST}:{port}")
        .parse()
        .map_err(|e| format!("Invalid address: {e}"))?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))
        .map_err(|e| format!("Sidecar connection error: {e}"))?;
    let _ = stream.set_nodelay(true);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(60)));

    let req = format!(
        "GET {endpoint} HTTP/1.0\r\nHost: {SIDECAR_HOST}:{port}\r\nX-GS-Token: {token}\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("Write error: {e}"))?;

    let mut raw_resp = Vec::with_capacity(16384);
    stream
        .read_to_end(&mut raw_resp)
        .map_err(|e| format!("Read error: {e}"))?;

    parse_http_json_bytes(&raw_resp)
}

/// Perform a local loopback HTTP/1.0 POST request with JSON payload and custom timeout.
pub fn http_post_with_timeout(endpoint: &str, payload: &Value, read_timeout: Duration) -> Result<Value, String> {
    let port = active_sidecar_port();
    let token = active_sidecar_token();
    let addr: SocketAddr = format!("{SIDECAR_HOST}:{port}")
        .parse()
        .map_err(|e| format!("Invalid address: {e}"))?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(3))
        .map_err(|e| format!("Sidecar connection error: {e}"))?;
    let _ = stream.set_nodelay(true);
    let _ = stream.set_read_timeout(Some(read_timeout));

    let body = serde_json::to_string(payload).map_err(|e| e.to_string())?;
    let req = format!(
        "POST {endpoint} HTTP/1.0\r\nHost: {SIDECAR_HOST}:{port}\r\nContent-Type: application/json\r\nX-GS-Token: {token}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("Write error: {e}"))?;

    let mut raw_resp = Vec::with_capacity(16384);
    stream
        .read_to_end(&mut raw_resp)
        .map_err(|e| format!("Read error: {e}"))?;

    parse_http_json_bytes(&raw_resp)
}

/// Perform a local loopback HTTP/1.0 POST request with JSON payload to the active sidecar port.
pub fn http_post(endpoint: &str, payload: &Value) -> Result<Value, String> {
    http_post_with_timeout(endpoint, payload, Duration::from_secs(120))
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

fn decode_chunked_bytes(raw_body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw_body.len());
    let mut rest = raw_body;
    while !rest.is_empty() {
        let Some(pos) = find_subsequence(rest, b"\r\n") else {
            out.extend_from_slice(rest);
            break;
        };
        let len_line = String::from_utf8_lossy(&rest[..pos]);
        let len_hex = len_line.split(';').next().unwrap_or("").trim();
        let Ok(chunk_len) = usize::from_str_radix(len_hex, 16) else {
            out.extend_from_slice(rest);
            break;
        };
        if chunk_len == 0 {
            break;
        }
        let after_len = &rest[pos + 2..];
        if after_len.len() >= chunk_len {
            out.extend_from_slice(&after_len[..chunk_len]);
            let rem = &after_len[chunk_len..];
            rest = rem.strip_prefix(b"\r\n").unwrap_or(rem);
        } else {
            out.extend_from_slice(after_len);
            break;
        }
    }
    out
}

fn parse_http_json_bytes(raw_resp: &[u8]) -> Result<Value, String> {
    let Some(split_idx) = find_subsequence(raw_resp, b"\r\n\r\n") else {
        return Err("Malformed HTTP response from sidecar".to_string());
    };
    let header_bytes = &raw_resp[..split_idx];
    let body_raw = &raw_resp[split_idx + 4..];

    let header = String::from_utf8_lossy(header_bytes);
    let status_code = header
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .unwrap_or(200);

    let decoded_bytes = if header.to_lowercase().contains("transfer-encoding: chunked") {
        decode_chunked_bytes(body_raw)
    } else {
        body_raw.to_vec()
    };

    let body_str = String::from_utf8_lossy(&decoded_bytes);
    let body = body_str.trim();
    let parsed: Value =
        serde_json::from_str(body).map_err(|e| format!("JSON parse error (HTTP {status_code}): {e}"))?;

    if status_code >= 400 {
        if let Some(detail) = parsed.get("detail").and_then(|d| d.as_str()) {
            return Err(detail.to_string());
        }
        return Err(format!("Sidecar returned HTTP {status_code}"));
    }

    Ok(parsed)
}

/// Notify the Python multimodal worker that a file was created or modified on disk.
pub fn notify_file_changed(path: &Path) -> Result<Value, String> {
    let payload = serde_json::json!({
        "path": path.to_string_lossy(),
        "force": false,
        "includeState": false
    });
    http_post("/index/file", &payload)
}

pub fn notify_file_removed(path: &Path) -> Result<Value, String> {
    let payload = serde_json::json!({
        "path": path.to_string_lossy()
    });
    http_post("/index/remove", &payload)
}

pub fn notify_files_batch(changed: &[PathBuf], removed: &[PathBuf]) -> Result<Value, String> {
    let changed_strs: Vec<String> = changed
        .iter()
        .map(|p| p.to_string_lossy().to_string())
        .collect();
    let removed_strs: Vec<String> = removed
        .iter()
        .map(|p| p.to_string_lossy().to_string())
        .collect();
    let payload = serde_json::json!({
        "changed": changed_strs,
        "removed": removed_strs
    });
    http_post("/index/batch", &payload)
}

/// Subscribe to the Python sidecar's `/events` SSE stream and emit `indexing://stage-update`
/// Tauri events in real time to the frontend window. Ensures at most one bridge thread runs.
pub fn spawn_sse_event_bridge(app: AppHandle) {
    if SSE_BRIDGE_ACTIVE.swap(true, Ordering::SeqCst) {
        return;
    }
    thread::spawn(move || loop {
        let port = active_sidecar_port();
        let addr: SocketAddr = match format!("{SIDECAR_HOST}:{port}").parse() {
            Ok(a) => a,
            Err(_) => {
                SSE_BRIDGE_ACTIVE.store(false, Ordering::SeqCst);
                return;
            }
        };
        if let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_secs(5)) {
            let _ = stream.set_nodelay(true);
            let token = active_sidecar_token();
            let req = format!(
                "GET /events HTTP/1.0\r\nHost: {SIDECAR_HOST}:{port}\r\nAccept: text/event-stream\r\nX-GS-Token: {token}\r\n\r\n"
            );
            if stream.write_all(req.as_bytes()).is_ok() {
                let reader = BufReader::new(stream);
                for line in reader.lines().map_while(Result::ok) {
                    if let Some(json_str) = line.strip_prefix("data: ") {
                        if let Ok(val) = serde_json::from_str::<Value>(json_str) {
                            let _ = app.emit("indexing://stage-update", val);
                        }
                    }
                }
            }
        }
        thread::sleep(Duration::from_secs(2));
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunked_utf8_multibyte_split_across_chunks_does_not_panic() {
        // Multi-byte UTF-8 sequence "—" (E2 80 94) split across chunk boundaries
        let json_payload = r#"{"ok":true,"label":"shelf — ₱120…"}"#;
        let bytes = json_payload.as_bytes();
        let split_at = bytes.iter().position(|&b| b == 0xE2).unwrap() + 1;
        let part1 = &bytes[..split_at];
        let part2 = &bytes[split_at..];

        let mut resp = Vec::new();
        resp.extend_from_slice(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n");
        resp.extend_from_slice(format!("{:x}\r\n", part1.len()).as_bytes());
        resp.extend_from_slice(part1);
        resp.extend_from_slice(b"\r\n");
        resp.extend_from_slice(format!("{:x}\r\n", part2.len()).as_bytes());
        resp.extend_from_slice(part2);
        resp.extend_from_slice(b"\r\n0\r\n\r\n");

        let parsed = parse_http_json_bytes(&resp).expect("should parse split UTF-8 chunks cleanly");
        assert_eq!(parsed["ok"], true);
        assert_eq!(parsed["label"], "shelf — ₱120…");
    }

    #[test]
    fn http_error_surfaces_detail_message() {
        let resp = b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n\r\n{\"detail\":\"Unknown model\"}";
        let err = parse_http_json_bytes(resp).unwrap_err();
        assert_eq!(err, "Unknown model");
    }

    #[test]
    fn cloning_sidecar_manager_shares_inner_without_premature_drop_shutdown() {
        let mgr = SidecarManager::new();
        assert_eq!(Arc::strong_count(&mgr.inner), 1);
        {
            let clone = mgr.clone();
            assert_eq!(Arc::strong_count(&mgr.inner), 2);
            drop(clone);
        }
        assert_eq!(Arc::strong_count(&mgr.inner), 1);
    }

    #[test]
    fn sidecar_token_is_64_hex_chars_and_stable() {
        let t1 = active_sidecar_token();
        let t2 = active_sidecar_token();
        assert_eq!(t1, t2);
        assert_eq!(t1.len(), 64);
        assert!(t1.chars().all(|c| c.is_ascii_hexdigit()));
    }
}
