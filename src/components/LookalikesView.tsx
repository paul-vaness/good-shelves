import React, { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { LookalikePair, ShelfFile, resolveLocalAssetSrc } from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";

interface LookalikesViewProps {
  active: boolean;
  lookalikes: LookalikePair[];
  files?: ShelfFile[];
  onBackToHome?: () => void;
  onResolveLookalike: (pairId: string) => Promise<void> | void;
  onSwapLookalike?: (pairId: string) => Promise<void> | void;
  onSelectFile: (fileId: string) => void;
}

export const LookalikesView = memoView<LookalikesViewProps>(({
  active,
  lookalikes,
  files = [],
  onBackToHome,
  onResolveLookalike,
  onSwapLookalike,
  onSelectFile,
}) => {
const [openDiffIds, setOpenDiffIds] = useState<Record<string, boolean>>({});
    const [busyPairId, setBusyPairId] = useState<string | null>(null);
    const [confirmArchiveId, setConfirmArchiveId] = useState<string | null>(null);
    const confirmTimerRef = useRef<number | null>(null);
    const fileById = useMemo(() => new Map(files.map((f) => [f.id, f])), [files]);

  useEffect(() => {
    return () => {
      if (confirmTimerRef.current) window.clearTimeout(confirmTimerRef.current);
    };
  }, []);

  const toggleDiff = (id: string) => {
    setOpenDiffIds((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const handleResolve = async (pairId: string) => {
    if (busyPairId) return;
    if (confirmArchiveId === pairId) {
      if (confirmTimerRef.current) {
        window.clearTimeout(confirmTimerRef.current);
        confirmTimerRef.current = null;
      }
      setConfirmArchiveId(null);
      setBusyPairId(pairId);
      try {
        await onResolveLookalike(pairId);
      } finally {
        setBusyPairId(null);
      }
      return;
    }
    setConfirmArchiveId(pairId);
    if (confirmTimerRef.current) window.clearTimeout(confirmTimerRef.current);
    confirmTimerRef.current = window.setTimeout(() => {
      confirmTimerRef.current = null;
      setConfirmArchiveId((c) => (c === pairId ? null : c));
    }, 4000);
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
            {onBackToHome && (
              <button
                type="button"
                className="page-back-btn"
                onClick={onBackToHome}
                title="Back to Home (Esc)"
                aria-label="Back to Home"
              >
                <ArrowLeft size={13} strokeWidth={1.75} aria-hidden="true" />
                <span>Home</span>
              </button>
            )}
            <h1 className="page-title">Duplicates &amp; lookalikes</h1>
            <p className="page-subtitle">
              Exact and near-duplicate files detected locally. Archived copies stay recoverable in Trash &amp; Archive.
            </p>
          </div>
          {reclaimTotalMB === "0.00" ? (
            <div style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
              <span id="reclaimTotalLabel" className="tabular-nums">0 MB</span> reclaimable
            </div>
          ) : (
            <div
              style={{
                padding: "7px 14px",
                border: "1px solid var(--rule-strong)",
                borderRadius: "var(--radius-sm)",
                fontSize: "12.5px",
                background: "var(--bg-surface)",
                color: "var(--ink)",
              }}
            >
              Space ready to reclaim:{" "}
              <strong id="reclaimTotalLabel" className="tabular-nums" style={{ fontWeight: 600 }}>
                {reclaimTotalMB} MB
              </strong>
            </div>
          )}
        </div>

        <div id="lookalikesListWrap" style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
          {lookalikes.length === 0 ? (
            <div className="surface-card" style={{ padding: "28px 20px", textAlign: "center", fontSize: "14px", color: "var(--ink-2)" }}>
              No duplicate or near-duplicate files detected across your watched folders.
            </div>
          ) : (
            lookalikes.map((p) => {
              const isDiffOpen = Boolean(openDiffIds[p.id]);
              return (
                <div
                  key={p.id}
                  className="surface-card"
                  style={{
                    opacity: p.resolved ? 0.56 : 1,
                    transition: "opacity 0.25s var(--ease)",
                  }}
                >
                  <div
                    style={{
                      paddingBottom: "12px",
                      borderBottom: "1px solid var(--rule)",
                    }}
                  >
                    <span className="file-type-tag">{p.badge}</span>
                    <h3 className="font-display" style={{ fontSize: "19px", fontWeight: 500, marginTop: "6px" }}>
                      {p.title}
                    </h3>
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
                          <div
                            onClick={() => onSelectFile(p.keep.id)}
                            className="folder-choice-card selected"
                            style={{ flexDirection: "column", alignItems: "stretch", justifyContent: "flex-start", padding: "12px", cursor: "pointer" }}
                          >
                            {keepThumb && (
                              <div style={{ marginBottom: "8px", borderRadius: "var(--radius-sm)", overflow: "hidden", border: "1px solid var(--rule)", height: "120px", background: "var(--bg-surface)" }}>
                                <img
                                  src={keepThumb}
                                  alt={p.keep.name}
                                  loading="lazy"
                                  decoding="async"
                                  onError={(e) => {
                                    (e.currentTarget as HTMLImageElement).style.display = "none";
                                  }}
                                  style={{ width: "100%", height: "100%", objectFit: "contain" }}
                                />
                              </div>
                            )}
                            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" }}>
                              <span style={{ fontSize: "11px", fontWeight: 600, color: "var(--success)", letterSpacing: "0.03em" }}>
                                Keep this file
                              </span>
                              <button
                                type="button"
                                className="btn-tactile sm"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  onSelectFile(p.keep.id);
                                }}
                              >
                                Preview
                              </button>
                            </div>
                            <div className="font-display" style={{ fontSize: "14px", fontWeight: 500, marginTop: "4px", wordBreak: "break-all" }}>
                              {p.keep.name}
                            </div>
                            <div style={{ fontSize: "12px", color: "var(--ink-2)", marginTop: "2px" }}>{p.keep.meta}</div>
                          </div>

                          <div
                            onClick={() => onSelectFile(p.copy.id)}
                            className="folder-choice-card"
                            style={{ flexDirection: "column", alignItems: "stretch", justifyContent: "flex-start", padding: "12px", cursor: "pointer" }}
                          >
                            {copyThumb && (
                              <div style={{ marginBottom: "8px", borderRadius: "var(--radius-sm)", overflow: "hidden", border: "1px solid var(--rule)", height: "120px", background: "var(--bg-subtle)" }}>
                                <img
                                  src={copyThumb}
                                  alt={p.copy.name}
                                  loading="lazy"
                                  decoding="async"
                                  onError={(e) => {
                                    (e.currentTarget as HTMLImageElement).style.display = "none";
                                  }}
                                  style={{ width: "100%", height: "100%", objectFit: "contain" }}
                                />
                              </div>
                            )}
                            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" }}>
                              <span style={{ fontSize: "11px", fontWeight: 600, color: "var(--warning)", letterSpacing: "0.03em" }}>
                                Archive copy
                              </span>
                              <button
                                type="button"
                                className="btn-tactile sm"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  onSelectFile(p.copy.id);
                                }}
                              >
                                Preview
                              </button>
                            </div>
                            <div className="font-display" style={{ fontSize: "14px", fontWeight: 500, marginTop: "4px", wordBreak: "break-all" }}>
                              {p.copy.name}
                            </div>
                            <div style={{ fontSize: "12px", color: "var(--ink-2)", marginTop: "2px" }}>{p.copy.meta}</div>
                          </div>
                        </>
                      );
                    })()}
                  </div>

                  <div
                    style={{
                      marginTop: "12px",
                      padding: "8px 12px",
                      borderRadius: "var(--radius-sm)",
                      background: "var(--bg-subtle)",
                      border: "1px solid var(--rule-soft)",
                      fontSize: "12px",
                      color: "var(--ink-2)",
                      lineHeight: 1.45,
                    }}
                  >
                    What will change: <strong>‘{p.copy.name}’</strong> in {p.copy.meta.split("·")[0].trim()} will move to your reversible archive folder ({p.saved_mb} MB reclaimed). <strong>‘{p.keep.name}’</strong> stays untouched.
                  </div>

                  <div
                    style={{
                      borderTop: "1px solid var(--rule-soft)",
                      marginTop: "14px",
                      paddingTop: "12px",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      flexWrap: "wrap",
                      gap: "8px",
                    }}
                  >
                    <button
                      type="button"
                      className="btn-tactile sm"
                      onClick={() => toggleDiff(p.id)}
                    >
                      <span>{isDiffOpen ? "Hide differences" : "Compare differences"}</span>
                    </button>
                    <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
                      {!p.resolved && onSwapLookalike && (
                        <button
                          type="button"
                          className="btn-tactile sm"
                          disabled={busyPairId === p.id}
                          onClick={() => handleSwap(p.id)}
                          title="Swap which file is kept vs archived"
                        >
                          Swap
                        </button>
                      )}
                      <button
                        type="button"
                        className={`btn-tactile sm ${p.resolved ? "" : "solid"}`}
                        disabled={busyPairId === p.id}
                        onClick={() => handleResolve(p.id)}
                      >
                        {busyPairId === p.id
                          ? "Working…"
                          : !p.resolved && confirmArchiveId === p.id
                          ? "Confirm Archive"
                          : p.resolved
                          ? "Restore archived copy"
                          : `Archive copy (${p.saved_mb} MB)`}
                      </button>
                    </div>
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
                              borderRadius: "var(--radius-sm)",
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
                          <div style={{ padding: "10px 12px", border: "1px solid var(--rule)", borderRadius: "var(--radius-sm)", background: "var(--bg-subtle)" }}>
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
