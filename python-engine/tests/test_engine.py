import os
import sys
import zipfile
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devices import devices_signature, list_connected_devices
from parsers import parse_file
from vector_store import VectorStore


def _make_pptx(path: Path) -> None:
    slide_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Quarterly Roadmap</a:t></a:r></a:p>"
        "<a:p><a:r><a:t>Key milestones for Q4 release</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>"
        "</p:sld>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide_xml)


def _make_odt(path: Path) -> None:
    content_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:text>"
        "<text:h>OpenDocument Heading</text:h>"
        "<text:p>Budget summary and quarterly notes.</text:p>"
        "</office:text></office:body></office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", content_xml)


def test_parsers_cover_office_legacy_and_code(tmp_path: Path) -> None:
    xlsx_p = tmp_path / "budget.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Category", "Amount"])
    ws.append(["Groceries", 120])
    wb.save(xlsx_p)

    pptx_p = tmp_path / "roadmap.pptx"
    _make_pptx(pptx_p)

    odt_p = tmp_path / "report.odt"
    _make_odt(odt_p)

    rtf_p = tmp_path / "memo.rtf"
    rtf_p.write_text(r"{\rtf1\ansi {\fonttbl{\f0 Arial;}} Hello \b World \b0 \par Second line.}", encoding="utf-8")

    php_p = tmp_path / "handler.php"
    php_p.write_text("<?php\nfunction greet($name) { return 'Hi ' . $name; }\n?>", encoding="utf-8")

    html_p = tmp_path / "page.html"
    html_p.write_text("<html><body><h1>Welcome</h1><p>Local preview</p></body></html>", encoding="utf-8")

    p_xlsx = parse_file(xlsx_p)
    assert p_xlsx is not None and p_xlsx.metadata.get("kind") == "spreadsheet"

    p_pptx = parse_file(pptx_p)
    assert p_pptx is not None and "Quarterly Roadmap" in p_pptx.extracted_text
    assert p_pptx.metadata.get("slideCount") == 1

    p_odt = parse_file(odt_p)
    assert p_odt is not None and "OpenDocument Heading" in p_odt.extracted_text

    p_rtf = parse_file(rtf_p)
    assert p_rtf is not None and "Hello" in p_rtf.extracted_text and "Second line" in p_rtf.extracted_text

    p_php = parse_file(php_p)
    assert p_php is not None and p_php.modality == "code" and p_php.metadata.get("language") == "php"

    p_html = parse_file(html_p)
    assert p_html is not None and p_html.modality == "code" and p_html.metadata.get("language") == "html"


def test_devices_signature_stable() -> None:
    devs = list_connected_devices(set())
    assert isinstance(devs, list)
    sig = devices_signature([{"id": "1", "path": "/mnt/usb"}])
    assert sig == (("1", "/mnt/usb"),)


