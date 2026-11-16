#!/usr/bin/env python3
"""
Cross-platform PyInstaller packager for the Good Shelves Python sidecar.
Produces `src-tauri/binaries/good-shelves-engine-<target-triple>[.exe]` so Tauri
can bundle the Python + ONNX + SQLite-vec engine with zero system Python dependency.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path


def detect_target_triple() -> str:
    sys_name = platform.system()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        arch = machine

    if sys_name == "Windows":
        return f"{arch}-pc-windows-msvc"
    if sys_name == "Darwin":
        return f"{arch}-apple-darwin"
    return f"{arch}-unknown-linux-gnu"


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    engine_dir = repo_root / "python-engine"
    models_dir = engine_dir / "models"
    out_dir = repo_root / "src-tauri" / "binaries"
    out_dir.mkdir(parents=True, exist_ok=True)

    triple = detect_target_triple()
    ext = ".exe" if platform.system() == "Windows" else ""
    sep = ";" if platform.system() == "Windows" else ":"
    bin_name = f"good-shelves-engine-{triple}{ext}"

    models_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        f"good-shelves-engine-{triple}",
        "--distpath",
        str(out_dir),
        "--paths",
        str(engine_dir),
        "--add-data",
        f"{models_dir}{sep}models",
        "--collect-all",
        "sqlite_vec",
        "--collect-all",
        "onnxruntime",
        "--collect-all",
        "hdbscan",
        "--collect-all",
        "pymupdf",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "sklearn.decomposition",
        "--hidden-import",
        "sklearn.feature_extraction.text",
        "--hidden-import",
        "sklearn.metrics",
        "--hidden-import",
        "sklearn.metrics.pairwise",
        "--hidden-import",
        "multipart",
        "--hidden-import",
        "python_multipart",
        "--hidden-import",
        "openpyxl",
        "--hidden-import",
        "docx",
        str(engine_dir / "main.py"),
    ]

    print(f"[build_sidecar] Building {bin_name} ...")
    subprocess.check_call(cmd, cwd=str(repo_root))

    # Also keep an un-suffixed copy for direct sidecar resolution next to the binary
    plain_target = out_dir / f"good-shelves-engine{ext}"
    shutil.copy2(out_dir / bin_name, plain_target)
    print(f"[build_sidecar] Ready: {out_dir / bin_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
