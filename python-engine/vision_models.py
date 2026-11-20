"""
Local Vision-Language Model Registry & Downloader for Good Shelves.
Supports 100% offline joint image-text embeddings via ONNX Runtime.
"""

from __future__ import annotations

import io
import json
import shutil
import time
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
        "sizeLabel": "~202 MB",
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

# Zero-shot visual concepts for Google-Photos-style local photo tagging, smart naming & recall
VISUAL_CONCEPTS: list[str] = [
    # People, portraits & social moments
    "girl", "woman", "man", "men", "boy", "baby", "child", "person", "people",
    "couple", "friends", "family", "group photo", "crowd", "selfie", "portrait", "smiling face",
    # Flowers, plants & nature
    "flower", "flowers", "rose", "bouquet", "blossom", "sunflower", "plant", "leaf",
    "tree", "forest", "garden", "grass", "mountain", "hill", "beach", "ocean", "lake",
    "river", "waterfall", "sunset", "sunrise", "sky", "clouds", "night sky", "moon", "rain", "snow",
    # Animals
    "dog", "puppy", "cat", "kitten", "bird", "fish", "butterfly", "horse", "pet",
    # Food & drink
    "food", "meal", "breakfast", "pizza", "burger", "cake", "dessert", "coffee",
    "drink", "fruit", "restaurant", "cafe",
    # Places & architecture
    "city street", "building", "house", "bridge", "church", "temple", "park",
    "shopping mall", "cinema", "movie theater", "concert", "stage", "airport", "train",
    "office", "desk", "classroom", "library", "bedroom", "kitchen", "gym",
    # Vehicles & everyday objects
    "car", "motorcycle", "bicycle", "bus", "airplane", "boat",
    "laptop", "computer", "phone", "camera", "book", "notebook", "guitar",
    "glasses", "hat", "dress", "shirt", "shoes", "bag", "fashion outfit",
    # Art, illustrations & documents
    "art", "painting", "drawing", "sketch", "anime", "cartoon", "illustration",
    "poster", "meme", "sign", "map", "calendar", "handwriting",
    "document", "receipt", "invoice", "id card", "screenshot", "chart", "diagram", "whiteboard", "code",
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


def resolve_default_vision_model(models_dir: Path) -> str:
    """Prefer the bundled compact quantized SigLIP model when available, else CLIP ViT-B/32."""
    for candidate in ("siglip-base-224", "clip-vit-b32", "clip-vit-b16"):
        paths = resolve_vision_model_paths(models_dir, candidate)
        if paths["vision"] is not None:
            return candidate
    return "siglip-base-224"


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
    detail_cb: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Download a vision + matching text ONNX pair from HuggingFace Hub with byte-level progress."""
    from huggingface_hub import hf_hub_download

    spec = get_model_spec(model_id)
    target_dir = models_dir / "vision_catalog" / spec["id"]
    target_dir.mkdir(parents=True, exist_ok=True)

    def _emit_detail(
        stage: str,
        file_name: str,
        dl_bytes: int,
        tot_bytes: int,
        base_pct: float,
        span_pct: float,
        message: str,
    ) -> None:
        frac = (float(dl_bytes) / float(tot_bytes)) if tot_bytes > 0 else 0.0
        pct = min(100, max(0, int(round(base_pct + span_pct * frac))))
        if progress_cb:
            progress_cb(message)
        if detail_cb:
            detail_cb({
                "modelId": spec["id"],
                "modelName": spec["name"],
                "status": "downloading",
                "stage": stage,
                "fileName": file_name,
                "downloadedBytes": int(dl_bytes),
                "totalBytes": int(tot_bytes),
                "percent": pct,
                "message": message,
            })

    def _make_tqdm_cls(stage: str, file_name: str, base_pct: float, span_pct: float) -> Optional[type]:
        try:
            from tqdm.std import tqdm as BaseTqdm
        except Exception:
            return None

        class _HookTqdm(BaseTqdm):  # type: ignore[valid-type,misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["file"] = io.StringIO()
                kwargs["disable"] = False
                super().__init__(*args, **kwargs)
                self._gs_last_emit = 0.0
                _emit_detail(
                    stage,
                    file_name,
                    int(self.n or 0),
                    int(self.total or 0),
                    base_pct,
                    span_pct,
                    f"Downloading {spec['name']} · {file_name}…",
                )

            def update(self, n: int = 1) -> bool | None:
                res = super().update(n)
                now = time.monotonic()
                tot = int(self.total or 0)
                cur = int(self.n or 0)
                if (now - self._gs_last_emit) >= 0.12 or (tot > 0 and cur >= tot):
                    self._gs_last_emit = now
                    _emit_detail(
                        stage,
                        file_name,
                        cur,
                        tot,
                        base_pct,
                        span_pct,
                        f"Downloading {file_name}…",
                    )
                return res

            def close(self) -> None:
                tot = int(self.total or 0)
                cur = int(self.n or 0)
                if tot > 0 and cur > 0:
                    _emit_detail(
                        stage,
                        file_name,
                        tot,
                        tot,
                        base_pct,
                        span_pct,
                        f"Downloaded {file_name}",
                    )
                super().close()

        return _HookTqdm

    vis_tqdm = _make_tqdm_cls("vision", "vision.onnx", 2.0, 72.0)
    _emit_detail("vision", "vision.onnx", 0, 0, 2.0, 72.0, f"Downloading {spec['name']} vision encoder…")
    vis_kwargs: dict[str, Any] = {"tqdm_class": vis_tqdm} if vis_tqdm is not None else {}
    vis_dl = Path(hf_hub_download(repo_id=spec["visionRepo"], filename=spec["visionFile"], **vis_kwargs))
    shutil.copy2(vis_dl, target_dir / "vision.onnx")
    _emit_detail("vision", "vision.onnx", 1, 1, 74.0, 0.0, "Prepared vision encoder")

    txt_tqdm = _make_tqdm_cls("text", "text.onnx", 75.0, 22.0)
    _emit_detail("text", "text.onnx", 0, 0, 75.0, 22.0, f"Downloading {spec['name']} text encoder…")
    txt_kwargs: dict[str, Any] = {"tqdm_class": txt_tqdm} if txt_tqdm is not None else {}
    txt_dl = Path(hf_hub_download(repo_id=spec["textRepo"], filename=spec["textFile"], **txt_kwargs))
    shutil.copy2(txt_dl, target_dir / "text.onnx")
    _emit_detail("text", "text.onnx", 1, 1, 97.0, 0.0, "Prepared text encoder")

    _emit_detail("tokenizer", "tokenizer.json", 0, len(spec["tokenizerFiles"]), 97.0, 3.0, "Downloading tokenizer files…")
    for idx, tf in enumerate(spec["tokenizerFiles"], start=1):
        try:
            dl = Path(hf_hub_download(repo_id=spec["tokenizerRepo"], filename=tf))
            shutil.copy2(dl, target_dir / Path(tf).name)
        except Exception:
            pass
        _emit_detail("tokenizer", Path(tf).name, idx, max(1, len(spec["tokenizerFiles"])), 97.0, 3.0, f"Downloaded {Path(tf).name}")

    (target_dir / "meta.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return {"ok": True, "modelId": spec["id"], "dir": str(target_dir)}
