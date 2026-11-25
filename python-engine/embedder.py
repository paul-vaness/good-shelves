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
import threading
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
    resolve_default_vision_model,
    resolve_vision_model_paths,
)

_EMBEDDER_LOCK = threading.RLock()
_SESSION_CACHE: dict[tuple[str, str], tuple[ort.InferenceSession, list[str], Optional[Tokenizer]]] = {}
_PROJ_CACHE: dict[tuple[int, int, int], np.ndarray] = {}
_CONCEPT_BANK_GLOBAL_CACHE: dict[tuple[str, int], tuple[list[str], np.ndarray]] = {}


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        out = np.zeros_like(vec, dtype=np.float32)
        out[0] = 1.0
        return out
    return (vec / norm).astype(np.float32)


def _extract_pooled_embed(sess: ort.InferenceSession, outputs: list[np.ndarray]) -> np.ndarray:
    """Select the pooled 2D joint embedding tensor (`pooler_output`/`image_embeds`/`text_embeds`) over 3D hidden states."""
    out_meta = sess.get_outputs()
    preferred = {"image_embeds", "text_embeds", "pooler_output"}
    for meta, arr in zip(out_meta, outputs):
        if meta.name in preferred and arr.ndim == 2:
            return arr
    for arr in outputs:
        if arr.ndim == 2:
            return arr
    raw = outputs[0]
    if raw.ndim == 3:
        return raw[:, 0, :]
    return raw


def _make_orthonormal_proj(in_dim: int, out_dim: int = 128, seed: int = 20261114) -> np.ndarray:
    key = (in_dim, out_dim, seed)
    cached = _PROJ_CACHE.get(key)
    if cached is not None:
        return cached
    rng = np.random.default_rng(seed=seed + in_dim)
    q, _ = np.linalg.qr(rng.standard_normal((in_dim, out_dim)).astype(np.float32))
    proj = q[:, :out_dim].astype(np.float32)
    _PROJ_CACHE[key] = proj
    return proj


