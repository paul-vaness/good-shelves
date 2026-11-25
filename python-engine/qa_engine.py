"""
Deep Document Q&A, Summarization, Multi-Modal Comparison & Financial Analytics Engine
for Good Shelves.

Eliminates 600-char cache blindness by reading full SQLite `extracted_text`/`ocr_text`
(with `get_file_excerpt` fallback and self-healing targeted RapidOCR for 1-2 queried
un-OCR'd images/PDFs).

All Markdown output uses crisp headings, bold labels, and bullet/numbered lists —
NEVER GFM pipe tables (for 100% compatibility with `renderAiMarkdown` in the UI).
"""

from __future__ import annotations

import csv
import difflib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

from entity_extractor import extract_entities
from llm_router import synthesize_comparison
from parsers import _compute_column_stats, _extract_code_deep_meta, clean_extracted_text


def _fmt_bytes(size_bytes: int) -> str:
    val = float(max(0, int(size_bytes)))
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            return f"{int(val)} B" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{size_bytes} B"


def _meta_dict(f: dict[str, Any]) -> dict[str, Any]:
    m = f.get("metadata") if "metadata" in f else f.get("metadata_json")
    if isinstance(m, dict):
        return m
    if isinstance(m, str) and m.strip():
        try:
            obj = json.loads(m)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def get_full_texts_batch(
    store: Any,
    file_dicts: list[dict[str, Any]],
    fallback_excerpt: bool = False,
) -> dict[str, dict[str, str]]:
    """
    Fetch FULL `extracted_text` and `ocr_text` from SQLite for `file_dicts`.
    Returns `{fileId: {"extracted": str, "ocr": str, "combined": str}}`.
    """
    ids = [str(f.get("id") or "") for f in file_dicts if f.get("id")]
    if not ids:
        return {}
    raw_db: dict[str, tuple[str, str]] = {}
    with store._lock:
        conn = store._connect()
        try:
            for i in range(0, len(ids), 400):
                batch = ids[i : i + 400]
                ph = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT id, extracted_text, ocr_text FROM files WHERE id IN ({ph})",
                    batch,
                ).fetchall()
                for r in rows:
                    raw_db[str(r["id"])] = (
                        str(r["extracted_text"] or ""),
                        str(r["ocr_text"] or ""),
                    )
        finally:
            conn.close()

    out: dict[str, dict[str, str]] = {}
    for f in file_dicts:
        fid = str(f.get("id") or "")
        if not fid:
            continue
        ext_t, ocr_t = raw_db.get(
            fid,
            (str(f.get("extractedText") or ""), str(f.get("ocrText") or "")),
        )
        if fallback_excerpt and len(ext_t.strip()) < 40 and len(ocr_t.strip()) < 40:
            try:
                ex_res = store.get_file_excerpt(fid, max_lines=300)
                ex_txt = str(ex_res.get("text") or "").strip()
                if ex_txt and not ex_txt.startswith("("):
                    ext_t = ex_txt
            except Exception:
                pass
        combined = "\n".join(p for p in (ocr_t.strip(), ext_t.strip()) if p).strip()
        out[fid] = {"extracted": ext_t, "ocr": ocr_t, "combined": combined}
    return out


def ensure_ocr_for_focus_files(
    store: Any,
    focus_files: list[dict[str, Any]],
    max_auto_ocr: int = 2,
) -> list[str]:
    """
    Self-healing OCR-before-answer: if the user specifically queries 1-2 un-OCR'd images
    or scanned PDFs (and `ocr_enabled` is True), run targeted RapidOCR synchronously
    so the answer immediately reflects the file's real text.
    Returns list of file names that were enriched.
    """
    if not focus_files or len(focus_files) > max_auto_ocr:
        return []
    settings = store.get_settings()
    if not bool(settings.get("ocr_enabled", True)):
        return []

    full_map = get_full_texts_batch(store, focus_files)
    enriched_names: list[str] = []
    for f in focus_files[:max_auto_ocr]:
        fid = str(f.get("id") or "")
        if not fid:
            continue
        mod = str(f.get("modality") or "").lower()
        fmt = str(f.get("format") or "").upper()
        if mod != "image" and fmt != "PDF":
            continue
        texts = full_map.get(fid, {})
        has_ocr = bool(str(texts.get("ocr") or "").strip())
        has_ext = len(str(texts.get("extracted") or "").strip()) >= 40
        if has_ocr or (fmt == "PDF" and has_ext):
            continue
        p = Path(str(f.get("path") or ""))
        if not p.exists():
            continue
        try:
            res = store.execute_file_action(fid, "run_ocr")
            if res.get("ok") and str(res.get("ocrText") or "").strip():
                f["ocrText"] = str(res["ocrText"])
                if not str(f.get("extractedText") or "").strip():
                    f["extractedText"] = str(res["ocrText"])[:600]
                enriched_names.append(str(f.get("name") or fid))
        except Exception:
            continue
    return enriched_names


def split_sliding_chunks(text: str, max_chunks: int = 36, chunk_chars: int = 320) -> list[str]:
    """Split full document text into clean sentence-aligned overlapping windows."""
    cleaned = clean_extracted_text(text or "", preserve_newlines=True)
    if not cleaned.strip():
        return []
    raw_segs = [
        re.sub(r"\s+", " ", s).strip(" -•*\t")
        for s in re.split(r"\n+|(?<=[.!?])\s+| · ", cleaned)
        if len(s.strip()) >= 14
    ]
    if not raw_segs:
        return [cleaned.strip()[:chunk_chars]]

    windows: list[str] = []
    i = 0
    while i < len(raw_segs) and len(windows) < max_chunks:
        cur = raw_segs[i]
        j = i + 1
        while j < len(raw_segs) and len(cur) + 1 + len(raw_segs[j]) <= chunk_chars:
            cur = f"{cur} {raw_segs[j]}"
            j += 1
        windows.append(cur[:chunk_chars])
        # Advance with 1-sentence overlap when multiple sentences were merged
        i = max(i + 1, j - 1) if j > i + 1 else j
    return windows


def mmr_select_passages(
    candidates: list[str],
    cand_vecs: list[np.ndarray],
    query_vec: np.ndarray,
    k: int = 3,
    lambda_param: float = 0.72,
) -> list[str]:
    """Maximal Marginal Relevance selection for diverse, query-focused extractive summaries."""
    if not candidates:
        return []
    if len(candidates) <= k or len(cand_vecs) != len(candidates):
        return candidates[:k]

    rel_scores = [float(np.clip(np.dot(query_vec, v), -1.0, 1.0)) for v in cand_vecs]
    selected_idx: list[int] = []
    remaining = list(range(len(candidates)))

    while remaining and len(selected_idx) < k:
        best_i = remaining[0]
        best_mmr = -1e9
        for idx in remaining:
            rel = rel_scores[idx]
            if not selected_idx:
                mmr = rel
            else:
                max_sim = max(
                    float(np.clip(np.dot(cand_vecs[idx], cand_vecs[s_i]), -1.0, 1.0))
                    for s_i in selected_idx
                )
                mmr = lambda_param * rel - (1.0 - lambda_param) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_i = idx
        selected_idx.append(best_i)
        remaining.remove(best_i)

    # Return in original document reading order for readability
    selected_idx.sort()
    return [candidates[i] for i in selected_idx]


def summarize_file_mmr(
    store: Any,
    file_dict: dict[str, Any],
    query: str = "",
    max_points: int = 3,
    full_text_override: Optional[str] = None,
) -> list[str]:
    """Extract diverse, high-signal key points from FULL SQLite text using MMR + salience."""
    if full_text_override is not None:
        combined = full_text_override
    else:
        ft_map = get_full_texts_batch(store, [file_dict], fallback_excerpt=True)
        combined = ft_map.get(str(file_dict.get("id") or ""), {}).get("combined", "")

    if combined.strip():
        chunks = split_sliding_chunks(combined, max_chunks=28, chunk_chars=240)
        title_norm = re.sub(r"[^a-z0-9]+", " ", str(file_dict.get("title") or "").lower()).strip()
        filtered: list[str] = []
        seen_norm: set[str] = set()
        for seg in chunks:
            low = seg.lower()
            if len(seg) < 18 or re.match(r"^(arxiv:|https?://|page \d+|\d+\s*$)", low):
                continue
            sn = re.sub(r"[^a-z0-9]+", " ", low).strip()
            if not sn or sn == title_norm or sn in seen_norm:
                continue
            seen_norm.add(sn)
            filtered.append(seg)

        if filtered:
            if len(filtered) <= max_points:
                return filtered[:max_points]
            try:
                anchor = query.strip() if query.strip() else f"{file_dict.get('title', '')} summary key findings total amount"
                q_vec = store.embedder._encode_text_384(anchor)
                c_vecs = store.embedder.encode_text_384_batch(filtered)
                if len(c_vecs) == len(filtered):
                    return mmr_select_passages(filtered, c_vecs, q_vec, k=max_points)
            except Exception:
                pass
            return filtered[:max_points]

    # Metadata fallback when file has no text (e.g. non-OCR photo or code without comments)
    meta = _meta_dict(file_dict)
    points: list[str] = []
    if meta.get("merchant") or meta.get("amount"):
        parts = []
        if meta.get("merchant"):
            parts.append(f"Merchant: {meta['merchant']}")
        if meta.get("amount") is not None:
            cur = meta.get("currency") or ""
            parts.append(f"Amount: {cur} {meta['amount']}".strip())
        if meta.get("due_date"):
            parts.append(f"Due: {meta['due_date']}")
        points.append(" · ".join(parts))
    headings = ", ".join((meta.get("headings") or [])[:3])
    symbols = ", ".join((meta.get("codeSymbols") or [])[:4])
    concepts = ", ".join((meta.get("visualConcepts") or [])[:5])
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


