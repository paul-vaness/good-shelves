use serde::{Deserialize, Serialize};
use similar::{ChangeTag, TextDiff};
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileExcerpt {
    pub path: String,
    pub content: String,
    pub total_lines: usize,
    pub truncated: bool,
}

/// Open a file using the native OS handler (`Start-Process` on Windows, `xdg-open` on Linux, `open` on macOS).
pub fn open_file_os(path: &Path) -> Result<(), String> {
    if !path.exists() {
        return Err(format!("File does not exist: {}", path.display()));
    }

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        Command::new("powershell")
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Start-Process -LiteralPath $env:GS_TARGET_PATH",
            ])
            .env("GS_TARGET_PATH", path)
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
            .arg(path)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "linux")]
    {
        Command::new("xdg-open")
            .arg(path)
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
    if !path.exists() {
        return Err(format!("File does not exist: {}", path.display()));
    }

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        let path_str = path.to_string_lossy();
        let trimmed = path_str.strip_prefix(r"\\?\").unwrap_or(&path_str);
        Command::new("explorer")
            .raw_arg(format!("/select,\"{}\"", trimmed.replace('"', "")))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .args(["-R", &path.to_string_lossy()])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "linux")]
    {
        let parent = if path.is_dir() {
            path.to_path_buf()
        } else {
            path.parent().unwrap_or(path).to_path_buf()
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
    if target.exists() && target != old_path {
        return Err(format!("Target already exists: {}", target.display()));
    }
    fs::rename(old_path, &target).map_err(|e| format!("Failed to rename file: {e}"))?;
    Ok(target)
}

/// Move a redundant file to the OS Trash or `~/Documents/GoodShelves_Archive`.
pub fn archive_or_trash_file(path: &Path, use_os_trash: bool) -> Result<PathBuf, String> {
    if !path.exists() {
        return Err(format!("File not found: {}", path.display()));
    }
    if use_os_trash {
        trash::delete(path).map_err(|e| format!("OS Trash error: {e}"))?;
        return Ok(path.to_path_buf());
    }
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    let archive_dir = home.join("Documents").join("GoodShelves_Archive");
    fs::create_dir_all(&archive_dir).map_err(|e| e.to_string())?;
    let file_name = path
        .file_name()
        .ok_or_else(|| "Invalid filename".to_string())?;
    let mut dest = archive_dir.join(file_name);
    if dest.exists() {
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("file");
        let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
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
    if let Err(err) = fs::rename(path, &dest) {
        // Handle cross-filesystem moves (EXDEV) safely via copy + remove
        if err.kind() == std::io::ErrorKind::CrossesDevices || err.raw_os_error() == Some(18) {
            fs::copy(path, &dest).map_err(|e| e.to_string())?;
            fs::remove_file(path).map_err(|e| e.to_string())?;
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
    const MAX_EXCERPT_BYTES: usize = 256 * 1024;
    let file = File::open(path).map_err(|e| format!("Cannot read text file: {e}"))?;
    let mut reader = BufReader::new(file);
    let mut lines = Vec::with_capacity(max_lines.min(256));
    let mut total_lines = 0usize;
    let mut total_bytes = 0usize;
    let mut truncated = false;
    let mut buf = Vec::new();

    loop {
        buf.clear();
        let n = reader
            .read_until(b'\n', &mut buf)
            .map_err(|e| format!("Cannot read text file: {e}"))?;
        if n == 0 {
            break;
        }
        total_lines += 1;
        if lines.len() < max_lines && total_bytes < MAX_EXCERPT_BYTES {
            total_bytes += n;
            let text = String::from_utf8_lossy(&buf);
            lines.push(text.trim_end_matches(&['\r', '\n'][..]).to_string());
        } else {
            truncated = true;
        }
    }

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
        assert_eq!(ex.total_lines, 5);
        assert!(ex.truncated);
        assert_eq!(ex.content, "line1\nline2");
        let _ = fs::remove_dir_all(&dir);
    }
}