class MultimodalEmbedder:
    """
    100% Offline ONNX Multimodal Embedder producing 512-d L2-normalized embeddings.
    Combines:
      - Quantized ONNX Text Encoder (384-d semantic representation)
      - Swappable ONNX Vision-Language Encoder (512-d / 768-d joint image-text space -> 128-d)
    into a unified 512-d cosine similarity space.
    """

    def __init__(self, models_dir: Optional[Path] = None, vision_model_id: Optional[str] = None) -> None:
        env_models = os.environ.get("GOOD_SHELVES_MODELS_DIR")
        exe_models = Path(sys.executable).resolve().parent / "models" if getattr(sys, "frozen", False) else None
        if models_dir is not None:
            base_dir = models_dir
        elif env_models and Path(env_models).exists():
            base_dir = Path(env_models)
        elif exe_models is not None and exe_models.exists():
            base_dir = exe_models
        elif getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") and (Path(sys._MEIPASS) / "models").exists():  # type: ignore[attr-defined]
            base_dir = Path(sys._MEIPASS) / "models"  # type: ignore[attr-defined]
        else:
            base_dir = Path(__file__).resolve().parent / "models"
        self.models_dir = base_dir
        self.vision_model_id = vision_model_id or resolve_default_vision_model(self.models_dir)

        self.text_session: Optional[ort.InferenceSession] = None
        self.tokenizer: Optional[Tokenizer] = None
        self._text_input_names: set[str] = set()
        self._text_loaded = False

        self.vision_session: Optional[ort.InferenceSession] = None
        self.vision_text_session: Optional[ort.InferenceSession] = None
        self.vision_tokenizer: Optional[Tokenizer] = None
        self._vision_input_name: str = "pixel_values"
        self._vision_text_input_names: set[str] = set()
        self._vision_loaded = False
        self._vision_text_loaded = False
        self._vision_spec = get_model_spec(self.vision_model_id)
        self._vision_paths = resolve_vision_model_paths(self.models_dir, self.vision_model_id)
        self._concept_bank_cache: Optional[tuple[list[str], np.ndarray]] = None

        # Deterministic orthonormal projections into 128-d visual subspace
        proj_path = base_dir / "vision_proj_128.npy"
        if proj_path.exists():
            proj_key = (512, 128, hash(str(proj_path)))
            cached_proj = _PROJ_CACHE.get(proj_key)
            if cached_proj is None:
                cached_proj = np.load(proj_path).astype(np.float32)
                _PROJ_CACHE[proj_key] = cached_proj
            self.vis_proj_512 = cached_proj
        else:
            self.vis_proj_512 = _make_orthonormal_proj(512, 128)
        self.vis_proj_768 = _make_orthonormal_proj(768, 128)

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

    def _ensure_text_session(self, sess_opts: Optional[ort.SessionOptions] = None) -> None:
        if self._text_loaded:
            return
        with _EMBEDDER_LOCK:
            if self._text_loaded:
                return
            text_onnx = self.models_dir / "text_encoder" / "model.onnx"
            tok_json = self.models_dir / "text_encoder" / "tokenizer.json"
            if text_onnx.exists() and tok_json.exists():
                cache_key = ("text", f"{text_onnx.resolve()}|{tok_json.resolve()}")
                cached = _SESSION_CACHE.get(cache_key)
                if cached is None:
                    opts = sess_opts or self._build_session_options()
                    providers = self._preferred_providers()
                    sess = ort.InferenceSession(str(text_onnx), sess_options=opts, providers=providers)
                    in_names = [i.name for i in sess.get_inputs()]
                    tok = Tokenizer.from_file(str(tok_json))
                    tok.enable_truncation(max_length=256)
                    cached = (sess, in_names, tok)
                    _SESSION_CACHE[cache_key] = cached
                self.text_session = cached[0]
                self._text_input_names = set(cached[1])
                self.tokenizer = cached[2]
            self._text_loaded = True

    def _ensure_vision_session(self, sess_opts: Optional[ort.SessionOptions] = None) -> None:
        if self._vision_loaded:
            return
        with _EMBEDDER_LOCK:
            if self._vision_loaded:
                return
            vis_path = self._vision_paths.get("vision")
            if vis_path is not None:
                cache_key = ("vision", str(vis_path.resolve()))
                cached = _SESSION_CACHE.get(cache_key)
                if cached is None:
                    opts = sess_opts or self._build_session_options()
                    providers = self._preferred_providers()
                    sess = ort.InferenceSession(str(vis_path), sess_options=opts, providers=providers)
                    in_names = [i.name for i in sess.get_inputs()]
                    cached = (sess, in_names, None)
                    _SESSION_CACHE[cache_key] = cached
                self.vision_session = cached[0]
                self._vision_input_name = cached[1][0] if cached[1] else "pixel_values"
            self._vision_loaded = True

    def _ensure_vision_text_session(self, sess_opts: Optional[ort.SessionOptions] = None) -> None:
        if self._vision_text_loaded:
            return
        with _EMBEDDER_LOCK:
            if self._vision_text_loaded:
                return
            txt_path = self._vision_paths.get("text")
            tok_path = self._vision_paths.get("tokenizer")
            if txt_path is not None and tok_path is not None:
                max_len = 64 if "siglip" in self.vision_model_id else 77
                cache_key = (f"vision_text:{max_len}", f"{txt_path.resolve()}|{tok_path.resolve()}")
                cached = _SESSION_CACHE.get(cache_key)
                if cached is None:
                    try:
                        opts = sess_opts or self._build_session_options()
                        providers = self._preferred_providers()
                        sess = ort.InferenceSession(str(txt_path), sess_options=opts, providers=providers)
                        in_names = [i.name for i in sess.get_inputs()]
                        tok = Tokenizer.from_file(str(tok_path))
                        tok.enable_truncation(max_length=max_len)
                        if "siglip" in self.vision_model_id or "attention_mask" not in in_names:
                            pad_id = tok.token_to_id("<pad>")
                            if pad_id is None:
                                pad_id = tok.token_to_id("</s>")
                            tok.enable_padding(pad_id=1 if pad_id is None else int(pad_id), length=max_len)
                        cached = (sess, in_names, tok)
                        _SESSION_CACHE[cache_key] = cached
                    except Exception:
                        cached = None
                if cached is not None:
                    self.vision_text_session = cached[0]
                    self._vision_text_input_names = set(cached[1])
                    self.vision_tokenizer = cached[2]
            self._vision_text_loaded = True

    def _load_sessions(self) -> None:
        sess_opts = self._build_session_options()
        self._ensure_text_session(sess_opts)
        self._load_vision_sessions(sess_opts)

    def _load_vision_sessions(self, sess_opts: Optional[ort.SessionOptions] = None) -> None:
        opts = sess_opts or self._build_session_options()
        self._vision_spec = get_model_spec(self.vision_model_id)
        self._vision_paths = resolve_vision_model_paths(self.models_dir, self.vision_model_id)

        self.vision_session = None
        self.vision_text_session = None
        self.vision_tokenizer = None
        self._concept_bank_cache = None
        self._vision_loaded = False
        self._vision_text_loaded = False

        self._ensure_vision_session(opts)
        self._ensure_vision_text_session(opts)

    def set_vision_model(self, model_id: str) -> None:
        """Hot-swap the active local vision-language model."""
        with _EMBEDDER_LOCK:
            old_vis_path = self._vision_paths.get("vision")
            old_txt_path = self._vision_paths.get("text")
            old_tok_path = self._vision_paths.get("tokenizer")
            new_id = model_id or resolve_default_vision_model(self.models_dir)
            new_paths = resolve_vision_model_paths(self.models_dir, new_id)
            if new_id != self.vision_model_id:
                if old_vis_path is not None and old_vis_path != new_paths.get("vision"):
                    _SESSION_CACHE.pop(("vision", str(old_vis_path.resolve())), None)
                if old_txt_path is not None and old_tok_path is not None and (
                    old_txt_path != new_paths.get("text") or old_tok_path != new_paths.get("tokenizer")
                ):
                    old_max_len = 64 if "siglip" in self.vision_model_id else 77
                    _SESSION_CACHE.pop(
                        (f"vision_text:{old_max_len}", f"{old_txt_path.resolve()}|{old_tok_path.resolve()}"),
                        None,
                    )
            self.vision_model_id = new_id
            self._vision_spec = get_model_spec(self.vision_model_id)
            self._vision_paths = new_paths
            self.vision_session = None
            self.vision_text_session = None
            self.vision_tokenizer = None
            self._concept_bank_cache = None
            self._vision_loaded = False
            self._vision_text_loaded = False
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
        self._ensure_text_session()
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

    def encode_text_384_batch(self, texts: list[str], batch_size: int = 32) -> list[np.ndarray]:
        """Batch-encode multiple texts into 384-d L2-normalized vectors in minimal ONNX calls."""
        if not texts:
            return []
        if len(texts) == 1:
            return [self._encode_text_384(texts[0])]
        self._ensure_text_session()
        if self.text_session is None or self.tokenizer is None:
            return [self._encode_text_384(t) for t in texts]

        results: list[Optional[np.ndarray]] = [None] * len(texts)
        non_empty_indices: list[int] = []
        non_empty_texts: list[str] = []
        for idx, raw in enumerate(texts):
            clean = (raw or "").strip()[:2048]
            if not clean:
                results[idx] = np.zeros(384, dtype=np.float32)
            else:
                non_empty_indices.append(idx)
                non_empty_texts.append(clean)

        if non_empty_texts:
            for start in range(0, len(non_empty_texts), batch_size):
                chunk_texts = non_empty_texts[start : start + batch_size]
                chunk_indices = non_empty_indices[start : start + batch_size]
                with _EMBEDDER_LOCK:
                    prev_pad = self.tokenizer.padding
                    try:
                        self.tokenizer.enable_padding()
                        encs = self.tokenizer.encode_batch(chunk_texts)
                    finally:
                        if prev_pad is None:
                            self.tokenizer.no_padding()
                        else:
                            self.tokenizer.enable_padding(**prev_pad)
                input_ids = np.array([enc.ids for enc in encs], dtype=np.int64)
                attention_mask = np.array([enc.attention_mask for enc in encs], dtype=np.int64)
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
                pooled = summed / counts
                cls_vecs = hidden[:, 0, :]
                combined = 0.65 * pooled + 0.35 * cls_vecs
                for pos, orig_idx in enumerate(chunk_indices):
                    results[orig_idx] = l2_normalize(combined[pos].astype(np.float32))

        return [v if v is not None else np.zeros(384, dtype=np.float32) for v in results]

    def _encode_vision_text_raw(self, text: str) -> Optional[np.ndarray]:
        """Encode text using the joint Vision-Language text tower (CLIP/SigLIP)."""
        self._ensure_vision_text_session()
        if self.vision_text_session is None or self.vision_tokenizer is None:
            return None
        with _EMBEDDER_LOCK:
            enc = self.vision_tokenizer.encode(text)
        input_ids = np.array([enc.ids], dtype=np.int64)
        attention_mask = np.array([enc.attention_mask], dtype=np.int64)
        feed: dict[str, np.ndarray] = {"input_ids": input_ids}
        if "attention_mask" in self._vision_text_input_names:
            feed["attention_mask"] = attention_mask
        outputs = self.vision_text_session.run(None, feed)
        raw = _extract_pooled_embed(self.vision_text_session, outputs)
        if raw.ndim >= 2:
            raw = raw[0]
        return l2_normalize(raw.astype(np.float32))

    def _encode_vision_text_batch(self, texts: list[str]) -> Optional[list[np.ndarray]]:
        """Encode multiple prompts through the Vision-Language text tower in minimal ONNX calls."""
        if not texts:
            return []
        self._ensure_vision_text_session()
        if self.vision_text_session is None or self.vision_tokenizer is None:
            return None
        if len(texts) == 1:
            vec = self._encode_vision_text_raw(texts[0])
            return [vec] if vec is not None else None

        has_mask = "attention_mask" in self._vision_text_input_names
        if has_mask:
            with _EMBEDDER_LOCK:
                prev_pad = self.vision_tokenizer.padding
                try:
                    self.vision_tokenizer.enable_padding()
                    encs = self.vision_tokenizer.encode_batch(texts)
                finally:
                    if prev_pad is None:
                        self.vision_tokenizer.no_padding()
                    else:
                        self.vision_tokenizer.enable_padding(**prev_pad)
            input_ids = np.array([enc.ids for enc in encs], dtype=np.int64)
            attention_mask = np.array([enc.attention_mask for enc in encs], dtype=np.int64)
            outputs = self.vision_text_session.run(
                None, {"input_ids": input_ids, "attention_mask": attention_mask}
            )
            raw = _extract_pooled_embed(self.vision_text_session, outputs)
            vecs: list[np.ndarray] = []
            for row in raw:
                vecs.append(l2_normalize(row.astype(np.float32)))
            return vecs

        # Models without attention_mask (e.g. SigLIP) cannot pad without altering attention weights:
        # bucket identical token lengths together so there is zero padding while still batching.
        with _EMBEDDER_LOCK:
            encs = self.vision_tokenizer.encode_batch(texts)
        buckets: dict[int, list[tuple[int, list[int]]]] = {}
        for idx, enc in enumerate(encs):
            buckets.setdefault(len(enc.ids), []).append((idx, enc.ids))
        out_vecs: list[Optional[np.ndarray]] = [None] * len(texts)
        for group in buckets.values():
            ids = np.array([item[1] for item in group], dtype=np.int64)
            outputs = self.vision_text_session.run(None, {"input_ids": ids})
            raw = _extract_pooled_embed(self.vision_text_session, outputs)
            for pos, (orig_idx, _) in enumerate(group):
                out_vecs[orig_idx] = l2_normalize(raw[pos].astype(np.float32))
        return [v for v in out_vecs if v is not None]

    def _encode_vision_text_128_uncached(self, text: str) -> Optional[np.ndarray]:
        clean = text.strip()
        if not clean:
            return None
        has_prompt = not clean.lower().startswith(("a photo of", "photo of", "an image of", "picture of"))
        prompt = f"a photo of {clean}" if has_prompt else clean
        raw = self._encode_vision_text_raw(prompt)
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
        self._ensure_vision_session()
        if self.vision_session is not None:
            pixel_values = self._preprocess_image(img)
            outputs = self.vision_session.run(None, {self._vision_input_name: pixel_values})
            raw = _extract_pooled_embed(self.vision_session, outputs)
            if raw.ndim >= 2:
                raw = raw[0]
            return l2_normalize(raw.astype(np.float32))

        small = img.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
        arr = np.asarray(small, dtype=np.float32).flatten()
        vec = np.zeros(512, dtype=np.float32)
        vec[: min(512, len(arr))] = arr[:512]
        return l2_normalize(vec)

    def _concept_bank_cache_paths(self) -> list[Path]:
        fname = f"concept_bank_{self.vision_model_id}.npy"
        data_env = os.environ.get("GOOD_SHELVES_DATA_DIR")
        if data_env:
            data_dir = Path(data_env)
        else:
            system = platform.system()
            if system == "Windows":
                appdata = os.environ.get("APPDATA")
                base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
                data_dir = base / "good-shelves"
            elif system == "Darwin":
                data_dir = Path.home() / "Library" / "Application Support" / "good-shelves"
            else:
                xdg = os.environ.get("XDG_DATA_HOME")
                base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
                data_dir = base / "good-shelves"
        return [self.models_dir / fname, data_dir / fname]

    def _get_concept_bank(self) -> Optional[tuple[list[str], np.ndarray]]:
        if self._concept_bank_cache is not None:
            return self._concept_bank_cache
        if self._vision_paths.get("text") is None or self._vision_paths.get("tokenizer") is None:
            return None

        global_key = (self.vision_model_id, len(VISUAL_CONCEPTS))
        cached_global = _CONCEPT_BANK_GLOBAL_CACHE.get(global_key)
        if cached_global is not None:
            self._concept_bank_cache = cached_global
            return cached_global

        expected_dim = int(self._vision_spec.get("dim", 512))
        expected_shape = (len(VISUAL_CONCEPTS), expected_dim)
        for cache_path in self._concept_bank_cache_paths():
            if cache_path.exists():
                try:
                    mat = np.load(cache_path).astype(np.float32, copy=False)
                    if mat.shape == expected_shape:
                        bank = (list(VISUAL_CONCEPTS), mat)
                        _CONCEPT_BANK_GLOBAL_CACHE[global_key] = bank
                        self._concept_bank_cache = bank
                        return bank
                except Exception:
                    pass

        vecs = self._encode_vision_text_batch([f"a photo of {concept}" for concept in VISUAL_CONCEPTS])
        if not vecs or len(vecs) != len(VISUAL_CONCEPTS):
            return None
        mat = np.stack(vecs, axis=0).astype(np.float32)
        bank = (list(VISUAL_CONCEPTS), mat)
        _CONCEPT_BANK_GLOBAL_CACHE[global_key] = bank
        self._concept_bank_cache = bank

        for cache_path in self._concept_bank_cache_paths():
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                np.save(cache_path, mat)
                break
            except OSError:
                continue

        return bank

    def classify_image_concepts(
        self, img: Image.Image, top_k: int = 4, vis_vec: Optional[np.ndarray] = None
    ) -> list[str]:
        """Zero-shot classify an image against VISUAL_CONCEPTS using the active Vision-Language model."""
        bank = self._get_concept_bank()
        if bank is None:
            return []
        labels, mat = bank
        if vis_vec is None:
            vis_vec = self._encode_image_raw(img)
        if vis_vec.shape[0] != mat.shape[1]:
            return []
        sims = mat @ vis_vec
        top_indices = np.argsort(sims)[::-1][:top_k]
        if len(top_indices) == 0:
            return []
        best = float(sims[int(top_indices[0])])
        median_sim = float(np.median(sims))
        is_siglip = "siglip" in self.vision_model_id
        min_best = 0.050 if is_siglip else 0.250
        min_margin = 0.045
        window = 0.025 if is_siglip else 0.030
        # Reject flat/abstract/low-confidence images so noisy guesses never become labels
        if best < min_best or (best - median_sim) < min_margin:
            return []
        cutoff = max(min_best - 0.005, best - window)
        return [labels[int(i)] for i in top_indices if float(sims[int(i)]) >= cutoff]

    def embed_parsed_file(self, parsed: ParsedFile) -> tuple[np.ndarray, int, int]:
        """
        Compute the fused 512-d L2-normalized multimodal vector for a ParsedFile,
        returning (vector_512, visual_share_pct, text_share_pct).
        """
        vis_raw: Optional[np.ndarray] = None
        if parsed.pil_image is not None:
            vis_raw = self._encode_image_raw(parsed.pil_image)

        auto_concepts: list[str] = []
        ocr_word_count = len((parsed.ocr_text or "").split())
        if (
            parsed.pil_image is not None
            and parsed.modality == "image"
            and vis_raw is not None
            and parsed.specimen_type not in {"receipt", "idcard", "diagram"}
            and ocr_word_count < 20
        ):
            auto_concepts = self.classify_image_concepts(parsed.pil_image, top_k=4, vis_vec=vis_raw)

        merged_visual_tags = list(dict.fromkeys([*(parsed.visual_tags or []), *auto_concepts]))
        if auto_concepts:
            parsed.visual_tags = merged_visual_tags
            # Keep parsed.title clean and honest (never overwrite it with raw CLIP guesses like "blossom · rose · anime")

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
        lead_text = full_text[:1500]
        text_384_lead = self._encode_text_384(lead_text)
        extra_chunks = [c[:1500] for c in (parsed.chunks or [])[1:4] if len(c.strip()) >= 60]
        if extra_chunks:
            extra_vecs = self.encode_text_384_batch(extra_chunks)
            mean_extra = l2_normalize(np.mean(extra_vecs, axis=0))
            text_384 = l2_normalize(0.75 * text_384_lead + 0.25 * mean_extra)
        else:
            text_384 = text_384_lead

        if vis_raw is not None:
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
        if has_vis and parsed.modality == "image" and word_count < 8:
            w_text = np.sqrt(0.55)
            w_vis = np.sqrt(0.45)
        elif has_vis:
            w_text = np.sqrt(0.80)
            w_vis = np.sqrt(0.20)
        else:
            w_text = 1.0
            w_vis = 0.0
        fused_512 = np.concatenate([w_text * text_384, w_vis * vis_128]).astype(np.float32)
        return l2_normalize(fused_512), visual_share, text_share

    def _encode_query_uncached(self, clean_q: str) -> np.ndarray:
        text_384 = self._encode_text_384(clean_q)
        vis_128 = self._cached_vis_text_128(clean_q)
        if vis_128 is None:
            vis_128 = np.zeros(128, dtype=np.float32)
            return l2_normalize(np.concatenate([text_384, vis_128]).astype(np.float32))
        w_text = np.sqrt(0.62)
        w_vis = np.sqrt(0.38)
        return l2_normalize(np.concatenate([w_text * text_384, w_vis * vis_128]).astype(np.float32))

    def encode_query(self, query: str, vis_128: Optional[np.ndarray] = None) -> tuple[np.ndarray, float]:
        """Encode a natural language search query into the 512-d L2-normalized vector space."""
        t0 = time.perf_counter()
        clean_q = (query or "").strip()
        if not clean_q:
            vec = np.zeros(512, dtype=np.float32)
            vec[0] = 1.0
            return vec, 0.2

        key = clean_q.lower()
        if vis_128 is not None:
            text_384 = self._encode_text_384(key)
            w_text = np.sqrt(0.62)
            w_vis = np.sqrt(0.38)
            fused_512 = l2_normalize(np.concatenate([w_text * text_384, w_vis * vis_128]).astype(np.float32))
        else:
            fused_512 = self._cached_query_512(key).copy()
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return fused_512, latency_ms
