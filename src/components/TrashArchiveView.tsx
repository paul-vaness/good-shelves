import React, { useCallback, useEffect, useState } from "react";
import { RotateCcw, Trash2 } from "lucide-react";
import {
  TrashArchiveResponse,
  emptyTrashPermanently,
  fetchTrashAndArchive,
  restoreTrashedFile,
  toggleResolveLookalikePair,
} from "../lib/tauri-api";

interface TrashArchiveViewProps {
  active: boolean;
  onRefreshState: (state: any) => void;
  onShowToast: (msg: string) => void;
}

export const TrashArchiveView: React.FC<TrashArchiveViewProps> = ({
  active,
  onRefreshState,
  onShowToast,
}) => {
  const [data, setData] = useState<TrashArchiveResponse>({ trashed: [], archived: [] });
  const [loading, setLoading] = useState(false);
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchTrashAndArchive();
      setData(res);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) {
      setConfirmEmpty(false);
      refresh();
    }
  }, [active, refresh]);

  const handleRestoreTrash = async (fileId: string, name: string) => {
    if (busyId) return;
    setBusyId(fileId);
    try {
      const res = await restoreTrashedFile(fileId);
      onRefreshState(res.state);
      await refresh();
      onShowToast(`Restored ${name}`);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not restore file");
    } finally {
      setBusyId(null);
    }
  };

  const handleRestoreArchive = async (pairId: string, name: string) => {
    if (busyId) return;
    setBusyId(pairId);
    try {
      const res = await toggleResolveLookalikePair(pairId);
      onRefreshState(res.state);
      await refresh();
      onShowToast(`Restored ${name} from GoodShelves_Archive`);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not restore archive item");
    } finally {
      setBusyId(null);
    }
  };

  const handleEmptyTrash = async () => {
    setBusyId("empty");
    try {
      const res = await emptyTrashPermanently();
      onRefreshState(res.state);
      setConfirmEmpty(false);
      await refresh();
      onShowToast(`Permanently deleted ${res.removedCount} trashed file(s)`);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not empty trash");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className={`view-sheet ${active ? "active" : ""}`} id="sheet-trash">
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
            <div style={{ fontSize: "11.5px", fontWeight: 500, letterSpacing: "0.06em", color: "var(--ink-3)" }}>
              recovery &amp; cleanup
            </div>
            <h1
              className="font-display"
              style={{ fontSize: "clamp(32px, 4.2vw, 48px)", fontWeight: 300, letterSpacing: "-0.03em", lineHeight: 1.02 }}
            >
              trash &amp; archive
            </h1>
            <p style={{ fontSize: "15px", color: "var(--ink-2)", marginTop: "8px", maxWidth: "56ch" }}>
              restore any file moved to GoodShelves_Trash or archived duplicate copies from GoodShelves_Archive.
            </p>
          </div>
          {data.trashed.length > 0 && (
            confirmEmpty ? (
              <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                <button
                  type="button"
                  className="btn-tactile danger"
                  disabled={busyId === "empty"}
                  onClick={handleEmptyTrash}
                >
                  <Trash2 size={13} />
                  <span>{busyId === "empty" ? "emptying…" : `confirm delete ${data.trashed.length}`}</span>
                </button>
                <button type="button" className="btn-tactile" onClick={() => setConfirmEmpty(false)}>
                  cancel
                </button>
              </div>
            ) : (
              <button type="button" className="btn-tactile danger" onClick={() => setConfirmEmpty(true)}>
                <Trash2 size={13} />
                <span>empty trash ({data.trashed.length})</span>
              </button>
            )
          )}
        </div>

        {/* Trashed Files */}
        <div className="shelf-section">
          <div className="shelf-section-header">
            <div>
              <h2 className="font-display" style={{ fontSize: "20px", fontWeight: 400 }}>
                trash bin
              </h2>
              <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                files removed inside good shelves (reversible)
              </p>
            </div>
            <span className="font-mono-code" style={{ fontSize: "12px", color: "var(--ink-3)" }}>
              {data.trashed.length} items
            </span>
          </div>
          {loading && data.trashed.length === 0 ? (
            <div style={{ padding: "20px", color: "var(--ink-3)", fontSize: "13px" }}>Loading…</div>
          ) : data.trashed.length === 0 ? (
            <div style={{ padding: "20px", color: "var(--ink-3)", fontSize: "13.5px" }}>Trash is empty.</div>
          ) : (
            data.trashed.map((f) => (
              <div key={f.id} className="file-item-row" style={{ cursor: "default" }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    <span className="file-type-tag">{f.format}</span>
                    <span className="font-mono-code" style={{ fontSize: "13px", fontWeight: 500, wordBreak: "break-all" }}>
                      {f.name}
                    </span>
                  </div>
                  <div style={{ fontSize: "12px", color: "var(--ink-3)", marginTop: "3px" }}>
                    {f.size} · {f.date}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn-tactile"
                  disabled={busyId === f.id}
                  onClick={() => handleRestoreTrash(f.id, f.name)}
                >
                  <RotateCcw size={12} />
                  <span>{busyId === f.id ? "restoring…" : "restore"}</span>
                </button>
              </div>
            ))
          )}
        </div>

        {/* Archived Duplicate Copies */}
        <div className="shelf-section">
          <div className="shelf-section-header">
            <div>
              <h2 className="font-display" style={{ fontSize: "20px", fontWeight: 400 }}>
                archived duplicates
              </h2>
              <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                redundant lookalikes moved to GoodShelves_Archive
              </p>
            </div>
            <span className="font-mono-code" style={{ fontSize: "12px", color: "var(--ink-3)" }}>
              {data.archived.length} items
            </span>
          </div>
          {data.archived.length === 0 ? (
            <div style={{ padding: "20px", color: "var(--ink-3)", fontSize: "13.5px" }}>
              No archived duplicate copies.
            </div>
          ) : (
            data.archived.map((item) => (
              <div key={item.pairId} className="file-item-row" style={{ cursor: "default" }}>
                <div style={{ minWidth: 0 }}>
                  <div className="font-mono-code" style={{ fontSize: "13px", fontWeight: 500, wordBreak: "break-all" }}>
                    {item.name}
                  </div>
                  <div className="font-mono-code" style={{ fontSize: "11.5px", color: "var(--ink-3)", marginTop: "3px", wordBreak: "break-all" }}>
                    original: {item.originalPath}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn-tactile"
                  disabled={busyId === item.pairId}
                  onClick={() => handleRestoreArchive(item.pairId, item.name)}
                >
                  <RotateCcw size={12} />
                  <span>{busyId === item.pairId ? "restoring…" : "restore"}</span>
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    </section>
  );
};
