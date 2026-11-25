use serde::{Deserialize, Serialize};
use similar::{ChangeTag, TextDiff};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileExcerpt {
    pub path: String,
    pub content: String,
    pub total_lines: usize,
    pub truncated: bool,
}

fn validate_os_path(path: &Path) -> Result<PathBuf, String> {
    let raw = path.to_string_lossy();
    if raw.contains('\0') || raw.contains('\r') || raw.contains('\n') {
        return Err("Invalid control characters in path".to_string());
    }
    let canonical = path
        .canonicalize()
        .map_err(|e| format!("Cannot resolve path {}: {e}", path.display()))?;
    let meta = fs::metadata(&canonical)
        .map_err(|e| format!("Cannot inspect path {}: {e}", canonical.display()))?;
    if !meta.is_file() && !meta.is_dir() {
        return Err("Unsupported file type (not a regular file or directory)".to_string());
    }
    Ok(canonical)
}

fn validate_readable_file(path: &Path) -> Result<PathBuf, String> {
    let canonical = validate_os_path(path)?;
    let meta = fs::metadata(&canonical)
        .map_err(|e| format!("Cannot inspect path {}: {e}", canonical.display()))?;
    if !meta.is_file() {
        return Err("Not a regular file".to_string());
    }
    Ok(canonical)
}

fn strip_windows_verbatim(path: &Path) -> PathBuf {
    let raw = path.to_string_lossy();
    if let Some(rest) = raw.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{rest}"))
    } else if let Some(rest) = raw.strip_prefix(r"\\?\") {
        PathBuf::from(rest)
    } else {
        path.to_path_buf()
    }
}

fn normalize_path_for_compare(path: &Path) -> PathBuf {
    let stripped = strip_windows_verbatim(path);
    #[cfg(target_os = "windows")]
    {
        PathBuf::from(stripped.to_string_lossy().to_lowercase())
    }
    #[cfg(not(target_os = "windows"))]
    {
        stripped
    }
}

/// Reject attempts to delete or trash filesystem roots, the home directory,
/// or well-known system locations.
pub fn validate_trash_target(path: &Path) -> Result<PathBuf, String> {
    let canonical = validate_os_path(path)?;
    let normalized = normalize_path_for_compare(&canonical);
    if canonical.parent().is_none() || normalized.parent().is_none() {
        return Err("Cannot trash a filesystem root".to_string());
    }

    let mut protected: Vec<PathBuf> = Vec::new();
    let mut push_protected = |p: PathBuf| {
        protected.push(normalize_path_for_compare(&p));
        if let Ok(c) = p.canonicalize() {
            protected.push(normalize_path_for_compare(&c));
        }
    };

    if let Some(home) = dirs::home_dir() {
        for name in ["Documents", "Downloads", "Desktop", "Pictures"] {
            push_protected(home.join(name));
        }
        push_protected(home);
    }
    for user_dir in [
        dirs::document_dir(),
        dirs::download_dir(),
        dirs::desktop_dir(),
        dirs::picture_dir(),
    ]
    .into_iter()
    .flatten()
    {
        push_protected(user_dir);
    }
    if let Some(data) = dirs::data_dir() {
        push_protected(data.join("good-shelves"));
        push_protected(data);
    }

    #[cfg(target_os = "windows")]
    {
        if let Ok(sys_root) = std::env::var("SystemRoot") {
            let sys_root_path = PathBuf::from(sys_root);
            let norm_sys_root = normalize_path_for_compare(&sys_root_path);
            if normalized.starts_with(&norm_sys_root) {
                return Err("Cannot trash a Windows system path".to_string());
            }
            if let Ok(canon_sys_root) = sys_root_path.canonicalize() {
                if normalized.starts_with(normalize_path_for_compare(&canon_sys_root)) {
                    return Err("Cannot trash a Windows system path".to_string());
                }
            }
        }
    }
    #[cfg(unix)]
    {
        if canonical == Path::new("/") {
            return Err("Cannot trash a filesystem root".to_string());
        }
        for root in [
            "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev", "/proc", "/sys",
        ] {
            if canonical.starts_with(root) {
                return Err("Cannot trash a system directory".to_string());
            }
        }
        for root in ["/root", "/var"] {
            if canonical == Path::new(root) {
                return Err("Cannot trash a system directory".to_string());
            }
        }
    }
    if protected.iter().any(|p| p == &normalized) {
        return Err("Cannot trash a protected folder".to_string());
    }
    Ok(canonical)
}

