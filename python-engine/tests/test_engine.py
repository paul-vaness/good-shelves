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
