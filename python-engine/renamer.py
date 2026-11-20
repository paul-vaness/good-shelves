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
    re.compile(r"^(img|dsc|dcim|pxl|mvimg|vid|fb_img|received)[-_]?\d+", re.IGNORECASE),
    re.compile(r"^(screenshot|screen|capture|snip|shot)[-_]?\d*", re.IGNORECASE),
    re.compile(r"^(scan|scanned|doc|document|image|photo|picture|download|export|attachment)[-_]?\d*$", re.IGNORECASE),
    re.compile(r"^(scan|scanned|doc|document|image|photo|picture)[-_]?\d+", re.IGNORECASE),
    re.compile(r"(final[-_]?final|revised[-_]?v\d|draft[-_]?v?\d|untitled|new[-_]?file|new[-_ ]?text|test[-_]?new|script[-_]?test)", re.IGNORECASE),
    re.compile(r"^(whatsapp|signal|telegram|viber|messenger)[-_ ]image", re.IGNORECASE),
    re.compile(r"^copy[-_ ]of[-_ ]", re.IGNORECASE),
    re.compile(r"\(\d+\)$"),
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
    stem = Path(filename).stem
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


def generate_rename_suggestion(parsed: ParsedFile) -> tuple[Optional[str], Optional[str]]:
    """
    If the file has a cryptic or messy name, generate:
      (suggested_filename, human_reason)
    in the format `YYYY-MM-DD_topic-slug.ext`.
    Returns (None, None) if the filename is already clean and descriptive.
    """
    if not is_cryptic_filename(parsed.name):
        return None, None

    ext = parsed.path.suffix.lower()
    date_prefix = parsed.created_date  # YYYY-MM-DD
    combined_text = f"{parsed.ocr_text} {parsed.extracted_text}".strip()

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
                return f"{date_prefix}_{slug}{ext}", reason

    # 2. ID / Registration card detection
    if parsed.specimen_type == "idcard":
        first_seg = next((s.strip() for s in re.split(r"[·|\n]", combined_text) if len(s.strip()) >= 5), "")
        slug = _slugify(first_seg or parsed.title or "id-registration-card", max_words=5)
        if slug:
            reason = f"id / registration text ({parsed.display_date})"
            return f"{date_prefix}_{slug}{ext}", reason

    # 3. Code file function / class symbol detection
    if parsed.modality == "code" and parsed.code_symbols:
        primary_fn = parsed.code_symbols[0]
        slug = _slugify(primary_fn.replace("_", " "), max_words=4)
        if slug:
            reason = f"primary symbol: {primary_fn}()"
            return f"{date_prefix}_{slug}{ext}", reason

    # 4. Document heading / title detection
    if parsed.headings:
        top_heading = re.sub(r"\s*\(.*?\)\s*", " ", parsed.headings[0]).strip()
        slug = _slugify(top_heading, max_words=5)
        if slug:
            reason = f"heading: {parsed.headings[0][:48]}"
            return f"{date_prefix}_{slug}{ext}", reason

    # 5. Diagram / Schematic OCR label detection
    if parsed.specimen_type == "diagram" and combined_text:
        first_seg = next((s.strip() for s in re.split(r"[·|\n]", combined_text) if len(s.strip()) >= 5), parsed.title)
        slug = _slugify(first_seg, max_words=5)
        if slug:
            snippet = combined_text[:54].strip()
            reason = f"diagram text: {snippet}"
            return f"{date_prefix}_{slug}{ext}", reason

    # 6. Visual concept detection for photos/images without OCR text (e.g. girl, flowers, sunset)
    scene_tags = [t for t in (parsed.visual_tags or []) if t.lower() not in GENERIC_VISUAL_LAYOUT_TAGS]
    if parsed.modality == "image" and scene_tags:
        slug = _slugify(" ".join(scene_tags[:3]), max_words=4)
        if slug:
            reason = f"visual scene: {', '.join(scene_tags[:3])}"
            suggested = f"{date_prefix}_{slug}{ext}"
            if suggested.lower() != parsed.name.lower():
                return suggested, reason

    # 7. General fallback from derived title (skip meaningless numeric/camera/unchanged titles)
    slug = _slugify(parsed.title, max_words=4)
    if (
        not slug
        or slug.replace("-", "").isdigit()
        or slug in {"img", "dsc", "pxl", "photo", "image", "scan"}
        or is_cryptic_filename(f"{slug}{ext}")
    ):
        return None, None
    reason = f"content title: {parsed.title[:44]}"
    suggested = f"{date_prefix}_{slug}{ext}"
    if suggested.lower() == parsed.name.lower():
        return None, None
    return suggested, reason


def format_rename_template(file_dict: dict, template: str, seq_index: int = 1) -> str:
    """
    Format a custom rename template for any indexed file dictionary.
    Supported tokens: {date}, {title}, {original}, {shelf}, {merchant}, {amount}, {seq}, {ext}.
    """
    orig_name = str(file_dict.get("name") or "file")
    orig_path = Path(orig_name)
    ext = orig_path.suffix.lstrip(".") or str(file_dict.get("format") or "file")
    ext_with_dot = f".{ext}" if ext else ""

    date_val = str(file_dict.get("createdDate") or file_dict.get("created_date") or "").strip()
    if not date_val:
        date_val = "2026-01-01"

    title_slug = _slugify(str(file_dict.get("title") or orig_path.stem), max_words=5) or _slugify(orig_path.stem, max_words=5) or "file"
    orig_slug = _slugify(orig_path.stem, max_words=5) or "file"
    shelf_raw = re.sub(r"^\d+\s*", "", str(file_dict.get("shelfName") or file_dict.get("shelf_name") or "shelf"))
    shelf_slug = _slugify(shelf_raw, max_words=3) or "shelf"

    meta = file_dict.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json
            meta = json.loads(meta)
        except Exception:
            meta = {}
    rec = meta.get("receiptFields") or {}
    merchant_slug = _slugify(str(rec.get("merchant") or ""), max_words=3) or title_slug
    amt_raw = str(rec.get("total") or "")
    amt_slug = re.sub(r"[^0-9.]", "", amt_raw) or "0"

    safe_tpl = (template or "{date}_{title}").strip()
    # Strip extension placeholder if present at end; always append true extension cleanly
    if safe_tpl.endswith(".{ext}"):
        safe_tpl = safe_tpl[:-6]
    elif safe_tpl.endswith("{ext}"):
        safe_tpl = safe_tpl[:-5]

    rendered = (
        safe_tpl.replace("{date}", date_val)
        .replace("{title}", title_slug)
        .replace("{original}", orig_slug)
        .replace("{shelf}", shelf_slug)
        .replace("{merchant}", merchant_slug)
        .replace("{amount}", amt_slug)
        .replace("{seq}", f"{seq_index:03d}")
    )
    rendered = re.sub(r'[<>:"/\\|?*]+', "_", rendered).strip(" ._-")
    if not rendered:
        rendered = f"{date_val}_{title_slug}"
    return f"{rendered[:96]}{ext_with_dot}"
