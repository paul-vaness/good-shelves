"""
Multimodal Local File Parsers & OCR Extraction Engine for Good Shelves.
Supports PDFs (PyMuPDF), Word Documents (python-docx), HTML, Text/Code chunking (512 tokens, 64 overlap),
and Images (PIL + RapidOCR / Tesseract OCR + perceptual dHash + visual layout heuristics).
"""

from __future__ import annotations

import base64
import csv
import datetime
import hashlib
import io
import os
import platform
import re
import shutil
import stat as stat_mod
import subprocess
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageOps

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

try:
    import docx
except ImportError:
    docx = None  # type: ignore

try:
    import openpyxl
except ImportError:
    openpyxl = None  # type: ignore

_RAPID_OCR: Any = None
_RAPID_OCR_INIT_DONE = False
_RAPID_OCR_LOCK = threading.Lock()
_RAPID_OCR_RUN_LOCK = threading.Lock()
_FITZ_LOCK = threading.Lock()


def _subprocess_win_kwargs() -> dict[str, Any]:
    """Hide transient console windows when spawning helper CLIs on Windows."""
    if platform.system() == "Windows":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _get_rapid_ocr() -> Any:
    """Lazy-initialize RapidOCR on first image OCR request so server startup stays instant."""
    global _RAPID_OCR, _RAPID_OCR_INIT_DONE
    if _RAPID_OCR_INIT_DONE:
        return _RAPID_OCR
    with _RAPID_OCR_LOCK:
        if not _RAPID_OCR_INIT_DONE:
            try:
                from rapidocr_onnxruntime import RapidOCR  # type: ignore

                _RAPID_OCR = RapidOCR(
                    det_limit_type="max",
                    det_limit_side_len=640,
                    use_angle_cls=False,
                    rec_batch_num=1,
                )
            except Exception as exc:
                import sys

                print(f"[parsers] RapidOCR init failed: {exc}", file=sys.stderr)
                _RAPID_OCR = None
            _RAPID_OCR_INIT_DONE = True
    return _RAPID_OCR


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif", ".avif", ".heic", ".svg"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
ARCHIVE_EXTS = {".zip", ".tar", ".tgz", ".gz"}
SPREADSHEET_EXTS = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}
DOC_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".odt", ".txt", ".md", ".markdown", ".csv", ".tsv", ".rtf", ".log", ".xlsx", ".xlsm", ".xls"}
CODE_EXTS = {
    ".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".sql", ".sh", ".html", ".htm", ".css", ".php", ".xml", ".vue",
    ".svelte", ".c", ".cpp", ".h", ".java", ".go", ".rb", ".kt", ".swift",
    ".ini", ".conf",
}


@dataclass
class ParsedFile:
    path: Path
    name: str
    ext: str
    modality: str  # 'image' | 'document' | 'code'
    size_bytes: int
    mtime: float
    created_date: str  # YYYY-MM-DD
    display_date: str  # e.g., Nov 9, 2026
    sha256: str
    title: str
    extracted_text: str
    ocr_text: str
    ocr_confidence: float
    chunks: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    code_symbols: list[str] = field(default_factory=list)
    visual_tags: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    page_count: int = 1
    dhash: str = ""
    specimen_type: str = "text"  # 'receipt' | 'diagram' | 'code' | 'idcard' | 'text'
    thumbnail_b64: Optional[str] = None
    pil_image: Optional[Image.Image] = None
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with filepath.open("rb") as f:
        while chunk := f.read(131072):
            h.update(chunk)
    return h.hexdigest()


def chunk_text_tokens(text: str, max_tokens: int = 512, overlap: int = 64) -> list[str]:
    """Chunk UTF-8 text into ~512-token windows with 64-token overlap."""
    words = text.split()
    if not words:
        return []
    if len(words) <= max_tokens:
        return [" ".join(words)]
    chunks: list[str] = []
    step = max(1, max_tokens - overlap)
    for start in range(0, len(words), step):
        window = words[start : start + max_tokens]
        if window:
            chunks.append(" ".join(window))
        if start + max_tokens >= len(words):
            break
    return chunks


def compute_dhash(img: Image.Image, hash_size: int = 8) -> str:
    """Compute 64-bit perceptual difference hash (dHash) for near-duplicate image detection."""
    gray = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
    raw = gray.tobytes()
    bits = 0
    w = hash_size + 1
    for row in range(hash_size):
        row_start = row * w
        for col in range(hash_size):
            bits = (bits << 1) | (1 if raw[row_start + col] > raw[row_start + col + 1] else 0)
    return f"{bits:016x}"


_TESSERACT_BIN_CACHE: Optional[str] = None
_TESSERACT_CHECKED = False


def _find_tesseract_bin() -> Optional[str]:
    global _TESSERACT_BIN_CACHE, _TESSERACT_CHECKED
    if _TESSERACT_CHECKED:
        return _TESSERACT_BIN_CACHE
    _TESSERACT_CHECKED = True
    found = shutil.which("tesseract")
    if found:
        _TESSERACT_BIN_CACHE = found
        return found
    if platform.system() == "Windows":
        for p in [
            Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        ]:
            if p.exists():
                _TESSERACT_BIN_CACHE = str(p)
                return _TESSERACT_BIN_CACHE
    return None


def _clean_ocr_lines(lines_with_conf: list[tuple[str, float]]) -> tuple[str, float]:
    """Filter out low-confidence single-character OCR noise while preserving short meaningful labels."""
    kept_lines: list[str] = []
    kept_confs: list[float] = []
    for text, conf in lines_with_conf:
        cleaned = re.sub(r"\s+", " ", text).strip()
        alnum = re.sub(r"[^a-zA-Z0-9]", "", cleaned)
        if len(alnum) < 2:
            continue
        if len(alnum) == 2 and conf < 0.84:
            continue
        if len(alnum) <= 4 and conf < 0.72:
            continue
        if conf < 0.50:
            continue
        kept_lines.append(cleaned)
        kept_confs.append(conf)

    if not kept_lines:
        return "", 0.0
    joined = " · ".join(kept_lines)
    avg_conf = (sum(kept_confs) / len(kept_confs)) * 100.0
    return joined, round(min(99.9, avg_conf), 1)


