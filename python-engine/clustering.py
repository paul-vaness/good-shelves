"""
Unsupervised Topic Shelf Discovery via UMAP (512-d -> 5-d & 2-d) + HDBSCAN + TF-IDF Keyphrase Naming.
Computes real 2D Meaning Map coordinates, HDBSCAN cluster assignments, Quiet Tray outliers (cluster_id = -1),
and real Silhouette Score.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import hdbscan
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import eigsh
from sklearn.decomposition import PCA, TruncatedSVD
from sklearn.metrics import silhouette_score


@dataclass
class DiscoveredShelf:
    shelf_id: str
    cluster_id: int
    num: str
    name: str
    desc: str
    file_ids: list[str]
    centroid_x: float
    centroid_y: float


def _refine_umap_layout(Y_init: np.ndarray, W: np.ndarray, epochs: int = 20) -> np.ndarray:
    """Run vectorized attractive/repulsive fuzzy cross-entropy force refinement in float32."""
    Y = Y_init.astype(np.float32, copy=True)
    std = np.std(Y, axis=0, keepdims=True)
    Y = Y / np.where(std > 1e-8, std, np.float32(1.0))
    W32 = W.astype(np.float32, copy=False)
    one_minus_W = np.float32(1.0) - W32
    n = Y.shape[0]
    chunk_size = 256 if n > 300 else max(1, n)

    for epoch in range(epochs):
        lr = np.float32(0.36 * (1.0 - epoch / float(epochs)))
        if chunk_size >= n:
            diff = Y[:, None, :] - Y[None, :, :]  # (N, N, d)
            sq_dist = np.sum(diff * diff, axis=-1) + np.float32(1e-4)
            inv_1p = np.float32(1.0) / (np.float32(1.0) + sq_dist)
            coeff = (
                (np.float32(2.0) * W32 - np.float32(0.08) * one_minus_W / (np.float32(0.1) + sq_dist))
                * inv_1p
            )[:, :, None]
            grad = np.sum(coeff * diff, axis=1)
        else:
            grad = np.zeros_like(Y)
            for s in range(0, n, chunk_size):
                e = min(n, s + chunk_size)
                diff = Y[s:e, None, :] - Y[None, :, :]
                sq_dist = np.sum(diff * diff, axis=-1) + np.float32(1e-4)
                inv_1p = np.float32(1.0) / (np.float32(1.0) + sq_dist)
                coeff = (
                    (np.float32(2.0) * W32[s:e] - np.float32(0.08) * one_minus_W[s:e] / (np.float32(0.1) + sq_dist))
                    * inv_1p
                )[:, :, None]
                grad[s:e] = np.sum(coeff * diff, axis=1)
        Y -= lr * np.clip(grad, np.float32(-4.0), np.float32(4.0))

    return Y


def _refine_umap_sparse(
    Y_init: np.ndarray,
    edge_src: np.ndarray,
    edge_dst: np.ndarray,
    edge_w: np.ndarray,
    epochs: int = 16,
    neg_samples: int = 8,
    seed: int = 42,
) -> np.ndarray:
    """Sparse edge-based attractive + negative-sampled repulsive UMAP refinement in O(|E|)."""
    Y = Y_init.astype(np.float32, copy=True)
    std = np.std(Y, axis=0, keepdims=True)
    Y = Y / np.where(std > 1e-8, std, np.float32(1.0))
    n = Y.shape[0]
    if n <= 1 or len(edge_src) == 0:
        return Y
    rng = np.random.default_rng(seed)
    grad = np.zeros_like(Y)
    w32 = edge_w.astype(np.float32, copy=False)

    for epoch in range(epochs):
        lr = np.float32(0.32 * (1.0 - epoch / float(epochs)))
        grad.fill(0.0)
        diff_e = Y[edge_src] - Y[edge_dst]
        sq_e = np.sum(diff_e * diff_e, axis=-1) + np.float32(1e-4)
        coeff_e = (np.float32(2.0) * w32 / (np.float32(1.0) + sq_e))[:, None]
        np.add.at(grad, edge_src, coeff_e * diff_e)

        # Sampled negative repulsion keeps memory and runtime O(N * neg_samples)
        for _ in range(neg_samples):
            neg = rng.integers(0, n, size=n)
            diff_n = Y - Y[neg]
            sq_n = np.sum(diff_n * diff_n, axis=-1) + np.float32(1e-4)
            coeff_n = (-np.float32(0.08) / ((np.float32(0.1) + sq_n) * (np.float32(1.0) + sq_n)))[:, None]
            grad += coeff_n * diff_n

        Y -= lr * np.clip(grad, np.float32(-4.0), np.float32(4.0))
    return Y


def _umap_project_pair_large(
    vectors: np.ndarray,
    n_comp_high: int,
    n_neighbors: int,
    random_state: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Chunked sparse-graph UMAP for large corpora (>1000 files) avoiding O(N^2) matrices."""
    n_samples = vectors.shape[0]
    k = min(n_neighbors, max(2, n_samples - 1))
    v32 = vectors.astype(np.float32, copy=False)
    v_norms = np.linalg.norm(v32, axis=1, keepdims=True)
    v_unit = v32 / np.where(v_norms > 1e-12, v_norms, np.float32(1.0))

    nn_idx = np.empty((n_samples, k), dtype=np.int32)
    nn_dists = np.empty((n_samples, k), dtype=np.float32)
    block = 512
    for s in range(0, n_samples, block):
        e = min(n_samples, s + block)
        sims = v_unit[s:e] @ v_unit.T
        dists = np.clip(np.float32(1.0) - sims, np.float32(0.0), np.float32(2.0))
        row_ids = np.arange(s, e)
        dists[np.arange(e - s), row_ids] = np.inf
        part_idx = np.argpartition(dists, k, axis=1)[:, :k]
        part_dists = dists[np.arange(e - s)[:, None], part_idx]
        order = np.argsort(part_dists, axis=1)
        nn_idx[s:e] = part_idx[np.arange(e - s)[:, None], order]
        nn_dists[s:e] = part_dists[np.arange(e - s)[:, None], order]

    pos_dists = np.where(nn_dists > 0.0, nn_dists, np.inf)
    min_pos = np.min(pos_dists, axis=1, keepdims=True)
    rho = np.where(np.isfinite(min_pos), min_pos, np.float32(0.0))
    shifted = np.maximum(np.float32(0.0), nn_dists - rho)

    target = np.float32(np.log2(k + 1))
    lo = np.full((n_samples, 1), 1e-5, dtype=np.float32)
    hi = np.full((n_samples, 1), 10.0, dtype=np.float32)
    for _ in range(16):
        mid = np.float32(0.5) * (lo + hi)
        psum = np.sum(np.exp(-shifted / mid), axis=1, keepdims=True)
        hi = np.where(psum > target, mid, hi)
        lo = np.where(psum <= target, mid, lo)
    sigma = np.maximum(np.float32(0.5) * (lo + hi), np.float32(1e-5))
    weights = np.exp(-shifted / sigma).astype(np.float32)

    rows_flat = np.repeat(np.arange(n_samples, dtype=np.int32), k)
    cols_flat = nn_idx.ravel()
    A = sp.csr_matrix((weights.ravel(), (rows_flat, cols_flat)), shape=(n_samples, n_samples), dtype=np.float32)
    W = (A + A.T - A.multiply(A.T)).tocoo()

    max_c = max(n_comp_high, 2)
    deg = np.asarray(W.sum(axis=1)).ravel()
    deg_inv_sqrt = np.where(deg > 1e-9, 1.0 / np.sqrt(np.maximum(deg, 1e-9)), 0.0).astype(np.float32)
    D_inv = sp.diags(deg_inv_sqrt)
    L_sym = sp.eye(n_samples, dtype=np.float32) - (D_inv @ W @ D_inv)
    try:
        _, vecs = eigsh(L_sym, k=min(max_c + 1, n_samples - 2), which="SM")
        Y_spec = vecs[:, 1 : 1 + max_c].astype(np.float32)
        if Y_spec.shape[1] < max_c:
            Y_spec = TruncatedSVD(n_components=max_c, random_state=random_state).fit_transform(v_unit).astype(np.float32)
    except Exception:
        Y_spec = TruncatedSVD(n_components=max_c, random_state=random_state).fit_transform(v_unit).astype(np.float32)

    emb_5d = _refine_umap_sparse(Y_spec[:, :n_comp_high], W.row, W.col, W.data, epochs=16, seed=random_state)
    emb_2d = _refine_umap_sparse(Y_spec[:, :2], W.row, W.col, W.data, epochs=18, seed=random_state + 1)
    return emb_5d, emb_2d


