# Good Shelves — Curated Online Sample Seed Workspace

`samples/` contains **35 real-world, publicly available sample files** (~13.2 MB total) curated from open-access papers, public-domain datasets, and open-source sample repositories to exercise every capability of **Good Shelves**:

- **Multimodal Shelves & 2D UMAP Map**: 5 cohesive thematic folders (`research-nlp/`, `finance/`, `work-reports/`, `photos-and-diagrams/`, `code-vector-search/`).
- **Ask Your Shelves (Grounded Q&A)**: Real arXiv Transformer/RAG/BERT/LoRA papers, NIST SP 800-63A/B/C digital identity standards, invoices, and spreadsheets.
- **RapidOCR & Receipt Extraction**: Real scanned receipts and Transformer architecture diagram PNG.
- **Vision Search**: Public-domain/CC photos across distinct visual concepts (cat, mountain, coffee, Eiffel Tower, Golden Gate Bridge, pizza).
- **Tidy Names (Smart Rename Queue)**: Realistic cryptic filenames (`IMG_20251014_093122.jpg`, `Screenshot_20260115_transformer.png`, `document_final_v2.odt`, `untitled_script.py`).
- **Lookalikes (Near-Duplicate Resolver)**: Original vs. cropped receipt image and two versions of a Word board report.
- **Multi-Format Quick Look**: Covers `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.odt`, `.rtf`, `.jpg`, `.jpeg`, `.png`, `.md`, `.py`, `.ts`, `.c`, `.html`, and `.css`.

## Clean & Re-fetch

```bash
python3 scripts/fetch_samples.py --clean
```

## Index & Benchmark

```bash
.venv/bin/python python-engine/benchmark.py --folder samples
```

## File Manifest & Attribution

