"""
Semantic Near-Duplicate Discovery & Real Unified Text/Image Diffing Engine for Good Shelves.
Detects lookalike pairs via cosine similarity (>= 0.92) or perceptual dHash + OCR overlap,
and computes real line/paragraph diffs (difflib) and image resolution/crop/OCR comparisons.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

import numpy as np


def _hamming_distance_hex(h1: str, h2: str) -> int:
    if not h1 or not h2 or len(h1) != len(h2):
        return 64
    try:
        v1 = int(h1, 16)
        v2 = int(h2, 16)
        return (v1 ^ v2).bit_count()
    except ValueError:
        return 64


def _tokenize_set(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-Z0-9]{3,}", text.lower())}


def _set_jaccard(s1: set[str], s2: set[str]) -> float:
    if not s1 or not s2:
        return 0.0
    inter = len(s1 & s2)
    if inter == 0:
        return 0.0
    return inter / float(len(s1) + len(s2) - inter)


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{max(1, round(size_bytes / 1024.0))} KB"


def _pick_keep_and_copy(f_a: dict[str, Any], f_b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Decide which file in a near-duplicate pair is the primary version to keep
    and which is the redundant copy to archive.
    """
    name_a = str(f_a.get("name", "")).lower()
    name_b = str(f_b.get("name", "")).lower()

    redundant_pat = re.compile(r"(\(\d+\)|copy|cropped|compressed|old|earlier|draft|backup)", re.IGNORECASE)
    a_is_red = bool(redundant_pat.search(name_a)) and "revised" not in name_a
    b_is_red = bool(redundant_pat.search(name_b)) and "revised" not in name_b
    if b_is_red and not a_is_red:
        return f_a, f_b
    if a_is_red and not b_is_red:
        return f_b, f_a

    if f_a.get("modality") == "image" and f_b.get("modality") == "image":
        pixels_a = int(f_a.get("width", 0)) * int(f_a.get("height", 0))
        pixels_b = int(f_b.get("width", 0)) * int(f_b.get("height", 0))
        if pixels_a != pixels_b:
            return (f_a, f_b) if pixels_a > pixels_b else (f_b, f_a)
        if f_a.get("ext") == "png" and f_b.get("ext") in {"jpg", "jpeg"}:
            return f_a, f_b
        if f_b.get("ext") == "png" and f_a.get("ext") in {"jpg", "jpeg"}:
            return f_b, f_a

    if float(f_a.get("mtime", 0)) >= float(f_b.get("mtime", 0)):
        return f_a, f_b
    return f_b, f_a


