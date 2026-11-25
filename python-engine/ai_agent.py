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
    r"(?:move|transfer|relocate|put|place|send|shift|copy|duplicate|pin|unpin|"
    r"tag|label|untag|auto[- ]?tag|clear\s+tags?|rename\s+tag|"
    r"delete|remove|trash|erase|discard|empty\s+trash|"
    r"create|make|add|shelve|unshelve|assign|rename|replace|strip|"
    r"restore|recover|untrash|unarchive|archive|clean(?:\s+up)?|organize|sort|tidy(?:\s+up)?|"
    r"ocr|run\s+ocr|reindex|rescan|verify|convert|export|extract|open|reveal|swap|"
    r"save\s+search|remember|forget|undo|redo)"
)

_SPLIT_RE = re.compile(
    rf"\s*(?:;\s*|"
    rf",?\s+(?:and\s+then|then|after\s+that|also|plus)\s+|"
    rf",\s*and\s+(?={_CHAIN_VERBS}\b)|"
    rf",\s+(?={_CHAIN_VERBS}\b)|"
    rf"\s+and\s+(?={_CHAIN_VERBS}\b))",
    re.IGNORECASE,
)

_STEP0_QUERY_RE = re.compile(
    r"^(?:find|show(?:\s+me)?|list|get|grab|pick|select|take|fetch|pull\s+up|search(?:\s+for)?)\s+(.+)$",
    re.IGNORECASE,
)

_ELIDED_OBJECT_VERBS = {
    "pin", "unpin", "trash", "delete", "remove", "archive", "restore",
    "unarchive", "ocr", "run ocr", "auto-tag", "auto tag", "clear tags",
    "unshelve", "rename", "smart rename",
}


def split_compound_commands(prompt: str) -> list[str]:
    """Split a prompt into ordered action clauses without breaking quoted names or {tokens}."""
    text = (prompt or "").strip()
    if not text:
        return []

    # Protect quoted spans and {template_tokens} (including {seq:02d}) temporarily
    placeholders: dict[str, str] = {}

    def _mask(m: re.Match[str]) -> str:
        key = f"__GSQ{len(placeholders)}__"
        placeholders[key] = m.group(0)
        return key

    masked = re.sub(r'["“\'][^"”\'\n]+["”\']|\{[A-Za-z0-9_:]+\}', _mask, text)
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


def extract_step0_query_clause(parts: list[str]) -> tuple[Optional[str], list[str]]:
    """
    Detect Query-as-Step-0 pipelines such as:
      "find all receipts from March, tag them #tax, and move to Finance"
    Returns `(step0_target_query, remaining_action_parts)` if Step 0 is a pure selection
    clause followed by >= 1 mutation clause; otherwise `(None, parts)`.
    """
    if len(parts) < 2:
        return None, parts
    m = _STEP0_QUERY_RE.match(parts[0].strip())
    if not m:
        return None, parts
    # Make sure parts[1] actually starts with a mutation/chain verb
    if not re.match(rf"^{_CHAIN_VERBS}\b", parts[1].strip(), re.IGNORECASE):
        return None, parts
    return m.group(1).strip(), parts[1:]


def propagate_elided_object(clause: str, has_carried_files: bool) -> str:
    """
    Fill in elided pronoun 'them' on follow-up clauses in a compound command:
      - "pin" -> "pin them"
      - "move to Finance" -> "move them to Finance"
      - "tag #tax" -> "tag them #tax"
      - "rename to {date}_{merchant}_{amount}" -> "rename them to {date}_{merchant}_{amount}"
      - "shelve under Tax 2026" -> "shelve them under Tax 2026"
    """
    if not has_carried_files:
        return clause
    c = (clause or "").strip()
    c_low = c.lower()
    if c_low in _ELIDED_OBJECT_VERBS:
        if c_low == "run ocr":
            return "run ocr on them"
        if c_low == "ocr":
            return "run ocr on them"
        if c_low == "clear tags":
            return "clear tags from them"
        return f"{c} them"
    # "move to Finance" / "copy into Archive" / "put in Tax 2026"
    m_mv = re.match(r"^(move|transfer|relocate|copy|duplicate|put|place|send)\s+(to|into|in|onto|under|inside)\s+(.+)$", c, re.I)
    if m_mv:
        return f"{m_mv.group(1)} them {m_mv.group(2)} {m_mv.group(3)}"
    # "move there" / "put there"
    m_th = re.match(r"^(move|put|place|send)\s+(there|into\s+it|to\s+it|in\s+it|where\s+they\s+belong)$", c, re.I)
    if m_th:
        return f"{m_th.group(1)} them {m_th.group(2)}"
    # "tag #tax" / "tag with #tax" / "tag as #tax"
    m_tg = re.match(r"^(tag|label)\s+(?:(?:with|as)\s+)?(#?[A-Za-z0-9_\-,#\s]+)$", c, re.I)
    if m_tg and not re.search(r"\b(?:them|these|those|this|that|it|files?|receipts?|pdfs?|photos?)\b", m_tg.group(2), re.I):
        return f"{m_tg.group(1)} them with {m_tg.group(2)}"
    # "rename to {date}_{title}" / "rename using format ..."
    m_rn = re.match(r"^(?:batch\s+)?rename\s+(to|as|using|with|in|following)\s+(.+)$", c, re.I)
    if m_rn:
        return f"rename them {m_rn.group(1)} {m_rn.group(2)}"
    # "shelve under Tax 2026" / "shelve to Tax 2026"
    m_sh = re.match(r"^shelve\s+(under|in|to|into|onto|on)\s+(.+)$", c, re.I)
    if m_sh:
        return f"shelve them {m_sh.group(1)} {m_sh.group(2)}"
    return c


class VirtualWorkspaceSimulator:
    """
    Lightweight virtual state tracker for multi-step compound plans.
    Simulates renames, folder creations, folder moves, shelf creations, and trashed/archived
    files across steps so downstream steps preview accurate post-previous-step filenames
    and destinations, while merging `unified_items` uniquely per `fileId`.
    """

    def __init__(self, initial_files: list[dict[str, Any]]) -> None:
        self.files_by_id: dict[str, dict[str, Any]] = {str(f["id"]): dict(f) for f in initial_files}
        self.created_folders: dict[str, str] = {}  # lower_name -> full_path
        self.created_shelves: dict[str, str] = {}  # lower_name -> virtual_shelf_id
        self.trashed_ids: set[str] = set()
        self.last_created_folder: str = ""
        self.last_created_folder_name: str = ""
        self.last_created_shelf_name: str = ""

    def active_files(self) -> list[dict[str, Any]]:
        return [f for fid, f in self.files_by_id.items() if fid not in self.trashed_ids]

    def apply_step(self, step_plan: dict[str, Any]) -> None:
        """Advance simulated workspace state according to a planned step."""
        atype = str(step_plan.get("actionType") or "")
        payload = step_plan.get("payload") or {}
        items = step_plan.get("items") or []

        if atype == "create_folder":
            parent_p = str(payload.get("parentPath") or "")
            fname = str(payload.get("folderName") or "")
            if parent_p and fname:
                full_p = str((Path(parent_p) / fname).resolve())
                self.created_folders[fname.lower()] = full_p
                self.last_created_folder = full_p
                self.last_created_folder_name = fname

        elif atype == "create_shelf":
            sname = str(payload.get("shelfName") or "")
            if sname:
                self.created_shelves[sname.lower()] = f"__virtual_shelf__:{sname}"
                self.last_created_shelf_name = sname
                for it in items:
                    fid = str(it.get("fileId") or "")
                    if fid in self.files_by_id:
                        self.files_by_id[fid]["shelfName"] = sname

        elif atype == "rename_single":
            new_n = str(payload.get("newName") or "")
            if new_n and items:
                fid = str(items[0].get("fileId") or "")
                if fid in self.files_by_id:
                    self._simulate_rename(fid, new_n)

        elif atype == "rename_batch":
            rmap = payload.get("renameMap") or {}
            for fid, new_n in rmap.items():
                if str(fid) in self.files_by_id and new_n:
                    self._simulate_rename(str(fid), str(new_n))

        elif atype == "move":
            dst_folder = str(payload.get("targetFolder") or "")
            move_map = payload.get("moveMap") or {}
            for it in items:
                fid = str(it.get("fileId") or "")
                dst = str(move_map.get(fid) or dst_folder)
                if fid in self.files_by_id and dst:
                    self._simulate_move(fid, dst)

        elif atype in {"trash", "delete_permanent", "archive_files", "archive_duplicates"}:
            for it in items:
                fid = str(it.get("fileId") or "")
                if fid:
                    self.trashed_ids.add(fid)

        elif atype == "tag":
            raw_tag = str(payload.get("tag") or "")
            new_tags = [t.strip(" #").lower() for t in re.split(r"[,;]+", raw_tag) if t.strip(" #")]
            for it in items:
                fid = str(it.get("fileId") or "")
                if fid in self.files_by_id:
                    cur = list(self.files_by_id[fid].get("tags") or [])
                    for nt in new_tags:
                        if nt not in cur:
                            cur.append(nt)
                    self.files_by_id[fid]["tags"] = cur

    def _simulate_rename(self, fid: str, new_name: str) -> None:
        f = self.files_by_id[fid]
        cur_p = Path(str(f.get("path") or new_name))
        f["name"] = new_name
        f["path"] = str(cur_p.parent / new_name)
        f["title"] = Path(new_name).stem.replace("_", " ").replace("-", " ").strip() or Path(new_name).stem

    def _simulate_move(self, fid: str, target_folder: str) -> None:
        f = self.files_by_id[fid]
        dst_p = Path(target_folder)
        f["folderFull"] = str(dst_p)
        f["folder"] = dst_p.name
        f["path"] = str(dst_p / str(f.get("name") or ""))


