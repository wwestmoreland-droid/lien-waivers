from datetime import date
from decimal import Decimal

import pytest

from app.mail.templates import BANNED_PHRASES, TEMPLATES, file_text, lint, render_text


@pytest.mark.parametrize("key", list(TEMPLATES))
def test_templates_follow_writing_rules(key):
    text = file_text(key)
    body = "\n".join(line for line in text.splitlines() if not line.startswith("Subject:"))
    for phrase in BANNED_PHRASES:
        assert phrase.lower() not in text.lower(), f"{key} uses banned phrase {phrase!r}"
    assert "—" not in body, f"{key} has an em dash"
    assert "!" not in body, f"{key} has an exclamation point"
    assert lint(text) == []


def test_lint_catches_banned_phrase_and_em_dash():
    problems = lint("Hi,\n\nI hope this email finds you well — please sign!\n")
    assert len(problems) == 3


VALUES = dict(first_name="Dana", project_name="Studio Co - Palatine, IL", amount=Decimal("29647.8"),
              invoice_no="Pay App 1", paid_date=date(2026, 10, 16), through_date=date(2026, 9, 30), id=7,
              signature="Will Westmoreland\nBuildout Pros", gate=False, suppliers=False)


def test_partial_renders_amounts_dates_token():
    r = render_text(file_text("partial"), VALUES)
    assert r.subject == "Lien waiver – Studio Co - Palatine, IL – $29,647.80 [LW-7]"
    assert r.body.startswith("Hi Dana,\n")
    assert "on Oct 16, 2026 ($29,647.80)" in r.body
    assert "through Sep 30, 2026" in r.body
    assert "next payment" not in r.body  # [if gate] off: payments are never held
    assert r.body.rstrip().endswith("Thanks,\nWill Westmoreland\nBuildout Pros")


def test_empty_first_name_and_empty_field_sentences_are_dropped():
    r = render_text(file_text("partial"), dict(VALUES, first_name="", invoice_no=None))
    assert r.body.startswith("Hi,\n")
    assert "We paid invoice" not in r.body          # sentence with an empty field is left out
    assert "The lien waiver for that payment" in r.body  # the next sentence on the line stays
    assert "{" not in r.body


def test_if_lines():
    r = render_text(file_text("conditional_final"), dict(VALUES, exceptions="Pending CO: Reroute line ($4,500.00)"))
    assert "We listed these open items as exceptions: Pending CO: Reroute line ($4,500.00)." in r.body
    r = render_text(file_text("conditional_final"), VALUES)
    assert "exceptions" not in r.body
    assert "We'll release payment as soon as we have everything." in r.body


def test_received_without_name():
    assert render_text(file_text("received"), {"first_name": ""}).body == "Got it, thanks.\n"


def test_escalation_greeting():
    r = render_text(file_text("escalation"), dict(VALUES, pm_first_name="Pat", vendor="Delta Fire",
                                                  waiver_type="unconditional partial lien waiver",
                                                  requested_date=date(2026, 10, 1), billing_name="Dana AP",
                                                  billing_email="ap@deltafire.com", billing_phone="555-0100"))
    assert r.subject == "Waiver still out – Delta Fire – Studio Co - Palatine, IL"
    assert r.body.startswith("Pat,\n")
    assert "We asked on Oct 1, 2026" in r.body
