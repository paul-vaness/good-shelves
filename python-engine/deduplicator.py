"""
Semantic Near-Duplicate Discovery & Real Unified Text/Image Diffing Engine for Good Shelves.
Detects lookalike pairs via cosine similarity (>= 0.92) or perceptual dHash + OCR overlap,
and computes real line/paragraph diffs (difflib) and image resolution/crop/OCR comparisons.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
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


_REDUNDANT_NAME_PAT = re.compile(
    r"(\(\d+\)|copy|cropped|compressed|old|earlier|draft|backup|^dup_)",
    re.IGNORECASE,
)

_UNORGANIZED_FOLDER_PAT = re.compile(r"(downloads|desktop|tmp|temp|incoming)", re.IGNORECASE)


def _compute_keep_signals(f: dict[str, Any], other: dict[str, Any]) -> tuple[float, list[str]]:
    """
    Composite Keep-Score for deciding the canonical copy in a duplicate pair:
      Pinned (+100) > Clean filename (+30) > Quality/resolution/pages (+25)
      > Organized folder (+20) > Tagged/manual shelf (+10) > Recency (+5).
    """
    score = 0.0
    reasons: list[str] = []

    if bool(f.get("pinned")):
        score += 100.0
        reasons.append("pinned")

    name_low = str(f.get("name", "")).lower()
    is_redundant = bool(_REDUNDANT_NAME_PAT.search(name_low)) and "revised" not in name_low
    if not is_redundant:
        score += 30.0
        reasons.append("clean filename")

    # Quality / resolution / pages / lossless format
    mod = str(f.get("modality") or "")
    other_mod = str(other.get("modality") or "")
    if mod == "image" and other_mod == "image":
        px_a = int(f.get("width", 0)) * int(f.get("height", 0))
        px_b = int(other.get("width", 0)) * int(other.get("height", 0))
        if px_a > px_b:
            score += 25.0
            reasons.append("higher resolution")
        elif px_a == px_b and str(f.get("ext", "")).lower() == "png" and str(other.get("ext", "")).lower() in {"jpg", "jpeg"}:
            score += 25.0
            reasons.append("lossless PNG")
    else:
        pages_a = int(f.get("page_count", f.get("pageCount", 0)) or 0)
        pages_b = int(other.get("page_count", other.get("pageCount", 0)) or 0)
        size_a = int(f.get("size_bytes", f.get("sizeBytes", 0)) or 0)
        size_b = int(other.get("size_bytes", other.get("sizeBytes", 0)) or 0)
        if pages_a > pages_b:
            score += 25.0
            reasons.append(f"more pages ({pages_a})")
        elif size_a > size_b:
            score += 12.0
            reasons.append("larger file")

    folder_str = str(f.get("folder_short") or f.get("folder") or f.get("path") or "")
    if folder_str and not _UNORGANIZED_FOLDER_PAT.search(folder_str):
        score += 20.0
        reasons.append("organized folder")

    tags_val = f.get("tags") or []
    has_tags = bool(tags_val) if isinstance(tags_val, list) else (str(f.get("tags_json") or "").strip() not in {"", "[]"})
    has_manual_shelf = bool(str(f.get("user_shelf_id") or "").strip())
    if has_tags or has_manual_shelf:
        score += 10.0
        reasons.append("tagged/shelved")

    mtime_a = float(f.get("mtime", 0) or 0)
    mtime_b = float(other.get("mtime", 0) or 0)
    if mtime_a > mtime_b:
        score += 5.0
        reasons.append("newer")

    return score, reasons


def pick_keep_and_copy(
    f_a: dict[str, Any],
    f_b: dict[str, Any],
    keep_strategy: str = "best",
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """
    Decide which file in a duplicate/near-duplicate pair to keep vs archive,
    returning `(keep, copy, keep_reason)`.
    Supports `keep_strategy`: `'best'` (default composite score), `'newest'`,
    `'oldest'`, `'largest'`, or `'folder:<name>'`.
    """
    strat = (keep_strategy or "best").strip().lower()

    if strat == "newest":
        if float(f_a.get("mtime", 0) or 0) >= float(f_b.get("mtime", 0) or 0):
            return f_a, f_b, "newest copy (strategy)"
        return f_b, f_a, "newest copy (strategy)"

    if strat == "oldest":
        if float(f_a.get("mtime", 0) or 0) <= float(f_b.get("mtime", 0) or 0):
            return f_a, f_b, "oldest original (strategy)"
        return f_b, f_a, "oldest original (strategy)"

    if strat == "largest":
        sa = int(f_a.get("size_bytes", f_a.get("sizeBytes", 0)) or 0)
        sb = int(f_b.get("size_bytes", f_b.get("sizeBytes", 0)) or 0)
        if sa >= sb:
            return f_a, f_b, "largest file (strategy)"
        return f_b, f_a, "largest file (strategy)"

    if strat.startswith("folder:"):
        pref_folder = strat[7:].strip()
        fa_fld = str(f_a.get("folder_short") or f_a.get("folder") or f_a.get("path") or "").lower()
        fb_fld = str(f_b.get("folder_short") or f_b.get("folder") or f_b.get("path") or "").lower()
        a_in = pref_folder in fa_fld
        b_in = pref_folder in fb_fld
        if a_in and not b_in:
            return f_a, f_b, f"inside preferred folder {pref_folder}"
        if b_in and not a_in:
            return f_b, f_a, f"inside preferred folder {pref_folder}"

    score_a, reasons_a = _compute_keep_signals(f_a, f_b)
    score_b, reasons_b = _compute_keep_signals(f_b, f_a)

    if score_a > score_b:
        diff_reasons = [r for r in reasons_a if r not in reasons_b] or reasons_a
        return f_a, f_b, ", ".join(diff_reasons[:3]) or "higher keep score"
    if score_b > score_a:
        diff_reasons = [r for r in reasons_b if r not in reasons_a] or reasons_b
        return f_b, f_a, ", ".join(diff_reasons[:3]) or "higher keep score"

    if float(f_a.get("mtime", 0) or 0) >= float(f_b.get("mtime", 0) or 0):
        return f_a, f_b, ", ".join(reasons_a[:2]) or "newer copy"
    return f_b, f_a, ", ".join(reasons_b[:2]) or "newer copy"


def _pick_keep_and_copy(f_a: dict[str, Any], f_b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Backward-compatible wrapper returning `(keep, copy)` using composite keep score."""
    keep, copy, _ = pick_keep_and_copy(f_a, f_b, keep_strategy="best")
    return keep, copy


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
    swapped_pair_ids: set[str] | None = None,
    keep_strategy: str = "best",
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

    v32 = vectors.astype(np.float32, copy=False)
    token_sets = [
        _tokenize_set(f"{f.get('title', '')} {f.get('extracted_text', '')[:1500]} {f.get('ocr_text', '')}")
        for f in files_meta
    ]

    pairs: list[dict[str, Any]] = []
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
            best_idx = idxs[0]
            for cand_idx in idxs[1:]:
                winner, _ = _pick_keep_and_copy(files_meta[best_idx], files_meta[cand_idx])
                if winner is files_meta[cand_idx]:
                    best_idx = cand_idx
            for other in idxs:
                if other == best_idx:
                    continue
                pair_key = (min(best_idx, other), max(best_idx, other))
                seen_pairs.add(pair_key)
                candidates.append((1.0, best_idx, other, True))

    # 2. Chunked cosine similarity candidate discovery (>= 0.72, or >= 0.62 for image-image)
    # avoiding an N x N matrix allocation in RAM on multi-thousand-file libraries.
    is_image_arr = np.array([f.get("modality") == "image" for f in files_meta], dtype=bool)
    cand_cos_map: dict[tuple[int, int], float] = {}
    block_size = 512
    for start in range(0, n, block_size):
        end = min(n, start + block_size)
        blk_sim = np.clip(v32[start:end] @ v32.T, -1.0, 1.0)
        for local_r in range(end - start):
            i = start + local_r
            row_sims = blk_sim[local_r, i + 1 :]
            if row_sims.size == 0:
                continue
            if is_image_arr[i]:
                hit_mask = (row_sims >= 0.72) | ((row_sims >= 0.62) & is_image_arr[i + 1 :])
            else:
                hit_mask = row_sims >= 0.72
            for offset in np.flatnonzero(hit_mask):
                j = i + 1 + int(offset)
                if (i, j) not in seen_pairs:
                    cand_cos_map[(i, j)] = float(row_sims[int(offset)])

    # 3. Vectorized perceptual dHash Hamming distance <= 14 across image pairs
    valid_dhash_idx: list[int] = []
    valid_dhash_vals: list[int] = []
    for img_idx in image_indices:
        dh = str(files_meta[img_idx].get("dhash", ""))
        if len(dh) == 16:
            try:
                valid_dhash_vals.append(int(dh, 16))
                valid_dhash_idx.append(img_idx)
            except ValueError:
                pass

    if len(valid_dhash_vals) >= 2:
        _POPCOUNT_LUT = np.array([bin(x).count("1") for x in range(256)], dtype=np.uint8)
        dh_arr = np.array(valid_dhash_vals, dtype=np.uint64)
        m = len(dh_arr)
        for s in range(0, m, 512):
            e = min(m, s + 512)
            for local_a in range(e - s):
                ia_pos = s + local_a
                if ia_pos + 1 >= m:
                    continue
                xor_vals = dh_arr[ia_pos] ^ dh_arr[ia_pos + 1 :]
                bytes_view = xor_vals.view(np.uint8).reshape(-1, 8)
                h_dists = _POPCOUNT_LUT[bytes_view].sum(axis=1)
                for off in np.flatnonzero(h_dists <= 14):
                    ib_pos = ia_pos + 1 + int(off)
                    i_idx = valid_dhash_idx[ia_pos]
                    j_idx = valid_dhash_idx[ib_pos]
                    pair_key = (min(i_idx, j_idx), max(i_idx, j_idx))
                    if pair_key not in seen_pairs and pair_key not in cand_cos_map:
                        cand_cos_map[pair_key] = float(np.clip(np.dot(v32[pair_key[0]], v32[pair_key[1]]), -1.0, 1.0))

    _SPREADSHEET_EXTS = {"xlsx", "xlsm", "xls", "csv", "tsv"}
    _PRESENTATION_EXTS = {"pptx", "ppt"}

    for (i, j), cos_sim in cand_cos_map.items():
        fi = files_meta[i]
        fj = files_meta[j]

        sha_i = str(fi.get("sha256", ""))
        sha_j = str(fj.get("sha256", ""))
        is_same_sha = bool(sha_i and sha_i == sha_j)

        mod_i = str(fi.get("modality") or "document").lower()
        mod_j = str(fj.get("modality") or "document").lower()
        ext_i = str(fi.get("ext") or fi.get("format") or Path(str(fi.get("name") or "")).suffix.lstrip(".")).lower().lstrip(".")
        ext_j = str(fj.get("ext") or fj.get("format") or Path(str(fj.get("name") or "")).suffix.lstrip(".")).lower().lstrip(".")

        if not is_same_sha:
            # 1. Cross-Modality / Cross-Kind Guard
            if mod_i != mod_j:
                continue
            if (ext_i in _SPREADSHEET_EXTS) != (ext_j in _SPREADSHEET_EXTS):
                continue
            if (ext_i in _PRESENTATION_EXTS) != (ext_j in _PRESENTATION_EXTS):
                continue

        jaccard = _set_jaccard(token_sets[i], token_sets[j])

        if not is_same_sha and mod_i != "image":
            # 2. Minimum Content Overlap Guard for Documents & Code
            if jaccard < 0.38:
                continue

        dhash_sim = 0.0
        if mod_i == "image" and mod_j == "image":
            dh_i = str(fi.get("dhash", ""))
            dh_j = str(fj.get("dhash", ""))
            h_dist = _hamming_distance_hex(dh_i, dh_j)
            # 3. Minimum Visual / OCR Corroboration Guard for Images
            if not is_same_sha and len(dh_i) == 16 and len(dh_j) == 16 and h_dist != 64:
                if h_dist > 18 and jaccard < 0.35:
                    continue
            if h_dist <= 14:
                dhash_sim = 1.0 - (h_dist / 64.0)

        effective_sim = cos_sim if (jaccard >= 0.28 or dhash_sim >= 0.78) else (cos_sim * 0.94)
        if dhash_sim >= 0.80 and jaccard >= 0.25:
            effective_sim = max(effective_sim, dhash_sim * 0.97)
        if jaccard >= 0.52 and mod_i == mod_j:
            effective_sim = max(effective_sim, min(0.98, 0.88 + jaccard * 0.14))

        if effective_sim >= threshold:
            candidates.append((effective_sim, i, j, False))

    candidates.sort(key=lambda x: x[0], reverse=True)

    # Track files already assigned as redundant copies so triplets/quads still pair each
    # extra copy against the canonical kept file without dropping the 3rd+ duplicate.
    seen_copies: set[str] = set()
    for eff_sim, i, j, is_exact in candidates:
        fi = files_meta[i]
        fj = files_meta[j]
        keep, copy, keep_reason = pick_keep_and_copy(fi, fj, keep_strategy=keep_strategy)
        pair_id = f"dup_{keep['id']}_{copy['id']}"
        is_swapped = bool(swapped_pair_ids and pair_id in swapped_pair_ids)
        if is_swapped:
            keep, copy = copy, keep
            keep_reason = "swapped by user"
        keep_id = str(keep["id"])
        copy_id = str(copy["id"])
        if copy_id in seen_copies or keep_id in seen_copies:
            continue
        seen_copies.add(copy_id)
        floor_pct = min(92, int(round(threshold * 100)))
        sim_pct = 100 if is_exact else min(99, max(floor_pct, int(round(eff_sim * 100))))

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
                "isExact": bool(is_exact),
                "keepReason": keep_reason,
                "swapped": is_swapped,
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