def extract_factoid_answer(
    question: str,
    files: list[dict[str, Any]],
    full_texts: dict[str, dict[str, str]],
) -> Optional[str]:
    """
    Answer direct factoid / entity lookups (invoice number, due date, email, phone, TIN,
    author, payment method, tax/VAT, URLs, action items/TODOs, spreadsheet stats, code symbols/imports)
    directly from structured entities + full SQLite text.
    """
    q_low = (question or "").lower()
    if not files:
        return None

    wants_invoice = bool(re.search(r"\b(?:invoice\s+(?:number|no\.?|#|id)|ref(?:erence)?\s+(?:number|no\.?|#)|receipt\s+(?:number|no\.?|#))\b", q_low))
    wants_due = bool(re.search(r"\b(?:due\s+date|when\s+is\s+.+?\s+due|payment\s+due|deadline|expires?)\b", q_low))
    wants_email = bool(re.search(r"\b(?:email|e-mail|mail\s+address)\b", q_low))
    wants_phone = bool(re.search(r"\b(?:phone|contact\s+number|telephone|mobile\s+number|hotline)\b", q_low))
    wants_tin = bool(re.search(r"\b(?:tin|tax\s+id|tax\s+number)\b", q_low))
    wants_author = bool(re.search(r"\b(?:who\s+wrote|who\s+is\s+the\s+author|author\s+of|prepared\s+by|submitted\s+by|written\s+by|who\s+prepared|who\s+submitted)\b", q_low))
    wants_payment = bool(re.search(r"\b(?:payment\s+method|how\s+was\s+.+?\s+paid|paid\s+via|paid\s+with|paid\s+by|mode\s+of\s+payment)\b", q_low))
    wants_tax = bool(re.search(r"\b(?:vat\s+amount|tax\s+amount|sales\s+tax|how\s+much\s+(?:vat|tax)|what\s+is\s+the\s+(?:vat|tax))\b", q_low))
    wants_urls = bool(re.search(r"\b(?:what\s+(?:urls?|links?|websites?)|urls?\s+in|links?\s+in|websites?\s+in|any\s+(?:urls?|links?))\b", q_low))
    wants_todos = bool(re.search(r"\b(?:action\s+items?|todos?|fixmes?|next\s+steps?|pending\s+tasks?|what\s+tasks?\s+are\s+in)\b", q_low))
    wants_sheet_fact = bool(re.search(r"\b(?:what\s+columns?|column\s+headers?|how\s+many\s+rows?|how\s+many\s+columns?|(?:total|sum|average|avg|min|max)\s+of\s+(?:column\s+)?[a-z0-9_]+)\b", q_low))
    wants_code_fact = bool(re.search(r"\b(?:what\s+(?:libraries|imports|packages|modules|functions|classes|methods)|which\s+(?:libraries|imports|functions|classes)|code\s+symbols?)\b", q_low))

    if not any((
        wants_invoice, wants_due, wants_email, wants_phone, wants_tin,
        wants_author, wants_payment, wants_tax, wants_urls, wants_todos,
        wants_sheet_fact, wants_code_fact,
    )):
        return None

    hits: list[str] = []
    for f in files[:6]:
        fid = str(f.get("id") or "")
        raw_meta = _meta_dict(f)
        full_body = full_texts.get(fid, {}).get("combined", "") or str(f.get("extractedText") or "")
        fname = str(f.get("name") or f.get("title") or "file")
        meta = extract_entities(full_body, fname, raw_meta) if full_body else dict(raw_meta)

        if wants_invoice:
            val = str(meta.get("invoice_number") or "").strip()
            if not val and full_body:
                m = re.search(r"\b(?:invoice|inv|receipt|ref|reference)\s*(?:(?:no\.?|#|number|id)\s*[:\-]?|[:#])\s*([A-Z0-9][A-Z0-9\-]{3,24})\b", full_body, re.I)
                if m:
                    val = m.group(1)
            if val:
                hits.append(f"- **Invoice / Ref #:** `{val}` *(from `{fname}`)*")

        if wants_due:
            val = str(meta.get("due_date") or "").strip()
            if not val and full_body:
                m = re.search(r"\b(?:due\s+date|payment\s+due|due)\s*[:\-]?\s*([A-Za-z0-9,/\- ]{6,24})", full_body, re.I)
                if m:
                    val = m.group(1).strip()
            if val:
                amt = meta.get("amount")
                cur = meta.get("currency") or ""
                amt_note = f" · {cur} {amt:,.2f}".strip() if isinstance(amt, (int, float)) else ""
                hits.append(f"- **Due date:** **{val}**{(' (' + amt_note + ')') if amt_note else ''} *(from `{fname}`)*")

        if wants_email:
            val = str(meta.get("email") or "").strip()
            if not val and full_body:
                m = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", full_body)
                if m:
                    val = m.group(0)
            if val:
                hits.append(f"- **Email:** `{val}` *(from `{fname}`)*")

        if wants_phone:
            val = str(meta.get("phone") or "").strip()
            if not val and full_body:
                m = re.search(r"\b(?:\+?\d{1,3}[\- ]?)?\(?\d{2,4}\)?[\- ]?\d{3,4}[\- ]?\d{4}\b", full_body)
                if m:
                    val = m.group(0)
            if val:
                hits.append(f"- **Phone:** `{val}` *(from `{fname}`)*")

        if wants_tin:
            val = str(meta.get("tin") or "").strip()
            if val:
                hits.append(f"- **TIN:** `{val}` *(from `{fname}`)*")

        if wants_author:
            val = str(meta.get("author_or_person") or "").strip()
            if val:
                hits.append(f"- **Author / Person:** **{val}** *(from `{fname}`)*")

        if wants_payment:
            val = str(meta.get("payment_method") or "").strip()
            if val:
                hits.append(f"- **Payment method:** **{val}** *(from `{fname}`)*")

        if wants_tax:
            t_amt = meta.get("tax_amount")
            if isinstance(t_amt, (int, float)):
                cur = str(meta.get("currency") or "").strip()
                hits.append(f"- **Tax / VAT:** **{cur} {float(t_amt):,.2f}** *(from `{fname}`)*".replace("  ", " "))

        if wants_urls:
            urls = list(meta.get("urls") or [])
            if urls:
                hits.append(f"- **URLs / Links (`{fname}`):** " + ", ".join(f"`{u}`" for u in urls[:5]))

        if wants_todos:
            items = list(meta.get("action_items") or []) + list(meta.get("todos") or [])
            if not items and full_body and str(f.get("modality") or "").lower() == "code":
                items = _extract_code_deep_meta(full_body).get("todos", [])
            if items:
                for it in items[:5]:
                    hits.append(f"- **Action / TODO (`{fname}`):** {it}")

        if wants_sheet_fact:
            headers = list(meta.get("headers") or [])
            col_stats = meta.get("columnStats") if isinstance(meta.get("columnStats"), dict) else {}
            if not headers and full_body and Path(fname).suffix.lower() in {".csv", ".tsv"}:
                delim = "\t" if fname.lower().endswith(".tsv") else ","
                csv_lines = [ln for ln in full_body.splitlines() if ln.strip() and not ln.lower().startswith("column stats:")]
                parsed_rows = list(csv.reader(csv_lines[:251], delimiter=delim))
                if parsed_rows:
                    headers = [str(c).strip() for c in parsed_rows[0]]
                    col_stats = _compute_column_stats(headers, parsed_rows[1:])
            tot_rows = meta.get("totalRows")
            tot_cols = meta.get("totalCols") or len(headers)
            if headers or col_stats or tot_rows is not None:
                if "how many rows" in q_low and tot_rows is not None:
                    hits.append(f"- **Rows (`{fname}`):** **{tot_rows}** data row(s)")
                if ("what columns" in q_low or "column headers" in q_low or "how many columns" in q_low) and headers:
                    hits.append(f"- **Columns (`{fname}`, {tot_cols} total):** " + ", ".join(f"`{h}`" for h in headers if h))
                if col_stats:
                    for c_name, st in col_stats.items():
                        if c_name.lower() in q_low or any(k in q_low for k in ("total", "sum", "average", "avg", "min", "max")):
                            hits.append(
                                f"- **`{c_name}` (`{fname}`):** sum = **{st['sum']:,.2f}** · avg = **{st['avg']:,.2f}** · min = {st['min']:,.2f} · max = {st['max']:,.2f} ({st['count']} rows)"
                            )

        if wants_code_fact:
            code_deep = _extract_code_deep_meta(full_body) if full_body else {"imports": [], "todos": [], "docstrings": []}
            imports = list(meta.get("imports") or code_deep.get("imports") or [])
            symbols = list(meta.get("codeSymbols") or f.get("codeSymbols") or [])
            if any(w in q_low for w in ("import", "imports", "libraries", "packages", "modules")) and imports:
                hits.append(f"- **Imports (`{fname}`):** " + ", ".join(f"`{imp}`" for imp in imports[:12]))
            if any(w in q_low for w in ("function", "functions", "class", "classes", "method", "methods", "symbol", "symbols")) and symbols:
                hits.append(f"- **Defined symbols (`{fname}`):** " + ", ".join(f"`{s}`" for s in symbols[:12]))

    if hits:
        return "\n".join(hits[:10])
    return None


def build_file_context_dossier(
    store: Any,
    file_dict: dict[str, Any],
    full_text_override: Optional[str] = None,
) -> dict[str, Any]:
    """
    Synthesize a complete, multi-modal semantic profile (Deep File Context Dossier) of a file:
      - Identity & location (title, name, format, modality, specimen, shelf, sub-shelf, folder, size, dates, dimensions/pages)
      - Structured entities (merchant, amount, currency, tax_amount, payment_method, date, due_date,
        invoice_number, doc_type, author_or_person, email, phone, tin, urls, action_items)
      - Modality-specific deep structure:
        * Spreadsheet: sheets, row/col counts, column headers, columnStats (sum/avg/min/max/count), sampleRows
        * Presentation: slideCount, per-slide titles & bullets ([Slide N])
        * PDF / Document: pageCount, headings, wordCount, readingTimeMin, pageTexts ([Page N]), keyTakeaways
        * Code: language, lineCount, codeSymbols, imports, todos ([Line N]), docstrings
        * Image / Scan / Receipt: resolution, camera, takenDate, visualConcepts, ocrText, ocrConfidence, receiptFields
        * Archive: totalEntries, totalSizeBytes, totalSizeHuman, extBreakdown, topEntries
    """
    fid = str(file_dict.get("id") or "")
    fname = str(file_dict.get("name") or "")
    ext = Path(fname).suffix.lower() if fname else ("." + str(file_dict.get("format") or "").lower().lstrip("."))
    modality = str(file_dict.get("modality") or "document").lower()
    raw_meta = _meta_dict(file_dict)

    if full_text_override is not None:
        full_text = full_text_override
        ocr_text = str(file_dict.get("ocrText") or file_dict.get("ocr_text") or "")
    elif store is not None and fid:
        ft_map = get_full_texts_batch(store, [file_dict], fallback_excerpt=True)
        entry = ft_map.get(fid, {})
        full_text = entry.get("combined", "")
        ocr_text = entry.get("ocr", "") or str(file_dict.get("ocrText") or "")
    else:
        ext_t = str(file_dict.get("extractedText") or file_dict.get("extracted_text") or "")
        ocr_text = str(file_dict.get("ocrText") or file_dict.get("ocr_text") or "")
        full_text = "\n".join(p for p in (ocr_text.strip(), ext_t.strip()) if p).strip()

    entities = extract_entities(full_text, fname, raw_meta)
    size_bytes = int(file_dict.get("sizeBytes") or file_dict.get("size_bytes") or 0)
    size_human = str(file_dict.get("size") or "") or _fmt_bytes(size_bytes)
    width = int(file_dict.get("width") or raw_meta.get("width") or 0)
    height = int(file_dict.get("height") or raw_meta.get("height") or 0)
    page_count = int(file_dict.get("pageCount") or file_dict.get("page_count") or raw_meta.get("pageCount") or 1)
    headings = list(raw_meta.get("headings") or file_dict.get("headings") or [])
    word_count = int(raw_meta.get("wordCount") or len(full_text.split()))
    reading_time_min = max(1, int(round(word_count / 200.0))) if word_count > 0 else 0

    dossier: dict[str, Any] = {
        "id": fid,
        "title": str(file_dict.get("title") or fname),
        "name": fname,
        "format": str(file_dict.get("format") or ext.lstrip(".")).upper(),
        "ext": ext,
        "modality": modality,
        "specimen": str(file_dict.get("specimen") or file_dict.get("specimen_type") or "text"),
        "shelfName": str(file_dict.get("shelfName") or file_dict.get("shelf_name") or "Quiet Tray"),
        "subShelf": str(file_dict.get("subShelf") or file_dict.get("sub_shelf") or ""),
        "folder": str(file_dict.get("folder") or file_dict.get("folder_short") or ""),
        "folderFull": str(file_dict.get("folderFull") or ""),
        "sizeBytes": size_bytes,
        "size": size_human,
        "createdDate": str(file_dict.get("createdDate") or file_dict.get("created_date") or ""),
        "date": str(file_dict.get("date") or file_dict.get("display_date") or ""),
        "mtime": float(file_dict.get("mtime") or 0.0),
        "pageCount": page_count,
        "width": width,
        "height": height,
        "tags": list(file_dict.get("tags") or []),
        "pinned": bool(file_dict.get("pinned")),
        "wordCount": word_count,
        "readingTimeMin": reading_time_min,
        "headings": headings,
        "entities": {
            "merchant": entities.get("merchant"),
            "amount": entities.get("amount"),
            "currency": entities.get("currency"),
            "tax_amount": entities.get("tax_amount"),
            "payment_method": entities.get("payment_method"),
            "date": entities.get("date"),
            "due_date": entities.get("due_date"),
            "invoice_number": entities.get("invoice_number"),
            "doc_type": entities.get("doc_type"),
            "author_or_person": entities.get("author_or_person"),
            "email": entities.get("email"),
            "phone": entities.get("phone"),
            "tin": entities.get("tin"),
            "urls": list(entities.get("urls") or []),
            "action_items": list(entities.get("action_items") or []),
        },
    }

    kind = str(raw_meta.get("kind") or "").lower()

    # 1. Spreadsheet structure
    if kind == "spreadsheet" or ext in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}:
        headers = list(raw_meta.get("headers") or [])
        rows = list(raw_meta.get("rows") or [])
        col_stats = dict(raw_meta.get("columnStats") or {})
        total_rows = int(raw_meta.get("totalRows") or 0)
        total_cols = int(raw_meta.get("totalCols") or len(headers))
        if (not headers or not col_stats) and full_text and ext in {".csv", ".tsv"}:
            delim = "\t" if ext == ".tsv" else ","
            csv_lines = [ln for ln in full_text.splitlines() if ln.strip() and not ln.lower().startswith("column stats:")]
            parsed_csv = list(csv.reader(csv_lines[:251], delimiter=delim))
            if parsed_csv:
                if not headers:
                    headers = [str(c).strip() for c in parsed_csv[0][:12]]
                data_r = [[str(c).strip() for c in r[:12]] for r in parsed_csv[1:]]
                if not rows:
                    rows = [r[:10] for r in data_r[:15]]
                if not col_stats:
                    col_stats = _compute_column_stats(headers, data_r)
                if not total_rows:
                    total_rows = len(data_r)
                if not total_cols:
                    total_cols = len(parsed_csv[0])
        elif not col_stats and headers and rows:
            col_stats = _compute_column_stats(headers, rows)
        dossier["spreadsheet"] = {
            "sheets": list(raw_meta.get("sheets") or ["Sheet1"]),
            "activeSheet": str(raw_meta.get("activeSheet") or "Sheet1"),
            "totalRows": total_rows,
            "totalCols": total_cols,
            "headers": headers,
            "columnStats": col_stats,
            "sampleRows": rows[:8],
            "sheetsData": raw_meta.get("sheetsData") or {},
        }

    # 2. Presentation (PPTX) structure
    if kind == "pptx" or ext in {".pptx", ".ppt"}:
        slides = list(raw_meta.get("slides") or [])
        if not slides and full_text:
            for m_sl in re.finditer(r"\[Slide\s+(\d+)\]\s*([^\n]+)", full_text):
                s_idx = int(m_sl.group(1))
                s_body = m_sl.group(2).strip()
                slides.append({"index": s_idx, "title": s_body[:80], "text": s_body})
        dossier["presentation"] = {
            "slideCount": int(raw_meta.get("slideCount") or len(slides) or 1),
            "slides": slides,
        }

    # 3. Code structure
    if modality == "code" or kind == "code":
        code_deep = _extract_code_deep_meta(full_text) if full_text else {"imports": [], "todos": [], "docstrings": []}
        symbols = list(raw_meta.get("codeSymbols") or file_dict.get("codeSymbols") or [])
        imports = list(raw_meta.get("imports") or code_deep.get("imports") or [])
        todos = list(raw_meta.get("todos") or code_deep.get("todos") or [])
        docstrings = list(raw_meta.get("docstrings") or code_deep.get("docstrings") or [])
        line_count = int(raw_meta.get("lineCount") or (full_text.count("\n") + 1 if full_text else 0))
        dossier["code"] = {
            "language": str(raw_meta.get("language") or ext.lstrip(".") or "code"),
            "lineCount": line_count,
            "codeSymbols": symbols,
            "imports": imports,
            "todos": todos,
            "docstrings": docstrings,
        }

    # 4. Archive structure
    if modality == "archive" or kind == "archive" or ext in {".zip", ".tar", ".tgz", ".gz"}:
        entries = list(raw_meta.get("entries") or [])
        ext_counts: dict[str, int] = {}
        for e in entries:
            if not e.get("isDir"):
                e_ext = str(e.get("ext") or "other").lower() or "other"
                ext_counts[e_ext] = ext_counts.get(e_ext, 0) + 1
        uncomp_bytes = int(raw_meta.get("totalSizeBytes") or 0)
        dossier["archive"] = {
            "totalEntries": int(raw_meta.get("totalEntries") or len(entries)),
            "totalSizeBytes": uncomp_bytes,
            "totalSizeHuman": _fmt_bytes(uncomp_bytes),
            "extBreakdown": ext_counts,
            "topEntries": [e for e in entries if not e.get("isDir")][:15],
        }

    # 5. Image / Scan / Receipt structure
    if modality == "image" or dossier["specimen"] == "receipt":
        concepts = list(raw_meta.get("visualConcepts") or file_dict.get("visualTags") or [])
        dossier["image"] = {
            "resolution": f"{width}×{height}px" if width and height else "",
            "camera": str(raw_meta.get("camera") or ""),
            "takenDate": str(raw_meta.get("takenDate") or ""),
            "visualConcepts": concepts,
            "ocrText": clean_extracted_text(ocr_text, preserve_newlines=True)[:1200],
            "ocrConfidence": float(file_dict.get("ocrConfidence") or file_dict.get("ocr_confidence") or 0.0),
            "receiptFields": raw_meta.get("receiptFields") if isinstance(raw_meta.get("receiptFields"), dict) else {},
        }

    # 6. PDF / Document per-page texts & MMR takeaways
    page_texts = list(raw_meta.get("pageTexts") or [])
    dossier["pageTexts"] = page_texts
    if store is not None:
        dossier["keyTakeaways"] = summarize_file_mmr(
            store,
            file_dict,
            max_points=4,
            full_text_override=full_text,
        )
        try:
            all_active = [
                f for f in (store.get_all_state().get("files") or [])
                if not f.get("archived")
            ]
            if len(all_active) > 1:
                graph = build_cross_file_knowledge_graph(
                    store,
                    file_dict,
                    all_active,
                    {fid: {"combined": full_text}},
                )
                dossier["connectedFiles"] = graph.get("connectedFiles") or []
        except Exception:
            dossier["connectedFiles"] = []
    else:
        chunks = split_sliding_chunks(full_text, max_chunks=4, chunk_chars=240)
        dossier["keyTakeaways"] = chunks[:4]
        dossier["connectedFiles"] = []

    return dossier


