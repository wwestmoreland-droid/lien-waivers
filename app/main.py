from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.config import get_settings
from app.db.models import SyncRun
from app.db.session import SessionLocal
from app.jobs import scheduler
from app.logs import configure_logging
from app.web import email_routes, routes, waiver_routes
from app.web.security import SecurityMiddleware, locked_app, production_problems
from app.web.templating import templates

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    scheduler.start()
    yield
    scheduler.shutdown()


def create_app() -> FastAPI:
    s = get_settings()
    problems = production_problems(s)
    if problems:
        # Don't crash-loop (that hides the reason); serve a locked page that names the setting.
        log.error("config.unsafe", problems=problems)
        print("UNSAFE CONFIGURATION, app locked: " + " ".join(problems), flush=True)
        return locked_app(problems)
    app = FastAPI(title="Buildout Pros – Lien Waivers", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(SecurityMiddleware, base_url=s.app_base_url)
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, same_site="lax",
                       https_only=s.app_base_url.startswith("https"), max_age=60 * 60 * 12)
    app.mount("/static", StaticFiles(directory=str(Path(routes.__file__).parent / "static")), name="static")
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(waiver_routes.router)
    app.include_router(email_routes.router)

    @app.exception_handler(auth.NotAuthenticated)
    async def _not_auth(request: Request, _exc):
        if request.headers.get("HX-Request"):
            return HTMLResponse("Signed out", status_code=401, headers={"HX-Redirect": "/login"})
        request.session["next"] = str(request.url.path)
        return RedirectResponse("/login" if s.google_client_id else "/signed-out", status_code=303)

    @app.get("/signed-out", response_class=HTMLResponse)
    def signed_out(request: Request):
        return templates.TemplateResponse(request, "signed_out.html", {"user": None})

    @app.get("/health")
    def health():
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            last = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))
            dialect = db.get_bind().dialect.name
        except Exception as exc:
            return JSONResponse({"status": "error", "db": repr(exc)}, status_code=503)
        finally:
            db.close()
        return {
            "status": "ok",
            "database": "postgres" if dialect == "postgresql" else "sqlite (not for production)",
            "time": datetime.now(timezone.utc).isoformat(),
            "last_sync": None if not last else {
                "id": last.id, "status": last.status,
                "finished_at": last.finished_at.isoformat() if last.finished_at else None,
            },
            "scheduler_next_run": (lambda t: t.isoformat() if t else None)(scheduler.next_run_time()),
        }

    return app


app = create_app()