/// Open a file using the native OS handler (`Start-Process` on Windows, `xdg-open` on Linux, `open` on macOS).
pub fn open_file_os(path: &Path) -> Result<(), String> {
    let canonical = validate_os_path(path)?;

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        let target = strip_windows_verbatim(&canonical);
        Command::new("powershell")
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Start-Process -LiteralPath $env:GS_TARGET_PATH",
            ])
            .env("GS_TARGET_PATH", target)
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(&canonical)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "linux")]
    {
        Command::new("xdg-open")
            .arg(&canonical)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    Ok(())
}

/// Reveal a file inside Windows Explorer (`explorer /select,`), macOS Finder (`open -R`), or Linux File Manager.
pub fn reveal_file_os(path: &Path) -> Result<(), String> {
    let canonical = validate_os_path(path)?;

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        let target = strip_windows_verbatim(&canonical);
        let path_str = target.to_string_lossy();
        Command::new("explorer")
            .raw_arg(format!("/select,\"{}\"", path_str.replace('"', "")))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .args(["-R", &canonical.to_string_lossy()])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "linux")]
    {
        let parent = if canonical.is_dir() {
            canonical.clone()
        } else {
            canonical.parent().unwrap_or(&canonical).to_path_buf()
        };
        Command::new("xdg-open")
            .arg(parent)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    Ok(())
}

/// Perform an atomic rename on disk using `std::fs::rename`, validating against path traversal and overwrites.
pub fn atomic_rename_on_disk(old_path: &Path, new_name: &str) -> Result<PathBuf, String> {
    if !old_path.exists() {
        return Err(format!("Source file missing: {}", old_path.display()));
    }
    let trimmed = new_name.trim();
    if trimmed.is_empty() || trimmed.contains('/') || trimmed.contains('\\') {
        return Err("Invalid target filename".to_string());
    }
    let mut comps = Path::new(trimmed).components();
    match (comps.next(), comps.next()) {
        (Some(Component::Normal(_)), None) => {}
        _ => return Err("Invalid target filename".to_string()),
    }
    let parent = old_path
        .parent()
        .ok_or_else(|| "File has no parent directory".to_string())?;
    let target = parent.join(trimmed);
    let same_file_case_only = old_path
        .to_string_lossy()
        .eq_ignore_ascii_case(&target.to_string_lossy())
        && old_path
            .canonicalize()
            .ok()
            .zip(target.canonicalize().ok())
            .map(|(a, b)| a == b)
            .unwrap_or(false);
    if target.exists() && target != old_path && !same_file_case_only {
        return Err(format!("Target already exists: {}", target.display()));
    }
    fs::rename(old_path, &target).map_err(|e| format!("Failed to rename file: {e}"))?;
    Ok(target)
}

/// Move a redundant file to the OS Trash or `~/Documents/GoodShelves_Archive`.
pub fn archive_or_trash_file(path: &Path, use_os_trash: bool) -> Result<PathBuf, String> {
    let canonical = validate_trash_target(path)?;
    if use_os_trash {
        trash::delete(&canonical).map_err(|e| format!("OS Trash error: {e}"))?;
        return Ok(canonical);
    }
    let archive_dir = dirs::document_dir()
        .unwrap_or_else(|| {
            let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
            home.join("Documents")
        })
        .join("GoodShelves_Archive");
    fs::create_dir_all(&archive_dir).map_err(|e| e.to_string())?;
    let file_name = canonical
        .file_name()
        .ok_or_else(|| "Invalid filename".to_string())?;
    let mut dest = archive_dir.join(file_name);
    if dest.exists() {
        let stem = canonical.file_stem().and_then(|s| s.to_str()).unwrap_or("file");
        let ext = canonical.extension().and_then(|e| e.to_str()).unwrap_or("");
        let mut idx = 1usize;
        while dest.exists() {
            let candidate = if ext.is_empty() {
                format!("{stem}_{idx}")
            } else {
                format!("{stem}_{idx}.{ext}")
            };
            dest = archive_dir.join(candidate);
            idx += 1;
        }
    }
    if let Err(err) = fs::rename(&canonical, &dest) {
        // Handle cross-filesystem moves (EXDEV) safely via copy + remove
        if err.kind() == std::io::ErrorKind::CrossesDevices || err.raw_os_error() == Some(18) {
            fs::copy(&canonical, &dest).map_err(|e| e.to_string())?;
            fs::remove_file(&canonical).map_err(|e| e.to_string())?;
        } else {
            return Err(err.to_string());
        }
    }
    Ok(dest)
}

