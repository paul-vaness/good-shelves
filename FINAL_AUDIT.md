# Good Shelves — UI/UX & Reliability Audit Log

## Baseline Checks (Pre-Fix)
- `pnpm lint` (`tsc --noEmit`): PASS
- `pnpm build` (`tsc && vite build`): PASS
- `cargo test` (7 Rust tests): PASS
- `cargo clippy --all-targets`: 1 warning (`SidecarManager::new` without `Default` in `src-tauri/src/sidecar.rs`)
- `.venv/bin/python -m pytest python-engine/tests/test_engine.py -v`: 5 passed

## Issues Found & Fix Status

| ID | Priority | Area | Issue | Status |
|---|---|---|---|---|
| A1 | P0 | Backend Startup (`python-engine/main.py`) | `get_store()` unlocked; `bootstrap_initial_index` and `_device_hotplug_monitor` raced on fresh DB `_init_db()` causing `sqlite3.OperationalError: duplicate column name: metadata_json`. | **Fixed & Verified** (`_store_lock` double-checked locking + `test_concurrent_get_store_and_edge_case_resilience` + fresh-DB live launch) |
| A2 | P0 | Layout / Shell (`src/index.css`, `src/App.tsx`, `InspectorSheet.tsx`) | Fixed `.right-inspector` (`400px`) overlapped `.center-stage` at 1920×1080, 1440×900, and 1024×768, intercepting clicks on `cluster map`, `+ save search`, `apply all suggestions`, row quick-actions, and at `<=1080px` covered `TopMemoryBar`. | **Fixed & Verified** (docked flex column on `>1180px`, `top: 60px` overlay on `<=1180px`; Playwright verified 0 pointer interception & 0 layout issues across 1920×1080, 1024×768, 760×700) |
| A3 | P0 | Inspector (`InspectorSheet.tsx`) | `.inspector-reveal-fab` never rendered when collapsed; `QuickActions` JSX duplicated 180 lines across panel & Quick Look modal; `TagsEditor` retained `tagInput` across file changes; broken `<img>` lacked error fallback. | **Fixed & Verified** (rendered bottom-right `.inspector-reveal-fab`, extracted `FileQuickActions`, reset `tagInput` on `file.id`, added `imgFailed` fallback verified on corrupt PNG) |
| A4 | P0 | Keyboard & Modals (`App.tsx`, `FileActionDialogs.tsx`, `ShelvesView.tsx`, `TopMemoryBar.tsx`, `CommandPalette.tsx`) | `Space` opened Quick Look on focused `<button>`/`[role="switch"]`/`[role="tab"]`; `Backspace` triggered delete dialog; `ShelvesView` `ArrowDown`/`ArrowUp` bubbled to `App.tsx` moving 2 rows; `FileActionDialogs` delete `Enter` intercepted `Enter` on `Close`; capture-phase Escape missing on popover/dialog causing Inspector to collapse; stale `handleUndoAction` closure. | **Fixed & Verified** (interactive target guard on `Space`/shortcuts, `Delete` only, `stopPropagation` on list arrows, capture-phase `Escape` on popover & dialogs, `Enter` button guard on delete dialog; Playwright verified all) |
| A5 | P1 | Error Handling & Edge Cases (`vector_store.py`, `parsers.py`, `main.py`, `tauri-api.ts`) | `parse_file` `stat()`/`compute_sha256()` outside `try/except`; `scan_folder` didn't isolate per-file exceptions or guarantee progress completion; `add_watched_folder` silently created typo paths & returned 500 on errors; `sidecarGet`/`sidecarPost` discarded JSON `detail` messages; no shutdown event for mid-index exit. | **Fixed & Verified** (added `OSError`/`PermissionError` guards, per-file isolation & `finally` progress completion, folder validation + HTTP 400 `detail` parsing, `_shutdown_event`; verified via pytest & live invalid-folder toast) |
| A6 | P1 | State & Repeated Clicks (`ShelvesView.tsx`, `TidyNamesView.tsx`, `LookalikesView.tsx`, `SettingsDrawer.tsx`, `App.tsx`) | `ShelvesView` cleared `checkedIds` before async bulk action/dialog finished; `TidyNamesView` & `LookalikesView` lacked busy/disabled guards on repeated clicks; `SettingsDrawer` lacked busy states on rescan/verify/folder toggle and mislabeled bundled active `clip-vit-b32`; Quick Look stayed open on deleted file. | **Fixed & Verified** (conditional selection clear + `clearSelectionSignal`, per-item & batch busy guards, `vision bundled` / `active · add text pair` labels, Quick Look auto-close on delete) |
| A7 | P1 | Accessibility & Contrast (`src/index.css`, `FindView.tsx`, `TopMemoryBar.tsx`, `SmartCleanModal.tsx`, `CommandPalette.tsx`) | `--ink-3` contrast was ~3.2–3.5:1; missing `aria-label` on `FindView` row/saved-search buttons & `ShelvesView` checkboxes; missing `role="dialog" aria-modal="true"` on QuickLook/Organize/SmartClean/CommandPalette. | **Fixed & Verified** (`--ink-3` updated to `#6E6A63` light / `#969289` dark, added ARIA labels/roles and `aria-expanded` on `?` button) |
| A8 | P1 | Responsiveness & Motion (`src/index.css`, `ActivityView.tsx`, `SettingsDrawer.tsx`, `TidyNamesView.tsx`, `LookalikesView.tsx`) | Missing `@media (prefers-reduced-motion: reduce)`; hover `padding-left` shift caused text jitter; `ActivityView` hardcoded 4-col pipeline strip instead of responsive `.pipeline-stepper-grid`; huge filenames could overflow rows; `TopMemoryBar` & `SettingsDrawer` squeezed at narrow widths. | **Fixed & Verified** (standardized `--ease`/`--ease-out` to `cubic-bezier(0.16, 1, 0.3, 1)`, added `prefers-reduced-motion` rule, removed hover padding shift, used `.pipeline-stepper-grid`, added `wordBreak`/`overflow` guards; verified via Playwright) |
| A9 | P2 | Rust Clippy (`src-tauri/src/sidecar.rs`) | `SidecarManager` missing `Default` implementation warning. | **Fixed & Verified** (`cargo clippy --all-targets -- -D warnings` passes with 0 warnings) |

