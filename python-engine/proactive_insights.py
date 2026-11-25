"""
Proactive Insight Engine for Good Shelves.

Runs on idle and detects actionable patterns across the indexed workspace:
  - Near-duplicate clusters with reclaimable space
  - Receipts/invoices missing structured entities (merchant, date, amount)
  - Upcoming due dates parsed from OCR
  - Stale Downloads files (30+ days untouched)
  - Files with cryptic names ready for rename
  - Files in the Quiet Tray (uncategorized)

Emits insight_detected SSE events that the frontend renders as dismissible cards.
Each insight includes an actionPrompt that routes through the existing ai_agent.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _days_ago(mtime: float, now: float) -> int:
    return max(0, int((now - mtime) / 86400.0))


def _format_bytes_mb(size_bytes: int) -> str:
    mb = size_bytes / (1024.0 * 1024.0)
    if mb >= 1024:
        return f"{mb / 1024.0:.1f} GB"
    return f"{mb:.1f} MB"


def detect_insights(
    files: list[dict[str, Any]],
    lookalikes: list[dict[str, Any]],
    now: Optional[float] = None,
    max_insights: int = 6,
    active_vision_model: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Analyze the current workspace state and return a list of actionable insights.

    Each insight dict:
      id: str (stable key for dedup)
      type: str (duplicates | missing_ocr | due_soon | stale_downloads | cryptic_names | uncategorized)
      severity: str (info | warning | action)
      title: str (short headline)
      detail: str (one-line explanation)
      count: int (number of affected files)
      action_prompt: str (natural language command for ai_agent)
      action_label: str (button label)
      reclaim_mb: float (optional, for duplicates)
    """
    if now is None:
        now = time.time()

    insights: list[dict[str, Any]] = []

    unresolved_dups = [p for p in (lookalikes or []) if not p.get("resolved")]
    if unresolved_dups:
        reclaim_mb = sum(float(p.get("saved_mb", 0)) for p in unresolved_dups)
        top_dup = max(unresolved_dups, key=lambda p: float(p.get("saved_mb") or 0.0))
        top_dup_name = str(
            (top_dup.get("copy") or {}).get("name")
            or (top_dup.get("keep") or {}).get("name")
            or ""
        ).strip()
        dup_sample = f" (e.g. {top_dup_name})" if top_dup_name else ""
        insights.append({
            "id": "duplicates",
            "type": "duplicates",
            "severity": "warning",
            "title": f"{len(unresolved_dups)} duplicate pair(s) found",
            "detail": f"{_format_bytes_mb(int(reclaim_mb * 1024 * 1024))} reclaimable{dup_sample} · review and archive copies",
            "count": len(unresolved_dups),
            "action_prompt": "show me all the duplicates",
            "action_label": "Review duplicates",
            "reclaim_mb": round(reclaim_mb, 1),
        })

    ocr_eligible = [
        f for f in files
        if f.get("modality") == "image" or str(f.get("format", "")).upper() == "PDF"
    ]
    ocr_missing = [
        f for f in ocr_eligible
        if not str(f.get("ocrText") or f.get("ocr_text") or "").strip()
    ]
    if ocr_missing:
        insights.append({
            "id": "ocr_missing",
            "type": "missing_ocr",
            "severity": "info",
            "title": f"{len(ocr_missing)} file(s) missing OCR text",
            "detail": "Run OCR to make receipts, screenshots, and scanned PDFs searchable",
            "count": len(ocr_missing),
            "action_prompt": "run ocr on all images and PDFs",
            "action_label": "Run OCR",
        })

    due_soon: list[dict[str, Any]] = []
    today_date = datetime.fromtimestamp(now).date()
    mismatched_vision: list[dict[str, Any]] = []
    for f in files:
        meta = f.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if active_vision_model and f.get("modality") == "image":
            indexed_vm = str(meta.get("visionModel") or "").strip()
            if indexed_vm and indexed_vm != active_vision_model:
                mismatched_vision.append(f)
        due = str(meta.get("due_date") or "").strip()
        if due:
            try:
                due_d = datetime.strptime(due[:10], "%Y-%m-%d").date()
                days_until = (due_d - today_date).days
                if -7 <= days_until <= 7:
                    due_soon.append({**f, "_days_until": days_until, "_due_date": due[:10]})
            except (ValueError, OverflowError):
                continue
    if due_soon:
        due_soon.sort(key=lambda x: x["_days_until"])
        soonest = due_soon[0]
        d_val = soonest["_days_until"]
        has_overdue = any(x["_days_until"] < 0 for x in due_soon)
        if d_val < 0:
            due_phrase = f"overdue by {abs(d_val)}d ({soonest['_due_date']})"
        elif d_val == 0:
            due_phrase = f"due today ({soonest['_due_date']})"
        else:
            due_phrase = f"due {soonest['_due_date']} ({d_val}d)"
        title_str = (
            f"{len(due_soon)} payment(s) overdue or due within 7 days"
            if has_overdue
            else f"{len(due_soon)} payment(s) due within 7 days"
        )
        insights.append({
            "id": "due_soon",
            "type": "due_soon",
            "severity": "warning",
            "title": title_str,
            "detail": f"Next: {soonest.get('name', 'file')} · {due_phrase}",
            "count": len(due_soon),
            "action_prompt": "show me payments due in the next 7 days",
            "action_label": "View due payments",
        })

    if mismatched_vision:
        insights.append({
            "id": "vision_model_mismatch",
            "type": "vision_model_mismatch",
            "severity": "action",
            "title": f"{len(mismatched_vision)} image(s) indexed with an older vision model",
            "detail": f"Re-index images so visual search matches active model ({active_vision_model})",
            "count": len(mismatched_vision),
            "action_prompt": "reindex images with the active vision model",
            "action_label": "Re-index images",
        })

    stale_downloads = [
        f for f in files
        if "download" in str(f.get("folderFull") or f.get("folder") or "").lower()
        and _days_ago(float(f.get("mtime") or now), now) > 30
    ]
    if stale_downloads:
        total_mb = sum(int(f.get("sizeBytes") or 0) for f in stale_downloads) / (1024 * 1024)
        insights.append({
            "id": "stale_downloads",
            "type": "stale_downloads",
            "severity": "info",
            "title": f"{len(stale_downloads)} stale Download(s) (30+ days old)",
            "detail": f"{_format_bytes_mb(int(total_mb * 1024 * 1024))} · organize or archive to free up space",
            "count": len(stale_downloads),
            "action_prompt": "organize my stale downloads",
            "action_label": "Organize Downloads",
        })

    cryptic = [
        f for f in files
        if f.get("suggestedName") and not f.get("renamed")
    ]
    if cryptic:
        first_c = cryptic[0]
        c_name = str(first_c.get("name") or "").strip()
        c_sugg = str(first_c.get("suggestedName") or "").strip()
        sample_preview = f" (e.g. {c_name} → {c_sugg})" if c_name and c_sugg else ""
        insights.append({
            "id": "cryptic_names",
            "type": "cryptic_names",
            "severity": "info",
            "title": f"{len(cryptic)} file(s) with cryptic names",
            "detail": f"Smart rename suggestions ready{sample_preview} — clean filenames improve search",
            "count": len(cryptic),
            "action_prompt": "rename messy files",
            "action_label": "Review renames",
        })

    uncategorized = [
        f for f in files
        if str(f.get("shelfId") or f.get("shelf_id") or "") in {"", "tray", "uncategorized"}
    ]
    if len(uncategorized) > 5:
        insights.append({
            "id": "uncategorized",
            "type": "uncategorized",
            "severity": "info",
            "title": f"{len(uncategorized)} file(s) in the Quiet Tray",
            "detail": "These files haven't been auto-categorized yet — recluster or assign shelves manually",
            "count": len(uncategorized),
            "action_prompt": "organize uncategorized files into shelves",
            "action_label": "Organize shelves",
        })

    insights.sort(key=lambda x: {"warning": 0, "action": 1, "info": 2}.get(x["severity"], 3))
    return insights[:max_insights]


def format_insight_markdown(insights: list[dict[str, Any]]) -> str:
    """Format insights as a Markdown summary for the AI assistant."""
    if not insights:
        return "Your workspace looks clean! No actionable insights detected."
    lines = ["Here's what I found in your workspace:\n"]
    for i, ins in enumerate(insights, 1):
        lines.append(f"**{i}. {ins['title']}**\n")
        lines.append(f"   {ins['detail']}\n")
        if ins.get("action_prompt"):
            lines.append(f"   → Try: \"{ins['action_prompt']}\"\n")
    return "\n".join(lines)