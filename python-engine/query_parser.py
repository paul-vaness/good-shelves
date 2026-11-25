"""
Unified Natural-Language & Operator File Query Parser (FileQuerySpec AST) for Good Shelves.

Shared across semantic search (`/search`), Ask AI Q&A, and action target resolution
(`_resolve_action_files`) so every part of Good Shelves understands identical filters:
  - Multi-type & formats ("all images and PDFs", "Word docs or spreadsheets")
  - Dynamic folder & shelf names (matched against ALL indexed folders/subfolders & shelves)
  - Past + future/due temporal windows ("last week" = 7d, "older than 6 months", "stale",
    "Q1 2026", "between Jan and Mar", "due in the next 7 days", "overdue", bare-year guard)
  - Size constraints ("over 50MB", "under 100KB", "empty files")
  - Entity filters (merchant, amount, doc_type, due_date, invoice, email, phone)
  - Status & attribute filters (tags, untagged, pinned, unnamed, missing/has OCR, duplicates)
  - Negations ("except receipts", "except pinned", "without draft")
  - Sorting & top-K ("top 5 largest PDFs", "3 oldest screenshots")
  - Full-text SQLite evaluator (eliminates 600-char cache blindness)
"""

from __future__ import annotations

import calendar
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from entity_extractor import (
    entity_filters_from_query,
    matches_entity_filters,
    strip_entity_operators,
)

_MONTH_MAP: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_MONTHS_PAT = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

_CAMERA_BRANDS = (
    "iphone", "samsung", "galaxy", "pixel", "canon", "nikon", "sony",
    "fujifilm", "fuji", "leica", "gopro", "dji", "xiaomi", "oppo", "vivo", "huawei",
)

_EXPLICIT_LIST_TRIGGER = re.compile(
    r"\b(?:list(?:\s+all|\s+every|\s+the|\s+things|\s+items)?|show\s+(?:me\s+)?(?:all|every|my)?|"
    r"find\s+all|give\s+me\s+all|how\s+many|count(?:\s+of)?|number\s+of|only|all\s+my|all\s+the)\b",
    re.IGNORECASE,
)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "can", "could",
    "did", "do", "does", "every", "find", "for", "from", "get", "give", "had",
    "has", "have", "here", "how", "i", "in", "into", "is", "it", "its", "just",
    "list", "look", "looking", "many", "me", "more", "most", "much", "my", "of",
    "on", "only", "or", "our", "please", "search", "show", "some", "that", "the",
    "their", "them", "there", "these", "they", "this", "those", "to", "was", "we",
    "were", "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "you", "your", "all", "files", "file", "items", "item", "stuff", "things", "ones",
}

_TYPE_WORD_MAP: dict[str, tuple[Optional[str], set[str], Optional[str], str]] = {
    # word -> (modality, exts, special_tag, label)
    "screenshot": ("image", set(), "screenshot", "screenshots"),
    "screenshots": ("image", set(), "screenshot", "screenshots"),
    "screencap": ("image", set(), "screenshot", "screenshots"),
    "screencaps": ("image", set(), "screenshot", "screenshots"),
    "photo": ("image", set(), None, "type: image"),
    "photos": ("image", set(), None, "type: image"),
    "picture": ("image", set(), None, "type: image"),
    "pictures": ("image", set(), None, "type: image"),
    "pic": ("image", set(), None, "type: image"),
    "pics": ("image", set(), None, "type: image"),
    "image": ("image", set(), None, "type: image"),
    "images": ("image", set(), None, "type: image"),
    "selfie": ("image", set(), None, "type: image"),
    "selfies": ("image", set(), None, "type: image"),
    "portrait": ("image", set(), None, "type: image"),
    "portraits": ("image", set(), None, "type: image"),
    "png": (None, {"png"}, None, "format: png"),
    "pngs": (None, {"png"}, None, "format: png"),
    "jpg": (None, {"jpg", "jpeg"}, None, "format: jpg"),
    "jpgs": (None, {"jpg", "jpeg"}, None, "format: jpg"),
    "jpeg": (None, {"jpg", "jpeg"}, None, "format: jpeg"),
    "webp": (None, {"webp"}, None, "format: webp"),
    "heic": (None, {"heic"}, None, "format: heic"),
    "svg": (None, {"svg"}, None, "format: svg"),
    "pdf": (None, {"pdf"}, None, "format: pdf"),
    "pdfs": (None, {"pdf"}, None, "format: pdf"),
    "docx": (None, {"docx", "doc"}, None, "format: docx"),
    "word": (None, {"docx", "doc", "rtf", "odt"}, None, "type: word"),
    "spreadsheet": (None, {"xlsx", "xlsm", "xls", "csv", "tsv"}, None, "type: spreadsheet"),
    "spreadsheets": (None, {"xlsx", "xlsm", "xls", "csv", "tsv"}, None, "type: spreadsheet"),
    "excel": (None, {"xlsx", "xlsm", "xls"}, None, "type: spreadsheet"),
    "xlsx": (None, {"xlsx", "xlsm", "xls"}, None, "format: xlsx"),
    "xls": (None, {"xls", "xlsx"}, None, "format: xls"),
    "csv": (None, {"csv"}, None, "format: csv"),
    "csvs": (None, {"csv"}, None, "format: csv"),
    "workbook": (None, {"xlsx", "xlsm", "xls"}, None, "type: spreadsheet"),
    "sheets": (None, {"xlsx", "xlsm", "xls", "csv"}, None, "type: spreadsheet"),
    "slide": (None, {"ppt", "pptx", "key"}, None, "type: slides"),
    "slides": (None, {"ppt", "pptx", "key"}, None, "type: slides"),
    "presentation": (None, {"ppt", "pptx", "key"}, None, "type: slides"),
    "presentations": (None, {"ppt", "pptx", "key"}, None, "type: slides"),
    "ppt": (None, {"ppt", "pptx"}, None, "type: slides"),
    "pptx": (None, {"pptx", "ppt"}, None, "format: pptx"),
    "powerpoint": (None, {"ppt", "pptx"}, None, "type: slides"),
    "deck": (None, {"ppt", "pptx", "key"}, None, "type: slides"),
    "markdown": (None, {"md", "markdown"}, None, "format: md"),
    "md": (None, {"md", "markdown"}, None, "format: md"),
    "json": (None, {"json"}, None, "format: json"),
    "python": (None, {"py"}, None, "format: py"),
    "py": (None, {"py"}, None, "format: py"),
    "typescript": (None, {"ts", "tsx"}, None, "format: ts"),
    "ts": (None, {"ts", "tsx"}, None, "format: ts"),
    "javascript": (None, {"js", "jsx"}, None, "format: js"),
    "txt": (None, {"txt"}, None, "format: txt"),
    "video": ("video", set(), None, "type: video"),
    "videos": ("video", set(), None, "type: video"),
    "clip": ("video", set(), None, "type: video"),
    "clips": ("video", set(), None, "type: video"),
    "movie": ("video", set(), None, "type: video"),
    "movies": ("video", set(), None, "type: video"),
    "mp4": (None, {"mp4"}, None, "format: mp4"),
    "mov": (None, {"mov"}, None, "format: mov"),
    "audio": ("audio", set(), None, "type: audio"),
    "song": ("audio", set(), None, "type: audio"),
    "songs": ("audio", set(), None, "type: audio"),
    "music": ("audio", set(), None, "type: audio"),
    "mp3": (None, {"mp3"}, None, "format: mp3"),
    "wav": (None, {"wav"}, None, "format: wav"),
    "m4a": (None, {"m4a"}, None, "format: m4a"),
    "podcast": ("audio", set(), None, "type: audio"),
    "archive": ("archive", set(), None, "type: archive"),
    "archives": ("archive", set(), None, "type: archive"),
    "zip": (None, {"zip", "tar", "gz", "7z", "rar"}, None, "type: archive"),
    "zips": (None, {"zip", "tar", "gz", "7z", "rar"}, None, "type: archive"),
    "code": ("code", set(), None, "type: code"),
    "script": ("code", set(), None, "type: code"),
    "scripts": ("code", set(), None, "type: code"),
    "document": ("document", set(), None, "type: document"),
    "documents": ("document", set(), None, "type: document"),
    "doc": ("document", set(), None, "type: document"),
    "docs": ("document", set(), None, "type: document"),
}

_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "fifteen": 15, "twenty": 20,
}

_DOC_TYPE_NOUNS: dict[str, str] = {
    "receipt": "receipt",
    "receipts": "receipt",
    "invoice": "invoice",
    "invoices": "invoice",
    "bill": "bill",
    "bills": "bill",
    "statement": "statement",
    "statements": "statement",
    "contract": "contract",
    "contracts": "contract",
    "ticket": "ticket",
    "tickets": "ticket",
    "prescription": "prescription",
    "prescriptions": "prescription",
    "certificate": "certificate",
    "certificates": "certificate",
}


def parse_size_bytes(raw: str) -> Optional[int]:
    """Parse human size strings like '50MB', '100 KB', '2.5gb' into bytes."""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb)?$", (raw or "").strip().lower())
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2) or "b"
    mult = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}.get(unit, 1)
    return int(val * mult)


