use serde::{Deserialize, Serialize};
use similar::{ChangeTag, TextDiff};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileExcerpt {
    pub path: String,
    pub content: String,
    pub total_lines: usize,
    pub truncated: bool,
}

/// Open a file using the native OS handler (`cmd /C start` on Windows, `xdg-open` on Linux, `open` on macOS).
pub fn open_file_os(path: &Path) -> Result<(), String> {
    if !path.exists() {
        return Err(format!("File does not exist: {}", path.display()));
    }

    #[cfg(target_os = "windows")]
    {
        Command::new("cmd")
            .args(["/C", "start", "", &path.to_string_lossy()])
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(path)
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "linux")]
    {
        Command::new("xdg-open")
            .arg(path)
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    Ok(())
}

/// Reveal a file inside Windows Explorer (`explorer /select,`) or Linux File Manager (`xdg-open` on parent dir).
pub fn reveal_file_os(path: &Path) -> Result<(), String> {
    if !path.exists() {
        return Err(format!("File does not exist: {}", path.display()));
    }

    #[cfg(target_os = "windows")]
    {
        let arg = format!("/select,{}", path.display());
        Command::new("explorer")
            .arg(arg)
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .args(["-R", &path.to_string_lossy()])
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
            .spawn()
            .map_err(|e| e.to_string())?;
    }

    Ok(())
}

/// Perform an atomic rename on disk using `std::fs::rename`.
pub fn atomic_rename_on_disk(old_path: &Path, new_name: &str) -> Result<PathBuf, String> {
    if !old_path.exists() {
        return Err(format!("Source file missing: {}", old_path.display()));
    }
    let parent = old_path
        .parent()
        .ok_or_else(|| "File has no parent directory".to_string())?;
    let target = parent.join(new_name);
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
    let dest = archive_dir.join(file_name);
    fs::rename(path, &dest).map_err(|e| e.to_string())?;
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

/// Read a UTF-8 preview excerpt from a local text/code file.
pub fn read_text_excerpt(path: &Path, max_lines: usize) -> Result<FileExcerpt, String> {
    let raw = fs::read_to_string(path).map_err(|e| format!("Cannot read text file: {e}"))?;
    let all_lines: Vec<&str> = raw.lines().collect();
    let total_lines = all_lines.len();
    let truncated = total_lines > max_lines;
    let slice = if truncated {
        &all_lines[..max_lines]
    } else {
        &all_lines[..]
    };
    Ok(FileExcerpt {
        path: path.to_string_lossy().to_string(),
        content: slice.join("\n"),
        total_lines,
        truncated,
    })
}