def test_vector_store_file_ops_tags_pins_device_import_and_undo(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()

    note1 = watch_dir / "IMG_20250101_001.txt"
    note1.write_text("Quarterly budget planning and grocery expenses.", encoding="utf-8")
    note2 = watch_dir / "roadmap.md"
    note2.write_text("# Product Roadmap\nMilestones for desktop release.", encoding="utf-8")

    store = VectorStore(db_path=db_path)
    progress_events: list[dict] = []
    store.register_stage_callback(lambda evt: progress_events.append(evt) if evt.get("type") == "progress" else None)

    res_setup = store.complete_setup([str(watch_dir)])
    assert res_setup["scannedCount"] == 2
    assert any(e["type"] == "progress" for e in progress_events)

    state = store.get_all_state()
    assert len(state["files"]) == 2
    f1 = next(f for f in state["files"] if f["name"] == "IMG_20250101_001.txt")

    # Search
    sr = store.search_semantic("quarterly budget")
    assert len(sr["results"]) >= 1

    # Pins, tags, saved searches
    pin_res = store.toggle_pin_file(f1["id"])
    assert pin_res["pinned"] is True
    tag_res = store.set_file_tags(f1["id"], ["finance", "Q4"])
    assert tag_res["tags"] == ["finance", "q4"]

    if state["shelves"]:
        sh_id = state["shelves"][0]["id"]
        assert store.toggle_pin_shelf(sh_id)["pinned"] is True

    ss = store.save_search("Budget docs", "quarterly budget", "document", "finance")
    assert len(store.list_saved_searches()) == 1
    store.delete_saved_search(ss["id"])
    assert len(store.list_saved_searches()) == 0

    # Create folder, copy, move + undo/redo
    new_dir_res = store.create_folder(str(watch_dir), "ArchiveBox")
    new_dir = Path(new_dir_res["path"])
    assert new_dir.is_dir()

    copy_res = store.copy_file(f1["id"], str(new_dir))
    assert Path(copy_res["path"]).exists()

    move_res = store.move_file(f1["id"], str(new_dir))
    moved_path = Path(move_res["path"])
    assert moved_path.exists()
    assert not note1.exists()

    undo_mv = store.undo_action()
    assert "Moved" in undo_mv["summary"] or "Move" in undo_mv["summary"]
    assert note1.exists()

    redo_mv = store.redo_action()
    assert moved_path.exists()

    # Bulk tag + pin
    all_ids = [f["id"] for f in store.get_all_state()["files"]]
    bulk_res = store.bulk_file_action(all_ids, "tag", tag="reviewed")
    assert bulk_res["count"] == len(all_ids)

    # Device safe import + undo
    phone_dir = tmp_path / "mock_phone"
    (phone_dir / "DCIM").mkdir(parents=True)
    (phone_dir / "DCIM" / "photo_notes.txt").write_text("Receipt from coffee shop on phone", encoding="utf-8")
    import_dest = tmp_path / "imported_phone"
    imp_res = store.import_from_device(str(phone_dir), str(import_dest))
    assert imp_res["importedCount"] == 1
    assert (import_dest / "photo_notes.txt").exists()
    store.undo_action()
    assert not (import_dest / "photo_notes.txt").exists()

    # Remove by path
    rem_res = store.remove_file_by_path(str(moved_path))
    assert rem_res["removed"] is True

    store.close()


def test_deduplicator_surfaces_triplets_and_parsers_exif_transpose(tmp_path: Path) -> None:
    import numpy as np
    from PIL import Image
    from deduplicator import find_lookalike_pairs

    # 3 identical SHA-256 items should yield 2 lookalike pairs (keeping 1 canonical copy)
    metas = [
        {"id": "f1", "name": "photo.jpg", "path": "/a/photo.jpg", "folder_short": "~/a", "ext": "jpg", "display_date": "Nov 1", "size_bytes": 1000, "sha256": "abc", "mtime": 100.0, "modality": "image", "extracted_text": "sunset", "ocr_text": "", "title": "photo", "width": 100, "height": 100},
        {"id": "f2", "name": "photo (1).jpg", "path": "/b/photo (1).jpg", "folder_short": "~/b", "ext": "jpg", "display_date": "Nov 1", "size_bytes": 1000, "sha256": "abc", "mtime": 101.0, "modality": "image", "extracted_text": "sunset", "ocr_text": "", "title": "photo", "width": 100, "height": 100},
        {"id": "f3", "name": "photo copy.jpg", "path": "/c/photo copy.jpg", "folder_short": "~/c", "ext": "jpg", "display_date": "Nov 1", "size_bytes": 1000, "sha256": "abc", "mtime": 102.0, "modality": "image", "extracted_text": "sunset", "ocr_text": "", "title": "photo", "width": 100, "height": 100},
    ]
    vecs = np.tile(np.ones((1, 512), dtype=np.float32) / np.sqrt(512.0), (3, 1))
    pairs = find_lookalike_pairs(metas, vecs, {})
    assert len(pairs) == 2

    # Image with EXIF orientation=6 (90 deg CW rotation) should be transposed to portrait
    img_p = tmp_path / "rotated.jpg"
    im = Image.new("RGB", (120, 60), color=(20, 120, 220))
    exif = im.getexif()
    exif[274] = 6  # Orientation: Rotate 90 CW
    im.save(img_p, exif=exif)
    parsed = parse_file(img_p, enable_ocr=False)
    assert parsed is not None
    assert parsed.width == 60 and parsed.height == 120


def test_smart_clean_undo_and_fastapi_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient
    import main as engine_main

    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    watch_dir = tmp_path / "docs"
    watch_dir.mkdir()
    (watch_dir / "IMG_20260101_01.txt").write_text("Receipt for coffee shop latte", encoding="utf-8")
    (watch_dir / "IMG_20260101_02.txt").write_text("Receipt for coffee shop latte", encoding="utf-8")

    test_store = VectorStore(db_path=tmp_path / "api_test.db")
    test_store.complete_setup([str(watch_dir)])
    monkeypatch.setattr(engine_main, "store", test_store)

    client = TestClient(engine_main.app)
    assert client.get("/health").status_code == 200

    # Saved search falls back cleanly on returned name
    ss_resp = client.post("/searches/saved", json={"name": "", "query": "coffee receipt", "modality": "all", "tag": ""})
    assert ss_resp.status_code == 200
    assert ss_resp.json()["item"]["name"] == "coffee receipt"

    # Re-indexing images should not raise AttributeError
    reidx_resp = client.post("/vision/reindex-images")
    assert reidx_resp.status_code == 200
    assert reidx_resp.json()["ok"] is True

    # Unknown vision model returns 400 cleanly
    bad_vis = client.post("/vision/select", json={"modelId": "nonexistent-model", "download": False})
    assert bad_vis.status_code == 400

    # Missing reveal target returns 404
    bad_rev = client.post("/os/reveal", json={"path": str(tmp_path / "does_not_exist.txt")})
    assert bad_rev.status_code == 404

    # Smart clean logs single batch (no duplicate archive entries) and undoes cleanly
    sc_resp = client.post("/smart-clean/execute", json={"applyRenames": True, "archiveDuplicates": True, "organizeFolders": False})
    assert sc_resp.status_code == 200
    state_after = sc_resp.json()["state"]
    smart_entries = [a for a in state_after["actionHistory"] if a["actionType"] == "smart_clean"]
    dup_arch_entries = [a for a in state_after["actionHistory"] if a["actionType"] == "archive"]
    assert len(smart_entries) <= 1 and len(dup_arch_entries) == 0

    # Permanent delete removes vec_files row as well
    if state_after["files"]:
        fid = state_after["files"][0]["id"]
        del_resp = client.post("/file/delete", json={"fileId": fid})
        assert del_resp.status_code == 200
        ver = test_store.verify_integrity()
        assert ver["activeFiles"] == ver["vectorRows"]

    test_store.close()


def test_concurrent_get_store_and_edge_case_resilience(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import concurrent.futures
    from fastapi.testclient import TestClient
    import main as engine_main

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("GOOD_SHELVES_DATA_DIR", str(data_dir))
    monkeypatch.setattr(engine_main, "store", None)

    # 1. Concurrent get_store() calls must not race on _init_db()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: engine_main.get_store(), range(4)))
    assert len({id(s) for s in stores}) == 1

    st = stores[0]

    # 2. Invalid watched folders return clean errors via FastAPI (400), not 500
    client = TestClient(engine_main.app)
    r_missing = client.post("/folders/add", json={"path": str(tmp_path / "does_not_exist")})
    assert r_missing.status_code == 400
    assert "does not exist" in r_missing.json()["detail"].lower()

    not_a_dir = tmp_path / "plain.txt"
    not_a_dir.write_text("hello world 12345678", encoding="utf-8")
    r_file = client.post("/folders/add", json={"path": str(not_a_dir)})
    assert r_file.status_code == 400

    # 3. scan_folder survives corrupt, huge, and unicode filenames and always completes progress
    edge_dir = tmp_path / "edge_files"
    edge_dir.mkdir()
    (edge_dir / "corrupt.png").write_bytes(b"NOT_A_PNG")
    (edge_dir / ("long_" + "x" * 150 + ".txt")).write_text("Long filename resilience check.", encoding="utf-8")
    (edge_dir / "unicode_日本語_café.txt").write_text("Multilingual notes for search.", encoding="utf-8")

    progress_events: list[dict] = []
    st.register_stage_callback(lambda evt: progress_events.append(evt) if evt.get("type") == "progress" else None)
    scanned = st.scan_folder(edge_dir, max_files=20, recursive=False)
    assert scanned >= 2
    assert progress_events and progress_events[-1]["current"] == progress_events[-1]["total"]

    st.close()
    monkeypatch.setattr(engine_main, "store", None)


def test_new_parsers_templates_operators_and_api_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient
    import main as engine_main
    from renamer import format_rename_template

    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    watch_dir = tmp_path / "workspace"
    watch_dir.mkdir()

    # 1. Archive (.zip) parser
    zip_p = watch_dir / "bundle.zip"
    with zipfile.ZipFile(zip_p, "w") as zf:
        zf.writestr("readme.md", "# Archive Readme")
        zf.writestr("data/sample.csv", "a,b\n1,2\n")
    p_zip = parse_file(zip_p)
    assert p_zip is not None and p_zip.modality == "archive"
    assert p_zip.metadata.get("totalEntries") == 2
    assert any(e["name"].endswith("readme.md") for e in p_zip.metadata.get("entries", []))

    # 2. Multi-sheet Excel parser
    xlsx_p = watch_dir / "multi_budget.xlsx"
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Summary"
    ws1.append(["Month", "Spend"])
    ws1.append(["Jan", 400])
    ws2 = wb.create_sheet("Travel")
    ws2.append(["City", "Hotel"])
    ws2.append(["Tokyo", 950])
    wb.save(xlsx_p)
    p_xlsx = parse_file(xlsx_p)
    assert p_xlsx is not None and len(p_xlsx.metadata.get("sheetsData", {})) == 2
    assert ["Tokyo", "950"] in p_xlsx.metadata["sheetsData"]["Travel"]["rows"]

    # 3. Receipt structured extraction + OCR operator
    receipt_p = watch_dir / "IMG_9991.txt"
    receipt_p.write_text(
        "Acme Roasters Coffee\nInvoice # INV-4412\nDate: 2026-04-12\nTotal: $42.50\nThank you!",
        encoding="utf-8",
    )
    p_rec = parse_file(receipt_p)
    assert p_rec is not None
    assert p_rec.metadata.get("receiptFields", {}).get("total") == "$42.50"
    assert p_rec.metadata.get("receiptFields", {}).get("reference") == "INV-4412"

    # 4. Duplicate & markdown files
    md_p = watch_dir / "architecture.md"
    md_p.write_text("# Good Shelves Spec\nLocal ONNX embeddings keep user files private.", encoding="utf-8")
    md_dup = watch_dir / "architecture_copy.md"
    md_dup.write_text("# Good Shelves Spec\nLocal ONNX embeddings keep user files private.", encoding="utf-8")

    # 5. Template formatter unit test
    formatted = format_rename_template(
        {"name": "IMG_9991.txt", "ext": "txt", "shelf": "Finance & Bills", "display_date": "Apr 12", "mtime": 1776000000.0},
        "{date}_{shelf}_{seq}_{orig}",
        seq_index=3,
    )
    assert "003" in formatted and formatted.endswith(".txt")

    # Setup VectorStore + TestClient
    st = VectorStore(db_path=tmp_path / "full.db")
    st.complete_setup([str(watch_dir)])
    monkeypatch.setattr(engine_main, "store", st)
    client = TestClient(engine_main.app)

    h = client.get("/health").json()
    assert h["status"] == "ok" and h["app"] == "good-shelves"

    state = st.get_all_state()
    rec_file = next(f for f in state["files"] if f["name"] == "IMG_9991.txt")
    md_file = next(f for f in state["files"] if f["name"] == "architecture.md")

    # 6. Search operators + Similar files + Ask Your Shelves
    st.set_file_tags(md_file["id"], ["spec"])
    op_res = st.search_semantic("local onnx tag:spec -coffee")
    assert any(r["id"] == md_file["id"] for r in op_res["results"])

    sim_resp = client.post("/search/similar", json={"fileId": md_file["id"], "limit": 5})
    assert sim_resp.status_code == 200
    assert sim_resp.json()["sourceFile"]["id"] == md_file["id"]

    ask_resp = client.post("/ask", json={"question": "How does Good Shelves keep files private?"})
    assert ask_resp.status_code == 200
    assert len(ask_resp.json()["citations"]) >= 1
    assert "private" in ask_resp.json()["answer"].lower() or "onnx" in ask_resp.json()["answer"].lower()

    # 7. Batch Rename Template Studio
    prev_tpl = client.post("/rename/template/preview", json={"fileIds": [rec_file["id"]], "template": "receipt_{date}_{seq}"})
    assert prev_tpl.status_code == 200 and len(prev_tpl.json()["previews"]) == 1
    exec_tpl = client.post("/rename/template/execute", json={"fileIds": [rec_file["id"]], "template": "receipt_{date}_{seq}"})
    assert exec_tpl.status_code == 200 and exec_tpl.json()["renamedCount"] == 1
    renamed_rec = next(f for f in exec_tpl.json()["state"]["files"] if f["id"] == rec_file["id"])
    assert renamed_rec["name"].startswith("receipt_")

    # 8. Custom Smart Shelf CRUD & Rename
    sh_resp = client.post("/shelves/custom", json={"name": "Q2 Receipts", "fileIds": [rec_file["id"]], "query": "receipt"})
    assert sh_resp.status_code == 200
    sh_id = sh_resp.json()["shelfId"]
    ren_sh = client.post("/shelves/rename", json={"shelfId": sh_id, "newName": "Receipts 2026"})
    assert ren_sh.status_code == 200
    assert any(s["name"] == "Receipts 2026" for s in ren_sh.json()["state"]["shelves"])
    del_sh = client.delete(f"/shelves/custom/{sh_id}")
    assert del_sh.status_code == 200

    # 9. Folders browser + rename + copy/paste + delete
    mk = client.post("/folders/create", json={"parentPath": str(watch_dir), "folderName": "TempBox"})
    assert mk.status_code == 200
    temp_dir = mk.json()["path"]
    br = client.get("/folders/browse", params={"path": str(watch_dir)})
    assert br.status_code == 200 and any(d["name"] == "TempBox" for d in br.json()["folders"])
    rn_dir = client.post("/folders/rename", json={"path": temp_dir, "newName": "RenamedBox"})
    assert rn_dir.status_code == 200
    renamed_dir = rn_dir.json()["path"]
    paste_resp = client.post("/files/paste", json={"fileIds": [md_file["id"]], "targetFolder": renamed_dir, "mode": "copy"})
    assert paste_resp.status_code == 200 and paste_resp.json()["count"] == 1
    rm_dir = client.post("/folders/delete", json={"path": renamed_dir})
    assert rm_dir.status_code == 200

    # 10. Reversible Trash -> List -> Restore -> Trash -> Empty
    tr_resp = client.post("/file/trash", json={"fileId": rec_file["id"]})
    assert tr_resp.status_code == 200
    tlist = client.get("/trash/list").json()
    assert len(tlist["trashed"]) >= 1
    tid = tlist["trashed"][0]["id"]
    rst = client.post("/trash/restore", json={"fileId": tid})
    assert rst.status_code == 200
    st.trash_file(rec_file["id"])
    emp = client.post("/trash/empty")
    assert emp.status_code == 200 and emp.json()["removedCount"] >= 1

    # 11. Insights, Excerpt, Duplicate check, Battery pause, Rescan all_files
    ins = client.get("/insights").json()
    assert ins["totalFiles"] >= 2 and len(ins["byModality"]) >= 1
    exc = client.get("/file/excerpt", params={"file_id": md_file["id"]}).json()
    assert "Good Shelves Spec" in exc["content"]
    md_dup_file = next(f for f in state["files"] if f["name"] == "architecture_copy.md")
    assert st.check_duplicate_for_file(md_dup_file["id"]) is not None
    st.update_setting("battery_pause", True)
    assert isinstance(st.is_paused_on_battery(), bool)
    rescan = client.post("/index/rescan", json={"all_files": True})
    assert rescan.status_code == 200

    st.close()
    monkeypatch.setattr(engine_main, "store", None)


def test_audit_fixes_exclude_operators_shelf_override_swap_and_folder_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    import main as engine_main

    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    watch_dir = tmp_path / "audit_ws"
    watch_dir.mkdir()

    (watch_dir / "doc_keep.md").write_text(
        "# Quarterly Report\nFinal approved annual budget summary for Acme Corp.",
        encoding="utf-8",
    )
    (watch_dir / "doc_draft.md").write_text(
        "# Quarterly Report\nDraft scratchpad rough notes for Acme Corp budget.",
        encoding="utf-8",
    )
    (watch_dir / "IMG_4040.txt").write_text(
        "Acme Roasters Coffee\nInvoice # INV-7788\nDate: 2026-04-12\nTotal: $18.25",
        encoding="utf-8",
    )
    (watch_dir / "dup_a.md").write_text(
        "# Local Architecture\nPure offline vector indexing with deterministic ONNX.",
        encoding="utf-8",
    )
    (watch_dir / "dup_b.md").write_text(
        "# Local Architecture\nPure offline vector indexing with deterministic ONNX.",
        encoding="utf-8",
    )
    trash_sub = watch_dir / "TrashMeBox"
    trash_sub.mkdir()
    (trash_sub / "note_in_box.txt").write_text(
        "Important project scratchpad inside trashable folder.",
        encoding="utf-8",
    )

    st = VectorStore(db_path=tmp_path / "audit.db")
    st.complete_setup([str(watch_dir)])
    monkeypatch.setattr(engine_main, "store", st)
    client = TestClient(engine_main.app)

    state = st.get_all_state()
    doc_keep = next(f for f in state["files"] if f["name"] == "doc_keep.md")
    doc_draft = next(f for f in state["files"] if f["name"] == "doc_draft.md")
    img_file = next(f for f in state["files"] if f["name"] == "IMG_4040.txt")

    # 1. Manual shelf override + persistence across recluster_all()
    sh_override = client.post(
        "/file/shelf",
        json={"fileId": doc_keep["id"], "shelfId": "shelf-screenshots"},
    )
    assert sh_override.status_code == 200
    assert sh_override.json()["shelfId"] == "shelf-screenshots"
    st.recluster_all()
    persisted = st.get_file_by_id(doc_keep["id"])
    assert persisted is not None and persisted["shelfId"] == "shelf-screenshots"

    # 2. Search -exclude & quoted shelf:"..." operators
    excl_res = client.post("/search", json={"query": "budget -draft"})
    assert excl_res.status_code == 200
    excl_ids = {r["id"] for r in excl_res.json()["results"]}
    assert doc_keep["id"] in excl_ids
    assert doc_draft["id"] not in excl_ids

    quoted_res = client.post(
        "/search",
        json={"query": 'budget shelf:"Screenshots & Captures"'},
    )
    assert quoted_res.status_code == 200
    quoted_ids = {r["id"] for r in quoted_res.json()["results"]}
    assert doc_keep["id"] in quoted_ids
    assert doc_draft["id"] not in quoted_ids

    # 3. Lookalike keep/copy swap toggle
    state_now = st.get_all_state()
    assert len(state_now["lookalikes"]) >= 1
    pair = state_now["lookalikes"][0]
    orig_keep_id = pair["keep"]["id"]
    orig_copy_id = pair["copy"]["id"]

    sw1 = client.post("/lookalikes/swap", json={"pairId": pair["id"]})
    assert sw1.status_code == 200
    assert sw1.json()["swapped"] is True
    swapped_pair = next(p for p in sw1.json()["state"]["lookalikes"] if p["id"] == pair["id"])
    assert swapped_pair["swapped"] is True
    assert swapped_pair["keep"]["id"] == orig_copy_id
    assert swapped_pair["copy"]["id"] == orig_keep_id

    sw2 = client.post("/lookalikes/swap", json={"pairId": pair["id"]})
    assert sw2.status_code == 200
    assert sw2.json()["swapped"] is False
    restored_pair = next(p for p in sw2.json()["state"]["lookalikes"] if p["id"] == pair["id"])
    assert restored_pair["keep"]["id"] == orig_keep_id
    assert restored_pair["copy"]["id"] == orig_copy_id

    # 4. Reversible folder delete + history undo
    assert any(f["name"] == "note_in_box.txt" for f in state_now["files"])
    rm_resp = client.post("/folders/delete", json={"path": str(trash_sub)})
    assert rm_resp.status_code == 200
    assert not trash_sub.exists()
    assert not any(f["name"] == "note_in_box.txt" for f in rm_resp.json()["state"]["files"])

    undo_folder = client.post("/history/undo", json={})
    assert undo_folder.status_code == 200
    assert trash_sub.exists() and (trash_sub / "note_in_box.txt").exists()
    assert any(f["name"] == "note_in_box.txt" for f in undo_folder.json()["state"]["files"])

    # 5. Smart Clean (rename + organize) single-step undo & redo
    orig_img_path = Path(img_file["path"])
    orig_img_name = img_file["name"]
    sc = client.post(
        "/smart-clean/execute",
        json={"applyRenames": True, "archiveDuplicates": False, "organizeFolders": True},
    )
    assert sc.status_code == 200
    sc_data = sc.json()
    assert sc_data["renamedCount"] >= 1
    assert sc_data["movedCount"] >= 1
    img_after = next(f for f in sc_data["state"]["files"] if f["id"] == img_file["id"])
    after_path = Path(img_after["path"])
    assert img_after["name"] != orig_img_name
    assert after_path != orig_img_path
    assert after_path.exists() and not orig_img_path.exists()

    undo_sc = client.post("/history/undo", json={})
    assert undo_sc.status_code == 200
    img_undone = next(f for f in undo_sc.json()["state"]["files"] if f["id"] == img_file["id"])
    assert img_undone["name"] == orig_img_name
    assert Path(img_undone["path"]) == orig_img_path
    assert orig_img_path.exists() and not after_path.exists()

    redo_sc = client.post("/history/redo", json={})
    assert redo_sc.status_code == 200
    img_redone = next(f for f in redo_sc.json()["state"]["files"] if f["id"] == img_file["id"])
    assert img_redone["name"] == img_after["name"]
    assert Path(img_redone["path"]) == after_path
    assert after_path.exists() and not orig_img_path.exists()

    # 6. Clear activity resets completed rename state & clears logs
    assert img_redone["renamed"] is True
    clr = client.post("/activity/clear")
    assert clr.status_code == 200
    clr_state = clr.json()["state"]
    assert clr_state["actionHistory"] == []
    assert clr_state["renameHistory"] == []
    assert clr_state["recentEvents"] == []
    img_cleared = next(f for f in clr_state["files"] if f["id"] == img_file["id"])
    assert img_cleared["renamed"] is False
    assert img_cleared.get("suggestedName") is None

    # 7. Remove watched folder stops watching and prunes index without deleting disk files
    extra_dir = tmp_path / "extra_watch"
    extra_dir.mkdir()
    extra_file = extra_dir / "extra_note.txt"
    extra_file.write_text("Temporary project notes for watched folder removal.", encoding="utf-8")
    add_res = client.post("/folders/add", json={"path": str(extra_dir)})
    assert add_res.status_code == 200
    assert any(f["name"] == "extra_note.txt" for f in add_res.json()["state"]["files"])

    rem_res = client.post("/folders/remove", json={"path": str(extra_dir)})
    assert rem_res.status_code == 200
    rem_state = rem_res.json()["state"]
    assert extra_file.exists()
    assert not any(Path(w["path"]).resolve() == extra_dir.resolve() for w in rem_state["watchedFolders"])
    assert not any(f["name"] == "extra_note.txt" for f in rem_state["files"])

    st.close()
    monkeypatch.setattr(engine_main, "store", None)


def test_nl_query_list_includes_dates_exif_and_conversational(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image
    from fastapi.testclient import TestClient
    import main as engine_main

    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    watch_dir = tmp_path / "nl_workspace"
    watch_dir.mkdir()

    # Photo 1 with real EXIF DateTimeOriginal + camera Make/Model (stored under generic filename!)
    photo_exif = watch_dir / "DCIM_001.jpg"
    img1 = Image.new("RGB", (400, 300), color=(210, 120, 80))
    exif_obj = img1.getexif()
    exif_obj[271] = "Apple"
    exif_obj[272] = "iPhone 15"
    exif_obj[306] = "2025:10:18 14:20:05"
    exif_obj[36867] = "2025:10:18 14:20:05"
    img1.save(photo_exif, format="JPEG", exif=exif_obj.tobytes())

    # Photo 2 from a different date
    photo_other = watch_dir / "IMG_20240502_100000.png"
    Image.new("RGB", (300, 300), color=(40, 140, 200)).save(photo_other, format="PNG")

    # Photo 3 screenshot
    shot_p = watch_dir / "Screenshot_budget_view.png"
    Image.new("RGB", (600, 350), color=(240, 240, 240)).save(shot_p, format="PNG")

    # Docs for phrase inclusion & exclusions
    doc1 = watch_dir / "solar_notes.md"
    doc1.write_text("# Solar Notes\nThis architecture uses solar tracking with dual sensors.", encoding="utf-8")
    doc2 = watch_dir / "solar_notes_draft.md"
    doc2.write_text("# Solar Draft\nContains solar tracking rough outline.", encoding="utf-8")
    doc3 = watch_dir / "recipe_card.txt"
    doc3.write_text("Coffee Espresso Guide\nBrew ratio 1:2 at 93C.", encoding="utf-8")

    # Verify EXIF parser directly
    parsed_photo = parse_file(photo_exif)
    assert parsed_photo is not None
    assert parsed_photo.created_date == "2025-10-18"
    assert parsed_photo.metadata.get("takenDate") == "2025-10-18"
    assert "iphone 15" in str(parsed_photo.metadata.get("camera", "")).lower()

    st = VectorStore(db_path=tmp_path / "nl.db")
    st.complete_setup([str(watch_dir)], user_name="paul")
    monkeypatch.setattr(engine_main, "store", st)
    client = TestClient(engine_main.app)

    # 1. "list things that includes 'solar tracking'" (strict phrase + exclude draft)
    r_inc = client.post("/ask", json={"question": 'list things that includes "solar tracking" without draft'})
    assert r_inc.status_code == 200
    d_inc = r_inc.json()
    assert d_inc["intentMode"] == "list"
    names_inc = [c["fileName"] for c in d_inc["citations"]]
    assert names_inc == ["solar_notes.md"]
    assert d_inc["answer"].startswith("Paul,")

    # 2. Unquoted "list things that includes dual sensors"
    r_unq = client.post("/ask", json={"question": "list things that includes dual sensors"})
    assert r_unq.status_code == 200
    assert [c["fileName"] for c in r_unq.json()["citations"]] == ["solar_notes.md"]

    # 3. "list all photos that are taken on Oct 18, 2025" (EXIF capture date + strict image filter)
    r_oct = client.post("/ask", json={"question": "list all photos that are taken on Oct 18, 2025"})
    assert r_oct.status_code == 200
    d_oct = r_oct.json()
    assert d_oct["intentMode"] == "list"
    assert [c["fileName"] for c in d_oct["citations"]] == ["DCIM_001.jpg"]

    # 4. Camera query: "list all photos taken with iPhone"
    r_cam = client.post("/ask", json={"question": "list all photos taken with iPhone"})
    assert r_cam.status_code == 200
    assert [c["fileName"] for c in r_cam.json()["citations"]] == ["DCIM_001.jpg"]

    # 5. Count query: "how many files include 'solar tracking'?"
    r_cnt = client.post("/ask", json={"question": "how many files include 'solar tracking'?"})
    assert r_cnt.status_code == 200
    d_cnt = r_cnt.json()
    assert d_cnt["intentMode"] == "count"
    assert d_cnt["totalMatched"] == 2
    assert "2 matching" in d_cnt["answer"]

    # 6. Zero-match strict filter: honest explanation, zero unrelated citations
    r_empty = client.post("/ask", json={"question": "list all photos taken on Jan 5, 2021"})
    assert r_empty.status_code == 200
    d_empty = r_empty.json()
    assert d_empty["citations"] == []
    assert "Paul," in d_empty["answer"]

    # 7. Conversational small talk & unrelated question with user's name
    for msg in ("hi", "tell me a joke", "whats for dinner?"):
        rc = client.post("/ask", json={"question": msg}).json()
        assert rc["mode"] == "conversational"
        assert rc["citations"] == []
        assert "Paul" in rc["answer"]

    st.close()
    monkeypatch.setattr(engine_main, "store", None)


def test_release_hardening_auth_token_trash_original_path_and_folder_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    import main as engine_main

    fake_home = tmp_path / "home"
    docs_dir = fake_home / "Documents"
    docs_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    watch_dir = tmp_path / "release_ws"
    watch_dir.mkdir()
    sub_dir = watch_dir / "SubFolder"
    sub_dir.mkdir()
    f1 = sub_dir / "important_contract.txt"
    f1.write_text("Contract terms and delivery milestones for 2026.", encoding="utf-8")

    st = VectorStore(db_path=tmp_path / "release.db")
    st.complete_setup([str(watch_dir)])
    monkeypatch.setattr(engine_main, "store", st)
    monkeypatch.setenv("GOOD_SHELVES_TOKEN", "secret_test_token_123")

    client = TestClient(engine_main.app)

    # 1. Token auth protects API endpoints while /health reports tokenValid cleanly
    h_no_tok = client.get("/health")
    assert h_no_tok.status_code == 200
    assert h_no_tok.json()["authRequired"] is True
    assert h_no_tok.json()["tokenValid"] is False

    h_ok = client.get("/health", headers={"X-GS-Token": "secret_test_token_123"})
    assert h_ok.status_code == 200
    assert h_ok.json()["tokenValid"] is True

    assert client.get("/state").status_code == 401
    assert client.get("/state", headers={"X-GS-Token": "wrong"}).status_code == 401
    state_resp = client.get("/state", headers={"X-GS-Token": "secret_test_token_123"})
    assert state_resp.status_code == 200

    # 2. Protected folders (home, Documents, root) cannot be deleted or renamed
    headers = {"X-GS-Token": "secret_test_token_123"}
    assert client.post("/folders/delete", json={"path": str(fake_home)}, headers=headers).status_code == 400
    assert client.post("/folders/delete", json={"path": str(docs_dir)}, headers=headers).status_code == 400
    assert client.post("/folders/rename", json={"path": str(docs_dir), "newName": "Nope"}, headers=headers).status_code == 400

    # 3. Trashing a file and clearing activity log still preserves original_path for restore
    target_file = next(f for f in state_resp.json()["files"] if f["name"] == "important_contract.txt")
    tr = client.post("/file/trash", json={"fileId": target_file["id"]}, headers=headers)
    assert tr.status_code == 200
    assert not f1.exists()

    clr = client.post("/activity/clear", headers=headers)
    assert clr.status_code == 200
    assert clr.json()["state"]["actionHistory"] == []

    rst = client.post("/trash/restore", json={"fileId": target_file["id"]}, headers=headers)
    assert rst.status_code == 200
    assert f1.exists()

    # 4. External folder deletion prunes child indexed rows via prefix removal
    rm_prefix = client.post("/index/remove", json={"path": str(sub_dir)}, headers=headers)
    assert rm_prefix.status_code == 200
    assert rm_prefix.json()["removed"] is True
    assert rm_prefix.json()["removedCount"] >= 1
    assert not any(f["name"] == "important_contract.txt" for f in rm_prefix.json()["state"]["files"])

    st.close()
    monkeypatch.delenv("GOOD_SHELVES_TOKEN", raising=False)
    monkeypatch.setattr(engine_main, "store", None)


def test_large_scale_clustering_dedup_and_thumbnail_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import numpy as np
    from PIL import Image
    from fastapi.testclient import TestClient
    import main as engine_main
    from clustering import run_shelf_clustering
    from deduplicator import find_lookalike_pairs

    # 1. Verify sparse-graph clustering & chunked deduplicator at >1000 scale without O(N^2) blowup
    rng = np.random.default_rng(seed=7)
    n_items = 1100
    raw_vecs = rng.standard_normal((n_items, 512)).astype(np.float32)
    raw_vecs[1] = raw_vecs[0]  # Exact vector duplicate pair
    norms = np.linalg.norm(raw_vecs, axis=1, keepdims=True)
    vecs = raw_vecs / np.where(norms > 1e-12, norms, 1.0)
    metas = [
        {
            "id": f"item_{i}",
            "name": f"doc_{i}.txt",
            "path": f"/tmp/ws/doc_{i}.txt",
            "folder_short": "~/ws",
            "ext": "txt",
            "display_date": "Nov 20",
            "size_bytes": 1200,
            "sha256": f"sha_{i}",
            "mtime": float(1700000000 + i),
            "modality": "document",
            "extracted_text": "shared quarterly summary text" if i < 2 else f"topic note {i}",
            "ocr_text": "",
            "title": "shared quarterly summary" if i < 2 else f"note {i}",
        }
        for i in range(n_items)
    ]
    shelves, updates, _sil = run_shelf_clustering(metas, vecs)
    assert len(shelves) >= 1
    assert len(updates) == n_items
    pairs = find_lookalike_pairs(metas, vecs, {})
    assert any(p["keep"]["id"] in {"item_0", "item_1"} and p["copy"]["id"] in {"item_0", "item_1"} for p in pairs)

    # 2. Verify /state omits inline thumbnailB64 and /file/thumbnail serves JPEG bytes
    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    ws = tmp_path / "img_ws"
    ws.mkdir()
    img_path = ws / "sunset_beach.png"
    Image.new("RGB", (240, 180), color=(240, 140, 60)).save(img_path, format="PNG")

    st = VectorStore(db_path=tmp_path / "thumb.db")
    st.complete_setup([str(ws)])
    monkeypatch.setattr(engine_main, "store", st)
    client = TestClient(engine_main.app)

    state = client.get("/state").json()
    img_rec = next(f for f in state["files"] if f["name"] == "sunset_beach.png")
    assert img_rec["thumbnailB64"] is None
    assert img_rec["hasThumbnail"] is True

    thumb_resp = client.get("/file/thumbnail", params={"file_id": img_rec["id"]})
    assert thumb_resp.status_code == 200
    assert thumb_resp.headers["content-type"].startswith("image/jpeg")
    assert len(thumb_resp.content) > 100

    # 3. Verify SigLIP concept classification works when SigLIP model is installed
    from embedder import MultimodalEmbedder
    from vision_models import resolve_vision_model_paths
    repo_models = Path(__file__).resolve().parent.parent / "models"
    if resolve_vision_model_paths(repo_models, "siglip-base-224")["vision"] is not None:
        cat_sample = Path(__file__).resolve().parent.parent.parent / "samples" / "photos-and-diagrams" / "tabby-cat-portrait.jpg"
        if cat_sample.exists():
            emb_sig = MultimodalEmbedder(models_dir=repo_models, vision_model_id="siglip-base-224")
            tags = emb_sig.classify_image_concepts(Image.open(cat_sample))
            assert any(t in {"cat", "kitten", "pet"} for t in tags), f"expected cat tags, got {tags}"

    st.close()
    monkeypatch.setattr(engine_main, "store", None)


def test_ai_nl_file_folder_operations_confirmation_and_smart_assistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from PIL import Image
    import main as engine_main

    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    ws = tmp_path / "smart_ai_ws"
    downloads_dir = ws / "Downloads"
    finance_dir = ws / "Finance"
    incoming_dir = ws / "Incoming"
    sub_folder = incoming_dir / "Batch2026"
    for d in (downloads_dir, finance_dir, sub_folder):
        d.mkdir(parents=True)

    receipt_a = downloads_dir / "coffee_receipt.txt"
    receipt_a.write_text("Coffee Cafe Receipt Total USD 14.50", encoding="utf-8")
    receipt_b = downloads_dir / "cloud_invoice_receipt.txt"
    receipt_b.write_text("Receipt for cloud hosting USD 49.00", encoding="utf-8")
    notes = downloads_dir / "meeting_notes.txt"
    notes.write_text("Weekly project standup notes.", encoding="utf-8")

    pic_dl = downloads_dir / "beach_sunset.png"
    Image.new("RGB", (120, 90), color=(210, 120, 70)).save(pic_dl, format="PNG")
    folder_note = sub_folder / "batch_item.txt"
    folder_note.write_text("Archived batch document.", encoding="utf-8")

    st = VectorStore(db_path=tmp_path / "ai_actions.db")
    st.complete_setup([str(ws)], user_name="paul")
    st.update_setting("ollama_enabled", False)
    monkeypatch.setattr(engine_main, "store", st)

    client = TestClient(engine_main.app)
    state0 = client.get("/state").json()
    pic_rec = next(f for f in state0["files"] if f["name"] == "beach_sunset.png")

    # 1. Contextual "delete this picture from Downloads" with selectedFileId -> zero mutation before confirm!
    del_preview = client.post(
        "/ask",
        json={"question": "delete this picture from Downloads", "selectedFileId": pic_rec["id"]},
    ).json()
    assert del_preview["mode"] == "action-plan"
    assert del_preview["proposedAction"]["actionType"] == "trash"
    assert del_preview["proposedAction"]["destructive"] is True
    assert del_preview["proposedAction"]["reversible"] is True
    assert [i["fileId"] for i in del_preview["proposedAction"]["items"]] == [pic_rec["id"]]
    assert pic_dl.exists(), "file must NOT be deleted until user confirms!"

    # Execute confirmation -> moves safely to GoodShelves_Trash and supports restore
    del_exec = client.post(
        "/ask/execute",
        json={
            "actionType": "trash",
            "fileIds": [pic_rec["id"]],
        },
    ).json()
    assert del_exec["ok"] is True
    assert not pic_dl.exists()

    restore_preview = client.post("/ask", json={"question": "restore trashed files"}).json()
    assert restore_preview["proposedAction"]["actionType"] == "restore_trash"
    restore_exec = client.post(
        "/ask/execute",
        json={
            "actionType": "restore_trash",
            "fileIds": [i["fileId"] for i in restore_preview["proposedAction"]["items"]],
        },
    ).json()
    assert restore_exec["ok"] is True
    assert pic_dl.exists()

    # 2. NL bulk move: "can you move all receipts from Downloads to Finance"
    mv_preview = client.post(
        "/ask",
        json={"question": "can you move all receipts from Downloads to Finance"},
    ).json()
    assert mv_preview["mode"] == "action-plan"
    act_mv = mv_preview["proposedAction"]
    assert act_mv["actionType"] == "move"
    moved_names = {i["fileName"] for i in act_mv["items"]}
    assert moved_names == {"coffee_receipt.txt", "cloud_invoice_receipt.txt"}
    assert receipt_a.exists() and not (finance_dir / "coffee_receipt.txt").exists()

    mv_exec = client.post(
        "/ask/execute",
        json={
            "actionType": act_mv["actionType"],
            "fileIds": [i["fileId"] for i in act_mv["items"]],
            **(act_mv["payload"] or {}),
        },
    ).json()
    assert mv_exec["ok"] is True
    assert mv_exec["count"] == 2
    assert (finance_dir / "coffee_receipt.txt").exists()
    assert (finance_dir / "cloud_invoice_receipt.txt").exists()

    # 3. NL tag, create folder, create shelf, and move folder with confirmation
    tag_preview = client.post(
        "/ask",
        json={"question": "tag meeting_notes with priority"},
    ).json()
    assert tag_preview["proposedAction"]["actionType"] == "tag"
    tag_exec = client.post(
        "/ask/execute",
        json={
            "actionType": "tag",
            "fileIds": [i["fileId"] for i in tag_preview["proposedAction"]["items"]],
            **(tag_preview["proposedAction"]["payload"] or {}),
        },
    ).json()
    assert tag_exec["ok"] is True
    notes_rec = next(f for f in tag_exec["state"]["files"] if f["name"] == "meeting_notes.txt")
    assert "priority" in notes_rec["tags"]

    cf_preview = client.post("/ask", json={"question": "create a folder called Trips"}).json()
    assert cf_preview["proposedAction"]["actionType"] == "create_folder"
    cf_exec = client.post(
        "/ask/execute",
        json={"actionType": "create_folder", **(cf_preview["proposedAction"]["payload"] or {})},
    ).json()
    assert cf_exec["ok"] is True
    assert Path(cf_exec["path"]).exists()

    cs_preview = client.post(
        "/ask",
        json={"question": "create a shelf called Tax 2026 for all receipts"},
    ).json()
    assert cs_preview["proposedAction"]["actionType"] == "create_shelf"
    assert len(cs_preview["proposedAction"]["items"]) == 2
    cs_exec = client.post(
        "/ask/execute",
        json={
            "actionType": "create_shelf",
            "fileIds": [i["fileId"] for i in cs_preview["proposedAction"]["items"]],
            **(cs_preview["proposedAction"]["payload"] or {}),
        },
    ).json()
    assert cs_exec["ok"] is True
    assert any(s["name"] == "Tax 2026" for s in cs_exec["state"]["shelves"])

    # Folder move confirmation: "move folder Batch2026 to Trips"
    mf_preview = client.post("/ask", json={"question": "move folder Batch2026 to Trips"}).json()
    assert mf_preview["proposedAction"]["actionType"] == "move_folder"
    mf_exec = client.post(
        "/ask/execute",
        json={"actionType": "move_folder", **(mf_preview["proposedAction"]["payload"] or {})},
    ).json()
    assert mf_exec["ok"] is True
    assert (Path(cf_exec["path"]) / "Batch2026" / "batch_item.txt").exists()

    # 4. Smart workspace insights, math utilities, and conversational shelf guide
    math_resp = client.post("/ask", json={"question": "what is 15% of 400?"}).json()
    assert math_resp["mode"] == "conversational"
    assert "60" in math_resp["answer"]

    ov_resp = client.post("/ask", json={"question": "what's on my shelves?"}).json()
    assert ov_resp["mode"] == "workspace-insight"
    assert "shelf" in ov_resp["answer"].lower()

    guide_resp = client.post("/ask", json={"question": "what is the difference between a shelf and a folder?"}).json()
    assert guide_resp["mode"] == "workspace-insight"
    assert "shelf" in guide_resp["answer"].lower()

    st.close()
    monkeypatch.setattr(engine_main, "store", None)


def test_vision_model_download_progress_endpoint_and_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    import main as engine_main
    import vector_store as vs_mod

    ws = tmp_path / "dl_ws"
    ws.mkdir()
    (ws / "hello.txt").write_text("sample doc", encoding="utf-8")

    st = VectorStore(db_path=tmp_path / "dl.db")
    st.complete_setup([str(ws)])
    monkeypatch.setattr(engine_main, "store", st)
    client = TestClient(engine_main.app)

    # Idle before download starts
    idle_prog = client.get("/vision/download-progress").json()
    assert idle_prog["active"] is False
    assert idle_prog["status"] == "idle"

    observed_percents: list[int] = []
    observed_events: list[dict[str, object]] = []
    st.register_stage_callback(lambda evt: observed_events.append(evt))

    # Exercise download_vision_model + tqdm_class byte bridge directly
    import sys
    import types
    fake_src_vis = tmp_path / "hub_vis.onnx"
    fake_src_vis.write_bytes(b"vis_bytes")
    fake_src_txt = tmp_path / "hub_txt.onnx"
    fake_src_txt.write_bytes(b"txt_bytes")
    fake_src_tok = tmp_path / "hub_tok.json"
    fake_src_tok.write_text("{}", encoding="utf-8")

    def _mock_hf_dl(repo_id: str, filename: str, tqdm_class=None, **kwargs):  # type: ignore[no-untyped-def]
        if tqdm_class is not None:
            bar = tqdm_class(total=1000, initial=0, desc=filename)
            bar.update(500)
            bar.update(500)
            bar.close()
        if "vision" in filename:
            return str(fake_src_vis)
        if "text" in filename:
            return str(fake_src_txt)
        return str(fake_src_tok)

    fake_hf_mod = types.ModuleType("huggingface_hub")
    fake_hf_mod.hf_hub_download = _mock_hf_dl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf_mod)

    hook_updates: list[dict[str, object]] = []
    from vision_models import download_vision_model
    download_vision_model(tmp_path / "direct_dl", "clip-vit-b16", detail_cb=lambda d: hook_updates.append(d))
    assert any(d.get("percent", 0) >= 30 and d.get("stage") == "vision" for d in hook_updates)
    assert any(d.get("stage") == "text" for d in hook_updates)
    assert hook_updates[-1]["percent"] == 100

    def _fake_dl(models_dir: Path, model_id: str, progress_cb=None, detail_cb=None):  # type: ignore[no-untyped-def]
        target_dir = models_dir / "vision_catalog" / model_id
        target_dir.mkdir(parents=True, exist_ok=True)
        for pct, fname, dl_b, tot_b in [
            (25, "vision.onnx", 50 * 1024 * 1024, 200 * 1024 * 1024),
            (74, "vision.onnx", 200 * 1024 * 1024, 200 * 1024 * 1024),
            (92, "text.onnx", 40 * 1024 * 1024, 50 * 1024 * 1024),
            (100, "tokenizer.json", 1, 1),
        ]:
            if detail_cb:
                detail_cb({
                    "modelId": model_id,
                    "modelName": "CLIP ViT-B/16 (Detailed)",
                    "status": "downloading",
                    "stage": "vision",
                    "fileName": fname,
                    "downloadedBytes": dl_b,
                    "totalBytes": tot_b,
                    "percent": pct,
                    "message": f"Downloading {fname}…",
                })
            snap = client.get("/vision/download-progress").json()
            assert snap["active"] is True
            assert snap["modelId"] == model_id
            observed_percents.append(snap["percent"])
        return {"ok": True, "modelId": model_id, "dir": str(target_dir)}

    monkeypatch.setattr(st.embedder, "models_dir", tmp_path / "fresh_models")
    monkeypatch.setattr(vs_mod, "download_vision_model", _fake_dl)
    monkeypatch.setattr(st.embedder, "set_vision_model", lambda mid: None)

    resp = client.post("/vision/select", json={"modelId": "clip-vit-b16", "download": True})
    assert resp.status_code == 200
    assert observed_percents == [25, 74, 92, 100]
    assert any(
        e.get("type") == "progress" and "downloading" in str(e.get("operation", "")).lower() and e.get("current") == 74
        for e in observed_events
    )

    # Cleared back to idle after completion
    done_prog = client.get("/vision/download-progress").json()
    assert done_prog["active"] is False

    st.close()
    monkeypatch.setattr(engine_main, "store", None)


def test_preview_sanitization_rtf_and_advanced_multi_step_ai_assistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    import main as engine_main
    from parsers import _parse_rtf_text, clean_extracted_text

    # 1. Verify preview sanitization strips '?', PUA bullets, U+FFFD, ligatures, and RTF fallback '?'
    rtf_sample = r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\u8226? First item with \'93smart quotes\'94\par ? Broken bullet\par \u8212? Dash}"
    rtf_clean = _parse_rtf_text(rtf_sample)
    assert "?" not in rtf_clean
    assert "First item" in rtf_clean
    assert "smart quotes" in rtf_clean
    assert "•" in rtf_clean

    raw_pdf_junk = "\uf0b7 First point\n? Second point\nJohn?s ﬁle ﬂow\ufffd trans-\nformer\u200b!"
    sanitized = clean_extracted_text(raw_pdf_junk, preserve_newlines=True)
    assert "\ufffd" not in sanitized
    assert "\uf0b7" not in sanitized
    assert "\u200b" not in sanitized
    assert "file flow" in sanitized
    assert "transformer!" in sanitized
    assert "John's" in sanitized
    assert "? Second" not in sanitized

    # 2. Verify multi-step chained workflows, pending-plan refinement, comparison & smart choices
    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    ws = tmp_path / "adv_ai_ws"
    dl_dir = ws / "Downloads"
    dl_dir.mkdir(parents=True)
    r_txt = dl_dir / "coffee_receipt.txt"
    r_txt.write_text("Receipt Coffee Total USD 12.00\nLine 1 unchanged", encoding="utf-8")
    r_pdf_like = dl_dir / "travel_receipt.md"
    r_pdf_like.write_text("Receipt Flight Booking Total USD 320.00", encoding="utf-8")
    dup_a = ws / "report_v1.txt"
    dup_a.write_text("Quarterly budget projection\nRevenue 100k\nNotes original", encoding="utf-8")
    dup_b = ws / "report_v2.txt"
    dup_b.write_text("Quarterly budget projection\nRevenue 120k\nNotes updated", encoding="utf-8")

    st = VectorStore(db_path=tmp_path / "adv_ai.db")
    st.complete_setup([str(ws)], user_name="paul")
    st.update_setting("ollama_enabled", False)
    monkeypatch.setattr(engine_main, "store", st)
    client = TestClient(engine_main.app)

    # Broad cleanup asks clarifying choices cleanly
    broad = client.post("/ask", json={"question": "clean up my files"}).json()
    assert len(broad["choices"]) >= 2

    # Pending-action refinement ("exclude txt")
    mv_plan = client.post("/ask", json={"question": "tag all receipts with finance"}).json()
    pending_act = mv_plan["proposedAction"]
    assert pending_act and len(pending_act["items"]) == 2
    refined = client.post(
        "/ask",
        json={"question": "exclude travel_receipt", "pendingAction": pending_act},
    ).json()
    assert refined["proposedAction"] is not None
    assert [i["fileName"] for i in refined["proposedAction"]["items"]] == ["coffee_receipt.txt"]

    # Chained multi-step command: create folder, move receipts into it, and pin them
    chain = client.post(
        "/ask",
        json={"question": "create a folder called Tax 2026, move all receipts from Downloads into it, and pin them"},
    ).json()
    act_chain = chain["proposedAction"]
    assert act_chain is not None
    assert act_chain["actionType"] == "multi_step"
    assert len(act_chain["steps"]) == 3
    assert r_txt.exists()  # zero mutation before confirm

    exec_res = client.post(
        "/ask/execute",
        json={
            "actionType": "multi_step",
            "fileIds": [i["fileId"] for i in act_chain["items"]],
            "steps": act_chain["steps"],
        },
    ).json()
    assert exec_res["ok"] is True
    assert (dl_dir.parent / "Tax 2026" / "coffee_receipt.txt").exists()
    pinned_rec = next(f for f in exec_res["state"]["files"] if f["name"] == "coffee_receipt.txt")
    assert pinned_rec["pinned"] is True

    # File comparison returns clean Markdown diff + choices
    cmp_res = client.post("/ask", json={"question": "compare report_v1 and report_v2"}).json()
    assert "Comparison" in cmp_res["answer"]
    assert len(cmp_res["citations"]) == 2
    assert len(cmp_res["choices"]) >= 1

    # Verify AI action handlers for rename_single, rename_batch template, and archive_duplicates
    dup_file_ids = [
        f["id"] for f in st.get_all_state()["files"] if f["name"] in {"report_v1.txt", "report_v2.txt"}
    ]
    assert len(dup_file_ids) == 2

    single_rename = client.post(
        "/ask/execute",
        json={"actionType": "rename_single", "fileIds": [dup_file_ids[0]], "newName": "report_v1_clean"},
    ).json()
    assert single_rename["ok"] is True
    assert "report_v1_clean.txt" in single_rename["summary"]

    batch_tpl = client.post(
        "/ask/execute",
        json={"actionType": "rename_batch", "fileIds": [dup_file_ids[0]], "template": "Audit_{orig}_{seq}"},
    ).json()
    assert batch_tpl["ok"] is True
    assert batch_tpl["count"] == 1
    assert "Renamed 1 file(s)." == batch_tpl["summary"]

    dup_c = ws / "report_v2_copy.txt"
    dup_c.write_text("Quarterly budget projection\nRevenue 120k\nNotes updated", encoding="utf-8")
    st.index_single_file(dup_c, force=True, recluster=True)
    pairs = st.get_all_state()["lookalikes"]
    assert len(pairs) >= 1
    arch_res = client.post(
        "/ask/execute",
        json={"actionType": "archive_duplicates", "pairIds": [pairs[0]["id"]]},
    ).json()
    assert arch_res["ok"] is True
    assert arch_res["count"] == 1
    assert next(p for p in st.get_all_state()["lookalikes"] if p["id"] == pairs[0]["id"])["resolved"] is True

    st.close()
    monkeypatch.setattr(engine_main, "store", None)
