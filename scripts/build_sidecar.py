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


def _write_windows_version_file(work_dir: Path, version_str: str) -> Path:
    """Create a PyInstaller VSVersionInfo resource so Windows Defender sees signed-style PE metadata."""
    parts = [int(p) if p.isdigit() else 0 for p in (version_str.split(".") + ["0", "0", "0", "0"])[:4]]
    ver_tuple = f"({parts[0]}, {parts[1]}, {parts[2]}, {parts[3]})"
    ver_dotted = f"{parts[0]}.{parts[1]}.{parts[2]}.{parts[3]}"
    vf_path = work_dir / "sidecar_version_info.txt"
    vf_path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={ver_tuple},
    prodvers={ver_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', 'Calaca City Global College'),
            StringStruct('FileDescription', 'Good Shelves Local Multimodal Indexing Engine'),
            StringStruct('FileVersion', '{ver_dotted}'),
            StringStruct('InternalName', 'good-shelves-engine'),
            StringStruct('LegalCopyright', 'MIT License — Good Shelves Contributors'),
            StringStruct('OriginalFilename', 'good-shelves-engine.exe'),
            StringStruct('ProductName', 'good shelves'),
            StringStruct('ProductVersion', '{ver_dotted}'),
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    return vf_path


def build_sidecar(target_triple: str) -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    engine_dir = repo_root / "python-engine"
    dist_dir = repo_root / "build-sidecar-dist"
    work_dir = repo_root / "build-sidecar-work"
    stage_dir = repo_root / "build-release-sidecar"
    stage_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    is_windows = "windows" in target_triple
    exe_suffix = ".exe" if is_windows else ""
    bin_name = f"good-shelves-engine{exe_suffix}"
    final_dir = stage_dir / "good-shelves-engine"
    final_bin = final_dir / bin_name

    # Clean previous temp build artifacts
    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(final_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

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

    exclude_modules = [
        "tkinter",
        "_tkinter",
        "matplotlib",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        "pip",
        "setuptools",
        "wheel",
        "PIL.ImageTk",
        "sphinx",
        "docutils",
    ]

    # Use --onedir + --noupx instead of --onefile to eliminate temp self-extraction
    # (which causes Windows Defender false positives and slower cold launches).
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noupx",
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
        import json

        pkg_ver = "1.0.1"
        pkg_json = repo_root / "package.json"
        if pkg_json.exists():
            try:
                pkg_ver = str(json.loads(pkg_json.read_text(encoding="utf-8")).get("version", pkg_ver))
            except Exception:
                pass
        vf_path = _write_windows_version_file(work_dir, pkg_ver)
        ico_path = repo_root / "src-tauri" / "icons" / "icon.ico"
        cmd.append("--noconsole")
        cmd.extend(["--version-file", str(vf_path)])
        if ico_path.exists():
            cmd.extend(["--icon", str(ico_path)])

    for mod in exclude_modules:
        cmd.extend(["--exclude-module", mod])
    for mod in hidden_imports:
        cmd.extend(["--hidden-import", mod])
    for pkg in collect_all:
        cmd.extend(["--collect-all", pkg])

    cmd.append(str(engine_dir / "main.py"))

    print(f"[build_sidecar] Building --onedir sidecar {bin_name} using {sys.executable}...")
    subprocess.run(cmd, check=True, cwd=str(repo_root))

    built_dir = dist_dir / "good-shelves-engine"
    built_bin = built_dir / bin_name
    if not built_bin.exists():
        raise FileNotFoundError(f"Expected PyInstaller binary at {built_bin}")

    shutil.copytree(built_dir, final_dir)
    if not is_windows:
        os.chmod(final_bin, 0o755)

    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    total_mb = sum(p.stat().st_size for p in final_dir.rglob("*") if p.is_file()) / (1024 * 1024)
    print(f"[build_sidecar] Built --onedir sidecar -> {final_dir} ({total_mb:.1f} MB)")
    return final_bin


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Good Shelves sidecar binary for Tauri externalBin")
    parser.add_argument("--target", type=str, default=None, help="Rust target triple override")
    args = parser.parse_args()
    triple = args.target or detect_target_triple()
    build_sidecar(triple)
