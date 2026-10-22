"""Web security: safe file serving, security headers, cross-site request checks, startup checks."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from app.config import Settings

# Files that browsers can show without running code from them.
INLINE_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def safe_file_response(path: str, filename: str, claimed_type: str | None) -> FileResponse:
    """Serve an uploaded or emailed file without trusting the sender's content type.

    PDFs and plain images are shown inline; anything else (HTML, SVG, Office files...) is a
    download, so a crafted attachment can never run script on the app's site.
    """
    name = Path(filename).name.replace('"', "")
    media = INLINE_TYPES.get(Path(name).suffix.lower())
    headers = {"X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"}
    if media:
        headers["Content-Disposition"] = f'inline; filename="{name}"'
        return FileResponse(path, media_type=media, headers=headers)
    headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(path, media_type="application/octet-stream", headers=headers)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Rejects form posts that come from another website, and adds standard security headers."""

    def __init__(self, app, base_url: str):
        super().__init__(app)
        self.allowed = {urlparse(base_url).netloc.lower()} if base_url else set()

    async def dispatch(self, request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            source = request.headers.get("origin") or request.headers.get("referer")
            if source:
                host = urlparse(source).netloc.lower()
                own = request.headers.get("host", "").lower()
                if host not in self.allowed and host != own:
                    return PlainTextResponse("Cross-site request refused.", status_code=403)
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


WEAK_SECRETS = {"", "dev-only-change-me", "changeme", "secret"}


def check_production_settings(s: Settings) -> None:
    """Refuse to start an internet-facing app with settings that would let someone in."""
    public = s.app_base_url.startswith("https://")
    if not public:
        return
    problems = []
    if s.secret_key in WEAK_SECRETS or len(s.secret_key) < 32:
        problems.append("SECRET_KEY must be set to a long random value (at least 32 characters).")
    if s.dev_auth_email:
        problems.append("DEV_AUTH_EMAIL must not be set on a public server (it skips sign-in).")
    if not s.google_client_id or not s.google_client_secret:
        problems.append("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required.")
    if s.mail_provider != "gmail":
        problems.append("MAIL_PROVIDER must be gmail on a public server.")
    if problems:
        raise RuntimeError("Unsafe configuration, not starting: " + " ".join(problems))