def merge_unique_plan_items(step_plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Aggregate items across multi-step plans while guaranteeing STRICT UNIQUENESS per `fileId`
    (prevents duplicate React keys and checkbox collisions in `FindView.tsx`).
    Files appearing in multiple steps get their per-step details concatenated cleanly.
    """
    ordered_ids: list[str] = []
    merged_by_id: dict[str, dict[str, Any]] = {}
    details_by_id: dict[str, list[str]] = {}

    for sp in step_plans:
        s_idx = int(sp.get("_stepIndex", sp.get("stepIndex", 0)))
        s_title = str(sp.get("title") or sp.get("actionType") or "Step")
        for it in sp.get("items") or []:
            fid = str(it.get("fileId") or "")
            if not fid:
                continue
            step_detail = str(it.get("detail") or s_title).strip()
            tagged_detail = (
                step_detail
                if step_detail.startswith(f"[Step {s_idx + 1}]")
                else f"[Step {s_idx + 1}] {step_detail}"
            )
            if fid not in merged_by_id:
                ordered_ids.append(fid)
                it_copy = dict(it)
                it_copy["stepIndex"] = s_idx
                it_copy["stepTitle"] = s_title
                merged_by_id[fid] = it_copy
                details_by_id[fid] = [tagged_detail]
            else:
                if tagged_detail not in details_by_id[fid]:
                    details_by_id[fid].append(tagged_detail)
                # Keep selectedByDefault False if any destructive step marked it unsafe (e.g. pinned)
                if it.get("selectedByDefault") is False:
                    merged_by_id[fid]["selectedByDefault"] = False

    out: list[dict[str, Any]] = []
    for fid in ordered_ids:
        item = merged_by_id[fid]
        item["detail"] = " → ".join(details_by_id[fid])
        out.append(item)
    return out


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


_MEMORY_HALFLIFE_DAYS = 90.0


def _memory_decay_factor(updated_at: float, now: Optional[float] = None) -> float:
    """90-day exponential half-life decay so stale behavioral habits fade naturally."""
    if not updated_at or updated_at <= 0:
        return 1.0
    t_now = time.time() if now is None else now
    age_days = max(0.0, (t_now - float(updated_at)) / 86400.0)
    return float(0.5 ** (age_days / _MEMORY_HALFLIFE_DAYS))


def load_ai_memory(conn: Any, limit: int = 80) -> list[dict[str, Any]]:
    """Load persisted local behavioral and explicit AI memory rows with 90-day decay."""
    rows = conn.execute(
        "SELECT id, kind, key_pattern, value, weight, hits, updated_at "
        "FROM ai_memory ORDER BY weight DESC, updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    now = time.time()
    entries: list[dict[str, Any]] = []
    for r in rows:
        raw_w = float(r["weight"])
        upd = float(r["updated_at"])
        # Explicit user rules decay much more slowly than implicit behavioral observations
        kind = str(r["kind"])
        eff_w = raw_w if kind == "user_rule" else round(raw_w * _memory_decay_factor(upd, now), 4)
        entries.append({
            "id": int(r["id"]),
            "kind": kind,
            "keyPattern": str(r["key_pattern"]),
            "value": str(r["value"]),
            "weight": raw_w,
            "effectiveWeight": eff_w,
            "hits": int(r["hits"]),
            "updatedAt": upd,
        })
    entries.sort(key=lambda e: (e["effectiveWeight"], e["updatedAt"]), reverse=True)
    return entries


def find_matching_memory_entries(memory_rows: list[dict[str, Any]], phrase: str) -> list[dict[str, Any]]:
    """Find saved AI memory entries matching a user's `forget <rule>` phrase."""
    clean = re.sub(r"^(?:the\s+|that\s+|my\s+)?(?:rule\s+(?:that|about|for)?\s*)?", "", (phrase or "").strip(" \"'“”?!."), flags=re.IGNORECASE).strip().lower()
    if not clean:
        return []
    toks = [t for t in re.findall(r"[a-z0-9_\-]{2,}", clean) if t not in {"go", "goes", "in", "to", "into", "the", "my", "all", "rule", "about"}]
    matches: list[dict[str, Any]] = []
    for row in memory_rows:
        hay = f"{row.get('kind', '')} {row.get('keyPattern', '')} {row.get('value', '')}".lower()
        if clean in hay or (toks and all(t in hay for t in toks[:4])):
            matches.append(row)
    return matches


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
    """Find the strongest learned destination folder matching the query or files (time-decayed)."""
    phrase_low = (target_phrase or "").lower()
    sample_hay = phrase_low + " " + " ".join(
        f"{f.get('name', '')} {f.get('specimen', '')} {f.get('modality', '')} {' '.join(f.get('tags') or [])}".lower()
        for f in files[:8]
    )
    now = time.time()
    best_path: Optional[str] = None
    best_score = 0.0
    for row in memory_rows:
        if row["kind"] not in {"route", "user_rule"}:
            continue
        pat = row["keyPattern"].lower()
        if pat and re.search(rf"\b{re.escape(pat)}\b", sample_hay):
            eff_w = float(row.get("effectiveWeight") if "effectiveWeight" in row else row["weight"] * _memory_decay_factor(float(row.get("updatedAt") or now), now))
            score = eff_w + 0.3 * int(row.get("hits", 1))
            if score > best_score and Path(row["value"]).exists():
                best_score = score
                best_path = row["value"]
    return best_path if best_score >= 1.2 else None


def is_item_excluded_by_memory(memory_rows: list[dict[str, Any]], file_dict: dict[str, Any]) -> bool:
    """Check if the user repeatedly unchecked this semantic category/folder in past plans (time-decayed)."""
    folder = str(file_dict.get("folderFull") or file_dict.get("folder") or "").lower()
    spec = str(file_dict.get("specimen") or "").lower()
    tags_set = {str(t).lower() for t in (file_dict.get("tags") or [])}
    now = time.time()
    for row in memory_rows:
        if row["kind"] != "exclude":
            continue
        eff_w = float(row.get("effectiveWeight") if "effectiveWeight" in row else row["weight"] * _memory_decay_factor(float(row.get("updatedAt") or now), now))
        if eff_w < 2.0:
            continue
        pat = row["keyPattern"].lower()
        if pat.startswith("folder:") and pat[7:] and pat[7:] in folder:
            return True
        if pat.startswith("specimen:") and spec and pat[9:] == spec:
            return True
        if pat.startswith("tag:") and pat[4:] in tags_set:
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
    verified_facts: str = "",
) -> Optional[str]:
    """Synthesize a grounded Markdown answer via `llm_router` (/api/chat + circuit breaker)."""
    from llm_router import synthesize_qa_answer
    return synthesize_qa_answer(
        store=store,
        question=question,
        deep_contexts=deep_contexts,
        memory_rows=memory_rows,
        verified_facts=verified_facts,
        scope_folder=scope_folder,
        history=history,
    )


