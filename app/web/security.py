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


def production_problems(s: Settings) -> list[str]:
    """Settings that would let someone in on an internet-facing server."""
    if not s.app_base_url.startswith("https://"):
        return []
    problems = []
    if s.secret_key in WEAK_SECRETS or len(s.secret_key) < 32:
        problems.append("SECRET_KEY must be set to a long random value (at least 32 characters).")
    if s.dev_auth_email:
        problems.append("DEV_AUTH_EMAIL must not be set on a public server (it skips sign-in).")
    if not s.google_client_id or not s.google_client_secret:
        problems.append("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required.")
    if s.mail_provider != "gmail":
        problems.append("MAIL_PROVIDER must be gmail on a public server.")
    if s.database_url.startswith("sqlite"):
        problems.append("DATABASE_URL isn't set, so the app would keep its data in a temporary file that is "
                        "wiped on every deploy. Set DATABASE_URL to ${{Postgres.DATABASE_URL}} (the Postgres "
                        "service's connection string).")
    return problems


def check_production_settings(s: Settings) -> None:
    problems = production_problems(s)
    if problems:
        raise RuntimeError("Unsafe configuration: " + " ".join(problems))


def locked_app(problems: list[str]):
    """Served instead of the real app when the configuration is unsafe: nothing is reachable,
    and the page says which setting to fix (never the values)."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(docs_url=None, redoc_url=None)
    items = "".join(f"<li>{p}</li>" for p in problems)
    page = ("<!doctype html><meta charset=utf-8><title>Lien Waivers: settings needed</title>"
            "<body style='font-family:system-ui;max-width:640px;margin:60px auto;line-height:1.5'>"
            "<h1>The app is locked until these settings are fixed</h1><ul>" + items + "</ul>"
            "<p>Fix them in Railway → the app service → Variables. The app restarts on its own.</p></body>")

    @app.get("/health")
    def health():
        return JSONResponse({"status": "misconfigured", "problems": problems})

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    def everything(path: str):
        return HTMLResponse(page, status_code=503)

    return app