def umap_project_pair(
    vectors: np.ndarray,
    n_comp_high: int = 5,
    n_neighbors: int = 6,
    random_state: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Simultaneously project 512-d L2-normalized vectors into 5-d (for HDBSCAN) and 2-d (for Meaning Map)
    by sharing the fuzzy simplicial set graph construction and spectral Laplacian eigendecomposition.
    """
    n_samples = vectors.shape[0]
    if n_samples == 0:
        return np.zeros((0, n_comp_high), dtype=np.float32), np.zeros((0, 2), dtype=np.float32)
    if n_samples <= 3:
        out5 = np.zeros((n_samples, n_comp_high), dtype=np.float32)
        out2 = np.zeros((n_samples, 2), dtype=np.float32)
        out5[:, : min(n_samples, n_comp_high)] = vectors[:, : min(n_samples, n_comp_high)]
        out2[:, : min(n_samples, 2)] = vectors[:, : min(n_samples, 2)]
        return out5, out2
    if n_samples > 1000:
        return _umap_project_pair_large(vectors, n_comp_high, n_neighbors, random_state)

    k = min(n_neighbors, max(2, n_samples - 1))
    v32 = vectors.astype(np.float32, copy=False)
    v_norms = np.linalg.norm(v32, axis=1, keepdims=True)
    v_unit = v32 / np.where(v_norms > 1e-12, v_norms, np.float32(1.0))
    dist_mat = np.clip(np.float32(1.0) - (v_unit @ v_unit.T), np.float32(0.0), np.float32(2.0))
    np.fill_diagonal(dist_mat, np.inf)

    # Step 1: Vectorized kNN selection + batched binary search for sigma_i
    k_part = min(k, n_samples - 1)
    nn_idx = np.argpartition(dist_mat, k_part, axis=1)[:, :k]
    rows = np.arange(n_samples)[:, None]
    nn_dists = dist_mat[rows, nn_idx]
    order = np.argsort(nn_dists, axis=1)
    nn_idx = nn_idx[rows, order]
    nn_dists = nn_dists[rows, order]

    pos_dists = np.where(nn_dists > 0.0, nn_dists, np.inf)
    min_pos = np.min(pos_dists, axis=1, keepdims=True)
    rho = np.where(np.isfinite(min_pos), min_pos, np.float32(0.0))
    shifted = np.maximum(np.float32(0.0), nn_dists - rho)

    target = np.float32(np.log2(k + 1))
    lo = np.full((n_samples, 1), 1e-5, dtype=np.float32)
    hi = np.full((n_samples, 1), 10.0, dtype=np.float32)
    for _ in range(16):
        mid = np.float32(0.5) * (lo + hi)
        psum = np.sum(np.exp(-shifted / mid), axis=1, keepdims=True)
        hi = np.where(psum > target, mid, hi)
        lo = np.where(psum <= target, mid, lo)
    sigma = np.maximum(np.float32(0.5) * (lo + hi), np.float32(1e-5))

    A = np.zeros((n_samples, n_samples), dtype=np.float32)
    A[rows, nn_idx] = np.exp(-shifted / sigma)

    # Step 2: Fuzzy set union W = A + A^T - A o A^T
    W = A + A.T - (A * A.T)
    np.fill_diagonal(W, 0.0)

    # Step 3: Normalized Laplacian spectral initialization (computed once for both 5-d and 2-d)
    deg = np.sum(W, axis=1)
    deg_inv_sqrt = np.where(deg > 1e-9, np.float32(1.0) / np.sqrt(np.maximum(deg, np.float32(1e-9))), np.float32(0.0))
    L_sym = np.eye(n_samples, dtype=np.float32) - (deg_inv_sqrt[:, None] * W * deg_inv_sqrt[None, :])

    max_c = max(n_comp_high, 2)
    try:
        _, vecs = np.linalg.eigh(L_sym)
        start_idx = 1 if n_samples > max_c + 1 else 0
        Y_spec = vecs[:, start_idx : start_idx + max_c].astype(np.float32)
        if Y_spec.shape[1] < max_c:
            Y_spec = PCA(n_components=max_c, random_state=random_state).fit_transform(vectors).astype(np.float32)
    except Exception:
        Y_spec = PCA(n_components=max_c, random_state=random_state).fit_transform(vectors).astype(np.float32)

    emb_5d = _refine_umap_layout(Y_spec[:, :n_comp_high], W, epochs=18)
    emb_2d = _refine_umap_layout(Y_spec[:, :2], W, epochs=20)
    return emb_5d, emb_2d


def umap_project(
    vectors: np.ndarray,
    n_components: int = 2,
    n_neighbors: int = 6,
    min_dist: float = 0.15,
    random_state: int = 42,
) -> np.ndarray:
    """Single-projection entrypoint preserved for API compatibility."""
    emb_high, emb_2d = umap_project_pair(
        vectors, n_comp_high=max(2, n_components), n_neighbors=n_neighbors, random_state=random_state
    )
    return emb_2d if n_components == 2 else emb_high[:, :n_components]


_GENERAL_CATEGORIES: dict[str, tuple[str, str, str]] = {
    "receipts": (
        "shelf-receipts-finance",
        "Receipts & Finance",
        "Receipts, invoices, statements, and financial records",
    ),
    "screenshots": (
        "shelf-screenshots",
        "Screenshots & Captures",
        "Screen captures, interface snapshots, and quick clips",
    ),
    "people": (
        "shelf-photos-people",
        "Photos & People",
        "Portraits, people, family, and camera photos",
    ),
    "places": (
        "shelf-photos-places",
        "Photos & Places",
        "Landscapes, nature, flowers, food, pets, and travel photos",
    ),
    "diagrams": (
        "shelf-diagrams-design",
        "Diagrams & Design",
        "System diagrams, charts, illustrations, and design assets",
    ),
    "code": (
        "shelf-code-data",
        "Code & Data",
        "Source code, scripts, configuration files, and tabular datasets",
    ),
    "ids": (
        "shelf-ids-forms",
        "IDs & Official Forms",
        "Identification cards, registrations, contracts, and official forms",
    ),
    "media": (
        "shelf-media-archives",
        "Media & Archives",
        "Audio recordings, video clips, and compressed archives",
    ),
    "documents": (
        "shelf-documents-reports",
        "Documents & Reports",
        "Articles, notes, PDFs, presentations, and written documents",
    ),
    "tray": (
        "tray",
        "Quiet Tray",
        "Miscellaneous or unclustered files waiting for a shelf",
    ),
}

_RECEIPT_RE = re.compile(
    r"\b(receipt|invoice|billing|statement|payment|subtotal|total due|official receipt|gcash|maya|paypal|vat|tax invoice|expense|budget|reimbursement)\b",
    re.IGNORECASE,
)
_ID_FORM_RE = re.compile(
    r"\b(passport|driver'?s? license|national id|philid|birth certificate|registration form|tax form|application form|agreement|contract)\b",
    re.IGNORECASE,
)
_SCREENSHOT_NAME_RE = re.compile(
    r"(screenshot|screen[-_ ]?shot|clean[-_ ]?shot|screen[-_ ]?capture|snip|scr[-_])",
    re.IGNORECASE,
)
_CAMERA_NAME_RE = re.compile(r"^(img|dsc|pxl|dcim|mvimg|vid)[-_]", re.IGNORECASE)
_DIAGRAM_RE = re.compile(
    r"\b(diagram|flowchart|wireframe|schematic|architecture diagram|whiteboard|mindmap)\b",
    re.IGNORECASE,
)
_PEOPLE_WORDS = {
    "person", "people", "girl", "boy", "man", "men", "woman", "women",
    "portrait", "selfie", "face", "family", "friends", "couple", "wedding",
    "baby", "child", "crowd", "group",
}
_UI_VISUAL_TAGS = {"screenshot", "ui screen", "web page", "terminal window", "dashboard", "chat message"}
_CODE_EXTS = {
    "py", "ts", "tsx", "js", "jsx", "rs", "go", "java", "c", "cpp", "h",
    "cs", "php", "rb", "sh", "bash", "zsh", "sql", "json", "yaml", "yml",
    "toml", "xml", "html", "css", "scss", "csv", "xlsx", "xlsm", "tsv",
}
_MEDIA_EXTS = {
    "mp3", "wav", "flac", "m4a", "aac", "ogg",
    "mp4", "mov", "mkv", "avi", "webm",
    "zip", "tar", "gz", "7z", "rar",
}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "heic", "avif", "gif", "bmp", "tif", "tiff"}
_DOC_EXTS = {"pdf", "docx", "doc", "txt", "md", "markdown", "rtf", "odt", "pptx", "ppt", "epub"}


def _extract_visual_tags(m: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    raw_vt = m.get("visual_tags")
    if isinstance(raw_vt, list):
        tags.extend(str(t).lower() for t in raw_vt)
    meta_raw = m.get("metadata_json") or m.get("metadata")
    if isinstance(meta_raw, str) and meta_raw.strip():
        try:
            meta_obj = json.loads(meta_raw)
        except Exception:
            meta_obj = {}
    elif isinstance(meta_raw, dict):
        meta_obj = meta_raw
    else:
        meta_obj = {}
    vc = meta_obj.get("visualConcepts")
    if isinstance(vc, list):
        tags.extend(str(t).lower() for t in vc)
    return tags


def _classify_single_file_category(m: dict[str, Any]) -> str:
    """Classify any file metadata row into one clean generalized category key."""
    name = str(m.get("name") or "")
    title = str(m.get("title") or "")
    ext = str(m.get("format") or m.get("ext") or Path(name).suffix.lstrip(".")).lower().lstrip(".")
    modality = str(m.get("modality") or "").lower()
    specimen = str(m.get("specimen_type") or m.get("specimen") or "").lower()
    ocr_text = str(m.get("ocr_text") or "")[:450]
    extracted_text = str(m.get("extracted_text") or "")[:650]
    visual_tags = _extract_visual_tags(m)
    vt_set = set(visual_tags)

    header_blob = f"{name.replace('_', ' ').replace('-', ' ')} {title}".lower()
    body_blob = f"{ocr_text} {extracted_text}".lower()
    body_words = len(body_blob.split())
    try:
        page_count = int(m.get("page_count") or m.get("pageCount") or 1)
    except (TypeError, ValueError):
        page_count = 1

    is_pure_code = (
        modality == "code"
        or specimen == "code"
        or ext in (_CODE_EXTS - {"csv", "xlsx", "xlsm", "tsv"})
    )

    # 1. IDs & Official Forms
    is_test_script = "test script" in header_blob or "test_script" in name.lower()
    is_academic_or_doc = any(
        w in header_blob
        for w in (
            "transcript",
            "interview",
            "chapter",
            "thesis",
            "paper template",
            "syllabus",
            "scholarship",
            "lecture notes",
            "study guide",
            "assignment",
            "lab report",
        )
    )
    if not is_pure_code and not is_test_script and not is_academic_or_doc:
        if (
            specimen == "idcard"
            or _ID_FORM_RE.search(header_blob)
            or (
                body_words <= 220
                and page_count <= 2
                and ext not in {"md", "markdown", "txt", "csv", "json", "xlsx", "pptx", "py", "ts", "tsx", "html"}
                and len(_ID_FORM_RE.findall(body_blob)) >= 2
            )
        ):
            return "ids"

    # 2. Screenshots & Captures
    is_image = modality == "image" or ext in _IMAGE_EXTS
    if is_image and (
        _SCREENSHOT_NAME_RE.search(name)
        or (not _CAMERA_NAME_RE.match(name) and bool(vt_set & _UI_VISUAL_TAGS))
    ):
        return "screenshots"

    # 3. Diagrams & Design (checked before Receipts so whiteboard/diagram images are never misrouted)
    is_not_diagram_doc = (
        is_pure_code
        or page_count >= 2
        or ext in {"md", "markdown", "txt", "docx", "rtf", "odt", "pptx", "ppt", "xlsx", "xlsm", "xls", "csv", "tsv"}
        or any(
            w in header_blob
            for w in (
                "assignment",
                "study guide",
                "lab report",
                "syllabus",
                "paper",
                "chapter",
                "residual learning",
            )
        )
    )
    if not is_not_diagram_doc:
        if (
            ext in {"svg", "fig", "drawio"}
            or _DIAGRAM_RE.search(header_blob)
            or (specimen == "diagram" and page_count == 1 and body_words < 180)
        ):
            return "diagrams"

    # 4. Receipts & Finance
    is_travel_booking = any(
        w in header_blob or w in body_blob[:260]
        for w in ("boarding pass", "flight itinerary", "flight booking", "booking reference (pnr)", "hotel reservation")
    )
    if not is_pure_code and not is_test_script and not is_academic_or_doc and not is_travel_booking:
        if (
            specimen == "receipt"
            or _RECEIPT_RE.search(header_blob)
            or (
                ext in {"csv", "xlsx", "xlsm", "xls"}
                and any(
                    k in header_blob or k in body_blob[:240]
                    for k in (
                        "balance",
                        "due date",
                        "amount_php",
                        "amount",
                        "budget",
                        "expense",
                        "invoice",
                        "receipt",
                        "tuition",
                        "salary",
                        "payroll",
                    )
                )
            )
            or (
                body_words <= 240
                and page_count <= 2
                and ext not in {"md", "markdown", "txt", "py", "ts", "tsx", "html", "docx", "pptx"}
                and len(_RECEIPT_RE.findall(body_blob)) >= 2
            )
        ):
            return "receipts"

    # 5. Code & Data
    if is_pure_code or ext in _CODE_EXTS:
        return "code"

    # 6. Media & Archives
    if modality in {"audio", "video", "archive"} or ext in _MEDIA_EXTS:
        return "media"

    # 7. Photos & People / Photos & Places
    if is_image:
        name_title_tokens = set(re.findall(r"[a-z]+", f"{name} {title}".lower()))
        if (vt_set & _PEOPLE_WORDS) or (name_title_tokens & _PEOPLE_WORDS):
            return "people"
        ocr_words = len(ocr_text.split())
        if ocr_words >= 25 and not _CAMERA_NAME_RE.match(name):
            return "documents"
        return "places"

    if modality == "document" or ext in _DOC_EXTS:
        return "documents"

    return "documents"


def derive_specific_sub_shelf(m: dict[str, Any], cat_key: Optional[str] = None) -> str:
    """Assign a clean, specific sub-shelf label nested under a file's generalized shelf."""
    cat = cat_key or _classify_single_file_category(m)
    name = str(m.get("name") or "")
    title = str(m.get("title") or "")
    ext = str(m.get("format") or m.get("ext") or Path(name).suffix.lstrip(".")).lower().lstrip(".")
    modality = str(m.get("modality") or "").lower()
    specimen = str(m.get("specimen_type") or m.get("specimen") or "").lower()
    ocr_text = str(m.get("ocr_text") or "")[:450]
    extracted_text = str(m.get("extracted_text") or "")[:650]
    visual_tags = _extract_visual_tags(m)
    blob = f"{name.replace('_', ' ').replace('-', ' ')} {title} {ocr_text} {extracted_text} {' '.join(visual_tags)}".lower()

    if cat == "receipts":
        if any(k in blob for k in ("bir", "2316", "itr", "w-2", "1099", "tax return", "withholding tax", "tax certificate")):
            return "Tax & Filings"
        if any(k in blob for k in ("payslip", "payroll", "net pay", "gross pay", "salary slip")):
            return "Payslips & Payroll"
        if any(k in blob for k in ("bank statement", "statement of account", "credit card statement", "bdo", "bpi", "unionbank", "chase")):
            return "Bank & Card Statements"
        if any(k in blob for k in ("aws", "amazon web services", "google cloud", "azure", "cloudhost", "vercel", "digitalocean", "openai", "anthropic", "github", "subscription")):
            return "Cloud & SaaS"
        if any(k in blob for k in ("coffee", "latte", "cafe", "restaurant", "food", "bakery", "grocery", "market", "meal", "grabfood", "jollibee", "starbucks")):
            return "Food & Dining"
        if any(k in blob for k in ("electric", "water", "meralco", "maynilad", "internet", "pldt", "globe", "converge", "utility", "rent", "tuition")):
            return "Bills & Utilities"
        if ext in {"csv", "xlsx", "xlsm"} or any(k in blob for k in ("budget", "ledger", "expense")):
            return "Budgets & Sheets"
        if any(k in blob for k in ("invoice", "billing", "statement")):
            return "Invoices & Statements"
        return "Receipts & Slips"

    if cat == "screenshots":
        if any(k in blob for k in ("terminal", "vscode", "console", "traceback", "localhost", "stack")):
            return "Code & Terminal Snaps"
        if any(k in blob for k in ("browser", "chrome", "firefox", "web", "chat", "message", "discord", "slack")):
            return "Web & Chat Captures"
        return "App & Screen Captures"

    if cat == "people":
        if any(k in blob for k in ("portrait", "selfie", "headshot", "profile")):
            return "Portraits & Selfies"
        if any(k in blob for k in ("family", "friends", "group", "wedding", "birthday", "graduation", "party", "team")):
            return "Group & Family"
        return "People Photos"

    if cat == "places":
        tokens_set = set(re.findall(r"[a-z0-9]+", blob))
        if (
            any(p in blob for p in ("van gogh", "starry night"))
            or bool(
                tokens_set
                & {
                    "painting",
                    "art",
                    "artwork",
                    "drawing",
                    "sketch",
                    "illustration",
                    "rosetta",
                    "stone",
                    "artifact",
                    "sculpture",
                    "museum",
                    "poster",
                    "symposium",
                }
            )
        ):
            return "Art & Heritage"
        if bool(
            tokens_set
            & {
                "microchip",
                "cpu",
                "die",
                "macro",
                "prism",
                "light",
                "dispersion",
                "optics",
                "cell",
                "mitosis",
                "onion",
                "microscope",
                "specimen",
                "lab",
            }
        ):
            return "Science & Macro"
        if bool(
            tokens_set
            & {
                "flower",
                "blossom",
                "rose",
                "garden",
                "plant",
                "tree",
                "forest",
                "sunset",
                "sunrise",
                "mountain",
                "mount",
                "fuji",
                "glacier",
                "ice",
                "canyon",
                "strata",
                "ocean",
                "beach",
                "lake",
                "river",
                "waterfall",
                "eclipse",
                "solar",
                "corona",
                "sky",
                "nature",
                "landscape",
                "butterfly",
                "monarch",
            }
        ):
            return "Nature & Landscapes"
        if "study desk" in blob or bool(
            tokens_set
            & {
                "food",
                "meal",
                "cake",
                "pizza",
                "coffee",
                "drink",
                "dish",
                "breakfast",
                "dinner",
                "cafe",
                "restaurant",
            }
        ):
            return "Food & Drinks"
        if bool(tokens_set & {"dog", "cat", "puppy", "kitten", "pet", "bird", "animal", "horse", "fish"}):
            return "Pets & Animals"
        if bool(
            tokens_set
            & {
                "parthenon",
                "athens",
                "colosseum",
                "roman",
                "temple",
                "church",
                "bridge",
                "building",
                "house",
                "library",
                "interior",
                "exterior",
                "campus",
                "university",
                "city",
                "street",
                "travel",
                "trip",
                "hotel",
                "flight",
                "monument",
                "tower",
                "arch",
                "car",
                "road",
            }
        ):
            return "Landmarks & Architecture"
        return "Camera Photos"

    if cat == "diagrams":
        if ext in {"svg", "fig", "drawio"}:
            return "Vector & SVG Assets"
        if any(k in blob for k in ("architecture", "flowchart", "erd", "uml", "sequence", "pipeline", "system")):
            return "Architecture & Flowcharts"
        return "Design & Sketches"

    if cat == "code":
        if ext == "py":
            return "Python Scripts"
        if ext in {"rs", "c", "cpp", "h", "go"}:
            return "Rust & Systems"
        if ext in {"ts", "tsx", "js", "jsx", "html", "css", "scss"}:
            return "Web & TypeScript"
        if ext in {"csv", "xlsx", "xlsm", "tsv"}:
            return "Spreadsheets & CSV"
        if any(k in blob for k in ("dockerfile", "docker-compose", "kubernetes", "terraform", "github actions", "workflow")):
            return "DevOps & Containers"
        if ext in {"json", "yaml", "yml", "toml", "xml", "sql"}:
            return "Configs & Schemas"
        return "Source Code"

    if cat == "ids":
        if any(k in blob for k in ("contract", "agreement", "nda", "msa", "mou", "moa", "lease")):
            return "Contracts & Agreements"
        if any(k in blob for k in ("bir", "2316", "itr", "tax return", "tax certificate", "nbi clearance", "psa")):
            return "Tax & Government Forms"
        if any(k in blob for k in ("medical", "laboratory", "cbc", "prescription", "hmo", "vaccination", "physician")):
            return "Medical & Health Records"
        if any(k in blob for k in ("insurance", "policy number", "beneficiary")):
            return "Insurance & Policies"
        if specimen == "idcard" or any(k in blob for k in ("passport", "license", "national id", "philid")):
            return "IDs & Passports"
        return "Official Forms"

    if cat == "media":
        if ext in {"zip", "tar", "gz", "7z", "rar"} or modality == "archive":
            return "Archives & Backups"
        if ext in {"mp3", "wav", "flac", "m4a", "aac", "ogg"} or modality == "audio":
            return "Audio Recordings"
        return "Video Clips"

    # documents
    if ext in {"pptx", "ppt"}:
        return "Slide Decks"
    if any(k in blob for k in ("boarding pass", "flight", "itinerary", "pnr", "hotel reservation", "cebu pacific", "airbnb", "booking reference")):
        return "Travel & Bookings"
    if any(k in blob for k in ("resume", "curriculum vitae", "work experience", "cover letter", "portfolio")):
        return "Resumes & Career"
    if any(k in blob for k in ("laboratory report", "complete blood count", "cbc", "prescription", "attending physician", "medical certificate")):
        return "Medical & Health Records"
    if any(k in blob for k in ("meeting notes", "sync notes", "minutes of meeting", "agenda", "attendees", "standup")):
        return "Meeting Notes & Agendas"
    if any(k in blob for k in ("transcript", "interview")):
        return "Transcripts & Interviews"
    if any(k in blob for k in ("test script", "test case", "qa")):
        return "Test Scripts & QA"
    if any(
        k in blob
        for k in (
            "assignment",
            "syllabus",
            "study guide",
            "review notes",
            "midterm",
            "exam",
            "quiz",
            "homework",
            "course",
            "lecture",
        )
    ):
        return "Assignments & Coursework"
    if ext in {"md", "markdown", "txt"} or any(k in blob for k in ("notes", "journal", "todo", "readme")):
        return "Notes & Markdown"
    if any(k in blob for k in ("paper", "research", "thesis", "imrad", "report", "study", "abstract", "methodology", "evaluation", "chapter", "scholarship")):
        return "Research & Reports"
    if ext == "pdf":
        return "PDF Documents"
    return "Written Docs"


_CLUSTER_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "your", "are",
    "was", "were", "have", "has", "had", "not", "but", "what", "all", "can",
    "out", "use", "used", "using", "file", "files", "page", "pages", "section",
    "document", "documents", "image", "photo", "picture", "screenshot", "scan",
    "untitled", "copy", "draft", "final", "new", "img", "dsc", "pxl", "pdf",
    "jpg", "jpeg", "png", "txt", "docx", "xlsx", "csv", "json", "html",
    "webp", "bmp", "svg", "gif", "heic", "avif", "tiff", "tif", "mp4", "mov",
    "mkv", "avi", "mp3", "wav", "flac", "m4a", "zip", "tar", "gz", "py", "ts",
    "tsx", "js", "jsx", "cpp", "c", "rs", "go", "java", "sql", "md", "markdown",
    "rtf", "odt", "pptx", "ppt", "log", "tsv", "xlsm", "xls", "script", "table",
    "spreadsheet", "archive", "entries",
}

