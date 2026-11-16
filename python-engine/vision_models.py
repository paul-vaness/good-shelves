"""
Local Vision-Language Model Registry & Downloader for Good Shelves.
Supports 100% offline joint image-text embeddings via ONNX Runtime.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Optional


VISION_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "clip-vit-b32",
        "name": "CLIP ViT-B/32 (Fast)",
        "description": "Fast local image-text search. Great for everyday photos, screenshots, and objects.",
        "sizeLabel": "~340 MB",
        "speedLabel": "fast · cpu",
        "accuracyLabel": "balanced",
        "dim": 512,
        "imageSize": 224,
        "mean": [0.48145466, 0.4578275, 0.40821073],
        "std": [0.26862954, 0.26130258, 0.27577711],
        "visionRepo": "Qdrant/clip-ViT-B-32-vision",
        "visionFile": "model.onnx",
        "textRepo": "Qdrant/clip-ViT-B-32-text",
        "textFile": "model.onnx",
        "tokenizerRepo": "Qdrant/clip-ViT-B-32-text",
        "tokenizerFiles": ["tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"],
    },
    {
        "id": "clip-vit-b16",
        "name": "CLIP ViT-B/16 (Detailed)",
        "description": "Higher-resolution patches for finer details like mall signs, cinema screens, and scenes.",
        "sizeLabel": "~350 MB",
        "speedLabel": "medium · cpu",
        "accuracyLabel": "high",
        "dim": 512,
        "imageSize": 224,
        "mean": [0.48145466, 0.4578275, 0.40821073],
        "std": [0.26862954, 0.26130258, 0.27577711],
        "visionRepo": "Xenova/clip-vit-base-patch16",
        "visionFile": "onnx/vision_model_quantized.onnx",
        "textRepo": "Xenova/clip-vit-base-patch16",
        "textFile": "onnx/text_model_quantized.onnx",
        "tokenizerRepo": "Xenova/clip-vit-base-patch16",
        "tokenizerFiles": ["tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"],
    },
    {
        "id": "siglip-base-224",
        "name": "SigLIP Base 224 (Smartest)",
        "description": "Google SigLIP sigmoid-loss vision-language model. Best scene, mood, and natural-query recall.",
        "sizeLabel": "~380 MB",
        "speedLabel": "medium · cpu",
        "accuracyLabel": "highest",
        "dim": 768,
        "imageSize": 224,
        "mean": [0.5, 0.5, 0.5],
        "std": [0.5, 0.5, 0.5],
        "visionRepo": "Xenova/siglip-base-patch16-224",
        "visionFile": "onnx/vision_model_quantized.onnx",
        "textRepo": "Xenova/siglip-base-patch16-224",
        "textFile": "onnx/text_model_quantized.onnx",
        "tokenizerRepo": "Xenova/siglip-base-patch16-224",
        "tokenizerFiles": ["tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"],
    },
]

# Zero-shot visual concepts for automatic local photo tagging & explanations
VISUAL_CONCEPTS: list[str] = [
    "sunset", "sunrise", "beach", "ocean", "mountain", "forest", "park", "city street",
    "night sky", "cinema", "movie theater", "shopping mall", "restaurant", "cafe",
    "food", "concert", "stage", "office", "desk", "classroom", "library",
    "car", "road", "airport", "train", "house", "bedroom", "kitchen",
    "selfie", "portrait", "group photo", "family", "childhood", "wedding", "birthday",
    "dog", "cat", "pet", "flower", "garden",
    "document", "receipt", "invoice", "screenshot", "chart", "diagram", "whiteboard", "code",
]


def get_model_spec(model_id: str) -> dict[str, Any]:
    for spec in VISION_MODEL_CATALOG:
        if spec["id"] == model_id:
            return spec
    return VISION_MODEL_CATALOG[0]


def resolve_vision_model_paths(models_dir: Path, model_id: str) -> dict[str, Optional[Path]]:
    """
    Resolve paths to vision ONNX, matching vision-text ONNX, and tokenizer.json for a model_id.
    For the default `clip-vit-b32`, falls back to `models_dir/vision_encoder/model.onnx` if present.
    """
    catalog_dir = models_dir / "vision_catalog" / model_id
    vis_path = catalog_dir / "vision.onnx"
    txt_path = catalog_dir / "text.onnx"
    tok_path = catalog_dir / "tokenizer.json"

    if model_id == "clip-vit-b32" and not vis_path.exists():
        legacy_vis = models_dir / "vision_encoder" / "model.onnx"
        if legacy_vis.exists():
            vis_path = legacy_vis

    return {
        "vision": vis_path if vis_path.exists() else None,
        "text": txt_path if txt_path.exists() else None,
        "tokenizer": tok_path if tok_path.exists() else None,
        "dir": catalog_dir,
    }


def get_catalog_status(models_dir: Path, active_model_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for spec in VISION_MODEL_CATALOG:
        paths = resolve_vision_model_paths(models_dir, spec["id"])
        has_vision = paths["vision"] is not None
        has_text = paths["text"] is not None and paths["tokenizer"] is not None
        items.append({
            "id": spec["id"],
            "name": spec["name"],
            "description": spec["description"],
            "sizeLabel": spec["sizeLabel"],
            "speedLabel": spec["speedLabel"],
            "accuracyLabel": spec["accuracyLabel"],
            "installed": has_vision,
            "jointTextReady": has_vision and has_text,
            "active": spec["id"] == active_model_id,
        })
    return items


def download_vision_model(
    models_dir: Path,
    model_id: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Download a vision + matching text ONNX pair from HuggingFace Hub into local storage."""
    from huggingface_hub import hf_hub_download

    spec = get_model_spec(model_id)
    target_dir = models_dir / "vision_catalog" / spec["id"]
    target_dir.mkdir(parents=True, exist_ok=True)

    if progress_cb:
        progress_cb(f"Downloading {spec['name']} vision encoder…")
    vis_dl = Path(hf_hub_download(repo_id=spec["visionRepo"], filename=spec["visionFile"]))
    shutil.copy2(vis_dl, target_dir / "vision.onnx")

    if progress_cb:
        progress_cb(f"Downloading {spec['name']} text encoder…")
    txt_dl = Path(hf_hub_download(repo_id=spec["textRepo"], filename=spec["textFile"]))
    shutil.copy2(txt_dl, target_dir / "text.onnx")

    for tf in spec["tokenizerFiles"]:
        try:
            dl = Path(hf_hub_download(repo_id=spec["tokenizerRepo"], filename=tf))
            shutil.copy2(dl, target_dir / Path(tf).name)
        except Exception:
            continue

    (target_dir / "meta.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return {"ok": True, "modelId": spec["id"], "dir": str(target_dir)}
