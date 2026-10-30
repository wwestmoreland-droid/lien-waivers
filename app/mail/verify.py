"""Read a returned waiver with Claude and compare it to what was requested.

The result only informs the person in the Verification queue; nothing is accepted or
rejected automatically.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import structlog

from app.config import get_settings
from app.db.models import EmailAttachment, Waiver
from app.waivers.types import WaiverType

log = structlog.get_logger()

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_lien_waiver", "appears_signed", "notary_completed", "waiver_type", "amount",
                 "through_date", "project_name_on_form", "project_matches", "signer_company",
                 "is_supplier_waiver", "issues", "summary"],
    "properties": {
        "is_lien_waiver": {"type": "boolean"},
        "appears_signed": {"type": "boolean"},
        "notary_completed": {"type": "boolean",
                             "description": "Notary section filled in, signed and sealed/stamped"},
        "waiver_type": {"type": "string", "enum": ["conditional_partial", "unconditional_partial",
                                                   "conditional_final", "unconditional_final", "unknown"]},
        "amount": {"type": ["string", "null"], "description": "Dollar amount written on the form, digits only"},
        "through_date": {"type": ["string", "null"], "description": "Through date on the form, YYYY-MM-DD"},
        "project_name_on_form": {"type": ["string", "null"]},
        "project_matches": {"type": "boolean"},
        "signer_company": {"type": ["string", "null"]},
        "is_supplier_waiver": {"type": "boolean",
                               "description": "Signed by a supplier of the sub rather than the sub itself"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

PROMPT = """You are checking a signed lien waiver that a subcontractor returned to Buildout Pros LLC.
Read the attached document and report what it actually says. Don't guess: if something isn't
on the page or can't be read, say so in issues.

What Buildout Pros asked for:
- Waiver type: {waiver_type}
- Subcontractor: {sub}
- Project: {project} ({address})
- Amount: {amount}
- Through date: {through_date}
- Notarization required: {notary}

A signature counts whether it's handwritten, typed in a script font, an e-signature (Adobe, DocuSign
and the like) or a pasted image of a signature. Only say it isn't signed if the signature line is
actually empty.

List in issues anything a reviewer should fix before accepting it: missing signature, blank or
incomplete notary section, wrong form type, an amount or date that doesn't match, wrong project,
or pages that are unreadable."""


def _expected(w: Waiver) -> dict:
    p = w.subcontract.project
    return {
        "waiver_type": WaiverType(w.type).label,
        "sub": w.subcontract.vendor.name,
        "project": p.name or p.number,
        "address": p.address or "address not on file",
        "amount": f"${w.amount:,.2f}" if w.amount is not None else "no amount on this form",
        "through_date": w.through_date.isoformat() if w.through_date and not WaiverType(w.type).is_final
        else "not applicable",
        "notary": "yes",
    }


def _to_decimal(s: str | None) -> Decimal | None:
    if not s:
        return None
    try:
        return Decimal("".join(ch for ch in s if ch.isdigit() or ch == ".")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def compare(w: Waiver, found: dict, notary_required: bool = True) -> list[str]:
    """Plain-language problems, with the reviewer's reject reasons in mind."""
    problems = []
    if not found.get("is_lien_waiver"):
        problems.append("it doesn't look like a lien waiver")
    if not found.get("appears_signed"):
        problems.append("it isn't signed")
    if notary_required and not found.get("notary_completed"):
        problems.append("the notary section is blank")
    if found.get("waiver_type") not in ("unknown", None) and found["waiver_type"] != WaiverType(w.type).value.lower():
        problems.append("it's the wrong waiver form")
    if w.amount is not None:
        amt = _to_decimal(found.get("amount"))
        if amt is not None and amt != w.amount:
            problems.append(f"the amount should be ${w.amount:,.2f}")
    if w.through_date and not WaiverType(w.type).is_final and found.get("through_date"):
        if found["through_date"] != w.through_date.isoformat():
            problems.append(f"the date should be {w.through_date:%b %-d, %Y}")
    if found.get("project_matches") is False:
        problems.append("it's for the wrong project")
    return problems


def supplier_through_date(sub_waiver: Waiver):
    """Supplier partials that come with a sub's partial are for the sub's previous pay application."""
    from app.mail.outbound import previous_pay_app

    if WaiverType(sub_waiver.type).is_final:
        return None
    prev = previous_pay_app(sub_waiver.subcontract, sub_waiver.payable)
    return prev.through_date if prev else sub_waiver.through_date


def compare_supplier(sub_waiver: Waiver, found: dict, notary_required: bool = True) -> list[str]:
    """A supplier's waiver that came with the sub's: signed, notarized, and (for a partial)
    through the sub's previous pay application."""
    problems = []
    if not found.get("appears_signed"):
        problems.append("it isn't signed")
    if notary_required and not found.get("notary_completed"):
        problems.append("the notary section is blank")
    due = supplier_through_date(sub_waiver)
    if due and found.get("through_date") and found.get("waiver_type") in (
            "conditional_partial", "unconditional_partial") and found["through_date"] != due.isoformat():
        problems.append(f"the date should be {due:%b %-d, %Y}")
    if found.get("project_matches") is False:
        problems.append("it's for the wrong project")
    return problems


def verify(w: Waiver, attachment: EmailAttachment, client=None) -> dict:
    settings = get_settings()
    base = {"checked_at": datetime.now(timezone.utc).isoformat(), "attachment_id": attachment.id,
            "filename": attachment.filename}
    if client is None and not settings.anthropic_api_key:
        return base | {"status": "not_checked", "reason": "ANTHROPIC_API_KEY isn't set, so check it by eye."}
    if not Path(attachment.path).exists():
        return base | {"status": "not_checked", "reason": "The attachment file is missing."}
    data = base64.standard_b64encode(Path(attachment.path).read_bytes()).decode()
    if attachment.is_pdf:
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    elif attachment.is_image:
        block = {"type": "image", "source": {"type": "base64", "media_type": attachment.content_type, "data": data}}
    else:
        return base | {"status": "not_checked", "reason": "Attachment isn't a PDF or image."}
    try:
        import anthropic

        client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.beta.messages.create(
            model=settings.claude_model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT.format(**_expected(w))}]}],
        )
    except Exception as exc:
        log.warning("verify.failed", waiver_id=w.id, error=str(exc))
        return base | {"status": "error", "reason": f"Couldn't check it automatically: {exc}"[:300]}
    if resp.stop_reason == "refusal":
        return base | {"status": "error", "reason": "The automatic check declined this document; check it by eye."}
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)
    except json.JSONDecodeError:
        return base | {"status": "error", "reason": "The automatic check returned something unreadable."}
    problems = compare(w, found)
    return base | {"status": "issues" if problems else "looks_good", "problems": problems, "found": found,
                   "model": getattr(resp, "model", settings.claude_model)}
