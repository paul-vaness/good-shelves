"""
Local Ollama Hybrid Brain Router for Good Shelves.

Provides zero-overhead offline degradation (30s circuit breaker when Ollama is offline)
and automatic supercharging when local Ollama is running & enabled in Settings:
  - `/api/tags` model discovery & health caching (60s TTL) with smart model preference ranking
  - `/api/chat` multi-turn calls with `keep_alive: "10m"` and adaptive timeouts (14s planner / 20s synthesis)
  - JSON-schema structured intent/slot/chain planner with strict allowlist validation
  - Grounded RAG synthesis, comparison synthesis, smart batch rename, and general chat
  - Strict Markdown formatting rules (NO GFM pipe tables — crisp headings, bold labels, bullets)
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
_MODEL_CACHE_TTL = 60.0
_CIRCUIT_BREAKER_TTL = 30.0

_PREFERRED_MODEL_PREFIXES = (
    "qwen2.5:7b",
    "qwen2.5:3b",
    "qwen2.5",
    "llama3.2:3b",
    "llama3.1:8b",
    "llama3.2",
    "llama3.1",
    "llama3",
    "gemma2",
    "mistral",
    "phi3",
    "llama3.2:1b",
)

ALLOWED_PLANNER_ACTIONS = frozenset({
    "move",
    "copy",
    "trash",
    "archive_files",
    "restore_trash",
    "restore_archive",
    "tag",
    "untag",
    "auto_tag",
    "rename_tag",
    "clear_tags",
    "pin",
    "unpin",
    "shelve",
    "unshelve",
    "create_shelf",
    "rename_shelf",
    "delete_shelf",
    "pin_shelf",
    "unpin_shelf",
    "create_folder",
    "rename_folder",
    "move_folder",
    "delete_folder",
    "pin_folder",
    "unpin_folder",
    "rename_single",
    "rename_batch",
    "run_ocr",
    "archive_duplicates",
    "swap_lookalike",
    "tidy_folders",
    "smart_clean",
    "reindex_images",
    "rescan_folders",
    "verify_index",
    "file_convert",
    "open_file",
    "reveal_file",
    "save_search",
    "remember",
    "forget_memory",
    "undo",
    "redo",
    "qa",
    "compare",
    "search",
})

_PLANNER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["action", "multi_step", "qa", "compare", "search"]},
        "confidence": {"type": "number"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "actionType": {"type": "string"},
                    "targetQuery": {"type": "string"},
                    "destination": {"type": "string"},
                    "tag": {"type": "string"},
                    "oldTag": {"type": "string"},
                    "newTag": {"type": "string"},
                    "shelfName": {"type": "string"},
                    "newName": {"type": "string"},
                    "template": {"type": "string"},
                    "folderName": {"type": "string"},
                    "keepStrategy": {"type": "string"},
                    "fileAction": {"type": "string"},
                },
                "required": ["actionType"],
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["mode", "steps"],
}


class OllamaRouter:
    """Thread-safe local Ollama client with health caching and circuit breaker."""

    def __init__(self, base_url: str = OLLAMA_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._cached_models: list[str] = []
        self._models_fetched_at: float = 0.0
        self._circuit_open_until: float = 0.0

    def reset(self) -> None:
        """Reset cached health & circuit breaker (useful in tests or settings changes)."""
        with self._lock:
            self._cached_models = []
            self._models_fetched_at = 0.0
            self._circuit_open_until = 0.0

    def is_circuit_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._circuit_open_until

    def _trip_circuit(self) -> None:
        with self._lock:
            self._circuit_open_until = time.monotonic() + _CIRCUIT_BREAKER_TTL

    def discover_models(self, force_refresh: bool = False) -> list[str]:
        """Query local Ollama `/api/tags` (cached 60s) with 30s circuit breaker when offline."""
        now = time.monotonic()
        with self._lock:
            if not force_refresh and now < self._circuit_open_until:
                return []
            if not force_refresh and self._cached_models and (now - self._models_fetched_at) < _MODEL_CACHE_TTL:
                return list(self._cached_models)

        req = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            raw_models = data.get("models") or []
            names = [
                str(m.get("name") or "").strip()
                for m in raw_models
                if isinstance(m, dict) and str(m.get("name") or "").strip()
            ]
            # Exclude pure embedding models from chat selection
            chat_models = [n for n in names if "embed" not in n.lower()]
            chosen = chat_models or names
            with self._lock:
                self._cached_models = chosen
                self._models_fetched_at = time.monotonic()
                self._circuit_open_until = 0.0
            return list(chosen)
        except Exception:
            self._trip_circuit()
            return []

    def resolve_model(self, configured_model: str = "") -> Optional[str]:
        """Pick the best available installed model, preferring configured_model when present."""
        models = self.discover_models()
        if not models:
            return None
        cfg = (configured_model or "").strip()
        if cfg:
            # Exact match or tag-prefix match (e.g. "llama3.2" matches "llama3.2:latest")
            for m in models:
                if m.lower() == cfg.lower() or m.lower().startswith(f"{cfg.lower()}:"):
                    return m
        # Rank by preferred chat model order
        low_map = {m.lower(): m for m in models}
        for pref in _PREFERRED_MODEL_PREFIXES:
            for ml, orig in low_map.items():
                if ml == pref or ml.startswith(f"{pref}:") or ml.startswith(pref):
                    return orig
        return models[0]

    def chat(
        self,
        messages: list[dict[str, str]],
        configured_model: str = "",
        timeout: float = 20.0,
        json_schema: Optional[dict[str, Any]] = None,
        temperature: float = 0.2,
    ) -> Optional[str]:
        """Call Ollama `/api/chat` with `keep_alive: 10m` and structured output support."""
        if self.is_circuit_open():
            return None
        model = self.resolve_model(configured_model)
        if not model:
            return None

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": temperature},
        }
        if json_schema is not None:
            body["format"] = json_schema

        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            msg = data.get("message") or {}
            txt = str(msg.get("content") or data.get("response") or "").strip()
            return txt or None
        except urllib.error.URLError:
            self._trip_circuit()
            return None
        except TimeoutError:
            return None
        except Exception:
            return None


_DEFAULT_ROUTER = OllamaRouter()


def get_ollama_router() -> OllamaRouter:
    return _DEFAULT_ROUTER


def reset_ollama_router() -> None:
    _DEFAULT_ROUTER.reset()


def validate_ollama_plan(raw_plan: Any) -> Optional[dict[str, Any]]:
    """
    Strictly validate an Ollama JSON planner response against `ALLOWED_PLANNER_ACTIONS`.
    Never allows arbitrary paths, shell commands, or unknown action types.
    """
    if not isinstance(raw_plan, dict):
        return None
    mode = str(raw_plan.get("mode") or "").strip().lower()
    if mode not in {"action", "multi_step", "qa", "compare", "search"}:
        return None
    try:
        conf = float(raw_plan.get("confidence", 0.75))
    except (TypeError, ValueError):
        conf = 0.75
    if conf < 0.45:
        return None

    raw_steps = raw_plan.get("steps")
    if not isinstance(raw_steps, list):
        return None

    clean_steps: list[dict[str, str]] = []
    allowed_str_keys = (
        "targetQuery", "destination", "tag", "oldTag", "newTag",
        "shelfName", "newName", "template", "folderName", "keepStrategy", "fileAction",
    )
    for st in raw_steps[:6]:
        if not isinstance(st, dict):
            continue
        atype = str(st.get("actionType") or "").strip().lower()
        if atype not in ALLOWED_PLANNER_ACTIONS:
            continue
        cleaned: dict[str, str] = {"actionType": atype}
        for k in allowed_str_keys:
            v = st.get(k)
            if isinstance(v, str) and v.strip():
                # Strip any traversal / null-byte characters from planner strings
                val = v.strip().replace("\x00", "")[:180]
                if ".." not in val:
                    cleaned[k] = val
        clean_steps.append(cleaned)

    if mode in {"action", "multi_step"} and not clean_steps:
        return None

    return {
        "mode": mode,
        "confidence": round(max(0.0, min(1.0, conf)), 3),
        "steps": clean_steps,
        "reasoning": str(raw_plan.get("reasoning") or "")[:200],
    }


def plan_with_ollama(
    store: Any,
    prompt: str,
    folder_names: list[str],
    shelf_names: list[str],
    history: Optional[list[dict[str, str]]] = None,
) -> Optional[dict[str, Any]]:
    """
    Tier-1 Ollama JSON-schema planner for ambiguous or complex multi-step natural language commands.
    Returns a validated symbolic plan dict, or None when Ollama is disabled/offline/uncertain.
    """
    settings = store.get_settings()
    if not bool(settings.get("ollama_enabled", False)):
        return None

    router = get_ollama_router()
    if router.is_circuit_open():
        return None

    sys_prompt = (
        "You are Good Shelves' symbolic intent planner. Convert the user's request into a strict JSON plan. "
        "Allowed actionType values: move, copy, trash, archive_files, restore_trash, restore_archive, "
        "tag, untag, auto_tag, rename_tag, clear_tags, pin, unpin, shelve, unshelve, create_shelf, "
        "rename_shelf, delete_shelf, pin_shelf, unpin_shelf, create_folder, rename_folder, move_folder, "
        "delete_folder, pin_folder, unpin_folder, rename_single, rename_batch, run_ocr, archive_duplicates, "
        "swap_lookalike, tidy_folders, smart_clean, reindex_images, rescan_folders, verify_index, "
        "file_convert, open_file, reveal_file, save_search, remember, forget_memory, undo, redo, qa, compare, search.\n"
        f"Known folders: {', '.join(folder_names[:20]) or 'none'}.\n"
        f"Known shelves: {', '.join(shelf_names[:15]) or 'none'}.\n"
        "Never invent filesystem paths; emit natural-language targetQuery and destination names only."
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    for turn in (history or [])[-4:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        txt = str(turn.get("text") or "").strip()[:240]
        if txt:
            messages.append({"role": role, "content": txt})
    messages.append({"role": "user", "content": prompt})

    raw = router.chat(
        messages,
        configured_model=str(settings.get("ollama_model") or ""),
        timeout=14.0,
        json_schema=_PLANNER_JSON_SCHEMA,
        temperature=0.0,
    )
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        # Try extracting JSON object if wrapped in code fences
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return None
    return validate_ollama_plan(parsed)


def _strip_pipe_tables(markdown: str) -> str:
    """Safety net: convert accidental GFM pipe tables into crisp bullets (UI doesn't render pipe tables)."""
    if "|" not in markdown:
        return markdown
    lines = markdown.splitlines()
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if re.fullmatch(r"\|?[\s:\-|]+\|?", s) and "-" in s and "|" in s:
            continue
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 3:
            cells = [c.strip() for c in s.strip("|").split("|") if c.strip()]
            if cells:
                out.append("- " + " · ".join(cells))
            continue
        out.append(ln)
    return "\n".join(out)


def synthesize_qa_answer(
    store: Any,
    question: str,
    deep_contexts: list[dict[str, Any]],
    memory_rows: list[dict[str, Any]],
    verified_facts: str = "",
    scope_folder: Optional[str] = None,
    history: Optional[list[dict[str, str]]] = None,
) -> Optional[str]:
    """Synthesize a crisp, grounded Markdown answer via Ollama `/api/chat` (never emits pipe tables)."""
    settings = store.get_settings()
    if not bool(settings.get("ollama_enabled", False)):
        return None

    router = get_ollama_router()
    if router.is_circuit_open():
        return None

    scope_line = f"Active folder scope: {scope_folder}\n" if scope_folder else ""
    mem_lines = [
        f"- {m['kind']}: {m['keyPattern']} -> {m['value']}"
        for m in (memory_rows or [])[:6]
        if m.get("kind") in {"user_rule", "route", "tag_pref"}
    ]
    mem_block = ("Learned user preferences:\n" + "\n".join(mem_lines) + "\n\n") if mem_lines else ""
    facts_block = (f"Verified local numbers/facts (preserve these exactly):\n{verified_facts}\n\n") if verified_facts else ""

    ctx_blocks: list[str] = []
    for item in deep_contexts[:6]:
        ctx_blocks.append(
            f"### {item['name']} ({item.get('format', '')}, {item.get('folder', '')})\n{str(item.get('excerpt', ''))[:1600]}"
        )
    ctx_text = "\n\n".join(ctx_blocks)

    sys_msg = (
        "You are Good Shelves, an expert local multimodal file assistant. "
        "Answer accurately in crisp Markdown using ONLY the local file contents below, citing exact filenames in backticks. "
        "Use headings, bold labels, and bullet lists. NEVER use Markdown pipe tables (| col | col |). "
        "Do not invent facts or numbers not present in the local files."
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": sys_msg}]
    for turn in (history or [])[-4:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        txt = str(turn.get("text") or "").strip()[:240]
        if txt:
            messages.append({"role": role, "content": txt})
    messages.append({
        "role": "user",
        "content": f"{scope_line}{mem_block}{facts_block}Question: {question}\n\nLocal Files:\n{ctx_text}",
    })

    ans = router.chat(
        messages,
        configured_model=str(settings.get("ollama_model") or ""),
        timeout=20.0,
        temperature=0.2,
    )
    return _strip_pipe_tables(ans) if ans else None


def synthesize_comparison(
    store: Any,
    file_a: dict[str, Any],
    file_b: dict[str, Any],
    text_a: str,
    text_b: str,
    structured_summary: str,
) -> Optional[str]:
    """Optional Ollama-enhanced comparison synthesis grounded in exact metadata & full-text excerpts."""
    settings = store.get_settings()
    if not bool(settings.get("ollama_enabled", False)):
        return None
    router = get_ollama_router()
    if router.is_circuit_open():
        return None

    messages = [
        {
            "role": "system",
            "content": (
                "You are Good Shelves. Compare the two local files concisely using headings, bold labels, and bullets. "
                "NEVER use Markdown pipe tables. Preserve all verified metadata numbers and cite exact filenames."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Verified metadata & diff summary:\n{structured_summary}\n\n"
                f"### File A: {file_a.get('name')}\n{text_a[:1800]}\n\n"
                f"### File B: {file_b.get('name')}\n{text_b[:1800]}"
            ),
        },
    ]
    ans = router.chat(
        messages,
        configured_model=str(settings.get("ollama_model") or ""),
        timeout=20.0,
        temperature=0.2,
    )
    return _strip_pipe_tables(ans) if ans else None


def synthesize_smart_renames(
    store: Any,
    files_with_excerpts: list[dict[str, Any]],
) -> dict[str, str]:
    """Optional Ollama batch smart rename generator returning {fileId: clean_filename}."""
    settings = store.get_settings()
    if not bool(settings.get("ollama_enabled", False)) or not files_with_excerpts:
        return {}
    router = get_ollama_router()
    if router.is_circuit_open():
        return {}

    lines = []
    for item in files_with_excerpts[:15]:
        lines.append(
            f"- id={item['id']} | current={item['name']} | date={item.get('date', '')} | excerpt={str(item.get('excerpt', ''))[:260]}"
        )
    schema = {
        "type": "object",
        "properties": {
            "renames": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "newName": {"type": "string"},
                    },
                    "required": ["id", "newName"],
                },
            }
        },
        "required": ["renames"],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Generate clean, descriptive, filesystem-safe filenames in `YYYY-MM-DD_topic-slug.ext` format "
                "preserving each file's original extension. Return strict JSON."
            ),
        },
        {"role": "user", "content": "\n".join(lines)},
    ]
    raw = router.chat(
        messages,
        configured_model=str(settings.get("ollama_model") or ""),
        timeout=14.0,
        json_schema=schema,
        temperature=0.1,
    )
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    out: dict[str, str] = {}
    valid_ids = {str(it["id"]): str(it["name"]) for it in files_with_excerpts}
    for entry in parsed.get("renames") or []:
        if not isinstance(entry, dict):
            continue
        fid = str(entry.get("id") or "")
        nn = str(entry.get("newName") or "").strip()
        if fid in valid_ids and nn:
            safe = re.sub(r'[<>:"/\\|?*]+', "_", nn).strip(" .")
            if safe:
                out[fid] = safe[:96]
    return out


def chat_general(
    store: Any,
    question: str,
    user_name: str = "",
    history: Optional[list[dict[str, str]]] = None,
) -> Optional[str]:
    """General conversational reply via Ollama `/api/chat` when enabled."""
    settings = store.get_settings()
    if not bool(settings.get("ollama_enabled", False)):
        return None
    router = get_ollama_router()
    if router.is_circuit_open():
        return None
    name_note = f" The user's name is {user_name}." if user_name else ""
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": f"You are Good Shelves, a warm, helpful, concise local desktop file manager and AI assistant.{name_note} Reply naturally in 1-3 short sentences.",
        }
    ]
    for turn in (history or [])[-4:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        txt = str(turn.get("text") or "").strip()[:240]
        if txt:
            messages.append({"role": role, "content": txt})
    messages.append({"role": "user", "content": question})
    return router.chat(
        messages,
        configured_model=str(settings.get("ollama_model") or ""),
        timeout=14.0,
        temperature=0.4,
    )
