"""Gmail over its REST API, authorised with a stored OAuth refresh token."""
from __future__ import annotations

import base64
import email.utils
import html
import re
import time
from email.message import EmailMessage as MimeMessage

import httpx
import structlog

from app.config import get_settings
from app.mail.provider import (
    CursorExpired,
    Incoming,
    MessageGone,
    InAttachment,
    Outgoing,
    SentResult,
    Summary,
    TokenError,
)

log = structlog.get_logger()

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

SENDER_SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send"]
READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _addresses(value: str | None) -> list[str]:
    return [a.lower() for _, a in email.utils.getaddresses([value or ""]) if a]


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


class GmailProvider:
    def __init__(self, email_addr: str, refresh_token: str):
        self.email = email_addr
        self.refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0
        self.http = httpx.Client(timeout=60.0)

    # ------------------------------------------------------------ auth

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        s = get_settings()
        resp = self.http.post(TOKEN_URL, data={
            "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        })
        if resp.status_code in (400, 401):
            raise TokenError(resp.json().get("error_description") or resp.text)
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    def _call(self, method: str, path: str, **kw) -> dict:
        for attempt in range(5):
            resp = self.http.request(method, API + path, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
            if resp.status_code == 401:
                self._access_token = None
                if attempt:
                    raise TokenError("Gmail rejected the access token")
                continue
            if resp.status_code == 429 or resp.status_code >= 500 or (
                    resp.status_code == 403 and "rateLimitExceeded" in resp.text):
                time.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code == 404 and path.startswith("/history"):
                raise CursorExpired()
            if resp.status_code == 404 and path.startswith("/messages/"):
                raise MessageGone(path)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        resp.raise_for_status()
        return {}

    # ------------------------------------------------------------ send

    def send(self, out: Outgoing) -> SentResult:
        msg = MimeMessage()
        msg["From"] = email.utils.formataddr((out.from_name or "", out.from_addr))
        msg["To"] = ", ".join(out.to)
        if out.cc:
            msg["Cc"] = ", ".join(out.cc)
        msg["Subject"] = out.subject
        msg["Reply-To"] = out.from_addr
        if out.in_reply_to:
            msg["In-Reply-To"] = out.in_reply_to
            msg["References"] = (out.references + " " if out.references else "") + out.in_reply_to
        msg.set_content(out.body)
        for a in out.attachments:
            main, _, sub = a.content_type.partition("/")
            msg.add_attachment(a.data, maintype=main or "application", subtype=sub or "octet-stream",
                               filename=a.filename)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body = {"raw": raw}
        if out.thread_id:
            body["threadId"] = out.thread_id
        sent = self._call("POST", "/messages/send", json=body)
        meta = self._call("GET", f"/messages/{sent['id']}", params={"format": "metadata",
                                                                      "metadataHeaders": ["Message-ID"]})
        rfc = next((h["value"] for h in meta.get("payload", {}).get("headers", [])
                    if h["name"].lower() == "message-id"), None)
        return SentResult(message_id=sent["id"], thread_id=sent["threadId"], rfc_message_id=rfc)

    # ------------------------------------------------------------ read

    def changes_since(self, cursor: str | None) -> tuple[list[str], str]:
        if not cursor:
            return [], str(self._call("GET", "/profile")["historyId"])
        ids: list[str] = []
        page = None
        latest = cursor
        while True:
            params = {"startHistoryId": cursor, "historyTypes": "messageAdded", "maxResults": 500}
            if page:
                params["pageToken"] = page
            data = self._call("GET", "/history", params=params)
            for h in data.get("history", []):
                for added in h.get("messagesAdded", []):
                    mid = added["message"]["id"]
                    if "DRAFT" in added["message"].get("labelIds", []):
                        continue  # drafts are rewritten and deleted constantly; the sent copy comes later
                    if mid not in ids:
                        ids.append(mid)
            latest = str(data.get("historyId", latest))
            page = data.get("nextPageToken")
            if not page:
                break
        return ids, latest

    def get_message(self, message_id: str, attachments: bool = True) -> Incoming:
        data = self._call("GET", f"/messages/{message_id}", params={"format": "full"})
        payload = data.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        texts: list[str] = []
        htmls: list[str] = []
        atts: list[InAttachment] = []

        def walk(part: dict) -> None:
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            filename = part.get("filename") or ""
            if filename and not attachments:
                pass  # only the text was asked for (e.g. to read a signature)
            elif filename:
                raw = body.get("data")
                if not raw and body.get("attachmentId"):
                    raw = self._call("GET", f"/messages/{message_id}/attachments/{body['attachmentId']}")["data"]
                if raw:
                    atts.append(InAttachment(filename=filename, content_type=mime, data=_b64url_decode(raw)))
            elif mime == "text/plain" and body.get("data"):
                texts.append(_b64url_decode(body["data"]).decode("utf-8", "replace"))
            elif mime == "text/html" and body.get("data"):
                htmls.append(_b64url_decode(body["data"]).decode("utf-8", "replace"))
            for p in part.get("parts", []) or []:
                walk(p)

        walk(payload)
        return self._incoming(data, body="\n".join(texts) if texts else _html_to_text("\n".join(htmls)),
                              attachments=atts)

    @staticmethod
    def _incoming(data: dict, body: str, attachments: list[InAttachment]) -> Incoming:
        headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
        name, addr = email.utils.parseaddr(headers.get("from", ""))
        recipients = []
        for h in ("to", "cc", "delivered-to", "x-forwarded-to", "x-original-to", "envelope-to"):
            recipients += _addresses(headers.get(h))
        date = None
        try:
            date = email.utils.parsedate_to_datetime(headers["date"]) if headers.get("date") else None
        except (TypeError, ValueError):
            pass
        return Incoming(
            message_id=data["id"], thread_id=data["threadId"], rfc_message_id=headers.get("message-id"),
            in_reply_to=headers.get("in-reply-to"), references=headers.get("references"),
            from_addr=addr.lower(), from_name=name or None, to=_addresses(headers.get("to")),
            cc=_addresses(headers.get("cc")), recipients_all=recipients, subject=headers.get("subject", ""),
            body_text=body, date=date, label_ids=data.get("labelIds", []), headers=headers, attachments=attachments,
        )

    METADATA_HEADERS = ["From", "To", "Cc", "Reply-To", "Subject", "Date", "Delivered-To", "X-Forwarded-To",
                        "X-Original-To", "List-Unsubscribe", "List-Id", "Precedence", "Auto-Submitted",
                        "X-Autoreply", "X-Autorespond", "Content-Type", "Message-ID", "In-Reply-To", "References"]

    def get_metadata(self, message_id: str) -> Incoming:
        data = self._call("GET", f"/messages/{message_id}", params={
            "format": "metadata", "metadataHeaders": self.METADATA_HEADERS})
        return self._incoming(data, body="", attachments=[])

    def list_page(self, query: str, page_token: str | None, size: int = 100) -> tuple[list[str], str | None, int]:
        params = {"q": query, "maxResults": size}
        if page_token:
            params["pageToken"] = page_token
        data = self._call("GET", "/messages", params=params)
        return ([m["id"] for m in data.get("messages", [])], data.get("nextPageToken"),
                int(data.get("resultSizeEstimate") or 0))

    def list_recent(self, query: str = "", limit: int = 50) -> list[Summary]:
        data = self._call("GET", "/messages", params={"q": query, "maxResults": limit})
        out = []
        for m in data.get("messages", []):
            meta = self._call("GET", f"/messages/{m['id']}", params={
                "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
            h = {x["name"].lower(): x["value"] for x in meta.get("payload", {}).get("headers", [])}
            name, addr = email.utils.parseaddr(h.get("from", ""))
            try:
                date = email.utils.parsedate_to_datetime(h["date"]) if h.get("date") else None
            except (TypeError, ValueError):
                date = None
            out.append(Summary(message_id=meta["id"], thread_id=meta["threadId"], from_addr=addr.lower(),
                               from_name=name or None, subject=h.get("subject", ""), date=date,
                               snippet=html.unescape(meta.get("snippet", "")),
                               unread="UNREAD" in meta.get("labelIds", [])))
        return out

    def recent_ids(self, days: int, limit: int = 500) -> list[str]:
        ids: list[str] = []
        page = None
        while len(ids) < limit:
            params = {"q": f"newer_than:{days}d", "maxResults": min(500, limit - len(ids))}
            if page:
                params["pageToken"] = page
            data = self._call("GET", "/messages", params=params)
            ids += [m["id"] for m in data.get("messages", [])]
            page = data.get("nextPageToken")
            if not page:
                break
        return list(reversed(ids))

    # ------------------------------------------------------------ labels

    def ensure_label(self, name: str) -> str:
        for lab in self._call("GET", "/labels").get("labels", []):
            if lab["name"] == name:
                return lab["id"]
        created = self._call("POST", "/labels", json={
            "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
        return created["id"]

    def label_thread(self, thread_id: str, label_id: str) -> None:
        self._call("POST", f"/threads/{thread_id}/modify", json={"addLabelIds": [label_id]})

    def set_read(self, message_id: str, read: bool) -> None:
        body = {"removeLabelIds": ["UNREAD"]} if read else {"addLabelIds": ["UNREAD"]}
        self._call("POST", f"/messages/{message_id}/modify", json=body)

    def revoke(self) -> None:
        try:
            self.http.post(REVOKE_URL, params={"token": self.refresh_token})
        except httpx.HTTPError:
            log.warning("gmail.revoke_failed", mailbox=self.email)
