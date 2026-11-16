import React, { useEffect, useState } from "react";
import { ConnectedDevice, WatchedFolder, pickFolderNative } from "../lib/tauri-api";

interface OnboardingModalProps {
  open: boolean;
  watchedFolders: WatchedFolder[];
  connectedDevices: ConnectedDevice[];
  onComplete: (folders: string[]) => Promise<void>;
}

export const OnboardingModal: React.FC<OnboardingModalProps> = ({
  open,
  watchedFolders,
  connectedDevices,
  onComplete,
}) => {
  const [selected, setSelected] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    for (const wf of watchedFolders) {
      init[wf.path] = true;
    }
    return init;
  });
  const [customPath, setCustomPath] = useState("");
  const [customFolders, setCustomFolders] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);

  // Sync default watched folders once initial workspace state arrives asynchronously
  useEffect(() => {
    if (watchedFolders.length === 0) return;
    setSelected((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const wf of watchedFolders) {
        if (!(wf.path in next)) {
          next[wf.path] = true;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [watchedFolders]);

  if (!open) return null;

  const allCandidates: Array<{ path: string; label: string; hint: string }> = [
    ...watchedFolders.map((w) => ({
      path: w.path,
      label: w.display_path,
      hint: "default folder",
    })),
    ...connectedDevices
      .filter((d) => !watchedFolders.some((w) => w.path === d.path))
      .map((d) => ({
        path: d.path,
        label: d.name,
        hint: `${d.kind}${d.sizeLabel ? ` · ${d.sizeLabel}` : ""}`,
      })),
    ...customFolders.map((p) => ({
      path: p,
      label: p,
      hint: "custom folder",
    })),
  ];

  const isChecked = (path: string) => selected[path] ?? false;

  const togglePath = (path: string) => {
    setSelected((prev) => ({ ...prev, [path]: !(prev[path] ?? false) }));
  };

  const handleAddCustom = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = customPath.trim();
    if (!trimmed) return;
    if (!customFolders.includes(trimmed)) {
      setCustomFolders((prev) => [...prev, trimmed]);
    }
    setSelected((prev) => ({ ...prev, [trimmed]: true }));
    setCustomPath("");
  };

  const handleStart = async () => {
    const chosen = allCandidates
      .map((c) => c.path)
      .filter((p) => selected[p]);
    setSubmitting(true);
    try {
      await onComplete(chosen);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-wrap open" role="dialog" aria-modal="true" aria-label="Choose folders to watch">
      <div className="modal-card" style={{ maxWidth: "560px" }}>
        <div
          style={{
            fontSize: "11.5px",
            fontWeight: 500,
            letterSpacing: "0.06em",
            color: "var(--ink-3)",
            marginBottom: "4px",
          }}
        >
          first-run setup
        </div>
        <h2
          className="font-display"
          style={{ fontSize: "26px", fontWeight: 400, letterSpacing: "-0.02em" }}
        >
          choose folders to watch
        </h2>
        <p style={{ fontSize: "14px", color: "var(--ink-2)", marginTop: "6px" }}>
          Pick which folders or connected devices to index. Everything stays
          offline on your machine, and you can change this anytime in settings.
        </p>

        <div
          style={{
            marginTop: "18px",
            border: "1px solid var(--rule)",
            borderRadius: "7px",
            overflow: "hidden",
          }}
        >
          {allCandidates.map((item, idx) => {
            const checked = isChecked(item.path);
            return (
              <label
                key={item.path}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  padding: "11px 14px",
                  background: checked ? "var(--bg-subtle)" : "var(--bg-canvas)",
                  borderTop: idx > 0 ? "1px solid var(--rule)" : "none",
                  cursor: "pointer",
                  gap: "12px",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "10px", minWidth: 0 }}>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => togglePath(item.path)}
                    style={{ width: "15px", height: "15px", accentColor: "var(--ink)" }}
                  />
                  <span
                    className="font-mono-code"
                    style={{
                      fontSize: "13px",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {item.label}
                  </span>
                </div>
                <span style={{ fontSize: "11.5px", color: "var(--ink-3)", flexShrink: 0 }}>
                  {item.hint}
                </span>
              </label>
            );
          })}
        </div>

        <form
          onSubmit={handleAddCustom}
          style={{ display: "flex", gap: "8px", marginTop: "12px" }}
        >
          <input
            type="text"
            value={customPath}
            onChange={(e) => setCustomPath(e.target.value)}
            placeholder="Add another folder (e.g. ~/Pictures)…"
            className="font-mono-code"
            style={{
              flex: 1,
              padding: "7px 11px",
              border: "1px solid var(--rule-strong)",
              borderRadius: "6px",
              fontSize: "12.5px",
              background: "var(--bg-canvas)",
            }}
          />
          <button
            type="button"
            className="btn-tactile"
            onClick={async () => {
              const picked = await pickFolderNative();
              if (picked) {
                if (!customFolders.includes(picked)) {
                  setCustomFolders((prev) => [...prev, picked]);
                }
                setSelected((prev) => ({ ...prev, [picked]: true }));
              }
            }}
          >
            browse…
          </button>
          <button type="submit" className="btn-tactile">
            add
          </button>
        </form>

        <div
          style={{
            marginTop: "20px",
            display: "flex",
            justifyContent: "flex-end",
            gap: "10px",
          }}
        >
          <button
            type="button"
            className="btn-tactile solid"
            disabled={submitting}
            onClick={handleStart}
          >
            {submitting ? "indexing selected folders…" : "start indexing"}
          </button>
        </div>
      </div>
    </div>
  );
};
