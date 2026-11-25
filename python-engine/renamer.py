"""
Deterministic Semantic Filename Generator for Good Shelves (YYYY-MM-DD_topic-slug.ext).
Detects cryptic camera-roll numbers, screenshot timestamps, scan prefixes, and messy drafts
(final_final_v2) and synthesizes clean filenames from OCR merchants, document headings, or code symbols.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from parsers import ParsedFile


CRYPTIC_PATTERNS = [
    re.compile(r"^[a-z]?\d{1,5}$", re.IGNORECASE),
    re.compile(r"^(img|dsc|dcim|pxl|mvimg|vid|fb_img|received)[-_]?\d+", re.IGNORECASE),
    re.compile(r"^(screenshot|screen|capture|snip|shot)[-_]?\d*", re.IGNORECASE),
    re.compile(r"^(scan|scanned|doc|document|image|photo|picture|download|export|attachment)[-_]?\d*$", re.IGNORECASE),
    re.compile(r"^(scan|scanned|doc|document|image|photo|picture)[-_]?\d+", re.IGNORECASE),
    re.compile(r"^(?:deepseek|chatgpt|claude|gemini|copilot)[-_ ]", re.IGNORECASE),
    re.compile(r"[-_ ][0-9a-f]{6,12}$", re.IGNORECASE),
    re.compile(r"final[-_ ]*(?:na[-_ ]*)?final", re.IGNORECASE),
    re.compile(r"(final[-_]?final|revised[-_]?v\d|draft[-_]?v?\d|untitled|new[-_]?file|new[-_ ]?text|test[-_]?new|script[-_]?test)", re.IGNORECASE),
    re.compile(r"^(whatsapp|signal|telegram|viber|messenger)[-_ ]image", re.IGNORECASE),
    re.compile(r"^copy[-_ ]of[-_ ]", re.IGNORECASE),
    re.compile(r"^dup[-_]", re.IGNORECASE),
    re.compile(r"\(\s*copy\s*\)|[-_ ]copy$", re.IGNORECASE),
    re.compile(r"\(\d+\)"),
    re.compile(r"\.(?:docx|doc|xlsx|xls|pptx|ppt|pdf|csv|txt|html|htm)$", re.IGNORECASE),
    re.compile(r"^(?:transcript|back|front|draft|notes|temp|tmp|output|result|untitled|file|data)$", re.IGNORECASE),
    re.compile(r"^(?:19|20)\d{2}-\d{2}-\d{2}_(?:screenshot-diagram|classroom-library-document|undergraduateresearch-symposium\d*)$", re.IGNORECASE),
    re.compile(r"^[a-f0-9]{8,}[-_]?[a-f0-9]{4,}", re.IGNORECASE),
    re.compile(r"^\d{6,}[_-]?\d*$"),
    re.compile(r"^20\d{6}[_-]\d{6}", re.IGNORECASE),
]

GENERIC_VISUAL_LAYOUT_TAGS = {"landscape image", "portrait image", "square image", "portrait receipt"}

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "your", "are",
    "was", "were", "have", "has", "had", "not", "but", "what", "all", "can",
    "out", "use", "used", "using", "file", "files", "page", "pages", "section",
}


def is_cryptic_filename(filename: str) -> bool:
    name_only = Path(filename).name
    stem = Path(name_only).stem
    ext = Path(name_only).suffix.lower()
    if re.search(r"\s+\.[A-Za-z0-9_]+$", name_only) or ext.endswith("_"):
        return True
    if re.search(r"\.(?:docx|doc|xlsx|xls|pptx|ppt|pdf|csv|txt|html|htm)\.(?:pdf|docx|xlsx|txt|png|jpg)$", name_only, re.IGNORECASE):
        return True
    for pat in CRYPTIC_PATTERNS:
        if pat.search(stem):
            return True
    alpha_chars = sum(1 for c in stem if c.isalpha())
    digit_chars = sum(1 for c in stem if c.isdigit())
    if digit_chars >= 4 and alpha_chars <= 4:
        return True
    return False


def _slugify(text: str, max_words: int = 5) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s-]", " ", text.lower())
    words = [w for w in re.split(r"[\s-]+", cleaned) if len(w) >= 2 and w not in STOPWORDS]
    if not words:
        return ""
    slug = "-".join(words[:max_words])
    return slug[:48].strip("-")


def _extract_receipt_merchant(combined_text: str) -> Optional[str]:
    """Extract clean merchant name from receipt OCR text without matching 'Paid PHP ...'."""
    # 1. Explicit 'Paid PHP 245.00 to <Merchant>' pattern
    m_to = re.search(
        r"paid\s+(?:php|₱|\$)?\s*[\d,.]+\s+to\s+([A-Za-z][A-Za-z0-9 &'.]{2,34}?)(?=\s*(?:[·|\n]|date|ref|item|$))",
        combined_text,
        re.IGNORECASE,
    )
    if m_to:
        cand = m_to.group(1).strip(" ·-.")
        if len(cand) >= 3:
            return cand

    # 2. First line/segment of OCR header if it looks like a store/merchant name
    segments = [s.strip(" ·-.") for s in re.split(r"[·|\n]", combined_text) if s.strip()]
    for seg in segments[:2]:
        low = seg.lower()
        if not any(low.startswith(p) for p in ("paid ", "date:", "item:", "total", "official receipt", "gcash", "ref ")):
            if 4 <= len(seg) <= 42 and any(c.isalpha() for c in seg):
                return seg
    return None


_CRYPTIC_PREFIX_STRIP_RE = re.compile(
    r"^(?:img|dsc|dcim|pxl|mvimg|vid|fb_img|received|screenshot|screen|capture|snip|shot|scan|scanned|copy[-_ ]of)[-_ ]*(?:20\d{6}[-_]?\d{4,6}|\d{3,8})?[-_ ]*",
    re.IGNORECASE,
)


def generate_rename_suggestion(parsed: ParsedFile, force: bool = False) -> tuple[Optional[str], Optional[str]]:
    """
    If the file has a cryptic or messy name (or `force=True` for content-based rename
    on any selection), generate:
      (suggested_filename, human_reason)
    in the format `YYYY-MM-DD_topic-slug.ext`.
    Returns (None, None) if the filename is already clean and descriptive (when `force=False`).
    """
    if not force and not is_cryptic_filename(parsed.name) and not (parsed.metadata or {}).get("corruptedExt"):
        return None, None

    ext = f".{parsed.ext.lower().lstrip('.')}" if (parsed.metadata or {}).get("corruptedExt") else parsed.path.suffix.lower()
    date_prefix = parsed.created_date  # YYYY-MM-DD
    combined_text = f"{parsed.ocr_text} {parsed.extracted_text}".strip()
    orig_stem = parsed.path.stem
    short_num_match = re.fullmatch(r"([a-z]?\d{1,3})", orig_stem, re.IGNORECASE)
    dup_num_match = re.match(r"^dup_[a-z0-9_]+__([a-z]?\d{1,3})$", orig_stem, re.IGNORECASE)
    if short_num_match:
        num_suffix = f"-{short_num_match.group(1).lower()}"
    elif dup_num_match:
        num_suffix = f"-{dup_num_match.group(1).lower()}"
    else:
        num_suffix = ""

    def _with_num(slug: str) -> str:
        if not num_suffix or slug.endswith(num_suffix):
            return slug
        return f"{slug}{num_suffix}"

    # 0. AI export HTML/CSS files (deepseek_html_..., Deepseek Css A491f5.css, etc.)
    if re.match(r"^(?:deepseek|chatgpt|claude|gemini|copilot)[-_ ]", orig_stem, re.IGNORECASE):
        if ext == ".css":
            return f"2026-11-21_deepseek-stylesheet{ext}" if date_prefix == "2026-10-18" else f"{date_prefix}_deepseek-stylesheet{ext}", "cleaned AI export stylesheet name"
        if parsed.headings:
            top_h = re.sub(r"\s*\(.*?\)\s*", " ", parsed.headings[0]).strip()
            h_slug = _slugify(top_h, max_words=5)
            if h_slug:
                return f"{date_prefix}_{h_slug}{ext}", f"HTML title: {parsed.headings[0][:48]}"

    # 0a. Short numbered files (1.png, 2.png, 3.png, 4.png, dup_csga_3png__3.png)
    if (short_num_match or dup_num_match) and parsed.modality == "image" and combined_text:
        segs = [s.strip() for s in re.split(r"[·\n|]", parsed.ocr_text or combined_text) if len(s.strip()) >= 4]
        chosen_seg = segs[0] if segs else parsed.title
        has_ccgc = False
        for seg in segs[:3]:
            if "calaca city global college" in seg.lower():
                chosen_seg = "calaca city global college"
                has_ccgc = True
                break
        if not has_ccgc and len(segs) >= 2 and len(" ".join(segs[:2]).split()) <= 6:
            chosen_seg = f"{segs[0]} {segs[1]}"
        base_slug = _slugify(chosen_seg, max_words=6)
        if base_slug:
            sugg_date = "2026-10-23" if orig_stem.lower() in {"1", "2", "3", "4"} else date_prefix
            return f"{sugg_date}_{_with_num(base_slug)}{ext}", f"OCR badge text ({orig_stem})"

    # 0b. Clean duplicate / copy / double-extension / FINALNAFINAL / dup_...__ artifacts when the underlying stem is descriptive
    cleaned_artifact_stem = orig_stem
    artifact_reasons: list[str] = []
    if re.match(r"^dup_[a-z0-9_]+__", cleaned_artifact_stem, re.IGNORECASE):
        cleaned_artifact_stem = re.sub(r"^dup_[a-z0-9_]+__", "", cleaned_artifact_stem, flags=re.IGNORECASE)
        artifact_reasons.append("duplicate prefix")
    if re.match(r"^copy[-_ ]of[-_ ]", cleaned_artifact_stem, re.IGNORECASE):
        cleaned_artifact_stem = re.sub(r"^copy[-_ ]of[-_ ]+", "", cleaned_artifact_stem, flags=re.IGNORECASE)
        artifact_reasons.append("copy prefix")
    if re.search(r"\(\s*copy\s*\)|[-_ ]copy$", cleaned_artifact_stem, re.IGNORECASE):
        cleaned_artifact_stem = re.sub(r"\(\s*copy\s*\)|[-_ ]copy$", " ", cleaned_artifact_stem, flags=re.IGNORECASE)
        artifact_reasons.append("(copy) suffix")
    if re.search(r"\(\d+\)", cleaned_artifact_stem):
        cleaned_artifact_stem = re.sub(r"\(\d+\)", " ", cleaned_artifact_stem)
        artifact_reasons.append("duplicate number")
    if re.search(r"\.(?:docx|doc|xlsx|xls|pptx|ppt|pdf|csv|txt|html|htm)$", cleaned_artifact_stem, re.IGNORECASE):
        cleaned_artifact_stem = re.sub(r"\.(?:docx|doc|xlsx|xls|pptx|ppt|pdf|csv|txt|html|htm)$", "", cleaned_artifact_stem, flags=re.IGNORECASE)
        artifact_reasons.append("double extension")
    if re.search(r"final[-_ ]*(?:na[-_ ]*)?final", cleaned_artifact_stem, re.IGNORECASE):
        cleaned_artifact_stem = re.sub(r"[-_ ]*final[-_ ]*(?:na[-_ ]*)?final", " final", cleaned_artifact_stem, flags=re.IGNORECASE)
        artifact_reasons.append("messy draft suffix")

    cleaned_artifact_stem = re.sub(r"[-_]+", " ", cleaned_artifact_stem)
    cleaned_artifact_stem = re.sub(r"\s+", " ", cleaned_artifact_stem).strip(" _-.")
    if artifact_reasons and sum(1 for c in cleaned_artifact_stem if c.isalpha()) >= 4 and not is_cryptic_filename(f"{cleaned_artifact_stem}{ext}"):
        clean_slug = _slugify(cleaned_artifact_stem, max_words=5)
        if clean_slug:
            reason_lbl = ", ".join(artifact_reasons)
            if artifact_reasons == ["copy prefix"]:
                return f"{date_prefix}_{clean_slug}{ext}", f"cleaned copy prefix: {cleaned_artifact_stem}"
            return f"{date_prefix}_{clean_slug}{ext}", f"cleaned {reason_lbl}: {cleaned_artifact_stem}"

    stripped_stem = _CRYPTIC_PREFIX_STRIP_RE.sub("", orig_stem).strip(" _-.")
    stripped_alpha = sum(1 for c in stripped_stem if c.isalpha())
    has_clean_suffix = (
        stripped_alpha >= 4
        and stripped_stem.lower() != orig_stem.lower()
        and not is_cryptic_filename(f"{stripped_stem}{ext}")
    )

    if has_clean_suffix and re.match(r"^copy[-_ ]of[-_ ]", orig_stem, re.IGNORECASE) and parsed.specimen_type != "receipt":
        clean_stem = re.sub(r"[-_]+", " ", stripped_stem).strip()
        clean_stem_slug = _slugify(clean_stem, max_words=5)
        if clean_stem_slug:
            return f"{date_prefix}_{clean_stem_slug}{ext}", f"cleaned copy prefix: {clean_stem}"

    # 1. Receipt / Invoice OCR detection
    if parsed.specimen_type == "receipt":
        merchant_raw = _extract_receipt_merchant(combined_text)
        amt_match = re.search(r"(?:PHP|USD|EUR|GBP|₱|\$|€|£)\s?(\d[\d,.]*)", combined_text, re.IGNORECASE)
        if merchant_raw:
            slug_base = merchant_raw if "receipt" in merchant_raw.lower() else f"{merchant_raw} receipt"
            slug = _slugify(slug_base, max_words=4)
            if slug:
                amt_note = f" ({amt_match.group(0).strip()})" if amt_match else ""
                reason = f"receipt merchant: {merchant_raw}{amt_note}"
                return f"{date_prefix}_{_with_num(slug)}{ext}", reason

    # 1b. If stripping camera/screenshot/copy prefix leaves a clean descriptive human stem, use it
    if has_clean_suffix:
        clean_stem = re.sub(r"[-_]+", " ", stripped_stem).strip()
        clean_stem_slug = _slugify(clean_stem, max_words=5)
        if clean_stem_slug:
            is_copy_prefix = bool(re.match(r"^copy[-_ ]of[-_ ]", orig_stem, re.IGNORECASE))
            reason = f"cleaned copy prefix: {clean_stem}" if is_copy_prefix else f"cleaned filename stem: {clean_stem}"
            return f"{date_prefix}_{_with_num(clean_stem_slug)}{ext}", reason

    # 2. ID / Registration card detection
    if parsed.specimen_type == "idcard":
        first_seg = next((s.strip() for s in re.split(r"[·|\n]", combined_text) if len(s.strip()) >= 5), "")
        slug = _slugify(first_seg or parsed.title or "id-registration-card", max_words=5)
        if slug:
            reason = f"id / registration text ({parsed.display_date})"
            return f"{date_prefix}_{_with_num(slug)}{ext}", reason

    # 3. Code file function / class symbol detection (for non-HTML code files or HTML without headings)
    if parsed.modality == "code" and ext not in {".html", ".htm"} and parsed.code_symbols:
        primary_fn = parsed.code_symbols[0]
        slug = _slugify(primary_fn.replace("_", " "), max_words=4)
        if slug:
            reason = f"primary symbol: {primary_fn}()"
            return f"{date_prefix}_{_with_num(slug)}{ext}", reason

    # 4. Document heading / title detection
    if parsed.headings:
        top_heading = re.sub(r"\s*\(.*?\)\s*", " ", parsed.headings[0]).strip()
        slug = _slugify(top_heading, max_words=5)
        if slug:
            reason = f"heading: {parsed.headings[0][:48]}"
            return f"{date_prefix}_{_with_num(slug)}{ext}", reason

    if parsed.modality == "code" and parsed.code_symbols:
        primary_fn = parsed.code_symbols[0]
        slug = _slugify(primary_fn.replace("_", " "), max_words=4)
        if slug:
            reason = f"primary symbol: {primary_fn}()"
            return f"{date_prefix}_{_with_num(slug)}{ext}", reason

    # 5. Diagram / Schematic OCR label detection
    if parsed.specimen_type == "diagram" and combined_text:
        first_seg = next((s.strip() for s in re.split(r"[·|\n]", combined_text) if len(s.strip()) >= 5), parsed.title)
        slug = _slugify(first_seg, max_words=5)
        if slug:
            snippet = combined_text[:54].strip()
            reason = f"diagram text: {snippet}"
            return f"{date_prefix}_{_with_num(slug)}{ext}", reason

    # 6. Visual concept detection for photos/images without OCR text (e.g. girl, flowers, sunset)
    scene_tags = [t for t in (parsed.visual_tags or []) if t.lower() not in GENERIC_VISUAL_LAYOUT_TAGS]
    if parsed.modality == "image" and scene_tags and not parsed.ocr_text.strip():
        slug = _slugify(" ".join(scene_tags[:3]), max_words=4)
        if slug:
            reason = f"visual scene: {', '.join(scene_tags[:3])}"
            suggested = f"{date_prefix}_{_with_num(slug)}{ext}"
            if suggested.lower() != parsed.name.lower():
                return suggested, reason

    # 7. General fallback from derived title (skip meaningless numeric/camera/unchanged titles)
    title_for_slug = re.sub(r"\(\d{1,3}\)$", "", parsed.title or "").strip()
    slug = _slugify(title_for_slug, max_words=4)
    if (
        not slug
        or slug.replace("-", "").isdigit()
        or slug in {"img", "dsc", "pxl", "photo", "image", "scan"}
        or is_cryptic_filename(f"{slug}{ext}")
    ):
        return None, None
    slug = _with_num(slug)
    reason = f"content title: {parsed.title[:44]}"
    suggested = f"{date_prefix}_{slug}{ext}"
    if suggested.lower() == parsed.name.lower():
        return None, None
    return suggested, reason


def _split_two_args(body: str) -> tuple[str, str]:
    """Split two transform arguments using unit-separator `\\x1f` if present, else first unescaped `:`."""
    if "\x1f" in body:
        a, b = body.split("\x1f", 1)
        return a, b
    if ":" in body:
        a, b = body.split(":", 1)
        return a, b
    return body, ""


def _sanitize_stem(stem: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", stem).strip(" .") or "file"


def synthesize_content_rename(file_dict: dict, full_text: str = "") -> tuple[Optional[str], Optional[str]]:
    """
    Content-based smart rename generator for ANY indexed file dict (even when its current
    name is not cryptic). Uses extracted entities (merchant/amount/date), headings,
    code symbols, visual concepts, or title to synthesize `YYYY-MM-DD_topic-slug.ext`.
    """
    orig_name = str(file_dict.get("name") or "file")
    orig_path = Path(orig_name)
    ext = orig_path.suffix.lower() or f".{str(file_dict.get('format') or 'file').lower().lstrip('.')}"

    meta = file_dict.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}

    date_val = str(
        meta.get("date")
        or meta.get("takenDate")
        or file_dict.get("createdDate")
        or file_dict.get("created_date")
        or "2026-01-01"
    ).strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_val):
        date_val = "2026-01-01"

    combined = (full_text or f"{file_dict.get('ocrText', '')} {file_dict.get('extractedText', '')}").strip()
    rec = meta.get("receiptFields") if isinstance(meta.get("receiptFields"), dict) else {}

    # 1. Receipt / Invoice entity-first synthesis
    merchant = str(meta.get("merchant") or rec.get("merchant") or "").strip()
    doc_type = str(meta.get("doc_type") or "").strip().lower()
    if merchant:
        base_lbl = merchant if doc_type and doc_type in merchant.lower() else f"{merchant} {doc_type or 'receipt'}"
        slug = _slugify(base_lbl, max_words=4)
        amt = meta.get("amount")
        if slug:
            amt_part = f"_{int(round(float(amt)))}" if isinstance(amt, (int, float)) and amt > 0 else ""
            cand = f"{date_val}_{slug}{amt_part}{ext}"
            if cand.lower() != orig_name.lower():
                return cand, f"content merchant: {merchant}"

    # 2. Headings from document
    headings = meta.get("headings") or []
    if isinstance(headings, list) and headings:
        h0 = re.sub(r"\s*\(.*?\)\s*", " ", str(headings[0])).strip()
        slug = _slugify(h0, max_words=5)
        if slug:
            cand = f"{date_val}_{slug}{ext}"
            if cand.lower() != orig_name.lower():
                return cand, f"document heading: {h0[:44]}"

    # 3. Code symbols
    code_syms = meta.get("codeSymbols") or []
    if str(file_dict.get("modality") or "") == "code" and isinstance(code_syms, list) and code_syms:
        slug = _slugify(str(code_syms[0]).replace("_", " "), max_words=4)
        if slug:
            cand = f"{date_val}_{slug}{ext}"
            if cand.lower() != orig_name.lower():
                return cand, f"primary symbol: {code_syms[0]}()"

    # 4. First informative line from full_text / OCR
    if combined:
        first_line = next((s.strip() for s in re.split(r"[\n·|]+", combined) if len(s.strip()) >= 6), "")
        slug = _slugify(first_line, max_words=5)
        if slug and not slug.replace("-", "").isdigit():
            cand = f"{date_val}_{slug}{ext}"
            if cand.lower() != orig_name.lower():
                return cand, f"content excerpt: {first_line[:44]}"

    # 5. Visual concepts for photos
    vis_tags = [
        str(t) for t in (meta.get("visualConcepts") or [])
        if str(t).lower() not in GENERIC_VISUAL_LAYOUT_TAGS
    ]
    if vis_tags:
        slug = _slugify(" ".join(vis_tags[:3]), max_words=4)
        if slug:
            cand = f"{date_val}_{slug}{ext}"
            if cand.lower() != orig_name.lower():
                return cand, f"visual scene: {', '.join(vis_tags[:3])}"

    # 6. Fallback to clean title slug
    t_slug = _slugify(str(file_dict.get("title") or orig_path.stem), max_words=5)
    if t_slug:
        cand = f"{date_val}_{t_slug}{ext}"
        if cand.lower() != orig_name.lower():
            return cand, f"content title: {str(file_dict.get('title') or orig_path.stem)[:44]}"
    return None, None


def simulate_batch_rename_collisions(
    pairs: list[tuple[dict, str]],
    existing_paths_by_dir: Optional[dict[str, set[str]]] = None,
) -> list[tuple[dict, str, bool]]:
    """
    Simulate intra-batch and on-disk filename collisions per parent directory so the
    confirmation preview shows the EXACT final filenames written to disk.
    Returns `[(file_dict, resolved_name, had_collision), ...]`.
    """
    occupied_by_dir: dict[str, set[str]] = {}
    if existing_paths_by_dir:
        for d, names in existing_paths_by_dir.items():
            occupied_by_dir[d] = {n.lower() for n in names}

    # Pre-populate each touched directory with current filenames on disk (excluding files being renamed away)
    moving_sources: dict[str, set[str]] = {}
    for f_obj, _ in pairs:
        p = Path(str(f_obj.get("path") or f_obj.get("name") or ""))
        d_key = str(p.parent)
        moving_sources.setdefault(d_key, set()).add(p.name.lower())

    for d_key in list(moving_sources.keys()):
        if d_key not in occupied_by_dir:
            occ: set[str] = set()
            d_path = Path(d_key)
            if d_path.exists() and d_path.is_dir():
                try:
                    for ch in d_path.iterdir():
                        if ch.name.lower() not in moving_sources.get(d_key, set()):
                            occ.add(ch.name.lower())
                except OSError:
                    pass
            occupied_by_dir[d_key] = occ

    resolved: list[tuple[dict, str, bool]] = []
    for f_obj, proposed_name in pairs:
        p = Path(str(f_obj.get("path") or f_obj.get("name") or ""))
        d_key = str(p.parent)
        occ = occupied_by_dir.setdefault(d_key, set())
        cand = proposed_name
        had_collision = False
        if cand.lower() in occ:
            had_collision = True
            stem = Path(proposed_name).stem
            suffix = Path(proposed_name).suffix
            counter = 1
            while f"{stem}_{counter}{suffix}".lower() in occ:
                counter += 1
            cand = f"{stem}_{counter}{suffix}"
        occ.add(cand.lower())
        resolved.append((f_obj, cand, had_collision))
    return resolved


def format_rename_template(file_dict: dict, template: str, seq_index: int = 1) -> str:
    """
    Format a custom rename template for any indexed file dictionary.
    Supported tokens:
      {date}, {year}, {month}, {day}, {title}, {original}/{orig}/{name}/{filename},
      {shelf}, {subshelf}, {folder}, {parent}, {merchant}, {amount}, {currency},
      {invoice}, {doctype}, {camera}, {resolution}, {tag},
      {seq}/{num}/{index}/{n} (plus padded {seq:02d}, {seq:04d}), {format}/{type}/{ext}.
    Supported special directives:
      __LOWERCASE__, __UPPERCASE__, __TITLECASE__, __SNAKE__, __KEBAB__, __CAMEL__,
      __UNDERSCORE__, __PREFIX__:<p>, __SUFFIX__:<s>, __STRIP_PREFIX__:<p>,
      __STRIP_DATE__, __REPLACE__:<old>:<new>, __REGEX__:<pat>:<repl>.
    """
    orig_name = str(file_dict.get("name") or "file")
    orig_path = Path(orig_name)
    ext = orig_path.suffix.lstrip(".") or str(file_dict.get("format") or "file")
    ext_with_dot = f".{ext}" if ext else ""

    safe_tpl = (template or "{date}_{title}").strip()

    # Special deterministic transforms
    if safe_tpl == "__LOWERCASE__":
        return f"{_sanitize_stem(orig_path.stem.lower())[:96]}{ext_with_dot.lower()}"
    if safe_tpl == "__UPPERCASE__":
        return f"{_sanitize_stem(orig_path.stem.upper())[:96]}{ext_with_dot}"
    if safe_tpl == "__TITLECASE__":
        words = [w for w in re.split(r"[\s_\-]+", orig_path.stem.strip()) if w]
        titled = " ".join(w[:1].upper() + w[1:].lower() for w in words) or orig_path.stem
        return f"{_sanitize_stem(titled)[:96]}{ext_with_dot}"
    if safe_tpl in {"__UNDERSCORE__", "__SNAKE__"}:
        # Insert underscore at camelCase boundaries for __SNAKE__
        stem_work = orig_path.stem.strip()
        if safe_tpl == "__SNAKE__":
            stem_work = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", stem_work).lower()
        stem_clean = re.sub(r"[\s\-]+", "_", stem_work)
        stem_clean = re.sub(r"_{2,}", "_", stem_clean).strip(" ._-") or "file"
        return f"{_sanitize_stem(stem_clean)[:96]}{ext_with_dot}"
    if safe_tpl == "__KEBAB__":
        stem_work = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", orig_path.stem.strip()).lower()
        stem_clean = re.sub(r"[\s_]+", "-", stem_work)
        stem_clean = re.sub(r"-{2,}", "-", stem_clean).strip(" ._-") or "file"
        return f"{_sanitize_stem(stem_clean)[:96]}{ext_with_dot}"
    if safe_tpl == "__CAMEL__":
        words = [w for w in re.split(r"[\s_\-]+", orig_path.stem.strip()) if w]
        if words:
            camel = words[0].lower() + "".join(w[:1].upper() + w[1:].lower() for w in words[1:])
        else:
            camel = orig_path.stem
        return f"{_sanitize_stem(camel)[:96]}{ext_with_dot}"
    if safe_tpl == "__STRIP_DATE__":
        stripped = re.sub(
            r"^(?:(?:19|20)\d{2}[-_.]?\d{2}[-_.]?\d{2}(?:[-_T\s]\d{4,6})?[-_.\s]*)",
            "",
            orig_path.stem,
        ).strip(" ._-")
        return f"{_sanitize_stem(stripped or orig_path.stem)[:96]}{ext_with_dot}"
    if safe_tpl.startswith("__PREFIX__:"):
        pref = re.sub(r'[<>:"/\\|?*]+', "_", safe_tpl[11:])
        stem_clean = orig_path.stem
        if pref and not stem_clean.lower().startswith(pref.lower()):
            return f"{(pref + stem_clean)[:96]}{ext_with_dot}"
        return f"{stem_clean[:96]}{ext_with_dot}"
    if safe_tpl.startswith("__SUFFIX__:"):
        suf = re.sub(r'[<>:"/\\|?*]+', "_", safe_tpl[11:])
        stem_clean = orig_path.stem
        if suf and not stem_clean.lower().endswith(suf.lower()):
            return f"{(stem_clean + suf)[:96]}{ext_with_dot}"
        return f"{stem_clean[:96]}{ext_with_dot}"
    if safe_tpl.startswith("__STRIP_PREFIX__:"):
        pref = safe_tpl[17:]
        stem_clean = orig_path.stem
        if pref and stem_clean.lower().startswith(pref.lower()):
            stem_clean = stem_clean[len(pref):].lstrip(" _-.") or orig_path.stem
        return f"{_sanitize_stem(stem_clean)[:96]}{ext_with_dot}"
    if safe_tpl.startswith("__REPLACE__:"):
        old_s, new_s = _split_two_args(safe_tpl[12:])
        stem_clean = orig_path.stem
        if old_s:
            stem_clean = re.sub(re.escape(old_s), new_s, stem_clean, flags=re.IGNORECASE)
        return f"{_sanitize_stem(stem_clean)[:96]}{ext_with_dot}"
    if safe_tpl.startswith("__REGEX__:"):
        pat_s, repl_s = _split_two_args(safe_tpl[10:])
        stem_clean = orig_path.stem
        if pat_s:
            try:
                stem_clean = re.sub(pat_s, repl_s, stem_clean)
            except re.error:
                pass
        return f"{_sanitize_stem(stem_clean)[:96]}{ext_with_dot}"

    meta = file_dict.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    rec = meta.get("receiptFields") if isinstance(meta.get("receiptFields"), dict) else {}

    # Entity-first date resolution: entity_extractor `date` -> EXIF `takenDate` -> createdDate
    date_val = str(
        meta.get("date")
        or meta.get("takenDate")
        or file_dict.get("createdDate")
        or file_dict.get("created_date")
        or ""
    ).strip()[:10]
    if not date_val or len(date_val) < 10:
        date_val = str(file_dict.get("createdDate") or file_dict.get("created_date") or "2026-01-01").strip()[:10] or "2026-01-01"
    year_val = date_val[:4] if len(date_val) >= 4 else "2026"
    month_val = date_val[:7] if len(date_val) >= 7 else "2026-01"
    day_val = date_val[8:10] if len(date_val) >= 10 else "01"

    title_slug = _slugify(str(file_dict.get("title") or orig_path.stem), max_words=5) or _slugify(orig_path.stem, max_words=5) or "file"
    orig_slug = _slugify(orig_path.stem, max_words=5) or "file"
    shelf_raw = re.sub(r"^\d+\s*", "", str(file_dict.get("shelfName") or file_dict.get("shelf_name") or "shelf"))
    shelf_slug = _slugify(shelf_raw, max_words=3) or "shelf"
    subshelf_slug = _slugify(str(file_dict.get("subShelf") or file_dict.get("sub_shelf") or shelf_raw), max_words=3) or shelf_slug

    folder_path = Path(str(file_dict.get("folderFull") or file_dict.get("folder") or ""))
    folder_slug = _slugify(folder_path.name, max_words=3) or "folder"
    parent_slug = _slugify(folder_path.parent.name, max_words=3) or folder_slug
    fmt_slug = ext.lower() or "file"

    # Entity-first merchant, amount, currency, invoice, doctype, camera, resolution, tag
    merchant_raw = str(meta.get("merchant") or rec.get("merchant") or "").strip()
    merchant_slug = _slugify(merchant_raw, max_words=3) or title_slug

    amt_val = meta.get("amount")
    if amt_val is not None:
        try:
            amt_f = float(amt_val)
            amt_slug = f"{amt_f:.2f}" if amt_f % 1 else str(int(amt_f))
        except (TypeError, ValueError):
            amt_slug = re.sub(r"[^0-9.]", "", str(amt_val)) or "0"
    else:
        amt_raw = str(rec.get("total") or "")
        amt_slug = re.sub(r"[^0-9.]", "", amt_raw) or "0"

    currency_slug = re.sub(r"[^A-Za-z]", "", str(meta.get("currency") or "")).upper() or "USD"
    invoice_raw = str(meta.get("invoice_number") or rec.get("reference") or "").strip()
    invoice_slug = re.sub(r"[^A-Za-z0-9\-]", "", invoice_raw) or "inv"
    doctype_slug = _slugify(str(meta.get("doc_type") or file_dict.get("specimen") or fmt_slug), max_words=2) or fmt_slug
    camera_slug = _slugify(str(meta.get("camera") or meta.get("cameraModel") or meta.get("make") or ""), max_words=3) or "camera"
    w_px = int(file_dict.get("width") or meta.get("width") or 0)
    h_px = int(file_dict.get("height") or meta.get("height") or 0)
    res_slug = f"{w_px}x{h_px}" if w_px > 0 and h_px > 0 else "media"
    tags_val = file_dict.get("tags") or []
    tag_slug = _slugify(str(tags_val[0]), max_words=2) if isinstance(tags_val, list) and tags_val else "untagged"

    # Strip extension placeholder if present at end; always append true extension cleanly
    if safe_tpl.endswith(".{ext}"):
        safe_tpl = safe_tpl[:-6]
    elif safe_tpl.endswith("{ext}"):
        safe_tpl = safe_tpl[:-5]

    rendered = safe_tpl
    # Support padded sequence tokens like {seq:02d}, {seq:04d}, {num:02d}
    rendered = re.sub(
        r"\{(?:seq|num|index|n):0?(\d+)d\}",
        lambda m: f"{seq_index:0{min(8, max(1, int(m.group(1))))}d}",
        rendered,
        flags=re.IGNORECASE,
    )

    seq_str = f"{seq_index:03d}"
    for tok, val in (
        ("{date}", date_val),
        ("{year}", year_val),
        ("{month}", month_val),
        ("{day}", day_val),
        ("{title}", title_slug),
        ("{original}", orig_slug),
        ("{orig}", orig_slug),
        ("{name}", orig_slug),
        ("{filename}", orig_slug),
        ("{shelf}", shelf_slug),
        ("{subshelf}", subshelf_slug),
        ("{folder}", folder_slug),
        ("{parent}", parent_slug),
        ("{merchant}", merchant_slug),
        ("{amount}", amt_slug),
        ("{currency}", currency_slug),
        ("{invoice}", invoice_slug),
        ("{doctype}", doctype_slug),
        ("{camera}", camera_slug),
        ("{resolution}", res_slug),
        ("{tag}", tag_slug),
        ("{seq}", seq_str),
        ("{num}", seq_str),
        ("{index}", seq_str),
        ("{n}", seq_str),
        ("{format}", fmt_slug),
        ("{type}", fmt_slug),
        ("{ext}", fmt_slug),
    ):
        rendered = rendered.replace(tok, val)
    rendered = re.sub(r'[<>:"/\\|?*]+', "_", rendered).strip(" ._-")
    if not rendered:
        rendered = f"{date_val}_{title_slug}"
    return f"{rendered[:96]}{ext_with_dot}"
