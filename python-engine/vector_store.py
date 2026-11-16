"""
Embedded SQLite + sqlite-vec Vector Store & Metadata Engine for Good Shelves.
Stores 512-dimensional L2-normalized vectors in a virtual `vec0` cosine index alongside
rich document/image/OCR metadata, rename history, folder tidy rollback logs, and user settings.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import sqlite_vec

from clustering import run_shelf_clustering
from deduplicator import find_lookalike_pairs
from devices import list_connected_devices
from embedder import MultimodalEmbedder
from vision_models import download_vision_model, get_catalog_status
from parsers import (
    AUDIO_EXTS,
    CODE_EXTS,
    DOC_EXTS,
    IMAGE_EXTS,
    SPREADSHEET_EXTS,
    VIDEO_EXTS,
    ParsedFile,
    compute_sha256,
    parse_file,
    run_ocr_on_image,
)
from renamer import generate_rename_suggestion


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


def pack_f32(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).ravel().tobytes()


def unpack_f32(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


# ---------------------------------------------------------------------------
# Query understanding helpers: light stemming, small synonym groups, and
# natural-language cues for modality / recency / folder intent.
# ---------------------------------------------------------------------------

_SEARCH_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "your", "you",
    "are", "was", "were", "have", "has", "had", "not", "but", "what", "all",
    "can", "out", "use", "used", "using", "file", "files", "find", "show", "me",
    "my", "about", "any", "some", "please", "get", "give", "which", "where",
    "there", "here", "one", "ones", "thing", "things", "stuff", "last", "week",
    "month", "year", "today", "yesterday", "recent", "latest", "new", "old",
    "of", "in", "on", "at", "to", "is", "it", "as", "by", "be", "or", "an",
    "a", "do", "does", "did", "so", "if", "then", "than", "up", "down",
}

_SYNONYM_GROUPS: tuple[set[str], ...] = (
    {"receipt", "invoice", "bill", "payment", "paid", "transaction", "purchase", "order"},
    {"photo", "picture", "image", "screenshot", "screen", "scan", "capture"},
    {"paper", "article", "study", "research", "journal", "publication", "manuscript", "thesis", "report"},
    {"code", "script", "program", "source", "function", "repository", "repo"},
    {"school", "college", "university", "student", "course", "enrollment", "registration", "transcript", "academic"},
    {"tax", "taxation", "revenue", "statute", "legal", "law", "court", "supreme"},
    {"coffee", "cafe", "latte", "espresso", "roast", "barista"},
    {"budget", "allowance", "expense", "finance", "money", "cost", "price", "cash"},
    {"slide", "deck", "presentation", "lecture", "chapter"},
    {"id", "card", "identity", "badge", "license"},
)

_SYNONYM_LOOKUP: dict[str, set[str]] = {}
for _group in _SYNONYM_GROUPS:
    for _word in _group:
        _SYNONYM_LOOKUP.setdefault(_word, set()).update(_group)

_MODALITY_CUES: dict[str, set[str]] = {
    "image": {"image", "images", "photo", "photos", "picture", "pictures", "screenshot", "screenshots", "scan", "scans", "png", "jpg", "jpeg", "webp", "diagram"},
    "document": {"pdf", "pdfs", "doc", "docx", "document", "documents", "paper", "papers", "article", "report", "slides", "spreadsheet", "csv", "text", "notes"},
    "code": {"code", "script", "scripts", "python", "javascript", "typescript", "source", "repo", "repository", "function", "class", "py", "rs", "ts", "tsx", "js"},
}

_FOLDER_CUES = {
    "download": "downloads",
    "desktop": "desktop",
    "document": "documents",
}

_RECENCY_CUES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\btoday\b", re.I), 1),
    (re.compile(r"\byesterday\b", re.I), 2),
    (re.compile(r"\bthis week\b|\blast week\b|\bpast week\b", re.I), 14),
    (re.compile(r"\bthis month\b|\blast month\b|\bpast month\b", re.I), 62),
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

_CONVERSATIONAL_NOISE = re.compile(
    r"\b(where\s+is|where's|wheres|find\s+my|show\s+me|looking\s+for|that|this|the|my|"
    r"i\s+took|i've\s+took|ive\s+took|taken|photo|pic|picture|image|snapshot|"
    r"last\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|month|year)|"
    r"this\s+(?:week|month|year)|yesterday|today|on\s+a|at\s+a|in\s+a|from)\b",
    re.IGNORECASE,
)


def _parse_temporal_window(query: str) -> Optional[tuple[float, float, str]]:
    """Parse natural-language date expressions like 'last sunday', 'yesterday', 'in 2024'."""
    lowered = (query or "").lower()
    now_dt = datetime.now()
    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    if "today" in lowered:
        return today_start.timestamp(), now_dt.timestamp(), "today"
    if "yesterday" in lowered:
        y0 = today_start - timedelta(days=1)
        return y0.timestamp(), today_start.timestamp(), "yesterday"

    m_day = re.search(r"\b(?:last|on)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    if m_day:
        target_wd = _WEEKDAYS[m_day.group(1)]
        days_back = (now_dt.weekday() - target_wd) % 7
        if days_back == 0:
            days_back = 7
        day_start = today_start - timedelta(days=days_back)
        day_end = day_start + timedelta(days=1)
        return day_start.timestamp(), day_end.timestamp(), f"last {m_day.group(1)}"

    if "last week" in lowered:
        return (today_start - timedelta(days=14)).timestamp(), (today_start - timedelta(days=7)).timestamp(), "last week"
    if "this week" in lowered:
        return (today_start - timedelta(days=now_dt.weekday())).timestamp(), now_dt.timestamp(), "this week"
    if "last month" in lowered:
        return (today_start - timedelta(days=60)).timestamp(), (today_start - timedelta(days=30)).timestamp(), "last month"
    if "this month" in lowered:
        return today_start.replace(day=1).timestamp(), now_dt.timestamp(), "this month"

    m_year = re.search(r"\b(?:in\s+)?(20[0-2]\d|19[89]\d)\b", lowered)
    if m_year:
        yr = int(m_year.group(1))
        y_start = datetime(yr, 1, 1).timestamp()
        y_end = datetime(yr + 1, 1, 1).timestamp()
        return y_start, y_end, str(yr)

    return None


def _clean_visual_query(query: str) -> str:
    """Strip conversational & temporal filler so CLIP/SigLIP sees the core visual subject."""
    cleaned = _CONVERSATIONAL_NOISE.sub(" ", query or "")
    cleaned = re.sub(r"\b20[0-2]\d\b", " ", cleaned)
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
    body_raw = _search_tokens(f"{f_row['extracted_text']} {f_row['ocr_text']}")

    title_norm = " ".join(title_raw)
    body_norm = " ".join(body_raw)

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
    """Return a clean excerpt centered around the first matched term when present."""
    clean = re.sub(r"\s+", " ", text or "").strip()
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


class _PooledConnection:
    """Lightweight proxy around the persistent SQLite connection so `.close()` is a safe no-op."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, parameters)

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
        self.silhouette_score: float = 0.68
        self._stage_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self.recent_events: list[dict[str, Any]] = []
        self._raw_conn: Optional[sqlite3.Connection] = None
        self._settings_cache: Optional[dict[str, Any]] = None
        self._state_cache: Optional[dict[str, Any]] = None
        self._init_db()
        active_vis = self.get_settings().get("vision_model", "clip-vit-b32")
        if active_vis and active_vis != self.embedder.vision_model_id:
            self.embedder.set_vision_model(active_vis)

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

                shelf_cols = {r["name"] for r in conn.execute("PRAGMA table_info(shelves)").fetchall()}
                if "pinned" not in shelf_cols:
                    conn.execute("ALTER TABLE shelves ADD COLUMN pinned INTEGER DEFAULT 0")

                existing_file_count = conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
                defaults = {
                    "realtime_watch": "true",
                    "ocr_enabled": "true",
                    "code_indexing": "true",
                    "battery_pause": "false",
                    "max_results": "50",
                    "min_similarity": "0.22",
                    "silhouette_score": "0.0",
                    "setup_completed": "true" if existing_file_count > 0 else "false",
                }
                for k, v in defaults.items():
                    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

                home = Path.home()
                for folder_name in ["Documents", "Downloads", "Desktop"]:
                    f_path = (home / folder_name).resolve()
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
                parsed = {
                    "realtime_watch": raw.get("realtime_watch", "true") == "true",
                    "ocr_enabled": raw.get("ocr_enabled", "true") == "true",
                    "code_indexing": raw.get("code_indexing", "true") == "true",
                    "battery_pause": raw.get("battery_pause", "false") == "true",
                    "max_results": int(raw.get("max_results", "50")),
                    "min_similarity": float(raw.get("min_similarity", "0.22")),
                    "silhouette_score": float(raw.get("silhouette_score", "0.0")),
                    "setup_completed": raw.get("setup_completed", "true") == "true",
                    "vision_model": raw.get("vision_model", "clip-vit-b32"),
                }
                self._settings_cache = parsed
                return dict(parsed)
            finally:
                conn.close()

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
                file_paths = [Path(r["path"]) for r in all_files]

                result: list[dict[str, Any]] = []
                for f in folders:
                    f_path = Path(f["path"])
                    count = sum(1 for fp in file_paths if fp == f_path or f_path in fp.parents)
                    result.append(
                        {
                            "path": str(f_path),
                            "display_path": f["display_path"],
                            "files": count,
                            "active": bool(f["active"]),
                        }
                    )
                return result
            finally:
                conn.close()

    def add_watched_folder(self, folder_path: str) -> list[dict[str, Any]]:
        resolved = Path(os.path.expanduser(folder_path)).resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO watched_folders (path, display_path, active) VALUES (?, ?, 1) "
                    "ON CONFLICT(path) DO UPDATE SET active = 1",
                    (str(resolved), format_path_short(resolved)),
                )
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()
        self.scan_folder(resolved, max_files=80)
        self.recluster_all()
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
        snippet = (parsed.extracted_text or parsed.title)[:82].replace("\n", " ").strip()
        return f"Matched document content (“{snippet}”)."

    def index_single_file(self, filepath: Path, force: bool = False, emit_telemetry: bool = True, recluster: bool = False) -> Optional[dict[str, Any]]:
        """
        Run the 4-step local ingestion pipeline on a single file:
          01 Notice -> 02 Read & See (Parse + OCR) -> 03 Understand (512-d ONNX Embed) -> 04 Remember (SQLite-vec)
        """
        t_start = time.perf_counter()
        filepath = Path(filepath).resolve()
        if not filepath.exists() or not filepath.is_file():
            return None

        if filepath.name.startswith(".") or "GoodShelves_Archive" in filepath.parts:
            return None

        ext = filepath.suffix.lower()
        all_supported = IMAGE_EXTS | DOC_EXTS | CODE_EXTS | VIDEO_EXTS | AUDIO_EXTS | SPREADSHEET_EXTS
        if ext not in all_supported:
            return None

        stat = filepath.stat()
        if stat.st_size == 0 or (ext in {".txt", ".md", ".log"} and stat.st_size < 16):
            return None

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
                    and abs(float(existing["mtime"]) - stat.st_mtime) < 1e-3
                    and int(existing["size_bytes"]) == stat.st_size
                ):
                    return self.get_file_by_id(existing["id"], conn=conn)

                sha256 = compute_sha256(filepath)
                if existing and not force and existing["sha256"] == sha256:
                    return self.get_file_by_id(existing["id"], conn=conn)

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
                                stat.st_mtime,
                                mc["id"],
                            ),
                        )
                        conn.commit()
                        self._invalidate_cache()
                        return self.get_file_by_id(mc["id"], conn=conn)
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
        parsed = parse_file(
            filepath,
            enable_ocr=settings.get("ocr_enabled", True),
            enable_code=settings.get("code_indexing", True),
            precomputed_sha256=sha256,
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
        suggested_name, rename_reason = generate_rename_suggestion(parsed)
        why_matched = self._build_default_why_matched(parsed)
        folder_p = filepath.parent
        folder_short = format_path_short(folder_p)
        size_human = format_bytes(parsed.size_bytes)
        vec_blob = pack_f32(vec_512)
        meta_json = json.dumps(parsed.metadata or {}, ensure_ascii=False)

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
                        file_id = f"f_{parsed.sha256[:8]}_{abs(hash(str(filepath))) % 100000}"
                    cur = conn.execute(
                        """
                        INSERT INTO files (
                            id, path, name, original_name, suggested_name, rename_reason, renamed,
                            title, ext, modality, folder, folder_short, size_bytes, size_human,
                            mtime, created_date, display_date, sha256, visual_share, text_share,
                            extracted_text, ocr_text, ocr_confidence, why_matched, specimen_type,
                            width, height, page_count, dhash, thumbnail_b64, vector_norm, archived, embedding_blob, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
                conn.commit()
                self._invalidate_cache()
            finally:
                conn.close()

        if recluster:
            self.recluster_all()

        item_dict = self.get_file_by_id(file_id)
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

    def scan_folder(self, folder: Path, max_files: int = 60, recursive: bool = True) -> int:
        """Scan a folder and index supported files."""
        folder = Path(folder).resolve()
        if not folder.exists() or not folder.is_dir():
            return 0

        indexed = 0
        candidates: list[Path] = []
        try:
            iterator = folder.rglob("*") if recursive else folder.iterdir()
            for entry in iterator:
                if entry.is_file() and not any(p.startswith(".") for p in entry.parts):
                    if "GoodShelves_Archive" in entry.parts or "node_modules" in entry.parts or ".venv" in entry.parts:
                        continue
                    ext = entry.suffix.lower()
                    all_supported = IMAGE_EXTS | DOC_EXTS | CODE_EXTS | VIDEO_EXTS | AUDIO_EXTS | SPREADSHEET_EXTS
                    if ext in all_supported:
                        candidates.append(entry)
        except PermissionError:
            pass

        # Prefer the newest files when a large folder exceeds the scan budget.
        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        candidates.sort(key=_mtime, reverse=True)
        batch = candidates[:max_files]
        total = len(batch)
        for idx, fp in enumerate(batch, start=1):
            self.emit_progress("scanning", idx, total, fp.name)
            res = self.index_single_file(fp, force=False, emit_telemetry=False, recluster=False)
            if res is not None:
                indexed += 1
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

                # Cluster unique content only, then let exact copies inherit their representative's
                # shelf. This stops duplicate downloads from skewing cluster shape or labels.
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
                for idx, meta in enumerate(files_meta):
                    rep_idx = representative[str(meta.get("sha256") or meta["id"])]
                    upd = unique_updates.get(str(files_meta[rep_idx]["id"]))
                    if upd:
                        file_updates[str(meta["id"])] = dict(upd)
                self.silhouette_score = sil_score

                pinned_rows = conn.execute("SELECT shelf_id, pinned FROM shelves WHERE pinned = 1").fetchall()
                pinned_by_id = {str(r["shelf_id"]) for r in pinned_rows}
                conn.execute("DELETE FROM shelves")
                for idx, sh in enumerate(shelves):
                    is_pinned = 1 if sh.shelf_id in pinned_by_id else 0
                    conn.execute(
                        """
                        INSERT INTO shelves (shelf_id, cluster_id, num, name, desc, centroid_x, centroid_y, pinned, seq_order)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sh.shelf_id,
                            sh.cluster_id,
                            sh.num,
                            sh.name,
                            sh.desc,
                            sh.centroid_x,
                            sh.centroid_y,
                            is_pinned,
                            idx,
                        ),
                    )

                for fid, upd in file_updates.items():
                    conn.execute(
                        """
                        UPDATE files SET
                            cluster_id = ?, shelf_id = ?, shelf_name = ?, map_x = ?, map_y = ?
                        WHERE id = ?
                        """,
                        (
                            upd["cluster_id"],
                            upd["shelf_id"],
                            upd["shelf_name"],
                            upd["map_x"],
                            upd["map_y"],
                            fid,
                        ),
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
            "clusterId": d["cluster_id"],
            "matchScore": score_val,
            "cosine": f"{cos_val:.3f}",
            "vectorNorm": f"{float(d.get('vector_norm', 1.0)):.4f}",
            "whyMatched": custom_why or d["why_matched"],
            "visualShare": int(d["visual_share"]),
            "textShare": int(d["text_share"]),
            "extractedText": d["extracted_text"] or d["ocr_text"] or "",
            "ocrText": d["ocr_text"] or "",
            "ocrConfidence": float(d.get("ocr_confidence", 0.0)),
            "specimen": d["specimen_type"],
            "width": int(d.get("width", 0)),
            "height": int(d.get("height", 0)),
            "pageCount": int(d.get("page_count", 1)),
            "mapX": float(d.get("map_x", 50.0)),
            "mapY": float(d.get("map_y", 50.0)),
            "thumbnailB64": d.get("thumbnail_b64"),
            "metadata": meta_obj,
            "tags": tags_list,
            "pinned": bool(d.get("pinned", 0)),
        }

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
                lookalikes = find_lookalike_pairs(raw_meta, vectors, resolved_map, threshold=0.92)

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
                for shelf in shelves_list:
                    name = str(shelf["name"]).strip()
                    if shelf["id"] == "tray" or not name:
                        continue
                    if name.lower() not in {s.lower() for s in suggestions}:
                        suggestions.append(name)
                    if len(suggestions) >= 5:
                        break
                if len(suggestions) < 3:
                    for f in files_list:
                        title = re.sub(r"\s+", " ", str(f["title"] or "")).strip(" .-_")
                        if len(title) < 4:
                            continue
                        title = title[:44]
                        if title.lower() not in {s.lower() for s in suggestions}:
                            suggestions.append(title)
                        if len(suggestions) >= 5:
                            break

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

                state = {
                    "files": files_list,
                    "shelves": shelves_list,
                    "lookalikes": lookalikes,
                    "watchedFolders": watched,
                    "connectedDevices": list_connected_devices(watched_paths),
                    "settings": self.get_settings(),
                    "silhouetteScore": self.silhouette_score,
                    "renameHistory": rename_hist,
                    "actionHistory": action_hist,
                    "savedSearches": saved_searches,
                    "querySuggestions": suggestions[:5],
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
        if not clean_q:
            state = self.get_all_state()
            all_files = state["files"]
            if modality_filter != "all":
                items = [f for f in all_files if f["modality"] == modality_filter][:max_k]
            else:
                items = all_files[:max_k]
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)
            return {"results": items, "latencyMs": elapsed, "query": ""}

        concepts, raw_terms, phrase = _query_concepts(clean_q)
        intent = _infer_query_features(clean_q)
        temporal_win = _parse_temporal_window(clean_q)
        visual_subject = _clean_visual_query(clean_q)
        q_vis_128 = self.embedder.encode_visual_query(visual_subject) if visual_subject else None
        now = time.time()

        # Queries that only express time / folder / format (e.g. "recent files")
        # should browse by recency instead of forcing a meaningless embedding match.
        if not concepts:
            state = self.get_all_state()
            items = list(state["files"])
            if modality_filter != "all":
                items = [f for f in items if f["modality"] == modality_filter]
            elif intent["modality"]:
                items = [f for f in items if f["modality"] == intent["modality"]]
            if intent["folder"]:
                items = [f for f in items if intent["folder"] in f["folderFull"].lower()]
            if intent["recency_days"]:
                cutoff = now - intent["recency_days"] * 86400.0
                items = [f for f in items if float(f["mtime"]) >= cutoff]
            items.sort(key=lambda f: float(f["mtime"]), reverse=True)
            results = []
            for idx, f in enumerate(items[:max_k]):
                item = dict(f)
                item["matchScore"] = max(62, 94 - idx * 2)
                item["cosine"] = "1.000"
                item["whyMatched"] = f"Recently modified · {f['date']} · {f['folder']}"
                results.append(item)
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)
            return {"results": results, "latencyMs": elapsed, "query": clean_q}

        q_vec, _ = self.embedder.encode_query(clean_q)
        q_blob = pack_f32(q_vec)

        with self._lock:
            conn = self._connect()
            try:
                total_rows = conn.execute("SELECT COUNT(*) AS c FROM vec_files").fetchone()["c"]
                if total_rows == 0:
                    return {"results": [], "latencyMs": 0.5, "query": clean_q}

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
                for term in raw_terms:
                    for variant in {term, _search_stem(term)}:
                        if len(variant) >= 2 and variant not in lexical_terms:
                            lexical_terms.append(variant)

                lexical_rowids: set[int] = set()
                if lexical_terms:
                    clauses = []
                    params: list[Any] = []
                    for term in lexical_terms[:10]:
                        like = f"%{term}%"
                        clauses.append(
                            "(lower(title) LIKE ? OR lower(name) LIKE ? OR lower(extracted_text) LIKE ? "
                            "OR lower(ocr_text) LIKE ? OR lower(shelf_name) LIKE ?)"
                        )
                        params.extend([like] * 5)
                    lex_rows = conn.execute(
                        f"SELECT rowid FROM files WHERE archived = 0 AND ({' OR '.join(clauses)}) LIMIT 300",
                        params,
                    ).fetchall()
                    lexical_rowids = {int(r["rowid"]) for r in lex_rows}

                candidate_rowids = set(vec_distance) | lexical_rowids
                # When a joint visual-text query is active, also include indexed images so visual matches
                # with generic camera filenames (IMG_1234.jpg) are never missed by text-tower filtering.
                if q_vis_128 is not None and (modality_filter in ("all", "image") or intent["modality"] == "image"):
                    img_rows = conn.execute(
                        "SELECT rowid FROM files WHERE archived = 0 AND modality = 'image' LIMIT 400"
                    ).fetchall()
                    candidate_rowids |= {int(r["rowid"]) for r in img_rows}
                if not candidate_rowids:
                    return {"results": [], "latencyMs": 0.8, "query": clean_q}

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
                    if modality_filter != "all" and f_row["modality"] != modality_filter:
                        continue

                    cos_sim = 0.0
                    vis_sim = 0.0
                    blob = f_row["embedding_blob"]
                    if blob:
                        try:
                            stored = unpack_f32(blob)
                            if stored.shape[0] == q_vec.shape[0]:
                                cos_sim = float(np.clip(np.dot(q_vec, stored), -1.0, 1.0))
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
                    if temporal_win is not None:
                        t_start, t_end, _ = temporal_win
                        if t_start <= file_mtime <= t_end:
                            boost += 0.16
                            temporal_hit = True
                    if intent["folder"] and intent["folder"] in str(f_row["folder"]).lower():
                        boost += 0.045

                    visual_boost = max(0.0, vis_sim) * 0.55 if f_row["modality"] == "image" else 0.0
                    blended = 0.55 * max(0.0, cos_sim) + 0.30 * lex_score + visual_boost + boost
                    if lex_score < 0.12 and cos_sim < min_sim and vis_sim < 0.21:
                        continue

                    if phrase_hit:
                        reason = f"Matched “{phrase}”"
                    elif matched_terms:
                        terms_fmt = ", ".join(f"“{t}”" for t in matched_terms[:4])
                        reason = f"Matched {terms_fmt}"
                    elif vis_sim >= 0.21:
                        reason = f"Visual match for “{visual_subject}” ({vis_sim:.2f})"
                    else:
                        reason = f"Semantic match ({cos_sim:.2f})"
                    if temporal_hit and temporal_win is not None:
                        reason += f" · {temporal_win[2]}"
                    if intent["modality"] and f_row["modality"] == intent["modality"]:
                        reason += f" · {f_row['modality']}"
                    raw_body = f_row["ocr_text"] or f_row["extracted_text"] or f_row["title"] or ""
                    snippet = _extract_snippet_around_terms(raw_body, matched_terms or raw_terms, max_len=96)
                    why = f"{reason} · {snippet}" if snippet else f"{reason} · {f_row['why_matched']}"

                    ui_score = min(99, max(1, int(round(blended * 100))))
                    item_dict = self._row_to_file_dict(
                        f_row, cosine_sim=float(np.clip(blended, 0.0, 0.999)), match_score=ui_score, custom_why=why
                    )
                    ranked.append((blended, item_dict))

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
        archive_dir = Path.home() / "Documents" / "GoodShelves_Archive"
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
                            renamed_flag = 0 if reverse else int(mv.get("renamedAfter", 0))
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
            done = 0
            with self._lock:
                conn = self._connect()
                try:
                    for it in (reversed(items) if reverse else items):
                        src = Path(it["toPath"] if reverse else it["fromPath"])
                        dst = Path(it["fromPath"] if reverse else it["toPath"])
                        if src.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(src), str(dst))
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
            return f"{verb} {done} file(s)"

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

    def complete_setup(self, folders: list[str]) -> dict[str, Any]:
        """Save user's initial watched folder choices and mark first-run setup complete."""
        resolved_set = {str(Path(os.path.expanduser(p)).resolve()) for p in folders if p.strip()}
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
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('setup_completed', 'true') "
                    "ON CONFLICT(key) DO UPDATE SET value = 'true'"
                )
                conn.commit()
                self._invalidate_cache(settings_changed=True)
            finally:
                conn.close()

        total = 0
        for wf in self.get_watched_folders():
            if wf["active"]:
                total += self.scan_folder(Path(wf["path"]), max_files=150, recursive=True)
        self.verify_integrity()
        self.recluster_all()
        return {"scannedCount": total}

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
                dst = src.with_name(str(r["suggested_name"]))
                if src.exists() and not dst.exists():
                    src.rename(dst)
                    with self._lock:
                        conn = self._connect()
                        try:
                            conn.execute(
                                "UPDATE files SET path = ?, name = ?, renamed = 1 WHERE id = ?",
                                (str(dst), dst.name, r["id"]),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                    moves_log.append({
                        "fileId": r["id"],
                        "fromPath": str(src),
                        "toPath": str(dst),
                        "renamedAfter": 1,
                    })
                    renamed_count += 1

        if archive_duplicates:
            archive_dir = Path.home() / "Documents" / "GoodShelves_Archive"
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
                src = Path(m["fromPath"])
                dst_orig = Path(m["toPath"])
                if src.exists():
                    dst_orig.parent.mkdir(parents=True, exist_ok=True)
                    dst = _unique_dest_path(dst_orig.parent, dst_orig.name, src_path=src)
                    shutil.move(str(src), str(dst))
                    with self._lock:
                        conn = self._connect()
                        try:
                            conn.execute(
                                "UPDATE files SET path = ?, folder = ?, folder_short = ? WHERE id = ?",
                                (str(dst), str(dst.parent), format_path_short(dst.parent), m["fileId"]),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                    moves_log.append({
                        "fileId": m["fileId"],
                        "fromPath": str(src),
                        "toPath": str(dst),
                        "renamedAfter": 0,
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
        """Remove a deleted or externally moved file from SQLite and vec_files."""
        target = str(Path(path_str).resolve()) if path_str else ""
        removed = False
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
                    conn.commit()
                    self._invalidate_cache()
                    removed = True
            finally:
                conn.close()
        return {"removed": removed, "path": path_str}

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
            else (Path.home() / "Documents" / "GoodShelves_Imports" / dev_label).resolve()
        )
        dest_root.mkdir(parents=True, exist_ok=True)

        # Ensure destination is in watched_folders so it stays indexed
        self.add_watched_folder(str(dest_root))

        all_supported = IMAGE_EXTS | DOC_EXTS | CODE_EXTS | VIDEO_EXTS | AUDIO_EXTS | SPREADSHEET_EXTS
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
        trash_dir = (Path.home() / "Documents" / "GoodShelves_Trash").resolve()
        trash_dir.mkdir(parents=True, exist_ok=True)
        dst = _unique_dest_path(trash_dir, src.name)
        if src.exists():
            shutil.move(str(src), str(dst))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE files SET path = ?, folder = ?, folder_short = ?, archived = 1 WHERE id = ?",
                    (str(dst), str(dst.parent), format_path_short(dst.parent), file_id),
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
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys_name == "Darwin":
            cmd = ["open", "-R", str(p)] if reveal else ["open", str(p)]
            subprocess.Popen(cmd)
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
            trash_dir = (Path.home() / "Documents" / "GoodShelves_Trash").resolve()
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
                            "UPDATE files SET path = ?, folder = ?, folder_short = ?, archived = 1 WHERE id = ?",
                            (str(dst), str(dst.parent), format_path_short(dst.parent), fid),
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
            with self._lock:
                conn = self._connect()
                try:
                    for fid in clean_ids:
                        conn.execute("UPDATE files SET pinned = ? WHERE id = ?", (val, fid))
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return {"ok": True, "count": len(clean_ids)}

        if action == "tag" and tag:
            clean_tag = tag.strip().lower()
            with self._lock:
                conn = self._connect()
                try:
                    for fid in clean_ids:
                        row = conn.execute("SELECT tags_json FROM files WHERE id = ?", (fid,)).fetchone()
                        if not row:
                            continue
                        cur_tags = json.loads(row["tags_json"] or "[]")
                        if clean_tag not in cur_tags:
                            cur_tags.append(clean_tag)
                        conn.execute("UPDATE files SET tags_json = ? WHERE id = ?", (json.dumps(cur_tags), fid))
                    conn.commit()
                    self._invalidate_cache()
                finally:
                    conn.close()
            return {"ok": True, "count": len(clean_ids)}

        raise ValueError(f"Unsupported bulk action: {action}")

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
        active_id = str(self.get_settings().get("vision_model", "clip-vit-b32"))
        return {
            "activeModel": active_id,
            "models": get_catalog_status(self.embedder.models_dir, active_id),
        }

    def select_vision_model(self, model_id: str, download: bool = True) -> dict[str, Any]:
        """Download (if requested & missing) and activate a local vision-language model."""
        status_list = get_catalog_status(self.embedder.models_dir, model_id)
        target = next((m for m in status_list if m["id"] == model_id), None)
        if target is None:
            raise ValueError(f"Unknown vision model: {model_id}")

        if not target["jointTextReady"] and download:
            self._emit_stage(3, "embed", "running", target["name"], f"downloading {target['name']}…", 0.0)
            download_vision_model(
                self.embedder.models_dir,
                model_id,
                progress_cb=lambda msg: self._emit_stage(3, "embed", "running", target["name"], msg, 0.0),
            )

        self.update_setting("vision_model", model_id)
        self.embedder.set_vision_model(model_id)
        self._emit_stage(3, "embed", "done", target["name"], f"activated {target['name']}", 0.0)
        return self.get_vision_models()

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
