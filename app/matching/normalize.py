"""Vendor name and value normalization."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Legal-form suffixes that don't distinguish one vendor from another.
_SUFFIXES = {
    "inc", "incorporated", "llc", "l l c", "co", "company", "corp", "corporation",
    "ltd", "limited", "lp", "llp", "pllc", "pc",
}


def normalize_vendor(name: str | None) -> str:
    """Lower-case, strip punctuation and legal suffixes.

    "Palatine Builders Supply, inc." and "Palatine Builders Supply, Inc." and
    "Palatine Builders Supply" all become "palatine builders supply".
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = s.encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    words = s.split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    # "l l c" split by punctuation ("L.L.C.")
    joined = " ".join(words)
    for suf in (" l l c", " l l p"):
        if joined.endswith(suf):
            joined = joined[: -len(suf)]
    return joined.strip()


def split_vendor_project(raw: str | None, project_name: str | None,
                         other_project_names: list[str] | tuple = ()) -> str:
    """Return the vendor part of a "Vendor - Project name" string.

    Cuts the row's project name off the end instead of splitting at the first hyphen,
    so vendor names that contain a hyphen survive. A near match is accepted for the
    row's own project (e.g. the "CCDPC" typo for "CCPDC"). If nothing matches, the whole
    string is the vendor name (AR/AP has many rows with no project suffix).
    """
    from rapidfuzz import fuzz

    if not raw:
        return ""
    s = raw.strip()
    names = [project_name] if project_name else []
    for pname in names + sorted((p for p in other_project_names if p), key=len, reverse=True):
        suffix = " - " + pname.strip()
        if s.lower().endswith(suffix.lower()):
            return s[: -len(suffix)].strip()
    if project_name:
        target = normalize_vendor(project_name)
        idx = s.find(" - ")
        while idx != -1:
            tail = s[idx + 3:]
            if fuzz.ratio(normalize_vendor(tail), target) >= 90:
                return s[:idx].strip()
            idx = s.find(" - ", idx + 1)
    return s


def state_from_project_number(number: str | None) -> str | None:
    """Characters 4-5 of the project number, e.g. P--CA-012425-1 -> CA."""
    if not number or len(number) < 5:
        return None
    st = number[3:5].upper()
    return st if st.isalpha() else None


def clean_invoice_no(value) -> str | None:
    """Smartsheet returns numeric invoice numbers as floats ("134822.0")."""
    if value is None or value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    s = str(value).strip()
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s or None


def to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    s = str(value).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def to_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def to_datetime(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        d = to_date(value)
        return datetime(d.year, d.month, d.day) if d else None


_RETENTION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*retention", re.I)


def retainage_pct_from_terms(terms: str | None) -> Decimal | None:
    """"Net 30 with 10% Retention" -> 10."""
    if not terms:
        return None
    m = _RETENTION_RE.search(terms)
    return Decimal(m.group(1)) if m else None


def looks_like_retention_invoice(*texts: str | None) -> bool:
    return any(t and re.search(r"retention|retainage", t, re.I) for t in texts)


def text(value) -> str | None:
    """Flatten a cell value to a trimmed string (contacts -> name)."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("name") or value.get("email")
    if isinstance(value, list):
        return ", ".join(filter(None, (text(v) for v in value))) or None
    s = str(value).strip()
    return s or None
