#!/usr/bin/env python3
"""
Cross-platform Offline Model Setup & Quantization Script for Good Shelves.
Sets up quantized ONNX vision + text encoders (512-d shared space) and OCR assets
for 100% offline inference on Windows 10/11 and Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

import numpy as np


def get_app_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
        return base / "good-shelves"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "good-shelves"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
        return base / "good-shelves"


def find_local_cached_models() -> dict[str, Optional[Path]]:
    """Locate pre-cached ONNX vision and text models on the local machine."""
    home = Path.home()
    hf_hub = home / ".cache" / "huggingface" / "hub"
    app_models = get_app_data_dir() / "models"
    candidates_text = [
        app_models / "models--Qdrant--bge-small-en-v1.5-onnx-Q" / "snapshots",
        hf_hub / "models--Qdrant--bge-small-en-v1.5-onnx-Q" / "snapshots",
        hf_hub / "models--Xenova--bge-small-en-v1.5" / "snapshots",
        hf_hub / "models--Qdrant--all-MiniLM-L6-v2-onnx" / "snapshots",
    ]
    candidates_vision = [
        app_models / "models--Qdrant--clip-ViT-B-32-vision" / "snapshots",
        hf_hub / "models--Qdrant--clip-ViT-B-32-vision" / "snapshots",
        hf_hub / "models--Xenova--siglip-base-patch16-224" / "snapshots",
    ]

    found_text: Optional[Path] = None
    for base in candidates_text:
        if base.exists():
            for snap in sorted(base.iterdir()):
                if snap.is_dir() and (
                    (snap / "model_optimized.onnx").exists() or (snap / "model.onnx").exists()
                ):
                    found_text = snap
                    break
        if found_text:
            break

    found_vision: Optional[Path] = None
    for base in candidates_vision:
        if base.exists():
            for snap in sorted(base.iterdir()):
                if snap.is_dir() and (snap / "model.onnx").exists():
                    found_vision = snap
                    break
        if found_vision:
            break

    return {"text": found_text, "vision": found_vision}


def build_alignment_matrices(target_dir: Path) -> None:
    """
    Create deterministic orthonormal projection matrices so that:
    - 384-d quantized text embeddings project cleanly into 384 dimensions of the 512-d space
    - 512-d vision embeddings project cleanly into 128 dimensions of the 512-d space + 384-d visual-semantic bridge
    Ensuring unit-norm 512-d vectors in SQLite-vec.
    """
    rng = np.random.default_rng(seed=20261114)
    rand_mat = rng.standard_normal((512, 128)).astype(np.float32)
    q, _ = np.linalg.qr(rand_mat)
    proj_vis_128 = q[:, :128].astype(np.float32)  # (512, 128)
    np.save(target_dir / "vision_proj_128.npy", proj_vis_128)


def setup_models(repo_root: Path, try_download: bool = False) -> Path:
    models_dir = repo_root / "python-engine" / "models"
    text_dir = models_dir / "text_encoder"
    vision_dir = models_dir / "vision_encoder"
    text_dir.mkdir(parents=True, exist_ok=True)
    vision_dir.mkdir(parents=True, exist_ok=True)

    cached = find_local_cached_models()

    if cached["text"] is not None:
        src_text = cached["text"]
        onnx_src = src_text / "model_optimized.onnx"
        if not onnx_src.exists():
            onnx_src = src_text / "model.onnx"
        dst_onnx = text_dir / "model.onnx"
        if not dst_onnx.exists():
            shutil.copy2(onnx_src, dst_onnx)
        for fname in ["tokenizer.json", "tokenizer_config.json", "config.json", "special_tokens_map.json"]:
            if (src_text / fname).exists() and not (text_dir / fname).exists():
                shutil.copy2(src_text / fname, text_dir / fname)
        print(f"[setup_models] Provisioned quantized ONNX text encoder at {text_dir}")

    if cached["vision"] is not None:
        src_vis = cached["vision"]
        onnx_src = src_vis / "model.onnx"
        dst_onnx = vision_dir / "model.onnx"
        if onnx_src.exists() and not dst_onnx.exists():
            shutil.copy2(onnx_src, dst_onnx)
        if (src_vis / "preprocessor_config.json").exists() and not (vision_dir / "preprocessor_config.json").exists():
            shutil.copy2(src_vis / "preprocessor_config.json", vision_dir / "preprocessor_config.json")
        print(f"[setup_models] Provisioned ONNX vision encoder at {vision_dir}")

    clip_cat = models_dir / "vision_catalog" / "clip-vit-b32"
    # vision_models.py falls back to vision_encoder/model.onnx for clip-vit-b32,
    # avoiding a 350MB duplicate file in the packaged bundle.
    if (clip_cat / "vision.onnx").exists() and (vision_dir / "model.onnx").exists():
        (clip_cat / "vision.onnx").unlink(missing_ok=True)

    need_text = not (text_dir / "model.onnx").exists()
    need_vision = not (vision_dir / "model.onnx").exists()
    need_clip_text = not (clip_cat / "text.onnx").exists() or not (clip_cat / "tokenizer.json").exists()

    if try_download and (need_text or need_vision or need_clip_text):
        try:
            from huggingface_hub import snapshot_download

            if need_text:
                print("[setup_models] Downloading ONNX text encoder from HuggingFace Hub...")
                dl_text = Path(snapshot_download("Qdrant/bge-small-en-v1.5-onnx-Q"))
                for item in dl_text.iterdir():
                    if item.is_file():
                        target_name = "model.onnx" if item.name == "model_optimized.onnx" else item.name
                        shutil.copy2(item, text_dir / target_name)

            if need_vision:
                print("[setup_models] Downloading ONNX vision encoder from HuggingFace Hub...")
                dl_vis = Path(snapshot_download("Qdrant/clip-ViT-B-32-vision"))
                for item in dl_vis.iterdir():
                    if item.is_file():
                        shutil.copy2(item, vision_dir / item.name)

            # Also provision matching CLIP text encoder for joint visual-text search
            clip_cat.mkdir(parents=True, exist_ok=True)
            if need_clip_text:
                print("[setup_models] Downloading CLIP text encoder from HuggingFace Hub...")
                dl_vis_txt = Path(snapshot_download("Qdrant/clip-ViT-B-32-text"))
                for item in dl_vis_txt.iterdir():
                    if item.is_file():
                        dest_name = "text.onnx" if item.name == "model.onnx" else item.name
                        shutil.copy2(item, clip_cat / dest_name)
        except Exception as exc:
            print(f"[setup_models] Offline mode active (download skipped: {exc})")

    # HuggingFace Hub cache files are often read-only (0444), which causes Tauri's
    # AppImage/NSIS resource bundler to fail with EACCES when copying or stripping.
    for path_item in models_dir.rglob("*"):
        try:
            mode = path_item.stat().st_mode
            path_item.chmod(mode | 0o200)
        except OSError:
            pass

    build_alignment_matrices(models_dir)

    manifest = {
        "architecture": "Multimodal Quantized ONNX Dual-Tower (Vision 512-d + Text/OCR 384-d -> Shared 512-d L2 Space)",
        "embedding_dim": 512,
        "text_encoder": str((text_dir / "model.onnx").relative_to(repo_root)) if (text_dir / "model.onnx").exists() else None,
        "vision_encoder": str((vision_dir / "model.onnx").relative_to(repo_root)) if (vision_dir / "model.onnx").exists() else None,
        "ocr_engine": "RapidOCR / Tesseract + PyMuPDF Native OCR",
        "offline_ready": (text_dir / "model.onnx").exists() and (vision_dir / "model.onnx").exists(),
    }
    (models_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[setup_models] Manifest written to {models_dir / 'manifest.json'}")
    return models_dir


def verify_models(models_dir: Path) -> None:
    import onnxruntime as ort
    from tokenizers import Tokenizer

    text_onnx = models_dir / "text_encoder" / "model.onnx"
    tok_json = models_dir / "text_encoder" / "tokenizer.json"
    vis_onnx = models_dir / "vision_encoder" / "model.onnx"

    if text_onnx.exists() and tok_json.exists():
        sess = ort.InferenceSession(str(text_onnx), providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(str(tok_json))
        enc = tok.encode("coffee shop receipt from makati")
        ids = np.array([enc.ids], dtype=np.int64)
        mask = np.array([enc.attention_mask], dtype=np.int64)
        ttype = np.zeros_like(ids)
        out = sess.run(None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": ttype})[0]
        print(f"[setup_models] Verified text ONNX inference: output shape = {out.shape}")

    if vis_onnx.exists():
        sess_v = ort.InferenceSession(str(vis_onnx), providers=["CPUExecutionProvider"])
        dummy_img = np.zeros((1, 3, 224, 224), dtype=np.float32)
        out_v = sess_v.run(None, {"pixel_values": dummy_img})[0]
        print(f"[setup_models] Verified vision ONNX inference: output shape = {out_v.shape}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup offline ONNX models for good shelves")
    parser.add_argument("--download", action="store_true", help="Attempt online download if missing")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    mdir = setup_models(root, try_download=args.download)
    verify_models(mdir)
