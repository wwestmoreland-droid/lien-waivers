"""Suggested answers: a sub's question gets a drafted reply that waits in Ready to send."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import EmailMessage, Supplier
from app.mail import answer, outbound, templates
from app.services import today
from app.waivers.types import WaiverStatus
from tests.test_email_e2e import mail, send_request  # noqa: F401
from tests.test_reply_scenarios import W, asked, reply


class FakeClaude:
    def __init__(self, reply="We'll check the amount against our records and send a corrected waiver if it's off.",
                 action="answer", fail=False, check="", summary="Asked about the amount"):
        self.out = {"action": action, "reply": reply if action != "wait" else "", "check": check,
                    "summary": summary}
        self.fail = fail
        self.prompts = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        if self.fail:
            raise RuntimeError("overloaded")
        self.prompts.append(kw["messages"][0]["content"])
        return SimpleNamespace(stop_reason="end_turn", model="m",
                               content=[SimpleNamespace(type="text", text=json.dumps(self.out))])


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(answer, "get_client", lambda: fake)
    return fake


def answers(db):
    return db.scalars(select(EmailMessage).where(EmailMessage.kind == "answer")).all()


def test_question_gets_a_suggested_answer_waiting_for_review(mail, claude):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body="Is this amount right? We were paid $31,000.")
    [a] = answers(db)
    assert a.status not in ("sent",) and a.reply_to_email_id is not None
    assert "corrected waiver" in a.body_text and a.body_text.rstrip().endswith(a.mailbox.signature or a.mailbox.display_name)
    assert a.to_addrs == ["ap@deltafire.com"]
    # The facts and their words went to Claude.
    assert "$31,000" in claude.prompts[0] and "Delta" in claude.prompts[0]
    # Shown in Ready to send with their question, but not part of Send all.
    [item] = [t for t in today.ready_to_send(db) if t.email.id == a.id]
    assert "31,000" in item.question and not item.in_send_all and item.sendable
    assert asked(db, "sub_replied") is None  # the draft itself is the thing to review
    assert W(db, w).status == WaiverStatus.SENT


def test_never_auto_sent(mail, claude):
    db = mail.db
    from app.services.settings_store import set_setting
    w, e = send_request(mail)
    set_setting(db, "auto_send", {k: True for k in outbound.AUTO_SEND_KINDS} | {"answer": True}, "ap")
    db.commit()
    reply(mail, e, body="Can you resend to our new AP person, jane@deltafire.com?")
    [a] = answers(db)
    assert a.status != "sent" and "answer" not in outbound.AUTO_SEND_KINDS


@pytest.mark.parametrize("body", ["Thanks, will do.", "Our owner is out until Monday, she'll sign it then.",
                                  "Got it, working on getting it notarized."])
def test_waiting_game_no_draft_no_question(mail, monkeypatch, body):
    fake = FakeClaude(action="wait", summary="Will sign it Monday")
    monkeypatch.setattr(answer, "get_client", lambda: fake)
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body=body)
    assert answers(db) == [] and asked(db, "sub_replied") is None
    from app.mail.unsigned import last_word
    ww = W(db, w)
    assert ww.status == WaiverStatus.SENT and last_word(db, ww)  # shown under Waiting on subs


def test_check_first_drafts_and_says_what_to_check(mail, monkeypatch):
    fake = FakeClaude(action="check_first", reply="We'll check on that payment and get back to you.",
                      check="Confirm whether invoice 1042 was paid.")
    monkeypatch.setattr(answer, "get_client", lambda: fake)
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body="When will invoice 1042 be paid?")
    assert len(answers(db)) == 1
    q = asked(db, "sub_replied")
    assert "Confirm whether invoice 1042 was paid." in q.message and "Ready to send" in q.message


def test_prompt_has_the_whole_record(mail, claude):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body="Which invoices does this cover?")
    p = claude.prompts[0]
    for part in ("Original contract", "Paid to date".lower(), "Their invoices on this job", "Earlier emails in this thread",
                 "Internal, don't mention", "Today is"):
        assert part.lower() in p.lower(), part
    assert e.subject not in ("",) and "Us" in p  # our request is in the thread history


def test_question_after_accepted_is_answered(mail, claude):
    db = mail.db
    from app.waivers import service as waivers
    from tests.test_reply_scenarios import SIGNED
    w, e = send_request(mail)
    reply(mail, e, attachments=[SIGNED])
    waivers.accept(db, W(db, w), "ap")
    db.commit()
    reply(mail, e, body="Did you get everything you need from us?")
    assert len(answers(db)) == 1


def test_no_key_or_failed_call_still_asks(mail, monkeypatch):
    monkeypatch.setattr(answer, "get_client", lambda: FakeClaude(fail=True))
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body="What date should this be through?")
    assert answers(db) == [] and asked(db, "sub_replied") is not None
    monkeypatch.setattr(answer, "get_client", lambda: None)
    reply(mail, e, body="Hello? What date should this be through?")
    assert answers(db) == []


def test_question_with_not_signed_yet_also_drafts(mail, claude):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body="Forwarded to our billing dept to sign. Does it need to be notarized?")
    assert len(answers(db)) == 1
    from app.mail.unsigned import last_word
    assert last_word(db, W(db, w))  # and the waiver still shows they're on it


def test_one_draft_per_email(mail, claude):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body="Is this amount right?")
    [inbound_msg] = db.scalars(select(EmailMessage).where(EmailMessage.direction == "in",
                                                          EmailMessage.waiver_id == w.id)).all()
    assert answer.respond(db, W(db, w), inbound_msg, "Is this amount right?", client=claude) is None
    assert len(answers(db)) == 1


def test_draft_follows_writing_rules(mail, monkeypatch):
    fake = FakeClaude(reply="Good question! Kindly note the amount is total paid to date — not one invoice. "
                            "It covers everything through the date shown.")
    monkeypatch.setattr(answer, "get_client", lambda: fake)
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body="Why is this more than our invoice?")
    [a] = answers(db)
    assert templates.lint(a.body_text) == []
    assert "covers everything" in a.body_text


def test_never_writes_to_a_supplier(mail, claude):
    db = mail.db
    w, e = send_request(mail)
    db.add(Supplier(subcontract_id=w.subcontract_id, name="ABC Supply", email="ar@abcsupply.com"))
    db.commit()
    reply(mail, e, from_addr="ar@abcsupply.com", body="Do you need our waiver too?")
    assert answers(db) == []
    reply(mail, e, body="Who else needs to sign?", cc=["ar@abcsupply.com"])
    [a] = answers(db)
    assert "ar@abcsupply.com" not in (a.to_addrs or []) + (a.cc_addrs or [])


def test_today_shows_the_question_and_draft_without_send_all(mail, claude):
    from fastapi.testclient import TestClient

    from app.main import create_app
    w, e = send_request(mail)
    reply(mail, e, body="Is this amount right? We were paid $31,000.")
    with TestClient(create_app()) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert "Suggested answer" in r.text and "They wrote:" in r.text and "corrected waiver" in r.text
    assert "Send all" not in r.text
