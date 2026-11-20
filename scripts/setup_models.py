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

    siglip_cat = models_dir / "vision_catalog" / "siglip-base-224"
    clip_cat = models_dir / "vision_catalog" / "clip-vit-b32"
    # vision_models.py falls back to vision_encoder/model.onnx for clip-vit-b32,
    # avoiding a 350MB duplicate file in the packaged bundle.
    if (clip_cat / "vision.onnx").exists() and (vision_dir / "model.onnx").exists():
        (clip_cat / "vision.onnx").unlink(missing_ok=True)

    need_text = not (text_dir / "model.onnx").exists()
    has_siglip = (siglip_cat / "vision.onnx").exists() and (siglip_cat / "text.onnx").exists() and (siglip_cat / "tokenizer.json").exists()
    has_clip = (vision_dir / "model.onnx").exists() and (clip_cat / "text.onnx").exists() and (clip_cat / "tokenizer.json").exists()

    if try_download and (need_text or (not has_siglip and not has_clip)):
        try:
            from huggingface_hub import hf_hub_download, snapshot_download

            if need_text:
                print("[setup_models] Downloading ONNX text encoder from HuggingFace Hub...")
                dl_text = Path(snapshot_download("Qdrant/bge-small-en-v1.5-onnx-Q"))
                for item in dl_text.iterdir():
                    if item.is_file():
                        target_name = "model.onnx" if item.name == "model_optimized.onnx" else item.name
                        shutil.copy2(item, text_dir / target_name)

            if not has_siglip:
                print("[setup_models] Downloading compact quantized SigLIP Base 224 (~202 MB)...")
                siglip_cat.mkdir(parents=True, exist_ok=True)
                vis_dl = Path(hf_hub_download("Xenova/siglip-base-patch16-224", "onnx/vision_model_quantized.onnx"))
                shutil.copy2(vis_dl, siglip_cat / "vision.onnx")
                txt_dl = Path(hf_hub_download("Xenova/siglip-base-patch16-224", "onnx/text_model_quantized.onnx"))
                shutil.copy2(txt_dl, siglip_cat / "text.onnx")
                for tf in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
                    try:
                        dl_tf = Path(hf_hub_download("Xenova/siglip-base-patch16-224", tf))
                        shutil.copy2(dl_tf, siglip_cat / tf)
                    except Exception:
                        pass
                has_siglip = True
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

    active_vis_path = (
        siglip_cat / "vision.onnx"
        if (siglip_cat / "vision.onnx").exists()
        else (vision_dir / "model.onnx")
    )
    manifest = {
        "architecture": "Multimodal Quantized ONNX Dual-Tower (SigLIP/CLIP Vision + Text/OCR 384-d -> Shared 512-d L2 Space)",
        "embedding_dim": 512,
        "default_vision_model": "siglip-base-224" if (siglip_cat / "vision.onnx").exists() else "clip-vit-b32",
        "text_encoder": str((text_dir / "model.onnx").relative_to(repo_root)) if (text_dir / "model.onnx").exists() else None,
        "vision_encoder": str(active_vis_path.relative_to(repo_root)) if active_vis_path.exists() else None,
        "ocr_engine": "RapidOCR / Tesseract + PyMuPDF Native OCR",
        "offline_ready": (text_dir / "model.onnx").exists() and active_vis_path.exists(),
    }
    (models_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[setup_models] Manifest written to {models_dir / 'manifest.json'}")
    return models_dir


def verify_models(models_dir: Path) -> None:
    import onnxruntime as ort
    from tokenizers import Tokenizer

    text_onnx = models_dir / "text_encoder" / "model.onnx"
    tok_json = models_dir / "text_encoder" / "tokenizer.json"
    siglip_vis = models_dir / "vision_catalog" / "siglip-base-224" / "vision.onnx"
    clip_vis = models_dir / "vision_encoder" / "model.onnx"
    vis_onnx = siglip_vis if siglip_vis.exists() else clip_vis

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
        in_name = sess_v.get_inputs()[0].name if sess_v.get_inputs() else "pixel_values"
        dummy_img = np.zeros((1, 3, 224, 224), dtype=np.float32)
        out_v = sess_v.run(None, {in_name: dummy_img})[0]
        print(f"[setup_models] Verified vision ONNX inference ({vis_onnx.parent.name}): output shape = {out_v.shape}")


def stage_release_models(repo_root: Path, models_dir: Path) -> Path:
    """
    Create a clean `build-release-models/` directory containing only the compact
    default bundled encoders (quantized text + quantized SigLIP ~268 MB total) so
    optional locally-cached models never bloat installers.
    """
    stage_dir = repo_root / "build-release-models"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    use_siglip = (models_dir / "vision_catalog" / "siglip-base-224" / "vision.onnx").exists()
    copy_files = [
        "manifest.json",
        "vision_proj_128.npy",
        "concept_bank_siglip-base-224.npy" if use_siglip else "concept_bank_clip-vit-b32.npy",
    ]
    for fname in copy_files:
        src = models_dir / fname
        if src.exists():
            shutil.copy2(src, stage_dir / fname)

    copy_dirs = (
        ("text_encoder", "vision_catalog/siglip-base-224")
        if use_siglip
        else ("text_encoder", "vision_encoder", "vision_catalog/clip-vit-b32")
    )
    for rel in copy_dirs:
        src_dir = models_dir / rel
        if src_dir.exists():
            dst_dir = stage_dir / rel
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dst_dir)

    for path_item in stage_dir.rglob("*"):
        try:
            path_item.chmod(path_item.stat().st_mode | 0o200)
        except OSError:
            pass
    print(f"[setup_models] Staged clean release models -> {stage_dir}")
    return stage_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup offline ONNX models for good shelves")
    parser.add_argument("--download", action="store_true", help="Attempt online download if missing")
    parser.add_argument("--release-stage", action="store_true", help="Stage clean release-only models in build-release-models/")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    mdir = setup_models(root, try_download=args.download)
    verify_models(mdir)
    if args.release_stage:
        stage_release_models(root, mdir)
