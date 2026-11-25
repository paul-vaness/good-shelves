import React, { useEffect, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { Copy, Maximize2, Minimize2, Minus, Square, X } from "lucide-react";
import { isTauriRuntime } from "../lib/tauri-api";

interface WindowControlsProps {
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
}

export const WindowControls: React.FC<WindowControlsProps> = ({
  isFullscreen,
  onToggleFullscreen,
}) => {
  const [isMaximized, setIsMaximized] = useState(false);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let active = true;
    const win = getCurrentWindow();

    const sync = async () => {
      try {
        const maxed = await win.isMaximized();
        if (active) setIsMaximized(maxed);
      } catch {
        // ignore in non-window contexts
      }
    };

    void sync();
    let unlisten: (() => void) | null = null;
    win
      .onResized(() => {
        void sync();
      })
      .then((fn) => {
        if (active) unlisten = fn;
        else fn();
      })
      .catch(() => {});

    return () => {
      active = false;
      unlisten?.();
    };
  }, []);

  if (!isTauriRuntime()) return null;

  const win = getCurrentWindow();

  return (
    <div
      className={`window-controls-bar ${isFullscreen ? "fullscreen" : ""}`}
      aria-label="Window controls"
    >
      <button
        type="button"
        className="win-ctrl-btn"
        onClick={onToggleFullscreen}
        title={isFullscreen ? "Exit fullscreen (F11)" : "Fullscreen (F11)"}
        aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
        aria-pressed={isFullscreen}
      >
        {isFullscreen ? (
          <Minimize2 size={13} strokeWidth={1.6} aria-hidden="true" />
        ) : (
          <Maximize2 size={13} strokeWidth={1.6} aria-hidden="true" />
        )}
      </button>

      {!isFullscreen && (
        <>
          <button
            type="button"
            className="win-ctrl-btn"
            onClick={() => void win.minimize()}
            title="Minimize"
            aria-label="Minimize window"
          >
            <Minus size={14} strokeWidth={1.6} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="win-ctrl-btn"
            onClick={() => void win.toggleMaximize()}
            title={isMaximized ? "Restore" : "Maximize"}
            aria-label={isMaximized ? "Restore window" : "Maximize window"}
          >
            {isMaximized ? (
              <Copy size={12} strokeWidth={1.6} aria-hidden="true" />
            ) : (
              <Square size={12} strokeWidth={1.6} aria-hidden="true" />
            )}
          </button>
        </>
      )}

      <button
        type="button"
        className="win-ctrl-btn close"
        onClick={() => void win.close()}
        title="Close"
        aria-label="Close window"
      >
        <X size={14} strokeWidth={1.6} aria-hidden="true" />
      </button>
    </div>
  );
};