import React, { useMemo, useState } from "react";
import { LookalikePair, ShelfFile, resolveLocalAssetSrc } from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";

interface LookalikesViewProps {
  active: boolean;
  lookalikes: LookalikePair[];
  files?: ShelfFile[];
  onResolveLookalike: (pairId: string) => Promise<void> | void;
  onSwapLookalike?: (pairId: string) => Promise<void> | void;
  onSelectFile: (fileId: string) => void;
}

export const LookalikesView = memoView<LookalikesViewProps>(({
  active,
  lookalikes,
  files = [],
  onResolveLookalike,
  onSwapLookalike,
  onSelectFile,
}) => {
  const [openDiffIds, setOpenDiffIds] = useState<Record<string, boolean>>({});
  const [busyPairId, setBusyPairId] = useState<string | null>(null);
  const fileById = useMemo(() => new Map(files.map((f) => [f.id, f])), [files]);

  const toggleDiff = (id: string) => {
    setOpenDiffIds((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const handleResolve = async (pairId: string) => {
    if (busyPairId) return;
    setBusyPairId(pairId);
    try {
      await onResolveLookalike(pairId);
    } finally {
      setBusyPairId(null);
    }
  };

  const handleSwap = async (pairId: string) => {
    if (busyPairId || !onSwapLookalike) return;
    setBusyPairId(pairId);
    try {
      await onSwapLookalike(pairId);
    } finally {
      setBusyPairId(null);
    }
  };

  const reclaimTotalMB = lookalikes
    .filter((d) => !d.resolved)
    .reduce((sum, d) => sum + d.saved_mb, 0)
    .toFixed(2);

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-lookalikes"
      aria-hidden={!active}
      inert={!active ? true : undefined}
    >
      <div className="page-container">
        <div className="page-header">
          <div>
            <h1 className="page-title">Duplicates &amp; lookalikes</h1>
            <p className="page-subtitle">
              Exact and near-duplicate files detected locally. Archived copies stay recoverable in Trash &amp; Archive.
            </p>
          </div>
          <div
            style={{
              padding: "7px 14px",
              border: "1px solid var(--warning-border)",
              borderRadius: "8px",
              fontSize: "12.5px",
              background: "var(--warning-bg)",
              color: "var(--ink)",
            }}
          >
            Space ready to reclaim:{" "}
            <strong id="reclaimTotalLabel" style={{ fontWeight: 600 }}>
              {reclaimTotalMB} MB
            </strong>
          </div>
        </div>

        <div id="lookalikesListWrap" style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
          {lookalikes.length === 0 ? (
            <div className="shelf-section" style={{ padding: "28px 20px", textAlign: "center", fontSize: "14px", color: "var(--ink-2)" }}>
              No duplicate or near-duplicate files detected across your watched folders.
            </div>
          ) : (
            lookalikes.map((p) => {
              const isDiffOpen = Boolean(openDiffIds[p.id]);
              return (
                <div
                  key={p.id}
                  className="shelf-section"
                  style={{
                    padding: "18px 20px",
                    opacity: p.resolved ? 0.56 : 1,
                    transition: "opacity 0.25s var(--ease)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      justifyContent: "space-between",
                      alignItems: "flex-start",
                      gap: "12px",
                      paddingBottom: "12px",
                      borderBottom: "1px solid var(--rule)",
                    }}
                  >
                    <div>
                      <span className="file-type-tag">{p.badge}</span>
                      <h3 className="font-display" style={{ fontSize: "19px", fontWeight: 500, marginTop: "6px" }}>
                        {p.title}
                      </h3>
                    </div>
                    <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                      {!p.resolved && onSwapLookalike && (
                        <button
                          type="button"
                          className="btn-tactile"
                          disabled={busyPairId === p.id}
                          onClick={() => handleSwap(p.id)}
                          title="Swap which file is kept vs archived"
                        >
                          Swap keep/copy
                        </button>
                      )}
                      <button
                        type="button"
                        className={`btn-tactile ${p.resolved ? "" : "solid"}`}
                        disabled={busyPairId === p.id}
                        onClick={() => handleResolve(p.id)}
                      >
                        {busyPairId === p.id
                          ? "Working…"
                          : p.resolved
                          ? "Restore archived copy"
                          : `Keep best & archive copy (${p.saved_mb} MB)`}
                      </button>
                    </div>
                  </div>

                  <p style={{ fontSize: "13px", color: "var(--ink-2)", marginTop: "10px" }}>{p.desc}</p>

                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
                      gap: "12px",
                      marginTop: "12px",
                    }}
                  >
                    {(() => {
                      const keepFile = fileById.get(p.keep.id);
                      const copyFile = fileById.get(p.copy.id);
                      const keepThumb = keepFile ? resolveLocalAssetSrc(keepFile) : null;
                      const copyThumb = copyFile ? resolveLocalAssetSrc(copyFile) : null;
                      return (
                        <>
                          <button
                            type="button"
                            onClick={() => onSelectFile(p.keep.id)}
                            className="folder-choice-card selected"
                            style={{ flexDirection: "column", alignItems: "stretch", justifyContent: "flex-start", padding: "12px" }}
                          >
                            {keepThumb && (
                              <div style={{ marginBottom: "8px", borderRadius: "5px", overflow: "hidden", border: "1px solid var(--rule)", height: "120px", background: "var(--bg-surface)" }}>
                                <img src={keepThumb} alt={p.keep.name} loading="lazy" decoding="async" style={{ width: "100%", height: "100%", objectFit: "contain" }} />
                              </div>
                            )}
                            <span style={{ fontSize: "11px", fontWeight: 600, color: "var(--success)", letterSpacing: "0.03em" }}>
                              Keep this file
                            </span>
                            <div className="font-display" style={{ fontSize: "14px", fontWeight: 500, marginTop: "4px", wordBreak: "break-all" }}>
                              {p.keep.name}
                            </div>
                            <div style={{ fontSize: "12px", color: "var(--ink-2)", marginTop: "2px" }}>{p.keep.meta}</div>
                          </button>

                          <button
                            type="button"
                            onClick={() => onSelectFile(p.copy.id)}
                            className="folder-choice-card"
                            style={{ flexDirection: "column", alignItems: "stretch", justifyContent: "flex-start", padding: "12px" }}
                          >
                            {copyThumb && (
                              <div style={{ marginBottom: "8px", borderRadius: "5px", overflow: "hidden", border: "1px solid var(--rule)", height: "120px", background: "var(--bg-subtle)" }}>
                                <img src={copyThumb} alt={p.copy.name} loading="lazy" decoding="async" style={{ width: "100%", height: "100%", objectFit: "contain" }} />
                              </div>
                            )}
                            <span style={{ fontSize: "11px", fontWeight: 600, color: "var(--warning)", letterSpacing: "0.03em" }}>
                              Archive copy
                            </span>
                            <div className="font-display" style={{ fontSize: "14px", fontWeight: 500, marginTop: "4px", wordBreak: "break-all" }}>
                              {p.copy.name}
                            </div>
                            <div style={{ fontSize: "12px", color: "var(--ink-2)", marginTop: "2px" }}>{p.copy.meta}</div>
                          </button>
                        </>
                      );
                    })()}
                  </div>

                  <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "10px" }}>
                    <button
                      type="button"
                      className="btn-tactile"
                      style={{ padding: "3px 10px", minHeight: "28px", fontSize: "12px" }}
                      onClick={() => toggleDiff(p.id)}
                    >
                      <span>{isDiffOpen ? "Hide differences" : "Compare differences"}</span>
                    </button>
                  </div>

                  <div className={`accordion-drawer ${isDiffOpen ? "open" : ""}`}>
                    <div className="accordion-inner">
                      <div
                        style={{
                          marginTop: "12px",
                          paddingTop: "12px",
                          borderTop: "1px solid var(--rule)",
                          display: "flex",
                          flexDirection: "column",
                          gap: "8px",
                        }}
                      >
                        {p.diffs.map((d, idx) => (
                          <div
                            key={idx}
                            style={{
                              padding: "10px 12px",
                              border: "1px solid var(--rule)",
                              borderRadius: "7px",
                              background: "var(--bg-subtle)",
                              fontSize: "12.5px",
                            }}
                          >
                            <div style={{ fontSize: "11px", fontWeight: 600, color: "var(--ink-3)", textTransform: "uppercase" }}>
                              {d.label}
                            </div>
                            <div style={{ marginTop: "3px" }}>
                              <strong>Kept version:</strong> {d.keep}
                            </div>
                            <div style={{ color: "var(--ink-2)", marginTop: "2px" }}>
                              <strong>Archived copy:</strong> {d.copy}
                            </div>
                          </div>
                        ))}

                        {p.unified_diff && p.unified_diff.length > 0 && (
                          <div style={{ padding: "10px 12px", border: "1px solid var(--rule)", borderRadius: "7px", background: "var(--bg-subtle)" }}>
                            <div style={{ fontSize: "10.5px", fontWeight: 600, color: "var(--ink-3)", marginBottom: "6px" }}>
                              TEXT DIFF
                            </div>
                            <pre className="font-code-raw" style={{ fontSize: "11px", lineHeight: 1.5, color: "var(--ink-2)", overflowX: "auto", whiteSpace: "pre-wrap" }}>
                              {p.unified_diff.join("\n")}
                            </pre>
                          </div>
                        )}
                      </div>
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
});
