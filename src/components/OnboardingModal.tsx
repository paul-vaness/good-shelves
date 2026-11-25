import React, { useEffect, useState } from "react";
import { ConnectedDevice, WatchedFolder, pickFolderNative } from "../lib/tauri-api";
import { defaultExamplePath } from "../lib/platform";
import { useFocusTrap } from "../lib/use-focus-trap";

export interface OnboardingOptions {
  userName: string;
  focusAreas: string[];
  adaptiveLearning: boolean;
  termsAccepted: boolean;
}

interface OnboardingModalProps {
  open: boolean;
  watchedFolders: WatchedFolder[];
  connectedDevices: ConnectedDevice[];
  initialUserName?: string;
  onComplete: (folders: string[], options?: OnboardingOptions) => Promise<void>;
}

const FOCUS_AREA_CHOICES = [
  { id: "school", label: "School & Research", hint: "Notes, PDFs, reviewers, slides" },
  { id: "finance", label: "Receipts & Finance", hint: "Bills, invoices, transfers" },
  { id: "photos", label: "Photos & Design", hint: "Images, screenshots, diagrams" },
  { id: "code", label: "Code & Projects", hint: "Scripts, configs, docs" },
  { id: "work", label: "Work & Admin", hint: "Resumes, contracts, reports" },
  { id: "personal", label: "Personal Archive", hint: "Forms, records, saved files" },
];