def make_plan_id() -> str:
    return f"act_{uuid.uuid4().hex[:10]}"


def _format_human_bytes(n: int) -> str:
    val = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            return f"{int(val)} B" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{n} B"


def build_workspace_process_audit(
    state: dict[str, Any],
    scoped_files: list[dict[str, Any]],
    tidy_moves: list[dict[str, Any]],
    scope_folder: Optional[str] = None,
) -> dict[str, Any]:
    """
    Inspect all files in scope and return a complete breakdown of every process
    available in Good Shelves (duplicates, messy renames, shelf folder moves,
    pending OCR, Quiet Tray items, stale Downloads) plus UI progress stages.
    """
    scoped_ids = {str(f.get("id") or "") for f in scoped_files}
    total_files = len(scoped_files)
    total_bytes = sum(int(f.get("sizeBytes") or 0) for f in scoped_files)

    all_lookalikes = list(state.get("lookalikes") or [])
    if scope_folder:
        relevant_pairs = [
            p for p in all_lookalikes
            if (p.get("copy") or {}).get("id") in scoped_ids or (p.get("keep") or {}).get("id") in scoped_ids
        ]
    else:
        relevant_pairs = all_lookalikes

    unresolved_dups = [p for p in relevant_pairs if not p.get("resolved")]
    resolved_dups = [p for p in relevant_pairs if p.get("resolved")]
    dup_reclaim_mb = round(sum(float(p.get("saved_mb") or 0.0) for p in unresolved_dups), 2)

    renames = [f for f in scoped_files if f.get("suggestedName") and not f.get("renamed")]
    scoped_moves = [m for m in tidy_moves if str(m.get("fileId") or "") in scoped_ids]

    ocr_eligible = [
        f for f in scoped_files
        if f.get("modality") == "image" or str(f.get("format") or "").upper() == "PDF"
    ]
    ocr_pending = [f for f in ocr_eligible if not str(f.get("ocrText") or "").strip()]
    quiet_tray = [
        f for f in scoped_files
        if str(f.get("shelfId") or "") in {"", "tray", "uncategorized"}
    ]
    now = time.time()
    stale_downloads = [
        f for f in scoped_files
        if "download" in str(f.get("folderFull") or "").lower() and (now - float(f.get("mtime") or now)) > 30 * 86400
    ]

    def _pct(done_cnt: int, tot_cnt: int) -> int:
        if tot_cnt <= 0:
            return 100
        return max(0, min(100, int(round((done_cnt / float(tot_cnt)) * 100.0))))

    dup_total = len(relevant_pairs)
    dup_done = len(resolved_dups)
    dup_pending = len(unresolved_dups)

    ren_done = max(0, total_files - len(renames))
    mv_done = max(0, total_files - len(scoped_moves))
    ocr_tot = len(ocr_eligible)
    ocr_done = max(0, ocr_tot - len(ocr_pending))

    process_summary: list[dict[str, Any]] = [
        {
            "key": "duplicates",
            "label": "Duplicates & lookalikes",
            "pending": dup_pending,
            "done": dup_done,
            "total": dup_total,
            "percent": 100 if dup_pending == 0 else _pct(dup_done, max(1, dup_total)),
            "detail": (
                f"{dup_pending} pair(s) ready · {dup_reclaim_mb} MB reclaimable"
                if dup_pending > 0
                else "No unresolved duplicates"
            ),
            "actionPrompt": "show me all the duplicates" if dup_pending > 0 else "",
        },
        {
            "key": "renames",
            "label": "Filename cleanup",
            "pending": len(renames),
            "done": ren_done,
            "total": total_files,
            "percent": _pct(ren_done, total_files),
            "detail": (
                f"{len(renames)} unclear filename(s) ready to rename"
                if renames
                else "All filenames are already descriptive"
            ),
            "actionPrompt": "rename messy files" if renames else "",
        },
        {
            "key": "folders",
            "label": "Shelf folder organization",
            "pending": len(scoped_moves),
            "done": mv_done,
            "total": total_files,
            "percent": _pct(mv_done, total_files),
            "detail": (
                f"{len(scoped_moves)} file(s) ready to organize into ~/Shelves/"
                if scoped_moves
                else "All shelf items are organized"
            ),
            "actionPrompt": "organize files into shelf folders" if scoped_moves else "",
        },
        {
            "key": "ocr",
            "label": "OCR text extraction",
            "pending": len(ocr_pending),
            "done": ocr_done,
            "total": ocr_tot,
            "percent": _pct(ocr_done, ocr_tot),
            "detail": (
                f"{len(ocr_pending)} image/PDF file(s) missing OCR text"
                if ocr_pending
                else (f"All {ocr_tot} image/PDF file(s) OCR-indexed" if ocr_tot else "No image/PDF OCR targets")
            ),
            "actionPrompt": "run ocr" if ocr_pending else "",
        },
    ]

    total_pending = dup_pending + len(renames) + len(scoped_moves) + len(ocr_pending)
    return {
        "totalFiles": total_files,
        "totalBytes": total_bytes,
        "totalHuman": _format_human_bytes(total_bytes),
        "dupPairs": unresolved_dups,
        "dupReclaimMB": dup_reclaim_mb,
        "renames": renames,
        "tidyMoves": scoped_moves,
        "ocrPending": ocr_pending,
        "quietTray": quiet_tray,
        "staleDownloads": stale_downloads,
        "totalPending": total_pending,
        "processSummary": process_summary,
    }


