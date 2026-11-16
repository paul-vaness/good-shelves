import React, { useState } from "react";
import { LookalikePair } from "../lib/tauri-api";

interface LookalikesViewProps {
  active: boolean;
  lookalikes: LookalikePair[];
  onResolveLookalike: (pairId: string) => void;
  onSelectFile: (fileId: string) => void;
}

export const LookalikesView: React.FC<LookalikesViewProps> = ({
  active,
  lookalikes,
  onResolveLookalike,
  onSelectFile,
}) => {
  const [openDiffIds, setOpenDiffIds] = useState<Record<string, boolean>>({});

  const toggleDiff = (id: string) => {
    setOpenDiffIds((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const reclaimTotalMB = lookalikes
    .filter((d) => !d.resolved)
    .reduce((sum, d) => sum + d.saved_mb, 0)
    .toFixed(2);

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-lookalikes"
    >
      <div style={{ maxWidth: "820px", margin: "0 auto" }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "flex-end",
            justifyContent: "space-between",
            gap: "16px",
            paddingBottom: "20px",
            borderBottom: "1.5px solid var(--ink)",
            marginBottom: "24px",
          }}
        >
          <div>
            <div
              style={{
                fontSize: "11.5px",
                fontWeight: 500,
                letterSpacing: "0.06em",
                color: "var(--ink-3)",
              }}
            >
              near-duplicates
            </div>
            <h1
              className="font-display"
              style={{
                fontSize: "clamp(32px, 4.2vw, 48px)",
                fontWeight: 300,
                letterSpacing: "-0.03em",
                lineHeight: 1.02,
              }}
            >
              duplicates
            </h1>
            <p
              style={{
                fontSize: "15px",
                color: "var(--ink-2)",
                marginTop: "8px",
                maxWidth: "56ch",
              }}
            >
              detects exact copies, cropped screenshots, and edited revisions
              across watched folders.
            </p>
          </div>
          <div
            style={{
              padding: "7px 13px",
              border: "1px solid var(--rule-strong)",
              borderRadius: "6px",
              fontSize: "12.5px",
              background: "var(--bg-subtle)",
            }}
          >
            space ready to reclaim:{" "}
            <strong id="reclaimTotalLabel" style={{ fontWeight: 600 }}>
              {reclaimTotalMB} MB
            </strong>
          </div>
        </div>

        <div
          id="lookalikesListWrap"
          style={{ display: "flex", flexDirection: "column", gap: "20px" }}
        >
          {lookalikes.length === 0 ? (
            <div
              className="shelf-section"
              style={{
                padding: "28px 20px",
                fontSize: "14px",
                color: "var(--ink-2)",
              }}
            >
              No near-duplicate lookalikes detected across watched folders.
            </div>
          ) : (
            lookalikes.map((p) => {
              const isDiffOpen = Boolean(openDiffIds[p.id]);
              return (
                <div
                  key={p.id}
                  className="shelf-section"
                  style={{
                    padding: "20px",
                    opacity: p.resolved ? 0.52 : 1,
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
                      paddingBottom: "14px",
                      borderBottom: "1px solid var(--rule)",
                    }}
                  >
                    <div>
                      <span className="file-type-tag">{p.badge}</span>
                      <h3
                        className="font-display"
                        style={{
                          fontSize: "21px",
                          fontWeight: 400,
                          marginTop: "6px",
                        }}
                      >
                        {p.title}
                      </h3>
                    </div>
                    <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                      <button
                        type="button"
                        className="btn-tactile"
                        onClick={() => toggleDiff(p.id)}
                      >
                        {isDiffOpen
                          ? "hide differences ↑"
                          : "show differences ↓"}
                      </button>
                      <button
                        type="button"
                        className={`btn-tactile ${p.resolved ? "" : "solid"}`}
                        onClick={() => onResolveLookalike(p.id)}
                      >
                        {p.resolved
                          ? "undo archive"
                          : `keep best & archive copy (${p.saved_mb} MB)`}
                      </button>
                    </div>
                  </div>

                  <p
                    style={{
                      fontSize: "13.5px",
                      color: "var(--ink-2)",
                      marginTop: "12px",
                    }}
                  >
                    {p.desc}
                  </p>

                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns:
                        "repeat(auto-fit, minmax(240px, 1fr))",
                      gap: "12px",
                      marginTop: "14px",
                    }}
                  >
                    <div
                      onClick={() => onSelectFile(p.keep.id)}
                      style={{
                        padding: "12px",
                        border: "1px solid var(--ink)",
                        borderRadius: "6px",
                        background: "var(--bg-subtle)",
                        cursor: "pointer",
                      }}
                    >
                      <div
                        style={{
                          fontSize: "10.5px",
                          fontWeight: 600,
                          color: "var(--ink-3)",
                        }}
                      >
                        KEEP THIS FILE
                      </div>
                      <div
                        className="font-mono-code"
                        style={{
                          fontSize: "13px",
                          fontWeight: 600,
                          marginTop: "3px",
                        }}
                      >
                        {p.keep.name}
                      </div>
                      <div
                        style={{
                          fontSize: "12px",
                          color: "var(--ink-2)",
                          marginTop: "2px",
                        }}
                      >
                        {p.keep.meta}
                      </div>
                    </div>

                    <div
                      onClick={() => onSelectFile(p.copy.id)}
                      style={{
                        padding: "12px",
                        border: "1px solid var(--rule)",
                        borderRadius: "6px",
                        background: "var(--bg-canvas)",
                        cursor: "pointer",
                      }}
                    >
                      <div
                        style={{
                          fontSize: "10.5px",
                          fontWeight: 600,
                          color: "var(--ink-3)",
                        }}
                      >
                        REDUNDANT LOOKALIKE
                      </div>
                      <div
                        className="font-mono-code"
                        style={{ fontSize: "13px", marginTop: "3px" }}
                      >
                        {p.copy.name}
                      </div>
                      <div
                        style={{
                          fontSize: "12px",
                          color: "var(--ink-2)",
                          marginTop: "2px",
                        }}
                      >
                        {p.copy.meta}
                      </div>
                    </div>
                  </div>

                  {/* Real Diff Accordion */}
                  <div
                    className={`accordion-drawer ${isDiffOpen ? "open" : ""}`}
                  >
                    <div className="accordion-inner">
                      <div
                        style={{
                          marginTop: "14px",
                          paddingTop: "14px",
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
                              borderRadius: "6px",
                              background: "var(--bg-canvas)",
                              fontSize: "12.5px",
                            }}
                          >
                            <div
                              style={{
                                fontSize: "11px",
                                fontWeight: 600,
                                color: "var(--ink-3)",
                                textTransform: "uppercase",
                              }}
                            >
                              {d.label}
                            </div>
                            <div style={{ marginTop: "3px" }}>
                              <strong>Kept version:</strong> {d.keep}
                            </div>
                            <div
                              style={{
                                color: "var(--ink-2)",
                                marginTop: "2px",
                              }}
                            >
                              <strong>Redundant copy:</strong> {d.copy}
                            </div>
                          </div>
                        ))}

                        {p.unified_diff && p.unified_diff.length > 0 && (
                          <div
                            style={{
                              padding: "10px 12px",
                              border: "1px solid var(--rule)",
                              borderRadius: "6px",
                              background: "var(--bg-subtle)",
                            }}
                          >
                            <div
                              style={{
                                fontSize: "10.5px",
                                fontWeight: 600,
                                color: "var(--ink-3)",
                                fontFamily: "var(--mono)",
                                marginBottom: "6px",
                              }}
                            >
                              TEXT DIFF
                            </div>
                            <pre
                              className="font-mono-code"
                              style={{
                                fontSize: "11px",
                                lineHeight: 1.5,
                                color: "var(--ink-2)",
                                overflowX: "auto",
                                whiteSpace: "pre-wrap",
                              }}
                            >
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
};