def run_ocr_on_image(img: Image.Image) -> tuple[str, float]:
    """
    Extract printed words inside photos, screenshots, receipts, diagrams, and ID cards.
    Uses RapidOCR (ONNX Runtime) with bounded detection & contrast recovery, falling back to Tesseract.
    """
    # Fast skip for blank or solid-color images
    try:
        lo, hi = img.convert("L").resize((64, 64), Image.Resampling.BILINEAR).getextrema()
        if (hi - lo) < 12:
            return "", 0.0
    except Exception:
        pass

    rapid_ocr = _get_rapid_ocr()
    if rapid_ocr is not None:
        try:
            import numpy as np

            work = img.convert("RGB")
            max_dim = max(work.size)
            if max_dim < 540:
                scale = min(2.0, 720.0 / max(1, max_dim))
                work = work.resize(
                    (max(1, int(work.width * scale)), max(1, int(work.height * scale))),
                    Image.Resampling.BICUBIC,
                )
            elif max_dim > 840:
                scale = 840.0 / max_dim
                work = work.resize(
                    (max(1, int(work.width * scale)), max(1, int(work.height * scale))),
                    Image.Resampling.BILINEAR,
                )

            def _run_rapid_fast(pil_in: Image.Image) -> tuple[str, float, int]:
                arr = np.asarray(pil_in)
                with _RAPID_OCR_RUN_LOCK:
                    p_img, ratio_h, ratio_w = rapid_ocr.preprocess(arr)
                    op_record = {"preprocess": {"ratio_h": ratio_h, "ratio_w": ratio_w}}
                    p_img, op_record = rapid_ocr.maybe_add_letterbox(p_img, op_record)
                    dt_boxes, _ = rapid_ocr.auto_text_det(p_img)
                    if dt_boxes is None or len(dt_boxes) == 0:
                        return "", 0.0, 0
                    crops = rapid_ocr.get_crop_img_list(p_img, dt_boxes)
                    if not crops:
                        return "", 0.0, 0
                    raw_count = len(crops)
                    if len(crops) > 12:
                        filtered = [c for c in crops if c.shape[0] >= 10 and c.shape[1] >= 18]
                        if filtered:
                            crops = filtered
                    if len(crops) > 28:
                        head = crops[:16]
                        rest_indexed = sorted(
                            enumerate(crops[16:], start=16),
                            key=lambda item: item[1].shape[0] * item[1].shape[1],
                            reverse=True,
                        )[:12]
                        rest_indexed.sort(key=lambda item: item[0])
                        crops = head + [c for _, c in rest_indexed]
                    rec_res, _ = rapid_ocr.text_rec(crops, False)
                if not rec_res:
                    return "", 0.0, raw_count
                pairs = [
                    (str(item[0]), float(item[1]) if len(item) >= 2 else 0.92)
                    for item in rec_res
                    if item and len(item) >= 1 and item[0]
                ]
                cleaned_txt, avg_conf = _clean_ocr_lines(pairs)
                return cleaned_txt, avg_conf, raw_count

            txt, conf, box_count = _run_rapid_fast(work)
            if 1 <= box_count <= 12 and not txt:
                # Retry once with autocontrast grayscale->RGB for faint receipts & dark-mode text
                enhanced = ImageOps.autocontrast(work.convert("L"), cutoff=1).convert("RGB")
                txt, conf, _ = _run_rapid_fast(enhanced)
            return txt, conf
        except Exception:
            pass

    tess_bin = _find_tesseract_bin()
    if tess_bin:
        try:
            work_img = img.convert("L")
            w, h = work_img.size
            if max(w, h) < 900:
                work_img = work_img.resize((w * 2, h * 2), Image.Resampling.BICUBIC)
            elif max(w, h) > 1600:
                scale = 1600.0 / max(w, h)
                work_img = work_img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)
            work_img = ImageOps.autocontrast(work_img)

            buf = io.BytesIO()
            work_img.save(buf, format="BMP")
            env = {**os.environ, "OMP_THREAD_LIMIT": "2"}
            proc = subprocess.run(
                [tess_bin, "stdin", "stdout", "--psm", "6"],
                input=buf.getvalue(),
                capture_output=True,
                timeout=8,
                env=env,
                **_subprocess_win_kwargs(),
            )
            raw_text = proc.stdout.decode("utf-8", errors="replace").strip()
            cleaned_lines = [
                (re.sub(r"\s+", " ", ln).strip(), 0.94)
                for ln in raw_text.splitlines()
                if len(re.sub(r"[^a-zA-Z0-9]", "", ln)) >= 3
            ]
            return _clean_ocr_lines(cleaned_lines)
        except Exception:
            pass

    return "", 0.0


def _parse_exif_datetime(raw: Any) -> Optional[tuple[str, str, str]]:
    """Parse EXIF timestamps like '2025:10:18 14:20:05' into (YYYY-MM-DD, HumanDate, RawClean)."""
    s = str(raw or "").strip()
    m = re.search(r"(\d{4})[:\-/](\d{1,2})[:\-/](\d{1,2})(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?", s)
    if not m:
        return None
    try:
        dt = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        iso = dt.strftime("%Y-%m-%d")
        human = dt.strftime("%b %d, %Y").replace(" 0", " ")
        time_part = f" {m.group(4)}" if m.group(4) else ""
        return iso, human, f"{iso}{time_part}"
    except ValueError:
        return None


def _extract_image_exif(raw_img: Image.Image) -> dict[str, str]:
    """Safely extract Make, Model, and capture DateTimeOriginal/Digitized/ModifyDate from Pillow EXIF."""
    exif_meta: dict[str, str] = {}
    try:
        exif = raw_img.getexif()
        if not exif:
            return exif_meta
        make = str(exif.get(271) or "").strip()
        model = str(exif.get(272) or "").strip()
        if make or model:
            if make and model and make.lower() in model.lower():
                exif_meta["camera"] = model
            elif make and model:
                exif_meta["camera"] = f"{make} {model}"
            else:
                exif_meta["camera"] = model or make

        exif_ifd: Any = {}
        try:
            exif_ifd = exif.get_ifd(0x8769) or {}
        except Exception:
            exif_ifd = {}

        raw_dt = (
            exif_ifd.get(36867)  # DateTimeOriginal
            or exif.get(36867)
            or exif_ifd.get(36868)  # DateTimeDigitized
            or exif.get(36868)
            or exif.get(306)  # ModifyDate
        )
        parsed_dt = _parse_exif_datetime(raw_dt)
        if parsed_dt:
            iso, human, full_dt = parsed_dt
            exif_meta["takenDate"] = iso
            exif_meta["takenDisplayDate"] = human
            exif_meta["date"] = full_dt
        elif raw_dt:
            exif_meta["date"] = str(raw_dt)
    except Exception:
        pass
    return exif_meta