def _find_passage_anchor(passage: str, dossier: dict[str, Any], full_text: str) -> str:
    """Locate `[Page N]`, `[Slide N]`, or `[Line N]` anchor for a passage when available."""
    if not passage:
        return ""
    m_sl = re.search(r"\[Slide\s+(\d+)\]", passage)
    if m_sl:
        return f"[Slide {m_sl.group(1)}] "

    probe = passage[:48].strip().lower()
    page_texts = dossier.get("pageTexts") or []
    if page_texts and probe:
        for idx, pt in enumerate(page_texts, start=1):
            if probe in str(pt or "").lower():
                return f"[Page {idx}] "

    pres = dossier.get("presentation")
    if isinstance(pres, dict) and probe:
        for sl in pres.get("slides") or []:
            if probe in str(sl.get("text") or "").lower() or probe in str(sl.get("title") or "").lower():
                return f"[Slide {sl.get('index')}] "

    if dossier.get("modality") == "code" and full_text and probe:
        for ln_idx, ln in enumerate(full_text.splitlines(), start=1):
            if probe[:28] in ln.lower():
                return f"[Line {ln_idx}] "
    return ""


def _render_file_dossier_markdown(dossier: dict[str, Any]) -> str:
    """Format a single file's Deep File Context Dossier into crisp Markdown (NO pipe tables!)."""
    lines: list[str] = []
    fname = dossier["name"]
    title = dossier["title"]
    ent = dossier.get("entities") or {}
    doc_type_lbl = f" · Type: **{ent['doc_type']}**" if ent.get("doc_type") else ""
    shelf_lbl = dossier["shelfName"] + (f" / {dossier['subShelf']}" if dossier.get("subShelf") else "")

    lines.append(f"**Deep File Context:** `{fname}` ({title})")
    lines.append("\n**Overview & Classification**")
    lines.append(
        f"- **Format & Modality:** {dossier['format']} ({dossier['modality']}){doc_type_lbl}"
    )
    lines.append(
        f"- **Location & Shelf:** `{dossier['folder'] or '.'}` · Shelf: **{shelf_lbl}**"
    )
    meta_parts = [f"Size: {dossier['size']}"]
    if dossier.get("createdDate") or dossier.get("date"):
        meta_parts.append(f"Date: {dossier.get('createdDate') or dossier.get('date')}")
    if dossier.get("pageCount", 1) > 1 or dossier.get("ext") == ".pdf":
        meta_parts.append(f"Pages: {dossier['pageCount']}")
    if dossier.get("width") and dossier.get("height"):
        meta_parts.append(f"Resolution: {dossier['width']}×{dossier['height']}px")
    if "code" in dossier:
        meta_parts.append(f"Lines: {dossier['code']['lineCount']}")
    elif dossier.get("wordCount", 0) > 0:
        meta_parts.append(f"Words: {dossier['wordCount']:,} (~{dossier['readingTimeMin']} min read)")
    lines.append("- **Metrics:** " + " · ".join(meta_parts))
    if dossier.get("tags"):
        lines.append("- **Tags:** " + ", ".join(f"`#{t}`" for t in dossier["tags"]))

    # Key Entities & Metadata section
    ent_lines: list[str] = []
    if ent.get("merchant"):
        ent_lines.append(f"- **Merchant / Vendor:** **{ent['merchant']}**")
    if ent.get("amount") is not None:
        cur = ent.get("currency") or ""
        tax_str = f" (Tax/VAT: {cur} {ent['tax_amount']:,.2f})".strip() if isinstance(ent.get("tax_amount"), (int, float)) else ""
        ent_lines.append(f"- **Total Amount:** **{cur} {float(ent['amount']):,.2f}**{tax_str}".replace("  ", " "))
    elif isinstance(ent.get("tax_amount"), (int, float)):
        cur = ent.get("currency") or ""
        ent_lines.append(f"- **Tax / VAT:** **{cur} {float(ent['tax_amount']):,.2f}**".replace("  ", " "))
    if ent.get("payment_method"):
        ent_lines.append(f"- **Payment Method:** {ent['payment_method']}")
    if ent.get("invoice_number"):
        ent_lines.append(f"- **Invoice / Ref #:** `{ent['invoice_number']}`")
    if ent.get("due_date"):
        ent_lines.append(f"- **Due Date:** **{ent['due_date']}**")
    if ent.get("author_or_person"):
        ent_lines.append(f"- **Author / Person:** **{ent['author_or_person']}**")
    contact_parts = []
    if ent.get("email"):
        contact_parts.append(f"Email: `{ent['email']}`")
    if ent.get("phone"):
        contact_parts.append(f"Phone: `{ent['phone']}`")
    if ent.get("tin"):
        contact_parts.append(f"TIN: `{ent['tin']}`")
    if contact_parts:
        ent_lines.append("- **Contact / Tax IDs:** " + " · ".join(contact_parts))
    if ent.get("urls"):
        ent_lines.append("- **URLs / Links:** " + ", ".join(f"`{u}`" for u in ent["urls"][:5]))
    if ent.get("action_items"):
        for ai in ent["action_items"][:5]:
            ent_lines.append(f"- **Action Item:** {ai}")
    if ent_lines:
        lines.append("\n**Key Entities & Metadata**")
        lines.extend(ent_lines)

    # Structure & Architecture section
    struct_lines: list[str] = []
    if "spreadsheet" in dossier:
        sp = dossier["spreadsheet"]
        struct_lines.append(
            f"- **Sheets & Grid:** {', '.join(sp['sheets'])} ({sp['totalRows']} rows × {sp['totalCols']} columns)"
        )
        if sp.get("headers"):
            struct_lines.append("- **Columns:** " + ", ".join(f"`{h}`" for h in sp["headers"] if h))
        if sp.get("columnStats"):
            for col_n, st in list(sp["columnStats"].items())[:8]:
                struct_lines.append(
                    f"- **Numeric Column `{col_n}`:** sum = **{st['sum']:,.2f}** · avg = **{st['avg']:,.2f}** · min = {st['min']:,.2f} · max = {st['max']:,.2f} ({st['count']} rows)"
                )
        if sp.get("sampleRows"):
            for r_i, s_row in enumerate(sp["sampleRows"][:3], start=1):
                row_pairs = [
                    f"{sp['headers'][idx]}: {val}" if idx < len(sp.get("headers") or []) and sp["headers"][idx] else val
                    for idx, val in enumerate(s_row[:6])
                    if val
                ]
                if row_pairs:
                    struct_lines.append(f"- **Row {r_i}:** " + " · ".join(row_pairs))

    elif "presentation" in dossier:
        pr = dossier["presentation"]
        struct_lines.append(f"- **Slide Count:** {pr['slideCount']} slide(s)")
        for sl in (pr.get("slides") or [])[:10]:
            struct_lines.append(f"- **[Slide {sl['index']}] {sl['title']}:** {sl['text'][:180]}")

    elif "code" in dossier:
        cd = dossier["code"]
        struct_lines.append(f"- **Language & Size:** `{cd['language']}` · {cd['lineCount']} line(s)")
        if cd.get("docstrings"):
            struct_lines.append(f"- **Purpose / Docstring:** {cd['docstrings'][0]}")
        if cd.get("codeSymbols"):
            struct_lines.append("- **Defined Functions / Classes:** " + ", ".join(f"`{s}`" for s in cd["codeSymbols"][:12]))
        if cd.get("imports"):
            struct_lines.append("- **Imported Libraries / Modules:** " + ", ".join(f"`{imp}`" for imp in cd["imports"][:12]))
        if cd.get("todos"):
            for td in cd["todos"][:5]:
                struct_lines.append(f"- **Code TODO:** `{td}`")

    elif "archive" in dossier:
        ar = dossier["archive"]
        struct_lines.append(
            f"- **Archive Summary:** {ar['totalEntries']} entries · Uncompressed size: {ar['totalSizeHuman']}"
        )
        if ar.get("extBreakdown"):
            bd = ", ".join(f"`.{k}` ({v})" for k, v in sorted(ar["extBreakdown"].items(), key=lambda x: -x[1])[:8])
            struct_lines.append(f"- **File Types Inside:** {bd}")
        if ar.get("topEntries"):
            struct_lines.append(
                "- **Top Entries:** " + ", ".join(f"`{e['name']}` ({_fmt_bytes(e.get('sizeBytes', 0))})" for e in ar["topEntries"][:8])
            )

    elif "image" in dossier:
        im = dossier["image"]
        if im.get("camera") or im.get("takenDate"):
            struct_lines.append(
                "- **Camera & Capture:** " + " · ".join(p for p in (im.get("camera"), im.get("takenDate")) if p)
            )
        if im.get("visualConcepts"):
            struct_lines.append("- **Visual Scene Tags:** " + ", ".join(im["visualConcepts"][:8]))
        if im.get("ocrText"):
            conf_lbl = f" ({im['ocrConfidence']:.0f}% confidence)" if im.get("ocrConfidence") else ""
            struct_lines.append(f"- **OCR Transcript{conf_lbl}:** {im['ocrText'][:360]}")

    else:
        if dossier.get("headings"):
            struct_lines.append("- **Section Headings:** " + " · ".join(f"**{h}**" for h in dossier["headings"][:6]))
        page_texts = dossier.get("pageTexts") or []
        if len(page_texts) > 1:
            for p_idx, pt in enumerate(page_texts[:6], start=1):
                first_line = next((ln.strip() for ln in str(pt).splitlines() if len(ln.strip()) >= 4), "")
                if first_line:
                    struct_lines.append(f"- **[Page {p_idx}]:** {first_line[:160]}")

    if struct_lines:
        lines.append("\n**Structure & Architecture**")
        lines.extend(struct_lines)

    # Key Takeaways & Context
    takeaways = dossier.get("keyTakeaways") or []
    if takeaways:
        lines.append("\n**Key Takeaways & Context**")
        for tk in takeaways[:4]:
            anchor = _find_passage_anchor(tk, dossier, "")
            clean_tk = re.sub(r"^\[Slide\s+\d+\]\s*", "", tk).strip()
            lines.append(f"- {anchor}{clean_tk}")

    connected = dossier.get("connectedFiles") or []
    if connected:
        lines.append("\n**Connected Files & Knowledge Graph**")
        for cf in connected[:5]:
            lines.append(f"- **`{cf['fileName']}`** ({cf['relation']}): {cf['detail']}")

    return "\n".join(lines)


