import React, { useMemo, useState } from "react";
import { Pencil, FolderInput, Trash2 } from "lucide-react";
import { SavedSearch, ShelfFile } from "../lib/tauri-api";

interface FindViewProps {
  active: boolean;
  query: string;
  modalityFilter: string;
  results: ShelfFile[];
  allFiles?: ShelfFile[];
  savedSearches?: SavedSearch[];
  suggestions: string[];
  latencyMs: number;
  selectedFileId: string;
  onQueryChange: (q: string) => void;
  onTriggerPrompt: (q: string) => void;
  onSetModalityFilter: (mod: string) => void;
  onSelectFile: (fileId: string) => void;
  onOpenQuickLook: () => void;
  onSaveSearch?: (name: string, query: string, modality: string, tag: string) => void;
  onDeleteSavedSearch?: (searchId: string) => void;
  onRenameFile?: (file: ShelfFile) => void;
  onMoveFile?: (file: ShelfFile) => void;
  onDeleteFile?: (file: ShelfFile) => void;
}

const FALLBACK_QUERIES = [
  "recent receipts and invoices",
  "presentation slides with diagrams",
  "scripts that clean data",
  "research papers and reports",
  "identity scans and forms",
];

export const FindView: React.FC<FindViewProps> = ({
  active,
  query,
  modalityFilter,
  results,
  allFiles = [],
  savedSearches = [],
  suggestions,
  latencyMs,
  selectedFileId,
  onQueryChange,
  onTriggerPrompt,
  onSetModalityFilter,
  onSelectFile,
  onOpenQuickLook,
  onSaveSearch,
  onDeleteSavedSearch,
  onRenameFile,
  onMoveFile,
  onDeleteFile,
}) => {
  const [activeTag, setActiveTag] = useState<string>("");

  const availableTags = useMemo(() => {
    const set = new Set<string>();
    for (const f of allFiles.length ? allFiles : results) {
      for (const t of f.tags || []) {
        set.add(t);
      }
    }
    return Array.from(set).sort();
  }, [allFiles, results]);

  const filteredResults = useMemo(() => {
    if (!activeTag) return results;
    return results.filter((f) => (f.tags || []).includes(activeTag));
  }, [results, activeTag]);
  return (
    <section className={`view-sheet ${active ? "active" : ""}`} id="sheet-find">
      <div style={{ maxWidth: "820px", margin: "0 auto" }}>
        <div
          style={{
            paddingBottom: "20px",
            borderBottom: "1.5px solid var(--ink)",
            marginBottom: "20px",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "baseline",
            }}
          >
            <span
              style={{
                fontSize: "11.5px",
                fontWeight: 500,
                letterSpacing: "0.06em",
                color: "var(--ink-3)",
              }}
            >
              semantic search
            </span>
            <span
              id="findSpeedLabel"
              className="font-mono-code"
              style={{ fontSize: "11.5px", color: "var(--ink-3)" }}
            >
              {latencyMs.toFixed(1)} ms · local sqlite-vec index
            </span>
          </div>

          <input
            id="dedicatedSearchInput"
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={query}
            placeholder="search by topic, text, or visual content…"
            className="font-display"
            style={{
              width: "100%",
              fontSize: "clamp(26px, 3.6vw, 40px)",
              fontWeight: 300,
              letterSpacing: "-0.025em",
              marginTop: "8px",
              color: "var(--ink)",
            }}
            onChange={(e) => onQueryChange(e.target.value)}
          />

          {/* Clickable Memory Prompts */}
          <div
            style={{
              marginTop: "16px",
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              gap: "8px",
            }}
          >
            <span
              style={{
                fontSize: "12px",
                color: "var(--ink-3)",
                marginRight: "4px",
              }}
            >
              suggestions:
            </span>
            {(suggestions.length > 0 ? suggestions : FALLBACK_QUERIES).map((promptText) => (
              <button
                key={promptText}
                type="button"
                className={`btn-tactile ${
                  query.toLowerCase() === promptText.toLowerCase()
                    ? "active-tab"
                    : ""
                }`}
                onClick={() => onTriggerPrompt(promptText)}
              >
                “{promptText}”
              </button>
            ))}
          </div>
        </div>

        {/* Modality Filter Buttons */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "12px",
            marginBottom: "16px",
          }}
        >
          <div className="seg-group">
            {[
              { id: "all", label: "all" },
              { id: "image", label: "images" },
              { id: "document", label: "docs & sheets" },
              { id: "video", label: "video" },
              { id: "audio", label: "audio" },
              { id: "code", label: "code" },
            ].map((mod) => (
              <button
                key={mod.id}
                type="button"
                className={`seg-btn ${
                  modalityFilter === mod.id ? "active" : ""
                }`}
                onClick={() => onSetModalityFilter(mod.id)}
              >
                {mod.label}
              </button>
            ))}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            {onSaveSearch && query.trim().length > 0 && (
              <button
                type="button"
                className="btn-tactile"
                style={{ fontSize: "11.5px", padding: "4px 10px" }}
                onClick={() => onSaveSearch(query.trim(), query.trim(), modalityFilter, activeTag)}
              >
                + save search
              </button>
            )}
            <span
              id="findResultsCounter"
              style={{ fontSize: "12.5px", color: "var(--ink-3)" }}
            >
              {filteredResults.length}{" "}
              {filteredResults.length === 1 ? "matching file" : "matching files"}
            </span>
          </div>
        </div>

        {/* Saved Searches & Tag Filters */}
        {(savedSearches.length > 0 || availableTags.length > 0) && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", marginBottom: "14px", alignItems: "center" }}>
            {savedSearches.map((ss) => (
              <span
                key={ss.id}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                  padding: "3px 9px",
                  border: "1px solid var(--rule-strong)",
                  borderRadius: "999px",
                  fontSize: "11.5px",
                  background: "var(--bg-subtle)",
                }}
              >
                <button
                  type="button"
                  style={{ cursor: "pointer", color: "var(--ink)" }}
                  onClick={() => {
                    onSetModalityFilter(ss.modality || "all");
                    setActiveTag(ss.tag || "");
                    onTriggerPrompt(ss.query);
                  }}
                >
                  ★ {ss.name}
                </button>
                {onDeleteSavedSearch && (
                  <button
                    type="button"
                    style={{ cursor: "pointer", color: "var(--ink-3)" }}
                    onClick={() => onDeleteSavedSearch(ss.id)}
                  >
                    ×
                  </button>
                )}
              </span>
            ))}
            {availableTags.map((tag) => (
              <button
                key={tag}
                type="button"
                className={`btn-tactile ${activeTag === tag ? "solid" : ""}`}
                style={{ fontSize: "11px", padding: "3px 8px", borderRadius: "999px" }}
                onClick={() => setActiveTag((prev) => (prev === tag ? "" : tag))}
              >
                #{tag}
              </button>
            ))}
          </div>
        )}

        <div className="shelf-section" id="findResultsContainer">
          {filteredResults.length === 0 ? (
            <div
              style={{
                padding: "32px 20px",
                color: "var(--ink-2)",
                fontSize: "14px",
              }}
            >
              No files matched that memory. Try another phrase, add a folder
              in settings, or clear the format filter.
            </div>
          ) : (
            filteredResults.map((f) => {
              const isSel = f.id === selectedFileId;
              return (
                <div
                  key={f.id}
                  className={`file-item-row ${isSel ? "selected" : ""}`}
                  onClick={() => onSelectFile(f.id)}
                  onDoubleClick={onOpenQuickLook}
                >
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "10px",
                        flexWrap: "wrap",
                      }}
                    >
                      <span className="file-type-tag">{f.format}</span>
                      <span
                        className="font-display"
                        style={{
                          fontSize: "17px",
                          fontWeight: 400,
                          color: "var(--ink)",
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                        }}
                      >
                        {f.title}
                      </span>
                      <span
                        className="font-mono-code"
                        style={{ fontSize: "11.5px", color: "var(--ink-3)" }}
                      >
                        {f.name}
                      </span>
                    </div>
                    <div
                      style={{
                        fontSize: "13px",
                        color: "var(--ink-2)",
                        marginTop: "3px",
                      }}
                    >
                      {f.whyMatched}
                    </div>
                  </div>

                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "12px",
                      flexShrink: 0,
                    }}
                  >
                    {(onRenameFile || onMoveFile || onDeleteFile) && (
                      <div className="row-quick-actions" onClick={(e) => e.stopPropagation()}>
                        {onRenameFile && (
                          <button
                            type="button"
                            className="row-icon-btn"
                            title="Rename file"
                            onClick={() => onRenameFile(f)}
                          >
                            <Pencil size={13} />
                          </button>
                        )}
                        {onMoveFile && (
                          <button
                            type="button"
                            className="row-icon-btn"
                            title="Move or copy file"
                            onClick={() => onMoveFile(f)}
                          >
                            <FolderInput size={13} />
                          </button>
                        )}
                        {onDeleteFile && (
                          <button
                            type="button"
                            className="row-icon-btn danger"
                            title="Delete or trash file"
                            onClick={() => onDeleteFile(f)}
                          >
                            <Trash2 size={13} />
                          </button>
                        )}
                      </div>
                    )}
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "flex-end",
                        justifyContent: "center",
                        gap: "2px",
                      }}
                    >
                      <span
                        className="font-mono-code"
                        style={{
                          fontSize: "12px",
                          fontWeight: 600,
                          color: "var(--ink)",
                        }}
                      >
                        {f.matchScore}% match
                      </span>
                      <span style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>
                        {f.shelfName}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </section>
  );
};