def format_process_audit_markdown(
    audit: dict[str, Any],
    name_prefix: str = "",
    scope_label: Optional[str] = None,
) -> str:
    """Format a clear Markdown inventory of everything available to process first."""
    total_files = int(audit["totalFiles"])
    scope_note = f" in **{scope_label}**" if scope_label else ""
    lines: list[str] = [
        f"{name_prefix}here is the full system readiness report{scope_note} across **{total_files} indexed file(s)** ({audit['totalHuman']}):\n"
    ]

    dups = audit["dupPairs"]
    renames = audit["renames"]
    moves = audit["tidyMoves"]
    ocr_pending = audit["ocrPending"]
    quiet_tray = audit["quietTray"]
    stale = audit["staleDownloads"]

    lines.append(f"**1. Duplicates & Lookalikes:** {len(dups)} pair(s) ({audit['dupReclaimMB']} MB reclaimable)")
    if dups:
        for idx, p in enumerate(dups[:8], start=1):
            keep_n = (p.get("keep") or {}).get("name", "original")
            copy_n = (p.get("copy") or {}).get("name", "copy")
            lines.append(f"- {idx}. Copy `{copy_n}` ↔ Keep `{keep_n}` ({p.get('similarity_pct', 100)}% · {p.get('saved_mb', 0)} MB)")
        if len(dups) > 8:
            lines.append(f"- *…plus {len(dups) - 8} more pair(s) — ask `show me all the duplicates` to review every pair.*")

    lines.append(f"\n**2. Cryptic / Messy Filenames:** {len(renames)} file(s) ready for smart rename")
    if renames:
        for f in renames[:6]:
            lines.append(f"- `{f['name']}` → `{f['suggestedName']}`" + (f" *({f['renameReason']})*" if f.get("renameReason") else ""))
        if len(renames) > 6:
            lines.append(f"- *…plus {len(renames) - 6} more rename candidate(s).*")

    lines.append(f"\n**3. Shelf Folder Organization (`~/Shelves/`):** {len(moves)} file(s) ready to organize")
    if moves:
        for mv in moves[:6]:
            lines.append(f"- `{mv['fromShort']}` → `{mv['toShort']}`")
        if len(moves) > 6:
            lines.append(f"- *…plus {len(moves) - 6} more file move(s).*")

    lines.append(f"\n**4. OCR Text Extraction:** {len(ocr_pending)} image/PDF file(s) without OCR text")
    if ocr_pending:
        sample_ocr = ", ".join(f"`{f['name']}`" for f in ocr_pending[:5])
        more_ocr = f" (+{len(ocr_pending) - 5} more)" if len(ocr_pending) > 5 else ""
        lines.append(f"- Pending: {sample_ocr}{more_ocr}")

    if quiet_tray or stale:
        extras = []
        if quiet_tray:
            extras.append(f"{len(quiet_tray)} item(s) in Quiet Tray")
        if stale:
            extras.append(f"{len(stale)} stale Download(s) (>30d)")
        lines.append(f"\n**5. Additional Items:** {' · '.join(extras)}")

    if audit["totalPending"] == 0:
        lines.append("\nEverything looks clean! You can also batch-rename any files using a custom format (e.g. `rename all receipts to {date}_{merchant}_{amount}`), list everything, or chain custom actions.")
    else:
        lines.append("\nPick any step below (or choose **Run full organize plan** to preview everything in a single checklist before applying).")

    return "\n".join(lines)


_NATURAL_TOKEN_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\{(?:orig|name|filename)\}", re.I), "{original}"),
    (re.compile(r"\{(?:num|index|n)\}", re.I), "{seq}"),
    (re.compile(r"\{(?:type|ext)\}", re.I), "{format}"),
    (re.compile(r"\{(?:vendor|store)\}", re.I), "{merchant}"),
    (re.compile(r"\{(?:total|price)\}", re.I), "{amount}"),
    (re.compile(r"\bYYYY[-_]?MM[-_]?DD\b", re.I), "{date}"),
    (re.compile(r"\bYYYY[-_]?MM\b", re.I), "{month}"),
    (re.compile(r"\bYYYY\b", re.I), "{year}"),
)


def _normalize_natural_template(raw_spec: str) -> str:
    """Convert either explicit `{date}_{title}` or natural `date_merchant_seq` into a template."""
    spec = raw_spec.strip(" \"'“”`?!.")
    had_explicit_braces = "{" in spec and "}" in spec
    for pat, repl in _NATURAL_TOKEN_ALIASES:
        spec = pat.sub(repl, spec)

    # If user already used `{...}` placeholders, preserve separators & literals
    if had_explicit_braces:
        return spec

    # Otherwise map bare format words like "date_merchant_amount" or "shelf - title - seq"
    token_words = {
        "date": "{date}",
        "year": "{year}",
        "month": "{month}",
        "day": "{day}",
        "title": "{title}",
        "topic": "{title}",
        "original": "{original}",
        "orig": "{original}",
        "name": "{original}",
        "shelf": "{shelf}",
        "subshelf": "{subshelf}",
        "category": "{shelf}",
        "folder": "{folder}",
        "parent": "{parent}",
        "merchant": "{merchant}",
        "store": "{merchant}",
        "vendor": "{merchant}",
        "amount": "{amount}",
        "total": "{amount}",
        "price": "{amount}",
        "currency": "{currency}",
        "invoice": "{invoice}",
        "doctype": "{doctype}",
        "camera": "{camera}",
        "resolution": "{resolution}",
        "tag": "{tag}",
        "seq": "{seq}",
        "num": "{seq}",
        "number": "{seq}",
        "index": "{seq}",
        "format": "{format}",
        "type": "{format}",
    }

    def _sub_word(m: re.Match[str]) -> str:
        w = m.group(0).lower()
        if w in {"{date}", "{month}", "{year}"}:
            return w
        return token_words.get(w, m.group(0))

    converted = re.sub(r"\{[A-Za-z0-9_:]+\}|[A-Za-z]+", _sub_word, spec)
    converted = re.sub(r"\}\s*(?:,\s*and\s+|\s+and\s+|,\s*|\s+)\s*\{", "}_{", converted, flags=re.IGNORECASE)
    if "{" in converted:
        return converted
    return ""