def _extract_all_spreadsheet_rows(
    f: dict[str, Any],
    sp: dict[str, Any],
    full_text: str,
) -> tuple[list[str], list[list[str]]]:
    """Return `(headers, all_data_rows)` from CSV/TSV full text or XLSX metadata."""
    fname = str(f.get("name") or "")
    ext = Path(fname).suffix.lower()
    headers = [str(h).strip() for h in (sp.get("headers") or [])]
    rows: list[list[str]] = []

    if full_text and ext in {".csv", ".tsv"}:
        delim = "\t" if ext == ".tsv" else ","
        csv_lines = [
            ln for ln in full_text.splitlines()
            if ln.strip() and not ln.lower().startswith("column stats:")
        ]
        parsed = list(csv.reader(csv_lines, delimiter=delim))
        if parsed:
            headers = [str(c).strip() for c in parsed[0]]
            rows = [[str(c).strip() for c in r] for r in parsed[1:] if any(str(c).strip() for c in r)]
            return headers, rows

    raw_meta = _meta_dict(f)
    if isinstance(raw_meta.get("rows"), list) and raw_meta["rows"]:
        rows = [[str(c).strip() for c in r] for r in raw_meta["rows"]]
    elif sp.get("sampleRows"):
        rows = [[str(c).strip() for c in r] for r in sp["sampleRows"]]
    return headers, rows


def _parse_cell_float(cell: str) -> Optional[float]:
    cleaned = re.sub(r"^(?:PHP|USD|EUR|GBP|JPY|AUD|CAD|SGD|KRW|INR|A\$|C\$|S\$|[₱$€£¥₩₹])\s*", "", (cell or "").strip(), flags=re.IGNORECASE)
    cleaned = cleaned.replace(",", "").rstrip("%").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _answer_conditional_spreadsheet_query(
    question: str,
    f: dict[str, Any],
    sp: dict[str, Any],
    full_text: str,
    name_prefix: str = "",
) -> Optional[str]:
    """
    Execute natural-language conditional filtering (`where`, `for`, `>`, `<`, `=`)
    and `group by` / `breakdown by` aggregations over spreadsheet/CSV rows.
    """
    q_clean = (question or "").strip()
    q_low = q_clean.lower()
    headers, rows = _extract_all_spreadsheet_rows(f, sp, full_text)
    if not headers or not rows:
        return None

    fname = str(f.get("name") or "spreadsheet")
    col_map = {h.lower(): idx for idx, h in enumerate(headers) if h}

    def _match_col_name(text_frag: str) -> Optional[tuple[int, str]]:
        frag_low = text_frag.strip(" ?.,\"'`").lower()
        for idx, h in enumerate(headers):
            if not h:
                continue
            hl = h.lower()
            if hl == frag_low or hl.replace("_", " ") == frag_low:
                return idx, h
        # Check longest header substring in frag_low
        best: Optional[tuple[int, str]] = None
        for idx, h in enumerate(headers):
            if not h:
                continue
            hl = h.lower()
            if re.search(rf"\b{re.escape(hl)}\b", frag_low) or re.search(rf"\b{re.escape(hl.replace('_', ' '))}\b", frag_low):
                if best is None or len(h) > len(best[1]):
                    best = (idx, h)
        return best

    # 1. Group-by / Breakdown query: "breakdown Cost_USD by Department", "group by Department"
    m_grp = re.search(
        r"\b(?:breakdown|break\s+down|group(?:\s+by)?|grouped\s+by|by)\s+(?:([a-z0-9_]+)\s+by\s+)?([a-z0-9_ ]+?)(?:\s+in\s+|\s+from\s+|\s+for\s+|$|\?)",
        q_low,
    )
    if m_grp and any(k in q_low for k in ("breakdown", "break down", "group by", "grouped by")):
        grp_col_hit = _match_col_name(m_grp.group(2) or "")
        if not grp_col_hit:
            for idx, h in enumerate(headers):
                if f"by {h.lower()}" in q_low or f"by {h.lower().replace('_', ' ')}" in q_low:
                    grp_col_hit = (idx, h)
                    break
        if grp_col_hit:
            g_idx, g_name = grp_col_hit
            # Determine target numeric column to aggregate (either explicitly mentioned or first numeric column)
            num_col_hit: Optional[tuple[int, str]] = None
            for idx, h in enumerate(headers):
                if idx == g_idx or not h:
                    continue
                if h.lower() in q_low or h.lower().replace("_", " ") in q_low:
                    num_col_hit = (idx, h)
                    break
            if num_col_hit is None:
                col_stats = sp.get("columnStats") or {}
                for h_cand in col_stats:
                    if h_cand in col_map and col_map[h_cand] != g_idx:
                        num_col_hit = (col_map[h_cand], h_cand)
                        break

            groups: dict[str, list[float]] = {}
            group_counts: dict[str, int] = {}
            for r in rows:
                if g_idx >= len(r):
                    continue
                g_val = r[g_idx].strip() or "(Empty)"
                group_counts[g_val] = group_counts.get(g_val, 0) + 1
                if num_col_hit and num_col_hit[0] < len(r):
                    fv = _parse_cell_float(r[num_col_hit[0]])
                    if fv is not None:
                        groups.setdefault(g_val, []).append(fv)

            if group_counts:
                if num_col_hit and groups:
                    n_name = num_col_hit[1]
                    lines = [
                        f"{name_prefix}here is the breakdown of **`{n_name}`** by **`{g_name}`** in `{fname}` ({len(rows)} rows):\n"
                    ]
                    ordered_g = sorted(groups.items(), key=lambda kv: sum(kv[1]), reverse=True)
                    for gv, vals in ordered_g[:15]:
                        tot = sum(vals)
                        avg = tot / len(vals)
                        lines.append(
                            f"- **{gv}:** Total = **{tot:,.2f}** · Average = **{avg:,.2f}** ({len(vals)} row{'s' if len(vals) != 1 else ''})"
                        )
                    return "\n".join(lines)
                else:
                    lines = [f"{name_prefix}here is the row count grouped by **`{g_name}`** in `{fname}`:\n"]
                    for gv, cnt in sorted(group_counts.items(), key=lambda kv: -kv[1])[:15]:
                        lines.append(f"- **{gv}:** **{cnt}** row(s)")
                    return "\n".join(lines)

    # 2. Conditional filter (`where <col> <op> <val>` OR `for <CategoryValue>`)
    filter_col_idx: Optional[int] = None
    filter_col_name: str = ""
    filter_op: str = "="
    filter_val_raw: str = ""

    # Check explicit `where <col> ...`
    m_where = re.search(
        r"\bwhere\s+([a-z0-9_ ]+?)\s+(?:(is\s+not|!=|>=|<=|>|<|=|is|equals?|contains?)\s+)([^\n?.!]+?)(?:\s+in\s+[a-z0-9_.\-]+|\?|$)",
        q_clean,
        re.IGNORECASE,
    )
    if m_where:
        c_hit = _match_col_name(m_where.group(1))
        if c_hit:
            filter_col_idx, filter_col_name = c_hit
            op_tok = m_where.group(2).strip().lower()
            filter_op = (
                "!=" if op_tok in {"is not", "!="}
                else op_tok if op_tok in {">=", "<=", ">", "<"}
                else "contains" if "contain" in op_tok
                else "="
            )
            filter_val_raw = m_where.group(3).strip(" \"'?.!,")

    # Also support natural `"for <Value>"` (e.g. "what is the total Cost_USD for Engineering in q1_expenses.csv?")
    if filter_col_idx is None:
        m_for = re.search(
            r"\b(?:for|in\s+department|in\s+category|with\s+status)\s+([A-Za-z0-9_.\-]+?)(?:\s+in\s+[A-Za-z0-9_.\-]+|\?|$)",
            q_clean,
            re.IGNORECASE,
        )
        if m_for:
            cand_val = m_for.group(1).strip(" \"'?.!,")
            cand_low = cand_val.lower()
            if cand_low not in {fname.lower(), Path(fname).stem.lower()}:
                for c_i, h in enumerate(headers):
                    if any(c_i < len(r) and r[c_i].strip().lower() == cand_low for r in rows):
                        filter_col_idx = c_i
                        filter_col_name = h
                        filter_op = "="
                        filter_val_raw = cand_val
                        break

    if filter_col_idx is None:
        return None

    # Evaluate filter across rows
    matched_rows: list[list[str]] = []
    f_num = _parse_cell_float(filter_val_raw)
    f_low = filter_val_raw.lower()
    for r in rows:
        if filter_col_idx >= len(r):
            continue
        cell_s = r[filter_col_idx].strip()
        cell_low = cell_s.lower()
        cell_num = _parse_cell_float(cell_s)
        keep = False
        if filter_op in {">", "<", ">=", "<="} and cell_num is not None and f_num is not None:
            if filter_op == ">":
                keep = cell_num > f_num
            elif filter_op == "<":
                keep = cell_num < f_num
            elif filter_op == ">=":
                keep = cell_num >= f_num
            elif filter_op == "<=":
                keep = cell_num <= f_num
        elif filter_op == "!=":
            keep = cell_low != f_low
        elif filter_op == "contains":
            keep = f_low in cell_low
        else:
            keep = (cell_low == f_low) or (cell_num is not None and f_num is not None and abs(cell_num - f_num) < 1e-6)
        if keep:
            matched_rows.append(r)

    cond_label = f"`{filter_col_name} {filter_op} {filter_val_raw}`"

    # Determine if user asked for an aggregation (sum/total/average/min/max/count) or row listing
    wants_count_only = bool(re.search(r"\b(?:how\s+many\s+rows|count\s+of\s+rows|number\s+of\s+rows)\b", q_low))
    wants_agg = bool(re.search(r"\b(?:total|sum|average|avg|mean|min|max)\b", q_low))

    if wants_count_only:
        return f"{name_prefix}there are **{len(matched_rows)} matching row(s)** in `{fname}` where {cond_label} (out of {len(rows)} total rows)."

    # Check if a numeric target column is mentioned in the question before `where`/`for`
    q_head = re.split(r"\b(?:where|for)\b", q_low, maxsplit=1)[0]
    agg_col_hit = _match_col_name(q_head)
    if agg_col_hit and agg_col_hit[0] == filter_col_idx and filter_op not in {">", "<", ">=", "<="}:
        agg_col_hit = None
    if agg_col_hit is None and wants_agg:
        col_stats = sp.get("columnStats") or {}
        for h_cand in col_stats:
            if h_cand in col_map and col_map[h_cand] != filter_col_idx:
                agg_col_hit = (col_map[h_cand], h_cand)
                break

    if wants_agg and agg_col_hit:
        a_idx, a_name = agg_col_hit
        vals = [
            fv for r in matched_rows
            if a_idx < len(r)
            for fv in (_parse_cell_float(r[a_idx]),)
            if fv is not None
        ]
        if not vals:
            return f"{name_prefix}no numeric values found for **`{a_name}`** in `{fname}` where {cond_label} (0 matching rows)."
        tot = sum(vals)
        avg = tot / len(vals)
        lines = [
            f"{name_prefix}for **`{a_name}`** in `{fname}` where {cond_label} (**{len(vals)} matching row(s)**):\n",
            f"- **Total (Sum):** **{tot:,.2f}**",
            f"- **Average:** **{avg:,.2f}**",
            f"- **Min / Max:** {min(vals):,.2f} / {max(vals):,.2f}",
        ]
        for r_i, mr in enumerate(matched_rows[:5], start=1):
            row_desc = " · ".join(f"{headers[i]}: {mr[i]}" for i in range(min(len(headers), len(mr), 6)) if mr[i])
            lines.append(f"- **Match {r_i}:** {row_desc}")
        return "\n".join(lines)

    # Otherwise list the matching rows cleanly
    if not matched_rows:
        return f"{name_prefix}no rows in `{fname}` matched {cond_label}."
    lines = [
        f"{name_prefix}found **{len(matched_rows)} row(s)** in `{fname}` where {cond_label}:\n"
    ]
    for r_i, mr in enumerate(matched_rows[:12], start=1):
        row_desc = " · ".join(f"**{headers[i]}:** {mr[i]}" for i in range(min(len(headers), len(mr), 7)) if mr[i])
        lines.append(f"- **Row {r_i}:** {row_desc}")
    if len(matched_rows) > 12:
        lines.append(f"- *…plus {len(matched_rows) - 12} more matching row(s).*")
    return "\n".join(lines)


