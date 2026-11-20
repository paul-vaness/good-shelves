#!/usr/bin/env python3
"""
Curated Online Sample Seed Workspace Fetcher for Good Shelves.

Cleans `samples/` and downloads a high-cohesion set of publicly available,
permissively licensed / public-domain files from arXiv, NIST, Wikimedia Commons,
Microsoft/Packt open repos, CourtBouillon WeasyPrint samples, pypdf, sqlite-vec,
and OCR receipt datasets into `samples/` for clean clustering, retrieval, OCR,
vision search, Tidy Names, Lookalikes, and multi-format Quick Look testing.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SampleSpec:
    rel_path: str
    url: str
    source_name: str
    license_note: str
    demonstrates: str


SAMPLES: list[SampleSpec] = [
    # -------------------------------------------------------------------------
    # 1. AI & NLP Research (Tight Academic Transformer/RAG Shelf)
    # -------------------------------------------------------------------------
    SampleSpec(
        rel_path="research-nlp/attention-is-all-you-need-2017.pdf",
        url="https://arxiv.org/pdf/1706.03762",
        source_name="arXiv:1706.03762 (Vaswani et al., 2017)",
        license_note="arXiv open-access preprint",
        demonstrates="Transformer architecture paper clustering & PDF Q&A",
    ),
    SampleSpec(
        rel_path="research-nlp/bert-pretraining-transformers-2018.pdf",
        url="https://arxiv.org/pdf/1810.04805",
        source_name="arXiv:1810.04805 (Devlin et al., 2018)",
        license_note="arXiv open-access preprint",
        demonstrates="Deep contextual language representation PDF retrieval",
    ),
    SampleSpec(
        rel_path="research-nlp/rag-knowledge-intensive-nlp-2020.pdf",
        url="https://arxiv.org/pdf/2005.11401",
        source_name="arXiv:2005.11401 (Lewis et al., 2020)",
        license_note="arXiv open-access preprint",
        demonstrates="Retrieval-Augmented Generation PDF search & citations",
    ),
    SampleSpec(
        rel_path="research-nlp/lora-low-rank-adaptation-2021.pdf",
        url="https://arxiv.org/pdf/2106.09685",
        source_name="arXiv:2106.09685 (Hu et al., 2021)",
        license_note="arXiv open-access preprint",
        demonstrates="LLM adaptation paper semantic search & Q&A",
    ),
    SampleSpec(
        rel_path="research-nlp/ml-confusion-matrix-slides.pptx",
        url="https://raw.githubusercontent.com/Ravikumar10593-hub/ppt_of_ml/main/Confusion_Matrix-7.pptx",
        source_name="Ravikumar10593-hub/ppt_of_ml",
        license_note="Public GitHub educational slides",
        demonstrates="PPTX slide-by-slide Quick Look & ML evaluation shelf",
    ),
    # -------------------------------------------------------------------------
    # 2. Receipts, Invoices & Financial Workbooks (Finance Shelf + OCR + Diff)
    # -------------------------------------------------------------------------
    SampleSpec(
        rel_path="finance/IMG_20251014_093122.jpg",
        url="https://upload.wikimedia.org/wikipedia/commons/thumb/d/df/Save_Mart_recipt_2010-10-23.jpg/960px-Save_Mart_recipt_2010-10-23.jpg",
        source_name="Wikimedia Commons (Save Mart Supermarket Receipt)",
        license_note="CC-BY-SA / Public Domain",
        demonstrates="RapidOCR grocery receipt extraction + Tidy Names (`IMG_...`)",
    ),
    SampleSpec(
        rel_path="finance/receipt-sample-grocery.jpg",
        url="https://raw.githubusercontent.com/bhimrazy/receipt-ocr/main/images/receipt.jpg",
        source_name="bhimrazy/receipt-ocr",
        license_note="MIT License",
        demonstrates="RapidOCR itemized receipt search & expense totals",
    ),
    SampleSpec(
        rel_path="finance/IMG_20251018_142005.jpeg",
        url="https://raw.githubusercontent.com/sovit-123/receipt_ocr/main/inference_data/image_2.jpeg",
        source_name="sovit-123/receipt_ocr (SROIE sample)",
        license_note="MIT License",
        demonstrates="Original receipt scan (paired with cropped copy for Lookalikes)",
    ),
    SampleSpec(
        rel_path="finance/IMG_20251018_142005_cropped.jpeg",
        url="https://raw.githubusercontent.com/sovit-123/receipt_ocr/main/inference_data/image_2_cropped.jpeg",
        source_name="sovit-123/receipt_ocr (SROIE cropped sample)",
        license_note="MIT License",
        demonstrates="Lookalikes image diff (cropped framing vs full scan)",
    ),
    SampleSpec(
        rel_path="finance/invoice-web-design.pdf",
        url="https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/invoice/invoice.pdf",
        source_name="CourtBouillon/weasyprint-samples",
        license_note="BSD-3-Clause / CC0 sample",
        demonstrates="PDF invoice indexing & financial Q&A",
    ),
    SampleSpec(
        rel_path="finance/invoice-template.html",
        url="https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/invoice/invoice.html",
        source_name="CourtBouillon/weasyprint-samples",
        license_note="BSD-3-Clause / CC0 sample",
        demonstrates="Sandboxed HTML invoice preview in Quick Look",
    ),
    SampleSpec(
        rel_path="finance/invoice-styles.css",
        url="https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/invoice/invoice.css",
        source_name="CourtBouillon/weasyprint-samples",
        license_note="BSD-3-Clause / CC0 sample",
        demonstrates="Invoice stylesheet paired with HTML invoice",
    ),
    SampleSpec(
        rel_path="finance/Financial Sample.xlsx",
        url="https://raw.githubusercontent.com/parulnith/Data-Visualisation-libraries/master/Data%20Visualisation%20with%20Power%20BI/Financial%20Sample.xlsx",
        source_name="Microsoft Power BI Financial Sample",
        license_note="Microsoft public sample workbook",
        demonstrates="Spreadsheet Quick Look (XLSX) & row filtering",
    ),
    SampleSpec(
        rel_path="finance/Budget Sample.xlsx",
        url="https://raw.githubusercontent.com/PacktPublishing/Power-BI-for-Finance/main/Budget%20Sample.xlsx",
        source_name="PacktPublishing/Power-BI-for-Finance",
        license_note="MIT License",
        demonstrates="Multi-sheet budget Excel workbook preview",
    ),
    # -------------------------------------------------------------------------
    # 3. Security Standards, Work Reports & Office Docs (DOCX, ODT, RTF, PDF)
    # -------------------------------------------------------------------------
    SampleSpec(
        rel_path="work-reports/nist-sp-800-63a-enrollment.pdf",
        url="https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63a.pdf",
        source_name="NIST SP 800-63A Enrollment & Identity Proofing",
        license_note="Public Domain (U.S. Federal Government)",
        demonstrates="Cohesive identity-security PDF Q&A",
    ),
    SampleSpec(
        rel_path="work-reports/nist-sp-800-63b-authentication.pdf",
        url="https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63b.pdf",
        source_name="NIST SP 800-63B Authentication & Lifecycle",
        license_note="Public Domain (U.S. Federal Government)",
        demonstrates="Standards/security PDF Q&A & citation extraction",
    ),
    SampleSpec(
        rel_path="work-reports/nist-sp-800-63c-federation.pdf",
        url="https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63c.pdf",
        source_name="NIST SP 800-63C Federation & Assertions",
        license_note="Public Domain (U.S. Federal Government)",
        demonstrates="Federation policy PDF clustering with 800-63A/B",
    ),
    SampleSpec(
        rel_path="work-reports/Board Report Sample.docx",
        url="https://raw.githubusercontent.com/PacktPublishing/Power-BI-for-Finance/main/Board%20Report%20Sample.docx",
        source_name="PacktPublishing/Power-BI-for-Finance",
        license_note="MIT License",
        demonstrates="DOCX extraction & Lookalikes baseline document",
    ),
    SampleSpec(
        rel_path="work-reports/Board Report Sample (copy).docx",
        url="https://raw.githubusercontent.com/PacktPublishing/Power-BI-for-Finance/main/Chapter%207%20Board%20Report%20Sample.docx",
        source_name="PacktPublishing/Power-BI-for-Finance",
        license_note="MIT License",
        demonstrates="Lookalikes unified text diff between revised DOCX versions",
    ),
    SampleSpec(
        rel_path="work-reports/annual-company-report.pdf",
        url="https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/report/report.pdf",
        source_name="CourtBouillon/weasyprint-samples",
        license_note="BSD-3-Clause / CC0 sample",
        demonstrates="Multi-page styled business report PDF",
    ),
    SampleSpec(
        rel_path="work-reports/business-letter-sample.pdf",
        url="https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/letter/letter.pdf",
        source_name="CourtBouillon/weasyprint-samples",
        license_note="BSD-3-Clause / CC0 sample",
        demonstrates="Business letter PDF extraction",
    ),
    SampleSpec(
        rel_path="work-reports/document_final_v2.odt",
        url="https://raw.githubusercontent.com/alexschiller/file-format-commons/master/files/ffc.odt",
        source_name="alexschiller/file-format-commons",
        license_note="CC0 / MIT",
        demonstrates="OpenDocument (.odt) parsing + Tidy Names draft detection",
    ),
    SampleSpec(
        rel_path="work-reports/meeting-notes-memo.rtf",
        url="https://raw.githubusercontent.com/alexschiller/file-format-commons/master/files/ffc.rtf",
        source_name="alexschiller/file-format-commons",
        license_note="CC0 / MIT",
        demonstrates="Rich Text Format (.rtf) text extraction",
    ),
    # -------------------------------------------------------------------------
    # 4. Photos & Diagrams (SigLIP / CLIP Visual Search & OCR)
    # -------------------------------------------------------------------------
    SampleSpec(
        rel_path="photos-and-diagrams/tabby-cat-portrait.jpg",
        url="https://upload.wikimedia.org/wikipedia/commons/thumb/1/15/Cat_August_2010-4.jpg/960px-Cat_August_2010-4.jpg",
        source_name="Wikimedia Commons (Cat August 2010-4)",
        license_note="CC-BY-SA",
        demonstrates="Zero-shot vision search ('cat', 'pet', 'animal portrait')",
    ),
    SampleSpec(
        rel_path="photos-and-diagrams/alpine-mountain-landscape.jpg",
        url="https://upload.wikimedia.org/wikipedia/commons/thumb/0/0d/Mountain_landscape.jpg/960px-Mountain_landscape.jpg",
        source_name="Wikimedia Commons (Mountain landscape)",
        license_note="CC-BY-SA / Public Domain",
        demonstrates="Vision search ('mountain', 'nature landscape')",
    ),
    SampleSpec(
        rel_path="photos-and-diagrams/espresso-coffee-cup.jpg",
        url="https://upload.wikimedia.org/wikipedia/commons/thumb/4/45/A_small_cup_of_coffee.JPG/960px-A_small_cup_of_coffee.JPG",
        source_name="Wikimedia Commons (Small cup of coffee)",
        license_note="CC-BY-SA",
        demonstrates="Vision search ('coffee cup', 'drink')",
    ),
    SampleSpec(
        rel_path="photos-and-diagrams/paris-eiffel-tower.jpg",
        url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/a8/Tour_Eiffel_Wikimedia_Commons.jpg/960px-Tour_Eiffel_Wikimedia_Commons.jpg",
        source_name="Wikimedia Commons (Tour Eiffel)",
        license_note="CC-BY-SA",
        demonstrates="Vision landmark search ('Eiffel tower', 'architecture')",
    ),
    SampleSpec(
        rel_path="photos-and-diagrams/golden-gate-bridge.jpg",
        url="https://upload.wikimedia.org/wikipedia/commons/thumb/c/c2/Golden_Gate_Bridge%2C_SF_%28cropped%29.jpg/960px-Golden_Gate_Bridge%2C_SF_%28cropped%29.jpg",
        source_name="Wikimedia Commons (Golden Gate Bridge)",
        license_note="CC-BY-SA",
        demonstrates="Vision cityscape search ('bridge', 'cityscape')",
    ),
    SampleSpec(
        rel_path="photos-and-diagrams/pizza-margherita.jpg",
        url="https://upload.wikimedia.org/wikipedia/commons/thumb/c/c8/Pizza_Margherita_stu_spivack.jpg/960px-Pizza_Margherita_stu_spivack.jpg",
        source_name="Wikimedia Commons (Pizza Margherita)",
        license_note="CC-BY-SA",
        demonstrates="Vision food search ('pizza', 'food')",
    ),
    SampleSpec(
        rel_path="photos-and-diagrams/Screenshot_20260115_transformer.png",
        url="https://upload.wikimedia.org/wikipedia/commons/thumb/3/34/Transformer%2C_full_architecture.png/960px-Transformer%2C_full_architecture.png",
        source_name="Wikimedia Commons (Transformer architecture)",
        license_note="CC-BY-SA",
        demonstrates="Diagram OCR + Vision + Tidy Names (`Screenshot_...` -> descriptive name)",
    ),
    # -------------------------------------------------------------------------
    # 5. Software & Vector Engine Code (Code Symbols & Markdown Quick Look)
    # -------------------------------------------------------------------------
    SampleSpec(
        rel_path="code-vector-search/sqlite-vec-readme.md",
        url="https://raw.githubusercontent.com/asg017/sqlite-vec/main/README.md",
        source_name="asg017/sqlite-vec",
        license_note="MIT / Apache-2.0",
        demonstrates="Rendered vs Raw Markdown Quick Look & vector search Q&A",
    ),
    SampleSpec(
        rel_path="code-vector-search/sqlite-vec-demo.py",
        url="https://raw.githubusercontent.com/asg017/sqlite-vec/main/examples/simple-python/demo.py",
        source_name="asg017/sqlite-vec",
        license_note="MIT / Apache-2.0",
        demonstrates="Python vector search code parsing & symbol extraction",
    ),
    SampleSpec(
        rel_path="code-vector-search/sqlite-vec-demo.c",
        url="https://raw.githubusercontent.com/asg017/sqlite-vec/main/examples/simple-c/demo.c",
        source_name="asg017/sqlite-vec",
        license_note="MIT / Apache-2.0",
        demonstrates="C SQLite vector extension code indexing",
    ),
    SampleSpec(
        rel_path="code-vector-search/hono-router.ts",
        url="https://raw.githubusercontent.com/honojs/hono/main/src/hono.ts",
        source_name="honojs/hono",
        license_note="MIT License",
        demonstrates="TypeScript code indexing & Quick Look",
    ),
    SampleSpec(
        rel_path="code-vector-search/untitled_script.py",
        url="https://raw.githubusercontent.com/py-pdf/pypdf/main/resources/afm_to_dataclass.py",
        source_name="py-pdf/pypdf",
        license_note="BSD-3-Clause",
        demonstrates="Tidy Names using extracted Python class/function symbols",
    ),
]


def download_file(url: str, dest: Path, timeout: int = 45, retries: int = 3) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "GoodShelvesSampleFetcher/1.1 (+https://github.com/paul-vaness/good-shelves)"
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                chunks: list[bytes] = []
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
            if len(data) == 0:
                raise ValueError(f"Downloaded 0 bytes from {url}")
            tmp_path.write_bytes(data)
            shutil.move(str(tmp_path), str(dest))
            return len(data)
        except (urllib.error.URLError, urllib.error.HTTPError, http.client.HTTPException, TimeoutError, OSError, ValueError) as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def write_manifest_doc(doc_path: Path, samples_dir: Path, results: list[tuple[SampleSpec, int, str]]) -> None:
    """Write sample attribution manifest outside `samples/` so it never pollutes search/clustering metrics."""
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(size for _, size, _ in results)
    lines = [
        "# Good Shelves — Curated Online Sample Seed Workspace",
        "",
        f"`samples/` contains **{len(results)} real-world, publicly available sample files** (~{total_bytes / (1024 * 1024):.1f} MB total) curated from open-access papers, public-domain datasets, and open-source sample repositories to exercise every capability of **Good Shelves**:",
        "",
        "- **Multimodal Shelves & 2D UMAP Map**: 5 cohesive thematic folders (`research-nlp/`, `finance/`, `work-reports/`, `photos-and-diagrams/`, `code-vector-search/`).",
        "- **Ask Your Shelves (Grounded Q&A)**: Real arXiv Transformer/RAG/BERT/LoRA papers, NIST SP 800-63A/B/C digital identity standards, invoices, and spreadsheets.",
        "- **RapidOCR & Receipt Extraction**: Real scanned receipts and Transformer architecture diagram PNG.",
        "- **Vision Search**: Public-domain/CC photos across distinct visual concepts (cat, mountain, coffee, Eiffel Tower, Golden Gate Bridge, pizza).",
        "- **Tidy Names (Smart Rename Queue)**: Realistic cryptic filenames (`IMG_20251014_093122.jpg`, `Screenshot_20260115_transformer.png`, `document_final_v2.odt`, `untitled_script.py`).",
        "- **Lookalikes (Near-Duplicate Resolver)**: Original vs. cropped receipt image and two versions of a Word board report.",
        "- **Multi-Format Quick Look**: Covers `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.odt`, `.rtf`, `.jpg`, `.jpeg`, `.png`, `.md`, `.py`, `.ts`, `.c`, `.html`, and `.css`.",
        "",
        "## Clean & Re-fetch",
        "",
        "```bash",
        "python3 scripts/fetch_samples.py --clean",
        "```",
        "",
        "## Index & Benchmark",
        "",
        "```bash",
        ".venv/bin/python python-engine/benchmark.py --folder samples",
        "```",
        "",
        "## File Manifest & Attribution",
        "",
        "| File | Size | Source | License | Demonstrates |",
        "|---|---:|---|---|---|",
    ]
    for spec, size, _sha in results:
        size_kb = max(1, round(size / 1024))
        lines.append(
            f"| `{spec.rel_path}` | {size_kb} KB | [{spec.source_name}]({spec.url}) | {spec.license_note} | {spec.demonstrates} |"
        )
    lines.append("")
    doc_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean and fetch curated online sample seed files into samples/")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "samples",
        help="Destination directory (default: <repo>/samples)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download files even if they already exist locally",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete all existing files in destination before fetching fresh samples",
    )
    args = parser.parse_args()

    samples_dir: Path = args.dest.resolve()
    if args.clean and samples_dir.exists():
        shutil.rmtree(samples_dir)
    samples_dir.mkdir(parents=True, exist_ok=True)

    results: list[tuple[SampleSpec, int, str]] = []
    failures: list[tuple[SampleSpec, str]] = []

    for idx, spec in enumerate(SAMPLES, start=1):
        target = samples_dir / spec.rel_path
        if target.exists() and target.stat().st_size > 0 and not args.force and not args.clean:
            data = target.read_bytes()
            sha = hashlib.sha256(data).hexdigest()[:12]
            results.append((spec, len(data), sha))
            print(f"[{idx:02d}/{len(SAMPLES)}] CACHED  {spec.rel_path} ({len(data) // 1024} KB)")
            continue

        try:
            size = download_file(spec.url, target)
            sha = hashlib.sha256(target.read_bytes()).hexdigest()[:12]
            results.append((spec, size, sha))
            print(f"[{idx:02d}/{len(SAMPLES)}] FETCHED {spec.rel_path} ({max(1, size // 1024)} KB)")
        except (urllib.error.URLError, urllib.error.HTTPError, http.client.HTTPException, TimeoutError, ValueError, OSError) as exc:
            failures.append((spec, str(exc)))
            print(f"[{idx:02d}/{len(SAMPLES)}] FAILED  {spec.rel_path}: {exc}", file=sys.stderr)

    doc_path = Path(__file__).resolve().parent.parent / "docs" / "SAMPLES.md"
    write_manifest_doc(doc_path, samples_dir, results)
    total_mb = sum(s for _, s, _ in results) / (1024 * 1024)
    print(f"\nDone: {len(results)}/{len(SAMPLES)} files ready in {samples_dir} ({total_mb:.2f} MB).")
    if failures:
        print(f"Failed ({len(failures)}):", file=sys.stderr)
        for spec, err in failures:
            print(f"  - {spec.rel_path}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