/// Compute a unified line-by-line diff between two UTF-8 text snippets using `similar`.
pub fn compute_unified_line_diff(old_text: &str, new_text: &str) -> Vec<String> {
    let diff = TextDiff::from_lines(old_text, new_text);
    let mut out = Vec::new();
    for change in diff.iter_all_changes() {
        let prefix = match change.tag() {
            ChangeTag::Delete => "- ",
            ChangeTag::Insert => "+ ",
            ChangeTag::Equal => "  ",
        };
        out.push(format!("{}{}", prefix, change.to_string().trim_end()));
        if out.len() >= 80 {
            break;
        }
    }
    out
}

/// Read a bounded UTF-8 preview excerpt from a local text/code file without loading huge files into RAM.
pub fn read_text_excerpt(path: &Path, max_lines: usize) -> Result<FileExcerpt, String> {
    let canonical = validate_readable_file(path)?;
    const MAX_EXCERPT_BYTES: usize = 256 * 1024;
    let file = File::open(&canonical).map_err(|e| format!("Cannot read text file: {e}"))?;
    let mut reader = BufReader::new(file);
    let mut lines = Vec::with_capacity(max_lines.min(256));
    let mut total_bytes = 0usize;
    let mut truncated = false;
    let mut buf = Vec::new();

    loop {
        if lines.len() >= max_lines || total_bytes >= MAX_EXCERPT_BYTES {
            truncated = reader
                .fill_buf()
                .map(|b| !b.is_empty())
                .unwrap_or(false);
            break;
        }
        buf.clear();
        let remaining = MAX_EXCERPT_BYTES - total_bytes;
        let limit = (remaining as u64).saturating_add(1);
        let n = (&mut reader)
            .take(limit)
            .read_until(b'\n', &mut buf)
            .map_err(|e| format!("Cannot read text file: {e}"))?;
        if n == 0 {
            break;
        }
        if buf.len() > remaining {
            buf.truncate(remaining);
            let text = String::from_utf8_lossy(&buf);
            lines.push(text.trim_end_matches(&['\r', '\n'][..]).to_string());
            truncated = true;
            break;
        }
        total_bytes += n;
        let text = String::from_utf8_lossy(&buf);
        lines.push(text.trim_end_matches(&['\r', '\n'][..]).to_string());
    }

    let total_lines = lines.len();
    Ok(FileExcerpt {
        path: path.to_string_lossy().to_string(),
        content: lines.join("\n"),
        total_lines,
        truncated,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rename_rejects_traversal_and_existing_target() {
        let dir = std::env::temp_dir().join("gs_test_rename_ops");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let f1 = dir.join("a.txt");
        let f2 = dir.join("b.txt");
        fs::write(&f1, "hello").unwrap();
        fs::write(&f2, "world").unwrap();

        assert!(atomic_rename_on_disk(&f1, "../evil.txt").is_err());
        assert!(atomic_rename_on_disk(&f1, "b.txt").is_err());
        let renamed = atomic_rename_on_disk(&f1, "c.txt").unwrap();
        assert!(renamed.exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn excerpt_caps_lines_and_handles_lossy_utf8() {
        let dir = std::env::temp_dir().join("gs_test_excerpt_ops");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let f = dir.join("sample.txt");
        fs::write(&f, b"line1\nline2\nline3\n\xff\xfe\nline5\n").unwrap();

        let ex = read_text_excerpt(&f, 2).unwrap();
        assert_eq!(ex.total_lines, 2);
        assert!(ex.truncated);
        assert_eq!(ex.content, "line1\nline2");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn excerpt_bounds_single_huge_line() {
        let dir = std::env::temp_dir().join("gs_test_excerpt_huge_line");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let f = dir.join("minified.js");
        let huge = vec![b'a'; 512 * 1024];
        fs::write(&f, &huge).unwrap();

        let ex = read_text_excerpt(&f, 100).unwrap();
        assert!(ex.truncated);
        assert_eq!(ex.total_lines, 1);
        assert!(ex.content.len() <= 256 * 1024);
        let _ = fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn validate_trash_target_blocks_system_subpaths() {
        assert!(validate_trash_target(Path::new("/")).is_err());
        assert!(validate_trash_target(Path::new("/etc/hosts")).is_err());
    }
}