def _extract_date_from_name_or_stat(
    filepath: Path,
    stat_mtime: float,
    text_hint: str = "",
    exif_date: str = "",
) -> tuple[str, str]:
    """Extract YYYY-MM-DD and human readable date from EXIF capture time, filename, content, or mtime."""
    if exif_date:
        parsed_exif = _parse_exif_datetime(exif_date)
        if parsed_exif:
            return parsed_exif[0], parsed_exif[1]

    name = filepath.name
    m = re.search(r"((?:19|20)\d{2})[-_]?([01]\d)[-_]?([0-3]\d)", name)
    if not m and text_hint:
        m = re.search(r"((?:19|20)\d{2})[-/]([01]\d)[-/]([0-3]\d)", text_hint)
    if m:
        try:
            dt = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return dt.strftime("%Y-%m-%d"), dt.strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            pass

    dt = datetime.datetime.fromtimestamp(stat_mtime)
    iso = dt.strftime("%Y-%m-%d")
    human = dt.strftime("%b %d, %Y").replace(" 0", " ")
    return iso, human


def _classify_specimen_and_visual_tags(
    modality: str,
    ext: str,
    name: str,
    text: str,
    ocr_text: str,
    width: int,
    height: int,
    has_drawings: bool = False,
) -> tuple[str, list[str]]:
    combined = f"{name} {text} {ocr_text}".lower()
    words = set(re.findall(r"[a-z0-9_]+", combined))
    tags: list[str] = []

    if modality == "code":
        tags.extend(["code", "script", ext.lstrip(".")])
        return "code", tags

    receipt_words = {
        "receipt", "invoice", "subtotal", "tax", "vat", "payment",
        "paid", "merchant", "cashier", "change", "order", "billing",
    }
    receipt_hits = len(words & receipt_words)
    has_currency = bool(re.search(r"(?:php|usd|eur|gbp|₱|\$|€|£)\s*\d[\d,.]*", combined))
    if "official receipt" in combined or "tax invoice" in combined or receipt_hits >= 3 or (has_currency and receipt_hits >= 1):
        tags.extend(["receipt", "invoice", "payment", "financial"])
        if width > 0 and height > width:
            tags.append("portrait receipt")
        return "receipt", tags

    idcard_phrases = ("student id", "id card", "registration form", "student registration", "enrollment card", "driver license", "passport")
    idcard_words = {"enrollment", "registration", "student", "validated", "semester", "transcript", "applicant"}
    if any(p in combined for p in idcard_phrases) or (modality == "image" and len(words & idcard_words) >= 3):
        tags.extend(["id card", "registration", "form", "document"])
        return "idcard", tags

    diagram_words = {"diagram", "schematic", "flowchart", "architecture", "pipeline", "figure", "chart", "graph", "workflow"}
    diagram_hits = len(words & diagram_words)
    if (has_drawings and diagram_hits >= 1) or diagram_hits >= 2:
        tags.extend(["diagram", "schematic", "chart", "visual"])
        return "diagram", tags

    if modality == "image":
        lower_name = name.lower()
        if any(tok in lower_name for tok in ("screenshot", "screen_shot", "screen shot", "screencap", "snip")):
            tags.append("screenshot")
        if width > height * 1.2:
            tags.append("landscape image")
        elif height > width * 1.2:
            tags.append("portrait image")
        else:
            tags.append("square image")

    if any(p in combined for p in ("boarding pass", "flight", "itinerary", "e-ticket", "booking confirmation", "airline")):
        tags.append("travel")
    if any(p in combined for p in ("certificate", "diploma", "transcript", "awarded to")):
        tags.append("certificate")
    if ext in SPREADSHEET_EXTS:
        tags.extend(["spreadsheet", "table"])
    if ext in {".ppt", ".pptx", ".key"}:
        tags.extend(["presentation", "slides"])

    return "text", tags


def _derive_human_title(
    filepath: Path,
    modality: str,
    headings: list[str],
    code_symbols: list[str],
    extracted_text: str,
    ocr_text: str,
) -> str:
    """Generate a clean, lowercase editorial title from real file contents."""
    if headings:
        skip_headings = {"table of contents", "contents", "abstract", "untitled", "page 1", "cover"}
        for h in headings:
            clean_h = re.sub(r"\s+", " ", h).strip()
            if 5 <= len(clean_h) <= 78 and clean_h.lower() not in skip_headings:
                return clean_h.lower()

    if modality == "code" and code_symbols:
        sym = code_symbols[0].replace("_", " ")
        return f"{sym} ({filepath.suffix.lstrip('.')} script)".lower()

    if ocr_text:
        parts = [p.strip() for p in re.split(r"[·\n|]", ocr_text) if len(p.strip()) >= 4]
        if parts:
            top = " · ".join(parts[:2])
            if len(top) > 68:
                top = top[:65] + "…"
            return top.lower()

    if extracted_text:
        first_line = next((ln.strip() for ln in extracted_text.splitlines() if len(ln.strip()) >= 5), "")
        if first_line and not first_line.startswith(("import ", "from ", "#!", "{", "[", "<", "/*")):
            cleaned = re.sub(r"^[#*>\-\d.]+\s*", "", first_line)
            if 5 <= len(cleaned) <= 75:
                return cleaned.lower()

    stem = filepath.stem
    stem = re.sub(r"^(IMG|DSC|Screenshot|screen|scan)[-_]?\d+[-_]?\d*", "", stem, flags=re.IGNORECASE).strip("_- ")
    if not stem:
        stem = filepath.stem
    return re.sub(r"[-_]+", " ", stem).strip().lower()


