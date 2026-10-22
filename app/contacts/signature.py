"""Pull phone, mobile, title and company out of an email signature with plain rules.

Only the sender's own text is looked at: quoted replies and forwarded mail below it are cut
off first. When the rules find nothing in a signature block, the caller may ask Claude
(see ai.py), but only for a new or changed contact.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

QUOTE_START = re.compile(
    r"^(on .{5,200}wrote:?\s*$|-{2,}\s*original message|_{8,}\s*$|from:\s|sent from my |"
    r"begin forwarded message|-{3,}\s*forwarded message)", re.I)
CLOSINGS = re.compile(r"^(thanks|thank you|thx|regards|best|best regards|kind regards|warm regards|sincerely|"
                      r"cheers|respectfully|have a great|-- ?)\b[ ,.!]*", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?:\s*(?:x|ext\.?)\s*(\d{1,6}))?(?!\d)")
MOBILE_LABEL = re.compile(r"\b(m|mob|mobile|c|cell|cel)\b\s*[:.]?\s*$", re.I)
FAX_LABEL = re.compile(r"\b(f|fax)\b\s*[:.]?\s*$", re.I)
TITLE_WORDS = re.compile(
    r"\b(manager|estimator|accounts? (payable|receivable)|accounting|accountant|billing|bookkeeper|controller|"
    r"superintendent|super|foreman|owner|president|vice president|vp|ceo|cfo|coo|principal|partner|"
    r"coordinator|administrator|admin|assistant|director|project engineer|engineer|operations|dispatcher|"
    r"office manager|sales|representative|rep|specialist|supervisor|lead|clerk)\b", re.I)
COMPANY_WORDS = re.compile(
    r"\b(inc|llc|l\.l\.c|co|corp|corporation|company|ltd|group|construction|contractors?|contracting|electric(al)?|"
    r"plumbing|mechanical|roofing|painting|flooring|drywall|glass|glazing|steel|concrete|services|supply|"
    r"builders|interiors|coverings|fire|protection|hvac|cabinets?|millwork|doors|hardware|masonry|"
    r"insulation|demolition|excavating|signs?|enterprises|industries|systems)\b\.?", re.I)
NOISE = re.compile(r"https?://|www\.|@|confidential|this (e-?mail|message)|unsubscribe|^\W*$|license|lic\.? ?#", re.I)

ROLE_RULES = [
    (re.compile(r"\b(ap|a/p|ar|a/r|accounts? (payable|receivable)|accounting|accountant|billing|bookkeep\w*|"
                r"controller|collections?)\b", re.I), "billing"),
    (re.compile(r"\bproject manager|\bpm\b|project coordinator", re.I), "pm"),
    (re.compile(r"\bestimat\w*", re.I), "estimator"),
    (re.compile(r"\b(superintendent|foreman|forman|field (lead|supervisor))\b", re.I), "foreman"),
    (re.compile(r"\b(owner|president|principal|ceo|founder)\b", re.I), "owner"),
]
ROLE_NAMES = {"billing": "Billing", "pm": "Project manager", "estimator": "Estimator", "foreman": "Foreman / super",
              "owner": "Owner", "other": "Other"}


@dataclass
class Signature:
    block: list[str] = field(default_factory=list)
    phone: str | None = None
    mobile: str | None = None
    title: str | None = None
    company: str | None = None

    @property
    def found_anything(self) -> bool:
        return bool(self.phone or self.mobile or self.title or self.company)

    @property
    def text(self) -> str:
        return "\n".join(self.block)

    @property
    def hash(self) -> str | None:
        if not self.block:
            return None
        return hashlib.sha256(re.sub(r"\s+", " ", self.text.lower()).encode()).hexdigest()


def guess_role(title: str | None) -> str | None:
    for rule, role in ROLE_RULES:
        if title and rule.search(title):
            return role
    return None


def fmt_phone(m: re.Match) -> str:
    out = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
    return out + (f" x{m.group(4)}" if m.group(4) else "")


def own_text(body: str) -> list[str]:
    """The sender's own lines, without quoted replies or forwarded mail."""
    out = []
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith(">") or QUOTE_START.match(s):
            break
        out.append(s)
    return out


def signature_block(body: str, sender_name: str | None = None) -> list[str]:
    lines = own_text(body)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return []
    first = (sender_name or "").split()[0].lower() if sender_name and sender_name.split() else ""
    start = None
    for i in range(len(lines) - 1, max(-1, len(lines) - 25), -1):
        low = lines[i].lower()
        if CLOSINGS.match(lines[i]) and len(lines[i]) < 40:
            start = i + 1
            break
        if first and len(first) > 1 and low.startswith(first) and len(lines[i]) < 50:
            start = i
            break
    if start is None:
        start = max(0, len(lines) - 8)
    return [s for s in lines[start:start + 14] if s and not s.lower().startswith("sent from my")]


def parse(body: str, sender_name: str | None = None) -> Signature:
    sig = Signature(block=signature_block(body, sender_name))
    for line in sig.block:
        for m in PHONE.finditer(line):
            label = line[:m.start()]
            if FAX_LABEL.search(label):
                continue
            if MOBILE_LABEL.search(label):
                sig.mobile = sig.mobile or fmt_phone(m)
            else:
                sig.phone = sig.phone or fmt_phone(m)
        if NOISE.search(line) or PHONE.search(line) or len(line) > 70:
            continue
        if sender_name and line.lower().strip(" ,.") == sender_name.lower().strip():
            continue
        if not sig.company and COMPANY_WORDS.search(line) and not TITLE_WORDS.search(line.split("|")[0]):
            sig.company = line.strip(" |-–,")
        elif not sig.title and TITLE_WORDS.search(line) and len(line) <= 60:
            # "Jane Doe | Accounts Payable" or "Jane Doe, Project Manager": keep the title part.
            parts = [p.strip() for p in re.split(r"\s[|–-]\s|,\s", line) if p.strip()]
            sig.title = next((p for p in parts if TITLE_WORDS.search(p)), line).strip(" |-–,")
    return sig