_GENERIC_SUB_SHELVES = {"Written Docs", "Camera Photos", "Source Code", "Other Files"}


def _compute_cluster_topics(files_meta: list[dict[str, Any]], labels: np.ndarray) -> dict[int, str]:
    """Derive 1-2 word TF-IDF topic labels for coherent HDBSCAN clusters."""
    n = len(files_meta)
    doc_tokens: list[set[str]] = []
    global_df: Counter[str] = Counter()
    for m in files_meta:
        blob = " ".join([
            str(m.get("title") or ""),
            str(m.get("name") or "").replace("_", " ").replace("-", " "),
            str(m.get("extracted_text") or "")[:360],
            str(m.get("ocr_text") or "")[:360],
        ]).lower()
        toks = {t for t in re.findall(r"[a-z]{3,}", blob) if t not in _CLUSTER_STOPWORDS}
        doc_tokens.append(toks)
        global_df.update(toks)

    cluster_members: dict[int, list[int]] = {}
    for idx, cid in enumerate(labels):
        c = int(cid)
        if c >= 0:
            cluster_members.setdefault(c, []).append(idx)

    topics: dict[int, str] = {}
    for cid, idxs in cluster_members.items():
        if len(idxs) < 2:
            continue
        tf: Counter[str] = Counter()
        for i in idxs:
            tf.update(doc_tokens[i])
        scored: list[tuple[float, str]] = []
        for tok, cnt in tf.items():
            if cnt < 2:
                continue
            idf = float(np.log((n + 1.0) / (global_df[tok] + 0.5)) + 1.0)
            scored.append((cnt * idf, tok))
        scored.sort(reverse=True)
        if scored:
            top_words = [w.capitalize() for _, w in scored[:2]]
            topics[cid] = " & ".join(top_words) if len(top_words) == 2 else top_words[0]
    return topics


