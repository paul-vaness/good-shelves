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
import functools
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
from deduplicator import find_lookalike_pairs, pick_keep_and_copy
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
    _CJK_HANGUL_RE,
    _CODE_KEYWORD_EXCLUDES,
    _FITZ_LOCK,
    _derive_human_title,
    _parse_html_clean,
    _parse_odt_preview,
    _parse_pptx_preview,
    _parse_rtf_text,
    _read_bounded_text,
    _subprocess_win_kwargs,
    clean_extracted_text,
    clean_ocr_text_block,
    compute_sha256,
    detect_magic_ext,
    parse_file,
    run_ocr_on_image,
)
from renamer import (
    format_rename_template,
    generate_rename_suggestion,
    simulate_batch_rename_collisions,
    synthesize_content_rename,
)
from reranker import rerank_candidates
from entity_extractor import extract_entities, entity_filters_from_query, matches_entity_filters, strip_entity_operators
from proactive_insights import detect_insights, format_insight_markdown
from query_parser import (
    FileQuerySpec,
    apply_sort_and_limit,
    load_full_texts_from_db,
    matches_query_spec,
    parse_file_query,
    parse_temporal_spec as _qp_parse_temporal_spec,
    row_matches_due_window,
    row_matches_temporal as _qp_row_matches_temporal,
)
from qa_engine import (
    _find_passage_anchor,
    answer_cross_file_question,
    answer_deep_file_question,
    build_cross_file_knowledge_graph,
    build_file_context_dossier,
    compare_files_rich,
    compute_financial_analytics,
    compute_scoped_workspace_stats,
    ensure_ocr_for_focus_files,
    extract_factoid_answer,
    format_due_payments_answer,
    format_timeline_answer,
    get_full_texts_batch,
    split_sliding_chunks,
    summarize_file_mmr,
)
from llm_router import (
    chat_general as _ollama_chat_general,
    plan_with_ollama,
    synthesize_qa_answer,
    synthesize_smart_renames,
)
from ai_agent import (
    _SHELF_TOPIC_FOLDER_ALIASES,
    VirtualWorkspaceSimulator,
    build_deep_file_context,
    build_workspace_process_audit,
    extract_conversation_context,
    extract_step0_query_clause,
    filter_files_by_scope,
    find_matching_memory_entries,
    format_process_audit_markdown,
    infer_learned_route,
    infer_shelf_folder_for_file,
    is_deictic_file_phrase,
    is_deictic_folder_phrase,
    is_item_excluded_by_memory,
    load_ai_memory,
    make_plan_id,
    match_files_by_title_priority,
    merge_unique_plan_items,
    parse_rename_format_request,
    propagate_elided_object,
    resolve_anaphora_subset,
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


@functools.lru_cache(maxsize=16384)
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
    """Return term groups (base forms + weighted synonyms + domain/acronym/merchant expansions), display terms, and a normalized phrase."""
    raw_terms: list[str] = []
    for token in _search_tokens(query):
        if token in _SEARCH_STOPWORDS or len(token) < 2:
            continue
        if token not in raw_terms:
            raw_terms.append(token)

    try:
        from ai_training_feeder import ExpandedDomainKnowledgeBank
    except Exception:
        ExpandedDomainKnowledgeBank = None  # type: ignore[assignment]

    concepts: list[dict[str, set[str]]] = []
    for term in raw_terms:
        base = {term, _search_stem(term)}
        synonyms: set[str] = set()
        for key in (term, _search_stem(term)):
            synonyms.update(_SYNONYM_LOOKUP.get(key, set()))
            if ExpandedDomainKnowledgeBank is not None:
                if key in ExpandedDomainKnowledgeBank.ACRONYM_EXPANSIONS:
                    for exp in ExpandedDomainKnowledgeBank.ACRONYM_EXPANSIONS[key]:
                        synonyms.add(exp.lower())
                        synonyms.update(_search_tokens(exp))
                for canonical, aliases in ExpandedDomainKnowledgeBank.MERCHANT_CANONICAL_ALIASES.items():
                    if key in aliases or key == canonical.lower():
                        synonyms.add(canonical.lower())
                        synonyms.update(_search_tokens(canonical))
                        for al in aliases[:3]:
                            synonyms.update(_search_tokens(al))
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
    """Delegate to unified `query_parser.parse_temporal_spec` (7-day last week, stale, Q1-Q4, bare-year guard)."""
    return _qp_parse_temporal_spec(query)


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


def _parse_nl_query(raw_query: str, state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Unified NL query parser delegating to `query_parser.parse_file_query`."""
    return parse_file_query(raw_query, state=state).to_nl_dict()


def _row_matches_temporal(f_row: dict[str, Any], spec: dict[str, Any]) -> bool:
    return _qp_row_matches_temporal(f_row, spec)


def _row_matches_nl_filters(
    f_row: dict[str, Any],
    nl: dict[str, Any],
    full_text_map: Optional[dict[str, tuple[str, str]]] = None,
    duplicate_ids: Optional[set[str]] = None,
    exact_duplicate_ids: Optional[set[str]] = None,
) -> bool:
    spec_obj = nl.get("spec")
    if isinstance(spec_obj, FileQuerySpec):
        return matches_query_spec(
            f_row,
            spec_obj,
            full_text_map=full_text_map,
            duplicate_ids=duplicate_ids,
            exact_duplicate_ids=exact_duplicate_ids,
        )
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
    body_all = [*body_raw, *tags_raw, *meta_raw]
    body_norm = " ".join(body_all)

    title_tokens = set(title_raw) | {_search_stem(t) for t in title_raw}
    shelf_tokens = set(shelf_raw) | {_search_stem(t) for t in shelf_raw}
    body_tokens = set(body_all) | {_search_stem(t) for t in body_all}

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
            v_prefix = variant[:2]
            best_ratio = 0.0
            for cand in title_tokens:
                if cand[:2] == v_prefix and abs(len(cand) - len(variant)) <= 2:
                    r = difflib.SequenceMatcher(None, variant, cand).ratio()
                    if r > best_ratio:
                        best_ratio = r
            if best_ratio >= 0.82:
                return 0.72 * best_ratio, True
            for cand in shelf_tokens | body_tokens:
                if cand[:2] == v_prefix and abs(len(cand) - len(variant)) <= 2 and len(cand) >= 4:
                    r = difflib.SequenceMatcher(None, variant, cand).ratio()
                    if r > best_ratio:
                        best_ratio = r
            if best_ratio >= 0.84:
                return 0.42 * best_ratio, True
        return 0.0, False

    total = 0.0
    weight_sum = 0.0
    matched_labels: list[str] = []
    matched_concept_count = 0
    base_hit_count = 0
    for concept in concepts:
        best = 0.0
        best_label = ""
        is_base_hit = False
        for variant in concept["base"]:
            weight, hit = _match_weight(variant, synonym=False)
            if hit and weight > best:
                best = weight
                best_label = variant
                is_base_hit = True
        for variant in concept["syn"]:
            weight, hit = _match_weight(variant, synonym=True)
            if hit and weight * 0.9 > best:
                best = weight * 0.9
                best_label = variant
                is_base_hit = False
        idf = max((idf_map.get(v, 1.0) for v in concept["base"]), default=1.0) if idf_map else 1.0
        weight_sum += idf
        if best > 0:
            total += best * idf
            matched_labels.append(best_label)
            matched_concept_count += 1
            if is_base_hit:
                base_hit_count += 1

    score = total / max(1e-6, weight_sum)
    if len(concepts) >= 2:
        coverage_ratio = matched_concept_count / float(len(concepts))
        score = score * (0.45 + 0.55 * (coverage_ratio ** 1.2))
        if matched_concept_count > 0 and base_hit_count == 0:
            score *= 0.85

    phrase_hit = False
    if phrase and len(phrase) >= 4:
        if phrase in title_norm:
            score = min(1.0, score + 0.22)
            phrase_hit = True
        elif phrase in body_norm:
            score = min(1.0, score + 0.12)
            phrase_hit = True
        elif len(concepts) >= 3:
            p_words = phrase.split()
            for i in range(len(p_words) - 1):
                bg = f"{p_words[i]} {p_words[i + 1]}"
                if len(bg) >= 5:
                    if bg in title_norm:
                        score = min(1.0, score + 0.16)
                        phrase_hit = True
                        break
                    elif bg in body_norm:
                        score = min(1.0, score + 0.10)
                        phrase_hit = True
                        break
    return min(1.0, score), matched_labels, phrase_hit


def _extract_snippet_around_terms(text: str, terms: list[str], max_len: int = 96) -> str:
    """Return a clean, symbol-sanitized excerpt centered around the first matched term."""
    raw = (text or "").strip()
    if not raw:
        return ""
    if terms:
        low = raw.lower()
        for term in terms:
            pos = low.find(term.lower())
            if pos >= 0:
                start = max(0, pos - 32)
                end = min(len(raw), start + max_len + 32)
                window = clean_extracted_text(raw[start:end], preserve_newlines=False)
                window = re.sub(r"\s+", " ", window).strip()
                excerpt = window[:max_len].strip()
                if start > 0:
                    excerpt = "…" + excerpt
                if end < len(raw):
                    excerpt = excerpt + "…"
                return excerpt
    window = clean_extracted_text(raw[: max_len + 40], preserve_newlines=False)
    return re.sub(r"\s+", " ", window).strip()[:max_len]


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
        self._folder_tree_cache: Optional[tuple[float, list[Path]]] = None
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
        self._folder_tree_cache = None
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

                    CREATE TABLE IF NOT EXISTS search_telemetry (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        query TEXT NOT NULL,
                        latency_ms REAL NOT NULL,
                        result_count INTEGER NOT NULL,
                        clicked_file_id TEXT DEFAULT '',
                        clicked_rank INTEGER DEFAULT -1,
                        timestamp REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS action_feedback (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action_type TEXT NOT NULL,
                        file_count INTEGER NOT NULL,
                        accepted INTEGER NOT NULL,
                        timestamp REAL NOT NULL
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

                # FTS5 full-text search index for BM25-ranked lexical retrieval
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                        title, name, extracted_text, ocr_text, tags, shelf_name,
                        tokenize='porter unicode61'
                    )
                    """
                )
                # Triggers to keep standalone FTS in sync with files table
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS files_fts_ai AFTER INSERT ON files BEGIN
                        INSERT INTO files_fts(rowid, title, name, extracted_text, ocr_text, tags, shelf_name)
                        VALUES (new.rowid, new.title, new.name, new.extracted_text, new.ocr_text, new.tags_json, new.shelf_name);
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS files_fts_ad AFTER DELETE ON files BEGIN
                        DELETE FROM files_fts WHERE rowid = old.rowid;
                    END
                    """
                )
                conn.execute("DROP TRIGGER IF EXISTS files_fts_au")
                conn.execute(
                    """
                    CREATE TRIGGER files_fts_au
                    AFTER UPDATE OF title, name, extracted_text, ocr_text, tags_json, shelf_name ON files
                    BEGIN
                        DELETE FROM files_fts WHERE rowid = old.rowid;
                        INSERT INTO files_fts(rowid, title, name, extracted_text, ocr_text, tags, shelf_name)
                        SELECT new.rowid, new.title, new.name, new.extracted_text, new.ocr_text, new.tags_json, new.shelf_name
                        WHERE new.archived = 0;
                    END
                    """
                )

                # Backfill FTS for existing active files if empty
                fts_count = conn.execute("SELECT COUNT(*) FROM files_fts").fetchone()[0]
                if fts_count == 0:
                    existing = conn.execute("SELECT rowid, title, name, extracted_text, ocr_text, tags_json, shelf_name FROM files WHERE archived = 0 LIMIT 5000").fetchall()
                    if existing:
                        conn.executemany(
                            "INSERT INTO files_fts(rowid, title, name, extracted_text, ocr_text, tags, shelf_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [(r["rowid"], r["title"], r["name"], r["extracted_text"], r["ocr_text"], r["tags_json"], r["shelf_name"]) for r in existing],
                        )

                # Clean any archived files that lingered in vec_files / files_fts from older versions
                archived_rows = conn.execute("SELECT rowid FROM files WHERE archived = 1").fetchall()
                for ar in archived_rows:
                    conn.execute("DELETE FROM vec_files WHERE rowid = ?", (ar["rowid"],))
                    conn.execute("DELETE FROM files_fts WHERE rowid = ?", (ar["rowid"],))

                # Self-healing migration for OCR CJK hallucinations, meaningless titles, and rename suggestions
                cur_ver = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
                needs_title_repair = cur_ver < 3
                if not needs_title_repair:
                    broken_check = conn.execute(
                        """
                        SELECT 1 FROM files
                        WHERE title IN (':root {', 'times new roman;', 'image/svg+xml <path sty', 'include "sqlite3.h"', 'extends (ts script)')
                           OR title LIKE '"id":%'
                           OR title LIKE '"path":%'
                           OR title LIKE 'table: %'
                           OR title LIKE 'spreadsheet: %'
                           OR title LIKE 'archive: %'
                           OR title LIKE '%风区%'
                        LIMIT 1
                        """
                    ).fetchone()
                    needs_title_repair = broken_check is not None
                if needs_title_repair:
                    self._repair_indexed_titles_and_renames(conn)

                # Schema versioning
                conn.execute("PRAGMA user_version = 3")

                conn.commit()
            finally:
                conn.close()

    def _repair_indexed_titles_and_renames(self, conn: Any) -> int:
        """
        Self-healing migration:
        - Strips RapidOCR CJK hallucinations and leading/trailing 1-2 char OCR noise tokens
        - Re-parses corrupt RTF font-table extractions and SVG XML fragment extractions when file exists on disk
        - Re-computes clean human editorial titles via `_derive_human_title`
        - Re-computes smart rename suggestions via `generate_rename_suggestion` for un-renamed files
        - Rebuilds `files_fts` for active files
        """
        rows = conn.execute(
            """
            SELECT rowid, id, path, name, ext, modality, size_bytes, mtime, created_date, display_date,
                   sha256, title, extracted_text, ocr_text, ocr_confidence, specimen_type,
                   width, height, page_count, dhash, renamed, suggested_name, rename_reason,
                   archived, metadata_json, tags_json, shelf_name
            FROM files
            """
        ).fetchall()
        if not rows:
            return 0

        repaired_count = 0
        for r in rows:
            rowid = int(r["rowid"])
            filepath = Path(str(r["path"]))
            name = str(r["name"] or filepath.name)
            # Some archived rows have a timestamp suffix on disk path while `name` holds the original filename
            path_for_title = filepath.parent / name if name else filepath
            ext_dot = path_for_title.suffix.lower() or f".{str(r['ext'] or '').lstrip('.').lower()}"
            modality = str(r["modality"] or "document")
            old_title = str(r["title"] or "")
            old_ocr = str(r["ocr_text"] or "")
            old_txt = str(r["extracted_text"] or "")
            old_sugg = r["suggested_name"]
            old_reason = r["rename_reason"]

            try:
                meta = json.loads(r["metadata_json"] or "{}")
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}

            headings = [
                clean_extracted_text(str(h), preserve_newlines=False)
                for h in (meta.get("headings") or [])
                if str(h).strip()
            ]
            code_symbols = [
                str(s)
                for s in (meta.get("codeSymbols") or [])
                if str(s).strip() and str(s).strip().lower() not in _CODE_KEYWORD_EXCLUDES
            ]
            visual_tags = [str(t) for t in (meta.get("visualConcepts") or []) if str(t).strip()]

            new_ocr = clean_ocr_text_block(clean_extracted_text(old_ocr, preserve_newlines=True)) if old_ocr else ""
            if modality == "image" and ext_dot != ".svg":
                if old_ocr:
                    new_txt = new_ocr
                else:
                    new_txt = clean_extracted_text(old_txt, preserve_newlines=True)
            elif ext_dot == ".rtf" and ("Times New Roman;" in old_txt or "Cambria Math;" in old_txt) and filepath.exists():
                try:
                    raw_rtf = _read_bounded_text(filepath, max_bytes=262144)
                    new_txt = _parse_rtf_text(raw_rtf)
                except Exception:
                    new_txt = ""
            elif ext_dot == ".svg" and ("image/svg+xml" in old_txt or "<path" in old_txt):
                if filepath.exists():
                    try:
                        raw_svg = _read_bounded_text(filepath, max_bytes=131072)
                        new_txt, svg_h = _parse_html_clean(raw_svg[:60000], strip_svg=False)
                        if svg_h:
                            headings = svg_h
                    except Exception:
                        new_txt = ""
                else:
                    new_txt = ""
            elif modality != "code" and ext_dot not in {".html", ".htm", ".php"}:
                new_txt = clean_extracted_text(old_txt, preserve_newlines=True)
            else:
                new_txt = _CJK_HANGUL_RE.sub(" ", old_txt).replace("\ufffd", " ")

            new_title = _derive_human_title(
                path_for_title,
                modality,
                headings,
                code_symbols,
                new_txt,
                new_ocr,
            )

            meta_changed = False
            if meta.get("codeSymbols") != code_symbols and "codeSymbols" in meta:
                meta["codeSymbols"] = code_symbols
                meta_changed = True
            if meta.get("headings") != headings and "headings" in meta:
                meta["headings"] = headings
                meta_changed = True

            if int(r["renamed"] or 0) == 0:
                parsed_stub = ParsedFile(
                    path=path_for_title,
                    name=name,
                    ext=str(r["ext"] or ext_dot.lstrip(".")),
                    modality=modality,
                    size_bytes=int(r["size_bytes"] or 0),
                    mtime=float(r["mtime"] or 0.0),
                    created_date=str(r["created_date"] or "2026-01-01"),
                    display_date=str(r["display_date"] or "Jan 1, 2026"),
                    sha256=str(r["sha256"] or ""),
                    title=new_title,
                    extracted_text=new_txt,
                    ocr_text=new_ocr,
                    ocr_confidence=float(r["ocr_confidence"] or 0.0),
                    headings=headings,
                    code_symbols=code_symbols,
                    visual_tags=visual_tags,
                    width=int(r["width"] or 0),
                    height=int(r["height"] or 0),
                    page_count=int(r["page_count"] or 1),
                    dhash=str(r["dhash"] or ""),
                    specimen_type=str(r["specimen_type"] or "text"),
                    metadata=meta,
                )
                new_sugg, new_reason = generate_rename_suggestion(parsed_stub)
            else:
                new_sugg, new_reason = None, None

            if (
                new_title != old_title
                or new_ocr != old_ocr
                or new_txt != old_txt
                or new_sugg != old_sugg
                or new_reason != old_reason
                or meta_changed
            ):
                new_meta_json = json.dumps(meta, ensure_ascii=False)
                conn.execute(
                    """
                    UPDATE files
                    SET title = ?, ocr_text = ?, extracted_text = ?,
                        suggested_name = ?, rename_reason = ?, metadata_json = ?
                    WHERE rowid = ?
                    """,
                    (new_title, new_ocr, new_txt, new_sugg, new_reason, new_meta_json, rowid),
                )
                repaired_count += 1

        # Rebuild files_fts cleanly for all active files
        conn.execute("DELETE FROM files_fts")
        active_rows = conn.execute(
            "SELECT rowid, title, name, extracted_text, ocr_text, tags_json, shelf_name FROM files WHERE archived = 0"
        ).fetchall()
        if active_rows:
            conn.executemany(
                "INSERT INTO files_fts(rowid, title, name, extracted_text, ocr_text, tags, shelf_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        ar["rowid"],
                        ar["title"],
                        ar["name"],
                        ar["extracted_text"],
                        ar["ocr_text"],
                        ar["tags_json"],
                        ar["shelf_name"],
                    )
                    for ar in active_rows
                ],
            )
        return repaired_count

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
                    "ollama_model": raw.get("ollama_model", ""),
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
        try:
            vec_512, visual_share, text_share = self.embedder.embed_parsed_file(parsed)
        finally:
            if parsed.pil_image is not None:
                try:
                    parsed.pil_image.close()
                except Exception:
                    pass
                parsed.pil_image = None
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
        if parsed.modality == "image":
            parsed.metadata = {**(parsed.metadata or {}), "visionModel": self.embedder.vision_model_id}
        if parsed.visual_tags:
            parsed.metadata = {**(parsed.metadata or {}), "visualConcepts": parsed.visual_tags[:8]}
            if parsed.modality == "image" and not parsed.extracted_text.strip():
                scene_tags = [t for t in parsed.visual_tags if t not in {"landscape image", "portrait image", "square image"}]
                if scene_tags:
                    parsed.extracted_text = ", ".join(scene_tags[:8])

        # Extract structured entities (merchant, amount, date, invoice, etc.) from OCR/text
        entity_text = f"{parsed.ocr_text or ''}\n{parsed.extracted_text or ''}"
        if entity_text.strip():
            entities = extract_entities(entity_text, filename=parsed.name, existing_metadata=parsed.metadata)
            if entities:
                parsed.metadata = entities

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
                            fp = Path(entry.path).resolve() if is_sym else Path(entry.path)
                            if ext not in all_supported and not detect_magic_ext(fp):
                                continue
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

    def _remove_file_from_search_indices(self, conn: sqlite3.Connection, file_id: Optional[str] = None, rowid: Optional[int] = None) -> None:
        """Remove an archived/trashed file from vec_files and files_fts so it never appears in search."""
        if rowid is None and file_id is not None:
            r = conn.execute("SELECT rowid FROM files WHERE id = ?", (file_id,)).fetchone()
            if not r:
                return
            rowid = int(r["rowid"])
        if rowid is None:
            return
        conn.execute("DELETE FROM vec_files WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (rowid,))

    def _restore_file_to_search_indices(self, conn: sqlite3.Connection, file_id: Optional[str] = None, rowid: Optional[int] = None) -> None:
        """Re-insert a restored file into vec_files and files_fts."""
        if rowid is not None:
            r = conn.execute(
                "SELECT rowid, title, name, extracted_text, ocr_text, tags_json, shelf_name, embedding_blob "
                "FROM files WHERE rowid = ? AND archived = 0",
                (rowid,),
            ).fetchone()
        elif file_id is not None:
            r = conn.execute(
                "SELECT rowid, title, name, extracted_text, ocr_text, tags_json, shelf_name, embedding_blob "
                "FROM files WHERE id = ? AND archived = 0",
                (file_id,),
            ).fetchone()
        else:
            return
        if not r:
            return
        rid = int(r["rowid"])
        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (rid,))
        conn.execute(
            "INSERT INTO files_fts(rowid, title, name, extracted_text, ocr_text, tags, shelf_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, r["title"], r["name"], r["extracted_text"], r["ocr_text"], r["tags_json"], r["shelf_name"]),
        )
        if r["embedding_blob"]:
            conn.execute("DELETE FROM vec_files WHERE rowid = ?", (rid,))
            conn.execute("INSERT INTO vec_files(rowid, embedding) VALUES (?, ?)", (rid, r["embedding_blob"]))

    def recluster_all(self) -> dict[str, Any]:
        """Run UMAP + HDBSCAN over all active files in SQLite and persist discovered shelves."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM files WHERE archived = 0 ORDER BY mtime DESC"
                ).fetchall()
                if not rows:
                    conn.execute("DELETE FROM shelves")
                    conn.commit()
                    self.silhouette_score = 0.0
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
                else clean_extracted_text((d["extracted_text"] or d["ocr_text"] or "")[:800], preserve_newlines=True)[:600]
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
        # Check disk cache first (LRU)
        cache_dir = self.data_dir / "thumb_cache"
        cache_path = cache_dir / f"{file_id}.jpg"
        if cache_path.exists():
            try:
                return cache_path.read_bytes()
            except Exception:
                pass

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
            data = base64.b64decode(raw_b64)
            # Populate disk cache
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(data)
                # LRU eviction: keep cache under 100MB
                self._evict_thumb_cache_if_needed(cache_dir, max_bytes=100 * 1024 * 1024)
            except Exception:
                pass
            return data
        except Exception:
            return None

    def _evict_thumb_cache_if_needed(self, cache_dir: Path, max_bytes: int = 100 * 1024 * 1024) -> None:
        """Evict oldest thumbnail cache files if total size exceeds max_bytes."""
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in cache_dir.glob("*.jpg")]
            total = sum(s for _, s, _ in files)
            if total <= max_bytes:
                return
            files.sort()  # oldest first
            excess = total - max_bytes
            for _, size, p in files:
                if excess <= 0:
                    break
                try:
                    p.unlink()
                    excess -= size
                except Exception:
                    pass
        except Exception:
            pass

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
        dedup_exact: bool = True,
    ) -> dict[str, Any]:
        """
        Encode natural language query via ONNX Text + Visual Concept Encoder and query
        `sqlite-vec` (`vec_files`) for nearest neighbors by cosine distance.
        Uses unified `FileQuerySpec` AST + full-text SQLite filtering (no 600-char blindness).
        """
        t0 = time.perf_counter()
        settings = self.get_settings()
        max_k = limit if (limit is not None and limit > 0) else int(settings.get("max_results", 50))
        min_sim = min_similarity if min_similarity is not None else float(settings.get("min_similarity", 0.22))

        clean_q = (query or "").strip()
        state_for_parse = self.get_all_state()
        text_q, op_filters = _extract_search_operators(clean_q)
        entity_filters = entity_filters_from_query(text_q)
        text_q_clean = strip_entity_operators(text_q)
        spec = parse_file_query(text_q, state=state_for_parse)
        nl = spec.to_nl_dict()
        if spec.entity_filters:
            entity_filters = {**spec.entity_filters, **entity_filters}
        effective_modality = (
            modality_filter
            if modality_filter != "all"
            else (op_filters.get("modality") or nl.get("strict_modality") or "all")
        )
        if spec.top_k is not None and spec.top_k > 0:
            max_k = min(max_k, spec.top_k) if limit else spec.top_k

        if not clean_q:
            all_files = filter_files_by_scope(state_for_parse["files"], scope_folder, recursive=scope_recursive)
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

        topic_raw = nl["topic_query"] if nl["has_strict_filters"] or nl["intent_mode"] in ("list", "count", "due_soon", "timeline") else text_q_clean
        sort_mode: Optional[str] = spec.sort_by
        if not sort_mode and text_q_clean:
            if re.search(r"\b(?:largest|biggest)\b", text_q_clean, re.IGNORECASE):
                sort_mode = "largest"
            elif re.search(r"\b(?:smallest|lightest)\b", text_q_clean, re.IGNORECASE):
                sort_mode = "smallest"
            elif re.search(r"\b(?:oldest|earliest)\b", text_q_clean, re.IGNORECASE):
                sort_mode = "oldest"
            elif re.search(r"\b(?:newest|latest|most\s+recent)\b", text_q_clean, re.IGNORECASE):
                sort_mode = "newest"
        clean_topic_q = re.sub(
            r"\b(?:largest|biggest|smallest|lightest|oldest|earliest|newest|latest|most\s+recent)\b",
            " ",
            topic_raw,
            flags=re.IGNORECASE,
        )
        clean_topic_q = " ".join(clean_topic_q.split()).strip() if clean_topic_q else ""

        concepts, raw_terms, phrase = _query_concepts(clean_topic_q)
        intent = _infer_query_features(text_q_clean)
        temporal_spec = nl.get("temporal")
        temporal_win = (
            (temporal_spec["start_ts"], temporal_spec["end_ts"], temporal_spec["label"])
            if temporal_spec is not None
            else _parse_temporal_window(text_q_clean)
        )
        visual_subject = _clean_visual_query(clean_topic_q or text_q_clean) if (clean_topic_q or not nl["has_strict_filters"]) else ""
        now = time.time()

        # Precompute duplicate ID sets only when requested by query spec
        dup_ids: Optional[set[str]] = None
        exact_dup_ids: Optional[set[str]] = None
        if spec.duplicates_only or spec.exact_duplicates_only:
            dup_ids = set()
            exact_dup_ids = set()
            for p in state_for_parse.get("lookalikes") or []:
                if p.get("resolved"):
                    continue
                cid = str((p.get("copy") or {}).get("id") or "")
                kid = str((p.get("keep") or {}).get("id") or "")
                if cid:
                    dup_ids.add(cid)
                if kid:
                    dup_ids.add(kid)
                if p.get("isExact") or int(p.get("similarity_pct") or 0) == 100:
                    if cid:
                        exact_dup_ids.add(cid)
                    if kid:
                        exact_dup_ids.add(kid)

        # Queries that express operators, dates, phrases, or type filters without a remaining semantic topic
        # scan active files strictly using FULL SQLite text (eliminates 600-char blindness!).
        if not concepts:
            items = filter_files_by_scope(list(state_for_parse["files"]), scope_folder, recursive=scope_recursive)
            need_full_text = bool(
                spec.include_phrases or spec.exclude_terms or spec.has_ocr is not None or op_filters.get("exclude_terms")
            )
            full_text_map = load_full_texts_from_db(self, [str(f["id"]) for f in items]) if need_full_text else None
            if effective_modality != "all":
                items = [f for f in items if f["modality"] == effective_modality]
            elif not nl["has_strict_filters"] and intent["modality"]:
                items = [f for f in items if f["modality"] == intent["modality"]]
            if not nl["has_strict_filters"] and intent["folder"]:
                items = [f for f in items if intent["folder"] in f["folderFull"].lower()]
            if not nl["has_strict_filters"] and not temporal_spec and not spec.due_window and intent["recency_days"]:
                cutoff = now - intent["recency_days"] * 86400.0
                recent_items = [f for f in items if float(f["mtime"]) >= cutoff]
                if recent_items or sort_mode != "newest":
                    items = recent_items
            if op_filters:
                items = [f for f in items if _matches_operator_filters(f, op_filters)]
            if nl["has_strict_filters"]:
                items = [
                    f for f in items
                    if _row_matches_nl_filters(
                        f,
                        nl,
                        full_text_map=full_text_map,
                        duplicate_ids=dup_ids,
                        exact_duplicate_ids=exact_dup_ids,
                    )
                ]
            if entity_filters:
                items = [f for f in items if matches_entity_filters((f.get("metadata") or {}), entity_filters)]

            if sort_mode == "largest":
                items.sort(key=lambda f: (int(f.get("sizeBytes") or 0), float(f["mtime"])), reverse=True)
            elif sort_mode == "smallest":
                items.sort(key=lambda f: (int(f.get("sizeBytes") or 0), float(f["mtime"])), reverse=False)
            elif sort_mode == "oldest":
                items.sort(key=lambda f: (str(f.get("createdDate") or ""), float(f["mtime"])), reverse=False)
            elif sort_mode == "due_date":
                items.sort(key=lambda f: str((f.get("metadata") or {}).get("due_date") or "9999-12-31"))
            else:
                items.sort(key=lambda f: (str(f.get("createdDate") or ""), float(f["mtime"])), reverse=True)

            results = []
            for idx, f in enumerate(items[:max_k]):
                item = dict(f)
                item["matchScore"] = max(72, 98 - idx)
                item["cosine"] = "1.000"
                if nl["include_phrases"]:
                    ft_pair = full_text_map.get(str(f["id"])) if full_text_map else None
                    raw_body = (
                        f"{ft_pair[1]} {ft_pair[0]}".strip()
                        if ft_pair
                        else (f.get("ocrText") or f.get("extractedText") or f.get("title") or "")
                    )
                    snip = _extract_snippet_around_terms(raw_body, nl["include_phrases"], max_len=110)
                    why_msg = f'Matched “{nl["include_phrases"][0]}”' + (f" · {snip}" if snip else "")
                elif nl["applied_filters"]:
                    why_msg = f"{' · '.join(nl['applied_filters'][:3])} · {f['date']}"
                elif sort_mode == "largest":
                    why_msg = f"Largest first · {f['size']} · {f['folder']}"
                elif sort_mode == "smallest":
                    why_msg = f"Smallest first · {f['size']} · {f['folder']}"
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

        non_image_intent = (
            intent["modality"] in ("code", "document", "audio", "video", "archive")
            or (
                bool(op_filters.get("ext"))
                and not (set(op_filters["ext"]) & {"png", "jpg", "jpeg", "webp", "heic", "svg", "gif", "bmp", "tiff"})
            )
        )
        need_visual_query = (
            effective_modality == "image"
            or intent["modality"] == "image"
            or (effective_modality == "all" and not non_image_intent)
        )
        q_vis_128 = (
            self.embedder.encode_visual_query(visual_subject)
            if (visual_subject and need_visual_query)
            else None
        )
        topic_query = clean_topic_q or text_q
        reuse_vis = (
            q_vis_128
            if q_vis_128 is not None
            else (np.zeros(128, dtype=np.float32) if not need_visual_query else None)
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
                # Include high-precision acronym & merchant alias expansions in lexical recall
                try:
                    from ai_training_feeder import ExpandedDomainKnowledgeBank
                    for exp_term in ExpandedDomainKnowledgeBank.expand_query_with_domain_synonyms(clean_topic_q or text_q, max_expansions=4):
                        for tok in _search_tokens(exp_term):
                            if len(tok) >= 3 and tok not in _SEARCH_STOPWORDS and tok not in lexical_terms:
                                lexical_terms.append(tok)
                                if len(lexical_terms) >= 14:
                                    break
                        if len(lexical_terms) >= 14:
                            break
                except Exception:
                    pass

                lexical_rowids: set[int] = set()
                if lexical_terms:
                    clauses = []
                    params: list[Any] = []
                    for term in lexical_terms[:14]:
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

                # 2a. Typo-tolerant fuzzy lexical recovery when query terms produce sparse/zero exact hits
                if raw_terms and len(lexical_rowids) < 2:
                    import difflib
                    vocab_rows = conn.execute(
                        "SELECT title, name, shelf_name, tags_json FROM files WHERE archived = 0 LIMIT 1500"
                    ).fetchall()
                    vocab_set: set[str] = set()
                    for vr in vocab_rows:
                        blob_v = f"{vr['title']} {vr['name']} {vr['shelf_name']} {vr['tags_json']}"
                        for vt in _search_tokens(blob_v):
                            if len(vt) >= 4:
                                vocab_set.add(vt)
                    if vocab_set:
                        vocab_list = list(vocab_set)
                        fuzzy_added: list[str] = []
                        for idx_c, rt in enumerate(raw_terms):
                            if len(rt) < 4 or rt in vocab_set:
                                continue
                            close = difflib.get_close_matches(rt, vocab_list, n=2, cutoff=0.76)
                            for cm in close:
                                if cm not in lexical_terms:
                                    lexical_terms.append(cm)
                                    fuzzy_added.append(cm)
                                if idx_c < len(concepts):
                                    concepts[idx_c]["syn"].add(cm)
                        if fuzzy_added:
                            f_clauses = []
                            f_params: list[Any] = []
                            for ft in fuzzy_added[:8]:
                                like = f"%{ft}%"
                                f_clauses.append(
                                    "(lower(title) LIKE ? OR lower(name) LIKE ? OR lower(extracted_text) LIKE ? "
                                    "OR lower(ocr_text) LIKE ? OR lower(shelf_name) LIKE ? OR lower(tags_json) LIKE ?)"
                                )
                                f_params.extend([like] * 6)
                            f_rows = conn.execute(
                                f"SELECT rowid FROM files WHERE archived = 0 AND ({' OR '.join(f_clauses)}) LIMIT 250",
                                f_params,
                            ).fetchall()
                            lexical_rowids |= {int(r["rowid"]) for r in f_rows}

                # 2b. FTS5 BM25 candidates (porter-stemmed, ranked by relevance)
                fts_rowids: set[int] = set()
                fts_rank_map: dict[int, int] = {}
                fts_terms = [
                    (f'"{t}"*' if len(t) >= 4 and " " not in t else f'"{t}"')
                    for t in lexical_terms[:8]
                ]
                fts_query = " OR ".join(fts_terms) if fts_terms else ""
                if fts_query:
                    try:
                        fts_rows = conn.execute(
                            "SELECT rowid, bm25(files_fts) AS rank FROM files_fts "
                            "WHERE files_fts MATCH ? AND rank < 0 ORDER BY rank LIMIT 100",
                            (fts_query,),
                        ).fetchall()
                        fts_rowids = {int(r["rowid"]) for r in fts_rows}
                        # Map rowid -> 1-based BM25 rank position for RRF-style fusion boost.
                        fts_rank_map = {int(r["rowid"]): pos for pos, r in enumerate(fts_rows, start=1)}
                    except Exception:
                        fts_rowids = set()
                        fts_rank_map = {}

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

                candidate_rowids = set(vec_distance) | lexical_rowids | fts_rowids
                # When strict NL constraints, operators, or entity filters are active, include all active rowids
                # so exact filters never drop matching files outside top-K vector proximity.
                if nl["has_strict_filters"] or op_filters or entity_filters:
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
                    if nl["has_strict_filters"] and not _row_matches_nl_filters(
                        f_dict_raw,
                        nl,
                        duplicate_ids=dup_ids,
                        exact_duplicate_ids=exact_dup_ids,
                    ):
                        continue
                    if entity_filters:
                        try:
                            meta_raw = f_dict_raw.get("metadata_json") or f_dict_raw.get("metadata") or "{}"
                            if isinstance(meta_raw, str):
                                meta_obj = json.loads(meta_raw)
                            else:
                                meta_obj = meta_raw
                        except Exception:
                            meta_obj = {}
                        if not matches_entity_filters(meta_obj, entity_filters):
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
                    elif f_row["modality"] == "archive" and intent["modality"] != "archive":
                        boost -= 0.06
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
                    capped_aff = 0.0
                    if adaptive_on:
                        f_aff = file_affinity.get(str(f_row["id"]), 0.0)
                        s_aff = shelf_affinity.get(str(f_row["shelf_id"]), 0.0)
                        if f_aff > 0:
                            capped_aff += min(0.085, float(0.03 * np.log1p(f_aff)))
                        if s_aff > 0:
                            capped_aff += min(0.045, float(0.015 * np.log1p(s_aff)))
                        boost += capped_aff
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
                    # Reciprocal Rank Fusion boost from FTS5 BM25 ranking.
                    rrf_boost = 0.0
                    fts_pos = fts_rank_map.get(rowid)
                    if fts_pos is not None:
                        rrf_boost = 0.15 * (10.0 / (10.0 + fts_pos))
                    blended = 0.50 * max(0.0, cos_sim) + 0.30 * lex_score + visual_boost + boost + rrf_boost
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
                    # Stash first-stage signals so the reranker can reuse them without re-embedding.
                    item_dict["_cosSim"] = float(max(0.0, min(1.0, cos_sim)))
                    item_dict["_visSim"] = float(vis_sim)
                    item_dict["_lexScore"] = float(lex_score)
                    item_dict["_phraseHit"] = bool(phrase_hit)
                    item_dict["_matchedTermsCount"] = len(matched_terms)
                    item_dict["_affinityBoost"] = float(capped_aff)
                    item_dict["_ftsScore"] = float(10.0 / (10.0 + fts_pos)) if fts_pos is not None else 0.0
                    ranked.append((blended, item_dict))

                # Cross-encoder rerank pass: sort descending first, re-score top-80, and preserve tail.
                if ranked and not sort_mode:
                    ranked.sort(key=lambda x: (x[0], float(x[1]["mtime"])), reverse=True)
                    top_slice = ranked[:80]
                    tail_slice = ranked[80:]
                    rerank_input: list[dict[str, Any]] = []
                    for score_val, item in top_slice:
                        file_modality = item.get("modality", "")
                        rerank_input.append({
                            "title": item.get("title", ""),
                            "name": item.get("name", ""),
                            "shelf_name": item.get("shelfName", ""),
                            "tags": item.get("tags", []),
                            "ocr_text": item.get("ocrText", ""),
                            "extracted_text": item.get("extractedText", ""),
                            "metadata_json": json.dumps(item.get("metadata", {})),
                            "modality": file_modality,
                            "mtime": item.get("mtime", now),
                            "_dense_sim": float(item.get("_cosSim", 0.0)),
                            "_vis_sim": float(item.get("_visSim", 0.0)) if file_modality == "image" else 0.0,
                            "_affinity_boost": float(item.get("_affinityBoost", 0.0)),
                            "_fts_score": float(item.get("_ftsScore", 0.0)),
                            "_original_score": score_val,
                            "_original_item": item,
                        })
                    reranked = rerank_candidates(
                        query=clean_topic_q or text_q,
                        candidates=rerank_input,
                        query_embedding=q_vec,
                        top_n=len(rerank_input),
                        now=now,
                        idf_map=idf_map,
                        has_recency_intent=bool(intent["recency_days"] or temporal_spec),
                    )
                    # Blend original score with rerank score (60% rerank + 40% original)
                    # and refresh the displayed matchScore/cosine so the UI stays consistent.
                    new_ranked: list[tuple[float, dict[str, Any]]] = []
                    for rc in reranked:
                        orig_score = float(rc.get("_original_score", 0.0))
                        rerank_score = float(rc.get("_rerank_score", 0.0))
                        blended_score = 0.60 * rerank_score + 0.40 * orig_score
                        orig_item = rc.get("_original_item", {})
                        clipped = float(np.clip(blended_score, 0.0, 0.999))
                        orig_item["cosine"] = f"{clipped:.3f}"
                        orig_item["matchScore"] = min(99, max(1, int(round(clipped * 100))))
                        new_ranked.append((blended_score, orig_item))
                    ranked = new_ranked + tail_slice

                # Precision cutoff: when top results have strong lexical/concept matches, drop unrelated low-score tail noise
                if ranked and not sort_mode and not nl["has_strict_filters"] and not op_filters and not entity_filters:
                    top_blended = ranked[0][0]
                    has_strong_lex = any(
                        float(it.get("_lexScore", 0.0)) >= 0.28 or bool(it.get("_phraseHit"))
                        for _, it in ranked[:5]
                    )
                    if has_strong_lex and top_blended >= 0.45:
                        min_keep_score = max(0.44, top_blended * 0.68)
                        filtered_ranked = [
                            (sc, it)
                            for sc, it in ranked
                            if float(it.get("_lexScore", 0.0)) >= 0.36
                            or bool(it.get("_phraseHit"))
                            or sc >= min_keep_score
                        ]
                        if filtered_ranked:
                            ranked = filtered_ranked

                if sort_mode == "largest":
                    ranked.sort(
                        key=lambda x: (int(x[1].get("sizeBytes") or 0), x[0], float(x[1]["mtime"])),
                        reverse=True,
                    )
                elif sort_mode == "smallest":
                    ranked.sort(
                        key=lambda x: (int(x[1].get("sizeBytes") or 0), -x[0], float(x[1]["mtime"])),
                    )
                elif sort_mode == "oldest":
                    ranked.sort(key=lambda x: (float(x[1]["mtime"]), -x[0]))
                elif sort_mode == "newest":
                    ranked.sort(key=lambda x: (float(x[1]["mtime"]), x[0]), reverse=True)
                elif sort_mode == "due_date":
                    ranked.sort(key=lambda x: (str((x[1].get("metadata") or {}).get("due_date") or "9999-12-31"), -x[0]))
                else:
                    ranked.sort(key=lambda x: (x[0], float(x[1]["mtime"])), reverse=True)

                # Collapse exact-content duplicates for UI search (disabled when `dedup_exact=False` for action resolution).
                final_results: list[dict[str, Any]] = []
                seen_content: set[str] = set()
                for _, item in ranked:
                    if dedup_exact:
                        key = item["sha256"] or item["path"]
                        if key in seen_content:
                            continue
                        seen_content.add(key)
                    # Strip internal reranker signals before returning to the frontend.
                    for _pk in ("_cosSim", "_visSim", "_lexScore", "_phraseHit", "_matchedTermsCount", "_affinityBoost", "_ftsScore"):
                        item.pop(_pk, None)
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
                    self._restore_file_to_search_indices(conn, file_id=fid)
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
                self._remove_file_from_search_indices(conn, file_id=fid)
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
                file_count = conn.execute("SELECT COUNT(*) AS c FROM files WHERE archived = 0").fetchone()["c"]
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
                            self._restore_file_to_search_indices(conn, file_id=arch["fileId"])
                            conn.execute("DELETE FROM archive_history WHERE pair_id = ?", (pair_id,))
                        else:
                            self._remove_file_from_search_indices(conn, file_id=arch["fileId"])
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
                        if reverse:
                            self._restore_file_to_search_indices(conn, file_id=it["fileId"])
                        else:
                            self._remove_file_from_search_indices(conn, file_id=it["fileId"])
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
                        self._restore_file_to_search_indices(conn, file_id=detail["fileId"])
                    else:
                        self._remove_file_from_search_indices(conn, file_id=detail["fileId"])
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

        if act_type == "multi_step_tx":
            child_ids = [int(cid) for cid in (detail.get("childActionIds") or []) if cid]
            ordered_ids = list(reversed(child_ids)) if reverse else child_ids
            summaries: list[str] = []
            for cid in ordered_ids:
                with self._lock:
                    conn = self._connect()
                    try:
                        crow = conn.execute("SELECT action_type, detail_json FROM action_log WHERE id = ?", (cid,)).fetchone()
                    finally:
                        conn.close()
                if not crow:
                    continue
                try:
                    c_det = json.loads(crow["detail_json"])
                    s_msg = self._apply_action_direction(str(crow["action_type"]), c_det, reverse=reverse)
                    summaries.append(s_msg)
                    with self._lock:
                        conn = self._connect()
                        try:
                            conn.execute("UPDATE action_log SET undone = ? WHERE id = ?", (1 if reverse else 0, cid))
                            conn.commit()
                        finally:
                            conn.close()
                except Exception:
                    continue
            verb = "Undid" if reverse else "Redid"
            lbl = detail.get("label") or f"{len(ordered_ids)}-step workflow"
            return f"{verb} {lbl}" + (f": {' · '.join(summaries[:4])}" if summaries else "")

        if act_type == "create_folder":
            folder_p = Path(str(detail.get("path") or ""))
            if reverse:
                if folder_p.exists() and folder_p.is_dir():
                    try:
                        if not any(folder_p.iterdir()):
                            folder_p.rmdir()
                    except OSError:
                        pass
                with self._lock:
                    conn = self._connect()
                    try:
                        conn.execute("DELETE FROM watched_folders WHERE path = ?", (str(folder_p),))
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                return f"Removed created folder {folder_p.name}"
            else:
                folder_p.mkdir(parents=True, exist_ok=True)
                with self._lock:
                    conn = self._connect()
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO watched_folders (path, display_path, active) VALUES (?, ?, 1)",
                            (str(folder_p), format_path_short(folder_p)),
                        )
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                return f"Recreated folder {folder_p.name}"

        if act_type in {"copy_file", "bulk_copy"}:
            copies = detail.get("copies", [])
            done = 0
            if reverse:
                with self._lock:
                    conn = self._connect()
                    try:
                        for cp in reversed(copies):
                            dst = Path(str(cp.get("toPath") or ""))
                            if dst.exists() and dst.is_file():
                                dst.unlink(missing_ok=True)
                            fid = str(cp.get("newFileId") or "")
                            if fid:
                                self._remove_file_from_search_indices(conn, file_id=fid)
                                conn.execute("DELETE FROM files WHERE id = ?", (fid,))
                            done += 1
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                return f"Removed {done} copied file(s)"
            else:
                for cp in copies:
                    src = Path(str(cp.get("fromPath") or ""))
                    dst = Path(str(cp.get("toPath") or ""))
                    if src.exists() and not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src), str(dst))
                        self.index_single_file(dst, force=True, emit_telemetry=False, recluster=False)
                        done += 1
                self._invalidate_cache()
                return f"Re-copied {done} file(s)"

        if act_type == "bulk_shelve":
            entries = detail.get("entries", [])
            done = 0
            with self._lock:
                conn = self._connect()
                try:
                    for ent in entries:
                        fid = str(ent.get("fileId") or "")
                        if not fid:
                            continue
                        st_obj = ent.get("before" if reverse else "after") or {}
                        conn.execute(
                            "UPDATE files SET user_shelf_id = ?, shelf_id = ?, shelf_name = ? WHERE id = ?",
                            (
                                str(st_obj.get("userShelfId") or ""),
                                str(st_obj.get("shelfId") or ""),
                                str(st_obj.get("shelfName") or ""),
                                fid,
                            ),
                        )
                        done += 1
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Undid' if reverse else 'Redid'} shelf assignment ({done} file(s))"

        if act_type == "create_shelf":
            sid = str(detail.get("shelfId") or "")
            sname = str(detail.get("name") or "")
            with self._lock:
                conn = self._connect()
                try:
                    if reverse:
                        conn.execute("DELETE FROM custom_shelves WHERE shelf_id = ?", (sid,))
                    else:
                        conn.execute(
                            "INSERT OR REPLACE INTO custom_shelves (shelf_id, name, desc, query, modality, tag, file_ids_json, pinned, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                            (
                                sid,
                                sname,
                                str(detail.get("desc") or ""),
                                str(detail.get("query") or ""),
                                str(detail.get("modality") or "all"),
                                str(detail.get("tag") or ""),
                                json.dumps(detail.get("fileIds") or []),
                                float(detail.get("createdAt") or time.time()),
                            ),
                        )
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Removed' if reverse else 'Recreated'} shelf “{sname}”"

        if act_type == "delete_shelf":
            sid = str(detail.get("shelfId") or "")
            sname = str(detail.get("name") or sid)
            with self._lock:
                conn = self._connect()
                try:
                    if reverse:
                        conn.execute(
                            "INSERT OR REPLACE INTO custom_shelves (shelf_id, name, desc, query, modality, tag, file_ids_json, pinned, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                sid,
                                sname,
                                str(detail.get("desc") or ""),
                                str(detail.get("query") or ""),
                                str(detail.get("modality") or "all"),
                                str(detail.get("tag") or ""),
                                str(detail.get("fileIdsJson") or "[]"),
                                int(detail.get("pinned", 1)),
                                float(detail.get("createdAt") or time.time()),
                            ),
                        )
                    else:
                        conn.execute("DELETE FROM custom_shelves WHERE shelf_id = ?", (sid,))
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Restored' if reverse else 'Deleted'} shelf “{sname}”"

        if act_type == "rename_shelf":
            sid = str(detail.get("shelfId") or "")
            target_name = str(detail.get("oldName" if reverse else "newName") or "")
            if sid and target_name:
                self.rename_shelf(sid, target_name, record_log=False)
            return f"{'Restored' if reverse else 'Renamed'} shelf to “{target_name}”"

        if act_type == "rename_folder":
            src_p = Path(str(detail.get("toPath" if reverse else "fromPath") or ""))
            dst_p = Path(str(detail.get("fromPath" if reverse else "toPath") or ""))
            if src_p.exists() and not dst_p.exists():
                src_p.rename(dst_p)
            src_prefix = str(src_p)
            dst_prefix = str(dst_p)
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute("SELECT id, path FROM files").fetchall()
                    for r in rows:
                        p = Path(r["path"])
                        if p == src_p or src_p in p.parents:
                            new_p = Path(dst_prefix + str(p)[len(src_prefix):])
                            conn.execute(
                                "UPDATE files SET path = ?, folder = ?, folder_short = ? WHERE id = ?",
                                (str(new_p), str(new_p.parent), format_path_short(new_p.parent), r["id"]),
                            )
                    conn.execute(
                        "UPDATE watched_folders SET path = ?, display_path = ? WHERE path = ?",
                        (dst_prefix, format_path_short(dst_p), src_prefix),
                    )
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Restored' if reverse else 'Renamed'} folder to “{dst_p.name}”"

        if act_type == "move_folder":
            f_src = Path(str(detail.get("toPath" if reverse else "fromPath") or ""))
            f_dst = Path(str(detail.get("fromPath" if reverse else "toPath") or ""))
            if f_src.exists():
                f_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f_src), str(f_dst))
            src_prefix = str(f_src)
            dst_prefix = str(f_dst)
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute("SELECT id, path FROM files").fetchall()
                    for r in rows:
                        p = Path(r["path"])
                        if p == f_src or f_src in p.parents:
                            new_p = Path(dst_prefix + str(p)[len(src_prefix):])
                            conn.execute(
                                "UPDATE files SET path = ?, folder = ?, folder_short = ? WHERE id = ?",
                                (str(new_p), str(new_p.parent), format_path_short(new_p.parent), r["id"]),
                            )
                    conn.execute(
                        "UPDATE watched_folders SET path = ?, display_path = ? WHERE path = ?",
                        (dst_prefix, format_path_short(f_dst), src_prefix),
                    )
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Undid' if reverse else 'Redid'} folder move “{f_dst.name}”"

        if act_type == "restore_trash":
            items = detail.get("items", [])
            done = 0
            with self._lock:
                conn = self._connect()
                try:
                    for it in (reversed(items) if reverse else items):
                        src = Path(str(it["toPath"] if reverse else it["fromPath"]))
                        dst = Path(str(it["fromPath"] if reverse else it["toPath"]))
                        if src.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(src), str(dst))
                        conn.execute(
                            "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ?, archived = ?, original_path = ? WHERE id = ?",
                            (
                                str(dst),
                                dst.name,
                                str(dst.parent),
                                format_path_short(dst.parent),
                                1 if reverse else 0,
                                str(it["toPath"]) if reverse else "",
                                it["fileId"],
                            ),
                        )
                        if reverse:
                            self._remove_file_from_search_indices(conn, file_id=it["fileId"])
                        else:
                            self._restore_file_to_search_indices(conn, file_id=it["fileId"])
                        done += 1
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Returned to Trash' if reverse else 'Restored'} {done} file(s)"

        if act_type == "archive_files":
            items = detail.get("items", [])
            done = 0
            with self._lock:
                conn = self._connect()
                try:
                    for it in (reversed(items) if reverse else items):
                        src = Path(str(it["toPath"] if reverse else it["fromPath"]))
                        dst = Path(str(it["fromPath"] if reverse else it["toPath"]))
                        if src.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(src), str(dst))
                        conn.execute(
                            "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ?, archived = ?, original_path = ? WHERE id = ?",
                            (
                                str(dst),
                                dst.name,
                                str(dst.parent),
                                format_path_short(dst.parent),
                                0 if reverse else 1,
                                "" if reverse else str(it["fromPath"]),
                                it["fileId"],
                            ),
                        )
                        if reverse:
                            self._restore_file_to_search_indices(conn, file_id=it["fileId"])
                        else:
                            self._remove_file_from_search_indices(conn, file_id=it["fileId"])
                        done += 1
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Unarchived' if reverse else 'Archived'} {done} file(s)"

        if act_type in {"pin_folder", "unpin_folder"}:
            fp = str(detail.get("path") or "")
            target_pin = bool(detail.get("before" if reverse else "after"))
            if fp and Path(fp).exists():
                self.toggle_pin_folder(fp, pinned=target_pin, record_log=False)
            return f"{'Pinned' if target_pin else 'Unpinned'} folder {Path(fp).name}"

        if act_type == "remember":
            kind_m = str(detail.get("kind") or "user_rule")
            key_p = str(detail.get("keyPattern") or "")
            val_t = str(detail.get("value") or "")
            with self._lock:
                conn = self._connect()
                try:
                    if reverse:
                        conn.execute(
                            "DELETE FROM ai_memory WHERE kind = ? AND key_pattern = ? AND value = ?",
                            (kind_m, key_p.lower(), val_t),
                        )
                    else:
                        upsert_ai_memory(conn, kind_m, key_p, val_t, delta_weight=2.0)
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return f"{'Removed' if reverse else 'Restored'} AI memory rule"

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

        if adaptive_learning:
            try:
                from ai_training_feeder import seed_foundational_ai_knowledge
                seed_foundational_ai_knowledge(self, include_default_routes=False)
            except Exception:
                pass

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
        """Learn user query -> file/shelf preferences over time locally (including thumbs_up / thumbs_down)."""
        if not self.get_settings().get("adaptive_learning", True):
            return {"ok": True, "recorded": False}
        act_low = (action or "select").strip().lower()
        tokens = [
            t
            for t in _search_tokens(query)
            if len(t) >= 2 and t not in _SEARCH_STOPWORDS
        ]
        if not tokens and act_low not in ("thumbs_up", "helpful", "thumbs_down", "unhelpful"):
            return {"ok": True, "recorded": False}
        if act_low in ("thumbs_up", "helpful"):
            delta = 3.5
        elif act_low in ("thumbs_down", "unhelpful"):
            delta = -2.0
        elif act_low in ("open", "move", "pin"):
            delta = 1.6
        else:
            delta = 1.0
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                resolved_shelf = shelf_id
                file_name = ""
                if file_id:
                    row = conn.execute("SELECT shelf_id, name FROM files WHERE id = ?", (file_id,)).fetchone()
                    if row:
                        if not resolved_shelf:
                            resolved_shelf = str(row["shelf_id"] or "")
                        file_name = str(row["name"] or "")
                for tok in tokens[:8]:
                    for key_tok in {tok, _search_stem(tok)}:
                        if file_id:
                            conn.execute(
                                "INSERT INTO user_affinities (token, target_id, kind, weight, updated_at) "
                                "VALUES (?, ?, 'file', ?, ?) "
                                "ON CONFLICT(token, target_id, kind) DO UPDATE SET "
                                "weight = MAX(-5.0, weight + excluded.weight), updated_at = excluded.updated_at",
                                (key_tok, file_id, delta, now),
                            )
                        if resolved_shelf:
                            conn.execute(
                                "INSERT INTO user_affinities (token, target_id, kind, weight, updated_at) "
                                "VALUES (?, ?, 'shelf', ?, ?) "
                                "ON CONFLICT(token, target_id, kind) DO UPDATE SET "
                                "weight = MAX(-5.0, weight + excluded.weight), updated_at = excluded.updated_at",
                                (key_tok, resolved_shelf, delta * 0.6, now),
                            )
                if act_low in ("thumbs_up", "helpful") and query.strip() and (file_name or file_id):
                    upsert_ai_memory(conn, "qa_feedback", query.strip().lower()[:60], f"helpful:{file_name or file_id}", delta_weight=1.5)
                elif act_low in ("thumbs_down", "unhelpful") and query.strip() and (file_name or file_id):
                    upsert_ai_memory(conn, "qa_feedback", query.strip().lower()[:60], f"unhelpful:{file_name or file_id}", delta_weight=1.0)
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        return {"ok": True, "recorded": True, "action": act_low, "delta": delta}

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

            ocr_parts: list[str] = []
            confs: list[float] = []
            pages_ocrd = 0
            if src_path.suffix.lower() == ".pdf":
                import pymupdf

                with _FITZ_LOCK:
                    doc = pymupdf.open(str(src_path))
                    try:
                        if len(doc) == 0:
                            return {"ok": False, "message": "Cannot run OCR on an empty PDF", "ocrText": ""}
                        # Multi-page PDF OCR: scan up to 12 text-less/low-text pages (not just page 0!)
                        target_pages: list[int] = []
                        for p_idx in range(len(doc)):
                            emb_txt = doc[p_idx].get_text("text").strip()
                            if len(emb_txt) < 40:
                                target_pages.append(p_idx)
                            if len(target_pages) >= 12:
                                break
                        if not target_pages:
                            target_pages = [0]
                        for p_idx in target_pages:
                            pix = doc[p_idx].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
                            page_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                            try:
                                p_txt, p_conf = run_ocr_on_image(page_img)
                                if p_txt.strip():
                                    ocr_parts.append(p_txt.strip())
                                    confs.append(float(p_conf))
                                pages_ocrd += 1
                            finally:
                                try:
                                    page_img.close()
                                except Exception:
                                    pass
                    finally:
                        doc.close()
                ocr_txt = "\n\n".join(ocr_parts).strip()
                ocr_conf = (sum(confs) / len(confs)) if confs else 0.0
            else:
                img = None
                try:
                    try:
                        with Image.open(src_path) as raw_img:
                            img = raw_img.convert("RGB")
                    except Exception as exc:
                        raise ValueError(f"OCR is only supported for images and PDFs ({exc})")
                    ocr_txt, ocr_conf = run_ocr_on_image(img)
                    pages_ocrd = 1
                finally:
                    if img is not None:
                        try:
                            img.close()
                        except Exception:
                            pass

            with self._lock:
                conn = self._connect()
                try:
                    row_cur = conn.execute("SELECT rowid, * FROM files WHERE id = ?", (file_id,)).fetchone()
                    if row_cur:
                        existing_ext = str(row_cur["extracted_text"] or "")
                        new_ext = ocr_txt if not existing_ext.strip() else existing_ext
                        try:
                            meta_obj = json.loads(row_cur["metadata_json"] or "{}")
                        except Exception:
                            meta_obj = {}
                        ent_text = f"{ocr_txt}\n{new_ext}".strip()
                        if ent_text:
                            meta_obj = extract_entities(ent_text, filename=str(row_cur["name"] or ""), existing_metadata=meta_obj)
                        new_meta_json = json.dumps(meta_obj, ensure_ascii=False)
                        conn.execute(
                            "UPDATE files SET ocr_text = ?, ocr_confidence = ?, extracted_text = ?, metadata_json = ? WHERE id = ?",
                            (ocr_txt, ocr_conf, new_ext, new_meta_json, file_id),
                        )
                        # Refresh FTS5 index so newly extracted OCR text is immediately searchable via BM25
                        rid = int(row_cur["rowid"])
                        try:
                            conn.execute("DELETE FROM files_fts WHERE rowid = ?", (rid,))
                            conn.execute(
                                "INSERT INTO files_fts(rowid, title, name, extracted_text, ocr_text, shelf_name, tags_json) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    rid,
                                    str(row_cur["title"] or ""),
                                    str(row_cur["name"] or ""),
                                    new_ext,
                                    ocr_txt,
                                    str(row_cur["shelf_name"] or ""),
                                    str(row_cur["tags_json"] or ""),
                                ),
                            )
                        except Exception:
                            pass
                        # Re-embed text subspace while preserving existing visual subspace if present
                        embed_text = " · ".join(p for p in [str(row_cur["title"] or ""), str(row_cur["name"] or ""), ocr_txt, new_ext[:1500]] if p)
                        txt_384 = self.embedder._encode_text_384(embed_text[:1500])
                        vis_128 = np.zeros(128, dtype=np.float32)
                        if row_cur["embedding_blob"]:
                            try:
                                prev_vec = unpack_f32(row_cur["embedding_blob"])
                                if prev_vec.shape[0] == 512:
                                    v_slice = prev_vec[384:512]
                                    vn = float(np.linalg.norm(v_slice))
                                    if vn > 1e-6:
                                        vis_128 = (v_slice / vn).astype(np.float32)
                            except Exception:
                                pass
                        has_vis = float(np.linalg.norm(vis_128)) > 1e-6
                        if has_vis:
                            w_t, w_v = np.sqrt(0.80), np.sqrt(0.20)
                            new_vec = np.concatenate([w_t * txt_384, w_v * vis_128]).astype(np.float32)
                        else:
                            new_vec = np.concatenate([txt_384, vis_128]).astype(np.float32)
                        vn_all = float(np.linalg.norm(new_vec))
                        if vn_all > 1e-9:
                            new_vec = (new_vec / vn_all).astype(np.float32)
                        new_blob = pack_f32(new_vec)
                        conn.execute("UPDATE files SET embedding_blob = ?, vector_norm = ? WHERE rowid = ?", (new_blob, round(float(np.linalg.norm(new_vec)), 4), rid))
                        conn.execute("DELETE FROM vec_files WHERE rowid = ?", (rid,))
                        conn.execute("INSERT INTO vec_files(rowid, embedding) VALUES (?, ?)", (rid, new_blob))
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            page_note = f" across {pages_ocrd} page(s)" if pages_ocrd > 1 else ""
            return {
                "ok": True,
                "message": f"OCR extracted {len(ocr_txt.split())} words{page_note} ({ocr_conf:.0f}% conf)",
                "ocrText": ocr_txt,
                "pagesOcrd": pages_ocrd,
            }

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
                            self._remove_file_from_search_indices(conn, file_id=fid)
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

    def create_folder(self, parent_path: str, folder_name: str, watch: bool = True, record_log: bool = True) -> dict[str, Any]:
        """Create a new folder inside a parent path, watch it, and record an undoable action."""
        clean_name = re.sub(r'[<>:"/\\|?*]+', "_", folder_name).strip(" .")
        if not clean_name:
            raise ValueError("Folder name cannot be empty")
        parent = Path(parent_path).expanduser().resolve()
        if not parent.exists() or not parent.is_dir():
            raise ValueError("Parent folder does not exist")
        if parent.parent == parent:
            raise ValueError("Cannot create folders in the filesystem root")
        target = parent / clean_name
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        if watch:
            try:
                self.add_watched_folder(str(target))
            except Exception:
                pass
        if record_log and not existed:
            self._record_action(
                "create_folder",
                f"Created folder “{clean_name}”",
                {"label": f"Create folder {clean_name}", "path": str(target), "name": clean_name},
            )
        return {"ok": True, "path": str(target), "name": clean_name}

    def copy_file(self, file_id: str, target_folder: str, record_log: bool = True) -> dict[str, Any]:
        """Copy an indexed file into target_folder, index the new copy, and optionally log for undo."""
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
        new_fid = str((new_item or {}).get("id") or "")
        if record_log:
            self._record_action(
                "copy_file",
                f"Copied {src.name} to {format_path_short(dst_dir)}",
                {
                    "label": f"Copy {src.name}",
                    "copies": [{"fromPath": str(src), "toPath": str(dst), "newFileId": new_fid}],
                },
            )
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
                self._remove_file_from_search_indices(conn, file_id=file_id)
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
                # Preserve original extension if user omitted it (unless repairing a corrupted extension to true_ext)
                true_ext = f".{str(row['ext'] or '').lower().lstrip('.')}"
                if cur_path.suffix and not clean.lower().endswith((cur_path.suffix.lower(), true_ext)):
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
        if not reveal and p.is_file() and p.suffix.lower() in {
            ".appimage",
            ".bat",
            ".cmd",
            ".com",
            ".dll",
            ".desktop",
            ".exe",
            ".jar",
            ".lnk",
            ".msi",
            ".ps1",
            ".scr",
            ".sh",
            ".so",
            ".vbs",
        }:
            raise ValueError("Refusing to launch executable file types")
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
                        self._remove_file_from_search_indices(conn, file_id=fid)
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

    def toggle_pin_folder(self, folder_path: str, pinned: Optional[bool] = None, record_log: bool = False) -> dict[str, Any]:
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
                was_pinned = existing is not None
                should_pin = (not was_pinned) if pinned is None else bool(pinned)
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
            finally:
                conn.close()
        if record_log:
            act_t = "pin_folder" if should_pin else "unpin_folder"
            self._record_action(
                act_t,
                f"{'Pinned' if should_pin else 'Unpinned'} folder “{resolved.name or p_str}”",
                {"label": f"{'Pin' if should_pin else 'Unpin'} {resolved.name}", "path": p_str, "before": was_pinned, "after": should_pin},
            )
        return {"path": p_str, "name": resolved.name or p_str, "pinned": should_pin}

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
        """
        Continuously learn semantic routing, tag, and exclusion preferences from real confirmations.
        Hygiene rule: NEVER poison global memory with bare file extensions (like `pdf` or `png`)
        as routes or exclusions — require semantic topic/specimen/folder keys.
        """
        if not self.get_settings().get("adaptive_learning", True):
            return
        unselected_ids = [str(fid) for fid in (req.get("unselectedIds") or []) if fid]
        target_folder = str(req.get("targetFolder") or "").strip()
        tag_val = str(req.get("tag") or "").strip()
        source_query = str(req.get("sourceQuery") or "").strip()
        _GENERIC_EXT_TOKENS = {
            "pdf", "pdfs", "png", "pngs", "jpg", "jpgs", "jpeg", "txt", "doc", "docs", "docx",
            "xlsx", "xls", "csv", "pptx", "ppt", "md", "json", "zip", "mp4", "mp3", "wav",
            "file", "files", "item", "items", "folder", "move", "copy", "these", "those", "them",
        }

        with self._lock:
            conn = self._connect()
            try:
                if action_type in {"move", "copy"} and target_folder and file_ids:
                    placeholders = ",".join("?" for _ in file_ids[:20])
                    rows = conn.execute(
                        f"SELECT name, specimen_type, modality, ext, metadata_json FROM files WHERE id IN ({placeholders})",
                        file_ids[:20],
                    ).fetchall()
                    learned_keys: set[str] = set()
                    for tok in _search_tokens(source_query):
                        if len(tok) >= 4 and tok not in _SEARCH_STOPWORDS and tok not in _GENERIC_EXT_TOKENS:
                            learned_keys.add(tok)
                    # Learn semantic specimen / doc_type only when homogeneous across moved files
                    specs = {str(r["specimen_type"] or "").lower() for r in rows if str(r["specimen_type"] or "").lower() in {"receipt", "diagram", "idcard"}}
                    if len(specs) == 1:
                        learned_keys.update(specs)
                    doc_types: set[str] = set()
                    for r in rows:
                        try:
                            m = json.loads(r["metadata_json"] or "{}")
                            if m.get("doc_type"):
                                doc_types.add(str(m["doc_type"]).lower())
                        except Exception:
                            pass
                    if len(doc_types) == 1:
                        learned_keys.update(doc_types)
                    for k in list(learned_keys)[:4]:
                        upsert_ai_memory(conn, "route", k, target_folder, delta_weight=1.2)

                if action_type == "tag" and tag_val and file_ids:
                    for tok in _search_tokens(source_query)[:4]:
                        if len(tok) >= 4 and tok not in _SEARCH_STOPWORDS and tok not in _GENERIC_EXT_TOKENS:
                            upsert_ai_memory(conn, "tag_pref", tok, tag_val, delta_weight=1.0)

                if unselected_ids:
                    placeholders = ",".join("?" for _ in unselected_ids[:15])
                    skipped_rows = conn.execute(
                        f"SELECT specimen_type, folder FROM files WHERE id IN ({placeholders})",
                        unselected_ids[:15],
                    ).fetchall()
                    # Only record semantic specimen or folder skip patterns (never bare extensions!)
                    folder_skips: dict[str, int] = {}
                    for sr in skipped_rows:
                        fld_name = Path(str(sr["folder"] or "")).name.lower()
                        if fld_name and len(fld_name) >= 3:
                            folder_skips[fld_name] = folder_skips.get(fld_name, 0) + 1
                    for fld_k, cnt in folder_skips.items():
                        if cnt >= 2:
                            upsert_ai_memory(conn, "exclude", f"folder:{fld_k}", "skip", delta_weight=0.7)

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
        try:
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
        finally:
            self.emit_progress("idle", max(1, total), max(1, total), "")
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
        raw_snip = snippet or default_snip
        cleaned_snip = clean_extracted_text(raw_snip, preserve_newlines=False)
        cite: dict[str, Any] = {
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
        m_page = re.search(r"\[Page\s+(\d+)\]|\bPage\s+(\d+)\b", raw_snip, re.IGNORECASE)
        if m_page:
            cite["pageNumber"] = int(m_page.group(1) or m_page.group(2))
        m_slide = re.search(r"\[Slide\s+(\d+)\]|\bSlide\s+(\d+)\b", raw_snip, re.IGNORECASE)
        if m_slide:
            cite["slideNumber"] = int(m_slide.group(1) or m_slide.group(2))
        m_line = re.search(r"\[Line\s+(\d+)\]|\bL(\d+)\s*\((?:TODO|FIXME|HACK)\)", raw_snip, re.IGNORECASE)
        if m_line:
            cite["lineNumber"] = int(m_line.group(1) or m_line.group(2))
        m_sheet = re.search(r"\[Sheet\s+([^\]]+)\]", raw_snip, re.IGNORECASE)
        if m_sheet:
            cite["sheetName"] = m_sheet.group(1).strip()
        return cite

    def build_ai_ingestion_report(self, file_id: str, apply_auto_rules: bool = True) -> dict[str, Any]:
        """
        Proactive Autonomous File-Drop & Watch Copilot:
        When a file is dropped or ingested, automatically synthesize:
          - Smart shelf & sub-shelf placement
          - Smart auto-tags + any matching user `watch_rule` / `tag_pref` from `ai_memory`
          - Suggested content-based filename
          - Extracted entities & key takeaways
          - Cross-file knowledge graph links & duplicate check
        """
        f = self.get_file_by_id(file_id)
        if not f:
            return {"fileId": file_id, "ok": False}

        sugg_tags = self._suggest_auto_tags_for_file(f)
        state = self.get_all_state()
        mem_rows = list(state.get("aiMemory") or [])
        all_active = [x for x in (state.get("files") or []) if not x.get("archived")]

        matched_rules: list[str] = []
        rule_tags: list[str] = []
        f_hay = f"{f.get('name', '')} {f.get('title', '')} {f.get('format', '')} {f.get('modality', '')} {f.get('specimen', '')} {json.dumps(f.get('metadata') or {})}".lower()
        for m in mem_rows:
            kind = str(m.get("kind") or "")
            kp = str(m.get("keyPattern") or "").lower().strip()
            val = str(m.get("value") or "").strip()
            if not kp or not val:
                continue
            if kind in {"watch_rule", "tag_pref"} and (kp in f_hay or kp in {"all", "dropped", "any"}):
                matched_rules.append(f"{kp} → {val}")
                for t_cand in re.findall(r"#?([a-z0-9_\-]{2,24})", val.lower()):
                    if t_cand not in {"tag", "auto", "with", "move", "to", "in"} and t_cand not in rule_tags:
                        rule_tags.append(t_cand)

        existing_tags = [str(t).lower() for t in (f.get("tags") or []) if str(t).strip()]
        merged_tags = list(existing_tags)
        applied_tags: list[str] = []
        for t in [*rule_tags, *sugg_tags][:4]:
            if t not in merged_tags:
                merged_tags.append(t)
                applied_tags.append(t)

        if apply_auto_rules and applied_tags:
            try:
                self.set_file_tags(file_id, merged_tags)
                f = self.get_file_by_id(file_id) or f
            except Exception:
                pass

        dossier = build_file_context_dossier(self, f)
        graph = build_cross_file_knowledge_graph(self, f, all_active)
        dup = self.check_duplicate_for_file(file_id)
        connected_summary = [
            {
                "fileId": c["fileId"],
                "fileName": c["fileName"],
                "title": c["title"],
                "relation": c["relation"],
                "detail": c["detail"],
            }
            for c in (graph.get("connectedFiles") or [])[:4]
        ]
        ent = dossier.get("entities") or {}
        summary_parts = [f"Shelved in **{f.get('shelfName') or 'Workspace'}**"]
        if ent.get("doc_type"):
            summary_parts.append(f"Type: {ent['doc_type']}")
        if ent.get("merchant"):
            summary_parts.append(f"Merchant: {ent['merchant']}")
        if ent.get("amount") is not None:
            summary_parts.append(f"Amount: {ent.get('currency') or ''} {float(ent['amount']):,.2f}".strip())
        if applied_tags:
            summary_parts.append("Auto-tagged: " + " ".join(f"#{t}" for t in applied_tags))
        if f.get("suggestedName") and not f.get("renamed"):
            summary_parts.append(f"Suggested rename: `{f['suggestedName']}`")

        return {
            "ok": True,
            "fileId": file_id,
            "fileName": f["name"],
            "title": f["title"],
            "format": f["format"],
            "modality": f["modality"],
            "shelfId": f["shelfId"],
            "shelfName": f["shelfName"],
            "subShelf": f.get("subShelf", ""),
            "suggestedName": f.get("suggestedName"),
            "renameReason": f.get("renameReason"),
            "autoTagsApplied": applied_tags,
            "tags": f.get("tags", []),
            "matchedRules": matched_rules,
            "entities": ent,
            "keyTakeaways": (dossier.get("keyTakeaways") or [])[:3],
            "connectedFiles": connected_summary,
            "duplicateOf": dup,
            "summaryLine": " · ".join(summary_parts),
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
        """Generate top-3 highest-leverage clickable follow-up & refinement choices (UI displays .slice(0, 3))."""
        choices: list[dict[str, str]] = []
        if proposed_action:
            items = proposed_action.get("items") or []
            if len(items) > 2 and proposed_action.get("requiresSelection"):
                fmts = {str(it.get("format") or "").upper() for it in items}
                if "PDF" in fmts and len(fmts) > 1:
                    choices.append({"label": "Only PDFs", "prompt": "only include pdfs", "kind": "clarify"})
                if any(it.get("modality") == "image" for it in items) and len(fmts) > 1:
                    choices.append({"label": "Exclude images", "prompt": "exclude images", "kind": "clarify"})
                if any(it.get("selectedByDefault") is False for it in items):
                    choices.append({"label": "Why were some skipped?", "prompt": "why did you pick these?", "kind": "clarify"})
            return choices[:3]

        if citations:
            if intent_mode == "deep_context":
                fn0 = citations[0].get("fileName") or citations[0].get("title") or "this file"
                choices.append({"label": "Find related files", "prompt": f"find files similar to {fn0}", "kind": "followup"})
                choices.append({"label": "Auto-tag file", "prompt": f"auto-tag {fn0}", "kind": "action"})
                choices.append({"label": "Smart rename from content", "prompt": f"smart rename {fn0} based on its content", "kind": "action"})
            elif intent_mode in ("list", "count", "due_soon", "timeline") or len(citations) >= 2:
                choices.append({"label": "Summarize key points", "prompt": "summarize these files", "kind": "followup"})
                choices.append({"label": "Deep context inspect", "prompt": "explain these files", "kind": "followup"})
                choices.append({"label": "Auto-tag these files", "prompt": "auto-tag these files", "kind": "action"})
            elif len(citations) == 1:
                fn = citations[0].get("fileName") or citations[0].get("title") or "this file"
                choices.append({"label": f"Explain {fn[:18]}", "prompt": f"explain {fn}", "kind": "followup"})
                choices.append({"label": "Find related files", "prompt": f"find files similar to {citations[0].get('title', fn)}", "kind": "followup"})
                choices.append({"label": "Auto-tag file", "prompt": f"auto-tag {fn}", "kind": "action"})
        elif any(w in q_low for w in ("hello", "hi", "hey", "what can you do", "help")):
            choices = [
                {"label": "What's on my shelves?", "prompt": "what's on my shelves?", "kind": "followup"},
                {"label": "Any duplicates?", "prompt": "show me all the duplicates", "kind": "action"},
                {"label": "How much space?", "prompt": "how much space am I using?", "kind": "followup"},
            ]
        return choices[:3]

    def _get_known_dirs_depth4(
        self,
        files: list[dict[str, Any]],
        watched_folders: list[dict[str, Any]],
    ) -> list[Path]:
        """Collect all indexed folders & subfolders up to depth 4 (cached 30s)."""
        now = time.monotonic()
        if self._folder_tree_cache is not None and (now - self._folder_tree_cache[0]) < 30.0:
            return list(self._folder_tree_cache[1])

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

        def _walk_depth4(root: Path) -> None:
            if not root.exists() or not root.is_dir():
                return
            _add_dir(root)
            stack: list[tuple[Path, int]] = [(root, 0)]
            visited = 0
            while stack and visited < 500:
                cur, depth = stack.pop()
                if depth >= 4:
                    continue
                try:
                    for ch in cur.iterdir():
                        if ch.is_dir() and not ch.name.startswith(".") and ch.name not in {".git", "node_modules", "__pycache__", "target", "dist"}:
                            visited += 1
                            _add_dir(ch)
                            if depth + 1 < 4:
                                stack.append((ch, depth + 1))
                except OSError:
                    continue

        for wf in watched_folders:
            _walk_depth4(Path(wf["path"]))
        shelves_root = Path.home() / "Shelves"
        if shelves_root.exists() and shelves_root.is_dir():
            _walk_depth4(shelves_root)
        for f in files:
            ff = f.get("folderFull")
            if ff:
                _add_dir(Path(ff))

        self._folder_tree_cache = (now, known_dirs)
        return list(known_dirs)

    def _resolve_folder_candidate(
        self,
        hint: str,
        files: list[dict[str, Any]],
        watched_folders: list[dict[str, Any]],
        allow_create_sub: bool = False,
        preferred_parent: Optional[Path] = None,
        source_files: Optional[list[dict[str, Any]]] = None,
        conv_ctx: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        clean = re.sub(r'^(?:a |an |the )?(?:new\s+)?folder\s+(?:called|named)\s+', "", (hint or "").strip(), flags=re.IGNORECASE)
        clean = re.sub(r'^(?:the\s+)?folder\s+', "", clean, flags=re.IGNORECASE).strip(" \"'“”`?!.,")
        clean = re.sub(r'\s+folder$', "", clean, flags=re.IGNORECASE).strip()
        if not clean:
            return None

        src_f0 = (source_files or [None])[0] or (conv_ctx or {}).get("primaryFile")
        src_folder_resolved = ""
        if src_f0:
            try:
                src_folder_resolved = str(Path(src_f0.get("folderFull") or Path(src_f0["path"]).parent).resolve())
            except Exception:
                src_folder_resolved = ""

        # 0. Deictic / contextual destination resolution ("that", "there", "that folder", "where it belongs", "its shelf")
        if is_deictic_folder_phrase(clean):
            clean_low = clean.lower()
            if any(k in clean_low for k in ("belong", "shelf", "right", "proper", "correct")):
                shelf_dir = infer_shelf_folder_for_file(src_f0)
                if shelf_dir:
                    return {
                        "path": str(shelf_dir),
                        "label": format_path_short(shelf_dir),
                        "exists": shelf_dir.exists(),
                        "willCreate": not shelf_dir.exists(),
                    }
            if conv_ctx:
                for mf_path in conv_ctx.get("mentionedFolders") or []:
                    if mf_path != src_folder_resolved and Path(mf_path).exists():
                        return {
                            "path": mf_path,
                            "label": format_path_short(Path(mf_path)),
                            "exists": True,
                            "willCreate": False,
                        }
                sec_f = conv_ctx.get("secondaryFile")
                if sec_f and sec_f.get("folderFull"):
                    sec_dir = Path(str(sec_f["folderFull"])).resolve()
                    if str(sec_dir) != src_folder_resolved and sec_dir.exists():
                        return {
                            "path": str(sec_dir),
                            "label": format_path_short(sec_dir),
                            "exists": True,
                            "willCreate": False,
                        }
            shelf_dir = infer_shelf_folder_for_file(src_f0)
            if shelf_dir and str(shelf_dir) != src_folder_resolved:
                return {
                    "path": str(shelf_dir),
                    "label": format_path_short(shelf_dir),
                    "exists": shelf_dir.exists(),
                    "willCreate": not shelf_dir.exists(),
                }
            arch_dir = (get_user_special_dir("Documents") / "GoodShelves_Archive").resolve()
            return {
                "path": str(arch_dir),
                "label": format_path_short(arch_dir),
                "exists": arch_dir.exists(),
                "willCreate": not arch_dir.exists(),
            }

        # Direct filesystem path when user explicitly provides /, ~, or Windows drive
        if clean.startswith(("/", "~")) or (len(clean) >= 3 and clean[1:3] in (":\\", ":/")):
            cand = Path(clean).expanduser().resolve()
            if cand.exists() and cand.is_dir():
                return {"path": str(cand), "label": format_path_short(cand), "exists": True, "willCreate": False}
            if allow_create_sub and cand.parent.exists():
                return {"path": str(cand), "label": format_path_short(cand), "exists": False, "willCreate": True}

        low = clean.lower()
        low_slash = low.replace("\\", "/").strip("/")
        low_slug = re.sub(r"[^a-z0-9]+", "", low)
        known_dirs = self._get_known_dirs_depth4(files, watched_folders)
        shelves_root = Path.home() / "Shelves"

        # 1. Exact folder name, nested relative path suffix ("2026/Taxes"), or collapsed slug ("tax2026" -> "Tax 2026")
        for d in known_dirs:
            if not (d.exists() and d.is_dir()):
                continue
            d_low = d.name.lower()
            d_posix = str(d).replace("\\", "/").lower()
            d_slug = re.sub(r"[^a-z0-9]+", "", d_low)
            if (
                d_low == low
                or d_low.replace("-", " ") == low
                or d_low.replace("_", " ") == low
                or (low_slug and len(low_slug) >= 3 and d_slug == low_slug)
                or d_posix.endswith(f"/{low_slash}")
                or format_path_short(d).replace("\\", "/").lower().endswith(f"/{low_slash}")
            ):
                return {"path": str(d), "label": format_path_short(d), "exists": True, "willCreate": False}

        # 1b. Canonical Shelf / Topic folder alias ("receipts", "finance", "docs", "code", "diagrams", "archive", etc.)
        if low in {"archive", "archived", "goodshelves_archive"}:
            arch_p = (get_user_special_dir("Documents") / "GoodShelves_Archive").resolve()
            return {"path": str(arch_p), "label": format_path_short(arch_p), "exists": arch_p.exists(), "willCreate": not arch_p.exists()}
        if low in _SHELF_TOPIC_FOLDER_ALIASES:
            shelf_p = (shelves_root / _SHELF_TOPIC_FOLDER_ALIASES[low]).resolve()
            if shelf_p.exists() or shelves_root.exists():
                return {"path": str(shelf_p), "label": format_path_short(shelf_p), "exists": shelf_p.exists(), "willCreate": not shelf_p.exists()}

        # 2. Substring match on known directories
        for d in known_dirs:
            if low in d.name.lower() and d.exists() and d.is_dir():
                return {"path": str(d), "label": format_path_short(d), "exists": True, "willCreate": False}

        # 2b. Fuzzy typo match ("Finace" -> "Finance", "Documnts" -> "Documents")
        if len(low) >= 4 and "/" not in low_slash:
            existing_by_name = {d.name.lower(): d for d in known_dirs if d.exists() and d.is_dir()}
            close = difflib.get_close_matches(low, list(existing_by_name.keys()), n=1, cutoff=0.78)
            if close:
                d_hit = existing_by_name[close[0]]
                return {"path": str(d_hit), "label": format_path_short(d_hit), "exists": True, "willCreate": False}

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
            parts = [re.sub(r'[<>:"/\\|?*]+', "_", seg).strip(" .") for seg in clean.replace("\\", "/").split("/") if seg.strip(" .")]
            if not parts:
                return None
            base_parent = preferred_parent
            if base_parent is None and watched_folders:
                base_parent = Path(watched_folders[0]["path"]).expanduser().resolve()
            if base_parent is None and files:
                base_parent = Path(files[0].get("folderFull") or ".").expanduser().resolve()
            if base_parent is None:
                base_parent = get_user_special_dir("Documents").resolve()
            cand = base_parent
            for seg in parts:
                cand = cand / seg
            cand = cand.resolve()
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
        conv_ctx: Optional[dict[str, Any]] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Resolve natural-language file references using:
          1. Rich coreference & anaphora (`resolve_anaphora_subset`: 1st..10th, first 3, last 2,
             items 1 and 3, "the PDF one", "the largest one", "all except screenshots")
          2. Plural & singular deictics ("these", "them", "this", "that", "it")
          3. Unified `FileQuerySpec` AST (`query_parser.py`) with FULL SQLite text (no 600-char blindness!),
             multi-type union ("all images and PDFs"), dynamic folders/shelves, dates, sizes, tags,
             untagged, pinned, entities, negations, sorting, and top-K!
          4. Title-priority & exact filename match, full-text lexical match, and `dedup_exact=False` semantic search.
        """
        all_files: list[dict[str, Any]] = [f for f in state.get("files", []) if not f.get("archived")]
        by_id = {f["id"]: f for f in all_files}
        phrase = (target_phrase or "").strip(" \"'“”`?!.,")
        phrase_low = phrase.lower()
        meta: dict[str, Any] = {"sourceFolder": "", "usedContext": False, "needsTargetClarification": False, "appliedFilters": []}
        ctx = conv_ctx or extract_conversation_context(state, None, selected_file_id, recent_file_ids)
        ctx_files: list[dict[str, Any]] = list(ctx.get("contextFiles") or [])

        # Context pool ordered by recent citations -> history context
        ordered_ctx = [by_id[fid] for fid in recent_file_ids if fid in by_id]
        for cf in ctx_files:
            if cf["id"] in by_id and not any(x["id"] == cf["id"] for x in ordered_ctx):
                ordered_ctx.append(by_id[cf["id"]])

        # 0. Rich anaphora / coreference subsets ("items 1 and 3", "first 3", "the PDF one", "all except screenshots")
        if ordered_ctx:
            anaphora_hits = resolve_anaphora_subset(phrase, ordered_ctx)
            if anaphora_hits is not None:
                meta["usedContext"] = True
                return anaphora_hits, meta

        # Extract trailing/embedded 'from <folder>' / 'in <folder>' / 'inside <folder>' (only when not a temporal/shelf/merchant clause)
        source_folder_hint = ""
        folder_m = re.search(
            r"\b(?:from|in|inside)\s+(?:the\s+|my\s+)?(?:folder\s+(?:called|named)\s+)?([A-Za-z0-9 _.\-/~]+?)(?:\s+folder)?$",
            phrase,
            flags=re.IGNORECASE,
        )
        remainder = phrase
        if folder_m:
            cand_folder = folder_m.group(1).strip(" \"'“”.,")
            # Avoid stealing shelf phrases, dates, or exclusions ("except ...")
            if not re.search(
                r"\b(shelf|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{4}|last\s+week|last\s+month|today|yesterday|except|without|excluding)\b",
                cand_folder,
                flags=re.IGNORECASE,
            ):
                # Only strip as folder hint if it matches a real folder (or explicitly says "folder")
                cand_low = cand_folder.lower()
                is_known_f = (
                    "folder" in folder_m.group(0).lower()
                    or cand_low in {"downloads", "desktop", "documents", "pictures", "music", "videos"}
                    or any(
                        Path(str(f.get("folderFull") or "")).name.lower() == cand_low
                        or f"/{cand_low}" in str(f.get("folderFull") or "").replace("\\", "/").lower()
                        for f in all_files
                    )
                )
                if is_known_f:
                    source_folder_hint = cand_folder
                    meta["sourceFolder"] = source_folder_hint
                    remainder = phrase[:folder_m.start()].strip()

        rem_low = remainder.lower().strip()

        # 1. Plural deictic ("these", "those", "them", "these things", "all of them", "both")
        if re.fullmatch(
            r"(?:all\s+(?:of\s+)?|both\s+(?:of\s+)?)?(?:these|those|them|both)(?:\s+(?:ones|files|items|things|stuff|pictures|photos|images|docs|documents|pdfs|receipts))?",
            rem_low,
        ):
            meta["usedContext"] = True
            valid_recent = [by_id[fid] for fid in recent_file_ids if fid in by_id] or [by_id[f["id"]] for f in ctx_files if f["id"] in by_id]
            if source_folder_hint:
                valid_recent = self._filter_by_folder_hint(valid_recent, source_folder_hint)
            valid_recent = self._filter_by_modality_hint(valid_recent, rem_low)
            if valid_recent:
                return (valid_recent[:2] if "both" in rem_low else valid_recent), meta
            if selected_file_id and selected_file_id in by_id:
                return [by_id[selected_file_id]], meta
            if all_files:
                fallback_pool = self._filter_by_folder_hint(all_files, source_folder_hint) if source_folder_hint else all_files
                fallback_pool = self._filter_by_modality_hint(fallback_pool, rem_low)
                if fallback_pool:
                    return fallback_pool[:10], meta
            meta["needsTargetClarification"] = True
            return [], meta

        # 2. Singular deictic ("this", "that", "it", "this one", "that file", "the selected file")
        is_singular_deictic = bool(
            is_deictic_file_phrase(rem_low)
            or re.fullmatch(
                r"(?:the\s+)?(?:selected\s+|current\s+)?(?:this|that|it|one|selected)(?:\s+(?:one|file|item|thing|shit|stuff|picture|photo|image|pic|screenshot|pdf|doc|document|receipt|invoice))?",
                rem_low,
            )
            or re.match(r"^(?:this|that)\s+(?:one|file|item|thing|shit|stuff|picture|photo|image|pic|screenshot|pdf|doc|document|receipt|invoice)\b", rem_low)
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
            pool = [by_id[f["id"]] for f in ctx_files if f["id"] in by_id]
            if not pool:
                pool = [by_id[fid] for fid in recent_file_ids if fid in by_id]
            if not pool:
                pool = sorted(all_files, key=lambda x: float(x.get("mtime") or 0), reverse=True)
            if source_folder_hint:
                pool = self._filter_by_folder_hint(pool, source_folder_hint)
            pool = self._filter_by_modality_hint(pool, rem_low)
            if pool:
                if source_folder_hint and len(pool) <= 5 and not re.search(r"\b(one|it)\b", rem_low):
                    return pool, meta
                return [pool[0]], meta
            meta["needsTargetClarification"] = True
            return [], meta

        # 3. Unified `FileQuerySpec` AST evaluation!
        # Handles multi-type ("all images and PDFs"), dates, sizes, tags, untagged, pinned,
        # stale downloads, uncategorized/Quiet Tray, entities, negations ("except ..."), sort, top-K!
        spec = parse_file_query(phrase, state=state)
        if spec.applied_filters:
            meta["appliedFilters"] = list(spec.applied_filters)

        # Build duplicate ID sets only when requested
        dup_ids: Optional[set[str]] = None
        exact_dup_ids: Optional[set[str]] = None
        if spec.duplicates_only or spec.exact_duplicates_only:
            dup_ids = set()
            exact_dup_ids = set()
            for p in state.get("lookalikes") or []:
                if p.get("resolved"):
                    continue
                cid = str((p.get("copy") or {}).get("id") or "")
                kid = str((p.get("keep") or {}).get("id") or "")
                if cid:
                    dup_ids.add(cid)
                if kid:
                    dup_ids.add(kid)
                if p.get("isExact") or int(p.get("similarity_pct") or 0) == 100:
                    if cid:
                        exact_dup_ids.add(cid)
                    if kid:
                        exact_dup_ids.add(kid)

        need_full_text = bool(spec.include_phrases or spec.exclude_terms or spec.has_ocr is not None)
        full_text_map = load_full_texts_from_db(self, [str(f["id"]) for f in all_files]) if need_full_text else None

        # Pure filter/sort/top-K query with no leftover topic (e.g., "all images and PDFs",
        # "top 5 largest PDFs from last month", "stale downloads", "untagged files except pinned")
        if spec.has_strict_filters and not spec.topic_query:
            filtered = [
                f for f in all_files
                if matches_query_spec(
                    f,
                    spec,
                    full_text_map=full_text_map,
                    duplicate_ids=dup_ids,
                    exact_duplicate_ids=exact_dup_ids,
                )
            ]
            filtered = apply_sort_and_limit(filtered, spec)
            return filtered[:100], meta

        # Catch-all ("all files", "everything", "all files in Downloads", "top 5 largest files")
        clean_core = re.sub(r"^(?:all\s+(?:the\s+)?|every\s+|any\s+|the\s+|my\s+)", "", rem_low).strip()
        is_generic_bucket = clean_core in {
            "", "file", "files", "item", "items", "everything", "stuff", "things",
        }
        if is_generic_bucket or (spec.sort_by and not spec.topic_query):
            pool = list(all_files)
            if spec.has_strict_filters:
                pool = [f for f in pool if matches_query_spec(f, spec, full_text_map=full_text_map, duplicate_ids=dup_ids, exact_duplicate_ids=exact_dup_ids)]
            elif source_folder_hint:
                pool = self._filter_by_folder_hint(pool, source_folder_hint)
            pool = apply_sort_and_limit(pool, spec)
            return pool[:100], meta

        # Pre-filter candidate pool by strict AST constraints when combining filters + topic
        # (e.g., "receipts from March except Starbucks", "budget PDFs over 1MB")
        candidate_pool = all_files
        if spec.has_strict_filters:
            candidate_pool = [
                f for f in all_files
                if matches_query_spec(
                    f,
                    spec,
                    full_text_map=full_text_map,
                    duplicate_ids=dup_ids,
                    exact_duplicate_ids=exact_dup_ids,
                )
            ]
            if not candidate_pool:
                return [], meta
        elif source_folder_hint:
            candidate_pool = self._filter_by_folder_hint(all_files, source_folder_hint)

        search_term = (
            spec.topic_query
            if spec.has_strict_filters and spec.topic_query
            else (
                re.sub(
                    r"^(?:all\s+(?:the\s+)?|every\s+|any\s+|the\s+|my\s+)?(?:files?|items?|docs?|documents?|pictures?|photos?|images?|pdfs?)?\s*(?:about|named|called|matching|containing|with|regarding)?\s*",
                    "",
                    rem_low,
                ).strip()
                or clean_core
                or rem_low
            )
        )

        # 4. Stand firmly on file titles & filenames FIRST before falling back to body text
        title_hits = match_files_by_title_priority(search_term, candidate_pool)
        if not title_hits and remainder != search_term and not spec.has_strict_filters:
            title_hits = match_files_by_title_priority(remainder, candidate_pool)
        if title_hits:
            return apply_sort_and_limit(title_hits, spec)[:50], meta

        # 5. Exact or substring filename match
        exact_hits = [
            f
            for f in candidate_pool
            if f["name"].lower() == search_term
            or Path(f["name"]).stem.lower() == search_term
            or (len(search_term) >= 3 and search_term in f["name"].lower())
        ]
        if exact_hits:
            return apply_sort_and_limit(exact_hits, spec)[:50], meta

        # 6. Full-text lexical / stem match across SQLite full text (eliminates 600-char blindness!)
        term_tokens = [tok for tok in re.findall(r"[a-z0-9]{2,}", search_term) if tok not in {"file", "files", "item", "items", "folder"}]
        if term_tokens:
            if full_text_map is None:
                full_text_map = load_full_texts_from_db(self, [str(f["id"]) for f in candidate_pool])
            stems = [tok[:-1] if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss") else tok for tok in term_tokens]
            lex_hits: list[dict[str, Any]] = []
            for f in candidate_pool:
                ft_pair = full_text_map.get(str(f["id"]), ("", ""))
                hay = " ".join([
                    str(f.get("name", "")),
                    str(f.get("title", "")),
                    str(f.get("specimen", "")),
                    str(f.get("shelfName", "")),
                    " ".join(f.get("tags") or []),
                    json.dumps(f.get("metadata") or {}),
                    ft_pair[0] or str(f.get("extractedText", "")),
                    ft_pair[1] or str(f.get("ocrText", "")),
                ]).lower()
                if all(st_tok in hay for st_tok in stems):
                    lex_hits.append(f)
            if lex_hits:
                return apply_sort_and_limit(lex_hits, spec)[:50], meta

        # 7. Semantic search fallback with `dedup_exact=False` so exact duplicates aren't hidden from actions!
        search_res = self.search_semantic(
            search_term,
            modality_filter="all",
            limit=60,
            min_similarity=0.28,
            dedup_exact=False,
        )
        cand_ids = {str(f["id"]) for f in candidate_pool}
        cands = [f for f in search_res.get("results", []) if str(f["id"]) in cand_ids]
        return apply_sort_and_limit(cands, spec)[:50], meta

    @staticmethod
    def _suggest_auto_tags_for_file(file_dict: dict[str, Any]) -> list[str]:
        """Suggest clean semantic tags for a file from entities, year, modality, and visual concepts."""
        meta = file_dict.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        existing = {str(t).lower().strip() for t in (file_dict.get("tags") or []) if str(t).strip()}
        cands: list[str] = []

        def _push(raw_t: str) -> None:
            t = re.sub(r"[^a-z0-9\-]+", "-", (raw_t or "").strip().lower()).strip("-")
            if 2 <= len(t) <= 24 and t not in existing and t not in cands:
                cands.append(t)

        doc_type = str(meta.get("doc_type") or "").strip().lower()
        if doc_type:
            _push(doc_type)
        merchant = str(meta.get("merchant") or "").strip()
        if merchant and len(merchant) <= 24:
            _push(merchant.split()[0])
        if meta.get("due_date"):
            _push("bill")
        date_s = str(meta.get("date") or file_dict.get("createdDate") or "").strip()
        if re.match(r"^(19|20)\d{2}", date_s):
            _push(date_s[:4])
        mod = str(file_dict.get("modality") or "").lower()
        ext = str(file_dict.get("format") or "").lower()
        if mod == "code" and ext:
            _push(ext)
            _push("code")
        elif mod == "image":
            for vc in (meta.get("visualConcepts") or [])[:2]:
                vc_s = str(vc).lower()
                if vc_s not in {"landscape image", "portrait image", "square image", "portrait receipt"}:
                    _push(vc_s.split()[0])
            if "screenshot" in str(file_dict.get("name") or "").lower():
                _push("screenshot")
        elif ext == "pdf" and not cands:
            _push("document")
        if not cands:
            sh = re.sub(r"^\d+\s*", "", str(file_dict.get("shelfName") or "")).strip()
            if sh and sh.lower() not in {"quiet tray", "uncategorized"}:
                _push(sh.split()[0])
        return cands[:3]

    def _classify_file_to_best_shelf(
        self,
        file_dict: dict[str, Any],
        shelves: list[dict[str, Any]],
    ) -> tuple[str, str, int]:
        """Classify an uncategorized/Quiet Tray file to its best shelf `(shelfId, shelfName, matchPct)`."""
        # 1. Use clustering's semantic category classifier first
        meta = file_dict.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        try:
            cat_key = _classify_single_file_category(
                modality=str(file_dict.get("modality") or "document"),
                ext=str(file_dict.get("format") or "").lower(),
                name=str(file_dict.get("name") or ""),
                title=str(file_dict.get("title") or ""),
                extracted_text=str(file_dict.get("extractedText") or ""),
                ocr_text=str(file_dict.get("ocrText") or ""),
                specimen_type=str(file_dict.get("specimen") or ""),
                metadata=meta,
            )
        except Exception:
            cat_key = "documents"

        cat_entry = _GENERAL_CATEGORIES.get(cat_key) or _GENERAL_CATEGORIES.get("documents")
        best_sid = cat_entry[0] if cat_entry else "shelf_documents"
        best_name = cat_entry[1] if cat_entry else "Documents & Reports"
        best_pct = 86

        # Match existing renamed/active shelf if present
        for sh in shelves:
            sid = str(sh.get("id") or "")
            if sid == best_sid:
                best_name = str(sh.get("name") or best_name)
                best_pct = 90
                break
        return best_sid, best_name, best_pct

    def _route_file_auto_sort(
        self,
        file_dict: dict[str, Any],
        mem_list: list[dict[str, Any]],
        known_dirs: list[Path],
    ) -> Optional[Path]:
        """Compute individual destination folder for a file (learned route -> matching subfolder -> ~/Shelves/<topic>)."""
        # 1. Learned memory route
        learned = infer_learned_route(mem_list, str(file_dict.get("title") or ""), [file_dict])
        if learned:
            return Path(learned).resolve()
        # 2. Matching existing folder by doc_type / shelf topic inside watched tree
        meta = file_dict.get("metadata") or {}
        doc_type = str(meta.get("doc_type") or file_dict.get("specimen") or "").lower()
        topic_hints = {
            "receipt": ("finance", "receipts", "taxes"),
            "invoice": ("finance", "invoices", "taxes"),
            "bill": ("finance", "bills"),
            "code": ("code", "projects", "dev"),
            "diagram": ("design", "diagrams"),
        }.get(doc_type, ())
        for hint in topic_hints:
            for d in known_dirs:
                if d.exists() and d.is_dir() and d.name.lower() == hint:
                    return d.resolve()
        # 3. Canonical ~/Shelves/<topic-slug>
        return infer_shelf_folder_for_file(file_dict)

    @staticmethod
    def _preview_collision_name(dest_dir: Path, filename: str, batch_occupied: set[str]) -> tuple[str, bool]:
        """Preview exact destination filename including `_1` auto-suffix if collision exists on disk or in batch."""
        cand = filename
        had_collision = False
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        key = cand.lower()
        if key in batch_occupied or (dest_dir.exists() and (dest_dir / cand).exists()):
            had_collision = True
            c = 1
            while True:
                cand = f"{stem}_{c}{suffix}"
                if cand.lower() not in batch_occupied and not (dest_dir.exists() and (dest_dir / cand).exists()):
                    break
                c += 1
        batch_occupied.add(cand.lower())
        return cand, had_collision

    def _translate_ollama_plan(
        self,
        plan: dict[str, Any],
        scoped_state: dict[str, Any],
        selected_file_id: Optional[str],
        recent_file_ids: list[str],
        name_prefix: str,
        name_comma: str,
        t0: float,
        scope_folder: Optional[str],
        scope_recursive: bool,
        memory_rows: list[dict[str, Any]],
        history: Optional[list[dict[str, str]]],
    ) -> Optional[dict[str, Any]]:
        """Translate a validated Tier-1 Ollama JSON plan into canonical natural clauses and resolve symbolically."""
        mode = plan.get("mode")
        steps = plan.get("steps") or []
        if mode not in {"action", "multi_step"} or not steps:
            return None

        clauses: list[str] = []
        for st in steps:
            atype = str(st.get("actionType") or "")
            tgt = str(st.get("targetQuery") or "these files").strip()
            dst = str(st.get("destination") or "").strip()
            if atype in {"move", "copy"} and dst:
                clauses.append(f"{atype} {tgt} to {dst}")
            elif atype == "trash":
                clauses.append(f"trash {tgt}")
            elif atype == "archive_files":
                clauses.append(f"archive {tgt}")
            elif atype == "tag" and st.get("tag"):
                clauses.append(f"tag {tgt} with #{st['tag'].lstrip('#')}")
            elif atype == "untag" and st.get("tag"):
                clauses.append(f"untag #{st['tag'].lstrip('#')} from {tgt}")
            elif atype == "auto_tag":
                clauses.append(f"auto-tag {tgt}")
            elif atype == "rename_tag" and st.get("oldTag") and st.get("newTag"):
                clauses.append(f"rename tag #{st['oldTag'].lstrip('#')} to #{st['newTag'].lstrip('#')}")
            elif atype == "clear_tags":
                clauses.append(f"clear tags from {tgt}")
            elif atype in {"pin", "unpin"}:
                clauses.append(f"{atype} {tgt}")
            elif atype == "create_folder" and st.get("folderName"):
                clauses.append(f"create folder {st['folderName']}" + (f" inside {dst}" if dst else ""))
            elif atype == "create_shelf" and st.get("shelfName"):
                clauses.append(f"create shelf {st['shelfName']} with {tgt}")
            elif atype == "shelve" and st.get("shelfName"):
                clauses.append(f"shelve {tgt} under {st['shelfName']}")
            elif atype == "unshelve":
                clauses.append(f"unshelve {tgt}")
            elif atype == "rename_batch" and st.get("template"):
                clauses.append(f"rename {tgt} to {st['template']}")
            elif atype == "rename_single" and st.get("newName"):
                clauses.append(f"rename {tgt} to {st['newName']}")
            elif atype == "run_ocr":
                clauses.append(f"run ocr on {tgt}")
            elif atype == "reindex_images":
                clauses.append("reindex images")
            elif atype == "rescan_folders":
                clauses.append("rescan folders")
            elif atype == "verify_index":
                clauses.append("verify index")
            elif atype == "undo":
                clauses.append("undo")
            elif atype == "redo":
                clauses.append("redo")

        if not clauses:
            return None
        synthetic = " and then ".join(clauses)
        res = self._detect_and_build_ai_action(
            synthetic,
            scoped_state,
            selected_file_id=selected_file_id,
            recent_file_ids=recent_file_ids,
            name_prefix=name_prefix,
            name_comma=name_comma,
            t0=t0,
            pending_action=None,
            scope_folder=scope_folder,
            scope_recursive=scope_recursive,
            memory_rows=memory_rows,
            history=history,
            _allow_ollama_planner=False,
        )
        if res is not None:
            tr = list(res.get("trace") or [])
            tr.append(f"Planned via local Ollama ({plan.get('confidence', 0.8):.0%} confidence)")
            res["trace"] = tr
        return res

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
        history: Optional[list[dict[str, str]]] = None,
        _allow_ollama_planner: bool = True,
        _in_chain: bool = False,
    ) -> Optional[dict[str, Any]]:
        """
        Detect natural-language file & folder management requests (including Query-as-Step-0 chained
        multi-step commands, full pending-plan editing, heterogeneous moveMap/shelfMap/tagMap,
        proactive insight prompts, and optional Ollama Tier-1 fallback planning) and return a safe
        confirmation plan (`proposedAction`) without mutating disk.
        """
        q_stripped = re.sub(
            r"^(?:hey\s+|hi\s+|please\s+|could\s+you\s+|can\s+you\s+|would\s+you\s+|will\s+you\s+|"
            r"help\s+me\s+|just\s+|i\s+want\s+to\s+|i\s+need\s+you\s+to\s+|i\s+need\s+to\s+|"
            r"i'?d\s+like\s+to\s+|let'?s\s+|go\s+ahead\s+and\s+|actually[,\s]+)+",
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
        conv_ctx = extract_conversation_context(
            state,
            history=history,
            selected_file_id=selected_file_id,
            recent_file_ids=recent_file_ids,
            scope_folder=scope_folder,
        )

        def _make_item_with_mem(f: dict[str, Any], detail: str = "", destructive: bool = False) -> dict[str, Any]:
            excluded = is_item_excluded_by_memory(mem_list, f)
            is_pinned = bool(f.get("pinned"))
            if destructive and is_pinned:
                eff_detail = f"⚠ Pinned file · {detail}" if detail else "⚠ Pinned file (unchecked for safety)"
                return self._make_action_item(f, eff_detail, selected_by_default=False)
            effective_detail = f"{detail} · Previously skipped" if (excluded and detail) else ("Previously skipped" if excluded else detail)
            return self._make_action_item(f, effective_detail, selected_by_default=not excluded)

        def _reply(
            answer: str,
            citations: list[dict[str, Any]] | None = None,
            proposed: dict[str, Any] | None = None,
            mode: str = "action-plan",
            choices: list[dict[str, str]] | None = None,
            trace: list[str] | None = None,
            process_summary: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            c_list = citations or []
            if proposed is not None and "sourceQuery" not in proposed:
                proposed["sourceQuery"] = q_clean
            raw_choices = choices if choices is not None else self._build_smart_choices("action", c_list, proposed, q_low)
            out: dict[str, Any] = {
                "question": q_clean,
                "answer": answer,
                "citations": c_list,
                "mode": mode,
                "appliedFilters": ([f"folder: {format_path_short(scope_folder)}"] if scope_folder else []),
                "intentMode": "action",
                "totalMatched": len(c_list),
                "proposedAction": proposed,
                "choices": raw_choices[:3],
                "trace": trace if trace is not None else scope_trace,
                "scopeFolder": scope_folder,
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }
            if process_summary is not None:
                out["processSummary"] = process_summary
            return out

        def _build_audit_choices(audit: dict[str, Any]) -> list[dict[str, str]]:
            ch: list[dict[str, str]] = []
            if audit["totalPending"] > 0:
                ch.append({"label": f"Run full organize ({audit['totalPending']})", "prompt": "run full organize plan", "kind": "action"})
            if audit["dupPairs"]:
                ch.append({"label": f"Archive {len(audit['dupPairs'])} duplicate(s)", "prompt": "show me all the duplicates", "kind": "action"})
            if audit["renames"]:
                ch.append({"label": f"Rename {len(audit['renames'])} messy file(s)", "prompt": "rename messy files", "kind": "action"})
            if audit["tidyMoves"]:
                ch.append({"label": f"Organize {len(audit['tidyMoves'])} to Shelf folders", "prompt": "organize files into shelf folders", "kind": "action"})
            if audit["ocrPending"]:
                ch.append({"label": f"Run OCR ({len(audit['ocrPending'])})", "prompt": "run ocr on all images and PDFs", "kind": "action"})
            if len(ch) < 3:
                ch.append({"label": "List all files", "prompt": "list everything", "kind": "followup"})
            if len(ch) < 3:
                ch.append({"label": "Check space usage", "prompt": "how much space am I using?", "kind": "followup"})
            return ch[:3]

        # 0-CONFIRM. DIRECT CONVERSATIONAL CONFIRMATION ("do it", "yes", "confirm", "apply", "go ahead", "run it")
        is_direct_confirm = bool(
            re.fullmatch(
                r"(?:yes|yeah|yep|yup|sure|ok|okay)?\s*(?:do\s+it|do\s+that|run\s+it|apply(?:\s+it|\s+that)?|confirm(?:\s+it|\s+plan)?|execute(?:\s+it)?|go\s+ahead|proceed|make\s+it\s+happen)(?:\s+now|\s+please)?",
                q_low,
            )
            or (pending_action is not None and re.fullmatch(r"(?:yes|yeah|yep|yup|sure|ok|okay|confirm|apply)", q_low))
        )
        if is_direct_confirm:
            if pending_action:
                auto_plan = {**pending_action, "autoExecute": True}
                return _reply(
                    f"{name_prefix}running **{pending_action.get('title') or 'your confirmed plan'}** now.",
                    proposed=auto_plan,
                    trace=[*scope_trace, "Confirmed pending action plan via chat"],
                )
            q_low = "run full organize plan"
            q_stripped = "run full organize plan"

        # 0A. FULL PENDING-PLAN CONVERSATIONAL EDITING (runs BEFORE read-only guard!)
        # Supports: cancel, explain ("why did you pick these?"), filter/drop items,
        # swap destination ("move to Finance instead"), swap verb ("copy instead"),
        # swap tag ("use tag #audit instead"), swap format ("use format {year}_{title} instead"),
        # swap shelf ("use shelf Q1 instead").
        if pending_action:
            if re.fullmatch(r"(?:cancel|cancel plan|never ?mind|abort|stop|forget it|dismiss)", q_low):
                return _reply(
                    f"{name_prefix}okay, cancelled that pending plan — no files were modified.",
                    mode="conversational",
                )
            if re.search(r"\bwhy\s+(?:did\s+you\s+(?:pick|choose|select|include|skip)|these|this\s+plan)\b", q_low):
                p_items = list(pending_action.get("items") or [])
                p_summary = str(pending_action.get("summary") or pending_action.get("title") or "Pending action")
                reasons = [
                    f"- `{it.get('fileName')}` — {it.get('detail') or it.get('folder') or 'matched your query'}"
                    + (" *(unchecked by default)*" if it.get("selectedByDefault") is False else "")
                    for it in p_items[:10]
                ]
                md = (
                    f"{name_prefix}here's why I built **{pending_action.get('title') or 'this plan'}** ({p_summary}):\n"
                    + ("\n".join(reasons) if reasons else "- No individual file rows were attached to this plan.")
                )
                return _reply(md, proposed=pending_action, mode="conversational")

            # Swap destination: "actually move to Finance instead", "put them in Archive instead", "to Finance instead"
            swap_dst_m = re.match(
                r"^(?:(?:move|put|copy|send|relocate)\s+(?:(?:them|these|those|it)\s+)?)?(?:to|into|in)\s+(.+?)\s+instead$|"
                r"^change\s+(?:the\s+)?(?:destination|folder|target)\s+to\s+(.+)$",
                q_stripped,
                flags=re.IGNORECASE,
            )
            if swap_dst_m and pending_action.get("actionType") in {"move", "copy"}:
                new_dst_hint = (swap_dst_m.group(1) or swap_dst_m.group(2) or "").strip(" \"'“”?!.")
                dst_info = self._resolve_folder_candidate(new_dst_hint, all_files_raw, watched, allow_create_sub=True, conv_ctx=conv_ctx)
                if dst_info:
                    verb_lbl = "Copy" if pending_action["actionType"] == "copy" else "Move"
                    new_items = []
                    occ: set[str] = set()
                    dst_dir = Path(dst_info["path"])
                    for it in pending_action.get("items") or []:
                        cand_n, had_col = self._preview_collision_name(dst_dir, str(it.get("fileName") or ""), occ)
                        col_note = " (collision → auto-renamed)" if had_col else ""
                        new_items.append({**it, "detail": f"{it.get('folder') or 'Current'} → {dst_info['label']}/{cand_n}{col_note}"})
                    new_payload = {**(pending_action.get("payload") or {}), "targetFolder": dst_info["path"], "createTargetFolder": dst_info["willCreate"]}
                    new_payload.pop("moveMap", None)
                    updated_plan = {
                        **pending_action,
                        "actionId": make_plan_id(),
                        "title": f"{verb_lbl} {len(new_items)} file(s) to {dst_info['label']}",
                        "summary": f"{verb_lbl} {len(new_items)} file(s) into {dst_info['label']}.",
                        "targetLabel": dst_info["label"],
                        "items": new_items,
                        "payload": new_payload,
                    }
                    return _reply(
                        f"{name_prefix}updated destination to **{dst_info['label']}** for {len(new_items)} file(s). Confirm below when ready.",
                        proposed=updated_plan,
                    )

            # Swap verb: "copy them instead", "move instead", "archive instead", "trash instead"
            swap_verb_m = re.match(
                r"^(copy|move|archive|trash|delete)\s+(?:them\s+|these\s+|those\s+|it\s+)?instead$",
                q_low,
            )
            if swap_verb_m and pending_action.get("items"):
                new_v = swap_verb_m.group(1)
                p_items = list(pending_action["items"])
                if new_v == "copy" and pending_action.get("actionType") == "move":
                    updated_plan = {
                        **pending_action,
                        "actionId": make_plan_id(),
                        "actionType": "copy",
                        "title": str(pending_action.get("title") or "").replace("Move ", "Copy ", 1),
                        "summary": str(pending_action.get("summary") or "").replace("Move ", "Copy ", 1),
                        "reversible": True,
                    }
                    return _reply(f"{name_prefix}switched the plan from **Move** to **Copy** (keeping originals in place). Confirm below.", proposed=updated_plan)
                if new_v == "move" and pending_action.get("actionType") == "copy":
                    updated_plan = {
                        **pending_action,
                        "actionId": make_plan_id(),
                        "actionType": "move",
                        "title": str(pending_action.get("title") or "").replace("Copy ", "Move ", 1),
                        "summary": str(pending_action.get("summary") or "").replace("Copy ", "Move ", 1),
                        "reversible": True,
                    }
                    return _reply(f"{name_prefix}switched the plan from **Copy** to **Move**. Confirm below.", proposed=updated_plan)
                if new_v == "archive":
                    updated_plan = {
                        **pending_action,
                        "actionId": make_plan_id(),
                        "actionType": "archive_files",
                        "title": f"Archive {len(p_items)} file(s)",
                        "summary": "Safely move selected files to GoodShelves_Archive (reversible via Undo).",
                        "targetLabel": "GoodShelves_Archive",
                        "reversible": True,
                        "destructive": False,
                        "items": [{**it, "detail": "Archive → GoodShelves_Archive"} for it in p_items],
                        "payload": {},
                    }
                    return _reply(f"{name_prefix}switched the plan to **Archive** ({len(p_items)} file(s)). Confirm below.", proposed=updated_plan)
                if new_v in {"trash", "delete"}:
                    updated_plan = {
                        **pending_action,
                        "actionId": make_plan_id(),
                        "actionType": "trash",
                        "title": f"Move to Trash: {len(p_items)} file(s)",
                        "summary": "Move selected files to GoodShelves_Trash (reversible via Undo).",
                        "targetLabel": "GoodShelves_Trash",
                        "reversible": True,
                        "destructive": True,
                        "items": [{**it, "detail": "Move to Trash"} for it in p_items],
                        "payload": {},
                    }
                    return _reply(f"{name_prefix}switched the plan to **Move to Trash** ({len(p_items)} file(s)). Confirm below.", proposed=updated_plan)

            # Swap tag: "use tag #audit instead"
            swap_tag_m = re.match(r"^(?:use|change\s+to)\s+tag\s+#?([A-Za-z0-9_\-,#\s]+?)(?:\s+instead)?$", q_stripped, re.I)
            if swap_tag_m and pending_action.get("actionType") in {"tag", "untag"}:
                toks = [t.strip(" #").lower() for t in re.split(r"[,;]+", swap_tag_m.group(1)) if t.strip(" #")]
                if toks:
                    clean_t = ", ".join(toks)
                    lbl_t = " ".join(f"#{t}" for t in toks)
                    is_u = pending_action["actionType"] == "untag"
                    p_items = [{**it, "detail": f"{'Remove' if is_u else 'Add'} tag {lbl_t}"} for it in (pending_action.get("items") or [])]
                    updated_plan = {
                        **pending_action,
                        "actionId": make_plan_id(),
                        "title": f"{'Remove' if is_u else 'Tag'} {len(p_items)} file(s) with {lbl_t}",
                        "targetLabel": f"-{lbl_t}" if is_u else lbl_t,
                        "items": p_items,
                        "payload": {**(pending_action.get("payload") or {}), "tag": clean_t},
                    }
                    return _reply(f"{name_prefix}updated the pending tag to **{lbl_t}**. Confirm below.", proposed=updated_plan)

            # Swap rename format/template: "use format {year}_{title} instead"
            swap_tpl_m = re.match(r"^(?:use|change\s+to)\s+(?:format|template|pattern)\s+(.+?)(?:\s+instead)?$", q_stripped, re.I)
            if swap_tpl_m and pending_action.get("actionType") == "rename_batch":
                new_tpl = swap_tpl_m.group(1).strip(" \"'“”`")
                by_id_raw = {f["id"]: f for f in all_files_raw}
                raw_pairs: list[tuple[dict[str, Any], str]] = []
                for idx_s, it in enumerate(pending_action.get("items") or [], start=1):
                    f_obj = by_id_raw.get(str(it.get("fileId") or ""))
                    if f_obj:
                        nn = format_rename_template(f_obj, new_tpl, seq_index=idx_s)
                        raw_pairs.append((f_obj, nn))
                if raw_pairs:
                    sim_pairs = simulate_batch_rename_collisions(raw_pairs)
                    rmap = {f["id"]: nn for f, nn, _ in sim_pairs}
                    new_items = [self._make_action_item(f, f"{f['name']} → {nn}" + (" (auto-suffixed)" if col else "")) for f, nn, col in sim_pairs]
                    updated_plan = {
                        **pending_action,
                        "actionId": make_plan_id(),
                        "title": f"Rename {len(new_items)} file(s) using format `{new_tpl}`",
                        "targetLabel": new_tpl,
                        "items": new_items,
                        "payload": {"template": new_tpl, "renameMap": rmap},
                    }
                    return _reply(f"{name_prefix}updated rename format to `{new_tpl}` ({len(new_items)} file(s)). Confirm below.", proposed=updated_plan)

            # Swap shelf name: "use shelf Q1 instead"
            swap_sh_m = re.match(r"^(?:use|change\s+to|call\s+it)\s+(?:shelf\s+)?[\"“']?(.+?)[\"”']?(?:\s+instead)?$", q_stripped, re.I)
            if swap_sh_m and pending_action.get("actionType") in {"create_shelf", "shelve"}:
                new_sn = swap_sh_m.group(1).strip(" \"'“”?!.")
                if new_sn and new_sn.lower() not in {"pdfs", "images", "docs"}:
                    existing_sh = next((s for s in shelves if s["name"].lower() == new_sn.lower()), None)
                    p_items = [{**it, "detail": f"{it.get('shelfName') or 'Workspace'} → {new_sn}"} for it in (pending_action.get("items") or [])]
                    if existing_sh:
                        updated_plan = {
                            **pending_action,
                            "actionId": make_plan_id(),
                            "actionType": "shelve",
                            "title": f"Shelve {len(p_items)} item(s) under “{existing_sh['name']}”",
                            "targetLabel": existing_sh["name"],
                            "items": p_items,
                            "payload": {"shelfId": existing_sh["id"], "shelfName": existing_sh["name"]},
                        }
                    else:
                        updated_plan = {
                            **pending_action,
                            "actionId": make_plan_id(),
                            "actionType": "create_shelf",
                            "title": f"Create shelf “{new_sn}”" + (f" with {len(p_items)} item(s)" if p_items else ""),
                            "targetLabel": new_sn,
                            "items": p_items,
                            "payload": {"shelfName": new_sn},
                        }
                    return _reply(f"{name_prefix}updated target shelf to **{new_sn}**. Confirm below.", proposed=updated_plan)

            # Filter / drop items from pending plan ("only include pdfs", "exclude images", "drop the first one", "remove #2")
            if pending_action.get("items"):
                drop_ord_m = re.match(
                    r"^(?:drop|remove|skip|exclude|uncheck)\s+(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last|#\d+)(?:\s+(?:one|item|file))?$",
                    q_low,
                )
                if drop_ord_m:
                    ord_tok = drop_ord_m.group(1)
                    items_in = list(pending_action["items"])
                    ord_idx_map = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "last": -1}
                    if ord_tok.startswith("#"):
                        rm_idx = int(ord_tok[1:]) - 1
                    else:
                        rm_idx = ord_idx_map.get(ord_tok, 0)
                    if rm_idx < 0:
                        rm_idx = len(items_in) + rm_idx
                    if 0 <= rm_idx < len(items_in):
                        dropped = items_in[rm_idx]
                        kept = [it for i, it in enumerate(items_in) if i != rm_idx]
                        updated_plan = {
                            **pending_action,
                            "actionId": make_plan_id(),
                            "items": kept,
                            "summary": f"Removed {dropped.get('fileName')} ({len(kept)} item(s) remaining)",
                        }
                        return _reply(
                            f"{name_prefix}dropped `{dropped.get('fileName')}` from the plan (**{len(kept)} item(s)** remaining). Confirm below.",
                            proposed=updated_plan,
                        )

                ref_m = re.match(r"^(?:only|only include|keep only|just)\s+(.+)$", q_low)
                excl_m = re.match(r"^(?:exclude|without|skip|remove|drop)\s+(.+)$", q_low)
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
                        if tok in {"pinned", "starred"}:
                            return "pinned" in str(it.get("detail") or "").lower()
                        return tok in f"{it.get('fileName', '')} {it.get('title', '')} {it.get('folder', '')}".lower()

                    filtered_items = [it for it in items_in if (not _item_matches_tok(it) if is_excl else _item_matches_tok(it))]
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
                        "actionId": make_plan_id(),
                        "items": filtered_items,
                        "summary": f"Refined selection ({len(filtered_items)} item(s)): {pending_action.get('summary', '')}",
                    }
                    return _reply(
                        f"{name_prefix}updated the plan to **{len(filtered_items)} item(s)** ({'excluded' if is_excl else 'kept only'} {tok}). Review and confirm below.",
                        proposed=updated_plan,
                    )

        # 0B. MULTI-STEP CHAINED COMMANDS (runs BEFORE read-only guard so Query-as-Step-0 works!)
        # Example: "find all receipts from March, tag them #tax, rename to {date}_{merchant}_{amount}, and move to Tax 2026"
        if not _in_chain:
            raw_chain_parts = split_compound_commands(q_stripped)
            if len(raw_chain_parts) >= 2:
                step0_query, action_parts = extract_step0_query_clause(raw_chain_parts)
                # Guard: only enter chain planner if we have Step-0 query + >=1 action OR >=2 non-query parts
                should_plan_chain = (step0_query is not None and len(action_parts) >= 1) or (
                    step0_query is None and not re.match(r"^(?:find|show|list|search|where|what|when|who|why|how\s+many|how\s+much|summarize|explain|tell\s+me)\b", raw_chain_parts[0].lower())
                )
                if should_plan_chain:
                    sim = VirtualWorkspaceSimulator(all_files)
                    carried_files: list[dict[str, Any]] = []
                    step0_trace = ""
                    if step0_query:
                        carried_files, _ = self._resolve_action_files(
                            step0_query,
                            scoped_state,
                            selected_file_id,
                            recent_file_ids,
                            conv_ctx=conv_ctx,
                        )
                        step0_trace = f"Step 0 selected {len(carried_files)} file(s) for “{step0_query}”"
                        if not carried_files:
                            return _reply(
                                f"{name_prefix}I didn't find any files matching “{step0_query}” for step 1 of your workflow.",
                                mode="conversational",
                            )

                    step_plans: list[dict[str, Any]] = []
                    unclear_parts: list[str] = []
                    for step_idx, raw_part in enumerate(action_parts):
                        effective_q = propagate_elided_object(raw_part, bool(carried_files))
                        if sim.last_created_folder_name and re.search(
                            r"\b(?:into\s+it|to\s+it|in\s+it|into\s+that\s+folder|to\s+that\s+folder|there)\b",
                            effective_q,
                            flags=re.IGNORECASE,
                        ):
                            effective_q = re.sub(
                                r"\b(?:into\s+it|to\s+it|in\s+it|into\s+that\s+folder|to\s+that\s+folder|there)\b",
                                f"to {sim.last_created_folder_name}",
                                effective_q,
                                flags=re.IGNORECASE,
                            )
                        if sim.last_created_shelf_name and re.search(
                            r"\b(?:onto\s+it|under\s+it|to\s+that\s+shelf|onto\s+that\s+shelf)\b",
                            effective_q,
                            flags=re.IGNORECASE,
                        ):
                            effective_q = re.sub(
                                r"\b(?:onto\s+it|under\s+it|to\s+that\s+shelf|onto\s+that\s+shelf)\b",
                                f"under {sim.last_created_shelf_name}",
                                effective_q,
                                flags=re.IGNORECASE,
                            )

                        sim_state = {**scoped_state, "files": sim.active_files()}
                        if carried_files and re.search(r"\b(them|those|these|all\s+of\s+them)\b", effective_q, flags=re.IGNORECASE):
                            sub_ids = [f["id"] for f in carried_files if f["id"] not in sim.trashed_ids]
                        elif carried_files and step0_query is not None:
                            sub_ids = [f["id"] for f in carried_files if f["id"] not in sim.trashed_ids]
                        else:
                            sub_ids = recent_file_ids

                        sub_res = self._detect_and_build_ai_action(
                            effective_q,
                            sim_state,
                            selected_file_id,
                            sub_ids,
                            name_prefix,
                            name_comma,
                            t0,
                            pending_action=None,
                            scope_folder=scope_folder,
                            scope_recursive=scope_recursive,
                            memory_rows=mem_list,
                            history=history,
                            _allow_ollama_planner=False,
                            _in_chain=True,
                        )
                        sub_act = sub_res.get("proposedAction") if sub_res else None
                        if not sub_act or sub_act.get("actionType") == "multi_step":
                            unclear_parts.append(raw_part)
                            continue

                        # Resolve virtual created folder / shelf targets accurately
                        if sim.last_created_folder and sub_act["actionType"] in {"move", "copy"}:
                            payload = dict(sub_act.get("payload") or {})
                            if str(payload.get("targetFolder", "")).lower().endswith(sim.last_created_folder_name.lower()):
                                payload["targetFolder"] = sim.last_created_folder
                                payload["createTargetFolder"] = False
                                sub_act["payload"] = payload
                        if sim.last_created_shelf_name and sub_act["actionType"] in {"create_shelf", "shelve"}:
                            payload = dict(sub_act.get("payload") or {})
                            if str(payload.get("shelfName") or "").lower() == sim.last_created_shelf_name.lower():
                                sub_act["actionType"] = "shelve"
                                payload["shelfId"] = f"__last_created_shelf__"
                                payload["shelfName"] = sim.last_created_shelf_name
                                sub_act["payload"] = payload

                        sub_act["_stepIndex"] = len(step_plans)
                        sim.apply_step(sub_act)
                        step_items_raw = list(sub_act.get("items") or [])
                        if step_items_raw:
                            carried_files = [
                                sim.files_by_id[i["fileId"]]
                                for i in step_items_raw
                                if i["fileId"] in sim.files_by_id and i["fileId"] not in sim.trashed_ids
                            ]
                        step_plans.append(sub_act)

                    # Single action derived from Step-0 query ("find receipts from March and tag them #tax")
                    if step0_query and len(step_plans) == 1 and not unclear_parts:
                        single_act = step_plans[0]
                        single_act.pop("_stepIndex", None)
                        tr_list = [*scope_trace, step0_trace, f"Prepared {single_act['actionType']}"]
                        return _reply(
                            f"{name_prefix}found **{len(single_act.get('items') or [])} file(s)** matching “{step0_query}” — ready to **{single_act['title']}**. Confirm below.",
                            proposed=single_act,
                            trace=tr_list,
                        )

                    # Partial-parse recovery if some steps parsed and one was unclear
                    if step_plans and unclear_parts and len(step_plans) < len(action_parts):
                        parsed_desc = ", ".join(sp["title"] for sp in step_plans)
                        unclear_desc = "; ".join(f"`{u}`" for u in unclear_parts)
                        if len(step_plans) == 1:
                            return _reply(
                                f"{name_prefix}I understood **{parsed_desc}**, but I wasn't sure what you meant by {unclear_desc}. "
                                "Confirm the understood step below, or rephrase the unclear step.",
                                proposed=step_plans[0],
                                trace=[*scope_trace, f"Partial chain parse: skipped {unclear_desc}"],
                            )

                    if len(step_plans) >= 2:
                        # Strictly unique `fileId` rows across all steps (prevents React key collisions!)
                        unified_items = merge_unique_plan_items(step_plans)
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
                        unique_file_count = len(unified_items)
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
                        unclear_note = (
                            f"\n\n*(Note: I skipped unclear clause `{unclear_parts[0]}` — let me know if you want to add it.)*"
                            if unclear_parts
                            else ""
                        )
                        tr_chain = [*scope_trace, *([step0_trace] if step0_trace else []), f"Planned {len(step_plans)} chained actions ({unique_file_count} unique item(s))"]
                        return _reply(
                            f"{name_prefix}I put together a **{len(step_plans)}-step plan**:\n"
                            + "\n".join(f"{idx}. {sp['title']}" for idx, sp in enumerate(step_plans, start=1))
                            + unclear_note
                            + "\n\nReview the checklist below and click **Confirm** to run all steps.",
                            proposed=proposed,
                            trace=tr_chain,
                        )

        # 0C. OPEN / REVEAL FILE IN SYSTEM APP OR FILE MANAGER ("open my resume", "reveal report.pdf in finder/explorer")
        reveal_m = re.match(
            r"^(?:reveal|show)\s+(.+?)\s+(?:in\s+)?(?:finder|explorer|file\s+manager|folder|files)$|^reveal\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        open_m = None if reveal_m else re.match(r"^open\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if reveal_m or open_m:
            is_reveal = reveal_m is not None
            tgt_p = (reveal_m.group(1) or reveal_m.group(2) if reveal_m else open_m.group(1)).strip()  # type: ignore[union-attr]
            if tgt_p.lower() not in {"settings", "preferences", "help", "clean", "shelves"}:
                matched_files, _ = self._resolve_action_files(tgt_p, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
                if matched_files:
                    f0 = matched_files[0]
                    act_t = "reveal_file" if is_reveal else "open_file"
                    verb_l = "Reveal in file manager" if is_reveal else "Open"
                    proposed = {
                        "actionId": make_plan_id(),
                        "actionType": act_t,
                        "title": f"{verb_l}: “{f0['name']}”",
                        "summary": f"{'Show containing folder for' if is_reveal else 'Open'} {f0['name']} using your system app.",
                        "targetLabel": f0.get("folder") or f0["name"],
                        "reversible": False,
                        "destructive": False,
                        "requiresSelection": True,
                        "items": [self._make_action_item(f0, f"{verb_l} ({f0.get('folder') or ''})")],
                        "payload": {"reveal": is_reveal},
                    }
                    return _reply(
                        f"{name_prefix}ready to {verb_l.lower()} **{f0['title']}** (`{f0['name']}`). Click **Confirm** below.",
                        citations=[self._make_citation(f0, f"{f0.get('folder')}/{f0['name']}", 96)],
                        proposed=proposed,
                    )

        # 0D. SWAP LOOKALIKE PAIR ("swap keep and copy for pair 1")
        swap_m = re.match(
            r"^swap\s+(?:keep\s+and\s+copy\s+(?:for\s+)?|duplicate\s+)?(?:pair\s+)?#?(\d+|[a-zA-Z0-9_\-]+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if swap_m:
            pair_tok = swap_m.group(1).strip()
            unresolved = [p for p in (state.get("lookalikes") or []) if not p.get("resolved")]
            target_pair = None
            if pair_tok.isdigit():
                p_idx = int(pair_tok) - 1
                if 0 <= p_idx < len(unresolved):
                    target_pair = unresolved[p_idx]
            else:
                target_pair = next((p for p in unresolved if p["id"] == pair_tok), None)
            if not target_pair:
                return _reply(f"{name_prefix}I couldn't find duplicate pair `{pair_tok}`.", mode="conversational")
            keep_n = (target_pair.get("keep") or {}).get("name", "keep")
            copy_n = (target_pair.get("copy") or {}).get("name", "copy")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "swap_lookalike",
                "title": f"Swap duplicate pair: keep “{copy_n}” instead of “{keep_n}”",
                "summary": f"Mark {copy_n} as the kept original and {keep_n} as the redundant copy.",
                "targetLabel": copy_n,
                "reversible": True,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"pairIds": [target_pair["id"]]},
            }
            return _reply(
                f"{name_prefix}ready to swap pair **{pair_tok}**: keep `{copy_n}` and mark `{keep_n}` as the copy. Confirm below.",
                proposed=proposed,
            )

        # 0E. PROACTIVE INSIGHTS & SMART SYSTEM ACTIONS
        # 1) Reindex images with active vision model
        if re.search(r"\b(?:reindex|re-index)\s+(?:all\s+|my\s+)?images?\b", q_low):
            img_cnt = sum(1 for f in all_files_raw if f.get("modality") == "image")
            vis_m = str(self.get_settings().get("vision_model") or "active vision model")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "reindex_images",
                "title": f"Re-index {img_cnt} image(s) with {vis_m}",
                "summary": f"Refresh visual embeddings for all {img_cnt} indexed image(s) using {vis_m}.",
                "targetLabel": vis_m,
                "reversible": False,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {},
            }
            return _reply(
                f"{name_prefix}ready to re-index **{img_cnt} image(s)** with **{vis_m}** so visual search matches your active model. Confirm below.",
                proposed=proposed,
            )

        # 2) Rescan watched folders / refresh index
        if re.fullmatch(r"(?:rescan|re-scan|refresh|sync)\s+(?:all\s+|my\s+)?(?:watched\s+)?(?:folders?|index|library|workspace|files)", q_low):
            active_wf = [w for w in watched if w.get("active", True)]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "rescan_folders",
                "title": f"Rescan {len(active_wf)} watched folder(s)",
                "summary": "Scan all active watched folders for new or modified files and refresh shelves.",
                "targetLabel": f"{len(active_wf)} folder(s)",
                "reversible": False,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {},
            }
            return _reply(
                f"{name_prefix}ready to rescan **{len(active_wf)} watched folder(s)** and update your index. Confirm below.",
                proposed=proposed,
            )

        # 3) Verify index integrity
        if re.fullmatch(r"(?:verify|check|repair)\s+(?:the\s+|my\s+)?(?:index|database|integrity|orphans?)", q_low):
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "verify_index",
                "title": "Verify index integrity & prune orphans",
                "summary": "Check all indexed entries against disk and clean up stale/deleted file records.",
                "targetLabel": "Index Integrity",
                "reversible": False,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {},
            }
            return _reply(
                f"{name_prefix}ready to verify your local index and prune any orphaned entries. Confirm below.",
                proposed=proposed,
            )

        # 4) Empty Trash (explicit count + MB preview, destructive: True)
        if re.fullmatch(r"(?:empty|clear|purge)\s+(?:the\s+|my\s+)?(?:good\s+shelves\s+)?trash", q_low):
            trashed = self.list_trash_and_archive().get("trashed", [])
            if not trashed:
                return _reply(f"{name_prefix}your Good Shelves Trash is already empty.", mode="conversational")
            tot_b = sum(int(f.get("sizeBytes") or 0) for f in trashed)
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "empty_trash",
                "title": f"Empty Trash ({len(trashed)} file(s) · {format_bytes(tot_b)})",
                "summary": f"Permanently delete all {len(trashed)} trashed file(s) ({format_bytes(tot_b)}) — this cannot be undone.",
                "targetLabel": "Permanent Delete",
                "reversible": False,
                "destructive": True,
                "requiresSelection": False,
                "items": [self._make_action_item(f, f"Permanent delete ({f.get('size', '')})") for f in trashed[:30]],
                "payload": {},
            }
            return _reply(
                f"{name_prefix}Trash contains **{len(trashed)} file(s)** totaling **{format_bytes(tot_b)}**. "
                "Confirm below to permanently empty Trash (this cannot be undone).",
                proposed=proposed,
            )

        # 5) Save Search ("save search 'Tax 2026' for receipts from 2026")
        ss_m = re.match(
            r"^save\s+(?:this\s+)?search(?:\s+(?:as|named|called))?\s+[\"“']?([^\"”']+?)[\"”']?\s+(?:for|query|:)\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if ss_m:
            s_name = ss_m.group(1).strip(" \"'“”")
            s_query = ss_m.group(2).strip(" \"'“”?!.")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "save_search",
                "title": f"Save search “{s_name}”",
                "summary": f"Save reusable query `{s_query}` under Saved Searches.",
                "targetLabel": s_name,
                "reversible": False,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"searchName": s_name, "searchQuery": s_query},
            }
            return _reply(
                f"{name_prefix}ready to save search **{s_name}** (`{s_query}`). Confirm below.",
                proposed=proposed,
            )

        # 6) Forget AI Memory rule ("forget that receipts go in Finance", "forget rule about screenshots")
        fg_m = re.match(r"^forget\s+(?:that\s+)?(.+)$", q_stripped, flags=re.IGNORECASE)
        if fg_m and q_low not in {"forget it"}:
            fg_phrase = fg_m.group(1).strip()
            hits_mem = find_matching_memory_entries(mem_list, fg_phrase)
            if not hits_mem:
                return _reply(
                    f"{name_prefix}I didn't find any saved AI memory rule matching “{fg_phrase}”. Use `/memory` to view saved rules.",
                    mode="conversational",
                )
            m0 = hits_mem[0]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "forget_memory",
                "title": f"Forget rule: `{m0['keyPattern']}` → `{m0['value']}`",
                "summary": f"Remove {len(hits_mem)} matching entry(ies) from local AI memory.",
                "targetLabel": "Local AI Memory",
                "reversible": False,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"memoryId": m0["id"], "keyPattern": m0["keyPattern"]},
            }
            return _reply(
                f"{name_prefix}found memory rule `{m0['keyPattern']}` → `{m0['value']}`. Confirm below to forget it.",
                proposed=proposed,
            )

        # 7) Proactive Insight #5: "organize uncategorized files into shelves" -> smart `shelve` with `shelfMap`!
        if re.search(
            r"\b(?:organize|sort|assign|categorize|shelve|move)\s+(?:all\s+|my\s+)?(?:uncategorized|unsorted|quiet\s+tray)\s+(?:files?|items?)(?:\s+(?:into|to|onto)\s+shelves?)?\b",
            q_low,
        ):
            tray_files = [
                f for f in all_files
                if str(f.get("shelfId") or "") in {"", "tray", "uncategorized"}
            ]
            if not tray_files:
                return _reply(
                    f"{name_prefix}your Quiet Tray is completely clear — all files are already organized into shelves!",
                    mode="workspace-insight",
                )
            shelf_map: dict[str, str] = {}
            items = []
            preview_lines = []
            for f in tray_files[:50]:
                best_sid, best_name, pct = self._classify_file_to_best_shelf(f, shelves)
                shelf_map[f["id"]] = best_sid
                items.append(self._make_action_item(f, f"Quiet Tray → {best_name} ({pct}% match)"))
                if len(preview_lines) < 10:
                    preview_lines.append(f"- `{f['name']}` → **{best_name}** ({pct}% match)")
            if len(tray_files) > 10:
                preview_lines.append(f"- *…plus {len(tray_files) - 10} more item(s) in the checklist below.*")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "shelve",
                "title": f"Auto-shelve {len(items)} Quiet Tray file(s)",
                "summary": "Assign uncategorized files to their best matching shelves based on content (100% reversible).",
                "targetLabel": "Matched Shelves",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"shelfMap": shelf_map, "shelfName": "Matched Shelves"},
            }
            return _reply(
                f"{name_prefix}I classified **{len(items)} Quiet Tray file(s)** into their best shelves:\n"
                + "\n".join(preview_lines)
                + "\n\nUncheck anything you want to leave unsorted, then click **Confirm**.",
                citations=[self._make_citation(f, f"→ {self._classify_file_to_best_shelf(f, shelves)[1]}", 90) for f in tray_files[:12]],
                proposed=proposed,
                trace=[*scope_trace, f"Classified {len(items)} Quiet Tray file(s) into shelves"],
            )

        # 8) Proactive Insight #4 & heterogeneous auto-sort:
        #    "organize my stale downloads", "auto-sort Downloads", "put these where they belong"
        auto_sort_m = re.match(
            r"^(?:(?:organize|auto[- ]?sort|sort)\s+(?:my\s+|the\s+|all\s+)?(stale\s+downloads|.+?\s+stale\s+downloads)|"
            r"auto[- ]?sort\s+(?:my\s+|the\s+|all\s+)?(.+)|"
            r"(?:put|move|place|sort)\s+(.+?)\s+where\s+(?:it\s+belongs|they\s+belong))$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if auto_sort_m:
            tgt_sort = (auto_sort_m.group(1) or auto_sort_m.group(2) or auto_sort_m.group(3) or "").strip()
            matched_sort, _ = self._resolve_action_files(tgt_sort, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_sort:
                return _reply(
                    f"{name_prefix}I didn't find any files matching “{tgt_sort}” to organize.",
                    mode="conversational",
                )
            known_dirs = self._get_known_dirs_depth4(all_files_raw, watched)
            move_map: dict[str, str] = {}
            items = []
            occ_by_dir: dict[str, set[str]] = {}
            preview_lines = []
            for f in matched_sort[:60]:
                dst_p = self._route_file_auto_sort(f, mem_list, known_dirs)
                if not dst_p:
                    continue
                cur_dir = Path(str(f.get("folderFull") or Path(f["path"]).parent)).resolve()
                if cur_dir == dst_p.resolve():
                    continue
                dst_str = str(dst_p.resolve())
                move_map[f["id"]] = dst_str
                cand_n, had_col = self._preview_collision_name(dst_p, f["name"], occ_by_dir.setdefault(dst_str, set()))
                col_note = " (auto-suffixed)" if had_col else ""
                dst_short = format_path_short(dst_p)
                items.append(_make_item_with_mem(f, f"{f.get('folder') or 'Current'} → {dst_short}/{cand_n}{col_note}"))
                if len(preview_lines) < 10:
                    preview_lines.append(f"- `{f['name']}` → `{dst_short}/{cand_n}`")
            if not items:
                return _reply(
                    f"{name_prefix}all {len(matched_sort)} matching file(s) are already in their target folders!",
                    mode="conversational",
                )
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "move",
                "title": f"Auto-sort {len(items)} file(s) into topic folders",
                "summary": f"Move {len(items)} file(s) individually into their best matching topic/learned folders (100% undoable).",
                "targetLabel": "Matched Folders",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"moveMap": move_map, "createTargetFolder": True},
            }
            return _reply(
                f"{name_prefix}I prepared an individual auto-sort plan for **{len(items)} file(s)**:\n"
                + "\n".join(preview_lines)
                + "\n\nUncheck any file to leave in place, then click **Confirm**.",
                citations=[self._make_citation(f, f"→ {format_path_short(Path(move_map[f['id']]))}", 92) for f in matched_sort[:12] if f["id"] in move_map],
                proposed=proposed,
                trace=[*scope_trace, f"Built heterogeneous moveMap for {len(items)} file(s)"],
            )

        # 0-CLEAN-FOLDER. FOLDER-SPECIFIC CLEANUP ("clean my downloads", "clean downloads", "tidy downloads")
        clean_folder_m = re.match(
            r"^(?:clean(?:\s+up)?|tidy(?:\s+up)?|fix(?:\s+up)?)\s+(?:my\s+|the\s+|all\s+)?([A-Za-z0-9 _.\-/~]+?)(?:\s+folder)?$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if clean_folder_m:
            f_hint = clean_folder_m.group(1).strip()
            if f_hint.lower() not in {"files", "my files", "all files", "everything", "workspace", "system", "this", "that", "it", "these", "them", "those", "duplicates", "duplicate files"}:
                f_cand = self._resolve_folder_candidate(f_hint, all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
                if f_cand and Path(f_cand["path"]).is_dir():
                    target_dir = Path(f_cand["path"]).resolve()
                    try:
                        newly = self.scan_folder(target_dir, max_files=120, recursive=True, emit_idle=False)
                        if newly > 0:
                            self.recluster_all()
                            state = self.get_all_state()
                            all_files_raw = [f for f in state.get("files", []) if not f.get("archived")]
                    except Exception:
                        pass
                    scope_folder = str(target_dir)
                    all_files = filter_files_by_scope(all_files_raw, scope_folder, recursive=True)
                    scoped_state = {**state, "files": all_files}
                    scope_trace = [f"Scoped cleanup to {f_cand['label']} ({len(all_files)} file(s))"]
                    q_low = "run full organize plan"
                    q_stripped = "run full organize plan"

        # 0-VAGUE-FIX. VAGUE FILE FIX / TIDY ("fix this", "fix it", "clean this", "tidy this")
        vague_fix_m = re.match(
            r"^(?:fix|clean(?:\s+up)?|tidy(?:\s+up)?|sort(?:\s+out)?|handle|organize)\s+(this|that|it|this\s+one|that\s+one|this\s+file|that\s+file|these|those|them|this\s+thing|this\s+shit)$",
            q_low,
        )
        if vague_fix_m:
            target_tok = vague_fix_m.group(1)
            matched_files, _ = self._resolve_action_files(target_tok, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if matched_files:
                f0 = matched_files[0]
                shelf_dir = infer_shelf_folder_for_file(f0)
                cur_dir = Path(str(f0.get("folderFull") or Path(f0["path"]).parent)).resolve()
                if f0.get("suggestedName") and not f0.get("renamed"):
                    sugg = str(f0["suggestedName"])
                    items = [self._make_action_item(f0, f"{f0['name']} → {sugg}")]
                    proposed = {
                        "actionId": make_plan_id(),
                        "actionType": "rename_single",
                        "title": f"Rename “{f0['name']}” → “{sugg}”",
                        "summary": f"Apply clean filename derived from content ({f0.get('renameReason') or 'smart rename'}).",
                        "targetLabel": sugg,
                        "reversible": True,
                        "destructive": False,
                        "requiresSelection": True,
                        "items": items,
                        "payload": {"newName": sugg},
                    }
                    return _reply(
                        f"{name_prefix}I prepared a clean filename for **{f0['title']}**: `{f0['name']}` → `{sugg}`. Confirm below to apply.",
                        citations=[self._make_citation(f0, f"→ {sugg}", 95)],
                        proposed=proposed,
                    )
                if shelf_dir and cur_dir != shelf_dir:
                    dst_lbl = format_path_short(shelf_dir)
                    items = [self._make_action_item(f0, f"{f0.get('folder') or 'Current'} → {dst_lbl}/{f0['name']}")]
                    proposed = {
                        "actionId": make_plan_id(),
                        "actionType": "move",
                        "title": f"Move “{f0['name']}” to {dst_lbl}",
                        "summary": f"Organize file into its topic Shelf folder ({dst_lbl}).",
                        "targetLabel": dst_lbl,
                        "reversible": True,
                        "destructive": False,
                        "requiresSelection": True,
                        "items": items,
                        "payload": {"targetFolder": str(shelf_dir), "createTargetFolder": not shelf_dir.exists()},
                    }
                    return _reply(
                        f"{name_prefix}ready to organize **{f0['title']}** into its Shelf folder (**{dst_lbl}**). Confirm below to move it.",
                        citations=[self._make_citation(f0, f"Destination: {dst_lbl}", 94)],
                        proposed=proposed,
                    )

        # 0-PRE. DUPLICATE INSPECTION & CLEANUP (supports exact-only, folder/modality filters, keep_strategy & keepReason!)
        is_dup_intent = bool(
            re.search(
                r"\b(?:show|list|find|check|display|see|view|get|clean(?:\s+up)?|archive|remove|deduplicate|any|what)\b.*?\b(?:duplicates?|lookalikes?|duplicate\s+files?|duplicate\s+copies|duplicate\s+photos?|duplicate\s+images?|duplicate\s+pdfs?)\b",
                q_low,
            )
            or re.fullmatch(r"(?:exact\s+)?(?:duplicates?|all\s+duplicates?|lookalikes?)", q_low)
        )
        if is_dup_intent and not re.search(r"\b(?:unarchive|restore)\b", q_low):
            audit = build_workspace_process_audit(state, all_files, self.preview_tidy_folders(), scope_folder=scope_folder)
            pairs = list(audit["dupPairs"])
            by_id = {f["id"]: f for f in all_files_raw}

            # Filter exact-only / folder / modality if requested in prompt (strip leading action verb so "archive" isn't parsed as ZIP modality!)
            if re.search(r"\bexact\s+duplicates?\b", q_low):
                pairs = [p for p in pairs if p.get("isExact") or int(p.get("similarity_pct") or 0) == 100]
            dup_filter_q = re.sub(
                r"^(?:show(?:\s+me)?|list|find|check|display|see|view|get|clean(?:\s+up)?|archive|remove|deduplicate)\s+",
                "",
                q_stripped,
                flags=re.IGNORECASE,
            ).strip()
            dup_spec = parse_file_query(dup_filter_q, state=state)
            if dup_spec.folder_hints:
                def _pair_in_folder(p: dict[str, Any]) -> bool:
                    for side in ("keep", "copy"):
                        fid_s = (p.get(side) or {}).get("id")
                        f_s = by_id.get(fid_s)
                        if f_s and any(fh in str(f_s.get("folderFull") or f_s.get("folder") or "").lower() for fh in dup_spec.folder_hints):
                            return True
                    return False
                pairs = [p for p in pairs if _pair_in_folder(p)]
            if dup_spec.modalities or dup_spec.exts:
                def _pair_matches_type(p: dict[str, Any]) -> bool:
                    f_s = by_id.get((p.get("keep") or {}).get("id")) or by_id.get((p.get("copy") or {}).get("id"))
                    if not f_s:
                        return False
                    mod_s = str(f_s.get("modality") or "").lower()
                    ext_s = str(f_s.get("format") or "").lower()
                    return (mod_s in dup_spec.modalities) or (ext_s in dup_spec.exts)
                pairs = [p for p in pairs if _pair_matches_type(p)]

            # Optional keep_strategy override ("keeping the oldest", "keeping the newest", "keeping the largest")
            keep_strat = "best"
            ks_m = re.search(r"\bkeeping\s+(?:the\s+)?(oldest|newest|largest|biggest)\b", q_low)
            if ks_m:
                keep_strat = "largest" if ks_m.group(1) in {"largest", "biggest"} else ks_m.group(1)

            if not pairs:
                return _reply(
                    f"{name_prefix}your files look spotless: **0 unresolved duplicate pairs** matching that criteria!",
                    mode="workspace-insight",
                    choices=_build_audit_choices(audit),
                    process_summary=audit["processSummary"],
                    trace=[*scope_trace, "Checked duplicate/lookalike index (0 matching)"],
                )

            items = []
            pair_ids = []
            dup_lines: list[str] = []
            cites: list[dict[str, Any]] = []
            reclaim_filtered = round(sum(float(p.get("saved_mb") or 0.0) for p in pairs), 2)
            for idx, p in enumerate(pairs[:50], start=1):
                f_keep = by_id.get((p.get("keep") or {}).get("id"))
                f_copy = by_id.get((p.get("copy") or {}).get("id"))
                keep_reason = str(p.get("keepReason") or "higher keep score")
                if keep_strat != "best" and f_keep and f_copy:
                    chosen_keep, chosen_copy, keep_reason = pick_keep_and_copy(f_keep, f_copy, keep_strategy=keep_strat)
                    f_keep, f_copy = chosen_keep, chosen_copy
                copy_name = (f_copy["name"] if f_copy else (p.get("copy") or {}).get("name", "copy"))
                keep_name = (f_keep["name"] if f_keep else (p.get("keep") or {}).get("name", "original"))
                pct = p.get("similarity_pct", 100)
                badge = p.get("badge") or "Match"
                saved = p.get("saved_mb", 0)
                dup_lines.append(
                    f"{idx}. **Archive copy:** `{copy_name}` ↔ **Keep:** `{keep_name}` *({keep_reason})* · {badge} ({pct}%) · saves {saved} MB"
                )
                if f_copy:
                    items.append(self._make_action_item(f_copy, f"Archive copy · Keeping {keep_name} ({keep_reason} · saves {saved} MB)"))
                    pair_ids.append(p["id"])
                    if len(cites) < 16:
                        cites.append(self._make_citation(f_copy, f"Duplicate of {keep_name} ({pct}% · {keep_reason})", int(pct)))
            more_note = f"\n- *…plus {len(pairs) - 50} more pair(s).*" if len(pairs) > 50 else ""
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "archive_duplicates",
                "title": f"Archive {len(items)} duplicate copy(ies) ({reclaim_filtered} MB)",
                "summary": "Safely move duplicate copies to GoodShelves_Archive while keeping originals intact (100% undoable).",
                "targetLabel": "GoodShelves_Archive",
                "reversible": True,
                "destructive": False,
                "requiresSelection": len(items) > 0,
                "items": items,
                "payload": {"pairIds": pair_ids, "keepStrategy": keep_strat},
            }
            md = (
                f"{name_prefix}I found **{len(pairs)} duplicate/lookalike pair(s)** "
                f"(**{reclaim_filtered} MB** reclaimable):\n\n"
                + "\n".join(dup_lines)
                + more_note
                + "\n\nUncheck any copy you want to keep (or say `swap keep and copy for pair 1`), then click **Confirm**."
            )
            return _reply(
                md,
                citations=cites,
                proposed=proposed,
                process_summary=audit["processSummary"],
                trace=[*scope_trace, f"Listed {len(pairs)} duplicate pair(s) ({reclaim_filtered} MB · strategy={keep_strat})"],
            )

        # 0-PRE-2. SYSTEM PROCESS AUDIT / BROAD ORGANIZE / CLEAN UP
        is_broad_audit_or_organize = bool(
            re.fullmatch(
                r"(?:clean(?:\s+up)?(?:\s+(?:my\s+|all\s+)?files)?|organize(?:\s+(?:my\s+|all\s+)?(?:files|everything|workspace|system))?|tidy(?:\s+up)?(?:\s+(?:my\s+|all\s+)?files)?|help\s+me\s+(?:clean(?:\s+up)?|organize|tidy(?:\s+up)?)(?:\s+(?:my\s+)?files)?|audit(?:\s+(?:workspace|files|system))?|system\s+status)",
                q_low,
            )
            or re.search(
                r"\b(?:what(?:'s| is)\s+(?:available|ready|there)\s+(?:to\s+process|for\s+process)|what\s+can\s+(?:be\s+processed|you\s+process|i\s+organize)|show\s+(?:me\s+)?(?:available\s+(?:actions|processes)|whats\s+on\s+there|what's\s+on\s+there|progress))\b",
                q_low,
            )
        )
        if is_broad_audit_or_organize:
            audit = build_workspace_process_audit(state, all_files, self.preview_tidy_folders(), scope_folder=scope_folder)
            scope_lbl = format_path_short(scope_folder) if scope_folder else None
            md = format_process_audit_markdown(audit, name_prefix=name_prefix, scope_label=scope_lbl)
            return _reply(
                md,
                proposed=None,
                mode="workspace-insight",
                choices=_build_audit_choices(audit),
                process_summary=audit["processSummary"],
                trace=[*scope_trace, f"Audited {audit['totalFiles']} file(s) · {audit['totalPending']} item(s) ready to process"],
            )

        # 0-PRE-3. EXPLICIT FULL ORGANIZE / SMART CLEAN / TIDY FOLDERS PLAN
        is_full_organize = bool(
            re.fullmatch(
                r"(?:run\s+(?:full\s+)?organize(?:\s+plan)?|run\s+smart\s+clean|execute\s+smart\s+clean|smart\s+clean|auto[- ]?organize(?:\s+everything|\s+all)?|organize\s+everything\s+now)",
                q_low,
            )
        )
        is_tidy_folders = bool(
            re.search(
                r"\b(?:organize|move|sort|tidy)\s+(?:all\s+|my\s+)?files\s+(?:into|to)\s+(?:shelf\s+folders|shelves\s+folders|folders\s+on\s+disk)\b",
                q_low,
            )
            or re.fullmatch(r"(?:tidy\s+folders|organize\s+folders|organize\s+into\s+shelves)", q_low)
        )
        if is_tidy_folders:
            audit = build_workspace_process_audit(state, all_files, self.preview_tidy_folders(), scope_folder=scope_folder)
            moves = audit["tidyMoves"]
            if not moves:
                return _reply(
                    f"{name_prefix}all files in scope are already neatly organized inside their Shelf folders!",
                    mode="workspace-insight",
                    choices=_build_audit_choices(audit),
                    process_summary=audit["processSummary"],
                )
            by_id = {f["id"]: f for f in all_files}
            items = []
            for mv in moves[:60]:
                f_obj = by_id.get(mv["fileId"])
                if f_obj:
                    items.append(self._make_action_item(f_obj, f"{mv['fromShort']} → {mv['toShort']}"))
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "tidy_folders",
                "title": f"Organize {len(items)} file(s) into ~/Shelves/ folders",
                "summary": "Move files on disk into topic-named Shelf folders under ~/Shelves/ (100% reversible).",
                "targetLabel": "~/Shelves/",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            preview_lines = [f"- `{mv['fromShort']}` → `{mv['toShort']}`" for mv in moves[:12]]
            if len(moves) > 12:
                preview_lines.append(f"- *…plus {len(moves) - 12} more move(s) in the checklist below.*")
            return _reply(
                f"{name_prefix}here are **{len(items)} file(s)** ready to organize into Shelf folders:\n"
                + "\n".join(preview_lines)
                + "\n\nUncheck any file you want to leave in place, then click **Confirm**.",
                proposed=proposed,
                process_summary=audit["processSummary"],
                trace=[*scope_trace, f"Prepared {len(items)} Shelf folder move(s)"],
            )

        if is_full_organize:
            audit = build_workspace_process_audit(state, all_files, self.preview_tidy_folders(), scope_folder=scope_folder)
            steps_payload: list[dict[str, Any]] = []
            by_id = {f["id"]: f for f in all_files_raw}
            step_idx = 0

            archived_copy_ids: set[str] = set()
            if audit["dupPairs"]:
                dup_items = []
                dup_pair_ids = []
                for p in audit["dupPairs"][:30]:
                    f_copy = by_id.get((p.get("copy") or {}).get("id"))
                    if f_copy:
                        archived_copy_ids.add(str(f_copy["id"]))
                        keep_n = (p.get("keep") or {}).get("name", "original")
                        it = self._make_action_item(
                            f_copy,
                            f"Archive copy · Keeping {keep_n} ({p.get('saved_mb', 0)} MB)",
                            step_index=step_idx,
                            step_title="Archive duplicates",
                        )
                        dup_items.append(it)
                        dup_pair_ids.append(p["id"])
                if dup_items:
                    steps_payload.append({
                        "stepIndex": step_idx,
                        "actionType": "archive_duplicates",
                        "title": f"Archive {len(dup_items)} duplicate copy(ies)",
                        "summary": "Move redundant copies to GoodShelves_Archive",
                        "targetLabel": "GoodShelves_Archive",
                        "reversible": True,
                        "destructive": False,
                        "requiresSelection": True,
                        "fileIds": [it["fileId"] for it in dup_items],
                        "items": dup_items,
                        "payload": {"pairIds": dup_pair_ids},
                    })
                    step_idx += 1

            if audit["renames"]:
                ren_cands = [f for f in audit["renames"] if str(f["id"]) not in archived_copy_ids][:40]
                if ren_cands:
                    sim_ren = simulate_batch_rename_collisions([(f, str(f["suggestedName"])) for f in ren_cands])
                    ren_map = {f["id"]: nn for f, nn, _ in sim_ren}
                    ren_items = [
                        self._make_action_item(f, f"{f['name']} → {nn}", step_index=step_idx, step_title="Smart rename")
                        for f, nn, _ in sim_ren
                    ]
                    steps_payload.append({
                        "stepIndex": step_idx,
                        "actionType": "rename_batch",
                        "title": f"Rename {len(ren_items)} messy filename(s)",
                        "summary": "Apply descriptive smart filenames",
                        "targetLabel": f"{len(ren_items)} renames",
                        "reversible": True,
                        "destructive": False,
                        "requiresSelection": True,
                        "fileIds": [it["fileId"] for it in ren_items],
                        "items": ren_items,
                        "payload": {"renameMap": ren_map},
                    })
                    step_idx += 1

            if audit["tidyMoves"]:
                mv_cands = [m for m in audit["tidyMoves"] if str(m["fileId"]) not in archived_copy_ids][:50]
                mv_items = []
                for mv in mv_cands:
                    f_obj = by_id.get(mv["fileId"])
                    if f_obj:
                        mv_items.append(
                            self._make_action_item(
                                f_obj,
                                f"{mv['fromShort']} → {mv['toShort']}",
                                step_index=step_idx,
                                step_title="Organize into Shelf folders",
                            )
                        )
                if mv_items:
                    steps_payload.append({
                        "stepIndex": step_idx,
                        "actionType": "tidy_folders",
                        "title": f"Organize {len(mv_items)} file(s) into ~/Shelves/",
                        "summary": "Move files into Shelf folders on disk",
                        "targetLabel": "~/Shelves/",
                        "reversible": True,
                        "destructive": False,
                        "requiresSelection": True,
                        "fileIds": [it["fileId"] for it in mv_items],
                        "items": mv_items,
                        "payload": {},
                    })
                    step_idx += 1

            if not steps_payload:
                return _reply(
                    f"{name_prefix}everything in your workspace is already clean and organized! Nothing pending to run.",
                    mode="workspace-insight",
                    choices=_build_audit_choices(audit),
                    process_summary=audit["processSummary"],
                )

            unified_items = merge_unique_plan_items(steps_payload)
            unique_count = len(unified_items)
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "multi_step",
                "title": f"Full organize workflow ({len(steps_payload)} step(s) · {unique_count} file(s))",
                "summary": " → ".join(st["title"] for st in steps_payload),
                "targetLabel": f"{len(steps_payload)} steps",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "steps": steps_payload,
                "items": unified_items,
                "payload": {"steps": steps_payload},
            }
            return _reply(
                f"{name_prefix}I prepared a **{len(steps_payload)}-step organize plan** covering everything ready to process:\n"
                + "\n".join(f"{idx}. {st['title']}" for idx, st in enumerate(steps_payload, start=1))
                + "\n\nReview the checklist below, uncheck anything to skip, and click **Confirm**.",
                proposed=proposed,
                process_summary=audit["processSummary"],
                trace=[*scope_trace, f"Built {len(steps_payload)}-step full organize plan ({unique_count} unique file(s))"],
            )

        # Never hijack pure query/search verbs (compound chains, pending edits, open/reveal & duplicates already handled above!)
        if re.match(r"^(find|show|list|search|where|what|when|who|why|how\s+many|how\s+much|summarize|explain|analyze|analyse|inspect|understand|reconcile|breakdown|break\s+down|connect\s+the\s+dots|tell\s+me)\b", q_low):
            return None

        # 0B-CMP. RICH MULTI-MODAL COMPARE / DIFF TWO FILES (full SQLite text + 512-d cosine + entity deltas!)
        cmp_m = re.match(r"^(?:compare|diff|what(?:'s| is)\s+different\s+between)\s+(.+?)\s+(?:and|vs\.?|with|against)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if cmp_m:
            left_q = cmp_m.group(1).strip(" \"'“”")
            right_q = cmp_m.group(2).strip(" \"'“”")
            left_hits, _ = self._resolve_action_files(left_q, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            right_hits, _ = self._resolve_action_files(right_q, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            f_a = left_hits[0] if left_hits else None
            f_b = next((r for r in right_hits if not f_a or r["id"] != f_a["id"]), right_hits[0] if right_hits else None)
            if f_a and f_b:
                md, cmp_trace = compare_files_rich(self, f_a, f_b, name_prefix=name_prefix)
                return _reply(
                    md,
                    citations=[self._make_citation(f_a, score=95), self._make_citation(f_b, score=94)],
                    mode="workspace-insight",
                    choices=[
                        {"label": "Check duplicates", "prompt": "show me all the duplicates", "kind": "action"},
                        {"label": f"Pin {f_a['name'][:20]}", "prompt": f"pin {f_a['name']}", "kind": "action"},
                    ],
                    trace=[*scope_trace, *cmp_trace],
                )

        # 0E. REMEMBER EXPLICIT RULE ("remember receipts go in Finance", "whenever a receipt is dropped, auto-tag it #receipt")
        watch_rule_m = re.match(
            r"^(?:watch\s+rule\s*[:\-]?\s*)?(?:whenever|when|every\s+time|always)\s+(?:a\s+|an\s+)?(.+?)\s+(?:is\s+(?:dropped|added|uploaded|indexed|ingested)|are\s+(?:dropped|added))[,\s]+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        rem_m = watch_rule_m or re.match(r"^remember(?:\s+that)?\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if rem_m:
            if watch_rule_m:
                subj_pat = re.sub(r"^(?:new|dropped|added)\s+", "", watch_rule_m.group(1).strip(), flags=re.IGNORECASE).lower()
                act_val = watch_rule_m.group(2).strip(" \"'“”?!.")
                rule_txt = f"When {subj_pat} is dropped → {act_val}"
                key_pat = subj_pat[:60]
                val_txt = act_val
                kind_mem = "watch_rule"
            else:
                rule_txt = rem_m.group(1).strip(" \"'“”?!.")
                route_m = re.match(r"^(?:my\s+)?(.+?)\s+(?:go|goes|belong|belongs|should\s+go|move)\s+(?:in|into|to)\s+(.+)$", rule_txt, flags=re.IGNORECASE)
                key_pat = rule_txt.lower()[:80]
                val_txt = rule_txt
                kind_mem = "user_rule"
                if route_m:
                    key_pat = re.sub(r"^(?:all|the|my)\s+", "", route_m.group(1).strip(), flags=re.IGNORECASE).lower()
                    dst_info = self._resolve_folder_candidate(route_m.group(2).strip(), all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
                    if dst_info:
                        kind_mem = "route"
                        val_txt = dst_info["path"]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "remember",
                "title": f"Save AI preference: “{rule_txt[:60]}”",
                "summary": "Persist this rule in local AI memory so future file drops, searches, and organization plans follow it.",
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
        pf_m = re.match(r"^(pin|unpin)\s+(?:the\s+)?folder\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if pf_m:
            verb = pf_m.group(1).lower()
            folder_hint = pf_m.group(2).strip(" \"'“”?!.")
            f_info = self._resolve_folder_candidate(folder_hint, all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
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

        # 0F-2. SHELF CONTROLS: rename_shelf, delete_shelf, pin_shelf, unpin_shelf, unshelve
        rsh_m = re.match(r"^rename\s+(?:the\s+)?shelf\s+[\"“']?(.+?)[\"”']?\s+to\s+[\"“']?(.+?)[\"”']?$", q_stripped, flags=re.IGNORECASE)
        if rsh_m:
            old_sh_hint = rsh_m.group(1).strip(" \"'“”")
            new_sh_name = rsh_m.group(2).strip(" \"'“”?!.")
            sh_match = next((s for s in shelves if s["name"].lower() == old_sh_hint.lower() or old_sh_hint.lower() in s["name"].lower()), None)
            if not sh_match:
                return _reply(f"{name_prefix}I couldn't find a shelf named “{old_sh_hint}”.", mode="conversational")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "rename_shelf",
                "title": f"Rename shelf “{sh_match['name']}” → “{new_sh_name}”",
                "summary": f"Update shelf display name to “{new_sh_name}” (reversible via Undo).",
                "targetLabel": new_sh_name,
                "reversible": True,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"shelfId": sh_match["id"], "shelfName": sh_match["name"], "newName": new_sh_name},
            }
            return _reply(f"{name_prefix}ready to rename shelf **{sh_match['name']}** to **{new_sh_name}**. Confirm below.", proposed=proposed)

        dsh_m = re.match(r"^(?:delete|remove)\s+(?:the\s+)?(?:custom\s+)?shelf\s+[\"“']?(.+?)[\"”']?$", q_stripped, flags=re.IGNORECASE)
        if dsh_m:
            sh_hint = dsh_m.group(1).strip(" \"'“”?!.")
            sh_match = next((s for s in shelves if s.get("isCustom") and (s["name"].lower() == sh_hint.lower() or sh_hint.lower() in s["name"].lower())), None)
            if not sh_match:
                return _reply(f"{name_prefix}I couldn't find a custom shelf named “{sh_hint}” to delete (only custom shelves can be deleted).", mode="conversational")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "delete_shelf",
                "title": f"Delete custom shelf “{sh_match['name']}”",
                "summary": "Remove this virtual shelf (your real files on disk stay untouched; reversible via Undo).",
                "targetLabel": sh_match["name"],
                "reversible": True,
                "destructive": True,
                "requiresSelection": False,
                "items": [],
                "payload": {"shelfId": sh_match["id"], "shelfName": sh_match["name"]},
            }
            return _reply(f"{name_prefix}ready to delete custom shelf **{sh_match['name']}** (your files on disk remain untouched). Confirm below.", proposed=proposed)

        psh_m = re.match(r"^(pin|unpin)\s+(?:the\s+)?shelf\s+[\"“']?(.+?)[\"”']?$", q_stripped, flags=re.IGNORECASE)
        if psh_m:
            verb = psh_m.group(1).lower()
            sh_hint = psh_m.group(2).strip(" \"'“”?!.")
            sh_match = next((s for s in shelves if s["name"].lower() == sh_hint.lower() or sh_hint.lower() in s["name"].lower()), None)
            if not sh_match:
                return _reply(f"{name_prefix}I couldn't find a shelf matching “{sh_hint}”.", mode="conversational")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "pin_shelf" if verb == "pin" else "unpin_shelf",
                "title": f"{verb.capitalize()} shelf “{sh_match['name']}”",
                "summary": f"{'Pin' if verb == 'pin' else 'Unpin'} shelf “{sh_match['name']}” in your sidebar.",
                "targetLabel": sh_match["name"],
                "reversible": False,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"shelfId": sh_match["id"], "shelfName": sh_match["name"]},
            }
            return _reply(f"{name_prefix}confirm below to {verb} shelf **{sh_match['name']}**.", proposed=proposed)

        unsh_m = re.match(r"^(?:unshelve|unassign\s+shelf\s+(?:from|for)|reset\s+shelf\s+(?:for|on))\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if unsh_m:
            tgt_p = unsh_m.group(1).strip()
            matched_files, _ = self._resolve_action_files(tgt_p, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find matching files for “{tgt_p}” to unshelve.", mode="conversational")
            items = [self._make_action_item(f, f"Reset shelf ({f.get('shelfName') or ''} → Auto)") for f in matched_files]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "unshelve",
                "title": f"Unshelve {len(items)} file(s) (return to Auto)",
                "summary": "Clear manual shelf override so Good Shelves auto-organizes these files (undoable).",
                "targetLabel": "Auto Shelf",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"shelfId": "auto"},
            }
            return _reply(f"{name_prefix}ready to reset shelf override for {len(items)} file(s). Confirm below.", proposed=proposed)

        # 0G. RUN OCR ON IMAGES / SCANNED PDFS (fixes Proactive Insight #1: "run ocr on all images and PDFs"!)
        ocr_m = re.match(r"^(?:run\s+ocr|ocr|extract\s+text(?:\s+with\s+ocr)?)(?:\s+(?:on|for|from)\s+(.+))?$", q_stripped, flags=re.IGNORECASE)
        if ocr_m:
            target_phrase = (ocr_m.group(1) or "").strip()
            if target_phrase:
                matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
                # Prefer items missing OCR when user ran a broad command like "all images and PDFs"
                if re.search(r"\b(?:all|every|missing)\b", target_phrase.lower()):
                    missing_only = [
                        f for f in matched_files
                        if (f.get("modality") == "image" or str(f.get("format", "")).upper() == "PDF")
                        and not str(f.get("ocrText") or "").strip()
                    ]
                    if missing_only:
                        matched_files = missing_only
            else:
                matched_files = [
                    f for f in all_files
                    if (f.get("modality") == "image" or str(f.get("format", "")).upper() == "PDF")
                    and not str(f.get("ocrText") or "").strip()
                ][:30]
                if not matched_files:
                    matched_files = [f for f in all_files if f.get("modality") == "image" or str(f.get("format", "")).upper() == "PDF"][:20]
            ocr_cands = [f for f in matched_files if f.get("modality") == "image" or str(f.get("format", "")).upper() == "PDF"]
            if not ocr_cands:
                return _reply(f"{name_prefix}I didn't find any images or PDFs matching that to run OCR on.", mode="conversational")
            items = [_make_item_with_mem(f, f"Extract text via RapidOCR ({f.get('format', '')} · {f.get('folder') or ''})") for f in ocr_cands[:30]]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "run_ocr",
                "title": f"Run RapidOCR on {len(items)} file(s)",
                "summary": "Extract searchable text from images & scanned PDFs (up to 12 pages per PDF) locally using RapidOCR.",
                "targetLabel": "RapidOCR Engine",
                "reversible": False,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            return _reply(
                f"{name_prefix}ready to run local RapidOCR on **{len(items)} image/PDF file(s)**. Confirm below to extract text.",
                citations=[self._make_citation(f, "OCR scan target", 90) for f in ocr_cands[:12]],
                proposed=proposed,
            )

        # 0G-2. FORMAT CONVERSIONS (XLSX->CSV, PDF->TXT, video->frame)
        conv_csv_m = re.match(r"^(?:convert|export)\s+(.+?)\s+(?:to|as)\s+csv$", q_stripped, flags=re.IGNORECASE)
        conv_txt_m = re.match(r"^(?:convert|export)\s+(.+?)\s+(?:to|as)\s+(?:txt|text)|^extract\s+pdf\s+text\s+(?:from\s+)?(.+)$", q_stripped, flags=re.IGNORECASE)
        conv_frm_m = re.match(r"^extract\s+(?:a\s+)?(?:video\s+)?frame\s+(?:from\s+)?(.+)$", q_stripped, flags=re.IGNORECASE)
        if conv_csv_m or conv_txt_m or conv_frm_m:
            if conv_csv_m:
                fa, tgt_p, fa_lbl = "export_csv", conv_csv_m.group(1).strip(), "Export to CSV"
            elif conv_txt_m:
                fa, tgt_p, fa_lbl = "extract_pdf_text", (conv_txt_m.group(1) or conv_txt_m.group(2) or "").strip(), "Extract PDF text (.txt)"
            else:
                fa, tgt_p, fa_lbl = "extract_frame", conv_frm_m.group(1).strip(), "Extract video frame (.png)"  # type: ignore[union-attr]
            matched_files, _ = self._resolve_action_files(tgt_p, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find a file matching “{tgt_p}” for {fa_lbl}.", mode="conversational")
            items = [self._make_action_item(f, f"{fa_lbl} → {f.get('folder') or ''}") for f in matched_files[:10]]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "file_convert",
                "title": f"{fa_lbl} ({len(items)} file(s))",
                "summary": f"Run {fa_lbl} locally and index the generated output file.",
                "targetLabel": fa_lbl,
                "reversible": False,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"fileAction": fa},
            }
            return _reply(f"{name_prefix}ready to run **{fa_lbl}** on {len(items)} file(s). Confirm below.", proposed=proposed)

        # 0H. LEARNED SMART ORGANIZE OR FOLDER-SCOPED AUDIT ("organize my receipts", "organize Downloads")
        org_m = re.match(r"^(?:organize|sort|auto-?organize|file)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if org_m and not re.search(r"\b(?:to|into|under|shelf)\b", q_stripped, flags=re.IGNORECASE):
            target_phrase = org_m.group(1).strip()
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
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
            folder_cand = self._resolve_folder_candidate(target_phrase, all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
            if folder_cand:
                folder_files = filter_files_by_scope(all_files_raw, folder_cand["path"], recursive=True)
                audit = build_workspace_process_audit(state, folder_files, self.preview_tidy_folders(), scope_folder=folder_cand["path"])
                md = format_process_audit_markdown(audit, name_prefix=name_prefix, scope_label=folder_cand["label"])
                return _reply(
                    md,
                    proposed=None,
                    mode="workspace-insight",
                    choices=_build_audit_choices(audit),
                    process_summary=audit["processSummary"],
                    trace=[*scope_trace, f"Audited folder {folder_cand['label']} ({len(folder_files)} file(s))"],
                )
            if matched_files:
                audit = build_workspace_process_audit(state, matched_files, self.preview_tidy_folders(), scope_folder=scope_folder)
                md = format_process_audit_markdown(audit, name_prefix=name_prefix, scope_label=f"matching “{target_phrase}”")
                return _reply(
                    md,
                    citations=[self._make_citation(f, f"{f.get('shelfName') or 'Workspace'} · {f['size']}", 88) for f in matched_files[:12]],
                    proposed=None,
                    mode="workspace-insight",
                    choices=_build_audit_choices(audit),
                    process_summary=audit["processSummary"],
                    trace=[*scope_trace, f"Audited {len(matched_files)} matching file(s) for organize"],
                )

        # 1. UNDO & REDO LAST ACTION
        if re.fullmatch(r"(?:undo|revert)(?:\s+(?:that|this|it|last\s+action|the\s+last\s+action|my\s+last\s+action))?", q_low):
            log_items = self.get_action_log(limit=1)
            if not log_items:
                return _reply(f"There aren't any recent actions in your undo history yet{name_comma}.", mode="conversational")
            latest = log_items[0]
            proposed = {
                "actionId": make_plan_id(),
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

        if re.fullmatch(r"redo(?:\s+(?:that|this|it|last\s+action|the\s+last\s+action|my\s+last\s+action))?", q_low):
            with self._lock:
                conn = self._connect()
                try:
                    u_row = conn.execute("SELECT id, summary FROM action_log WHERE undone = 1 ORDER BY id DESC LIMIT 1").fetchone()
                finally:
                    conn.close()
            if not u_row:
                return _reply(f"There isn't any undone action ready to redo{name_comma}.", mode="conversational")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "redo",
                "title": f"Redo: {u_row['summary']}",
                "summary": f"Re-apply “{u_row['summary']}”.",
                "targetLabel": "Workspace History",
                "reversible": True,
                "destructive": False,
                "requiresSelection": False,
                "items": [],
                "payload": {"actionId": int(u_row["id"])},
            }
            return _reply(
                f"{name_prefix}I can redo: “{u_row['summary']}”. Confirm below to re-apply it.",
                proposed=proposed,
            )

        # 2. RESTORE ARCHIVED FILES ("unarchive duplicates", "restore archived files")
        if re.search(r"\b(?:unarchive|restore\s+(?:all\s+|my\s+)?archived(?:\s+(?:files?|duplicates?|copies?))?|restore\s+(?:all\s+)?duplicates?)\b", q_low):
            archived_list = self.list_trash_and_archive().get("archived", [])
            if not archived_list:
                return _reply(f"There are no archived duplicate files waiting in GoodShelves_Archive{name_comma}.", mode="conversational")
            m_arch_t = re.sub(r"^.*?\b(?:unarchive|restore)\b\s*(?:all\s+|my\s+)?(?:archived\s+)?(?:files?|duplicates?|copies?)?\s*", "", q_stripped, flags=re.IGNORECASE).strip(" ?!.")
            matched_arch = archived_list
            if m_arch_t and m_arch_t.lower() not in {"all", "everything", "them", "these", "those"}:
                matched_arch = [a for a in archived_list if m_arch_t.lower() in str(a.get("name") or "").lower()]
                if not matched_arch:
                    return _reply(
                        f"{name_prefix}I didn't find any archived file matching “{m_arch_t}” in GoodShelves_Archive.",
                        mode="conversational",
                        choices=[{"label": f"Restore all {len(archived_list)} archived", "prompt": "unarchive duplicates", "kind": "action"}],
                    )
            items = [{
                "fileId": str(a["fileId"]),
                "fileName": str(a["name"]),
                "title": str(a["name"]),
                "format": Path(str(a["name"])).suffix.lstrip(".").upper() or "FILE",
                "modality": "document",
                "folder": "GoodShelves_Archive",
                "path": str(a["archivedPath"]),
                "size": "",
                "date": "",
                "shelfName": "Archive",
                "detail": f"Restore → {a.get('originalPath') or 'original folder'}",
                "selectedByDefault": True,
            } for a in matched_arch[:50]]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "restore_archive",
                "title": f"Restore {len(items)} archived file(s)",
                "summary": "Move archived files from GoodShelves_Archive back to their original paths.",
                "targetLabel": "Original locations",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"pairIds": [str(a["pairId"]) for a in matched_arch[:50]]},
            }
            return _reply(
                f"{name_prefix}I found **{len(items)} archived file(s)** ready to restore. Review below and click **Confirm**.",
                proposed=proposed,
            )

        # 2B. RESTORE TRASHED FILES (FIXED: NEVER silently falls back to `or trashed`!)
        if re.search(r"\b(restore|recover|untrash)\b", q_low):
            trashed = self.list_trash_and_archive().get("trashed", [])
            if not trashed:
                return _reply(f"Your Good Shelves Trash is currently empty{name_comma}.", mode="conversational")
            m_target = re.sub(r"^.*?\b(?:restore|recover|untrash)\b\s*", "", q_stripped, flags=re.IGNORECASE).strip(" ?!.")
            mt_low = m_target.lower()
            matched: list[dict[str, Any]] = []
            if not m_target or mt_low in {"all", "all files", "everything", "trash", "trashed files", "the trash", "all trashed files", "all deleted files"}:
                matched = trashed
            elif re.search(r"\b(?:last|latest|most\s+recent)\s+(?:deleted|trashed|removed)?\s*(?:file|item|one)?\b", mt_low):
                matched = [trashed[0]]
            elif re.search(r"\b(?:pdfs?|images?|photos?|pictures?|docs?|documents?|videos?)\b", mt_low):
                matched = self._filter_by_modality_hint(trashed, mt_low)
            else:
                clean_mt = re.sub(r"^(?:the\s+|my\s+)?(?:trashed\s+|deleted\s+)?(?:file\s+)?", "", mt_low).strip()
                matched = [
                    f for f in trashed
                    if clean_mt in str(f.get("name") or "").lower()
                    or clean_mt in str(f.get("title") or "").lower()
                ]
            if not matched:
                sample_choices = [
                    {"label": f"Restore {f['name'][:22]}", "prompt": f"restore {f['name']}", "kind": "action"}
                    for f in trashed[:2]
                ]
                sample_choices.append({"label": f"Restore all ({len(trashed)})", "prompt": "restore all trashed files", "kind": "action"})
                sample_list = ", ".join(f"`{f['name']}`" for f in trashed[:5])
                return _reply(
                    f"{name_prefix}I didn't find “{m_target}” in Trash. Currently in Trash ({len(trashed)}): {sample_list}.",
                    mode="conversational",
                    choices=sample_choices[:3],
                )
            items = [self._make_action_item(f, f"Restore to {f.get('originalPath') or f.get('folder') or 'original folder'}") for f in matched[:50]]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "restore_trash",
                "title": f"Restore {len(items)} trashed file(s)",
                "summary": "Return selected files from GoodShelves_Trash back to their original locations (undoable).",
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

        # 2C. ARCHIVE FILES ("archive old screenshots", "archive these files")
        arch_m = re.match(r"^archive\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if arch_m and not re.search(r"\b(?:duplicates?|lookalikes?)\b", q_low):
            tgt_arch = arch_m.group(1).strip()
            matched_files, _ = self._resolve_action_files(tgt_arch, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find any files matching “{tgt_arch}” to archive.", mode="conversational")
            items = [_make_item_with_mem(f, f"Archive → GoodShelves_Archive/{f['name']}") for f in matched_files]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "archive_files",
                "title": f"Archive {len(items)} file(s)",
                "summary": "Safely move selected files out of active shelves into GoodShelves_Archive (100% reversible).",
                "targetLabel": "GoodShelves_Archive",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            return _reply(
                f"{name_prefix}ready to archive **{len(items)} file(s)** to `GoodShelves_Archive`. Confirm below.",
                citations=[self._make_citation(f, "Archive target", 90) for f in matched_files[:12]],
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
            in_split = re.match(r"^(.*?)\s+(?:inside|in|under)\s+(.+)$", folder_name, flags=re.IGNORECASE)
            if in_split and not parent_hint:
                folder_name = in_split.group(1).strip(" \"'“”")
                parent_hint = in_split.group(2).strip()
            parent_info = (
                self._resolve_folder_candidate(parent_hint, all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
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
                "actionId": make_plan_id(),
                "actionType": "create_folder",
                "title": f"Create folder “{folder_name}”",
                "summary": f"Create a new folder at {format_path_short(full_preview)} and start watching it (undoable).",
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
                r"^shelve\s+(.+?)\s+(?:under|in|to|as|into|onto|on)\s+[\"“']?(.+?)[\"”']?$",
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
                matched_files, meta = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
                if not matched_files and not existing_shelf and cs_m is None:
                    return _reply(
                        f"{name_prefix}I couldn't find any files matching “{target_phrase}” to place on shelf “{shelf_name}”.",
                        mode="conversational",
                    )

            items = [self._make_action_item(f, f"{f.get('shelfName') or 'Workspace'} → {shelf_name}") for f in matched_files]
            if existing_shelf and matched_files:
                proposed = {
                    "actionId": make_plan_id(),
                    "actionType": "shelve",
                    "title": f"Shelve {len(items)} item(s) under “{existing_shelf['name']}”",
                    "summary": f"Pin these {len(items)} item(s) onto existing shelf “{existing_shelf['name']}” (keeps disk folders untouched, undoable).",
                    "targetLabel": existing_shelf["name"],
                    "reversible": True,
                    "destructive": False,
                    "requiresSelection": True,
                    "items": items,
                    "payload": {"shelfId": existing_shelf["id"], "shelfName": existing_shelf["name"]},
                }
            else:
                proposed = {
                    "actionId": make_plan_id(),
                    "actionType": "create_shelf",
                    "title": f"Create shelf “{shelf_name}”" + (f" with {len(items)} item(s)" if items else ""),
                    "summary": f"Add a curated virtual shelf named “{shelf_name}” without moving files on disk (undoable).",
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
                in_parent = re.match(r"^(?:in|from|inside)\s+(?:the\s+)?(.+)$", src_hint, flags=re.IGNORECASE)
                dst_hint = mf_m.group(2).strip()
                dst_info = self._resolve_folder_candidate(dst_hint, all_files_raw, watched, allow_create_sub=True, conv_ctx=conv_ctx)
                if not dst_info:
                    return _reply(f"{name_prefix}please specify which destination folder you'd like to move those folders into.", mode="conversational")

                folder_targets: list[Path] = []
                if in_parent:
                    parent_info = self._resolve_folder_candidate(in_parent.group(1), all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
                    if parent_info and Path(parent_info["path"]).is_dir():
                        for child in sorted(Path(parent_info["path"]).iterdir()):
                            if child.is_dir() and not child.name.startswith(".") and child.resolve() != Path(dst_info["path"]).resolve():
                                folder_targets.append(child.resolve())
                else:
                    src_info = self._resolve_folder_candidate(src_hint, all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
                    if src_info:
                        folder_targets.append(Path(src_info["path"]).resolve())

                if not folder_targets:
                    return _reply(f"{name_prefix}I couldn't find any matching subfolder to move for “{src_hint}”.", mode="conversational")

                items = []
                for fp in folder_targets:
                    count_inside = sum(1 for f in all_files_raw if fp == Path(f["path"]).parent or fp in Path(f["path"]).parents)
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
                    "actionId": make_plan_id(),
                    "actionType": "move_folder",
                    "title": f"Move {len(items)} folder(s) to {dst_info['label']}",
                    "summary": f"Move {len(items)} folder(s) and their contents into {dst_info['label']} (undoable).",
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
                src_info = self._resolve_folder_candidate(src_hint, all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
                if not src_info:
                    return _reply(f"{name_prefix}I couldn't find a folder matching “{src_hint}”.", mode="conversational")
                fp = Path(src_info["path"]).resolve()
                try:
                    _validate_mutable_folder(fp)
                except ValueError as exc:
                    return _reply(f"{name_prefix}I can't delete that folder for safety: {exc}.", mode="conversational")
                count_inside = sum(1 for f in all_files_raw if fp == Path(f["path"]).parent or fp in Path(f["path"]).parents)
                proposed = {
                    "actionId": make_plan_id(),
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
                src_info = self._resolve_folder_candidate(src_hint, all_files_raw, watched, allow_create_sub=False, conv_ctx=conv_ctx)
                if not src_info:
                    return _reply(f"{name_prefix}I couldn't find a folder matching “{src_hint}”.", mode="conversational")
                fp = Path(src_info["path"]).resolve()
                proposed = {
                    "actionId": make_plan_id(),
                    "actionType": "rename_folder",
                    "title": f"Rename folder “{fp.name}” → “{new_fname}”",
                    "summary": f"Rename {src_info['label']} on disk and update all indexed file paths (undoable).",
                    "targetLabel": new_fname,
                    "reversible": True,
                    "destructive": False,
                    "requiresSelection": False,
                    "items": [],
                    "payload": {"folderPath": str(fp), "newName": new_fname},
                }
                return _reply(
                    f"{name_prefix}confirm below to rename folder “{fp.name}” to “{new_fname}”.",
                    proposed=proposed,
                )

        # 6. MOVE / COPY FILES (with pre-flight collision previews + real undo on both move & copy!)
        mv_m = re.match(
            r"^(move|transfer|relocate|copy|duplicate|put|place|send|shift|drop|toss)\s+(.+?)\s+(?:to|into|in|onto|under|inside)\s+(.+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if not mv_m:
            mv_there_m = re.match(
                r"^(move|transfer|relocate|put|place|send|file)\s+(.+?)\s+(there|in\s+there|into\s+there|over\s+there|where\s+it\s+belongs|where\s+they\s+belong|away|to\s+its\s+shelf|into\s+its\s+shelf)$",
                q_stripped,
                flags=re.IGNORECASE,
            )
            if mv_there_m:
                raw_dest = mv_there_m.group(3).strip()
                if raw_dest.lower() == "away":
                    raw_dest = "where it belongs"
                mv_m = re.match(
                    r"^(\w+)\s+(.+?)\s+__DEST__(.+)$",
                    f"{mv_there_m.group(1)} {mv_there_m.group(2)} __DEST__{raw_dest}",
                    flags=re.IGNORECASE,
                )
        if mv_m:
            verb = mv_m.group(1).lower()
            is_copy = verb in {"copy", "duplicate"}
            target_phrase = mv_m.group(2).strip()
            dest_phrase = re.sub(r"^(?:the\s+)?folder\s+", "", mv_m.group(3).strip(" \"'“”?!."), flags=re.IGNORECASE).strip()

            matched_files, meta = self._resolve_action_files(
                target_phrase,
                scoped_state,
                selected_file_id,
                recent_file_ids,
                conv_ctx=conv_ctx,
            )
            if meta.get("needsTargetClarification") or not matched_files:
                return _reply(
                    f"{name_prefix}I didn't find matching files for “{target_phrase}”. "
                    "Try naming a title, folder, or file type (for example: “move raw interview transcript to Documents”).",
                    mode="conversational",
                )

            src_parents = [Path(f["folderFull"]).parent for f in matched_files if f.get("folderFull")]
            preferred_parent = src_parents[0] if src_parents and src_parents[0].exists() else None
            dst_info = self._resolve_folder_candidate(
                dest_phrase,
                all_files_raw,
                watched,
                allow_create_sub=not is_deictic_folder_phrase(dest_phrase),
                preferred_parent=preferred_parent,
                source_files=matched_files,
                conv_ctx=conv_ctx,
            )
            if not dst_info:
                return _reply(
                    f"{name_prefix}I found {len(matched_files)} file(s), but couldn't resolve destination “{dest_phrase}”.",
                    citations=[self._make_citation(f, score=88) for f in matched_files[:12]],
                    mode="conversational",
                )

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
            occ_names: set[str] = set()
            dst_dir_obj = Path(dst_info["path"])
            items = []
            for f in eligible:
                cand_n, had_col = self._preview_collision_name(dst_dir_obj, f["name"], occ_names)
                col_note = " (collision → auto-renamed)" if had_col else ""
                items.append(_make_item_with_mem(f, f"{f.get('folder') or 'Current'} → {dst_info['label']}/{cand_n}{col_note}"))

            proposed = {
                "actionId": make_plan_id(),
                "actionType": action_type,
                "title": f"{verb_label} {len(items)} file(s) to {dst_info['label']}",
                "summary": f"{verb_label} {len(items)} file(s) into {dst_info['label']}{create_note} (undoable).",
                "targetLabel": dst_info["label"],
                "reversible": True,
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
            matched_files, _ = self._resolve_action_files(incomplete_mv.group(2).strip(), scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if matched_files:
                return _reply(
                    f"{name_prefix}I found {len(matched_files)} matching file(s). Where would you like to {incomplete_mv.group(1).lower()} them? "
                    "For example, reply with “move them to Archive”.",
                    citations=[self._make_citation(f, score=85) for f in matched_files[:12]],
                    mode="conversational",
                )

        # 7. DELETE / TRASH FILES (safety guard: pinned files start UNCHECKED with ⚠ Pinned file warning!)
        del_m = re.match(
            r"^(permanently\s+)?(delete|remove|trash|erase|discard|get\s+rid\s+of)\s+(.+)$",
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
            is_permanent = bool(re.search(r"\b(permanently|forever|for\s+good)\b", q_low))
            if len(del_m.groups()) == 3 and del_m.group(3) and del_m.group(3).lower() in {"permanently", "forever"}:
                target_phrase = del_m.group(2).strip()
            else:
                target_phrase = re.sub(r"\b(permanently|forever|for\s+good)\b", "", del_m.group(3).strip(), flags=re.IGNORECASE).strip()

            matched_files, meta = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
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
            pinned_cnt = sum(1 for f in matched_files if f.get("pinned"))
            items = [
                _make_item_with_mem(
                    f,
                    f"{'Permanent delete' if is_permanent else 'Move to Trash'} from {f.get('folder') or 'workspace'}",
                    destructive=True,
                )
                for f in matched_files
            ]
            pin_warn = f" ({pinned_cnt} pinned file(s) unchecked automatically for safety)" if pinned_cnt else ""
            proposed = {
                "actionId": make_plan_id(),
                "actionType": action_type,
                "title": f"{'Permanently delete' if is_permanent else 'Move to Trash'}: {len(items)} file(s)",
                "summary": dest_note + pin_warn,
                "targetLabel": "Permanent Delete" if is_permanent else "GoodShelves_Trash",
                "reversible": not is_permanent,
                "destructive": True,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            return _reply(
                f"{name_prefix}I found {len(items)} matching file(s) to {'permanently delete' if is_permanent else 'move to Trash'}.{pin_warn} "
                "Please double-check the list below and click Confirm.",
                citations=[self._make_citation(f, dest_note, 94) for f in matched_files[:12]],
                proposed=proposed,
            )

        # 8. TAGS: rename_tag, clear_tags, auto_tag, untag, tag
        rtag_m = re.match(
            r"^rename\s+tag\s+#?([A-Za-z0-9_\-]+)\s+to\s+#?([A-Za-z0-9_\-]+)$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if rtag_m:
            old_t = rtag_m.group(1).strip(" #").lower()
            new_t = rtag_m.group(2).strip(" #").lower()
            tagged_files = [f for f in all_files if old_t in [str(t).lower() for t in (f.get("tags") or [])]]
            if not tagged_files:
                return _reply(f"{name_prefix}no files currently have tag `#{old_t}`.", mode="conversational")
            items = [self._make_action_item(f, f"#{old_t} → #{new_t}") for f in tagged_files]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "rename_tag",
                "title": f"Rename tag #{old_t} → #{new_t} ({len(items)} file(s))",
                "summary": f"Replace #{old_t} with #{new_t} across {len(items)} file(s) (undoable).",
                "targetLabel": f"#{new_t}",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"oldTag": old_t, "newTag": new_t},
            }
            return _reply(f"{name_prefix}ready to rename tag `#{old_t}` to `#{new_t}` on **{len(items)} file(s)**. Confirm below.", proposed=proposed)

        ctag_m = re.match(r"^(?:clear|remove)\s+all\s+tags\s+(?:from|on|for)\s+(.+)$|^clear\s+tags\s+(?:from|on|for)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if ctag_m:
            tgt_ct = (ctag_m.group(1) or ctag_m.group(2) or "").strip()
            matched_files, _ = self._resolve_action_files(tgt_ct, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            with_tags = [f for f in matched_files if f.get("tags")]
            if not with_tags:
                return _reply(f"{name_prefix}none of the files matching “{tgt_ct}” have tags to clear.", mode="conversational")
            items = [self._make_action_item(f, f"Clear tags ({', '.join('#' + t for t in (f.get('tags') or []))})") for f in with_tags]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "clear_tags",
                "title": f"Clear tags from {len(items)} file(s)",
                "summary": "Remove all tags from selected files (undoable).",
                "targetLabel": "Clear Tags",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {},
            }
            return _reply(f"{name_prefix}ready to clear all tags from **{len(items)} file(s)**. Confirm below.", proposed=proposed)

        atag_m = re.match(r"^(?:auto[- ]?tag|smart[- ]?tag|suggest\s+tags\s+(?:for|on))\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if atag_m:
            tgt_at = atag_m.group(1).strip()
            matched_files, _ = self._resolve_action_files(tgt_at, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find files matching “{tgt_at}” to auto-tag.", mode="conversational")
            tag_map: dict[str, list[str]] = {}
            items = []
            for f in matched_files[:50]:
                sugg_tags = self._suggest_auto_tags_for_file(f)
                if sugg_tags:
                    tag_map[f["id"]] = sugg_tags
                    items.append(self._make_action_item(f, f"Add {' '.join('#' + t for t in sugg_tags)}"))
            if not items:
                return _reply(f"{name_prefix}all matching files already have relevant tags!", mode="conversational")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "auto_tag",
                "title": f"Auto-tag {len(items)} file(s)",
                "summary": "Apply smart content-derived tags (doc type, merchant, year, visual concepts) per file (undoable).",
                "targetLabel": "Smart Tags",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"tagMap": tag_map},
            }
            return _reply(
                f"{name_prefix}I suggested smart tags for **{len(items)} file(s)**. Review per-file tags below and click **Confirm**.",
                citations=[self._make_citation(f, f"Tags: {' '.join('#' + t for t in tag_map[f['id']])}", 90) for f in matched_files[:12] if f["id"] in tag_map],
                proposed=proposed,
            )

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
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find any files matching “{target_phrase}” to untag #{clean_tag}.", mode="conversational")
            items = [self._make_action_item(f, f"Remove tag #{clean_tag}") for f in matched_files]
            proposed = {
                "actionId": make_plan_id(),
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
            re.match(r"^(?:tag|label)\s+(.+?)\s+(?:with|as)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
            or re.match(r"^(?:add|apply)\s+tags?\s+#?([A-Za-z0-9 _\-,#]+?)\s+(?:to|on)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
            or re.match(r"^(?:tag|label)\s+#([A-Za-z0-9_\-]+)\s+(?:on|for|to)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
            or re.match(r"^(?:tag|label)\s+(.+?)\s+(#[A-Za-z0-9_\-,#\s]+)$", q_stripped, flags=re.IGNORECASE)
        )
        if tag_m:
            if q_low.startswith(("add tag", "add tags", "apply tag", "apply tags")) or re.match(r"^(?:tag|label)\s+#", q_low):
                raw_tag, target_phrase = tag_m.group(1).strip(), tag_m.group(2).strip()
            else:
                target_phrase, raw_tag = tag_m.group(1).strip(), tag_m.group(2).strip()
            tag_tokens = [p.strip(" #\"'“”?!.").lower() for p in re.split(r"[,;]+", raw_tag) if p.strip(" #\"'“”?!.")]
            clean_tag = ", ".join(tag_tokens)
            tag_label = " ".join(f"#{t}" for t in tag_tokens)
            matched_files, meta = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files and re.fullmatch(r"(?:these|those|them)(?:\s+files?)?", target_phrase.lower()):
                matched_files = sorted(all_files, key=lambda x: float(x.get("mtime") or 0), reverse=True)[:10]
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find any files matching “{target_phrase}” to tag with {tag_label or '#reviewed'}.", mode="conversational")
            items = [self._make_action_item(f, f"Add tag {tag_label}") for f in matched_files]
            proposed = {
                "actionId": make_plan_id(),
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
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
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
                    {"label": "Auto-tag smartly", "prompt": f"auto-tag {target_phrase}", "kind": "action"},
                    {"label": "Tag #reviewed", "prompt": "tag these files with reviewed", "kind": "action"},
                    {"label": "Tag #important", "prompt": "tag these files with important", "kind": "action"},
                ],
            )

        # 9. PIN / UNPIN FILES
        pin_m = re.match(r"^(pin|unpin)\s+(.+)$", q_stripped, flags=re.IGNORECASE)
        if pin_m:
            act = pin_m.group(1).lower()
            target_phrase = pin_m.group(2).strip()
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find any files matching “{target_phrase}” to {act}.", mode="conversational")
            items = [self._make_action_item(f, "Pin to top of shelf" if act == "pin" else "Unpin") for f in matched_files]
            proposed = {
                "actionId": make_plan_id(),
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

        # 10. RENAME FILES (CUSTOM FORMAT / TRANSFORMS, CONTENT-BASED SMART RENAME, SINGLE FILE, OR MESSY BATCH)
        fmt_req = parse_rename_format_request(q_stripped)
        if fmt_req:
            target_phrase = fmt_req["target"].strip()
            tpl = fmt_req["template"]
            fmt_label = fmt_req["label"]
            if target_phrase.lower() in {"messy files", "unnamed files", "cryptic files", "suggested files"}:
                matched_files = [f for f in all_files if f.get("suggestedName") and not f.get("renamed")]
            else:
                matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
                if not matched_files and target_phrase.lower() in {
                    "this", "these", "these things", "these files", "this stuff", "them", "those", "everything", "all", "all files"
                }:
                    by_id_rec = {f["id"]: f for f in all_files}
                    rec_pool = [by_id_rec[fid] for fid in recent_file_ids if fid in by_id_rec]
                    matched_files = rec_pool if rec_pool else all_files[:50]
            if not matched_files:
                return _reply(
                    f"{name_prefix}I didn't find files matching “{target_phrase}” to rename with `{fmt_label}`. "
                    "Try naming a file type or folder, e.g. `rename all receipts to {date}_{merchant}_{amount}`.",
                    mode="conversational",
                )
            raw_pairs: list[tuple[dict[str, Any], str]] = []
            for seq_i, f_item in enumerate(matched_files[:60], start=1):
                new_n = format_rename_template(f_item, tpl, seq_index=seq_i)
                if new_n and new_n != f_item["name"]:
                    raw_pairs.append((f_item, new_n))
            if not raw_pairs:
                return _reply(
                    f"{name_prefix}all {len(matched_files)} matching file(s) already match format `{fmt_label}`!",
                    mode="conversational",
                )
            sim_pairs = simulate_batch_rename_collisions(raw_pairs)
            rename_map = {f["id"]: new_n for f, new_n, _ in sim_pairs}
            items = [
                self._make_action_item(f, f"{f['name']} → {new_n}" + (" (auto-suffixed)" if had_col else ""))
                for f, new_n, had_col in sim_pairs
            ]
            preview_lines = [f"- `{f['name']}` → `{new_n}`" for f, new_n, _ in sim_pairs[:12]]
            if len(sim_pairs) > 12:
                preview_lines.append(f"- *…plus {len(sim_pairs) - 12} more rename(s) in the checklist below.*")
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "rename_batch",
                "title": f"Rename {len(items)} file(s) using format `{fmt_label}`",
                "summary": f"Apply custom rename format ({fmt_label}) across selected files (100% undoable).",
                "targetLabel": fmt_label,
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"template": tpl, "renameMap": rename_map},
            }
            return _reply(
                f"{name_prefix}I previewed **{len(items)} file(s)** with format `{fmt_label}`:\n"
                + "\n".join(preview_lines)
                + "\n\nUncheck any file you want to skip, then click **Confirm** to rename on disk.",
                citations=[self._make_citation(f, f"→ {new_n}", 94) for f, new_n, _ in sim_pairs[:12]],
                proposed=proposed,
                trace=[*scope_trace, f"Previewed format `{fmt_label}` on {len(items)} file(s)"],
            )

        # Content-based smart rename on ANY selection ("smart rename these files based on their content")
        smart_ren_m = re.match(
            r"^(?:smart[- ]?rename|content[- ]?rename)\s+(.+?)(?:\s+based\s+on\s+(?:their|its)\s+content)?$|"
            r"^rename\s+(.+?)\s+based\s+on\s+(?:their|its)\s+content$",
            q_stripped,
            flags=re.IGNORECASE,
        )
        if smart_ren_m:
            tgt_sr = (smart_ren_m.group(1) or smart_ren_m.group(2) or "").strip()
            matched_files, _ = self._resolve_action_files(tgt_sr, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files:
                return _reply(f"{name_prefix}I didn't find files matching “{tgt_sr}” to smart-rename.", mode="conversational")
            full_map = get_full_texts_batch(self, matched_files[:40])
            # Optional Ollama enhancement when enabled
            ollama_map = synthesize_smart_renames(
                self,
                [
                    {"id": f["id"], "name": f["name"], "date": f.get("createdDate", ""), "excerpt": full_map.get(f["id"], {}).get("combined", "")[:300]}
                    for f in matched_files[:15]
                ],
            )
            raw_pairs = []
            reasons_by_id: dict[str, str] = {}
            for f in matched_files[:40]:
                if f["id"] in ollama_map:
                    cand_n = ollama_map[f["id"]]
                    if Path(f["name"]).suffix and not cand_n.lower().endswith(Path(f["name"]).suffix.lower()):
                        cand_n = f"{cand_n}{Path(f['name']).suffix}"
                    if cand_n.lower() != f["name"].lower():
                        raw_pairs.append((f, cand_n))
                        reasons_by_id[f["id"]] = "Ollama content rename"
                        continue
                cand_n, rsn = synthesize_content_rename(f, full_text=full_map.get(f["id"], {}).get("combined", ""))
                if cand_n and cand_n.lower() != f["name"].lower():
                    raw_pairs.append((f, cand_n))
                    reasons_by_id[f["id"]] = rsn or "content-based rename"
            if not raw_pairs:
                return _reply(f"{name_prefix}all {len(matched_files)} matching file(s) already have descriptive content names!", mode="conversational")
            sim_pairs = simulate_batch_rename_collisions(raw_pairs)
            rename_map = {f["id"]: nn for f, nn, _ in sim_pairs}
            items = [
                self._make_action_item(f, f"{f['name']} → {nn} ({reasons_by_id.get(f['id'], 'content')})")
                for f, nn, _ in sim_pairs
            ]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "rename_batch",
                "title": f"Smart rename {len(items)} file(s) from content",
                "summary": "Synthesize clean filenames from each file's text, entities, and headings (100% undoable).",
                "targetLabel": f"{len(items)} smart renames",
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"renameMap": rename_map},
            }
            return _reply(
                f"{name_prefix}I generated content-based filenames for **{len(items)} file(s)**. Review below and click **Confirm**.",
                citations=[self._make_citation(f, f"→ {nn}", 94) for f, nn, _ in sim_pairs[:12]],
                proposed=proposed,
            )

        ren_single_m = re.match(r"^rename\s+(.+?)\s+to\s+[\"“']?([^\"”'\n]+?)[\"”']?$", q_stripped, flags=re.IGNORECASE)
        if ren_single_m:
            target_phrase = ren_single_m.group(1).strip()
            new_name = ren_single_m.group(2).strip(" \"'“”?!.")
            matched_files, _ = self._resolve_action_files(target_phrase, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
            if not matched_files:
                return _reply(f"{name_prefix}I couldn't find “{target_phrase}” to rename.", mode="conversational")
            f0 = matched_files[0]
            if "." not in new_name and Path(f0["name"]).suffix:
                new_name = f"{new_name}{Path(f0['name']).suffix}"
            # Disambiguate cleanly if multiple distinct files matched equally closely
            ambig_note = ""
            if len(matched_files) > 1 and target_phrase.lower() != f0["name"].lower() and not is_deictic_file_phrase(target_phrase):
                others = ", ".join(f"`{f['name']}`" for f in matched_files[1:4])
                ambig_note = f" *(Also matched {others} — reply with the exact filename if you meant another.)*"
            items = [self._make_action_item(f0, f"{f0['name']} → {new_name}")]
            proposed = {
                "actionId": make_plan_id(),
                "actionType": "rename_single",
                "title": f"Rename “{f0['name']}” → “{new_name}”",
                "summary": "Rename file on disk cleanly (undoable).",
                "targetLabel": new_name,
                "reversible": True,
                "destructive": False,
                "requiresSelection": True,
                "items": items,
                "payload": {"newName": new_name},
            }
            return _reply(
                f"{name_prefix}ready to rename “{f0['name']}” to “{new_name}”.{ambig_note} Confirm below to apply.",
                citations=[self._make_citation(f0, f"→ {new_name}", 95)],
                proposed=proposed,
            )

        if re.match(r"^(?:rename\b|apply\s+(?:all\s+)?(?:smart\s+|suggested\s+)?renames?)", q_low):
            ren_target = re.sub(r"^(?:rename|apply\s+(?:all\s+)?(?:smart\s+|suggested\s+)?renames?)\s*", "", q_stripped, flags=re.IGNORECASE).strip()
            if not ren_target or re.search(r"\b(messy|unnamed|cryptic|screenshots|suggested|clean|all)\b", ren_target.lower()):
                candidates = [f for f in all_files if f.get("suggestedName") and not f.get("renamed")]
            else:
                matched_files, _ = self._resolve_action_files(ren_target, scoped_state, selected_file_id, recent_file_ids, conv_ctx=conv_ctx)
                candidates = [f for f in matched_files if f.get("suggestedName")]
            if not candidates:
                return _reply(
                    f"{name_prefix}all matching files already have clean names! To rename any selection from its content, try `smart rename these files based on their content` or `rename all receipts to {{date}}_{{merchant}}_{{amount}}`.",
                    mode="conversational",
                )
            sim_pairs = simulate_batch_rename_collisions([(f, str(f["suggestedName"])) for f in candidates[:40]])
            rename_map = {f["id"]: nn for f, nn, _ in sim_pairs}
            items = [self._make_action_item(f, f"{f['name']} → {nn}" + (" (auto-suffixed)" if col else "")) for f, nn, col in sim_pairs]
            proposed = {
                "actionId": make_plan_id(),
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
                citations=[self._make_citation(f, f"Suggested: {nn}", 92) for f, nn, _ in sim_pairs[:12]],
                proposed=proposed,
            )

        # Tier-1 Ollama JSON-Schema Planner fallback for complex/ambiguous commands when Ollama is enabled & healthy
        if _allow_ollama_planner and bool(self.get_settings().get("ollama_enabled", False)):
            looks_command_like = bool(
                re.search(
                    r"\b(move|copy|put|place|sort|organize|tidy|clean|trash|delete|archive|restore|tag|label|pin|shelve|rename|ocr|convert|export|open|reveal|reindex|rescan|undo|redo)\b",
                    q_low,
                )
            )
            if looks_command_like:
                folder_names = [Path(w["path"]).name for w in watched if w.get("path")]
                shelf_names = [str(s.get("name") or "") for s in shelves if s.get("name")]
                o_plan = plan_with_ollama(self, q_clean, folder_names, shelf_names, history=history)
                if o_plan:
                    translated = self._translate_ollama_plan(
                        o_plan,
                        scoped_state,
                        selected_file_id=selected_file_id,
                        recent_file_ids=recent_file_ids,
                        name_prefix=name_prefix,
                        name_comma=name_comma,
                        t0=t0,
                        scope_folder=scope_folder,
                        scope_recursive=scope_recursive,
                        memory_rows=mem_list,
                        history=history,
                    )
                    if translated is not None:
                        return translated

        return None

    def execute_ai_action(self, req: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a confirmed AI action plan safely with full audit logging, atomic composite
        undo (`multi_step_tx`), live progress, and honest structured partial-failure reporting.
        """
        action_type = str(req.get("actionType") or "").strip()
        file_ids = [str(fid) for fid in (req.get("fileIds") or []) if fid and not str(fid).startswith("folder::")]
        target_folder = str(req.get("targetFolder") or "").strip()
        is_nested = bool(req.get("_nested"))

        def _name_for(fid: str) -> str:
            f_obj = self.get_file_by_id(fid)
            return str(f_obj.get("name") or fid) if f_obj else fid

        def _fail_suffix(failed_list: list[dict[str, str]]) -> str:
            if not failed_list:
                return ""
            reasons = "; ".join(f"{x['fileId']}: {x['error']}" for x in failed_list[:2])
            return f" ({len(failed_list)} failed: {reasons})"

        try:
            if action_type == "move":
                move_map = req.get("moveMap") or {}
                if move_map and isinstance(move_map, dict):
                    # Heterogeneous per-file auto-sort moves (single undoable bulk_move log!)
                    selected_fids = file_ids or list(move_map.keys())
                    moves_log: list[dict[str, Any]] = []
                    failed: list[dict[str, str]] = []
                    tot = max(1, len(selected_fids))
                    with self._lock:
                        conn = self._connect()
                        try:
                            for idx, fid in enumerate(selected_fids, start=1):
                                dst_folder_str = str(move_map.get(fid) or target_folder or "").strip()
                                if not dst_folder_str:
                                    continue
                                self.emit_progress("Auto-sorting files", idx, tot, _name_for(fid))
                                try:
                                    row = conn.execute("SELECT path, renamed FROM files WHERE id = ?", (fid,)).fetchone()
                                    if not row:
                                        failed.append({"fileId": fid, "error": "file not in index"})
                                        continue
                                    src = Path(row["path"])
                                    if not src.exists():
                                        failed.append({"fileId": fid, "error": "missing on disk"})
                                        continue
                                    dst_dir = Path(dst_folder_str).expanduser().resolve()
                                    dst_dir.mkdir(parents=True, exist_ok=True)
                                    dst = _unique_dest_path(dst_dir, src.name, src_path=src)
                                    shutil.move(str(src), str(dst))
                                    conn.execute(
                                        "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ? WHERE id = ?",
                                        (str(dst), dst.name, str(dst.parent), format_path_short(dst.parent), fid),
                                    )
                                    moves_log.append({
                                        "fileId": fid,
                                        "fromPath": str(src),
                                        "toPath": str(dst),
                                        "renamedAfter": int(row["renamed"] or 0),
                                    })
                                except Exception as exc:
                                    failed.append({"fileId": fid, "error": str(exc)})
                            conn.commit()
                            self._invalidate_cache()
                        finally:
                            conn.close()
                    if moves_log:
                        self._record_action(
                            "bulk_move",
                            f"Auto-sorted {len(moves_log)} file(s)",
                            {"label": f"Auto-Sort ({len(moves_log)})", "moves": moves_log},
                        )
                    return {
                        "ok": True,
                        "count": len(moves_log),
                        "failed": failed,
                        "summary": f"Moved {len(moves_log)} file(s) into their matched folders.{_fail_suffix(failed)}",
                    }

                if not target_folder:
                    raise ValueError("targetFolder is required for move")
                self.emit_progress(f"Moving to {format_path_short(target_folder)}", 0, max(1, len(file_ids)), "Preparing moves…")
                res = self.bulk_file_action(file_ids, "move", target_folder=target_folder)
                cnt = int(res.get("count", 0))
                self.emit_progress(f"Moving to {format_path_short(target_folder)}", cnt, max(1, len(file_ids)), f"Moved {cnt} file(s)")
                self._learn_from_executed_action("move", req, file_ids)
                return {"ok": True, "count": cnt, "failed": [], "summary": f"Moved {cnt} file(s) to {format_path_short(target_folder)}."}

            if action_type == "copy":
                if not target_folder:
                    raise ValueError("targetFolder is required for copy")
                self.emit_progress(f"Copying to {format_path_short(target_folder)}", 0, max(1, len(file_ids)), "Preparing copies…")
                res = self.paste_files(file_ids, target_folder, mode="copy")
                cnt = int(res.get("count", 0))
                failed = list(res.get("failed") or [])
                self._learn_from_executed_action("copy", req, file_ids)
                return {
                    "ok": True,
                    "count": cnt,
                    "failed": failed,
                    "summary": f"Copied {cnt} file(s) to {format_path_short(target_folder)}.{_fail_suffix(failed)}",
                }

            if action_type == "trash":
                self.emit_progress("Moving to Trash", 0, max(1, len(file_ids)), "Archiving to GoodShelves_Trash…")
                res = self.bulk_file_action(file_ids, "trash")
                cnt = int(res.get("count", 0))
                return {"ok": True, "count": cnt, "failed": [], "summary": f"Moved {cnt} file(s) to GoodShelves_Trash."}

            if action_type == "archive_files":
                arch_dir = (get_user_special_dir("Documents") / "GoodShelves_Archive").resolve()
                arch_dir.mkdir(parents=True, exist_ok=True)
                items_log: list[dict[str, Any]] = []
                failed = []
                tot = max(1, len(file_ids))
                with self._lock:
                    conn = self._connect()
                    try:
                        for idx, fid in enumerate(file_ids, start=1):
                            self.emit_progress("Archiving files", idx, tot, _name_for(fid))
                            try:
                                row = conn.execute("SELECT path FROM files WHERE id = ?", (fid,)).fetchone()
                                if not row:
                                    failed.append({"fileId": fid, "error": "file not found"})
                                    continue
                                src = Path(row["path"])
                                dst = _unique_dest_path(arch_dir, src.name)
                                if src.exists():
                                    shutil.move(str(src), str(dst))
                                conn.execute(
                                    "UPDATE files SET path = ?, name = ?, folder = ?, folder_short = ?, archived = 1, original_path = ? WHERE id = ?",
                                    (str(dst), dst.name, str(dst.parent), format_path_short(dst.parent), str(src), fid),
                                )
                                self._remove_file_from_search_indices(conn, file_id=fid)
                                items_log.append({"fileId": fid, "fromPath": str(src), "toPath": str(dst)})
                            except Exception as exc:
                                failed.append({"fileId": fid, "error": str(exc)})
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                if items_log:
                    self._record_action(
                        "archive_files",
                        f"Archived {len(items_log)} file(s) to GoodShelves_Archive",
                        {"label": f"Archive ({len(items_log)})", "items": items_log},
                    )
                return {
                    "ok": True,
                    "count": len(items_log),
                    "failed": failed,
                    "summary": f"Archived {len(items_log)} file(s) to GoodShelves_Archive.{_fail_suffix(failed)}",
                }

            if action_type == "delete_permanent":
                deleted = 0
                failed = []
                tot = max(1, len(file_ids))
                for idx, fid in enumerate(file_ids, start=1):
                    self.emit_progress("Permanently deleting", idx, tot, _name_for(fid))
                    try:
                        self.delete_file_permanently(fid)
                        deleted += 1
                    except Exception as exc:
                        failed.append({"fileId": fid, "error": str(exc)})
                return {
                    "ok": True,
                    "count": deleted,
                    "failed": failed,
                    "summary": f"Permanently deleted {deleted} file(s).{_fail_suffix(failed)}",
                }

            if action_type == "empty_trash":
                res = self.empty_trash()
                cnt = int(res.get("removedCount", 0))
                return {"ok": True, "count": cnt, "failed": [], "summary": f"Permanently emptied {cnt} file(s) from GoodShelves_Trash."}

            if action_type in {"tag", "untag"}:
                tag = str(req.get("tag") or "").strip()
                self.emit_progress("Updating tags", 0, max(1, len(file_ids)), f"#{tag}")
                res = self.bulk_file_action(file_ids, action_type, tag=tag)
                cnt = int(res.get("count", 0))
                self._learn_from_executed_action(action_type, req, file_ids)
                verb = "Tagged" if action_type == "tag" else "Removed tag from"
                tag_disp = ", ".join(f"#{t.strip(' #')}" for t in re.split(r"[,;]+", tag) if t.strip(" #"))
                return {"ok": True, "count": cnt, "failed": [], "summary": f"{verb} {cnt} file(s) ({tag_disp})."}

            if action_type == "auto_tag":
                tag_map = req.get("tagMap") or {}
                selected_fids = file_ids or list(tag_map.keys())
                entries: list[dict[str, Any]] = []
                failed = []
                with self._lock:
                    conn = self._connect()
                    try:
                        for fid in selected_fids:
                            add_tags = [str(t).strip(" #").lower() for t in (tag_map.get(fid) or []) if str(t).strip(" #")]
                            if not add_tags:
                                continue
                            row = conn.execute("SELECT tags_json FROM files WHERE id = ?", (fid,)).fetchone()
                            if not row:
                                failed.append({"fileId": fid, "error": "file not found"})
                                continue
                            before = json.loads(row["tags_json"] or "[]")
                            after = list(before)
                            for nt in add_tags:
                                if nt not in after:
                                    after.append(nt)
                            conn.execute("UPDATE files SET tags_json = ? WHERE id = ?", (json.dumps(after), fid))
                            entries.append({"fileId": fid, "before": before, "after": after})
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                if entries:
                    self._record_action("bulk_tag", f"Auto-tagged {len(entries)} file(s)", {"label": f"Auto-tag ({len(entries)})", "entries": entries})
                return {"ok": True, "count": len(entries), "failed": failed, "summary": f"Auto-tagged {len(entries)} file(s).{_fail_suffix(failed)}"}

            if action_type == "rename_tag":
                old_t = str(req.get("oldTag") or "").strip(" #").lower()
                new_t = str(req.get("newTag") or "").strip(" #").lower()
                if not old_t or not new_t:
                    raise ValueError("oldTag and newTag are required for rename_tag")
                entries = []
                with self._lock:
                    conn = self._connect()
                    try:
                        target_ids = file_ids
                        if not target_ids:
                            rows = conn.execute("SELECT id, tags_json FROM files WHERE archived = 0").fetchall()
                        else:
                            ph = ",".join("?" for _ in target_ids)
                            rows = conn.execute(f"SELECT id, tags_json FROM files WHERE id IN ({ph})", target_ids).fetchall()
                        for r in rows:
                            before = json.loads(r["tags_json"] or "[]")
                            if not any(str(t).lower() == old_t for t in before):
                                continue
                            after: list[str] = []
                            for t in before:
                                repl = new_t if str(t).lower() == old_t else str(t).lower()
                                if repl not in after:
                                    after.append(repl)
                            conn.execute("UPDATE files SET tags_json = ? WHERE id = ?", (json.dumps(after), r["id"]))
                            entries.append({"fileId": str(r["id"]), "before": before, "after": after})
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                if entries:
                    self._record_action("bulk_tag", f"Renamed tag #{old_t} → #{new_t} ({len(entries)} file(s))", {"label": f"Rename tag #{old_t}", "entries": entries})
                return {"ok": True, "count": len(entries), "failed": [], "summary": f"Renamed tag #{old_t} → #{new_t} on {len(entries)} file(s)."}

            if action_type == "clear_tags":
                entries = []
                with self._lock:
                    conn = self._connect()
                    try:
                        for fid in file_ids:
                            row = conn.execute("SELECT tags_json FROM files WHERE id = ?", (fid,)).fetchone()
                            if not row:
                                continue
                            before = json.loads(row["tags_json"] or "[]")
                            if not before:
                                continue
                            conn.execute("UPDATE files SET tags_json = '[]' WHERE id = ?", (fid,))
                            entries.append({"fileId": fid, "before": before, "after": []})
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                if entries:
                    self._record_action("bulk_untag", f"Cleared tags from {len(entries)} file(s)", {"label": f"Clear tags ({len(entries)})", "entries": entries})
                return {"ok": True, "count": len(entries), "failed": [], "summary": f"Cleared tags from {len(entries)} file(s)."}

            if action_type in {"pin", "unpin"}:
                res = self.bulk_file_action(file_ids, action_type)
                cnt = int(res.get("count", 0))
                verb = "Pinned" if action_type == "pin" else "Unpinned"
                return {"ok": True, "count": cnt, "failed": [], "summary": f"{verb} {cnt} file(s)."}

            if action_type in {"shelve", "unshelve"}:
                shelf_map = req.get("shelfMap") or {}
                default_sid = "auto" if action_type == "unshelve" else str(req.get("shelfId") or "").strip()
                shelf_name = str(req.get("shelfName") or default_sid)
                selected_fids = file_ids or list(shelf_map.keys())
                shelf_entries: list[dict[str, Any]] = []
                failed = []
                tot = max(1, len(selected_fids))
                for idx, fid in enumerate(selected_fids, start=1):
                    target_sid = str(shelf_map.get(fid) or default_sid).strip()
                    self.emit_progress(f"Shelving under {shelf_name}", idx, tot, _name_for(fid))
                    try:
                        with self._lock:
                            conn = self._connect()
                            try:
                                before_r = conn.execute("SELECT user_shelf_id, shelf_id, shelf_name FROM files WHERE id = ?", (fid,)).fetchone()
                            finally:
                                conn.close()
                        before_state = {
                            "userShelfId": str(before_r["user_shelf_id"] or "") if before_r else "",
                            "shelfId": str(before_r["shelf_id"] or "") if before_r else "",
                            "shelfName": str(before_r["shelf_name"] or "") if before_r else "",
                        }
                        res_s = self.set_file_shelf(fid, target_sid)
                        after_state = {
                            "userShelfId": "" if target_sid in {"", "auto"} else target_sid,
                            "shelfId": str(res_s.get("shelfId") or target_sid),
                            "shelfName": str(res_s.get("shelfName") or shelf_name),
                        }
                        shelf_entries.append({"fileId": fid, "before": before_state, "after": after_state})
                    except Exception as exc:
                        failed.append({"fileId": fid, "error": str(exc)})
                if shelf_entries:
                    self._record_action(
                        "bulk_shelve",
                        f"{'Unshelved' if action_type == 'unshelve' else 'Shelved'} {len(shelf_entries)} item(s)",
                        {"label": f"Shelve ({len(shelf_entries)})", "entries": shelf_entries},
                    )
                verb_s = "Unshelved" if action_type == "unshelve" else f"Shelved under “{shelf_name}”:"
                return {
                    "ok": True,
                    "count": len(shelf_entries),
                    "failed": failed,
                    "summary": f"{verb_s} {len(shelf_entries)} item(s).{_fail_suffix(failed)}",
                }

            if action_type == "create_shelf":
                shelf_name = str(req.get("shelfName") or "").strip()
                res = self.create_custom_shelf(name=shelf_name, file_ids=file_ids)
                return {
                    "ok": True,
                    "count": len(file_ids),
                    "shelfId": res.get("shelfId"),
                    "failed": [],
                    "summary": f"Created shelf “{shelf_name}” with {len(file_ids)} item(s).",
                }

            if action_type == "rename_shelf":
                sid = str(req.get("shelfId") or "").strip()
                new_n = str(req.get("newName") or "").strip()
                res = self.rename_shelf(sid, new_n)
                return {"ok": True, "count": 1, "failed": [], "summary": f"Renamed shelf to “{res.get('name', new_n)}”."}

            if action_type == "delete_shelf":
                sid = str(req.get("shelfId") or "").strip()
                sname = str(req.get("shelfName") or sid)
                self.delete_custom_shelf(sid)
                return {"ok": True, "count": 1, "failed": [], "summary": f"Deleted custom shelf “{sname}”."}

            if action_type in {"pin_shelf", "unpin_shelf"}:
                sid = str(req.get("shelfId") or "").strip()
                sname = str(req.get("shelfName") or sid)
                desired_pin = action_type == "pin_shelf"
                cur_res = self.toggle_pin_shelf(sid)
                if bool(cur_res.get("pinned")) != desired_pin:
                    cur_res = self.toggle_pin_shelf(sid)
                return {"ok": True, "count": 1, "failed": [], "summary": f"{'Pinned' if desired_pin else 'Unpinned'} shelf “{sname}”."}

            if action_type == "create_folder":
                parent_path = str(req.get("parentPath") or "").strip()
                folder_name = str(req.get("folderName") or "").strip()
                res = self.create_folder(parent_path, folder_name, watch=True, record_log=True)
                return {"ok": True, "count": 1, "path": res.get("path"), "failed": [], "summary": f"Created and watched folder “{folder_name}”."}

            if action_type == "rename_single":
                if not file_ids:
                    raise ValueError("No file selected to rename")
                new_name = str(req.get("newName") or "").strip()
                self.emit_progress("Renaming file", 1, 1, new_name)
                res = self.rename_file_custom(file_ids[0], new_name)
                renamed_name = (res.get("file") or {}).get("name") or new_name
                return {"ok": True, "count": 1, "failed": [], "summary": f"Renamed file to “{renamed_name}”."}

            if action_type == "rename_batch":
                rename_map = req.get("renameMap") or {}
                template = str(req.get("template") or "").strip()
                if template and not rename_map:
                    self.emit_progress("Batch renaming", 0, max(1, len(file_ids)), template)
                    res = self.execute_batch_rename_template(file_ids, template)
                    cnt = int(res.get("renamedCount", 0))
                    self.emit_progress("Batch renaming", cnt, max(1, len(file_ids)), f"Renamed {cnt} file(s)")
                    return {"ok": True, "count": cnt, "failed": [], "summary": f"Renamed {cnt} file(s)."}
                # Execute explicit renameMap as a single atomic rename_batch action_log!
                moves_log = []
                failed = []
                tot = max(1, len(file_ids))
                with self._lock:
                    conn = self._connect()
                    try:
                        for idx, fid in enumerate(file_ids, start=1):
                            target_name = str(rename_map.get(fid) or "").strip()
                            if not target_name:
                                continue
                            self.emit_progress("Batch renaming", idx, tot, target_name)
                            try:
                                row = conn.execute("SELECT path, name, ext FROM files WHERE id = ?", (fid,)).fetchone()
                                if not row:
                                    failed.append({"fileId": fid, "error": "file not found"})
                                    continue
                                src = Path(row["path"])
                                if not src.exists():
                                    failed.append({"fileId": fid, "error": "missing on disk"})
                                    continue
                                dst = _unique_dest_path(src.parent, target_name, src_path=src)
                                if dst == src:
                                    continue
                                src.rename(dst)
                                new_title = dst.stem.replace("_", " ").replace("-", " ").strip() or dst.stem
                                conn.execute(
                                    "UPDATE files SET path = ?, name = ?, title = ?, renamed = 1 WHERE id = ?",
                                    (str(dst), dst.name, new_title, fid),
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
                            except Exception as exc:
                                failed.append({"fileId": fid, "error": str(exc)})
                        conn.commit()
                        self._invalidate_cache()
                    finally:
                        conn.close()
                if moves_log:
                    self._record_action(
                        "rename_batch",
                        f"Batch renamed {len(moves_log)} file(s)",
                        {"label": f"Batch Rename ({len(moves_log)})", "moves": moves_log},
                    )
                return {
                    "ok": True,
                    "count": len(moves_log),
                    "failed": failed,
                    "summary": f"Renamed {len(moves_log)} file(s).{_fail_suffix(failed)}",
                }

            if action_type == "move_folder":
                folder_paths = [str(p) for p in (req.get("folderPaths") or []) if p]
                if not folder_paths and req.get("folderPath"):
                    folder_paths = [str(req["folderPath"])]
                if not target_folder:
                    raise ValueError("targetFolder is required to move folders")
                moved = 0
                failed = []
                tot = max(1, len(folder_paths))
                for idx, fp in enumerate(folder_paths, start=1):
                    self.emit_progress("Moving folders", idx, tot, Path(fp).name)
                    try:
                        self.move_folder(fp, target_folder)
                        moved += 1
                    except Exception as exc:
                        failed.append({"fileId": fp, "error": str(exc)})
                return {
                    "ok": True,
                    "count": moved,
                    "failed": failed,
                    "summary": f"Moved {moved} folder(s) to {format_path_short(target_folder)}.{_fail_suffix(failed)}",
                }

            if action_type == "delete_folder":
                fp = str(req.get("folderPath") or "").strip()
                if not fp:
                    raise ValueError("folderPath is required")
                res = self.delete_folder(fp)
                return {"ok": True, "count": int(res.get("movedFiles", 0)), "failed": [], "summary": f"Moved folder {Path(fp).name} to GoodShelves_Trash."}

            if action_type == "rename_folder":
                fp = str(req.get("folderPath") or "").strip()
                new_name = str(req.get("newName") or "").strip()
                res = self.rename_folder(fp, new_name, record_log=True)
                return {"ok": True, "count": 1, "failed": [], "summary": f"Renamed folder to “{res.get('name', new_name)}”."}

            if action_type == "restore_trash":
                restored_items: list[dict[str, Any]] = []
                failed = []
                tot = max(1, len(file_ids))
                for idx, fid in enumerate(file_ids, start=1):
                    self.emit_progress("Restoring from Trash", idx, tot, _name_for(fid))
                    try:
                        r_res = self.restore_trashed_file(fid, record_log=False)
                        restored_items.append({
                            "fileId": fid,
                            "fromPath": str(r_res.get("fromPath") or ""),
                            "toPath": str(r_res.get("restoredPath") or ""),
                        })
                    except Exception as exc:
                        failed.append({"fileId": fid, "error": str(exc)})
                if restored_items:
                    self._record_action(
                        "restore_trash",
                        f"Restored {len(restored_items)} file(s) from Trash",
                        {"label": f"Restore Trash ({len(restored_items)})", "items": restored_items},
                    )
                return {
                    "ok": True,
                    "count": len(restored_items),
                    "failed": failed,
                    "summary": f"Restored {len(restored_items)} file(s) from Trash.{_fail_suffix(failed)}",
                }

            if action_type == "restore_archive":
                pair_ids = [str(pid) for pid in (req.get("pairIds") or []) if pid]
                restored = 0
                failed = []
                tot = max(1, len(pair_ids))
                for idx, pid in enumerate(pair_ids, start=1):
                    self.emit_progress("Restoring archived files", idx, tot, f"Item {idx}/{len(pair_ids)}")
                    try:
                        res_a = self.toggle_archive_lookalike(pid)
                        if not res_a.get("resolved"):
                            restored += 1
                    except Exception as exc:
                        failed.append({"fileId": pid, "error": str(exc)})
                if restored > 0:
                    self.recluster_all()
                return {
                    "ok": True,
                    "count": restored,
                    "failed": failed,
                    "summary": f"Restored {restored} archived file(s).{_fail_suffix(failed)}",
                }

            if action_type == "undo":
                aid = req.get("actionId")
                res = self.undo_action(int(aid) if aid is not None else None)
                return {"ok": True, "count": 1, "failed": [], "summary": str(res.get("summary") or "Undid last action.")}

            if action_type == "redo":
                aid = req.get("actionId")
                res = self.redo_action(int(aid) if aid is not None else None)
                return {"ok": True, "count": 1, "failed": [], "summary": str(res.get("summary") or "Redid last action.")}

            if action_type == "swap_lookalike":
                pair_ids = [str(pid) for pid in (req.get("pairIds") or []) if pid]
                swapped_cnt = 0
                failed = []
                for pid in pair_ids:
                    try:
                        self.swap_lookalike_pair(pid)
                        swapped_cnt += 1
                    except Exception as exc:
                        failed.append({"fileId": pid, "error": str(exc)})
                return {"ok": True, "count": swapped_cnt, "failed": failed, "summary": f"Swapped {swapped_cnt} duplicate pair(s).{_fail_suffix(failed)}"}

            if action_type == "archive_duplicates":
                pair_ids = [str(pid) for pid in (req.get("pairIds") or []) if pid]
                if file_ids:
                    allowed_copy_ids = set(file_ids)
                    pair_map = {str(p["id"]): str((p.get("copy") or {}).get("id") or "") for p in self.get_all_state().get("lookalikes", [])}
                    pair_ids = [pid for pid in pair_ids if pair_map.get(pid) in allowed_copy_ids or pid not in pair_map]
                archived_cnt = 0
                failed = []
                tot = max(1, len(pair_ids))
                for idx, pid in enumerate(pair_ids, start=1):
                    self.emit_progress("Archiving duplicates", idx, tot, f"Pair {idx} of {len(pair_ids)}")
                    try:
                        res = self.toggle_archive_lookalike(pid)
                        if res.get("resolved"):
                            archived_cnt += 1
                    except Exception as exc:
                        failed.append({"fileId": pid, "error": str(exc)})
                if archived_cnt > 0:
                    self.recluster_all()
                return {
                    "ok": True,
                    "count": archived_cnt,
                    "failed": failed,
                    "summary": f"Archived {archived_cnt} duplicate copy(ies) cleanly.{_fail_suffix(failed)}",
                }

            if action_type == "tidy_folders":
                moves = self.preview_tidy_folders()
                if file_ids:
                    allowed = set(file_ids)
                    moves = [m for m in moves if m["fileId"] in allowed]
                batch_id = f"tidy_{int(time.time())}"
                moved_count = 0
                failed = []
                tot = max(1, len(moves))
                actual_moves: list[dict[str, Any]] = []
                with self._lock:
                    conn = self._connect()
                    try:
                        for idx, m in enumerate(moves, start=1):
                            self.emit_progress("Organizing into ~/Shelves/", idx, tot, m["fileName"])
                            try:
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
                                        "INSERT INTO tidy_moves_history (batch_id, file_id, old_path, new_path, timestamp, undone) VALUES (?, ?, ?, ?, ?, 0)",
                                        (batch_id, m["fileId"], str(src), str(dst), time.time()),
                                    )
                                    actual_moves.append({
                                        "fileId": m["fileId"],
                                        "fromPath": str(src),
                                        "toPath": str(dst),
                                        "renamedAfter": 0,
                                    })
                                    moved_count += 1
                            except Exception as exc:
                                failed.append({"fileId": str(m["fileId"]), "error": str(exc)})
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
                return {
                    "ok": True,
                    "count": moved_count,
                    "failed": failed,
                    "summary": f"Organized {moved_count} file(s) into ~/Shelves/.{_fail_suffix(failed)}",
                }

            if action_type == "smart_clean":
                self.emit_progress("Running Smart Clean", 0, 3, "Cleaning duplicates, names, and folders…")
                res = self.execute_smart_clean(
                    apply_renames=bool(req.get("applyRenames", True)),
                    archive_duplicates=bool(req.get("archiveDuplicates", True)),
                    organize_folders=bool(req.get("organizeFolders", True)),
                )
                tot_chg = int(res.get("renamedCount", 0)) + int(res.get("archivedCount", 0)) + int(res.get("movedCount", 0))
                return {
                    "ok": True,
                    "count": tot_chg,
                    "failed": [],
                    "summary": f"Smart Clean: {res.get('renamedCount', 0)} renamed, {res.get('archivedCount', 0)} duplicate(s) archived, {res.get('movedCount', 0)} organized.",
                }

            if action_type == "pin_folder":
                fp = str(req.get("folderPath") or "").strip()
                res = self.toggle_pin_folder(fp, pinned=True, record_log=True)
                return {"ok": True, "count": 1, "failed": [], "summary": f"Pinned folder “{res['name']}” to sidebar."}

            if action_type == "unpin_folder":
                fp = str(req.get("folderPath") or "").strip()
                res = self.toggle_pin_folder(fp, pinned=False, record_log=True)
                return {"ok": True, "count": 1, "failed": [], "summary": f"Unpinned folder “{res['name']}” from sidebar."}

            if action_type == "run_ocr":
                ocr_done = 0
                failed = []
                tot = max(1, len(file_ids))
                for idx, fid in enumerate(file_ids, start=1):
                    self.emit_progress("Running RapidOCR", idx, tot, _name_for(fid))
                    try:
                        self.execute_file_action(fid, "run_ocr")
                        ocr_done += 1
                    except Exception as exc:
                        failed.append({"fileId": fid, "error": str(exc)})
                return {
                    "ok": True,
                    "count": ocr_done,
                    "failed": failed,
                    "summary": f"Extracted OCR text for {ocr_done} file(s).{_fail_suffix(failed)}",
                }

            if action_type == "file_convert":
                fa = str(req.get("fileAction") or "").strip()
                done_cnt = 0
                failed = []
                tot = max(1, len(file_ids))
                for idx, fid in enumerate(file_ids, start=1):
                    self.emit_progress(f"Running {fa}", idx, tot, _name_for(fid))
                    try:
                        self.execute_file_action(fid, fa)
                        done_cnt += 1
                    except Exception as exc:
                        failed.append({"fileId": fid, "error": str(exc)})
                return {"ok": True, "count": done_cnt, "failed": failed, "summary": f"Completed {fa} on {done_cnt} file(s).{_fail_suffix(failed)}"}

            if action_type in {"open_file", "reveal_file"}:
                if not file_ids:
                    raise ValueError("No file selected")
                is_rev = action_type == "reveal_file" or bool(req.get("reveal"))
                self.open_file_external(file_ids[0], reveal=is_rev)
                return {"ok": True, "count": 1, "failed": [], "summary": f"{'Revealed' if is_rev else 'Opened'} “{_name_for(file_ids[0])}”."}

            if action_type == "reindex_images":
                res = self.reindex_images_only()
                cnt = int(res.get("reindexed", res.get("count", 0)))
                return {"ok": True, "count": cnt, "failed": [], "summary": f"Re-indexed {cnt} image(s) with the active vision model."}

            if action_type == "rescan_folders":
                total_scanned = 0
                active_wf = [wf for wf in self.get_watched_folders() if wf.get("active", True)]
                max_files = int(self.get_settings().get("max_scan_files", 500))
                for idx, wf in enumerate(active_wf, start=1):
                    self.emit_progress("Rescanning folders", idx, max(1, len(active_wf)), Path(wf["path"]).name)
                    try:
                        total_scanned += self.scan_folder(Path(wf["path"]), max_files=max_files, recursive=True, emit_idle=False)
                    except OSError:
                        continue
                self.verify_integrity()
                self.recluster_all()
                return {"ok": True, "count": total_scanned, "failed": [], "summary": f"Rescanned {len(active_wf)} folder(s) ({total_scanned} file(s) updated)."}

            if action_type == "verify_index":
                res = self.verify_integrity()
                orphans = int(res.get("orphansRemoved", 0))
                if orphans > 0:
                    self.recluster_all()
                return {
                    "ok": True,
                    "count": orphans,
                    "failed": [],
                    "summary": f"Index verified: {res.get('activeFiles', 0)} active file(s), {orphans} orphan(s) pruned.",
                }

            if action_type == "save_search":
                s_name = str(req.get("searchName") or "").strip()
                s_query = str(req.get("searchQuery") or "").strip()
                self.save_search(s_name, s_query)
                return {"ok": True, "count": 1, "failed": [], "summary": f"Saved search “{s_name}”."}

            if action_type == "forget_memory":
                mem_id = req.get("memoryId")
                if mem_id is not None:
                    self.delete_ai_memory_entry(int(mem_id))
                    return {"ok": True, "count": 1, "failed": [], "summary": "Removed rule from local AI memory."}
                raise ValueError("memoryId is required for forget_memory")

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
                self._record_action(
                    "remember",
                    f"Saved AI memory rule “{key_pat}”",
                    {"label": f"Remember {key_pat}", "kind": kind_mem, "keyPattern": key_pat, "value": val_txt},
                )
                return {"ok": True, "count": 1, "failed": [], "summary": "Saved rule to local AI memory."}

            if action_type == "multi_step":
                steps = list(req.get("steps") or [])
                if not steps:
                    raise ValueError("No steps provided for multi_step workflow")
                summaries: list[str] = []
                all_failed: list[dict[str, str]] = []
                last_count = len(file_ids)
                selected_set = set(file_ids)
                last_created_dir: Optional[str] = None
                last_created_shelf_id: Optional[str] = None
                total_steps = len(steps)

                # Snapshot max action_log ID before running child steps so we can group them into ONE atomic `multi_step_tx`!
                with self._lock:
                    conn = self._connect()
                    try:
                        prev_max_row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM action_log").fetchone()
                        max_log_before = int(prev_max_row["m"]) if prev_max_row else 0
                    finally:
                        conn.close()

                for s_pos, st_item in enumerate(steps, start=1):
                    st_type = str(st_item.get("actionType") or "")
                    st_title = str(st_item.get("title") or st_type)
                    self.emit_progress(f"Step {s_pos}/{total_steps}: {st_title}", s_pos, total_steps, st_title)
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
                    if last_created_shelf_id and st_type == "shelve":
                        if str(st_payload.get("shelfId") or "") in {"", "__last_created_shelf__"}:
                            st_payload["shelfId"] = last_created_shelf_id

                    sub_req = {**st_payload, "actionType": st_type, "fileIds": step_fids, "_nested": True}
                    try:
                        sub_res = self.execute_ai_action(sub_req)
                    except Exception as exc:
                        all_failed.append({"fileId": st_title, "error": str(exc)})
                        summaries.append(f"{st_title} failed ({exc})")
                        continue
                    if st_type == "create_folder" and sub_res.get("path"):
                        last_created_dir = str(sub_res["path"])
                    if st_type == "create_shelf" and sub_res.get("shelfId"):
                        last_created_shelf_id = str(sub_res["shelfId"])
                    if sub_res.get("failed"):
                        all_failed.extend(sub_res["failed"])
                    summaries.append(str(sub_res.get("summary") or st_type))
                    if sub_res.get("count") is not None:
                        last_count = max(last_count, int(sub_res["count"]))

                # Group all newly created child action_log entries into ONE atomic `multi_step_tx`!
                with self._lock:
                    conn = self._connect()
                    try:
                        child_rows = conn.execute(
                            "SELECT id FROM action_log WHERE id > ? AND undone = 0 ORDER BY id ASC",
                            (max_log_before,),
                        ).fetchall()
                        child_ids = [int(r["id"]) for r in child_rows]
                        if len(child_ids) >= 2:
                            # Mark child rows as grouped (undone = 2) so latest undo_action targets the parent tx!
                            ph = ",".join("?" for _ in child_ids)
                            conn.execute(f"UPDATE action_log SET undone = 2 WHERE id IN ({ph})", child_ids)
                            tx_summary = " · ".join(summaries[:4]) if summaries else f"{total_steps}-step workflow"
                            conn.execute(
                                "INSERT INTO action_log (action_type, summary, detail_json, timestamp, undone) VALUES (?, ?, ?, ?, 0)",
                                (
                                    "multi_step_tx",
                                    tx_summary,
                                    json.dumps({"label": f"{total_steps}-step workflow", "childActionIds": child_ids}),
                                    time.time(),
                                ),
                            )
                            conn.commit()
                            self._invalidate_cache()
                    finally:
                        conn.close()

                self._learn_from_executed_action("multi_step", req, file_ids)
                return {
                    "ok": True,
                    "count": last_count,
                    "failed": all_failed,
                    "summary": " · ".join(summaries) if summaries else "Completed workflow.",
                }

            raise ValueError(f"Unsupported AI actionType: {action_type}")
        finally:
            if not is_nested:
                self.emit_progress("idle", 1, 1, "")

    def _answer_workspace_overview(
        self,
        q_low: str,
        state: dict[str, Any],
        name_prefix: str,
        name_comma: str,
        scope_folder: Optional[str] = None,
    ) -> Optional[tuple[str, list[dict[str, Any]]]]:
        """Answer high-level questions about shelves, storage, duplicates, or how Good Shelves works (strictly scope-aware)."""
        all_files = [f for f in state.get("files", []) if not f.get("archived")]
        shelves = state.get("shelves", [])
        total_files = len(all_files)
        scope_note = f" in **{format_path_short(scope_folder)}**" if scope_folder else ""

        # Shelf vs folder metaphor / how-to guidance
        if re.search(r"\b(difference between|shelf vs folder|shelves work|what is a shelf|what are shelves|quiet tray|how does undo|shortcuts)\b", q_low):
            if "quiet tray" in q_low:
                quiet_count = sum(1 for f in all_files if not f.get("shelfId") or f.get("shelfId") in {"tray", "uncategorized"})
                return (
                    f"{name_prefix}the Quiet Tray holds {quiet_count} unsorted item(s){scope_note} gently at the edge of your workspace "
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

        # Scope-aware storage / space breakdown (NEVER leaks global workspace numbers when scoped!)
        if re.search(r"\b(how much space|storage|disk space|taking up space|workspace size|(?:storage|space|workspace|shelf|shelves|file)\s+breakdown)\b", q_low):
            stats = compute_scoped_workspace_stats(all_files, state.get("lookalikes", []), scope_folder=scope_folder)
            mods = ", ".join(f"{m['count']} {m['modality']}(s)" for m in stats["byModality"][:4])
            top_sh = ", ".join(f"{s['shelfName']} ({format_bytes(s['bytes'])})" for s in stats["byShelf"][:3])
            cites = [
                self._make_citation(f, f"Size: {f['size']} · {f['shelfName']}", 92)
                for f in sorted(all_files, key=lambda x: int(x.get("sizeBytes") or 0), reverse=True)[:8]
            ]
            return (
                f"{name_prefix}your shelves{scope_note} hold **{stats['totalFiles']} file(s)** totaling **{stats['totalHuman']}**"
                + (f" ({mods})" if mods else "")
                + (f". Heaviest shelves: {top_sh}." if top_sh else "."),
                cites,
            )

        # Scope-aware duplicates check
        if re.search(r"\b(any duplicates|duplicate files|lookalike|lookalikes|reclaim space)\b", q_low):
            stats = compute_scoped_workspace_stats(all_files, state.get("lookalikes", []), scope_folder=scope_folder)
            cnt = stats["duplicatePairsCount"]
            if cnt == 0:
                return (f"{name_prefix}your shelves{scope_note} look clean: zero unresolved duplicate or near-duplicate pairs found.", [])
            return (
                f"{name_prefix}I spotted {cnt} duplicate/near-duplicate pair(s){scope_note} that could reclaim about {stats['reclaimableMB']} MB. "
                "Ask `show me all the duplicates` to review and archive extras safely.",
                [],
            )

        # Scope-aware workspace / shelves summary
        if re.search(
            r"\b(what(?:'s| is) on my shelves|my shelves|list (?:all )?shelves|summarize my (?:files|workspace)|overview of my|how are my files)\b",
            q_low,
        ):
            stats = compute_scoped_workspace_stats(all_files, state.get("lookalikes", []), scope_folder=scope_folder)
            shelf_summary = ", ".join(f"{s['shelfName']} ({s['count']})" for s in stats["byShelf"][:6])
            recent = sorted(all_files, key=lambda x: float(x.get("mtime") or 0), reverse=True)[:6]
            cites = [self._make_citation(f, f"{f.get('shelfName', '')} · {f['size']}", 88) for f in recent]
            sh_cnt = len(stats["byShelf"]) if scope_folder else len(shelves)
            return (
                f"{name_prefix}you currently have **{total_files} indexed file(s)** ({stats['totalHuman']}){scope_note} arranged across {sh_cnt} shelf(ves)"
                + (f": {shelf_summary}." if shelf_summary else "."),
                cites,
            )

        return None

    def _summarize_file_key_points(self, file_dict: dict[str, Any], max_points: int = 3, query: str = "") -> list[str]:
        """Extract concise, diverse key points from FULL SQLite extracted/OCR text via MMR."""
        return summarize_file_mmr(self, file_dict, query=query, max_points=max_points)

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
                "explain": f"explain {rest_clean or 'this file'}",
                "context": f"explain {rest_clean or 'this file'}",
                "inspect": f"inspect {rest_clean or 'this file'}",
                "graph": f"connect the dots for {rest_clean or 'this file'}",
                "reconcile": f"reconcile {rest_clean or 'invoices against spreadsheet'}",
                "compare": f"compare {rest_clean}" if rest_clean else "compare recent files",
                "ocr": f"run ocr on {rest_clean}" if rest_clean else "run ocr",
                "duplicates": "show me all the duplicates",
                "rename": f"rename {rest_clean}" if rest_clean else "rename messy files",
                "cleanup": "clean up my files",
                "organize": f"organize {rest_clean}".strip() if rest_clean else "organize",
                "audit": "audit workspace",
                "train": "/train",
                "list": f"list {rest_clean}" if rest_clean else "list everything",
                "help": "what can you do",
                "capabilities": "what can you do",
            }
            if cmd_low in {"train", "calibrate", "benchmark", "feed"}:
                from ai_training_feeder import run_continuous_ai_evaluation_and_feed, seed_foundational_ai_knowledge
                seeded = seed_foundational_ai_knowledge(self, include_default_routes=False)
                report = run_continuous_ai_evaluation_and_feed(store=None, auto_learn=True)
                cat_lines = [
                    f"- **{cat_name}:** **{info['score_pct']:.1f}%** ({info['passed']}/{info['total']} passed)"
                    for cat_name, info in (report.get("category_scores") or {}).items()
                ]
                ret_m = report.get("retrieval_metrics") or {}
                lg = report.get("learning_gain") or {}
                ans_train = (
                    f"{name_prefix}completed **Continuous AI Training, Data Feeding & Calibration** across **{report.get('total_cases', 66)} benchmark cases** "
                    f"(Overall accuracy: **{report.get('overall_score_pct', 100.0):.1f}%**):\n\n"
                    "**Category Scorecard**\n"
                    + "\n".join(cat_lines)
                    + "\n\n**Retrieval & Learning Calibration**\n"
                    + f"- **Precision@1:** {ret_m.get('P@1', 1.0):.4f} · **Precision@5:** {ret_m.get('P@5', 1.0):.4f} · **MRR:** {ret_m.get('MRR', 1.0):.4f}\n"
                    + f"- **Seeded Domain Knowledge Rules:** {seeded.get('seededCount', 0)} foundational preference(s) active in local memory\n"
                    + f"- **Adaptive Affinity Gain:** target rank improved from #{lg.get('before_affinity_rank', 1)} → #{lg.get('after_affinity_rank', 1)} ({lg.get('learned_rules_count', 0)} learned rules verified)"
                )
                return {
                    "question": q_clean,
                    "answer": ans_train,
                    "citations": [],
                    "choices": [
                        {"label": "Inspect AI memory", "prompt": "/memory", "kind": "followup"},
                        {"label": "Audit workspace", "prompt": "/audit", "kind": "action"},
                        {"label": "Explain a file", "prompt": "explain this file", "kind": "followup"},
                    ],
                    "trace": [
                        f"Seeded {seeded.get('seededCount', 0)} foundational domain rule(s)",
                        f"Evaluated {report.get('total_cases', 66)} multimodal AI cases ({report.get('overall_score_pct', 100.0):.1f}% pass)",
                    ],
                    "scopeFolder": effective_scope,
                    "mode": "workspace-insight",
                    "intentMode": "overview",
                    "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
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
            re.search(
                r"\b(who are you|what are you|what is good shelves|what can (?:you|u|ai) do|"
                r"what(?: all)? (?:can|do) you do|list (?:all )?(?:your )?(?:capabilities|features|actions|commands)|"
                r"your (?:capabilities|features)|how do you work|help me)\b",
                q_low,
            )
            or re.fullmatch(r"(?:help|commands|features|capabilities)", q_low)
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
                f"Hi{name_comma}! Here is everything I can do locally across your **{total_files} indexed file(s)**:\n\n"
                "**1. Semantic Search, OCR & Q&A**\n"
                "- **Search by meaning & content:** Ask questions across PDFs, DOCX, PPTX, XLSX, ODT, RTF, Markdown, code, and **RapidOCR** text inside images/receipts.\n"
                "- **Power filters & mentions:** Use `type:pdf`, `shelf:...`, `tag:...`, `after:YYYY-MM-DD`, `before:YYYY-MM-DD`, `has:ocr`, `-exclude`, or `@filename`.\n"
                "- **Summarize, totals & compare:** `summarize these files`, `summarize my tax PDFs`, `how much did I spend on receipts?`, `compare fileA and fileB`, `find files similar to ...`.\n\n"
                "**2. Folder Scoping & Sidebar Pinning**\n"
                "- **Scope Ask AI to a folder:** Pick a folder in the Scope bar, click **Ask AI** in Folders or on a pinned sidebar folder, or type `/scope <folder>` (`/scope all` to reset).\n"
                "- **Pin / unpin folders:** `pin folder Taxes` or `unpin folder Taxes`.\n\n"
                "**3. Safe File, Folder & Shelf Actions (Always Confirmed First)**\n"
                "- **Organize & System Audit:** `organize` or `/audit` lists everything ready to process first (duplicates, messy names, shelf folders, pending OCR) with readiness progress, and `run full organize plan` executes everything with one confirmation card.\n"
                "- **Move / Copy / Folders:** `move <files> to <folder>`, `copy <files> to <folder>`, `create folder <name> inside <parent>`, `move folder <src> to <dst>`, `rename folder <old> to <new>`, `delete folder <name>`.\n"
                "- **Rename (Single, Messy, or Custom Format):** `rename <file> to <new name>`, `rename messy files`, or custom formats like `rename all receipts to {date}_{merchant}_{amount}`, `rename photos using format Vacation_{date}_{seq}`, `add prefix Tax_ to receipts`.\n"
                "- **Tag, Pin & Shelve:** `tag <files> with #tag`, `untag #tag from <files>`, `pin <files>`, `unpin <files>`, `create shelf <name> with <files>`, `shelve <files> under <shelf>`.\n"
                "- **OCR, Duplicates & Trash:** `run ocr on <images/pdfs>` (`/ocr`), `show me all the duplicates` (`/duplicates`), `delete <files>` (reversible Trash), `permanently delete <files>`, `restore trashed files`, and `undo`.\n\n"
                "**4. Multi-Action Chaining & Refinement**\n"
                "- Chain multiple actions in one prompt (even on different files), e.g. *“create folder Tax 2026, move receipts into it, tag them #2026, and trash duplicate screenshots”*.\n"
                "- Refine any pending confirmation card before applying: `only include pdfs`, `exclude images`, or `cancel plan`.\n\n"
                "**5. Continuous Local Learning & Slash Commands**\n"
                "- I learn folder routes, tag preferences, and skipped file types from your confirmations — or teach me directly with `remember receipts go in Finance` (`/memory` to inspect).\n"
                "- **Slash commands:** `/audit`, `/organize`, `/list`, `/summarize`, `/compare`, `/ocr`, `/duplicates`, `/rename`, `/cleanup`, `/memory`, `/scope`, `/help` — plus quick math & date/time."
            )
            return {
                "question": q_clean,
                "answer": msg,
                "citations": [],
                "choices": self._build_smart_choices("conversational", [], None, "help"),
                "trace": trace,
                "scopeFolder": effective_scope,
                "mode": "conversational",
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

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
            history=history,
        )
        if action_resp is not None:
            return action_resp

        conv_ctx = extract_conversation_context(
            state,
            history=history,
            selected_file_id=selected_file_id,
            recent_file_ids=recent_ids,
            scope_folder=effective_scope,
        )

        # Explicit "list everything" / "show all files" / "what files are here" -> list all indexed items first + process summary
        is_list_everything = bool(
            re.fullmatch(
                r"(?:list|show|display|give\s+me)\s+(?:all\s+(?:the\s+)?|every(?:thing)?(?:\s+in\s+(?:here|this\s+folder|workspace))?|all\s+(?:my\s+)?files|everything)|"
                r"(?:what\s+files\s+(?:are\s+(?:in\s+here|here|indexed)|do\s+i\s+have)|what(?:'s| is)\s+in\s+(?:here|this\s+folder))",
                q_low,
            )
        )
        if is_list_everything:
            ordered = sorted(active_files, key=lambda x: (str(x.get("shelfName") or "zz"), -float(x.get("mtime") or 0)))
            audit = build_workspace_process_audit(state, active_files, self.preview_tidy_folders(), scope_folder=effective_scope)
            cites = [self._make_citation(f, f"{f.get('shelfName') or 'Workspace'} · {f.get('folder') or ''}", 92) for f in ordered[:60]]
            by_shelf_lines: dict[str, list[str]] = {}
            for f in ordered[:45]:
                sh = str(f.get("shelfName") or "Quiet Tray")
                by_shelf_lines.setdefault(sh, []).append(
                    f"  - `{f['name']}` ({f['format']}, {f['size']} · {f.get('folder') or ''})"
                )
            sections = [f"**{sh}** ({len(items)} shown)\n" + "\n".join(items) for sh, items in by_shelf_lines.items()]
            more_note = f"\n\n*…showing first 45 of {len(ordered)} files; see the citations below or narrow by folder/type.*" if len(ordered) > 45 else ""
            ans_txt = (
                f"{name_prefix}here is everything currently indexed ({len(ordered)} file(s) · {audit['totalHuman']}):\n\n"
                + ("\n\n".join(sections) if sections else "No files indexed yet.")
                + more_note
            )
            return {
                "question": q_clean,
                "answer": ans_txt,
                "citations": cites,
                "choices": [
                    {"label": "Organize / audit workspace", "prompt": "organize", "kind": "action"},
                    {"label": "Show duplicates", "prompt": "show me all the duplicates", "kind": "action"},
                    {"label": "Summarize key points", "prompt": "summarize these files", "kind": "followup"},
                ],
                "processSummary": audit["processSummary"],
                "trace": [*trace, f"Listed all {len(ordered)} file(s) in scope"],
                "scopeFolder": effective_scope,
                "mode": "workspace-insight",
                "appliedFilters": ([f"folder: {format_path_short(effective_scope)}"] if effective_scope else []),
                "intentMode": "list",
                "totalMatched": len(ordered),
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        # High-level workspace, storage, duplicate, and shelf-metaphor queries (strictly scope-aware!)
        overview = self._answer_workspace_overview(q_low, scoped_state, name_prefix, name_comma, scope_folder=effective_scope)
        if overview is not None:
            ov_ans, ov_cites = overview
            return {
                "question": q_clean,
                "answer": ov_ans,
                "citations": ov_cites,
                "choices": self._build_smart_choices("overview", ov_cites, None, q_low),
                "trace": trace,
                "scopeFolder": effective_scope,
                "mode": "workspace-insight",
                "appliedFilters": ([f"folder: {format_path_short(effective_scope)}"] if effective_scope else []),
                "intentMode": "overview",
                "totalMatched": len(ov_cites),
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        # Cross-File Knowledge Graph, Version Lineage & Invoice-to-Spreadsheet Reconciliation
        is_cross_file_q = bool(
            re.search(
                r"\b(?:reconcile|which\s+(?:invoices?|receipts?|bills?)\s+are\s+(?:missing|not\s+in|unrecorded)|"
                r"missing\s+from\s+\S+\.(?:csv|xlsx|tsv)|compare\s+(?:all\s+)?(?:invoices?|receipts?)\s+(?:against|with|to)\s+|"
                r"connect\s+the\s+dots|what\s+files\s+are\s+(?:connected|linked)\s+to|connected\s+files\s+for|"
                r"knowledge\s+graph\s+for|version\s+history\s+of|version\s+lineage\s+of|lineage\s+of)\b",
                q_low,
            )
        )
        if is_cross_file_q and active_files:
            by_id_cf = {f["id"]: f for f in active_files}
            focus_cf: list[dict[str, Any]] = []
            for f in active_files:
                fn_low = str(f.get("name") or "").lower()
                st_low = Path(fn_low).stem.lower()
                if (len(fn_low) >= 4 and fn_low in q_low) or (len(st_low) >= 4 and re.search(rf"\b{re.escape(st_low)}\b", q_low)):
                    focus_cf.append(f)
            if not focus_cf and selected_file_id and selected_file_id in by_id_cf:
                focus_cf = [by_id_cf[selected_file_id]]
            if not focus_cf and conv_ctx.get("contextFiles"):
                focus_cf = [f for f in conv_ctx["contextFiles"] if f["id"] in by_id_cf][:3]
            if not focus_cf and recent_ids:
                focus_cf = [by_id_cf[fid] for fid in recent_ids if fid in by_id_cf][:3]
            if not focus_cf:
                focus_cf = active_files[:3]
            ft_cf = get_full_texts_batch(self, focus_cf, fallback_excerpt=True)
            cf_res = answer_cross_file_question(self, q_effective, focus_cf, active_files, ft_cf, name_prefix=name_prefix)
            if cf_res is not None:
                cf_md, cf_files = cf_res
                cf_cites = [self._make_citation(f, f"{f.get('shelfName') or ''} · {f.get('folder') or ''}", 95) for f in cf_files[:max_citations]]
                return {
                    "question": q_clean,
                    "answer": cf_md,
                    "citations": cf_cites,
                    "choices": self._build_smart_choices("deep_context", cf_cites, None, q_low),
                    "trace": [*trace, f"Synthesized cross-file knowledge graph across {len(cf_files)} file(s)"],
                    "scopeFolder": effective_scope,
                    "mode": "workspace-insight",
                    "appliedFilters": [],
                    "intentMode": "deep_context",
                    "totalMatched": len(cf_cites),
                    "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                }

        # Context follow-up on selected, recent, or key-point summary / deep-context requests
        # (now supports self-healing on-demand OCR, Deep File Context Dossier, granular structure Q&A, factoids, MMR summary, AND Ollama synthesis!)
        is_deictic_deep_context = bool(
            re.search(
                r"\b(?:explain|analyze|analyse|inspect|deep\s+dive(?:\s+into)?|understand|file\s+context|context\s+of|full\s+profile|dossier|break\s+down|what(?:'s|\s+is)\s+inside)\s+"
                r"(?:it|this|that|this\s+file|that\s+file|this\s+document|that\s+document|this\s+script|this\s+code|this\s+spreadsheet|this\s+csv|this\s+pdf|this\s+deck|this\s+presentation|this\s+image|this\s+photo|this\s+receipt|these|them|those|these\s+files)\b",
                q_low,
            )
            or re.fullmatch(
                r"(?:explain|analyze|analyse|inspect|deep\s+dive|file\s+context|dossier|explain\s+this|inspect\s+this|analyze\s+this|what\s+does\s+this\s+(?:code|script|file)\s+do|how\s+does\s+this\s+(?:code|script|file)\s+work)",
                q_low,
            )
        )
        is_deictic_qa = is_deictic_deep_context or bool(
            re.search(
                r"\b(this file|that file|this document|that document|this picture|that picture|this photo|that photo|this image|that image|selected file|"
                r"what is (?:this|that|it)|what(?:'s| is) in (?:this|that|it)|what does (?:this|that|it) say|read (?:this|that|it)|"
                r"what about (?:this|that|it|the other one|the second one|the first one)|"
                r"summarize (?:it|these|them|those|this|that|files|all files|this folder|folder|key points|the key points|these files|these docs|these documents)|"
                r"(?:give|show|extract|list)(?:\s+me)?(?:\s+the)?\s+key\s+(?:points|takeaways)|"
                r"tell me (?:more |everything )?about (?:it|this|that)|where is (?:it|this|that))\b",
                q_low,
            )
            or re.fullmatch(r"(?:key\s+points|key\s+takeaways|summarize|what\s+is\s+this|what\s+is\s+that|what\s+about\s+that)", q_low)
        )
        if is_deictic_qa:
            by_id = {f["id"]: f for f in active_files}
            focus_list: list[dict[str, Any]] = []
            ctx_list = [f for f in (conv_ctx.get("contextFiles") or []) if f["id"] in by_id]
            if re.search(r"\b(second|2nd|other)\b", q_low) and len(ctx_list) >= 2:
                focus_list = [ctx_list[1]]
            elif re.search(r"\b(these|them|those|key points|takeaways)\b", q_low) and (recent_ids or ctx_list):
                focus_list = [by_id[fid] for fid in recent_ids[:6] if fid in by_id] or ctx_list[:6]
            elif selected_file_id and selected_file_id in by_id and not re.search(r"\b(these|them|those)\b", q_low):
                focus_list = [by_id[selected_file_id]]
            elif ctx_list:
                focus_list = [ctx_list[0]] if re.search(r"\b(it|this|that)\b", q_low) else ctx_list[:6]
            elif recent_ids:
                focus_list = [by_id[fid] for fid in recent_ids[:6] if fid in by_id]
            elif active_files:
                focus_list = sorted(active_files, key=lambda x: float(x.get("mtime") or 0), reverse=True)[:5]

            if focus_list:
                # Self-healing on-demand targeted OCR if user asks about 1-2 un-OCR'd images/PDFs
                enriched_ocr = ensure_ocr_for_focus_files(self, focus_list, max_auto_ocr=2)
                if enriched_ocr:
                    trace.append(f"Auto-ran RapidOCR on {', '.join(enriched_ocr)}")
                full_map_d = get_full_texts_batch(self, focus_list, fallback_excerpt=True)
                deep_ans_d = (
                    answer_deep_file_question(self, q_effective, focus_list, full_map_d, name_prefix=name_prefix)
                    if is_deictic_deep_context or "everything about" in q_low
                    else None
                )
                factoid_hit = None if deep_ans_d else extract_factoid_answer(q_effective, focus_list, full_map_d)
                cites = []
                bullets = []
                pts_per_file = 3 if len(focus_list) == 1 else 2
                for ff in focus_list:
                    ft_comb = full_map_d.get(str(ff["id"]), {}).get("combined", "")
                    pts = summarize_file_mmr(self, ff, query=q_effective, max_points=pts_per_file, full_text_override=ft_comb)
                    cites.append(self._make_citation(ff, pts[0] if pts else "", 96))
                    if len(focus_list) == 1:
                        bullets.extend(f"- {pt}" for pt in pts)
                    else:
                        joined_pts = " · ".join(pts)
                        bullets.append(f"- **{ff['title']}** (`{ff['format']}`, {ff['size']}): {joined_pts}")
                if deep_ans_d:
                    ans = deep_ans_d
                    verified_d = deep_ans_d
                    eff_intent = "deep_context"
                else:
                    header = (
                        f"{name_prefix}key points from **{focus_list[0]['title']}**:"
                        if len(focus_list) == 1
                        else f"{name_prefix}key points across **{len(focus_list)} file(s)**:"
                    )
                    ans = (f"{factoid_hit}\n\n" if factoid_hit else "") + f"{header}\n" + "\n".join(bullets)
                    verified_d = factoid_hit or ""
                    eff_intent = "summarize"
                out_mode = "intent-extractive"
                if bool(settings.get("ollama_enabled", False)):
                    deep_ctx = build_deep_file_context(self, cites, max_files=6, chars_per_file=2000)
                    ollama_ans = synthesize_qa_answer(
                        self,
                        q_effective,
                        deep_ctx,
                        memory_rows,
                        verified_facts=verified_d,
                        scope_folder=effective_scope,
                        history=history,
                    )
                    if ollama_ans:
                        ans = ollama_ans
                        out_mode = "ollama-local"
                        trace.append("Synthesized deictic answer via local Ollama")
                return {
                    "question": q_clean,
                    "answer": ans,
                    "citations": cites,
                    "choices": self._build_smart_choices(eff_intent, cites, None, q_low),
                    "trace": [*trace, f"Inspected {len(cites)} file(s) ({eff_intent})"],
                    "scopeFolder": effective_scope,
                    "mode": out_mode,
                    "appliedFilters": [],
                    "intentMode": eff_intent,
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
                        "trace": trace,
                        "scopeFolder": effective_scope,
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
                    "trace": trace,
                    "scopeFolder": effective_scope,
                    "mode": "intent-extractive",
                    "appliedFilters": [],
                    "intentMode": "search",
                    "totalMatched": 0,
                    "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
                }

        # Pre-parse unified `FileQuerySpec` AST (dates, due windows, multi-type, entities, timeline, finance, etc.)
        text_q, op_filters = _extract_search_operators(q_effective)
        query_spec = parse_file_query(text_q, state=scoped_state)
        nl = query_spec.to_nl_dict()
        intent_mode = nl["intent_mode"]
        applied_filters = list(nl["applied_filters"])
        if effective_scope:
            applied_filters.insert(0, f"folder: {format_path_short(effective_scope)}")
        has_structured_intent = bool(
            nl["has_strict_filters"]
            or op_filters
            or intent_mode in ("list", "count", "summarize", "deep_context", "finance", "due_soon", "timeline")
        )

        # Direct due-payments / upcoming bills handler (fixes Proactive Insight #2: "show me payments due in the next 7 days"!)
        if intent_mode == "due_soon" and query_spec.due_window is not None:
            due_matches = [f for f in active_files if matches_query_spec(f, query_spec)]
            due_matches = apply_sort_and_limit(due_matches, query_spec)
            due_md = format_due_payments_answer(due_matches, due_label=query_spec.due_window["label"], name_prefix=name_prefix)
            due_cites = [
                self._make_citation(
                    f,
                    f"Due {(f.get('metadata') or {}).get('due_date', '')} · {(f.get('metadata') or {}).get('merchant', f['title'])}",
                    96,
                )
                for f in due_matches[:max(max_citations, 12)]
            ]
            return {
                "question": q_clean,
                "answer": due_md,
                "citations": due_cites,
                "choices": self._build_smart_choices("due_soon", due_cites, None, q_low),
                "trace": [*trace, f"Matched {len(due_matches)} due payment(s) ({query_spec.due_window['label']})"],
                "scopeFolder": effective_scope,
                "mode": "intent-extractive",
                "appliedFilters": applied_filters,
                "intentMode": "due_soon",
                "totalMatched": len(due_matches),
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        # Direct specific-file question ("what does receipt_04.png say?") -> self-healing OCR if un-OCR'd!
        ask_file_m = re.match(r"^(?:what\s+does|read|show\s+text\s+(?:of|in|from)|what(?:'s| is)\s+(?:in|inside|on))\s+([A-Za-z0-9 _.\-]+\.(?:png|jpg|jpeg|webp|bmp|tiff|pdf))\b", q_effective.strip(), re.I)
        if ask_file_m:
            fname_q = ask_file_m.group(1).strip().lower()
            exact_f = [f for f in active_files if f["name"].lower() == fname_q]
            if exact_f:
                enriched_ocr = ensure_ocr_for_focus_files(self, exact_f, max_auto_ocr=1)
                if enriched_ocr:
                    trace.append(f"Auto-ran RapidOCR on {enriched_ocr[0]}")

        # Check general/non-file prompts when Ollama is enabled or when nothing matches well
        search_limit = 60 if intent_mode in ("list", "count", "timeline", "finance") else max(18, max_citations * 2)
        search_res = self.search_semantic(
            q_effective,
            modality_filter="all",
            limit=search_limit,
            min_similarity=0.14,
            scope_folder=effective_scope,
            scope_recursive=scope_recursive,
        )
        candidates = list(search_res.get("results", []))
        # Stand on the title in Q&A: if any active files have a direct title match for the query, promote them to the top!
        title_priority_hits = match_files_by_title_priority(nl["topic_query"] or q_effective, active_files)
        if title_priority_hits:
            tp_ids = {f["id"] for f in title_priority_hits}
            boosted = [
                {**f, "matchScore": max(int(f.get("matchScore") or 85), 96 - idx * 2)}
                for idx, f in enumerate(title_priority_hits[:8])
            ]
            rest_cands = [c for c in candidates if c["id"] not in tp_ids]
            candidates = boosted + rest_cands
        total_matched = max(int(search_res.get("totalMatched") or len(candidates)), len(candidates))
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
                "trace": trace,
                "scopeFolder": effective_scope,
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
                        "trace": trace,
                        "scopeFolder": effective_scope,
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
                "trace": trace,
                "scopeFolder": effective_scope,
                "mode": "conversational",
                "appliedFilters": [],
                "intentMode": "conversational",
                "totalMatched": 0,
                "latencyMs": round((time.perf_counter() - t0) * 1000.0, 2),
            }

        is_factoid_lookup = bool(
            re.search(
                r"\b(?:invoice\s+(?:number|no\.?|#|id)|ref(?:erence)?\s+(?:number|no\.?|#)|receipt\s+(?:number|no\.?|#)|"
                r"due\s+date|when\s+is\s+.+?\s+due|email|e-mail|phone|contact\s+number|telephone|tin|tax\s+id)\b",
                q_low,
            )
        )
        is_tabular_or_structural_qa = bool(
            intent_mode == "deep_context"
            or re.search(
                r"\b(?:in\s+\S+\.(?:csv|tsv|xlsx|xlsm|xls)|where\s+[a-z0-9_ ]+\s*(?:is|equals?|=|>|<|>=|<=|!=|contains?)|"
                r"(?:breakdown|break\s+down|group\s+by|grouped\s+by)\s+[a-z0-9_ ]+|"
                r"what\s+columns?|column\s+headers?|how\s+many\s+(?:rows?|columns?|slides?)|"
                r"on\s+page\s+\d+|summarize\s+page\s+\d+|on\s+slide\s+\d+|outline\s+(?:the\s+)?slides)\b",
                q_low,
            )
        )
        is_superlative_newest = bool(re.search(r"\b(latest|newest|most recent|last|recent)\b", q_low)) and intent_mode != "list" and not is_tabular_or_structural_qa
        is_superlative_oldest = bool(re.search(r"\b(oldest|earliest|first)\b", q_low)) and not is_tabular_or_structural_qa
        is_superlative_largest = bool(re.search(r"\b(largest|biggest|heaviest|most space)\b", q_low)) and not is_tabular_or_structural_qa
        is_finance_intent = (not is_factoid_lookup) and (not is_tabular_or_structural_qa) and (
            intent_mode == "finance"
            or bool(re.search(r"\b(how much|total|spent|spend|cost|price|paid|receipt|invoice|amount|bill|expense)\b", q_low))
        )
        is_location_intent = (not is_tabular_or_structural_qa) and bool(
            re.search(r"\b(where\s+is|find|locate|looking for|do i have|bring|get|send|move|copy|need|open)\b", q_low)
        )

        q_vec_384 = self.embedder._encode_text_384(nl["topic_query"] or q_clean)
        concepts, raw_terms, qa_phrase = _query_concepts(nl["topic_query"] or q_clean)
        q_tokens = {t for c in concepts for t in c["base"]} | set(raw_terms) | {t.lower() for t in nl["include_phrases"]}

        effective_max = max(max_citations, 25) if intent_mode in ("list", "count") else max_citations
        pool_limit = max(len(candidates), 30) if intent_mode in ("list", "count") else 14

        # Collect FULL-TEXT passage windows from SQLite (eliminates 600-char blindness!)
        pool_candidates = candidates[:pool_limit]
        full_texts_pool = get_full_texts_batch(self, pool_candidates, fallback_excerpt=False)

        qa_idf_map: dict[str, float] = {}
        n_pool = max(1, len(pool_candidates))
        for concept in concepts:
            for variant in concept["base"]:
                df = sum(
                    1
                    for f in pool_candidates
                    if variant in f"{f.get('title', '')} {f.get('name', '')} {full_texts_pool.get(str(f.get('id', '')), {}).get('combined', '')}".lower()
                )
                qa_idf_map[variant] = float(np.log((n_pool + 1.0) / (df + 0.5)) + 1.0)
        qa_words = qa_phrase.split() if qa_phrase else []
        qa_bigrams = [
            f"{qa_words[i]} {qa_words[i + 1]}"
            for i in range(len(qa_words) - 1)
            if len(f"{qa_words[i]} {qa_words[i + 1]}") >= 5
        ]
        file_windows: list[Optional[list[str]]] = []
        flat_windows: list[str] = []
        flat_offsets: list[tuple[int, int]] = []
        for f in pool_candidates:
            ft_entry = full_texts_pool.get(str(f.get("id") or ""), {})
            body = ft_entry.get("combined") or " ".join(p for p in [f.get("ocrText", ""), f.get("extractedText", ""), f.get("title", "")] if p).strip()
            if nl["include_phrases"] and body:
                file_windows.append(None)
                flat_offsets.append((0, 0))
            elif body:
                windows = split_sliding_chunks(body, max_chunks=24, chunk_chars=300)
                start_i = len(flat_windows)
                flat_windows.extend(windows)
                file_windows.append(windows)
                flat_offsets.append((start_i, len(flat_windows)))
            else:
                file_windows.append([])
                flat_offsets.append((0, 0))

        flat_vecs = self.embedder.encode_text_384_batch(flat_windows) if flat_windows else []

        scored_passages: list[tuple[float, dict[str, Any]]] = []
        for f, windows, (start_i, end_i) in zip(pool_candidates, file_windows, flat_offsets):
            ft_entry = full_texts_pool.get(str(f.get("id") or ""), {})
            body = ft_entry.get("combined") or " ".join(p for p in [f.get("ocrText", ""), f.get("extractedText", ""), f.get("title", "")] if p).strip()
            best_seg = f"{f.get('shelfName', '')} · {f.get('folder', '')}/{f.get('name', '')}"
            best_score = float(f.get("matchScore", 50)) / 100.0
            f_title_toks = _field_tokens(f"{f.get('title', '')} {f.get('name', '')}")

            if windows is None:
                best_seg = _extract_snippet_around_terms(body, nl["include_phrases"], max_len=240)
            elif windows:
                best_seg = windows[0]
                best_score = -1.0
                base_match = float(f.get("matchScore", 50)) / 100.0
                for w, w_vec in zip(windows, flat_vecs[start_i:end_i]):
                    sem = float(np.clip(np.dot(q_vec_384, w_vec), 0.0, 1.0))
                    w_toks = _field_tokens(w)
                    combined_toks = w_toks | f_title_toks
                    if concepts:
                        idf_hit = 0.0
                        idf_tot = 0.0
                        hit_concepts = 0
                        for c in concepts:
                            c_idf = max((qa_idf_map.get(v, 1.0) for v in c["base"]), default=1.0)
                            idf_tot += c_idf
                            if c["base"] & w_toks:
                                idf_hit += c_idf
                                hit_concepts += 1
                            elif c["base"] & f_title_toks:
                                idf_hit += 0.65 * c_idf
                                hit_concepts += 1
                            elif c["syn"] & combined_toks:
                                idf_hit += 0.55 * c_idf
                                hit_concepts += 1
                        cov_ratio = hit_concepts / float(max(1, len(concepts)))
                        overlap = (idf_hit / max(1e-6, idf_tot)) * (0.55 + 0.45 * cov_ratio)
                    else:
                        overlap = (len(q_tokens & w_toks) / float(max(1, len(q_tokens)))) if q_tokens else 0.0

                    w_norm = " ".join(_search_tokens(w))
                    bg_bonus = 0.0
                    if qa_phrase and len(qa_phrase) >= 4 and qa_phrase in w_norm:
                        bg_bonus = 0.14
                    elif qa_bigrams and any(bg in w_norm for bg in qa_bigrams):
                        bg_bonus = 0.09
                    if is_finance_intent and re.search(r"(?:total|amount|paid|PHP|USD|₱|\$)\s*[:=]?\s*[\d,]+(?:\.\d{2})?", w, re.I):
                        bg_bonus += 0.06

                    score = 0.46 * sem + 0.36 * overlap + 0.18 * base_match + bg_bonus
                    if score > best_score:
                        best_score = score
                        best_seg = w

            f_meta_anc = f.get("metadata") if isinstance(f.get("metadata"), dict) else {}
            if best_seg and not re.search(r"^\s*\[(?:Page|Slide|Line|Sheet)\s+", best_seg):
                anc = _find_passage_anchor(
                    best_seg,
                    {
                        "modality": f.get("modality"),
                        "pageTexts": f_meta_anc.get("pageTexts") or [],
                        "presentation": {"slides": f_meta_anc.get("slides") or []},
                    },
                    body,
                )
                if anc:
                    best_seg = f"{anc}{best_seg}"

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

        # Filter out noisy low-relevance tail citations when the top citation is a strong match
        if scored_passages and intent_mode not in ("list", "count") and not (nl["has_strict_filters"] or op_filters):
            top_p_score = scored_passages[0][0]
            if top_p_score >= 0.48:
                cutoff_p = max(0.36, top_p_score * 0.64)
                tight_passages = [sp for sp in scored_passages if sp[0] >= cutoff_p]
                if tight_passages:
                    scored_passages = tight_passages
                    total_matched = len(tight_passages)

        citations = [item for _, item in scored_passages[:effective_max]]
        cited_ids_set = {str(c.get("fileId") or "") for c in citations}

        top = citations[0] if citations else None
        answer = "I didn't find any matching files yet."
        verified_facts = ""
        if top:
            filter_note = f" ({', '.join(applied_filters)})" if applied_filters else ""
            by_id_qa = {f["id"]: f for f in active_files}
            top_files_qa = [by_id_qa[c["fileId"]] for c in citations[:4] if c["fileId"] in by_id_qa]
            for af in active_files:
                if af["name"].lower() in q_low and all(x["id"] != af["id"] for x in top_files_qa):
                    top_files_qa.insert(0, af)
                    if str(af["id"]) not in full_texts_pool:
                        full_texts_pool.update(get_full_texts_batch(self, [af], fallback_excerpt=False))
            pre_deep_ans = (
                answer_deep_file_question(self, q_effective, top_files_qa, full_texts_pool, name_prefix=name_prefix)
                if is_tabular_or_structural_qa
                else None
            )
            if pre_deep_ans:
                answer = pre_deep_ans
                verified_facts = pre_deep_ans
                intent_mode = "deep_context"
                trace.append(f"Synthesized deep file context profile for {top_files_qa[0]['name']}" if top_files_qa else "Synthesized deep file context")
            elif intent_mode == "timeline":
                t_label = query_spec.temporal["label"] if query_spec.temporal else ""
                answer = format_timeline_answer(candidates, window_label=t_label, name_prefix=name_prefix)
            elif intent_mode == "count":
                shelf_counts: dict[str, int] = {}
                for c in citations:
                    sh = c.get("shelfName") or "Workspace"
                    shelf_counts[sh] = shelf_counts.get(sh, 0) + 1
                sh_summary = ", ".join(f"{cnt} in {sh}" for sh, cnt in list(shelf_counts.items())[:4])
                noun = "file" if total_matched == 1 else "files"
                answer = f"{name_prefix}you have {total_matched} matching {noun}{filter_note}" + (f": {sh_summary}." if sh_summary else ".")
            elif intent_mode == "list":
                noun = "item" if total_matched == 1 else "items"
                if len(citations) <= 4:
                    titles_preview = ", ".join(f"“{c['title']}”" for c in citations)
                    answer = f"{name_prefix}I found {total_matched} {noun}{filter_note}: {titles_preview}."
                else:
                    listed_lines = [
                        f"- **{c['title']}** (`{c['fileName']}` · {c['format']}, {c['size']})"
                        for c in citations[:20]
                    ]
                    more_note = f"\n- *…plus {len(citations) - 20} more in the cards below.*" if len(citations) > 20 else ""
                    answer = f"{name_prefix}I found **{total_matched} {noun}**{filter_note}:\n" + "\n".join(listed_lines) + more_note
            elif is_finance_intent:
                # Full-corpus deduplicated financial analytics (scans ALL matching financial records, never capped at 18!)
                specific_merchant_tokens = [
                    t for t in raw_terms
                    if t not in {"how", "much", "did", "spend", "spent", "spending", "total", "cost", "price", "paid", "pay", "receipt", "receipts", "invoice", "invoices", "bill", "bills", "expense", "expenses", "amount", "money", "on", "at", "for", "in", "my", "all", "average", "breakdown", "summary"}
                ]
                # Start from ALL active files in scope that match strict filters (e.g., date/folder/merchant)
                corpus_pool = [
                    f for f in active_files
                    if (not query_spec.has_strict_filters or matches_query_spec(f, query_spec))
                ]
                if specific_merchant_tokens:
                    ft_fin = get_full_texts_batch(self, corpus_pool)
                    scored_merchants: list[tuple[int, dict[str, Any]]] = []
                    for f in corpus_pool:
                        body_m = ft_fin.get(str(f["id"]), {}).get("combined", "")
                        hay_m = f"{f.get('title', '')} {f.get('name', '')} {json.dumps(f.get('metadata') or {})} {body_m}".lower()
                        hit_cnt = sum(1 for tok in specific_merchant_tokens if tok in hay_m)
                        if hit_cnt > 0:
                            scored_merchants.append((hit_cnt, f))
                    if scored_merchants:
                        max_m_hits = max(cnt for cnt, _ in scored_merchants)
                        fin_pool = [f for cnt, f in scored_merchants if cnt == max_m_hits]
                    else:
                        fin_pool = candidates
                else:
                    fin_pool = corpus_pool

                analytics = compute_financial_analytics(self, fin_pool, question=q_effective, name_prefix=name_prefix)
                if analytics["records"]:
                    answer = analytics["markdown"]
                    verified_facts = analytics["markdown"]
                    rec_ids = {r["fileId"] for r in analytics["records"]}
                    citations = [
                        self._make_citation(
                            r["file"],
                            f"{r['symbol']}{r['amount']:,.2f} · {r['merchant']} ({r['date'] or ''})",
                            95,
                        )
                        for r in analytics["records"][:effective_max]
                    ]
                    total_matched = len(rec_ids)
                    trace.append(
                        f"Analyzed {len(analytics['records'])} unique financial record(s)"
                        + (f" ({analytics['duplicateSkipped']} exact dup skipped)" if analytics["duplicateSkipped"] else "")
                    )
                else:
                    answer = f"{name_prefix}I found **{top['title']}** ({top['shelfName']}): {top['snippet']}"
            elif is_superlative_largest:
                answer = f"{name_prefix}the largest match is “{top['title']}” ({top['size']})."
            elif is_superlative_newest or is_superlative_oldest:
                adj = "oldest" if is_superlative_oldest else "most recent"
                answer = f"{name_prefix}your {adj} match is “{top['title']}” ({top['date']})."
            elif intent_mode == "summarize" and len(citations) >= 1:
                by_id_sum = {f["id"]: f for f in active_files}
                top_f = by_id_sum.get(citations[0]["fileId"])
                if len(citations) == 1 and top_f:
                    pts = self._summarize_file_key_points(top_f, max_points=3, query=q_effective)
                    answer = f"{name_prefix}key points from **{top_f['title']}**:\n" + "\n".join(f"- {pt}" for pt in pts)
                else:
                    lines_out = []
                    for c in citations[:4]:
                        ff = by_id_sum.get(c["fileId"])
                        pts = self._summarize_file_key_points(ff, max_points=2, query=q_effective) if ff else [c["snippet"][:200]]
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
                by_id_qa = {f["id"]: f for f in active_files}
                top_files_qa = [by_id_qa[c["fileId"]] for c in citations[:4] if c["fileId"] in by_id_qa]
                deep_qa_ans = answer_deep_file_question(self, q_effective, top_files_qa, full_texts_pool, name_prefix=name_prefix)
                if deep_qa_ans:
                    answer = deep_qa_ans
                    verified_facts = deep_qa_ans
                    trace.append(f"Synthesized deep file context profile for {top_files_qa[0]['name']}" if top_files_qa else "Synthesized deep file context")
                else:
                    factoid_ans = extract_factoid_answer(q_effective, top_files_qa, full_texts_pool)
                    top_f = by_id_qa.get(top["fileId"])
                    pts = self._summarize_file_key_points(top_f, max_points=3, query=q_effective) if top_f else []
                    lead_snip = top["snippet"].strip()
                    bullet_lines = [f"- {lead_snip}"]
                    for pt in pts:
                        if pt.lower()[:40] not in lead_snip.lower():
                            bullet_lines.append(f"- {pt}")
                        if len(bullet_lines) >= 3:
                            break
                    fact_lead = f"{factoid_ans}\n\n" if factoid_ans else ""
                    if factoid_ans:
                        verified_facts = factoid_ans
                    answer = (
                        fact_lead
                        + f"{name_prefix}from **{top['title']}** (`{top['fileName']}` · {top['shelfName']}):\n"
                        + "\n".join(bullet_lines)
                    )
                    if len(citations) > 1 and citations[1].get("snippet") and citations[1].get("score", 0) >= top.get("score", 90) - 12:
                        answer += f"\n\n**Related match** · **{citations[1]['title']}** (`{citations[1]['fileName']}`): {citations[1]['snippet'][:180]}"

        mode = "intent-extractive"
        if citations:
            deep_ctx = build_deep_file_context(self, citations, max_files=6, chars_per_file=2000)
            trace.append(f"Read {len(deep_ctx)} file(s) (full extracted + OCR text)")
            if bool(settings.get("ollama_enabled", False)):
                ollama_ans = try_ollama_structured_synthesis(
                    self,
                    q_effective,
                    deep_ctx,
                    memory_rows,
                    scope_folder=effective_scope,
                    history=history,
                    verified_facts=verified_facts,
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
        """Answer general conversational questions via `llm_router` (/api/chat + circuit breaker)."""
        return _ollama_chat_general(self, question, user_name=user_name, history=history)

    def _try_ollama_answer(
        self,
        question: str,
        citations: list[dict[str, Any]],
        history: Optional[list[dict[str, str]]] = None,
    ) -> Optional[str]:
        """Synthesize a Markdown answer via `llm_router` (/api/chat + circuit breaker)."""
        deep_ctx = build_deep_file_context(self, citations, max_files=6, chars_per_file=1800)
        return synthesize_qa_answer(self, question, deep_ctx, [], history=history)

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

    def rename_folder(self, folder_path: str, new_name: str, record_log: bool = True) -> dict[str, Any]:
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
        if record_log:
            self._record_action(
                "rename_folder",
                f"Renamed folder “{src.name}” → “{clean}”",
                {"label": f"Rename folder {src.name}", "fromPath": src_prefix, "toPath": dst_prefix},
            )
        return {"ok": True, "path": dst_prefix, "name": clean}

    def move_folder(self, folder_path: str, target_parent_folder: str) -> dict[str, Any]:
        """Move a directory cleanly into another directory and update indexed paths (undoable even if empty)."""
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
        self._record_action(
            "move_folder",
            f"Moved folder {src.name} to {format_path_short(target_parent)}",
            {"label": f"Move folder {src.name}", "fromPath": src_prefix, "toPath": dst_prefix, "moves": moves_log},
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
                        self._remove_file_from_search_indices(conn, rowid=int(r["rowid"]))
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
        copied_log: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for fid in file_ids:
            try:
                item = self.get_file_by_id(fid)
                src_p = str((item or {}).get("path") or "")
                res = self.copy_file(fid, target_folder, record_log=False)
                new_f = res.get("file") or {}
                copied_log.append({
                    "fromPath": src_p,
                    "toPath": str(res.get("path") or ""),
                    "newFileId": str(new_f.get("id") or ""),
                })
            except Exception as exc:
                failed.append({"fileId": str(fid), "error": str(exc)})
        if copied_log:
            self._record_action(
                "bulk_copy",
                f"Copied {len(copied_log)} file(s) to {format_path_short(Path(target_folder))}",
                {"label": f"Bulk Copy ({len(copied_log)})", "copies": copied_log},
            )
        return {"ok": True, "count": len(copied_log), "failed": failed}

    def create_custom_shelf(
        self,
        name: str,
        desc: str = "",
        query: str = "",
        modality: str = "all",
        tag: str = "",
        file_ids: Optional[list[str]] = None,
        record_log: bool = True,
    ) -> dict[str, Any]:
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Shelf name cannot be empty")
        sid = f"custom_{_stable_short_hash(f'{clean}:{query}:{time.time_ns()}', 10)}"
        created_at = time.time()
        fids = list(file_ids or [])
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO custom_shelves (shelf_id, name, desc, query, modality, tag, file_ids_json, pinned, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (sid, clean, desc.strip(), query.strip(), modality, tag.strip(), json.dumps(fids), created_at),
                )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        if record_log:
            self._record_action(
                "create_shelf",
                f"Created shelf “{clean}”",
                {
                    "label": f"Create shelf {clean}",
                    "shelfId": sid,
                    "name": clean,
                    "desc": desc.strip(),
                    "query": query.strip(),
                    "modality": modality,
                    "tag": tag.strip(),
                    "fileIds": fids,
                    "createdAt": created_at,
                },
            )
        return {"ok": True, "shelfId": sid}

    def rename_shelf(self, shelf_id: str, new_name: str, record_log: bool = True) -> dict[str, Any]:
        clean = (new_name or "").strip()
        if not clean:
            raise ValueError("Shelf name cannot be empty")
        old_name = shelf_id
        with self._lock:
            conn = self._connect()
            try:
                if shelf_id.startswith("custom_"):
                    row = conn.execute("SELECT name FROM custom_shelves WHERE shelf_id = ?", (shelf_id,)).fetchone()
                    if row:
                        old_name = str(row["name"])
                    conn.execute("UPDATE custom_shelves SET name = ? WHERE shelf_id = ?", (clean, shelf_id))
                else:
                    ren_r = conn.execute("SELECT custom_name FROM shelf_renames WHERE shelf_id = ?", (shelf_id,)).fetchone()
                    sh_r = conn.execute("SELECT name FROM shelves WHERE shelf_id = ?", (shelf_id,)).fetchone()
                    if ren_r:
                        old_name = str(ren_r["custom_name"])
                    elif sh_r:
                        old_name = str(sh_r["name"])
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
        if record_log and old_name != clean:
            self._record_action(
                "rename_shelf",
                f"Renamed shelf “{old_name}” → “{clean}”",
                {"label": f"Rename shelf {old_name}", "shelfId": shelf_id, "oldName": old_name, "newName": clean},
            )
        return {"ok": True, "shelfId": shelf_id, "name": clean}

    def delete_custom_shelf(self, shelf_id: str, record_log: bool = True) -> dict[str, Any]:
        saved_row: Optional[dict[str, Any]] = None
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute("SELECT * FROM custom_shelves WHERE shelf_id = ?", (shelf_id,)).fetchone()
                if r:
                    saved_row = dict(r)
                conn.execute("DELETE FROM custom_shelves WHERE shelf_id = ?", (shelf_id,))
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        if record_log and saved_row:
            self._record_action(
                "delete_shelf",
                f"Deleted shelf “{saved_row.get('name', shelf_id)}”",
                {
                    "label": f"Delete shelf {saved_row.get('name', shelf_id)}",
                    "shelfId": shelf_id,
                    "name": saved_row.get("name", ""),
                    "desc": saved_row.get("desc", ""),
                    "query": saved_row.get("query", ""),
                    "modality": saved_row.get("modality", "all"),
                    "tag": saved_row.get("tag", ""),
                    "fileIdsJson": saved_row.get("file_ids_json", "[]"),
                    "pinned": saved_row.get("pinned", 1),
                    "createdAt": saved_row.get("created_at", time.time()),
                },
            )
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

    def restore_trashed_file(self, file_id: str, record_log: bool = False) -> dict[str, Any]:
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
                self._restore_file_to_search_indices(conn, file_id=file_id)
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        if record_log:
            self._record_action(
                "restore_trash",
                f"Restored {dest.name} from Trash",
                {"label": f"Restore {dest.name}", "items": [{"fileId": file_id, "fromPath": str(cur_p), "toPath": str(dest)}]},
            )
        self.recluster_all()
        return {"ok": True, "restoredPath": str(dest), "fromPath": str(cur_p)}

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

    def get_proactive_insights(self) -> list[dict[str, Any]]:
        """Detect actionable patterns across the workspace and return insight cards."""
        state = self.get_all_state()
        return detect_insights(
            state["files"],
            state.get("lookalikes", []),
            active_vision_model=self.embedder.vision_model_id,
        )

    def record_search_telemetry(self, query: str, latency_ms: float, result_count: int, clicked_file_id: str = "", clicked_rank: int = -1) -> None:
        """Record a search event for local opt-in telemetry."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO search_telemetry (query, latency_ms, result_count, clicked_file_id, clicked_rank, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                    (query[:500], latency_ms, result_count, clicked_file_id, clicked_rank, time.time()),
                )
                # Keep only last 5000 telemetry rows
                conn.execute(
                    "DELETE FROM search_telemetry WHERE id NOT IN (SELECT id FROM search_telemetry ORDER BY id DESC LIMIT 5000)"
                )
                conn.commit()
            finally:
                conn.close()

    def record_action_feedback(self, action_type: str, file_count: int, accepted: bool) -> None:
        """Record whether a proposed AI action was accepted or rejected."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO action_feedback (action_type, file_count, accepted, timestamp) VALUES (?, ?, ?, ?)",
                    (action_type, file_count, 1 if accepted else 0, time.time()),
                )
                conn.commit()
            finally:
                conn.close()

    def get_telemetry_summary(self) -> dict[str, Any]:
        """Return aggregated telemetry metrics."""
        with self._lock:
            conn = self._connect()
            try:
                total_searches = conn.execute("SELECT COUNT(*) FROM search_telemetry").fetchone()[0]
                avg_latency = conn.execute("SELECT AVG(latency_ms) FROM search_telemetry").fetchone()[0] or 0.0
                total_actions = conn.execute("SELECT COUNT(*) FROM action_feedback").fetchone()[0]
                accepted_actions = conn.execute("SELECT COUNT(*) FROM action_feedback WHERE accepted = 1").fetchone()[0]
                acceptance_rate = (accepted_actions / total_actions * 100.0) if total_actions > 0 else 0.0
                return {
                    "totalSearches": total_searches,
                    "avgLatencyMs": round(avg_latency, 2),
                    "totalActions": total_actions,
                    "acceptedActions": accepted_actions,
                    "acceptanceRate": round(acceptance_rate, 1),
                }
            finally:
                conn.close()

    def export_index(self, export_path: Optional[str] = None) -> dict[str, Any]:
        """Export the full index to a .gsbk backup file for portability."""
        if export_path is None:
            export_path = str(self.data_dir / f"good-shelves-backup-{int(time.time())}.gsbk")
        export_file = Path(export_path)
        with self._lock:
            conn = self._connect()
            try:
                dest = sqlite3.connect(str(export_file))
                try:
                    self._raw_conn.backup(dest)
                finally:
                    dest.close()
            finally:
                conn.close()
        return {
            "path": str(export_file),
            "size_bytes": export_file.stat().st_size,
            "size_human": format_bytes(export_file.stat().st_size),
        }
