"""Claude reads a signature only when the rules found nothing in it, and only for a new or
changed contact, so the cost stays small. Each run has a cap on calls."""
from __future__ import annotations

import json

import structlog

from app.config import get_settings

log = structlog.get_logger()

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "title", "company", "phone", "mobile"],
    "properties": {k: {"type": ["string", "null"]} for k in ("name", "title", "company", "phone", "mobile")},
}

PROMPT = """This is the end of an email from {sender}. Pull the sender's contact details out of the
signature. Use null for anything that isn't there; don't guess. Phone numbers as written.

{block}"""


class AiBudget:
    def __init__(self, calls: int):
        self.left = calls

    def take(self) -> bool:
        if self.left <= 0 or not get_settings().anthropic_api_key:
            return False
        self.left -= 1
        return True


def parse_signature(block: str, sender: str, client=None) -> dict | None:
    settings = get_settings()
    try:
        import anthropic

        client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.beta.messages.create(
            model=settings.claude_model,
            max_tokens=1000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": PROMPT.format(sender=sender, block=block[:2000])}],
        )
        if resp.stop_reason == "refusal":
            return None
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        return json.loads(text)
    except Exception as exc:
        log.warning("contacts.ai_signature_failed", error=str(exc)[:200])
        return None
