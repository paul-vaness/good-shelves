"""
Unsupervised Topic Shelf Discovery via UMAP (512-d -> 5-d & 2-d) + HDBSCAN + TF-IDF Keyphrase Naming.
Computes real 2D Meaning Map coordinates, HDBSCAN cluster assignments, Quiet Tray outliers (cluster_id = -1),
and real Silhouette Score.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional

import hdbscan
import numpy as np
from sklearn.decomposition import PCA
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sklearn.metrics.pairwise import cosine_distances


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


def _refine_umap_layout(Y_init: np.ndarray, W: np.ndarray, epochs: int = 36) -> np.ndarray:
    """Run vectorized attractive/repulsive fuzzy cross-entropy force refinement."""
    Y = Y_init.astype(np.float64, copy=True)
    std = np.std(Y, axis=0, keepdims=True)
    Y = Y / np.where(std > 1e-8, std, 1.0)
    one_minus_W = 1.0 - W

    for epoch in range(epochs):
        lr = 0.36 * (1.0 - epoch / float(epochs))
        diff = Y[:, None, :] - Y[None, :, :]  # (N, N, d)
        sq_dist = np.sum(diff * diff, axis=-1) + 1e-4
        inv_1p = 1.0 / (1.0 + sq_dist)
        attr_coeff = (2.0 * W * inv_1p)[:, :, None]
        rep_coeff = (0.08 * one_minus_W / (0.1 + sq_dist) * inv_1p)[:, :, None]
        grad = np.sum((attr_coeff - rep_coeff) * diff, axis=1)
        Y -= lr * np.clip(grad, -4.0, 4.0)

    return Y.astype(np.float32)


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

    k = min(n_neighbors, max(2, n_samples - 1))
    dist_mat = cosine_distances(vectors).astype(np.float64)
    np.fill_diagonal(dist_mat, 0.0)

    # Step 1: Smooth kNN distances (rho_i and sigma_i via binary search)
    A = np.zeros((n_samples, n_samples), dtype=np.float64)
    target = np.log2(k + 1)
    for i in range(n_samples):
        row = dist_mat[i]
        nn_idx = np.argsort(row)[1 : k + 1]
        nn_dists = row[nn_idx]
        nonzero = nn_dists[nn_dists > 0]
        rho = float(nonzero[0]) if len(nonzero) > 0 else 0.0

        lo, hi = 1e-5, 10.0
        sigma = 1.0
        for _ in range(24):
            mid = 0.5 * (lo + hi)
            vals = np.exp(-np.maximum(0.0, nn_dists - rho) / mid)
            psum = float(np.sum(vals))
            if abs(psum - target) < 1e-3:
                sigma = mid
                break
            if psum > target:
                hi = mid
            else:
                lo = mid
            sigma = mid

        A[i, nn_idx] = np.exp(-np.maximum(0.0, nn_dists - rho) / max(sigma, 1e-5))

    # Step 2: Fuzzy set union W = A + A^T - A o A^T
    W = A + A.T - (A * A.T)
    np.fill_diagonal(W, 0.0)

    # Step 3: Normalized Laplacian spectral initialization (computed once for both 5-d and 2-d)
    deg = np.sum(W, axis=1)
    deg_inv_sqrt = np.where(deg > 1e-9, 1.0 / np.sqrt(deg), 0.0)
    L_sym = np.eye(n_samples) - (deg_inv_sqrt[:, None] * W * deg_inv_sqrt[None, :])

    max_c = max(n_comp_high, 2)
    try:
        _, vecs = np.linalg.eigh(L_sym)
        start_idx = 1 if n_samples > max_c + 1 else 0
        Y_spec = vecs[:, start_idx : start_idx + max_c].astype(np.float64)
        if Y_spec.shape[1] < max_c:
            Y_spec = PCA(n_components=max_c, random_state=random_state).fit_transform(vectors).astype(np.float64)
    except Exception:
        Y_spec = PCA(n_components=max_c, random_state=random_state).fit_transform(vectors).astype(np.float64)

    emb_5d = _refine_umap_layout(Y_spec[:, :n_comp_high], W, epochs=32)
    emb_2d = _refine_umap_layout(Y_spec[:, :2], W, epochs=36)
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


_LABEL_BANNED = {
    "file", "files", "pdf", "png", "jpg", "jpeg", "webp", "txt", "doc", "docx",
    "page", "pages", "html", "css", "div", "span", "style", "font", "border",
    "color", "width", "height", "padding", "margin", "flex", "display", "button",
    "readonly", "class", "none", "true", "false", "world", "hello", "indexed",
    "metadata", "script", "code", "document", "documents", "data", "type",
    "value", "name", "http", "https", "www", "com", "org", "src", "public",
    "import", "export", "const", "function", "return", "string", "number",
    "object", "array", "online", "program", "week", "chapter", "version",
    "master", "plan", "final", "draft", "copy", "untitled", "test", "index",
    "main", "component", "def", "self", "print", "async", "await", "from",
}


def _plausible_word(word: str) -> bool:
    """Reject OCR gibberish such as 'oencesn' while keeping real technical words."""
    if len(word) < 3 or len(word) > 24:
        return False
    if not re.fullmatch(r"[a-z][a-z0-9'-]*", word):
        return False
    if not re.search(r"[aeiouy]", word):
        return False
    if re.search(r"[bcdfghjklmnpqrstvwxz]{4,}", word):
        return False
    return True


def _derive_shelf_label_and_desc(files_meta: list[dict[str, Any]], cluster_idx: int) -> tuple[str, str, str]:
    """Generate a shelf title from weighted TF-IDF keyphrases shared across cluster members."""
    corpus_texts = []
    for m in files_meta:
        meta = f"{m.get('title', '')} {m.get('name', '').replace('_', ' ').replace('-', ' ')}"
        body = f"{m.get('extracted_text', '')[:700]} {m.get('ocr_text', '')[:300]}"
        corpus_texts.append(f"{meta} {meta} {meta} {body}".lower())

    try:
        vec = TfidfVectorizer(
            stop_words="english",
            max_features=160,
            token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9'-]{2,23}\b",
            ngram_range=(1, 2),
            sublinear_tf=True,
            min_df=1,
        )
        tfidf = vec.fit_transform(corpus_texts)
        scores = np.asarray(tfidf.mean(axis=0)).ravel()
        terms = vec.get_feature_names_out()
        doc_freq = np.asarray((tfidf > 0).sum(axis=0)).ravel()

        min_shared_df = 2 if len(corpus_texts) >= 3 else 1
        candidates: list[tuple[float, str]] = []
        for require_shared in (True, False):
            if candidates:
                break
            for idx in np.argsort(scores)[::-1]:
                cand = str(terms[idx]).lower().strip()
                words = cand.split()
                if not cand or any(w in _LABEL_BANNED for w in words):
                    continue
                if not all(_plausible_word(w) for w in words):
                    continue
                if require_shared and doc_freq[idx] < min_shared_df:
                    continue
                if len(words) == 2 and len(set(words)) == 1:
                    continue
                # Boost terms shared across more files in the cluster and natural 2-word phrases
                df_weight = 1.0 + 0.25 * min(int(doc_freq[idx]), 5)
                ngram_weight = 1.35 if len(words) == 2 else 1.0
                candidates.append((float(scores[idx]) * df_weight * ngram_weight, cand))
                if len(candidates) >= 24:
                    break

        candidates.sort(key=lambda x: x[0], reverse=True)
        top_cand = candidates[0][1] if candidates else ""
        if top_cand and len(top_cand.split()) == 1:
            top_score = candidates[0][0]
            for score, cand in candidates[1:]:
                words = cand.split()
                if len(words) == 2 and top_cand in words and score >= top_score * 0.45:
                    top_cand = cand
                    break

        picked: list[str] = [top_cand] if top_cand else []
        for _, cand in candidates:
            if cand == top_cand:
                continue
            words = cand.split()
            if any(set(words) & set(p.split()) for p in picked):
                continue
            picked.append(cand)
            if len(picked) >= 3:
                break

        if picked:
            top_words = picked[0].split()
            if len(top_words) >= 2:
                name = picked[0]
            elif len(picked) >= 2:
                name = f"{picked[0]} & {picked[1]}"
            else:
                name = f"{picked[0]} files"
            name = name[:40].strip(" &")
            slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
            kw_summary = ", ".join(picked[:3])
            desc = f"{len(files_meta)} files · {kw_summary}"
            return f"shelf-{cluster_idx}-{slug}", name, desc
    except Exception:
        pass

    mod_counts = Counter(m.get("modality", "document") for m in files_meta)
    top_mod = mod_counts.most_common(1)[0][0] if mod_counts else "topic"
    return f"shelf-{cluster_idx}-{top_mod}", f"{top_mod} group {cluster_idx + 1}", f"{len(files_meta)} {top_mod} files."


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

    # 1. Compute 5-d UMAP embedding for HDBSCAN and 2-d UMAP embedding for Meaning Map in a single shared pass
    n_comp_5 = min(5, max(2, n - 1))
    emb_5d, emb_2d = umap_project_pair(
        vectors, n_comp_high=n_comp_5, n_neighbors=min(5, max(2, n - 1)), random_state=42
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

    # 3. Cosine-coherence outlier pruning: remove weak cluster members & dissolve singleton clusters
    sim_matrix = np.clip(vectors @ vectors.T, -1.0, 1.0)
    np.fill_diagonal(sim_matrix, 0.0)

    labels = np.full(n, -1, dtype=np.int32)
    for i in range(n):
        c_id = int(raw_labels[i])
        if c_id == -1:
            continue
        peers = [j for j in range(n) if j != i and int(raw_labels[j]) == c_id]
        if peers and float(np.max(sim_matrix[i, peers])) >= 0.35:
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

    # 5. Normalize 2D UMAP coordinates to [14%, 86%] canvas space with gentle collision separation
    if n == 1:
        coords_x = np.array([50.0], dtype=np.float32)
        coords_y = np.array([50.0], dtype=np.float32)
    else:
        min_xy = np.min(emb_2d, axis=0)
        max_xy = np.max(emb_2d, axis=0)
        span_xy = np.where((max_xy - min_xy) > 1e-6, max_xy - min_xy, 1.0)
        norm_xy = (emb_2d - min_xy) / span_xy
        coords_x = 16.0 + norm_xy[:, 0] * 68.0
        coords_y = 18.0 + norm_xy[:, 1] * 64.0

        for _ in range(14):
            for i in range(n):
                for j in range(i + 1, n):
                    dx = float(coords_x[i] - coords_x[j])
                    dy = float(coords_y[i] - coords_y[j])
                    dist = np.hypot(dx, dy)
                    if dist < 9.0:
                        push = (9.0 - dist) * 0.35
                        angle = np.arctan2(dy, dx) if dist > 1e-4 else (i + j) * 0.7
                        coords_x[i] = float(np.clip(coords_x[i] + np.cos(angle) * push, 12.0, 88.0))
                        coords_y[i] = float(np.clip(coords_y[i] + np.sin(angle) * push, 14.0, 86.0))
                        coords_x[j] = float(np.clip(coords_x[j] - np.cos(angle) * push, 12.0, 88.0))
                        coords_y[j] = float(np.clip(coords_y[j] - np.sin(angle) * push, 14.0, 86.0))

    # 6. Group clusters and build DiscoveredShelf objects
    cluster_groups: dict[int, list[int]] = {}
    for idx, c_id in enumerate(labels):
        cluster_groups.setdefault(int(c_id), []).append(idx)

    provisional_shelves: list[tuple[int, str, str, str, list[int]]] = []
    used_ids: set[str] = set()
    merged_any = False

    for c_id, member_indices in sorted(cluster_groups.items(), key=lambda kv: kv[0]):
        if c_id == -1:
            continue
        members_meta = [files_meta[i] for i in member_indices]
        s_id, s_name, s_desc = _derive_shelf_label_and_desc(members_meta, c_id)
        if s_id in used_ids:
            for idx_p, (p_cid, p_sid, p_sname, _, p_members) in enumerate(provisional_shelves):
                if p_sid == s_id:
                    p_members.extend(member_indices)
                    for m_i in member_indices:
                        labels[m_i] = p_cid
                    _, _, new_desc = _derive_shelf_label_and_desc([files_meta[i] for i in p_members], p_cid)
                    provisional_shelves[idx_p] = (p_cid, p_sid, p_sname, new_desc, p_members)
                    merged_any = True
                    break
        else:
            used_ids.add(s_id)
            provisional_shelves.append((c_id, s_id, s_name, s_desc, member_indices))

    if merged_any:
        non_noise_mask = labels != -1
        unique_clusters = set(int(x) for x in labels[non_noise_mask])
        if len(unique_clusters) >= 2 and int(np.sum(non_noise_mask)) > len(unique_clusters):
            try:
                sil_score = round(
                    float(
                        silhouette_score(
                            emb_5d[non_noise_mask],
                            labels[non_noise_mask],
                            metric="euclidean",
                        )
                    ),
                    3,
                )
            except Exception:
                sil_score = 0.0
        else:
            sil_score = 0.0

    provisional_shelves.sort(key=lambda item: -len(item[4]))

    shelves: list[DiscoveredShelf] = []
    file_updates: dict[str, dict[str, Any]] = {}

    for seq_idx, (c_id, s_id, s_name, s_desc, member_indices) in enumerate(provisional_shelves, start=1):
        num_str = f"{seq_idx:02d}"
        full_shelf_name = f"{num_str} {s_name}"
        cx = float(np.mean([coords_x[i] for i in member_indices]))
        cy = float(np.mean([coords_y[i] for i in member_indices]))
        f_ids = [str(files_meta[i]["id"]) for i in member_indices]
        shelves.append(
            DiscoveredShelf(
                shelf_id=s_id,
                cluster_id=c_id,
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
            file_updates[fid] = {
                "cluster_id": c_id,
                "shelf_id": s_id,
                "shelf_name": full_shelf_name,
                "map_x": round(float(coords_x[i]), 1),
                "map_y": round(float(coords_y[i]), 1),
            }

    # Always include Uncategorized shelf for outliers (cluster_id = -1)
    tray_indices = cluster_groups.get(-1, [])
    tray_num = f"{len(shelves) + 1:02d}"
    tray_full_name = f"{tray_num} uncategorized"
    tray_cx = float(np.mean([coords_x[i] for i in tray_indices])) if tray_indices else 50.0
    tray_cy = float(np.mean([coords_y[i] for i in tray_indices])) if tray_indices else 52.0
    shelves.append(
        DiscoveredShelf(
            shelf_id="tray",
            cluster_id=-1,
            num=tray_num,
            name="uncategorized",
            desc="files that did not fit into a shared topic group.",
            file_ids=[str(files_meta[i]["id"]) for i in tray_indices],
            centroid_x=round(tray_cx, 1),
            centroid_y=round(tray_cy, 1),
        )
    )
    for i in tray_indices:
        fid = str(files_meta[i]["id"])
        file_updates[fid] = {
            "cluster_id": -1,
            "shelf_id": "tray",
            "shelf_name": tray_full_name,
            "map_x": round(float(coords_x[i]), 1),
            "map_y": round(float(coords_y[i]), 1),
        }

    return shelves, file_updates, sil_score