export const OnboardingModal: React.FC<OnboardingModalProps> = ({
  open,
  watchedFolders,
  connectedDevices,
  initialUserName = "",
  onComplete,
}) => {
  const [step, setStep] = useState<1 | 2>(1);
  const [userName, setUserName] = useState<string>(initialUserName);
  const [focusAreas, setFocusAreas] = useState<string[]>(["school", "finance"]);
  const [adaptiveLearning, setAdaptiveLearning] = useState<boolean>(true);
  const [agreedTerms, setAgreedTerms] = useState<boolean>(false);

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
  const [error, setError] = useState<string | null>(null);
  const modalCardRef = useFocusTrap<HTMLDivElement>(open);

  useEffect(() => {
    if (open) {
      setStep(1);
      setError(null);
      if (initialUserName) setUserName(initialUserName);
    }
  }, [open, initialUserName]);

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
      hint: "Default folder",
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
      hint: "Added folder",
    })),
  ];

  const isChecked = (path: string) => selected[path] ?? false;

  const togglePath = (path: string) => {
    setSelected((prev) => ({ ...prev, [path]: !(prev[path] ?? false) }));
  };

  const toggleFocus = (id: string) => {
    setFocusAreas((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const commitTypedCustomFolder = () => {
    const trimmed = customPath.trim();
    if (!trimmed) return null;
    if (!customFolders.includes(trimmed)) {
      setCustomFolders((prev) => [...prev, trimmed]);
    }
    setSelected((prev) => ({ ...prev, [trimmed]: true }));
    setCustomPath("");
    return trimmed;
  };

  const finishSetup = async (opts?: { skip?: boolean; termsAccepted?: boolean }) => {
    const skipDefaults = opts?.skip ?? false;
    const termsAccepted = opts?.termsAccepted ?? agreedTerms;
    if (!termsAccepted && !skipDefaults) {
      setError("Please confirm the local privacy terms to continue.");
      return;
    }
    const extra = commitTypedCustomFolder();
    const chosen = Array.from(
      new Set([
        ...allCandidates.map((c) => c.path).filter((p) => selected[p]),
        ...(extra ? [extra] : []),
      ])
    );
    setSubmitting(true);
    setError(null);
    try {
      await onComplete(chosen, {
        userName: skipDefaults ? "" : userName.trim(),
        focusAreas,
        adaptiveLearning,
        termsAccepted,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not complete initial setup.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-wrap open" id="onboardingModal" role="dialog" aria-modal="true" aria-label="Welcome to Good Shelves">
      <div ref={modalCardRef} tabIndex={-1} className="modal-card modal-md">
        <div className="modal-header" style={{ alignItems: "center" }}>
          <div>
            <div style={{ fontSize: "11.5px", fontWeight: 500, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--ink-3)" }}>
              Step {step} of 2
            </div>
            <h2 className="modal-title" style={{ marginTop: "4px" }}>
              {step === 1 ? "Choose folders to organize" : "Personalize your workspace"}
            </h2>
          </div>
          <button
            type="button"
            className="btn-ghost"
            style={{ minHeight: "30px" }}
            disabled={submitting}
            onClick={() => finishSetup({ skip: true })}
          >
            Skip for now
          </button>
        </div>

        {step === 1 && (
          <>
            <p style={{ fontSize: "13.5px", color: "var(--ink-2)", marginTop: "14px" }}>
              Pick which folders Good Shelves should index on this device. You can change folders anytime in Settings.
            </p>

            <div
              className="shelf-list-surface"
              style={{
                marginTop: "14px",
                maxHeight: "230px",
                overflowY: "auto",
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
                      background: checked ? "var(--bg-hover)" : "var(--bg-surface)",
                      borderTop: idx > 0 ? "1px solid var(--rule-soft)" : "none",
                      cursor: "pointer",
                      gap: "12px",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "10px", minWidth: 0 }}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => togglePath(item.path)}
                        style={{ width: "16px", height: "16px", accentColor: "var(--ink)" }}
                      />
                      <span
                        style={{ fontSize: "13.5px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      >
                        {item.label}
                      </span>
                    </div>
                    <span style={{ fontSize: "11.5px", color: "var(--ink-3)", flexShrink: 0 }}>{item.hint}</span>
                  </label>
                );
              })}
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                commitTypedCustomFolder();
              }}
              style={{ display: "flex", gap: "8px", marginTop: "12px" }}
            >
              <input
                type="text"
                aria-label="Custom folder path"
                value={customPath}
                onChange={(e) => setCustomPath(e.target.value)}
                placeholder={`Add another folder (e.g. ${defaultExamplePath("Pictures")})…`}
                className="dialog-input"
                style={{ flex: 1, fontSize: "13px", padding: "7px 11px" }}
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
                <span>Browse…</span>
              </button>
              {customPath.trim() && (
                <button type="submit" className="btn-tactile">
                  Add
                </button>
              )}
            </form>
          </>
        )}

        {step === 2 && (
          <>
            <p style={{ fontSize: "13.5px", color: "var(--ink-2)", marginTop: "14px" }}>
              Optional preferences to tailor your shelves and search suggestions.
            </p>

            <div style={{ marginTop: "16px" }}>
              <label
                htmlFor="onboardingUserName"
                style={{ display: "block", fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "6px" }}
              >
                Your name (optional)
              </label>
              <input
                id="onboardingUserName"
                type="text"
                value={userName}
                onChange={(e) => setUserName(e.target.value)}
                placeholder="e.g. Alex"
                className="dialog-input"
              />
            </div>

            <div style={{ marginTop: "16px" }}>
              <div style={{ fontSize: "12px", fontWeight: 500, color: "var(--ink-2)", marginBottom: "8px" }}>
                What do you organize most?
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(165px, 1fr))", gap: "8px" }}>
                {FOCUS_AREA_CHOICES.map((item) => {
                  const active = focusAreas.includes(item.id);
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className={`folder-choice-card ${active ? "selected" : ""}`}
                      onClick={() => toggleFocus(item.id)}
                      style={{ flexDirection: "column", alignItems: "flex-start", gap: "2px", padding: "9px 11px" }}
                    >
                      <div style={{ fontSize: "13px", fontWeight: 500, color: "var(--ink)" }}>
                        {active ? "✓ " : ""}{item.label}
                      </div>
                      <div style={{ fontSize: "11px", color: "var(--ink-3)" }}>{item.hint}</div>
                    </button>
                  );
                })}
              </div>
            </div>

            <label
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: "10px",
                marginTop: "14px",
                padding: "10px 12px",
                borderRadius: "var(--radius-sm)",
                border: "1px solid var(--rule)",
                background: "var(--bg-subtle)",
                cursor: "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={adaptiveLearning}
                onChange={(e) => setAdaptiveLearning(e.target.checked)}
                style={{ marginTop: "3px", width: "15px", height: "15px", accentColor: "var(--ink)" }}
              />
              <span style={{ fontSize: "12.5px", color: "var(--ink-2)", lineHeight: 1.45 }}>
                <strong style={{ color: "var(--ink)", fontWeight: 600 }}>Adapt over time:</strong>{" "}
                remember which files and shelves you pick to refine future suggestions.
              </span>
            </label>

            <label
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: "10px",
                marginTop: "10px",
                padding: "10px 12px",
                borderRadius: "var(--radius-sm)",
                border: "1px solid var(--rule)",
                background: "var(--bg-surface)",
                cursor: "pointer",
                fontSize: "12.5px",
                color: "var(--ink-2)",
              }}
            >
              <input
                id="onboardingTermsCheckbox"
                type="checkbox"
                checked={agreedTerms}
                onChange={(e) => {
                  setAgreedTerms(e.target.checked);
                  if (e.target.checked) setError(null);
                }}
                style={{ marginTop: "2px", width: "15px", height: "15px", accentColor: "var(--ink)" }}
              />
              <span>
                I understand indexing runs locally on this computer and file actions are reversible.
              </span>
            </label>
          </>
        )}

        {error && (
          <div className="inline-error-banner" role="alert" style={{ marginTop: "14px" }}>
            {error}
          </div>
        )}

        <div
          className="modal-footer"
          style={{
            justifyContent: "space-between",
          }}
        >
          <div>
            {step === 2 && (
              <button
                type="button"
                className="btn-tactile"
                disabled={submitting}
                onClick={() => {
                  setError(null);
                  setStep(1);
                }}
              >
                Back
              </button>
            )}
          </div>
          <div style={{ display: "flex", gap: "8px" }}>
            {step === 1 ? (
              <button
                type="button"
                className="btn-tactile solid"
                onClick={() => {
                  commitTypedCustomFolder();
                  setError(null);
                  setStep(2);
                }}
              >
                Continue
              </button>
            ) : (
              <button
                type="button"
                className="btn-tactile solid"
                disabled={submitting || !agreedTerms}
                onClick={() => finishSetup({ termsAccepted: agreedTerms })}
              >
                {submitting ? "Setting up your shelves…" : "Start using Good Shelves"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