def answer_deep_file_question(
    store: Any,
    question: str,
    focus_files: list[dict[str, Any]],
    full_texts: dict[str, dict[str, str]],
    name_prefix: str = "",
) -> Optional[str]:
    """
    Deeply answer natural-language questions about 1 or more files' context:
      - Full Deep File Context Dossier ("explain", "analyze", "inspect", "deep dive", "understand", "what is this file", "file context")
      - Page-specific PDF questions ("what is on page 2", "summarize page 3")
      - Slide deck questions ("what is on slide 2", "outline the slides", "how many slides")
      - Spreadsheet / CSV questions ("what columns are in...", "total/sum/average of...", "how many rows...")
      - Code understanding questions ("what functions/classes are in...", "what does this script do", "what libraries/imports", "any todos")
      - Archive questions ("what's inside the zip/archive", "how many files in the zip")
      - Action items / TODOs / Authors / URLs questions
    All output strictly uses bold headings and bullet lists — NEVER GFM pipe tables!
    """
    if not focus_files:
        return None
    q_low = (question or "").strip().lower()
    if not q_low:
        return None

    def _full_for(f: dict[str, Any]) -> str:
        fid = str(f.get("id") or "")
        return (
            full_texts.get(fid, {}).get("combined", "")
            or str(f.get("extractedText") or f.get("extracted_text") or "")
        )

    # 1. Page-specific PDF question ("what is on page 2", "summarize page 3")
    m_page = re.search(r"\bpage\s+(\d+)\b", q_low)
    if m_page:
        page_num = int(m_page.group(1))
        for f in focus_files[:3]:
            dossier = build_file_context_dossier(store, f, full_text_override=_full_for(f))
            page_texts = dossier.get("pageTexts") or []
            if page_texts:
                if 1 <= page_num <= len(page_texts):
                    p_txt = page_texts[page_num - 1].strip() or "(No extractable text on this page)"
                    chunks = split_sliding_chunks(p_txt, max_chunks=4, chunk_chars=240)
                    bullets = "\n".join(f"- **[Page {page_num}]** {c}" for c in (chunks or [p_txt[:300]]))
                    return f"{name_prefix}here is the content from **[Page {page_num}]** of `{f['name']}` ({dossier['pageCount']} page(s) total):\n\n{bullets}"
                return f"{name_prefix}`{f['name']}` only has **{len(page_texts)} indexed page(s)** (you asked for page {page_num})."

    # 2. Slide-specific or presentation outline question ("what is on slide 2", "outline the slides", "how many slides")
    m_slide = re.search(r"\bslide\s+(\d+)\b", q_low)
    if m_slide or re.search(r"\b(?:how\s+many\s+slides|outline\s+(?:the\s+)?slides|slide\s+outline|list\s+(?:the\s+)?slides)\b", q_low):
        for f in focus_files[:3]:
            dossier = build_file_context_dossier(store, f, full_text_override=_full_for(f))
            pres = dossier.get("presentation")
            if isinstance(pres, dict) and pres.get("slides"):
                slides = pres["slides"]
                if m_slide:
                    s_num = int(m_slide.group(1))
                    match_sl = next((s for s in slides if int(s.get("index", 0)) == s_num), None)
                    if match_sl:
                        return (
                            f"{name_prefix}here is **[Slide {s_num}]** of `{f['name']}` ({pres['slideCount']} slides total):\n\n"
                            f"- **Title:** **{match_sl['title']}**\n"
                            f"- **Content:** {match_sl['text']}"
                        )
                    return f"{name_prefix}`{f['name']}` has **{pres['slideCount']} slide(s)** (slide {s_num} was not found)."
                out_lines = [f"{name_prefix}`{f['name']}` has **{pres['slideCount']} slide(s)**:\n"]
                for sl in slides[:15]:
                    out_lines.append(f"- **[Slide {sl['index']}] {sl['title']}:** {sl['text'][:200]}")
                return "\n".join(out_lines)

    # 3. Spreadsheet / CSV structural, conditional (`where`/`for`), group-by & numeric column questions
    if re.search(
        r"\b(?:what\s+columns?|column\s+headers?|how\s+many\s+rows?|how\s+many\s+columns?|column\s+stats?|"
        r"(?:total|sum|average|avg|mean|min|max)\s+(?:of\s+)?[a-z0-9_ ]+|"
        r"(?:rows?|show\s+rows?|list\s+rows?)\s+(?:in\s+\S+\s+)?where\s+|"
        r"where\s+[a-z0-9_ ]+\s+(?:is|equals?|=|>|<|>=|<=|!=|contains?)|"
        r"(?:breakdown|break\s+down|group\s+by|grouped\s+by)\s+[a-z0-9_ ]+|"
        r"explain\s+this\s+(?:spreadsheet|csv|table|workbook))\b",
        q_low,
    ):
        for f in focus_files[:3]:
            body_f = _full_for(f)
            dossier = build_file_context_dossier(store, f, full_text_override=body_f)
            sp = dossier.get("spreadsheet")
            if isinstance(sp, dict) and (sp.get("headers") or sp.get("columnStats") or sp.get("totalRows")):
                cond_ans = _answer_conditional_spreadsheet_query(question, f, sp, body_f, name_prefix=name_prefix)
                if cond_ans:
                    return cond_ans
                lines = [
                    f"{name_prefix}here is the spreadsheet analysis for `{f['name']}` ({sp['totalRows']} rows × {sp['totalCols']} columns, sheets: {', '.join(sp['sheets'])}):\n"
                ]
                if sp.get("headers"):
                    lines.append("- **Column Headers:** " + ", ".join(f"`{h}`" for h in sp["headers"] if h))
                col_stats = sp.get("columnStats") or {}
                if col_stats:
                    matched_cols = [c for c in col_stats if c.lower() in q_low]
                    target_cols = matched_cols if matched_cols else list(col_stats.keys())
                    lines.append("\n**Column Statistics**")
                    for cn in target_cols[:10]:
                        st = col_stats[cn]
                        lines.append(
                            f"- **`{cn}`:** Total (sum) = **{st['sum']:,.2f}** · Average = **{st['avg']:,.2f}** · Min = {st['min']:,.2f} · Max = {st['max']:,.2f} ({st['count']} numeric rows)"
                        )
                if sp.get("sampleRows"):
                    lines.append("\n**Sample Rows**")
                    for r_idx, r_vals in enumerate(sp["sampleRows"][:4], start=1):
                        pairs = [
                            f"{sp['headers'][i]}: {v}" if i < len(sp.get("headers") or []) and sp["headers"][i] else v
                            for i, v in enumerate(r_vals[:6])
                            if v
                        ]
                        if pairs:
                            lines.append(f"- **Row {r_idx}:** " + " · ".join(pairs))
                return "\n".join(lines)

    # 4. Code understanding questions ("what functions/classes", "what does this script do", "how does this code work", "what libraries/imports", "any todos")
    if re.search(
        r"\b(?:what\s+(?:functions?|classes?|methods?|symbols?|libraries|imports?|packages?|modules?)|how\s+does\s+this\s+(?:code|script|file)\s+work|what\s+does\s+this\s+(?:code|script)\s+do|any\s+(?:todos?|fixmes?))\b",
        q_low,
    ):
        code_files = [
            f for f in focus_files[:4]
            if str(f.get("modality") or "").lower() == "code"
            or Path(str(f.get("name") or "")).suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".c", ".cpp", ".sh", ".php"}
        ]
        if code_files:
            out_blocks: list[str] = []
            for f in code_files[:3]:
                body = _full_for(f)
                dossier = build_file_context_dossier(store, f, full_text_override=body)
                cd = dossier.get("code") or {}
                b_lines = [
                    f"**Code Analysis:** `{f['name']}` (`{cd.get('language', 'code')}`, {cd.get('lineCount', 0)} lines)"
                ]
                if cd.get("docstrings"):
                    b_lines.append(f"- **Purpose / Docstring:** {cd['docstrings'][0]}")
                if cd.get("codeSymbols"):
                    b_lines.append("- **Defined Functions & Classes:** " + ", ".join(f"`{s}`" for s in cd["codeSymbols"][:12]))
                else:
                    b_lines.append("- **Defined Functions & Classes:** *(none detected)*")
                if cd.get("imports"):
                    b_lines.append("- **Imported Libraries / Modules:** " + ", ".join(f"`{imp}`" for imp in cd["imports"][:12]))
                if cd.get("todos"):
                    for td in cd["todos"][:6]:
                        b_lines.append(f"- **TODO / FIXME:** `{td}`")
                for tk in (dossier.get("keyTakeaways") or [])[:2]:
                    anchor = _find_passage_anchor(tk, dossier, body)
                    b_lines.append(f"- **Context:** {anchor}`{tk[:180]}`")
                out_blocks.append("\n".join(b_lines))
            return f"{name_prefix}" + "\n\n".join(out_blocks)

    # 5. Archive inspection questions ("what's inside the zip/archive", "how many files in the zip")
    if re.search(r"\b(?:inside\s+the\s+(?:zip|archive|tar)|what(?:'s|\s+is)\s+in\s+the\s+(?:zip|archive)|how\s+many\s+files\s+in\s+the\s+(?:zip|archive)|archive\s+contents?)\b", q_low):
        for f in focus_files[:3]:
            dossier = build_file_context_dossier(store, f, full_text_override=_full_for(f))
            ar = dossier.get("archive")
            if isinstance(ar, dict):
                lines = [
                    f"{name_prefix}`{f['name']}` contains **{ar['totalEntries']} entries** (uncompressed: **{ar['totalSizeHuman']}**):\n"
                ]
                if ar.get("extBreakdown"):
                    bd = ", ".join(f"`.{k}` ({v})" for k, v in sorted(ar["extBreakdown"].items(), key=lambda x: -x[1])[:8])
                    lines.append(f"- **File types:** {bd}")
                for e in (ar.get("topEntries") or [])[:12]:
                    lines.append(f"- `{e['name']}` ({_fmt_bytes(e.get('sizeBytes', 0))})")
                return "\n".join(lines)

    # 6. Action items / TODOs / Authors / Contacts / URLs across files
    fact_ans = extract_factoid_answer(question, focus_files, full_texts)
    if fact_ans:
        return f"{name_prefix}here is what I extracted from your file(s):\n\n{fact_ans}"

    # 7. Deep File Context Dossier ("explain", "analyze", "understand", "deep dive", "what is this file", "tell me everything about", "file context", "inspect")
    is_deep_dossier_q = bool(
        re.search(
            r"\b(?:explain|analyze|analyse|inspect|deep\s+dive|understand|tell\s+me\s+everything|file\s+context|context\s+of|what\s+is\s+this\s+file|what\s+is\s+inside|full\s+profile|dossier|break\s+down)\b",
            q_low,
        )
    )
    if is_deep_dossier_q:
        dossiers = [
            _render_file_dossier_markdown(
                build_file_context_dossier(store, f, full_text_override=_full_for(f))
            )
            for f in focus_files[:3]
        ]
        if dossiers:
            header = f"{name_prefix}here is the complete context profile:\n\n" if name_prefix else ""
            return header + "\n\n---\n\n".join(dossiers)

    return None


def _normalize_lineage_stem(filename: str) -> str:
    """Normalize a filename stem to detect version lineage (`report_v1.docx`, `report_v2.docx`, `report_final.docx`)."""
    stem = Path(filename or "").stem.lower()
    stem = re.sub(r"^(?:19|20)\d{2}[-_][01]\d[-_][0-3]\d[-_ ]*", "", stem)
    stem = re.sub(r"^(?:copy\s+of\s+|dup_[a-z0-9_]+__)", "", stem)
    # Strip trailing version / status tokens repeatedly
    for _ in range(3):
        stem = re.sub(
            r"[\s_\-]*(?:v\d+(?:\.\d+)?|ver\d+|rev\d+|r\d+|draft\d*|final\d*|revised|updated|edited|clean|copy|\(\d+\)|new|old)$",
            "",
            stem,
            flags=re.IGNORECASE,
        ).strip(" _-.")
    return stem