def _make_thumbnail_b64(img: Image.Image, max_side: int = 320) -> str:
    thumb = img.copy()
    thumb.thumbnail((max_side, max_side), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    thumb.convert("RGB").save(buf, format="JPEG", quality=80)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _parse_html_clean(raw_html: str) -> tuple[str, list[str]]:
    """Strip <style> and <script> boilerplate from HTML files so actual visible text and titles are indexed."""
    headings: list[str] = []
    title_m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    if title_m:
        t_str = re.sub(r"\s+", " ", title_m.group(1)).strip()
        if t_str:
            headings.append(t_str)

    for hm in re.finditer(r"<h[1-3][^>]*>(.*?)</h[1-3]>", raw_html, flags=re.IGNORECASE | re.DOTALL):
        h_clean = re.sub(r"<[^>]+>", " ", hm.group(1))
        h_clean = re.sub(r"\s+", " ", h_clean).strip()
        if 4 <= len(h_clean) <= 85 and h_clean not in headings:
            headings.append(h_clean)
            if len(headings) >= 5:
                break

    cleaned = re.sub(r"<(style|script|svg|head)[^>]*>.*?</\1>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"&(?:nbsp|amp|lt|gt|quot|#39|mdash|ndash|middot);", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:6000], headings


def _format_duration(seconds: float) -> str:
    secs = max(0, int(round(seconds)))
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def _probe_media(filepath: Path) -> dict[str, Any]:
    """Extract duration, resolution, and codec from video/audio using ffprobe when available."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            **_subprocess_win_kwargs(),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        import json as _json

        info = _json.loads(proc.stdout)
        fmt = info.get("format", {})
        dur = float(fmt.get("duration") or 0.0)
        bitrate = int(fmt.get("bit_rate") or 0)
        v_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        a_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
        out: dict[str, Any] = {
            "durationSec": round(dur, 1),
            "durationLabel": _format_duration(dur) if dur > 0 else "",
            "bitrateKbps": round(bitrate / 1000) if bitrate > 0 else 0,
        }
        if v_stream:
            out["width"] = int(v_stream.get("width") or 0)
            out["height"] = int(v_stream.get("height") or 0)
            out["codec"] = str(v_stream.get("codec_name") or "")
        elif a_stream:
            out["codec"] = str(a_stream.get("codec_name") or "")
        return out
    except Exception:
        return {}


def _extract_video_thumbnail(filepath: Path, duration_sec: float = 0.0) -> Optional[Image.Image]:
    """Extract a representative frame from a video file using ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    seek_sec = min(3.0, max(0.5, duration_sec * 0.15)) if duration_sec > 1.0 else 0.2
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss", f"{seek_sec:.2f}",
                    "-i", str(filepath),
                    "-frames:v", "1",
                    "-q:v", "4",
                    str(tmp_path),
                ],
                capture_output=True,
                timeout=8,
                **_subprocess_win_kwargs(),
            )
            if proc.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
                img = Image.open(tmp_path)
                img.load()
                return img.convert("RGB")
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def _safe_zip_read(zf: Any, member_name: str, max_bytes: int = 8 * 1024 * 1024) -> Optional[bytes]:
    """Read a ZIP member with a strict uncompressed byte limit to guard against zip bombs."""
    try:
        info = zf.getinfo(member_name)
        if info.file_size > max_bytes:
            return None
        with zf.open(info, "r") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        return data
    except Exception:
        return None


def _parse_pptx_preview(filepath: Path) -> tuple[str, list[str], dict[str, Any]]:
    """Extract slide text, titles, and structured slide cards from a .pptx archive."""
    import xml.etree.ElementTree as ET
    import zipfile

    slides_text: list[str] = []
    slides_cards: list[dict[str, Any]] = []
    headings: list[str] = []
    slide_count = 0
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            slide_names = sorted(
                (n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
                key=lambda s: int(re.search(r"slide(\d+)\.xml$", s).group(1)) if re.search(r"slide(\d+)\.xml$", s) else 0,
            )
            slide_count = len(slide_names)
            for idx, sname in enumerate(slide_names[:50], start=1):
                raw_xml = _safe_zip_read(zf, sname)
                if not raw_xml:
                    continue
                root = ET.fromstring(raw_xml)
                texts = [elem.text.strip() for elem in root.iter() if elem.tag.endswith("}t") and elem.text and elem.text.strip()]
                if texts:
                    title = texts[0][:90]
                    body = " ".join(texts[1:]) if len(texts) > 1 else texts[0]
                    if len(headings) < 8:
                        headings.append(f"Slide {idx}: {title[:70]}")
                    slides_text.append(f"[Slide {idx}] " + " ".join(texts))
                    slides_cards.append({"index": idx, "title": title, "text": body[:600]})
    except Exception:
        pass
    text = "\n".join(slides_text).strip()
    return text, headings, {"kind": "pptx", "slideCount": slide_count, "slides": slides_cards, "wordCount": len(text.split())}


def _parse_archive_preview(filepath: Path, ext: str) -> tuple[str, list[str], dict[str, Any]]:
    """List archive members and sizes without extracting to disk."""
    import tarfile
    import zipfile

    entries: list[dict[str, Any]] = []
    total_uncompressed = 0
    total_count = 0
    try:
        if ext == ".zip":
            with zipfile.ZipFile(filepath, "r") as zf:
                infos = zf.infolist()
                total_count = len(infos)
                for info in infos[:180]:
                    total_uncompressed += int(info.file_size or 0)
                    entries.append({
                        "name": info.filename,
                        "sizeBytes": int(info.file_size or 0),
                        "isDir": info.is_dir(),
                        "ext": Path(info.filename).suffix.lstrip(".").lower(),
                    })
        elif ext in {".tar", ".tgz"} or filepath.name.lower().endswith(".tar.gz"):
            with tarfile.open(filepath, "r:*") as tf:
                members = tf.getmembers()
                total_count = len(members)
                for m in members[:180]:
                    total_uncompressed += int(m.size or 0)
                    entries.append({
                        "name": m.name,
                        "sizeBytes": int(m.size or 0),
                        "isDir": m.isdir(),
                        "ext": Path(m.name).suffix.lstrip(".").lower(),
                    })
    except Exception:
        pass

    names_text = "\n".join(e["name"] for e in entries if not e["isDir"])[:6000]
    headings = [f"Archive: {total_count} entries"] if total_count else []
    meta = {
        "kind": "archive",
        "format": ext.lstrip("."),
        "totalEntries": total_count,
        "totalSizeBytes": total_uncompressed,
        "entries": entries,
    }
    return names_text or filepath.stem, headings, meta


def _extract_receipt_fields(combined: str) -> dict[str, str]:
    """Extract structured receipt metadata (merchant, total, date, reference) when present."""
    fields: dict[str, str] = {}
    if not combined:
        return fields
    m_to = re.search(
        r"paid\s+(?:php|₱|\$|€|£)?\s*[\d,.]+\s+to\s+([A-Za-z][A-Za-z0-9 &'.]{2,36}?)(?=\s*(?:[·|\n]|date|ref|item|$))",
        combined,
        re.IGNORECASE,
    )
    if m_to:
        fields["merchant"] = m_to.group(1).strip(" ·-.")
    else:
        for seg in re.split(r"[·|\n]", combined)[:3]:
            s = seg.strip(" ·-.")
            low = s.lower()
            if 4 <= len(s) <= 44 and any(c.isalpha() for c in s) and not any(
                low.startswith(p) for p in ("paid ", "date:", "ref ", "total", "subtotal", "tax", "vat", "official receipt")
            ):
                fields["merchant"] = s
                break
    m_total = re.search(r"(?:total|amount|paid|subtotal)[:\s]*((?:PHP|USD|EUR|GBP|JPY|₱|\$|€|£|¥)\s?[\d,.]+)", combined, re.IGNORECASE)
    if not m_total:
        m_total = re.search(r"((?:PHP|USD|EUR|GBP|JPY|₱|\$|€|£|¥)\s?[\d,.]+)", combined, re.IGNORECASE)
    if m_total:
        fields["total"] = m_total.group(1).strip()
    m_date = re.search(r"(20\d{2}[-/][01]\d[-/][0-3]\d|[A-Za-z]{3,9}\s+\d{1,2},?\s+20\d{2})", combined)
    if m_date:
        fields["date"] = m_date.group(1).strip()
    m_ref = re.search(r"(?:ref(?:erence)?|or|inv(?:oice)?|receipt)\s*(?:#|no\.?|number|:)?\s*([A-Z0-9-]{4,24})", combined, re.IGNORECASE)
    if m_ref:
        fields["reference"] = m_ref.group(1).strip()
    return fields


def _parse_odt_preview(filepath: Path) -> tuple[str, list[str], dict[str, Any]]:
    """Extract plain text and headings from an OpenDocument .odt archive."""
    import xml.etree.ElementTree as ET
    import zipfile

    paras: list[str] = []
    headings: list[str] = []
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            if "content.xml" in zf.namelist():
                raw_xml = _safe_zip_read(zf, "content.xml")
                if raw_xml:
                    root = ET.fromstring(raw_xml)
                    for elem in root.iter():
                        tag = elem.tag.split("}")[-1]
                        if tag in ("h", "p"):
                            txt = "".join(elem.itertext()).strip()
                            if not txt:
                                continue
                            if tag == "h" and len(headings) < 8:
                                headings.append(txt[:80])
                            paras.append(txt)
    except Exception:
        pass
    text = "\n".join(paras).strip()
    return text, headings, {"kind": "odt", "wordCount": len(text.split())}


_PUA_BULLETS = {
    "\uf0b7", "\uf0a7", "\uf0d8", "\uf0fc", "\uf076", "\uf0a8", "\uf0e0",
    "\uf0b0", "\uf09f", "\uf06e", "\uf071", "\uf0fe", "\uf02d", "\uf03e",
}


def clean_extracted_text(text: str, preserve_newlines: bool = True) -> str:
    """
    Sanitize extracted document/OCR text for readable previews and citations:
    - Normalizes Unicode compatibility ligatures (e.g. fi, fl, ff) via NFKC
    - Replaces Private-Use-Area / Wingdings symbol bullets with clean '•'
    - Strips Unicode replacement characters (U+FFFD), zero-width/soft-hyphen/BOM junk, and control bytes
    - Cleans standalone '?' glyph-substitution artifacts and rejoined broken PDF hyphen wraps
    """
    if not text:
        return ""

    s = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip zero-width spaces, BOM, soft hyphens, and non-printable control chars (keep \n and \t)
    s = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]", "", s)
    s = "".join(ch for ch in s if ch in ("\n", "\t") or ord(ch) >= 32)
    s = s.replace("\ufffd", " ")

    # Normalize PUA / Symbol font glyphs (U+E000..U+F8FF) -> bullet if known or line-leading, else drop
    out_chars: list[str] = []
    for idx, ch in enumerate(s):
        code = ord(ch)
        if 0xE000 <= code <= 0xF8FF:
            at_line_start = idx == 0 or s[idx - 1] in ("\n", " ", "\t")
            if ch in _PUA_BULLETS or (at_line_start and (idx + 1 < len(s) and s[idx + 1] == " ")):
                out_chars.append("•")
            else:
                out_chars.append(" ")
        else:
            out_chars.append(ch)
    s = "".join(out_chars)

    # Expand ligatures & compatibility forms (ﬁ -> fi, ﬂ -> fl, fullwidth punctuation, etc.)
    s = unicodedata.normalize("NFKC", s)

    # Clean broken PDF line-wrap hyphens between lowercase/alphabetic fragments
    if preserve_newlines:
        s = re.sub(r"([a-z]{2,})-\n([a-z]{2,})", r"\1\2", s)

    # Clean standalone '?' artifacts from broken PDF Type3 / RTF glyph fallbacks:
    # 1) Line-leading '? ' (e.g. broken bullet "? Item") -> "• Item"
    s = re.sub(r"(?m)^[ \t]*\?[ \t]+(?=[A-Za-z0-9(\"'“])", "• ", s)
    # 2) Isolated '?' between letters/words like "John?s" or "2020 ? 2025" or "section ? 3"
    s = re.sub(r"(?<=[a-z])\?(?=s\b)", "'", s)
    s = re.sub(r"(?<=\s)\?(?=\s)", " ", s)

    if preserve_newlines:
        clean_lines: list[str] = []
        blank_run = 0
        for ln in s.splitlines():
            ln_clean = re.sub(r"[ \t]{2,}", " ", ln).strip()
            if not ln_clean:
                blank_run += 1
                if blank_run <= 1 and clean_lines:
                    clean_lines.append("")
            else:
                blank_run = 0
                clean_lines.append(ln_clean)
        return "\n".join(clean_lines).strip()

    return re.sub(r"\s+", " ", s).strip()


def _parse_rtf_text(raw: str) -> str:
    """Decode RTF Unicode/cp1252 escapes and strip control groups into clean readable plain text."""
    # First remove invisible header destinations ({\fonttbl...}, {\colortbl...}, {\*\...}, etc.)
    cleaned = re.sub(r"\{\\(?:fonttbl|colortbl|stylesheet|info|pict|object|\*\\[^}]+)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", " ", raw)

    # Decode \uN followed by its 1-char (or \'xx) ANSI fallback (commonly '?') BEFORE stripping other control words
    def _decode_rtf_unicode(match: re.Match[str]) -> str:
        val = int(match.group(1))
        if val < 0:
            val += 65536
        if 32 <= val <= 0x10FFFF:
            return chr(val)
        return " "

    cleaned = re.sub(r"\\u(-?\d+)(?:\\'[0-9a-fA-F]{2}|[^\\\s{}])?", _decode_rtf_unicode, cleaned)

    # Decode RTF hex escapes (\'93 -> “, \'94 -> ”, etc.) via cp1252
    def _decode_rtf_hex(match: re.Match[str]) -> str:
        try:
            return bytes.fromhex(match.group(1)).decode("cp1252", errors="replace")
        except Exception:
            return " "

    cleaned = re.sub(r"\\'([0-9a-fA-F]{2})", _decode_rtf_hex, cleaned)
    cleaned = re.sub(r"\\par\b|\\line\b", "\n", cleaned)
    cleaned = re.sub(r"\\tab\b", "\t", cleaned)
    cleaned = re.sub(r"\\emdash\b", "—", cleaned)
    cleaned = re.sub(r"\\endash\b", "–", cleaned)
    cleaned = re.sub(r"\\lquote\b", "‘", cleaned)
    cleaned = re.sub(r"\\rquote\b", "’", cleaned)
    cleaned = re.sub(r"\\ldblquote\b", "“", cleaned)
    cleaned = re.sub(r"\\rdblquote\b", "”", cleaned)
    cleaned = re.sub(r"\\bullet\b", "•", cleaned)
    cleaned = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", cleaned)
    cleaned = cleaned.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    cleaned = cleaned.replace("{", "").replace("}", "")
    return clean_extracted_text(cleaned, preserve_newlines=True)


def _parse_spreadsheet_preview(filepath: Path, ext: str) -> tuple[str, list[str], dict[str, Any]]:
    """Parse CSV/TSV/XLSX into searchable text and a structured table preview."""
    headings: list[str] = []
    meta: dict[str, Any] = {}
    if ext in {".csv", ".tsv"}:
        raw = filepath.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        delim = "\t" if ext == ".tsv" else ","
        rows = list(csv.reader(lines[:25], delimiter=delim))
        headers = [str(c).strip() for c in rows[0][:10]] if rows else []
        body_rows = [[str(c).strip() for c in r[:10]] for r in rows[1:16]]
        if headers:
            headings.append("Table: " + ", ".join(h for h in headers[:6] if h))
        meta = {
            "sheets": ["Sheet1"],
            "activeSheet": "Sheet1",
            "headers": headers,
            "rows": body_rows,
            "totalRows": max(0, len(lines) - 1),
            "totalCols": len(rows[0]) if rows else 0,
        }
        return "\n".join(lines[:60]), headings, meta

    if ext in {".xlsx", ".xlsm"} and openpyxl is not None:
        wb = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)
        sheets_data: dict[str, dict[str, Any]] = {}
        try:
            sheet_names = list(wb.sheetnames)
            for s_name in sheet_names[:6]:
                ws = wb[s_name]
                rows_data: list[list[str]] = []
                s_text_lines: list[str] = []
                total_rows = 0
                total_cols = 0
                for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                    total_rows += 1
                    vals = ["" if v is None else str(v).strip() for v in row]
                    if any(vals):
                        total_cols = max(total_cols, len(vals))
                        if r_idx < 40:
                            rows_data.append(vals[:12])
                        if s_name == sheet_names[0] and r_idx < 60:
                            s_text_lines.append(" | ".join(v for v in vals[:10] if v))
                    if r_idx >= 250:
                        break
                s_headers = rows_data[0] if rows_data else []
                s_body = rows_data[1:35] if len(rows_data) > 1 else []
                sheets_data[s_name] = {
                    "headers": s_headers,
                    "rows": s_body,
                    "totalRows": max(0, total_rows - 1),
                    "totalCols": total_cols,
                }
                if s_name == sheet_names[0]:
                    text_lines = s_text_lines
        finally:
            wb.close()
        first_name = sheet_names[0] if sheet_names else "Sheet1"
        first_sheet = sheets_data.get(first_name, {"headers": [], "rows": [], "totalRows": 0, "totalCols": 0})
        headers = first_sheet["headers"]
        body_rows = first_sheet["rows"]
        if headers:
            headings.append("Spreadsheet: " + ", ".join(h for h in headers[:6] if h))
        meta = {
            "sheets": sheet_names[:8],
            "activeSheet": first_name,
            "headers": headers,
            "rows": body_rows,
            "totalRows": first_sheet["totalRows"],
            "totalCols": first_sheet["totalCols"],
            "sheetsData": sheets_data,
        }
        return "\n".join(text_lines), headings, meta

    if ext == ".xls":
        try:
            import xlrd  # type: ignore

            book = xlrd.open_workbook(str(filepath))
            sheet_names = list(book.sheet_names())
            sheet = book.sheet_by_index(0) if sheet_names else None
            rows_data: list[list[str]] = []
            text_lines: list[str] = []
            if sheet is not None:
                for r_idx in range(min(sheet.nrows, 250)):
                    vals = [str(sheet.cell_value(r_idx, c)).strip() for c in range(min(sheet.ncols, 10))]
                    if any(vals):
                        if r_idx < 25:
                            rows_data.append(vals)
                        if r_idx < 60:
                            text_lines.append(" | ".join(v for v in vals if v))
            headers = rows_data[0] if rows_data else []
            body_rows = rows_data[1:16] if len(rows_data) > 1 else []
            meta = {
                "sheets": sheet_names[:8],
                "activeSheet": sheet_names[0] if sheet_names else "Sheet1",
                "headers": headers,
                "rows": body_rows,
                "totalRows": max(0, (sheet.nrows - 1) if sheet else 0),
                "totalCols": sheet.ncols if sheet else 0,
            }
            return "\n".join(text_lines), headings, meta
        except Exception:
            raw = filepath.read_bytes()[:262144]
            strings = re.findall(rb"[A-Za-z0-9][A-Za-z0-9 _.,:/-]{3,80}", raw)
            decoded = [s.decode("ascii", errors="ignore").strip() for s in strings[:80]]
            return " ".join(decoded), headings, {"kind": "spreadsheet", "format": "xls"}

    return "", headings, meta


