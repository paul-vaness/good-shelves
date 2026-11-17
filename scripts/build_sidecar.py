#!/usr/bin/env python3
"""
Cross-platform PyInstaller Sidecar Builder for Good Shelves.
Builds `src-tauri/binaries/good-shelves-engine-<target_triple>(.exe)` for Tauri `externalBin`.
Models are packaged separately via Tauri `bundle.resources` so cold startup stays fast (<2s).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def detect_target_triple() -> str:
    try:
        out = subprocess.check_output(["rustc", "-vV"], text=True)
        for line in out.splitlines():
            if line.startswith("host:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass

    sys_name = platform.system()
    machine = platform.machine().lower()
    if sys_name == "Windows":
        return "x86_64-pc-windows-msvc"
    if sys_name == "Darwin":
        return "aarch64-apple-darwin" if "arm" in machine or "aarch64" in machine else "x86_64-apple-darwin"
    if "aarch64" in machine or "arm64" in machine:
        return "aarch64-unknown-linux-gnu"
    return "x86_64-unknown-linux-gnu"


def build_sidecar(target_triple: str) -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    engine_dir = repo_root / "python-engine"
    dist_dir = repo_root / "build-sidecar-dist"
    work_dir = repo_root / "build-sidecar-work"
    binaries_dir = repo_root / "src-tauri" / "binaries"
    binaries_dir.mkdir(parents=True, exist_ok=True)

    is_windows = "windows" in target_triple
    exe_suffix = ".exe" if is_windows else ""
    out_name = f"good-shelves-engine-{target_triple}{exe_suffix}"
    final_bin = binaries_dir / out_name

    # Clean previous temp build artifacts
    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)

    hidden_imports = [
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "sqlite_vec",
        "hdbscan",
        "sklearn.neighbors._partition_nodes",
        "sklearn.utils._cython_blas",
        "sklearn.metrics._pairwise_distances_reduction._datasets_pair",
        "sklearn.metrics._pairwise_distances_reduction._middle_term_computer",
        "rapidocr_onnxruntime",
        "openpyxl",
        "docx",
        "pymupdf",
        "fitz",
        "psutil",
        "PIL",
        "tokenizers",
        "onnxruntime",
    ]

    collect_all = [
        "sqlite_vec",
        "rapidocr_onnxruntime",
        "onnxruntime",
        "tokenizers",
    ]

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "good-shelves-engine",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        "--paths",
        str(engine_dir),
    ]
    if is_windows:
        cmd.append("--noconsole")

    for mod in hidden_imports:
        cmd.extend(["--hidden-import", mod])
    for pkg in collect_all:
        cmd.extend(["--collect-all", pkg])

    cmd.append(str(engine_dir / "main.py"))

    print(f"[build_sidecar] Building {out_name} using {sys.executable}...")
    subprocess.run(cmd, check=True, cwd=str(repo_root))

    built_bin = dist_dir / f"good-shelves-engine{exe_suffix}"
    if not built_bin.exists():
        raise FileNotFoundError(f"Expected PyInstaller binary at {built_bin}")

    shutil.copy2(built_bin, final_bin)
    if not is_windows:
        os.chmod(final_bin, 0o755)

    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    print(f"[build_sidecar] Built sidecar binary -> {final_bin} ({final_bin.stat().st_size / (1024*1024):.1f} MB)")
    return final_bin


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Good Shelves sidecar binary for Tauri externalBin")
    parser.add_argument("--target", type=str, default=None, help="Rust target triple override")
    args = parser.parse_args()
    triple = args.target or detect_target_triple()
    build_sidecar(triple)