def compute_real_pair_diff(keep: dict[str, Any], copy: dict[str, Any], sim_pct: int) -> tuple[str, str, list[dict[str, str]], list[str]]:
    """
    Compute real structured differences and unified diff lines between `keep` and `copy`.
    Returns (badge, description, structured_diffs, unified_diff_lines).
    """
    mod_k = keep.get("modality", "document")
    ext_k = str(keep.get("ext", "")).lower()
    ext_c = str(copy.get("ext", "")).lower()
    size_k = _format_size(int(keep.get("size_bytes", 0)))
    size_c = _format_size(int(copy.get("size_bytes", 0)))

    diffs: list[dict[str, str]] = []
    unified_lines: list[str] = []

    if mod_k == "image" or copy.get("modality") == "image":
        wk, hk = int(keep.get("width", 0)), int(keep.get("height", 0))
        wc, hc = int(copy.get("width", 0)), int(copy.get("height", 0))
        area_k = max(1, wk * hk)
        area_c = max(1, wc * hc)
        crop_pct = max(0, round((1.0 - (min(area_k, area_c) / float(max(area_k, area_c)))) * 100))

        ocr_k = str(keep.get("ocr_text") or keep.get("extracted_text") or "").strip()
        ocr_c = str(copy.get("ocr_text") or copy.get("extracted_text") or "").strip()

        if ext_k != ext_c or crop_pct > 2:
            badge = f"{sim_pct}% similar · cropped & re-saved as {ext_c.upper()}"
            desc = (
                f"Both images depict the same visual content ({keep.get('title')}). "
                f"The second copy ({copy.get('name')}) has {crop_pct}% smaller framing and is stored as {ext_c.upper()}."
            )
        else:
            badge = f"{sim_pct}% similar · near-identical image scan"
            desc = f"Both image files contain matching visual features and OCR text across {keep.get('folder_short')} and {copy.get('folder_short')}."

        diffs.append(
            {
                "label": "Image resolution & framing",
                "keep": f"Full {wk}×{hk}px canvas ({ocr_k[:64] or 'complete header intact'})",
                "copy": f"{wc}×{hc}px ({'cropped ' + str(crop_pct) + '% from edges' if crop_pct > 0 else 'identical dimensions'})",
            }
        )
        diffs.append(
            {
                "label": "Format & compression",
                "keep": f"{ext_k.upper()} format ({size_k})",
                "copy": f"{ext_c.upper()} format ({size_c})",
            }
        )
        if ocr_k or ocr_c:
            u_diff = list(
                difflib.unified_diff(
                    [s.strip() for s in re.split(r"[·\n]", ocr_c) if s.strip()][:40],
                    [s.strip() for s in re.split(r"[·\n]", ocr_k) if s.strip()][:40],
                    fromfile=str(copy.get("name", "copy")),
                    tofile=str(keep.get("name", "keep")),
                    lineterm="",
                )
            )
            unified_lines = u_diff[:24]
        return badge, desc, diffs, unified_lines

    sha_k = str(keep.get("sha256", ""))
    sha_c = str(copy.get("sha256", ""))
    text_k = str(keep.get("extracted_text", "")).strip()
    text_c = str(copy.get("extracted_text", "")).strip()

    lines_k = [ln.strip() for ln in text_k.splitlines() if ln.strip()][:100]
    lines_c = [ln.strip() for ln in text_c.splitlines() if ln.strip()][:100]

    u_diff = list(
        difflib.unified_diff(
            lines_c,
            lines_k,
            fromfile=str(copy.get("name", "copy")),
            tofile=str(keep.get("name", "keep")),
            lineterm="",
        )
    )
    unified_lines = u_diff[:30]

    if sha_k == sha_c or (text_k and text_k == text_c):
        badge = f"{sim_pct}% · exact duplicate" if sha_k == sha_c else f"{max(99, sim_pct)}% · identical text"
        desc = (
            f"Identical content ({keep.get('page_count', 1)} pages) saved as "
            f"“{keep.get('name')}” and “{copy.get('name')}”."
        )
        diffs.append(
            {
                "label": "Document pages & content",
                "keep": f"{keep.get('page_count', 1)} pages · SHA-256 {sha_k[:12]}… ({size_k})",
                "copy": f"{copy.get('page_count', 1)} pages · SHA-256 {sha_c[:12]}… ({size_c})",
            }
        )
        diffs.append(
            {
                "label": "File timestamp & location",
                "keep": f"Primary copy at {keep.get('folder_short')}/{keep.get('name')} ({keep.get('display_date')})",
                "copy": f"Redundant copy at {copy.get('folder_short')}/{copy.get('name')} ({copy.get('display_date')})",
            }
        )
        return badge, desc, diffs, unified_lines

    matcher = difflib.SequenceMatcher(None, lines_c, lines_k)
    changed_blocks = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed_blocks += 1
        old_seg = " ".join(lines_c[i1:i2])[:110] or "(none)"
        new_seg = " ".join(lines_k[j1:j2])[:110] or "(removed)"
        if len(diffs) < 3:
            diffs.append(
                {
                    "label": f"Revision hunk #{changed_blocks} ({tag})",
                    "keep": new_seg,
                    "copy": old_seg,
                }
            )

    if not diffs:
        diffs.append(
            {
                "label": "Content comparison",
                "keep": text_k[:100],
                "copy": text_c[:100],
            }
        )

    badge = f"{sim_pct}% · {max(1, changed_blocks)} passage{'s' if changed_blocks != 1 else ''} differ"
    desc = f"{max(1, changed_blocks)} section(s) differ between these revisions of “{keep.get('title')}”."
    return badge, desc, diffs, unified_lines


