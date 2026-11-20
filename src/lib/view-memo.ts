import React from "react";

/**
 * Skips re-rendering hidden view sheets while inactive so clicking files,
 * typing search queries, or toggling the inspector only updates the visible view.
 */
export function memoView<P extends { active: boolean }>(
  Component: React.FC<P>
): React.NamedExoticComponent<P> {
  return React.memo(Component, (prev, next) => !prev.active && !next.active);
}