def _month_bounds(yr: int, mo: int) -> tuple[datetime, datetime]:
    last_day = calendar.monthrange(yr, mo)[1]
    return datetime(yr, mo, 1, 0, 0, 0), datetime(yr, mo, last_day, 23, 59, 59)


def _parse_single_date(expr: str, now_dt: datetime) -> Optional[tuple[datetime, datetime, str]]:
    s = re.sub(r"\s+", " ", (expr or "").strip(" ,.?!\"'")).lower()
    if not s:
        return None
    if s == "today":
        t0 = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return t0, t0.replace(hour=23, minute=59, second=59), "Today"
    if s == "yesterday":
        y0 = now_dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return y0, y0.replace(hour=23, minute=59, second=59), "Yesterday"

    # ISO YYYY-MM-DD or YYYY/MM/DD
    m_iso = re.fullmatch(r"((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m_iso:
        try:
            d = datetime(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
            return d, d.replace(hour=23, minute=59, second=59), d.strftime("%b %d, %Y")
        except ValueError:
            return None

    # MM/DD/YYYY or DD/MM/YYYY
    m_us = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/]((?:19|20)\d{2})", s)
    if m_us:
        a, b, yr = int(m_us.group(1)), int(m_us.group(2)), int(m_us.group(3))
        try:
            mo, dy = (a, b) if a <= 12 else (b, a)
            d = datetime(yr, mo, dy)
            return d, d.replace(hour=23, minute=59, second=59), d.strftime("%b %d, %Y")
        except ValueError:
            return None

    # Month Day, Year (e.g. Oct 18, 2025)
    m_mdy = re.fullmatch(rf"({_MONTHS_PAT})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,\s*|\s+)((?:19|20)\d{{2}})", s)
    if m_mdy:
        mo = _MONTH_MAP.get(m_mdy.group(1), 0)
        dy, yr = int(m_mdy.group(2)), int(m_mdy.group(3))
        if mo:
            try:
                d = datetime(yr, mo, dy)
                return d, d.replace(hour=23, minute=59, second=59), d.strftime("%b %d, %Y")
            except ValueError:
                return None

    # Day Month Year (e.g. 18 Oct 2025)
    m_dmy = re.fullmatch(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTHS_PAT})\s+((?:19|20)\d{{2}})", s)
    if m_dmy:
        dy = int(m_dmy.group(1))
        mo = _MONTH_MAP.get(m_dmy.group(2), 0)
        yr = int(m_dmy.group(3))
        if mo:
            try:
                d = datetime(yr, mo, dy)
                return d, d.replace(hour=23, minute=59, second=59), d.strftime("%b %d, %Y")
            except ValueError:
                return None

    # Month Year (e.g. March 2026)
    m_my = re.fullmatch(rf"({_MONTHS_PAT})\s+((?:19|20)\d{{2}})", s)
    if m_my:
        mo = _MONTH_MAP.get(m_my.group(1), 0)
        yr = int(m_my.group(2))
        if mo:
            d0, d1 = _month_bounds(yr, mo)
            return d0, d1, d0.strftime("%b %Y")

    # Month Day (no year -> infer current or previous year)
    m_md = re.fullmatch(rf"({_MONTHS_PAT})\s+(\d{{1,2}})(?:st|nd|rd|th)?", s)
    if m_md:
        mo = _MONTH_MAP.get(m_md.group(1), 0)
        dy = int(m_md.group(2))
        if mo:
            yr = now_dt.year
            try:
                d = datetime(yr, mo, dy)
                if d > now_dt + timedelta(days=30):
                    d = datetime(yr - 1, mo, dy)
                return d, d.replace(hour=23, minute=59, second=59), d.strftime("%b %d, %Y")
            except ValueError:
                return None

    # Month only
    if s in _MONTH_MAP:
        mo = _MONTH_MAP[s]
        yr = now_dt.year if mo <= now_dt.month else now_dt.year - 1
        d0, d1 = _month_bounds(yr, mo)
        return d0, d1, d0.strftime("%b %Y")

    # Year only
    if re.fullmatch(r"(?:19|20)\d{2}", s):
        yr = int(s)
        return datetime(yr, 1, 1), datetime(yr, 12, 31, 23, 59, 59), str(yr)

    return None


