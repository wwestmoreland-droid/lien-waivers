"""Fixed email templates with merge fields (section 6A). AI never writes or rewrites email text.

Template files live in templates/email/. Edits made on the Settings page are stored in the
database (Railway's disk is replaced on every deploy) and win over the file.

Rules applied when rendering:
- "[if name] ..." lines appear only when `name` is truthy.
- A sentence whose merge field is empty is left out instead of printing a blank.
- An empty first name: "Hi {first_name}," becomes "Hi," and elsewhere the name is dropped.
- Amounts render as $12,450.00 and dates as Oct 22, 2026.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import BASE_DIR

EMAIL_DIR = BASE_DIR / "templates" / "email"

TEMPLATES = {
    "partial": ("01_partial.txt", "Partial waiver (after payment)"),
    "conditional_final": ("02_conditional_final.txt", "Conditional final waiver (before final payment)"),
    "unconditional_final": ("03_unconditional_final.txt", "Unconditional final waiver (after final payment)"),
    "reminder_1": ("04_reminder_1.txt", "First reminder (day 7 by default)"),
    "reminder_2": ("05_reminder_2.txt", "Second reminder (day 10 by default)"),
    "escalation": ("06_escalation.txt", "Escalation to PM and estimator (day 13 by default, internal)"),
    "correction": ("07_correction.txt", "Needs correction"),
    "received": ("08_received.txt", "Received (optional)"),
    "supplier_list": ("09_supplier_list.txt", "Supplier list request (major trades)"),
    "cancelled": ("10_cancelled.txt", "Waiver cancelled (please disregard)"),
}

BANNED_PHRASES = [
    "I hope this email finds you well",
    "I wanted to reach out",
    "Just circling back",
    "Please don't hesitate to reach out",
    "At your earliest convenience",
    "Thank you for your prompt attention",
    "Kindly",
    "Please be advised",
    "As per",
    "We appreciate your partnership",
    "Should you have any questions",
]

FIELD_RE = re.compile(r"\{([a-z_]+)\}")
IF_RE = re.compile(r"^\[if ([a-z_]+)\]\s*")
SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+")
NAME_FIELDS = ("first_name", "pm_first_name")


def money(v) -> str:
    if v is None or v == "":
        return ""
    return f"${Decimal(v):,.2f}"


def fdate(v) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        v = v.date()
    if isinstance(v, date):
        return v.strftime("%b %-d, %Y")
    return str(v)


def first_name(full: str | None) -> str:
    if not full:
        return ""
    name = full.strip().split()[0]
    # Skip things that are obviously not a person's name ("AP", "Accounts", "Billing").
    if name.lower() in {"ap", "accounts", "accounting", "billing", "office", "admin", "info"} or "@" in name:
        return ""
    return name.strip(",")


def template_text(db: Session | None, key: str) -> str:
    if db is not None:
        from app.services.settings_store import get_setting
        override = (get_setting(db, "email_templates") or {}).get(key)
        if override:
            return override
    return (EMAIL_DIR / TEMPLATES[key][0]).read_text()


def file_text(key: str) -> str:
    return (EMAIL_DIR / TEMPLATES[key][0]).read_text()


@dataclass
class Rendered:
    subject: str | None
    body: str


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return money(v)
    if isinstance(v, (date, datetime)):
        return fdate(v)
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x)
    return str(v).strip()


def _fill(line: str, values: dict) -> str | None:
    """Fill one line; drop sentences with an empty field. None means drop the whole line."""
    stripped = line.strip()
    for name in NAME_FIELDS:
        if not _fmt(values.get(name)):
            if stripped in (f"Hi {{{name}}},", f"{{{name}}},"):
                return "Hi,"
            line = line.replace(" {" + name + "}", "").replace("{" + name + "}", "")
    if not FIELD_RE.search(line):
        return line
    # In a list of fields ("{name}, {email}, {phone}") drop just the empty items.
    for f in set(FIELD_RE.findall(line)):
        if not _fmt(values.get(f)):
            line = re.sub(r"(\}),\s*\{" + f + r"\}", r"\1", line)
            line = re.sub(r"\{" + f + r"\},\s*(?=\{)", "", line)
    kept = []
    for sentence in SENTENCE_SPLIT.split(line):
        fields = FIELD_RE.findall(sentence)
        if any(not _fmt(values.get(f)) for f in fields):
            continue
        kept.append(FIELD_RE.sub(lambda m: _fmt(values.get(m.group(1))), sentence))
    if not kept:
        return None
    return " ".join(kept)


def render_text(text: str, values: dict) -> Rendered:
    lines = text.splitlines()
    subject = None
    if lines and lines[0].startswith("Subject:"):
        subject_line = lines[0][len("Subject:"):].strip()
        subject = FIELD_RE.sub(lambda m: _fmt(values.get(m.group(1))), subject_line)
        subject = re.sub(r"\s+–\s+(?=–|\[|$)", " ", subject).strip()  # tidy if a field was empty
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    out: list[str] = []
    for line in lines:
        m = IF_RE.match(line)
        if m:
            if not values.get(m.group(1)):
                continue
            line = line[m.end():]
        if line.strip() == "{signature}" and not _fmt(values.get("signature")):
            continue
        filled = _fill(line, values)
        if filled is None:
            continue
        out.append(filled.rstrip())
    body = "\n".join(out)
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return Rendered(subject=subject, body=body)


def render(db: Session | None, key: str, values: dict) -> Rendered:
    return render_text(template_text(db, key), values)


def lint(text: str) -> list[str]:
    """Problems with a template's wording, per the writing rules in section 6A."""
    problems = []
    body = "\n".join(line for line in text.splitlines() if not line.startswith("Subject:"))
    lower = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase.lower() in lower:
            problems.append(f'Uses the banned phrase "{phrase}".')
    if "—" in body:
        problems.append("Has an em dash in the body.")
    if "!" in body:
        problems.append("Has an exclamation point.")
    if "**" in body or "__" in body:
        problems.append("Uses bold formatting.")
    if re.search(r"[\U0001F300-\U0001FAFF☀-➿]", body):
        problems.append("Has an emoji.")
    return problems
