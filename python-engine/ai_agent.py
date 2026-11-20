"""
Local Agentic Planner, Multi-Step Action Resolver, and Behavioral Memory for Good Shelves.
100% offline-first (deterministic + multimodal embeddings + RapidOCR + optional local Ollama).
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from vector_store import VectorStore

_CHAIN_VERBS = (
    r"(?:move|transfer|relocate|copy|duplicate|pin|unpin|tag|label|untag|"
    r"delete|remove|trash|erase|discard|create|make|add|shelve|rename|"
    r"restore|recover|untrash|archive|clean(?:\s+up)?|ocr|run\s+ocr|remember|undo)"
)

_SPLIT_RE = re.compile(
    rf"\s*(?:;\s*|"
    rf",?\s+(?:and\s+then|then|after\s+that|also|plus)\s+|"
    rf",\s*and\s+(?={_CHAIN_VERBS}\b)|"
    rf",\s+(?={_CHAIN_VERBS}\b)|"
    rf"\s+and\s+(?={_CHAIN_VERBS}\b))",
    re.IGNORECASE,
)


def split_compound_commands(prompt: str) -> list[str]:
    """Split a prompt into ordered action clauses without breaking quoted names."""
    text = (prompt or "").strip()
    if not text:
        return []

    # Protect quoted spans temporarily so `"a and b.pdf"` isn't split
    placeholders: dict[str, str] = {}

    def _mask(m: re.Match[str]) -> str:
        key = f"__GSQ{len(placeholders)}__"
        placeholders[key] = m.group(0)
        return key

    masked = re.sub(r'["“\'][^"”\'\n]+["”\']', _mask, text)
    raw_parts = _SPLIT_RE.split(masked)

    parts: list[str] = []
    for raw in raw_parts:
        restored = raw
        for k, v in placeholders.items():
            restored = restored.replace(k, v)
        cleaned = re.sub(
            r"^(?:and\s+then|then|after\s+that|also|plus|and)\s+",
            "",
            restored.strip(),
            flags=re.IGNORECASE,
        ).strip(" ,;")
        if cleaned:
            parts.append(cleaned)
    return parts


def filter_files_by_scope(
    files: list[dict[str, Any]],
    scope_folder: Optional[str],
    recursive: bool = True,
) -> list[dict[str, Any]]:
    """Restrict a file list to files inside `scope_folder` (optionally recursive)."""
    if not scope_folder:
        return files
    try:
        root = Path(scope_folder).expanduser().resolve()
    except Exception:
        return files
    out: list[dict[str, Any]] = []
    for f in files:
        raw_p = str(f.get("path") or "")
        if not raw_p:
            continue
        try:
            fp = Path(raw_p).resolve()
            parent = fp.parent
            if recursive:
                if parent == root or root in fp.parents:
                    out.append(f)
            else:
                if parent == root:
                    out.append(f)
        except Exception:
            continue
    return out


def load_ai_memory(conn: Any, limit: int = 80) -> list[dict[str, Any]]:
    """Load persisted local behavioral and explicit AI memory rows."""
    rows = conn.execute(
        "SELECT id, kind, key_pattern, value, weight, hits, updated_at "
        "FROM ai_memory ORDER BY weight DESC, updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "kind": str(r["kind"]),
            "keyPattern": str(r["key_pattern"]),
            "value": str(r["value"]),
            "weight": float(r["weight"]),
            "hits": int(r["hits"]),
            "updatedAt": float(r["updated_at"]),
        }
        for r in rows
    ]


def upsert_ai_memory(
    conn: Any,
    kind: str,
    key_pattern: str,
    value: str,
    delta_weight: float = 1.0,
) -> None:
    """Insert or reinforce a local behavioral memory entry."""
    clean_key = (key_pattern or "").strip().lower()[:120]
    clean_val = (value or "").strip()[:300]
    if not clean_key or not clean_val:
        return
    now = time.time()
    conn.execute(
        "INSERT INTO ai_memory (kind, key_pattern, value, weight, hits, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?) "
        "ON CONFLICT(kind, key_pattern, value) DO UPDATE SET "
        "weight = MAX(0.1, weight + excluded.weight), hits = hits + 1, updated_at = excluded.updated_at",
        (kind, clean_key, clean_val, max(0.1, delta_weight), now),
    )


def infer_learned_route(
    memory_rows: list[dict[str, Any]],
    target_phrase: str,
    files: list[dict[str, Any]],
) -> Optional[str]:
    """Find the strongest learned destination folder matching the query or files."""
    phrase_low = (target_phrase or "").lower()
    sample_hay = phrase_low + " " + " ".join(
        f"{f.get('name', '')} {f.get('specimen', '')} {f.get('modality', '')} {' '.join(f.get('tags') or [])}".lower()
        for f in files[:8]
    )
    best_path: Optional[str] = None
    best_score = 0.0
    for row in memory_rows:
        if row["kind"] not in {"route", "user_rule"}:
            continue
        pat = row["keyPattern"].lower()
        if pat and pat in sample_hay:
            score = row["weight"] + 0.3 * row["hits"]
            if score > best_score and Path(row["value"]).exists():
                best_score = score
                best_path = row["value"]
    return best_path if best_score >= 1.2 else None


def is_item_excluded_by_memory(memory_rows: list[dict[str, Any]], file_dict: dict[str, Any]) -> bool:
    """Check if the user repeatedly unchecked this extension or folder in past plans."""
    ext = str(file_dict.get("format") or "").lower().lstrip(".")
    folder = str(file_dict.get("folderFull") or file_dict.get("folder") or "").lower()
    for row in memory_rows:
        if row["kind"] != "exclude" or row["weight"] < 2.0:
            continue
        pat = row["keyPattern"].lower()
        if pat.startswith("ext:") and ext == pat[4:]:
            return True
        if pat.startswith("folder:") and pat[7:] and pat[7:] in folder:
            return True
    return False


def build_deep_file_context(
    store: VectorStore,
    citations: list[dict[str, Any]],
    max_files: int = 6,
    chars_per_file: int = 1600,
) -> list[dict[str, Any]]:
    """Fetch full SQLite extracted + OCR text excerpts for top cited files."""
    ids = [str(c.get("fileId") or "") for c in citations[:max_files] if c.get("fileId")]
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    with store._lock:
        conn = store._connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, name, title, ext, folder, size_bytes, extracted_text, ocr_text, metadata_json "
                f"FROM files WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            by_id = {str(r["id"]): dict(r) for r in rows}
        finally:
            conn.close()

    for c in citations[:max_files]:
        fid = str(c.get("fileId") or "")
        row = by_id.get(fid)
        if not row:
            continue
        ocr_t = str(row.get("ocr_text") or "").strip()
        ext_t = str(row.get("extracted_text") or "").strip()
        body = "\n".join(p for p in (ocr_t, ext_t) if p).strip()[:chars_per_file]
        out.append(
            {
                "fileId": fid,
                "name": str(row.get("name") or c.get("fileName") or ""),
                "title": str(row.get("title") or c.get("title") or ""),
                "format": str(row.get("ext") or c.get("format") or ""),
                "folder": str(c.get("folder") or ""),
                "excerpt": body or str(c.get("snippet") or ""),
                "hasOcr": bool(ocr_t),
            }
        )
    return out


def try_ollama_structured_synthesis(
    store: VectorStore,
    question: str,
    deep_contexts: list[dict[str, Any]],
    memory_rows: list[dict[str, Any]],
    scope_folder: Optional[str] = None,
    history: Optional[list[dict[str, str]]] = None,
) -> Optional[str]:
    """Synthesize a grounded Markdown answer via local Ollama using full file contexts & learned memory."""
    settings = store.get_settings()
    if not bool(settings.get("ollama_enabled", False)):
        return None

    scope_line = f"Active folder scope: {scope_folder}\n" if scope_folder else ""
    mem_lines = [
        f"- {m['kind']}: {m['keyPattern']} -> {m['value']}"
        for m in memory_rows[:8]
        if m["kind"] in {"user_rule", "route", "tag_pref"}
    ]
    mem_block = ("Learned user preferences:\n" + "\n".join(mem_lines) + "\n\n") if mem_lines else ""

    ctx_blocks: list[str] = []
    for item in deep_contexts[:5]:
        ctx_blocks.append(
            f"### {item['name']} ({item['format']}, {item['folder']})\n{item['excerpt'][:1100]}"
        )
    ctx_text = "\n\n".join(ctx_blocks)

    hist_lines = []
    for turn in (history or [])[-4:]:
        role_label = "User" if turn.get("role") == "user" else "Assistant"
        txt = str(turn.get("text") or "").strip()[:240]
        if txt:
            hist_lines.append(f"{role_label}: {txt}")
    hist_block = ("Recent conversation:\n" + "\n".join(hist_lines) + "\n\n") if hist_lines else ""

    prompt = (
        "You are Good Shelves, an expert local multimodal file assistant. "
        "Answer accurately in crisp Markdown using ONLY the local file contents below, citing exact filenames. "
        "Do not invent facts not present in the files.\n\n"
        f"{scope_line}{mem_block}{hist_block}"
        f"Question: {question}\n\n"
        f"Local Files:\n{ctx_text}"
    )

    payload = json.dumps(
        {
            "model": str(settings.get("ollama_model") or "llama3.2:1b"),
            "prompt": prompt,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            txt = str(data.get("response") or "").strip()
            return txt or None
    except Exception:
        return None


def make_plan_id() -> str:
    return f"act_{uuid.uuid4().hex[:10]}"
