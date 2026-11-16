# good shelves — Local Multimodal Semantic File Manager (Windows & Linux)

**good shelves** is a 100% offline **Multimodal Embedding-Based Semantic Search and Organization System for Personal File Management** built with **Tauri v2 (Rust)**, a **Python ONNX / SQLite-vec Multimodal Sidecar**, and **React + TypeScript + Tailwind CSS**.

---

## 1. Repository Architecture

```text
good-shelves/
├── scripts/
│   └── setup_models.py         # Offline ONNX Vision/Text + OCR model provisioner & verifier
├── src-tauri/                  # Rust native core & OS integration (Tauri v2)
│   ├── src/
│   │   ├── main.rs             # Native desktop entry point
│   │   ├── lib.rs              # Tauri v2 command handlers & plugin initialization
│   │   ├── watcher.rs          # Real-time recursive notify watcher (Windows & Linux)
│   │   ├── fs_ops.rs           # Atomic rename, undo log, safe folder tidy, archive, open/reveal
│   │   ├── sidecar.rs          # Manages Python ML worker lifecycle & HTTP/SSE bridge (127.0.0.1:18420)
│   │   └── db.rs               # Fast SQLite metadata queries & state sync
│   ├── Cargo.toml
│   └── tauri.conf.json
├── python-engine/              # Offline Multimodal AI, OCR, Clustering & Auto-Rename Engine
│   ├── main.py                 # Local FastAPI + SSE IPC server bound to 127.0.0.1:18420
│   ├── parsers.py              # PyMuPDF (PDF), python-docx (DOCX), text/code chunker, RapidOCR/Tesseract
│   ├── embedder.py             # Quantized ONNX vision + text encoder (512-d L2 normalized)
│   ├── vector_store.py         # Embedded SQLite + sqlite-vec (vec0 cosine index)
│   ├── clustering.py           # UMAP (512-d -> 5-d & 2-d) + HDBSCAN shelf discovery + Silhouette score
│   ├── deduplicator.py         # Pairwise cosine similarity (>= 0.92) + real unified text/image diffing
│   ├── renamer.py              # Deterministic semantic filename generator (YYYY-MM-DD_topic-slug.ext)
│   └── benchmark.py            # Automated evaluation script (P@5, Recall@10, MRR, Latency, Silhouette)
└── src/                        # React + TypeScript + Tailwind CSS frontend
    ├── components/
    │   ├── LeftRail.tsx        # Workspace navigation & dynamic HDBSCAN shelf jumpers (01..0N)
    │   ├── TopMemoryBar.tsx    # Real-time debounced semantic query bar + quick actions
    │   ├── ShelvesView.tsx     # Shelf Cards + interactive zoomable 2D UMAP Meaning Map
    │   ├── FindView.tsx        # Ranked semantic search results with live match breakdown
    │   ├── TidyNamesView.tsx   # Batch & individual smart rename review with persistent SQLite Undo history
    │   ├── LookalikesView.tsx  # Near-duplicate resolver with real difflib & image resolution/crop diffs
    │   ├── ActivityView.tsx    # Live ingestion pipeline monitor ("how it learns") + real file drop zone
    │   ├── InspectorSheet.tsx  # Docked right file preview (PDF/Image/Code/Text) + Quick Look & Tidy modals
    │   └── SettingsDrawer.tsx  # Watched folders, OCR/indexing toggles, sensitivity sliders, credits
    └── lib/
        └── tauri-api.ts        # Strongly-typed Tauri command & SSE event wrappers
```

---

## 2. Cross-Platform Setup & Build Instructions

### A. Provision Offline ONNX Models
```bash
# Linux / macOS
.venv/bin/python scripts/setup_models.py

# Windows (PowerShell)
.\.venv\Scripts\python.exe scripts\setup_models.py
```

### B. Run Automated Research Benchmark (`P@5`, `Recall@10`, `MRR`, `Silhouette`, `Latency`)
```bash
.venv/bin/python python-engine/benchmark.py
```

### C. First Launch
On startup the sidecar indexes the supported files already present in your watched folders (`~/Documents`, `~/Downloads`, `~/Desktop` by default). Add or pause folders in **Settings**. No sample or mock files are created.

### D. Development & Production Desktop Builds (Windows 10/11 & Linux)
```bash
# Build frontend production bundle
pnpm build

# Run Tauri desktop app in development mode
cargo tauri dev

# Build native installer / binary bundle (.msi / .exe on Windows, .deb / .AppImage on Linux)
cargo tauri build
```

---

## 3. Research & Development Team
- **Paul Vaness B. Ramos** — System Developer
- **Ishee Margareth B. Esteleydes** — Project Manager
- **Ayessa S. Digno** — Documentation & Quality Assurance
- **Calaca City Global College** · College of Business Administration and Computer Studies · Computer Science Department · **Adviser: Prof. Clark Adrian Wick (PROL301)**