def parse_due_window(query: str, now_dt: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """
    Parse future / due-date windows such as:
      - "payments due in the next 7 days", "due within 14 days", "due in 5 days"
      - "due soon", "due this week", "due next week", "due this month", "due today"
      - "upcoming bills", "overdue bills", "overdue payments"
    """
    raw = query or ""
    low = raw.lower()
    now = now_dt or datetime.now()
    today = now.date()

    def _pack(start_d: Any, end_d: Any, label: str, span: str, overdue_only: bool = False, include_overdue: bool = True) -> dict[str, Any]:
        return {
            "start_date": start_d.strftime("%Y-%m-%d"),
            "end_date": end_d.strftime("%Y-%m-%d"),
            "label": label,
            "span_text": span,
            "overdue_only": overdue_only,
            "include_overdue": include_overdue,
        }

    # Explicit overdue
    m_over = re.search(r"\b(overdue(?:\s+(?:payments?|bills?|invoices?))?|past\s+due)\b", low)
    if m_over:
        return _pack(
            datetime(1970, 1, 1).date(),
            today - timedelta(days=1),
            "Overdue",
            m_over.group(0),
            overdue_only=True,
            include_overdue=True,
        )

    # "due in the next N days/weeks", "due within N days"
    m_next_n = re.search(
        r"\b(?:due|expir(?:ing|es?))\s+(?:in\s+(?:the\s+)?next|within(?:\s+the\s+next)?|in)\s+(\d+)\s+(day|days|week|weeks|month|months)\b",
        low,
    )
    if m_next_n:
        n = max(1, min(365, int(m_next_n.group(1))))
        unit = m_next_n.group(2)
        days = n * (30 if unit.startswith("month") else 7 if unit.startswith("week") else 1)
        # Match proactive_insights (-7d overdue window through +Nd upcoming) so insight clicks always match!
        return _pack(
            today - timedelta(days=7),
            today + timedelta(days=days),
            f"Due in next {n} {unit}",
            m_next_n.group(0),
            overdue_only=False,
            include_overdue=True,
        )

    m_due_today = re.search(r"\b(?:due|expir(?:ing|es?))\s+today\b", low)
    if m_due_today:
        return _pack(today, today, "Due today", m_due_today.group(0), include_overdue=False)

    m_due_week = re.search(r"\b(?:due|expir(?:ing|es?))\s+(?:soon|this\s+week|next\s+week)\b", low)
    if m_due_week:
        return _pack(today - timedelta(days=7), today + timedelta(days=7), "Due within 7 days", m_due_week.group(0))

    m_due_month = re.search(r"\b(?:due|expir(?:ing|es?))\s+(?:this\s+month|next\s+month)\b", low)
    if m_due_month:
        return _pack(today - timedelta(days=7), today + timedelta(days=30), "Due within 30 days", m_due_month.group(0))

    m_upcoming = re.search(r"\bupcoming\s+(?:bills?|payments?|invoices?|deadlines?|due\s+dates?)\b", low)
    if m_upcoming:
        return _pack(today, today + timedelta(days=14), "Upcoming (14 days)", m_upcoming.group(0), include_overdue=False)

    return None


def parse_temporal_spec(query: str, now_dt: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """
    Parse natural-language dates, months, quarters, ranges, and relative windows into exact timestamps.
    Fixes:
      - "last week" / "past week" -> exact 7 days (not 14 days!)
      - "older than N days/weeks/months/years" and "stale" (>30 days)
      - "Q1 2026" .. "Q4 2026" (and bare Q1..Q4)
      - Bare-year guard so "invoice 2025", "over 2000", "$2025", "#2025" aren't hijacked as years.
    """
    raw = query or ""
    lowered = raw.lower()
    now = now_dt or datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start.replace(hour=23, minute=59, second=59)

    date_scope = "taken" if re.search(r"\b(taken|captured|shot|snap|snapped|created)\b", lowered) else "any"

    def _pack(d0: datetime, d1: datetime, label: str, span: str) -> dict[str, Any]:
        return {
            "start_ts": d0.timestamp(),
            "end_ts": d1.timestamp(),
            "start_date": d0.strftime("%Y-%m-%d"),
            "end_date": d1.strftime("%Y-%m-%d"),
            "label": label,
            "span_text": span,
            "date_scope": date_scope,
        }

    # 1. "older than N days/weeks/months/years"
    m_older = re.search(
        r"\b(?:older\s+than|more\s+than|untouched\s+for(?:\s+over)?|over)\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)(?:\s+old|\s+ago)?\b",
        lowered,
    )
    if m_older:
        n = max(1, min(3650, int(m_older.group(1))))
        unit = m_older.group(2)
        days = n * (365 if unit.startswith("year") else 30 if unit.startswith("month") else 7 if unit.startswith("week") else 1)
        cutoff = today_start - timedelta(days=days)
        return _pack(datetime(1970, 1, 1), cutoff, f"Older than {n} {unit}", m_older.group(0))

    # 2. "stale" (>30 days untouched)
    m_stale = re.search(r"\bstale\b", lowered)
    if m_stale:
        cutoff = today_start - timedelta(days=30)
        return _pack(datetime(1970, 1, 1), cutoff, "Stale (>30 days)", m_stale.group(0))

    # 3. Quarters: Q1 2026, Q2, etc.
    m_qtr = re.search(r"\b(?:in\s+|from\s+|for\s+)?q([1-4])(?:\s+((?:19|20)\d{2}))?\b", lowered)
    if m_qtr:
        q_num = int(m_qtr.group(1))
        yr = int(m_qtr.group(2)) if m_qtr.group(2) else now.year
        start_mo = (q_num - 1) * 3 + 1
        end_mo = start_mo + 2
        d0, _ = _month_bounds(yr, start_mo)
        _, d1 = _month_bounds(yr, end_mo)
        return _pack(d0, d1, f"Q{q_num} {yr}", m_qtr.group(0))

    # 4. Range: 'between X and Y' or 'from X to Y'
    m_range = re.search(
        r"\b(?:between|from)\s+([a-z0-9,/\-\s]{3,28}?)\s+(?:and|to|through|-)\s+([a-z0-9,/\-\s]{3,28}?)(?=$|\s+(?:without|excluding|except|that|with|which|in\s+my)\b|[?.!,])",
        lowered,
    )
    if m_range:
        p1 = _parse_single_date(m_range.group(1), now)
        p2 = _parse_single_date(m_range.group(2), now)
        if p1 and p2:
            d0 = min(p1[0], p2[0])
            d1 = max(p1[1], p2[1])
            return _pack(d0, d1, f"{p1[2]} – {p2[2]}", m_range.group(0))

    # 5. Open-ended comparisons: 'after <date>', 'since <date>', 'before <date>'
    m_cmp = re.search(
        rf"\b(after|since|before)\s+((?:{_MONTHS_PAT}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s*(?:(?:19|20)\d{{2}})?|{_MONTHS_PAT}\s+(?:19|20)\d{{2}}|(?:19|20)\d{{2}}[-/]\d{{1,2}}[-/]\d{{1,2}}|\d{{1,2}}[-/]\d{{1,2}}[-/](?:19|20)\d{{2}}|(?:19|20)\d{{2}}|yesterday|last\s+week|last\s+month))",
        lowered,
    )
    if m_cmp:
        op, rhs = m_cmp.group(1), m_cmp.group(2).strip()
        parsed_rhs = _parse_single_date(rhs, now)
        if not parsed_rhs and rhs == "last week":
            w0 = today_start - timedelta(days=7)
            parsed_rhs = (w0, today_end, "last week")
        elif not parsed_rhs and rhs == "last month":
            m0 = today_start - timedelta(days=30)
            parsed_rhs = (m0, today_end, "last month")
        if parsed_rhs:
            if op in ("after", "since"):
                return _pack(parsed_rhs[0], today_end, f"Since {parsed_rhs[2]}", m_cmp.group(0))
            return _pack(datetime(1970, 1, 1), parsed_rhs[1], f"Before {parsed_rhs[2]}", m_cmp.group(0))

    # 6. Specific calendar dates
    date_patterns = [
        r"\b(?:on|taken\s+on|from|dated)?\s*((?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2})\b",
        r"\b(?:on|taken\s+on|from|dated)?\s*(\d{1,2}[-/]\d{1,2}[-/](?:19|20)\d{2})\b",
        rf"\b(?:on|taken\s+on|from|dated)?\s*({_MONTHS_PAT}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*,\s*|\s+)(?:19|20)\d{{2}})\b",
        rf"\b(?:on|taken\s+on|from|dated)?\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS_PAT}\s+(?:19|20)\d{{2}})\b",
        rf"\b(?:in|during|from|taken\s+in)?\s*({_MONTHS_PAT}\s+(?:19|20)\d{{2}})\b",
        rf"\b(?:on|taken\s+on|from)?\s*({_MONTHS_PAT}\s+\d{{1,2}}(?:st|nd|rd|th)?)\b",
    ]
    for pat in date_patterns:
        md = re.search(pat, lowered)
        if md:
            parsed = _parse_single_date(md.group(1), now)
            if parsed:
                return _pack(parsed[0], parsed[1], parsed[2], md.group(0))

    # 7. Relative counts: 'last 7 days', 'past 2 weeks', '3 days ago'
    m_rel = re.search(r"\b(?:last|past)\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)\b", lowered)
    if m_rel:
        n = max(1, min(3650, int(m_rel.group(1))))
        unit = m_rel.group(2)
        days = n * (365 if unit.startswith("year") else 30 if unit.startswith("month") else 7 if unit.startswith("week") else 1)
        d0 = today_start - timedelta(days=days)
        return _pack(d0, today_end, f"Last {n} {unit}", m_rel.group(0))

    m_ago = re.search(r"\b(\d+)\s+(day|days|week|weeks|month|months)\s+ago\b", lowered)
    if m_ago:
        n = max(1, min(365, int(m_ago.group(1))))
        unit = m_ago.group(2)
        days = n * (30 if unit.startswith("month") else 7 if unit.startswith("week") else 1)
        target = today_start - timedelta(days=days)
        return _pack(target, target.replace(hour=23, minute=59, second=59), f"{n} {unit} ago", m_ago.group(0))

    if re.search(r"\btoday\b", lowered):
        return _pack(today_start, today_end, "Today", "today")
    if re.search(r"\byesterday\b", lowered):
        y0 = today_start - timedelta(days=1)
        return _pack(y0, y0.replace(hour=23, minute=59, second=59), "Yesterday", "yesterday")

    m_day = re.search(r"\b(?:last|on|from)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    if m_day:
        target_wd = _WEEKDAYS[m_day.group(1)]
        days_back = (now.weekday() - target_wd) % 7
        if days_back == 0:
            days_back = 7
        day_start = today_start - timedelta(days=days_back)
        return _pack(day_start, day_start.replace(hour=23, minute=59, second=59), f"Last {m_day.group(1).title()}", m_day.group(0))

    if re.search(r"\b(?:this|last)\s+weekend\b", lowered):
        days_since_sat = (now.weekday() - 5) % 7
        if "last weekend" in lowered and days_since_sat < 2:
            days_since_sat += 7
        sat = today_start - timedelta(days=days_since_sat)
        sun_end = (sat + timedelta(days=1)).replace(hour=23, minute=59, second=59)
        lbl = "Last weekend" if "last weekend" in lowered else "This weekend"
        return _pack(sat, sun_end, lbl, lbl.lower())

    m_month_only = re.search(rf"\b(last|this|in|from|during)\s+({_MONTHS_PAT})\b", lowered)
    if m_month_only:
        mod, mo_name = m_month_only.group(1), m_month_only.group(2)
        mo = _MONTH_MAP[mo_name]
        yr = now.year
        if mod == "last" or (mod in ("in", "from", "during") and mo > now.month):
            if mo >= now.month:
                yr -= 1
        d0, d1 = _month_bounds(yr, mo)
        return _pack(d0, d1, d0.strftime("%b %Y"), m_month_only.group(0))

    # Fixed "last week" / "past week" -> exact 7 days!
    if "last week" in lowered or "past week" in lowered:
        span_txt = "last week" if "last week" in lowered else "past week"
        return _pack(today_start - timedelta(days=7), today_end, "Last week", span_txt)
    if "this week" in lowered:
        return _pack(today_start - timedelta(days=now.weekday()), today_end, "This week", "this week")
    if "last month" in lowered or "past month" in lowered:
        first_this = today_start.replace(day=1)
        last_prev = first_this - timedelta(seconds=1)
        first_prev = last_prev.replace(day=1, hour=0, minute=0, second=0)
        return _pack(first_prev, last_prev, first_prev.strftime("%b %Y"), "last month")
    if "this month" in lowered:
        return _pack(today_start.replace(day=1), today_end, today_start.strftime("%b %Y"), "this month")
    if "last year" in lowered:
        yr = now.year - 1
        return _pack(datetime(yr, 1, 1), datetime(yr, 12, 31, 23, 59, 59), str(yr), "last year")
    if "this year" in lowered:
        yr = now.year
        return _pack(datetime(yr, 1, 1), today_end, str(yr), "this year")

    # 8. Bare-year guard: match explicit temporal prepositions first; refuse to hijack
    #    "invoice 2025", "order 2025", "over 2000", "under 2050", "$2025", "PHP 2000", "#2025", "ref 2025".
    m_year_prep = re.search(r"\b(?:in|from|during|taken\s+in|created\s+in|year|dated)\s+((?:19|20)\d{2})\b", lowered)
    if m_year_prep:
        yr = int(m_year_prep.group(1))
        return _pack(datetime(yr, 1, 1), datetime(yr, 12, 31, 23, 59, 59), str(yr), m_year_prep.group(0))

    m_year_bare = re.search(r"(?<![#$₱€£\d-])\b((?:19|20)\d{2})\b(?![\d.])", lowered)
    if m_year_bare:
        before_ctx = lowered[:m_year_bare.start()].rstrip()
        # Reject if preceded by identifier / currency / numeric comparison words
        bad_prev = re.search(
            r"(?:\b(?:invoice|inv|order|ref|reference|no\.?|number|id|ticket|receipt|bill|check|cheque|"
            r"over|under|above|below|more\s+than|less\s+than|at\s+least|about|around|php|usd|eur|gbp|amount|total|cost|price|paid|spent|port|error|code|line|page|room|zip)\s*|"
            r"[#$₱€£])\s*$",
            before_ctx,
        )
        if not bad_prev:
            yr = int(m_year_bare.group(1))
            if 1990 <= yr <= now.year + 1:
                return _pack(datetime(yr, 1, 1), datetime(yr, 12, 31, 23, 59, 59), str(yr), m_year_bare.group(0))

    return None


@dataclass
class FileQuerySpec:
    """Unified query AST for search, Q&A, and action file resolution."""

    raw_query: str = ""
    topic_query: str = ""
    intent_mode: str = "search"  # search | list | count | finance | summarize | timeline | due_soon

    # Type / format filters (when both modalities and exts are non-empty and type_union=True, match EITHER!)
    modalities: set[str] = field(default_factory=set)
    exts: set[str] = field(default_factory=set)
    type_union: bool = False
    exclude_modalities: set[str] = field(default_factory=set)
    exclude_exts: set[str] = field(default_factory=set)

    # Dynamic folder & shelf constraints
    folder_hints: list[str] = field(default_factory=list)
    exclude_folders: list[str] = field(default_factory=list)
    shelf_hints: list[str] = field(default_factory=list)
    exclude_shelves: list[str] = field(default_factory=list)
    uncategorized_only: bool = False

    # Tags & status flags
    required_tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    untagged_only: bool = False
    pinned_only: bool = False
    exclude_pinned: bool = False
    unnamed_only: bool = False
    has_ocr: Optional[bool] = None
    has_todos: Optional[bool] = None
    duplicates_only: bool = False
    exact_duplicates_only: bool = False

    # Temporal (past & future due)
    temporal: Optional[dict[str, Any]] = None
    due_window: Optional[dict[str, Any]] = None

    # Size
    min_size_bytes: Optional[int] = None
    max_size_bytes: Optional[int] = None
    empty_only: bool = False

    # Entity filters
    entity_filters: dict[str, Any] = field(default_factory=dict)

    # Text phrases, camera, negations
    include_phrases: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    camera_hint: Optional[str] = None

    # Sort & top-K
    sort_by: Optional[str] = None
    top_k: Optional[int] = None

    applied_filters: list[str] = field(default_factory=list)

    @property
    def strict_modality(self) -> Optional[str]:
        if len(self.modalities) == 1 and not self.exts:
            return next(iter(self.modalities))
        return None

    @property
    def has_strict_filters(self) -> bool:
        return bool(
            self.modalities
            or self.exts
            or self.exclude_modalities
            or self.exclude_exts
            or self.folder_hints
            or self.exclude_folders
            or self.shelf_hints
            or self.exclude_shelves
            or self.uncategorized_only
            or self.required_tags
            or self.exclude_tags
            or self.untagged_only
            or self.pinned_only
            or self.exclude_pinned
            or self.unnamed_only
            or self.has_ocr is not None
            or self.has_todos is not None
            or self.duplicates_only
            or self.exact_duplicates_only
            or self.temporal is not None
            or self.due_window is not None
            or self.min_size_bytes is not None
            or self.max_size_bytes is not None
            or self.empty_only
            or self.entity_filters
            or self.include_phrases
            or self.exclude_terms
            or self.camera_hint is not None
        )

    def to_nl_dict(self) -> dict[str, Any]:
        """Backward-compatible dict matching legacy `_parse_nl_query` consumers."""
        size_op: Optional[str] = None
        size_bytes: Optional[int] = None
        if self.min_size_bytes is not None:
            size_op, size_bytes = ">", self.min_size_bytes
        elif self.max_size_bytes is not None:
            size_op, size_bytes = "<", self.max_size_bytes
        return {
            "include_phrases": list(self.include_phrases),
            "exclude_terms": list(self.exclude_terms),
            "exclude_modalities": list(self.exclude_modalities),
            "exclude_exts": list(self.exclude_exts),
            "temporal": self.temporal,
            "due_window": self.due_window,
            "camera_hint": self.camera_hint,
            "size_op": size_op,
            "size_bytes": size_bytes,
            "strict_folder": self.folder_hints[0] if self.folder_hints else None,
            "strict_modality": self.strict_modality,
            "strict_exts": set(self.exts) if not self.type_union else set(),
            "required_tags": list(self.required_tags),
            "pinned_only": self.pinned_only,
            "unnamed_only": self.unnamed_only,
            "intent_mode": self.intent_mode,
            "topic_query": self.topic_query,
            "has_strict_filters": self.has_strict_filters,
            "applied_filters": list(self.applied_filters),
            "spec": self,
        }


def _collect_known_folder_names(state: Optional[dict[str, Any]]) -> dict[str, str]:
    """Build lowercase folder name -> display/path map dynamically up to depth 4."""
    names: dict[str, str] = {
        "downloads": "downloads",
        "desktop": "desktop",
        "documents": "documents",
        "pictures": "pictures",
        "music": "music",
        "videos": "videos",
    }
    if not state:
        return names
    for wf in state.get("watchedFolders") or []:
        wp = Path(str(wf.get("path") or ""))
        if wp.name:
            names[wp.name.lower()] = wp.name
        if wp.exists() and wp.is_dir():
            # Walk up to depth 4 safely (bounded to avoid huge trees)
            stack: list[tuple[Path, int]] = [(wp, 0)]
            visited = 0
            while stack and visited < 400:
                cur, depth = stack.pop()
                if depth >= 4:
                    continue
                try:
                    for ch in cur.iterdir():
                        if ch.is_dir() and not ch.name.startswith(".") and ch.name not in {"node_modules", ".git", "__pycache__", "target", "dist"}:
                            visited += 1
                            names[ch.name.lower()] = ch.name
                            if depth + 1 < 4:
                                stack.append((ch, depth + 1))
                except OSError:
                    continue
    for pf in state.get("pinnedFolders") or []:
        pn = str(pf.get("name") or Path(str(pf.get("path") or "")).name).strip()
        if pn:
            names[pn.lower()] = pn
    for f in state.get("files") or []:
        ff = str(f.get("folderFull") or "").strip()
        if ff:
            p = Path(ff)
            if p.name:
                names[p.name.lower()] = p.name
            if p.parent.name:
                names[p.parent.name.lower()] = p.parent.name
    return names


def _collect_known_shelf_names(state: Optional[dict[str, Any]]) -> dict[str, str]:
    shelves_map: dict[str, str] = {"quiet tray": "Quiet Tray", "uncategorized": "Quiet Tray"}
    if not state:
        return shelves_map
    for s in state.get("shelves") or []:
        sn = str(s.get("name") or "").strip()
        if sn:
            shelves_map[sn.lower()] = sn
    return shelves_map


def parse_file_query(
    raw_query: str,
    state: Optional[dict[str, Any]] = None,
    now_dt: Optional[datetime] = None,
) -> FileQuerySpec:
    """
    Parse any natural-language or operator query into a rich `FileQuerySpec` AST.
    """
    spec = FileQuerySpec(raw_query=(raw_query or "").strip())
    work = spec.raw_query
    if not work:
        return spec

    now = now_dt or datetime.now()
    known_folders = _collect_known_folder_names(state)
    known_shelves = _collect_known_shelf_names(state)

    def _add_filter(lbl: str) -> None:
        if lbl and lbl not in spec.applied_filters:
            spec.applied_filters.append(lbl)

    # 0. Operator entity filters first (merchant:, amount:, due:, invoice:, doctype:, phone:, email:)
    op_ent = entity_filters_from_query(work)
    if op_ent:
        spec.entity_filters.update(op_ent)
        for k, v in op_ent.items():
            if k not in {"amount_op"}:
                _add_filter(f"{k}: {v}")
        work = strip_entity_operators(work)

    # 1. Extract quoted phrases FIRST so inner words aren't hijacked by date/type regexes.
    quote_pat = re.compile(r'"([^"]+)"|\'([^\']+)\'')
    for m in list(quote_pat.finditer(work)):
        phrase_val = (m.group(1) or m.group(2) or "").strip()
        if phrase_val and phrase_val.lower() not in [p.lower() for p in spec.include_phrases]:
            spec.include_phrases.append(phrase_val)
            _add_filter(f'includes "{phrase_val}"')
    work = quote_pat.sub(" ", work)
    work = re.sub(
        r"\b(?:that|which)?\s*(?:includes?|including|contains?|containing|mentions?|mentioning|with\s+the\s+words?|titled|called|named)\s*(?=$|\b(?:and|or|taken|captured|shot|from|after|before|since|between|without|excluding|except|in|on)\b|[?.!,])",
        " ",
        work,
        flags=re.IGNORECASE,
    )

    # 2. Extract unquoted clause-based inclusions: 'that includes solar tracking ...'
    clause_pat = re.compile(
        r"\b(?:that\s+|which\s+)?(?:includes?|including|contains?|containing|mentions?|mentioning|with\s+text|with\s+the\s+word|titled|called|named)\s+"
        r"([a-z0-9][a-z0-9\s._\-/]{1,60}?)"
        r"(?=$|\s+(?:taken|captured|shot|from|on|in\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{4}|downloads|desktop|documents)|after|before|since|between|without|excluding|except|larger|bigger|smaller|under|over)\b|[?.!,;])",
        re.IGNORECASE,
    )
    for m in list(clause_pat.finditer(work)):
        frag = re.sub(r"^(?:the\s+|this\s+|word\s+|words\s+|phrase\s+|text\s+)+", "", m.group(1).strip(), flags=re.IGNORECASE).strip()
        if len(frag) >= 2 and frag.lower() not in {"it", "this", "that", "them"}:
            if frag.lower() not in [p.lower() for p in spec.include_phrases]:
                spec.include_phrases.append(frag)
                _add_filter(f'includes "{frag}"')
    work = clause_pat.sub(" ", work)

    # 3. Extract negations: "except pinned", "except screenshots", "except receipts", "without draft", "not tagged #foo", "excluding Downloads"
    not_tag_pat = re.compile(r"\b(?:not\s+tagged|without\s+tag|except\s+tagged)\s+#?([a-z0-9_\-]{2,30})\b", re.IGNORECASE)
    for m in list(not_tag_pat.finditer(work)):
        t = m.group(1).strip().lower()
        if t and t not in spec.exclude_tags:
            spec.exclude_tags.append(t)
            _add_filter(f"without #{t}")
    work = not_tag_pat.sub(" ", work)

    excl_pat = re.compile(
        r"\b(?:without|excluding|exclude|except(?:\s+for)?|not\s+in)\s+(?:the\s+|my\s+)?([a-z0-9._\-#]{2,34})\b",
        re.IGNORECASE,
    )
    for m in list(excl_pat.finditer(work)):
        raw_t = m.group(1).strip().lower()
        t = raw_t.lstrip("#")
        if t in {"ocr", "ocr_text", "text"} and m.group(0).lower().startswith("without"):
            # "without ocr" -> missing OCR status filter, NOT term exclusion!
            spec.has_ocr = False
            _add_filter("missing OCR")
            work = work[:m.start()] + " " + work[m.end():]
            continue
        if raw_t.startswith("#"):
            spec.exclude_tags.append(t)
            _add_filter(f"without #{t}")
        elif t in {"pinned", "starred", "favorite", "favorites"}:
            spec.exclude_pinned = True
            _add_filter("except pinned")
        elif t in {"screenshot", "screenshots"}:
            spec.exclude_terms.append("screenshot")
            _add_filter("except screenshots")
        elif t in {"photo", "photos", "image", "images", "picture", "pictures"}:
            spec.exclude_modalities.add("image")
            _add_filter("without images")
        elif t in {"pdf", "pdfs"}:
            spec.exclude_exts.add("pdf")
            _add_filter("without pdf")
        elif t in {"doc", "docs", "document", "documents"}:
            spec.exclude_modalities.add("document")
            _add_filter("without documents")
        elif t in {"video", "videos"}:
            spec.exclude_modalities.add("video")
            _add_filter("without videos")
        elif t in known_folders:
            spec.exclude_folders.append(t)
            _add_filter(f"except folder: {known_folders[t]}")
        elif t in known_shelves:
            spec.exclude_shelves.append(t)
            _add_filter(f"except shelf: {known_shelves[t]}")
        else:
            spec.exclude_terms.append(t)
            _add_filter(f"without {t}")
    work = excl_pat.sub(
        lambda m: m.group(0) if m.group(1).lower() == "ocr" and m.group(0).lower().startswith("without") else " ",
        work,
    )

    # 4. Future/Due temporal windows FIRST (so "due in the next 7 days" isn't confused with past temporal windows!)
    due_win = parse_due_window(work, now_dt=now)
    if due_win:
        spec.due_window = due_win
        _add_filter(due_win["label"])
        work = re.sub(re.escape(due_win["span_text"]), " ", work, flags=re.IGNORECASE)

    # 5. Natural-language amount filters ("over $50", "under ₱1,000", "above 500 php")
    amt_pat = re.compile(
        r"\b(over|above|more\s+than|at\s+least|greater\s+than|under|below|less\s+than|at\s+most)\s*"
        r"(?:(PHP|USD|EUR|GBP|₱|\$|€|£)\s*)?(\d[\d,]*(?:\.\d{1,2})?)(?:\s*(PHP|USD|EUR|GBP|pesos?|dollars?))?\b",
        re.IGNORECASE,
    )
    for m in list(amt_pat.finditer(work)):
        # Skip if followed by size unit (mb/kb/gb/b) or time unit (days/weeks/months/years)
        after_s = work[m.end():m.end() + 14].lower()
        if re.match(r"^\s*(?:b|kb|mb|gb|tb|days?|weeks?|months?|years?|files?|items?|pages?)\b", after_s):
            continue
        cur_pre, cur_post = m.group(2), m.group(4)
        # Require currency marker OR financial context in query so "over 2000" only triggers when currency/finance is meant
        has_fin_ctx = bool(
            cur_pre or cur_post
            or re.search(r"\b(?:receipt|receipts|invoice|invoices|bill|bills|spent|spend|paid|cost|price|amount|expense|expenses|total|payment|payments)\b", spec.raw_query, re.I)
        )
        if not has_fin_ctx:
            continue
        op_phrase = m.group(1).lower()
        try:
            val = float(m.group(3).replace(",", ""))
        except ValueError:
            continue
        if any(w in op_phrase for w in ("over", "above", "more", "greater")):
            spec.entity_filters["amount_op"] = ">"
            spec.entity_filters["amount_value"] = val
            _add_filter(f"amount > {val:g}")
        elif "least" in op_phrase:
            spec.entity_filters["amount_op"] = ">="
            spec.entity_filters["amount_value"] = val
            _add_filter(f"amount >= {val:g}")
        elif "most" in op_phrase:
            spec.entity_filters["amount_op"] = "<="
            spec.entity_filters["amount_value"] = val
            _add_filter(f"amount <= {val:g}")
        else:
            spec.entity_filters["amount_op"] = "<"
            spec.entity_filters["amount_value"] = val
            _add_filter(f"amount < {val:g}")
        work = work[:m.start()] + " " + work[m.end():]
        break

    # 6. Past / Calendar temporal spec (after due_window & amount extraction!)
    temp_spec = parse_temporal_spec(work, now_dt=now)
    if temp_spec:
        spec.temporal = temp_spec
        _add_filter(f"date: {temp_spec['label']}")
        span_esc = re.escape(temp_spec["span_text"].strip())
        work = re.sub(rf"(?:\btaken\s+|\bcaptured\s+|\bshot\s+|\bcreated\s+)?{span_esc}", " ", work, flags=re.IGNORECASE)

    # 7. Size constraints ("over 50MB", "under 100KB", "larger than 2 MB", "empty files")
    if re.search(r"\bempty\s+(?:files?|docs?|documents?|items?)\b|\b0\s*(?:b|bytes)\b", work, re.IGNORECASE):
        spec.empty_only = True
        _add_filter("empty (0 B)")
        work = re.sub(r"\bempty\b", " ", work, flags=re.IGNORECASE)

    sz_pat = re.compile(
        r"\b(larger\s+than|bigger\s+than|more\s+than|over|above|at\s+least|smaller\s+than|less\s+than|under|below)\s+(\d+(?:\.\d+)?\s*(?:b|kb|mb|gb|tb))\b",
        re.IGNORECASE,
    )
    for m_sz in list(sz_pat.finditer(work)):
        verb = m_sz.group(1).lower()
        amt_s = m_sz.group(2).replace(" ", "")
        parsed_sz = parse_size_bytes(amt_s)
        if parsed_sz is not None:
            if any(w in verb for w in ("larger", "bigger", "more", "over", "above", "least")):
                spec.min_size_bytes = parsed_sz
                _add_filter(f"size > {amt_s.upper()}")
            else:
                spec.max_size_bytes = parsed_sz
                _add_filter(f"size < {amt_s.upper()}")
            work = work[:m_sz.start()] + " " + work[m_sz.end():]

    # 8. Camera hints ("taken with iPhone", "shot on Canon")
    cam_pat = re.compile(
        r"\b(?:taken\s+with|shot\s+on|shot\s+with|captured\s+with|camera)\s+([a-z0-9][a-z0-9\s\-]{1,24}?)(?=$|\s+(?:in|on|from|after|before|without|except)\b|[?.!,])",
        re.IGNORECASE,
    )
    m_cam = cam_pat.search(work)
    if m_cam:
        spec.camera_hint = m_cam.group(1).strip().lower()
        _add_filter(f"camera: {m_cam.group(1).strip()}")
        work = cam_pat.sub(" ", work)
    else:
        low_toks = set(re.findall(r"[a-z0-9]{2,}", work.lower()))
        for brand in _CAMERA_BRANDS:
            if brand in low_toks and (low_toks & {"photo", "photos", "picture", "pictures", "image", "images", "shot", "taken", "camera"}):
                spec.camera_hint = brand
                _add_filter(f"camera: {brand}")
                break

    # 9. Tags (#tag, tagged X, untagged)
    if re.search(r"\b(?:untagged|no\s+tags?|without\s+tags?)\b", work, re.IGNORECASE):
        spec.untagged_only = True
        _add_filter("untagged")
        work = re.sub(r"\b(?:untagged|no\s+tags?|without\s+tags?)\b", " ", work, flags=re.IGNORECASE)

    for m_ht in list(re.finditer(r"(?<!\w)#([a-zA-Z0-9_\-]{2,32})\b", work)):
        t = m_ht.group(1).lower()
        if t not in spec.required_tags and t not in spec.exclude_tags:
            spec.required_tags.append(t)
            _add_filter(f"#{t}")
    work = re.sub(r"(?<!\w)#[a-zA-Z0-9_\-]{2,32}\b", " ", work)

    m_tagged = re.search(r"\btagged\s+(?:with\s+|as\s+)?([a-z0-9_\-]{2,32})\b", work, re.IGNORECASE)
    if m_tagged:
        t = m_tagged.group(1).lower()
        if t not in spec.required_tags:
            spec.required_tags.append(t)
            _add_filter(f"#{t}")
        work = work[:m_tagged.start()] + " " + work[m_tagged.end():]

    # 10. Status flags: OCR missing/present, action items/TODOs, duplicates, uncategorized/Quiet Tray, pinned, unnamed
    if re.search(r"\b(?:with\s+(?:action\s+items?|todos?|fixmes?|pending\s+tasks?)|having\s+(?:action\s+items?|todos?)|has\s+(?:action\s+items?|todos?)|files?\s+with\s+(?:action\s+items?|todos?))\b", work, re.IGNORECASE):
        spec.has_todos = True
        _add_filter("has TODOs / action items")
        work = re.sub(r"\b(?:with\s+(?:action\s+items?|todos?|fixmes?|pending\s+tasks?)|having\s+(?:action\s+items?|todos?)|has\s+(?:action\s+items?|todos?))\b", " ", work, flags=re.IGNORECASE)

    if re.search(r"\b(?:missing\s+ocr|without\s+ocr|no\s+ocr|un-?ocr(?:'?d)?|needs?\s+ocr)\b", work, re.IGNORECASE):
        spec.has_ocr = False
        _add_filter("missing OCR")
        work = re.sub(r"\b(?:missing\s+ocr|without\s+ocr|no\s+ocr|un-?ocr(?:'?d)?|needs?\s+ocr)\b", " ", work, flags=re.IGNORECASE)
    elif re.search(r"\b(?:has\s+ocr|with\s+ocr|ocr(?:'?d|ed))\b", work, re.IGNORECASE):
        spec.has_ocr = True
        _add_filter("has OCR")
        work = re.sub(r"\b(?:has\s+ocr|with\s+ocr|ocr(?:'?d|ed))\b", " ", work, flags=re.IGNORECASE)

    if re.search(r"\bexact\s+duplicates?\b", work, re.IGNORECASE):
        spec.exact_duplicates_only = True
        spec.duplicates_only = True
        _add_filter("exact duplicates")
        work = re.sub(r"\bexact\s+duplicates?\b", " ", work, flags=re.IGNORECASE)
    elif re.search(r"\b(?:duplicates?|lookalikes?)\b", work, re.IGNORECASE):
        spec.duplicates_only = True
        _add_filter("duplicates")
        work = re.sub(r"\b(?:duplicates?|lookalikes?)\b", " ", work, flags=re.IGNORECASE)

    if re.search(r"\b(?:uncategorized|unsorted|quiet\s+tray)\b", work, re.IGNORECASE):
        spec.uncategorized_only = True
        _add_filter("Quiet Tray")
        work = re.sub(r"\b(?:on\s+(?:the\s+)?|in\s+(?:the\s+)?)?(?:uncategorized|unsorted|quiet\s+tray)\b", " ", work, flags=re.IGNORECASE)

    if re.search(r"\b(?:pinned|starred|favorites?)\b", work, re.IGNORECASE) and not spec.exclude_pinned:
        spec.pinned_only = True
        _add_filter("pinned")
        work = re.sub(r"\b(?:pinned|starred|favorites?)\b", " ", work, flags=re.IGNORECASE)

    if re.search(r"\b(?:unnamed|cryptic|messy)\b", work, re.IGNORECASE):
        spec.unnamed_only = True
        _add_filter("unnamed")
        work = re.sub(r"\b(?:unnamed|cryptic|messy)\b", " ", work, flags=re.IGNORECASE)

    # 11. Dynamic shelf hints ("on shelf Finance", "in the Receipts shelf", "from shelf Work")
    sh_pat = re.compile(
        r"\b(?:on|in|from)\s+(?:the\s+)?shelf\s+[\"“']?([a-z0-9 &_\-]{2,40}?)[\"”']?(?=$|\s+(?:and|or|with|that|except|without|larger|smaller|older)\b|[?.!,])|"
        r"\b(?:on|in|from)\s+(?:the\s+)?([a-z0-9 &_\-]{2,36}?)\s+shelf\b",
        re.IGNORECASE,
    )
    for m_sh in list(sh_pat.finditer(work)):
        sh_cand = (m_sh.group(1) or m_sh.group(2) or "").strip()
        if sh_cand:
            sh_low = sh_cand.lower()
            if sh_low in {"quiet tray", "uncategorized"}:
                spec.uncategorized_only = True
                _add_filter("Quiet Tray")
            else:
                spec.shelf_hints.append(sh_low)
                _add_filter(f"shelf: {known_shelves.get(sh_low, sh_cand)}")
            work = work[:m_sh.start()] + " " + work[m_sh.end():]

    # 12. Dynamic folder hints ("in Downloads", "from Finance", "inside Q1_Reports", "in my Work folder")
    fld_pat = re.compile(
        r"\b(?:inside|in|from)\s+(?:my\s+|the\s+)?(?:folder\s+(?:called|named)\s+)?([a-z0-9][a-z0-9 _.\-/]{1,42}?)(?:\s+folder)?(?=$|\s+(?:and|or|with|that|which|except|without|larger|bigger|smaller|older|over|under|due|sorted|top)\b|[?.!,;])",
        re.IGNORECASE,
    )
    for m_fl in list(fld_pat.finditer(work)):
        cand = m_fl.group(1).strip(" \"'.,")
        cand_low = cand.lower()
        # Check if it's a known folder name (exact or last path segment) or explicitly says "folder"
        last_seg = cand_low.rstrip("/").rsplit("/", 1)[-1]
        is_explicit_folder = "folder" in m_fl.group(0).lower()
        if is_explicit_folder or cand_low in known_folders or last_seg in known_folders:
            if cand_low not in _MONTH_MAP and not re.fullmatch(r"(?:19|20)\d{2}", cand_low):
                spec.folder_hints.append(cand_low)
                _add_filter(f"folder: {known_folders.get(cand_low, cand)}")
                work = work[:m_fl.start()] + " " + work[m_fl.end():]
        elif (
            "from " in m_fl.group(0).lower()
            and not spec.entity_filters.get("merchant")
            and re.search(r"\b(?:receipt|receipts|invoice|invoices|bill|bills|order|orders|payment|payments|spent|paid)\b", spec.raw_query, re.I)
            and cand_low not in _STOPWORDS
            and cand_low not in _MONTH_MAP
            and cand_low not in _TYPE_WORD_MAP
        ):
            # Natural-language merchant filter: "receipts from Starbucks", "invoices from Lazada"
            spec.entity_filters["merchant"] = cand_low
            _add_filter(f"merchant: {cand}")
            work = work[:m_fl.start()] + " " + work[m_fl.end():]

    # Also match standalone folder noun when user says e.g. "my stale downloads" or "organize downloads"
    if not spec.folder_hints:
        work_low_tmp = work.lower()
        if re.search(r"\b(?:my\s+)?downloads\b", work_low_tmp):
            spec.folder_hints.append("downloads")
            _add_filter("folder: downloads")
            work = re.sub(r"\bdownloads\b", " ", work, flags=re.IGNORECASE)

    # Natural-language merchant with "at <Merchant>" for finance queries ("spent at Starbucks")
    if not spec.entity_filters.get("merchant") and re.search(
        r"\b(?:receipt|receipts|invoice|invoices|spent|spend|paid|bought|purchase|bill|bills)\b",
        spec.raw_query,
        re.IGNORECASE,
    ):
        m_at = re.search(
            r"\b(?:at|from)\s+([A-Z][a-zA-Z0-9&'.\-]{1,28}(?:\s+[A-Z][a-zA-Z0-9&'.\-]{1,20})?)\b",
            work,
        )
        if m_at:
            merch = m_at.group(1).strip()
            if merch.lower() not in known_folders and merch.lower() not in _MONTH_MAP and merch.lower() not in _STOPWORDS:
                spec.entity_filters["merchant"] = merch.lower()
                _add_filter(f"merchant: {merch}")
                work = work[:m_at.start()] + " " + work[m_at.end():]

    # 13. Sorting & Top-K ("top 5 largest PDFs", "3 oldest screenshots", "first 10 files", "smallest files")
    m_topk = re.search(
        r"\b(?:top|first|last)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty)\b|"
        r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(?:largest|biggest|heaviest|smallest|newest|latest|most\s+recent|oldest|earliest)\b",
        work,
        re.IGNORECASE,
    )
    if m_topk:
        raw_num = (m_topk.group(1) or m_topk.group(2) or "").lower()
        k_val = int(raw_num) if raw_num.isdigit() else _NUMBER_WORDS.get(raw_num)
        if k_val and 1 <= k_val <= 200:
            spec.top_k = k_val
            _add_filter(f"top {k_val}")
            if m_topk.group(1):
                work = work[:m_topk.start()] + " " + work[m_topk.end():]
            else:
                # Only remove the leading number, keep the sort adjective for step below
                work = work[:m_topk.start(2)] + " " + work[m_topk.end(2):]

    low_orig = spec.raw_query.lower()
    if re.search(r"\b(?:largest|biggest|heaviest)\b", work, re.IGNORECASE):
        spec.sort_by = "largest"
        work = re.sub(r"\b(?:largest|biggest|heaviest)\b", " ", work, flags=re.IGNORECASE)
    elif re.search(r"\b(?:smallest|lightest|tiniest)\b", work, re.IGNORECASE):
        spec.sort_by = "smallest"
        work = re.sub(r"\b(?:smallest|lightest|tiniest)\b", " ", work, flags=re.IGNORECASE)
    elif re.search(r"\b(?:oldest|earliest)\b", work, re.IGNORECASE):
        spec.sort_by = "oldest"
        work = re.sub(r"\b(?:oldest|earliest)\b", " ", work, flags=re.IGNORECASE)
    elif re.search(r"\b(?:newest|latest|most\s+recent)\b", work, re.IGNORECASE):
        spec.sort_by = "newest"
        work = re.sub(r"\b(?:newest|latest|most\s+recent)\b", " ", work, flags=re.IGNORECASE)
    elif spec.due_window is not None:
        spec.sort_by = "due_date"

    # 14. Intent classification
    if spec.due_window is not None:
        spec.intent_mode = "due_soon"
    elif re.search(r"\b(?:how\s+many|count\s+(?:the|my|all)|number\s+of)\b", low_orig):
        spec.intent_mode = "count"
    elif re.search(r"\b(?:timeline|chronolog|what\s+did\s+i\s+(?:work\s+on|edit|modify|create|add|do)|activity\s+(?:in|from|last|this))\b", low_orig):
        spec.intent_mode = "timeline"
    elif re.search(r"\b(?:how\s+much|total\s+spent|spent\s+on|spending|expenses?\s+(?:summary|breakdown|total)|sum\s+of|receipts?\s+total|average\s+(?:receipt|spend|expense))\b", low_orig):
        spec.intent_mode = "finance"
    elif re.search(r"\b(?:summarize|summary|overview|key\s+points|takeaways)\b", low_orig):
        spec.intent_mode = "summarize"
    elif re.search(
        r"\b(?:explain|analyze|analyse|inspect|deep\s+dive(?:\s+into)?|understand|what\s+is\s+inside|context\s+of|file\s+context|tell\s+me\s+everything\s+about)\b",
        low_orig,
    ):
        spec.intent_mode = "deep_context"
    elif _EXPLICIT_LIST_TRIGGER.search(low_orig) or spec.top_k is not None:
        spec.intent_mode = "list"
    else:
        spec.intent_mode = "search"

    # 15. Multi-type & format extraction ("all images and PDFs", "Word docs or spreadsheets")
    work_tokens = re.findall(r"[a-z0-9]{2,}", work.lower())
    work_tok_set = set(work_tokens)

    # Handle multi-word compound type phrase "word docs" / "word documents"
    if re.search(r"\bword\s+(?:docs?|documents?)\b", work, re.IGNORECASE):
        spec.exts.update({"docx", "doc", "rtf", "odt"})
        _add_filter("type: word")
        work = re.sub(r"\bword\s+(?:docs?|documents?)\b", " ", work, flags=re.IGNORECASE)
        work_tokens = re.findall(r"[a-z0-9]{2,}", work.lower())
        work_tok_set = set(work_tokens)

    is_explicit_type_query = bool(
        spec.intent_mode in ("list", "count", "due_soon")
        or spec.temporal is not None
        or spec.camera_hint is not None
        or spec.min_size_bytes is not None
        or spec.max_size_bytes is not None
        or spec.empty_only
        or bool(spec.include_phrases)
        or bool(spec.folder_hints)
        or bool(spec.shelf_hints)
        or bool(spec.required_tags)
        or spec.untagged_only
        or spec.pinned_only
        or spec.exclude_pinned
        or spec.has_ocr is not None
        or spec.has_todos is not None
        or spec.duplicates_only
        or spec.sort_by is not None
        or spec.top_k is not None
        or re.search(
            r"\b(?:my|all|every|only|any|these|those|the)\s+(?:photos?|pictures?|images?|screenshots?|pdfs?|spreadsheets?|slides?|presentations?|videos?|audios?|recordings?|archives?|docs?|documents?|word|excel|csvs?|markdown|code|scripts?)\b",
            low_orig,
        )
        or re.search(
            r"\b(?:images?|photos?|pictures?|pdfs?|docs?|documents?|spreadsheets?|videos?|audios?|slides?|csvs?|markdown)\s+(?:and|or)\s+(?:images?|photos?|pictures?|pdfs?|docs?|documents?|spreadsheets?|videos?|audios?|slides?|csvs?|markdown)\b",
            low_orig,
        )
    )

    matched_type_words: set[str] = set()
    if is_explicit_type_query:
        for tok in work_tokens:
            if tok in _TYPE_WORD_MAP:
                # Guard "code" when query says "receipt code" or "zip code"
                if tok == "code" and (work_tok_set & {"receipt", "zip", "area", "promo", "qr", "bar"}):
                    continue
                # Guard "word" standalone unless accompanied by doc/file context
                if tok == "word" and not (work_tok_set & {"doc", "docs", "document", "documents", "file", "files", "or", "and"}):
                    continue
                mod, exts, sp_tag, lbl = _TYPE_WORD_MAP[tok]
                if mod:
                    spec.modalities.add(mod)
                if exts:
                    spec.exts.update(exts)
                if sp_tag and sp_tag not in spec.required_tags:
                    spec.required_tags.append(sp_tag)
                _add_filter(lbl)
                matched_type_words.add(tok)

        # If BOTH modalities and exts were extracted (e.g. "all images and PDFs"), treat them as UNION (OR)!
        if spec.modalities and spec.exts:
            spec.type_union = True

    # 16. Natural-language residual topic tokens
    deep_intent_filler = (
        {"explain", "analyze", "analyse", "inspect", "deep", "dive", "understand", "inside", "context", "tell", "everything", "about"}
        if spec.intent_mode == "deep_context"
        else set()
    )
    residual_tokens = [
        t for t in re.findall(r"[a-z0-9]{2,}", work.lower())
        if t not in _STOPWORDS
        and t not in matched_type_words
        and t not in deep_intent_filler
        and t not in {"pinned", "starred", "favorite", "favorites", "unnamed", "cryptic", "messy", "untagged", "empty", "stale"}
    ]
    # Remove bare "due" / "payments" filler when due_window is active and no specific topic remains
    if spec.due_window is not None:
        residual_tokens = [
            t for t in residual_tokens
            if t not in {"due", "payment", "payments", "bill", "bills", "upcoming", "next", "within", "days", "week", "month", "overdue"}
        ]
    spec.topic_query = " ".join(residual_tokens).strip()
    return spec


def load_full_texts_from_db(store: Any, file_ids: list[str]) -> dict[str, tuple[str, str]]:
    """
    Fetch FULL `extracted_text` and `ocr_text` directly from SQLite for `file_ids`,
    completely eliminating the 600-char truncation present in `state['files']`.
    """
    clean_ids = [str(fid) for fid in file_ids if fid]
    if not clean_ids or store is None:
        return {}
    out: dict[str, tuple[str, str]] = {}
    with store._lock:
        conn = store._connect()
        try:
            # Batch in chunks of 400 for SQLite bind-parameter safety
            for i in range(0, len(clean_ids), 400):
                batch = clean_ids[i : i + 400]
                ph = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT id, extracted_text, ocr_text FROM files WHERE id IN ({ph})",
                    batch,
                ).fetchall()
                for r in rows:
                    out[str(r["id"])] = (
                        str(r["extracted_text"] or ""),
                        str(r["ocr_text"] or ""),
                    )
        finally:
            conn.close()
    return out


def _get_meta_dict(f_row: dict[str, Any]) -> dict[str, Any]:
    meta_raw = f_row.get("metadata_json") if "metadata_json" in f_row else f_row.get("metadata")
    if isinstance(meta_raw, dict):
        return meta_raw
    if isinstance(meta_raw, str) and meta_raw.strip():
        try:
            obj = json.loads(meta_raw)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def row_matches_temporal(f_row: dict[str, Any], spec_t: dict[str, Any]) -> bool:
    """Check a file row against a temporal window using EXIF takenDate, created_date, and mtime."""
    meta_obj = _get_meta_dict(f_row)
    taken_date = str(meta_obj.get("takenDate") or "").strip()
    created_date = str(f_row.get("created_date") or f_row.get("createdDate") or "").strip()
    mtime_val = float(f_row.get("mtime") or 0.0)
    mtime_date = datetime.fromtimestamp(mtime_val).strftime("%Y-%m-%d") if mtime_val > 0 else ""

    s_date = str(spec_t["start_date"])
    e_date = str(spec_t["end_date"])
    s_ts = float(spec_t["start_ts"])
    e_ts = float(spec_t["end_ts"])

    primary_date = taken_date or created_date
    if spec_t.get("date_scope") == "taken" and primary_date:
        return s_date <= primary_date <= e_date

    # For "older than ..." / "stale", check mtime primarily (a file modified 40 days ago is stale)
    if str(spec_t.get("label") or "").startswith(("Older than", "Stale")):
        if mtime_val > 0:
            return s_ts <= mtime_val <= e_ts
        if primary_date:
            return s_date <= primary_date <= e_date
        return False

    if primary_date and s_date <= primary_date <= e_date:
        return True
    if mtime_val > 0 and s_ts <= mtime_val <= e_ts:
        return True
    if mtime_date and s_date <= mtime_date <= e_date:
        return True
    return False


def row_matches_due_window(f_row: dict[str, Any], due_win: dict[str, Any]) -> bool:
    """Match a file's extracted `due_date` entity against a future/overdue window."""
    meta = _get_meta_dict(f_row)
    due = str(meta.get("due_date") or "").strip()
    if not due:
        return False
    due_norm = due[:10]
    s_d = str(due_win["start_date"])
    e_d = str(due_win["end_date"])
    return s_d <= due_norm <= e_d


def matches_query_spec(
    f_row: dict[str, Any],
    spec: FileQuerySpec,
    full_text_map: Optional[dict[str, tuple[str, str]]] = None,
    duplicate_ids: Optional[set[str]] = None,
    exact_duplicate_ids: Optional[set[str]] = None,
) -> bool:
    """Evaluate ALL `FileQuerySpec` constraints against a file row (using full SQLite text when provided)."""
    fid = str(f_row.get("id") or "")
    mod_val = str(f_row.get("modality") or "").lower()
    ext_val = str(f_row.get("ext") or f_row.get("format") or "").lower().lstrip(".")

    # 1. Type / format filters (supporting OR union like "images and PDFs")
    if spec.modalities or spec.exts:
        if spec.type_union:
            if (mod_val not in spec.modalities) and (ext_val not in spec.exts):
                return False
        else:
            if spec.modalities and mod_val not in spec.modalities:
                return False
            if spec.exts and ext_val not in spec.exts:
                return False

    if mod_val in spec.exclude_modalities:
        return False
    if ext_val in spec.exclude_exts:
        return False

    # 2. Pin / unnamed / uncategorized / duplicates
    is_pinned = bool(f_row.get("pinned"))
    if spec.pinned_only and not is_pinned:
        return False
    if spec.exclude_pinned and is_pinned:
        return False
    if spec.unnamed_only and not (f_row.get("suggested_name") or f_row.get("suggestedName")):
        return False

    sid = str(f_row.get("shelf_id") or f_row.get("shelfId") or "").lower()
    sname = str(f_row.get("shelf_name") or f_row.get("shelfName") or "").lower()
    if spec.uncategorized_only and sid not in {"", "tray", "uncategorized"}:
        return False
    if spec.shelf_hints:
        if not any(sh in sname or sh in sid for sh in spec.shelf_hints):
            return False
    if spec.exclude_shelves:
        if any(sh in sname or sh in sid for sh in spec.exclude_shelves):
            return False

    if spec.exact_duplicates_only:
        if exact_duplicate_ids is not None and fid not in exact_duplicate_ids:
            return False
    elif spec.duplicates_only:
        if duplicate_ids is not None and fid not in duplicate_ids:
            return False

    # 3. Size constraints
    sb = int(f_row.get("size_bytes") if "size_bytes" in f_row else (f_row.get("sizeBytes") or 0))
    if spec.empty_only and sb != 0:
        return False
    if spec.min_size_bytes is not None and sb <= spec.min_size_bytes:
        return False
    if spec.max_size_bytes is not None and sb >= spec.max_size_bytes:
        return False

    # 4. Dynamic folder constraints
    folder_full = str(f_row.get("folder") or f_row.get("folderFull") or "").replace("\\", "/").lower()
    folder_short = str(f_row.get("folder_short") or f_row.get("folder") or "").replace("\\", "/").lower()
    folder_hay = f"{folder_full} {folder_short}"
    if spec.folder_hints:
        matched_f = False
        for fh in spec.folder_hints:
            fh_clean = fh.replace("\\", "/").strip("/")
            if (
                f"/{fh_clean}" in folder_full
                or folder_full.endswith(fh_clean)
                or fh_clean in folder_hay
            ):
                matched_f = True
                break
        if not matched_f:
            return False
    if spec.exclude_folders:
        for ef in spec.exclude_folders:
            if ef in folder_hay:
                return False

    # 5. Temporal & due windows
    if spec.temporal and not row_matches_temporal(f_row, spec.temporal):
        return False
    if spec.due_window and not row_matches_due_window(f_row, spec.due_window):
        return False

    # 6. Tags (list or JSON string)
    raw_tags = f_row.get("tags") if "tags" in f_row else f_row.get("tags_json")
    if isinstance(raw_tags, list):
        tags_list = [str(t).lower().strip() for t in raw_tags if str(t).strip()]
    elif isinstance(raw_tags, str) and raw_tags.strip():
        try:
            parsed_t = json.loads(raw_tags)
            tags_list = [str(t).lower().strip() for t in parsed_t if str(t).strip()] if isinstance(parsed_t, list) else []
        except Exception:
            tags_list = []
    else:
        tags_list = []
    tags_set = set(tags_list)

    if spec.untagged_only and tags_list:
        return False
    for req_tag in spec.required_tags:
        if req_tag == "screenshot":
            name_low = str(f_row.get("name") or "").lower()
            if "screenshot" not in tags_set and "screenshot" not in name_low and "screen" not in name_low:
                return False
        elif req_tag.lower() not in tags_set:
            return False
    for ex_tag in spec.exclude_tags:
        if ex_tag.lower() in tags_set:
            return False

    # 7. Full-text & OCR checks (bypassing 600-char truncation via full_text_map!)
    if full_text_map and fid in full_text_map:
        ext_txt, ocr_txt = full_text_map[fid]
    else:
        ext_txt = str(f_row.get("extracted_text") or f_row.get("extractedText") or "")
        ocr_txt = str(f_row.get("ocr_text") or f_row.get("ocrText") or "")

    if spec.has_ocr is False and ocr_txt.strip():
        return False
    if spec.has_ocr is True and not ocr_txt.strip():
        return False

    meta_dict = _get_meta_dict(f_row)
    if spec.has_todos is not None:
        has_td = bool(
            meta_dict.get("action_items")
            or meta_dict.get("todos")
            or re.search(r"\b(?:TODO|FIXME|ACTION\s+ITEM|NEXT\s+STEP)\b\s*[:\-]|(?:^|\n)\s*[-*•]\s*\[\s*\]", f"{ext_txt}\n{ocr_txt}", re.IGNORECASE)
        )
        if spec.has_todos != has_td:
            return False

    if spec.entity_filters and not matches_entity_filters(meta_dict, spec.entity_filters):
        return False

    meta_str = json.dumps(meta_dict, ensure_ascii=False) if meta_dict else str(f_row.get("metadata_json") or "")
    raw_content_hay = " ".join([
        str(f_row.get("title") or ""),
        str(f_row.get("name") or ""),
        str(f_row.get("original_name") or f_row.get("originalName") or ""),
        ext_txt,
        ocr_txt,
        " ".join(tags_list),
        meta_str,
    ])
    content_hay_norm = re.sub(r"\s+", " ", raw_content_hay.lower())

    if spec.camera_hint and spec.camera_hint not in meta_str.lower() and spec.camera_hint not in content_hay_norm:
        return False

    for phrase in spec.include_phrases:
        p_norm = re.sub(r"\s+", " ", phrase.lower().strip())
        if p_norm and p_norm not in content_hay_norm:
            return False

    excl_hay_norm = f"{content_hay_norm} {sname} {str(f_row.get('specimen') or f_row.get('specimen_type') or '').lower()}"
    for ex_term in spec.exclude_terms:
        ex_low = ex_term.lower()
        ex_stem = ex_low[:-1] if len(ex_low) > 4 and ex_low.endswith("s") and not ex_low.endswith("ss") else ex_low
        if ex_low in excl_hay_norm or ex_stem in excl_hay_norm:
            return False

    return True


def apply_sort_and_limit(files: list[dict[str, Any]], spec: FileQuerySpec) -> list[dict[str, Any]]:
    """Sort and apply top_k limit according to `spec`."""
    out = list(files)
    if spec.sort_by == "largest":
        out.sort(key=lambda f: (int(f.get("sizeBytes") or f.get("size_bytes") or 0), float(f.get("mtime") or 0)), reverse=True)
    elif spec.sort_by == "smallest":
        out.sort(key=lambda f: (int(f.get("sizeBytes") or f.get("size_bytes") or 0), float(f.get("mtime") or 0)))
    elif spec.sort_by == "oldest":
        out.sort(key=lambda f: (float(f.get("mtime") or 0), str(f.get("createdDate") or "")))
    elif spec.sort_by == "newest":
        out.sort(key=lambda f: (float(f.get("mtime") or 0), str(f.get("createdDate") or "")), reverse=True)
    elif spec.sort_by == "name":
        out.sort(key=lambda f: str(f.get("name") or "").lower())
    elif spec.sort_by == "due_date":
        out.sort(key=lambda f: str(_get_meta_dict(f).get("due_date") or "9999-12-31"))
    if spec.top_k is not None and spec.top_k > 0:
        out = out[: spec.top_k]
    return out