def build_cross_file_knowledge_graph(
    store: Any,
    focus_file: dict[str, Any],
    all_files: list[dict[str, Any]],
    full_texts: Optional[dict[str, dict[str, str]]] = None,
) -> dict[str, Any]:
    """
    Build a cross-file knowledge graph linking `focus_file` to other files across the workspace by:
      1. Shared Entities (same merchant, invoice_number, author_or_person, email, or exact amount)
      2. Version Lineage (files sharing the same normalized base stem ordered chronologically v1 -> v2 -> final)
      3. Spreadsheet / Ledger Mentions (spreadsheets whose rows mention focus_file's invoice_number, merchant, or amount)
      4. Code Import / Reference Links (code files importing focus_file's module stem or vice versa)
    """
    fid = str(focus_file.get("id") or "")
    fname = str(focus_file.get("name") or "")
    f_ext = Path(fname).suffix.lower()
    f_stem = Path(fname).stem.lower()
    f_base_stem = _normalize_lineage_stem(fname)

    ft_map = full_texts or {}
    if store is not None and fid and fid not in ft_map:
        ft_map = {**ft_map, **get_full_texts_batch(store, [focus_file])}
    f_body = ft_map.get(fid, {}).get("combined", "") or str(focus_file.get("extractedText") or "")
    f_meta = extract_entities(f_body, fname, _meta_dict(focus_file))

    f_merch = str(f_meta.get("merchant") or "").strip().lower()
    f_inv = str(f_meta.get("invoice_number") or "").strip().lower()
    f_auth = str(f_meta.get("author_or_person") or "").strip().lower()
    f_email = str(f_meta.get("email") or "").strip().lower()
    f_amt = f_meta.get("amount")
    f_imports = {str(x).lower().split(".")[0] for x in (f_meta.get("imports") or []) if x}
    if not f_imports and str(focus_file.get("modality") or "") == "code" and f_body:
        f_imports = {str(x).lower().split(".")[0] for x in _extract_code_deep_meta(f_body).get("imports", []) if x}

    shared_entities: list[dict[str, Any]] = []
    version_lineage: list[dict[str, Any]] = []
    spreadsheet_mentions: list[dict[str, Any]] = []
    code_links: list[dict[str, Any]] = []
    connected_files: list[dict[str, Any]] = []
    seen_conn_ids: set[str] = set()

    # Load full texts for spreadsheets & code peers if needed
    sheet_And_code_peers = [
        p for p in all_files
        if str(p.get("id") or "") != fid
        and (
            Path(str(p.get("name") or "")).suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}
            or str(p.get("modality") or "") == "code"
            or len(_normalize_lineage_stem(str(p.get("name") or ""))) >= 3
        )
    ]
    if store is not None and sheet_And_code_peers:
        missing_peers = [p for p in sheet_And_code_peers[:80] if str(p.get("id") or "") not in ft_map]
        if missing_peers:
            ft_map = {**ft_map, **get_full_texts_batch(store, missing_peers)}

    def _add_connected(peer: dict[str, Any], relation: str, detail: str) -> None:
        pid = str(peer.get("id") or "")
        if not pid or pid == fid or pid in seen_conn_ids:
            return
        seen_conn_ids.add(pid)
        connected_files.append({
            "fileId": pid,
            "fileName": str(peer.get("name") or ""),
            "title": str(peer.get("title") or peer.get("name") or ""),
            "relation": relation,
            "detail": detail,
            "file": peer,
        })

    # Collect version lineage candidates (including focus_file itself for chronological chain)
    lineage_peers: list[dict[str, Any]] = []
    if len(f_base_stem) >= 3:
        for peer in all_files:
            p_name = str(peer.get("name") or "")
            if _normalize_lineage_stem(p_name) == f_base_stem:
                lineage_peers.append(peer)
    if len(lineage_peers) >= 2:
        lineage_peers.sort(key=lambda x: (str(x.get("createdDate") or ""), float(x.get("mtime") or 0), str(x.get("name") or "")))
        for idx, lp in enumerate(lineage_peers, start=1):
            version_lineage.append({
                "step": idx,
                "fileId": str(lp.get("id") or ""),
                "fileName": str(lp.get("name") or ""),
                "size": str(lp.get("size") or _fmt_bytes(int(lp.get("sizeBytes") or 0))),
                "date": str(lp.get("createdDate") or lp.get("date") or ""),
                "isFocus": str(lp.get("id") or "") == fid,
                "file": lp,
            })
            if str(lp.get("id") or "") != fid:
                _add_connected(lp, "Version Lineage", f"Step {idx} in `{f_base_stem}` version history ({lp.get('size', '')})")

    for peer in all_files:
        pid = str(peer.get("id") or "")
        if not pid or pid == fid:
            continue
        pname = str(peer.get("name") or "")
        p_ext = Path(pname).suffix.lower()
        p_stem = Path(pname).stem.lower()
        p_body = ft_map.get(pid, {}).get("combined", "") or str(peer.get("extractedText") or "")
        p_meta = extract_entities(p_body, pname, _meta_dict(peer)) if p_body else _meta_dict(peer)

        # 1. Shared Entities
        reasons: list[str] = []
        p_inv = str(p_meta.get("invoice_number") or "").strip().lower()
        p_merch = str(p_meta.get("merchant") or "").strip().lower()
        p_auth = str(p_meta.get("author_or_person") or "").strip().lower()
        p_email = str(p_meta.get("email") or "").strip().lower()
        p_amt = p_meta.get("amount")

        if f_inv and p_inv and f_inv == p_inv:
            reasons.append(f"Same Invoice/Ref # (`{p_meta.get('invoice_number')}`)")
        if f_merch and p_merch and (f_merch == p_merch or f_merch in p_merch or p_merch in f_merch):
            reasons.append(f"Same Merchant (**{p_meta.get('merchant')}**)")
        if f_auth and p_auth and f_auth == p_auth:
            reasons.append(f"Same Author (**{p_meta.get('author_or_person')}**)")
        if f_email and p_email and f_email == p_email:
            reasons.append(f"Same Email (`{p_meta.get('email')}`)")
        if isinstance(f_amt, (int, float)) and isinstance(p_amt, (int, float)) and f_amt > 0 and abs(float(f_amt) - float(p_amt)) < 0.01:
            reasons.append(f"Matching Amount ({f_meta.get('currency') or ''} {float(f_amt):,.2f})".strip())

        if reasons:
            detail_s = " · ".join(reasons)
            shared_entities.append({
                "fileId": pid,
                "fileName": pname,
                "reasons": reasons,
                "detail": detail_s,
                "file": peer,
            })
            _add_connected(peer, "Shared Entity", detail_s)

        # 2. Spreadsheet / Ledger Mentions
        if p_ext in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"} and p_body:
            p_low = p_body.lower()
            match_notes: list[str] = []
            if f_inv and len(f_inv) >= 4 and f_inv in p_low:
                match_notes.append(f"mentions invoice `{f_meta.get('invoice_number')}`")
            if f_merch and len(f_merch) >= 3 and f_merch in p_low:
                match_notes.append(f"mentions merchant **{f_meta.get('merchant')}**")
            if isinstance(f_amt, (int, float)) and f_amt > 0:
                amt_plain = f"{float(f_amt):.2f}".rstrip("0").rstrip(".")
                amt_2d = f"{float(f_amt):.2f}"
                if re.search(rf"\b{re.escape(amt_plain)}\b", p_low) or re.search(rf"\b{re.escape(amt_2d)}\b", p_low):
                    match_notes.append(f"records amount **{float(f_amt):,.2f}**")
            if match_notes:
                det_m = ", ".join(match_notes)
                spreadsheet_mentions.append({
                    "fileId": pid,
                    "fileName": pname,
                    "detail": det_m,
                    "file": peer,
                })
                _add_connected(peer, "Spreadsheet Ledger Link", det_m)

        # Also check reverse: if focus_file IS a spreadsheet and peer is an invoice/receipt mentioned inside focus_file!
        if f_ext in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"} and f_body:
            f_low = f_body.lower()
            rev_notes: list[str] = []
            if p_inv and len(p_inv) >= 4 and p_inv in f_low:
                rev_notes.append(f"invoice `{p_meta.get('invoice_number')}` recorded in `{fname}`")
            if p_merch and len(p_merch) >= 3 and p_merch in f_low:
                rev_notes.append(f"merchant **{p_meta.get('merchant')}** recorded in `{fname}`")
            if rev_notes:
                det_r = ", ".join(rev_notes)
                spreadsheet_mentions.append({
                    "fileId": pid,
                    "fileName": pname,
                    "detail": det_r,
                    "file": peer,
                })
                _add_connected(peer, "Recorded Receipt/Invoice", det_r)

        # 3. Code Import / Reference Links
        if str(focus_file.get("modality") or "") == "code" or str(peer.get("modality") or "") == "code":
            p_imports = {str(x).lower().split(".")[0] for x in (p_meta.get("imports") or []) if x}
            if not p_imports and p_body and str(peer.get("modality") or "") == "code":
                p_imports = {str(x).lower().split(".")[0] for x in _extract_code_deep_meta(p_body).get("imports", []) if x}
            if f_stem in p_imports:
                det_c = f"`{pname}` imports `{f_stem}`"
                code_links.append({"fileId": pid, "fileName": pname, "detail": det_c, "file": peer})
                _add_connected(peer, "Imported By", det_c)
            elif p_stem in f_imports:
                det_c = f"`{fname}` imports `{p_stem}`"
                code_links.append({"fileId": pid, "fileName": pname, "detail": det_c, "file": peer})
                _add_connected(peer, "Imports Module", det_c)

    return {
        "focusFile": focus_file,
        "sharedEntities": shared_entities,
        "versionLineage": version_lineage,
        "spreadsheetMentions": spreadsheet_mentions,
        "codeLinks": code_links,
        "connectedFiles": connected_files,
    }