## Changed Files
- Backend: `python-engine/main.py`, `python-engine/parsers.py`, `python-engine/vector_store.py`, `python-engine/tests/test_engine.py`
- Rust: `src-tauri/src/sidecar.rs`
- Frontend: `src/index.css`, `src/lib/tauri-api.ts`, `src/App.tsx`, `src/components/TopMemoryBar.tsx`, `src/components/InspectorSheet.tsx`, `src/components/ShelvesView.tsx`, `src/components/FindView.tsx`, `src/components/TidyNamesView.tsx`, `src/components/LookalikesView.tsx`, `src/components/ActivityView.tsx`, `src/components/SettingsDrawer.tsx`, `src/components/FileActionDialogs.tsx`, `src/components/SmartCleanModal.tsx`, `src/components/CommandPalette.tsx`

## Final Verification Results (Post-Final-Edit)
- `pnpm lint` (`tsc --noEmit`): **PASS** (0 errors)
- `pnpm build` (`tsc && vite build`): **PASS** (0 errors, `dist/` built cleanly)
- `.venv/bin/python -m pytest python-engine/tests/test_engine.py -v`: **6/6 PASS**
- `cd src-tauri && cargo clippy --all-targets -- -D warnings`: **PASS** (0 warnings)
- `cd src-tauri && cargo test`: **7/7 PASS**
- Live Chromium Playwright E2E Audit (fresh DB onboarding in run 1 + full interactive 5-view/Settings/modal/keyboard/theme/reduced-motion/viewport pass in run 2): **PASS** across all views at `1920×1080`, `1024×768`, and `760×700`, `0` page errors, `0` layout overlaps/overflows, `0` sidecar stderr errors.