def _derive_shelf_label_and_desc(files_meta: list[dict[str, Any]], cluster_idx: int) -> tuple[str, str, str]:
    """Assign each cluster to its dominant generalized human-readable category."""
    if not files_meta:
        return _GENERAL_CATEGORIES["documents"]
    cat_counts = Counter(_classify_single_file_category(m) for m in files_meta)
    top_cat = cat_counts.most_common(1)[0][0]
    return _GENERAL_CATEGORIES.get(top_cat, _GENERAL_CATEGORIES["documents"])


def _island_anchors(m_shelves: int) -> list[tuple[float, float]]:
    """Return well-separated 2D shelf island anchors in [20, 80] x [24, 78]."""
    if m_shelves <= 1:
        return [(50.0, 50.0)]
    if m_shelves == 2:
        return [(30.0, 50.0), (70.0, 50.0)]
    if m_shelves == 3:
        return [(28.0, 34.0), (72.0, 34.0), (50.0, 72.0)]
    if m_shelves == 4:
        return [(28.0, 32.0), (72.0, 32.0), (28.0, 70.0), (72.0, 70.0)]

    cols = int(np.ceil(np.sqrt(m_shelves)))
    rows = int(np.ceil(m_shelves / cols))
    anchors: list[tuple[float, float]] = []
    for idx_g in range(m_shelves):
        r = idx_g // cols
        c = idx_g % cols
        row_items = cols if r < rows - 1 else (m_shelves - r * cols)
        ax = 50.0 if row_items <= 1 else 22.0 + c * (56.0 / (row_items - 1))
        ay = 50.0 if rows <= 1 else 28.0 + r * (46.0 / (rows - 1))
        anchors.append((float(ax), float(ay)))
    return anchors


