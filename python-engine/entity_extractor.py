"""
Lightweight Entity Extractor for Good Shelves.

Extracts structured entities from OCR and document text:
  - Merchant / vendor names (receipts, invoices)
  - Monetary amounts (totals, subtotals)
  - Dates (transaction dates, due dates)
  - Invoice / reference numbers
  - Document types (invoice, receipt, statement, etc.)

100% offline, regex + heuristic based. No ML model required.
Results are stored in metadata_json and become filterable search fields:
  merchant:lazada, amount:>500, due:next-week, invoice:INV-2024, doctype:receipt
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


_CODE_FILE_EXTS = {
    ".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".sql", ".sh", ".html", ".htm", ".css", ".php", ".xml", ".vue",
    ".svelte", ".c", ".cpp", ".h", ".java", ".go", ".rb", ".kt", ".swift",
    ".ini", ".conf",
}

_MERCHANT_KEYWORDS = (
    "merchant", "vendor", "store", "sold by", "from", "supplier",
    "restaurant", "cafe", "shop", "market", "mart", "pharmacy", "biller",
    "billed by", "issued by", "payee", "paid to", "customer", "client",
)

_MERCHANT_BAD_TOKENS = (
    "import ", "pathlib", "def ", "function ", "class ", "return ",
    "const ", "export ", "the ", "that ", "this ", "walk-ins", "walk-in",
    "http://", "https://",
)

_CURRENCY_TOKEN_RE = r"(?:PHP|USD|EUR|GBP|JPY|AUD|CAD|SGD|KRW|INR|A\$|C\$|S\$|₱|\$|€|£|¥|₩|₹)"

_KNOWN_MERCHANTS: list[tuple[str, re.Pattern[str]]] = [
    ("Google Cloud", re.compile(r"\bgoogle\s+cloud(?:\s+platform)?\b|\bgcp\b", re.IGNORECASE)),
    ("AWS", re.compile(r"\bamazon\s+web\s+services\b|\baws\b", re.IGNORECASE)),
    ("Amazon", re.compile(r"\bamazon(?:\.com|\.co\.jp|\.co\.uk|\s+prime)?\b", re.IGNORECASE)),
    ("Microsoft Azure", re.compile(r"\b(?:microsoft\s+)?azure\b", re.IGNORECASE)),
    ("Microsoft", re.compile(r"\bmicrosoft\b", re.IGNORECASE)),
    ("Apple", re.compile(r"\bapple(?:\s+inc\.?|\s+store|\s+pay|\s+cloud|\s+icloud)?\b", re.IGNORECASE)),
    ("GitHub", re.compile(r"\bgithub(?:\s+inc\.?)?\b", re.IGNORECASE)),
    ("OpenAI", re.compile(r"\bopenai\b|\bchatgpt\b", re.IGNORECASE)),
    ("Anthropic", re.compile(r"\banthropic\b|\bclaude\b", re.IGNORECASE)),
    ("DigitalOcean", re.compile(r"\bdigital\s*ocean\b", re.IGNORECASE)),
    ("Cloudflare", re.compile(r"\bcloudflare\b", re.IGNORECASE)),
    ("CloudHost", re.compile(r"\bcloudhost(?:\s+gmbh|\s+io)?\b", re.IGNORECASE)),
    ("Supabase", re.compile(r"\bsupabase\b", re.IGNORECASE)),
    ("Slack", re.compile(r"\bslack(?:\s+technologies)?\b", re.IGNORECASE)),
    ("Zoom", re.compile(r"\bzoom(?:\s+video|\s+communications)?\b", re.IGNORECASE)),
    ("Atlassian", re.compile(r"\batlassian\b|\bjira\b", re.IGNORECASE)),
    ("JetBrains", re.compile(r"\bjetbrains\b", re.IGNORECASE)),
    ("Stripe", re.compile(r"\bstripe\b", re.IGNORECASE)),
    ("PayPal", re.compile(r"\bpaypal\b", re.IGNORECASE)),
    ("Wise", re.compile(r"\bwise(?:\s+transfer)?\b|\btransferwise\b", re.IGNORECASE)),
    ("Revolut", re.compile(r"\brevolut\b", re.IGNORECASE)),
    ("Lazada", re.compile(r"\blazada\b", re.IGNORECASE)),
    ("Shopee", re.compile(r"\bshopee(?:pay)?\b", re.IGNORECASE)),
    ("Grab", re.compile(r"\bgrab(?:\s+taxi|\s*food|\s+express|\s*car)?\b", re.IGNORECASE)),
    ("Uber", re.compile(r"\buber(?:\s+eats|\s+trip)?\b", re.IGNORECASE)),
    ("Starbucks", re.compile(r"\bstarbucks(?:\s+coffee|\s+reserve)?\b|\bsbux\b", re.IGNORECASE)),
    ("Jollibee", re.compile(r"\bjollibee\b", re.IGNORECASE)),
    ("McDonald's", re.compile(r"\bmcdonald'?s\b|\bmcdo\b", re.IGNORECASE)),
    ("Walmart", re.compile(r"\bwalmart\b", re.IGNORECASE)),
    ("Costco", re.compile(r"\bcostco\b", re.IGNORECASE)),
    ("Target", re.compile(r"\btarget\s+(?:store|corp|corporation)\b", re.IGNORECASE)),
    ("Mercury Drug", re.compile(r"\bmercury\s+drug(?:store)?\b", re.IGNORECASE)),
    ("Watsons", re.compile(r"\bwatsons\b", re.IGNORECASE)),
    ("Meralco", re.compile(r"\bmeralco\b|\bmanila\s+electric\b", re.IGNORECASE)),
    ("Maynilad", re.compile(r"\bmaynilad\b", re.IGNORECASE)),
    ("Manila Water", re.compile(r"\bmanila\s+water\b", re.IGNORECASE)),
    ("PLDT", re.compile(r"\bpldt\b", re.IGNORECASE)),
    ("Globe", re.compile(r"\bglobe\s+telecom\b|\bglobe\s+at\s+home\b|\bglobe\b", re.IGNORECASE)),
    ("Converge", re.compile(r"\bconverge\s+ict\b|\bconverge\s+fiberx?\b", re.IGNORECASE)),
    ("Cebu Pacific", re.compile(r"\bcebu\s+pacific\b|\bcebupacific\b", re.IGNORECASE)),
    ("Philippine Airlines", re.compile(r"\bphilippine\s+airlines\b", re.IGNORECASE)),
    ("AirAsia", re.compile(r"\bair\s*asia\b", re.IGNORECASE)),
    ("Airbnb", re.compile(r"\bairbnb\b", re.IGNORECASE)),
    ("Booking.com", re.compile(r"\bbooking\.com\b", re.IGNORECASE)),
    ("Agoda", re.compile(r"\bagoda\b", re.IGNORECASE)),
    ("BDO", re.compile(r"\bbdo\s+unibank\b|\bbanco\s+de\s+oro\b|\bbdo\b", re.IGNORECASE)),
    ("BPI", re.compile(r"\bbank\s+of\s+the\s+philippine\s+islands\b|\bbpi\b", re.IGNORECASE)),
    ("UnionBank", re.compile(r"\bunion\s*bank\b", re.IGNORECASE)),
    ("Metrobank", re.compile(r"\bmetrobank\b|\bmetropolitan\s+bank\b", re.IGNORECASE)),
    ("Chase", re.compile(r"\bjpmorgan\s+chase\b|\bchase\s+bank\b", re.IGNORECASE)),
    ("GCash", re.compile(r"\bgcash\b", re.IGNORECASE)),
    ("Maya", re.compile(r"\bpaymaya\b|\bmaya\s+bank\b|\bmaya\s+philippines\b", re.IGNORECASE)),
    ("Maxicare", re.compile(r"\bmaxicare\b", re.IGNORECASE)),
    ("Medicard", re.compile(r"\bmedicard\b", re.IGNORECASE)),
    ("Netflix", re.compile(r"\bnetflix\b", re.IGNORECASE)),
    ("Spotify", re.compile(r"\bspotify\b", re.IGNORECASE)),
    ("Adobe", re.compile(r"\badobe(?:\s+inc\.?|\s+systems|\s+creative\s+cloud)?\b", re.IGNORECASE)),
    ("Figma", re.compile(r"\bfigma\b", re.IGNORECASE)),
    ("Notion", re.compile(r"\bnotion\s+labs\b|\bnotion\b", re.IGNORECASE)),
    ("Vercel", re.compile(r"\bvercel\b", re.IGNORECASE)),
]

_MERCHANT_PATTERNS = [
    re.compile(
        rf"paid\s+{_CURRENCY_TOKEN_RE}?\s*[\d,.]+\s+to\s+([A-Za-z][A-Za-z0-9 &'.\-]{{2,36}}?)(?=\s*(?:[·|\n]|date|ref|item|$))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:merchant|vendor|store|sold\s+by|supplier|biller|billed\s+by|issued\s+by|payee|paid\s+to|carrier|airline|hotel|clinic|hospital|customer|client)\s*[:\-]\s*([^\n\r,·]{2,50})",
        re.IGNORECASE,
    ),
]

_AMOUNT_PATTERNS = [
    re.compile(rf"(?:total|amount\s+due|grand\s+total|balance\s+due|amount\s+paid)\s*[:\-]?\s*{_CURRENCY_TOKEN_RE}?\s*([0-9,]+\.?\d{{0,2}})", re.IGNORECASE),
    re.compile(rf"(?:subtotal|sub\s+total)\s*[:\-]?\s*{_CURRENCY_TOKEN_RE}?\s*([0-9,]+\.?\d{{0,2}})", re.IGNORECASE),
    re.compile(rf"(?:total)\s*{_CURRENCY_TOKEN_RE}?\s*([0-9,]+\.?\d{{0,2}})", re.IGNORECASE),
]

_CURRENCY_AMOUNT_PATTERN = re.compile(rf"{_CURRENCY_TOKEN_RE}\s*([0-9,]+\.\d{{2}})")

_TAX_AMOUNT_PATTERN = re.compile(
    rf"\b(?:vat|sales\s+tax|tax(?:\s+amount)?|gst)\s*(?:\(\s*\d+(?:\.\d+)?%\s*\))?\s*[:\-]?\s*{_CURRENCY_TOKEN_RE}?\s*([0-9,]+\.\d{{2}})\b",
    re.IGNORECASE,
)

_PAYMENT_METHOD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GCash", re.compile(r"\bgcash\b", re.IGNORECASE)),
    ("Maya", re.compile(r"\b(?:paymaya|maya)\b", re.IGNORECASE)),
    ("PayPal", re.compile(r"\bpaypal\b", re.IGNORECASE)),
    ("Visa", re.compile(r"\bvisa(?:\s+card|\s+ending|\s+\*{2,}|\s+credit|\s+debit)?\b", re.IGNORECASE)),
    ("Mastercard", re.compile(r"\bmastercard\b", re.IGNORECASE)),
    ("Bank Transfer", re.compile(r"\b(?:bank\s+transfer|wire\s+transfer|instapay|pesonet|ach\s+transfer)\b", re.IGNORECASE)),
    ("Credit Card", re.compile(r"\bcredit\s+card\b", re.IGNORECASE)),
    ("Debit Card", re.compile(r"\bdebit\s+card\b", re.IGNORECASE)),
    ("Check", re.compile(r"\b(?:paid\s+by\s+check|check\s*#|cheque)\b", re.IGNORECASE)),
    ("Cash", re.compile(r"\b(?:paid\s+(?:in\s+|via\s+|by\s+)?cash|payment\s+method\s*[:\-]?\s*cash|cash\s+tendered)\b", re.IGNORECASE)),
]

_AUTHOR_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:author|prepared\s+by|submitted\s+by|written\s+by|candidate|student(?:\s+name)?|attendees?|name)\s*[:\-]\s*([A-Z][A-Za-z0-9 .,'\-]{2,54})",
    re.IGNORECASE,
)

_ACTION_ITEM_PATTERNS = [
    re.compile(r"(?:^|\n)\s*(?:[-*•]\s*\[\s*\]\s*)(.+)", re.IGNORECASE),
    re.compile(r"\b(?:TODO|FIXME|ACTION\s+ITEM|NEXT\s+STEP|TASK)\s*[:\-]\s*([^\n\r]{3,140})", re.IGNORECASE),
]

_URL_PATTERN = re.compile(r"\bhttps?://[^\s<>\"')\]}]+", re.IGNORECASE)

_DATE_PATTERNS = [
    re.compile(r"(?:date|dated|transaction\s+date|issued)\s*[:\-]?\s*([0-9]{1,2}[/\-][0-9]{1,2}[/\-][0-9]{2,4})", re.IGNORECASE),
    re.compile(r"(?:date|dated)\s*[:\-]?\s*([A-Za-z]{3,9}\s+[0-9]{1,2},?\s+[0-9]{4})", re.IGNORECASE),
    re.compile(r"\b([0-9]{4}-[0-9]{2}-[0-9]{2})\b"),
]

_DUE_DATE_PATTERNS = [
    re.compile(r"(?:due\s+date|due|payment\s+due|expires)\s*[:\-]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.IGNORECASE),
    re.compile(r"(?:due\s+date|due|payment\s+due|expires)\s*[:\-]?\s*([0-9]{1,2}[/\-][0-9]{1,2}[/\-][0-9]{2,4})", re.IGNORECASE),
    re.compile(r"(?:due\s+date|due|expires)\s*[:\-]?\s*([A-Za-z]{3,9}\s+[0-9]{1,2},?\s+[0-9]{2,4})", re.IGNORECASE),
]

_INVOICE_PATTERNS = [
    re.compile(
        r"\b(?:invoice|inv|receipt|or|ref|reference|transaction)\s*(?:(?:no\.?|#|number|id)\s*[:\-]?|[:#])\s*([A-Z0-9][A-Z0-9\-]{3,24})\b",
        re.IGNORECASE,
    ),
]

_RECEIPT_EXPLICIT_RE = re.compile(r"\bofficial\s+receipt\b|\breceipt\s*(?:no\.?|#|number|:)", re.IGNORECASE)
_RECEIPT_WORD_RE = re.compile(r"\breceipt\b", re.IGNORECASE)
_RECEIPT_CONTEXT_RE = re.compile(r"\b(?:total|subtotal|paid|cashier|amount|change|php|usd|eur|gbp|jpy|aud|cad|sgd|krw|inr)\b|[₱\$€£¥₩₹]", re.IGNORECASE)

_INVOICE_EXPLICIT_RE = re.compile(r"\btax\s+invoice\b|\binvoice\s*(?:no\.?|#|number|date|:)", re.IGNORECASE)
_INVOICE_WORD_RE = re.compile(r"\binvoice\b", re.IGNORECASE)
_INVOICE_CONTEXT_RE = re.compile(r"\b(?:total|amount\s+due|due\s+date|bill\s+to)\b", re.IGNORECASE)

_DOCTYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("statement", re.compile(r"\b(?:bank\s+statement|billing\s+statement|statement\s+of\s+account|account\s+statement|credit\s+card\s+statement)\b", re.IGNORECASE)),
    ("tax_filing", re.compile(r"\b(?:bir\s+(?:form\s+)?2316|income\s+tax\s+return|tax\s+return|form\s+w-?2|form\s+1099|certificate\s+of\s+compensation\s+payment|withholding\s+tax\s+certificate)\b", re.IGNORECASE)),
    ("payslip", re.compile(r"\b(?:payslip|pay\s+stub|payroll\s+statement|net\s+pay[\s\S]{0,400}gross\s+pay)\b", re.IGNORECASE)),
    ("bill", re.compile(r"\b(?:utility\s+bill|electric\s+bill|water\s+bill|amount\s+due|billing\s+statement|bill\s+to)\b", re.IGNORECASE)),
    ("order", re.compile(r"\bpurchase\s+order\b|\border\s*(?:no\.?|#|number|id|summary|confirmation)\b", re.IGNORECASE)),
    ("ticket", re.compile(r"\b(?:boarding\s+pass|e-ticket|flight\s+ticket|flight\s+itinerary|flight\s+booking|airline\s+ticket|hotel\s+reservation|booking\s+reference|pnr\s*[:#]|event\s+ticket|concert\s+ticket|bus\s+ticket)\b", re.IGNORECASE)),
    ("medical_record", re.compile(r"\b(?:laboratory\s+report|complete\s+blood\s+count|cbc\s+result|lipid\s+profile|urinalysis|medical\s+certificate|medical\s+abstract|discharge\s+summary|diagnostic\s+imaging|attending\s+physician)\b", re.IGNORECASE)),
    ("prescription", re.compile(r"\bprescription\b|\brx\s*(?:no\.?|#|:)\b", re.IGNORECASE)),
    ("insurance", re.compile(r"\b(?:insurance\s+policy|policy\s+number|beneficiary|hmo\s+coverage|health\s+insurance)\b", re.IGNORECASE)),
    ("certificate", re.compile(r"\bcertificate\s+of\b|\bofficial\s+certificate\b|\bcertificate\b", re.IGNORECASE)),
    ("contract", re.compile(r"\b(?:contract|master\s+service\s+agreement|service\s+agreement|lease\s+agreement|employment\s+agreement|non-disclosure\s+agreement|mutual\s+nda|memorandum\s+of\s+agreement|offer\s+letter)\b", re.IGNORECASE)),
    ("resume", re.compile(r"\b(?:curriculum\s+vitae|resume)\b|(?:\bwork\s+experience\b[\s\S]{0,1200}\beducation\b[\s\S]{0,1200}\bskills\b)", re.IGNORECASE)),
    ("syllabus", re.compile(r"\b(?:course\s+syllabus|course\s+outline|grading\s+system|learning\s+objectives)\b", re.IGNORECASE)),
    ("paper", re.compile(r"\b(?:arxiv|imrad)\b|\bdoi\s*:\s*10\.\d{4,}|(?:\babstract\b[\s\S]{0,2000}\bintroduction\b[\s\S]{0,4000}\b(?:methodology|references)\b)", re.IGNORECASE)),
    ("meeting_notes", re.compile(r"\b(?:meeting\s+minutes|meeting\s+notes|minutes\s+of\s+meeting|sync\s+notes)\b|(?:\b(?:attendees|agenda)\b[\s\S]{0,1200}\baction\s+items\b)", re.IGNORECASE)),
    ("id_card", re.compile(r"\b(?:republic\s+of\s+the\s+philippines|driver'?s?\s+license|national\s+id|philid|student\s+id|passport\s+no)\b", re.IGNORECASE)),
    ("transcript", re.compile(r"\b(?:official\s+transcript|transcript\s+of\s+records|interview\s+transcript|academic\s+transcript)\b", re.IGNORECASE)),
    ("study_guide", re.compile(r"\b(?:study\s+guide|lecture\s+notes|exam\s+reviewer)\b", re.IGNORECASE)),
    ("manual", re.compile(r"\b(?:user\s+manual|instruction\s+manual|installation\s+guide|reference\s+manual|operations\s+manual)\b", re.IGNORECASE)),
    ("report", re.compile(r"\b(?:annual|quarterly|technical|financial|lab|progress|audit|incident)\s+report\b", re.IGNORECASE)),
]

_RECEIPT_HEADER_SKIP_PREFIXES = (
    "paid ", "date", "time", "ref", "total", "subtotal", "sub total", "tax", "vat",
    "official receipt", "receipt", "invoice", "cashier", "change", "cash", "amount",
    "thank you", "customer", "table ", "order ", "tel", "phone", "tin", "gcash",
)


def _is_valid_merchant_candidate(cand: str) -> bool:
    s = cand.strip(" ·-.,:;|")
    if len(s) < 2 or len(s) > 52:
        return False
    low = s.lower()
    if low in _MERCHANT_KEYWORDS:
        return False
    if any(bad in low for bad in _MERCHANT_BAD_TOKENS):
        return False
    if not any(c.isalpha() for c in s):
        return False
    if s.startswith(("{", "[", "<", "//", "/*", "#!")):
        return False
    return True


def _extract_receipt_header_merchant(text: str) -> Optional[str]:
    for seg in re.split(r"[·|\n\r]", text or "")[:4]:
        s = seg.strip(" ·-.,:;|*#")
        low = s.lower()
        if not (3 <= len(s) <= 44):
            continue
        if any(low.startswith(p) for p in _RECEIPT_HEADER_SKIP_PREFIXES):
            continue
        if re.search(r"^\d+[\d\s/\-:.,]*$", s):
            continue
        if _is_valid_merchant_candidate(s):
            return s
    return None

_PHONE_PATTERN = re.compile(r"\b(?:\+?63|0)\d{2,3}[\- ]?\d{3,4}[\- ]?\d{4}\b")
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_TIN_PATTERN = re.compile(r"\b\d{3}[\- ]?\d{3}[\- ]?\d{3}[\- ]?\d{3}\b")


def _parse_amount(raw: str) -> Optional[float]:
    try:
        cleaned = raw.replace(",", "").strip()
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _normalize_date(raw: str) -> Optional[str]:
    s = raw.strip().replace(",", "")
    for fmt in (
        "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
        "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y",
        "%m/%d/%y", "%m-%d-%y", "%d/%m/%y", "%d-%m-%y",
        "%b %d %y", "%B %d %y", "%d %b %y", "%d %B %y",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _is_future_date(date_str: str) -> bool:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d > datetime.now()
    except ValueError:
        return False


def extract_entities(text: str, filename: str = "", existing_metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """
    Extract structured entities from OCR / extracted text.

    Returns a dict with keys:
      merchant, amount, date, due_date, invoice_number, doc_type,
      phone, email, tin, currency
    """
    result: dict[str, Any] = {}
    if existing_metadata:
        result.update(existing_metadata)

    combined = f"{filename}\n{text or ''}"
    if not combined.strip():
        return result

    ext = Path(filename).suffix.lower() if filename else ""
    is_code_file = (
        ext in _CODE_FILE_EXTS
        or str(result.get("kind") or "").lower() == "code"
        or bool(result.get("codeSymbols"))
    )

    rec_fields = result.get("receiptFields") if isinstance(result.get("receiptFields"), dict) else {}
    if not is_code_file and "merchant" not in result:
        for pat in _MERCHANT_PATTERNS:
            m = pat.search(text or "")
            if m:
                merchant = m.group(1).strip(" ·-.,:;|")
                if _is_valid_merchant_candidate(merchant):
                    result["merchant"] = merchant
                    break

    if not is_code_file and "merchant" not in result and rec_fields and rec_fields.get("merchant"):
        cand_m = str(rec_fields["merchant"]).strip()
        if _is_valid_merchant_candidate(cand_m) and not re.search(
            r"\b(?:itinerary|boarding\s+pass|bir\s+form|certificate|laboratory\s+report|curriculum\s+vitae|resume|agreement)\b",
            cand_m,
            re.IGNORECASE,
        ):
            result["merchant"] = cand_m

    for pat in _AMOUNT_PATTERNS:
        m = pat.search(text or "")
        if m:
            amt = _parse_amount(m.group(1))
            if amt is not None and amt > 0:
                result["amount"] = amt
                break

    if "amount" not in result and not is_code_file:
        for m in _CURRENCY_AMOUNT_PATTERN.finditer(text or ""):
            amt = _parse_amount(m.group(1))
            if amt is not None and amt > 0:
                result["amount"] = amt
                break

    if not is_code_file and "tax_amount" not in result:
        m_tax = _TAX_AMOUNT_PATTERN.search(text or "")
        if m_tax:
            t_amt = _parse_amount(m_tax.group(1))
            if t_amt is not None and t_amt > 0:
                result["tax_amount"] = t_amt

    text_raw = text or ""
    text_up = text_raw.upper()
    detected_currency = ""
    if "₱" in text_raw or re.search(r"\bPHP\b", text_up):
        detected_currency = "PHP"
    elif "€" in text_raw or re.search(r"\bEUR\b", text_up):
        detected_currency = "EUR"
    elif "£" in text_raw or re.search(r"\bGBP\b", text_up):
        detected_currency = "GBP"
    elif "¥" in text_raw or re.search(r"\bJPY\b", text_up):
        detected_currency = "JPY"
    elif "A$" in text_raw or re.search(r"\bAUD\b", text_up):
        detected_currency = "AUD"
    elif "C$" in text_raw or re.search(r"\bCAD\b", text_up):
        detected_currency = "CAD"
    elif "S$" in text_raw or re.search(r"\bSGD\b", text_up):
        detected_currency = "SGD"
    elif "₩" in text_raw or re.search(r"\bKRW\b", text_up):
        detected_currency = "KRW"
    elif "₹" in text_raw or re.search(r"\bINR\b", text_up):
        detected_currency = "INR"
    elif ("$" in text_raw and not is_code_file) or re.search(r"\bUSD\b", text_up):
        detected_currency = "USD"

    if detected_currency:
        result["currency"] = detected_currency
    elif "currency" not in result:
        result["currency"] = ""

    for pat in _DATE_PATTERNS:
        m = pat.search(text or "")
        if m:
            normalized = _normalize_date(m.group(1))
            if normalized:
                result["date"] = normalized
                break

    for pat in _DUE_DATE_PATTERNS:
        m = pat.search(text or "")
        if m:
            normalized = _normalize_date(m.group(1))
            if normalized:
                result["due_date"] = normalized
                break

    if not is_code_file:
        for pat in _INVOICE_PATTERNS:
            for m in pat.finditer(text or ""):
                inv = m.group(1).strip("-")
                if len(inv) >= 4 and any(c.isdigit() for c in inv):
                    result["invoice_number"] = inv
                    break
            if "invoice_number" in result:
                break
        if "invoice_number" not in result and rec_fields and rec_fields.get("reference"):
            ref_cand = str(rec_fields["reference"]).strip("-")
            if len(ref_cand) >= 4 and any(c.isdigit() for c in ref_cand):
                result["invoice_number"] = ref_cand

    if not is_code_file:
        fname_low = (filename or "").lower()
        text_str = text or ""
        is_interview_or_transcript = any(k in fname_low for k in ("transcript", "interview", "syllabus", "study_guide", "lecture_notes"))
        # Check high-specificity non-receipt document types first so tax forms, flights, and lab reports
        # aren't misclassified as generic receipts just because they contain a currency total.
        _HIGH_PRIORITY_DOCTYPES = {"tax_filing", "payslip", "ticket", "medical_record", "prescription", "insurance", "resume"}
        for doc_type, pat in _DOCTYPE_PATTERNS:
            if doc_type in _HIGH_PRIORITY_DOCTYPES and pat.search(combined):
                result["doc_type"] = doc_type
                break
        if "doc_type" in result:
            pass
        elif not is_interview_or_transcript and (
            "receipt" in fname_low
            or bool(rec_fields)
            or _RECEIPT_EXPLICIT_RE.search(combined)
            or (_RECEIPT_WORD_RE.search(text_str) and _RECEIPT_CONTEXT_RE.search(text_str))
        ):
            result["doc_type"] = "receipt"
        elif not is_interview_or_transcript and (
            "invoice" in fname_low
            or _INVOICE_EXPLICIT_RE.search(combined)
            or (_INVOICE_WORD_RE.search(text_str) and _INVOICE_CONTEXT_RE.search(text_str))
        ):
            result["doc_type"] = "invoice"
        else:
            for doc_type, pat in _DOCTYPE_PATTERNS:
                if pat.search(combined):
                    result["doc_type"] = doc_type
                    break
            if "doc_type" not in result:
                if "syllabus" in fname_low:
                    result["doc_type"] = "syllabus"
                elif "transcript" in fname_low:
                    result["doc_type"] = "transcript"
                elif "study_guide" in fname_low or "lecture_notes" in fname_low:
                    result["doc_type"] = "study_guide"
                elif "resume" in fname_low or "cv" in re.findall(r"[a-z0-9]+", fname_low):
                    result["doc_type"] = "resume"
                elif "manual" in fname_low:
                    result["doc_type"] = "manual"

        is_receipt_like = (
            result.get("doc_type") in {"receipt", "invoice", "bill", "statement", "order", "ticket"}
            or bool(rec_fields)
            or any(k in fname_low for k in ("receipt", "invoice", "bill", "statement", "order"))
            or bool(_RECEIPT_EXPLICIT_RE.search(combined))
            or (
                "amount" in result
                and any(
                    k in text_str.lower()
                    for k in ("subtotal", "cashier", "amount due", "official receipt", "change", "vat", "paid", "total", "billing", "subscription")
                )
            )
        )
        if "merchant" not in result and (is_receipt_like or "amount" in result):
            for canonical_brand, brand_pat in _KNOWN_MERCHANTS:
                if brand_pat.search(combined):
                    result["merchant"] = canonical_brand
                    break
        if "merchant" not in result and is_receipt_like:
            header_m = _extract_receipt_header_merchant(text_str)
            if header_m:
                result["merchant"] = header_m

        if "payment_method" not in result:
            for pm_label, pm_pat in _PAYMENT_METHOD_PATTERNS:
                if pm_pat.search(text_str):
                    if pm_label in {"Visa", "Mastercard"} and not (is_receipt_like or "amount" in result or "paid" in text_str.lower() or "payment" in text_str.lower()):
                        continue
                    result["payment_method"] = pm_label
                    break

        if "author_or_person" not in result:
            m_auth = _AUTHOR_PATTERN.search(text_str)
            if m_auth:
                cand_a = m_auth.group(1).strip(" ·-.,:;|")
                if 2 <= len(cand_a) <= 54 and not any(b in cand_a.lower() for b in _MERCHANT_BAD_TOKENS):
                    result["author_or_person"] = cand_a

    # Extract action items / TODOs (up to 5)
    action_items: list[str] = list(result.get("action_items") or [])
    for pat in _ACTION_ITEM_PATTERNS:
        for m_act in pat.finditer(text or ""):
            item_txt = re.sub(r"\s+", " ", m_act.group(1)).strip(" ·-.,:;|")
            if 3 <= len(item_txt) <= 140 and item_txt not in action_items:
                action_items.append(item_txt)
                if len(action_items) >= 5:
                    break
        if len(action_items) >= 5:
            break
    if action_items:
        result["action_items"] = action_items[:5]

    # Extract URLs (up to 5)
    urls: list[str] = list(result.get("urls") or [])
    for m_url in _URL_PATTERN.finditer(text or ""):
        u = m_url.group(0).rstrip(".,;:!?)")
        if u and u not in urls:
            urls.append(u)
            if len(urls) >= 5:
                break
    if urls:
        result["urls"] = urls[:5]

    m_phone = _PHONE_PATTERN.search(text or "")
    if m_phone:
        result["phone"] = m_phone.group(0)

    m_email = _EMAIL_PATTERN.search(text or "")
    if m_email:
        result["email"] = m_email.group(0)

    m_tin = _TIN_PATTERN.search(text or "")
    if m_tin:
        result["tin"] = m_tin.group(0)

    return result


def entity_filters_from_query(query: str) -> dict[str, Any]:
    """
    Parse entity-based search operators from a natural query:
      merchant:lazada, amount:>500, amount:<1000, date:2024-03-15,
      due:next-week, invoice:INV-2024, doctype:receipt, currency:PHP,
      payment:gcash, author:alice, person:alice, todo:review, url:github.com
    """
    filters: dict[str, Any] = {}
    tokens = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')+', query or "")

    for token in tokens:
        if ":" not in token:
            continue
        key, val = token.split(":", 1)
        k = key.lower().strip()
        v = val.strip().strip('"\'')
        if not v:
            continue

        if k == "merchant":
            filters["merchant"] = v.lower()
        elif k == "amount":
            if v.startswith(">=") or v.startswith("<="):
                filters["amount_op"] = v[:2]
                filters["amount_value"] = _parse_amount(v[2:])
            elif v.startswith(">") or v.startswith("<"):
                filters["amount_op"] = ">" if v.startswith(">") else "<"
                filters["amount_value"] = _parse_amount(v.lstrip("<>"))
            else:
                filters["amount_value"] = _parse_amount(v)
        elif k == "date":
            filters["date"] = v
        elif k == "due":
            filters["due_date"] = v
        elif k == "invoice":
            filters["invoice_number"] = v.lower()
        elif k == "doctype":
            filters["doc_type"] = v.lower()
        elif k == "phone":
            filters["phone"] = v
        elif k == "email":
            filters["email"] = v.lower()
        elif k == "currency":
            filters["currency"] = v.upper()
        elif k == "payment":
            filters["payment_method"] = v.lower()
        elif k in {"author", "person"}:
            filters["author_or_person"] = v.lower()
        elif k == "todo":
            filters["todo"] = v.lower()
        elif k == "url":
            filters["url"] = v.lower()

    return filters


_ENTITY_KEYS = {
    "merchant", "amount", "date", "due", "invoice", "doctype", "phone", "email",
    "currency", "payment", "author", "person", "todo", "url",
}


def strip_entity_operators(query: str) -> str:
    """Remove entity operator tokens (e.g. merchant:lazada, amount:>500) from a query."""
    tokens = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')+', query or "")
    kept: list[str] = []
    for token in tokens:
        if ":" in token:
            key = token.split(":", 1)[0].lower().strip()
            if key in _ENTITY_KEYS:
                continue
        kept.append(token)
    return " ".join(kept).strip()


def matches_entity_filters(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Check if a file's extracted entities match the parsed entity filters."""
    if not filters:
        return True

    if "merchant" in filters:
        m = str(metadata.get("merchant", "")).lower()
        if filters["merchant"] not in m:
            return False

    if "amount_value" in filters:
        target_amt = filters.get("amount_value")
        if target_amt is None:
            return False
        amt = metadata.get("amount")
        if amt is None:
            return False
        try:
            amt_val = float(amt)
            target_val = float(target_amt)
        except (ValueError, TypeError):
            return False
        op = filters.get("amount_op")
        if op == ">":
            if amt_val <= target_val:
                return False
        elif op == ">=":
            if amt_val < target_val:
                return False
        elif op == "<":
            if amt_val >= target_val:
                return False
        elif op == "<=":
            if amt_val > target_val:
                return False
        else:
            if abs(amt_val - target_val) > 0.01:
                return False

    if "date" in filters:
        d = str(metadata.get("date", ""))
        if filters["date"] not in d:
            return False

    if "due_date" in filters:
        dd = str(metadata.get("due_date", "")).strip()
        due_filter = str(filters["due_date"]).strip().lower()
        if due_filter in {"today", "overdue", "next-week", "soon", "week", "month", "next-month"}:
            if not dd:
                return False
            try:
                due_d = datetime.strptime(dd[:10], "%Y-%m-%d").date()
            except ValueError:
                return False
            today = datetime.now().date()
            if due_filter == "today" and due_d != today:
                return False
            elif due_filter == "overdue" and due_d >= today:
                return False
            elif due_filter in {"next-week", "soon", "week"} and not (today <= due_d <= today + timedelta(days=7)):
                return False
            elif due_filter in {"month", "next-month"} and not (today <= due_d <= today + timedelta(days=30)):
                return False
        elif filters["due_date"] not in dd:
            return False

    if "invoice_number" in filters:
        inv = str(metadata.get("invoice_number", "")).lower()
        if filters["invoice_number"] not in inv:
            return False

    if "doc_type" in filters:
        dt = str(metadata.get("doc_type", "")).lower()
        if not dt or not filters["doc_type"] or filters["doc_type"] not in dt:
            return False

    if "phone" in filters:
        p = str(metadata.get("phone", ""))
        p_digits = re.sub(r"\D", "", p)
        f_digits = re.sub(r"\D", "", str(filters["phone"]))
        if f_digits:
            if f_digits not in p_digits:
                return False
        elif str(filters["phone"]) not in p:
            return False

    if "email" in filters:
        em = str(metadata.get("email", "")).lower()
        if filters["email"] not in em:
            return False

    if "currency" in filters:
        cur = str(metadata.get("currency", "")).upper()
        if filters["currency"].upper() != cur:
            return False

    if "payment_method" in filters:
        pm = str(metadata.get("payment_method", "")).lower()
        if filters["payment_method"] not in pm:
            return False

    if "author_or_person" in filters:
        ap = str(metadata.get("author_or_person", "")).lower()
        if filters["author_or_person"] not in ap:
            return False

    if "todo" in filters:
        todos_list = list(metadata.get("action_items") or []) + list(metadata.get("todos") or [])
        todos_hay = " ".join(str(x).lower() for x in todos_list)
        if filters["todo"] not in todos_hay:
            return False

    if "url" in filters:
        urls_list = list(metadata.get("urls") or [])
        urls_hay = " ".join(str(x).lower() for x in urls_list)
        if filters["url"] not in urls_hay:
            return False

    return True