import React, { useCallback, useEffect, useState } from "react";
import { ArrowLeft } from "lucide-react";
import {
  TrashArchiveResponse,
  emptyTrashPermanently,
  fetchTrashAndArchive,
  restoreTrashedFile,
  toggleResolveLookalikePair,
} from "../lib/tauri-api";
import { memoView } from "../lib/view-memo";

interface TrashArchiveViewProps {
  active: boolean;
  onBackToHome?: () => void;
  onRefreshState: (state: any) => void;
  onShowToast: (msg: string) => void;
}

export const TrashArchiveView = memoView<TrashArchiveViewProps>(({
  active,
  onBackToHome,
  onRefreshState,
  onShowToast,
}) => {
const [data, setData] = useState<TrashArchiveResponse>({ trashed: [], archived: [] });
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [confirmEmpty, setConfirmEmpty] = useState(false);
    const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchTrashAndArchive();
      setData(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load trash and archive");
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
      onShowToast(`Restored ${name} from your local archive folder`);
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
      onShowToast(`Permanently deleted ${res.removedCount} trashed ${res.removedCount === 1 ? "file" : "files"}`);
    } catch (err) {
      onShowToast(err instanceof Error ? err.message : "Could not empty trash");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section
      className={`view-sheet ${active ? "active" : ""}`}
      id="sheet-trash"
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
            <h1 className="page-title">Trash &amp; Archive</h1>
            <p className="page-subtitle">
              Recover trashed files or archived duplicates back to their original folders.
            </p>
          </div>
          {data.trashed.length > 0 &&
            (confirmEmpty ? (
              <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                <button
                  type="button"
                  className="btn-tactile danger"
                  disabled={busyId === "empty"}
                  onClick={handleEmptyTrash}
                >
                  <span>{busyId === "empty" ? "Emptying…" : `Confirm delete ${data.trashed.length}`}</span>
                </button>
                <button type="button" className="btn-tactile" onClick={() => setConfirmEmpty(false)}>
                  Cancel
                </button>
              </div>
            ) : (
              <button type="button" className="btn-tactile danger" onClick={() => setConfirmEmpty(true)}>
                <span>Empty trash ({data.trashed.length})</span>
              </button>
            ))}
        </div>

        {/* Trashed Files */}
        {error && (
          <div
            className="inline-error-banner"
            role="alert"
            style={{
              marginBottom: "18px",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "12px",
              flexWrap: "wrap",
            }}
          >
            <span>{error}</span>
            <button
              type="button"
              className="btn-tactile sm"
              onClick={() => void refresh()}
            >
              Retry
            </button>
          </div>
        )}
        <div className="shelf-section">
          <div className="shelf-section-header">
            <div>
              <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                Trash bin
              </h2>
              <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                Files removed inside Good Shelves (recoverable)
              </p>
            </div>
            <span className="shelf-ledger-count">{data.trashed.length} items</span>
          </div>
          <div className="shelf-list-surface">
            {loading && data.trashed.length === 0 ? (
              <div style={{ padding: "20px", display: "flex", flexDirection: "column", gap: "8px" }}>
                <div className="skeleton-line" style={{ width: "55%" }} />
                <div className="skeleton-line" style={{ width: "38%" }} />
              </div>
            ) : data.trashed.length === 0 ? (
              <div style={{ padding: "24px 20px", color: "var(--ink-3)", fontSize: "13.5px" }}>
                Trash is empty.
              </div>
            ) : (
              data.trashed.map((f) => (
                <div key={f.id} className="file-item-row" style={{ cursor: "default" }}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className="font-display" style={{ fontSize: "15px", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {f.name}
                    </div>
                    <div style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                      {f.format.toLowerCase()} · {f.size} · {f.date}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn-tactile sm"
                    disabled={busyId === f.id}
                    onClick={() => handleRestoreTrash(f.id, f.name)}
                  >
                    <span>{busyId === f.id ? "Restoring…" : "Restore"}</span>
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Archived Duplicate Copies */}
        <div className="shelf-section">
          <div className="shelf-section-header">
            <div>
              <h2 className="font-display" style={{ fontSize: "18px", fontWeight: 500 }}>
                Archived duplicates
              </h2>
              <p style={{ fontSize: "12.5px", color: "var(--ink-3)" }}>
                Redundant lookalikes moved safely to your local archive folder
              </p>
            </div>
            <span className="shelf-ledger-count">{data.archived.length} items</span>
          </div>
          <div className="shelf-list-surface">
            {data.archived.length === 0 ? (
              <div style={{ padding: "24px 20px", color: "var(--ink-3)", fontSize: "13.5px" }}>
                No archived duplicate copies.
              </div>
            ) : (
              data.archived.map((item) => (
                <div key={item.pairId} className="file-item-row" style={{ cursor: "default" }}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className="font-display" style={{ fontSize: "15px", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {item.name}
                    </div>
                    <div style={{ fontSize: "12.5px", color: "var(--ink-3)", marginTop: "2px" }}>
                      Archived duplicate
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn-tactile sm"
                    disabled={busyId === item.pairId}
                    onClick={() => handleRestoreArchive(item.pairId, item.name)}
                  >
                    <span>{busyId === item.pairId ? "Restoring…" : "Restore"}</span>
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </section>
  );
});
