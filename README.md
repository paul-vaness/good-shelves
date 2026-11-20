# good shelves — Local Multimodal Semantic File Manager (Windows & Linux)

**good shelves** is a 100% offline **Multimodal Embedding-Based Semantic Search and Organization System for Personal File Management** built with **Tauri v2 (Rust)**, a **Python ONNX / SQLite-vec Multimodal Sidecar**, and **React + TypeScript + Tailwind CSS**.

> **Download Installers (Windows `.exe` / `.msi` & Linux `.AppImage` / `.deb`)**: [Latest GitHub Release (v1.0.3)](https://github.com/paul-vaness/good-shelves/releases/latest) — see [Section 2](#2-download--installation-windows--linux) below.

---

## 0. Signature AI File Manager Features

- **100% Offline Multimodal Indexing**: Text embeddings (`bge-small-en-v1.5` INT8 ONNX), multimodal vision embeddings (`siglip-base-224` / `clip-vit-b32` ONNX + visual tagging), and built-in **RapidOCR** (`rapidocr-onnxruntime`) for screenshots, receipts, and IDs.
- **Dynamic Shelf Discovery & Custom Smart Shelves**: UMAP (`512-d` → `5-d`/`2-d`) + HDBSCAN unsupervised clustering with lasso selection on the interactive 2D Cluster Map, shelf renaming, and user-defined Custom Smart Shelves.
- **Ask Your Shelves (Grounded Offline Q&A)**: Extractive local Q&A across indexed PDFs, notes, spreadsheets, and OCR text with clickable file citations and optional local **Ollama** integration.
- **Rich Multi-Format Previews & Quick Look (`Space`, `←`/`→` stepping)**:
  - **Images**: Zoom (`100%–300%`), pan, and toggle **OCR Text Overlay** (with copyable extracted receipt fields: merchant, total, date, reference).
  - **PDF**: Page-by-page rendered preview and extracted text toggle.
  - **Spreadsheets (`.xlsx`, `.xls`, `.csv`)**: Multi-sheet tab switching, live row filtering, and CSV export.
  - **Presentations (`.pptx`), Markdown (`.md`), Code/HTML/JSON, Archives (`.zip`, `.tar`, `.gz`)**: Slide stepper, rendered Markdown vs. source toggle, syntax-highlighted/truncated full excerpts, HTML sandboxed preview, and searchable archive entry listings.
- **Power Search Operators & Find Similar**: Boolean/field operators (`type:pdf`, `shelf:...`, `tag:...`, `after:YYYY-MM-DD`, `before:YYYY-MM-DD`, `has:ocr`, `-exclude`) plus right-click **Find similar files** via `512-d` cosine similarity.
- **Classic Folders Browser**: Breadcrumb path navigation, list/grid views, sorting, copy/cut/paste, folder creation/renaming/deletion, and right-click OS context actions.
- **Batch Rename Template Studio & Duplicate Resolver**: Live pattern previews (`{date}_{shelf}_{merchant}_{seq}`), visual/text diffing for lookalikes, and a **Trash & Archive** recovery center supporting both reversible `GoodShelves_Trash` (`Ctrl+Z` undo) and native OS Recycle Bin/Trash.
- **Storage & Workspace Insights + Battery Saver**: File-type breakdowns, reclaimable duplicate megabytes, stale Downloads highlights, and automatic indexing pause on battery power when enabled.

---

## 1. Repository Architecture

```text
good-shelves/
├── .github/workflows/build.yml # CI matrix building Linux (.AppImage, .deb) & Windows (.msi, .exe) installers
├── scripts/
│   ├── setup_models.py         # Offline ONNX Vision/Text + OCR model provisioner & verifier
│   └── build_sidecar.py        # PyInstaller standalone sidecar builder for Linux & Windows
├── src-tauri/                  # Rust native core & OS integration (Tauri v2)
│   ├── src/
│   │   ├── main.rs             # Native desktop entry point
│   │   ├── lib.rs              # Tauri v2 command handlers, OS dialogs, dynamic sidecar URL bridge
│   │   ├── watcher.rs          # Real-time recursive notify watcher (Windows & Linux)
│   │   ├── fs_ops.rs           # Atomic rename, undo log, safe folder tidy, archive, open/reveal
│   │   ├── sidecar.rs          # Manages Python ML worker lifecycle, dynamic port & bundled models
│   │   └── db.rs               # Fast SQLite metadata queries & Windows Known Folders support
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   └── tauri.release.conf.json # Bundles standalone PyInstaller sidecar & offline ONNX models
├── python-engine/              # Offline Multimodal AI, OCR, Clustering & Auto-Rename Engine
│   ├── main.py                 # Local FastAPI + SSE IPC server bound to 127.0.0.1
│   ├── parsers.py              # PDF, DOCX, PPTX slides, multi-sheet XLSX, ODT, RTF, ZIP/TAR, RapidOCR
│   ├── embedder.py             # Quantized ONNX vision + text encoder (512-d L2 normalized)
│   ├── vision_models.py        # Selectable offline vision model catalog & downloader
│   ├── devices.py              # Cross-platform USB, SD card, and phone mount detector
│   ├── vector_store.py         # Embedded SQLite + sqlite-vec, Q&A citations, custom shelves, trash & insights
│   ├── clustering.py           # UMAP + HDBSCAN shelf discovery + Silhouette score
│   ├── deduplicator.py         # Pairwise similarity & SHA-256 duplicate checks + visual/text diffs
│   ├── renamer.py              # Deterministic semantic renamer + batch template formatter
│   ├── benchmark.py            # Retrieval (P@5, Recall@10, MRR), Silhouette & latency benchmark
│   └── tests/test_engine.py    # Comprehensive backend pytest suite
└── src/                        # React + TypeScript + Tailwind CSS frontend
    ├── components/
    │   ├── LeftRail.tsx        # Workspace navigation, custom shelves & dynamic HDBSCAN shelf jumpers
    │   ├── TopMemoryBar.tsx    # Real-time debounced semantic query bar + quick actions
    │   ├── ShelvesView.tsx     # Shelf List / Grid / interactive zoomable & lasso 2D UMAP Map
    │   ├── FoldersView.tsx     # Classic breadcrumb filesystem browser with cut/copy/paste
    │   ├── FindView.tsx        # Semantic search + operator chips + Ask Your Shelves grounded Q&A
    │   ├── TidyNamesView.tsx   # Smart Rename queue + Batch Rename Template Studio + SQLite Undo history
    │   ├── LookalikesView.tsx  # Near-duplicate resolver with visual & unified diff previews
    │   ├── TrashArchiveView.tsx# Recovery bin for reversible trashed files & archived duplicates
    │   ├── ActivityView.tsx    # Live ingestion pipeline monitor + Storage & Workspace Insights
    │   ├── InspectorSheet.tsx  # Docked inspector + Quick Look modal (PDF/Image+OCR/Sheets/Slides/MD/ZIP/Code)
    │   ├── FileActionDialogs.tsx # Rename, Move/Copy, New Folder, Delete, and Shelf dialogs
    │   ├── FileContextMenu.tsx # Right-click menu (Quick Look, Find Similar, Open/Reveal, Copy/Cut, Pin, Delete)
    │   ├── CommandPalette.tsx  # Keyboard-first quick jump & action launcher
    │   ├── OnboardingModal.tsx # First-run profile & watched folder setup wizard
    │   ├── SmartCleanModal.tsx # Guided workspace cleanup & duplicate/trash triage
    │   ├── ErrorBoundary.tsx   # Resilient React error recovery boundary
    │   └── SettingsDrawer.tsx  # Watched folders, OCR/battery/trash-mode toggles, Ollama & model settings
    └── lib/
        ├── tauri-api.ts        # Strongly-typed Tauri command & dynamic sidecar API wrappers
        └── platform.ts         # Cross-platform shortcut formatting & OS path helpers
```

---

## 2. Download & Installation (Windows & Linux)

Pre-built standalone desktop installers (with the Python ML sidecar & offline ONNX models included — no Python or Node installation required) are published on **[GitHub Releases (Latest)](https://github.com/paul-vaness/good-shelves/releases/latest)**:

- **Windows (`x86_64`, Windows 10 / 11)**:
  - **`good.shelves_1.0.3_x64-setup.exe`** (NSIS installer — recommended for quick install)
  - **`good.shelves_1.0.3_x64_en-US.msi`** (Windows MSI installer)
- **Linux (`x86_64`)**:
  - **`good.shelves_1.0.3_amd64.AppImage`** — Universal portable executable for Ubuntu, Arch/Omarchy, Fedora, openSUSE, Debian (`chmod +x good.shelves_*.AppImage && ./good.shelves_*.AppImage`)
  - **`good.shelves_1.0.3_amd64.deb`** — Debian / Ubuntu / Pop!_OS (`sudo dpkg -i good.shelves_1.0.3_amd64.deb && sudo apt-get install -f`)

---

## 3. Building From Source (Windows & Linux)

### A. Provision Offline ONNX Models & Compile Standalone Sidecar
```bash
# Linux
.venv/bin/python scripts/setup_models.py --download
.venv/bin/python scripts/build_sidecar.py

# Windows (PowerShell)
.\.venv\Scripts\python.exe scripts\setup_models.py --download
.\.venv\Scripts\python.exe scripts\build_sidecar.py
```

### B. Run Automated Test Suites & Research Benchmark
```bash
# Backend Python tests
.venv/bin/python -m pytest python-engine/tests/ -v

# Rust lint & unit tests
(cd src-tauri && cargo clippy --all-targets -- -D warnings && cargo test)

# Frontend typecheck & production build
pnpm lint && pnpm build

# Research benchmark (P@5, Recall@10, MRR, Silhouette, Latency)
.venv/bin/python python-engine/benchmark.py
```

### C. First Launch & Watched Folders
On startup the sidecar indexes supported files in your watched folders (`~/Documents`, `~/Downloads`, `~/Desktop` on Linux; your Windows Known Folders on Windows 10/11). Add or pause folders anytime in **Onboarding** or **Settings → Watched Folders**. No sample or mock files are created anywhere.

To run the retrieval and clustering benchmark against a specific local folder:
```bash
.venv/bin/python python-engine/benchmark.py --folder ~/Documents
```

### D. Development & Packaged Release Desktop Builds
```bash
# Run Tauri desktop app in development mode
pnpm tauri dev

# Build self-contained release installers (.AppImage/.deb on Linux, .msi/.exe on Windows)
pnpm tauri build --config src-tauri/tauri.release.conf.json --bundles deb,appimage
```

---

## 4. Research & Development Team
- **Paul Vaness B. Ramos** — System Developer
- **Ishee Margareth B. Esteleydes** — Project Manager
- **Ayessa S. Digno** — Documentation & Quality Assurance
- **Calaca City Global College** · College of Business Administration and Computer Studies · Computer Science Department · **Adviser: Prof. Clark Adrian Wick (PROL301)**
