use serde_json::Value;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Emitter};

pub const SIDECAR_HOST: &str = "127.0.0.1";
pub const SIDECAR_PORT: u16 = 18420;

pub struct SidecarManager {
    child: Arc<Mutex<Option<Child>>>,
}

impl SidecarManager {
    pub fn new() -> Self {
        Self {
            child: Arc::new(Mutex::new(None)),
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

    pub fn ensure_started(&self, app_handle: Option<AppHandle>) {
        if is_sidecar_healthy() {
            if let Some(app) = app_handle {
                spawn_sse_event_bridge(app);
            }
            return;
        }

        if let Some((bin_path, base_args)) = Self::resolve_sidecar_command() {
            let mut guard = self.child.lock().unwrap();
            if guard.is_none() {
                let mut cmd = Command::new(&bin_path);
                cmd.args(&base_args)
                    .arg("--host")
                    .arg(SIDECAR_HOST)
                    .arg("--port")
                    .arg(SIDECAR_PORT.to_string())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());

                #[cfg(target_os = "windows")]
                {
                    use std::os::windows::process::CommandExt;
                    const CREATE_NO_WINDOW: u32 = 0x08000000;
                    cmd.creation_flags(CREATE_NO_WINDOW);
                }

                if let Ok(child_proc) = cmd.spawn() {
                    *guard = Some(child_proc);
                }
            }
        }

        // Wait up to 6 seconds in fast 100ms polls for FastAPI sidecar readiness
        for _ in 0..60 {
            if is_sidecar_healthy() {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }

        if let Some(app) = app_handle {
            spawn_sse_event_bridge(app);
        }
    }
}

impl Drop for SidecarManager {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut c) = guard.take() {
                let _ = c.kill();
            }
        }
    }
}

pub fn is_sidecar_healthy() -> bool {
    http_get("/health").is_ok()
}

/// Perform a fast local loopback HTTP/1.0 GET request to `127.0.0.1:18420`.
pub fn http_get(endpoint: &str) -> Result<Value, String> {
    let addr: SocketAddr = format!("{SIDECAR_HOST}:{SIDECAR_PORT}")
        .parse()
        .map_err(|e| format!("Invalid address: {e}"))?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))
        .map_err(|e| format!("Sidecar connection error: {e}"))?;
    let _ = stream.set_nodelay(true);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(20)));

    let req = format!(
        "GET {endpoint} HTTP/1.0\r\nHost: {SIDECAR_HOST}:{SIDECAR_PORT}\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("Write error: {e}"))?;

    let mut raw_resp = Vec::with_capacity(16384);
    stream
        .read_to_end(&mut raw_resp)
        .map_err(|e| format!("Read error: {e}"))?;

    let resp_str = String::from_utf8_lossy(&raw_resp);
    parse_http_json_body(&resp_str)
}

/// Perform a fast local loopback HTTP/1.0 POST request with JSON payload to `127.0.0.1:18420`.
pub fn http_post(endpoint: &str, payload: &Value) -> Result<Value, String> {
    let addr: SocketAddr = format!("{SIDECAR_HOST}:{SIDECAR_PORT}")
        .parse()
        .map_err(|e| format!("Invalid address: {e}"))?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))
        .map_err(|e| format!("Sidecar connection error: {e}"))?;
    let _ = stream.set_nodelay(true);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));

    let body = serde_json::to_string(payload).map_err(|e| e.to_string())?;
    let req = format!(
        "POST {endpoint} HTTP/1.0\r\nHost: {SIDECAR_HOST}:{SIDECAR_PORT}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
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

    let resp_str = String::from_utf8_lossy(&raw_resp);
    parse_http_json_body(&resp_str)
}

fn decode_chunked_body(raw_body: &str) -> String {
    let mut out = String::with_capacity(raw_body.len());
    let mut rest = raw_body;
    while !rest.is_empty() {
        let Some((len_line, after_len)) = rest.split_once("\r\n") else {
            out.push_str(rest);
            break;
        };
        let len_hex = len_line.split(';').next().unwrap_or("").trim();
        let Ok(chunk_len) = usize::from_str_radix(len_hex, 16) else {
            out.push_str(rest);
            break;
        };
        if chunk_len == 0 {
            break;
        }
        if after_len.len() >= chunk_len {
            out.push_str(&after_len[..chunk_len]);
            let rem = &after_len[chunk_len..];
            rest = rem.strip_prefix("\r\n").unwrap_or(rem);
        } else {
            out.push_str(after_len);
            break;
        }
    }
    out
}

fn parse_http_json_body(raw_resp: &str) -> Result<Value, String> {
    let parts: Vec<&str> = raw_resp.splitn(2, "\r\n\r\n").collect();
    if parts.len() < 2 {
        return Err("Malformed HTTP response from sidecar".to_string());
    }
    let header = parts[0];
    let status_code = header
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .unwrap_or(200);

    let decoded_storage: String;
    let body = if header.to_lowercase().contains("transfer-encoding: chunked") {
        decoded_storage = decode_chunked_body(parts[1]);
        decoded_storage.trim()
    } else {
        parts[1].trim()
    };

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
        "force": true
    });
    http_post("/index/file", &payload)
}

pub fn notify_file_removed(path: &Path) -> Result<Value, String> {
    let payload = serde_json::json!({
        "path": path.to_string_lossy()
    });
    http_post("/index/remove", &payload)
}

/// Subscribe to the Python sidecar's `/events` SSE stream and emit `indexing://stage-update`
/// Tauri events in real time to the frontend window.
pub fn spawn_sse_event_bridge(app: AppHandle) {
    thread::spawn(move || loop {
        let addr: SocketAddr = match format!("{SIDECAR_HOST}:{SIDECAR_PORT}").parse() {
            Ok(a) => a,
            Err(_) => return,
        };
        if let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_secs(5)) {
            let _ = stream.set_nodelay(true);
            let req = format!(
                "GET /events HTTP/1.1\r\nHost: {SIDECAR_HOST}:{SIDECAR_PORT}\r\nAccept: text/event-stream\r\nConnection: keep-alive\r\n\r\n"
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