def answer_cross_file_question(
    store: Any,
    question: str,
    focus_files: list[dict[str, Any]],
    all_files: list[dict[str, Any]],
    full_texts: dict[str, dict[str, str]],
    name_prefix: str = "",
) -> Optional[tuple[str, list[dict[str, Any]]]]:
    """
    Answer cross-file synthesis questions:
      1. Cross-File Invoice/Receipt vs. Spreadsheet Reconciliation ("reconcile", "which invoices are missing from...", "compare receipts against...")
      2. Cross-File Knowledge Graph & Version Lineage ("/graph", "what files are connected to...", "connect the dots for...", "version history of...", "lineage of...")
    Returns `(markdown_answer, cited_files_list)` or `None`.
    """
    q_low = (question or "").strip().lower()
    if not q_low or not all_files:
        return None

    # 1. Cross-File Invoice / Receipt vs. Spreadsheet Reconciliation
    is_reconcile = bool(
        re.search(
            r"\b(?:reconcile|which\s+(?:invoices?|receipts?|bills?)\s+are\s+(?:missing|not\s+in|unrecorded)|"
            r"missing\s+from\s+\S+\.(?:csv|xlsx|tsv)|compare\s+(?:all\s+)?(?:invoices?|receipts?)\s+(?:against|with|to)\s+)",
            q_low,
        )
    )
    if is_reconcile:
        spreadsheets = [
            f for f in all_files
            if Path(str(f.get("name") or "")).suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}
            or str((_meta_dict(f)).get("kind") or "") == "spreadsheet"
        ]
        if spreadsheets:
            # Prefer a spreadsheet explicitly named in the question
            target_sheet = next(
                (s for s in spreadsheets if str(s.get("name") or "").lower() in q_low or Path(str(s.get("name") or "")).stem.lower() in q_low),
                spreadsheets[0],
            )
            ft_all = {**full_texts, **get_full_texts_batch(store, [target_sheet, *all_files[:80]])} if store is not None else full_texts
            sheet_id = str(target_sheet.get("id") or "")
            sheet_body = (ft_all.get(sheet_id, {}).get("combined", "") or str(target_sheet.get("extractedText") or "")).lower()

            # Find all receipt / invoice / bill files in workspace
            fin_files: list[dict[str, Any]] = []
            seen_sha: set[str] = set()
            for f in all_files:
                if str(f.get("id") or "") == sheet_id:
                    continue
                if Path(str(f.get("name") or "")).suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}:
                    continue
                f_body = ft_all.get(str(f.get("id") or ""), {}).get("combined", "") or str(f.get("extractedText") or "")
                meta = extract_entities(f_body, str(f.get("name") or ""), _meta_dict(f))
                dt = str(meta.get("doc_type") or f.get("specimen") or "").lower()
                if dt in {"receipt", "invoice", "bill"} or meta.get("invoice_number") or (
                    meta.get("amount") and any(k in str(f.get("name") or "").lower() for k in ("inv", "rcpt", "receipt", "invoice", "bill"))
                ):
                    sha = str(f.get("sha256") or "")
                    if sha and sha in seen_sha:
                        continue
                    if sha:
                        seen_sha.add(sha)
                    fin_files.append({**f, "_ent": meta})

            if fin_files:
                matched_items: list[tuple[dict[str, Any], str]] = []
                missing_items: list[dict[str, Any]] = []
                missing_total = 0.0
                for ff in fin_files:
                    ent = ff["_ent"]
                    inv = str(ent.get("invoice_number") or "").strip()
                    merch = str(ent.get("merchant") or "").strip()
                    amt = ent.get("amount")
                    reasons: list[str] = []
                    if inv and len(inv) >= 4 and inv.lower() in sheet_body:
                        reasons.append(f"Ref `{inv}`")
                    if merch and len(merch) >= 3 and merch.lower() in sheet_body:
                        reasons.append(f"Merchant **{merch}**")
                    if isinstance(amt, (int, float)) and amt > 0:
                        a_plain = f"{float(amt):.2f}".rstrip("0").rstrip(".")
                        a_2d = f"{float(amt):.2f}"
                        if re.search(rf"\b{re.escape(a_plain)}\b", sheet_body) or re.search(rf"\b{re.escape(a_2d)}\b", sheet_body):
                            reasons.append(f"Amount **{float(amt):,.2f}**")
                    # Consider recorded if Invoice # matches OR (Merchant AND Amount match) OR (Invoice has no ref and Merchant/Amount matches)
                    is_recorded = bool(
                        (inv and inv.lower() in sheet_body)
                        or (len(reasons) >= 2)
                        or (not inv and len(reasons) == 1)
                    )
                    if is_recorded:
                        matched_items.append((ff, " · ".join(reasons)))
                    else:
                        missing_items.append(ff)
                        if isinstance(amt, (int, float)):
                            missing_total += float(amt)

                lines = [
                    f"{name_prefix}here is the **Cross-File Reconciliation** comparing **{len(fin_files)} invoice/receipt file(s)** against `{target_sheet['name']}`:\n",
                    f"**Recorded in `{target_sheet['name']}` ({len(matched_items)} matched)**",
                ]
                if matched_items:
                    for mf, rsn in matched_items[:12]:
                        ent = mf["_ent"]
                        cur = ent.get("currency") or ""
                        amt_s = f"{cur} {float(ent['amount']):,.2f}".strip() if isinstance(ent.get("amount"), (int, float)) else ""
                        lines.append(
                            f"- **`{mf['name']}`** ({ent.get('merchant') or mf.get('title')} · {amt_s}) — matched via {rsn}"
                        )
                else:
                    lines.append("- *(No invoices/receipts matched rows in this spreadsheet.)*")

                lines.append(
                    f"\n**Missing / Unrecorded in `{target_sheet['name']}` ({len(missing_items)} missing"
                    + (f" · Total unrecorded: **{missing_total:,.2f}**" if missing_total > 0 else "")
                    + ")**"
                )
                if missing_items:
                    for uf in missing_items[:12]:
                        ent = uf["_ent"]
                        cur = ent.get("currency") or ""
                        amt_s = f"{cur} {float(ent['amount']):,.2f}".strip() if isinstance(ent.get("amount"), (int, float)) else "amount N/A"
                        inv_s = f" · Ref `{ent['invoice_number']}`" if ent.get("invoice_number") else ""
                        lines.append(
                            f"- **`{uf['name']}`** — **{ent.get('merchant') or uf.get('title')}** (**{amt_s}**{inv_s}) is **NOT** in `{target_sheet['name']}`"
                        )
                else:
                    lines.append(f"- All {len(fin_files)} invoice/receipt file(s) are accounted for in `{target_sheet['name']}`!")

                cited = [target_sheet, *[m for m in missing_items[:6]], *[m for m, _ in matched_items[:4]]]
                return "\n".join(lines), cited

    # 2. Cross-File Knowledge Graph & Version Lineage
    is_graph_or_lineage = bool(
        re.search(
            r"\b(?:connect\s+the\s+dots|what\s+files\s+are\s+(?:connected|linked|related)\s+to|"
            r"connected\s+files\s+for|knowledge\s+graph\s+for|version\s+history\s+of|version\s+lineage\s+of|lineage\s+of|versions\s+of)\b",
            q_low,
        )
    )
    if is_graph_or_lineage and focus_files:
        f0 = focus_files[0]
        graph = build_cross_file_knowledge_graph(store, f0, all_files, full_texts)
        lines = [
            f"{name_prefix}here is the **Cross-File Knowledge Graph** for `{f0['name']}` (**{f0.get('title') or f0['name']}**):\n"
        ]
        any_section = False
        if graph["versionLineage"]:
            any_section = True
            lines.append(f"**Version Lineage ({len(graph['versionLineage'])} versions)**")
            chain_str = " → ".join(
                f"**`{v['fileName']}`**" if v["isFocus"] else f"`{v['fileName']}`"
                for v in graph["versionLineage"]
            )
            lines.append(f"- **Progression:** {chain_str}")
            for v in graph["versionLineage"]:
                focus_tag = " *(current file)*" if v["isFocus"] else ""
                lines.append(f"- **Step {v['step']}:** `{v['fileName']}` ({v['size']} · {v['date']}){focus_tag}")

        if graph["sharedEntities"]:
            any_section = True
            lines.append(f"\n**Shared Entities ({len(graph['sharedEntities'])} linked file(s))**")
            for se in graph["sharedEntities"][:10]:
                lines.append(f"- **`{se['fileName']}`:** {se['detail']}")

        if graph["spreadsheetMentions"]:
            any_section = True
            lines.append(f"\n**Spreadsheet & Ledger Links ({len(graph['spreadsheetMentions'])})**")
            for sm in graph["spreadsheetMentions"][:8]:
                lines.append(f"- **`{sm['fileName']}`:** {sm['detail']}")

        if graph["codeLinks"]:
            any_section = True
            lines.append(f"\n**Code Import & Module Links ({len(graph['codeLinks'])})**")
            for cl in graph["codeLinks"][:8]:
                lines.append(f"- **`{cl['fileName']}`:** {cl['detail']}")

        if not any_section:
            lines.append("- No direct entity, spreadsheet, code-import, or version-lineage links were found for this file yet.")

        cited = [f0, *[c["file"] for c in graph["connectedFiles"][:8]]]
        return "\n".join(lines), cited

    return None


def compare_files_rich(
    store: Any,
    f_a: dict[str, Any],
    f_b: dict[str, Any],
    name_prefix: str = "",
) -> tuple[str, list[str]]:
    """
    Rich multi-modal comparison of two files:
      - Metadata diff (format, size delta, date delta, folder, shelf, pages/dimensions)
      - Receipt/invoice entity diff (merchant, amount delta, date/due_date)
      - Real 512-d vector cosine similarity from SQLite `embedding_blob`
      - Full-text unified diff (no 600-char truncation!)
      - Optional Ollama synthesis (still zero pipe tables)
    Returns `(markdown_answer, trace_steps)`.
    """
    trace: list[str] = []
    full_map = get_full_texts_batch(store, [f_a, f_b], fallback_excerpt=True)
    text_a = full_map.get(str(f_a["id"]), {}).get("combined", "")
    text_b = full_map.get(str(f_b["id"]), {}).get("combined", "")
    trace.append(f"Loaded full text ({len(text_a)} chars vs {len(text_b)} chars)")

    # Compute real 512-d embedding cosine similarity from SQLite
    sim_pct: Optional[int] = None
    with store._lock:
        conn = store._connect()
        try:
            rows = conn.execute(
                "SELECT id, embedding_blob FROM files WHERE id IN (?, ?)",
                (str(f_a["id"]), str(f_b["id"])),
            ).fetchall()
            blobs = {str(r["id"]): r["embedding_blob"] for r in rows}
            ba, bb = blobs.get(str(f_a["id"])), blobs.get(str(f_b["id"]))
            if ba and bb:
                va = np.frombuffer(ba, dtype=np.float32)
                vb = np.frombuffer(bb, dtype=np.float32)
                if va.shape == vb.shape and va.size > 0:
                    na = float(np.linalg.norm(va))
                    nb = float(np.linalg.norm(vb))
                    if na > 1e-6 and nb > 1e-6:
                        cos = float(np.clip(np.dot(va, vb) / (na * nb), 0.0, 1.0))
                        sim_pct = int(round(cos * 100.0))
        finally:
            conn.close()

    size_a = int(f_a.get("sizeBytes") or 0)
    size_b = int(f_b.get("sizeBytes") or 0)
    size_delta = size_b - size_a
    delta_sign = "+" if size_delta > 0 else ("-" if size_delta < 0 else "")
    size_delta_str = f"{delta_sign}{_fmt_bytes(abs(size_delta))}" if size_delta != 0 else "identical size"

    meta_a = _meta_dict(f_a)
    meta_b = _meta_dict(f_b)

    lines: list[str] = [
        f"{name_prefix}**Comparison:** `{f_a['name']}` vs `{f_b['name']}`\n",
        "**Metadata & Structure**",
        f"- **File A (`{f_a['name']}`):** {f_a.get('format', '')} · {_fmt_bytes(size_a)} · {f_a.get('date', '')} · {f_a.get('folder', '')} · Shelf: {f_a.get('shelfName', '')}",
        f"- **File B (`{f_b['name']}`):** {f_b.get('format', '')} · {_fmt_bytes(size_b)} · {f_b.get('date', '')} · {f_b.get('folder', '')} · Shelf: {f_b.get('shelfName', '')}",
        f"- **Size delta:** {size_delta_str}" + (f" · **Semantic similarity:** {sim_pct}%" if sim_pct is not None else ""),
    ]

    # Image dimensions or document pages
    if f_a.get("modality") == "image" or f_b.get("modality") == "image":
        wa, ha = int(f_a.get("width") or 0), int(f_a.get("height") or 0)
        wb, hb = int(f_b.get("width") or 0), int(f_b.get("height") or 0)
        if (wa and ha) or (wb and hb):
            lines.append(f"- **Resolution:** {wa}×{ha}px vs {wb}×{hb}px")
    else:
        pa, pb = int(f_a.get("pageCount") or 0), int(f_b.get("pageCount") or 0)
        if pa or pb:
            lines.append(f"- **Page count:** {pa} page(s) vs {pb} page(s)")

    # Receipt / invoice entity deltas
    amt_a = meta_a.get("amount")
    amt_b = meta_b.get("amount")
    merch_a = str(meta_a.get("merchant") or "").strip()
    merch_b = str(meta_b.get("merchant") or "").strip()
    if merch_a or merch_b or amt_a is not None or amt_b is not None:
        lines.append("\n**Financial / Entity Comparison**")
        if merch_a or merch_b:
            lines.append(f"- **Merchant:** {merch_a or '—'} vs {merch_b or '—'}")
        if amt_a is not None or amt_b is not None:
            cur_a = str(meta_a.get("currency") or "").strip()
            cur_b = str(meta_b.get("currency") or "").strip()
            if isinstance(amt_a, (int, float)) and isinstance(amt_b, (int, float)):
                diff_amt = float(amt_b) - float(amt_a)
                sign_a = "+" if diff_amt > 0 else ("-" if diff_amt < 0 else "")
                lines.append(
                    f"- **Amount:** {cur_a} {amt_a:,.2f} vs {cur_b} {amt_b:,.2f} (delta: **{sign_a}{abs(diff_amt):,.2f}**)"
                )
            else:
                lines.append(f"- **Amount:** {cur_a} {amt_a if amt_a is not None else '—'} vs {cur_b} {amt_b if amt_b is not None else '—'}")
        due_a = str(meta_a.get("due_date") or "").strip()
        due_b = str(meta_b.get("due_date") or "").strip()
        if due_a or due_b:
            lines.append(f"- **Due date:** {due_a or '—'} vs {due_b or '—'}")

    # Full-text line diff
    lines_a = [ln.strip() for ln in clean_extracted_text(text_a, preserve_newlines=True).splitlines() if ln.strip()]
    lines_b = [ln.strip() for ln in clean_extracted_text(text_b, preserve_newlines=True).splitlines() if ln.strip()]
    diff_lines = [
        ln for ln in difflib.unified_diff(lines_a[:400], lines_b[:400], fromfile=f_a["name"], tofile=f_b["name"], lineterm="")
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    ]
    added = sum(1 for l in diff_lines if l.startswith("+"))
    removed = sum(1 for l in diff_lines if l.startswith("-"))

    lines.append("\n**Content Differences**")
    if str(f_a.get("sha256") or "") and f_a.get("sha256") == f_b.get("sha256"):
        lines.append("- **Byte-for-byte identical** (matching SHA-256 hash).")
    elif not lines_a and not lines_b:
        lines.append("- Neither file contains extracted text yet (run OCR if these are scans/images).")
    elif not diff_lines:
        lines.append(f"- Extracted text is identical across {len(lines_a)} line(s).")
    else:
        lines.append(f"- **Text diff:** +{added} added / -{removed} removed line(s) across full text.")
        for dl in diff_lines[:8]:
            prefix_lbl = "Added" if dl.startswith("+") else "Removed"
            lines.append(f"- **{prefix_lbl}:** `{dl[1:].strip()[:120]}`")
        if len(diff_lines) > 8:
            lines.append(f"- *…plus {len(diff_lines) - 8} more changed line(s).*")

    deterministic_md = "\n".join(lines)
    ollama_md = synthesize_comparison(store, f_a, f_b, text_a, text_b, deterministic_md)
    if ollama_md:
        trace.append("Enhanced comparison via local Ollama")
        return ollama_md, trace
    return deterministic_md, trace


