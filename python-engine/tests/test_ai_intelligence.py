"""
Comprehensive 65+ Test Suite for Good Shelves Ask AI Master Improvement Plan (Phases 1-3).

Covers all 12 Pillars and all 11 critical bug fixes:
  1. Unified FileQuerySpec AST (`query_parser.py`): multi-type union, dynamic folders/shelves,
     past + future/due temporal windows, 7-day "last week", stale/older-than, quarters,
     bare-year guard, size/empty, entities, tags/untagged/pinned/OCR/dups, negations, sort/top-K.
  2. Eliminating 600-char blindness across search, action resolution, compare, and Q&A.
  3. All 7 Proactive Insight prompts working end-to-end in Ask AI.
  4. Move / copy / organize / auto-sort (`moveMap`), depth-4 fuzzy folder resolution, collision previews,
     and clean behavioral learning (never learns bare extensions).
  5. Rename & batch rename (`renamer.py`): entity tokens `{merchant}`/`{amount}`/`{date}`,
     new tokens, padded `{seq:02d}`, case/find-replace/regex/strip transforms, content-based
     smart rename (`force=True`), intra-batch collision simulation.
  6. Delete / trash / restore / archive: fixed restore fallback (NEVER `or trashed`!),
     pinned files unchecked (`selectedByDefault: False`) on trash, `archive_files`, `restore_archive`, `empty_trash`.
  7. Smart deduplicator (`deduplicator.py`): Composite Keep-Score, `keep_strategy`, `keepReason`, `swap_lookalike`.
  8. Tags, shelves & saved searches: `auto_tag` (`tagMap`), `rename_tag`, `clear_tags`,
     `rename_shelf`, `delete_shelf`, `pin_shelf`/`unpin_shelf`, `unshelve`, `save_search`.
  9. Multi-page PDF OCR (up to 12 pages), self-healing on-demand OCR, `reindex_images`,
     `rescan_folders`, `verify_index`, `file_convert`, `open_file`/`reveal_file`.
 10. Deep Q&A, MMR summary, factoids, multi-modal compare (512-d cosine + entity diff + NO pipe tables!),
     full-corpus deduplicated financial analytics, timelines, and scope-aware workspace stats.
 11. Multi-step compound chaining: Query-as-Step-0, elided object propagation,
     `VirtualWorkspaceSimulator`, strictly unique `proposedAction.items`, `create_shelf` -> `shelve`,
     and atomic composite `multi_step_tx` Undo + Redo.
 12. Conversational memory, rich anaphora (1st..10th, ranges, attributes, subtractive),
     full pending-plan editing, word-boundary-safe history extraction, 90-day memory decay, `forget_memory`.
 13. Local Ollama hybrid brain (`llm_router.py`): `ollama_model` in `get_settings()`, `/api/tags` discovery,
     30s circuit breaker, `/api/chat` JSON-schema planner + allowlist validation, pipe-table stripping,
     and grounded synthesis (100% mocked, zero external network).
 14. Real undo logs across all reversible actions, top-3 choices cap, and structured partial-failure reporting.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from ai_agent import (
    VirtualWorkspaceSimulator,
    extract_conversation_context,
    extract_step0_query_clause,
    find_matching_memory_entries,
    load_ai_memory,
    merge_unique_plan_items,
    parse_rename_format_request,
    propagate_elided_object,
    resolve_anaphora_subset,
    split_compound_commands,
    upsert_ai_memory,
)
from deduplicator import find_lookalike_pairs, pick_keep_and_copy
from llm_router import (
    OllamaRouter,
    _strip_pipe_tables,
    get_ollama_router,
    plan_with_ollama,
    reset_ollama_router,
    synthesize_qa_answer,
    validate_ollama_plan,
)
from qa_engine import (
    compare_files_rich,
    compute_financial_analytics,
    compute_scoped_workspace_stats,
    extract_factoid_answer,
    format_due_payments_answer,
    format_timeline_answer,
    split_sliding_chunks,
)
from query_parser import (
    apply_sort_and_limit,
    matches_query_spec,
    parse_due_window,
    parse_file_query,
    parse_size_bytes,
    parse_temporal_spec,
)
from renamer import (
    format_rename_template,
    simulate_batch_rename_collisions,
    synthesize_content_rename,
)
from reranker import detect_rerank_intent, rerank_candidates
from vector_store import VectorStore


def _make_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[VectorStore, Path]:
    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    st = VectorStore(db_path=tmp_path / "intel.db")
    st.update_setting("ollama_enabled", False)
    return st, ws


def _create_sample_png(path: Path, color: tuple[int, int, int] = (20, 120, 200), size: tuple[int, int] = (120, 90)) -> None:
    from PIL import Image

    img = Image.new("RGB", size, color=color)
    img.save(path)


def _create_multipage_pdf(path: Path, page_count: int = 3) -> None:
    import pymupdf

    doc = pymupdf.open()
    for i in range(page_count):
        page = doc.new_page(width=200, height=200)
        page.draw_rect(pymupdf.Rect(10, 10, 190, 190), color=(0, 0, 0))
    doc.save(str(path))
    doc.close()


# ============================================================================
# PILLAR 1: Unified FileQuerySpec AST (`query_parser.py`)
# ============================================================================


def test_qp_multi_type_union_images_and_pdfs() -> None:
    spec = parse_file_query("all images and PDFs")
    assert "image" in spec.modalities
    assert "pdf" in spec.exts
    assert spec.type_union is True
    assert matches_query_spec({"id": "1", "modality": "image", "format": "png"}, spec)
    assert matches_query_spec({"id": "2", "modality": "document", "format": "pdf"}, spec)
    assert not matches_query_spec({"id": "3", "modality": "document", "format": "txt"}, spec)


def test_qp_multi_format_word_or_spreadsheets() -> None:
    spec = parse_file_query("find Word docs or spreadsheets")
    assert "docx" in spec.exts and "xlsx" in spec.exts and "csv" in spec.exts
    assert matches_query_spec({"id": "1", "modality": "document", "format": "docx"}, spec)
    assert matches_query_spec({"id": "2", "modality": "document", "format": "xlsx"}, spec)
    assert not matches_query_spec({"id": "3", "modality": "document", "format": "pdf"}, spec)


def test_qp_last_week_is_exact_7_days() -> None:
    ref_now = datetime(2026, 6, 15, 14, 0, 0)
    t = parse_temporal_spec("files from last week", now_dt=ref_now)
    assert t is not None
    assert t["start_date"] == "2026-06-08"
    assert t["end_date"] == "2026-06-15"


def test_qp_older_than_and_stale() -> None:
    ref_now = datetime(2026, 6, 15, 12, 0, 0)
    t_old = parse_temporal_spec("screenshots older than 60 days", now_dt=ref_now)
    assert t_old is not None and t_old["end_date"] == "2026-04-16"
    t_stale = parse_temporal_spec("my stale downloads", now_dt=ref_now)
    assert t_stale is not None and t_stale["end_date"] == "2026-05-16"


def test_qp_quarters_q1_to_q4() -> None:
    ref_now = datetime(2026, 6, 15, 12, 0, 0)
    t_q1 = parse_temporal_spec("reports from Q1 2026", now_dt=ref_now)
    assert t_q1 is not None and t_q1["start_date"] == "2026-01-01" and t_q1["end_date"] == "2026-03-31"
    t_q4 = parse_temporal_spec("Q4 2025 invoices", now_dt=ref_now)
    assert t_q4 is not None and t_q4["start_date"] == "2025-10-01" and t_q4["end_date"] == "2025-12-31"


def test_qp_bare_year_guard_does_not_hijack_invoice_or_amount() -> None:
    ref_now = datetime(2026, 6, 15, 12, 0, 0)
    assert parse_temporal_spec("invoice 2025", now_dt=ref_now) is None
    assert parse_temporal_spec("receipts over 2000", now_dt=ref_now) is None
    assert parse_temporal_spec("order #2024", now_dt=ref_now) is None
    assert parse_temporal_spec("PHP 2025 total", now_dt=ref_now) is None
    # Explicit temporal preposition still parses cleanly!
    t_ok = parse_temporal_spec("receipts from 2025", now_dt=ref_now)
    assert t_ok is not None and t_ok["start_date"] == "2025-01-01"


def test_qp_future_due_windows() -> None:
    ref_now = datetime(2026, 6, 15, 12, 0, 0)
    dw = parse_due_window("show me payments due in the next 7 days", now_dt=ref_now)
    assert dw is not None
    assert dw["end_date"] == "2026-06-22"
    assert dw["start_date"] == "2026-06-08"

    dw_over = parse_due_window("show overdue bills", now_dt=ref_now)
    assert dw_over is not None and dw_over["overdue_only"] is True


def test_qp_size_empty_entities_negations_sort_topk() -> None:
    spec = parse_file_query("top 5 largest PDFs over 2MB except pinned")
    assert spec.top_k == 5
    assert spec.sort_by == "largest"
    assert "pdf" in spec.exts
    assert spec.min_size_bytes == 2 * 1024 * 1024
    assert spec.exclude_pinned is True

    spec_emp = parse_file_query("find empty files")
    assert spec_emp.empty_only is True
    assert matches_query_spec({"id": "1", "sizeBytes": 0}, spec_emp)
    assert not matches_query_spec({"id": "2", "sizeBytes": 12}, spec_emp)

    spec_ent = parse_file_query("receipts from Starbucks over $50")
    assert spec_ent.entity_filters.get("merchant") == "starbucks"
    assert spec_ent.entity_filters.get("amount_op") == ">"
    assert spec_ent.entity_filters.get("amount_value") == 50.0


def test_qp_untagged_missing_ocr_and_dynamic_folders() -> None:
    fake_state = {
        "watchedFolders": [],
        "pinnedFolders": [],
        "files": [{"folderFull": "/tmp/ws/TaxArchive"}],
        "shelves": [{"name": "Tax 2026"}],
    }
    spec = parse_file_query("untagged files in TaxArchive without ocr", state=fake_state)
    assert spec.untagged_only is True
    assert spec.has_ocr is False
    assert "taxarchive" in spec.folder_hints
    assert matches_query_spec(
        {"id": "1", "folderFull": "/tmp/ws/TaxArchive", "tags": [], "ocrText": ""},
        spec,
    )
    assert not matches_query_spec(
        {"id": "2", "folderFull": "/tmp/ws/TaxArchive", "tags": ["tax"], "ocrText": ""},
        spec,
    )
    assert not matches_query_spec(
        {"id": "3", "folderFull": "/tmp/ws/TaxArchive", "tags": [], "ocrText": "already scanned"},
        spec,
    )


def test_qp_apply_sort_and_limit() -> None:
    files = [
        {"id": "a", "sizeBytes": 100, "mtime": 10},
        {"id": "b", "sizeBytes": 500, "mtime": 30},
        {"id": "c", "sizeBytes": 300, "mtime": 20},
    ]
    spec = parse_file_query("top 2 largest files")
    res = apply_sort_and_limit(files, spec)
    assert [f["id"] for f in res] == ["b", "c"]


# ============================================================================
# PILLAR 1/8: Eliminate 600-char Blindness (Full-Text Search, Action & Q&A)
# ============================================================================


def test_full_text_past_600_chars_found_in_search_action_and_qa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    long_doc = ws / "deep_contract.txt"
    filler = "Standard introductory boilerplate paragraph for archival contract review. " * 20  # > 1400 chars
    secret_tail = "\nSection 12: Exclusive termination penalty is USD 4,750.00 payable to Zylophone Logistics.\nInvoice No: INV-998877\n"
    long_doc.write_text(filler + secret_tail, encoding="utf-8")
    st.complete_setup([str(ws)], user_name="alice")

    # Confirm state["files"] cache is truncated at 600 chars, yet search/QA/action all see the tail!
    st_file = st.get_all_state()["files"][0]
    assert "Zylophone" not in st_file["extractedText"]

    # 1. Strict phrase search finds text past char 1400
    s_res = st.search_semantic('files containing "Zylophone Logistics"')
    assert len(s_res["results"]) == 1
    assert s_res["results"][0]["name"] == "deep_contract.txt"

    # 2. Action target resolver finds text past char 1400
    act_res = st.ask_your_shelves('tag files containing "Zylophone Logistics" with #legal')
    assert act_res["proposedAction"] is not None
    assert act_res["proposedAction"]["actionType"] == "tag"
    assert [it["fileName"] for it in act_res["proposedAction"]["items"]] == ["deep_contract.txt"]

    # 3. Q&A factoid & passage retrieval finds the tail past char 1400
    qa_res = st.ask_your_shelves("what is the invoice number for Zylophone Logistics?")
    assert "INV-998877" in qa_res["answer"]


# ============================================================================
# ALL 7 PROACTIVE INSIGHTS WORKING END-TO-END IN ASK AI
# ============================================================================


def test_proactive_insight_1_run_ocr_on_all_images_and_pdfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    img_p = ws / "scan_01.png"
    pdf_p = ws / "scanned_doc.pdf"
    _create_sample_png(img_p)
    _create_multipage_pdf(pdf_p, page_count=2)
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("run ocr on all images and PDFs")
    act = res["proposedAction"]
    assert act is not None
    assert act["actionType"] == "run_ocr"
    names = {it["fileName"] for it in act["items"]}
    assert "scan_01.png" in names
    assert "scanned_doc.pdf" in names  # Previously dropped PDFs!


def test_proactive_insight_2_payments_due_in_next_7_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    due_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    far_date = (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")
    (ws / "electric_bill.txt").write_text(f"Utility Bill\nMerchant: Meralco\nAmount Due: PHP 3200.00\nDue Date: {due_date}\n", encoding="utf-8")
    (ws / "annual_bill.txt").write_text(f"Utility Bill\nMerchant: FiberNet\nAmount Due: PHP 1500.00\nDue Date: {far_date}\n", encoding="utf-8")
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("show me payments due in the next 7 days")
    assert res["intentMode"] == "due_soon"
    assert "electric_bill.txt" in res["answer"] or "Meralco" in res["answer"]
    assert "FiberNet" not in res["answer"]
    assert len(res["citations"]) == 1


def test_proactive_insight_3_reindex_images_with_active_vision_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    _create_sample_png(ws / "photo_a.png")
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("reindex images with the active vision model")
    act = res["proposedAction"]
    assert act is not None
    assert act["actionType"] == "reindex_images"


def test_proactive_insight_4_organize_stale_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    dl = ws / "Downloads"
    dl.mkdir()
    old_f = dl / "old_receipt.txt"
    fresh_f = dl / "fresh_note.txt"
    old_f.write_text("Official Receipt Coffee Shop Total USD 15.00", encoding="utf-8")
    fresh_f.write_text("Fresh meeting notes from today", encoding="utf-8")
    old_ts = time.time() - 45 * 86400
    os.utime(old_f, (old_ts, old_ts))
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("organize my stale downloads")
    act = res["proposedAction"]
    assert act is not None
    assert act["actionType"] == "move"
    assert "moveMap" in act["payload"]
    names = [it["fileName"] for it in act["items"]]
    assert "old_receipt.txt" in names
    assert "fresh_note.txt" not in names


def test_proactive_insight_5_organize_uncategorized_files_into_shelves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    f1 = ws / "receipt_tray.txt"
    f1.write_text("Official Receipt StoreMart Total USD 42.00", encoding="utf-8")
    st.complete_setup([str(ws)])
    # Put f1 into Quiet Tray (uncategorized)
    fid = st.get_all_state()["files"][0]["id"]
    with st._lock:
        conn = st._connect()
        conn.execute("UPDATE files SET shelf_id = 'uncategorized', shelf_name = 'Quiet Tray' WHERE id = ?", (fid,))
        conn.commit()
        conn.close()
    st._invalidate_cache()

    res = st.ask_your_shelves("organize uncategorized files into shelves")
    act = res["proposedAction"]
    assert act is not None
    assert act["actionType"] == "shelve"
    assert fid in (act["payload"].get("shelfMap") or {})

    # Execute & undo!
    exec_r = st.execute_ai_action({**act["payload"], "actionType": "shelve", "fileIds": [fid]})
    assert exec_r["ok"] is True and exec_r["count"] == 1
    assert st.get_file_by_id(fid)["shelfId"] != "uncategorized"
    st.undo_action()
    assert st.get_file_by_id(fid)["shelfId"] == "uncategorized"


def test_proactive_insight_6_and_7_duplicates_and_rename_messy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "IMG_1234.txt").write_text("Quarterly Financial Forecast 2026\nRevenue targets", encoding="utf-8")
    (ws / "exact_a.txt").write_text("Exact duplicate body content repeated twice.", encoding="utf-8")
    (ws / "exact_a (1).txt").write_text("Exact duplicate body content repeated twice.", encoding="utf-8")
    st.complete_setup([str(ws)])

    dup_res = st.ask_your_shelves("show me all the duplicates")
    assert dup_res["proposedAction"] is not None
    assert dup_res["proposedAction"]["actionType"] == "archive_duplicates"

    ren_res = st.ask_your_shelves("rename messy files")
    assert ren_res["proposedAction"] is not None
    assert ren_res["proposedAction"]["actionType"] == "rename_batch"


# ============================================================================
# PILLAR 2: Depth-4 Fuzzy Folder Resolution, Collision Previews & Clean Learning
# ============================================================================


def test_fuzzy_nested_and_slug_folder_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    nested = ws / "2026" / "Finance" / "Tax 2026"
    nested.mkdir(parents=True)
    (ws / "note.txt").write_text("Tax planning notes", encoding="utf-8")
    st.complete_setup([str(ws)])
    state = st.get_all_state()

    # Nested relative path
    r_nested = st._resolve_folder_candidate("2026/Finance", state["files"], state["watchedFolders"])
    assert r_nested and r_nested["path"].endswith("2026/Finance")
    # Typo match ("Finace" -> "Finance")
    r_typo = st._resolve_folder_candidate("Finace", state["files"], state["watchedFolders"])
    assert r_typo and Path(r_typo["path"]).name == "Finance"
    # Collapsed slug ("tax2026" -> "Tax 2026")
    r_slug = st._resolve_folder_candidate("tax2026", state["files"], state["watchedFolders"])
    assert r_slug and Path(r_slug["path"]).name == "Tax 2026"


def test_move_preflight_collision_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    src_dir = ws / "Inbox"
    dst_dir = ws / "Archive"
    src_dir.mkdir()
    dst_dir.mkdir()
    (src_dir / "report.txt").write_text("New report version", encoding="utf-8")
    (dst_dir / "report.txt").write_text("Existing report in Archive", encoding="utf-8")
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("move report.txt from Inbox to Archive")
    act = res["proposedAction"]
    assert act is not None
    assert "report_1.txt" in act["items"][0]["detail"]


def test_clean_behavioral_learning_never_learns_bare_extensions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    dst_dir = ws / "Vault"
    dst_dir.mkdir()
    (ws / "doc_a.txt").write_text("General note A with sufficient content length", encoding="utf-8")
    (ws / "doc_b.txt").write_text("General note B with sufficient content length", encoding="utf-8")
    st.complete_setup([str(ws)])
    files = st.get_all_state()["files"]
    f_a, f_b = files[0], files[1]

    st.execute_ai_action({
        "actionType": "move",
        "fileIds": [f_a["id"]],
        "unselectedIds": [f_b["id"]],
        "targetFolder": str(dst_dir),
        "sourceQuery": "move txt files to Vault",
    })
    with st._lock:
        conn = st._connect()
        mems = load_ai_memory(conn)
        conn.close()
    # Must NOT have learned bare "txt" route or "ext:txt" exclusion!
    assert not any(m["keyPattern"] == "txt" and m["kind"] == "route" for m in mems)
    assert not any(m["keyPattern"] == "ext:txt" and m["kind"] == "exclude" for m in mems)


# ============================================================================
# PILLAR 3: Renamer Entity Tokens, Transforms, Smart Content Rename & Collisions
# ============================================================================


def test_renamer_entity_tokens_and_new_tokens() -> None:
    f_dict = {
        "name": "IMG_0091.png",
        "title": "Receipt Scan",
        "createdDate": "2026-01-01",
        "format": "png",
        "width": 1920,
        "height": 1080,
        "shelfName": "01 Receipts & Finance",
        "subShelf": "Coffee",
        "folderFull": "/home/user/Work/Receipts",
        "tags": ["tax", "work"],
        "metadata": {
            "merchant": "Starbucks Reserve",
            "amount": 18.5,
            "currency": "USD",
            "date": "2026-03-14",
            "invoice_number": "INV-4421",
            "doc_type": "receipt",
            "camera": "iPhone 15 Pro",
        },
    }
    out = format_rename_template(
        f_dict,
        "{date}_{day}_{merchant}_{amount}_{currency}_{invoice}_{doctype}",
        seq_index=3,
    )
    assert out == "2026-03-14_14_starbucks-reserve_18.50_USD_INV-4421_receipt.png"
    out2 = format_rename_template(
        f_dict,
        "{camera}_{resolution}_{tag}_{subshelf}_{parent}_{seq:02d}",
        seq_index=3,
    )
    assert out2 == "iphone-15-pro_1920x1080_tax_coffee_work_03.png"


def test_renamer_transforms_case_replace_regex_and_strip() -> None:
    f = {"name": "2026-03-14_IMG_Hello World_01.png", "format": "png"}
    assert format_rename_template(f, "__UPPERCASE__") == "2026-03-14_IMG_HELLO WORLD_01.png"
    assert format_rename_template({"name": "hello_world.txt"}, "__TITLECASE__") == "Hello World.txt"
    assert format_rename_template({"name": "HelloWorldTest.txt"}, "__SNAKE__") == "hello_world_test.txt"
    assert format_rename_template({"name": "HelloWorld Test.txt"}, "__KEBAB__") == "hello-world-test.txt"
    assert format_rename_template({"name": "hello_world_test.txt"}, "__CAMEL__") == "helloWorldTest.txt"
    assert format_rename_template(f, "__STRIP_DATE__") == "IMG_Hello World_01.png"
    assert format_rename_template({"name": "Copy of Budget.pdf"}, "__STRIP_PREFIX__:Copy of ") == "Budget.pdf"
    assert format_rename_template({"name": "IMG_4421.jpg"}, "__REPLACE__:IMG_\x1fTrip_") == "Trip_4421.jpg"
    assert format_rename_template({"name": "photo_0012.jpg"}, r"__REGEX__:_\d+$\x1f_final".replace(r"\x1f", "\x1f")) == "photo_final.jpg"


def test_parse_rename_format_request_extended() -> None:
    r1 = parse_rename_format_request("replace 'IMG_' with 'Trip_' in Downloads")
    assert r1 and r1["template"] == "__REPLACE__:IMG_\x1fTrip_"
    r2 = parse_rename_format_request("rename all files in Downloads to snake_case")
    assert r2 and r2["template"] == "__SNAKE__"
    r3 = parse_rename_format_request("strip prefix 'Copy of ' from drafts")
    assert r3 and r3["template"] == "__STRIP_PREFIX__:Copy of "
    r4 = parse_rename_format_request("strip dates from receipts")
    assert r4 and r4["template"] == "__STRIP_DATE__"
    r5 = parse_rename_format_request("rename photos to Trip_{seq:04d}")
    assert r5 and r5["template"] == "Trip_{seq:04d}"


def test_intra_batch_rename_collision_simulator() -> None:
    pairs = [
        ({"id": "1", "name": "a.txt", "path": "/nonexistent/a.txt"}, "same.txt"),
        ({"id": "2", "name": "b.txt", "path": "/nonexistent/b.txt"}, "same.txt"),
        ({"id": "3", "name": "c.txt", "path": "/nonexistent/c.txt"}, "same.txt"),
    ]
    res = simulate_batch_rename_collisions(pairs)
    assert [nn for _, nn, _ in res] == ["same.txt", "same_1.txt", "same_2.txt"]
    assert [col for _, _, col in res] == [False, True, True]


def test_content_based_smart_rename_on_non_cryptic_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    f = ws / "notes_alpha.txt"
    f.write_text("Official Receipt\nMerchant: Acme Bakery\nTotal: USD 28.50\nDate: 2026-04-02", encoding="utf-8")
    st.complete_setup([str(ws)])
    res = st.ask_your_shelves("smart rename notes_alpha.txt based on their content")
    act = res["proposedAction"]
    assert act is not None and act["actionType"] == "rename_batch"
    new_name = next(iter(act["payload"]["renameMap"].values()))
    assert "acme-bakery" in new_name and "2026-04-02" in new_name


# ============================================================================
# PILLAR 4: Trash Safety, Restore Bug Fix, Archive & Empty Trash
# ============================================================================


def test_restore_trash_never_falls_back_to_all_trashed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    f1 = ws / "secret_a.txt"
    f2 = ws / "secret_b.txt"
    f1.write_text("Secret document A with full archival text", encoding="utf-8")
    f2.write_text("Secret document B with full archival text", encoding="utf-8")
    st.complete_setup([str(ws)])
    for f in st.get_all_state()["files"]:
        st.trash_file(f["id"])

    # Asking for a non-existent trashed file MUST NOT propose restoring all trashed files!
    res = st.ask_your_shelves("restore nonexistent_missing.pdf")
    assert res["proposedAction"] is None
    assert "didn't find" in res["answer"].lower()
    assert len(res["choices"]) >= 1

    # Asking for "restore last deleted file" restores ONLY 1 file
    res_last = st.ask_your_shelves("restore last deleted file")
    assert res_last["proposedAction"] is not None
    assert len(res_last["proposedAction"]["items"]) == 1


def test_pinned_files_unchecked_by_default_on_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "keep_me.txt").write_text("Important pinned contract", encoding="utf-8")
    (ws / "discard_me.txt").write_text("Disposable draft contract", encoding="utf-8")
    st.complete_setup([str(ws)])
    files = st.get_all_state()["files"]
    pinned_f = next(f for f in files if f["name"] == "keep_me.txt")
    st.toggle_pin_file(pinned_f["id"])

    res = st.ask_your_shelves("trash all files")
    act = res["proposedAction"]
    assert act is not None and act["actionType"] == "trash"
    by_name = {it["fileName"]: it for it in act["items"]}
    assert by_name["keep_me.txt"]["selectedByDefault"] is False
    assert "Pinned" in by_name["keep_me.txt"]["detail"]
    assert by_name["discard_me.txt"]["selectedByDefault"] is True


def test_archive_files_and_undo_and_empty_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    f1 = ws / "old_slide.txt"
    f1.write_text("Old presentation slide deck", encoding="utf-8")
    st.complete_setup([str(ws)])
    fid = st.get_all_state()["files"][0]["id"]

    # Archive via Ask AI + execute + undo
    arch_q = st.ask_your_shelves("archive old_slide.txt")
    act = arch_q["proposedAction"]
    assert act is not None and act["actionType"] == "archive_files"
    exec_r = st.execute_ai_action({"actionType": "archive_files", "fileIds": [fid]})
    assert exec_r["count"] == 1
    assert not f1.exists()
    st.undo_action()
    assert f1.exists()

    # Trash + Empty Trash preview + execute
    st.trash_file(fid)
    et_q = st.ask_your_shelves("empty trash")
    assert et_q["proposedAction"] is not None
    assert et_q["proposedAction"]["actionType"] == "empty_trash"
    assert et_q["proposedAction"]["destructive"] is True
    et_exec = st.execute_ai_action({"actionType": "empty_trash"})
    assert et_exec["count"] == 1
    assert st.list_trash_and_archive()["trashed"] == []


# ============================================================================
# PILLAR 5: Smart Deduplicator Composite Keep-Score & Keep Strategy
# ============================================================================


def test_deduplicator_composite_keep_score_and_strategies() -> None:
    f_pinned = {"id": "1", "name": "copy_photo (1).jpg", "pinned": True, "folder_short": "~/Downloads", "mtime": 100, "size_bytes": 1000}
    f_clean = {"id": "2", "name": "vacation.jpg", "pinned": False, "folder_short": "~/Documents/Trips", "mtime": 200, "size_bytes": 2000}

    # Pinned (+100) beats clean filename (+30) + organized folder (+20)
    k, c, reason = pick_keep_and_copy(f_pinned, f_clean, keep_strategy="best")
    assert k["id"] == "1" and "pinned" in reason

    # Explicit keep_strategy="newest" overrides composite score
    k2, _, reason2 = pick_keep_and_copy(f_pinned, f_clean, keep_strategy="newest")
    assert k2["id"] == "2" and "newest" in reason2

    # Explicit keep_strategy="oldest"
    k3, _, reason3 = pick_keep_and_copy(f_pinned, f_clean, keep_strategy="oldest")
    assert k3["id"] == "1" and "oldest" in reason3

    # Explicit keep_strategy="largest"
    k4, _, _ = pick_keep_and_copy(f_pinned, f_clean, keep_strategy="largest")
    assert k4["id"] == "2"

    # Explicit keep_strategy="folder:Trips"
    k5, _, reason5 = pick_keep_and_copy(f_pinned, f_clean, keep_strategy="folder:Trips")
    assert k5["id"] == "2" and "trips" in reason5.lower()


def test_deduplicator_keep_reason_and_swap_lookalike_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "orig.txt").write_text("Identical duplicate content for testing swap", encoding="utf-8")
    (ws / "orig (1).txt").write_text("Identical duplicate content for testing swap", encoding="utf-8")
    st.complete_setup([str(ws)])
    dups = st.get_all_state()["lookalikes"]
    assert len(dups) == 1
    assert dups[0].get("keepReason")

    sw_q = st.ask_your_shelves("swap keep and copy for pair 1")
    act = sw_q["proposedAction"]
    assert act is not None and act["actionType"] == "swap_lookalike"
    exec_sw = st.execute_ai_action({**act["payload"], "actionType": "swap_lookalike"})
    assert exec_sw["ok"] is True
    dups_after = st.get_all_state()["lookalikes"]
    assert dups_after[0]["swapped"] is True


# ============================================================================
# PILLAR 6: Tags, Shelves & Saved Searches
# ============================================================================


def test_auto_tag_rename_tag_and_clear_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "coffee_receipt.txt").write_text("Official Receipt\nMerchant: Starbucks\nTotal: USD 14.50\nDate: 2026-03-10", encoding="utf-8")
    st.complete_setup([str(ws)])
    fid = st.get_all_state()["files"][0]["id"]

    # Auto-tag
    at_q = st.ask_your_shelves("auto-tag all files")
    act = at_q["proposedAction"]
    assert act is not None and act["actionType"] == "auto_tag"
    assert fid in act["payload"]["tagMap"]
    st.execute_ai_action({**act["payload"], "actionType": "auto_tag", "fileIds": [fid]})
    tags_now = st.get_file_by_id(fid)["tags"]
    assert "receipt" in tags_now

    # Rename tag #receipt -> #expense
    rt_q = st.ask_your_shelves("rename tag #receipt to #expense")
    rt_act = rt_q["proposedAction"]
    assert rt_act is not None and rt_act["actionType"] == "rename_tag"
    st.execute_ai_action({**rt_act["payload"], "actionType": "rename_tag", "fileIds": [fid]})
    assert "expense" in st.get_file_by_id(fid)["tags"]
    assert "receipt" not in st.get_file_by_id(fid)["tags"]

    # Clear tags + Undo
    ct_q = st.ask_your_shelves("clear tags from all files")
    assert ct_q["proposedAction"]["actionType"] == "clear_tags"
    st.execute_ai_action({"actionType": "clear_tags", "fileIds": [fid]})
    assert st.get_file_by_id(fid)["tags"] == []
    st.undo_action()
    assert "expense" in st.get_file_by_id(fid)["tags"]


def test_shelf_lifecycle_rename_delete_pin_unshelve_and_save_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "memo.txt").write_text("Quarterly engineering memo", encoding="utf-8")
    st.complete_setup([str(ws)])
    fid = st.get_all_state()["files"][0]["id"]

    # Create shelf + undo + redo
    cs_res = st.execute_ai_action({"actionType": "create_shelf", "shelfName": "Sprint Q2", "fileIds": [fid]})
    sid = cs_res["shelfId"]
    assert any(s["id"] == sid for s in st.get_all_state()["shelves"])
    st.undo_action()
    assert not any(s["id"] == sid for s in st.get_all_state()["shelves"])
    st.redo_action()
    assert any(s["id"] == sid for s in st.get_all_state()["shelves"])

    # Rename shelf + undo
    rn_q = st.ask_your_shelves("rename shelf Sprint Q2 to Sprint Q3")
    assert rn_q["proposedAction"]["actionType"] == "rename_shelf"
    st.execute_ai_action({**rn_q["proposedAction"]["payload"], "actionType": "rename_shelf"})
    assert next(s for s in st.get_all_state()["shelves"] if s["id"] == sid)["name"] == "Sprint Q3"
    st.undo_action()
    assert next(s for s in st.get_all_state()["shelves"] if s["id"] == sid)["name"] == "Sprint Q2"

    # Pin / Unpin shelf
    ps_q = st.ask_your_shelves("unpin shelf Sprint Q2")
    assert ps_q["proposedAction"]["actionType"] == "unpin_shelf"
    st.execute_ai_action({**ps_q["proposedAction"]["payload"], "actionType": "unpin_shelf"})

    # Unshelve
    st.set_file_shelf(fid, sid)
    us_q = st.ask_your_shelves("unshelve memo.txt")
    assert us_q["proposedAction"]["actionType"] == "unshelve"
    st.execute_ai_action({"actionType": "unshelve", "fileIds": [fid]})

    # Delete custom shelf + undo
    ds_q = st.ask_your_shelves("delete shelf Sprint Q2")
    assert ds_q["proposedAction"]["actionType"] == "delete_shelf"
    st.execute_ai_action({**ds_q["proposedAction"]["payload"], "actionType": "delete_shelf"})
    assert not any(s["id"] == sid for s in st.get_all_state()["shelves"])
    st.undo_action()
    assert any(s["id"] == sid for s in st.get_all_state()["shelves"])

    # Save search
    ss_q = st.ask_your_shelves("save search 'Tax 2026' for receipts from 2026")
    assert ss_q["proposedAction"]["actionType"] == "save_search"
    st.execute_ai_action({**ss_q["proposedAction"]["payload"], "actionType": "save_search"})
    assert any(s["name"] == "Tax 2026" for s in st.list_saved_searches())


# ============================================================================
# PILLAR 7: Multi-Page PDF OCR, Self-Healing OCR, Maintenance & Conversions
# ============================================================================


def test_multipage_pdf_ocr_scans_multiple_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vector_store as vs_mod

    st, ws = _make_workspace(tmp_path, monkeypatch)
    pdf_p = ws / "scanned_3page.pdf"
    _create_multipage_pdf(pdf_p, page_count=3)
    st.complete_setup([str(ws)])
    fid = st.get_all_state()["files"][0]["id"]

    calls: list[int] = []

    def _fake_ocr(_img: Any) -> tuple[str, float]:
        calls.append(1)
        return f"Scanned Page {len(calls)} Total PHP 120.00", 95.0

    monkeypatch.setattr(vs_mod, "run_ocr_on_image", _fake_ocr)
    res = st.execute_file_action(fid, "run_ocr")
    assert res["ok"] is True
    assert res["pagesOcrd"] == 3
    assert len(calls) == 3
    assert "Scanned Page 3" in res["ocrText"]


def test_self_healing_ondemand_ocr_before_answering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vector_store as vs_mod

    st, ws = _make_workspace(tmp_path, monkeypatch)
    img_p = ws / "receipt_04.png"
    _create_sample_png(img_p)
    # Index with OCR disabled initially so file has empty ocr_text
    st.update_setting("ocr_enabled", False)
    st.complete_setup([str(ws)])
    fid = st.get_all_state()["files"][0]["id"]
    assert st.get_file_by_id(fid)["ocrText"] == ""

    # Enable OCR and mock RapidOCR; asking "what does receipt_04.png say?" should auto-OCR it!
    st.update_setting("ocr_enabled", True)
    monkeypatch.setattr(vs_mod, "run_ocr_on_image", lambda _img: ("Starbucks Espresso Total USD 6.75", 97.0))
    ans = st.ask_your_shelves("what does receipt_04.png say?", selected_file_id=fid)
    assert "6.75" in ans["answer"] or "Starbucks" in ans["answer"]


def test_system_maintenance_file_convert_and_open_reveal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    pdf_p = ws / "guide.pdf"
    _create_multipage_pdf(pdf_p, page_count=1)
    resume = ws / "my_resume.txt"
    resume.write_text("Senior Engineer Resume", encoding="utf-8")
    st.complete_setup([str(ws)])

    assert st.ask_your_shelves("rescan folders")["proposedAction"]["actionType"] == "rescan_folders"
    assert st.ask_your_shelves("verify index")["proposedAction"]["actionType"] == "verify_index"
    vi = st.execute_ai_action({"actionType": "verify_index"})
    assert vi["ok"] is True

    # Open & Reveal actions (NOT blocked by read-only guard!)
    op_q = st.ask_your_shelves("open my_resume.txt")
    assert op_q["proposedAction"] is not None and op_q["proposedAction"]["actionType"] == "open_file"
    rev_q = st.ask_your_shelves("reveal my_resume.txt in finder")
    assert rev_q["proposedAction"] is not None and rev_q["proposedAction"]["actionType"] == "reveal_file"

    # File conversion (PDF -> TXT)
    conv_q = st.ask_your_shelves("convert guide.pdf to txt")
    assert conv_q["proposedAction"] is not None and conv_q["proposedAction"]["actionType"] == "file_convert"
    assert conv_q["proposedAction"]["payload"]["fileAction"] == "extract_pdf_text"


# ============================================================================
# PILLAR 8: Deep RAG, Multi-Modal Compare, Dedup Finance, Timelines, Scoped Stats
# ============================================================================


def test_compare_rich_full_text_entity_delta_and_no_pipe_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    pad = "Introductory text filler line. " * 25  # > 750 chars
    (ws / "inv_jan.txt").write_text(
        f"Invoice\nMerchant: CloudHost\nTotal: USD 100.00\n{pad}\nTail clause: Alpha Tier",
        encoding="utf-8",
    )
    (ws / "inv_feb.txt").write_text(
        f"Invoice\nMerchant: CloudHost\nTotal: USD 145.00\n{pad}\nTail clause: Beta Tier",
        encoding="utf-8",
    )
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("compare inv_jan.txt and inv_feb.txt")
    ans = res["answer"]
    assert "Comparison" in ans
    assert "45.00" in ans  # Exact entity amount delta!
    assert "Beta Tier" in ans  # Sees past 600 chars!
    assert "Semantic similarity" in ans
    # NEVER emit GFM pipe tables!
    assert not any(ln.strip().startswith("|") and ln.strip().endswith("|") for ln in ans.splitlines())


def test_full_corpus_deduplicated_financial_analytics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    # Create 22 unique receipts (> old 18 cap!) + 2 exact duplicates
    for i in range(1, 23):
        (ws / f"rcpt_{i:02d}.txt").write_text(
            f"Official Receipt #{i:02d}\nMerchant: Shop{i % 3}\nTotal: USD 10.00\nDate: 2026-0{(i % 2) + 1}-10",
            encoding="utf-8",
        )
    # Exact duplicates of rcpt_01 (identical content -> identical SHA-256)
    (ws / "rcpt_01_dup1.txt").write_text((ws / "rcpt_01.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (ws / "rcpt_01_dup2.txt").write_text((ws / "rcpt_01.txt").read_text(encoding="utf-8"), encoding="utf-8")
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("how much did I spend in total on receipts?")
    ans = res["answer"]
    # 22 unique receipts * $10 = $220.00 (exact duplicates MUST be excluded, NOT $240!)
    assert "$220.00" in ans
    assert "22 unique financial record" in ans
    assert "excluded 2 exact duplicate" in ans
    assert "By Merchant" in ans
    assert "By Month" in ans
    assert not any(ln.strip().startswith("|") and ln.strip().endswith("|") for ln in ans.splitlines())


def test_scoped_workspace_overview_does_not_leak_global_stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    fa = ws / "FolderA"
    fb = ws / "FolderB"
    fa.mkdir()
    fb.mkdir()
    (fa / "a1.txt").write_text("Small document located inside FolderA", encoding="utf-8")
    (fb / "b1.txt").write_text("X" * 50000, encoding="utf-8")
    (fb / "b2.txt").write_text("Y" * 50000, encoding="utf-8")
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("how much space am I using?", scope_folder=str(fa))
    assert "1 file(s)" in res["answer"]
    assert "3 file" not in res["answer"]


def test_timeline_query_groups_chronologically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "notes_today.txt").write_text("Worked on architecture refactor today", encoding="utf-8")
    st.complete_setup([str(ws)])
    res = st.ask_your_shelves("what did I work on this week?")
    assert res["intentMode"] == "timeline"
    assert "timeline" in res["answer"].lower()


# ============================================================================
# PILLAR 9: Multi-Step Chaining, Query-as-Step-0, Unique Items & Atomic Undo
# ============================================================================


def test_query_as_step0_compound_chaining_and_atomic_multi_step_undo_redo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    fin = ws / "Finance"
    fin.mkdir()
    r1 = ws / "starbucks_march.txt"
    r1.write_text("Official Receipt\nMerchant: Starbucks\nTotal: USD 12.00\nDate: 2026-03-05", encoding="utf-8")
    r2 = ws / "bakery_march.txt"
    r2.write_text("Official Receipt\nMerchant: Bakery\nTotal: USD 18.00\nDate: 2026-03-11", encoding="utf-8")
    st.complete_setup([str(ws)])

    # Query-as-Step-0: starts with "find ..." — previously killed by read-only guard!
    res = st.ask_your_shelves("find all receipts, tag them #tax, and move to Finance")
    act = res["proposedAction"]
    assert act is not None
    assert act["actionType"] == "multi_step"
    assert len(act["steps"]) == 2
    # Strictly UNIQUE `fileId` in proposedAction.items!
    item_fids = [it["fileId"] for it in act["items"]]
    assert len(item_fids) == len(set(item_fids)) == 2

    # Execute full multi-step workflow
    exec_r = st.execute_ai_action({
        "actionType": "multi_step",
        "fileIds": item_fids,
        "steps": act["steps"],
    })
    assert exec_r["ok"] is True
    assert (fin / "starbucks_march.txt").exists()
    assert (fin / "bakery_march.txt").exists()
    assert "tax" in st.get_file_by_id(item_fids[0])["tags"]

    # ONE single Undo reverses BOTH the move AND the tag!
    st.undo_action()
    assert r1.exists() and r2.exists()
    assert "tax" not in st.get_file_by_id(item_fids[0])["tags"]

    # ONE single Redo replays BOTH steps forward!
    st.redo_action()
    assert (fin / "starbucks_march.txt").exists()
    assert "tax" in st.get_file_by_id(item_fids[0])["tags"]


def test_elided_object_propagation_and_create_shelf_to_shelve_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert propagate_elided_object("pin", True) == "pin them"
    assert propagate_elided_object("move to Finance", True) == "move them to Finance"
    assert propagate_elided_object("tag #tax", True) == "tag them with #tax"

    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "tax_doc.txt").write_text("Annual tax summary 2026", encoding="utf-8")
    st.complete_setup([str(ws)])

    # "tag tax_doc.txt with #tax and pin" (elided object on step 2!)
    res = st.ask_your_shelves("tag tax_doc.txt with #tax and pin")
    act = res["proposedAction"]
    assert act is not None and act["actionType"] == "multi_step"
    assert len(act["steps"]) == 2
    assert act["steps"][1]["actionType"] == "pin"

    # create_shelf -> shelve passes last_created_shelf_id cleanly during execute!
    fid = st.get_all_state()["files"][0]["id"]
    exec_cs = st.execute_ai_action({
        "actionType": "multi_step",
        "fileIds": [fid],
        "steps": [
            {"actionType": "create_shelf", "title": "Create shelf", "fileIds": [], "payload": {"shelfName": "Chained Shelf"}},
            {"actionType": "shelve", "title": "Shelve", "fileIds": [fid], "requiresSelection": True, "payload": {"shelfId": "__last_created_shelf__", "shelfName": "Chained Shelf"}},
        ],
    })
    assert exec_cs["ok"] is True
    assert st.get_file_by_id(fid)["shelfName"] == "Chained Shelf"


def test_merge_unique_plan_items_aggregates_multi_step_details() -> None:
    steps = [
        {"_stepIndex": 0, "title": "Tag", "items": [{"fileId": "f1", "fileName": "a.pdf", "detail": "Add #tax", "selectedByDefault": True}]},
        {"_stepIndex": 1, "title": "Move", "items": [{"fileId": "f1", "fileName": "a.pdf", "detail": "→ ~/Finance/a.pdf", "selectedByDefault": True}]},
    ]
    merged = merge_unique_plan_items(steps)
    assert len(merged) == 1
    assert "[Step 1]" in merged[0]["detail"] and "[Step 2]" in merged[0]["detail"]


# ============================================================================
# PILLAR 10: Coreference, Pending-Plan Editing, Word-Boundary History & Memory Decay
# ============================================================================


def test_rich_anaphora_ordinals_ranges_attributes_and_subtractive() -> None:
    ctx = [
        {"id": "1", "name": "a.pdf", "format": "PDF", "modality": "document", "sizeBytes": 100, "mtime": 10, "folderFull": "/Downloads", "pinned": False, "tags": []},
        {"id": "2", "name": "screenshot_1.png", "format": "PNG", "modality": "image", "sizeBytes": 900, "mtime": 20, "folderFull": "/Desktop", "pinned": True, "tags": ["work"]},
        {"id": "3", "name": "b.pdf", "format": "PDF", "modality": "document", "sizeBytes": 500, "mtime": 30, "folderFull": "/Downloads", "pinned": False, "tags": []},
        {"id": "4", "name": "c.txt", "format": "TXT", "modality": "document", "sizeBytes": 50, "mtime": 40, "folderFull": "/Docs", "pinned": False, "tags": []},
    ]
    assert [f["id"] for f in resolve_anaphora_subset("the fourth one", ctx)] == ["4"]
    assert [f["id"] for f in resolve_anaphora_subset("first 2", ctx)] == ["1", "2"]
    assert [f["id"] for f in resolve_anaphora_subset("last 2", ctx)] == ["3", "4"]
    assert [f["id"] for f in resolve_anaphora_subset("items 1 and 3", ctx)] == ["1", "3"]
    assert [f["id"] for f in resolve_anaphora_subset("the largest one", ctx)] == ["2"]
    assert [f["id"] for f in resolve_anaphora_subset("the PDF ones", ctx)] == ["1", "3"]
    assert [f["id"] for f in resolve_anaphora_subset("the one in Desktop", ctx)] == ["2"]
    assert [f["id"] for f in resolve_anaphora_subset("all except screenshots", ctx)] == ["1", "3", "4"]
    assert [f["id"] for f in resolve_anaphora_subset("all except pinned", ctx)] == ["1", "3", "4"]


def test_full_pending_plan_editing_swap_dest_verb_tag_drop_and_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "Archive").mkdir()
    (ws / "Finance").mkdir()
    (ws / "doc1.txt").write_text("First document with enough content to index", encoding="utf-8")
    (ws / "doc2.txt").write_text("Second document with enough content to index", encoding="utf-8")
    st.complete_setup([str(ws)])

    # Start with a move plan
    p0 = st.ask_your_shelves("move all files to Archive")["proposedAction"]
    assert p0["actionType"] == "move" and len(p0["items"]) == 2

    # 1. Swap destination ("actually move to Finance instead")
    p1 = st.ask_your_shelves("actually move to Finance instead", pending_action=p0)["proposedAction"]
    assert p1["targetLabel"].endswith("Finance")

    # 2. Swap verb ("copy them instead")
    p2 = st.ask_your_shelves("copy them instead", pending_action=p1)["proposedAction"]
    assert p2["actionType"] == "copy" and p2["reversible"] is True

    # 3. Drop first item ("drop the first one")
    p3 = st.ask_your_shelves("drop the first one", pending_action=p2)["proposedAction"]
    assert len(p3["items"]) == 1

    # 4. Explain selection ("why did you pick these?")
    why_r = st.ask_your_shelves("why did you pick these?", pending_action=p3)
    assert p3["items"][0]["fileName"] in why_r["answer"]

    # 5. Swap tag ("use tag #audit instead")
    t0 = st.ask_your_shelves("tag all files with #draft")["proposedAction"]
    t1 = st.ask_your_shelves("use tag #audit instead", pending_action=t0)["proposedAction"]
    assert t1["payload"]["tag"] == "audit"


def test_word_boundary_history_extraction_no_short_filename_false_positives() -> None:
    state = {
        "files": [
            {"id": "1", "name": "a.txt", "title": "a", "folderFull": "/tmp"},
            {"id": "2", "name": "quarterly_report.pdf", "title": "Quarterly Report", "folderFull": "/tmp"},
        ],
        "watchedFolders": [],
        "pinnedFolders": [],
        "shelves": [],
    }
    ctx = extract_conversation_context(
        state,
        history=[{"role": "user", "text": "please check my alpha.txt and quarterly_report.pdf"}],
        selected_file_id=None,
        recent_file_ids=[],
    )
    mentioned_ids = [f["id"] for f in ctx["mentionedFiles"]]
    assert "2" in mentioned_ids
    assert "1" not in mentioned_ids  # "a.txt" inside "alpha.txt" MUST NOT match!


def test_memory_90_day_decay_and_forget_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "Finance").mkdir()
    st.complete_setup([str(ws)])

    # Insert an explicit rule via remember + forget it via Ask AI
    rem = st.ask_your_shelves("remember that receipts go in Finance")
    assert rem["proposedAction"]["actionType"] == "remember"
    st.execute_ai_action({**rem["proposedAction"]["payload"], "actionType": "remember"})
    assert len(st.get_all_state()["aiMemory"]) >= 1

    # Test 90-day half-life decay on route entry
    with st._lock:
        conn = st._connect()
        old_ts = time.time() - 180 * 86400  # 2 half-lives -> 0.25x weight
        conn.execute("UPDATE ai_memory SET updated_at = ? WHERE kind = 'route'", (old_ts,))
        conn.commit()
        mems = load_ai_memory(conn)
        conn.close()
    route_m = next(m for m in mems if m["kind"] == "route")
    assert route_m["effectiveWeight"] < route_m["weight"] * 0.35

    # Forget rule via Ask AI
    fg = st.ask_your_shelves("forget receipts go in Finance")
    assert fg["proposedAction"] is not None and fg["proposedAction"]["actionType"] == "forget_memory"
    st.execute_ai_action({**fg["proposedAction"]["payload"], "actionType": "forget_memory"})
    assert not any(m["keyPattern"] == "receipts" for m in st.get_all_state()["aiMemory"])


def test_conv_ctx_passed_to_action_matchers_tag_pin_ocr_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "budget_2026.txt").write_text("Annual household budget 2026", encoding="utf-8")
    (ws / "other_file.txt").write_text("Random scratchpad", encoding="utf-8")
    st.complete_setup([str(ws)])
    hist = [{"role": "assistant", "text": "I found `budget_2026.txt` in your workspace."}]

    # Previously tag/pin/delete dropped `conv_ctx` and ignored history!
    t_res = st.ask_your_shelves("tag it with #budget", history=hist)
    assert t_res["proposedAction"]["items"][0]["fileName"] == "budget_2026.txt"

    p_res = st.ask_your_shelves("pin it", history=hist)
    assert p_res["proposedAction"]["items"][0]["fileName"] == "budget_2026.txt"

    d_res = st.ask_your_shelves("trash it", history=hist)
    assert d_res["proposedAction"]["items"][0]["fileName"] == "budget_2026.txt"


# ============================================================================
# PILLAR 11: Local Ollama Hybrid Brain (`llm_router.py`, 100% Mocked)
# ============================================================================


def test_ollama_model_exposed_in_get_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, _ = _make_workspace(tmp_path, monkeypatch)
    st.update_setting("ollama_model", "qwen2.5:7b")
    assert st.get_settings()["ollama_model"] == "qwen2.5:7b"


def test_ollama_discovery_circuit_breaker_and_chat_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    reset_ollama_router()
    router = get_ollama_router()

    # 1. Offline failure trips 30s circuit breaker -> subsequent call does 0 network calls!
    call_count = {"n": 0}

    def _fail_urlopen(*_a: Any, **_kw: Any) -> Any:
        call_count["n"] += 1
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _fail_urlopen)
    assert router.discover_models() == []
    assert call_count["n"] == 1
    assert router.is_circuit_open() is True
    assert router.discover_models() == []
    assert call_count["n"] == 1  # Zero wasted latency while circuit open!

    # 2. Online mock: /api/tags auto-discovery ranks qwen2.5:7b over llama3.2:1b + /api/chat returns synthesis
    reset_ollama_router()

    class _FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._data = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    def _ok_urlopen(req: Any, timeout: float = 5.0) -> _FakeResp:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/tags"):
            return _FakeResp({"models": [{"name": "nomic-embed-text:latest"}, {"name": "llama3.2:1b"}, {"name": "qwen2.5:7b"}]})
        body = json.loads(req.data.decode("utf-8"))
        assert body["keep_alive"] == "10m"
        assert body["model"] == "qwen2.5:7b"
        if "format" in body:
            return _FakeResp({
                "message": {
                    "content": json.dumps({
                        "mode": "action",
                        "confidence": 0.92,
                        "steps": [{"actionType": "tag", "targetQuery": "all files", "tag": "reviewed"}],
                    })
                }
            })
        return _FakeResp({"message": {"content": "Summary of `memo.txt`:\n| Col A | Col B |\n|---|---|\n| Key | Value |"}})

    monkeypatch.setattr(urllib.request, "urlopen", _ok_urlopen)
    assert router.resolve_model("") == "qwen2.5:7b"

    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "memo.txt").write_text("Project launch date is July 2026.", encoding="utf-8")
    st.complete_setup([str(ws)])
    st.update_setting("ollama_enabled", True)

    # Planner returns allowlist-validated plan
    plan = plan_with_ollama(st, "please mark everything as reviewed somehow", ["ws"], [])
    assert plan is not None and plan["steps"][0]["actionType"] == "tag"

    # Synthesis strips pipe tables cleanly!
    ans = synthesize_qa_answer(st, "summarize memo", [{"name": "memo.txt", "excerpt": "Launch July 2026"}], [])
    assert ans is not None and "memo.txt" in ans
    assert not any(ln.strip().startswith("|") and ln.strip().endswith("|") for ln in ans.splitlines())
    reset_ollama_router()


def test_ollama_plan_validator_rejects_disallowed_actions_and_path_traversal() -> None:
    bad = validate_ollama_plan({"mode": "action", "confidence": 0.9, "steps": [{"actionType": "shell_exec", "targetQuery": "rm -rf /"}]})
    assert bad is None
    tricky = validate_ollama_plan({"mode": "action", "confidence": 0.9, "steps": [{"actionType": "move", "destination": "../../etc/passwd"}]})
    assert tricky is not None and "destination" not in tricky["steps"][0]
    low_conf = validate_ollama_plan({"mode": "action", "confidence": 0.2, "steps": [{"actionType": "trash"}]})
    assert low_conf is None


# ============================================================================
# PILLAR 12: Real Undo Logs, Reranker Intent+FTS5, Top-3 Choices & Partial Failures
# ============================================================================


def test_real_undo_logs_for_create_folder_copy_rename_folder_move_empty_folder_and_pin_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "sample.txt").write_text("Sample file with enough bytes to index cleanly", encoding="utf-8")
    st.complete_setup([str(ws)])
    fid = st.get_all_state()["files"][0]["id"]

    # 1. create_folder -> watched + undo removes empty folder
    cf = st.execute_ai_action({"actionType": "create_folder", "parentPath": str(ws), "folderName": "NewBox"})
    new_box = Path(cf["path"])
    assert new_box.exists()
    assert any(Path(w["path"]).resolve() == new_box.resolve() for w in st.get_watched_folders())
    st.undo_action()
    assert not new_box.exists()
    st.redo_action()
    assert new_box.exists()

    # 2. copy -> undo deletes copy
    st.execute_ai_action({"actionType": "copy", "fileIds": [fid], "targetFolder": str(new_box)})
    assert (new_box / "sample.txt").exists()
    st.undo_action()
    assert not (new_box / "sample.txt").exists()

    # 3. rename_folder -> undo restores old name
    st.execute_ai_action({"actionType": "rename_folder", "folderPath": str(new_box), "newName": "RenamedBox"})
    renamed_box = ws / "RenamedBox"
    assert renamed_box.exists() and not new_box.exists()
    st.undo_action()
    assert new_box.exists() and not renamed_box.exists()

    # 4. move empty folder -> undo moves it back!
    dest_holder = ws / "Holder"
    dest_holder.mkdir()
    st.execute_ai_action({"actionType": "move_folder", "folderPaths": [str(new_box)], "targetFolder": str(dest_holder)})
    assert (dest_holder / "NewBox").exists()
    st.undo_action()
    assert new_box.exists() and not (dest_holder / "NewBox").exists()

    # 5. pin_folder -> undo unpins
    st.execute_ai_action({"actionType": "pin_folder", "folderPath": str(new_box)})
    assert any(Path(p["path"]).resolve() == new_box.resolve() for p in st.get_all_state()["pinnedFolders"])
    st.undo_action()
    assert not any(Path(p["path"]).resolve() == new_box.resolve() for p in st.get_all_state()["pinnedFolders"])


def test_partial_failure_reporting_never_silently_swallows_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "real.txt").write_text("Real indexed file content for rename test", encoding="utf-8")
    st.complete_setup([str(ws)])
    real_fid = st.get_all_state()["files"][0]["id"]

    res = st.execute_ai_action({
        "actionType": "rename_batch",
        "fileIds": [real_fid, "nonexistent_file_id_999"],
        "renameMap": {real_fid: "renamed_real.txt", "nonexistent_file_id_999": "ghost.txt"},
    })
    assert res["ok"] is True
    assert res["count"] == 1
    assert len(res["failed"]) == 1
    assert res["failed"][0]["fileId"] == "nonexistent_file_id_999"
    assert "1 failed" in res["summary"]


def test_reranker_intent_detection_and_fts5_bm25_fusion() -> None:
    assert detect_rerank_intent("report_2026.pdf") == "title"
    assert detect_rerank_intent("receipt from Starbucks over $50") == "entity"
    assert detect_rerank_intent("sunset beach photo") == "visual"
    assert detect_rerank_intent('"exact quoted phrase"') == "lexical"

    cands = [
        {"title": "Doc A", "name": "a.txt", "extracted_text": "hello world", "modality": "document", "mtime": time.time(), "_dense_sim": 0.5, "_fts_score": 0.0},
        {"title": "Doc B", "name": "b.txt", "extracted_text": "hello world", "modality": "document", "mtime": time.time(), "_dense_sim": 0.5, "_fts_score": 0.95},
    ]
    out = rerank_candidates("hello world", cands)
    assert out[0]["name"] == "b.txt"
    assert out[0]["_rerank_features"]["fts_bm25"] > 0


def test_choices_always_capped_at_top_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    for i in range(5):
        (ws / f"item_{i}.txt").write_text(f"Document item {i} notes", encoding="utf-8")
    st.complete_setup([str(ws)])
    for q in ("hello", "list everything", "organize", "tag files"):
        res = st.ask_your_shelves(q)
        assert len(res.get("choices") or []) <= 3


def test_conversational_prefix_stripping_and_redo_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "plan.txt").write_text("Project roadmap and milestone plan document", encoding="utf-8")
    st.complete_setup([str(ws)])
    fid = st.get_all_state()["files"][0]["id"]

    # Conversational prefix "I want to tag ..."
    r = st.ask_your_shelves("I want to tag plan.txt with #roadmap")
    assert r["proposedAction"] is not None and r["proposedAction"]["actionType"] == "tag"
    st.execute_ai_action({"actionType": "tag", "fileIds": [fid], "tag": "roadmap"})
    st.undo_action()

    # Redo via Ask AI
    redo_q = st.ask_your_shelves("let's redo")
    assert redo_q["proposedAction"] is not None and redo_q["proposedAction"]["actionType"] == "redo"
    st.execute_ai_action({**redo_q["proposedAction"]["payload"], "actionType": "redo"})
    assert "roadmap" in st.get_file_by_id(fid)["tags"]


def test_fastapi_execute_endpoint_accepts_all_new_optional_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    import main as engine_main

    st, ws = _make_workspace(tmp_path, monkeypatch)
    f = ws / "receipt_api.txt"
    f.write_text("Official Receipt Merchant: CafeZ Total: USD 9.00 Date: 2026-05-01", encoding="utf-8")
    st.complete_setup([str(ws)])
    monkeypatch.setattr(engine_main, "store", st)
    client = TestClient(engine_main.app)
    fid = st.get_all_state()["files"][0]["id"]

    resp = client.post(
        "/ask/execute",
        json={
            "actionType": "auto_tag",
            "fileIds": [fid],
            "tagMap": {fid: ["cafez", "2026"]},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["count"] == 1
    assert "cafez" in next(x for x in body["state"]["files"] if x["id"] == fid)["tags"]


# ============================================================================
# ADDITIONAL DEEP COVERAGE TESTS (Bringing test_ai_intelligence.py to 70+ tests)
# ============================================================================


def test_virtual_workspace_simulator_tracks_renames_moves_and_trash() -> None:
    sim = VirtualWorkspaceSimulator([
        {"id": "f1", "name": "IMG_001.png", "path": "/ws/Downloads/IMG_001.png", "folderFull": "/ws/Downloads", "tags": []},
        {"id": "f2", "name": "dup.png", "path": "/ws/Downloads/dup.png", "folderFull": "/ws/Downloads", "tags": []},
    ])
    sim.apply_step({"actionType": "create_folder", "payload": {"parentPath": "/ws", "folderName": "Tax 2026"}, "items": []})
    assert sim.last_created_folder_name == "Tax 2026"
    sim.apply_step({"actionType": "rename_batch", "payload": {"renameMap": {"f1": "2026-03-01_starbucks.png"}}, "items": [{"fileId": "f1"}]})
    assert sim.files_by_id["f1"]["name"] == "2026-03-01_starbucks.png"
    sim.apply_step({"actionType": "move", "payload": {"targetFolder": "/ws/Tax 2026"}, "items": [{"fileId": "f1"}]})
    assert sim.files_by_id["f1"]["path"].endswith("Tax 2026/2026-03-01_starbucks.png")
    sim.apply_step({"actionType": "trash", "payload": {}, "items": [{"fileId": "f2"}]})
    assert [f["id"] for f in sim.active_files()] == ["f1"]


def test_split_compound_commands_protects_padded_tokens_and_extracts_step0() -> None:
    parts = split_compound_commands("find all receipts from March, rename to {date}_{merchant}_{seq:02d}, and move to Finance")
    assert len(parts) == 3
    assert "{seq:02d}" in parts[1]
    step0, rest = extract_step0_query_clause(parts)
    assert step0 == "all receipts from March"
    assert len(rest) == 2


def test_qa_sliding_chunks_factoids_and_pipe_table_stripper() -> None:
    text = ". ".join(f"Sentence number {i} discusses project milestone {i * 10} in detail" for i in range(1, 25))
    chunks = split_sliding_chunks(text, max_chunks=20, chunk_chars=180)
    assert len(chunks) >= 5
    assert any("milestone 240" in c for c in chunks)

    files = [{"id": "1", "name": "vendor.txt", "metadata": {"email": "billing@acme.io", "phone": "+63 917 555 1234", "due_date": "2026-07-01", "tin": "123-456-789-000"}}]
    assert "billing@acme.io" in (extract_factoid_answer("what is the billing email?", files, {}) or "")
    assert "+63 917 555 1234" in (extract_factoid_answer("what is the phone number?", files, {}) or "")
    assert "2026-07-01" in (extract_factoid_answer("when is the due date?", files, {}) or "")
    assert "123-456-789-000" in (extract_factoid_answer("what is the TIN?", files, {}) or "")

    stripped = _strip_pipe_tables("Header\n| A | B |\n|---|---|\n| 1 | 2 |")
    assert "|" not in stripped
    assert "- 1 · 2" in stripped


def test_qa_format_due_payments_and_timeline_and_scoped_stats() -> None:
    today = datetime.now().date()
    overdue_d = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    today_d = today.strftime("%Y-%m-%d")
    soon_d = (today + timedelta(days=4)).strftime("%Y-%m-%d")
    files = [
        {"id": "1", "name": "b1.txt", "title": "Water", "format": "TXT", "size": "1 KB", "sizeBytes": 1000, "mtime": time.time(), "modality": "document", "shelfName": "Finance", "folder": "~/Docs", "metadata": {"merchant": "WaterCo", "due_date": overdue_d, "amount": 50.0, "currency": "USD"}},
        {"id": "2", "name": "b2.txt", "title": "Net", "format": "TXT", "size": "2 KB", "sizeBytes": 2000, "mtime": time.time() - 86400, "modality": "document", "shelfName": "Finance", "folder": "~/Docs", "metadata": {"merchant": "NetCo", "due_date": today_d, "amount": 60.0, "currency": "USD"}},
        {"id": "3", "name": "b3.txt", "title": "Gas", "format": "TXT", "size": "3 KB", "sizeBytes": 3000, "mtime": time.time() - 172800, "modality": "image", "shelfName": "Photos", "folder": "~/Pics", "metadata": {"merchant": "GasCo", "due_date": soon_d, "amount": 70.0, "currency": "USD"}},
    ]
    due_md = format_due_payments_answer(files, "Due in next 7 days")
    assert "Overdue by 2d" in due_md and "Due today" in due_md and "Due in 4d" in due_md

    tl_md = format_timeline_answer(files, "Last week")
    assert "timeline" in tl_md.lower() and "b1.txt" in tl_md

    stats = compute_scoped_workspace_stats(files[:2], [{"resolved": False, "keep": {"id": "3"}, "copy": {"id": "9"}, "saved_mb": 5.0}], scope_folder="/scoped")
    assert stats["totalFiles"] == 2
    assert stats["totalBytes"] == 3000
    assert stats["duplicatePairsCount"] == 0


def test_qp_size_parser_and_negations_folder_tag_shelf() -> None:
    assert parse_size_bytes("50MB") == 50 * 1024 * 1024
    assert parse_size_bytes("100 KB") == 100 * 1024
    assert parse_size_bytes("2.5gb") == int(2.5 * 1024 ** 3)

    state = {
        "watchedFolders": [],
        "pinnedFolders": [],
        "files": [{"folderFull": "/ws/Downloads"}, {"folderFull": "/ws/Work"}],
        "shelves": [{"name": "Archive"}],
    }
    spec = parse_file_query("all files not tagged #draft except Downloads", state=state)
    assert "draft" in spec.exclude_tags
    assert "downloads" in spec.exclude_folders
    assert matches_query_spec({"id": "1", "folderFull": "/ws/Work", "tags": ["final"]}, spec)
    assert not matches_query_spec({"id": "2", "folderFull": "/ws/Work", "tags": ["draft"]}, spec)
    assert not matches_query_spec({"id": "3", "folderFull": "/ws/Downloads", "tags": ["final"]}, spec)


def test_renamer_synthesize_content_rename_fallbacks() -> None:
    # Heading fallback
    n1, r1 = synthesize_content_rename({"name": "random_doc.pdf", "createdDate": "2026-02-10", "metadata": {"headings": ["Quarterly Sales Strategy"]}})
    assert n1 == "2026-02-10_quarterly-sales-strategy.pdf" and "heading" in (r1 or "")
    # Code symbol fallback
    n2, r2 = synthesize_content_rename({"name": "temp.py", "modality": "code", "createdDate": "2026-02-11", "metadata": {"codeSymbols": ["compute_tax_summary"]}})
    assert n2 == "2026-02-11_compute-tax-summary.py" and "symbol" in (r2 or "")
    # Visual scene fallback
    n3, r3 = synthesize_content_rename({"name": "pic.jpg", "modality": "image", "createdDate": "2026-02-12", "metadata": {"visualConcepts": ["sunset", "beach", "ocean"]}})
    assert n3 == "2026-02-12_sunset-beach-ocean.jpg" and "visual" in (r3 or "")


def test_deduplicator_find_lookalike_pairs_keep_strategy_oldest() -> None:
    v = np.zeros((2, 8), dtype=np.float32)
    v[0, 0] = 1.0
    v[1, 0] = 1.0
    files_meta = [
        {"id": "old_id", "name": "copy_old.txt", "title": "Doc", "path": "/ws/copy_old.txt", "ext": "txt", "modality": "document", "sha256": "abc123", "size_bytes": 100, "mtime": 1000.0, "folder_short": "~/ws", "display_date": "Jan 1"},
        {"id": "new_id", "name": "clean_new.txt", "title": "Doc", "path": "/ws/clean_new.txt", "ext": "txt", "modality": "document", "sha256": "abc123", "size_bytes": 100, "mtime": 2000.0, "folder_short": "~/ws", "display_date": "Jan 2"},
    ]
    pairs_best = find_lookalike_pairs(files_meta, v, {}, keep_strategy="best")
    assert pairs_best[0]["keep"]["id"] == "new_id"
    assert pairs_best[0]["isExact"] is True
    assert pairs_best[0]["keepReason"]

    pairs_oldest = find_lookalike_pairs(files_meta, v, {}, keep_strategy="oldest")
    assert pairs_oldest[0]["keep"]["id"] == "old_id"
    assert "oldest" in pairs_oldest[0]["keepReason"]


def test_restore_archive_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    f1 = ws / "orig_contract.txt"
    f2 = ws / "orig_contract (1).txt"
    body = "Exact duplicate contract body for archive and restore testing."
    f1.write_text(body, encoding="utf-8")
    f2.write_text(body, encoding="utf-8")
    st.complete_setup([str(ws)])

    dup_q = st.ask_your_shelves("show me all the duplicates")
    act = dup_q["proposedAction"]
    assert act is not None and act["actionType"] == "archive_duplicates"
    st.execute_ai_action({**act["payload"], "actionType": "archive_duplicates", "fileIds": [it["fileId"] for it in act["items"]]})
    assert not f2.exists()

    # Now restore archived duplicates via Ask AI!
    unarch_q = st.ask_your_shelves("unarchive duplicates")
    u_act = unarch_q["proposedAction"]
    assert u_act is not None and u_act["actionType"] == "restore_archive"
    st.execute_ai_action({**u_act["payload"], "actionType": "restore_archive", "fileIds": [it["fileId"] for it in u_act["items"]]})
    assert f2.exists()


def test_pending_plan_swap_format_and_swap_shelf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "alpha_report.txt").write_text("Alpha financial report document 2026", encoding="utf-8")
    (ws / "beta_report.txt").write_text("Beta financial report document 2026", encoding="utf-8")
    st.complete_setup([str(ws)])

    # Pending batch rename -> swap template
    r0 = st.ask_your_shelves("rename all files to {date}_{title}")["proposedAction"]
    assert r0 is not None and r0["actionType"] == "rename_batch"
    r1 = st.ask_your_shelves("use format {year}_{original}_{seq:02d} instead", pending_action=r0)["proposedAction"]
    assert r1 is not None and r1["payload"]["template"] == "{year}_{original}_{seq:02d}"
    assert any("_01.txt" in v for v in r1["payload"]["renameMap"].values())

    # Pending create shelf -> swap shelf name
    s0 = st.ask_your_shelves("create shelf Drafts with all files")["proposedAction"]
    assert s0 is not None and s0["actionType"] == "create_shelf"
    s1 = st.ask_your_shelves("use shelf Q1 Finals instead", pending_action=s0)["proposedAction"]
    assert s1 is not None and s1["targetLabel"] == "Q1 Finals"


def test_find_matching_memory_entries_helper() -> None:
    rows = [
        {"id": 1, "kind": "route", "keyPattern": "receipts", "value": "/home/u/Finance"},
        {"id": 2, "kind": "tag_pref", "keyPattern": "invoice", "value": "tax"},
    ]
    hits = find_matching_memory_entries(rows, "the rule that receipts go in Finance")
    assert len(hits) == 1 and hits[0]["id"] == 1


def test_exact_duplicates_only_filter_in_ask_ai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "contract.txt").write_text("Master service agreement section one and two.", encoding="utf-8")
    (ws / "contract_copy.txt").write_text("Master service agreement section one and two.", encoding="utf-8")
    st.complete_setup([str(ws)])
    res = st.ask_your_shelves("show exact duplicates only")
    act = res["proposedAction"]
    assert act is not None and act["actionType"] == "archive_duplicates"
    assert len(act["items"]) == 1


def test_keep_strategy_override_in_ask_ai_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    f_old = ws / "copy_old.txt"
    f_new = ws / "clean_new.txt"
    body = "Identical file body used for keep_strategy oldest test."
    f_old.write_text(body, encoding="utf-8")
    f_new.write_text(body, encoding="utf-8")
    old_ts = time.time() - 10 * 86400
    os.utime(f_old, (old_ts, old_ts))
    st.complete_setup([str(ws)])

    res = st.ask_your_shelves("archive duplicates keeping the oldest")
    act = res["proposedAction"]
    assert act is not None and act["payload"]["keepStrategy"] == "oldest"
    # Keeping oldest means copy_old.txt is kept and clean_new.txt is archived!
    assert act["items"][0]["fileName"] == "clean_new.txt"


def test_deictic_qa_uses_ollama_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    reset_ollama_router()

    class _FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._data = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    def _ok_urlopen(req: Any, timeout: float = 5.0) -> _FakeResp:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/tags"):
            return _FakeResp({"models": [{"name": "llama3.2:3b"}]})
        return _FakeResp({"message": {"content": "**Ollama Summary:** `spec.txt` outlines the Q4 launch plan."}})

    monkeypatch.setattr(urllib.request, "urlopen", _ok_urlopen)
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "spec.txt").write_text("Q4 Launch Plan: Ship phase 1, phase 2, and phase 3 cleanly.", encoding="utf-8")
    st.complete_setup([str(ws)])
    st.update_setting("ollama_enabled", True)
    fid = st.get_all_state()["files"][0]["id"]

    # Previously "summarize this" returned BEFORE Ollama! Now it synthesizes via Ollama!
    res = st.ask_your_shelves("summarize this", selected_file_id=fid)
    assert res["mode"] == "ollama-local"
    assert "Ollama Summary" in res["answer"]
    reset_ollama_router()


def test_search_semantic_dedup_exact_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    body = "Quantum photonics calibration report number 77."
    (ws / "q_a.txt").write_text(body, encoding="utf-8")
    (ws / "q_b.txt").write_text(body, encoding="utf-8")
    st.complete_setup([str(ws)])

    res_dedup = st.search_semantic("quantum photonics", dedup_exact=True)
    res_all = st.search_semantic("quantum photonics", dedup_exact=False)
    assert len(res_dedup["results"]) == 1
    assert len(res_all["results"]) == 2


# ============================================================================
# PILLAR 13: Deep File Context Dossier, Granular Structural Q&A & Continuous AI Training Feeder
# ============================================================================


def test_deep_file_context_dossier_and_explain_slash_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    py_file = ws / "billing_engine.py"
    py_file.write_text(
        '"""Automated recurring invoice reconciliation engine."""\n'
        "import sqlite3\n"
        "import numpy as np\n\n"
        "# TODO: Add webhook signature verification\n"
        "class InvoiceReconciler:\n"
        "    def compute_vat_breakdown(self, amount: float) -> float:\n"
        "        return amount * 0.12\n",
        encoding="utf-8",
    )
    csv_file = ws / "q1_expenses.csv"
    csv_file.write_text(
        "Department,Month,Cost_USD\n"
        "Engineering,Jan,1200.00\n"
        "Engineering,Feb,1800.00\n"
        "Design,Jan,500.00\n",
        encoding="utf-8",
    )
    notes_file = ws / "sprint_notes.md"
    notes_file.write_text(
        "# Q2 Architecture Review\n"
        "Prepared by: Dr. Elena Vance\n"
        "Reference: https://docs.goodshelves.local/spec\n"
        "- [ ] Finalize ONNX quantization benchmarks\n"
        "- [ ] Ship deep file context dossier\n",
        encoding="utf-8",
    )
    st.complete_setup([str(ws)])
    files = {f["name"]: f for f in st.get_all_state()["files"]}

    # 1. Explain code file via /explain slash command or natural language
    res_code = st.ask_your_shelves("/explain billing_engine.py")
    assert "InvoiceReconciler" in res_code["answer"]
    assert "compute_vat_breakdown" in res_code["answer"]
    assert "sqlite3" in res_code["answer"]
    assert "TODO" in res_code["answer"]
    assert not any(ln.strip().startswith("|") and ln.strip().endswith("|") for ln in res_code["answer"].splitlines())

    # 2. Granular code question ("what libraries does billing_engine.py import?")
    res_imp = st.ask_your_shelves("what libraries does billing_engine.py import?")
    assert "sqlite3" in res_imp["answer"] and "numpy" in res_imp["answer"]

    # 3. Spreadsheet column statistics ("what is the total of Cost_USD in q1_expenses.csv?")
    res_csv = st.ask_your_shelves("what is the total of Cost_USD in q1_expenses.csv?")
    assert "3,500.00" in res_csv["answer"]
    assert "Department" in res_csv["answer"]

    # 4. Action items, Author & URL extraction on meeting notes
    res_todo = st.ask_your_shelves("what are the action items in sprint_notes.md?")
    assert "Finalize ONNX quantization benchmarks" in res_todo["answer"]
    res_author = st.ask_your_shelves("who is the author of sprint_notes.md?")
    assert "Elena Vance" in res_author["answer"]

    # 5. Deictic deep context ("explain this file") with selected_file_id
    res_deictic = st.ask_your_shelves("explain this file", selected_file_id=files["q1_expenses.csv"]["id"])
    assert res_deictic["intentMode"] == "deep_context"
    assert "3,500.00" in res_deictic["answer"]


def test_continuous_ai_training_feeder_and_train_slash_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, ws = _make_workspace(tmp_path, monkeypatch)
    (ws / "sample.txt").write_text("Initial workspace document for training test.", encoding="utf-8")
    st.complete_setup([str(ws)])

    res_train = st.ask_your_shelves("/train")
    assert "Continuous AI Training, Data Feeding & Calibration" in res_train["answer"]
    assert "100.0%" in res_train["answer"]
    assert len(st.get_all_state()["aiMemory"]) >= 5

