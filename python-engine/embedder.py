"""
Quantized ONNX Multimodal Vision + Text Embedder for Good Shelves.
Projects images, OCR, documents, and code into a unified 512-dimensional L2-normalized
vector space using ONNX Runtime on local CPU, with swappable Vision-Language models.
"""

from __future__ import annotations

import functools
import hashlib
import os
import platform
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import onnxruntime as ort
from PIL import Image
from tokenizers import Tokenizer

from parsers import ParsedFile
from vision_models import (
    VISUAL_CONCEPTS,
    get_model_spec,
    resolve_vision_model_paths,
)


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        out = np.zeros_like(vec, dtype=np.float32)
        out[0] = 1.0
        return out
    return (vec / norm).astype(np.float32)


def _make_orthonormal_proj(in_dim: int, out_dim: int = 128, seed: int = 20261114) -> np.ndarray:
    rng = np.random.default_rng(seed=seed + in_dim)
    q, _ = np.linalg.qr(rng.standard_normal((in_dim, out_dim)).astype(np.float32))
    return q[:, :out_dim].astype(np.float32)


class MultimodalEmbedder:
    """
    100% Offline ONNX Multimodal Embedder producing 512-d L2-normalized embeddings.
    Combines:
      - Quantized ONNX Text Encoder (384-d semantic representation)
      - Swappable ONNX Vision-Language Encoder (512-d / 768-d joint image-text space -> 128-d)
    into a unified 512-d cosine similarity space.
    """

    def __init__(self, models_dir: Optional[Path] = None, vision_model_id: str = "clip-vit-b32") -> None:
        if models_dir is not None:
            base_dir = models_dir
        elif getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            base_dir = Path(sys._MEIPASS) / "models"  # type: ignore[attr-defined]
        else:
            base_dir = Path(__file__).resolve().parent / "models"
        self.models_dir = base_dir
        self.vision_model_id = vision_model_id or "clip-vit-b32"

        self.text_session: Optional[ort.InferenceSession] = None
        self.tokenizer: Optional[Tokenizer] = None
        self._text_input_names: set[str] = set()

        self.vision_session: Optional[ort.InferenceSession] = None
        self.vision_text_session: Optional[ort.InferenceSession] = None
        self.vision_tokenizer: Optional[Tokenizer] = None
        self._vision_input_name: str = "pixel_values"
        self._vision_text_input_names: set[str] = set()
        self._vision_spec = get_model_spec(self.vision_model_id)
        self._concept_bank_cache: Optional[tuple[list[str], np.ndarray]] = None

        # Deterministic orthonormal projections into 128-d visual subspace
        proj_path = base_dir / "vision_proj_128.npy"
        if proj_path.exists():
            self.vis_proj_512 = np.load(proj_path).astype(np.float32)
        else:
            self.vis_proj_512 = _make_orthonormal_proj(512, 128)
        self.vis_proj_768 = _make_orthonormal_proj(768, 128)

        self._load_sessions()
        self._cached_text_384 = functools.lru_cache(maxsize=512)(self._encode_text_384_uncached)
        self._cached_query_512 = functools.lru_cache(maxsize=256)(self._encode_query_uncached)
        self._cached_vis_text_128 = functools.lru_cache(maxsize=256)(self._encode_vision_text_128_uncached)

    def _build_session_options(self) -> ort.SessionOptions:
        sess_opts = ort.SessionOptions()
        cpu_count = os.cpu_count() or 4
        sess_opts.intra_op_num_threads = min(4, max(2, cpu_count // 2))
        sess_opts.inter_op_num_threads = 1
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return sess_opts

    def _preferred_providers(self) -> list[str]:
        avail = set(ort.get_available_providers())
        system = platform.system()
        ordered: list[str] = []
        if system == "Darwin" and "CoreMLExecutionProvider" in avail:
            ordered.append("CoreMLExecutionProvider")
        elif system == "Windows" and "DmlExecutionProvider" in avail:
            ordered.append("DmlExecutionProvider")
        if "CUDAExecutionProvider" in avail:
            ordered.append("CUDAExecutionProvider")
        ordered.append("CPUExecutionProvider")
        return ordered

    def _load_sessions(self) -> None:
        sess_opts = self._build_session_options()
        providers = self._preferred_providers()

        text_onnx = self.models_dir / "text_encoder" / "model.onnx"
        tok_json = self.models_dir / "text_encoder" / "tokenizer.json"
        if text_onnx.exists() and tok_json.exists():
            self.text_session = ort.InferenceSession(
                str(text_onnx), sess_options=sess_opts, providers=providers
            )
            self._text_input_names = {i.name for i in self.text_session.get_inputs()}
            self.tokenizer = Tokenizer.from_file(str(tok_json))
            self.tokenizer.enable_truncation(max_length=512)

        self._load_vision_sessions(sess_opts)

    def _load_vision_sessions(self, sess_opts: Optional[ort.SessionOptions] = None) -> None:
        opts = sess_opts or self._build_session_options()
        providers = self._preferred_providers()
        self._vision_spec = get_model_spec(self.vision_model_id)
        paths = resolve_vision_model_paths(self.models_dir, self.vision_model_id)

        self.vision_session = None
        self.vision_text_session = None
        self.vision_tokenizer = None
        self._concept_bank_cache = None

        if paths["vision"] is not None:
            self.vision_session = ort.InferenceSession(
                str(paths["vision"]), sess_options=opts, providers=providers
            )
            self._vision_input_name = self.vision_session.get_inputs()[0].name

        if paths["text"] is not None and paths["tokenizer"] is not None:
            try:
                self.vision_text_session = ort.InferenceSession(
                    str(paths["text"]), sess_options=opts, providers=providers
                )
                self._vision_text_input_names = {i.name for i in self.vision_text_session.get_inputs()}
                self.vision_tokenizer = Tokenizer.from_file(str(paths["tokenizer"]))
                max_len = 64 if "siglip" in self.vision_model_id else 77
                self.vision_tokenizer.enable_truncation(max_length=max_len)
            except Exception:
                self.vision_text_session = None
                self.vision_tokenizer = None

    def set_vision_model(self, model_id: str) -> None:
        """Hot-swap the active local vision-language model."""
        self.vision_model_id = model_id or "clip-vit-b32"
        self._load_vision_sessions()
        self._cached_query_512.cache_clear()
        self._cached_vis_text_128.cache_clear()

    def _project_vis(self, vec: np.ndarray) -> np.ndarray:
        if vec.shape[0] == 768:
            return l2_normalize(vec @ self.vis_proj_768)
        if vec.shape[0] == 512:
            return l2_normalize(vec @ self.vis_proj_512)
        out = np.zeros(128, dtype=np.float32)
        n = min(128, vec.shape[0])
        out[:n] = vec[:n]
        return l2_normalize(out)

    def _encode_text_384(self, text: str) -> np.ndarray:
        text = (text or "").strip()
        if not text:
            return np.zeros(384, dtype=np.float32)
        return self._cached_text_384(text[:2048]).copy()

    def _encode_text_384_uncached(self, text: str) -> np.ndarray:
        if self.text_session is not None and self.tokenizer is not None:
            enc = self.tokenizer.encode(text)
            input_ids = np.array([enc.ids], dtype=np.int64)
            attention_mask = np.array([enc.attention_mask], dtype=np.int64)
            feed: dict[str, np.ndarray] = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }
            if "token_type_ids" in self._text_input_names:
                feed["token_type_ids"] = np.zeros_like(input_ids)

            outputs = self.text_session.run(None, feed)
            hidden = outputs[0]
            mask_exp = np.expand_dims(attention_mask, axis=-1).astype(np.float32)
            summed = np.sum(hidden * mask_exp, axis=1)
            counts = np.clip(mask_exp.sum(axis=1), a_min=1e-9, a_max=None)
            pooled = (summed / counts)[0]
            cls_vec = hidden[0, 0]
            combined = 0.65 * pooled + 0.35 * cls_vec
            return l2_normalize(combined.astype(np.float32))

        vec = np.zeros(384, dtype=np.float32)
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:2], "little") % 384
            sign = 1.0 if (digest[2] & 1) == 0 else -1.0
            vec[idx] += sign
        return l2_normalize(vec)

    def _encode_vision_text_raw(self, text: str) -> Optional[np.ndarray]:
        """Encode text using the joint Vision-Language text tower (CLIP/SigLIP)."""
        if self.vision_text_session is None or self.vision_tokenizer is None:
            return None
        enc = self.vision_tokenizer.encode(text)
        input_ids = np.array([enc.ids], dtype=np.int64)
        attention_mask = np.array([enc.attention_mask], dtype=np.int64)
        feed: dict[str, np.ndarray] = {"input_ids": input_ids}
        if "attention_mask" in self._vision_text_input_names:
            feed["attention_mask"] = attention_mask
        outputs = self.vision_text_session.run(None, feed)
        raw = outputs[0]
        if raw.ndim == 3:
            raw = raw[0, 0]
        elif raw.ndim == 2:
            raw = raw[0]
        return l2_normalize(raw.astype(np.float32))

    def _encode_vision_text_128_uncached(self, text: str) -> Optional[np.ndarray]:
        raw = self._encode_vision_text_raw(text)
        if raw is None:
            return None
        return self._project_vis(raw)

    def encode_visual_query(self, query: str) -> Optional[np.ndarray]:
        """Return 128-d L2-normalized joint visual-text query vector if vision-text tower is loaded."""
        clean = (query or "").strip()
        if not clean:
            return None
        res = self._cached_vis_text_128(clean.lower())
        return res.copy() if res is not None else None

    def _preprocess_image(self, img: Image.Image) -> np.ndarray:
        """Preprocess PIL Image to (1, 3, H, W) float32 tensor using active vision model spec."""
        size = int(self._vision_spec.get("imageSize", 224))
        rgb = img.convert("RGB")
        w, h = rgb.size
        short = min(w, h)
        left = (w - short) // 2
        top = (h - short) // 2
        cropped = rgb.crop((left, top, left + short, top + short)).resize((size, size), Image.Resampling.BILINEAR)
        arr = np.asarray(cropped, dtype=np.float32) * (1.0 / 255.0)
        mean = np.array(self._vision_spec.get("mean", [0.48145466, 0.4578275, 0.40821073]), dtype=np.float32)
        std = np.array(self._vision_spec.get("std", [0.26862954, 0.26130258, 0.27577711]), dtype=np.float32)
        normed = (arr - mean) / std
        chw = np.transpose(normed, (2, 0, 1))
        return np.expand_dims(chw, axis=0).astype(np.float32)

    def _encode_image_raw(self, img: Image.Image) -> np.ndarray:
        if self.vision_session is not None:
            pixel_values = self._preprocess_image(img)
            outputs = self.vision_session.run(None, {self._vision_input_name: pixel_values})
            raw = outputs[0]
            if raw.ndim == 3:
                raw = raw[0, 0]
            elif raw.ndim == 2:
                raw = raw[0]
            return l2_normalize(raw.astype(np.float32))

        small = img.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
        arr = np.asarray(small, dtype=np.float32).flatten()
        vec = np.zeros(512, dtype=np.float32)
        vec[: min(512, len(arr))] = arr[:512]
        return l2_normalize(vec)

    def _get_concept_bank(self) -> Optional[tuple[list[str], np.ndarray]]:
        if self._concept_bank_cache is not None:
            return self._concept_bank_cache
        if self.vision_text_session is None or self.vision_tokenizer is None:
            return None
        vecs = []
        labels = []
        for concept in VISUAL_CONCEPTS:
            raw = self._encode_vision_text_raw(f"a photo of {concept}")
            if raw is not None:
                vecs.append(raw)
                labels.append(concept)
        if not vecs:
            return None
        mat = np.stack(vecs, axis=0).astype(np.float32)
        self._concept_bank_cache = (labels, mat)
        return self._concept_bank_cache

    def classify_image_concepts(self, img: Image.Image, top_k: int = 4) -> list[str]:
        """Zero-shot classify an image against VISUAL_CONCEPTS using the active Vision-Language model."""
        bank = self._get_concept_bank()
        if bank is None:
            return []
        labels, mat = bank
        vis_vec = self._encode_image_raw(img)
        if vis_vec.shape[0] != mat.shape[1]:
            return []
        sims = mat @ vis_vec
        top_indices = np.argsort(sims)[::-1][:top_k]
        return [labels[int(i)] for i in top_indices if float(sims[int(i)]) > 0.21]

    def embed_parsed_file(self, parsed: ParsedFile) -> tuple[np.ndarray, int, int]:
        """
        Compute the fused 512-d L2-normalized multimodal vector for a ParsedFile,
        returning (vector_512, visual_share_pct, text_share_pct).
        """
        auto_concepts: list[str] = []
        if parsed.pil_image is not None and parsed.modality == "image":
            auto_concepts = self.classify_image_concepts(parsed.pil_image, top_k=4)

        merged_visual_tags = list(dict.fromkeys([*(parsed.visual_tags or []), *auto_concepts]))
        if auto_concepts:
            parsed.visual_tags = merged_visual_tags

        text_pieces: list[str] = [parsed.title, parsed.name.replace("_", " ").replace("-", " ")]
        if parsed.headings:
            text_pieces.append(" ".join(parsed.headings))
        if parsed.code_symbols:
            text_pieces.append("functions: " + " ".join(parsed.code_symbols))
        if merged_visual_tags:
            text_pieces.append("visual scenes: " + ", ".join(merged_visual_tags))
        if parsed.ocr_text:
            text_pieces.append(parsed.ocr_text)
        if parsed.chunks:
            text_pieces.extend(parsed.chunks[:3])
        elif parsed.extracted_text:
            text_pieces.append(parsed.extracted_text[:1500])

        full_text = " · ".join(p for p in text_pieces if p)
        if len(parsed.chunks) > 1:
            c_vecs = [self._encode_text_384(f"{parsed.title}. {ch}") for ch in parsed.chunks[:3]]
            text_384 = l2_normalize(0.4 * self._encode_text_384(full_text) + 0.6 * np.mean(c_vecs, axis=0))
        else:
            text_384 = self._encode_text_384(full_text)

        if parsed.pil_image is not None:
            vis_raw = self._encode_image_raw(parsed.pil_image)
            vis_128 = self._project_vis(vis_raw)
        else:
            vis_128 = np.zeros(128, dtype=np.float32)

        word_count = len((parsed.extracted_text + " " + parsed.ocr_text).split())
        if parsed.modality == "image":
            if word_count < 8:
                visual_share = 84
            elif word_count < 35:
                visual_share = 64
            else:
                visual_share = 56
        elif parsed.ext == "pdf" and parsed.pil_image is not None:
            if parsed.specimen_type == "diagram":
                visual_share = 58
            elif parsed.specimen_type == "receipt":
                visual_share = 45
            else:
                visual_share = 18
        elif parsed.modality == "code":
            visual_share = 5
        else:
            visual_share = 10 if parsed.specimen_type != "text" else 6

        text_share = 100 - visual_share

        has_vis = float(np.linalg.norm(vis_128)) > 1e-6
        w_text = np.sqrt(0.82) if has_vis else 1.0
        w_vis = np.sqrt(0.18) if has_vis else 0.0
        fused_512 = np.concatenate([w_text * text_384, w_vis * vis_128]).astype(np.float32)
        return l2_normalize(fused_512), visual_share, text_share

    def _encode_query_uncached(self, clean_q: str) -> np.ndarray:
        text_384 = self._encode_text_384(clean_q)
        vis_128 = self._encode_vision_text_128_uncached(clean_q)
        if vis_128 is None:
            vis_128 = np.zeros(128, dtype=np.float32)
            return l2_normalize(np.concatenate([text_384, vis_128]).astype(np.float32))
        w_text = np.sqrt(0.62)
        w_vis = np.sqrt(0.38)
        return l2_normalize(np.concatenate([w_text * text_384, w_vis * vis_128]).astype(np.float32))

    def encode_query(self, query: str) -> tuple[np.ndarray, float]:
        """Encode a natural language search query into the 512-d L2-normalized vector space."""
        t0 = time.perf_counter()
        clean_q = (query or "").strip()
        if not clean_q:
            vec = np.zeros(512, dtype=np.float32)
            vec[0] = 1.0
            return vec, 0.2

        fused_512 = self._cached_query_512(clean_q.lower()).copy()
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return fused_512, latency_ms
