"""
Connected Device & Phone Detection for Good Shelves (Linux, Windows, macOS).
Detects USB drives, SD cards, external disks, and MTP/gphoto2 phone mounts.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_DEVICES_CACHE_LOCK = threading.Lock()
_DEVICES_RAW_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_DEVICES_CACHE_TTL_SEC = 3.0


def _stable_dev_id(prefix: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{prefix}_{digest}"


def _guess_device_kind(name: str, path_str: str, tran: str = "") -> str:
    low = f"{name} {path_str} {tran}".lower()
    if any(k in low for k in ("mtp", "gphoto", "android", "iphone", "pixel", "samsung", "xiaomi", "oppo", "vivo", "phone")):
        return "phone"
    if any(k in low for k in ("sd", "mmc", "card")):
        return "sd"
    if "usb" in low:
        return "usb"
    return "external"


def _format_bytes_short(num_bytes: int) -> str:
    if num_bytes <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.1f} {unit}" if unit in ("GB", "TB") else f"{int(num_bytes)} {unit}"
        num_bytes /= 1024.0
    return ""


def _mount_disk_label(mount_path: Path) -> str:
    try:
        if hasattr(os, "statvfs"):
            st = os.statvfs(str(mount_path))
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
        else:
            usage = shutil.disk_usage(str(mount_path))
            total = usage.total
            free = usage.free
        if total > 0:
            return f"{_format_bytes_short(free)} free / {_format_bytes_short(total)}"
    except OSError:
        pass
    return ""


def _detect_linux_devices() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    user = os.environ.get("USER", "")
    uid = os.getuid() if hasattr(os, "getuid") else 1000

    # 1. GVFS phone mounts (MTP / gphoto2)
    gvfs_dir = Path(f"/run/user/{uid}/gvfs")
    if gvfs_dir.exists():
        try:
            for entry in sorted(gvfs_dir.iterdir()):
                if entry.is_dir():
                    resolved = str(entry.resolve())
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)
                    label = entry.name.split("host=")[-1].replace("%20", " ").replace("_", " ")
                    devices.append(
                        {
                            "id": _stable_dev_id("dev", resolved),
                            "name": label or entry.name,
                            "path": resolved,
                            "kind": _guess_device_kind(entry.name, resolved),
                            "sizeLabel": _mount_disk_label(entry) or "phone storage",
                        }
                    )
        except OSError:
            pass

    # 2. Removable media mounts (/run/media/$USER, /media/$USER, /media, /mnt)
    mount_roots = [
        Path(f"/run/media/{user}") if user else None,
        Path(f"/media/{user}") if user else None,
        Path("/media"),
        Path("/mnt"),
    ]
    for root in mount_roots:
        if root is None or not root.exists():
            continue
        try:
            for entry in sorted(root.iterdir()):
                if entry.is_dir() and entry.name != user:
                    resolved = str(entry.resolve())
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)
                    devices.append(
                        {
                            "id": _stable_dev_id("dev", resolved),
                            "name": entry.name,
                            "path": resolved,
                            "kind": _guess_device_kind(entry.name, resolved),
                            "sizeLabel": _mount_disk_label(entry),
                        }
                    )
        except OSError:
            pass

    # 3. Enrich with lsblk transport info if available
    try:
        proc = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,LABEL,MOUNTPOINT,TRAN,SIZE,RM"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            stack = list(data.get("blockdevices", []))
            while stack:
                dev = stack.pop(0)
                stack.extend(dev.get("children") or [])
                mp = dev.get("mountpoint")
                if not mp or mp in ("/", "/home", "/boot", "/boot/efi", "[SWAP]"):
                    continue
                is_removable = bool(dev.get("rm")) or str(dev.get("tran", "")).lower() == "usb"
                if not is_removable:
                    continue
                resolved = str(Path(mp).resolve())
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                label = dev.get("label") or Path(mp).name or dev.get("name") or "Removable Drive"
                devices.append(
                    {
                        "id": _stable_dev_id("dev", resolved),
                        "name": str(label),
                        "path": resolved,
                        "kind": _guess_device_kind(str(label), resolved, str(dev.get("tran", ""))),
                        "sizeLabel": str(dev.get("size") or _mount_disk_label(Path(resolved))),
                    }
                )
    except Exception:
        pass

    return devices


def _detect_windows_devices() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        bitmask = kernel32.GetLogicalDrives()
        for idx in range(26):
            if not (bitmask & (1 << idx)):
                continue
            letter = chr(65 + idx)
            root = f"{letter}:\\"
            drive_type = kernel32.GetDriveTypeW(root)
            # 2 = DRIVE_REMOVABLE
            if drive_type == 2:
                vol_buf = ctypes.create_unicode_buffer(256)
                kernel32.GetVolumeInformationW(root, vol_buf, 256, None, None, None, None, 0)
                label = vol_buf.value.strip() or f"Removable ({letter}:)"
                devices.append(
                    {
                        "id": f"dev_{letter}",
                        "name": label,
                        "path": root,
                        "kind": _guess_device_kind(label, root),
                        "sizeLabel": _mount_disk_label(Path(root)),
                    }
                )
    except Exception:
        pass

    # Windows Portable Devices (MTP phones without drive letters) via Shell.Application
    try:
        ps_cmd = (
            "$s = New-Object -ComObject Shell.Application; "
            "$pc = $s.Namespace(17); "
            "$pc.Items() | Where-Object { $_.Path -match '^::\\{' -and $_.Type -match 'Phone|Portable|MTP|Device' } | "
            "ForEach-Object { [PSCustomObject]@{ Name = $_.Name; Path = $_.Path; Type = $_.Type } } | "
            "ConvertTo-Json -Compress"
        )
        win_kwargs: dict[str, Any] = (
            {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
            if platform.system() == "Windows"
            else {}
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=3,
            **win_kwargs,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw = json.loads(proc.stdout)
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                name = str(item.get("Name") or "Phone").strip()
                path = str(item.get("Path") or "").strip()
                if not path:
                    continue
                devices.append(
                    {
                        "id": _stable_dev_id("dev_wpd", path),
                        "name": name,
                        "path": path,
                        "kind": "phone",
                        "sizeLabel": "MTP phone (Windows Portable Device)",
                        "mtpVirtual": True,
                    }
                )
    except Exception:
        pass
    return devices


def _detect_macos_devices() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    vols = Path("/Volumes")
    if vols.exists():
        try:
            for entry in sorted(vols.iterdir()):
                if entry.is_dir() and not entry.is_symlink():
                    resolved = str(entry.resolve())
                    devices.append(
                        {
                            "id": _stable_dev_id("dev", resolved),
                            "name": entry.name,
                            "path": resolved,
                            "kind": _guess_device_kind(entry.name, resolved),
                            "sizeLabel": _mount_disk_label(entry),
                        }
                    )
        except OSError:
            pass
    return devices


def list_connected_devices(watched_paths: set[str] | None = None) -> list[dict[str, Any]]:
    """Return all currently mounted phones, USB drives, and SD cards."""
    global _DEVICES_RAW_CACHE
    now = time.monotonic()
    with _DEVICES_CACHE_LOCK:
        cached = _DEVICES_RAW_CACHE
        if cached is not None and (now - cached[0]) < _DEVICES_CACHE_TTL_SEC:
            raw_devs = cached[1]
        else:
            sys_name = platform.system()
            if sys_name == "Windows":
                raw_devs = _detect_windows_devices()
            elif sys_name == "Darwin":
                raw_devs = _detect_macos_devices()
            else:
                raw_devs = _detect_linux_devices()
            _DEVICES_RAW_CACHE = (time.monotonic(), raw_devs)

    watched = watched_paths or set()
    devs: list[dict[str, Any]] = []
    for d in raw_devs:
        entry = dict(d)
        entry["alreadyWatched"] = entry["path"] in watched
        devs.append(entry)
    return devs


def devices_signature(devices: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    """Stable hashable signature for detecting hot-plug connect/disconnect events."""
    return tuple(sorted((str(d.get("id", "")), str(d.get("path", ""))) for d in devices))
