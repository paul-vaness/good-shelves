export function isMacOS(): boolean {
  if (typeof navigator === "undefined") return false;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const plat = (nav.userAgentData?.platform || navigator.platform || navigator.userAgent || "").toLowerCase();
  return plat.includes("mac");
}

export function isWindowsOS(): boolean {
  if (typeof navigator === "undefined") return false;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const plat = (nav.userAgentData?.platform || navigator.platform || navigator.userAgent || "").toLowerCase();
  return plat.includes("win");
}

export function formatShortcut(key: string, opts?: { shift?: boolean; mod?: boolean }): string {
  const mac = isMacOS();
  const useMod = opts?.mod !== false;
  const useShift = Boolean(opts?.shift);
  if (mac) {
    return `${useShift ? "⇧" : ""}${useMod ? "⌘" : ""}${key}`;
  }
  const parts: string[] = [];
  if (useMod) parts.push("Ctrl");
  if (useShift) parts.push("Shift");
  parts.push(key);
  return parts.join("+");
}

export function isWindowsPath(path: string): boolean {
  return path.includes("\\") || /^[A-Za-z]:/.test(path);
}

export function joinDisplayPath(folder: string, name: string): string {
  if (!folder) return name;
  if (!name) return folder;
  const sep = isWindowsPath(folder) ? "\\" : "/";
  const trimmed = folder.replace(/[\\/]+$/, "");
  return `${trimmed}${sep}${name}`;
}

export function pathBaseName(path: string): string {
  if (!path) return "";
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

export function defaultExamplePath(folderName: string): string {
  if (isWindowsOS()) {
    return `C:\\Users\\You\\${folderName}`;
  }
  return `~/${folderName}`;
}

export function sanitizeFolderName(raw: string): string {
  return raw
    .replace(/[<>:"/\\|?*\x00-\x1f]+/g, "_")
    .trim()
    .replace(/^[. ]+|[. ]+$/g, "");
}
