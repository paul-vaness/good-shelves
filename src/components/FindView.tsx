import React, { useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Pencil, FolderInput, Trash2, Sparkles, MessageSquare } from "lucide-react";
import { AskResponse, SavedSearch, ShelfFile } from "../lib/tauri-api";

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
  similarSource?: ShelfFile | null;
  askResult?: AskResponse | null;
  asking?: boolean;
  onQueryChange: (q: string) => void;
  onClearQuery?: () => void;
  onClearSimilar?: () => void;
  onAskShelves?: (question: string) => void;
  onTriggerPrompt: (q: string) => void;
  onSetModalityFilter: (mod: string) => void;
  onSelectFile: (fileId: string) => void;
  onOpenQuickLook: () => void;
  onSaveSearch?: (name: string, query: string, modality: string, tag: string) => void;
  onDeleteSavedSearch?: (searchId: string) => void;
  onRenameFile?: (file: ShelfFile) => void;
  onMoveFile?: (file: ShelfFile) => void;
  onDeleteFile?: (file: ShelfFile) => void;
  onContextMenuFile?: (e: React.MouseEvent, file: ShelfFile) => void;
}

const OPERATOR_CHIPS = [
  "type:pdf",
  "type:image",
  "has:ocr",
  "folder:Downloads",
  "unnamed:true",
  "after:2026-01-01",
];

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
  similarSource = null,
  askResult = null,
  asking = false,
  onQueryChange,
  onClearQuery,
  onClearSimilar,
  onAskShelves,
  onTriggerPrompt,
  onSetModalityFilter,
  onSelectFile,
  onOpenQuickLook,
  onSaveSearch,
  onDeleteSavedSearch,
  onRenameFile,
  onMoveFile,
  onDeleteFile,
  onContextMenuFile,
}) => {
  const [activeTag, setActiveTag] = useState<string>("");
  const [findMode, setFindMode] = useState<"search" | "ask">("search");
  const resultsScrollRef = useRef<HTMLDivElement | null>(null);

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

  const rowVirtualizer = useVirtualizer({
    count: filteredResults.length,
    getScrollElement: () => resultsScrollRef.current,
    estimateSize: () => 78,
    overscan: 6,
  });

  const appendOperator = (op: string) => {
    const trimmed = query.trim();
    if (trimmed.includes(op)) return;
    onQueryChange(trimmed ? `${trimmed} ${op}` : op);
  };

  const handleListKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
    const target = e.target as HTMLElement | null;
    if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
    if (!filteredResults.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      const idx = filteredResults.findIndex((f) => f.id === selectedFileId);
      const nextIdx =
        e.key === "ArrowDown"
          ? Math.min(filteredResults.length - 1, idx + 1)
          : Math.max(0, idx <= 0 ? 0 : idx - 1);
      onSelectFile(filteredResults[nextIdx].id);
    } else if (e.key === "Enter" && selectedFileId) {
      if (target && (target.tagName === "BUTTON" || target.tagName === "A")) return;
      onOpenQuickLook();
    }
  };

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-find"
      tabIndex={0}
      onKeyDown={handleListKeyDown}
    >
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
              alignItems: "center",
              flexWrap: "wrap",
              gap: "8px",
            }}
          >
            <div className="seg-group" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={findMode === "search"}
                className={`seg-btn ${findMode === "search" ? "active" : ""}`}
                onClick={() => setFindMode("search")}
              >
                semantic search
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={findMode === "ask"}
                className={`seg-btn ${findMode === "ask" ? "active" : ""}`}
                onClick={() => setFindMode("ask")}
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
                  <MessageSquare size={11} /> ask your shelves
                </span>
              </button>
            </div>
            <span
              id="findSpeedLabel"
              className="font-mono-code"
              style={{ fontSize: "11.5px", color: "var(--ink-3)" }}
            >
              {latencyMs.toFixed(1)} ms · local sqlite-vec index
            </span>
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (findMode === "ask" && onAskShelves && query.trim()) {
                onAskShelves(query.trim());
              }
            }}
            style={{ display: "flex", alignItems: "center", gap: "10px", marginTop: "8px" }}
          >
            <input
              id="dedicatedSearchInput"
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={query}
              placeholder={
                findMode === "ask"
                  ? "ask a question across your files (press Enter)…"
                  : "search by topic, text, operators (type:pdf), or visual content…"
              }
              className="font-display"
              style={{
                flex: 1,
                fontSize: "clamp(24px, 3.4vw, 38px)",
                fontWeight: 300,
                letterSpacing: "-0.025em",
                color: "var(--ink)",
              }}
              onChange={(e) => onQueryChange(e.target.value)}
            />
            {findMode === "ask" && onAskShelves && (
              <button
                type="submit"
                className="btn-tactile solid"
                disabled={asking || !query.trim()}
              >
                {asking ? "reading…" : "ask files"}
              </button>
            )}
          </form>

          {similarSource && (
            <div
              style={{
                marginTop: "12px",
                padding: "8px 12px",
                borderRadius: "6px",
                border: "1px solid var(--ink)",
                background: "var(--bg-subtle)",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "10px",
                fontSize: "12.5px",
              }}
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                <Sparkles size={13} />
                Showing files visually &amp; semantically similar to <strong>{similarSource.name}</strong>
              </span>
              {onClearSimilar && (
                <button type="button" className="btn-tactile" style={{ padding: "2px 8px", fontSize: "11px" }} onClick={onClearSimilar}>
                  clear
                </button>
              )}
            </div>
          )}

          {/* Operator Chips */}
          {findMode === "search" && (
            <div style={{ marginTop: "12px", display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px" }}>
              <span style={{ fontSize: "11.5px", color: "var(--ink-3)", marginRight: "4px" }}>filters:</span>
              {OPERATOR_CHIPS.map((op) => (
                <button
                  key={op}
                  type="button"
                  className="tag-pill-suggest font-mono-code"
                  onClick={() => appendOperator(op)}
                >
                  + {op}
                </button>
              ))}
            </div>
          )}

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
              { id: "archive", label: "archives" },
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
          <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
            {onClearQuery && query.trim().length > 0 && (
              <button
                type="button"
                className="btn-tactile"
                style={{ fontSize: "11.5px", padding: "4px 10px" }}
                onClick={onClearQuery}
              >
                clear
              </button>
            )}
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
                    aria-label={`Remove saved search ${ss.name}`}
                    title="Remove saved search"
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

        {/* Ask Your Shelves Grounded Q&A Panel */}
        {findMode === "ask" && askResult && (
          <div className="shelf-section" style={{ padding: "18px 20px", marginBottom: "18px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
              <span style={{ fontSize: "11px", fontWeight: 600, letterSpacing: "0.06em", color: "var(--ink-3)", textTransform: "uppercase" }}>
                grounded local answer · {askResult.mode}
              </span>
              <span className="font-mono-code" style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                {askResult.latencyMs.toFixed(1)} ms
              </span>
            </div>
            <p className="font-display" style={{ fontSize: "17px", lineHeight: 1.5, color: "var(--ink)", marginBottom: "14px" }}>
              {askResult.answer}
            </p>
            {askResult.citations.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                <div style={{ fontSize: "11px", fontWeight: 600, color: "var(--ink-3)", textTransform: "uppercase" }}>
                  cited sources ({askResult.citations.length})
                </div>
                {askResult.citations.map((cit, idx) => (
                  <button
                    key={`${cit.fileId}-${idx}`}
                    type="button"
                    className="folder-choice-card"
                    style={{ alignItems: "flex-start" }}
                    onClick={() => {
                      onSelectFile(cit.fileId);
                      onOpenQuickLook();
                    }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "3px" }}>
                        <span className="font-mono-code" style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                          [{idx + 1}]
                        </span>
                        <span className="file-type-tag">{cit.format}</span>
                        <span style={{ fontSize: "13px", fontWeight: 500 }}>{cit.fileName}</span>
                        <span className="font-mono-code" style={{ fontSize: "11px", color: "var(--ink-3)" }}>
                          {cit.score}%
                        </span>
                      </div>
                      <div style={{ fontSize: "12.5px", color: "var(--ink-2)", lineHeight: 1.45 }}>
                        “{cit.snippet}”
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}
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
          ) : filteredResults.length > 40 ? (
            <div ref={resultsScrollRef} style={{ maxHeight: "560px", overflowY: "auto" }}>
              <div style={{ height: `${rowVirtualizer.getTotalSize()}px`, position: "relative", width: "100%" }}>
                {rowVirtualizer.getVirtualItems().map((vRow) => {
                  const f = filteredResults[vRow.index];
                  const isSel = f.id === selectedFileId;
                  return (
                    <div
                      key={f.id}
                      className={`file-item-row ${isSel ? "selected" : ""}`}
                      style={{
                        position: "absolute",
                        top: 0,
                        left: 0,
                        width: "100%",
                        transform: `translateY(${vRow.start}px)`,
                      }}
                      onClick={() => onSelectFile(f.id)}
                      onDoubleClick={onOpenQuickLook}
                      onContextMenu={(e) => onContextMenuFile?.(e, f)}
                    >
                      <div style={{ minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
                          <span className="file-type-tag">{f.format}</span>
                          <span className="font-display" style={{ fontSize: "17px", fontWeight: 400, color: "var(--ink)" }}>
                            {f.title}
                          </span>
                          <span className="font-mono-code" style={{ fontSize: "11.5px", color: "var(--ink-3)" }}>
                            {f.name}
                          </span>
                        </div>
                        <div style={{ fontSize: "13px", color: "var(--ink-2)", marginTop: "3px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                          {f.whyMatched}
                        </div>
                      </div>
                      <span className="font-mono-code" style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink)", alignSelf: "center" }}>
                        {f.matchScore}% match
                      </span>
                    </div>
                  );
                })}
              </div>
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
                  onContextMenu={(e) => onContextMenuFile?.(e, f)}
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
                        style={{
                          fontSize: "11.5px",
                          color: "var(--ink-3)",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                          maxWidth: "100%",
                        }}
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
                            aria-label={`Rename ${f.name}`}
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
                            aria-label={`Move ${f.name}`}
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
                            aria-label={`Delete ${f.name}`}
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