def find_lookalike_pairs(
    files_meta: list[dict[str, Any]],
    vectors: np.ndarray,
    resolved_pair_ids: dict[str, dict[str, Any]],
    threshold: float = 0.92,
) -> list[dict[str, Any]]:
    """
    Discover near-duplicate file pairs across all watched folders where:
      - Exact SHA-256 match, OR
      - Cosine similarity >= threshold (default 0.92) with meaningful token/visual overlap, OR
      - Image perceptual dHash Hamming distance <= 10, OR
      - High token Jaccard overlap (>= 0.55) on same modality & specimen.
    """
    n = len(files_meta)
    if n < 2:
        return list(resolved_pair_ids.values())

    sim_mat = np.clip(vectors @ vectors.T, -1.0, 1.0)
    token_sets = [
        _tokenize_set(f"{f.get('title', '')} {f.get('extracted_text', '')[:1500]} {f.get('ocr_text', '')}")
        for f in files_meta
    ]

    pairs: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    candidates: list[tuple[float, int, int, bool]] = []
    seen_pairs: set[tuple[int, int]] = set()

    # 1. Exact SHA-256 duplicates in O(N)
    sha_groups: dict[str, list[int]] = {}
    image_indices: list[int] = []
    for idx, f in enumerate(files_meta):
        sha = str(f.get("sha256", ""))
        if sha:
            sha_groups.setdefault(sha, []).append(idx)
        if f.get("modality") == "image":
            image_indices.append(idx)

    for idxs in sha_groups.values():
        if len(idxs) > 1:
            base = idxs[0]
            for other in idxs[1:]:
                pair_key = (min(base, other), max(base, other))
                seen_pairs.add(pair_key)
                candidates.append((1.0, pair_key[0], pair_key[1], True))

    # 2. Vectorized cosine similarity candidate pairs (>= 0.72) + image dHash pairs
    upper_mask = np.triu(sim_mat, k=1) >= 0.72
    cand_indices: set[tuple[int, int]] = {
        (int(i), int(j)) for i, j in np.argwhere(upper_mask)
    }
    for pos_a in range(len(image_indices)):
        ia = image_indices[pos_a]
        for pos_b in range(pos_a + 1, len(image_indices)):
            ib = image_indices[pos_b]
            cand_indices.add((min(ia, ib), max(ia, ib)))

    for i, j in cand_indices:
        if (i, j) in seen_pairs:
            continue
        fi = files_meta[i]
        fj = files_meta[j]
        cos_sim = float(sim_mat[i, j])

        dhash_sim = 0.0
        if fi.get("modality") == "image" and fj.get("modality") == "image":
            h_dist = _hamming_distance_hex(str(fi.get("dhash", "")), str(fj.get("dhash", "")))
            if h_dist <= 14:
                dhash_sim = 1.0 - (h_dist / 64.0)

        jaccard = _set_jaccard(token_sets[i], token_sets[j])

        effective_sim = cos_sim if (jaccard >= 0.28 or dhash_sim >= 0.78) else (cos_sim * 0.94)
        if dhash_sim >= 0.80 and jaccard >= 0.25:
            effective_sim = max(effective_sim, dhash_sim * 0.97)
        if jaccard >= 0.52 and fi.get("modality") == fj.get("modality"):
            effective_sim = max(effective_sim, min(0.98, 0.88 + jaccard * 0.14))

        if effective_sim >= threshold:
            candidates.append((effective_sim, i, j, False))

    candidates.sort(key=lambda x: x[0], reverse=True)

    for eff_sim, i, j, is_exact in candidates:
        fi = files_meta[i]
        fj = files_meta[j]
        id_i = str(fi["id"])
        id_j = str(fj["id"])
        if id_i in seen_files or id_j in seen_files:
            continue
        seen_files.add(id_i)
        seen_files.add(id_j)

        keep, copy = _pick_keep_and_copy(fi, fj)
        pair_id = f"dup_{keep['id']}_{copy['id']}"
        sim_pct = 100 if is_exact else min(99, max(92, int(round(eff_sim * 100))))

        badge, desc, diffs, unified_lines = compute_real_pair_diff(keep, copy, sim_pct)
        saved_bytes = int(copy.get("size_bytes", 0))
        saved_mb = round(max(0.01, saved_bytes / (1024.0 * 1024.0)), 2)

        keep_size_str = _format_size(int(keep.get("size_bytes", 0)))
        copy_size_str = _format_size(int(copy.get("size_bytes", 0)))

        pairs.append(
            {
                "id": pair_id,
                "title": keep.get("title", keep.get("name", "lookalike pair")),
                "similarity": round(eff_sim, 4),
                "similarity_pct": sim_pct,
                "badge": badge,
                "desc": desc,
                "keep": {
                    "id": keep["id"],
                    "name": keep["name"],
                    "path": keep["path"],
                    "meta": f"{keep.get('folder_short', '~/Files')} · {keep.get('ext', '').upper()} ({keep.get('display_date', '')}) · {keep_size_str}",
                },
                "copy": {
                    "id": copy["id"],
                    "name": copy["name"],
                    "path": copy["path"],
                    "meta": f"{copy.get('folder_short', '~/Files')} · {copy.get('ext', '').upper()} ({copy.get('display_date', '')}) · {copy_size_str}",
                },
                "saved_bytes": saved_bytes,
                "saved_mb": saved_mb,
                "resolved": False,
                "archived_path": None,
                "diffs": diffs,
                "unified_diff": unified_lines,
            }
        )

    for r_id, r_pair in resolved_pair_ids.items():
        if not any(p["id"] == r_id for p in pairs):
            pairs.append(r_pair)

    return pairs
