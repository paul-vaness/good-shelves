"""
Embedded SQLite + sqlite-vec Vector Store & Metadata Engine for Good Shelves.
Stores 512-dimensional L2-normalized vectors in a virtual `vec0` cosine index alongside
rich document/image/OCR metadata, rename history, folder tidy rollback logs, and user settings.
"""

from __future__ import annotations

import ast
import base64
import calendar
import concurrent.futures
import difflib
import hashlib
import itertools
import json
import operator
import os
import platform
import re
import shutil
import sqlite3
import stat as stat_mod
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import psutil
import sqlite_vec

from clustering import (
    _GENERAL_CATEGORIES,
    DiscoveredShelf,
    _classify_single_file_category,
    derive_specific_sub_shelf,
    run_shelf_clustering,
)
from deduplicator import find_lookalike_pairs
from devices import list_connected_devices
from embedder import MultimodalEmbedder
from vision_models import download_vision_model, get_catalog_status, resolve_default_vision_model, resolve_vision_model_paths
from parsers import (
    ARCHIVE_EXTS,
    AUDIO_EXTS,
    CODE_EXTS,
    DOC_EXTS,
    IMAGE_EXTS,
    SPREADSHEET_EXTS,
    VIDEO_EXTS,
    ParsedFile,
    _FITZ_LOCK,
    _parse_odt_preview,
    _parse_pptx_preview,
    _parse_rtf_text,
    _subprocess_win_kwargs,
    clean_extracted_text,
    compute_sha256,
    parse_file,
    run_ocr_on_image,
)
from renamer import format_rename_template, generate_rename_suggestion
from ai_agent import (
    build_deep_file_context,
    filter_files_by_scope,
    infer_learned_route,
    is_item_excluded_by_memory,
    load_ai_memory,
    make_plan_id,
    split_compound_commands,
    try_ollama_structured_synthesis,
    upsert_ai_memory,
)


def get_user_special_dir(kind: str) -> Path:
    """Resolve user Documents/Downloads/Desktop/Pictures across Linux, macOS, and Windows (including OneDrive redirection)."""
    home = Path.home()
    if platform.system() == "Windows":
        try:
            import winreg  # type: ignore

            reg_map = {
                "Documents": "Personal",
                "Desktop": "Desktop",
                "Pictures": "My Pictures",
                "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
            }
            val_name = reg_map.get(kind)
            if val_name:
                key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    raw, _ = winreg.QueryValueEx(key, val_name)
                    expanded = os.path.expandvars(str(raw))
                    p = Path(expanded)
                    if p.exists():
                        return p
        except Exception:
            pass
    return (home / kind).resolve()


def get_default_data_dir() -> Path:
    """Resolve platform-native application data directory (%APPDATA%\\good-shelves or ~/.local/share/good-shelves)."""
    env_override = os.environ.get("GOOD_SHELVES_DATA_DIR")
    if env_override:
        p = Path(env_override)
        p.mkdir(parents=True, exist_ok=True)
        return p

    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
        out = base / "good-shelves"
    elif system == "Darwin":
        out = Path.home() / "Library" / "Application Support" / "good-shelves"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
        out = base / "good-shelves"

    out.mkdir(parents=True, exist_ok=True)
    return out


def format_path_short(p: Path) -> str:
    """Format a path relative to ~ for clean cross-platform display."""
    try:
        rel = p.resolve().relative_to(Path.home().resolve())
        rel_str = str(rel).replace("\\", "/")
        return f"~/{rel_str}" if rel_str != "." else "~"
    except Exception:
        return str(p)


def format_bytes(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024.0 * 1024.0):.1f} MB"
    return f"{max(1, round(size_bytes / 1024.0))} KB"


def _unique_dest_path(dest_dir: Path, filename: str, src_path: Optional[Path] = None) -> Path:
    """Return a non-colliding path inside `dest_dir`, avoiding silent overwrites."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    if src_path is not None:
        try:
            if candidate.resolve() == src_path.resolve():
                return candidate
        except OSError:
            pass
    p = Path(filename)
    stem, suffix = p.stem, p.suffix
    counter = 1
    while candidate.exists():
        candidate = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def _stable_short_hash(text: str, length: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:length]


def _validate_mutable_folder(target: Path) -> None:
    """Prevent deleting or renaming filesystem roots, home directory, or system/special roots."""
    res = target.resolve()
    if res.parent == res:
        raise ValueError("Cannot modify filesystem root directory")
    protected = {
        Path.home().resolve(),
        get_default_data_dir().resolve(),
        get_user_special_dir("Documents").resolve(),
        get_user_special_dir("Downloads").resolve(),
        get_user_special_dir("Desktop").resolve(),
        get_user_special_dir("Pictures").resolve(),
    }
    if res in protected:
        raise ValueError(f"Cannot modify protected folder: {res.name}")
    low = str(res).lower()
    if platform.system() == "Windows":
        sys_root = os.environ.get("SystemRoot", "C:\\Windows").lower()
        if low == sys_root or low.startswith(sys_root + "\\"):
            raise ValueError("Cannot modify Windows system folder")
    else:
        if low in {"/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/sbin", "/sys", "/usr", "/var"}:
            raise ValueError("Cannot modify system directory")


def pack_f32(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).ravel().tobytes()


def unpack_f32(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


# ---------------------------------------------------------------------------
# Query understanding helpers: light stemming, small synonym groups, and
# natural-language cues for modality / recency / folder intent.
# ---------------------------------------------------------------------------

_SEARCH_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those", "into",
    "your", "you", "are", "was", "were", "have", "has", "had", "not", "but", "what",
    "all", "every", "only", "can", "could", "would", "should", "out", "use", "used",
    "using", "file", "files", "item", "items", "list", "everything", "anything",
    "find", "show", "tell", "give", "look", "looking", "search", "me", "my", "our",
    "about", "any", "some", "please", "get", "which", "who", "when", "where", "how",
    "many", "much", "there", "here", "one", "ones", "thing", "things", "stuff",
    "include", "includes", "including", "contain", "contains", "containing",
    "mention", "mentions", "mentioning", "having", "titled", "called", "named",
    "taken", "captured", "shot", "snap", "snapped", "created", "modified", "edited",
    "updated", "saved", "downloaded", "uploaded", "stored", "inside", "within",
    "last", "past", "week", "weeks", "month", "months", "year", "years", "day",
    "days", "ago", "today", "yesterday", "recent", "recently", "latest", "newest",
    "new", "oldest", "earliest", "old", "largest", "biggest", "smallest", "tiniest",
    "without", "excluding", "exclude", "except", "more", "than", "less", "over",
    "under", "larger", "bigger", "smaller", "between", "after", "before", "since",
    "during", "of", "in", "on", "at", "to", "is", "it", "as", "by", "be", "or",
    "an", "a", "do", "does", "did", "so", "if", "then", "up", "down",
}

_SYNONYM_GROUPS: tuple[set[str], ...] = (
    {"receipt", "invoice", "bill", "billing", "payment", "paid", "transaction", "purchase", "order", "gcash", "maya", "paymaya", "transfer", "remittance", "subtotal", "vat", "merchant"},
    {"utility", "electric", "electricity", "meralco", "water", "maynilad", "internet", "wifi", "pldt", "globe", "converge", "cable", "postpaid"},
    {"photo", "photos", "picture", "pictures", "pic", "pics", "image", "images", "screenshot", "screenshots", "screen", "scan", "scans", "capture", "snap", "snapshot"},
    {"paper", "article", "study", "research", "journal", "publication", "manuscript", "thesis", "capstone", "dissertation", "report", "abstract"},
    {"code", "script", "scripts", "program", "source", "function", "repository", "repo", "module", "snippet", "config", "stylesheet", "css", "html", "json", "python", "javascript", "typescript"},
    {"school", "college", "university", "student", "course", "enrollment", "registration", "transcript", "academic", "syllabus", "curriculum", "outline", "semester", "campus"},
    {"reviewer", "study", "notes", "exam", "quiz", "midterm", "finals", "homework", "assignment", "lab", "worksheet", "handout"},
    {"resume", "cv", "curriculum", "portfolio", "biodata", "application", "applicant", "cover"},
    {"tax", "taxation", "revenue", "bir", "statute", "legal", "law", "court", "supreme", "itr"},
    {"contract", "agreement", "nda", "lease", "deed", "warranty", "terms", "policy", "signed"},
    {"coffee", "cafe", "latte", "espresso", "roast", "barista", "roasters"},
    {"budget", "allowance", "expense", "expenses", "finance", "financial", "money", "cost", "price", "cash", "bdo", "bpi", "salary", "payslip", "payroll", "bank", "statement"},
    {"spreadsheet", "sheet", "sheets", "excel", "xlsx", "csv", "table", "workbook", "ledger", "tracker"},
    {"slide", "slides", "deck", "presentation", "pptx", "powerpoint", "keynote", "lecture", "chapter"},
    {"document", "doc", "docs", "docx", "word", "pdf", "letter", "memo", "proposal", "minutes", "agenda", "manual", "guide"},
    {"whiteboard", "board", "diagram", "flowchart", "architecture", "schematic", "sketch", "wireframe", "chart", "graph", "blueprint"},
    {"id", "card", "identity", "badge", "license", "passport", "clearance", "sss", "philhealth", "pagibig", "tin", "certificate", "diploma"},
    {"travel", "flight", "ticket", "boarding", "airline", "airport", "hotel", "booking", "itinerary", "trip", "vacation", "tour"},
    {"medical", "health", "doctor", "clinic", "hospital", "prescription", "rx", "lab", "diagnosis", "dental", "checkup"},
    {"video", "videos", "clip", "clips", "movie", "footage", "recording", "mp4", "mov"},
    {"audio", "sound", "music", "song", "voice", "memo", "podcast", "recording", "mp3", "wav"},
    {"archive", "archives", "zip", "bundle", "compressed", "backup", "tar"},
    {"girl", "woman", "female", "lady", "women", "girls", "daughter", "sister", "mother", "mom"},
    {"man", "men", "boy", "male", "guy", "guys", "gentleman", "father", "dad", "brother", "son"},
    {"person", "people", "human", "humans", "friends", "friend", "couple", "family", "group", "crowd", "portrait", "selfie", "face", "team"},
    {"flower", "flowers", "floral", "rose", "roses", "blossom", "petal", "bouquet", "garden", "bloom", "sunflower"},
    {"tree", "trees", "plant", "plants", "leaf", "leaves", "forest", "woods", "nature", "park", "grass", "mountain", "hiking"},
    {"dog", "dogs", "puppy", "puppies", "canine", "pet"},
    {"cat", "cats", "kitten", "kittens", "feline", "pet"},
    {"food", "meal", "dish", "dinner", "lunch", "breakfast", "snack", "restaurant", "eat", "cafe", "bakery", "menu"},
    {"beach", "ocean", "sea", "coast", "shore", "wave", "waves", "sand", "island", "resort", "pool"},
    {"sunset", "sunrise", "golden", "dusk", "dawn", "sky", "clouds", "night", "moon", "stars"},
    {"city", "street", "urban", "building", "architecture", "road", "bridge", "downtown", "mall"},
    {"car", "vehicle", "auto", "automobile", "driving", "truck", "sedan", "motorcycle", "parking", "bike"},
)

_SYNONYM_LOOKUP: dict[str, set[str]] = {}
for _group in _SYNONYM_GROUPS:
    for _word in _group:
        _SYNONYM_LOOKUP.setdefault(_word, set()).update(_group)

_MODALITY_CUES: dict[str, set[str]] = {
    "image": {"image", "images", "photo", "photos", "picture", "pictures", "pic", "pics", "screenshot", "screenshots", "scan", "scans", "snap", "snapshots", "png", "jpg", "jpeg", "webp", "heic", "svg", "diagram"},
    "document": {"pdf", "pdfs", "doc", "docs", "docx", "document", "documents", "paper", "papers", "article", "report", "slides", "presentation", "pptx", "spreadsheet", "spreadsheets", "excel", "xlsx", "csv", "text", "notes", "markdown"},
    "code": {"code", "script", "scripts", "python", "javascript", "typescript", "source", "repo", "repository", "function", "class", "py", "rs", "ts", "tsx", "js", "json", "html", "css", "sql"},
    "video": {"video", "videos", "clip", "clips", "footage", "mp4", "mov", "mkv"},
    "audio": {"audio", "voice", "mp3", "wav", "m4a", "podcast", "music"},
    "archive": {"zip", "archive", "archives", "bundle", "tar", "gz"},
}

_FOLDER_CUES = {
    "download": "downloads",
    "desktop": "desktop",
    "document": "documents",
    "picture": "pictures",
    "finance": "finance",
    "research": "research",
    "work": "work",
    "sample": "samples",
}

_RECENCY_CUES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\btoday\b", re.I), 1),
    (re.compile(r"\byesterday\b", re.I), 2),
    (re.compile(r"\b(?:this|last|past)\s+week\b", re.I), 14),
    (re.compile(r"\b(?:this|last|past)\s+month\b", re.I), 62),
    (re.compile(r"\brecent(?:ly)?\b|\blatest\b|\bnewest\b", re.I), 45),
)


def _search_tokens(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[a-zA-Z0-9]{2,}", text or "")]


def _search_stem(token: str) -> str:
    """Very small suffix stripper so 'receipts'/'cleaning' still match their roots."""
    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("sses"):
        return token[:-2]
    if token.endswith(("ches", "shes", "xes", "zes")):
        return token[:-2]
    if token.endswith("ing") and len(token) > 5:
        return token[:-3]
    if token.endswith("ed") and len(token) > 4:
        return token[:-2]
    if token.endswith("es") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _query_concepts(query: str) -> tuple[list[dict[str, set[str]]], list[str], str]:
    """Return term groups (base forms + weighted synonyms), display terms, and a normalized phrase."""
    raw_terms: list[str] = []
    for token in _search_tokens(query):
        if token in _SEARCH_STOPWORDS or len(token) < 2:
            continue
        if token not in raw_terms:
            raw_terms.append(token)

    concepts: list[dict[str, set[str]]] = []
    for term in raw_terms:
        base = {term, _search_stem(term)}
        synonyms: set[str] = set()
        for key in (term, _search_stem(term)):
            synonyms.update(_SYNONYM_LOOKUP.get(key, set()))
        synonyms -= base
        concepts.append({
            "base": {v for v in base if len(v) >= 2},
            "syn": {v for v in synonyms if len(v) >= 2},
        })

    phrase = " ".join(t for t in _search_tokens(query) if t not in _SEARCH_STOPWORDS)
    return concepts, raw_terms, phrase


def _infer_query_features(query: str) -> dict[str, Any]:
    lowered = query.lower()
    tokens = set(_search_tokens(lowered))

    modality_scores = {
        modality: len(tokens & cues) for modality, cues in _MODALITY_CUES.items()
    }
    modality = max(modality_scores, key=modality_scores.get) if any(modality_scores.values()) else None

    recency_days = 0
    for pattern, days in _RECENCY_CUES:
        if pattern.search(lowered):
            recency_days = max(recency_days, days)

    folder_hint = next((folder for cue, folder in _FOLDER_CUES.items() if cue in lowered), None)
    return {"modality": modality, "recency_days": recency_days, "folder": folder_hint}


_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_CONVERSATIONAL_NOISE = re.compile(
    r"\b(where\s+is|where's|wheres|find\s+my|show\s+me|list\s+all|list\s+things|list|looking\s+for|"
    r"that\s+are|that|this|the|my|everything|anything|items|all|every|"
    r"i\s+took|i've\s+took|ive\s+took|taken|captured|shot|photo|photos|pic|pics|picture|pictures|"
    r"image|images|snapshot|screenshots?|last\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|month|year)|"
    r"this\s+(?:week|month|year)|yesterday|today|on\s+a|at\s+a|in\s+a|from)\b",
    re.IGNORECASE,
)


def _month_bounds(yr: int, mo: int) -> tuple[datetime, datetime]:
    start_dt = datetime(yr, mo, 1, 0, 0, 0)
    last_day = calendar.monthrange(yr, mo)[1]
    end_dt = datetime(yr, mo, last_day, 23, 59, 59)
    return start_dt, end_dt


def _parse_single_date(expr: str, now_dt: datetime) -> Optional[tuple[datetime, datetime, str]]:
    """Parse one concrete date phrase ('Oct 18, 2025', '2025-10-18', '10/18/2025', 'October 2025', 'Oct 18')."""
    s = (expr or "").strip().lower().replace(",", " ")
    s = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()

    # 1. ISO YYYY-MM-DD
    m = re.fullmatch(r"((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            d0 = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0)
            d1 = d0.replace(hour=23, minute=59, second=59)
            return d0, d1, d0.strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            return None

    # 2. Numeric MM/DD/YYYY (or DD/MM/YYYY if first > 12)
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/]((?:19|20)\d{2})", s)
    if m:
        a, b, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        mo, dy = (b, a) if a > 12 else (a, b)
        try:
            d0 = datetime(yr, mo, dy, 0, 0, 0)
            d1 = d0.replace(hour=23, minute=59, second=59)
            return d0, d1, d0.strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            return None

    # 3. Month Day Year ('oct 18 2025')
    m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})\s+((?:19|20)\d{2})", s)
    if m and m.group(1) in _MONTH_MAP:
        try:
            d0 = datetime(int(m.group(3)), _MONTH_MAP[m.group(1)], int(m.group(2)), 0, 0, 0)
            d1 = d0.replace(hour=23, minute=59, second=59)
            return d0, d1, d0.strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            return None

    # 4. Day Month Year ('18 oct 2025')
    m = re.fullmatch(r"(\d{1,2})\s+(?:of\s+)?([a-z]+)\s+((?:19|20)\d{2})", s)
    if m and m.group(2) in _MONTH_MAP:
        try:
            d0 = datetime(int(m.group(3)), _MONTH_MAP[m.group(2)], int(m.group(1)), 0, 0, 0)
            d1 = d0.replace(hour=23, minute=59, second=59)
            return d0, d1, d0.strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            return None

    # 5. Month Year ('october 2025', 'oct 2025')
    m = re.fullmatch(r"([a-z]+)\s+((?:19|20)\d{2})", s)
    if m and m.group(1) in _MONTH_MAP:
        mo = _MONTH_MAP[m.group(1)]
        yr = int(m.group(2))
        d0, d1 = _month_bounds(yr, mo)
        return d0, d1, d0.strftime("%b %Y")

    # 6. Month Day without year ('oct 18', 'november 19')
    m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})", s)
    if m and m.group(1) in _MONTH_MAP:
        mo = _MONTH_MAP[m.group(1)]
        dy = int(m.group(2))
        yr = now_dt.year
        try:
            cand = datetime(yr, mo, dy, 0, 0, 0)
            if (cand - now_dt).days > 30:
                cand = datetime(yr - 1, mo, dy, 0, 0, 0)
            return cand, cand.replace(hour=23, minute=59, second=59), cand.strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            return None

    # 7. Bare year ('2025')
    m = re.fullmatch(r"((?:19|20)\d{2})", s)
    if m:
        yr = int(m.group(1))
        return datetime(yr, 1, 1, 0, 0, 0), datetime(yr, 12, 31, 23, 59, 59), str(yr)

    return None


def _parse_temporal_spec(query: str) -> Optional[dict[str, Any]]:
    """
    Parse natural-language dates, months, ranges, and relative windows into exact timestamps and YYYY-MM-DD dates.
    """
    raw = query or ""
    lowered = raw.lower()
    now_dt = datetime.now()
    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start.replace(hour=23, minute=59, second=59)

    date_scope = "taken" if re.search(r"\b(taken|captured|shot|snap|snapped|created)\b", lowered) else "any"

    def _pack(d0: datetime, d1: datetime, label: str, span: str) -> dict[str, Any]:
        return {
            "start_ts": d0.timestamp(),
            "end_ts": d1.timestamp(),
            "start_date": d0.strftime("%Y-%m-%d"),
            "end_date": d1.strftime("%Y-%m-%d"),
            "label": label,
            "span_text": span,
            "date_scope": date_scope,
        }

    # Range: 'between X and Y' or 'from X to Y'
    m_range = re.search(
        r"\b(?:between|from)\s+([a-z0-9,/\-\s]{3,28}?)\s+(?:and|to|through|-)\s+([a-z0-9,/\-\s]{3,28}?)(?=$|\s+(?:without|excluding|except|that|with|which|in\s+my)\b|[?.!,])",
        lowered,
    )
    if m_range:
        p1 = _parse_single_date(m_range.group(1), now_dt)
        p2 = _parse_single_date(m_range.group(2), now_dt)
        if p1 and p2:
            d0 = min(p1[0], p2[0])
            d1 = max(p1[1], p2[1])
            return _pack(d0, d1, f"{p1[2]} – {p2[2]}", m_range.group(0))

    # Open-ended comparisons: 'after <date>', 'since <date>', 'before <date>'
    m_cmp = re.search(
        r"\b(after|since|before)\s+((?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?,?\s*(?:(?:19|20)\d{2})?|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+(?:19|20)\d{2}|(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/](?:19|20)\d{2}|(?:19|20)\d{2}|yesterday|last\s+week|last\s+month))",
        lowered,
    )
    if m_cmp:
        op, rhs = m_cmp.group(1), m_cmp.group(2).strip()
        parsed_rhs = _parse_single_date(rhs, now_dt)
        if not parsed_rhs and rhs == "yesterday":
            y0 = today_start - timedelta(days=1)
            parsed_rhs = (y0, y0.replace(hour=23, minute=59, second=59), "yesterday")
        elif not parsed_rhs and rhs == "last week":
            w0 = today_start - timedelta(days=7)
            parsed_rhs = (w0, today_end, "last week")
        elif not parsed_rhs and rhs == "last month":
            m0 = today_start - timedelta(days=30)
            parsed_rhs = (m0, today_end, "last month")
        if parsed_rhs:
            if op in ("after", "since"):
                return _pack(parsed_rhs[0], today_end, f"Since {parsed_rhs[2]}", m_cmp.group(0))
            return _pack(datetime(1970, 1, 1), parsed_rhs[1], f"Before {parsed_rhs[2]}", m_cmp.group(0))

    # Specific calendar dates in text (with optional preposition)
    months_pat = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    date_patterns = [
        r"\b(?:on|taken\s+on|from|dated)?\s*((?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2})\b",
        r"\b(?:on|taken\s+on|from|dated)?\s*(\d{1,2}[-/]\d{1,2}[-/](?:19|20)\d{2})\b",
        rf"\b(?:on|taken\s+on|from|dated)?\s*({months_pat}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*,\s*|\s+)(?:19|20)\d{{2}})\b",
        rf"\b(?:on|taken\s+on|from|dated)?\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{months_pat}\s+(?:19|20)\d{{2}})\b",
        rf"\b(?:in|during|from|taken\s+in)?\s*({months_pat}\s+(?:19|20)\d{{2}})\b",
        rf"\b(?:on|taken\s+on|from)?\s*({months_pat}\s+\d{{1,2}}(?:st|nd|rd|th)?)\b",
    ]
    for pat in date_patterns:
        md = re.search(pat, lowered)
        if md:
            parsed = _parse_single_date(md.group(1), now_dt)
            if parsed:
                return _pack(parsed[0], parsed[1], parsed[2], md.group(0))

    # Relative counts: 'last 7 days', 'past 2 weeks', '3 days ago'
    m_rel = re.search(r"\b(?:last|past)\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)\b", lowered)
    if m_rel:
        n = max(1, min(3650, int(m_rel.group(1))))
        unit = m_rel.group(2)
        days = n * (365 if unit.startswith("year") else 30 if unit.startswith("month") else 7 if unit.startswith("week") else 1)
        d0 = today_start - timedelta(days=days)
        return _pack(d0, today_end, f"Last {n} {unit}", m_rel.group(0))

    m_ago = re.search(r"\b(\d+)\s+(day|days|week|weeks|month|months)\s+ago\b", lowered)
    if m_ago:
        n = max(1, min(365, int(m_ago.group(1))))
        unit = m_ago.group(2)
        days = n * (30 if unit.startswith("month") else 7 if unit.startswith("week") else 1)
        target = today_start - timedelta(days=days)
        return _pack(target, target.replace(hour=23, minute=59, second=59), f"{n} {unit} ago", m_ago.group(0))

    if re.search(r"\btoday\b", lowered):
        return _pack(today_start, today_end, "Today", "today")
    if re.search(r"\byesterday\b", lowered):
        y0 = today_start - timedelta(days=1)
        return _pack(y0, y0.replace(hour=23, minute=59, second=59), "Yesterday", "yesterday")

    m_day = re.search(r"\b(?:last|on|from)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    if m_day:
        target_wd = _WEEKDAYS[m_day.group(1)]
        days_back = (now_dt.weekday() - target_wd) % 7
        if days_back == 0:
            days_back = 7
        day_start = today_start - timedelta(days=days_back)
        return _pack(day_start, day_start.replace(hour=23, minute=59, second=59), f"Last {m_day.group(1).title()}", m_day.group(0))

    if re.search(r"\b(?:this|last)\s+weekend\b", lowered):
        # Find the most recent Saturday-Sunday
        days_since_sat = (now_dt.weekday() - 5) % 7
        if "last weekend" in lowered and days_since_sat < 2:
            days_since_sat += 7
        sat = today_start - timedelta(days=days_since_sat)
        sun_end = (sat + timedelta(days=1)).replace(hour=23, minute=59, second=59)
        lbl = "Last weekend" if "last weekend" in lowered else "This weekend"
        return _pack(sat, sun_end, lbl, lbl.lower())

    m_month_only = re.search(rf"\b(last|this|in|from|during)\s+({months_pat})\b", lowered)
    if m_month_only:
        mod, mo_name = m_month_only.group(1), m_month_only.group(2)
        mo = _MONTH_MAP[mo_name]
        yr = now_dt.year
        if mod == "last" or (mod in ("in", "from", "during") and mo > now_dt.month):
            if mo >= now_dt.month:
                yr -= 1
        d0, d1 = _month_bounds(yr, mo)
        return _pack(d0, d1, d0.strftime("%b %Y"), m_month_only.group(0))

    if "last week" in lowered or "past week" in lowered:
        return _pack(today_start - timedelta(days=14), today_start - timedelta(seconds=1), "Last week", "last week")
    if "this week" in lowered:
        return _pack(today_start - timedelta(days=now_dt.weekday()), today_end, "This week", "this week")
    if "last month" in lowered or "past month" in lowered:
        first_this = today_start.replace(day=1)
        last_prev = first_this - timedelta(seconds=1)
        first_prev = last_prev.replace(day=1, hour=0, minute=0, second=0)
        return _pack(first_prev, last_prev, first_prev.strftime("%b %Y"), "last month")
    if "this month" in lowered:
        return _pack(today_start.replace(day=1), today_end, today_start.strftime("%b %Y"), "this month")
    if "last year" in lowered:
        yr = now_dt.year - 1
        return _pack(datetime(yr, 1, 1), datetime(yr, 12, 31, 23, 59, 59), str(yr), "last year")
    if "this year" in lowered:
        yr = now_dt.year
        return _pack(datetime(yr, 1, 1), today_end, str(yr), "this year")

    m_year = re.search(r"\b(?:in|from|taken\s+in|year)?\s*((?:19|20)\d{2})\b", lowered)
    if m_year:
        yr = int(m_year.group(1))
        return _pack(datetime(yr, 1, 1), datetime(yr, 12, 31, 23, 59, 59), str(yr), m_year.group(0))

    return None


def _parse_temporal_window(query: str) -> Optional[tuple[float, float, str]]:
    """Backward-compatible wrapper returning (start_ts, end_ts, label)."""
    spec = _parse_temporal_spec(query)
    if not spec:
        return None
    return spec["start_ts"], spec["end_ts"], spec["label"]


_EXPLICIT_LIST_TRIGGER = re.compile(
    r"\b(?:list(?:\s+all|\s+every|\s+the|\s+things|\s+items)?|show\s+(?:all|every)|find\s+all|give\s+me\s+all|how\s+many|count(?:\s+of)?|number\s+of|only|all\s+my|all\s+the)\b",
    re.IGNORECASE,
)

_CAMERA_BRANDS = (
    "iphone", "samsung", "galaxy", "pixel", "canon", "nikon", "sony",
    "fujifilm", "fuji", "leica", "gopro", "dji", "xiaomi", "oppo", "vivo", "huawei",
)


def _parse_nl_query(raw_query: str) -> dict[str, Any]:
    """
    Parse everyday English / conversational constraints from a search or Ask AI query:
      - Quoted & clause-based must-include phrases ('includes "solar"', 'containing budget')
      - Natural dates & relative windows ('taken on Oct 18, 2025', 'last 7 days', 'from Jan to Mar')
      - Strict file kind/modality filters ('list all photos...', 'only pdfs', 'spreadsheets')
      - Camera hints ('taken with iPhone'), size constraints ('larger than 2 MB'), exclusions ('without draft')
      - Intent classification ('list', 'count', 'superlative', 'finance', 'summarize', 'search')
    """
    work = (raw_query or "").strip()
    applied_filters: list[str] = []
    include_phrases: list[str] = []

    # 1. Extract quoted phrases FIRST so inner words aren't hijacked by date/type regexes.
    quote_pat = re.compile(r'"([^"]+)"|\'([^\']+)\'')
    for m in list(quote_pat.finditer(work)):
        phrase_val = (m.group(1) or m.group(2) or "").strip()
        if phrase_val and phrase_val.lower() not in include_phrases:
            include_phrases.append(phrase_val)
            applied_filters.append(f'includes "{phrase_val}"')
    work = quote_pat.sub(" ", work)
    # Remove hanging connector verbs that wrapped a now-extracted quoted phrase.
    work = re.sub(
        r"\b(?:that|which)?\s*(?:includes?|including|contains?|containing|mentions?|mentioning|with\s+the\s+words?|titled|called|named)\s*(?=$|\b(?:and|or|taken|captured|shot|from|after|before|since|between|without|excluding|except|in|on)\b|[?.!,])",
        " ",
        work,
        flags=re.IGNORECASE,
    )

    # 2. Extract unquoted clause-based inclusions: 'that includes solar tracking taken on ...'
    clause_pat = re.compile(
        r"\b(?:that\s+|which\s+)?(?:includes?|including|contains?|containing|mentions?|mentioning|with\s+text|with\s+the\s+word|titled|called|named)\s+"
        r"([a-z0-9][a-z0-9\s._\-/]{1,60}?)"
        r"(?=$|\s+(?:taken|captured|shot|from|on|in\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{4}|downloads|desktop|documents)|after|before|since|between|without|excluding|except|larger|bigger|smaller|under|over)\b|[?.!,;])",
        re.IGNORECASE,
    )
    for m in list(clause_pat.finditer(work)):
        frag = re.sub(r"^(?:the\s+|this\s+|word\s+|words\s+|phrase\s+|text\s+)+", "", m.group(1).strip(), flags=re.IGNORECASE).strip()
        if len(frag) >= 2 and frag.lower() not in {"it", "this", "that", "them"}:
            if frag.lower() not in [p.lower() for p in include_phrases]:
                include_phrases.append(frag)
                applied_filters.append(f'includes "{frag}"')
    work = clause_pat.sub(" ", work)

    # 3. Extract natural exclusions ('without draft', 'excluding copy', 'except pdfs')
    exclude_terms: list[str] = []
    exclude_mods: list[str] = []
    exclude_exts: list[str] = []
    excl_pat = re.compile(r"\b(?:without|excluding|exclude|except)\s+([a-z0-9._\-]{2,30})\b", re.IGNORECASE)
    for m in list(excl_pat.finditer(work)):
        term = m.group(1).strip().lower()
        if term in {"photo", "photos", "image", "images", "picture", "pictures"}:
            exclude_mods.append("image")
        elif term in {"pdf", "pdfs"}:
            exclude_exts.append("pdf")
        elif term in {"doc", "docs", "document", "documents"}:
            exclude_mods.append("document")
        else:
            exclude_terms.append(term)
        applied_filters.append(f"without {term}")
    work = excl_pat.sub(" ", work)

    # 4. Extract natural temporal / date spec
    temporal_spec = _parse_temporal_spec(work)
    if temporal_spec:
        applied_filters.append(f"date: {temporal_spec['label']}")
        span_esc = re.escape(temporal_spec["span_text"].strip())
        work = re.sub(rf"(?:\btaken\s+|\bcaptured\s+|\bshot\s+|\bcreated\s+)?{span_esc}", " ", work, flags=re.IGNORECASE)

    # 5. Extract camera hints ('taken with iPhone', 'shot on Canon')
    camera_hint: Optional[str] = None
    cam_pat = re.compile(r"\b(?:taken\s+with|shot\s+on|shot\s+with|captured\s+with|camera)\s+([a-z0-9][a-z0-9\s\-]{1,24}?)(?=$|\s+(?:in|on|from|after|before|without)\b|[?.!,])", re.IGNORECASE)
    m_cam = cam_pat.search(work)
    if m_cam:
        camera_hint = m_cam.group(1).strip().lower()
        applied_filters.append(f"camera: {m_cam.group(1).strip()}")
        work = cam_pat.sub(" ", work)
    else:
        low_toks = set(_search_tokens(work))
        for brand in _CAMERA_BRANDS:
            if brand in low_toks and any(k in low_toks for k in ("photo", "photos", "picture", "pictures", "image", "images", "shot", "taken", "camera")):
                camera_hint = brand
                applied_filters.append(f"camera: {brand}")
                break

    # 6. Extract natural size constraints ('larger than 2 mb', 'under 100 kb')
    size_op: Optional[str] = None
    size_bytes: Optional[int] = None
    m_sz = re.search(
        r"\b(larger\s+than|bigger\s+than|more\s+than|over|above|at\s+least|smaller\s+than|less\s+than|under|below)\s+(\d+(?:\.\d+)?\s*(?:b|kb|mb|gb))\b",
        work,
        flags=re.IGNORECASE,
    )
    if m_sz:
        verb, amt = m_sz.group(1).lower(), m_sz.group(2).replace(" ", "")
        parsed_sz = _parse_size_token(amt)
        if parsed_sz is not None:
            size_op = ">" if any(w in verb for w in ("larger", "bigger", "more", "over", "above", "least")) else "<"
            size_bytes = parsed_sz
            applied_filters.append(f"size {size_op} {amt.upper()}")
            work = work[:m_sz.start()] + " " + work[m_sz.end():]

    # 7. Extract natural folder cues ('inside Downloads', 'in Desktop folder')
    strict_folder: Optional[str] = None
    m_fld = re.search(r"\b(?:inside|in)\s+(?:my\s+|the\s+)?(downloads|desktop|documents|pictures|finance|research|work|samples)(?:\s+folder)?\b", work, re.IGNORECASE)
    if m_fld:
        strict_folder = m_fld.group(1).lower()
        applied_filters.append(f"folder: {strict_folder}")
        work = work[:m_fld.start()] + " " + work[m_fld.end():]

    # 8. Detect intent mode & strict modality / extensions / tags
    low_orig = (raw_query or "").lower()
    low_work = work.lower()
    work_tokens = set(_search_tokens(low_work))
    orig_tokens = set(_search_tokens(low_orig))

    intent_mode = "search"
    if re.search(r"\b(?:how\s+many|count\s+(?:the|my|all)|number\s+of)\b", low_orig):
        intent_mode = "count"
    elif _EXPLICIT_LIST_TRIGGER.search(low_orig):
        intent_mode = "list"
    elif re.search(r"\b(?:how\s+much|total\s+spent|spent\s+on|sum\s+of|receipts?\s+total)\b", low_orig):
        intent_mode = "finance"
    elif re.search(r"\b(?:summarize|summary|overview)\b", low_orig):
        intent_mode = "summarize"

    strict_modality: Optional[str] = None
    strict_exts: set[str] = set()
    required_tags: list[str] = []

    is_explicit_type_query = bool(
        intent_mode in ("list", "count")
        or temporal_spec is not None
        or camera_hint is not None
        or size_bytes is not None
        or bool(include_phrases)
        or re.search(r"\b(?:my|all|every|only)\s+(?:photos?|pictures?|images?|screenshots?|pdfs?|spreadsheets?|slides?|presentations?|videos?|audios?|recordings?|archives?)\b", low_orig)
    )

    if is_explicit_type_query:
        if work_tokens & {"screenshot", "screenshots", "screencap", "screencaps"}:
            strict_modality = "image"
            required_tags.append("screenshot")
            applied_filters.append("screenshots")
        elif work_tokens & {"photo", "photos", "picture", "pictures", "pic", "pics", "image", "images", "selfie", "selfies", "portrait", "portraits"}:
            strict_modality = "image"
            applied_filters.append("type: image")
        elif work_tokens & {"pdf", "pdfs"}:
            strict_exts = {"pdf"}
            applied_filters.append("format: pdf")
        elif work_tokens & {"spreadsheet", "spreadsheets", "excel", "xlsx", "xls", "csv", "workbook", "sheets"}:
            strict_exts = {"xlsx", "xlsm", "xls", "csv", "tsv"}
            applied_filters.append("type: spreadsheet")
        elif work_tokens & {"slide", "slides", "presentation", "presentations", "ppt", "pptx", "powerpoint", "deck"}:
            strict_exts = {"ppt", "pptx", "key"}
            applied_filters.append("type: slides")
        elif work_tokens & {"video", "videos", "clip", "clips", "mp4", "mov"}:
            strict_modality = "video"
            applied_filters.append("type: video")
        elif work_tokens & {"audio", "mp3", "wav", "m4a", "podcast"}:
            strict_modality = "audio"
            applied_filters.append("type: audio")
        elif work_tokens & {"archive", "archives", "zip", "zips"}:
            strict_modality = "archive"
            applied_filters.append("type: archive")
        elif work_tokens & {"code", "script", "scripts"} and not (work_tokens & {"receipt", "paper"}):
            strict_modality = "code"
            applied_filters.append("type: code")
        elif work_tokens & {"document", "documents", "doc", "docs"}:
            strict_modality = "document"
            applied_filters.append("type: document")

    pinned_only = bool(orig_tokens & {"pinned", "starred", "favorite", "favorites"})
    if pinned_only:
        applied_filters.append("pinned")
    unnamed_only = bool(orig_tokens & {"unnamed", "cryptic"})
    if unnamed_only:
        applied_filters.append("unnamed")

    # 9. Clean residual topic tokens: strip command/type noise words when already captured as filters.
    strip_type_words = set()
    if strict_modality or strict_exts or required_tags:
        strip_type_words = {
            "photo", "photos", "picture", "pictures", "pic", "pics", "image", "images",
            "screenshot", "screenshots", "pdf", "pdfs", "spreadsheet", "spreadsheets",
            "excel", "xlsx", "csv", "slide", "slides", "presentation", "presentations",
            "pptx", "document", "documents", "doc", "docs", "video", "videos", "audio",
            "archive", "archives", "zip", "code", "script", "scripts",
        }
    residual_tokens = [
        t for t in _search_tokens(work)
        if t not in _SEARCH_STOPWORDS
        and t not in strip_type_words
        and t not in {"pinned", "starred", "favorite", "favorites", "unnamed", "cryptic"}
    ]
    topic_query = " ".join(residual_tokens).strip()

    has_strict_filters = bool(
        include_phrases
        or exclude_terms
        or exclude_mods
        or exclude_exts
        or temporal_spec is not None
        or camera_hint is not None
        or size_bytes is not None
        or strict_folder is not None
        or strict_modality is not None
        or strict_exts
        or required_tags
        or pinned_only
        or unnamed_only
    )

    # Deduplicate applied filter labels preserving order
    seen_lbl: set[str] = set()
    dedup_filters: list[str] = []
    for lbl in applied_filters:
        if lbl not in seen_lbl:
            seen_lbl.add(lbl)
            dedup_filters.append(lbl)

    return {
        "include_phrases": include_phrases,
        "exclude_terms": exclude_terms,
        "exclude_modalities": exclude_mods,
        "exclude_exts": exclude_exts,
        "temporal": temporal_spec,
        "camera_hint": camera_hint,
        "size_op": size_op,
        "size_bytes": size_bytes,
        "strict_folder": strict_folder,
        "strict_modality": strict_modality,
        "strict_exts": strict_exts,
        "required_tags": required_tags,
        "pinned_only": pinned_only,
        "unnamed_only": unnamed_only,
        "intent_mode": intent_mode,
        "topic_query": topic_query,
        "has_strict_filters": has_strict_filters,
        "applied_filters": dedup_filters,
    }


def _row_matches_temporal(f_row: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Check a row against temporal_spec using EXIF takenDate, created_date, and mtime."""
    meta_raw = f_row.get("metadata_json") or f_row.get("metadata") or {}
    if isinstance(meta_raw, str):
        try:
            meta_obj = json.loads(meta_raw)
        except Exception:
            meta_obj = {}
    elif isinstance(meta_raw, dict):
        meta_obj = meta_raw
    else:
        meta_obj = {}

    taken_date = str(meta_obj.get("takenDate") or "").strip()
    created_date = str(f_row.get("created_date") or f_row.get("createdDate") or "").strip()
    mtime_val = float(f_row.get("mtime") or 0.0)
    mtime_date = datetime.fromtimestamp(mtime_val).strftime("%Y-%m-%d") if mtime_val > 0 else ""

    s_date = str(spec["start_date"])
    e_date = str(spec["end_date"])
    s_ts = float(spec["start_ts"])
    e_ts = float(spec["end_ts"])

    primary_date = taken_date or created_date
    if spec.get("date_scope") == "taken" and primary_date:
        return s_date <= primary_date <= e_date

    if primary_date and s_date <= primary_date <= e_date:
        return True
    if mtime_val > 0 and s_ts <= mtime_val <= e_ts:
        return True
    if mtime_date and s_date <= mtime_date <= e_date:
        return True
    return False


def _row_matches_nl_filters(f_row: dict[str, Any], nl: dict[str, Any]) -> bool:
    """Apply strict natural-language constraints cleanly against an indexed file row."""
    mod_val = str(f_row.get("modality") or "").lower()
    ext_val = str(f_row.get("ext") or f_row.get("format") or "").lower()

    if nl.get("strict_modality") and mod_val != nl["strict_modality"]:
        return False
    if nl.get("strict_exts") and ext_val not in nl["strict_exts"]:
        return False
    if any(m == mod_val for m in nl.get("exclude_modalities", [])):
        return False
    if any(ex == ext_val for ex in nl.get("exclude_exts", [])):
        return False
    if nl.get("pinned_only") and not bool(f_row.get("pinned")):
        return False
    if nl.get("unnamed_only") and not (f_row.get("suggested_name") or f_row.get("suggestedName")):
        return False

    sb = int(f_row.get("size_bytes") or f_row.get("sizeBytes") or 0)
    if nl.get("size_bytes") is not None:
        if nl.get("size_op") == ">" and sb <= int(nl["size_bytes"]):
            return False
        if nl.get("size_op") == "<" and sb >= int(nl["size_bytes"]):
            return False

    folder_hay = f"{f_row.get('folder', '')} {f_row.get('folder_short', '')} {f_row.get('folderFull', '')}".lower()
    if nl.get("strict_folder") and nl["strict_folder"] not in folder_hay:
        return False

    if nl.get("temporal") and not _row_matches_temporal(f_row, nl["temporal"]):
        return False

    tags_raw = str(f_row.get("tags_json") or f_row.get("tags") or "")
    meta_str = str(f_row.get("metadata_json") or f_row.get("metadata") or "")
    raw_content_hay = " ".join([
        str(f_row.get("title") or ""),
        str(f_row.get("name") or ""),
        str(f_row.get("original_name") or f_row.get("originalName") or ""),
        str(f_row.get("extracted_text") or f_row.get("extractedText") or ""),
        str(f_row.get("ocr_text") or f_row.get("ocrText") or ""),
        tags_raw,
        meta_str,
    ])
    content_hay_norm = re.sub(r"\s+", " ", raw_content_hay.lower())

    if nl.get("camera_hint") and nl["camera_hint"] not in meta_str.lower() and nl["camera_hint"] not in content_hay_norm:
        return False

    for req_tag in nl.get("required_tags", []):
        if req_tag == "screenshot":
            if "screenshot" not in tags_raw.lower() and "screenshot" not in str(f_row.get("name") or "").lower() and "screen" not in str(f_row.get("name") or "").lower():
                return False
        elif req_tag.lower() not in tags_raw.lower():
            return False

    for phrase in nl.get("include_phrases", []):
        p_norm = re.sub(r"\s+", " ", phrase.lower().strip())
        if p_norm and p_norm not in content_hay_norm:
            return False

    excl_hay_norm = f"{content_hay_norm} {str(f_row.get('shelf_name') or f_row.get('shelfName') or '').lower()}"
    for ex_term in nl.get("exclude_terms", []):
        if ex_term.lower() in excl_hay_norm:
            return False

    return True


def _clean_visual_query(query: str) -> str:
    """Strip conversational & temporal filler so CLIP/SigLIP sees the core visual subject."""
    cleaned = _CONVERSATIONAL_NOISE.sub(" ", query or "")
    cleaned = re.sub(r"\b(?:19|20)\d{2}\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.?!'\"")
    return cleaned or (query or "").strip()


def _field_tokens(text: str) -> set[str]:
    return {_search_stem(t) for t in _search_tokens(text)} | set(_search_tokens(text))


def _lexical_relevance(
    f_row: sqlite3.Row | dict[str, Any],
    concepts: list[dict[str, set[str]]],
    phrase: str,
    idf_map: Optional[dict[str, float]] = None,
) -> tuple[float, list[str], bool]:
    """BM25-inspired exact/synonym matching over title+name, shelf label, and body text."""
    if not concepts:
        return 0.0, [], False

    title_raw = _search_tokens(f"{f_row['title']} {f_row['name']} {Path(str(f_row['name'])).stem}")
    shelf_raw = _search_tokens(str(f_row["shelf_name"]))
    tags_raw = _search_tokens(str(f_row["tags_json"] if "tags_json" in f_row.keys() else ""))
    meta_raw = _search_tokens(str(f_row["metadata_json"] if "metadata_json" in f_row.keys() else ""))
    body_raw = _search_tokens(f"{f_row['extracted_text']} {f_row['ocr_text']}")

    title_norm = " ".join(title_raw)
    body_norm = " ".join([*body_raw, *tags_raw, *meta_raw])

    title_tokens = _field_tokens(title_norm)
    shelf_tokens = _field_tokens(" ".join(shelf_raw))
    body_tokens = _field_tokens(body_norm)

    # Term frequency saturation in body: tf / (tf + 1.2)
    body_counts: dict[str, int] = {}
    for tok in body_raw:
        body_counts[tok] = body_counts.get(tok, 0) + 1
        stem = _search_stem(tok)
        body_counts[stem] = body_counts.get(stem, 0) + 1

    def _match_weight(variant: str, synonym: bool) -> tuple[float, bool]:
        stemmed = _search_stem(variant)
        title_w, body_w = (0.55, 0.30) if synonym else (1.0, 0.55)
        stem_title_w, stem_body_w = (0.42, 0.22) if synonym else (0.85, 0.45)
        if variant in title_tokens:
            return title_w, True
        if variant in shelf_tokens or variant in body_tokens:
            tf = body_counts.get(variant, 1)
            tf_sat = min(1.15, (2.2 * tf) / (tf + 1.2))
            return body_w * tf_sat, True
        if stemmed in title_tokens:
            return stem_title_w, True
        if stemmed in shelf_tokens or stemmed in body_tokens:
            tf = body_counts.get(stemmed, 1)
            tf_sat = min(1.15, (2.2 * tf) / (tf + 1.2))
            return stem_body_w * tf_sat, True
        if not synonym and len(variant) >= 5:
            best_ratio = 0.0
            for cand in title_tokens:
                if abs(len(cand) - len(variant)) <= 2:
                    r = difflib.SequenceMatcher(None, variant, cand).ratio()
                    if r > best_ratio:
                        best_ratio = r
            if best_ratio >= 0.82:
                return 0.72 * best_ratio, True
            for cand in shelf_tokens | body_tokens:
                if abs(len(cand) - len(variant)) <= 2 and len(cand) >= 4:
                    r = difflib.SequenceMatcher(None, variant, cand).ratio()
                    if r > best_ratio:
                        best_ratio = r
            if best_ratio >= 0.84:
                return 0.42 * best_ratio, True
        return 0.0, False

    total = 0.0
    weight_sum = 0.0
    matched_labels: list[str] = []
    for concept in concepts:
        best = 0.0
        best_label = ""
        for variant in concept["base"]:
            weight, hit = _match_weight(variant, synonym=False)
            if hit and weight > best:
                best = weight
                best_label = variant
        for variant in concept["syn"]:
            weight, hit = _match_weight(variant, synonym=True)
            if hit and weight * 0.9 > best:
                best = weight * 0.9
                best_label = variant
        idf = max((idf_map.get(v, 1.0) for v in concept["base"]), default=1.0) if idf_map else 1.0
        weight_sum += idf
        if best > 0:
            total += best * idf
            matched_labels.append(best_label)

    score = total / max(1e-6, weight_sum)
    phrase_hit = False
    if phrase and len(phrase) >= 4:
        if phrase in title_norm:
            score = min(1.0, score + 0.22)
            phrase_hit = True
        elif phrase in body_norm:
            score = min(1.0, score + 0.12)
            phrase_hit = True
    return min(1.0, score), matched_labels, phrase_hit


def _extract_snippet_around_terms(text: str, terms: list[str], max_len: int = 96) -> str:
    """Return a clean, symbol-sanitized excerpt centered around the first matched term."""
    clean = clean_extracted_text(text or "", preserve_newlines=False)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return ""
    if terms:
        low = clean.lower()
        for term in terms:
            pos = low.find(term.lower())
            if pos >= 0:
                start = max(0, pos - 28)
                end = min(len(clean), start + max_len)
                excerpt = clean[start:end].strip()
                if start > 0:
                    excerpt = "…" + excerpt
                if end < len(clean):
                    excerpt = excerpt + "…"
                return excerpt
    return clean[:max_len]


def _parse_size_token(raw: str) -> Optional[int]:
    m = re.fullmatch(r"([<>]=?)?(\d+(?:\.\d+)?)(b|kb|mb|gb)?", raw.strip().lower())
    if not m:
        return None
    val = float(m.group(2))
    unit = m.group(3) or "b"
    mult = {"b": 1, "kb": 1024, "mb": 1024 * 1024, "gb": 1024 * 1024 * 1024}[unit]
    return int(val * mult)


def _extract_search_operators(query: str) -> tuple[str, dict[str, Any]]:
    """
    Parse structured search operators like `type:pdf`, `ext:py`, `folder:Downloads`,
    `tag:tax`, `shelf:"Receipts & Finance"`, `after:2026-01-01`, `before:2026-12-31`,
    `has:ocr`, `unnamed:true`, `pinned:true`, `size:>2mb`, and exclusions (`-draft`, `-tag:archive`).
    Returns (remaining_natural_query, filters_dict).
    """
    filters: dict[str, Any] = {}
    remaining: list[str] = []
    tokens = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')+', query or "")
    for token in tokens:
        is_neg = token.startswith("-") and len(token) > 1
        raw_tok = token[1:] if is_neg else token
        if ":" not in raw_tok:
            if is_neg:
                neg_term = raw_tok.strip().strip('"\'').lower()
                if len(neg_term) >= 2:
                    filters.setdefault("exclude_terms", []).append(neg_term)
                continue
            remaining.append(token)
            continue
        key, val = raw_tok.split(":", 1)
        k = key.lower().strip()
        v = val.strip().strip('"\'')
        if not v:
            if not is_neg:
                remaining.append(token)
            continue
        if is_neg:
            v_low = v.lower()
            if k in {"type", "modality"}:
                if v_low in {"doc", "docs"}:
                    filters.setdefault("exclude_modalities", []).append("document")
                elif v_low in {"pdf", "docx"}:
                    filters.setdefault("exclude_exts", []).append(v_low)
                elif v_low in {"img", "photo", "photos", "pic", "image", "images"}:
                    filters.setdefault("exclude_modalities", []).append("image")
                elif v_low in {"zip", "archive", "archives"}:
                    filters.setdefault("exclude_modalities", []).append("archive")
                elif v_low in {"image", "document", "code", "video", "audio", "archive"}:
                    filters.setdefault("exclude_modalities", []).append(v_low)
                else:
                    filters.setdefault("exclude_exts", []).append(v_low.lstrip("."))
            elif k in {"ext", "format"}:
                filters.setdefault("exclude_exts", []).append(v_low.lstrip("."))
            elif k == "folder":
                filters.setdefault("exclude_folders", []).append(v_low)
            elif k == "tag":
                filters.setdefault("exclude_tags", []).append(v_low.lstrip("#"))
            elif k == "shelf":
                filters.setdefault("exclude_shelves", []).append(v_low)
            elif k == "has" and v_low == "ocr":
                filters["has_ocr"] = False
            else:
                neg_term = raw_tok.strip().strip('"\'').lower()
                if len(neg_term) >= 2:
                    filters.setdefault("exclude_terms", []).append(neg_term)
            continue
        if k in {"type", "modality"}:
            v_low = v.lower()
            if v_low in {"doc", "docs", "pdf", "docx", "spreadsheet", "sheet"}:
                filters["modality"] = "document"
                if v_low in {"pdf", "docx"}:
                    filters["ext"] = v_low
            elif v_low in {"img", "photo", "photos", "pic", "image", "images"}:
                filters["modality"] = "image"
            elif v_low in {"zip", "archive", "archives"}:
                filters["modality"] = "archive"
            elif v_low in {"image", "document", "code", "video", "audio", "archive"}:
                filters["modality"] = v_low
            else:
                filters["ext"] = v_low.lstrip(".")
        elif k in {"ext", "format"}:
            filters["ext"] = v.lower().lstrip(".")
        elif k == "folder":
            filters["folder"] = v.lower()
        elif k == "tag":
            filters["tag"] = v.lower().lstrip("#")
        elif k == "shelf":
            filters["shelf"] = v.lower()
        elif k == "after":
            filters["after"] = v
        elif k == "before":
            filters["before"] = v
        elif k == "has" and v.lower() == "ocr":
            filters["has_ocr"] = True
        elif k == "unnamed" and v.lower() in {"true", "1", "yes"}:
            filters["unnamed"] = True
        elif k == "pinned" and v.lower() in {"true", "1", "yes"}:
            filters["pinned"] = True
        elif k == "size":
            op = ">" if v.startswith(">") else ("<" if v.startswith("<") else ">")
            sz = _parse_size_token(v)
            if sz is not None:
                filters["size_op"] = op
                filters["size_bytes"] = sz
        else:
            remaining.append(token)
    return " ".join(remaining).strip(), filters


def _matches_operator_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    if not filters:
        return True
    mod_val = str(row.get("modality", "")).lower()
    ext_val = str(row.get("format") or row.get("ext") or "").lower()
    f_hay = f"{row.get('folder', '')} {row.get('folderFull', '')} {row.get('folder_short', '')}".lower()
    sh_val = f"{row.get('shelfName', '')} {row.get('shelf_name', '')}".lower()
    tags = row.get("tags")
    if tags is None:
        try:
            tags = json.loads(row.get("tags_json") or "[]")
        except Exception:
            tags = []
    tags_lower = [str(t).lower() for t in tags]

    if "modality" in filters and mod_val != filters["modality"]:
        return False
    if "ext" in filters and ext_val != filters["ext"]:
        return False
    if "folder" in filters and filters["folder"] not in f_hay:
        return False
    if "shelf" in filters and filters["shelf"] not in sh_val:
        return False
    if "tag" in filters and filters["tag"] not in tags_lower:
        return False
    if "after" in filters:
        cdate = str(row.get("createdDate") or row.get("created_date") or "")
        if cdate and cdate < filters["after"]:
            return False
    if "before" in filters:
        cdate = str(row.get("createdDate") or row.get("created_date") or "")
        if cdate and cdate > filters["before"]:
            return False
    if "has_ocr" in filters:
        has_ocr = bool(str(row.get("ocrText") or row.get("ocr_text") or "").strip())
        if has_ocr != bool(filters["has_ocr"]):
            return False
    if filters.get("unnamed") and not (row.get("suggestedName") or row.get("suggested_name")):
        return False
    if filters.get("pinned") and not bool(row.get("pinned")):
        return False
    if "size_bytes" in filters:
        sb = int(row.get("sizeBytes") or row.get("size_bytes") or 0)
        if filters["size_op"] == ">" and sb <= filters["size_bytes"]:
            return False
        if filters["size_op"] == "<" and sb >= filters["size_bytes"]:
            return False

    if any(m == mod_val for m in filters.get("exclude_modalities", [])):
        return False
    if any(ex == ext_val for ex in filters.get("exclude_exts", [])):
        return False
    if any(fd in f_hay for fd in filters.get("exclude_folders", [])):
        return False
    if any(sf in sh_val for sf in filters.get("exclude_shelves", [])):
        return False
    if any(tg in tags_lower for tg in filters.get("exclude_tags", [])):
        return False
    exclude_terms = filters.get("exclude_terms", [])
    if exclude_terms:
        hay = (
            f"{row.get('title', '')} {row.get('name', '')} "
            f"{row.get('extractedText') or row.get('extracted_text') or ''} "
            f"{row.get('ocrText') or row.get('ocr_text') or ''}"
        ).lower()
        if any(term in hay for term in exclude_terms):
            return False
    return True


class _PooledConnection:
    """Lightweight proxy around the persistent SQLite connection so `.close()` is a safe no-op."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any) -> sqlite3.Cursor:
        return self._conn.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        return self._conn.executescript(sql_script)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        pass


class VectorStore:
    """
    Thread-safe SQLite + sqlite-vec engine managing file indexing, cosine search,
    UMAP+HDBSCAN shelf clustering, near-duplicate diffs, and atomic filesystem operations.
    """

    def __init__(self, db_path: Optional[Path] = None, embedder: Optional[MultimodalEmbedder] = None) -> None:
        self.data_dir = get_default_data_dir()
        self.db_path = db_path or (self.data_dir / "index.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder or MultimodalEmbedder()
        self._lock = threading.RLock()
        self._dl_lock = threading.Lock()
        self._vision_download_state: Optional[dict[str, Any]] = None
        self.silhouette_score: float = 0.68
        self._stage_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self.recent_events: list[dict[str, Any]] = []
        self._raw_conn: Optional[sqlite3.Connection] = None
        self._settings_cache: Optional[dict[str, Any]] = None
        self._state_cache: Optional[dict[str, Any]] = None
        self._lookalikes_cache: Optional[tuple[Any, list[dict[str, Any]]]] = None
        self._shutdown_event = threading.Event()
        self._init_db()
        default_vis = resolve_default_vision_model(self.embedder.models_dir)
        active_vis = self.get_settings().get("vision_model", default_vis)
        if active_vis and active_vis != self.embedder.vision_model_id:
            self.embedder.set_vision_model(active_vis)

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    def _invalidate_cache(self, settings_changed: bool = False) -> None:
        self._state_cache = None
        if settings_changed:
            self._settings_cache = None

    def register_stage_callback(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._stage_callbacks.append(cb)

    def _emit_stage(
        self,
        stage: int,
        stage_name: str,
        status: str,
        file_name: str,
        message: str,
        elapsed_ms: float,
        file_id: Optional[str] = None,
        shelf_name: Optional[str] = None,
    ) -> None:
        evt = {
            "stage": stage,
            "stage_name": stage_name,
            "status": status,
            "file_name": file_name,
            "file_id": file_id,
            "shelf_name": shelf_name,
            "message": message,
            "elapsed_ms": round(elapsed_ms, 1),
            "timestamp": time.time(),
        }
        self.recent_events.insert(0, evt)
        del self.recent_events[40:]
        for cb in list(self._stage_callbacks):
            try:
                cb(evt)
            except Exception:
                pass

    def emit_progress(self, operation: str, current: int, total: int, file_name: str = "") -> None:
        """Broadcast a live progress event to SSE subscribers."""
        evt = {
            "type": "progress",
            "operation": operation,
            "current": current,
            "total": max(1, total),
            "file_name": file_name,
            "timestamp": time.time(),
        }
        for cb in list(self._stage_callbacks):
            try:
                cb(evt)
            except Exception:
                pass

    def _connect(self) -> _PooledConnection:
        if self._raw_conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA cache_size=-16000;")
            self._raw_conn = conn
        return _PooledConnection(self._raw_conn)

    def close(self) -> None:
        with self._lock:
            if self._raw_conn is not None:
                try:
                    self._raw_conn.close()
                except Exception:
                    pass
                self._raw_conn = None

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS files (
                        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT UNIQUE NOT NULL,
                        path TEXT UNIQUE NOT NULL,
                        name TEXT NOT NULL,
                        original_name TEXT NOT NULL,
                        suggested_name TEXT,
                        rename_reason TEXT,
                        renamed INTEGER DEFAULT 0,
                        title TEXT NOT NULL,
                        ext TEXT NOT NULL,
                        modality TEXT NOT NULL,
                        folder TEXT NOT NULL,
                        folder_short TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        size_human TEXT NOT NULL,
                        mtime REAL NOT NULL,
                        created_date TEXT NOT NULL,
                        display_date TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        shelf_id TEXT DEFAULT 'tray',
                        shelf_name TEXT DEFAULT 'uncategorized',
                        sub_shelf TEXT DEFAULT '',
                        cluster_id INTEGER DEFAULT -1,
                        visual_share INTEGER DEFAULT 50,
                        text_share INTEGER DEFAULT 50,
                        extracted_text TEXT DEFAULT '',
                        ocr_text TEXT DEFAULT '',
                        ocr_confidence REAL DEFAULT 0.0,
                        why_matched TEXT DEFAULT '',
                        specimen_type TEXT DEFAULT 'text',
                        width INTEGER DEFAULT 0,
                        height INTEGER DEFAULT 0,
                        page_count INTEGER DEFAULT 1,
                        dhash TEXT DEFAULT '',
                        map_x REAL DEFAULT 50.0,
                        map_y REAL DEFAULT 50.0,
                        thumbnail_b64 TEXT,
                        vector_norm REAL DEFAULT 1.0,
                        archived INTEGER DEFAULT 0,
                        embedding_blob BLOB NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_files_archived_mtime ON files(archived, mtime DESC);
                    CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);

                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_files USING vec0(
                        embedding float[512] distance_metric=cosine
                    );

                    CREATE TABLE IF NOT EXISTS shelves (
                        shelf_id TEXT PRIMARY KEY,
                        cluster_id INTEGER NOT NULL,
                        num TEXT NOT NULL,
                        name TEXT NOT NULL,
                        desc TEXT NOT NULL,
                        centroid_x REAL DEFAULT 50.0,
                        centroid_y REAL DEFAULT 50.0,
                        seq_order INTEGER DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS watched_folders (
                        path TEXT PRIMARY KEY,
                        display_path TEXT NOT NULL,
                        active INTEGER DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS rename_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id TEXT NOT NULL,
                        old_path TEXT NOT NULL,
                        new_path TEXT NOT NULL,
                        old_name TEXT NOT NULL,
                        new_name TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        undone INTEGER DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS tidy_moves_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        batch_id TEXT NOT NULL,
                        file_id TEXT NOT NULL,
                        old_path TEXT NOT NULL,
                        new_path TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        undone INTEGER DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS archive_history (
                        pair_id TEXT PRIMARY KEY,
                        file_id TEXT NOT NULL,
                        original_path TEXT NOT NULL,
                        archived_path TEXT NOT NULL,
                        pair_json TEXT NOT NULL,
                        timestamp REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS action_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action_type TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        detail_json TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        undone INTEGER DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS saved_searches (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        query TEXT NOT NULL,
                        modality TEXT DEFAULT 'all',
                        tag TEXT DEFAULT '',
                        created_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS custom_shelves (
                        shelf_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        desc TEXT DEFAULT '',
                        query TEXT DEFAULT '',
                        modality TEXT DEFAULT 'all',
                        tag TEXT DEFAULT '',
                        file_ids_json TEXT DEFAULT '[]',
                        pinned INTEGER DEFAULT 0,
                        created_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS shelf_renames (
                        shelf_id TEXT PRIMARY KEY,
                        custom_name TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS user_affinities (
                        token TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        weight REAL DEFAULT 1.0,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (token, target_id, kind)
                    );

                    CREATE TABLE IF NOT EXISTS pinned_folders (
                        path TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        pinned_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS ai_memory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        key_pattern TEXT NOT NULL,
                        value TEXT NOT NULL,
                        weight REAL DEFAULT 1.0,
                        hits INTEGER DEFAULT 1,
                        updated_at REAL NOT NULL,
                        UNIQUE(kind, key_pattern, value)
                    );
                    """
                )

                # Safe schema migration for existing databases
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
                if "metadata_json" not in cols:
                    conn.execute("ALTER TABLE files ADD COLUMN metadata_json TEXT DEFAULT '{}'")
                if "tags_json" not in cols:
                    conn.execute("ALTER TABLE files ADD COLUMN tags_json TEXT DEFAULT '[]'")
                if "pinned" not in cols:
                    conn.execute("ALTER TABLE files ADD COLUMN pinned INTEGER DEFAULT 0")
                if "sub_shelf" not in cols:
                    conn.execute("ALTER TABLE files ADD COLUMN sub_shelf TEXT DEFAULT ''")
                if "user_shelf_id" not in cols:
                    conn.execute("ALTER TABLE files ADD COLUMN user_shelf_id TEXT DEFAULT ''")
                if "original_path" not in cols:
                    conn.execute("ALTER TABLE files ADD COLUMN original_path TEXT DEFAULT ''")

                shelf_cols = {r["name"] for r in conn.execute("PRAGMA table_info(shelves)").fetchall()}
                if "pinned" not in shelf_cols:
                    conn.execute("ALTER TABLE shelves ADD COLUMN pinned INTEGER DEFAULT 0")

                existing_file_count = conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
                defaults = {
                    "realtime_watch": "true",
                    "ocr_enabled": "true",
                    "code_indexing": "true",
                    "battery_pause": "false",
                    "trash_mode": "reversible",
                    "max_scan_files": "500",
                    "ignore_patterns": ".git,node_modules,target,dist,build,.venv,__pycache__",
                    "ollama_enabled": "false",
                    "max_results": "50",
                    "min_similarity": "0.22",
                    "silhouette_score": "0.0",
                    "setup_completed": "true" if existing_file_count > 0 else "false",
                    "user_name": "",
                    "user_focus_areas": "[]",
                    "adaptive_learning": "true",
                    "terms_accepted": "false",
                }
                for k, v in defaults.items():
                    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

                for folder_name in ["Documents", "Downloads", "Desktop"]:
                    f_path = get_user_special_dir(folder_name)
                    f_path.mkdir(parents=True, exist_ok=True)
                    conn.execute(
                        "INSERT OR IGNORE INTO watched_folders (path, display_path, active) VALUES (?, ?, 1)",
                        (str(f_path), format_path_short(f_path)),
                    )

                # Purge any legacy tiny <16 byte scratch text files from the index
                tiny_rows = conn.execute(
                    "SELECT rowid FROM files WHERE size_bytes < 16 AND ext IN ('txt', 'md', 'log')"
                ).fetchall()
                for tr in tiny_rows:
                    conn.execute("DELETE FROM vec_files WHERE rowid = ?", (tr["rowid"],))
                    conn.execute("DELETE FROM files WHERE rowid = ?", (tr["rowid"],))

                conn.commit()
            finally:
                conn.close()

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            if self._settings_cache is not None:
                return dict(self._settings_cache)
            conn = self._connect()
            try:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
                raw = {r["key"]: r["value"] for r in rows}
                focus_raw = raw.get("user_focus_areas", "[]")
                try:
                    focus_parsed = json.loads(focus_raw)
                    if not isinstance(focus_parsed, list):
                        focus_parsed = []
                except Exception:
                    focus_parsed = []
                default_vis = resolve_default_vision_model(self.embedder.models_dir)
                vis_model = raw.get("vision_model") or default_vis
                if resolve_vision_model_paths(self.embedder.models_dir, vis_model)["vision"] is None:
                    vis_model = default_vis
                parsed = {
                    "realtime_watch": raw.get("realtime_watch", "true") == "true",
                    "ocr_enabled": raw.get("ocr_enabled", "true") == "true",
                    "code_indexing": raw.get("code_indexing", "true") == "true",
                    "battery_pause": raw.get("battery_pause", "false") == "true",
                    "trash_mode": raw.get("trash_mode", "reversible"),
                    "max_scan_files": int(raw.get("max_scan_files", "500")),
                    "ignore_patterns": raw.get("ignore_patterns", ".git,node_modules,target,dist,build,.venv,__pycache__"),
                    "ollama_enabled": raw.get("ollama_enabled", "false") == "true",
                    "max_results": int(raw.get("max_results", "50")),
                    "min_similarity": float(raw.get("min_similarity", "0.22")),
                    "silhouette_score": float(raw.get("silhouette_score", "0.0")),
                    "setup_completed": raw.get("setup_completed", "true") == "true",
                    "vision_model": vis_model,
                    "user_name": raw.get("user_name", ""),
                    "user_focus_areas": [str(x) for x in focus_parsed if str(x).strip()],
                    "adaptive_learning": raw.get("adaptive_learning", "true") == "true",
                    "terms_accepted": raw.get("terms_accepted", "false") == "true",
                }
                self._settings_cache = parsed
                return dict(parsed)
            finally:
                conn.close()

    def is_paused_on_battery(self) -> bool:
        if not self.get_settings().get("battery_pause", False):
            return False
        try:
            bat = psutil.sensors_battery()
            return bool(bat is not None and not bat.power_plugged)
        except Exception:
            return False

    def update_setting(self, key: str, value: Any) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                str_val = "true" if value is True else ("false" if value is False else str(value))
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str_val),
                )
                conn.commit()
                self._invalidate_cache(settings_changed=True)
            finally:
                conn.close()
        return self.get_settings()

    def get_watched_folders(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                folders = conn.execute("SELECT path, display_path, active FROM watched_folders ORDER BY display_path").fetchall()
                all_files = conn.execute("SELECT path FROM files WHERE archived = 0").fetchall()
                file_paths = [str(r["path"]) for r in all_files]

                result: list[dict[str, Any]] = []
                for f in folders:
                    f_str = str(f["path"]).rstrip("\\/")
                    f_prefix = f_str + os.sep
                    f_slash_prefix = f_str + "/"
                    count = sum(
                        1
                        for fp in file_paths
                        if fp == f_str or fp.startswith(f_prefix) or fp.startswith(f_slash_prefix)
                    )
                    result.append(
                        {
                            "path": str(f["path"]),
                            "display_path": f["display_path"],
                            "files": count,
                            "active": bool(f["active"]),
                        }
                    )
                return result
            finally:
                conn.close()

    def add_watched_folder(self, folder_path: str) -> list[dict[str, Any]]:
        raw = (folder_path or "").strip()
        if not raw:
            raise ValueError("Folder path cannot be empty")
        if raw == "~/Pictures":
            cand = (Path.home() / "Pictures").resolve()
            resolved = cand if cand.exists() else get_user_special_dir("Pictures")
        else:
            resolved = Path(os.path.expanduser(raw)).resolve()
        if not resolved.exists():
            raise ValueError(f"Folder does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"Path is not a folder: {resolved}")
        if not os.access(resolved, os.R_OK):
            raise ValueError(f"Permission denied reading folder: {resolved}")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO watched_folders (path, display_path, active) VALUES (?, ?, 1) "
                    "ON CONFLICT(path) DO UPDATE SET active = 1",
                    (str(resolved), format_path_short(resolved)),
                )
                shelves_empty = (conn.execute("SELECT COUNT(*) FROM shelves").fetchone()[0] or 0) == 0
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        self.emit_progress(
            f"scanning {resolved.name}",
            0,
            1,
            f"discovering files in {format_path_short(resolved)}…",
        )
        max_scan = max(10, int(self.get_settings().get("max_scan_files", 500)))
        scanned = self.scan_folder(resolved, max_files=max_scan)
        if scanned > 0 or shelves_empty:
            self.emit_progress("organizing shelves", 0, 1, "grouping files by topic…")
            self.recluster_all()
        self.emit_progress("idle", 1, 1, "")
        return self.get_watched_folders()

    def toggle_watched_folder(self, folder_path: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT active FROM watched_folders WHERE path = ? OR display_path = ?", (folder_path, folder_path)).fetchone()
                if row:
                    new_state = 0 if row["active"] else 1
                    conn.execute(
                        "UPDATE watched_folders SET active = ? WHERE path = ? OR display_path = ?",
                        (new_state, folder_path, folder_path),
                    )
                    conn.commit()
                    self._invalidate_cache()
            finally:
                conn.close()
        return self.get_watched_folders()

    def remove_watched_folder(self, folder_path: str) -> list[dict[str, Any]]:
        """Stop watching a folder and purge its indexed active files from local storage."""
        raw = (folder_path or "").strip()
        if not raw:
            raise ValueError("Folder path cannot be empty")
        try:
            target_resolved = Path(os.path.expanduser(raw)).resolve()
        except Exception:
            target_resolved = Path(raw)

        removed_any = False
        with self._lock:
            conn = self._connect()
            try:
                wf_rows = conn.execute("SELECT path, display_path FROM watched_folders").fetchall()
                matched_paths: list[str] = []
                for wf in wf_rows:
                    wf_p = Path(wf["path"])
                    try:
                        wf_res = wf_p.resolve()
                    except Exception:
                        wf_res = wf_p
                    if wf["path"] == raw or wf["display_path"] == raw or wf_res == target_resolved:
                        matched_paths.append(str(wf["path"]))
                        matched_paths.append(str(wf_res))
                        conn.execute("DELETE FROM watched_folders WHERE path = ?", (wf["path"],))
                        removed_any = True

                if matched_paths:
                    folder_roots = []
                    for mp in dict.fromkeys(matched_paths):
                        try:
                            folder_roots.append(Path(mp).resolve())
                        except Exception:
                            folder_roots.append(Path(mp))
                    file_rows = conn.execute("SELECT rowid, path FROM files WHERE archived = 0").fetchall()
                    for fr in file_rows:
                        fp = Path(fr["path"])
                        try:
                            fp_res = fp.resolve()
                        except Exception:
                            fp_res = fp
                        if any(fp_res == root or root in fp_res.parents for root in folder_roots):
                            conn.execute("DELETE FROM vec_files WHERE rowid = ?", (fr["rowid"],))
                            conn.execute("DELETE FROM files WHERE rowid = ?", (fr["rowid"],))

                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()

        if removed_any:
            self.recluster_all()
        return self.get_watched_folders()

    def _build_default_why_matched(self, parsed: ParsedFile) -> str:
        if parsed.specimen_type == "receipt":
            snippet = (parsed.ocr_text or parsed.extracted_text)[:76].strip()
            return f"Matched receipt layout and OCR text (“{snippet}”)."
        if parsed.specimen_type == "diagram":
            snippet = (parsed.extracted_text or parsed.ocr_text)[:76].strip()
            return f"Matched schematic diagram structure and technical labels (“{snippet}”)."
        if parsed.specimen_type == "idcard":
            snippet = (parsed.ocr_text or parsed.extracted_text)[:76].strip()
            return f"Matched portrait ID card layout and enrollment text (“{snippet}”)."
        if parsed.modality == "code":
            fn_str = ", ".join(f"{fn}()" for fn in parsed.code_symbols[:2]) if parsed.code_symbols else parsed.name
            return f"Matched code structure and symbols ({fn_str})."
        if parsed.modality == "image" and parsed.visual_tags:
            scene_tags = [
                t for t in parsed.visual_tags
                if t not in {"landscape image", "portrait image", "square image"}
            ]
            if scene_tags:
                return f"Matched visual photo content ({', '.join(scene_tags[:2])})."
            return "Grouped by visual layout similarity."
        snippet = (parsed.extracted_text or parsed.title)[:82].replace("\n", " ").strip()
        return f"Grouped by document content and topic." if not snippet else f"Grouped by content similarity ({snippet[:60]})."

    def index_single_file(
        self,
        filepath: Path,
        force: bool = False,
        emit_telemetry: bool = True,
        recluster: bool = False,
        commit: bool = True,
        return_record: bool = True,
        precomputed_sha256: Optional[str] = None,
        precomputed_parsed: Optional[ParsedFile] = None,
        precomputed_stat: Optional[os.stat_result] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Run the 4-step local ingestion pipeline on a single file:
          01 Notice -> 02 Read & See (Parse + OCR) -> 03 Understand (512-d ONNX Embed) -> 04 Remember (SQLite-vec)
        """
        t_start = time.perf_counter()
        if precomputed_parsed is not None:
            filepath = precomputed_parsed.path
            ext = filepath.suffix.lower()
            stat_mtime = precomputed_parsed.mtime
            stat_size = precomputed_parsed.size_bytes
        else:
            filepath = Path(filepath).resolve()
            if precomputed_stat is not None:
                if not stat_mod.S_ISREG(precomputed_stat.st_mode):
                    return None
                stat = precomputed_stat
            else:
                if not filepath.exists() or not filepath.is_file():
                    return None
                stat = filepath.stat()

            if filepath.name.startswith(".") or "GoodShelves_Archive" in filepath.parts or "GoodShelves_Trash" in filepath.parts:
                return None

            ext = filepath.suffix.lower()
            all_supported = IMAGE_EXTS | DOC_EXTS | CODE_EXTS | VIDEO_EXTS | AUDIO_EXTS | SPREADSHEET_EXTS | ARCHIVE_EXTS
            if ext not in all_supported:
                return None

            if stat.st_size == 0 or (ext in {".txt", ".md", ".log"} and stat.st_size < 16):
                return None
            stat_mtime = stat.st_mtime
            stat_size = stat.st_size

        sha256 = precomputed_sha256 or (precomputed_parsed.sha256 if precomputed_parsed is not None else "")
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT id, sha256, mtime, size_bytes, path FROM files WHERE path = ?",
                    (str(filepath),),
                ).fetchone()
                # Fast-path: if mtime and size_bytes haven't changed, skip reading & SHA-256 hashing the file
                if (
                    existing
                    and not force
                    and abs(float(existing["mtime"]) - stat_mtime) < 1e-3
                    and int(existing["size_bytes"]) == stat_size
                ):
                    if return_record:
                        return self.get_file_by_id(existing["id"], conn=conn)
                    return {"id": existing["id"], "_newlyIndexed": False}

                if not sha256:
                    sha256 = compute_sha256(filepath)
                if existing and not force and existing["sha256"] == sha256:
                    if return_record:
                        return self.get_file_by_id(existing["id"], conn=conn)
                    return {"id": existing["id"], "_newlyIndexed": False}

                # Check if file was moved/renamed on disk (matching sha256 where old path no longer exists)
                moved_candidate = conn.execute(
                    "SELECT rowid, id, path, original_name FROM files WHERE sha256 = ? AND archived = 0",
                    (sha256,),
                ).fetchall()
                for mc in moved_candidate:
                    old_p = Path(mc["path"])
                    if not old_p.exists() and old_p != filepath:
                        folder_p = filepath.parent
                        conn.execute(
                            "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ?, mtime = ? WHERE id = ?",
                            (
                                str(filepath),
                                filepath.name,
                                str(folder_p),
                                format_path_short(folder_p),
                                stat_mtime,
                                mc["id"],
                            ),
                        )
                        if commit:
                            conn.commit()
                        self._invalidate_cache()
                        if return_record:
                            return self.get_file_by_id(mc["id"], conn=conn)
                        return {"id": mc["id"], "_newlyIndexed": False}
            finally:
                conn.close()

        settings = self.get_settings()

        # STEP 01: NOTICE
        t1 = (time.perf_counter() - t_start) * 1000.0
        if emit_telemetry:
            self._emit_stage(
                1,
                "noticed in folder",
                "detected",
                filepath.name,
                f'01 · Noticed "{filepath.name}" saved in {format_path_short(filepath.parent)}…',
                t1,
            )

        # STEP 02: PARSER & OCR
        if precomputed_parsed is not None:
            parsed = precomputed_parsed
        else:
            parsed = parse_file(
                filepath,
                enable_ocr=settings.get("ocr_enabled", True),
                enable_code=settings.get("code_indexing", True),
                precomputed_sha256=sha256,
                precomputed_stat=precomputed_stat,
            )
            if parsed is None:
                return None

        t2 = (time.perf_counter() - t_start) * 1000.0
        if emit_telemetry:
            self._emit_stage(
                2,
                "read words & visual layout",
                "parsed + ocr",
                filepath.name,
                f'02 · Read text & visual layout inside "{filepath.name}" ({parsed.modality})…',
                t2,
            )

        # STEP 03: MULTIMODAL EMBEDDING
        vec_512, visual_share, text_share = self.embedder.embed_parsed_file(parsed)
        vec_norm = float(np.linalg.norm(vec_512))
        t3 = (time.perf_counter() - t_start) * 1000.0
        if emit_telemetry:
            self._emit_stage(
                3,
                "mapped into shared meaning space",
                "512-d vector",
                filepath.name,
                f'03 · Projected "{filepath.name}" into 512-d L2 vector space (visual {visual_share}% / text {text_share}%)…',
                t3,
            )

        # STEP 04: SHELVE & RENAME SUGGESTION
        if parsed.visual_tags:
            parsed.metadata = {**(parsed.metadata or {}), "visualConcepts": parsed.visual_tags[:8]}
            if parsed.modality == "image" and not parsed.extracted_text.strip():
                scene_tags = [t for t in parsed.visual_tags if t not in {"landscape image", "portrait image", "square image"}]
                if scene_tags:
                    parsed.extracted_text = ", ".join(scene_tags[:8])
        suggested_name, rename_reason = generate_rename_suggestion(parsed)
        why_matched = self._build_default_why_matched(parsed)
        folder_p = filepath.parent
        folder_short = format_path_short(folder_p)
        size_human = format_bytes(parsed.size_bytes)
        vec_blob = pack_f32(vec_512)
        meta_json = json.dumps(parsed.metadata or {}, ensure_ascii=False)

        probe_meta = {
            "name": parsed.name,
            "title": parsed.title,
            "format": parsed.ext,
            "ext": parsed.ext,
            "modality": parsed.modality,
            "specimen_type": parsed.specimen_type,
            "ocr_text": parsed.ocr_text,
            "extracted_text": parsed.extracted_text,
            "metadata_json": meta_json,
        }
        init_cat = _classify_single_file_category(probe_meta)
        init_shelf_id, init_shelf_name, _ = _GENERAL_CATEGORIES.get(init_cat, _GENERAL_CATEGORIES["documents"])
        init_sub_shelf = derive_specific_sub_shelf(probe_meta, init_cat)

        with self._lock:
            conn = self._connect()
            try:
                existing_row = conn.execute("SELECT rowid, id, original_name, renamed FROM files WHERE path = ?", (str(filepath),)).fetchone()
                if existing_row:
                    rowid = int(existing_row["rowid"])
                    file_id = str(existing_row["id"])
                    renamed_flag = int(existing_row["renamed"])
                    conn.execute(
                        """
                        UPDATE files SET
                            name = ?, suggested_name = ?, rename_reason = ?, title = ?,
                            ext = ?, modality = ?, folder = ?, folder_short = ?,
                            size_bytes = ?, size_human = ?, mtime = ?, created_date = ?,
                            display_date = ?, sha256 = ?, visual_share = ?, text_share = ?,
                            extracted_text = ?, ocr_text = ?, ocr_confidence = ?, why_matched = ?,
                            specimen_type = ?, width = ?, height = ?, page_count = ?, dhash = ?,
                            thumbnail_b64 = ?, vector_norm = ?, archived = 0, embedding_blob = ?,
                            metadata_json = ?
                        WHERE rowid = ?
                        """,
                        (
                            parsed.name,
                            suggested_name if not renamed_flag else None,
                            rename_reason if not renamed_flag else None,
                            parsed.title,
                            parsed.ext,
                            parsed.modality,
                            str(folder_p),
                            folder_short,
                            parsed.size_bytes,
                            size_human,
                            parsed.mtime,
                            parsed.created_date,
                            parsed.display_date,
                            parsed.sha256,
                            visual_share,
                            text_share,
                            parsed.extracted_text,
                            parsed.ocr_text,
                            parsed.ocr_confidence,
                            why_matched,
                            parsed.specimen_type,
                            parsed.width,
                            parsed.height,
                            parsed.page_count,
                            parsed.dhash,
                            parsed.thumbnail_b64,
                            round(vec_norm, 4),
                            vec_blob,
                            meta_json,
                            rowid,
                        ),
                    )
                    conn.execute("DELETE FROM vec_files WHERE rowid = ?", (rowid,))
                    conn.execute("INSERT INTO vec_files(rowid, embedding) VALUES (?, ?)", (rowid, vec_blob))
                else:
                    file_id = f"f_{parsed.sha256[:10]}_{int(parsed.mtime) % 10000}"
                    collision = conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone()
                    if collision:
                        file_id = f"f_{parsed.sha256[:8]}_{_stable_short_hash(str(filepath), 8)}"
                    cur = conn.execute(
                        """
                        INSERT INTO files (
                            id, path, name, original_name, suggested_name, rename_reason, renamed,
                            title, ext, modality, folder, folder_short, size_bytes, size_human,
                            mtime, created_date, display_date, sha256, shelf_id, shelf_name, sub_shelf,
                            visual_share, text_share, extracted_text, ocr_text, ocr_confidence,
                            why_matched, specimen_type, width, height, page_count, dhash,
                            thumbnail_b64, vector_norm, archived, embedding_blob, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            file_id,
                            str(filepath),
                            parsed.name,
                            parsed.name,
                            suggested_name,
                            rename_reason,
                            parsed.title,
                            parsed.ext,
                            parsed.modality,
                            str(folder_p),
                            folder_short,
                            parsed.size_bytes,
                            size_human,
                            parsed.mtime,
                            parsed.created_date,
                            parsed.display_date,
                            parsed.sha256,
                            init_shelf_id,
                            init_shelf_name,
                            init_sub_shelf,
                            visual_share,
                            text_share,
                            parsed.extracted_text,
                            parsed.ocr_text,
                            parsed.ocr_confidence,
                            why_matched,
                            parsed.specimen_type,
                            parsed.width,
                            parsed.height,
                            parsed.page_count,
                            parsed.dhash,
                            parsed.thumbnail_b64,
                            round(vec_norm, 4),
                            vec_blob,
                            meta_json,
                        ),
                    )
                    rowid = int(cur.lastrowid)
                    conn.execute("INSERT INTO vec_files(rowid, embedding) VALUES (?, ?)", (rowid, vec_blob))
                if commit:
                    conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()

        if recluster:
            self.recluster_all()

        if not return_record and not emit_telemetry and not recluster:
            return {"id": file_id, "_newlyIndexed": True}

        item_dict = self.get_file_by_id(file_id)
        if item_dict is not None:
            item_dict["_newlyIndexed"] = True
        t4 = (time.perf_counter() - t_start) * 1000.0
        if emit_telemetry and item_dict:
            shelf_label = item_dict.get("shelfName") or item_dict.get("shelf_name") or "uncategorized"
            self._emit_stage(
                4,
                "placed onto shelf & suggested clean name",
                "shelved",
                filepath.name,
                f'04 · Shelved "{item_dict.get("title", filepath.name)}" onto {shelf_label} ({round(t4)} ms total)',
                t4,
                file_id=file_id,
                shelf_name=shelf_label,
            )
        return item_dict

    def scan_folder(
        self,
        folder: Path,
        max_files: int = 60,
        recursive: bool = True,
        emit_idle: bool = True,
    ) -> int:
        """Scan a folder and index supported new or modified files quickly."""
        folder = Path(folder).resolve()
        if not folder.exists() or not folder.is_dir():
            return 0
        if self.is_paused_on_battery():
            return 0

        settings = self.get_settings()
        code_enabled = bool(settings.get("code_indexing", True))
        ocr_enabled = bool(settings.get("ocr_enabled", True))
        raw_ignores = str(settings.get("ignore_patterns", ""))
        ignore_parts = {p.strip() for p in raw_ignores.split(",") if p.strip()} | {
            "GoodShelves_Archive",
            "GoodShelves_Trash",
            "node_modules",
            ".venv",
            "__pycache__",
            ".git",
            "target",
            "dist",
            "build",
        }
        all_supported = IMAGE_EXTS | DOC_EXTS | VIDEO_EXTS | AUDIO_EXTS | SPREADSHEET_EXTS | ARCHIVE_EXTS
        if code_enabled:
            all_supported = all_supported | CODE_EXTS

        candidates: list[tuple[Path, os.stat_result]] = []
        stack = [str(folder)]
        seen_dirs: set[str] = {str(folder)}
        while stack:
            cur_dir = stack.pop()
            try:
                with os.scandir(cur_dir) as it:
                    for entry in it:
                        name = entry.name
                        if name.startswith("."):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if recursive and name not in ignore_parts:
                                    real_d = os.path.realpath(entry.path)
                                    if real_d not in seen_dirs:
                                        seen_dirs.add(real_d)
                                        stack.append(entry.path)
                                continue
                            is_sym = entry.is_symlink()
                            if not is_sym and not entry.is_file(follow_symlinks=False):
                                continue
                            ext = os.path.splitext(name)[1].lower()
                            if ext not in all_supported:
                                continue
                            fp = Path(entry.path).resolve() if is_sym else Path(entry.path)
                            st = fp.stat() if is_sym else entry.stat(follow_symlinks=False)
                            if is_sym and not stat_mod.S_ISREG(st.st_mode):
                                continue
                            sz = st.st_size
                            if sz == 0 or sz > 500 * 1024 * 1024:
                                continue
                            if ext in {".txt", ".md", ".log"} and sz < 16:
                                continue
                            candidates.append((fp, st))
                        except OSError:
                            continue
            except (PermissionError, OSError):
                continue

        # Prefer the newest files when a large folder exceeds the scan budget.
        candidates.sort(key=lambda item: item[1].st_mtime, reverse=True)
        batch = candidates[:max_files] if max_files > 0 else candidates
        if not batch:
            return 0

        existing_map: dict[str, tuple[float, int, str]] = {}
        sha_to_paths: dict[str, list[str]] = {}
        with self._lock:
            conn = self._connect()
            try:
                for r in conn.execute("SELECT path, mtime, size_bytes, sha256 FROM files WHERE archived = 0").fetchall():
                    p_str = str(r["path"])
                    sha_val = str(r["sha256"] or "")
                    existing_map[p_str] = (float(r["mtime"]), int(r["size_bytes"]), sha_val)
                    if sha_val:
                        sha_to_paths.setdefault(sha_val, []).append(p_str)
            finally:
                conn.close()

        to_index: list[tuple[Path, os.stat_result]] = []
        for fp, st in batch:
            ex = existing_map.get(str(fp))
            if ex is not None and abs(ex[0] - st.st_mtime) < 1e-3 and ex[1] == st.st_size:
                continue
            to_index.append((fp, st))

        if not to_index:
            return 0

        indexed = 0
        total = len(to_index)

        def _prepare_file_for_index(item: tuple[Path, os.stat_result]) -> tuple[str, Path, os.stat_result, str, Optional[ParsedFile]]:
            fp, st = item
            try:
                sha = compute_sha256(fp)
                ex = existing_map.get(str(fp))
                if ex is not None and ex[2] == sha:
                    return ("unchanged", fp, st, sha, None)
                for old_p_str in sha_to_paths.get(sha, ()):
                    old_p = Path(old_p_str)
                    if old_p != fp and not old_p.exists():
                        return ("moved", fp, st, sha, None)
                parsed = parse_file(
                    fp,
                    enable_ocr=ocr_enabled,
                    enable_code=code_enabled,
                    precomputed_sha256=sha,
                    precomputed_stat=st,
                )
                return ("parsed", fp, st, sha, parsed)
            except Exception:
                return ("error", fp, st, "", None)

        try:
            if total == 1:
                fp, st = to_index[0]
                if not self._shutdown_event.is_set() and not self.is_paused_on_battery():
                    self.emit_progress(f"indexing {folder.name}", 1, 1, fp.name)
                    try:
                        res = self.index_single_file(
                            fp,
                            force=False,
                            emit_telemetry=False,
                            recluster=False,
                            commit=True,
                            return_record=False,
                            precomputed_stat=st,
                        )
                        if res is not None:
                            indexed += 1
                    except Exception:
                        pass
            else:
                workers = min(4, os.cpu_count() or 2)
                chunk_size = 16
                done_idx = 0
                self.emit_progress(f"indexing {folder.name}", 0, total, f"Analyzing {total} files…")
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    for start in range(0, total, chunk_size):
                        if self._shutdown_event.is_set() or self.is_paused_on_battery():
                            break
                        chunk = to_index[start : start + chunk_size]
                        prepared_chunk = list(pool.map(_prepare_file_for_index, chunk))
                        for status, fp, st, sha, parsed in prepared_chunk:
                            done_idx += 1
                            if self._shutdown_event.is_set() or self.is_paused_on_battery():
                                break
                            self.emit_progress(f"indexing {folder.name}", done_idx, total, fp.name)
                            if status in ("unchanged", "error"):
                                continue
                            try:
                                if status == "moved":
                                    res = self.index_single_file(
                                        fp,
                                        force=False,
                                        emit_telemetry=False,
                                        recluster=False,
                                        commit=False,
                                        return_record=False,
                                        precomputed_sha256=sha,
                                        precomputed_stat=st,
                                    )
                                    if res is not None:
                                        indexed += 1
                                elif parsed is not None:
                                    res = self.index_single_file(
                                        fp,
                                        force=False,
                                        emit_telemetry=False,
                                        recluster=False,
                                        commit=False,
                                        return_record=False,
                                        precomputed_sha256=sha,
                                        precomputed_parsed=parsed,
                                        precomputed_stat=st,
                                    )
                                    if res is not None:
                                        indexed += 1
                            except Exception:
                                continue
                        with self._lock:
                            self._connect().commit()
        finally:
            with self._lock:
                self._connect().commit()
            if emit_idle and total > 0:
                self.emit_progress("idle", total, total, "")
        return indexed

    def recluster_all(self) -> dict[str, Any]:
        """Run UMAP + HDBSCAN over all active files in SQLite and persist discovered shelves."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM files WHERE archived = 0 ORDER BY mtime DESC"
                ).fetchall()
                if not rows:
                    self._invalidate_cache()
                    return {"shelves": [], "silhouette": 0.0}
                files_meta = [dict(r) for r in rows]
                vectors = np.vstack([unpack_f32(r["embedding_blob"]) for r in rows])
            finally:
                conn.close()

        # Cluster unique content only outside self._lock so concurrent reads do not stall.
        representative: dict[str, int] = {}
        for idx, meta in enumerate(files_meta):
            key = str(meta.get("sha256") or meta["id"])
            representative.setdefault(key, idx)
        keep_indices = sorted(representative.values())

        shelves, unique_updates, sil_score = run_shelf_clustering(
            [files_meta[i] for i in keep_indices],
            vectors[keep_indices],
        )
        file_updates: dict[str, dict[str, Any]] = {}
        for meta in files_meta:
            rep_idx = representative[str(meta.get("sha256") or meta["id"])]
            upd = unique_updates.get(str(files_meta[rep_idx]["id"]))
            if upd:
                file_updates[str(meta["id"])] = dict(upd)

        with self._lock:
            self.silhouette_score = sil_score
            conn = self._connect()
            try:
                pinned_rows = conn.execute("SELECT shelf_id, pinned FROM shelves WHERE pinned = 1").fetchall()
                pinned_by_id = {str(r["shelf_id"]) for r in pinned_rows}
                rename_rows = conn.execute("SELECT shelf_id, custom_name FROM shelf_renames").fetchall()
                custom_names = {str(r["shelf_id"]): str(r["custom_name"]) for r in rename_rows}
                custom_shelf_rows = conn.execute("SELECT shelf_id, name FROM custom_shelves").fetchall()
                custom_shelf_names = {str(r["shelf_id"]): str(r["name"]) for r in custom_shelf_rows}

                # Respect manual shelf overrides set by the user
                cat_by_sid = {v[0]: v for v in _GENERAL_CATEGORIES.values()}
                shelf_by_id = {sh.shelf_id: sh for sh in shelves}
                for meta in files_meta:
                    fid = str(meta["id"])
                    u_sid = str(meta.get("user_shelf_id") or "").strip()
                    if not u_sid or fid not in file_updates:
                        continue
                    if u_sid in cat_by_sid:
                        s_id, s_name, s_desc = cat_by_sid[u_sid]
                        for sh in shelves:
                            if fid in sh.file_ids and sh.shelf_id != s_id:
                                sh.file_ids.remove(fid)
                        if s_id not in shelf_by_id:
                            new_sh = DiscoveredShelf(
                                shelf_id=s_id,
                                cluster_id=-1,
                                num=f"{len(shelves) + 1:02d}",
                                name=s_name,
                                desc=s_desc,
                                file_ids=[fid],
                                centroid_x=float(file_updates[fid].get("map_x", 50.0)),
                                centroid_y=float(file_updates[fid].get("map_y", 50.0)),
                            )
                            shelves.append(new_sh)
                            shelf_by_id[s_id] = new_sh
                        elif fid not in shelf_by_id[s_id].file_ids:
                            shelf_by_id[s_id].file_ids.append(fid)
                        file_updates[fid]["shelf_id"] = s_id
                        file_updates[fid]["shelf_name"] = custom_names.get(s_id, s_name)
                    elif u_sid in custom_shelf_names:
                        for sh in shelves:
                            if fid in sh.file_ids:
                                sh.file_ids.remove(fid)
                        file_updates[fid]["shelf_id"] = u_sid
                        file_updates[fid]["shelf_name"] = custom_shelf_names[u_sid]

                shelves = [sh for sh in shelves if sh.file_ids]
                for idx, sh in enumerate(shelves, start=1):
                    sh.num = f"{idx:02d}"

                conn.execute("DELETE FROM shelves")
                shelf_inserts = [
                    (
                        sh.shelf_id,
                        sh.cluster_id,
                        sh.num,
                        custom_names.get(sh.shelf_id, sh.name),
                        sh.desc,
                        sh.centroid_x,
                        sh.centroid_y,
                        1 if sh.shelf_id in pinned_by_id else 0,
                        idx,
                    )
                    for idx, sh in enumerate(shelves)
                ]
                if shelf_inserts:
                    conn.executemany(
                        """
                        INSERT INTO shelves (shelf_id, cluster_id, num, name, desc, centroid_x, centroid_y, pinned, seq_order)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        shelf_inserts,
                    )

                file_update_rows = []
                for fid, upd in file_updates.items():
                    s_id = upd["shelf_id"]
                    s_name = custom_names[s_id].strip() if s_id in custom_names else upd["shelf_name"]
                    sub_name = str(upd.get("sub_shelf") or "")
                    file_update_rows.append(
                        (
                            upd["cluster_id"],
                            s_id,
                            s_name,
                            sub_name,
                            upd["map_x"],
                            upd["map_y"],
                            fid,
                        )
                    )
                if file_update_rows:
                    conn.executemany(
                        """
                        UPDATE files SET
                            cluster_id = ?, shelf_id = ?, shelf_name = ?, sub_shelf = ?, map_x = ?, map_y = ?
                        WHERE id = ?
                        """,
                        file_update_rows,
                    )

                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('silhouette_score', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(sil_score),),
                )
                conn.commit()
                self._invalidate_cache(settings_changed=True)
                return {"shelves": [sh.__dict__ for sh in shelves], "silhouette": sil_score}
            finally:
                conn.close()

    def _row_to_file_dict(self, r: sqlite3.Row | dict[str, Any], cosine_sim: Optional[float] = None, match_score: Optional[int] = None, custom_why: Optional[str] = None) -> dict[str, Any]:
        d = dict(r)
        cos_val = cosine_sim if cosine_sim is not None else 0.92
        score_val = match_score if match_score is not None else min(99, max(72, int(round(cos_val * 100))))
        meta_obj: dict[str, Any] = {}
        raw_meta_json = d.get("metadata_json")
        if raw_meta_json:
            try:
                meta_obj = json.loads(raw_meta_json)
                if isinstance(meta_obj, dict):
                    meta_obj.pop("rawSnippet", None)
            except Exception:
                meta_obj = {}
        tags_list: list[str] = []
        raw_tags_json = d.get("tags_json")
        if raw_tags_json:
            try:
                parsed_tags = json.loads(raw_tags_json)
                if isinstance(parsed_tags, list):
                    tags_list = [str(t) for t in parsed_tags if str(t).strip()]
            except Exception:
                tags_list = []
        return {
            "id": d["id"],
            "path": d["path"],
            "name": d["name"],
            "originalName": d["original_name"],
            "suggestedName": d["suggested_name"],
            "renameReason": d["rename_reason"],
            "renamed": bool(d["renamed"]),
            "title": d["title"],
            "format": d["ext"],
            "modality": d["modality"],
            "folder": d["folder_short"],
            "folderFull": d["folder"],
            "size": d["size_human"],
            "sizeBytes": d["size_bytes"],
            "date": d["display_date"],
            "createdDate": d["created_date"],
            "mtime": d["mtime"],
            "sha256": d["sha256"],
            "shelfId": d["shelf_id"],
            "shelfName": d["shelf_name"],
            "subShelf": str(d.get("sub_shelf") or "").strip() or derive_specific_sub_shelf(d),
            "clusterId": d["cluster_id"],
            "matchScore": score_val,
            "cosine": f"{cos_val:.3f}",
            "vectorNorm": f"{float(d.get('vector_norm', 1.0)):.4f}",
            "whyMatched": custom_why or d["why_matched"],
            "visualShare": int(d["visual_share"]),
            "textShare": int(d["text_share"]),
            "extractedText": (
                (d["extracted_text"] or d["ocr_text"] or "")[:600]
                if d["modality"] == "code"
                else clean_extracted_text((d["extracted_text"] or d["ocr_text"] or "")[:900], preserve_newlines=True)[:600]
            ),
            "ocrText": clean_extracted_text(d["ocr_text"] or "", preserve_newlines=True),
            "ocrConfidence": float(d.get("ocr_confidence", 0.0)),
            "specimen": d["specimen_type"],
            "width": int(d.get("width", 0)),
            "height": int(d.get("height", 0)),
            "pageCount": int(d.get("page_count", 1)),
            "mapX": float(d.get("map_x", 50.0)),
            "mapY": float(d.get("map_y", 50.0)),
            "thumbnailB64": None,
            "hasThumbnail": bool(d.get("thumbnail_b64")),
            "metadata": meta_obj,
            "tags": tags_list,
            "pinned": bool(d.get("pinned", 0)),
        }

    def get_file_thumbnail_bytes(self, file_id: str) -> Optional[bytes]:
        with self._lock:
            c = self._connect()
            try:
                row = c.execute("SELECT thumbnail_b64 FROM files WHERE id = ?", (file_id,)).fetchone()
            finally:
                c.close()
        if not row or not row["thumbnail_b64"]:
            return None
        raw_b64 = str(row["thumbnail_b64"])
        if "," in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            return base64.b64decode(raw_b64)
        except Exception:
            return None

    def get_file_by_id(self, file_id: str, conn: Optional[_PooledConnection | sqlite3.Connection] = None) -> Optional[dict[str, Any]]:
        if conn is not None:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
            return self._row_to_file_dict(row) if row else None
        with self._lock:
            c = self._connect()
            try:
                row = c.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
                return self._row_to_file_dict(row) if row else None
            finally:
                c.close()

    def get_all_state(self) -> dict[str, Any]:
        """Return complete workspace state for the React/Tauri frontend (cached in memory for sub-millisecond reads)."""
        with self._lock:
            if self._state_cache is not None:
                cached = dict(self._state_cache)
                cached["recentEvents"] = self.recent_events[:15]
                return cached

            conn = self._connect()
            try:
                file_rows = conn.execute("SELECT * FROM files WHERE archived = 0 ORDER BY mtime DESC").fetchall()
                shelf_rows = conn.execute("SELECT * FROM shelves ORDER BY seq_order ASC").fetchall()

                files_list = [self._row_to_file_dict(r) for r in file_rows]
                pinned_shelves_raw = conn.execute(
                    "SELECT value FROM settings WHERE key = 'pinned_shelves'"
                ).fetchone()
                pinned_shelf_ids: set[str] = set()
                if pinned_shelves_raw and pinned_shelves_raw["value"]:
                    try:
                        pinned_shelf_ids = set(json.loads(pinned_shelves_raw["value"]))
                    except Exception:
                        pinned_shelf_ids = set()

                sub_shelf_counts: dict[str, dict[str, int]] = {}
                for f_item in files_list:
                    sh_id = str(f_item.get("shelfId") or "")
                    sub_nm = str(f_item.get("subShelf") or "").strip()
                    if sh_id and sub_nm:
                        bucket = sub_shelf_counts.setdefault(sh_id, {})
                        bucket[sub_nm] = bucket.get(sub_nm, 0) + 1

                shelves_list = [
                    {
                        "id": r["shelf_id"],
                        "clusterId": r["cluster_id"],
                        "num": r["num"],
                        "name": r["name"],
                        "desc": r["desc"],
                        "centroidX": r["centroid_x"],
                        "centroidY": r["centroid_y"],
                        "pinned": bool(r["pinned"]) or (r["shelf_id"] in pinned_shelf_ids),
                        "subShelves": [
                            {"name": k, "count": v}
                            for k, v in sorted(
                                sub_shelf_counts.get(r["shelf_id"], {}).items(),
                                key=lambda kv: (-kv[1], kv[0]),
                            )
                        ],
                    }
                    for r in shelf_rows
                ]

                raw_meta = [dict(r) for r in file_rows]
                vectors = (
                    np.vstack([unpack_f32(r["embedding_blob"]) for r in file_rows])
                    if file_rows
                    else np.zeros((0, 512), dtype=np.float32)
                )
                arch_rows = conn.execute("SELECT pair_id, pair_json FROM archive_history").fetchall()
                resolved_map = {r["pair_id"]: json.loads(r["pair_json"]) for r in arch_rows}
                swapped_row = conn.execute("SELECT value FROM settings WHERE key = 'swapped_lookalikes'").fetchone()
                swapped_ids: set[str] = set()
                if swapped_row and swapped_row["value"]:
                    try:
                        swapped_ids = {str(x) for x in json.loads(swapped_row["value"]) if str(x)}
                    except Exception:
                        swapped_ids = set()
                lookalike_key = (
                    tuple((str(r["id"]), str(r["path"]), round(float(r["mtime"] or 0.0), 4)) for r in file_rows),
                    tuple(sorted(resolved_map.keys())),
                    tuple(sorted(swapped_ids)),
                )
                if self._lookalikes_cache is not None and self._lookalikes_cache[0] == lookalike_key:
                    lookalikes = list(self._lookalikes_cache[1])
                else:
                    lookalikes = find_lookalike_pairs(
                        raw_meta,
                        vectors,
                        resolved_map,
                        threshold=0.92,
                        swapped_pair_ids=swapped_ids,
                    )
                    self._lookalikes_cache = (lookalike_key, list(lookalikes))

                db_size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
                for suffix in ["-wal", "-shm"]:
                    wal_p = Path(str(self.db_path) + suffix)
                    if wal_p.exists():
                        db_size_bytes += wal_p.stat().st_size

                rename_hist = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM rename_history WHERE undone = 0 ORDER BY timestamp DESC LIMIT 50"
                    ).fetchall()
                ]

                suggestions: list[str] = []

                watched = self.get_watched_folders()
                watched_paths = {w["path"] for w in watched}
                action_hist = [
                    {
                        "id": r["id"],
                        "actionType": r["action_type"],
                        "summary": r["summary"],
                        "detail": json.loads(r["detail_json"]),
                        "timestamp": r["timestamp"],
                        "undone": bool(r["undone"]),
                    }
                    for r in conn.execute(
                        "SELECT * FROM action_log ORDER BY id DESC LIMIT 40"
                    ).fetchall()
                ]

                saved_searches = [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "query": r["query"],
                        "modality": r["modality"],
                        "tag": r["tag"],
                        "createdAt": r["created_at"],
                    }
                    for r in conn.execute(
                        "SELECT * FROM saved_searches ORDER BY created_at DESC"
                    ).fetchall()
                ]

                custom_rows = conn.execute("SELECT * FROM custom_shelves ORDER BY created_at DESC").fetchall()
                for c_idx, cr in enumerate(custom_rows):
                    explicit_ids: list[str] = []
                    try:
                        explicit_ids = json.loads(cr["file_ids_json"] or "[]")
                    except Exception:
                        explicit_ids = []
                    c_query = str(cr["query"] or "").strip()
                    c_mod = str(cr["modality"] or "all")
                    c_tag = str(cr["tag"] or "").strip().lower()
                    matched_ids = set(explicit_ids)
                    if c_query or c_tag or c_mod != "all":
                        _, op_filters = _extract_search_operators(c_query)
                        for f in files_list:
                            if c_mod != "all" and f["modality"] != c_mod:
                                continue
                            if c_tag and c_tag not in [t.lower() for t in (f.get("tags") or [])]:
                                continue
                            if op_filters and not _matches_operator_filters(f, op_filters):
                                continue
                            if not c_query or any(
                                tok in f"{f['title']} {f['name']} {f['extractedText']} {f['ocrText']}".lower()
                                for tok in _search_tokens(c_query)
                            ):
                                matched_ids.add(f["id"])
                    shelves_list.insert(
                        0,
                        {
                            "id": cr["shelf_id"],
                            "clusterId": -100 - c_idx,
                            "num": "★",
                            "name": cr["name"],
                            "desc": cr["desc"] or f"Smart shelf ({len(matched_ids)} files)",
                            "centroidX": 50.0,
                            "centroidY": 50.0,
                            "pinned": bool(cr["pinned"]),
                            "isCustom": True,
                            "fileIds": sorted(matched_ids),
                        },
                    )

                trash_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM files WHERE archived = 1 AND folder LIKE '%GoodShelves_Trash%'"
                ).fetchone()["c"]

                pinned_folder_rows = conn.execute(
                    "SELECT path, display_name, pinned_at FROM pinned_folders ORDER BY pinned_at DESC"
                ).fetchall()
                pinned_folders_list = [
                    {
                        "path": str(pr["path"]),
                        "name": str(pr["display_name"]),
                        "displayPath": format_path_short(str(pr["path"])),
                        "pinnedAt": float(pr["pinned_at"]),
                    }
                    for pr in pinned_folder_rows
                    if Path(str(pr["path"])).exists()
                ]
                ai_memory_list = load_ai_memory(conn, limit=60)

                state = {
                    "files": files_list,
                    "shelves": shelves_list,
                    "lookalikes": lookalikes,
                    "watchedFolders": watched,
                    "pinnedFolders": pinned_folders_list,
                    "aiMemory": ai_memory_list,
                    "connectedDevices": list_connected_devices(watched_paths),
                    "settings": self.get_settings(),
                    "pausedOnBattery": self.is_paused_on_battery(),
                    "trashCount": int(trash_count),
                    "silhouetteScore": self.silhouette_score,
                    "renameHistory": rename_hist,
                    "actionHistory": action_hist,
                    "savedSearches": saved_searches,
                    "querySuggestions": suggestions,
                    "recentEvents": self.recent_events[:15],
                    "storageStats": {
                        "dbPath": format_path_short(self.db_path),
                        "dbPathFull": str(self.db_path),
                        "dbSizeMB": round(max(0.1, db_size_bytes / (1024.0 * 1024.0)), 2),
                        "indexedCount": len(files_list),
                    },
                }
                self._state_cache = state
                return dict(state)
            finally:
                conn.close()

    def search_semantic(
        self,
        query: str,
        modality_filter: str = "all",
        limit: Optional[int] = None,
        min_similarity: Optional[float] = None,
        scope_folder: Optional[str] = None,
        scope_recursive: bool = True,
    ) -> dict[str, Any]:
        """
        Encode natural language query via ONNX Text + Visual Concept Encoder and query
        `sqlite-vec` (`vec_files`) for nearest neighbors by cosine distance.
        """
        t0 = time.perf_counter()
        settings = self.get_settings()
        max_k = limit if (limit is not None and limit > 0) else int(settings.get("max_results", 50))
        min_sim = min_similarity if min_similarity is not None else float(settings.get("min_similarity", 0.22))

        clean_q = (query or "").strip()
        text_q, op_filters = _extract_search_operators(clean_q)
        nl = _parse_nl_query(text_q)
        effective_modality = (
            modality_filter
            if modality_filter != "all"
            else (op_filters.get("modality") or nl.get("strict_modality") or "all")
        )

        if not clean_q:
            state = self.get_all_state()
            all_files = filter_files_by_scope(state["files"], scope_folder, recursive=scope_recursive)
            if effective_modality != "all":
                items = [f for f in all_files if f["modality"] == effective_modality][:max_k]
            else:
                items = all_files[:max_k]
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)
            return {
                "results": items,
                "latencyMs": elapsed,
                "query": "",
                "appliedFilters": [],
                "intentMode": "search",
                "totalMatched": len(items),
            }

        topic_raw = nl["topic_query"] if nl["has_strict_filters"] or nl["intent_mode"] in ("list", "count") else text_q
        sort_mode: Optional[str] = None
        if text_q:
            if re.search(r"\b(?:largest|biggest)\b", text_q, re.IGNORECASE):
                sort_mode = "largest"
            elif re.search(r"\b(?:oldest|earliest)\b", text_q, re.IGNORECASE):
                sort_mode = "oldest"
            elif re.search(r"\b(?:newest|latest|most\s+recent)\b", text_q, re.IGNORECASE):
                sort_mode = "newest"
        clean_topic_q = re.sub(
            r"\b(?:largest|biggest|oldest|earliest|newest|latest|most\s+recent)\b",
            " ",
            topic_raw,
            flags=re.IGNORECASE,
        )
        clean_topic_q = " ".join(clean_topic_q.split()).strip() if clean_topic_q else ""

        concepts, raw_terms, phrase = _query_concepts(clean_topic_q)
        intent = _infer_query_features(text_q)
        temporal_spec = nl.get("temporal")
        temporal_win = (
            (temporal_spec["start_ts"], temporal_spec["end_ts"], temporal_spec["label"])
            if temporal_spec is not None
            else _parse_temporal_window(text_q)
        )
        visual_subject = _clean_visual_query(clean_topic_q or text_q) if (clean_topic_q or not nl["has_strict_filters"]) else ""
        now = time.time()

        # Queries that express operators, dates, phrases, or type filters without a remaining semantic topic
        # should scan active files strictly and sort cleanly by date/size.
        if not concepts:
            state = self.get_all_state()
            items = list(state["files"])
            if effective_modality != "all":
                items = [f for f in items if f["modality"] == effective_modality]
            elif not nl["has_strict_filters"] and intent["modality"]:
                items = [f for f in items if f["modality"] == intent["modality"]]
            if not nl["has_strict_filters"] and intent["folder"]:
                items = [f for f in items if intent["folder"] in f["folderFull"].lower()]
            if not nl["has_strict_filters"] and not temporal_spec and intent["recency_days"]:
                cutoff = now - intent["recency_days"] * 86400.0
                recent_items = [f for f in items if float(f["mtime"]) >= cutoff]
                if recent_items or sort_mode != "newest":
                    items = recent_items
            if op_filters:
                items = [f for f in items if _matches_operator_filters(f, op_filters)]
            if nl["has_strict_filters"]:
                items = [f for f in items if _row_matches_nl_filters(f, nl)]

            if sort_mode == "largest":
                items.sort(key=lambda f: (int(f.get("sizeBytes") or 0), float(f["mtime"])), reverse=True)
            elif sort_mode == "oldest":
                items.sort(key=lambda f: (str(f.get("createdDate") or ""), float(f["mtime"])), reverse=False)
            else:
                items.sort(key=lambda f: (str(f.get("createdDate") or ""), float(f["mtime"])), reverse=True)

            results = []
            for idx, f in enumerate(items[:max_k]):
                item = dict(f)
                item["matchScore"] = max(72, 98 - idx)
                item["cosine"] = "1.000"
                if nl["include_phrases"]:
                    raw_body = f.get("ocrText") or f.get("extractedText") or f.get("title") or ""
                    snip = _extract_snippet_around_terms(raw_body, nl["include_phrases"], max_len=110)
                    why_msg = f'Matched “{nl["include_phrases"][0]}”' + (f" · {snip}" if snip else "")
                elif nl["applied_filters"]:
                    why_msg = f"{' · '.join(nl['applied_filters'][:3])} · {f['date']}"
                elif sort_mode == "largest":
                    why_msg = f"Largest first · {f['size']} · {f['folder']}"
                elif sort_mode == "oldest":
                    why_msg = f"Oldest first · {f['date']} · {f['folder']}"
                elif op_filters:
                    why_msg = f"Filtered match · {f['date']} · {f['folder']}"
                else:
                    why_msg = f"Recently modified · {f['date']} · {f['folder']}"
                item["whyMatched"] = why_msg
                results.append(item)
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)
            return {
                "results": results,
                "latencyMs": elapsed,
                "query": clean_q,
                "appliedFilters": nl["applied_filters"],
                "intentMode": nl["intent_mode"],
                "totalMatched": len(items),
            }

        need_visual_query = effective_modality in ("all", "image") or intent["modality"] == "image"
        q_vis_128 = (
            self.embedder.encode_visual_query(visual_subject)
            if (visual_subject and need_visual_query)
            else None
        )
        topic_query = clean_topic_q or text_q
        reuse_vis = (
            q_vis_128
            if (q_vis_128 is not None and visual_subject.lower() == topic_query.lower())
            else None
        )
        q_vec, _ = self.embedder.encode_query(topic_query, vis_128=reuse_vis)
        q_blob = pack_f32(q_vec)

        with self._lock:
            conn = self._connect()
            try:
                total_rows = conn.execute("SELECT COUNT(*) AS c FROM vec_files").fetchone()["c"]
                if total_rows == 0:
                    return {
                        "results": [],
                        "latencyMs": 0.5,
                        "query": clean_q,
                        "appliedFilters": nl["applied_filters"],
                        "intentMode": nl["intent_mode"],
                        "totalMatched": 0,
                    }

                # 1. Dense vector candidates.
                k_fetch = min(int(total_rows), max(max_k * 3, 60))
                vec_hits = conn.execute(
                    "SELECT rowid, distance FROM vec_files WHERE embedding MATCH ? AND k = ?",
                    (q_blob, k_fetch),
                ).fetchall()
                vec_distance = {int(vh["rowid"]): float(vh["distance"]) for vh in vec_hits}

                # 2. Keyword candidates across the whole index, so exact names/topics are never
                #    missed just because they fall outside the embedding's nearest neighbourhood.
                lexical_terms: list[str] = []
                for term in [*raw_terms, *nl["include_phrases"]]:
                    for variant in {term, _search_stem(term)}:
                        if len(variant) >= 2 and variant not in lexical_terms:
                            lexical_terms.append(variant)
                    if len(term) >= 5 and term[:3] not in lexical_terms:
                        lexical_terms.append(term[:3])

                lexical_rowids: set[int] = set()
                if lexical_terms:
                    clauses = []
                    params: list[Any] = []
                    for term in lexical_terms[:12]:
                        like = f"%{term}%"
                        clauses.append(
                            "(lower(title) LIKE ? OR lower(name) LIKE ? OR lower(extracted_text) LIKE ? "
                            "OR lower(ocr_text) LIKE ? OR lower(shelf_name) LIKE ? OR lower(tags_json) LIKE ? OR lower(metadata_json) LIKE ?)"
                        )
                        params.extend([like] * 7)
                    lex_rows = conn.execute(
                        f"SELECT rowid FROM files WHERE archived = 0 AND ({' OR '.join(clauses)}) LIMIT 500",
                        params,
                    ).fetchall()
                    lexical_rowids = {int(r["rowid"]) for r in lex_rows}

                # Load adaptive user affinities & onboarding focus areas
                settings_now = self.get_settings()
                adaptive_on = bool(settings_now.get("adaptive_learning", True))
                focus_areas = [str(x).lower() for x in (settings_now.get("user_focus_areas") or [])]
                file_affinity: dict[str, float] = {}
                shelf_affinity: dict[str, float] = {}
                if adaptive_on and raw_terms:
                    aff_terms = list({*(t.lower() for t in raw_terms), *(_search_stem(t.lower()) for t in raw_terms)})[:12]
                    if aff_terms:
                        q_ph = ",".join("?" for _ in aff_terms)
                        aff_rows = conn.execute(
                            f"SELECT target_id, kind, SUM(weight) AS total_w FROM user_affinities "
                            f"WHERE token IN ({q_ph}) GROUP BY target_id, kind",
                            aff_terms,
                        ).fetchall()
                        for ar in aff_rows:
                            if ar["kind"] == "file":
                                file_affinity[str(ar["target_id"])] = float(ar["total_w"] or 0.0)
                            elif ar["kind"] == "shelf":
                                shelf_affinity[str(ar["target_id"])] = float(ar["total_w"] or 0.0)

                candidate_rowids = set(vec_distance) | lexical_rowids
                # When strict NL constraints or operators are active, include all active rowids so exact filters
                # never drop matching files outside top-K vector proximity.
                if nl["has_strict_filters"] or op_filters:
                    all_active_rows = conn.execute("SELECT rowid FROM files WHERE archived = 0 LIMIT 5000").fetchall()
                    candidate_rowids |= {int(r["rowid"]) for r in all_active_rows}
                elif q_vis_128 is not None and (effective_modality in ("all", "image") or intent["modality"] == "image"):
                    img_rows = conn.execute(
                        "SELECT rowid FROM files WHERE archived = 0 AND modality = 'image' LIMIT 1000"
                    ).fetchall()
                    candidate_rowids |= {int(r["rowid"]) for r in img_rows}
                if not candidate_rowids:
                    return {
                        "results": [],
                        "latencyMs": 0.8,
                        "query": clean_q,
                        "appliedFilters": nl["applied_filters"],
                        "intentMode": nl["intent_mode"],
                        "totalMatched": 0,
                    }

                placeholders = ",".join("?" for _ in candidate_rowids)
                file_rows = conn.execute(
                    f"SELECT rowid AS _rowid, * FROM files WHERE rowid IN ({placeholders}) AND archived = 0",
                    list(candidate_rowids),
                ).fetchall()

                # Compute lightweight IDF weights over the candidate pool so rare terms outweigh common ones
                idf_map: dict[str, float] = {}
                n_cand = max(1, len(file_rows))
                for concept in concepts:
                    for variant in concept["base"]:
                        df = sum(
                            1
                            for fr in file_rows
                            if variant in f"{fr['title']} {fr['name']} {fr['extracted_text']} {fr['ocr_text']}".lower()
                        )
                        idf_map[variant] = float(np.log((n_cand + 1.0) / (df + 0.5)) + 1.0)

                ranked: list[tuple[float, dict[str, Any]]] = []
                for f_row in file_rows:
                    rowid = int(f_row["_rowid"])
                    f_dict_raw = dict(f_row)
                    if effective_modality != "all" and f_row["modality"] != effective_modality:
                        continue
                    if op_filters and not _matches_operator_filters(f_dict_raw, op_filters):
                        continue
                    if nl["has_strict_filters"] and not _row_matches_nl_filters(f_dict_raw, nl):
                        continue
                    if scope_folder and not filter_files_by_scope([{"path": str(f_row["path"])}], scope_folder, recursive=scope_recursive):
                        continue

                    cos_sim = 0.0
                    vis_sim = 0.0
                    blob = f_row["embedding_blob"]
                    if blob:
                        try:
                            stored = unpack_f32(blob)
                            if stored.shape[0] == q_vec.shape[0]:
                                cos_sim = float(np.clip(np.dot(q_vec, stored), -1.0, 1.0))
                            if stored.shape[0] == 512 and q_vec.shape[0] == 512:
                                has_vis_stored = float(np.linalg.norm(stored[384:512])) >= 1e-6
                                if f_row["modality"] != "image" or not has_vis_stored:
                                    q_txt = q_vec[:384]
                                    s_txt = stored[:384]
                                    qn = float(np.linalg.norm(q_txt))
                                    sn = float(np.linalg.norm(s_txt))
                                    if qn > 1e-6 and sn > 1e-6:
                                        cos_sim = max(cos_sim, float(np.clip(np.dot(q_txt / qn, s_txt / sn), -1.0, 1.0)))
                            if q_vis_128 is not None and stored.shape[0] == 512 and f_row["modality"] == "image":
                                vis_slice = stored[384:512]
                                vis_norm = float(np.linalg.norm(vis_slice))
                                if vis_norm > 1e-6:
                                    vis_sim = float(np.clip(np.dot(q_vis_128, vis_slice / vis_norm), -1.0, 1.0))
                        except Exception:
                            cos_sim = 0.0
                    if cos_sim == 0.0 and rowid in vec_distance:
                        cos_sim = float(np.clip(1.0 - vec_distance[rowid], -1.0, 1.0))

                    lex_score, matched_terms, phrase_hit = _lexical_relevance(f_row, concepts, phrase, idf_map=idf_map)

                    # Intent-aware boosts: file type asked for, recent time window, named folder, temporal match.
                    boost = 0.0
                    if intent["modality"] and f_row["modality"] == intent["modality"]:
                        boost += 0.055
                    file_mtime = float(f_row["mtime"] or now)
                    age_days = max(0.0, (now - file_mtime) / 86400.0)
                    boost += 0.018 * float(np.exp(-age_days / 365.0))
                    if intent["recency_days"] and age_days <= intent["recency_days"]:
                        boost += 0.07
                    temporal_hit = False
                    if temporal_spec is not None and _row_matches_temporal(f_dict_raw, temporal_spec):
                        boost += 0.16
                        temporal_hit = True
                    elif temporal_win is not None:
                        t_start, t_end, _ = temporal_win
                        if t_start <= file_mtime <= t_end:
                            boost += 0.16
                            temporal_hit = True
                    if intent["folder"] and intent["folder"] in str(f_row["folder"]).lower():
                        boost += 0.045
                    if adaptive_on:
                        f_aff = file_affinity.get(str(f_row["id"]), 0.0)
                        s_aff = shelf_affinity.get(str(f_row["shelf_id"]), 0.0)
                        if f_aff > 0:
                            boost += min(0.085, 0.03 * np.log1p(f_aff))
                        if s_aff > 0:
                            boost += min(0.045, 0.015 * np.log1p(s_aff))
                        if focus_areas:
                            sh_low = str(f_row["shelf_name"] or "").lower()
                            if (
                                ("school" in focus_areas and any(k in sh_low for k in ("school", "study", "academ", "research")))
                                or ("finance" in focus_areas and any(k in sh_low for k in ("receipt", "financ", "bill")))
                                or ("code" in focus_areas and any(k in sh_low for k in ("code", "project", "dev")))
                                or ("photos" in focus_areas and any(k in sh_low for k in ("photo", "image", "design")))
                                or ("work" in focus_areas and any(k in sh_low for k in ("work", "office", "admin")))
                            ):
                                boost += 0.025

                    if nl["include_phrases"]:
                        boost += 0.14

                    visual_boost = max(0.0, vis_sim) * 0.72 if f_row["modality"] == "image" else 0.0
                    blended = 0.50 * max(0.0, cos_sim) + 0.30 * lex_score + visual_boost + boost
                    effective_min_sim = min_sim if not nl["has_strict_filters"] else max(0.08, min_sim * 0.55)
                    if not nl["include_phrases"] and lex_score < 0.12 and cos_sim < effective_min_sim and vis_sim < 0.145:
                        continue

                    if nl["include_phrases"]:
                        reason = f'Matched “{nl["include_phrases"][0]}”'
                    elif phrase_hit:
                        reason = f"Matched “{phrase}”"
                    elif matched_terms:
                        terms_fmt = ", ".join(f"“{t}”" for t in matched_terms[:4])
                        reason = f"Matched {terms_fmt}"
                    elif vis_sim >= 0.145:
                        reason = f"Visual match for “{visual_subject}”"
                    else:
                        reason = f"Matched content ({int(round(max(0.0, cos_sim) * 100))}%)"
                    if temporal_hit and temporal_win is not None:
                        reason += f" · {temporal_win[2]}"
                    if intent["modality"] and f_row["modality"] == intent["modality"]:
                        reason += f" · {f_row['modality']}"
                    raw_body = f_row["ocr_text"] or f_row["extracted_text"] or f_row["title"] or ""
                    snippet = _extract_snippet_around_terms(raw_body, nl["include_phrases"] or matched_terms or raw_terms, max_len=96)
                    why = f"{reason} · {snippet}" if snippet else f"{reason} · {f_row['why_matched']}"

                    ui_score = min(99, max(1, int(round(blended * 100))))
                    item_dict = self._row_to_file_dict(
                        f_row, cosine_sim=float(np.clip(blended, 0.0, 0.999)), match_score=ui_score, custom_why=why
                    )
                    ranked.append((blended, item_dict))

                if sort_mode == "largest":
                    ranked.sort(
                        key=lambda x: (int(x[1].get("sizeBytes") or 0), x[0], float(x[1]["mtime"])),
                        reverse=True,
                    )
                elif sort_mode == "oldest":
                    ranked.sort(key=lambda x: (float(x[1]["mtime"]), -x[0]))
                elif sort_mode == "newest":
                    ranked.sort(key=lambda x: (float(x[1]["mtime"]), x[0]), reverse=True)
                else:
                    ranked.sort(key=lambda x: (x[0], float(x[1]["mtime"])), reverse=True)

                # Collapse exact-content duplicates so one document does not fill several result slots.
                final_results: list[dict[str, Any]] = []
                seen_content: set[str] = set()
                for _, item in ranked:
                    key = item["sha256"] or item["path"]
                    if key in seen_content:
                        continue
                    seen_content.add(key)
                    final_results.append(item)
                    if len(final_results) >= max_k:
                        break

                latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                return {
                    "results": final_results,
                    "latencyMs": latency_ms,
                    "query": clean_q,
                    "appliedFilters": nl["applied_filters"],
                    "intentMode": nl["intent_mode"],
                    "totalMatched": len(ranked),
                }
            finally:
                conn.close()

    def toggle_rename_file(self, file_id: str) -> dict[str, Any]:
        """Execute an atomic file rename on disk (`os.replace` / `Path.rename`) or undo it."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
                if not row:
                    raise ValueError(f"File not found: {file_id}")

                cur_path = Path(row["path"])
                is_renamed = bool(row["renamed"])
                orig_name = str(row["original_name"])
                sugg_name = row["suggested_name"]

                if not is_renamed:
                    if not sugg_name:
                        raise ValueError("No rename suggestion available for this file")
                    target_name = str(sugg_name)
                    target_path = _unique_dest_path(cur_path.parent, target_name, src_path=cur_path)
                    target_name = target_path.name
                    if cur_path.exists() and target_path != cur_path:
                        cur_path.rename(target_path)
                    conn.execute(
                        "UPDATE files SET path = ?, name = ?, renamed = 1 WHERE id = ?",
                        (str(target_path), target_name, file_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO rename_history (file_id, old_path, new_path, old_name, new_name, timestamp, undone)
                        VALUES (?, ?, ?, ?, ?, ?, 0)
                        """,
                        (file_id, str(cur_path), str(target_path), cur_path.name, target_name, time.time()),
                    )
                    conn.execute(
                        "INSERT INTO action_log (action_type, summary, detail_json, timestamp, undone) VALUES (?, ?, ?, ?, 0)",
                        (
                            "rename",
                            f"Renamed {cur_path.name} → {target_name}",
                            json.dumps({
                                "label": f"rename {cur_path.name}",
                                "moves": [{
                                    "fileId": file_id,
                                    "fromPath": str(cur_path),
                                    "toPath": str(target_path),
                                    "renamedAfter": 1,
                                }],
                            }),
                            time.time(),
                        ),
                    )
                    action = "applied"
                else:
                    target_name = orig_name
                    target_path = _unique_dest_path(cur_path.parent, target_name, src_path=cur_path)
                    target_name = target_path.name
                    if cur_path.exists() and target_path != cur_path:
                        cur_path.rename(target_path)
                    conn.execute(
                        "UPDATE files SET path = ?, name = ?, renamed = 0 WHERE id = ?",
                        (str(target_path), target_name, file_id),
                    )
                    conn.execute(
                        "UPDATE rename_history SET undone = 1 WHERE file_id = ? AND undone = 0",
                        (file_id,),
                    )
                    action = "undone"

                conn.commit()
                self._invalidate_cache()
                updated = self.get_file_by_id(file_id, conn=conn)
                return {"action": action, "file": updated}
            finally:
                conn.close()

    def apply_all_renames(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id FROM files WHERE archived = 0 AND suggested_name IS NOT NULL AND renamed = 0"
                ).fetchall()
                ids = [str(r["id"]) for r in rows]
            finally:
                conn.close()

        applied_count = 0
        for fid in ids:
            try:
                self.toggle_rename_file(fid)
                applied_count += 1
            except Exception:
                pass
        return {"appliedCount": applied_count}

    def toggle_archive_lookalike(self, pair_id: str) -> dict[str, Any]:
        """Move redundant lookalike copy into `~/Documents/GoodShelves_Archive` (or restore it on Undo)."""
        archive_dir = get_user_special_dir("Documents") / "GoodShelves_Archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            conn = self._connect()
            try:
                arch_row = conn.execute("SELECT * FROM archive_history WHERE pair_id = ?", (pair_id,)).fetchone()
                if arch_row:
                    orig_p = Path(arch_row["original_path"])
                    arch_p = Path(arch_row["archived_path"])
                    fid = str(arch_row["file_id"])
                    if arch_p.exists():
                        orig_p.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(arch_p), str(orig_p))
                    conn.execute("UPDATE files SET path = ?, archived = 0 WHERE id = ?", (str(orig_p), fid))
                    conn.execute("DELETE FROM archive_history WHERE pair_id = ?", (pair_id,))
                    conn.commit()
                    self._invalidate_cache()
                    return {"resolved": False, "pairId": pair_id, "restoredPath": str(orig_p)}

                state = self.get_all_state()
                pair = next((p for p in state["lookalikes"] if p["id"] == pair_id), None)
                if not pair:
                    raise ValueError(f"Lookalike pair not found: {pair_id}")

                copy_info = pair["copy"]
                fid = str(copy_info["id"])
                src_p = Path(copy_info["path"])
                dst_p = _unique_dest_path(archive_dir, src_p.name)

                if src_p.exists():
                    shutil.move(str(src_p), str(dst_p))

                pair_resolved = dict(pair)
                pair_resolved["resolved"] = True
                pair_resolved["archived_path"] = str(dst_p)

                conn.execute("UPDATE files SET path = ?, archived = 1 WHERE id = ?", (str(dst_p), fid))
                conn.execute(
                    """
                    INSERT INTO archive_history (pair_id, file_id, original_path, archived_path, pair_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (pair_id, fid, str(src_p), str(dst_p), json.dumps(pair_resolved), time.time()),
                )
                conn.execute(
                    "INSERT INTO action_log (action_type, summary, detail_json, timestamp, undone) VALUES (?, ?, ?, ?, 0)",
                    (
                        "archive",
                        f"Archived duplicate {src_p.name} → ~/Documents/GoodShelves_Archive/",
                        json.dumps({
                            "label": f"archive {src_p.name}",
                            "pairId": pair_id,
                            "fileId": fid,
                            "fromPath": str(src_p),
                            "toPath": str(dst_p),
                            "pairJson": json.dumps(pair_resolved),
                        }),
                        time.time(),
                    ),
                )
                conn.commit()
                self._invalidate_cache()
                return {
                    "resolved": True,
                    "pairId": pair_id,
                    "archivedPath": str(dst_p),
                    "savedMB": pair["saved_mb"],
                }
            finally:
                conn.close()

    def preview_tidy_folders(self) -> list[dict[str, Any]]:
        """Preview physical folder organization moves into ~/Shelves/<shelf-slug>/."""
        shelves_root = Path.home() / "Shelves"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, path, name, folder_short, shelf_id, shelf_name FROM files WHERE archived = 0 AND shelf_id != 'tray'"
                ).fetchall()
                moves: list[dict[str, Any]] = []
                for r in rows:
                    cur_p = Path(r["path"])
                    slug = re.sub(r"^\d+\s*", "", str(r["shelf_name"])).replace("&", "and")
                    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
                    dest_dir = shelves_root / slug
                    dest_path = dest_dir / cur_p.name
                    if cur_p.resolve() != dest_path.resolve():
                        moves.append(
                            {
                                "fileId": r["id"],
                                "fileName": r["name"],
                                "fromShort": f"{r['folder_short']}/{r['name']}",
                                "toShort": f"~/Shelves/{slug}/",
                                "fromPath": str(cur_p),
                                "toPath": str(dest_path),
                            }
                        )
                return moves
            finally:
                conn.close()

    def execute_tidy_folders(self) -> dict[str, Any]:
        """Safely move files into ~/Shelves/<shelf-slug>/ with single-click rollback logging."""
        moves = self.preview_tidy_folders()
        batch_id = f"tidy_{int(time.time())}"
        moved_count = 0

        with self._lock:
            conn = self._connect()
            try:
                actual_moves: list[dict[str, Any]] = []
                for m in moves:
                    src = Path(m["fromPath"])
                    dst_orig = Path(m["toPath"])
                    if src.exists():
                        dst_orig.parent.mkdir(parents=True, exist_ok=True)
                        dst = _unique_dest_path(dst_orig.parent, dst_orig.name, src_path=src)
                        shutil.move(str(src), str(dst))
                        conn.execute(
                            "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ? WHERE id = ?",
                            (str(dst), dst.name, str(dst.parent), format_path_short(dst.parent), m["fileId"]),
                        )
                        conn.execute(
                            """
                            INSERT INTO tidy_moves_history (batch_id, file_id, old_path, new_path, timestamp, undone)
                            VALUES (?, ?, ?, ?, ?, 0)
                            """,
                            (batch_id, m["fileId"], str(src), str(dst), time.time()),
                        )
                        actual_moves.append(
                            {
                                "fileId": m["fileId"],
                                "fromPath": str(src),
                                "toPath": str(dst),
                                "renamedAfter": 0,
                            }
                        )
                        moved_count += 1
                if moved_count > 0:
                    conn.execute(
                        "INSERT INTO action_log (action_type, summary, detail_json, timestamp, undone) VALUES (?, ?, ?, ?, 0)",
                        (
                            "tidy_folders",
                            f"Organized {moved_count} files into ~/Shelves/",
                            json.dumps({"label": "organize folders", "moves": actual_moves}),
                            time.time(),
                        ),
                    )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()

        return {"batchId": batch_id, "movedCount": moved_count}

    def rollback_tidy_folders(self, batch_id: Optional[str] = None) -> dict[str, Any]:
        """Rollback the most recent physical folder organization batch."""
        restored = 0
        with self._lock:
            conn = self._connect()
            try:
                if not batch_id:
                    latest = conn.execute(
                        "SELECT batch_id FROM tidy_moves_history WHERE undone = 0 ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if not latest:
                        return {"restoredCount": 0}
                    batch_id = str(latest["batch_id"])

                rows = conn.execute(
                    "SELECT * FROM tidy_moves_history WHERE batch_id = ? AND undone = 0",
                    (batch_id,),
                ).fetchall()
                for r in rows:
                    old_p = Path(r["old_path"])
                    new_p = Path(r["new_path"])
                    if new_p.exists():
                        old_p.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(new_p), str(old_p))
                        conn.execute(
                            "UPDATE files SET path = ?, folder = ?, folder_short = ? WHERE id = ?",
                            (str(old_p), str(old_p.parent), format_path_short(old_p.parent), r["file_id"]),
                        )
                        restored += 1
                conn.execute("UPDATE tidy_moves_history SET undone = 1 WHERE batch_id = ?", (batch_id,))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"restoredCount": restored, "batchId": batch_id}

    def verify_integrity(self) -> dict[str, Any]:
        """Verify SQLite-vec virtual table alignment and remove orphaned or invalid scratch paths."""
        removed = 0
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT rowid, path, size_bytes, ext FROM files WHERE archived = 0").fetchall()
                for r in rows:
                    p = Path(r["path"])
                    is_tiny_scratch = int(r["size_bytes"]) < 16 and str(r["ext"]) in {"txt", "md", "log"}
                    if not p.exists() or is_tiny_scratch:
                        conn.execute("DELETE FROM vec_files WHERE rowid = ?", (r["rowid"],))
                        conn.execute("DELETE FROM files WHERE rowid = ?", (r["rowid"],))
                        removed += 1
                vec_count = conn.execute("SELECT COUNT(*) AS c FROM vec_files").fetchone()["c"]
                file_count = conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
                conn.commit()
                self._invalidate_cache()
                return {
                    "ok": True,
                    "activeFiles": file_count,
                    "vectorRows": vec_count,
                    "orphansRemoved": removed,
                }
            finally:
                conn.close()

    def _record_action(self, action_type: str, summary: str, detail: dict[str, Any]) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO action_log (action_type, summary, detail_json, timestamp, undone) VALUES (?, ?, ?, ?, 0)",
                    (action_type, summary, json.dumps(detail, ensure_ascii=False), time.time()),
                )
                conn.commit()
                self._invalidate_cache()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def undo_action(self, action_id: Optional[int] = None) -> dict[str, Any]:
        """Undo the latest (or specified) action in action_log and return a clear summary."""
        with self._lock:
            conn = self._connect()
            try:
                if action_id is not None:
                    row = conn.execute("SELECT * FROM action_log WHERE id = ? AND undone = 0", (action_id,)).fetchone()
                else:
                    row = conn.execute("SELECT * FROM action_log WHERE undone = 0 ORDER BY id DESC LIMIT 1").fetchone()
                if not row:
                    raise ValueError("Nothing to undo")
                act_id = int(row["id"])
                act_type = str(row["action_type"])
                detail = json.loads(row["detail_json"])
            finally:
                conn.close()

        summary = self._apply_action_direction(act_type, detail, reverse=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE action_log SET undone = 1 WHERE id = ?", (act_id,))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"actionId": act_id, "summary": summary}

    def redo_action(self, action_id: Optional[int] = None) -> dict[str, Any]:
        """Redo the most recently undone (or specified) action in action_log."""
        with self._lock:
            conn = self._connect()
            try:
                if action_id is not None:
                    row = conn.execute("SELECT * FROM action_log WHERE id = ? AND undone = 1", (action_id,)).fetchone()
                else:
                    row = conn.execute("SELECT * FROM action_log WHERE undone = 1 ORDER BY id DESC LIMIT 1").fetchone()
                if not row:
                    raise ValueError("Nothing to redo")
                act_id = int(row["id"])
                act_type = str(row["action_type"])
                detail = json.loads(row["detail_json"])
            finally:
                conn.close()

        summary = self._apply_action_direction(act_type, detail, reverse=False)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE action_log SET undone = 0 WHERE id = ?", (act_id,))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"actionId": act_id, "summary": summary}

    def _apply_action_direction(self, act_type: str, detail: dict[str, Any], reverse: bool) -> str:
        """Apply or reverse file moves/renames/archives recorded in action_log."""
        moves = detail.get("moves", [])
        if act_type in {"rename", "rename_batch", "tidy_folders", "smart_clean", "move_file", "bulk_move"}:
            done = 0
            with self._lock:
                conn = self._connect()
                try:
                    for mv in (reversed(moves) if reverse else moves):
                        src = Path(mv["toPath"] if reverse else mv["fromPath"])
                        dst = Path(mv["fromPath"] if reverse else mv["toPath"])
                        if src.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(src), str(dst))
                            renamed_flag = int(mv.get("renamedBefore", 0)) if reverse else int(mv.get("renamedAfter", 0))
                            if act_type in {"rename", "rename_batch", "smart_clean"}:
                                restored_title = dst.stem.replace("_", " ").replace("-", " ").strip() or dst.stem
                                conn.execute(
                                    "UPDATE files SET path = ?, name = ?, title = ?, folder = ?, folder_short = ?, renamed = ?, archived = 0 WHERE id = ?",
                                    (str(dst), dst.name, restored_title, str(dst.parent), format_path_short(dst.parent), renamed_flag, mv["fileId"]),
                                )
                            else:
                                conn.execute(
                                    "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ?, renamed = ?, archived = 0 WHERE id = ?",
                                    (str(dst), dst.name, str(dst.parent), format_path_short(dst.parent), renamed_flag, mv["fileId"]),
                                )
                            done += 1
                    for arch in detail.get("archives", []):
                        pair_id = arch["pairId"]
                        src = Path(arch["toPath"] if reverse else arch["fromPath"])
                        dst = Path(arch["fromPath"] if reverse else arch["toPath"])
                        if src.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(src), str(dst))
                        conn.execute(
                            "UPDATE files SET path = ?, archived = ? WHERE id = ?",
                            (str(dst), 0 if reverse else 1, arch["fileId"]),
                        )
                        if reverse:
                            conn.execute("DELETE FROM archive_history WHERE pair_id = ?", (pair_id,))
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            verb = "Undid" if reverse else "Redid"
            return f"{verb} {detail.get('label', act_type)} ({done} file(s))"

        if act_type in {"trash_file", "bulk_trash"}:
            items = detail.get("items", [detail] if "fileId" in detail else [])
            folder_mv = detail.get("folderMove")
            done = 0
            with self._lock:
                conn = self._connect()
                try:
                    if folder_mv:
                        f_src = Path(folder_mv["toPath"] if reverse else folder_mv["fromPath"])
                        f_dst = Path(folder_mv["fromPath"] if reverse else folder_mv["toPath"])
                        if f_src.exists():
                            f_dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(f_src), str(f_dst))
                            done += 1
                    for it in (reversed(items) if reverse else items):
                        src = Path(it["toPath"] if reverse else it["fromPath"])
                        dst = Path(it["fromPath"] if reverse else it["toPath"])
                        if not folder_mv:
                            if src.exists():
                                dst.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(src), str(dst))
                                done += 1
                        elif dst.exists():
                            done += 1
                        conn.execute(
                            "UPDATE files SET path = ?, folder = ?, folder_short = ?, archived = ? WHERE id = ?",
                            (str(dst), str(dst.parent), format_path_short(dst.parent), 0 if reverse else 1, it["fileId"]),
                        )
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            verb = "Restored" if reverse else "Moved to trash"
            return f"{verb} {done} item(s)"

        if act_type == "device_import":
            items = detail.get("items", [])
            done = 0
            if reverse:
                with self._lock:
                    conn = self._connect()
                    try:
                        for it in items:
                            dst = Path(it["toPath"])
                            if dst.exists():
                                dst.unlink(missing_ok=True)
                            row = conn.execute("SELECT rowid FROM files WHERE id = ?", (it["fileId"],)).fetchone()
                            if row:
                                conn.execute("DELETE FROM vec_files WHERE rowid = ?", (row["rowid"],))
                                conn.execute("DELETE FROM files WHERE rowid = ?", (row["rowid"],))
                            done += 1
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                return f"Removed {done} imported file(s)"
            else:
                for it in items:
                    src = Path(it["fromPath"])
                    dst = Path(it["toPath"])
                    if src.exists() and not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src), str(dst))
                        self.index_single_file(dst, force=True, emit_telemetry=False, recluster=False)
                        done += 1
                self.recluster_all()
                return f"Re-imported {done} file(s)"

        if act_type == "archive":
            pair_id = detail["pairId"]
            src = Path(detail["toPath"] if reverse else detail["fromPath"])
            dst = Path(detail["fromPath"] if reverse else detail["toPath"])
            with self._lock:
                conn = self._connect()
                try:
                    if src.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
                    conn.execute(
                        "UPDATE files SET path = ?, archived = ? WHERE id = ?",
                        (str(dst), 0 if reverse else 1, detail["fileId"]),
                    )
                    if reverse:
                        conn.execute("DELETE FROM archive_history WHERE pair_id = ?", (pair_id,))
                    elif detail.get("pairJson"):
                        conn.execute(
                            "INSERT OR REPLACE INTO archive_history (pair_id, file_id, original_path, archived_path, pair_json, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                            (pair_id, detail["fileId"], detail["fromPath"], detail["toPath"], detail["pairJson"], time.time()),
                        )
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Restored' if reverse else 'Archived'} {Path(detail['fromPath']).name}"

        if act_type in {"bulk_tag", "bulk_untag"}:
            entries = detail.get("entries", [])
            done = 0
            with self._lock:
                conn = self._connect()
                try:
                    for ent in entries:
                        fid = str(ent.get("fileId") or "")
                        if not fid:
                            continue
                        row = conn.execute("SELECT id FROM files WHERE id = ?", (fid,)).fetchone()
                        if not row:
                            continue
                        target_tags = ent.get("before" if reverse else "after", [])
                        conn.execute("UPDATE files SET tags_json = ? WHERE id = ?", (json.dumps(list(target_tags)), fid))
                        done += 1
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Undid' if reverse else 'Redid'} {detail.get('label', 'tag update')} ({done} file(s))"

        if act_type == "bulk_pin":
            entries = detail.get("entries", [])
            done = 0
            with self._lock:
                conn = self._connect()
                try:
                    for ent in entries:
                        fid = str(ent.get("fileId") or "")
                        if not fid:
                            continue
                        pin_val = int(ent.get("before" if reverse else "after", 0))
                        conn.execute("UPDATE files SET pinned = ? WHERE id = ?", (pin_val, fid))
                        done += 1
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Undid' if reverse else 'Redid'} {detail.get('label', 'pin update')} ({done} file(s))"

        return "Action updated"

    def scan_device(self, device_path: str, add_watch: bool = False) -> dict[str, Any]:
        """Scan a connected phone/USB/SD mount and optionally add it to watched folders."""
        resolved = Path(os.path.expanduser(device_path)).resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"Device path not accessible: {device_path}")
        if add_watch:
            self.add_watched_folder(str(resolved))
            return {"scannedCount": len(self.get_all_state()["files"]), "path": str(resolved)}
        count = self.scan_folder(resolved, max_files=120, recursive=True)
        self.recluster_all()
        return {"scannedCount": count, "path": str(resolved)}

    def complete_setup(
        self,
        folders: list[str],
        user_name: str = "",
        focus_areas: Optional[list[str]] = None,
        adaptive_learning: bool = True,
        terms_accepted: bool = True,
        scan_now: bool = True,
    ) -> dict[str, Any]:
        """Save user's initial setup, preferences, and watched folder choices."""
        resolved_set = {str(Path(os.path.expanduser(p)).resolve()) for p in folders if p.strip()}
        clean_name = (user_name or "").strip()[:80]
        clean_focus = [str(x).strip().lower() for x in (focus_areas or []) if str(x).strip()][:10]
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute("SELECT path FROM watched_folders").fetchall()
                for r in existing:
                    active = 1 if r["path"] in resolved_set else 0
                    conn.execute("UPDATE watched_folders SET active = ? WHERE path = ?", (active, r["path"]))
                for p_str in resolved_set:
                    p_obj = Path(p_str)
                    if p_obj.exists():
                        conn.execute(
                            "INSERT OR IGNORE INTO watched_folders (path, display_path, active) VALUES (?, ?, 1)",
                            (p_str, format_path_short(p_obj)),
                        )
                updates = {
                    "setup_completed": "true",
                    "user_name": clean_name,
                    "user_focus_areas": json.dumps(clean_focus),
                    "adaptive_learning": "true" if adaptive_learning else "false",
                    "terms_accepted": "true" if terms_accepted else "false",
                }
                for sk, sv in updates.items():
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (sk, sv),
                    )
                conn.commit()
                self._invalidate_cache(settings_changed=True)
            finally:
                conn.close()

        if not scan_now:
            return {"scannedCount": 0, "scanning": any(wf["active"] for wf in self.get_watched_folders())}

        total = 0
        for wf in self.get_watched_folders():
            if wf["active"]:
                total += self.scan_folder(Path(wf["path"]), max_files=150, recursive=True, emit_idle=False)
        self.verify_integrity()
        self.recluster_all()
        return {"scannedCount": total, "scanning": False}

    def clear_activity(self) -> dict[str, Any]:
        """Clear activity logs, reset finished rename badges, remove missing watched folders, and prune orphans."""
        self.recent_events.clear()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM action_log")
                conn.execute("DELETE FROM rename_history")
                conn.execute("DELETE FROM archive_history")
                conn.execute(
                    "UPDATE files SET renamed = 0, suggested_name = NULL, rename_reason = '' WHERE renamed = 1"
                )
                wf_rows = conn.execute("SELECT path FROM watched_folders").fetchall()
                for wf in wf_rows:
                    if not Path(wf["path"]).exists():
                        conn.execute("DELETE FROM watched_folders WHERE path = ?", (wf["path"],))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        integrity = self.verify_integrity()
        if integrity.get("orphansRemoved", 0) > 0:
            self.recluster_all()
        return {"ok": True, "orphansRemoved": integrity.get("orphansRemoved", 0)}

    def reset_setup(self) -> dict[str, Any]:
        """Reset setup_completed so the onboarding wizard can run cleanly."""
        self.recent_events.clear()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM action_log")
                conn.execute("DELETE FROM rename_history")
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('setup_completed', 'false') "
                    "ON CONFLICT(key) DO UPDATE SET value = 'false'"
                )
                conn.commit()
                self._invalidate_cache(settings_changed=True)
            finally:
                conn.close()
        return {"ok": True}

    def record_user_feedback(
        self,
        query: str = "",
        file_id: str = "",
        shelf_id: str = "",
        action: str = "select",
    ) -> dict[str, Any]:
        """Learn user query -> file/shelf preferences over time locally."""
        if not self.get_settings().get("adaptive_learning", True):
            return {"ok": True, "recorded": False}
        tokens = [
            t
            for t in _search_tokens(query)
            if len(t) >= 2 and t not in _SEARCH_STOPWORDS
        ]
        if not tokens:
            return {"ok": True, "recorded": False}
        delta = 1.6 if action in ("open", "move", "pin") else 1.0
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                resolved_shelf = shelf_id
                if file_id and not resolved_shelf:
                    row = conn.execute("SELECT shelf_id FROM files WHERE id = ?", (file_id,)).fetchone()
                    if row:
                        resolved_shelf = str(row["shelf_id"] or "")
                for tok in tokens[:8]:
                    for key_tok in {tok, _search_stem(tok)}:
                        if file_id:
                            conn.execute(
                                "INSERT INTO user_affinities (token, target_id, kind, weight, updated_at) "
                                "VALUES (?, ?, 'file', ?, ?) "
                                "ON CONFLICT(token, target_id, kind) DO UPDATE SET "
                                "weight = weight + excluded.weight, updated_at = excluded.updated_at",
                                (key_tok, file_id, delta, now),
                            )
                        if resolved_shelf:
                            conn.execute(
                                "INSERT INTO user_affinities (token, target_id, kind, weight, updated_at) "
                                "VALUES (?, ?, 'shelf', ?, ?) "
                                "ON CONFLICT(token, target_id, kind) DO UPDATE SET "
                                "weight = weight + excluded.weight, updated_at = excluded.updated_at",
                                (key_tok, resolved_shelf, delta * 0.6, now),
                            )
                conn.commit()
            finally:
                conn.close()
        return {"ok": True, "recorded": True}

    def execute_file_action(self, file_id: str, action: str) -> dict[str, Any]:
        """Run a format-specific action on a file (extract_frame, export_csv, run_ocr, extract_pdf_text)."""
        f_dict = self.get_file_by_id(file_id)
        if not f_dict:
            raise ValueError(f"File not found: {file_id}")
        src_path = Path(f_dict["path"])
        if not src_path.exists():
            raise ValueError("File no longer exists on disk")

        if action == "extract_frame":
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise ValueError("ffmpeg is not installed")
            out_path = _unique_dest_path(src_path.parent, f"{src_path.stem}_frame.png")
            proc = subprocess.run(
                [ffmpeg, "-y", "-ss", "1.0", "-i", str(src_path), "-frames:v", "1", str(out_path)],
                capture_output=True,
                timeout=10,
                **_subprocess_win_kwargs(),
            )
            if proc.returncode != 0 or not out_path.exists():
                raise ValueError("Could not extract video frame")
            self.index_single_file(out_path, force=True, emit_telemetry=True, recluster=True)
            return {"ok": True, "message": f"Saved frame to {format_path_short(out_path)}", "outputPath": str(out_path)}

        if action == "export_csv":
            import openpyxl
            import csv as _csv

            wb = openpyxl.load_workbook(str(src_path), read_only=True, data_only=True)
            try:
                ws = wb.active
                out_path = _unique_dest_path(src_path.parent, f"{src_path.stem}.csv")
                with out_path.open("w", encoding="utf-8", newline="") as fp:
                    writer = _csv.writer(fp)
                    for row in ws.iter_rows(values_only=True):
                        writer.writerow(["" if v is None else str(v) for v in row])
            finally:
                wb.close()
            self.index_single_file(out_path, force=True, emit_telemetry=True, recluster=True)
            return {"ok": True, "message": f"Exported CSV to {format_path_short(out_path)}", "outputPath": str(out_path)}

        if action == "extract_pdf_text":
            import pymupdf

            with _FITZ_LOCK:
                doc = pymupdf.open(str(src_path))
                try:
                    full_text = "\n\n".join(page.get_text("text").strip() for page in doc if page.get_text("text").strip())
                finally:
                    doc.close()
            out_path = _unique_dest_path(src_path.parent, f"{src_path.stem}.txt")
            out_path.write_text(full_text or "(no embedded text found)", encoding="utf-8")
            self.index_single_file(out_path, force=True, emit_telemetry=True, recluster=True)
            return {"ok": True, "message": f"Saved text to {format_path_short(out_path)}", "outputPath": str(out_path)}

        if action == "run_ocr":
            from PIL import Image

            img = Image.open(src_path).convert("RGB")
            ocr_txt, ocr_conf = run_ocr_on_image(img)
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "UPDATE files SET ocr_text = ?, ocr_confidence = ?, extracted_text = CASE WHEN extracted_text = '' THEN ? ELSE extracted_text END WHERE id = ?",
                        (ocr_txt, ocr_conf, ocr_txt, file_id),
                    )
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return {"ok": True, "message": f"OCR extracted {len(ocr_txt.split())} words ({ocr_conf:.0f}% conf)", "ocrText": ocr_txt}

        raise ValueError(f"Unknown action: {action}")

    def preview_smart_clean(self) -> dict[str, Any]:
        """Return a unified preview of pending renames, duplicate archives, and folder moves."""
        state = self.get_all_state()
        renames = [
            {
                "fileId": f["id"],
                "fromName": f["name"],
                "toName": f["suggestedName"],
                "folder": f["folder"],
                "reason": f["renameReason"] or "",
            }
            for f in state["files"]
            if f.get("suggestedName") and not f.get("renamed")
        ]
        duplicates = [
            {
                "pairId": p["id"],
                "keepName": p["keep"]["name"],
                "copyName": p["copy"]["name"],
                "savedMB": p["saved_mb"],
                "badge": p["badge"],
            }
            for p in state["lookalikes"]
            if not p.get("resolved")
        ]
        moves = self.preview_tidy_folders()
        return {
            "renames": renames,
            "duplicates": duplicates,
            "moves": moves,
            "totalSavedMB": round(sum(d["savedMB"] for d in duplicates), 2),
        }

    def execute_smart_clean(
        self,
        apply_renames: bool = True,
        archive_duplicates: bool = True,
        organize_folders: bool = True,
    ) -> dict[str, Any]:
        """Execute selected Smart Clean steps and record a single Undo/Redo batch."""
        moves_log: list[dict[str, Any]] = []
        renamed_entries: dict[str, dict[str, Any]] = {}
        archives_log: list[dict[str, Any]] = []
        renamed_count = 0
        archived_count = 0
        moved_count = 0

        if apply_renames:
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT id, path, name, suggested_name FROM files WHERE archived = 0 AND suggested_name IS NOT NULL AND renamed = 0"
                    ).fetchall()
                finally:
                    conn.close()
            for r in rows:
                src = Path(r["path"])
                if not src.exists():
                    continue
                dst = _unique_dest_path(src.parent, str(r["suggested_name"]), src_path=src)
                if dst != src:
                    src.rename(dst)
                    with self._lock:
                        conn = self._connect()
                        try:
                            conn.execute(
                                "UPDATE files SET path = ?, name = ?, renamed = 1 WHERE id = ?",
                                (str(dst), dst.name, r["id"]),
                            )
                            conn.execute(
                                "INSERT INTO rename_history (file_id, old_path, new_path, old_name, new_name, timestamp, undone) VALUES (?, ?, ?, ?, ?, ?, 0)",
                                (r["id"], str(src), str(dst), src.name, dst.name, time.time()),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                    entry = {
                        "fileId": r["id"],
                        "fromPath": str(src),
                        "toPath": str(dst),
                        "renamedBefore": 0,
                        "renamedAfter": 1,
                    }
                    moves_log.append(entry)
                    renamed_entries[str(r["id"])] = entry
                    renamed_count += 1

        if archive_duplicates:
            archive_dir = get_user_special_dir("Documents") / "GoodShelves_Archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            state = self.get_all_state()
            for p in state["lookalikes"]:
                if p.get("resolved"):
                    continue
                try:
                    copy_info = p["copy"]
                    fid = str(copy_info["id"])
                    src_p = Path(copy_info["path"])
                    dst_p = _unique_dest_path(archive_dir, src_p.name)
                    if src_p.exists():
                        shutil.move(str(src_p), str(dst_p))
                    pair_resolved = dict(p)
                    pair_resolved["resolved"] = True
                    pair_resolved["archived_path"] = str(dst_p)
                    with self._lock:
                        conn = self._connect()
                        try:
                            conn.execute("UPDATE files SET path = ?, archived = 1 WHERE id = ?", (str(dst_p), fid))
                            conn.execute(
                                """
                                INSERT INTO archive_history (pair_id, file_id, original_path, archived_path, pair_json, timestamp)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (p["id"], fid, str(src_p), str(dst_p), json.dumps(pair_resolved), time.time()),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                    archives_log.append({
                        "pairId": p["id"],
                        "fileId": fid,
                        "fromPath": str(src_p),
                        "toPath": str(dst_p),
                    })
                    archived_count += 1
                except Exception:
                    pass

        if organize_folders:
            for m in self.preview_tidy_folders():
                fid = str(m["fileId"])
                src = Path(m["fromPath"])
                dst_orig = Path(m["toPath"])
                if src.exists():
                    dst_orig.parent.mkdir(parents=True, exist_ok=True)
                    dst = _unique_dest_path(dst_orig.parent, dst_orig.name, src_path=src)
                    shutil.move(str(src), str(dst))
                    cur_renamed = 0
                    with self._lock:
                        conn = self._connect()
                        try:
                            ren_row = conn.execute("SELECT renamed FROM files WHERE id = ?", (fid,)).fetchone()
                            if ren_row:
                                cur_renamed = int(ren_row["renamed"] or 0)
                            conn.execute(
                                "UPDATE files SET path = ?, folder = ?, folder_short = ? WHERE id = ?",
                                (str(dst), str(dst.parent), format_path_short(dst.parent), fid),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                    if fid in renamed_entries:
                        renamed_entries[fid]["toPath"] = str(dst)
                    else:
                        moves_log.append({
                            "fileId": fid,
                            "fromPath": str(src),
                            "toPath": str(dst),
                            "renamedBefore": cur_renamed,
                            "renamedAfter": cur_renamed,
                        })
                    moved_count += 1

        parts = []
        if renamed_count:
            parts.append(f"{renamed_count} renamed")
        if archived_count:
            parts.append(f"{archived_count} archived")
        if moved_count:
            parts.append(f"{moved_count} organized")
        summary = "Smart Clean: " + (", ".join(parts) if parts else "no changes needed")

        if moves_log or archives_log:
            self._record_action(
                "smart_clean",
                summary,
                {"label": "Smart Clean", "moves": moves_log, "archives": archives_log},
            )
        self._invalidate_cache()
        self.recluster_all()
        return {
            "summary": summary,
            "renamedCount": renamed_count,
            "archivedCount": archived_count,
            "movedCount": moved_count,
        }

    # --- Real-time removal, safe device import, standard file ops, pins/tags/saved searches ---

    def remove_file_by_path(self, path_str: str) -> dict[str, Any]:
        """Remove a deleted file (or all active files under a deleted subfolder) from SQLite and vec_files."""
        target_p = Path(path_str).resolve() if path_str else None
        target = str(target_p) if target_p else ""
        removed = False
        removed_count = 0
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT rowid, id FROM files WHERE path = ? OR path = ?",
                    (path_str, target),
                ).fetchone()
                if row:
                    conn.execute("DELETE FROM vec_files WHERE rowid = ?", (row["rowid"],))
                    conn.execute("DELETE FROM files WHERE rowid = ?", (row["rowid"],))
                    removed = True
                    removed_count = 1
                elif target_p is not None:
                    # Handle external subfolder deletion/move by pruning indexed children under target_p
                    active_rows = conn.execute("SELECT rowid, path FROM files WHERE archived = 0").fetchall()
                    for ar in active_rows:
                        fp = Path(ar["path"])
                        try:
                            fp_res = fp.resolve()
                        except Exception:
                            fp_res = fp
                        if fp_res == target_p or target_p in fp_res.parents:
                            conn.execute("DELETE FROM vec_files WHERE rowid = ?", (ar["rowid"],))
                            conn.execute("DELETE FROM files WHERE rowid = ?", (ar["rowid"],))
                            removed = True
                            removed_count += 1
                if removed:
                    conn.commit()
                    self._invalidate_cache()
            finally:
                conn.close()
        return {"removed": removed, "removedCount": removed_count, "path": path_str}

    def import_from_device(
        self,
        device_path: str,
        destination_folder: Optional[str] = None,
        max_files: int = 120,
    ) -> dict[str, Any]:
        """Safely copy media/docs from a connected phone or USB drive into a local folder, then index."""
        src_root = Path(device_path).expanduser()
        if not src_root.exists() or not src_root.is_dir():
            raise ValueError(f"Device folder is not accessible: {device_path}")

        dev_label = re.sub(r"[^A-Za-z0-9_-]+", "_", src_root.name).strip("_") or "device"
        dest_root = (
            Path(destination_folder).expanduser().resolve()
            if destination_folder
            else (get_user_special_dir("Documents") / "GoodShelves_Imports" / dev_label).resolve()
        )
        dest_root.mkdir(parents=True, exist_ok=True)

        # Ensure destination is in watched_folders so it stays indexed
        self.add_watched_folder(str(dest_root))

        all_supported = IMAGE_EXTS | DOC_EXTS | CODE_EXTS | VIDEO_EXTS | AUDIO_EXTS | SPREADSHEET_EXTS | ARCHIVE_EXTS
        candidates: list[Path] = []
        # Prefer standard phone media/doc subdirectories first if present
        preferred_dirs = [src_root / d for d in ("DCIM", "Pictures", "Documents", "Downloads", "Music", "Movies")]
        scan_roots = [d for d in preferred_dirs if d.exists() and d.is_dir()] or [src_root]

        for root in scan_roots:
            try:
                for entry in root.rglob("*"):
                    if entry.is_file() and not any(p.startswith(".") for p in entry.parts):
                        if entry.suffix.lower() in all_supported:
                            candidates.append(entry)
                    if len(candidates) >= max_files * 2:
                        break
            except PermissionError:
                continue

        candidates = candidates[:max_files]
        total = len(candidates)
        imported_items: list[dict[str, Any]] = []
        for idx, src_fp in enumerate(candidates, start=1):
            self.emit_progress("importing", idx, total, src_fp.name)
            dst_fp = dest_root / src_fp.name
            if dst_fp.exists():
                stem = src_fp.stem
                suf = src_fp.suffix
                counter = 1
                while dst_fp.exists():
                    dst_fp = dest_root / f"{stem}_{counter}{suf}"
                    counter += 1
            shutil.copy2(str(src_fp), str(dst_fp))
            indexed = self.index_single_file(dst_fp, force=True, emit_telemetry=False, recluster=False)
            if indexed:
                imported_items.append({
                    "fileId": indexed["id"],
                    "fromPath": str(src_fp),
                    "toPath": str(dst_fp),
                })

        if imported_items:
            self.recluster_all()
            self._record_action(
                "device_import",
                f"Imported {len(imported_items)} file(s) from {src_root.name}",
                {"label": f"Import from {src_root.name}", "items": imported_items},
            )
            self._invalidate_cache()
        return {
            "importedCount": len(imported_items),
            "destination": str(dest_root),
        }

    def create_folder(self, parent_path: str, folder_name: str) -> dict[str, Any]:
        """Create a new folder inside a parent path and optionally watch it."""
        clean_name = re.sub(r'[<>:"/\\|?*]+', "_", folder_name).strip(" .")
        if not clean_name:
            raise ValueError("Folder name cannot be empty")
        parent = Path(parent_path).expanduser().resolve()
        target = parent / clean_name
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(target), "name": clean_name}

    def copy_file(self, file_id: str, target_folder: str) -> dict[str, Any]:
        """Copy an indexed file into target_folder and index the new copy."""
        item = self.get_file_by_id(file_id)
        if not item:
            raise ValueError("File not found")
        src = Path(item["path"])
        if not src.exists():
            raise ValueError("Source file no longer exists on disk")
        dst_dir = Path(target_folder).expanduser().resolve()
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        if dst.exists():
            counter = 1
            while dst.exists():
                dst = dst_dir / f"{src.stem}_copy{counter}{src.suffix}"
                counter += 1
        shutil.copy2(str(src), str(dst))
        new_item = self.index_single_file(dst, force=True, emit_telemetry=True, recluster=True)
        return {"ok": True, "path": str(dst), "file": new_item}

    def move_file(self, file_id: str, target_folder: str) -> dict[str, Any]:
        """Move a file to target_folder and record an undoable action."""
        item = self.get_file_by_id(file_id)
        if not item:
            raise ValueError("File not found")
        src = Path(item["path"])
        if not src.exists():
            raise ValueError("Source file no longer exists on disk")
        dst_dir = Path(target_folder).expanduser().resolve()
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        if dst.exists() and dst.resolve() != src.resolve():
            counter = 1
            while dst.exists():
                dst = dst_dir / f"{src.stem}_{counter}{src.suffix}"
                counter += 1
        shutil.move(str(src), str(dst))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ? WHERE id = ?",
                    (str(dst), dst.name, str(dst.parent), format_path_short(dst.parent), file_id),
                )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        self._record_action(
            "move_file",
            f"Moved {src.name} to {format_path_short(dst.parent)}",
            {
                "label": f"Move {src.name}",
                "moves": [{"fileId": file_id, "fromPath": str(src), "toPath": str(dst), "renamedAfter": int(item.get("renamed", False))}],
            },
        )
        return {"ok": True, "path": str(dst)}

    def trash_file(self, file_id: str) -> dict[str, Any]:
        """Move a file to GoodShelves_Trash (reversible via Undo) and archive from active index."""
        item = self.get_file_by_id(file_id)
        if not item:
            raise ValueError("File not found")
        src = Path(item["path"])
        trash_dir = (get_user_special_dir("Documents") / "GoodShelves_Trash").resolve()
        trash_dir.mkdir(parents=True, exist_ok=True)
        dst = _unique_dest_path(trash_dir, src.name)
        if src.exists():
            shutil.move(str(src), str(dst))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE files SET path = ?, folder = ?, folder_short = ?, archived = 1, original_path = ? WHERE id = ?",
                    (str(dst), str(dst.parent), format_path_short(dst.parent), str(src), file_id),
                )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        self._record_action(
            "trash_file",
            f"Moved {src.name} to Trash",
            {"label": f"Trash {src.name}", "items": [{"fileId": file_id, "fromPath": str(src), "toPath": str(dst)}]},
        )
        return {"ok": True, "trashedPath": str(dst)}

    def rename_file_custom(self, file_id: str, new_name: str) -> dict[str, Any]:
        """Rename a file on disk to a user-provided filename and record an undoable action."""
        clean = re.sub(r'[<>:"/\\|?*]+', "_", (new_name or "").strip()).strip(" .")
        if not clean:
            raise ValueError("Filename cannot be empty")
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
                if not row:
                    raise ValueError(f"File not found: {file_id}")
                cur_path = Path(row["path"])
                if not cur_path.exists():
                    raise ValueError("Source file no longer exists on disk")
                # Preserve original extension if user omitted one
                if "." not in clean and cur_path.suffix:
                    clean = f"{clean}{cur_path.suffix}"
                if clean == cur_path.name:
                    return {"ok": True, "file": self.get_file_by_id(file_id, conn=conn)}
                target_path = cur_path.parent / clean
                if target_path.exists():
                    raise ValueError(f"A file named '{clean}' already exists in this folder")
                cur_path.rename(target_path)
                new_title = target_path.stem.replace("_", " ").replace("-", " ").strip() or target_path.stem
                conn.execute(
                    "UPDATE files SET path = ?, name = ?, title = ?, renamed = 1 WHERE id = ?",
                    (str(target_path), clean, new_title, file_id),
                )
                conn.execute(
                    """
                    INSERT INTO rename_history (file_id, old_path, new_path, old_name, new_name, timestamp, undone)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (file_id, str(cur_path), str(target_path), cur_path.name, clean, time.time()),
                )
                conn.execute(
                    "INSERT INTO action_log (action_type, summary, detail_json, timestamp, undone) VALUES (?, ?, ?, ?, 0)",
                    (
                        "rename",
                        f"Renamed {cur_path.name} → {clean}",
                        json.dumps({
                            "label": f"rename {cur_path.name}",
                            "moves": [{
                                "fileId": file_id,
                                "fromPath": str(cur_path),
                                "toPath": str(target_path),
                                "renamedAfter": 1,
                            }],
                        }),
                        time.time(),
                    ),
                )
                conn.commit()
                self._invalidate_cache()
                updated = self.get_file_by_id(file_id, conn=conn)
                return {"ok": True, "file": updated}
            finally:
                conn.close()

    def delete_file_permanently(self, file_id: str) -> dict[str, Any]:
        """Permanently delete a file from disk and remove it from the local index."""
        item = self.get_file_by_id(file_id)
        if not item:
            raise ValueError("File not found")
        src = Path(item["path"])
        if src.exists():
            src.unlink()
        self.remove_file_by_path(str(src))
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT rowid FROM files WHERE id = ?", (file_id,)).fetchone()
                if row:
                    conn.execute("DELETE FROM vec_files WHERE rowid = ?", (row["rowid"],))
                conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"ok": True, "deletedPath": str(src)}

    def open_file_external(self, file_id: str, reveal: bool = False) -> dict[str, Any]:
        """Open a file with the system default app or reveal it in the system file manager."""
        item = self.get_file_by_id(file_id)
        if not item:
            raise ValueError("File not found")
        p = Path(item["path"])
        if not p.exists():
            raise ValueError("File does not exist on disk")
        sys_name = platform.system()
        target = str(p.parent if reveal else p)
        if sys_name == "Windows":
            if reveal:
                subprocess.Popen(["explorer", f"/select,{p}"], **_subprocess_win_kwargs())
            else:
                os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys_name == "Darwin":
            cmd = ["open", "-R", str(p)] if reveal else ["open", str(p)]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}

    def bulk_file_action(
        self,
        file_ids: list[str],
        action: str,
        target_folder: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute a bulk operation (move, trash, tag, pin, unpin) across multiple files."""
        clean_ids = [fid for fid in file_ids if fid]
        if not clean_ids:
            return {"ok": True, "count": 0}

        if action == "move":
            if not target_folder:
                raise ValueError("target_folder is required for bulk move")
            dst_dir = Path(target_folder).expanduser().resolve()
            dst_dir.mkdir(parents=True, exist_ok=True)
            moves_log: list[dict[str, Any]] = []
            with self._lock:
                conn = self._connect()
                try:
                    for fid in clean_ids:
                        row = conn.execute("SELECT path, renamed FROM files WHERE id = ?", (fid,)).fetchone()
                        if not row:
                            continue
                        src = Path(row["path"])
                        if not src.exists():
                            continue
                        dst = dst_dir / src.name
                        if dst.exists() and dst.resolve() != src.resolve():
                            c = 1
                            while dst.exists():
                                dst = dst_dir / f"{src.stem}_{c}{src.suffix}"
                                c += 1
                        shutil.move(str(src), str(dst))
                        conn.execute(
                            "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ? WHERE id = ?",
                            (str(dst), dst.name, str(dst.parent), format_path_short(dst.parent), fid),
                        )
                        moves_log.append({"fileId": fid, "fromPath": str(src), "toPath": str(dst), "renamedAfter": int(row["renamed"] or 0)})
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            if moves_log:
                self._record_action(
                    "bulk_move",
                    f"Moved {len(moves_log)} file(s) to {format_path_short(dst_dir)}",
                    {"label": f"Bulk Move ({len(moves_log)})", "moves": moves_log},
                )
            return {"ok": True, "count": len(moves_log)}

        if action == "trash":
            trash_dir = (get_user_special_dir("Documents") / "GoodShelves_Trash").resolve()
            trash_dir.mkdir(parents=True, exist_ok=True)
            items_log: list[dict[str, Any]] = []
            with self._lock:
                conn = self._connect()
                try:
                    for fid in clean_ids:
                        row = conn.execute("SELECT path FROM files WHERE id = ?", (fid,)).fetchone()
                        if not row:
                            continue
                        src = Path(row["path"])
                        dst = _unique_dest_path(trash_dir, src.name)
                        if src.exists():
                            shutil.move(str(src), str(dst))
                        conn.execute(
                            "UPDATE files SET path = ?, folder = ?, folder_short = ?, archived = 1, original_path = ? WHERE id = ?",
                            (str(dst), str(dst.parent), format_path_short(dst.parent), str(src), fid),
                        )
                        items_log.append({"fileId": fid, "fromPath": str(src), "toPath": str(dst)})
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            if items_log:
                self._record_action(
                    "bulk_trash",
                    f"Moved {len(items_log)} file(s) to Trash",
                    {"label": f"Bulk Trash ({len(items_log)})", "items": items_log},
                )
            return {"ok": True, "count": len(items_log)}

        if action in {"pin", "unpin"}:
            val = 1 if action == "pin" else 0
            pin_entries: list[dict[str, Any]] = []
            with self._lock:
                conn = self._connect()
                try:
                    for fid in clean_ids:
                        row = conn.execute("SELECT pinned FROM files WHERE id = ?", (fid,)).fetchone()
                        if not row:
                            continue
                        prev_val = int(row["pinned"] or 0)
                        conn.execute("UPDATE files SET pinned = ? WHERE id = ?", (val, fid))
                        pin_entries.append({"fileId": fid, "before": prev_val, "after": val})
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            if pin_entries:
                verb = "Pinned" if val == 1 else "Unpinned"
                self._record_action(
                    "bulk_pin",
                    f"{verb} {len(pin_entries)} file(s)",
                    {"label": f"{verb} ({len(pin_entries)})", "entries": pin_entries},
                )
            return {"ok": True, "count": len(pin_entries)}

        if action in {"tag", "untag"} and tag:
            raw_parts = [p.strip(" #\"'“”").lower() for p in re.split(r"[,;]+", tag) if p.strip(" #\"'“”")]
            clean_tags: list[str] = []
            for p in raw_parts:
                if p and p not in clean_tags:
                    clean_tags.append(p)
            if not clean_tags:
                raise ValueError("Tag name cannot be empty")
            tag_entries: list[dict[str, Any]] = []
            with self._lock:
                conn = self._connect()
                try:
                    for fid in clean_ids:
                        row = conn.execute("SELECT tags_json FROM files WHERE id = ?", (fid,)).fetchone()
                        if not row:
                            continue
                        cur_tags = json.loads(row["tags_json"] or "[]")
                        next_tags = list(cur_tags)
                        if action == "tag":
                            for ct in clean_tags:
                                if ct not in next_tags:
                                    next_tags.append(ct)
                        else:
                            next_tags = [t for t in next_tags if str(t).lower() not in clean_tags]
                        conn.execute("UPDATE files SET tags_json = ? WHERE id = ?", (json.dumps(next_tags), fid))
                        tag_entries.append({"fileId": fid, "before": cur_tags, "after": next_tags})
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            if tag_entries:
                tags_disp = ", ".join(f"#{t}" for t in clean_tags)
                verb = "Tagged" if action == "tag" else "Untagged"
                self._record_action(
                    "bulk_tag" if action == "tag" else "bulk_untag",
                    f"{verb} {len(tag_entries)} file(s) ({tags_disp})",
                    {"label": f"{verb} {tags_disp}", "entries": tag_entries},
                )
            return {"ok": True, "count": len(tag_entries)}

        raise ValueError(f"Unsupported bulk action: {action}")

    def toggle_pin_folder(self, folder_path: str, pinned: Optional[bool] = None) -> dict[str, Any]:
        resolved = Path(os.path.expanduser(folder_path)).resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"Folder not found: {folder_path}")
        p_str = str(resolved)
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT path FROM pinned_folders WHERE path = ?",
                    (p_str,),
                ).fetchone()
                should_pin = (existing is None) if pinned is None else bool(pinned)
                if should_pin:
                    conn.execute(
                        "INSERT INTO pinned_folders (path, display_name, pinned_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET display_name = excluded.display_name, pinned_at = excluded.pinned_at",
                        (p_str, resolved.name or p_str, time.time()),
                    )
                else:
                    conn.execute("DELETE FROM pinned_folders WHERE path = ?", (p_str,))
                conn.commit()
                self._invalidate_cache()
                return {"path": p_str, "name": resolved.name or p_str, "pinned": should_pin}
            finally:
                conn.close()

    def delete_ai_memory_entry(self, memory_id: int) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM ai_memory WHERE id = ?", (int(memory_id),))
                conn.commit()
                self._invalidate_cache()
                return {"ok": True}
            finally:
                conn.close()

    def clear_ai_memory(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM ai_memory")
                conn.execute("DELETE FROM user_affinities")
                conn.commit()
                self._invalidate_cache()
                return {"ok": True}
            finally:
                conn.close()

    def _learn_from_executed_action(
        self,
        action_type: str,
        req: dict[str, Any],
        file_ids: list[str],
    ) -> None:
        """Continuously learn routing, tag, and exclusion preferences from real user confirmations."""
        if not self.get_settings().get("adaptive_learning", True):
            return
        unselected_ids = [str(fid) for fid in (req.get("unselectedIds") or []) if fid]
        target_folder = str(req.get("targetFolder") or "").strip()
        tag_val = str(req.get("tag") or "").strip()
        source_query = str(req.get("sourceQuery") or "").strip()

        with self._lock:
            conn = self._connect()
            try:
                if action_type in {"move", "copy"} and target_folder and file_ids:
                    placeholders = ",".join("?" for _ in file_ids[:20])
                    rows = conn.execute(
                        f"SELECT name, specimen_type, modality, ext FROM files WHERE id IN ({placeholders})",
                        file_ids[:20],
                    ).fetchall()
                    learned_keys: set[str] = set()
                    for tok in _search_tokens(source_query):
                        if len(tok) >= 3 and tok not in _SEARCH_STOPWORDS:
                            learned_keys.add(tok)
                    for r in rows:
                        spec = str(r["specimen_type"] or "").lower()
                        if spec in {"receipt", "diagram", "code", "idcard"}:
                            learned_keys.add(spec)
                        fmt = str(r["ext"] or "").lower()
                        if fmt:
                            learned_keys.add(fmt)
                    for k in list(learned_keys)[:5]:
                        upsert_ai_memory(conn, "route", k, target_folder, delta_weight=1.2)

                if action_type == "tag" and tag_val and file_ids:
                    for tok in _search_tokens(source_query)[:4]:
                        if len(tok) >= 3 and tok not in _SEARCH_STOPWORDS:
                            upsert_ai_memory(conn, "tag_pref", tok, tag_val, delta_weight=1.0)

                if unselected_ids:
                    placeholders = ",".join("?" for _ in unselected_ids[:15])
                    skipped_rows = conn.execute(
                        f"SELECT ext, folder FROM files WHERE id IN ({placeholders})",
                        unselected_ids[:15],
                    ).fetchall()
                    for sr in skipped_rows:
                        fmt = str(sr["ext"] or "").lower().lstrip(".")
                        if fmt:
                            upsert_ai_memory(conn, "exclude", f"ext:{fmt}", "skip", delta_weight=0.7)

                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()

    def toggle_pin_file(self, file_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT pinned FROM files WHERE id = ?", (file_id,)).fetchone()
                if not row:
                    raise ValueError("File not found")
                new_val = 0 if int(row["pinned"] or 0) else 1
                conn.execute("UPDATE files SET pinned = ? WHERE id = ?", (new_val, file_id))
                conn.commit()
                self._invalidate_cache()
                return {"fileId": file_id, "pinned": bool(new_val)}
            finally:
                conn.close()

    def set_file_tags(self, file_id: str, tags: list[str]) -> dict[str, Any]:
        clean = []
        seen: set[str] = set()
        for t in tags:
            s = str(t).strip().lower()
            if s and s not in seen:
                seen.add(s)
                clean.append(s)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE files SET tags_json = ? WHERE id = ?", (json.dumps(clean), file_id))
                conn.commit()
                self._invalidate_cache()
                return {"fileId": file_id, "tags": clean}
            finally:
                conn.close()

    def set_file_shelf(self, file_id: str, shelf_id: str) -> dict[str, Any]:
        """Manually assign a file to a specific shelf (persisted across re-clustering)."""
        target_sid = (shelf_id or "").strip()
        if target_sid in {"", "auto"}:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute("UPDATE files SET user_shelf_id = '' WHERE id = ?", (file_id,))
                    conn.commit()
                finally:
                    conn.close()
            self.recluster_all()
            updated = self.get_file_by_id(file_id)
            return {
                "ok": True,
                "fileId": file_id,
                "shelfId": updated["shelfId"] if updated else "",
                "shelfName": updated["shelfName"] if updated else "",
            }

        cat_by_sid = {v[0]: v for v in _GENERAL_CATEGORIES.values()}
        with self._lock:
            conn = self._connect()
            try:
                f_row = conn.execute("SELECT id FROM files WHERE id = ?", (file_id,)).fetchone()
                if not f_row:
                    raise ValueError(f"File not found: {file_id}")

                target_name = ""
                if target_sid.startswith("custom_"):
                    cr = conn.execute("SELECT name, file_ids_json FROM custom_shelves WHERE shelf_id = ?", (target_sid,)).fetchone()
                    if not cr:
                        raise ValueError(f"Custom shelf not found: {target_sid}")
                    target_name = str(cr["name"])
                    try:
                        cur_ids = json.loads(cr["file_ids_json"] or "[]")
                    except Exception:
                        cur_ids = []
                    if file_id not in cur_ids:
                        cur_ids.append(file_id)
                        conn.execute(
                            "UPDATE custom_shelves SET file_ids_json = ? WHERE shelf_id = ?",
                            (json.dumps(cur_ids), target_sid),
                        )
                else:
                    sh_row = conn.execute("SELECT name FROM shelves WHERE shelf_id = ?", (target_sid,)).fetchone()
                    ren_row = conn.execute("SELECT custom_name FROM shelf_renames WHERE shelf_id = ?", (target_sid,)).fetchone()
                    if ren_row:
                        target_name = str(ren_row["custom_name"])
                    elif sh_row:
                        target_name = str(sh_row["name"])
                    elif target_sid in cat_by_sid:
                        target_name = cat_by_sid[target_sid][1]
                    else:
                        raise ValueError(f"Shelf not found: {target_sid}")

                    if not sh_row and target_sid in cat_by_sid:
                        _, def_name, def_desc = cat_by_sid[target_sid]
                        max_seq = conn.execute("SELECT COALESCE(MAX(seq_order), -1) AS m FROM shelves").fetchone()["m"]
                        conn.execute(
                            """
                            INSERT INTO shelves (shelf_id, cluster_id, num, name, desc, centroid_x, centroid_y, pinned, seq_order)
                            VALUES (?, -1, ?, ?, ?, 50.0, 50.0, 0, ?)
                            """,
                            (target_sid, f"{int(max_seq) + 2:02d}", target_name or def_name, def_desc, int(max_seq) + 1),
                        )

                conn.execute(
                    "UPDATE files SET user_shelf_id = ?, shelf_id = ?, shelf_name = ? WHERE id = ?",
                    (target_sid, target_sid, target_name, file_id),
                )
                conn.commit()
                self._invalidate_cache()
                return {"ok": True, "fileId": file_id, "shelfId": target_sid, "shelfName": target_name}
            finally:
                conn.close()

    def swap_lookalike_pair(self, pair_id: str) -> dict[str, Any]:
        """Toggle which file in a lookalike pair is kept vs marked as redundant copy."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM settings WHERE key = 'swapped_lookalikes'").fetchone()
                swapped: list[str] = []
                if row and row["value"]:
                    try:
                        swapped = [str(x) for x in json.loads(row["value"]) if str(x)]
                    except Exception:
                        swapped = []
                if pair_id in swapped:
                    swapped = [x for x in swapped if x != pair_id]
                    is_swapped = False
                else:
                    swapped.append(pair_id)
                    is_swapped = True
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('swapped_lookalikes', ?)",
                    (json.dumps(swapped),),
                )
                conn.commit()
                self._invalidate_cache()
                return {"ok": True, "pairId": pair_id, "swapped": is_swapped}
            finally:
                conn.close()

    def toggle_pin_shelf(self, shelf_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM settings WHERE key = 'pinned_shelves'").fetchone()
                pinned_ids: list[str] = []
                if row and row["value"]:
                    try:
                        pinned_ids = list(json.loads(row["value"]))
                    except Exception:
                        pinned_ids = []
                if shelf_id in pinned_ids:
                    pinned_ids = [s for s in pinned_ids if s != shelf_id]
                    is_pinned = False
                else:
                    pinned_ids.append(shelf_id)
                    is_pinned = True
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('pinned_shelves', ?)",
                    (json.dumps(pinned_ids),),
                )
                conn.execute("UPDATE shelves SET pinned = ? WHERE shelf_id = ?", (1 if is_pinned else 0, shelf_id))
                conn.commit()
                self._invalidate_cache()
                return {"shelfId": shelf_id, "pinned": is_pinned}
            finally:
                conn.close()

    def list_saved_searches(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM saved_searches ORDER BY created_at DESC").fetchall()
                return [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "query": r["query"],
                        "modality": r["modality"],
                        "tag": r["tag"],
                        "createdAt": r["created_at"],
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    def save_search(self, name: str, query: str, modality: str = "all", tag: str = "") -> dict[str, Any]:
        sid = f"ss_{abs(hash((name, query, modality, tag, time.time()))) % 10000000}"
        now = time.time()
        clean_name = name.strip() or query.strip()[:30] or "Saved search"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO saved_searches (id, name, query, modality, tag, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, clean_name, query, modality, tag, now),
                )
                conn.commit()
                self._invalidate_cache()
                return {"id": sid, "name": clean_name, "query": query, "modality": modality, "tag": tag, "createdAt": now}
            finally:
                conn.close()

    def delete_saved_search(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
                conn.commit()
                self._invalidate_cache()
                return {"deleted": True, "id": search_id}
            finally:
                conn.close()

    def get_vision_models(self) -> dict[str, Any]:
        default_vis = resolve_default_vision_model(self.embedder.models_dir)
        active_id = str(self.get_settings().get("vision_model", default_vis))
        return {
            "activeModel": active_id,
            "models": get_catalog_status(self.embedder.models_dir, active_id),
        }

    def get_vision_download_progress(self) -> dict[str, Any]:
        with self._dl_lock:
            if not self._vision_download_state:
                return {
                    "active": False,
                    "modelId": "",
                    "modelName": "",
                    "status": "idle",
                    "stage": "",
                    "fileName": "",
                    "downloadedBytes": 0,
                    "totalBytes": 0,
                    "percent": 0,
                    "message": "",
                }
            return dict(self._vision_download_state)

    def select_vision_model(self, model_id: str, download: bool = True) -> dict[str, Any]:
        """Download (if requested & missing) and activate a local vision-language model."""
        status_list = get_catalog_status(self.embedder.models_dir, model_id)
        target = next((m for m in status_list if m["id"] == model_id), None)
        if target is None:
            raise ValueError(f"Unknown vision model: {model_id}")

        def _set_dl_state(patch: dict[str, Any]) -> None:
            with self._dl_lock:
                self._vision_download_state = {"active": True, **patch}

        try:
            if not target["jointTextReady"] and download:
                init_state = {
                    "modelId": model_id,
                    "modelName": target["name"],
                    "status": "downloading",
                    "stage": "preparing",
                    "fileName": "",
                    "downloadedBytes": 0,
                    "totalBytes": 0,
                    "percent": 2,
                    "message": f"Starting download for {target['name']}…",
                }
                _set_dl_state(init_state)
                self._emit_stage(3, "embed", "running", target["name"], init_state["message"], 0.0)
                self.emit_progress(f"downloading {target['name']}", 2, 100, init_state["message"])

                def _on_dl_detail(info: dict[str, Any]) -> None:
                    _set_dl_state(info)
                    pct = int(info.get("percent") or 0)
                    dl_b = int(info.get("downloadedBytes") or 0)
                    tot_b = int(info.get("totalBytes") or 0)
                    fn = str(info.get("fileName") or "")
                    if tot_b > 1024 * 1024:
                        label = f"{fn} · {dl_b / (1024 * 1024):.1f} / {tot_b / (1024 * 1024):.1f} MB"
                    else:
                        label = str(info.get("message") or fn or "Downloading…")
                    self._emit_stage(3, "embed", "running", target["name"], label, 0.0)
                    self.emit_progress(f"downloading {target['name']}", pct, 100, label)

                download_vision_model(
                    self.embedder.models_dir,
                    model_id,
                    detail_cb=_on_dl_detail,
                )

            _set_dl_state({
                "modelId": model_id,
                "modelName": target["name"],
                "status": "activating",
                "stage": "activating",
                "fileName": "",
                "downloadedBytes": 0,
                "totalBytes": 0,
                "percent": 100,
                "message": f"Loading {target['name']} into runtime…",
            })
            self.update_setting("vision_model", model_id)
            self.embedder.set_vision_model(model_id)
            self._emit_stage(3, "embed", "done", target["name"], f"activated {target['name']}", 0.0)
            self.emit_progress("idle", 100, 100, "")
            return self.get_vision_models()
        finally:
            with self._dl_lock:
                self._vision_download_state = None
            self.emit_progress("idle", 100, 100, "")

    def reindex_images_only(self) -> dict[str, Any]:
        """Re-embed all indexed image files using the currently active vision-language model."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT path FROM files WHERE archived = 0 AND modality = 'image'"
                ).fetchall()
            finally:
                conn.close()

        total = len(rows)
        updated = 0
        for idx, r in enumerate(rows):
            p = Path(r["path"])
            if p.exists():
                self.emit_progress("re-indexing images", idx + 1, total, p.name)
                try:
                    self.index_single_file(p, force=True, emit_telemetry=False, recluster=False)
                    updated += 1
                except Exception:
                    continue
        if updated > 0:
            self.recluster_all()
        return {"ok": True, "reindexed": updated, "total": total}

    # --- Signature AI & Complete File Manager Methods ---

    def check_duplicate_for_file(self, file_id: str) -> Optional[dict[str, Any]]:
        """Check if an indexed file matches or closely resembles an already-indexed file."""
        target = self.get_file_by_id(file_id)
        if not target:
            return None
        with self._lock:
            conn = self._connect()
            try:
                exact = conn.execute(
                    "SELECT id, name, folder_short, path FROM files WHERE archived = 0 AND sha256 = ? AND id != ? LIMIT 1",
                    (target["sha256"], file_id),
                ).fetchone()
                if exact:
                    return {
                        "id": exact["id"],
                        "name": exact["name"],
                        "folder": exact["folder_short"],
                        "path": exact["path"],
                        "similarityPct": 100,
                        "kind": "exact",
                    }
                row = conn.execute("SELECT embedding_blob FROM files WHERE id = ?", (file_id,)).fetchone()
                if not row or not row["embedding_blob"]:
                    return None
                vec_hits = conn.execute(
                    "SELECT rowid, distance FROM vec_files WHERE embedding MATCH ? AND k = 5",
                    (row["embedding_blob"],),
                ).fetchall()
                for vh in vec_hits:
                    sim = float(1.0 - float(vh["distance"]))
                    if sim < 0.94:
                        continue
                    peer = conn.execute(
                        "SELECT id, name, folder_short, path FROM files WHERE rowid = ? AND archived = 0",
                        (int(vh["rowid"]),),
                    ).fetchone()
                    if peer and str(peer["id"]) != file_id:
                        return {
                            "id": peer["id"],
                            "name": peer["name"],
                            "folder": peer["folder_short"],
                            "path": peer["path"],
                            "similarityPct": min(99, int(round(sim * 100))),
                            "kind": "similar",
                        }
            finally:
                conn.close()
        return None

    def find_similar_files(self, file_id: str, limit: int = 24) -> dict[str, Any]:
        """Find visually and semantically similar files using a file's own 512-d vector."""
        t0 = time.perf_counter()
        src_dict = self.get_file_by_id(file_id)
        if not src_dict:
            raise ValueError(f"File not found: {file_id}")
        with self._lock:
            conn = self._connect()
            try:
                src_row = conn.execute("SELECT rowid, embedding_blob, shelf_id, tags_json FROM files WHERE id = ?", (file_id,)).fetchone()
                if not src_row or not src_row["embedding_blob"]:
                    return {"sourceFile": src_dict, "results": [], "latencyMs": 0.5}
                total_rows = conn.execute("SELECT COUNT(*) AS c FROM vec_files").fetchone()["c"]
                k_fetch = min(int(total_rows), max(limit + 8, 30))
                if k_fetch <= 1:
                    return {"sourceFile": src_dict, "results": [], "latencyMs": 0.5}
                hits = conn.execute(
                    "SELECT rowid, distance FROM vec_files WHERE embedding MATCH ? AND k = ?",
                    (src_row["embedding_blob"], k_fetch),
                ).fetchall()
                results: list[dict[str, Any]] = []
                for vh in hits:
                    rid = int(vh["rowid"])
                    if rid == int(src_row["rowid"]):
                        continue
                    fr = conn.execute("SELECT * FROM files WHERE rowid = ? AND archived = 0", (rid,)).fetchone()
                    if not fr:
                        continue
                    sim = float(np.clip(1.0 - float(vh["distance"]), 0.0, 0.999))
                    pct = min(99, max(1, int(round(sim * 100))))
                    shared_shelf = str(fr["shelf_id"]) == str(src_row["shelf_id"]) and str(fr["shelf_id"]) != "tray"
                    why = f"{pct}% vector similarity to “{src_dict['name']}”"
                    if shared_shelf:
                        why += f" · same shelf ({src_dict['shelfName']})"
                    results.append(self._row_to_file_dict(fr, cosine_sim=sim, match_score=pct, custom_why=why))
                    if len(results) >= limit:
                        break
            finally:
                conn.close()
        return {
            "sourceFile": src_dict,
            "results": results,
            "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    @staticmethod
    def _evaluate_quick_utility(q_clean: str, name_comma: str) -> Optional[str]:
        """Handle local date/time/calendar queries and safe arithmetic expressions offline."""
        q_low = q_clean.lower().strip(" ?!.,")

        if re.search(r"\b(what time is it|current time|time now)\b", q_low):
            return f"It's {datetime.now().strftime('%I:%M %p on %A, %B %d, %Y')}{name_comma}."
        if re.search(r"\b(what(?:'s| is) (?:the )?(?:date today|today's date|today)|what day is it)\b", q_low):
            return f"Today is {datetime.now().strftime('%A, %B %d, %Y')}{name_comma}."

        pct_match = re.match(
            r"^(?:what is |calculate |compute |how much is )?(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)\s*$",
            q_low,
        )
        if pct_match:
            pct = float(pct_match.group(1))
            base = float(pct_match.group(2))
            val = (pct / 100.0) * base
            fmt = f"{int(val)}" if val.is_integer() else f"{val:,.4f}".rstrip("0").rstrip(".")
            return f"{pct_match.group(1)}% of {pct_match.group(2)} is {fmt}{name_comma}."

        math_candidate = re.sub(
            r"^(?:what is |what's |calculate |compute |solve |eval(?:uate)? |how much is )",
            "",
            q_low,
        ).strip()
        if math_candidate and re.fullmatch(r"[0-9\.\+\-\*/\(\)%\s\^]+", math_candidate) and re.search(r"[\+\-\*/\^%]", math_candidate):
            expr = math_candidate.replace("^", "**")
            if len(expr) <= 80:
                ops = {
                    ast.Add: operator.add,
                    ast.Sub: operator.sub,
                    ast.Mult: operator.mul,
                    ast.Div: operator.truediv,
                    ast.FloorDiv: operator.floordiv,
                    ast.Mod: operator.mod,
                    ast.Pow: operator.pow,
                    ast.USub: operator.neg,
                    ast.UAdd: operator.pos,
                }

                def _eval_node(node: ast.AST) -> float:
                    if isinstance(node, ast.Expression):
                        return _eval_node(node.body)
                    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                        return float(node.value)
                    if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
                        return float(ops[type(node.op)](_eval_node(node.operand)))
                    if isinstance(node, ast.BinOp) and type(node.op) in ops:
                        left = _eval_node(node.left)
                        right = _eval_node(node.right)
                        if isinstance(node.op, ast.Pow) and abs(right) > 12:
                            raise ValueError("Exponent too large")
                        return float(ops[type(node.op)](left, right))
                    raise ValueError("Unsupported math expression")

                try:
                    tree = ast.parse(expr, mode="eval")
                    res = _eval_node(tree)
                    fmt = f"{int(res):,}" if res.is_integer() else f"{res:,.4f}".rstrip("0").rstrip(".")
                    return f"{math_candidate} = {fmt}{name_comma}."
                except Exception:
                    pass
        return None

    def _make_citation(self, f: dict[str, Any], snippet: str = "", score: int = 80) -> dict[str, Any]:
        default_snip = f"{f.get('shelfName', '')} · {f.get('folder', '')}/{f.get('name', '')}".strip(" ·/")
        cleaned_snip = clean_extracted_text(snippet or default_snip, preserve_newlines=False)
        return {
            "fileId": f["id"],
            "fileName": f["name"],
            "title": f["title"],
            "format": f["format"],
            "modality": f.get("modality", "document"),
            "folder": f.get("folder", ""),
            "folderFull": f.get("folderFull", ""),
            "path": f.get("path", ""),
            "size": f.get("size", ""),
            "sizeBytes": int(f.get("sizeBytes", 0) or 0),
            "mtime": float(f.get("mtime", 0) or 0),
            "date": f["date"],
            "shelfName": f["shelfName"],
            "subShelf": f.get("subShelf", ""),
            "snippet": cleaned_snip[:280],
            "hasThumbnail": bool(f.get("hasThumbnail")),
            "score": min(99, max(1, int(score))),
        }

    @staticmethod
    def _make_action_item(
        f: dict[str, Any],
        detail: str = "",
        selected_by_default: bool = True,
        step_index: Optional[int] = None,
        step_title: str = "",
    ) -> dict[str, Any]:
        raw_prev = str(f.get("extractedText") or f.get("ocrText") or "")
        preview = clean_extracted_text(raw_prev[:220], preserve_newlines=False)[:120]
        item: dict[str, Any] = {
            "fileId": f["id"],
            "fileName": f["name"],
            "title": f["title"],
            "format": f["format"],
            "modality": f.get("modality", "document"),
            "folder": f.get("folder", ""),
            "folderFull": f.get("folderFull", ""),
            "path": f.get("path", ""),
            "size": f.get("size", ""),
            "date": f["date"],
            "shelfName": f.get("shelfName", ""),
            "detail": detail,
            "previewText": preview,
            "hasThumbnail": bool(f.get("hasThumbnail")),
            "selectedByDefault": selected_by_default,
        }
        if step_index is not None:
            item["stepIndex"] = step_index
        if step_title:
            item["stepTitle"] = step_title
        return item

    @staticmethod
    def _build_smart_choices(
        intent_mode: str,
        citations: list[dict[str, Any]],
        proposed_action: Optional[dict[str, Any]],
        q_low: str,
    ) -> list[dict[str, str]]:
        """Generate Claude/ChatGPT-style clickable follow-up & refinement choices."""
        choices: list[dict[str, str]] = []
        if proposed_action:
            items = proposed_action.get("items") or []
            if len(items) > 2 and proposed_action.get("requiresSelection"):
                fmts = {str(it.get("format") or "").upper() for it in items}
                if "PDF" in fmts and len(fmts) > 1:
                    choices.append({"label": "Only PDFs", "prompt": "only include pdfs", "kind": "clarify"})
                if any(it.get("modality") == "image" for it in items) and len(fmts) > 1:
                    choices.append({"label": "Exclude images", "prompt": "exclude images", "kind": "clarify"})
            return choices[:3]

        if citations:
            mods = {str(c.get("modality") or "") for c in citations}
            if intent_mode in ("list", "count") or len(citations) >= 2:
                choices.append({"label": "Summarize key points", "prompt": "summarize these files", "kind": "followup"})
                choices.append({"label": "Create a shelf from these", "prompt": "create a shelf called Selected Items with these files", "kind": "action"})
                choices.append({"label": "Tag these files", "prompt": "tag these files with reviewed", "kind": "action"})
            elif len(citations) == 1:
                fn = citations[0].get("fileName") or citations[0].get("title") or "this file"
                choices.append({"label": f"Summarize {fn[:22]}", "prompt": f"summarize {fn}", "kind": "followup"})
                choices.append({"label": "Find related files", "prompt": f"find files similar to {citations[0].get('title', fn)}", "kind": "followup"})
                choices.append({"label": "Pin to shelf top", "prompt": f"pin {fn}", "kind": "action"})
            if "image" in mods and intent_mode != "finance" and len(choices) < 4:
                choices.append({"label": "Move these to a folder", "prompt": "move these to Archive", "kind": "clarify"})
        elif any(w in q_low for w in ("hello", "hi", "hey", "what can you do", "help")):
            choices = [
                {"label": "What's on my shelves?", "prompt": "what's on my shelves?", "kind": "followup"},
                {"label": "Any duplicates?", "prompt": "clean up duplicates", "kind": "action"},
                {"label": "How much space?", "prompt": "how much space am I using?", "kind": "followup"},
            ]
        return choices[:4]

    def _resolve_folder_candidate(
        self,
        hint: str,
        files: list[dict[str, Any]],
        watched_folders: list[dict[str, Any]],
        allow_create_sub: bool = False,
        preferred_parent: Optional[Path] = None,
    ) -> Optional[dict[str, Any]]:
        clean = re.sub(r'^(?:a |an |the )?(?:new\s+)?folder\s+(?:called|named)\s+', "", (hint or "").strip(), flags=re.IGNORECASE)
        clean = re.sub(r'^(?:the\s+)?folder\s+', "", clean, flags=re.IGNORECASE).strip(" \"'“”`?!.,")
        clean = re.sub(r'\s+folder$', "", clean, flags=re.IGNORECASE).strip()
        if not clean:
            return None

        # Direct filesystem path when user explicitly provides /, ~, or Windows drive
        if clean.startswith(("/", "~")) or (len(clean) >= 3 and clean[1:3] in (":\\", ":/")):
            cand = Path(clean).expanduser().resolve()
            if cand.exists() and cand.is_dir():
                return {"path": str(cand), "label": format_path_short(cand), "exists": True, "willCreate": False}
            if allow_create_sub and cand.parent.exists():
                return {"path": str(cand), "label": format_path_short(cand), "exists": False, "willCreate": True}

        low = clean.lower()
        known_dirs: list[Path] = []
        seen_paths: set[str] = set()

        def _add_dir(p: Path) -> None:
            try:
                r = str(p.resolve())
            except Exception:
                r = str(p)
            if r not in seen_paths:
                seen_paths.add(r)
                known_dirs.append(Path(r))

        for wf in watched_folders:
            wp = Path(wf["path"])
            _add_dir(wp)
            try:
                if wp.exists() and wp.is_dir():
                    for child in wp.iterdir():
                        if child.is_dir() and not child.name.startswith("."):
                            _add_dir(child)
            except Exception:
                pass
        for f in files:
            ff = f.get("folderFull")
            if ff:
                _add_dir(Path(ff))

        # 1. Exact folder name or path-suffix match inside indexed directories
        for d in known_dirs:
            if d.name.lower() == low or format_path_short(d).lower().endswith(f"/{low}") or str(d).lower().endswith(f"\\{low}"):
                if d.exists() and d.is_dir():
                    return {"path": str(d), "label": format_path_short(d), "exists": True, "willCreate": False}

        # 2. Substring match on known directories
        for d in known_dirs:
            if low in d.name.lower() and d.exists() and d.is_dir():
                return {"path": str(d), "label": format_path_short(d), "exists": True, "willCreate": False}

        # 3. Special user folder shortcut ONLY when not overridden by workspace folders
        special_map = {
            "desktop": "Desktop",
            "documents": "Documents",
            "downloads": "Downloads",
            "pictures": "Pictures",
            "photos": "Pictures",
            "music": "Music",
            "videos": "Videos",
        }
        if low in special_map:
            sp = get_user_special_dir(special_map[low]).resolve()
            if sp.exists() and sp.is_dir():
                return {"path": str(sp), "label": format_path_short(sp), "exists": True, "willCreate": False}

        # 4. Propose creating a new subfolder cleanly inside preferred parent or primary watched root
        if allow_create_sub:
            safe_sub = re.sub(r'[<>:"/\\|?*]+', "_", clean).strip(" .")
            if not safe_sub:
                return None
            base_parent = preferred_parent
            if base_parent is None and watched_folders:
                base_parent = Path(watched_folders[0]["path"]).expanduser().resolve()
            if base_parent is None and files:
                base_parent = Path(files[0].get("folderFull") or ".").expanduser().resolve()
            if base_parent is None:
                base_parent = get_user_special_dir("Documents").resolve()
            cand = (base_parent / safe_sub).resolve()
            return {"path": str(cand), "label": format_path_short(cand), "exists": cand.exists(), "willCreate": not cand.exists()}
        return None

    @staticmethod
    def _filter_by_folder_hint(candidates: list[dict[str, Any]], folder_hint: str) -> list[dict[str, Any]]:
        if not folder_hint:
            return candidates
        h = folder_hint.lower().strip(" \"'“”`")
        out: list[dict[str, Any]] = []
        for f in candidates:
            ff = str(f.get("folderFull") or f.get("path") or "").lower()
            fs = str(f.get("folder") or "").lower()
            if Path(ff).name.lower() == h or h in fs or f"/{h}" in ff.replace("\\", "/"):
                out.append(f)
        return out

    @staticmethod
    def _filter_by_modality_hint(candidates: list[dict[str, Any]], phrase_low: str) -> list[dict[str, Any]]:
        if re.search(r"\b(picture|pictures|photo|photos|image|images|pic|pics|screenshot|screenshots|img|png|jpg|jpeg)\b", phrase_low):
            filtered = [f for f in candidates if f.get("modality") == "image"]
            if "screenshot" in phrase_low:
                ss = [f for f in filtered if "screenshot" in f.get("name", "").lower() or "screen" in f.get("title", "").lower()]
                return ss if ss else filtered
            return filtered
        if re.search(r"\b(pdf|pdfs)\b", phrase_low):
            return [f for f in candidates if str(f.get("format", "")).upper() == "PDF"]
        if re.search(r"\b(video|videos|movie|movies|clip|clips)\b", phrase_low):
            return [f for f in candidates if f.get("modality") == "video"]
        if re.search(r"\b(audio|song|songs|music|recording|recordings)\b", phrase_low):
            return [f for f in candidates if f.get("modality") == "audio"]
        if re.search(r"\b(document|documents|doc|docs|note|notes|text)\b", phrase_low):
            return [f for f in candidates if f.get("modality") == "document"]
        return candidates

    def _resolve_action_files(
        self,
        target_phrase: str,
        state: dict[str, Any],
        selected_file_id: Optional[str],
        recent_file_ids: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Resolve natural-language file references ('this picture from Downloads', 'all receipts', 'these files', etc.)."""
        all_files: list[dict[str, Any]] = [f for f in state.get("files", []) if not f.get("archived")]
        by_id = {f["id"]: f for f in all_files}
        phrase = (target_phrase or "").strip(" \"'“”?!.,")
        phrase_low = phrase.lower()
        meta: dict[str, Any] = {"sourceFolder": "", "usedContext": False, "needsTargetClarification": False}

        # Extract trailing/embedded 'from <folder>' / 'in <folder>' / 'inside <folder>'
        source_folder_hint = ""
        folder_m = re.search(
            r"\b(?:from|in|inside)\s+(?:the\s+)?(?:folder\s+(?:called|named)\s+)?([A-Za-z0-9 _.\-/~]+?)(?:\s+folder)?$",
            phrase,
            flags=re.IGNORECASE,
        )
        remainder = phrase
        if folder_m:
            cand_folder = folder_m.group(1).strip(" \"'“”.,")
            # Avoid stealing shelf phrases like "in Work shelf" or dates like "in October 2025"
            if not re.search(r"\b(shelf|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{4})\b", cand_folder, flags=re.IGNORECASE):
                source_folder_hint = cand_folder
                meta["sourceFolder"] = source_folder_hint
                remainder = phrase[:folder_m.start()].strip()

        rem_low = remainder.lower()

        # Positional reference into recent citations ("first one", "second file", "last one")
        pos_map = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1}
        pos_m = re.search(r"\b(first|1st|second|2nd|third|3rd|last)\s+(?:one|file|item|picture|photo|doc|document)\b", rem_low)
        if pos_m and recent_file_ids:
            idx = pos_map[pos_m.group(1)]
            valid_recent = [by_id[fid] for fid in recent_file_ids if fid in by_id]
            if valid_recent:
                try:
                    meta["usedContext"] = True
                    return [valid_recent[idx]], meta
                except IndexError:
                    pass

        # Plural deictic ("these", "those", "them", "all of them")
        if re.fullmatch(
            r"(?:all\s+(?:of\s+)?)?(?:these|those|them)(?:\s+(?:files|items|pictures|photos|images|docs|documents|pdfs|receipts))?",
            rem_low,
        ):
            meta["usedContext"] = True
            valid_recent = [by_id[fid] for fid in recent_file_ids if fid in by_id]
            if source_folder_hint:
                valid_recent = self._filter_by_folder_hint(valid_recent, source_folder_hint)
            valid_recent = self._filter_by_modality_hint(valid_recent, rem_low)
            if valid_recent:
                return valid_recent, meta
            if selected_file_id and selected_file_id in by_id:
                return [by_id[selected_file_id]], meta
            meta["needsTargetClarification"] = True
            return [], meta

        # Singular deictic ("this", "this picture", "that file", "it", "the selected file")
        is_singular_deictic = bool(
            re.fullmatch(
                r"(?:the\s+)?(?:selected\s+|current\s+)?(?:this|that|it|one|selected)(?:\s+(?:file|item|picture|photo|image|pic|screenshot|pdf|doc|document|receipt|invoice))?",
                rem_low,
            )
            or re.match(r"^(?:this|that)\s+(?:file|item|picture|photo|image|pic|screenshot|pdf|doc|document|receipt|invoice)\b", rem_low)
        )
        if is_singular_deictic:
            meta["usedContext"] = True
            if selected_file_id and selected_file_id in by_id:
                sel = [by_id[selected_file_id]]
                if source_folder_hint:
                    sel_in_folder = self._filter_by_folder_hint(sel, source_folder_hint)
                    if sel_in_folder:
                        return sel_in_folder, meta
                else:
                    return sel, meta
            # Fall back to single recent match or strict folder+modality filter when clear
            pool = [by_id[fid] for fid in recent_file_ids if fid in by_id]
            if not pool and source_folder_hint:
                pool = all_files
            if source_folder_hint:
                pool = self._filter_by_folder_hint(pool, source_folder_hint)
            pool = self._filter_by_modality_hint(pool, rem_low)
            if pool and source_folder_hint:
                return pool, meta
            if len(pool) == 1:
                return pool, meta
            meta["needsTargetClarification"] = True
            return [], meta

        # Catch-all ("all files", "all the files", "everything", "all pictures", "all pdfs", "all receipts")
        clean_core = re.sub(
            r"^(?:all\s+(?:the\s+)?|every\s+|any\s+|the\s+|my\s+)",
            "",
            rem_low,
        ).strip()
        is_generic_bucket = clean_core in {
            "",
            "file",
            "files",
            "item",
            "items",
            "everything",
            "stuff",
            "things",
            "picture",
            "pictures",
            "photo",
            "photos",
            "image",
            "images",
            "pic",
            "pics",
            "screenshot",
            "screenshots",
            "pdf",
            "pdfs",
            "document",
            "documents",
            "doc",
            "docs",
            "video",
            "videos",
            "audio",
        }
        if is_generic_bucket:
            pool = list(all_files)
            if source_folder_hint:
                pool = self._filter_by_folder_hint(pool, source_folder_hint)
            pool = self._filter_by_modality_hint(pool, rem_low)
            if not source_folder_hint and clean_core in {"", "file", "files", "item", "items", "everything", "stuff", "things"}:
                # Cap un-filtered "everything" slightly high so user still sees full list safely
                return pool[:100], meta
            return pool[:100], meta

        search_term = re.sub(
            r"^(?:all\s+(?:the\s+)?|every\s+|any\s+|the\s+|my\s+)?(?:files?|items?|docs?|documents?|pictures?|photos?|images?|pdfs?)?\s*(?:about|named|called|matching|containing|with|regarding)?\s*",
            "",
            rem_low,
        ).strip() or clean_core or rem_low

        # Exact or substring filename match first (e.g. "meeting_notes" or "receipt_may.pdf")
        exact_hits = [
            f
            for f in all_files
            if f["name"].lower() == search_term
            or Path(f["name"]).stem.lower() == search_term
            or (len(search_term) >= 3 and search_term in f["name"].lower())
        ]
        if source_folder_hint:
            exact_hits = self._filter_by_folder_hint(exact_hits, source_folder_hint)
        if exact_hits:
            return exact_hits[:50], meta

        # Lexical / stem match across metadata & text (e.g. "receipts" -> "receipt")
        term_tokens = [tok for tok in re.findall(r"[a-z0-9]{2,}", search_term) if tok not in {"file", "files", "item", "items", "folder"}]
        if term_tokens:
            stems = []
            for tok in term_tokens:
                stem = tok[:-1] if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss") else tok
                stems.append(stem)
            lex_hits: list[dict[str, Any]] = []
            for f in all_files:
                hay = " ".join(
                    [
                        str(f.get("name", "")),
                        str(f.get("title", "")),
                        str(f.get("specimen", "")),
                        " ".join(f.get("tags") or []),
                        str(f.get("extractedText", ""))[:1200],
                        str(f.get("ocrText", ""))[:1200],
                    ]
                ).lower()
                if all(st_tok in hay for st_tok in stems):
                    lex_hits.append(f)
            if source_folder_hint:
                lex_hits = self._filter_by_folder_hint(lex_hits, source_folder_hint)
            lex_hits = self._filter_by_modality_hint(lex_hits, rem_low)
            if lex_hits:
                return lex_hits[:50], meta

        # Semantic + NL filter search with sensible threshold so unrelated files aren't included
        search_res = self.search_semantic(search_term, modality_filter="all", limit=60, min_similarity=0.28)
        cands = list(search_res.get("results", []))
        if source_folder_hint:
            cands = self._filter_by_folder_hint(cands, source_folder_hint)
        cands = self._filter_by_modality_hint(cands, rem_low)
        return cands[:50], meta

    def _detect_and_build_ai_action(
        self,
        q_clean: str,
        state: dict[str, Any],
        selected_file_id: Optional[str],
        recent_file_ids: list[str],
        name_prefix: str,
        name_comma: str,
        t0: float,
        pending_action: Optional[dict[str, Any]] = None,
        scope_folder: Optional[str] = None,
        scope_recursive: bool = True,
        memory_rows: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Detect natural-language file & folder management requests (including chained multi-step
        commands with per-step targets, pending-plan refinements, learned routing, OCR, folder pinning,
        duplicate cleanup, and file comparisons) and return a safe confirmation plan (`proposedAction`).
        """
        q_stripped = re.sub(
            r"^(?:hey\s+|hi\s+|please\s+|could you\s+|can you\s+|would you\s+|will you\s+|help me\s+)+",
            "",
            q_clean.strip(),
            flags=re.IGNORECASE,
        ).strip()
        q_low = q_stripped.lower().strip(" ?!.")
        mem_list = memory_rows or list(state.get("aiMemory") or [])
        all_files_raw = [f for f in state.get("files", []) if not f.get("archived")]
        all_files = filter_files_by_scope(all_files_raw, scope_folder, recursive=scope_recursive)
        scoped_state = {**state, "files": all_files} if scope_folder else state
        watched = state.get("watchedFolders", [])
        shelves = state.get("shelves", [])
        scope_trace = [f"Scoped to {format_path_short(scope_folder)} ({len(all_files)} files)"] if scope_folder else []

        # Never hijack pure query/search verbs
        if re.match(r"^(find|show|list|search|where|what|when|who|why|how many|how much|summarize|explain|tell me|open)\b", q_low):
            return None

        def _make_item_with_mem(f: dict[str, Any], detail: str = "") -> dict[str, Any]:
            excluded = is_item_excluded_by_memory(mem_list, f)
            effective_detail = f"{detail} · Previously skipped" if (excluded and detail) else ("Previously skipped" if excluded else detail)
            return self._make_action_item(f, effective_detail, selected_by_default=not excluded)

        def _reply(
            answer: str,
            citations: list[dict[str, Any]] | None = None,
            proposed: dict[str, Any] | None = None,
            mode: str = "action-plan",
            choices: list[dict[str, str]] | None = None,
            trace: list[str] | None = None,
        ) -> dict[str, Any]:
            c_list = citations or []
            if proposed is not None and "sourceQuery" not in proposed:
                proposed["sourceQuery"] = q_clean
            return {
                "question": q_clean,
                "answer": answer,
                "citations": c_list,
                "mode": mode,
                "appliedFilters": ([f"folder: {format_path_short(scope_folder)}"] if scope_folder else []),
                "intentMode": "action",
                "totalMatched": len(c_list),
                "proposedAction": proposed,
                "choices": choices if choices is not None else self._build_smart_choices("action", c_list, proposed, q_low),
                "trace": trace if trace is not None else scope_trace,
                "scopeFolder": scope_folder,
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        # 0A. PENDING ACTION REFINEMENT ("only include pdfs", "exclude images", "without draft", "cancel plan")
        if pending_action:
            if re.fullmatch(r"(?:cancel|cancel plan|never ?mind|abort|stop|forget it)", q_low):
                return _reply(
                    f"{name_prefix}okay, cancelled that pending plan — no files were modified.",
                    mode="conversational",
                )
        if pending_action and pending_action.get("items"):
            ref_m = re.match(r"^(?:only|only include|keep only|just)\s+(pdfs?|images?|pictures?|photos?|docs?|documents?|.+)$", q_low)
            excl_m = re.match(r"^(?:exclude|without|skip|remove|drop)\s+(pdfs?|images?|pictures?|photos?|docs?|documents?|.+)$", q_low)
            if ref_m or excl_m:
                items_in = list(pending_action["items"])
                tok = (ref_m or excl_m).group(1).strip().lower()  # type: ignore[union-attr]
                is_excl = excl_m is not None

                def _item_matches_tok(it: dict[str, Any]) -> bool:
                    if tok in {"pdf", "pdfs"}:
                        return str(it.get("format", "")).upper() == "PDF"
                    if tok in {"image", "images", "picture", "pictures", "photo", "photos", "png", "jpg"}:
                        return it.get("modality") == "image"
                    if tok in {"doc", "docs", "document", "documents"}:
                        return it.get("modality") == "document"
                    return tok in f"{it.get('fileName', '')} {it.get('title', '')} {it.get('folder', '')}".lower()

                filtered_items = [
                    it for it in items_in
                    if (not _item_matches_tok(it) if is_excl else _item_matches_tok(it))
                ]
                if not filtered_items:
                    return _reply(
                        f"{name_prefix}that filter would leave 0 items from the pending plan. Tell me another filter or click Cancel on the card above.",
                        mode="conversational",
                        choices=[
                            {"label": "Only PDFs", "prompt": "only include pdfs", "kind": "clarify"},
                            {"label": "Cancel plan", "prompt": "cancel plan", "kind": "clarify"},
                        ],
                    )
                updated_plan = {
                    **pending_action,
                    "actionId": f"act_{uuid.uuid4().hex[:10]}",
                    "items": filtered_items,
                    "summary": f"Refined selection ({len(filtered_items)} item(s)): {pending_action.get('summary', '')}",
                }
                return _reply(
                    f"{name_prefix}updated the plan to **{len(filtered_items)} item(s)** ({'excluded' if is_excl else 'kept only'} {tok}). Review and confirm below.",
                    proposed=updated_plan,
                )

        # 0B. COMPARE / DIFF TWO FILES
        cmp_m = re.match(r"^(?:compare|diff|what(?:'s| is) different between)\s+(.+?)\s+(?:and|vs\.?|with|against)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if cmp_m:
            left_q = cmp_m.group(1).strip(" \"'“”")
            right_q = cmp_m.group(2).strip(" \"'“”")
            left_hits, _ = self._resolve_action_files(left_q, scoped_state, selected_file_id, recent_file_ids)
            right_hits, _ = self._resolve_action_files(right_q, scoped_state, selected_file_id, recent_file_ids)
            f_a = left_hits[0] if left_hits else None
            f_b = next((r for r in right_hits if not f_a or r["id"] != f_a["id"]), right_hits[0] if right_hits else None)
            if f_a and f_b:
                txt_a = clean_extracted_text(str(f_a.get("extractedText") or ""), preserve_newlines=True).splitlines()
                txt_b = clean_extracted_text(str(f_b.get("extractedText") or ""), preserve_newlines=True).splitlines()
                diff_lines = [
                    ln for ln in difflib.unified_diff(txt_a[:120], txt_b[:120], fromfile=f_a["name"], tofile=f_b["name"], lineterm="")
                    if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
                ]
                added = sum(1 for l in diff_lines if l.startswith("+"))
                removed = sum(1 for l in diff_lines if l.startswith("-"))
                preview_diff = "\n".join(f"- `{ln[:100]}`" for ln in diff_lines[:6])
                md = (
                    f"**Comparison:** `{f_a['name']}` ({f_a['size']}) vs `{f_b['name']}` ({f_b['size']})\n"
                    f"- **Changed lines in preview:** +{added} added / -{removed} removed\n"
                    + (preview_diff if preview_diff else "- Text previews are nearly identical; compare full files in Quick Look.")
                )
                return _reply(
                    md,
                    citations=[self._make_citation(f_a, score=95), self._make_citation(f_b, score=94)],
                    mode="workspace-insight",
                    choices=[
                        {"label": "Check duplicates", "prompt": "clean up duplicates", "kind": "action"},
                        {"label": f"Pin {f_a['name'][:20]}", "prompt": f"pin {f_a['name']}", "kind": "action"},
                    ],
                )

        # 0C. CLEAN UP / ARCHIVE DUPLICATES OR BROAD CLEANUP CLARIFICATION
        if re.search(r"\b(clean up|archive|remove|deduplicate)\s+(?:my\s+|all\s+)?(duplicates|lookalikes|duplicate files)\b", q_low):
            pairs = [p for p in state.get("lookalikes", []) if not p.get("resolved")]
            if not pairs:
                return _reply(
                    f"{name_prefix}your files look spotless: no unresolved duplicates found!",
                    mode="conversational",
                    choices=[
                        {"label": "Rename messy files", "prompt": "rename messy files", "kind": "action"},
                        {"label": "What's on my shelves?", "prompt": "what's on my shelves?", "kind": "followup"},
                    ],
                )
            items = []
            pair_ids = []
            by_id = {f["id"]: f for f in state.get("files", [])}
            for p in pairs[:30]:
                copy_meta = p.get("copy") or {}
                keep_meta = p.get("keep") or {}
                f_copy = by_id.get(copy_meta.get("id"))
                if f_copy:
                    items.append(self._make_action_item(f_copy, f"Archive copy · Keeping {keep_meta.get('name', 'original')} (saves {p.get('saved_mb', 0)} MB)"))
                    pair_ids.append(p["id"])
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": "archive_duplicates",
                "title": f"Archive {len(items)} duplicate copy(ies)",
                "summary": "Safely move duplicate copies to GoodShelves_Archive while keeping originals intact (100% undoable).",
                "targetLabel": "GoodShelves_Archive",
                "reversible": True,
                "destructive": False,
                "requiresSelection": len(items) > 0,
                "items": items,
                "payload": {"pairIds": pair_ids},
            }
            return _reply(
                f"{name_prefix}I found **{len(items)} duplicate pair(s)** ready to clean up safely. Confirm below to archive the extra copies.",
                proposed=proposed,
            )

        if re.fullmatch(
            r"(?:clean(?:\s+up)?(?:\s+(?:my\s+|all\s+)?files)?|organize(?:\s+(?:my\s+|all\s+)?files)?|tidy(?:\s+up)?(?:\s+(?:my\s+|all\s+)?files)?|help me (?:clean(?:\s+up)?|organize|tidy(?:\s+up)?)(?:\s+(?:my\s+)?files)?)",
            q_low,
        ):
            return _reply(
                f"{name_prefix}I'd love to help tidy up! What would you like to start with?",
                mode="conversational",
                choices=[
                    {"label": "Archive duplicates", "prompt": "clean up duplicates", "kind": "action"},
                    {"label": "Rename messy filenames", "prompt": "rename messy files", "kind": "action"},
                    {"label": "Review Quiet Tray", "prompt": "list items on shelf Quiet Tray", "kind": "followup"},
                    {"label": "Check space usage", "prompt": "how much space am I using?", "kind": "followup"},
                ],
            )

        # 0D. MULTI-STEP CHAINED COMMANDS (supports both shared-target & independent per-step targets)
        chain_parts = split_compound_commands(q_stripped)
        if len(chain_parts) >= 2:
            step_plans: list[dict[str, Any]] = []
            created_folder_path = ""
            created_folder_name = ""
            carried_files: list[dict[str, Any]] = []
            by_id_all = {f["id"]: f for f in all_files}
            for step_idx, part_txt in enumerate(chain_parts):
                effective_q = part_txt
                if created_folder_name and re.search(
                    r"\b(?:into it|to it|in it|into that folder|to that folder|there)\b",
                    effective_q,
                    flags=re.IGNORECASE,
                ):
                    effective_q = re.sub(
                        r"\b(?:into it|to it|in it|into that folder|to that folder|there)\b",
                        f"to {created_folder_name}",
                        effective_q,
                        flags=re.IGNORECASE,
                    )
                if carried_files and re.search(r"\b(them|those|these|all of them)\b", effective_q, flags=re.IGNORECASE):
                    sub_ids = [f["id"] for f in carried_files]
                else:
                    sub_ids = recent_file_ids
                sub_res = self._detect_and_build_ai_action(
                    effective_q,
                    scoped_state,
                    selected_file_id,
                    sub_ids,
                    name_prefix,
                    name_comma,
                    t0,
                    pending_action=None,
                    scope_folder=scope_folder,
                    scope_recursive=scope_recursive,
                    memory_rows=mem_list,
                )
                sub_act = sub_res.get("proposedAction") if sub_res else None
                if not sub_act or sub_act.get("actionType") == "multi_step":
                    step_plans = []
                    break
                if sub_act["actionType"] == "create_folder":
                    created_folder_path = str((sub_act.get("payload") or {}).get("parentPath") or "")
                    created_folder_name = str((sub_act.get("payload") or {}).get("folderName") or "")
                    if created_folder_path and created_folder_name:
                        full_created = str((Path(created_folder_path) / created_folder_name).resolve())
                        sub_act["_createdTarget"] = full_created
                elif created_folder_name and sub_act["actionType"] in {"move", "copy"}:
                    payload = dict(sub_act.get("payload") or {})
                    if created_folder_path and (
                        str(payload.get("targetFolder", "")).lower().endswith(created_folder_name.lower())
                    ):
                        payload["targetFolder"] = str((Path(created_folder_path) / created_folder_name).resolve())
                        sub_act["payload"] = payload
                step_items_raw = list(sub_act.get("items") or [])
                if step_items_raw:
                    carried_files = [by_id_all[i["fileId"]] for i in step_items_raw if i["fileId"] in by_id_all]
                sub_act["_stepIndex"] = step_idx
                step_plans.append(sub_act)

            if len(step_plans) >= 2:
                # Preserve independent per-step items while avoiding duplicate rows when steps share identical files
                step_id_sets = [
                    tuple(str(it["fileId"]) for it in (sp.get("items") or []))
                    for sp in step_plans
                    if sp.get("items")
                ]
                shared_target = len(set(step_id_sets)) <= 1
                unified_items: list[dict[str, Any]] = []
                if shared_target:
                    unified_items = list(next((sp["items"] for sp in step_plans if sp.get("items")), []))
                else:
                    seen_item_keys: set[tuple[str, str]] = set()
                    for sp in step_plans:
                        s_idx = int(sp.get("_stepIndex", 0))
                        s_title = str(sp.get("title") or sp["actionType"])
                        for it in sp.get("items") or []:
                            fid = str(it["fileId"])
                            key = (fid, str(s_idx))
                            if key in seen_item_keys:
                                continue
                            seen_item_keys.add(key)
                            it_copy = dict(it)
                            it_copy["stepIndex"] = s_idx
                            it_copy["stepTitle"] = s_title
                            if it_copy.get("detail"):
                                it_copy["detail"] = f"[Step {s_idx + 1}] {it_copy['detail']}"
                            else:
                                it_copy["detail"] = f"[Step {s_idx + 1}] {s_title}"
                            unified_items.append(it_copy)

                steps_payload = [
                    {
                        "stepIndex": int(sp.get("_stepIndex", idx)),
                        "actionType": sp["actionType"],
                        "title": sp["title"],
                        "summary": sp.get("summary", ""),
                        "targetLabel": sp.get("targetLabel", ""),
                        "reversible": bool(sp.get("reversible", True)),
                        "destructive": bool(sp.get("destructive", False)),
                        "requiresSelection": bool(sp.get("requiresSelection", False)),
                        "fileIds": [
                            str(it["fileId"])
                            for it in (sp.get("items") or [])
                            if not str(it["fileId"]).startswith("folder::")
                        ],
                        "items": list(sp.get("items") or []),
                        "payload": sp.get("payload") or {},
                    }
                    for idx, sp in enumerate(step_plans)
                ]
                steps_summary = " → ".join(sp["title"] for sp in step_plans)
                is_destr = any(bool(sp.get("destructive")) for sp in step_plans)
                unique_file_count = len({str(it["fileId"]) for it in unified_items})
                proposed = {
                    "actionId": make_plan_id(),
                    "actionType": "multi_step",
                    "title": f"{len(step_plans)}-step workflow ({unique_file_count} item(s))" if unique_file_count else f"{len(step_plans)}-step workflow",
                    "summary": steps_summary,
                    "targetLabel": f"{len(step_plans)} steps",
                    "reversible": all(bool(sp.get("reversible", True)) for sp in step_plans),
                    "destructive": is_destr,
                    "requiresSelection": any(bool(sp.get("requiresSelection")) for sp in step_plans),
                    "steps": steps_payload,
                    "items": unified_items,
                    "payload": {"steps": steps_payload},
                }
                return _reply(
                    f"{name_prefix}I put together a **{len(step_plans)}-step plan**:\n"
                    + "\n".join(f"{idx}. {sp['title']}" for idx, sp in enumerate(step_plans, start=1))
                    + "\n\nReview the checklist below and click **Confirm** to run all steps.",
                    proposed=proposed,
                    trace=[*scope_trace, f"Planned {len(step_plans)} chained actions ({unique_file_count} item(s))"],
                )

        # 0E. REMEMBER EXPLICIT RULE ("remember receipts go in Finance", "remember to tag invoices #tax")
        rem_m = re.match(
            r"^remember(?:\s+that)?\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if rem_m:
            rule_txt = rem_m.group(1).strip(" \"'“”?!.")
            route_m = re.match(r"^(?:my\s+)?(.+?)\s+(?:go|goes|belong|belongs|should go|move)\s+(?:in|into|to)\s+(.+)$", rule_txt, flags=re.IGNORECASE)
            key_pat = rule_txt.lower()[:80]
            val_txt = rule_txt
            kind_mem = "user_rule"
            if route_m:
                key_pat = re.sub(r"^(?:all|the|my)\s+", "", route_m.group(1).strip(), flags=re.IGNORECASE).lower()
                dst_info = self._resolve_folder_candidate(route_m.group(2).strip(), all_files, watched, allow_create_sub=False)
                if dst_info:
                    kind_mem = "route"
                    val_txt = dst_info["path"]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "remember",
                "title": f"Save AI preference: “{rule_txt[:60]}”",
                "summary": "Persist this rule in local AI memory so future searches and organization plans follow it.",
                "targetLabel": "Local AI Memory",
                "reversible": True,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"kind": kind_mem, "keyPattern": key_pat, "value": val_txt, "ruleText": rule_txt},
            }
            return _reply(
                f"{name_prefix}confirm below to save this rule to my local memory: “{rule_txt}”.",
                proposed=proposed,
            )

        # 0F. PIN / UNPIN FOLDER IN SIDEBAR
        pf_m = re.match(
            r"^(pin|unpin)\s+(?:the\s+)?folder\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if pf_m:
            verb = pf_m.group(1).lower()
            folder_hint = pf_m.group(2).strip(" \"'“”?!.")
            f_info = self._resolve_folder_candidate(folder_hint, all_files_raw, watched, allow_create_sub=False)
            if not f_info:
                return _reply(f"{name_prefix}I couldn't find a folder matching “{folder_hint}”.", mode="conversational")
            fp = Path(f_info["path"]).resolve()
            act_type = "pin_folder" if verb == "pin" else "unpin_folder"
            proposed = {
                "actionId": make_plan_id(),
                "actionType": act_type,
                "title": f"{verb.capitalize()} folder “{fp.name}” {'to' if verb == 'pin' else 'from'} sidebar",
                "summary": f"{'Pin' if verb == 'pin' else 'Unpin'} {f_info['label']} in the left Pinned sidebar.",
                "targetLabel": "Sidebar · Pinned",
                "reversible": True,
                "destructive": False,
                "requiresSelection": False,
                "items": [{
                    "fileId": f"folder::{fp}",
                    "fileName": fp.name,
                    "title": f"Folder: {fp.name}",
                    "format": "FOLDER",
                    "modality": "folder",
                    "folder": format_path_short(fp.parent),
                    "path": str(fp),
                    "size": "",
                    "date": "",
                    "shelfName": "Folder",
                    "detail": f"{verb.capitalize()} in sidebar",
                    "selectedByDefault": True,
                }],
                "payload": {"folderPath": str(fp)},
            }
            return _reply(
                f"{name_prefix}confirm below to {verb} folder “{fp.name}” {'to' if verb == 'pin' else 'from'} your sidebar.",
                proposed=proposed,
            )

        # 0G. RUN OCR ON IMAGES / SCANNED PDFS
        ocr_m = re.match(r"^(?:run\s+ocr|ocr|extract\s+text(?:\s+with\s+ocr)?)(?:\s+(?:on|for|from)\s+(.+))?$", q_stripped, flags=re.IGNORECASE)
        if ocr_m:
            target_phrase = (ocr_m.group(1) or "").strip()
            if target_phrase:
                matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            else:
                matched_files = [
                    f for f in all_files
                    if (f.get("modality") == "image" or str(f.get("format", "")).upper() == "PDF")
                    and not str(f.get("ocrText") or "").strip()
                ][:30]
                if not matched_files:
                    matched_files = [f for f in all_files if f.get("modality") == "image"][:20]
            ocr_cands = [f for f in matched_files if f.get("modality") == "image" or str(f.get("format", "")).upper() == "PDF"]
            if not ocr_cands:
                return _reply(f"{name_prefix}I didn't find any images or PDFs matching that to run OCR on.", mode="conversational")
            items = [_make_item_with_mem(f, f"Extract text via RapidOCR ({f.get('folder') or ''})") for f in ocr_cands[:30]]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "run_ocr",
                "title": f"Run RapidOCR on {len(items)} file(s)",
                "summary": "Extract searchable text from images/scans locally using RapidOCR.",
                "targetLabel": "RapidOCR Engine",
                "reversible": False,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            return _reply(
                f"{name_prefix}ready to run local RapidOCR on {len(items)} file(s). Confirm below to extract text.",
                citations=[self._make_citation(f, "OCR scan target", 90) for f in ocr_cands[:12]],
                proposed=proposed,
            )

        # 0H. LEARNED SMART ORGANIZE ("organize my receipts", "sort invoices")
        org_m = re.match(r"^(?:organize|sort|auto-?organize|file)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if org_m and not re.search(r"\b(?:to|into|under|shelf)\b", q_stripped, flags=re.IGNORECASE):
            target_phrase = org_m.group(1).strip()
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            if matched_files:
                learned_dst = infer_learned_route(mem_list, target_phrase, matched_files)
                if learned_dst:
                    dst_label = format_path_short(learned_dst)
                    eligible = [
                        f for f in matched_files
                        if str(Path(f.get("folderFull") or Path(f["path"]).parent).resolve()) != str(Path(learned_dst).resolve())
                    ]
                    if eligible:
                        items = [_make_item_with_mem(f, f"{f.get('folder') or 'Current'} → {dst_label}/{f['name']} (Learned route)") for f in eligible]
                        proposed = {
                            "actionId": make_plan_id(),
                            "actionType": "move",
                            "title": f"Move {len(items)} file(s) to {dst_label} (Learned)",
                            "summary": f"Based on your past organization habits, move these to {dst_label} (undoable).",
                            "targetLabel": dst_label,
                            "reversible": True,
                            "destructive": False,
                            "requiresSelection": True,
                            "items": items,
                            "payload": {"targetFolder": learned_dst, "createTargetFolder": False},
                        }
                        return _reply(
                            f"{name_prefix}from your past organization choices, I recommend moving {len(items)} file(s) to **{dst_label}**. Confirm below to apply.",
                            citations=[self._make_citation(f, f"Learned route → {dst_label}", 92) for f in eligible[:12]],
                            proposed=proposed,
                            trace=[*scope_trace, f"Applied learned routing rule → {dst_label}"],
                        )

        # 1. UNDO LAST ACTION
        if re.fullmatch(r"(?:undo|revert)(?:\s+(?:that|this|it|last action|the last action|my last action))?", q_low):
            log_items = self.get_action_log(limit=1)
            if not log_items:
                return _reply(f"There aren't any recent actions in your undo history yet{name_comma}.", mode="conversational")
            latest = log_items[0]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": "undo",
                "title": f"Undo: {latest.get('label') or latest.get('summary')}",
                "summary": f"Revert “{latest.get('summary')}” back to its previous state.",
                "targetLabel": "Workspace History",
                "reversible": False,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"actionId": latest["id"]},
            }
            return _reply(
                f"{name_prefix}I can undo your last action: “{latest.get('summary')}”. Confirm below to revert it.",
                proposed=proposed,
            )

        # 2. RESTORE TRASHED FILES
        if re.search(r"\b(restore|recover|untrash)\b", q_low):
            trashed = self.list_trash_and_archive().get("trashed", [])
            if not trashed:
                return _reply(f"Your Good Shelves Trash is currently empty{name_comma}.", mode="conversational")
            m_target = re.sub(r"^.*?\b(?:restore|recover|untrash)\b\s*", "", q_stripped, flags=re.IGNORECASE).strip(" ?!.")
            matched = trashed
            if m_target and m_target.lower() not in {"all", "all files", "everything", "trash", "trashed files", "the trash", "all trashed files"}:
                matched = [f for f in trashed if m_target.lower() in f["name"].lower() or m_target.lower() in f["title"].lower()] or trashed
            items = [self._make_action_item(f, f"Restore to {f.get('originalPath') or f.get('folder') or 'original folder'}") for f in matched[:50]]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": "restore_trash",
                "title": f"Restore {len(items)} trashed file(s)",
                "summary": "Return selected files from GoodShelves_Trash back to their original locations.",
                "targetLabel": "Original locations",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            return _reply(
                f"{name_prefix}I found {len(items)} file(s) in Trash ready to restore. Review the list below and click Confirm when ready.",
                citations=[self._make_citation(f, "In Trash", 95) for f in matched[:12]],
                proposed=proposed,
            )

        # 3. CREATE FOLDER
        cf_m = re.match(
            r"^(?:create|make|add)(?:\s+a)?(?:\s+new)?\s+folder(?:\s+(?:called|named))?\s+[\"“']?([^\"”'\n]+?)[\"”']?(?:\s+(?:inside|in|under)\s+(.+))?$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if cf_m:
            folder_name = cf_m.group(1).strip(" \"'“”?!.")
            parent_hint = (cf_m.group(2) or "").strip()
            # Also handle "inside Downloads" accidentally inside group 1
            in_split = re.match(r"^(.*?)\s+(?:inside|in|under)\s+(.+)$", folder_name, flags=re.IGNORECASE)
            if in_split and not parent_hint:
                folder_name = in_split.group(1).strip(" \"'“”")
                parent_hint = in_split.group(2).strip()
            parent_info = (
                self._resolve_folder_candidate(parent_hint, all_files, watched, allow_create_sub=False)
                if parent_hint
                else None
            )
            if parent_info:
                parent_path = parent_info["path"]
            elif watched:
                parent_path = str(Path(watched[0]["path"]).expanduser().resolve())
            else:
                parent_path = str(get_user_special_dir("Documents").resolve())
            full_preview = str((Path(parent_path) / folder_name).resolve())
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": "create_folder",
                "title": f"Create folder “{folder_name}”",
                "summary": f"Create a new folder at {format_path_short(full_preview)} and start watching it.",
                "targetLabel": format_path_short(full_preview),
                "reversible": True,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"parentPath": parent_path, "folderName": folder_name},
            }
            return _reply(
                f"{name_prefix}ready to create folder “{folder_name}” inside {format_path_short(parent_path)}. Confirm below to create it on disk.",
                proposed=proposed,
            )

        # 4. CREATE SHELF / SHELVE UNDER A SHELF
        cs_from_m = re.match(
            r"^(?:create|make|add|start)(?:\s+a)?(?:\s+new)?\s+shelf\s+(?:from|with|containing|of|out of)\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        cs_m = (
            None
            if cs_from_m
            else re.match(
                r"^(?:create|make|add|start)(?:\s+a)?(?:\s+new)?\s+shelf(?:\s+(?:called|named))?\s+[\"“']?(.+?)[\"”']?(?:\s+(?:for|with|containing|of|from)\s+(.+))?$",
                q_stripped,
                flags=re.IGNORECASE,
            )
        )
        shelve_m = re.match(
            r"^(?:put|add|move|assign|shelve|place|file)\s+(.+?)\s+(?:on|onto|to|under|in)(?:\s+(?:a|the))?(?:\s+new)?\s+shelf(?:\s+(?:called|named))?\s+[\"“']?(.+?)[\"”']?$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if not shelve_m:
            shelve_m = re.match(
                r"^shelve\s+(.+?)\s+(?:under|in|to|as|into)\s+[\"“']?(.+?)[\"”']?$",
                q_stripped,
                flags=re.IGNORECASE,
            )
        if cs_from_m or cs_m or shelve_m:
            if shelve_m:
                target_phrase = shelve_m.group(1).strip()
                shelf_name = shelve_m.group(2).strip(" \"'“”?!.")
            elif cs_from_m:
                shelf_name = "Curated Selection"
                target_phrase = cs_from_m.group(1).strip()
            else:
                shelf_name = cs_m.group(1).strip(" \"'“”?!.")  # type: ignore[union-attr]
                target_phrase = (cs_m.group(2) or "").strip()  # type: ignore[union-attr]

            existing_shelf = next((s for s in shelves if s["name"].lower() == shelf_name.lower()), None)
            matched_files: list[dict[str, Any]] = []
            if target_phrase:
                matched_files, meta = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
                if not matched_files and not existing_shelf and cs_m is None:
                    return _reply(
                        f"{name_prefix}I couldn't find any files matching “{target_phrase}” to place on shelf “{shelf_name}”.",
                        mode="conversational",
                    )

            items = [self._make_action_item(f, f"{f.get('shelfName') or 'Workspace'} → {shelf_name}") for f in matched_files]
            if existing_shelf and matched_files:
                proposed = {
                    "actionId": f"act_{uuid.uuid4().hex[:10]}",
                    "actionType": "shelve",
                    "title": f"Shelve {len(items)} item(s) under “{existing_shelf['name']}”",
                    "summary": f"Pin these {len(items)} item(s) onto existing shelf “{existing_shelf['name']}” (keeps disk folders untouched).",
                    "targetLabel": existing_shelf["name"],
                    "reversible": True,
                    "destructive": False,
                    "requiresSelection": True,
                    "items": items,
                    "payload": {"shelfId": existing_shelf["id"], "shelfName": existing_shelf["name"]},
                }
            else:
                proposed = {
                    "actionId": f"act_{uuid.uuid4().hex[:10]}",
                    "actionType": "create_shelf",
                    "title": f"Create shelf “{shelf_name}”" + (f" with {len(items)} item(s)" if items else ""),
                    "summary": f"Add a curated virtual shelf named “{shelf_name}” without moving files on disk.",
                    "targetLabel": shelf_name,
                    "reversible": True,
                    "destructive": False,
                    "requiresSelection": bool(items),
                    "items": items,
                    "payload": {"shelfName": shelf_name},
                }
            noun = f"with {len(items)} item(s)" if items else "(empty curated shelf)"
            return _reply(
                f"{name_prefix}here is the plan to shelve {noun} under “{shelf_name}”. Confirm below to save it.",
                citations=[self._make_citation(f, f"Move to shelf {shelf_name}", 90) for f in matched_files[:12]],
                proposed=proposed,
            )

        # 5. EXPLICIT FOLDER MUTATIONS (move/delete/rename folder)
        mf_m = re.match(
            r"^(?:move|transfer)\s+(?:all\s+(?:the\s+)?)?folders?\s+(.+?)\s+(?:to|into)\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        df_m = re.match(
            r"^(?:delete|remove|trash)\s+(?:the\s+)?folder\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        rf_m = re.match(
            r"^rename\s+(?:the\s+)?folder\s+(.+?)\s+to\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if mf_m or df_m or rf_m:
            if mf_m:
                src_hint = re.sub(r"^(?:called|named)\s+", "", mf_m.group(1).strip(), flags=re.IGNORECASE)
                # Support "move all folders in Incoming to Archive"
                in_parent = re.match(r"^(?:in|from|inside)\s+(?:the\s+)?(.+)$", src_hint, flags=re.IGNORECASE)
                dst_hint = mf_m.group(2).strip()
                dst_info = self._resolve_folder_candidate(dst_hint, all_files, watched, allow_create_sub=True)
                if not dst_info:
                    return _reply(f"{name_prefix}please specify which destination folder you'd like to move those folders into.", mode="conversational")

                folder_targets: list[Path] = []
                if in_parent:
                    parent_info = self._resolve_folder_candidate(in_parent.group(1), all_files, watched, allow_create_sub=False)
                    if parent_info and Path(parent_info["path"]).is_dir():
                        for child in sorted(Path(parent_info["path"]).iterdir()):
                            if child.is_dir() and not child.name.startswith(".") and child.resolve() != Path(dst_info["path"]).resolve():
                                folder_targets.append(child.resolve())
                else:
                    src_info = self._resolve_folder_candidate(src_hint, all_files, watched, allow_create_sub=False)
                    if src_info:
                        folder_targets.append(Path(src_info["path"]).resolve())

                if not folder_targets:
                    return _reply(f"{name_prefix}I couldn't find any matching subfolder to move for “{src_hint}”.", mode="conversational")

                items = []
                for idx, fp in enumerate(folder_targets):
                    count_inside = sum(1 for f in all_files if fp == Path(f["path"]).parent or fp in Path(f["path"]).parents)
                    items.append({
                        "fileId": f"folder::{fp}",
                        "fileName": fp.name,
                        "title": f"Folder: {fp.name} ({count_inside} indexed files)",
                        "format": "FOLDER",
                        "modality": "folder",
                        "folder": format_path_short(fp.parent),
                        "folderFull": str(fp.parent),
                        "path": str(fp),
                        "size": f"{count_inside} files",
                        "date": "",
                        "shelfName": "Folder",
                        "detail": f"Move folder → {dst_info['label']}/{fp.name}",
                        "selectedByDefault": True,
                    })
                proposed = {
                    "actionId": f"act_{uuid.uuid4().hex[:10]}",
                    "actionType": "move_folder",
                    "title": f"Move {len(items)} folder(s) to {dst_info['label']}",
                    "summary": f"Move {len(items)} folder(s) and their contents into {dst_info['label']}.",
                    "targetLabel": dst_info["label"],
                    "reversible": True,
                    "destructive": False,
                    "requiresSelection": False,
                    "items": items,
                    "payload": {
                        "folderPaths": [str(fp) for fp in folder_targets],
                        "targetFolder": dst_info["path"],
                        "createTargetFolder": dst_info["willCreate"],
                    },
                }
                return _reply(
                    f"{name_prefix}I found {len(items)} folder(s) to move to {dst_info['label']}. Confirm below to apply.",
                    proposed=proposed,
                )

            if df_m:
                src_hint = re.sub(r"^(?:called|named)\s+", "", df_m.group(1).strip(" \"'“”?!."), flags=re.IGNORECASE)
                src_info = self._resolve_folder_candidate(src_hint, all_files, watched, allow_create_sub=False)
                if not src_info:
                    return _reply(f"{name_prefix}I couldn't find a folder matching “{src_hint}”.", mode="conversational")
                fp = Path(src_info["path"]).resolve()
                try:
                    _validate_mutable_folder(fp)
                except ValueError as exc:
                    return _reply(f"{name_prefix}I can't delete that folder for safety: {exc}.", mode="conversational")
                count_inside = sum(1 for f in all_files if fp == Path(f["path"]).parent or fp in Path(f["path"]).parents)
                proposed = {
                    "actionId": f"act_{uuid.uuid4().hex[:10]}",
                    "actionType": "delete_folder",
                    "title": f"Move folder “{fp.name}” to Trash",
                    "summary": f"Move folder {src_info['label']} ({count_inside} indexed files) to GoodShelves_Trash (undoable).",
                    "targetLabel": "GoodShelves_Trash",
                    "reversible": True,
                    "destructive": True,
                    "requiresSelection": False,
                    "items": [{
                        "fileId": f"folder::{fp}",
                        "fileName": fp.name,
                        "title": f"Folder: {fp.name}",
                        "format": "FOLDER",
                        "modality": "folder",
                        "folder": format_path_short(fp.parent),
                        "path": str(fp),
                        "size": f"{count_inside} files",
                        "date": "",
                        "shelfName": "Folder",
                        "detail": "Move entire folder to GoodShelves_Trash",
                        "selectedByDefault": True,
                    }],
                    "payload": {"folderPath": str(fp)},
                }
                return _reply(
                    f"{name_prefix}ready to move folder “{fp.name}” ({count_inside} indexed files) to Trash. Confirm below to proceed.",
                    proposed=proposed,
                )

            if rf_m:
                src_hint = rf_m.group(1).strip(" \"'“”")
                new_fname = rf_m.group(2).strip(" \"'“”?!.")
                src_info = self._resolve_folder_candidate(src_hint, all_files, watched, allow_create_sub=False)
                if not src_info:
                    return _reply(f"{name_prefix}I couldn't find a folder matching “{src_hint}”.", mode="conversational")
                fp = Path(src_info["path"]).resolve()
                proposed = {
                    "actionId": f"act_{uuid.uuid4().hex[:10]}",
                    "actionType": "rename_folder",
                    "title": f"Rename folder “{fp.name}” → “{new_fname}”",
                    "summary": f"Rename {src_info['label']} on disk and update all indexed file paths.",
                    "targetLabel": new_fname,
                    "reversible": False,
                    "destructive": False,
                    "requiresSelection": False,
                    "items": [],
                    "payload": {"folderPath": str(fp), "newName": new_fname},
                }
                return _reply(
                    f"{name_prefix}confirm below to rename folder “{fp.name}” to “{new_fname}”.",
                    proposed=proposed,
                )

        # 6. MOVE / COPY FILES
        mv_m = re.match(
            r"^(move|transfer|relocate|copy|duplicate)\s+(.+?)\s+(?:to|into)\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if not mv_m:
            mv_m = re.match(
                r"^(put)\s+(.+?)\s+(?:into|in|to)\s+(?:the\s+)?folder\s+(.+)$",
                q_stripped,
                flags=re.IGNORECASE,
            )
        if mv_m:
            verb = mv_m.group(1).lower()
            is_copy = verb in {"copy", "duplicate"}
            target_phrase = mv_m.group(2).strip()
            dest_phrase = mv_m.group(3).strip(" \"'“”?!.")

            matched_files, meta = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            if meta.get("needsTargetClarification") or not matched_files:
                return _reply(
                    f"{name_prefix}I didn't find matching files for “{target_phrase}”. "
                    "Try naming a folder, file type, or selecting a file first (for example: “move all receipts from Downloads to Finance”).",
                    mode="conversational",
                )

            # Resolve destination relative to source files' parent when creating a brand-new folder
            src_parents = [Path(f["folderFull"]).parent for f in matched_files if f.get("folderFull")]
            preferred_parent = src_parents[0] if src_parents and src_parents[0].exists() else None
            dst_info = self._resolve_folder_candidate(
                dest_phrase,
                all_files,
                watched,
                allow_create_sub=True,
                preferred_parent=preferred_parent,
            )
            if not dst_info:
                return _reply(
                    f"{name_prefix}I found {len(matched_files)} file(s), but couldn't resolve destination “{dest_phrase}”.",
                    citations=[self._make_citation(f, score=88) for f in matched_files[:12]],
                    mode="conversational",
                )

            # Skip files already inside target folder
            dst_resolved = str(Path(dst_info["path"]).resolve())
            eligible = [
                f for f in matched_files
                if str(Path(f.get("folderFull") or Path(f["path"]).parent).resolve()) != dst_resolved or is_copy
            ]
            if not eligible:
                return _reply(
                    f"{name_prefix}all {len(matched_files)} matching file(s) are already in {dst_info['label']}.",
                    citations=[self._make_citation(f, score=85) for f in matched_files[:12]],
                    mode="conversational",
                )

            verb_label = "Copy" if is_copy else "Move"
            action_type = "copy" if is_copy else "move"
            create_note = " (will create new folder)" if dst_info["willCreate"] else ""
            items = [
                self._make_action_item(f, f"{f.get('folder') or 'Current'} → {dst_info['label']}/{f['name']}")
                for f in eligible
            ]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": action_type,
                "title": f"{verb_label} {len(items)} file(s) to {dst_info['label']}",
                "summary": f"{verb_label} {len(items)} file(s) into {dst_info['label']}{create_note}.",
                "targetLabel": dst_info["label"],
                "reversible": not is_copy,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {
                    "targetFolder": dst_info["path"],
                    "createTargetFolder": dst_info["willCreate"],
                },
            }
            return _reply(
                f"{name_prefix}I found {len(items)} file(s) ready to {verb_label.lower()} to {dst_info['label']}{create_note}. "
                "Review the list below and click Confirm to run it.",
                citations=[self._make_citation(f, f"Destination: {dst_info['label']}", 92) for f in eligible[:12]],
                proposed=proposed,
            )

        # Incomplete move/copy prompt ("move all the files in Downloads" without destination)
        incomplete_mv = re.match(r"^(move|transfer|copy|duplicate)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if incomplete_mv and not re.search(r"\b(?:to|into)\b", q_stripped, flags=re.IGNORECASE):
            matched_files, _ = self._resolve_action_files(incomplete_mv.group(2).strip(), scoped_state, selected_file_id, recent_file_ids)
            if matched_files:
                return _reply(
                    f"{name_prefix}I found {len(matched_files)} matching file(s). Where would you like to {incomplete_mv.group(1).lower()} them? "
                    "For example, reply with “move them to Archive”.",
                    citations=[self._make_citation(f, score=85) for f in matched_files[:12]],
                    mode="conversational",
                )

        # 7. DELETE / TRASH FILES
        del_m = re.match(
            r"^(permanently\s+)?(delete|remove|trash|erase|discard|get rid of)\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if not del_m:
            del_m = re.match(
                r"^(delete|remove|trash)\s+(?:the\s+)?(.+?)\s+(permanently|forever)$",
                q_stripped,
                flags=re.IGNORECASE,
            )
        if del_m:
            is_permanent = bool(
                re.search(r"\b(permanently|forever|for good)\b", q_low)
            )
            if len(del_m.groups()) == 3 and del_m.group(3) and del_m.group(3).lower() in {"permanently", "forever"}:
                target_phrase = del_m.group(2).strip()
            else:
                target_phrase = re.sub(r"\b(permanently|forever|for good)\b", "", del_m.group(3).strip(), flags=re.IGNORECASE).strip()

            matched_files, meta = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            if meta.get("needsTargetClarification") or not matched_files:
                sample_pool = self._filter_by_folder_hint(all_files, meta.get("sourceFolder") or "")
                sample_pool = self._filter_by_modality_hint(sample_pool, target_phrase.lower())
                hint_cites = [self._make_citation(f, score=75) for f in sample_pool[:6]]
                return _reply(
                    f"{name_prefix}which file should I delete? Click one on your shelves first to select it, "
                    "or name the file or folder clearly (for example, “delete this picture” while selected, or “delete draft notes from Downloads”).",
                    citations=hint_cites,
                    mode="conversational",
                )

            action_type = "delete_permanent" if is_permanent else "trash"
            dest_note = "Permanently remove from disk (cannot be undone)" if is_permanent else "Move to GoodShelves_Trash (reversible via Undo)"
            items = [
                self._make_action_item(f, f"{'Permanent delete' if is_permanent else 'Move to Trash'} from {f.get('folder') or 'workspace'}")
                for f in matched_files
            ]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": action_type,
                "title": f"{'Permanently delete' if is_permanent else 'Move to Trash'}: {len(items)} file(s)",
                "summary": dest_note,
                "targetLabel": "Permanent Delete" if is_permanent else "GoodShelves_Trash",
                "reversible": not is_permanent,
                "destructive": True,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            return _reply(
                f"{name_prefix}I found {len(items)} matching file(s) to {'permanently delete' if is_permanent else 'move to Trash'}. "
                "Please double-check the list below and click Confirm.",
                citations=[self._make_citation(f, dest_note, 94) for f in matched_files[:12]],
                proposed=proposed,
            )

        # 8. TAG / UNTAG FILES
        untag_m = re.match(
            r"^(?:untag|remove\s+tag|delete\s+tag|clear\s+tag)\s+#?([A-Za-z0-9 _\-,]+?)\s+(?:from|on|off)\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        ) or re.match(
            r"^untag\s+(.+?)\s+#?([A-Za-z0-9 _\-,]+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if untag_m:
            if q_low.startswith("untag ") and not re.search(r"\s+(?:from|on|off)\s+", q_low):
                target_phrase, raw_tag = untag_m.group(1).strip(), untag_m.group(2).strip()
            else:
                raw_tag, target_phrase = untag_m.group(1).strip(), untag_m.group(2).strip()
            clean_tag = raw_tag.strip(" #\"'“”?!.").lower()
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find any files matching “{target_phrase}” to untag #{clean_tag}.", mode="conversational")
            items = [self._make_action_item(f, f"Remove tag #{clean_tag}") for f in matched_files]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": "untag",
                "title": f"Remove #{clean_tag} from {len(items)} file(s)",
                "summary": f"Remove #{clean_tag} across {len(items)} selected file(s) (undoable).",
                "targetLabel": f"-#{clean_tag}",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"tag": clean_tag},
            }
            return _reply(
                f"{name_prefix}ready to remove #{clean_tag} from {len(items)} file(s). Confirm below to apply.",
                citations=[self._make_citation(f, f"Remove tag: #{clean_tag}", 90) for f in matched_files[:12]],
                proposed=proposed,
            )

        tag_m = (
            re.match(
                r"^(?:tag|label)\s+(.+?)\s+(?:with|as)\s+(.+)$",
                q_stripped,
                flags=re.IGNORECASE,
            )
            or re.match(
                r"^(?:add|apply)\s+tags?\s+#?([A-Za-z0-9 _\-,#]+?)\s+(?:to|on)\s+(.+)$",
                q_stripped,
                flags=re.IGNORECASE,
            )
            or re.match(
                r"^(?:tag|label)\s+#([A-Za-z0-9_\-]+)\s+(?:on|for|to)\s+(.+)$",
                q_stripped,
                flags=re.IGNORECASE,
            )
            or re.match(
                r"^(?:tag|label)\s+(.+?)\s+(#[A-Za-z0-9_\-,#\s]+)$",
                q_stripped,
                flags=re.IGNORECASE,
            )
        )
        if tag_m:
            if q_low.startswith(("add tag", "add tags", "apply tag", "apply tags")) or re.match(r"^(?:tag|label)\s+#", q_low):
                raw_tag, target_phrase = tag_m.group(1).strip(), tag_m.group(2).strip()
            else:
                target_phrase, raw_tag = tag_m.group(1).strip(), tag_m.group(2).strip()
            tag_tokens = [p.strip(" #\"'“”?!.").lower() for p in re.split(r"[,;]+", raw_tag) if p.strip(" #\"'“”?!.")]
            clean_tag = ", ".join(tag_tokens)
            tag_label = " ".join(f"#{t}" for t in tag_tokens)
            matched_files, meta = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            if not matched_files and re.fullmatch(r"(?:these|those|them)(?:\s+files?)?", target_phrase.lower()):
                matched_files = sorted(all_files, key=lambda x: float(x.get("mtime") or 0), reverse=True)[:10]
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find any files matching “{target_phrase}” to tag with {tag_label or '#reviewed'}.", mode="conversational")
            items = [self._make_action_item(f, f"Add tag {tag_label}") for f in matched_files]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": "tag",
                "title": f"Tag {len(items)} file(s) with {tag_label}",
                "summary": f"Apply {tag_label} across {len(items)} selected file(s) (undoable).",
                "targetLabel": tag_label,
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"tag": clean_tag},
            }
            return _reply(
                f"{name_prefix}ready to tag {len(items)} file(s) with {tag_label}. Confirm below to apply.",
                citations=[self._make_citation(f, f"Tag: {tag_label}", 90) for f in matched_files[:12]],
                proposed=proposed,
            )

        tag_bare_m = re.match(r"^(?:tag|label)(?:\s+(.+))?$", q_stripped, flags=re.IGNORECASE)
        if tag_bare_m:
            target_phrase = (tag_bare_m.group(1) or "files").strip()
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            if not matched_files:
                matched_files = sorted(all_files, key=lambda x: float(x.get("mtime") or 0), reverse=True)[:10]
            if not matched_files:
                return _reply(f"{name_prefix}there are no indexed files to tag yet.", mode="conversational")
            cites = [self._make_citation(f, f"{f.get('shelfName') or 'Workspace'} · {f['size']}", 88) for f in matched_files[:12]]
            return _reply(
                f"{name_prefix}I found **{len(matched_files)} file(s)** ready to tag. Pick a tag below (or reply with e.g. `tag these files with #important`):",
                citations=cites,
                mode="conversational",
                choices=[
                    {"label": "Tag #reviewed", "prompt": "tag these files with reviewed", "kind": "action"},
                    {"label": "Tag #important", "prompt": "tag these files with important", "kind": "action"},
                    {"label": "Tag #work", "prompt": "tag these files with work", "kind": "action"},
                    {"label": "Tag #finance", "prompt": "tag these files with finance", "kind": "action"},
                ],
            )

        # 9. PIN / UNPIN FILES
        pin_m = re.match(r"^(pin|unpin)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if pin_m:
            act = pin_m.group(1).lower()
            target_phrase = pin_m.group(2).strip()
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find any files matching “{target_phrase}” to {act}.", mode="conversational")
            items = [self._make_action_item(f, "Pin to top of shelf" if act == "pin" else "Unpin") for f in matched_files]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": act,
                "title": f"{act.capitalize()} {len(items)} file(s)",
                "summary": f"{'Pin' if act == 'pin' else 'Unpin'} {len(items)} file(s) on their shelves (undoable).",
                "targetLabel": "Shelf Top" if act == "pin" else "Normal Order",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            return _reply(
                f"{name_prefix}ready to {act} {len(items)} file(s). Confirm below to apply.",
                citations=[self._make_citation(f, act.capitalize(), 90) for f in matched_files[:12]],
                proposed=proposed,
            )

        # 10. RENAME FILES (SINGLE OR MESSY BATCH)
        ren_single_m = re.match(r"^rename\s+(.+?)\s+to\s+[\"“']?([^\"”'\n]+?)[\"”']?$", q_stripped, flags=re.IGNORECASE)
        if ren_single_m:
            target_phrase = ren_single_m.group(1).strip()
            new_name = ren_single_m.group(2).strip(" \"'“”?!.")
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids)
            if not matched_files:
                return _reply(f"{name_prefix}I couldn't find “{target_phrase}” to rename.", mode="conversational")
            f0 = matched_files[0]
            # Preserve original extension if omitted
            if "." not in new_name and Path(f0["name"]).suffix:
                new_name = f"{new_name}{Path(f0['name']).suffix}"
            items = [self._make_action_item(f0, f"{f0['name']} → {new_name}")]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": "rename_single",
                "title": f"Rename “{f0['name']}” → “{new_name}”",
                "summary": f"Rename file on disk cleanly (undoable).",
                "targetLabel": new_name,
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"newName": new_name},
            }
            return _reply(
                f"{name_prefix}ready to rename “{f0['name']}” to “{new_name}”. Confirm below to apply.",
                citations=[self._make_citation(f0, f"→ {new_name}", 95)],
                proposed=proposed,
            )

        if re.match(r"^(?:rename\b|apply\s+(?:all\s+)?(?:smart\s+|suggested\s+)?renames?)", q_low):
            ren_target = re.sub(r"^(?:rename|apply\s+(?:all\s+)?(?:smart\s+|suggested\s+)?renames?)\s*", "", q_stripped, flags=re.IGNORECASE).strip()
            if not ren_target or re.search(r"\b(messy|unnamed|screenshots|suggested|clean|all)\b", ren_target.lower()):
                candidates = [f for f in all_files if f.get("suggestedName") and not f.get("renamed")]
            else:
                matched_files, _ = self._resolve_action_files(ren_target, scoped_state, selected_file_id, recent_file_ids)
                candidates = [f for f in matched_files if f.get("suggestedName")]
            if not candidates:
                return _reply(
                    f"{name_prefix}all matching files already have clean names! To rename a specific file, say e.g. “rename this file to Budget_2026.pdf”.",
                    mode="conversational",
                )
            rename_map = {f["id"]: str(f["suggestedName"]) for f in candidates[:40]}
            items = [self._make_action_item(f, f"{f['name']} → {f['suggestedName']}") for f in candidates[:40]]
            proposed = {
                "actionId": f"act_{uuid.uuid4().hex[:10]}",
                "actionType": "rename_batch",
                "title": f"Rename {len(items)} file(s) using smart suggestions",
                "summary": "Apply descriptive filenames on disk (100% undoable).",
                "targetLabel": f"{len(items)} renames",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"renameMap": rename_map},
            }
            return _reply(
                f"{name_prefix}I prepared clean filenames for {len(items)} file(s). Uncheck any you want to keep as-is, then click Confirm.",
                citations=[self._make_citation(f, f"Suggested: {f['suggestedName']}", 92) for f in candidates[:12]],
                proposed=proposed,
            )

        return None

    def execute_ai_action(self, req: dict[str, Any]) -> dict[str, Any]:
        """Execute a confirmed AI action plan safely with full audit logging."""
        action_type = str(req.get("actionType") or "").strip()
        file_ids = [str(fid) for fid in (req.get("fileIds") or []) if fid and not str(fid).startswith("folder::")]
        target_folder = str(req.get("targetFolder") or "").strip()

        if action_type == "move":
            if not target_folder:
                raise ValueError("targetFolder is required for move")
            res = self.bulk_file_action(file_ids, "move", target_folder=target_folder)
            cnt = int(res.get("count", 0))
            self._learn_from_executed_action("move", req, file_ids)
            return {"ok": True, "count": cnt, "summary": f"Moved {cnt} file(s) to {format_path_short(target_folder)}."}

        if action_type == "copy":
            if not target_folder:
                raise ValueError("targetFolder is required for copy")
            res = self.paste_files(file_ids, target_folder, mode="copy")
            cnt = int(res.get("count", 0))
            self._learn_from_executed_action("copy", req, file_ids)
            return {"ok": True, "count": cnt, "summary": f"Copied {cnt} file(s) to {format_path_short(target_folder)}."}

        if action_type == "trash":
            res = self.bulk_file_action(file_ids, "trash")
            cnt = int(res.get("count", 0))
            return {"ok": True, "count": cnt, "summary": f"Moved {cnt} file(s) to GoodShelves_Trash."}

        if action_type == "delete_permanent":
            deleted = 0
            for fid in file_ids:
                try:
                    self.delete_file_permanently(fid)
                    deleted += 1
                except Exception:
                    continue
            return {"ok": True, "count": deleted, "summary": f"Permanently deleted {deleted} file(s)."}

        if action_type in {"tag", "untag"}:
            tag = str(req.get("tag") or "").strip()
            res = self.bulk_file_action(file_ids, action_type, tag=tag)
            cnt = int(res.get("count", 0))
            self._learn_from_executed_action(action_type, req, file_ids)
            verb = "Tagged" if action_type == "tag" else "Removed tag from"
            tag_disp = ", ".join(f"#{t.strip(' #')}" for t in re.split(r"[,;]+", tag) if t.strip(" #"))
            return {"ok": True, "count": cnt, "summary": f"{verb} {cnt} file(s) ({tag_disp})."}

        if action_type in {"pin", "unpin"}:
            res = self.bulk_file_action(file_ids, action_type)
            cnt = int(res.get("count", 0))
            verb = "Pinned" if action_type == "pin" else "Unpinned"
            return {"ok": True, "count": cnt, "summary": f"{verb} {cnt} file(s)."}

        if action_type == "shelve":
            shelf_id = str(req.get("shelfId") or "").strip()
            shelf_name = str(req.get("shelfName") or shelf_id)
            updated = 0
            for fid in file_ids:
                try:
                    self.set_file_shelf(fid, shelf_id)
                    updated += 1
                except Exception:
                    continue
            return {"ok": True, "count": updated, "summary": f"Shelved {updated} item(s) under “{shelf_name}”."}

        if action_type == "create_shelf":
            shelf_name = str(req.get("shelfName") or "").strip()
            res = self.create_custom_shelf(name=shelf_name, file_ids=file_ids)
            return {
                "ok": True,
                "count": len(file_ids),
                "shelfId": res.get("shelfId"),
                "summary": f"Created shelf “{shelf_name}” with {len(file_ids)} item(s).",
            }

        if action_type == "create_folder":
            parent_path = str(req.get("parentPath") or "").strip()
            folder_name = str(req.get("folderName") or "").strip()
            res = self.create_folder(parent_path, folder_name)
            return {"ok": True, "count": 1, "path": res.get("path"), "summary": f"Created folder “{folder_name}”."}

        if action_type == "rename_single":
            if not file_ids:
                raise ValueError("No file selected to rename")
            new_name = str(req.get("newName") or "").strip()
            res = self.rename_file_custom(file_ids[0], new_name)
            renamed_name = (res.get("file") or {}).get("name") or new_name
            return {"ok": True, "count": 1, "summary": f"Renamed file to “{renamed_name}”."}

        if action_type == "rename_batch":
            rename_map = req.get("renameMap") or {}
            template = str(req.get("template") or "").strip()
            if template:
                res = self.execute_batch_rename_template(file_ids, template)
                cnt = int(res.get("renamedCount", 0))
                return {"ok": True, "count": cnt, "summary": f"Renamed {cnt} file(s)."}
            renamed = 0
            for fid in file_ids:
                target_name = rename_map.get(fid)
                if target_name:
                    try:
                        self.rename_file_custom(fid, str(target_name))
                        renamed += 1
                    except Exception:
                        continue
            return {"ok": True, "count": renamed, "summary": f"Renamed {renamed} file(s)."}

        if action_type == "move_folder":
            folder_paths = [str(p) for p in (req.get("folderPaths") or []) if p]
            if not folder_paths and req.get("folderPath"):
                folder_paths = [str(req["folderPath"])]
            if not target_folder:
                raise ValueError("targetFolder is required to move folders")
            moved = 0
            for fp in folder_paths:
                self.move_folder(fp, target_folder)
                moved += 1
            return {"ok": True, "count": moved, "summary": f"Moved {moved} folder(s) to {format_path_short(target_folder)}."}

        if action_type == "delete_folder":
            fp = str(req.get("folderPath") or "").strip()
            if not fp:
                raise ValueError("folderPath is required")
            res = self.delete_folder(fp)
            return {"ok": True, "count": int(res.get("movedFiles", 0)), "summary": f"Moved folder {Path(fp).name} to GoodShelves_Trash."}

        if action_type == "rename_folder":
            fp = str(req.get("folderPath") or "").strip()
            new_name = str(req.get("newName") or "").strip()
            res = self.rename_folder(fp, new_name)
            return {"ok": True, "count": 1, "summary": f"Renamed folder to “{res.get('name', new_name)}”."}

        if action_type == "restore_trash":
            restored = 0
            for fid in file_ids:
                try:
                    self.restore_trashed_file(fid)
                    restored += 1
                except Exception:
                    continue
            return {"ok": True, "count": restored, "summary": f"Restored {restored} file(s) from Trash."}

        if action_type == "undo":
            aid = req.get("actionId")
            res = self.undo_action(int(aid) if aid is not None else None)
            return {"ok": True, "count": 1, "summary": str(res.get("summary") or "Undid last action.")}

        if action_type == "archive_duplicates":
            pair_ids = [str(pid) for pid in (req.get("pairIds") or []) if pid]
            if file_ids:
                allowed_copy_ids = set(file_ids)
                pair_map = {str(p["id"]): str((p.get("copy") or {}).get("id") or "") for p in self.get_all_state().get("lookalikes", [])}
                pair_ids = [pid for pid in pair_ids if pair_map.get(pid) in allowed_copy_ids]
            archived_cnt = 0
            for pid in pair_ids:
                try:
                    res = self.toggle_archive_lookalike(pid)
                    if res.get("resolved"):
                        archived_cnt += 1
                except Exception:
                    continue
            if archived_cnt > 0:
                self.recluster_all()
            return {"ok": True, "count": archived_cnt, "summary": f"Archived {archived_cnt} duplicate copy(ies) cleanly."}

        if action_type == "pin_folder":
            fp = str(req.get("folderPath") or "").strip()
            res = self.toggle_pin_folder(fp, pinned=True)
            return {"ok": True, "count": 1, "summary": f"Pinned folder “{res['name']}” to sidebar."}

        if action_type == "unpin_folder":
            fp = str(req.get("folderPath") or "").strip()
            res = self.toggle_pin_folder(fp, pinned=False)
            return {"ok": True, "count": 1, "summary": f"Unpinned folder “{res['name']}” from sidebar."}

        if action_type == "run_ocr":
            ocr_done = 0
            for fid in file_ids:
                try:
                    self.execute_file_action(fid, "run_ocr")
                    ocr_done += 1
                except Exception:
                    continue
            return {"ok": True, "count": ocr_done, "summary": f"Extracted OCR text for {ocr_done} file(s)."}

        if action_type == "remember":
            kind_mem = str(req.get("kind") or "user_rule")
            key_pat = str(req.get("keyPattern") or "").strip()
            val_txt = str(req.get("value") or req.get("ruleText") or "").strip()
            with self._lock:
                conn = self._connect()
                try:
                    upsert_ai_memory(conn, kind_mem, key_pat, val_txt, delta_weight=2.0)
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return {"ok": True, "count": 1, "summary": f"Saved rule to local AI memory."}

        if action_type == "multi_step":
            steps = list(req.get("steps") or [])
            if not steps:
                raise ValueError("No steps provided for multi_step workflow")
            summaries: list[str] = []
            last_count = len(file_ids)
            selected_set = set(file_ids)
            last_created_dir: Optional[str] = None
            for st_item in steps:
                st_type = str(st_item.get("actionType") or "")
                st_payload = dict(st_item.get("payload") or {})
                st_explicit_ids = [str(x) for x in (st_item.get("fileIds") or []) if x]
                if st_explicit_ids:
                    step_fids = [fid for fid in st_explicit_ids if fid in selected_set] if selected_set else []
                else:
                    step_fids = list(file_ids)
                if st_item.get("requiresSelection") and not step_fids:
                    continue
                if last_created_dir and st_type in {"move", "copy"} and st_payload.get("targetFolder"):
                    if Path(str(st_payload["targetFolder"])).name.lower() == Path(last_created_dir).name.lower():
                        st_payload["targetFolder"] = last_created_dir
                sub_req = {**st_payload, "actionType": st_type, "fileIds": step_fids}
                sub_res = self.execute_ai_action(sub_req)
                if st_type == "create_folder" and sub_res.get("path"):
                    last_created_dir = str(sub_res["path"])
                summaries.append(str(sub_res.get("summary") or st_type))
                if sub_res.get("count") is not None:
                    last_count = max(last_count, int(sub_res["count"]))
            self._learn_from_executed_action("multi_step", req, file_ids)
            return {"ok": True, "count": last_count, "summary": " · ".join(summaries) if summaries else "Completed workflow."}

        raise ValueError(f"Unsupported AI actionType: {action_type}")

    def _answer_workspace_overview(
        self,
        q_low: str,
        state: dict[str, Any],
        name_prefix: str,
        name_comma: str,
    ) -> Optional[tuple[str, list[dict[str, Any]]]]:
        """Answer high-level questions about shelves, storage, duplicates, messy files, or how Good Shelves works."""
        all_files = [f for f in state.get("files", []) if not f.get("archived")]
        shelves = state.get("shelves", [])
        total_files = len(all_files)

        # Shelf vs folder metaphor / how-to guidance
        if re.search(r"\b(difference between|shelf vs folder|shelves work|what is a shelf|what are shelves|quiet tray|how does undo|shortcuts)\b", q_low):
            if "quiet tray" in q_low:
                quiet_count = sum(1 for f in all_files if not f.get("shelfId") or f.get("shelfId") == "uncategorized")
                return (
                    f"{name_prefix}the Quiet Tray holds {quiet_count} unsorted item(s) gently at the edge of your workspace "
                    "until you give them a shelf or move them cleanly, so nothing gets lost in clutter.",
                    [],
                )
            if "shortcut" in q_low:
                return (
                    f"{name_prefix}handy shortcuts: `/` focuses Search & AI anywhere, `Ctrl/Cmd+K` opens quick search, "
                    "`Ctrl/Cmd+Z` undoes your latest file move/rename/trash, and clicking any item opens Quick Look.",
                    [],
                )
            return (
                f"{name_prefix}in Good Shelves, a **Shelf** is a calm virtual collection organized by meaning "
                "(like a library bookcase across projects), while your real folders on disk stay untouched unless you explicitly move or tidy them. "
                "Just ask me things like “move all receipts from Downloads to Finance” or “shelve these under Tax 2026” and I'll always preview the list for confirmation first!",
                [],
            )

        # Storage / space breakdown
        if re.search(r"\b(how much space|storage|disk space|taking up space|workspace size|breakdown)\b", q_low):
            insights = self.get_workspace_insights()
            mods = ", ".join(f"{m['count']} {m['modality']}(s)" for m in insights["byModality"][:4])
            top_sh = ", ".join(f"{s['shelfName']} ({format_bytes(s['bytes'])})" for s in insights["byShelf"][:3])
            cites = [
                self._make_citation(f, f"Size: {f['size']} · {f['shelfName']}", 92)
                for f in sorted(all_files, key=lambda x: int(x.get("sizeBytes") or 0), reverse=True)[:8]
            ]
            return (
                f"{name_prefix}your shelves hold {insights['totalFiles']} files totaling {insights['totalHuman']}"
                + (f" ({mods})" if mods else "")
                + (f". Heaviest shelves: {top_sh}." if top_sh else "."),
                cites,
            )

        # Duplicates check
        if re.search(r"\b(any duplicates|duplicate files|lookalike|lookalikes|reclaim space)\b", q_low):
            insights = self.get_workspace_insights()
            cnt = insights["duplicatePairsCount"]
            if cnt == 0:
                return (f"{name_prefix}your shelves look clean: zero unresolved duplicate or near-duplicate pairs found.", [])
            return (
                f"{name_prefix}I spotted {cnt} duplicate/near-duplicate pair(s) that could reclaim about {insights['reclaimableMB']} MB. "
                "Open Clean in the left rail to review and archive extras safely.",
                [],
            )

        # Workspace / shelves summary ("what's on my shelves", "summarize my files", "list my shelves")
        if re.search(
            r"\b(what(?:'s| is) on my shelves|my shelves|list (?:all )?shelves|summarize my (?:files|workspace)|overview of my|how are my files)\b",
            q_low,
        ):
            shelf_counts: dict[str, int] = {}
            for f in all_files:
                sid = str(f.get("shelfId") or "")
                shelf_counts[sid] = shelf_counts.get(sid, 0) + 1
            shelf_summary = ", ".join(
                f"{s['name']} ({len(s.get('fileIds') or []) if s.get('isCustom') and s.get('fileIds') else shelf_counts.get(s['id'], 0)})"
                for s in shelves[:6]
            )
            insights = self.get_workspace_insights()
            recent = sorted(all_files, key=lambda x: float(x.get("mtime") or 0), reverse=True)[:6]
            cites = [self._make_citation(f, f"{f.get('shelfName', '')} · {f['size']}", 88) for f in recent]
            return (
                f"{name_prefix}you currently have {total_files} indexed file(s) ({insights['totalHuman']}) arranged across {len(shelves)} shelf(ves)"
                + (f": {shelf_summary}." if shelf_summary else "."),
                cites,
            )

        return None

    def _summarize_file_key_points(self, file_dict: dict[str, Any], max_points: int = 3) -> list[str]:
        """Extract concise, high-signal key points from full SQLite extracted/OCR text & metadata."""
        fid = str(file_dict.get("id") or "")
        raw_text = ""
        raw_ocr = ""
        if fid:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute("SELECT extracted_text, ocr_text FROM files WHERE id = ?", (fid,)).fetchone()
                    if row:
                        raw_text = str(row["extracted_text"] or "")
                        raw_ocr = str(row["ocr_text"] or "")
                finally:
                    conn.close()
        combined = "\n".join(p for p in (raw_ocr, raw_text) if p).strip()
        if not combined:
            combined = "\n".join(
                p for p in (str(file_dict.get("ocrText") or ""), str(file_dict.get("extractedText") or "")) if p
            ).strip()

        points: list[str] = []
        if combined:
            cleaned_block = clean_extracted_text(combined, preserve_newlines=True)
            title_norm = re.sub(r"[^a-z0-9]+", " ", str(file_dict.get("title") or "").lower()).strip()
            raw_chunks = [
                re.sub(r"\s+", " ", seg).strip(" -•*\t")
                for seg in re.split(r"\n+|(?<=[.!?])\s+", cleaned_block)
            ]
            scored: list[tuple[float, int, str]] = []
            seen_norm: set[str] = set()
            for idx, seg in enumerate(raw_chunks):
                if len(seg) < 18:
                    continue
                low = seg.lower()
                if re.match(r"^(arxiv:|https?://|page \d+|\d+\s*$)", low):
                    continue
                seg_norm = re.sub(r"[^a-z0-9]+", " ", low).strip()
                if not seg_norm or seg_norm == title_norm or seg_norm in seen_norm:
                    continue
                seen_norm.add(seg_norm)
                score = 0.0
                if re.search(r"\b(abstract|summary|conclusion|result|finding|propose|introduce|method|total|amount|date|key|overview)\b", low):
                    score += 3.0
                if re.search(r"(\$|₱|€|£|\d+%|\d{4})", seg):
                    score += 1.5
                if 45 <= len(seg) <= 200:
                    score += 1.5
                elif len(seg) > 200:
                    score += 0.5
                if idx <= 4:
                    score += 0.5
                scored.append((score, idx, seg[:220]))
            scored.sort(key=lambda x: (-x[0], x[1]))
            chosen = sorted(scored[:max_points], key=lambda x: x[1])
            points = [c[2] for c in chosen]

        if not points:
            meta = file_dict.get("metadata") or {}
            concepts = ", ".join(meta.get("visualConcepts") or [])
            headings = ", ".join((meta.get("headings") or [])[:3])
            symbols = ", ".join((meta.get("codeSymbols") or [])[:4])
            if headings:
                points.append(f"Sections: {headings}")
            if symbols:
                points.append(f"Defines: {symbols}")
            if concepts:
                points.append(f"Visual concepts: {concepts}")
            if not points:
                loc = f"{file_dict.get('folder') or ''}/{file_dict.get('name') or ''}".strip("/")
                points.append(f"{file_dict.get('shelfName') or 'Workspace'} · {loc}")
        return points[:max_points]

    def ask_your_shelves(
        self,
        question: str,
        max_citations: int = 6,
        selected_file_id: Optional[str] = None,
        recent_file_ids: Optional[list[str]] = None,
        history: Optional[list[dict[str, str]]] = None,
        pending_action: Optional[dict[str, Any]] = None,
        scope_folder: Optional[str] = None,
        scope_recursive: bool = True,
    ) -> dict[str, Any]:
        """
        100% local conversational assistant + safe NL multi-action planner across indexed files.
        Supports folder-scoped Q&A, slash commands, full-text + OCR synthesis, continuous local
        learning (`ai_memory`), and confirmable multi-step plans without mutating disk.
        """
        t0 = time.perf_counter()
        q_clean = (question or "").strip()
        settings = self.get_settings()
        raw_name = str(settings.get("user_name") or "").strip()
        display_name = f"{raw_name[:1].upper()}{raw_name[1:]}" if raw_name else ""
        name_comma = f", {display_name}" if display_name else ""
        name_prefix = f"{display_name}, " if display_name else ""

        if not q_clean:
            return {
                "question": "",
                "answer": f"Hi{name_comma}, ask me to find anything, inspect a folder, or chain file actions.",
                "citations": [],
                "choices": [],
                "trace": [],
                "scopeFolder": scope_folder,
                "mode": "conversational",
                "latencyMs": 0.1,
            }

        state = self.get_all_state()
        memory_rows = list(state.get("aiMemory") or [])
        trace: list[str] = []
        all_active_raw = [f for f in state.get("files", []) if not f.get("archived")]

        # Resolve inline `@folder` or slash `/scope <folder>` override if user wrote one
        effective_scope = scope_folder
        if q_clean.lower().startswith("/scope "):
            sc_hint = q_clean[7:].strip()
            if sc_hint.lower() in {"all", "clear", "reset", "workspace"}:
                effective_scope = None
                return {
                    "question": q_clean,
                    "answer": f"{name_prefix}cleared folder scope — searching across your entire workspace ({len(all_active_raw)} files).",
                    "citations": [],
                    "choices": [],
                    "trace": ["Cleared folder scope"],
                    "scopeFolder": None,
                    "mode": "conversational",
                    "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                }
            f_cand = self._resolve_folder_candidate(sc_hint, all_active_raw, state.get("watchedFolders", []), allow_create_sub=False)
            if f_cand:
                effective_scope = f_cand["path"]
                scoped_cnt = len(filter_files_by_scope(all_active_raw, effective_scope, recursive=scope_recursive))
                return {
                    "question": q_clean,
                    "answer": f"{name_prefix}scoped Ask AI to **{f_cand['label']}** ({scoped_cnt} indexed file(s)). Ask any question or give commands for this folder!",
                    "citations": [],
                    "choices": [],
                    "trace": [f"Scoped to {f_cand['label']} ({scoped_cnt} files)"],
                    "scopeFolder": effective_scope,
                    "mode": "conversational",
                    "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                }

        # Translate slash commands cleanly into natural intents
        q_effective = q_clean
        if q_clean.startswith("/"):
            cmd_tok, _, cmd_rest = q_clean[1:].partition(" ")
            cmd_low = cmd_tok.strip().lower()
            rest_clean = cmd_rest.strip()
            if cmd_low == "memory":
                if not memory_rows:
                    return {
                        "question": q_clean,
                        "answer": f"{name_prefix}I haven't recorded any custom rules or routing patterns yet. Confirm moves/tags or say `remember receipts go in Finance` and I'll learn automatically.",
                        "citations": [],
                        "choices": [],
                        "trace": ["Checked local AI memory (0 entries)"],
                        "scopeFolder": effective_scope,
                        "mode": "workspace-insight",
                        "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                    }
                mem_lines = [
                    f"- **{m['kind']}** · `{m['keyPattern']}` → `{m['value']}` ({m['hits']}x)"
                    for m in memory_rows[:12]
                ]
                return {
                    "question": q_clean,
                    "answer": f"{name_prefix}here is what I've learned locally from your usage:\n" + "\n".join(mem_lines),
                    "citations": [],
                    "choices": [],
                    "trace": [f"Loaded {len(memory_rows)} learned memory rule(s)"],
                    "scopeFolder": effective_scope,
                    "mode": "workspace-insight",
                    "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                }
            slash_map = {
                "summarize": f"summarize {rest_clean or 'these files'}",
                "compare": f"compare {rest_clean}" if rest_clean else "compare recent files",
                "ocr": f"run ocr on {rest_clean}" if rest_clean else "run ocr",
                "duplicates": "clean up duplicates",
                "rename": f"rename {rest_clean}" if rest_clean else "rename messy files",
                "cleanup": "clean up duplicates and rename messy files",
            }
            if cmd_low in slash_map:
                q_effective = slash_map[cmd_low]

        active_files = filter_files_by_scope(all_active_raw, effective_scope, recursive=scope_recursive)
        scoped_state = {**state, "files": active_files} if effective_scope else state
        if effective_scope:
            trace.append(f"Scoped to {format_path_short(effective_scope)} ({len(active_files)} files)")

        q_low = q_effective.lower().strip(" ?!.,")
        total_files = len(active_files)
        recent_ids = list(recent_file_ids or [])

        # Quick local math / date / time answers
        util_ans = self._evaluate_quick_utility(q_clean, name_comma)
        if util_ans:
            return {
                "question": q_clean,
                "answer": util_ans,
                "citations": [],
                "choices": [],
                "mode": "conversational",
                "appliedFilters": [],
                "intentMode": "conversational",
                "totalMatched": 0,
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        # Conversational intents (greetings, small talk, thanks, capabilities)
        is_greeting = bool(
            re.fullmatch(
                r"(hi|hello|hey|yo|sup|hiya|good morning|good afternoon|good evening|kamusta|kumusta|greetings)(\s+there|\s+good shelves|\s+[a-z]{2,18})?",
                q_low,
            )
        )
        is_how_are_you = bool(
            re.search(r"\b(how are you|how's it going|how are things|how do you feel|what's up|whats up|how r u|how are u)\b", q_low)
        )
        is_thanks = bool(
            re.fullmatch(r"(thanks|thank you|thx|ty|appreciated|awesome|nice|cool|great|good job|salamat|perfect|got it)(\s+so much|\s+a lot)?", q_low)
        )
        is_bye = bool(re.fullmatch(r"(bye|goodbye|see you|good night|later|cya)", q_low))
        is_identity = bool(
            re.search(r"\b(who are you|what are you|what is good shelves|what can you do|how do you work|help me)\b", q_low)
        )

        if is_greeting:
            msg = (
                f"Hey{name_comma}! Your library is ready with **{total_files} indexed files** across **{len(state.get('shelves', []))} shelves**. "
                "Ask me to summarize documents, compare files, or run actions with a confirmation list."
            )
            return {"question": q_clean, "answer": msg, "citations": [], "choices": self._build_smart_choices("conversational", [], None, "hi"), "mode": "conversational", "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2)}

        if is_how_are_you:
            msg = (
                f"Doing well{name_comma}! Everything is running quietly and locally on your device. "
                f"{total_files} files are indexed and ready whenever you want to search, summarize, or organize."
            )
            return {"question": q_clean, "answer": msg, "citations": [], "choices": self._build_smart_choices("conversational", [], None, "hi"), "mode": "conversational", "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2)}

        if is_thanks:
            msg = f"You're welcome{name_comma}! Anytime you need to find a file, shelve something, or tidy a folder, just tell me."
            return {"question": q_clean, "answer": msg, "citations": [], "choices": [], "mode": "conversational", "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2)}

        if is_bye:
            msg = f"Talk soon{name_comma}! Everything stays safely saved right here on your computer."
            return {"question": q_clean, "answer": msg, "citations": [], "choices": [], "mode": "conversational", "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2)}

        if is_identity:
            msg = (
                f"Hi{name_comma}! I'm **Good Shelves**, your local AI file manager and workspace assistant:\n"
                "- **Search & Summarize:** Find documents by meaning, read receipts/PDFs/images via OCR, and summarize key takeaways.\n"
                "- **File & Folder Actions:** Move, copy, delete to trash, rename, tag, pin, shelve, compare diffs, or chain multi-step workflows (always showing a confirmation list first).\n"
                "- **100% Local & Private:** Everything runs offline on your machine."
            )
            return {"question": q_clean, "answer": msg, "citations": [], "choices": self._build_smart_choices("conversational", [], None, "help"), "mode": "conversational", "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2)}

        # Check natural-language file & folder action requests (never mutates disk during /ask!)
        action_resp = self._detect_and_build_ai_action(
            q_effective,
            state,
            selected_file_id=selected_file_id,
            recent_file_ids=recent_ids,
            name_prefix=name_prefix,
            name_comma=name_comma,
            t0=t0,
            pending_action=pending_action,
            scope_folder=effective_scope,
            scope_recursive=scope_recursive,
            memory_rows=memory_rows,
        )
        if action_resp is not None:
            return action_resp

        # High-level workspace, storage, duplicate, and shelf-metaphor queries
        overview = self._answer_workspace_overview(q_low, scoped_state, name_prefix, name_comma)
        if overview is not None:
            ov_ans, ov_cites = overview
            return {
                "question": q_clean,
                "answer": ov_ans,
                "citations": ov_cites,
                "choices": self._build_smart_choices("overview", ov_cites, None, q_low),
                "mode": "workspace-insight",
                "appliedFilters": [],
                "intentMode": "overview",
                "totalMatched": len(ov_cites),
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        # Context follow-up on selected, recent, or key-point summary requests
        is_deictic_qa = bool(
            re.search(
                r"\b(this file|this document|this picture|this photo|this image|selected file|what is this|"
                r"summarize (?:it|these|them|those|this|key points|the key points|these files|these docs|these documents)|"
                r"(?:give|show|extract|list)(?:\s+me)?(?:\s+the)?\s+key\s+(?:points|takeaways)|"
                r"tell me more about it|where is it)\b",
                q_low,
            )
            or re.fullmatch(r"(?:key\s+points|key\s+takeaways|summarize)", q_low)
        )
        if is_deictic_qa:
            by_id = {f["id"]: f for f in active_files}
            focus_list: list[dict[str, Any]] = []
            if re.search(r"\b(these|them|those|key points|takeaways)\b", q_low) and recent_ids:
                focus_list = [by_id[fid] for fid in recent_ids[:6] if fid in by_id]
            elif selected_file_id and selected_file_id in by_id and not re.search(r"\b(these|them|those)\b", q_low):
                focus_list = [by_id[selected_file_id]]
            elif recent_ids:
                focus_list = [by_id[fid] for fid in recent_ids[:6] if fid in by_id]
            elif selected_file_id and selected_file_id in by_id:
                focus_list = [by_id[selected_file_id]]
            elif active_files:
                focus_list = sorted(active_files, key=lambda x: float(x.get("mtime") or 0), reverse=True)[:5]

            if focus_list:
                cites = []
                bullets = []
                pts_per_file = 3 if len(focus_list) == 1 else 2
                for ff in focus_list:
                    pts = self._summarize_file_key_points(ff, max_points=pts_per_file)
                    cites.append(self._make_citation(ff, pts[0] if pts else "", 96))
                    if len(focus_list) == 1:
                        bullets.extend(f"- {pt}" for pt in pts)
                    else:
                        joined_pts = " · ".join(pts)
                        bullets.append(f"- **{ff['title']}** (`{ff['format']}`, {ff['size']}): {joined_pts}")
                header = (
                    f"{name_prefix}key points from **{focus_list[0]['title']}**:"
                    if len(focus_list) == 1
                    else f"{name_prefix}key points across **{len(focus_list)} file(s)**:"
                )
                ans = f"{header}\n" + "\n".join(bullets)
                return {
                    "question": q_clean,
                    "answer": ans,
                    "citations": cites,
                    "choices": self._build_smart_choices("summarize", cites, None, q_low),
                    "mode": "intent-extractive",
                    "appliedFilters": [],
                    "intentMode": "summarize",
                    "totalMatched": len(cites),
                    "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                }

        # Find similar / related files ("find files similar to <name>", "find related files")
        sim_m = re.match(
            r"^(?:find|show|get|list)\s+(?:files\s+)?(?:similar|related)\s+(?:to|like)\s+(.+)$",
            q_low,
        ) or re.fullmatch(r"(?:find|show)\s+(?:related|similar)\s+files", q_low)
        if sim_m:
            target_hint = sim_m.group(1).strip() if sim_m.lastindex else "this"
            src_hits, _ = self._resolve_action_files(target_hint, scoped_state, selected_file_id, recent_ids)
            if src_hits:
                src_f = src_hits[0]
                sim_res = self.find_similar_files(src_f["id"], limit=8)
                neighbors = [f for f in sim_res.get("results", []) if f["id"] != src_f["id"]]
                if neighbors:
                    cites = [
                        self._make_citation(nf, f"{nf.get('shelfName') or ''} · match {nf.get('matchScore', 80)}%", int(nf.get("matchScore", 80)))
                        for nf in neighbors[:6]
                    ]
                    titles = ", ".join(f"“{nf['title']}”" for nf in neighbors[:3])
                    return {
                        "question": q_clean,
                        "answer": f"{name_prefix}here are {len(neighbors)} file(s) most similar to **{src_f['title']}**: {titles}.",
                        "citations": cites,
                        "choices": self._build_smart_choices("search", cites, None, q_low),
                        "mode": "intent-extractive",
                        "appliedFilters": [f"similar to: {src_f['name']}"],
                        "intentMode": "search",
                        "totalMatched": len(neighbors),
                        "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                    }
                return {
                    "question": q_clean,
                    "answer": f"{name_prefix}I checked your library, and **{src_f['title']}** doesn't have any close neighbors yet.",
                    "citations": [self._make_citation(src_f, score=90)],
                    "choices": [],
                    "mode": "intent-extractive",
                    "appliedFilters": [],
                    "intentMode": "search",
                    "totalMatched": 0,
                    "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                }

        # Pre-parse natural-language clauses, filters, dates, and list/count intents.
        text_q, op_filters = _extract_search_operators(q_effective)
        nl = _parse_nl_query(text_q)
        intent_mode = nl["intent_mode"]
        applied_filters = list(nl["applied_filters"])
        if effective_scope:
            applied_filters.insert(0, f"folder: {format_path_short(effective_scope)}")
        has_structured_intent = bool(
            nl["has_strict_filters"]
            or op_filters
            or intent_mode in ("list", "count", "summarize", "finance")
        )

        # Check general/non-file prompts when Ollama is enabled or when nothing matches well
        search_limit = 40 if intent_mode in ("list", "count") else max(18, max_citations * 2)
        search_res = self.search_semantic(
            q_effective,
            modality_filter="all",
            limit=search_limit,
            min_similarity=0.14,
            scope_folder=effective_scope,
            scope_recursive=scope_recursive,
        )
        candidates = search_res.get("results", [])
        total_matched = int(search_res.get("totalMatched") or len(candidates))
        trace.append(f"Searched {total_files} file(s) → {total_matched} match(es)")

        is_general_chitchat = bool(
            re.search(r"\b(joke|weather|news|time is it|date today|meaning of life|bored|hungry|dinner|lunch|breakfast|love you|sing|poem|story|motivate|advice|feeling|tired|happy|sad)\b", q_low)
        ) and not has_structured_intent
        top_raw_score = int(candidates[0].get("matchScore", 0)) if candidates else 0
        _, raw_q_terms, _ = _query_concepts(nl["topic_query"] or q_clean)
        has_file_kw_hit = False
        if raw_q_terms and candidates:
            top_hay = " ".join(
                f"{c.get('title', '')} {c.get('name', '')} {c.get('shelfName', '')} {c.get('extractedText', '')[:600]} {c.get('ocrText', '')[:600]}".lower()
                for c in candidates[:5]
            )
            has_file_kw_hit = any(tok in top_hay for tok in raw_q_terms)
        explicit_file_intent = bool(
            has_structured_intent
            or re.search(
                r"\b(file|files|doc|document|pdf|photo|image|pic|receipt|invoice|bill|expense|spent|how much|folder|latest|newest|oldest|largest|biggest|find|locate|where is|show me|shelf|shelves)\b",
                q_low,
            )
        )

        if (nl["has_strict_filters"] or op_filters) and not candidates:
            filter_desc = ", ".join(applied_filters) if applied_filters else "those filters"
            return {
                "question": q_clean,
                "answer": f"{name_prefix}I checked your {total_files} indexed files for {filter_desc} and didn't find any exact matches.",
                "citations": [],
                "mode": "intent-extractive",
                "appliedFilters": applied_filters,
                "intentMode": intent_mode,
                "totalMatched": 0,
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        if is_general_chitchat or not candidates or (not has_file_kw_hit and not explicit_file_intent and top_raw_score < 55):
            if bool(settings.get("ollama_enabled", False)):
                ollama_free = self._try_ollama_general(q_clean, display_name, history=history)
                if ollama_free:
                    return {
                        "question": q_clean,
                        "answer": ollama_free,
                        "citations": [],
                        "mode": "ollama-chat",
                        "appliedFilters": [],
                        "intentMode": "conversational",
                        "totalMatched": 0,
                        "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                    }
            if "joke" in q_low:
                answer_text = f"Here's one for you{name_comma}: Why did the folder go to therapy? Because it had too many unresolved attachments."
            elif "weather" in q_low:
                answer_text = f"I'm 100% offline on your machine{name_comma}, so I can't check live weather, but your local files are calm and ready!"
            elif any(w in q_low for w in ("dinner", "lunch", "breakfast", "hungry", "eat", "food")):
                answer_text = (
                    f"Hard to go wrong with something warm and comforting{name_comma}: maybe garlic rice with grilled chicken or a quick stir-fry! "
                    f"(And if you saved any recipes on your shelves, ask me to pull them up.)"
                )
            elif any(w in q_low for w in ("bored", "tired", "stressed", "sad")):
                answer_text = f"Take a breather{name_comma}. Grab some water, stretch for a minute, and let me handle tidying up any files whenever you're ready."
            elif re.match(r"^(yes|yeah|yep|ok|okay|sure|sounds good|go ahead)\b", q_low):
                answer_text = (
                    f"Got it{name_comma}! Tell me what you'd like to do next, such as searching for a file, "
                    "moving files into a folder, tagging items, or asking a question about your shelves."
                )
            else:
                answer_text = (
                    f"I'm listening{name_comma}! Right now I run 100% offline without web access (and local Ollama chat is turned off in Settings), "
                    f"so for broad open-ended topics I keep things concise, though I'm always ready to help with your {total_files} local files, math/dates, "
                    "or organizing anything on your shelves."
                )
            return {
                "question": q_clean,
                "answer": answer_text,
                "citations": [],
                "mode": "conversational",
                "appliedFilters": [],
                "intentMode": "conversational",
                "totalMatched": 0,
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        is_superlative_newest = bool(re.search(r"\b(latest|newest|most recent|last|recent)\b", q_low)) and intent_mode != "list"
        is_superlative_oldest = bool(re.search(r"\b(oldest|earliest|first)\b", q_low))
        is_superlative_largest = bool(re.search(r"\b(largest|biggest|heaviest|most space)\b", q_low))
        is_finance_intent = intent_mode == "finance" or bool(re.search(r"\b(how much|total|spent|spend|cost|price|paid|receipt|invoice|amount|bill|expense)\b", q_low))
        is_location_intent = bool(re.search(r"\b(where|find|locate|show|looking for|do i have|bring|get|send|move|copy|need|open)\b", q_low))

        q_vec_384 = self.embedder._encode_text_384(nl["topic_query"] or q_clean)
        concepts, raw_terms, _ = _query_concepts(nl["topic_query"] or q_clean)
        q_tokens = {t for c in concepts for t in c["base"]} | set(raw_terms) | {t.lower() for t in nl["include_phrases"]}

        effective_max = max(max_citations, 25) if intent_mode in ("list", "count") else max_citations
        pool_limit = max(len(candidates), 30) if intent_mode in ("list", "count") else 14

        scored_passages: list[tuple[float, dict[str, Any]]] = []
        for f in candidates[:pool_limit]:
            body = " ".join(p for p in [f.get("ocrText", ""), f.get("extractedText", ""), f.get("title", "")] if p).strip()
            best_seg = f"{f.get('shelfName', '')} · {f.get('folder', '')}/{f.get('name', '')}"
            best_score = float(f.get("matchScore", 50)) / 100.0

            if nl["include_phrases"] and body:
                best_seg = _extract_snippet_around_terms(body, nl["include_phrases"], max_len=240)
            elif body:
                raw_parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+| · ", body) if len(s.strip()) >= 12]
                if not raw_parts:
                    raw_parts = [body[:260]]
                windows: list[str] = []
                for idx in range(len(raw_parts)):
                    seg = raw_parts[idx]
                    if idx + 1 < len(raw_parts) and len(seg) < 110:
                        seg = f"{seg} {raw_parts[idx + 1]}"
                    windows.append(seg[:280])
                    if len(windows) >= 12:
                        break

                best_seg = windows[0]
                best_score = -1.0
                for w in windows:
                    w_vec = self.embedder._encode_text_384(w)
                    sem = float(np.clip(np.dot(q_vec_384, w_vec), 0.0, 1.0))
                    w_toks = _field_tokens(w)
                    overlap = (len(q_tokens & w_toks) / float(max(1, len(q_tokens)))) if q_tokens else 0.0
                    base_match = float(f.get("matchScore", 50)) / 100.0
                    score = 0.50 * sem + 0.32 * overlap + 0.18 * base_match
                    if score > best_score:
                        best_score = score
                        best_seg = w

            scored_passages.append((
                best_score,
                self._make_citation(
                    f,
                    snippet=best_seg,
                    score=min(99, max(int(f.get("matchScore", 1)), int(round(best_score * 100)))),
                ),
            ))

        # Preserve strict date order for 'list' queries, otherwise rank by relevance or superlative.
        if is_superlative_largest:
            scored_passages.sort(key=lambda x: (-x[1]["sizeBytes"], -x[0]))
        elif is_superlative_newest:
            scored_passages.sort(key=lambda x: (-x[1]["mtime"], -x[0]))
        elif is_superlative_oldest:
            scored_passages.sort(key=lambda x: (x[1]["mtime"], -x[0]))
        elif intent_mode != "list":
            scored_passages.sort(key=lambda x: x[0], reverse=True)

        citations = [item for _, item in scored_passages[:effective_max]]

        top = citations[0] if citations else None
        answer = "I didn't find any matching files yet."
        if top:
            filter_note = f" ({', '.join(applied_filters)})" if applied_filters else ""
            if intent_mode == "count":
                shelf_counts: dict[str, int] = {}
                for c in citations:
                    sh = c.get("shelfName") or "Workspace"
                    shelf_counts[sh] = shelf_counts.get(sh, 0) + 1
                sh_summary = ", ".join(f"{cnt} in {sh}" for sh, cnt in list(shelf_counts.items())[:4])
                noun = "file" if total_matched == 1 else "files"
                answer = f"{name_prefix}you have {total_matched} matching {noun}{filter_note}" + (f": {sh_summary}." if sh_summary else ".")
            elif intent_mode == "list":
                titles_preview = ", ".join(f"“{c['title']}”" for c in citations[:4])
                more_note = f", plus {len(citations) - 4} more" if len(citations) > 4 else ""
                noun = "item" if total_matched == 1 else "items"
                answer = f"{name_prefix}I found {total_matched} {noun}{filter_note}: {titles_preview}{more_note}."
            elif is_finance_intent:
                currency_totals: dict[str, float] = {}
                matched_receipts = 0
                for c in citations:
                    hay = f"{c['title']} {c['snippet']}"
                    amts = re.findall(r"(PHP|USD|EUR|GBP|JPY|₱|\$|€|£|¥)\s?([\d,]+(?:\.\d+)?)", hay, re.IGNORECASE)
                    if amts:
                        matched_receipts += 1
                        cur_sym, num_str = amts[-1]
                        sym_norm = cur_sym.upper()
                        if sym_norm == "PHP":
                            sym_norm = "₱"
                        try:
                            currency_totals[sym_norm] = currency_totals.get(sym_norm, 0.0) + float(num_str.replace(",", ""))
                        except ValueError:
                            pass
                if currency_totals:
                    sums_fmt = ", ".join(f"{sym}{val:,.2f}" for sym, val in currency_totals.items())
                    answer = (
                        f"{name_prefix}across {matched_receipts} matching financial record(s), "
                        f"the total is {sums_fmt}. Top match: “{top['title']}”."
                    )
                else:
                    answer = (
                        f"{name_prefix}I found “{top['title']}” ({top['shelfName']}): {top['snippet']}"
                    )
            elif is_superlative_largest:
                answer = (
                    f"{name_prefix}the largest match is “{top['title']}” ({top['size']})."
                )
            elif is_superlative_newest or is_superlative_oldest:
                adj = "oldest" if is_superlative_oldest else "most recent"
                answer = (
                    f"{name_prefix}your {adj} match is “{top['title']}” ({top['date']})."
                )
            elif intent_mode == "summarize" and len(citations) >= 1:
                by_id_sum = {f["id"]: f for f in active_files}
                top_f = by_id_sum.get(citations[0]["fileId"])
                if len(citations) == 1 and top_f:
                    pts = self._summarize_file_key_points(top_f, max_points=3)
                    answer = f"{name_prefix}key points from **{top_f['title']}**:\n" + "\n".join(f"- {pt}" for pt in pts)
                else:
                    lines_out = []
                    for c in citations[:4]:
                        ff = by_id_sum.get(c["fileId"])
                        pts = self._summarize_file_key_points(ff, max_points=2) if ff else [c["snippet"][:200]]
                        lines_out.append(f"- **{c['title']}** ({c['shelfName']} · {c['size']}): {' · '.join(pts)}")
                    answer = f"{name_prefix}here are the key points across your shelves:\n" + "\n".join(lines_out)
            elif is_location_intent or not top["snippet"].strip():
                loc = f"{top['folder']}/{top['fileName']}" if top["folder"] else top["fileName"]
                sub_part = f" · {top['subShelf']}" if top.get("subShelf") else ""
                answer = (
                    f"{name_prefix}“{top['title']}” is stored in {loc} "
                    f"({top['shelfName']}{sub_part} · {top['size']})."
                )
            else:
                answer = (
                    f"{name_prefix}from **{top['title']}**: “{top['snippet']}”"
                )
                if len(citations) > 1 and citations[1].get("snippet"):
                    answer += f"\n\nRelated match · **{citations[1]['title']}**: {citations[1]['snippet'][:160]}"

        mode = "intent-extractive"
        if citations:
            deep_ctx = build_deep_file_context(self, citations, max_files=6, chars_per_file=1600)
            trace.append(f"Read {len(deep_ctx)} file(s) (extracted + OCR text)")
            if bool(settings.get("ollama_enabled", False)):
                ollama_ans = try_ollama_structured_synthesis(
                    self,
                    q_effective,
                    deep_ctx,
                    memory_rows,
                    scope_folder=effective_scope,
                    history=history,
                )
                if ollama_ans:
                    answer = ollama_ans
                    mode = "ollama-local"
                    trace.append("Synthesized grounded answer via local Ollama")

        return {
            "question": q_clean,
            "answer": answer,
            "citations": citations,
            "choices": self._build_smart_choices(intent_mode, citations, None, q_low),
            "trace": trace,
            "scopeFolder": effective_scope,
            "mode": mode,
            "appliedFilters": applied_filters,
            "intentMode": intent_mode,
            "totalMatched": total_matched,
            "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    def _try_ollama_general(
        self,
        question: str,
        user_name: str = "",
        history: Optional[list[dict[str, str]]] = None,
    ) -> Optional[str]:
        """Optionally answer general conversational questions via local Ollama with recent conversation context."""
        name_note = f" The user's name is {user_name}." if user_name else ""
        hist_lines: list[str] = []
        for turn in (history or [])[-4:]:
            role_label = "User" if turn.get("role") == "user" else "Assistant"
            txt = str(turn.get("text") or "").strip()[:240]
            if txt:
                hist_lines.append(f"{role_label}: {txt}")
        hist_block = ("\nRecent conversation:\n" + "\n".join(hist_lines) + "\n") if hist_lines else "\n"
        prompt = (
            f"You are Good Shelves, a warm, helpful, concise local desktop file manager and AI assistant.{name_note} "
            "Reply naturally in 1-3 short sentences."
            f"{hist_block}"
            f"User: {question}"
        )
        payload = json.dumps({
            "model": str(self.get_settings().get("ollama_model") or "llama3.2:1b"),
            "prompt": prompt,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                txt = str(data.get("response") or "").strip()
                return txt or None
        except Exception:
            return None

    def _try_ollama_answer(
        self,
        question: str,
        citations: list[dict[str, Any]],
        history: Optional[list[dict[str, str]]] = None,
    ) -> Optional[str]:
        """Optionally synthesize a Markdown answer via local Ollama with conversation context."""
        context_lines = [
            f"- {c.get('title') or c.get('fileName')} ({c.get('folder')}/{c.get('fileName')}): {c.get('snippet', '')[:260]}"
            for c in citations[:5]
        ]
        hist_lines = []
        for turn in (history or [])[-4:]:
            role_label = "User" if turn.get("role") == "user" else "Assistant"
            txt = str(turn.get("text") or "").strip()[:220]
            if txt:
                hist_lines.append(f"{role_label}: {txt}")
        hist_block = ("\nConversation:\n" + "\n".join(hist_lines) + "\n") if hist_lines else ""
        prompt = (
            "You are Good Shelves, a crisp local AI file manager. Answer clearly in Markdown (use bold and concise bullets when helpful) using only these local file excerpts and cite filenames.\n"
            f"{hist_block}\nQuestion: {question}\n\nFiles:\n" + "\n".join(context_lines)
        )
        payload = json.dumps({
            "model": str(self.get_settings().get("ollama_model") or "llama3.2:1b"),
            "prompt": prompt,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                txt = str(data.get("response") or "").strip()
                return txt or None
        except Exception:
            return None

    def browse_folder(self, folder_path: Optional[str] = None) -> dict[str, Any]:
        """Browse a real filesystem directory with breadcrumbs, subfolders, and indexed metadata."""
        if folder_path:
            target = Path(os.path.expanduser(folder_path)).resolve()
        else:
            watched = self.get_watched_folders()
            active = next((w for w in watched if w["active"]), None)
            target = Path(active["path"]).resolve() if active else get_user_special_dir("Documents")
        if not target.exists() or not target.is_dir():
            raise ValueError(f"Folder does not exist: {target}")

        watched_set = {str(Path(w["path"]).resolve()) for w in self.get_watched_folders() if w["active"]}
        all_supported = IMAGE_EXTS | DOC_EXTS | CODE_EXTS | VIDEO_EXTS | AUDIO_EXTS | SPREADSHEET_EXTS | ARCHIVE_EXTS

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM files WHERE folder = ? AND archived = 0", (str(target),)).fetchall()
                indexed_by_path = {str(Path(r["path"]).resolve()): self._row_to_file_dict(r) for r in rows}
            finally:
                conn.close()

        subfolders: list[dict[str, Any]] = []
        files_out: list[dict[str, Any]] = []
        raw_entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(str(target)) as it:
                for entry in it:
                    if not entry.name.startswith("."):
                        raw_entries.append(entry)
        except PermissionError as exc:
            raise ValueError(f"Permission denied: {exc}")

        def _entry_sort_key(e: os.DirEntry[str]) -> tuple[bool, str]:
            try:
                is_d = e.is_dir()
            except OSError:
                is_d = False
            return (not is_d, e.name.lower())

        raw_entries.sort(key=_entry_sort_key)

        for entry in raw_entries:
            try:
                if entry.is_dir():
                    if entry.name in {"GoodShelves_Archive", "GoodShelves_Trash", "node_modules", "__pycache__"}:
                        continue
                    try:
                        with os.scandir(entry.path) as sub_it:
                            cnt = sum(1 for _ in itertools.islice(sub_it, 500))
                    except Exception:
                        cnt = 0
                    res_p = str(Path(entry.path).resolve())
                    subfolders.append({
                        "name": entry.name,
                        "path": res_p,
                        "itemCount": cnt,
                        "watched": res_p in watched_set,
                    })
                elif entry.is_file():
                    entry_p = Path(entry.path)
                    res_p = str(entry_p.resolve())
                    if res_p in indexed_by_path:
                        files_out.append(indexed_by_path[res_p])
                    else:
                        st = entry.stat()
                        ext = entry_p.suffix.lstrip(".").lower() or "file"
                        dt = datetime.fromtimestamp(st.st_mtime)
                        files_out.append({
                            "id": f"unindexed_{_stable_short_hash(res_p, 10)}",
                            "path": res_p,
                            "name": entry.name,
                            "originalName": entry.name,
                            "suggestedName": None,
                            "renameReason": None,
                            "renamed": False,
                            "title": entry_p.stem,
                            "format": ext,
                            "modality": "document",
                            "folder": format_path_short(target),
                            "folderFull": str(target),
                            "size": format_bytes(st.st_size),
                            "sizeBytes": st.st_size,
                            "date": dt.strftime("%b %d, %Y").replace(" 0", " "),
                            "createdDate": dt.strftime("%Y-%m-%d"),
                            "mtime": st.st_mtime,
                            "sha256": "",
                            "shelfId": "unindexed",
                            "shelfName": "not indexed yet",
                            "clusterId": -1,
                            "matchScore": 0,
                            "cosine": "0.000",
                            "vectorNorm": "1.0000",
                            "whyMatched": "Not indexed yet",
                            "visualShare": 0,
                            "textShare": 100,
                            "extractedText": "",
                            "ocrText": "",
                            "ocrConfidence": 0.0,
                            "specimen": "text",
                            "width": 0,
                            "height": 0,
                            "pageCount": 1,
                            "mapX": 50.0,
                            "mapY": 50.0,
                            "unindexed": True,
                            "supported": entry_p.suffix.lower() in all_supported,
                        })
            except OSError:
                continue

        crumbs: list[dict[str, str]] = []
        cur = target
        parts_chain: list[Path] = [cur]
        while cur.parent != cur:
            cur = cur.parent
            parts_chain.append(cur)
        for p in reversed(parts_chain):
            crumbs.append({"name": p.name or str(p), "path": str(p)})

        parent_p = str(target.parent) if target.parent != target else None
        return {
            "path": str(target),
            "displayPath": format_path_short(target),
            "parentPath": parent_p,
            "breadcrumbs": crumbs,
            "folders": subfolders,
            "files": files_out,
        }

    def rename_folder(self, folder_path: str, new_name: str) -> dict[str, Any]:
        src = Path(folder_path).expanduser().resolve()
        if not src.exists() or not src.is_dir():
            raise ValueError("Folder does not exist")
        _validate_mutable_folder(src)
        clean = re.sub(r'[<>:"/\\|?*]+', "_", (new_name or "").strip()).strip(" .")
        if not clean:
            raise ValueError("Folder name cannot be empty")
        dst = src.parent / clean
        if dst.exists():
            raise ValueError(f"Folder already exists: {clean}")
        src.rename(dst)
        src_prefix = str(src)
        dst_prefix = str(dst)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT id, path FROM files").fetchall()
                for r in rows:
                    p = Path(r["path"])
                    if p == src or src in p.parents:
                        new_p = Path(dst_prefix + str(p)[len(src_prefix):])
                        conn.execute(
                            "UPDATE files SET path = ?, folder = ?, folder_short = ? WHERE id = ?",
                            (str(new_p), str(new_p.parent), format_path_short(new_p.parent), r["id"]),
                        )
                conn.execute(
                    "UPDATE watched_folders SET path = ?, display_path = ? WHERE path = ?",
                    (dst_prefix, format_path_short(dst), src_prefix),
                )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"ok": True, "path": dst_prefix, "name": clean}

    def move_folder(self, folder_path: str, target_parent_folder: str) -> dict[str, Any]:
        """Move a directory cleanly into another directory and update indexed paths."""
        src = Path(folder_path).expanduser().resolve()
        if not src.exists() or not src.is_dir():
            raise ValueError("Source folder does not exist")
        _validate_mutable_folder(src)
        target_parent = Path(target_parent_folder).expanduser().resolve()
        target_parent.mkdir(parents=True, exist_ok=True)
        if target_parent == src or src in target_parent.parents:
            raise ValueError("Cannot move a folder into itself")
        dst = target_parent / src.name
        if dst.exists():
            dst = _unique_dest_path(target_parent, src.name)
        shutil.move(str(src), str(dst))
        src_prefix = str(src)
        dst_prefix = str(dst)
        moves_log: list[dict[str, Any]] = []
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT id, path, renamed FROM files").fetchall()
                for r in rows:
                    p = Path(r["path"])
                    if p == src or src in p.parents:
                        try:
                            rel = p.relative_to(src)
                            new_p = dst / rel
                        except ValueError:
                            new_p = Path(dst_prefix + str(p)[len(src_prefix):])
                        conn.execute(
                            "UPDATE files SET path = ?, folder = ?, folder_short = ? WHERE id = ?",
                            (str(new_p), str(new_p.parent), format_path_short(new_p.parent), r["id"]),
                        )
                        moves_log.append({"fileId": str(r["id"]), "fromPath": str(p), "toPath": str(new_p), "renamedAfter": int(r["renamed"] or 0)})
                conn.execute(
                    "UPDATE watched_folders SET path = ?, display_path = ? WHERE path = ?",
                    (dst_prefix, format_path_short(dst), src_prefix),
                )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        if moves_log:
            self._record_action(
                "bulk_move",
                f"Moved folder {src.name} to {format_path_short(target_parent)}",
                {"label": f"Move folder {src.name}", "moves": moves_log},
            )
        return {"ok": True, "path": dst_prefix, "name": dst.name, "movedFiles": len(moves_log)}

    def delete_folder(self, folder_path: str) -> dict[str, Any]:
        src = Path(folder_path).expanduser().resolve()
        if not src.exists() or not src.is_dir():
            raise ValueError("Folder does not exist")
        _validate_mutable_folder(src)
        trash_dir = (get_user_special_dir("Documents") / "GoodShelves_Trash").resolve()
        trash_dir.mkdir(parents=True, exist_ok=True)
        dst = _unique_dest_path(trash_dir, src.name)
        shutil.move(str(src), str(dst))
        items_log: list[dict[str, Any]] = []
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT rowid, id, path FROM files WHERE archived = 0").fetchall()
                for r in rows:
                    p = Path(r["path"])
                    if p == src or src in p.parents:
                        try:
                            rel = p.relative_to(src)
                        except ValueError:
                            rel = p.resolve().relative_to(src)
                        new_p = dst / rel
                        conn.execute(
                            "UPDATE files SET path = ?, folder = ?, folder_short = ?, archived = 1, original_path = ? WHERE rowid = ?",
                            (str(new_p), str(new_p.parent), format_path_short(new_p.parent), str(p), r["rowid"]),
                        )
                        items_log.append({
                            "fileId": str(r["id"]),
                            "fromPath": str(p),
                            "toPath": str(new_p),
                        })
                conn.execute("DELETE FROM watched_folders WHERE path = ?", (str(src),))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        self._record_action(
            "bulk_trash",
            f"Moved folder {src.name} to Trash",
            {
                "label": f"Trash folder {src.name}",
                "items": items_log,
                "folderMove": {"fromPath": str(src), "toPath": str(dst)},
            },
        )
        return {"ok": True, "trashedPath": str(dst), "movedFiles": len(items_log)}

    def paste_files(self, file_ids: list[str], target_folder: str, mode: str = "copy") -> dict[str, Any]:
        if mode == "move":
            return self.bulk_file_action(file_ids, "move", target_folder=target_folder)
        copied = 0
        for fid in file_ids:
            try:
                self.copy_file(fid, target_folder)
                copied += 1
            except Exception:
                continue
        return {"ok": True, "count": copied}

    def create_custom_shelf(
        self,
        name: str,
        desc: str = "",
        query: str = "",
        modality: str = "all",
        tag: str = "",
        file_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Shelf name cannot be empty")
        sid = f"custom_{_stable_short_hash(f'{clean}:{query}:{time.time_ns()}', 10)}"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO custom_shelves (shelf_id, name, desc, query, modality, tag, file_ids_json, pinned, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (sid, clean, desc.strip(), query.strip(), modality, tag.strip(), json.dumps(file_ids or []), time.time()),
                )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"ok": True, "shelfId": sid}

    def rename_shelf(self, shelf_id: str, new_name: str) -> dict[str, Any]:
        clean = (new_name or "").strip()
        if not clean:
            raise ValueError("Shelf name cannot be empty")
        with self._lock:
            conn = self._connect()
            try:
                if shelf_id.startswith("custom_"):
                    conn.execute("UPDATE custom_shelves SET name = ? WHERE shelf_id = ?", (clean, shelf_id))
                else:
                    conn.execute(
                        "INSERT INTO shelf_renames (shelf_id, custom_name) VALUES (?, ?) ON CONFLICT(shelf_id) DO UPDATE SET custom_name = excluded.custom_name",
                        (shelf_id, clean),
                    )
                    conn.execute("UPDATE shelves SET name = ? WHERE shelf_id = ?", (clean, shelf_id))
                    conn.execute("UPDATE files SET shelf_name = ? WHERE shelf_id = ?", (clean, shelf_id))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"ok": True, "shelfId": shelf_id, "name": clean}

    def delete_custom_shelf(self, shelf_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM custom_shelves WHERE shelf_id = ?", (shelf_id,))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"ok": True, "shelfId": shelf_id}

    def list_trash_and_archive(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                trashed_rows = conn.execute(
                    "SELECT * FROM files WHERE archived = 1 AND folder LIKE '%GoodShelves_Trash%' ORDER BY mtime DESC"
                ).fetchall()
                arch_rows = conn.execute("SELECT * FROM archive_history ORDER BY timestamp DESC").fetchall()
                trashed = [self._row_to_file_dict(r) for r in trashed_rows]
                archived = [
                    {
                        "pairId": r["pair_id"],
                        "fileId": r["file_id"],
                        "originalPath": r["original_path"],
                        "archivedPath": r["archived_path"],
                        "name": Path(r["archived_path"]).name,
                        "timestamp": r["timestamp"],
                    }
                    for r in arch_rows
                ]
                return {"trashed": trashed, "archived": archived}
            finally:
                conn.close()

    def restore_trashed_file(self, file_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM files WHERE id = ? AND archived = 1", (file_id,)).fetchone()
                if not row:
                    raise ValueError("Trashed file not found")
                cur_p = Path(row["path"])
                orig_p = Path(row["original_path"]) if "original_path" in row.keys() and row["original_path"] else None
                if orig_p is None:
                    logs = conn.execute("SELECT detail_json FROM action_log WHERE action_type IN ('trash_file', 'bulk_trash') ORDER BY id DESC LIMIT 200").fetchall()
                    for lg in logs:
                        try:
                            det = json.loads(lg["detail_json"])
                            for it in det.get("items", []):
                                if it.get("fileId") == file_id and it.get("fromPath"):
                                    orig_p = Path(it["fromPath"])
                                    break
                        except Exception:
                            continue
                        if orig_p:
                            break
                if orig_p is None:
                    orig_p = get_user_special_dir("Documents") / cur_p.name
                orig_p.parent.mkdir(parents=True, exist_ok=True)
                dest = _unique_dest_path(orig_p.parent, orig_p.name)
                if cur_p.exists():
                    shutil.move(str(cur_p), str(dest))
                conn.execute(
                    "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ?, archived = 0, original_path = '' WHERE id = ?",
                    (str(dest), dest.name, str(dest.parent), format_path_short(dest.parent), file_id),
                )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        self.recluster_all()
        return {"ok": True, "restoredPath": str(dest)}

    def empty_trash(self) -> dict[str, Any]:
        removed = 0
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT rowid, path FROM files WHERE archived = 1 AND folder LIKE '%GoodShelves_Trash%'"
                ).fetchall()
                for r in rows:
                    p = Path(r["path"])
                    if p.exists():
                        try:
                            p.unlink()
                        except OSError:
                            pass
                    conn.execute("DELETE FROM vec_files WHERE rowid = ?", (r["rowid"],))
                    conn.execute("DELETE FROM files WHERE rowid = ?", (r["rowid"],))
                    removed += 1
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        trash_dir = (get_user_special_dir("Documents") / "GoodShelves_Trash").resolve()
        if trash_dir.exists():
            for child in list(trash_dir.iterdir()):
                if child.is_dir():
                    try:
                        shutil.rmtree(child, ignore_errors=True)
                    except OSError:
                        pass
        return {"ok": True, "removedCount": removed}

    def preview_batch_rename_template(self, file_ids: list[str], template: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for idx, fid in enumerate(file_ids, start=1):
            f = self.get_file_by_id(fid)
            if not f:
                continue
            proposed = format_rename_template(f, template, seq_index=idx)
            out.append({
                "fileId": fid,
                "fromName": f["name"],
                "toName": proposed,
                "folder": f["folder"],
            })
        return out

    def execute_batch_rename_template(self, file_ids: list[str], template: str) -> dict[str, Any]:
        previews = self.preview_batch_rename_template(file_ids, template)
        moves_log: list[dict[str, Any]] = []
        renamed = 0
        with self._lock:
            conn = self._connect()
            try:
                for p in previews:
                    fid = p["fileId"]
                    row = conn.execute("SELECT path, name FROM files WHERE id = ?", (fid,)).fetchone()
                    if not row:
                        continue
                    src = Path(row["path"])
                    if not src.exists():
                        continue
                    dst = _unique_dest_path(src.parent, p["toName"], src_path=src)
                    if dst == src:
                        continue
                    src.rename(dst)
                    conn.execute(
                        "UPDATE files SET path = ?, name = ?, renamed = 1 WHERE id = ?",
                        (str(dst), dst.name, fid),
                    )
                    conn.execute(
                        "INSERT INTO rename_history (file_id, old_path, new_path, old_name, new_name, timestamp, undone) VALUES (?, ?, ?, ?, ?, ?, 0)",
                        (fid, str(src), str(dst), src.name, dst.name, time.time()),
                    )
                    moves_log.append({
                        "fileId": fid,
                        "fromPath": str(src),
                        "toPath": str(dst),
                        "renamedAfter": 1,
                    })
                    renamed += 1
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        if moves_log:
            self._record_action(
                "rename_batch",
                f"Batch renamed {renamed} file(s) with template",
                {"label": f"Template Rename ({renamed})", "moves": moves_log},
            )
        return {"ok": True, "renamedCount": renamed}

    def get_file_excerpt(self, file_id: str, max_lines: int = 500) -> dict[str, Any]:
        f = self.get_file_by_id(file_id)
        if not f:
            raise ValueError("File not found")
        p = Path(f["path"])
        if not p.exists():
            raise ValueError("File missing on disk")

        ext = p.suffix.lower()
        binary_or_rich = {
            ".pdf", ".docx", ".doc", ".pptx", ".odt", ".rtf",
            ".xlsx", ".xlsm", ".xls", ".zip", ".tar", ".tgz", ".gz",
        }
        if ext in binary_or_rich:
            extracted_full = ""
            if ext == ".pdf":
                try:
                    import pymupdf

                    with _FITZ_LOCK:
                        doc = pymupdf.open(str(p))
                        try:
                            pages = []
                            for idx in range(min(len(doc), 15)):
                                txt = doc[idx].get_text("text").strip()
                                if txt:
                                    pages.append(f"--- Page {idx + 1} ---\n{txt}")
                            extracted_full = "\n\n".join(pages)
                        finally:
                            doc.close()
                except Exception:
                    extracted_full = ""
            elif ext == ".docx":
                try:
                    import docx as python_docx  # type: ignore

                    d = python_docx.Document(str(p))
                    blocks = [para.text.strip() for para in d.paragraphs if para.text.strip()]
                    for tbl in d.tables:
                        for row in tbl.rows:
                            row_txt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                            if row_txt:
                                blocks.append(row_txt)
                    extracted_full = "\n\n".join(blocks)
                except Exception:
                    extracted_full = ""
            elif ext == ".rtf":
                try:
                    extracted_full = _parse_rtf_text(p.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    extracted_full = ""
            elif ext == ".odt":
                try:
                    extracted_full, _, _ = _parse_odt_preview(p)
                except Exception:
                    extracted_full = ""
            elif ext == ".pptx":
                try:
                    extracted_full, _, _ = _parse_pptx_preview(p)
                except Exception:
                    extracted_full = ""
            if not extracted_full:
                extracted_full = str(f.get("extractedText") or f.get("ocrText") or "")
            extracted_full = clean_extracted_text(extracted_full, preserve_newlines=True)
            all_lines = extracted_full.splitlines()
            sliced = all_lines[:max_lines]
            return {
                "fileId": file_id,
                "path": str(p),
                "content": "\n".join(sliced),
                "totalLines": len(all_lines),
                "truncated": len(all_lines) > max_lines,
            }

        max_bytes = 256 * 1024
        lines: list[str] = []
        total_lines = 0
        truncated = False
        total_bytes = 0
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                total_lines += 1
                if len(lines) < max_lines and total_bytes < max_bytes:
                    total_bytes += len(ln.encode("utf-8", errors="ignore"))
                    lines.append(ln.rstrip("\r\n"))
                else:
                    truncated = True
        return {
            "fileId": file_id,
            "path": str(p),
            "content": "\n".join(lines),
            "totalLines": total_lines,
            "truncated": truncated,
        }

    def get_workspace_insights(self) -> dict[str, Any]:
        state = self.get_all_state()
        files = state["files"]
        total_bytes = sum(int(f.get("sizeBytes") or 0) for f in files)
        by_mod: dict[str, dict[str, Any]] = {}
        by_shelf: dict[str, dict[str, Any]] = {}
        unnamed_count = 0
        now = time.time()
        stale_downloads: list[dict[str, Any]] = []

        for f in files:
            mod = str(f.get("modality") or "document")
            sb = int(f.get("sizeBytes") or 0)
            m_entry = by_mod.setdefault(mod, {"modality": mod, "count": 0, "bytes": 0})
            m_entry["count"] += 1
            m_entry["bytes"] += sb

            sh = str(f.get("shelfName") or "uncategorized")
            s_entry = by_shelf.setdefault(sh, {"shelfName": sh, "shelfId": f.get("shelfId", ""), "count": 0, "bytes": 0})
            s_entry["count"] += 1
            s_entry["bytes"] += sb

            if f.get("suggestedName") and not f.get("renamed"):
                unnamed_count += 1
            if "download" in str(f.get("folderFull", "")).lower() and (now - float(f.get("mtime") or now)) > 30 * 86400:
                stale_downloads.append({
                    "id": f["id"],
                    "name": f["name"],
                    "size": f["size"],
                    "date": f["date"],
                })

        largest = sorted(files, key=lambda x: int(x.get("sizeBytes") or 0), reverse=True)[:8]
        reclaimable_mb = round(sum(p["saved_mb"] for p in state["lookalikes"] if not p.get("resolved")), 2)
        return {
            "totalFiles": len(files),
            "totalBytes": total_bytes,
            "totalHuman": format_bytes(total_bytes),
            "byModality": sorted(by_mod.values(), key=lambda x: x["bytes"], reverse=True),
            "byShelf": sorted(by_shelf.values(), key=lambda x: x["bytes"], reverse=True)[:8],
            "reclaimableMB": reclaimable_mb,
            "duplicatePairsCount": len([p for p in state["lookalikes"] if not p.get("resolved")]),
            "unnamedCount": unnamed_count,
            "largestFiles": [{"id": f["id"], "name": f["name"], "title": f["title"], "size": f["size"], "format": f["format"]} for f in largest],
            "staleDownloads": stale_downloads[:8],
        }