| File | Size | Source | License | Demonstrates |
|---|---:|---|---|---|
| `research-nlp/attention-is-all-you-need-2017.pdf` | 2163 KB | [arXiv:1706.03762 (Vaswani et al., 2017)](https://arxiv.org/pdf/1706.03762) | arXiv open-access preprint | Transformer architecture paper clustering & PDF Q&A |
| `research-nlp/bert-pretraining-transformers-2018.pdf` | 757 KB | [arXiv:1810.04805 (Devlin et al., 2018)](https://arxiv.org/pdf/1810.04805) | arXiv open-access preprint | Deep contextual language representation PDF retrieval |
| `research-nlp/rag-knowledge-intensive-nlp-2020.pdf` | 865 KB | [arXiv:2005.11401 (Lewis et al., 2020)](https://arxiv.org/pdf/2005.11401) | arXiv open-access preprint | Retrieval-Augmented Generation PDF search & citations |
| `research-nlp/lora-low-rank-adaptation-2021.pdf` | 1572 KB | [arXiv:2106.09685 (Hu et al., 2021)](https://arxiv.org/pdf/2106.09685) | arXiv open-access preprint | LLM adaptation paper semantic search & Q&A |
| `research-nlp/ml-confusion-matrix-slides.pptx` | 553 KB | [Ravikumar10593-hub/ppt_of_ml](https://raw.githubusercontent.com/Ravikumar10593-hub/ppt_of_ml/main/Confusion_Matrix-7.pptx) | Public GitHub educational slides | PPTX slide-by-slide Quick Look & ML evaluation shelf |
| `finance/IMG_20251014_093122.jpg` | 158 KB | [Wikimedia Commons (Save Mart Supermarket Receipt)](https://upload.wikimedia.org/wikipedia/commons/thumb/d/df/Save_Mart_recipt_2010-10-23.jpg/960px-Save_Mart_recipt_2010-10-23.jpg) | CC-BY-SA / Public Domain | RapidOCR grocery receipt extraction + Tidy Names (`IMG_...`) |
| `finance/receipt-sample-grocery.jpg` | 164 KB | [bhimrazy/receipt-ocr](https://raw.githubusercontent.com/bhimrazy/receipt-ocr/main/images/receipt.jpg) | MIT License | RapidOCR itemized receipt search & expense totals |
| `finance/IMG_20251018_142005.jpeg` | 482 KB | [sovit-123/receipt_ocr (SROIE sample)](https://raw.githubusercontent.com/sovit-123/receipt_ocr/main/inference_data/image_2.jpeg) | MIT License | Original receipt scan (paired with cropped copy for Lookalikes) |
| `finance/IMG_20251018_142005_cropped.jpeg` | 490 KB | [sovit-123/receipt_ocr (SROIE cropped sample)](https://raw.githubusercontent.com/sovit-123/receipt_ocr/main/inference_data/image_2_cropped.jpeg) | MIT License | Lookalikes image diff (cropped framing vs full scan) |
| `finance/invoice-web-design.pdf` | 40 KB | [CourtBouillon/weasyprint-samples](https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/invoice/invoice.pdf) | BSD-3-Clause / CC0 sample | PDF invoice indexing & financial Q&A |
| `finance/invoice-template.html` | 2 KB | [CourtBouillon/weasyprint-samples](https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/invoice/invoice.html) | BSD-3-Clause / CC0 sample | Sandboxed HTML invoice preview in Quick Look |
| `finance/invoice-styles.css` | 2 KB | [CourtBouillon/weasyprint-samples](https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/invoice/invoice.css) | BSD-3-Clause / CC0 sample | Invoice stylesheet paired with HTML invoice |
| `finance/Financial Sample.xlsx` | 81 KB | [Microsoft Power BI Financial Sample](https://raw.githubusercontent.com/parulnith/Data-Visualisation-libraries/master/Data%20Visualisation%20with%20Power%20BI/Financial%20Sample.xlsx) | Microsoft public sample workbook | Spreadsheet Quick Look (XLSX) & row filtering |
| `finance/Budget Sample.xlsx` | 32 KB | [PacktPublishing/Power-BI-for-Finance](https://raw.githubusercontent.com/PacktPublishing/Power-BI-for-Finance/main/Budget%20Sample.xlsx) | MIT License | Multi-sheet budget Excel workbook preview |
| `work-reports/nist-sp-800-63a-enrollment.pdf` | 951 KB | [NIST SP 800-63A Enrollment & Identity Proofing](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63a.pdf) | Public Domain (U.S. Federal Government) | Cohesive identity-security PDF Q&A |
| `work-reports/nist-sp-800-63b-authentication.pdf` | 1446 KB | [NIST SP 800-63B Authentication & Lifecycle](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63b.pdf) | Public Domain (U.S. Federal Government) | Standards/security PDF Q&A & citation extraction |
| `work-reports/nist-sp-800-63c-federation.pdf` | 1258 KB | [NIST SP 800-63C Federation & Assertions](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63c.pdf) | Public Domain (U.S. Federal Government) | Federation policy PDF clustering with 800-63A/B |
| `work-reports/Board Report Sample.docx` | 76 KB | [PacktPublishing/Power-BI-for-Finance](https://raw.githubusercontent.com/PacktPublishing/Power-BI-for-Finance/main/Board%20Report%20Sample.docx) | MIT License | DOCX extraction & Lookalikes baseline document |
| `work-reports/Board Report Sample (copy).docx` | 73 KB | [PacktPublishing/Power-BI-for-Finance](https://raw.githubusercontent.com/PacktPublishing/Power-BI-for-Finance/main/Chapter%207%20Board%20Report%20Sample.docx) | MIT License | Lookalikes unified text diff between revised DOCX versions |
| `work-reports/annual-company-report.pdf` | 582 KB | [CourtBouillon/weasyprint-samples](https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/report/report.pdf) | BSD-3-Clause / CC0 sample | Multi-page styled business report PDF |
| `work-reports/business-letter-sample.pdf` | 609 KB | [CourtBouillon/weasyprint-samples](https://raw.githubusercontent.com/CourtBouillon/weasyprint-samples/main/letter/letter.pdf) | BSD-3-Clause / CC0 sample | Business letter PDF extraction |
| `work-reports/document_final_v2.odt` | 8 KB | [alexschiller/file-format-commons](https://raw.githubusercontent.com/alexschiller/file-format-commons/master/files/ffc.odt) | CC0 / MIT | OpenDocument (.odt) parsing + Tidy Names draft detection |
| `work-reports/meeting-notes-memo.rtf` | 29 KB | [alexschiller/file-format-commons](https://raw.githubusercontent.com/alexschiller/file-format-commons/master/files/ffc.rtf) | CC0 / MIT | Rich Text Format (.rtf) text extraction |
| `photos-and-diagrams/tabby-cat-portrait.jpg` | 125 KB | [Wikimedia Commons (Cat August 2010-4)](https://upload.wikimedia.org/wikipedia/commons/thumb/1/15/Cat_August_2010-4.jpg/960px-Cat_August_2010-4.jpg) | CC-BY-SA | Zero-shot vision search ('cat', 'pet', 'animal portrait') |
| `photos-and-diagrams/alpine-mountain-landscape.jpg` | 127 KB | [Wikimedia Commons (Mountain landscape)](https://upload.wikimedia.org/wikipedia/commons/thumb/0/0d/Mountain_landscape.jpg/960px-Mountain_landscape.jpg) | CC-BY-SA / Public Domain | Vision search ('mountain', 'nature landscape') |
| `photos-and-diagrams/espresso-coffee-cup.jpg` | 107 KB | [Wikimedia Commons (Small cup of coffee)](https://upload.wikimedia.org/wikipedia/commons/thumb/4/45/A_small_cup_of_coffee.JPG/960px-A_small_cup_of_coffee.JPG) | CC-BY-SA | Vision search ('coffee cup', 'drink') |
| `photos-and-diagrams/paris-eiffel-tower.jpg` | 331 KB | [Wikimedia Commons (Tour Eiffel)](https://upload.wikimedia.org/wikipedia/commons/thumb/a/a8/Tour_Eiffel_Wikimedia_Commons.jpg/960px-Tour_Eiffel_Wikimedia_Commons.jpg) | CC-BY-SA | Vision landmark search ('Eiffel tower', 'architecture') |
| `photos-and-diagrams/golden-gate-bridge.jpg` | 195 KB | [Wikimedia Commons (Golden Gate Bridge)](https://upload.wikimedia.org/wikipedia/commons/thumb/c/c2/Golden_Gate_Bridge%2C_SF_%28cropped%29.jpg/960px-Golden_Gate_Bridge%2C_SF_%28cropped%29.jpg) | CC-BY-SA | Vision cityscape search ('bridge', 'cityscape') |
| `photos-and-diagrams/pizza-margherita.jpg` | 121 KB | [Wikimedia Commons (Pizza Margherita)](https://upload.wikimedia.org/wikipedia/commons/thumb/c/c8/Pizza_Margherita_stu_spivack.jpg/960px-Pizza_Margherita_stu_spivack.jpg) | CC-BY-SA | Vision food search ('pizza', 'food') |
| `photos-and-diagrams/Screenshot_20260115_transformer.png` | 141 KB | [Wikimedia Commons (Transformer architecture)](https://upload.wikimedia.org/wikipedia/commons/thumb/3/34/Transformer%2C_full_architecture.png/960px-Transformer%2C_full_architecture.png) | CC-BY-SA | Diagram OCR + Vision + Tidy Names (`Screenshot_...` -> descriptive name) |
| `code-vector-search/sqlite-vec-readme.md` | 9 KB | [asg017/sqlite-vec](https://raw.githubusercontent.com/asg017/sqlite-vec/main/README.md) | MIT / Apache-2.0 | Rendered vs Raw Markdown Quick Look & vector search Q&A |
| `code-vector-search/sqlite-vec-demo.py` | 1 KB | [asg017/sqlite-vec](https://raw.githubusercontent.com/asg017/sqlite-vec/main/examples/simple-python/demo.py) | MIT / Apache-2.0 | Python vector search code parsing & symbol extraction |
| `code-vector-search/sqlite-vec-demo.c` | 2 KB | [asg017/sqlite-vec](https://raw.githubusercontent.com/asg017/sqlite-vec/main/examples/simple-c/demo.c) | MIT / Apache-2.0 | C SQLite vector extension code indexing |
| `code-vector-search/hono-router.ts` | 1 KB | [honojs/hono](https://raw.githubusercontent.com/honojs/hono/main/src/hono.ts) | MIT License | TypeScript code indexing & Quick Look |
| `code-vector-search/untitled_script.py` | 13 KB | [py-pdf/pypdf](https://raw.githubusercontent.com/py-pdf/pypdf/main/resources/afm_to_dataclass.py) | BSD-3-Clause | Tidy Names using extracted Python class/function symbols |