def parse_file(
    filepath: Path,
    enable_ocr: bool = True,
    enable_code: bool = True,
    precomputed_sha256: Optional[str] = None,
    precomputed_stat: Optional[os.stat_result] = None,
) -> Optional[ParsedFile]:
    """Parse any local file into structured text, OCR, chunks, and visual representation."""
    filepath = Path(filepath).resolve()

    ext = filepath.suffix.lower()
    if ext in CODE_EXTS and not enable_code:
        return None
    all_supported = IMAGE_EXTS | DOC_EXTS | CODE_EXTS | VIDEO_EXTS | AUDIO_EXTS | SPREADSHEET_EXTS | ARCHIVE_EXTS
    if ext not in all_supported:
        return None

    try:
        if precomputed_stat is not None:
            if not stat_mod.S_ISREG(precomputed_stat.st_mode):
                return None
            stat = precomputed_stat
        else:
            if not filepath.exists() or not filepath.is_file():
                return None
            stat = filepath.stat()
        size_bytes = stat.st_size
        if size_bytes == 0 or size_bytes > 500 * 1024 * 1024:
            return None
        # Skip tiny < 16-byte scratch text files
        if ext in {".txt", ".md", ".log"} and size_bytes < 16:
            return None

        sha256 = precomputed_sha256 or compute_sha256(filepath)
    except (OSError, PermissionError):
        return None
    if ext in IMAGE_EXTS:
        modality = "image"
    elif ext in VIDEO_EXTS:
        modality = "video"
    elif ext in AUDIO_EXTS:
        modality = "audio"
    elif ext in ARCHIVE_EXTS:
        modality = "archive"
    elif ext in CODE_EXTS:
        modality = "code"
    else:
        modality = "document"

    extracted_text = ""
    ocr_text = ""
    ocr_conf = 0.0
    headings: list[str] = []
    code_symbols: list[str] = []
    width = 0
    height = 0
    page_count = 1
    dhash = ""
    has_drawings = False
    thumb_b64: Optional[str] = None
    pil_img: Optional[Image.Image] = None
    pdf_rendered_img: Optional[Image.Image] = None
    extra_meta: dict[str, Any] = {}

    try:
        if ext == ".pdf":
            with _FITZ_LOCK:
                doc = fitz.open(str(filepath))
                try:
                    page_count = len(doc)
                    text_blocks: list[str] = []
                    if page_count > 0:
                        p0 = doc[0]
                        if len(p0.get_drawings()) > 5:
                            has_drawings = True
                        page0_lines: list[str] = []
                        blocks_dict = p0.get_text("dict").get("blocks", [])
                        for b in blocks_dict:
                            for ln in b.get("lines", []):
                                spans = ln.get("spans", [])
                                if spans:
                                    line_txt = "".join(s.get("text", "") for s in spans).strip()
                                    if line_txt:
                                        page0_lines.append(line_txt)
                                    max_sz = max((s.get("size", 0.0) for s in spans), default=0.0)
                                    if max_sz >= 13.5 and 5 <= len(line_txt) <= 90:
                                        headings.append(line_txt)
                        if page0_lines:
                            text_blocks.append("\n".join(page0_lines))

                        for idx in range(1, min(page_count, 12)):
                            p_text = doc[idx].get_text("text").strip()
                            if p_text:
                                text_blocks.append(p_text)

                        extracted_text = "\n".join(text_blocks).strip()
                        need_hi_res = len(extracted_text) < 80 or has_drawings
                        mat = fitz.Matrix(1.0, 1.0) if need_hi_res else fitz.Matrix(0.5, 0.5)
                        pix = p0.get_pixmap(matrix=mat, alpha=False)
                        pdf_rendered_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                        width, height = pdf_rendered_img.size
                        dhash = compute_dhash(pdf_rendered_img)
                        thumb_b64 = _make_thumbnail_b64(pdf_rendered_img)
                        if need_hi_res:
                            pil_img = pdf_rendered_img
                finally:
                    doc.close()

            if len(extracted_text) < 20 and enable_ocr and pil_img is not None:
                ocr_text, ocr_conf = run_ocr_on_image(pil_img)
                if ocr_text:
                    extracted_text = f"{extracted_text}\n{ocr_text}".strip() if extracted_text else ocr_text
            extra_meta = {
                "kind": "pdf",
                "pageCount": page_count,
                "wordCount": len(extracted_text.split()),
            }

        elif ext == ".docx" and docx is not None:
            document = docx.Document(str(filepath))
            paras: list[str] = []
            for p in document.paragraphs:
                txt = p.text.strip()
                if not txt:
                    continue
                style_name = (p.style.name if p.style else "").lower()
                if "heading" in style_name or "title" in style_name or (len(headings) == 0 and len(txt) < 85):
                    headings.append(txt)
                paras.append(txt)
            for table in document.tables:
                for row in table.rows:
                    row_txt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                    if row_txt:
                        paras.append(row_txt)
            extracted_text = "\n".join(paras).strip()
            extra_meta = {"kind": "docx", "wordCount": len(extracted_text.split())}

        elif ext == ".pptx":
            extracted_text, pptx_headings, extra_meta = _parse_pptx_preview(filepath)
            headings.extend(pptx_headings)

        elif ext == ".odt":
            extracted_text, odt_headings, extra_meta = _parse_odt_preview(filepath)
            headings.extend(odt_headings)

        elif ext == ".rtf":
            raw_rtf = filepath.read_text(encoding="utf-8", errors="replace")
            extracted_text = _parse_rtf_text(raw_rtf)
            extra_meta = {"kind": "rtf", "wordCount": len(extracted_text.split())}

        elif ext in ARCHIVE_EXTS:
            extracted_text, arch_headings, extra_meta = _parse_archive_preview(filepath, ext)
            headings.extend(arch_headings)

        elif ext in SPREADSHEET_EXTS:
            extracted_text, sheet_headings, sheet_meta = _parse_spreadsheet_preview(filepath, ext)
            headings.extend(sheet_headings)
            extra_meta = {"kind": "spreadsheet", **sheet_meta}

        elif ext in VIDEO_EXTS:
            media_info = _probe_media(filepath)
            width = int(media_info.get("width", 0))
            height = int(media_info.get("height", 0))
            pil_img = _extract_video_thumbnail(filepath, float(media_info.get("durationSec", 0.0)))
            if pil_img is not None:
                if width == 0 or height == 0:
                    width, height = pil_img.size
                dhash = compute_dhash(pil_img)
                thumb_b64 = _make_thumbnail_b64(pil_img)
            dur_lbl = media_info.get("durationLabel", "")
            codec = media_info.get("codec", "")
            res_lbl = f"{width}x{height}" if width and height else ""
            extracted_text = " · ".join(p for p in [filepath.stem.replace("_", " ").replace("-", " "), res_lbl, dur_lbl, codec] if p)
            extra_meta = {"kind": "video", **media_info}

        elif ext in AUDIO_EXTS:
            media_info = _probe_media(filepath)
            dur_lbl = media_info.get("durationLabel", "")
            codec = media_info.get("codec", "")
            extracted_text = " · ".join(p for p in [filepath.stem.replace("_", " ").replace("-", " "), dur_lbl, codec] if p)
            extra_meta = {"kind": "audio", **media_info}

        elif ext in IMAGE_EXTS:
            if ext == ".svg":
                raw_svg = filepath.read_text(encoding="utf-8", errors="replace")
                extracted_text, svg_headings = _parse_html_clean(raw_svg[:60000])
                headings.extend(svg_headings)
                extra_meta = {"kind": "image", "format": "svg", "rawSnippet": raw_svg[:40000]}
            else:
                with Image.open(filepath) as raw_img:
                    transposed = ImageOps.exif_transpose(raw_img)
                    exif_meta = _extract_image_exif(raw_img)
                    pil_img = transposed.convert("RGB")
                width, height = pil_img.size
                dhash = compute_dhash(pil_img)
                thumb_b64 = _make_thumbnail_b64(pil_img)
                if enable_ocr:
                    ocr_text, ocr_conf = run_ocr_on_image(pil_img)
                    extracted_text = ocr_text
                extra_meta = {"kind": "image", "width": width, "height": height, **exif_meta}

        elif ext in {".html", ".htm", ".php"}:
            raw = filepath.read_text(encoding="utf-8", errors="replace")
            extracted_text, html_headings = _parse_html_clean(raw[:120000])
            headings.extend(html_headings)
            for fn_match in re.finditer(r"function\s+([a-zA-Z_][a-zA-Z0-9_]*)", raw[:40000]):
                sym = fn_match.group(1)
                if sym not in code_symbols:
                    code_symbols.append(sym)
            if not extracted_text:
                extracted_text = raw[:8000].strip()
            extra_meta = {
                "kind": "code",
                "language": ext.lstrip("."),
                "lineCount": raw.count("\n") + 1,
                "rawSnippet": raw[:12000],
            }

        else:
            raw = filepath.read_text(encoding="utf-8", errors="replace")
            extracted_text = raw[:16000].strip()
            if modality == "code":
                doc_match = re.search(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', extracted_text, re.DOTALL)
                if doc_match:
                    doc_str = (doc_match.group(1) or doc_match.group(2) or "").strip().splitlines()[0]
                    if doc_str:
                        headings.append(doc_str.strip())
                for fn_match in re.finditer(
                    r"(?:def|fn|function|class|struct|interface)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
                    extracted_text,
                ):
                    sym = fn_match.group(1)
                    if sym not in {"main", "__init__", "new"} and sym not in code_symbols:
                        code_symbols.append(sym)
                extra_meta = {
                    "kind": "code",
                    "language": ext.lstrip("."),
                    "lineCount": raw.count("\n") + 1,
                    "rawSnippet": raw[:16000],
                }
            else:
                for ln in extracted_text.splitlines()[:10]:
                    s = ln.strip()
                    if s.startswith("#"):
                        headings.append(s.lstrip("# ").strip())
                extra_meta = {
                    "kind": "markdown" if ext in {".md", ".markdown"} else "text",
                    "lineCount": raw.count("\n") + 1,
                    "rawSnippet": raw[:20000],
                }

    except Exception as exc:
        extra_meta = {"kind": modality, "parseError": str(exc)[:240]}
        extracted_text = filepath.stem.replace("_", " ").replace("-", " ").strip()

    if modality != "code" and ext not in {".html", ".htm", ".php"}:
        extracted_text = clean_extracted_text(extracted_text, preserve_newlines=True)
    elif extracted_text:
        extracted_text = extracted_text.replace("\ufffd", " ")
    if ocr_text:
        ocr_text = clean_extracted_text(ocr_text, preserve_newlines=True)

    created_date, display_date = _extract_date_from_name_or_stat(
        filepath,
        stat.st_mtime,
        f"{extracted_text[:400]} {ocr_text[:400]}",
        exif_date=str(extra_meta.get("takenDate") or extra_meta.get("date") or ""),
    )
    specimen_type, visual_tags = _classify_specimen_and_visual_tags(
        modality, ext, filepath.name, extracted_text, ocr_text, width, height, has_drawings
    )
    if ext == ".pdf" and pil_img is None and pdf_rendered_img is not None and specimen_type in ("diagram", "receipt", "idcard"):
        pil_img = pdf_rendered_img
    if specimen_type == "receipt":
        rec_fields = _extract_receipt_fields(f"{ocr_text} {extracted_text}")
        if rec_fields:
            extra_meta["receiptFields"] = rec_fields
    title = _derive_human_title(filepath, modality, headings, code_symbols, extracted_text, ocr_text)
    chunks = chunk_text_tokens(extracted_text or ocr_text or title, max_tokens=512, overlap=64)

    return ParsedFile(
        path=filepath,
        name=filepath.name,
        ext=ext.lstrip(".") or "file",
        modality=modality,
        size_bytes=size_bytes,
        mtime=stat.st_mtime,
        created_date=created_date,
        display_date=display_date,
        sha256=sha256,
        title=title,
        extracted_text=extracted_text[:4000],
        ocr_text=ocr_text[:2000],
        ocr_confidence=ocr_conf,
        chunks=chunks,
        headings=headings[:5],
        code_symbols=code_symbols[:8],
        visual_tags=visual_tags,
        width=width,
        height=height,
        page_count=page_count,
        dhash=dhash,
        specimen_type=specimen_type,
        thumbnail_b64=thumb_b64,
        pil_image=pil_img,
        metadata={**extra_meta, "codeSymbols": code_symbols[:8], "headings": headings[:5]},
    )
