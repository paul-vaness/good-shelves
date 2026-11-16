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