def run_shelf_clustering(
    files_meta: list[dict[str, Any]],
    vectors: np.ndarray,
) -> tuple[list[DiscoveredShelf], dict[str, dict[str, Any]], float]:
    """
    Run UMAP (512-d -> 5-d & 2-d) + HDBSCAN over all indexed files.
    Returns:
      - shelves: ordered list of DiscoveredShelf (including Quiet Tray for cluster_id = -1)
      - file_updates: dict[file_id, {cluster_id, shelf_id, shelf_name, map_x, map_y}]
      - silhouette: float Silhouette score in [-1, 1]
    """
    n = len(files_meta)
    if n == 0:
        return [], {}, 0.0

    # 1. Pre-classify structural category keys and blend a small deterministic category-onehot anchor (0.18 weight)
    # with the L2-normalized 512-d embeddings before UMAP projection to improve HDBSCAN coherence & Silhouette score.
    cat_keys_order = tuple(_GENERAL_CATEGORIES.keys())
    cat_to_idx = {k: i for i, k in enumerate(cat_keys_order)}
    file_cats = [_classify_single_file_category(m) for m in files_meta]

    v32 = vectors.astype(np.float32, copy=False)
    v_norms = np.linalg.norm(v32, axis=1, keepdims=True)
    v_unit = v32 / np.where(v_norms > 1e-12, v_norms, np.float32(1.0))

    cat_anchor = np.zeros((n, len(cat_keys_order)), dtype=np.float32)
    for idx, c_key in enumerate(file_cats):
        cat_anchor[idx, cat_to_idx.get(c_key, cat_to_idx["documents"])] = np.float32(0.18)

    blended_vecs = np.hstack([v_unit, cat_anchor])
    b_norms = np.linalg.norm(blended_vecs, axis=1, keepdims=True)
    blended_vecs = blended_vecs / np.where(b_norms > 1e-12, b_norms, np.float32(1.0))

    n_comp_5 = min(5, max(2, n - 1))
    emb_5d, emb_2d = umap_project_pair(
        blended_vecs, n_comp_high=n_comp_5, n_neighbors=min(5, max(2, n - 1)), random_state=42
    )

    # 2. Run HDBSCAN density clustering on the 5-d UMAP manifold
    min_cs = max(2, min(5, n // 8)) if n >= 16 else 2
    sel_method = "leaf" if n > 16 else "eom"
    if n >= 4:
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=min_cs,
            min_samples=1,
            metric="euclidean",
            cluster_selection_method=sel_method,
        )
        raw_labels = clusterer.fit_predict(emb_5d)
    else:
        raw_labels = np.zeros(n, dtype=np.int32)

    # 3. Cosine-coherence outlier pruning: compute per-cluster peer similarity without full N x N matrix
    cluster_members: dict[int, list[int]] = {}
    for idx, cid in enumerate(raw_labels):
        c = int(cid)
        if c != -1:
            cluster_members.setdefault(c, []).append(idx)

    labels = np.full(n, -1, dtype=np.int32)
    for c_id, members in cluster_members.items():
        if len(members) < 2:
            continue
        m_arr = np.asarray(members, dtype=np.intp)
        sub_v = vectors[m_arr]
        if len(members) <= 1200:
            sub_sim = np.clip(sub_v @ sub_v.T, -1.0, 1.0)
            np.fill_diagonal(sub_sim, 0.0)
            max_peer_sim = np.max(sub_sim, axis=1)
        else:
            max_peer_sim = np.zeros(len(members), dtype=np.float32)
            for s in range(0, len(members), 512):
                e = min(len(members), s + 512)
                blk_sim = np.clip(sub_v[s:e] @ sub_v.T, -1.0, 1.0)
                blk_sim[np.arange(e - s), np.arange(s, e)] = 0.0
                max_peer_sim[s:e] = np.max(blk_sim, axis=1)
        for pos, i in enumerate(members):
            if float(max_peer_sim[pos]) >= 0.35:
                labels[i] = c_id

    # Dissolve any cluster that dropped below 2 members after coherence pruning
    kept_counts = Counter(int(x) for x in labels if x != -1)
    for i in range(n):
        if labels[i] != -1 and kept_counts[int(labels[i])] < 2:
            labels[i] = -1

    # 4. Calculate actual Silhouette score on clustered (non-noise) items in the UMAP manifold
    non_noise_mask = labels != -1
    unique_clusters = set(int(x) for x in labels[non_noise_mask])
    if len(unique_clusters) >= 2 and int(np.sum(non_noise_mask)) > len(unique_clusters):
        try:
            sil_score = float(
                silhouette_score(
                    emb_5d[non_noise_mask],
                    labels[non_noise_mask],
                    metric="euclidean",
                )
            )
        except Exception:
            sil_score = 0.0
    else:
        sil_score = 0.0
    sil_score = round(sil_score, 3)

    # 5. Organize files into generalized categories while surfacing HDBSCAN TF-IDF sub-shelves & Quiet Tray
    cluster_topics = _compute_cluster_topics(files_meta, labels)
    category_groups: dict[str, list[int]] = {}
    for idx in range(n):
        m = files_meta[idx]
        cat_key = file_cats[idx]
        ext = str(m.get("format") or Path(str(m.get("name") or "")).suffix.lstrip(".")).lower().lstrip(".")
        modality = str(m.get("modality") or "").lower()
        if (
            n >= 10
            and int(labels[idx]) == -1
            and cat_key == "documents"
            and modality != "document"
            and ext not in _DOC_EXTS
        ):
            cat_key = "tray"
        category_groups.setdefault(cat_key, []).append(idx)

    ordered_groups: list[tuple[str, str, str, str, list[int]]] = []
    for cat_key, member_indices in category_groups.items():
        if not member_indices:
            continue
        s_id, s_name, s_desc = _GENERAL_CATEGORIES.get(cat_key, _GENERAL_CATEGORIES["documents"])
        ordered_groups.append((cat_key, s_id, s_name, s_desc, member_indices))

    ordered_groups.sort(key=lambda item: (item[1] == "tray", -len(item[4])))

    # 6. Topic-island 2D placement + anisotropic pill-aware collision separation
    anchors = _island_anchors(len(ordered_groups))
    coords_x = np.full(n, 50.0, dtype=np.float32)
    coords_y = np.full(n, 50.0, dtype=np.float32)

    for grp_idx, (_, _, _, _, member_indices) in enumerate(ordered_groups):
        anchor_x, anchor_y = anchors[grp_idx]
        k = len(member_indices)
        if k == 1:
            coords_x[member_indices[0]] = anchor_x
            coords_y[member_indices[0]] = anchor_y
            continue
        rx = min(15.0, 4.5 + 2.6 * float(np.sqrt(k)))
        ry = min(13.0, 4.0 + 2.2 * float(np.sqrt(k)))
        sub_xy = emb_2d[member_indices]
        lo = np.min(sub_xy, axis=0)
        hi = np.max(sub_xy, axis=0)
        span = hi - lo
        mid = 0.5 * (lo + hi)
        if float(span[0]) > 1e-6:
            nx = (sub_xy[:, 0] - mid[0]) / (0.5 * span[0])
        else:
            ang = np.linspace(0.0, 2.0 * np.pi, k, endpoint=False)
            nx = np.cos(ang) * 0.5
        if float(span[1]) > 1e-6:
            ny = (sub_xy[:, 1] - mid[1]) / (0.5 * span[1])
        else:
            ang = np.linspace(0.0, 2.0 * np.pi, k, endpoint=False)
            ny = np.sin(ang) * 0.5
        for pos_idx, m_idx in enumerate(member_indices):
            coords_x[m_idx] = anchor_x + float(nx[pos_idx]) * rx
            coords_y[m_idx] = anchor_y + float(ny[pos_idx]) * ry

    if 1 < n <= 1000:
        idx_arr = np.arange(n, dtype=np.float64)
        fallback_angles = (idx_arr[:, None] + idx_arr[None, :]) * 0.7
        iters = 18 if n <= 600 else 8
        for _ in range(iters):
            dx = coords_x[:, None] - coords_x[None, :]
            dy = coords_y[:, None] - coords_y[None, :]
            ellip_dist = np.hypot(dx / 1.35, dy / 0.85)
            np.fill_diagonal(ellip_dist, 999.0)
            mask = ellip_dist < 8.5
            if not np.any(mask):
                break
            push = np.where(mask, (8.5 - ellip_dist) * 0.20, 0.0)
            angles = np.arctan2(dy / 0.85, dx / 1.35)
            same_pos = (ellip_dist <= 1e-4) & mask
            if np.any(same_pos):
                sign = np.where(idx_arr[:, None] > idx_arr[None, :], 0.0, np.pi)
                angles = np.where(same_pos, fallback_angles + sign, angles)
            step_x = np.clip(np.sum(np.cos(angles) * push, axis=1) * 1.35, -6.0, 6.0)
            step_y = np.clip(np.sum(np.sin(angles) * push, axis=1) * 0.85, -6.0, 6.0)
            coords_x = np.clip(coords_x + step_x, 10.0, 90.0).astype(np.float32)
            coords_y = np.clip(coords_y + step_y, 14.0, 88.0).astype(np.float32)
    elif n > 1000:
        # Per-shelf local jitter keeps coordinates bounded without O(N^2) matrix allocation
        for _, _, _, _, member_indices in ordered_groups:
            k = len(member_indices)
            if k <= 1 or k > 800:
                continue
            m_arr = np.asarray(member_indices, dtype=np.intp)
            sub_x = coords_x[m_arr]
            sub_y = coords_y[m_arr]
            dx = sub_x[:, None] - sub_x[None, :]
            dy = sub_y[:, None] - sub_y[None, :]
            ellip_dist = np.hypot(dx / 1.35, dy / 0.85)
            np.fill_diagonal(ellip_dist, 999.0)
            mask = ellip_dist < 4.5
            if np.any(mask):
                push = np.where(mask, (4.5 - ellip_dist) * 0.12, 0.0)
                angles = np.arctan2(dy / 0.85, dx / 1.35)
                step_x = np.clip(np.sum(np.cos(angles) * push, axis=1) * 1.35, -3.0, 3.0)
                step_y = np.clip(np.sum(np.sin(angles) * push, axis=1) * 0.85, -3.0, 3.0)
                coords_x[m_arr] = np.clip(sub_x + step_x, 10.0, 90.0).astype(np.float32)
                coords_y[m_arr] = np.clip(sub_y + step_y, 14.0, 88.0).astype(np.float32)

    shelves: list[DiscoveredShelf] = []
    file_updates: dict[str, dict[str, Any]] = {}

    for seq_idx, (cat_key, s_id, s_name, s_desc, member_indices) in enumerate(ordered_groups, start=1):
        num_str = f"{seq_idx:02d}"
        c_counts = Counter(int(labels[i]) for i in member_indices)
        rep_cid = c_counts.most_common(1)[0][0] if c_counts else -1
        cx = float(np.mean([coords_x[i] for i in member_indices]))
        cy = float(np.mean([coords_y[i] for i in member_indices]))
        f_ids = [str(files_meta[i]["id"]) for i in member_indices]
        shelves.append(
            DiscoveredShelf(
                shelf_id=s_id,
                cluster_id=rep_cid,
                num=num_str,
                name=s_name,
                desc=s_desc,
                file_ids=f_ids,
                centroid_x=round(cx, 1),
                centroid_y=round(cy, 1),
            )
        )
        for i in member_indices:
            fid = str(files_meta[i]["id"])
            cid = int(labels[i])
            sub_label = derive_specific_sub_shelf(files_meta[i], cat_key)
            if sub_label in _GENERIC_SUB_SHELVES and cid in cluster_topics:
                sub_label = cluster_topics[cid]
            file_updates[fid] = {
                "cluster_id": cid,
                "shelf_id": s_id,
                "shelf_name": s_name,
                "sub_shelf": sub_label,
                "map_x": round(float(coords_x[i]), 1),
                "map_y": round(float(coords_y[i]), 1),
            }

    return shelves, file_updates, sil_score
