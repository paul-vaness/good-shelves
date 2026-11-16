import React, { useEffect, useMemo, useState } from "react";
import { Pencil, FolderInput, Trash2 } from "lucide-react";
import { ShelfFile, ShelfGroup } from "../lib/tauri-api";

interface ShelvesViewProps {
  active: boolean;
  files: ShelfFile[];
  shelves: ShelfGroup[];
  selectedFileId: string;
  activeShelfFilter: string;
  silhouetteScore: number;
  submode: "list" | "map";
  onSetSubmode: (mode: "list" | "map") => void;
  onSelectFile: (fileId: string) => void;
  onOpenQuickLook: () => void;
  onTogglePinShelf?: (shelfId: string) => void;
  onBulkAction?: (fileIds: string[], action: "move" | "trash" | "tag" | "pin" | "unpin") => void;
  onRenameFile?: (file: ShelfFile) => void;
  onMoveFile?: (file: ShelfFile) => void;
  onDeleteFile?: (file: ShelfFile) => void;
}

export const ShelvesView: React.FC<ShelvesViewProps> = ({
  active,
  files,
  shelves,
  selectedFileId,
  activeShelfFilter,
  silhouetteScore,
  submode,
  onSetSubmode,
  onSelectFile,
  onOpenQuickLook,
  onTogglePinShelf,
  onBulkAction,
  onRenameFile,
  onMoveFile,
  onDeleteFile,
}) => {
  const [mapTopicFilter, setMapTopicFilter] = useState<string>("all");
  const [hoveredFileId, setHoveredFileId] = useState<string | null>(null);
  const [zoom, setZoom] = useState<number>(1.0);
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState<boolean>(false);
  const [panStart, setPanStart] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [checkedIds, setCheckedIds] = useState<string[]>([]);
  const [lastCheckedId, setLastCheckedId] = useState<string | null>(null);

  const visibleShelves = useMemo(() => {
    const filtered = shelves.filter(
      (s) => activeShelfFilter === "all" || s.id === activeShelfFilter
    );
    return [...filtered].sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)));
  }, [shelves, activeShelfFilter]);

  const filesByShelf = useMemo(() => {
    const map = new Map<string, ShelfFile[]>();
    for (const f of files) {
      const arr = map.get(f.shelfId);
      if (arr) arr.push(f);
      else map.set(f.shelfId, [f]);
    }
    return map;
  }, [files]);

  // Prune stale checked IDs when files are deleted/trashed
  useEffect(() => {
    const valid = new Set(files.map((f) => f.id));
    setCheckedIds((prev) => {
      const next = prev.filter((id) => valid.has(id));
      return next.length === prev.length ? prev : next;
    });
  }, [files]);

  const flatVisibleFiles = useMemo(
    () => visibleShelves.flatMap((sh) => filesByShelf.get(sh.id) || []),
    [visibleShelves, filesByShelf]
  );

  const toggleChecked = (fileId: string, shiftKey: boolean) => {
    if (shiftKey && lastCheckedId) {
      const idxA = flatVisibleFiles.findIndex((f) => f.id === lastCheckedId);
      const idxB = flatVisibleFiles.findIndex((f) => f.id === fileId);
      if (idxA !== -1 && idxB !== -1) {
        const [lo, hi] = idxA < idxB ? [idxA, idxB] : [idxB, idxA];
        const rangeIds = flatVisibleFiles.slice(lo, hi + 1).map((f) => f.id);
        setCheckedIds((prev) => Array.from(new Set([...prev, ...rangeIds])));
        setLastCheckedId(fileId);
        return;
      }
    }
    setCheckedIds((prev) =>
      prev.includes(fileId) ? prev.filter((id) => id !== fileId) : [...prev, fileId]
    );
    setLastCheckedId(fileId);
  };

  const handleListKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
    const target = e.target as HTMLElement | null;
    if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
    if (!flatVisibleFiles.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const idx = flatVisibleFiles.findIndex((f) => f.id === selectedFileId);
      const nextIdx =
        e.key === "ArrowDown"
          ? Math.min(flatVisibleFiles.length - 1, idx + 1)
          : Math.max(0, idx <= 0 ? 0 : idx - 1);
      onSelectFile(flatVisibleFiles[nextIdx].id);
    } else if (e.key === "Enter" && selectedFileId) {
      onOpenQuickLook();
    }
  };

  const focusFile =
    files.find((f) => f.id === (hoveredFileId || selectedFileId)) || files[0];
  const neighbors = focusFile
    ? files.filter(
        (f) => f.id !== focusFile.id && f.shelfId === focusFile.shelfId
      )
    : [];

  const handleMouseDownMap = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest("button")) return;
    setIsPanning(true);
    setPanStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
  };

  const handleMouseMoveMap = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!isPanning) return;
    setPan({ x: e.clientX - panStart.x, y: e.clientY - panStart.y });
  };

  const handleMouseUpMap = () => {
    setIsPanning(false);
  };

  const resetMapTransform = () => {
    setZoom(1.0);
    setPan({ x: 0, y: 0 });
  };

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-shelves"
      tabIndex={0}
      onKeyDown={handleListKeyDown}
    >
      <div style={{ maxWidth: "820px", margin: "0 auto" }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "flex-end",
            justifyContent: "space-between",
            gap: "16px",
            marginBottom: "24px",
            paddingBottom: "20px",
            borderBottom: "1.5px solid var(--ink)",
          }}
        >
          <div>
            <div
              style={{
                fontSize: "11.5px",
                fontWeight: 500,
                letterSpacing: "0.06em",
                color: "var(--ink-3)",
                textTransform: "lowercase",
                marginBottom: "4px",
              }}
            >
              collections
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
              shelves
            </h1>
            <p
              style={{
                fontSize: "15px",
                color: "var(--ink-2)",
                marginTop: "8px",
                maxWidth: "56ch",
              }}
            >
              files from your{" "}
              <strong style={{ fontWeight: 500, color: "var(--ink)" }}>
                Downloads
              </strong>
              ,{" "}
              <strong style={{ fontWeight: 500, color: "var(--ink)" }}>
                Desktop
              </strong>
              , and{" "}
              <strong style={{ fontWeight: 500, color: "var(--ink)" }}>
                Documents
              </strong>{" "}
              grouped by topic without moving them on disk.
            </p>
          </div>

          <div className="seg-group" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={submode === "list"}
              className={`seg-btn ${submode === "list" ? "active" : ""}`}
              onClick={() => onSetSubmode("list")}
            >
              shelf list
            </button>
              <button
                type="button"
                role="tab"
                aria-selected={submode === "map"}
                className={`seg-btn ${submode === "map" ? "active" : ""}`}
                onClick={() => onSetSubmode("map")}
              >
                cluster map
              </button>
          </div>
        </div>

        {/* Submode A: Structured Shelf Cards */}
        {submode === "list" && (
          <div id="shelvesListWrap">
            {visibleShelves.map((shelf) => {
              const items = filesByShelf.get(shelf.id) || [];
              return (
                <div className="shelf-section" key={shelf.id}>
                  <div className="shelf-section-header">
                    <div>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "baseline",
                          gap: "10px",
                        }}
                      >
                        <span
                          className="font-mono-code"
                          style={{ fontSize: "12px", color: "var(--ink-3)" }}
                        >
                          {shelf.num}
                        </span>
                        <h2
                          className="font-display"
                          style={{
                            fontSize: "22px",
                            fontWeight: 400,
                            letterSpacing: "-0.02em",
                          }}
                        >
                          {shelf.name}
                        </h2>
                      </div>
                      <p
                        style={{
                          fontSize: "13.5px",
                          color: "var(--ink-2)",
                          marginTop: "2px",
                        }}
                      >
                        {shelf.desc}
                      </p>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                      {onTogglePinShelf && (
                        <button
                          type="button"
                          className="btn-tactile"
                          style={{ padding: "2px 8px", fontSize: "11px" }}
                          onClick={() => onTogglePinShelf(shelf.id)}
                          title={shelf.pinned ? "Unpin shelf" : "Pin shelf to top"}
                        >
                          {shelf.pinned ? "★ pinned" : "☆ pin"}
                        </button>
                      )}
                      <span
                        className="font-mono-code"
                        style={{ fontSize: "12px", color: "var(--ink-3)" }}
                      >
                        {items.length} {items.length === 1 ? "file" : "files"}
                      </span>
                    </div>
                  </div>

                  <div>
                    {items.length === 0 ? (
                      <div
                        style={{
                          padding: "20px",
                          fontSize: "13.5px",
                          color: "var(--ink-3)",
                        }}
                      >
                        No files currently placed in this shelf.
                      </div>
                    ) : (
                      items.map((f) => {
                        const isSel = f.id === selectedFileId;
                        const isChecked = checkedIds.includes(f.id);
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
                                }}
                              >
                                <input
                                  type="checkbox"
                                  checked={isChecked}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    toggleChecked(f.id, e.shiftKey);
                                  }}
                                  onChange={() => {}}
                                  title="Select for bulk action (Shift+Click for range)"
                                  style={{ cursor: "pointer", flexShrink: 0 }}
                                />
                                <span className="file-type-tag">{f.format}</span>
                                {f.pinned && (
                                  <span style={{ fontSize: "11px", color: "var(--ink)" }}>★</span>
                                )}
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
                                {f.suggestedName && !f.renamed && (
                                  <span
                                    style={{
                                      fontSize: "11px",
                                      padding: "1px 6px",
                                      border: "1px solid var(--rule-strong)",
                                      borderRadius: "4px",
                                      color: "var(--ink-2)",
                                      flexShrink: 0,
                                    }}
                                  >
                                    rename ready
                                  </span>
                                )}
                              </div>
                              <div
                                style={{
                                  fontSize: "13px",
                                  color: "var(--ink-2)",
                                  marginTop: "3px",
                                  whiteSpace: "nowrap",
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                }}
                              >
                                {f.extractedText}
                              </div>
                              <div
                                className="font-mono-code"
                                style={{
                                  fontSize: "11.5px",
                                  color: "var(--ink-3)",
                                  marginTop: "4px",
                                }}
                              >
                                {f.folder}/{f.name} · {f.size} · {f.date}
                              </div>
                            </div>

                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: "10px",
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
                              <span className="row-action-cue">inspect →</span>
                            </div>
                          </div>
                        );
                      })
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Submode B: Interactive 2D Meaning Map */}
        {submode === "map" && (
          <div id="shelvesMapWrap">
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "12px",
                marginBottom: "14px",
              }}
            >
              <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                <button
                  type="button"
                  className={`btn-tactile ${
                    mapTopicFilter === "all" ? "active-tab" : ""
                  }`}
                  onClick={() => setMapTopicFilter("all")}
                >
                  all shelves
                </button>
                {shelves
                  .filter((s) => s.id !== "tray")
                  .map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      className={`btn-tactile ${
                        mapTopicFilter === s.id ? "active-tab" : ""
                      }`}
                      onClick={() => setMapTopicFilter(s.id)}
                    >
                      {s.num} {s.name.split(" ")[0]}
                    </button>
                  ))}
              </div>

              <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "4px 9px", fontSize: "11.5px" }}
                  onClick={() => setZoom((z) => Math.min(2.2, +(z + 0.2).toFixed(1)))}
                >
                  zoom +
                </button>
                <button
                  type="button"
                  className="btn-tactile"
                  style={{ padding: "4px 9px", fontSize: "11.5px" }}
                  onClick={() => setZoom((z) => Math.max(0.7, +(z - 0.2).toFixed(1)))}
                >
                  zoom −
                </button>
                {(zoom !== 1.0 || pan.x !== 0 || pan.y !== 0) && (
                  <button
                    type="button"
                    className="btn-tactile"
                    style={{ padding: "4px 9px", fontSize: "11.5px" }}
                    onClick={resetMapTransform}
                  >
                    reset
                  </button>
                )}
              </div>
            </div>

            <div
              className="meaning-map-box"
              id="meaningMapCanvas"
              onMouseDown={handleMouseDownMap}
              onMouseMove={handleMouseMoveMap}
              onMouseUp={handleMouseUpMap}
              onMouseLeave={handleMouseUpMap}
              style={{ cursor: isPanning ? "grabbing" : "grab" }}
            >
              <div
                style={{
                  position: "absolute",
                  inset: 0,
                  transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                  transformOrigin: "center center",
                  transition: isPanning ? "none" : "transform 0.22s var(--ease)",
                }}
              >
                <svg
                  width="100%"
                  height="100%"
                  style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
                >
                  <line
                    x1="50%"
                    y1="0"
                    x2="50%"
                    y2="100%"
                    stroke="var(--rule-strong)"
                    strokeWidth="1"
                    strokeDasharray="2 5"
                  />
                  <line
                    x1="0"
                    y1="50%"
                    x2="100%"
                    y2="50%"
                    stroke="var(--rule-strong)"
                    strokeWidth="1"
                    strokeDasharray="2 5"
                  />

                  {/* Topic Island Hulls */}
                  {shelves
                    .filter((s) => s.id !== "tray")
                    .map((s) => (
                      <circle
                        key={`hull-${s.id}`}
                        cx={`${s.centroidX}%`}
                        cy={`${s.centroidY}%`}
                        r="86"
                        fill="none"
                        stroke="var(--rule-strong)"
                        strokeWidth="1"
                        strokeDasharray="4 4"
                      />
                    ))}

                  {/* Nearest-Neighbor Semantic Filaments */}
                  {focusFile &&
                    neighbors.map((n) => (
                      <line
                        key={`edge-${focusFile.id}-${n.id}`}
                        x1={`${focusFile.mapX}%`}
                        y1={`${focusFile.mapY}%`}
                        x2={`${n.mapX}%`}
                        y2={`${n.mapY}%`}
                        stroke="var(--ink)"
                        strokeWidth="1.2"
                        strokeDasharray="3 4"
                        opacity="0.65"
                      />
                    ))}
                </svg>

                {/* Topic Island Labels */}
                {shelves
                  .filter((s) => s.id !== "tray")
                  .map((s) => (
                    <span
                      key={`lbl-${s.id}`}
                      style={{
                        position: "absolute",
                        left: `${s.centroidX}%`,
                        top: `${Math.max(6, s.centroidY - 19)}%`,
                        transform: "translateX(-50%)",
                        fontSize: "10.5px",
                        color: "var(--ink-3)",
                        fontFamily: "var(--mono)",
                        textTransform: "uppercase",
                        pointerEvents: "none",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {s.num} {s.name}
                    </span>
                  ))}

                {/* File Nodes */}
                {files.map((f) => {
                  const isSel = f.id === selectedFileId;
                  const dimmed =
                    mapTopicFilter !== "all" && f.shelfId !== mapTopicFilter;
                  const shortLabel = f.title.split(" ").slice(0, 3).join(" ");
                  return (
                    <button
                      key={f.id}
                      type="button"
                      className={`map-point-btn ${isSel ? "selected" : ""}`}
                      style={{
                        left: `${f.mapX}%`,
                        top: `${f.mapY}%`,
                        opacity: dimmed ? 0.24 : 1,
                      }}
                      onMouseEnter={() => setHoveredFileId(f.id)}
                      onMouseLeave={() => setHoveredFileId(null)}
                      onClick={() => onSelectFile(f.id)}
                      onDoubleClick={onOpenQuickLook}
                    >
                      <span
                        style={{
                          width: "6px",
                          height: "6px",
                          borderRadius: "50%",
                          background: "currentColor",
                        }}
                      ></span>
                      <span>{shortLabel}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            <div
              style={{
                marginTop: "12px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                fontSize: "12.5px",
                color: "var(--ink-2)",
              }}
            >
              <span id="mapFooterStatus">
                {focusFile
                  ? `focused: ${focusFile.title} (${focusFile.name}) — connected to ${neighbors.length} shelf neighbors.`
                  : `showing ${files.length} files projected by semantic similarity.`}
              </span>
              <span
                className="font-mono-code"
                style={{ fontSize: "11px", color: "var(--ink-3)" }}
              >
                umap + hdbscan (silhouette: {silhouetteScore.toFixed(2)})
              </span>
            </div>
          </div>
        )}
      </div>

      {checkedIds.length > 0 && onBulkAction && (
        <div
          style={{
            position: "sticky",
            bottom: "16px",
            margin: "16px auto 0",
            maxWidth: "720px",
            padding: "10px 14px",
            background: "var(--bg-canvas)",
            border: "1px solid var(--ink)",
            borderRadius: "6px",
            boxShadow: "0 10px 28px rgba(0,0,0,0.14)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "10px",
            zIndex: 30,
          }}
        >
          <span style={{ fontSize: "12.5px", fontWeight: 500 }}>
            {checkedIds.length} selected
          </span>
          <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "11.5px", padding: "4px 10px" }}
              onClick={() => {
                onBulkAction(checkedIds, "move");
                setCheckedIds([]);
              }}
            >
              move to…
            </button>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "11.5px", padding: "4px 10px" }}
              onClick={() => {
                onBulkAction(checkedIds, "tag");
                setCheckedIds([]);
              }}
            >
              add tag…
            </button>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "11.5px", padding: "4px 10px" }}
              onClick={() => {
                onBulkAction(checkedIds, "pin");
                setCheckedIds([]);
              }}
            >
              pin
            </button>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "11.5px", padding: "4px 10px", color: "#9a3412" }}
              onClick={() => {
                onBulkAction(checkedIds, "trash");
                setCheckedIds([]);
              }}
            >
              trash
            </button>
            <button
              type="button"
              className="btn-tactile"
              style={{ fontSize: "11.5px", padding: "4px 10px" }}
              onClick={() => setCheckedIds([])}
            >
              clear
            </button>
          </div>
        </div>
      )}
    </section>
  );
};