def compute_financial_analytics(
    store: Any,
    files: list[dict[str, Any]],
    question: str = "",
    name_prefix: str = "",
) -> dict[str, Any]:
    """
    Full-corpus deduplicated financial analytics across ALL matching files (no 18-item cap!).
    Excludes exact SHA-256 duplicates transparently so totals are never double-counted.
    Computes per-currency Total / Avg / Min / Max + Merchant breakdown + Monthly breakdown + Due bills.
    """
    full_map = get_full_texts_batch(store, files)

    seen_sha: set[str] = set()
    dup_skipped = 0
    records: list[dict[str, Any]] = []

    for f in files:
        fid = str(f.get("id") or "")
        sha = str(f.get("sha256") or "")
        meta = _meta_dict(f)
        body = full_map.get(fid, {}).get("combined", "")

        # Extract numeric amount from entity metadata -> receiptFields -> full text regex
        val: Optional[float] = None
        cur = str(meta.get("currency") or "").strip().upper()
        if meta.get("amount") is not None:
            try:
                val = float(meta["amount"])
            except (TypeError, ValueError):
                val = None
        if val is None and isinstance(meta.get("receiptFields"), dict):
            raw_tot = str(meta["receiptFields"].get("total") or "")
            m_n = re.search(r"[\d,]+(?:\.\d{1,2})?", raw_tot)
            if m_n:
                try:
                    val = float(m_n.group(0).replace(",", ""))
                except ValueError:
                    val = None
        if val is None and body:
            m_amt = re.search(
                r"(?:total|amount\s+due|grand\s+total|paid)\s*[:\-]?\s*(PHP|USD|EUR|GBP|₱|\$|€|£)?\s*([\d,]+(?:\.\d{2})?)",
                body,
                re.IGNORECASE,
            )
            if not m_amt:
                m_amt = re.search(r"(PHP|USD|EUR|GBP|₱|\$|€|£)\s*([\d,]+\.\d{2})", body, re.IGNORECASE)
            if m_amt:
                if not cur and m_amt.group(1):
                    cur = m_amt.group(1).upper()
                try:
                    val = float(m_amt.group(2).replace(",", ""))
                except ValueError:
                    val = None

        if val is None or val <= 0:
            continue

        # Deduplicate exact SHA-256 copies so totals are never double-counted
        if sha:
            if sha in seen_sha:
                dup_skipped += 1
                continue
            seen_sha.add(sha)

        if not cur:
            if "₱" in body or "PHP" in body.upper():
                cur = "PHP"
            elif "€" in body or "EUR" in body.upper():
                cur = "EUR"
            elif "£" in body or "GBP" in body.upper():
                cur = "GBP"
            else:
                cur = "USD"
        sym = "₱" if cur in {"PHP", "₱"} else ("$" if cur in {"USD", "$"} else ("€" if cur in {"EUR", "€"} else ("£" if cur in {"GBP", "£"} else cur)))

        merchant = str(meta.get("merchant") or "").strip()
        if not merchant and isinstance(meta.get("receiptFields"), dict):
            merchant = str(meta["receiptFields"].get("merchant") or "").strip()
        if not merchant:
            merchant = str(f.get("title") or f.get("name") or "Unknown")

        date_str = str(meta.get("date") or f.get("createdDate") or f.get("date") or "")[:10]
        month_key = date_str[:7] if len(date_str) >= 7 and re.match(r"\d{4}-\d{2}", date_str) else "Undated"
        due_str = str(meta.get("due_date") or "")[:10]

        records.append({
            "file": f,
            "fileId": fid,
            "title": str(f.get("title") or f.get("name") or ""),
            "name": str(f.get("name") or ""),
            "amount": val,
            "symbol": sym,
            "currency": cur,
            "merchant": merchant,
            "date": date_str,
            "month": month_key,
            "due_date": due_str,
        })

    if not records:
        return {"records": [], "markdown": "", "duplicateSkipped": dup_skipped, "totalsBySymbol": {}}

    # Per-currency stats
    by_sym: dict[str, list[float]] = {}
    by_merchant: dict[tuple[str, str], tuple[float, int]] = {}
    by_month: dict[tuple[str, str], tuple[float, int]] = {}
    for rec in records:
        s = rec["symbol"]
        by_sym.setdefault(s, []).append(rec["amount"])
        mk = (rec["merchant"], s)
        prev_m = by_merchant.get(mk, (0.0, 0))
        by_merchant[mk] = (prev_m[0] + rec["amount"], prev_m[1] + 1)
        ym = (rec["month"], s)
        prev_y = by_month.get(ym, (0.0, 0))
        by_month[ym] = (prev_y[0] + rec["amount"], prev_y[1] + 1)

    totals_by_sym = {s: round(sum(vals), 2) for s, vals in by_sym.items()}
    sums_fmt = ", ".join(f"{sym}{tot:,.2f}" for sym, tot in totals_by_sym.items())
    dup_note = f" *(excluded {dup_skipped} exact duplicate copy/copies)*" if dup_skipped > 0 else ""

    lines: list[str] = []
    if len(records) == 1:
        r0 = records[0]
        f0 = r0["file"]
        ocr_snip = clean_extracted_text(full_map.get(r0["fileId"], {}).get("combined", ""))[:220]
        lines.append(
            f"{name_prefix}you spent **{sums_fmt}** at **{r0['merchant']}** (`{r0['name']}`, {r0['date'] or f0.get('date', '')}).{dup_note}"
        )
        if r0["due_date"]:
            lines.append(f"- **Due date:** {r0['due_date']}")
        if ocr_snip:
            lines.append(f"- **Extracted details:** {ocr_snip}")
    else:
        lines.append(
            f"{name_prefix}across **{len(records)} unique financial record(s)**, the total is **{sums_fmt}**:{dup_note}\n"
        )
        lines.append("**Summary Statistics**")
        for sym, vals in by_sym.items():
            tot = sum(vals)
            avg = tot / len(vals)
            lines.append(
                f"- **{sym} total:** **{sym}{tot:,.2f}** across {len(vals)} record(s) · "
                f"avg {sym}{avg:,.2f} · min {sym}{min(vals):,.2f} · max {sym}{max(vals):,.2f}"
            )

        # Top merchants
        top_merchants = sorted(by_merchant.items(), key=lambda x: x[1][0], reverse=True)[:6]
        if len(top_merchants) >= 1:
            lines.append("\n**By Merchant**")
            for (m_name, sym), (m_tot, m_cnt) in top_merchants:
                lines.append(f"- **{m_name}:** {sym}{m_tot:,.2f} ({m_cnt} item{'s' if m_cnt != 1 else ''})")

        # Monthly breakdown if spanning multiple months
        dated_months = [(ym, v) for ym, v in by_month.items() if ym[0] != "Undated"]
        if len(dated_months) >= 2:
            lines.append("\n**By Month**")
            for (mo, sym), (mo_tot, mo_cnt) in sorted(dated_months, key=lambda x: x[0][0], reverse=True)[:6]:
                lines.append(f"- **{mo}:** {sym}{mo_tot:,.2f} ({mo_cnt} item{'s' if mo_cnt != 1 else ''})")

        # Top individual records
        lines.append("\n**Matching Records**")
        for rec in sorted(records, key=lambda r: r["amount"], reverse=True)[:8]:
            due_part = f" · due {rec['due_date']}" if rec["due_date"] else ""
            lines.append(
                f"- **{rec['title']}** (`{rec['name']}`) — {rec['symbol']}{rec['amount']:,.2f} ({rec['date'] or 'undated'}{due_part})"
            )
        if len(records) > 8:
            lines.append(f"- *…plus {len(records) - 8} more financial record(s).*")

    return {
        "records": records,
        "markdown": "\n".join(lines),
        "duplicateSkipped": dup_skipped,
        "totalsBySymbol": totals_by_sym,
    }


def format_due_payments_answer(
    files: list[dict[str, Any]],
    due_label: str = "Due soon",
    name_prefix: str = "",
) -> str:
    """Format upcoming & overdue bills cleanly without pipe tables."""
    today = datetime.now().date()
    if not files:
        return f"{name_prefix}no bills or payments found matching **{due_label}**."

    lines = [f"{name_prefix}here are **{len(files)} payment(s)** ({due_label}):\n"]
    for f in files[:20]:
        meta = _meta_dict(f)
        due = str(meta.get("due_date") or "")[:10]
        status = ""
        if due:
            try:
                due_d = datetime.strptime(due, "%Y-%m-%d").date()
                delta = (due_d - today).days
                if delta < 0:
                    status = f"**Overdue by {abs(delta)}d**"
                elif delta == 0:
                    status = "**Due today**"
                else:
                    status = f"Due in {delta}d"
            except ValueError:
                status = f"Due {due}"
        amt = meta.get("amount")
        cur = str(meta.get("currency") or "").strip()
        amt_str = f" · {cur} {amt:,.2f}".strip() if isinstance(amt, (int, float)) else ""
        merch = str(meta.get("merchant") or f.get("title") or f.get("name") or "")
        lines.append(f"- **{merch}** (`{f['name']}`) — {status} ({due}){amt_str}")
    return "\n".join(lines)


def format_timeline_answer(
    files: list[dict[str, Any]],
    window_label: str = "",
    name_prefix: str = "",
) -> str:
    """Group files chronologically by date for timeline/activity queries."""
    if not files:
        return f"{name_prefix}no files matched that timeline."

    ordered = sorted(files, key=lambda f: float(f.get("mtime") or 0), reverse=True)[:35]
    by_day: dict[str, list[dict[str, Any]]] = {}
    for f in ordered:
        mtime = float(f.get("mtime") or 0)
        day_lbl = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d (%a)") if mtime > 0 else str(f.get("createdDate") or "Undated")
        by_day.setdefault(day_lbl, []).append(f)

    win_note = f" ({window_label})" if window_label else ""
    lines = [f"{name_prefix}here is your chronological activity timeline{win_note} across **{len(files)} file(s)**:\n"]
    for day, items in list(by_day.items())[:10]:
        lines.append(f"**{day}** ({len(items)} file{'s' if len(items) != 1 else ''})")
        for it in items[:6]:
            lines.append(f"- `{it['name']}` ({it.get('format', '')}, {it.get('size', '')} · {it.get('folder', '')})")
        if len(items) > 6:
            lines.append(f"- *…plus {len(items) - 6} more on this date.*")
    return "\n".join(lines)


def compute_scoped_workspace_stats(
    scoped_files: list[dict[str, Any]],
    all_lookalikes: list[dict[str, Any]],
    scope_folder: Optional[str] = None,
) -> dict[str, Any]:
    """
    Compute accurate storage, modality, shelf, and duplicate stats strictly within
    `scoped_files` (never leaking full-workspace numbers when `scope_folder` is active!).
    """
    scoped_ids = {str(f.get("id") or "") for f in scoped_files}
    total_files = len(scoped_files)
    total_bytes = sum(int(f.get("sizeBytes") or 0) for f in scoped_files)

    by_mod: dict[str, dict[str, Any]] = {}
    by_shelf: dict[str, dict[str, Any]] = {}
    for f in scoped_files:
        mod = str(f.get("modality") or "document")
        sb = int(f.get("sizeBytes") or 0)
        bm = by_mod.setdefault(mod, {"modality": mod, "count": 0, "bytes": 0})
        bm["count"] += 1
        bm["bytes"] += sb

        sh_name = str(f.get("shelfName") or "Quiet Tray")
        bs = by_shelf.setdefault(sh_name, {"shelfName": sh_name, "count": 0, "bytes": 0})
        bs["count"] += 1
        bs["bytes"] += sb

    if scope_folder:
        rel_dups = [
            p for p in (all_lookalikes or [])
            if not p.get("resolved")
            and ((p.get("copy") or {}).get("id") in scoped_ids or (p.get("keep") or {}).get("id") in scoped_ids)
        ]
    else:
        rel_dups = [p for p in (all_lookalikes or []) if not p.get("resolved")]

    reclaim_mb = round(sum(float(p.get("saved_mb") or 0.0) for p in rel_dups), 2)
    return {
        "totalFiles": total_files,
        "totalBytes": total_bytes,
        "totalHuman": _fmt_bytes(total_bytes),
        "byModality": sorted(by_mod.values(), key=lambda x: (-x["count"], -x["bytes"])),
        "byShelf": sorted(by_shelf.values(), key=lambda x: (-x["bytes"], -x["count"])),
        "duplicatePairsCount": len(rel_dups),
        "reclaimableMB": reclaim_mb,
    }