def parse_rename_format_request(q_stripped: str) -> Optional[dict[str, str]]:
    """
    Detect custom-format batch rename & transform requests:
      - Custom token templates: "rename all receipts to {date}_{merchant}_{amount}",
        "rename photos using format Vacation_{seq:02d}"
      - Prefix / Suffix / Strip: "add prefix Tax_ to receipts", "strip prefix 'Copy of ' from ...",
        "strip dates from ..."
      - Find-and-replace & Regex: "replace 'IMG_' with 'Trip_' in Downloads",
        "regex replace '_\\d+$' with '' in Downloads"
      - Case & separator transforms: lowercase, UPPERCASE, Title Case, snake_case, kebab-case, camelCase, underscores.
    Returns None if this looks like a single-file literal rename ("rename foo.txt to bar.txt").
    """
    q = (q_stripped or "").strip()
    if not q:
        return None

    # 1. Prefix / Suffix / Strip helpers
    pref_m = re.match(
        r"^(?:add|prepend)\s+(?:the\s+)?prefix\s+[\"“']?([^\"”'\s]+)[\"”']?\s+(?:to|on|for)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if pref_m:
        pref = pref_m.group(1).strip()
        return {"target": pref_m.group(2).strip(), "template": f"__PREFIX__:{pref}", "label": f"prefix `{pref}`"}

    suf_m = re.match(
        r"^(?:add|append)\s+(?:the\s+)?suffix\s+[\"“']?([^\"”'\s]+)[\"”']?\s+(?:to|on|for)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if suf_m:
        suf = suf_m.group(1).strip()
        return {"target": suf_m.group(2).strip(), "template": f"__SUFFIX__:{suf}", "label": f"suffix `{suf}`"}

    strip_pref_m = re.match(
        r"^(?:strip|remove)\s+(?:the\s+)?prefix\s+[\"“']([^\"”']+)[\"”']\s+(?:from|in|on|for)\s+(.+)$|"
        r"^(?:strip|remove)\s+(?:the\s+)?prefix\s+([^\s]+)\s+(?:from|in|on|for)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if strip_pref_m:
        pref = strip_pref_m.group(1) if strip_pref_m.group(1) is not None else (strip_pref_m.group(3) or "").strip()
        tgt = (strip_pref_m.group(2) or strip_pref_m.group(4) or "").strip()
        return {"target": tgt, "template": f"__STRIP_PREFIX__:{pref}", "label": f"strip prefix `{pref}`"}

    strip_date_m = re.match(
        r"^(?:strip|remove)\s+(?:leading\s+)?dates?\s+(?:from|in|on|for)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if strip_date_m:
        return {"target": strip_date_m.group(1).strip(), "template": "__STRIP_DATE__", "label": "strip dates"}

    # 2. Regex replace & Find-and-replace
    rx_m = re.match(
        r"^(?:regex|regexp)\s+replace\s+[\"“']([^\"”']+)[\"”']\s+with\s+[\"“']([^\"”']*)[\"”']\s+(?:in|on|for|across)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if rx_m:
        pat_s, repl_s, tgt = rx_m.group(1), rx_m.group(2), rx_m.group(3).strip()
        return {"target": tgt, "template": f"__REGEX__:{pat_s}\x1f{repl_s}", "label": f"regex `{pat_s}` → `{repl_s}`"}

    us_m = re.match(
        r"^(?:replace\s+spaces\s+with\s+underscores|use\s+underscores)\s+(?:in|for|on)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if us_m:
        return {"target": us_m.group(1).strip(), "template": "__UNDERSCORE__", "label": "underscores"}

    dash_m = re.match(
        r"^replace\s+spaces\s+with\s+(?:dashes|hyphens)\s+(?:in|for|on)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if dash_m:
        return {"target": dash_m.group(1).strip(), "template": "__KEBAB__", "label": "kebab-case"}

    rep_m = re.match(
        r"^(?:rename\s+by\s+replacing|replace)\s+[\"“']([^\"”']+)[\"”']\s+with\s+[\"“']([^\"”']*)[\"”']\s+(?:in|on|for|across)\s+(.+)$|"
        r"^replace\s+([^\s\"'“”]+)\s+with\s+([^\s\"'“”]+)\s+(?:in|on|for|across)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if rep_m:
        old_s = rep_m.group(1) if rep_m.group(1) is not None else rep_m.group(4)
        new_s = rep_m.group(2) if rep_m.group(1) is not None else rep_m.group(5)
        tgt = (rep_m.group(3) if rep_m.group(1) is not None else rep_m.group(6) or "").strip()
        if old_s and tgt:
            return {"target": tgt, "template": f"__REPLACE__:{old_s}\x1f{new_s}", "label": f"replace `{old_s}` → `{new_s}`"}

    # 3. Case & style transforms (lowercase, UPPERCASE, Title Case, snake_case, kebab-case, camelCase)
    case_patterns: tuple[tuple[str, str, str], ...] = (
        (r"(?:all\s+)?lowercase", "__LOWERCASE__", "lowercase"),
        (r"(?:all\s+)?uppercase", "__UPPERCASE__", "UPPERCASE"),
        (r"title[\s\-]?case", "__TITLECASE__", "Title Case"),
        (r"snake[\s_\-]?case", "__SNAKE__", "snake_case"),
        (r"kebab[\s\-]?case", "__KEBAB__", "kebab-case"),
        (r"camel[\s\-]?case", "__CAMEL__", "camelCase"),
    )
    for pat_txt, tpl_code, lbl in case_patterns:
        cm = re.match(
            rf"^(?:rename|convert|change|make)\s+(.+?)\s+(?:to|into|in)\s+{pat_txt}(?:\s+names?)?$",
            q,
            flags=re.IGNORECASE,
        ) or re.match(
            rf"^{pat_txt}\s+(?:filenames?\s+(?:of|for|in)\s+)?(.+)$",
            q,
            flags=re.IGNORECASE,
        )
        if cm:
            return {"target": cm.group(1).strip(), "template": tpl_code, "label": lbl}

    # 4. Explicit format / pattern / template phrasing or {placeholder} syntax
    fmt_m = re.match(
        r"^(?:batch\s+)?rename\s+(.+?)\s+(?:using|with|in|on|to|as|by|following)\s+"
        r"(?:(?:this|that|the|a|an)\s+)?(?:(?:specific|custom|new)\s+)?(?:format|pattern|template|style)\s*(?:of|:)?\s*(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if fmt_m:
        target = fmt_m.group(1).strip()
        raw_spec = fmt_m.group(2).strip()
        tpl = _normalize_natural_template(raw_spec)
        if not tpl:
            cleaned_lit = re.sub(r'[<>:"/\\|?*]+', "_", raw_spec.strip(" \"'“”`?!."))
            if cleaned_lit:
                tpl = f"{cleaned_lit}_{{seq}}"
        if tpl:
            return {"target": target, "template": tpl, "label": tpl}

    # 5. `rename <target> to/as/by/with <spec>` when spec contains `{...}` or natural token sequences
    to_m = re.match(
        r"^(?:batch\s+)?rename\s+(.+?)\s+(?:to|as|into|by|with)\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if to_m:
        target = to_m.group(1).strip()
        raw_spec = to_m.group(2).strip()
        has_braces = "{" in raw_spec and "}" in raw_spec
        has_natural_pattern = bool(re.search(r"\b(?:YYYY[-_]?MM[-_]?DD|YYYY[-_]?MM|seq|merchant|amount)\b", raw_spec, re.I))
        token_hits = re.findall(
            r"\b(?:date|year|month|day|title|topic|original|orig|name|shelf|subshelf|category|folder|parent|merchant|store|vendor|amount|total|price|currency|invoice|doctype|camera|resolution|tag|seq|num|number|index|format|type)\b",
            raw_spec,
            flags=re.IGNORECASE,
        )
        non_connector_words = [
            w for w in re.findall(r"[A-Za-z]+", raw_spec)
            if w.lower() not in {"and", "the", "a", "an"}
        ]
        has_multi_token_phrase = len(token_hits) >= 2 and len(token_hits) == len(non_connector_words)
        if has_braces or has_natural_pattern or has_multi_token_phrase:
            tpl = _normalize_natural_template(raw_spec)
            if tpl:
                return {"target": target, "template": tpl, "label": tpl}

    return None


# ============================================================================
# Deep Title-Standing & Conversational Context Resolution Engine
# ============================================================================

_TITLE_STOPWORDS = {
    "a", "an", "the", "my", "this", "that", "these", "those", "file", "files",
    "item", "items", "doc", "docs", "document", "documents", "picture", "pictures",
    "photo", "photos", "image", "images", "pdf", "pdfs", "named", "called",
    "titled", "about", "for", "of", "in", "on", "to", "from", "with", "and",
}

_DEICTIC_FILE_SINGULAR = {
    "this", "that", "it", "one", "this one", "that one", "the file", "this file",
    "that file", "this item", "that item", "this doc", "that doc", "this document",
    "that document", "this pdf", "that pdf", "this picture", "that picture",
    "this photo", "that photo", "this image", "that image", "this receipt",
    "that receipt", "this thing", "that thing", "this shit", "that shit",
    "selected", "the selected file", "current", "current file",
}

_DEICTIC_FILE_PLURAL = {
    "these", "those", "them", "these ones", "those ones", "these files",
    "those files", "these items", "those items", "these things", "those things",
    "this stuff", "that stuff", "all of them", "all of these", "all of those",
    "both", "both of them",
}

_DEICTIC_FOLDER_WORDS = {
    "that", "this", "it", "there", "in there", "into there", "over there",
    "here", "in here", "that folder", "this folder", "the folder",
    "other folder", "the other folder", "that one", "the other one",
    "where it belongs", "where they belong", "the right folder",
    "right folder", "proper folder", "the proper folder", "correct folder",
    "its shelf", "their shelf", "its folder", "their folder", "the shelf",
    "that shelf", "this shelf",
}

_SHELF_TOPIC_FOLDER_ALIASES: dict[str, str] = {
    "documents": "documents-and-reports",
    "document": "documents-and-reports",
    "docs": "documents-and-reports",
    "doc": "documents-and-reports",
    "reports": "documents-and-reports",
    "report": "documents-and-reports",
    "papers": "documents-and-reports",
    "paper": "documents-and-reports",
    "notes": "documents-and-reports",
    "transcripts": "documents-and-reports",
    "transcript": "documents-and-reports",
    "documents & reports": "documents-and-reports",
    "documents and reports": "documents-and-reports",
    "receipts": "receipts-and-finance",
    "receipt": "receipts-and-finance",
    "finance": "receipts-and-finance",
    "financial": "receipts-and-finance",
    "bills": "receipts-and-finance",
    "invoices": "receipts-and-finance",
    "expenses": "receipts-and-finance",
    "budget": "receipts-and-finance",
    "receipts & finance": "receipts-and-finance",
    "receipts and finance": "receipts-and-finance",
    "code": "code-and-data",
    "data": "code-and-data",
    "scripts": "code-and-data",
    "script": "code-and-data",
    "dev": "code-and-data",
    "programming": "code-and-data",
    "datasets": "code-and-data",
    "spreadsheets": "code-and-data",
    "code & data": "code-and-data",
    "code and data": "code-and-data",
    "photos": "photos-and-places",
    "photo": "photos-and-places",
    "pictures": "photos-and-places",
    "picture": "photos-and-places",
    "images": "photos-and-places",
    "places": "photos-and-places",
    "travel": "photos-and-places",
    "photos & places": "photos-and-places",
    "photos and places": "photos-and-places",
    "people": "photos-and-people",
    "portraits": "photos-and-people",
    "portrait": "photos-and-people",
    "selfies": "photos-and-people",
    "photos & people": "photos-and-people",
    "photos and people": "photos-and-people",
    "diagrams": "diagrams-and-design",
    "diagram": "diagrams-and-design",
    "design": "diagrams-and-design",
    "charts": "diagrams-and-design",
    "drawings": "diagrams-and-design",
    "cad": "diagrams-and-design",
    "diagrams & design": "diagrams-and-design",
    "diagrams and design": "diagrams-and-design",
    "media": "media-and-archives",
    "archives": "media-and-archives",
    "audio": "media-and-archives",
    "video": "media-and-archives",
    "videos": "media-and-archives",
    "media & archives": "media-and-archives",
    "media and archives": "media-and-archives",
    "ids": "ids-and-official-forms",
    "id": "ids-and-official-forms",
    "forms": "ids-and-official-forms",
    "official": "ids-and-official-forms",
    "ids & official forms": "ids-and-official-forms",
    "ids and official forms": "ids-and-official-forms",
    "screenshots": "screenshots-and-captures",
    "screenshot": "screenshots-and-captures",
    "captures": "screenshots-and-captures",
    "screens": "screenshots-and-captures",
    "screenshots & captures": "screenshots-and-captures",
    "screenshots and captures": "screenshots-and-captures",
}


def is_deictic_folder_phrase(phrase: str) -> bool:
    clean = re.sub(r"\s+", " ", (phrase or "").strip(" \"'“”`?!.,").lower())
    return clean in _DEICTIC_FOLDER_WORDS


def is_deictic_file_phrase(phrase: str) -> bool:
    clean = re.sub(r"\s+", " ", (phrase or "").strip(" \"'“”`?!.,").lower())
    return clean in _DEICTIC_FILE_SINGULAR or clean in _DEICTIC_FILE_PLURAL


def _norm_title_str(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _token_fuzzy_hit(q_tok: str, cand_toks: list[str]) -> float:
    """Return 1.0 for exact token hit, 0.85 for prefix/stem hit, 0.75 for 1-edit typo hit, else 0.0."""
    if q_tok in cand_toks:
        return 1.0
    q_stem = q_tok[:-1] if len(q_tok) > 4 and q_tok.endswith("s") else q_tok
    for ct in cand_toks:
        ct_stem = ct[:-1] if len(ct) > 4 and ct.endswith("s") else ct
        if q_stem == ct_stem:
            return 0.95
        if len(q_tok) >= 4 and (ct.startswith(q_tok) or q_tok.startswith(ct)):
            return 0.85
        # 1-char edit distance for 5+ char words (handles typos like "transcrpt", "intervew")
        if len(q_tok) >= 5 and abs(len(q_tok) - len(ct)) <= 1:
            diffs = 0
            if len(q_tok) == len(ct):
                diffs = sum(1 for a, b in zip(q_tok, ct) if a != b)
            else:
                longer, shorter = (q_tok, ct) if len(q_tok) > len(ct) else (ct, q_tok)
                i = j = 0
                while i < len(longer) and j < len(shorter):
                    if longer[i] == shorter[j]:
                        i += 1
                        j += 1
                    else:
                        diffs += 1
                        i += 1
                        if diffs > 1:
                            break
            if diffs <= 1:
                return 0.75
    return 0.0


def match_files_by_title_priority(
    target_phrase: str,
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Stand firmly on file titles and filenames before falling back to body text.
    Tier 1: Exact normalized title, primary title (before '·'), filename, or date-stripped stem.
    Tier 2: Contiguous phrase match in title/stem OR 100% of query content tokens present in title/stem.
    Tier 3: High-coverage title match (>= 75% of query tokens in title/stem for multi-word queries).
    """
    raw = (target_phrase or "").strip(" \"'“”`?!.,")
    if not raw or is_deictic_file_phrase(raw):
        return []

    # Strip leading conversational fillers ("the file called ...", "my doc named ...")
    cleaned_q = re.sub(
        r"^(?:all\s+(?:the\s+)?|every\s+|any\s+|the\s+|my\s+|a\s+|an\s+|this\s+|that\s+)?"
        r"(?:(?:file|files|item|items|doc|docs|document|documents|pdf|pdfs|picture|pictures|photo|photos|image|images)\s+)?"
        r"(?:(?:named|called|titled|for|about)\s+)?",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip(" \"'“”`?!.,")
    q_norm = _norm_title_str(cleaned_q or raw)
    if not q_norm or len(q_norm) < 2:
        return []

    q_toks = [t for t in q_norm.split() if len(t) >= 2 and t not in _TITLE_STOPWORDS]
    if not q_toks:
        q_toks = [t for t in q_norm.split() if t]
    if not q_toks:
        return []

    want_all_explicit = bool(re.match(r"^(?:all|every)\b", raw.lower()))

    tier1: list[tuple[float, dict[str, Any]]] = []
    tier2: list[tuple[float, dict[str, Any]]] = []
    tier3: list[tuple[float, dict[str, Any]]] = []

    for f in files:
        name_raw = str(f.get("name") or "")
        name_low = name_raw.lower()
        stem_raw = Path(name_raw).stem.lower()
        stem_no_date = re.sub(r"^(?:19|20)\d{2}-\d{2}-\d{2}[_-]+", "", stem_raw)
        stem_norm = _norm_title_str(stem_no_date)
        full_stem_norm = _norm_title_str(stem_raw)

        title_raw = str(f.get("title") or "")
        title_norm = _norm_title_str(title_raw)
        primary_title = _norm_title_str(title_raw.split("·")[0])

        # Tier 1: Exact match against title, primary title segment, filename, or stem
        if (
            raw.lower() == name_low
            or cleaned_q.lower() == name_low
            or q_norm in {title_norm, primary_title, stem_norm, full_stem_norm}
        ):
            exact_bonus = 100.0 if q_norm == title_norm or raw.lower() == name_low else 95.0
            tier1.append((exact_bonus, f))
            continue

        title_toks = list(dict.fromkeys((title_norm + " " + stem_norm).split()))
        if not title_toks:
            continue

        # Check token hits against title + stem
        tok_scores = [_token_fuzzy_hit(qt, title_toks) for qt in q_toks]
        matched_cnt = sum(1 for s in tok_scores if s > 0)
        coverage = sum(tok_scores) / float(len(q_toks))
        phrase_in_title = len(q_norm) >= 4 and (q_norm in title_norm or q_norm in stem_norm)
        primary_toks = primary_title.split() or title_toks
        title_density = matched_cnt / float(max(1, len(primary_toks)))

        if phrase_in_title or matched_cnt == len(q_toks):
            score = (
                50.0
                + (25.0 if phrase_in_title else 0.0)
                + 15.0 * coverage
                + 12.0 * min(1.0, title_density)
            )
            tier2.append((score, f))
        elif len(q_toks) >= 2 and coverage >= 0.72:
            score = 25.0 + 15.0 * coverage + 8.0 * min(1.0, title_density)
            tier3.append((score, f))

    if tier1:
        tier1.sort(key=lambda x: (-x[0], -float(x[1].get("mtime") or 0)))
        return [f for _, f in tier1]

    if tier2:
        tier2.sort(key=lambda x: (-x[0], -float(x[1].get("mtime") or 0)))
        # If user named a specific title (not "all ...") and the top hit is a much tighter title match, stand on it!
        if not want_all_explicit and len(tier2) > 1:
            top_score = tier2[0][0]
            tight = [f for sc, f in tier2 if sc >= top_score - 6.0]
            if len(tight) <= 3:
                return tight
        return [f for _, f in tier2]

    if tier3:
        tier3.sort(key=lambda x: (-x[0], -float(x[1].get("mtime") or 0)))
        return [f for _, f in tier3[:10]]

    return []


_ORDINAL_MAP: dict[str, int] = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "other": 1,
    "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4, "5th": 4,
    "sixth": 5, "6th": 5, "seventh": 6, "7th": 6, "eighth": 7, "8th": 7,
    "ninth": 8, "9th": 8, "tenth": 9, "10th": 9, "last": -1,
}


def resolve_anaphora_subset(
    phrase: str,
    context_files: list[dict[str, Any]],
) -> Optional[list[dict[str, Any]]]:
    """
    Resolve rich conversational coreference & subset phrases against `context_files`:
      - Ordinals 1st..10th & last: "the 4th one", "third file", "last item"
      - Ranges & multi-index: "first 3", "last 2", "items 1 and 3", "files 1 to 3", "#2 and #4"
      - Attribute anaphora: "the PDF one", "the image one", "the largest one", "the oldest one",
        "the newest one", "the one in Downloads", "the pinned one", "the untagged ones"
      - Subtractive anaphora: "all except screenshots", "all of them except the first one",
        "them except pinned", "all except draft"
    Returns a matching subset (possibly empty if explicitly matched), or None if `phrase` is not anaphoric.
    """
    if not context_files:
        return None
    clean = re.sub(r"\s+", " ", (phrase or "").strip(" \"'“”`?!.,").lower())
    if not clean:
        return None

    # 1. Subtractive anaphora ("all except screenshots", "all of them except pinned", "them except the first one")
    m_sub = re.match(
        r"^(?:all(?:\s+of\s+(?:them|these|those))?|them|these|those)\s+(?:except(?:\s+for)?|without|excluding|minus|but\s+not)\s+(.+)$",
        clean,
    )
    if m_sub:
        ex_tok = m_sub.group(1).strip()
        ex_ord = re.search(r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last)\b", ex_tok)
        if ex_ord:
            idx = _ORDINAL_MAP[ex_ord.group(1)]
            norm_idx = (len(context_files) + idx) if idx < 0 else idx
            return [f for i, f in enumerate(context_files) if i != norm_idx]
        if ex_tok in {"pinned", "starred", "favorites"}:
            return [f for f in context_files if not f.get("pinned")]
        if ex_tok in {"screenshot", "screenshots"}:
            return [
                f for f in context_files
                if "screenshot" not in str(f.get("name", "")).lower()
                and "screenshot" not in " ".join(f.get("tags") or []).lower()
            ]
        if ex_tok in {"pdf", "pdfs"}:
            return [f for f in context_files if str(f.get("format", "")).upper() != "PDF"]
        if ex_tok in {"image", "images", "photo", "photos", "picture", "pictures"}:
            return [f for f in context_files if f.get("modality") != "image"]
        ex_stem = ex_tok[:-1] if len(ex_tok) > 4 and ex_tok.endswith("s") else ex_tok
        return [
            f for f in context_files
            if ex_tok not in f"{f.get('name', '')} {f.get('title', '')} {f.get('folder', '')} {' '.join(f.get('tags') or [])}".lower()
            and ex_stem not in f"{f.get('name', '')} {f.get('title', '')} {f.get('folder', '')}".lower()
        ]

    # 2. Explicit multi-index / ranges ("items 1 and 3", "files 1, 2, and 4", "#1 and #3", "items 1 to 3")
    m_rng = re.match(r"^(?:the\s+)?(?:items?|files?|ones?|results?)?\s*#?(\d+)\s*(?:to|through|-)\s*#?(\d+)$", clean)
    if m_rng:
        a, b = int(m_rng.group(1)), int(m_rng.group(2))
        lo, hi = max(1, min(a, b)), min(len(context_files), max(a, b))
        return context_files[lo - 1 : hi]

    m_multi = re.match(
        r"^(?:the\s+)?(?:items?|files?|ones?|results?)?\s*(#?\d+(?:\s*(?:,|and|&)\s*#?\d+)+)$",
        clean,
    )
    if m_multi:
        nums = [int(n) for n in re.findall(r"\d+", m_multi.group(1))]
        picked = [context_files[n - 1] for n in nums if 1 <= n <= len(context_files)]
        if picked:
            return picked

    # 3. "first N" / "top N" / "last N"
    m_fn = re.match(r"^(?:the\s+)?(first|top|last)\s+(\d+)(?:\s+(?:ones?|files?|items?|results?))?$", clean)
    if m_fn:
        kind, n = m_fn.group(1), max(1, int(m_fn.group(2)))
        return context_files[-n:] if kind == "last" else context_files[:n]

    # 4. Ordinals 1st..10th ("the fourth one", "2nd file", "last one")
    m_ord = re.match(
        r"^(?:the\s+)?(first|1st|second|2nd|other|third|3rd|fourth|4th|fifth|5th|sixth|6th|seventh|7th|eighth|8th|ninth|9th|tenth|10th|last)"
        r"(?:\s+(?:one|file|item|picture|photo|image|doc|document|pdf|receipt|result))?$",
        clean,
    )
    if m_ord:
        idx = _ORDINAL_MAP[m_ord.group(1)]
        try:
            return [context_files[idx]]
        except IndexError:
            return []

    # 5. Attribute anaphora ("the PDF one", "the largest one", "the one in Downloads", "the pinned ones")
    m_attr = re.match(
        r"^(?:the\s+)?(largest|biggest|smallest|newest|latest|oldest|earliest|pdf|pdfs|image|images|photo|photos|video|document|doc|pinned|untagged|screenshot|screenshots)\s+(?:one|ones|file|files|item|items)$",
        clean,
    )
    if m_attr:
        attr = m_attr.group(1)
        if attr in {"largest", "biggest"}:
            return [max(context_files, key=lambda f: int(f.get("sizeBytes") or 0))]
        if attr == "smallest":
            return [min(context_files, key=lambda f: int(f.get("sizeBytes") or 0))]
        if attr in {"newest", "latest"}:
            return [max(context_files, key=lambda f: float(f.get("mtime") or 0))]
        if attr in {"oldest", "earliest"}:
            return [min(context_files, key=lambda f: float(f.get("mtime") or 0))]
        if attr in {"pdf", "pdfs"}:
            return [f for f in context_files if str(f.get("format", "")).upper() == "PDF"]
        if attr in {"image", "images", "photo", "photos"}:
            return [f for f in context_files if f.get("modality") == "image"]
        if attr == "video":
            return [f for f in context_files if f.get("modality") == "video"]
        if attr in {"document", "doc"}:
            return [f for f in context_files if f.get("modality") == "document"]
        if attr == "pinned":
            return [f for f in context_files if f.get("pinned")]
        if attr == "untagged":
            return [f for f in context_files if not (f.get("tags") or [])]
        if attr in {"screenshot", "screenshots"}:
            return [f for f in context_files if "screenshot" in str(f.get("name", "")).lower()]

    m_in_fld = re.match(r"^(?:the\s+)?(?:one|ones|file|files|item|items)\s+(?:in|from|inside)\s+(?:the\s+)?([a-z0-9 _.\-/]+?)(?:\s+folder)?$", clean)
    if m_in_fld:
        fh = m_in_fld.group(1).strip()
        hits = [
            f for f in context_files
            if fh in str(f.get("folderFull") or f.get("folder") or "").lower()
        ]
        if hits:
            return hits

    return None


def extract_conversation_context(
    state: dict[str, Any],
    history: Optional[list[dict[str, str]]],
    selected_file_id: Optional[str],
    recent_file_ids: Optional[list[str]],
    scope_folder: Optional[str] = None,
) -> dict[str, Any]:
    """
    Build a unified multi-turn context stack from UI selection, recent citations,
    conversation history turns (word-boundary-safe!), pinned folders, and nested folders up to depth 4.
    """
    all_files = [f for f in state.get("files", []) if not f.get("archived")]
    by_id = {str(f["id"]): f for f in all_files}
    watched = list(state.get("watchedFolders") or [])
    pinned = list(state.get("pinnedFolders") or [])
    shelves = list(state.get("shelves") or [])

    mentioned_files: list[dict[str, Any]] = []
    mentioned_file_ids: set[str] = set()
    mentioned_folders: list[str] = []

    shelves_root = Path.home() / "Shelves"
    known_folder_map: dict[str, str] = {}

    def _walk_folders_depth4(root: Path) -> None:
        if not root.exists() or not root.is_dir():
            return
        known_folder_map[root.name.lower()] = str(root.resolve())
        stack: list[tuple[Path, int]] = [(root, 0)]
        visited = 0
        while stack and visited < 300:
            cur, depth = stack.pop()
            if depth >= 4:
                continue
            try:
                for ch in cur.iterdir():
                    if ch.is_dir() and not ch.name.startswith(".") and ch.name not in {".git", "node_modules", "__pycache__", "target", "dist"}:
                        visited += 1
                        res_p = str(ch.resolve())
                        known_folder_map[ch.name.lower()] = res_p
                        known_folder_map[ch.name.lower().replace("-", " ")] = res_p
                        if depth + 1 < 4:
                            stack.append((ch, depth + 1))
            except OSError:
                continue

    for wf in watched:
        _walk_folders_depth4(Path(str(wf.get("path") or "")))
    if shelves_root.exists() and shelves_root.is_dir():
        known_folder_map["shelves"] = str(shelves_root.resolve())
        _walk_folders_depth4(shelves_root)
    for alias, slug in _SHELF_TOPIC_FOLDER_ALIASES.items():
        cand_p = shelves_root / slug
        if cand_p.exists():
            known_folder_map[alias] = str(cand_p.resolve())
    for pf in pinned:
        pp = Path(str(pf.get("path") or ""))
        if pp.exists():
            known_folder_map[pp.name.lower()] = str(pp.resolve())

    for turn in reversed(list(history or [])[-8:]):
        txt = str(turn.get("text") or "").strip()
        if not txt:
            continue
        txt_low = txt.lower()

        # Extract explicitly quoted / backticked / bolded spans first
        spans = re.findall(r"`([^`\n]{2,120})`|\*\*([^*]{2,120})\*\*|[\"“]([^\"”\n]{2,120})[\"”]", txt)
        flat_spans = [next((g for g in grp if g), "").strip() for grp in spans]
        for sp in flat_spans:
            sp_low = sp.lower()
            if sp_low in known_folder_map and known_folder_map[sp_low] not in mentioned_folders:
                mentioned_folders.append(known_folder_map[sp_low])
            for f in all_files:
                if f["id"] in mentioned_file_ids:
                    continue
                if sp_low in {f["name"].lower(), Path(f["name"]).stem.lower(), str(f.get("title") or "").lower()}:
                    mentioned_files.append(f)
                    mentioned_file_ids.add(f["id"])

        # Check folder names mentioned in plain text (word-boundary safe)
        for fname_key, fpath_val in sorted(known_folder_map.items(), key=lambda x: -len(x[0])):
            if len(fname_key) >= 4 and re.search(rf"\b{re.escape(fname_key)}\b", txt_low):
                if fpath_val not in mentioned_folders:
                    mentioned_folders.append(fpath_val)

        # Word-boundary-safe filename & title extraction (no short filename false positives like "a.txt"!)
        for f in all_files:
            if f["id"] in mentioned_file_ids:
                continue
            fn_low = f["name"].lower()
            title_low = str(f.get("title") or "").lower().split("·")[0].strip()
            fn_hit = len(fn_low) >= 4 and bool(re.search(rf"(?<![a-z0-9_]){re.escape(fn_low)}(?![a-z0-9_])", txt_low))
            title_hit = len(title_low) >= 6 and bool(re.search(rf"(?<![a-z0-9_]){re.escape(title_low)}(?![a-z0-9_])", txt_low))
            if fn_hit or title_hit:
                mentioned_files.append(f)
                mentioned_file_ids.add(f["id"])

    # 2. Build ordered candidate file stack: selected -> history-mentioned -> recent_file_ids -> newest in scope
    ordered_context_files: list[dict[str, Any]] = []
    seen_cids: set[str] = set()

    def _push_f(f_obj: Optional[dict[str, Any]]) -> None:
        if f_obj and f_obj["id"] not in seen_cids:
            seen_cids.add(f_obj["id"])
            ordered_context_files.append(f_obj)

    if selected_file_id and selected_file_id in by_id:
        _push_f(by_id[selected_file_id])
    for mf in mentioned_files:
        _push_f(mf)
    for rid in (recent_file_ids or []):
        if rid in by_id:
            _push_f(by_id[rid])

    if not ordered_context_files:
        pool = filter_files_by_scope(all_files, scope_folder, recursive=True) if scope_folder else all_files
        for nf in sorted(pool, key=lambda x: float(x.get("mtime") or 0), reverse=True)[:8]:
            _push_f(nf)

    primary_file = ordered_context_files[0] if ordered_context_files else None
    secondary_file = ordered_context_files[1] if len(ordered_context_files) > 1 else None

    return {
        "primaryFile": primary_file,
        "secondaryFile": secondary_file,
        "contextFiles": ordered_context_files,
        "mentionedFiles": mentioned_files,
        "mentionedFolders": mentioned_folders,
        "knownFolderMap": known_folder_map,
        "shelves": shelves,
    }


def infer_shelf_folder_for_file(file_dict: Optional[dict[str, Any]]) -> Optional[Path]:
    """Return the canonical `~/Shelves/<topic-slug>` folder path for a file based on its shelf or modality."""
    shelves_root = Path.home() / "Shelves"
    if not file_dict:
        return shelves_root / "documents-and-reports" if shelves_root.exists() else None
    shelf_name = str(file_dict.get("shelfName") or "").lower().strip()
    if shelf_name in _SHELF_TOPIC_FOLDER_ALIASES:
        return (shelves_root / _SHELF_TOPIC_FOLDER_ALIASES[shelf_name]).resolve()
    for key, slug in _SHELF_TOPIC_FOLDER_ALIASES.items():
        if key in shelf_name:
            return (shelves_root / slug).resolve()
    mod = str(file_dict.get("modality") or "").lower()
    spec = str(file_dict.get("specimen") or "").lower()
    if spec == "receipt":
        return (shelves_root / "receipts-and-finance").resolve()
    if spec == "diagram":
        return (shelves_root / "diagrams-and-design").resolve()
    if spec == "idcard":
        return (shelves_root / "ids-and-official-forms").resolve()
    if mod == "code":
        return (shelves_root / "code-and-data").resolve()
    if mod == "image":
        return (shelves_root / "photos-and-places").resolve()
    if mod in {"video", "audio", "archive"}:
        return (shelves_root / "media-and-archives").resolve()
    return (shelves_root / "documents-and-reports").resolve()

