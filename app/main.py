from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app.database import get_db, init_db
from app.routers import admin, auth, api, mcp, oauth
from app.routers.auth import get_current_user
from app.services.auth import maybe_bootstrap_admin, should_use_secure_cookies
from app.services.client_ip import warn_if_non_ip_trusted_proxies

_health_db_warned = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_INSECURE_SECRET_KEYS = {"dev-secret-change-in-prod"}


def _check_secret_key():
    # This app has no public-facing surface (no signup, no email links), but
    # the session cookie is still forgeable with a known SECRET_KEY, so the
    # same warn-in-dev / refuse-in-prod split applies. "Production
    # intent" here is inferred from SECURE_COOKIES rather than an SMTP flag
    # (this app has no SMTP concept).
    secret_key = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    if secret_key not in _INSECURE_SECRET_KEYS:
        return
    if os.getenv("SECURE_COOKIES", "false").lower() == "true":
        raise RuntimeError(
            "SECRET_KEY is still the insecure default, but SECURE_COOKIES=true (production "
            "intent). Refusing to start: this would let anyone forge session/CSRF cookies. "
            "Set a real SECRET_KEY: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    logger.warning(
        "SECRET_KEY is still the insecure default. Fine for local dev, but must be a "
        "random value before any real deployment."
    )


def compat_admin_redirect_target(rest_of_path: str, query: str = "") -> str:
    """Relative path for old /admin/* bookmarks. Never a protocol-relative URL.

    `GET /admin/{path}` is world-reachable (crm-web). Concatenating
    `/{rest_of_path}` would turn `/admin//evil.example` into
    `Location: //evil.example`. Only a single relative path is allowed;
    query string is forwarded so `/admin/deals?due=1` keeps the Due filter.
    """
    if (
        not rest_of_path
        or rest_of_path.startswith("/")
        or rest_of_path.startswith("\\")
        or "://" in rest_of_path
        or "\\" in rest_of_path
        or "//" in rest_of_path
    ):
        return "/partners"
    target = f"/{rest_of_path}"
    if query:
        target = f"{target}?{query}"
    return target


def _production_intent() -> bool:
    """True when Secure cookies would be on, or BASE_URL is https.

    `should_use_secure_cookies()` already treats unset SECURE_COOKIES +
    https BASE_URL as production. The extra BASE_URL check covers
    SECURE_COOKIES=false with an https BASE_URL so /docs still stays off.
    """
    return should_use_secure_cookies() or os.getenv("BASE_URL", "").startswith("https://")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_secret_key()
    warn_if_non_ip_trusted_proxies()
    init_db()
    maybe_bootstrap_admin()
    yield


def create_app() -> FastAPI:
    disable_docs = _production_intent()
    application = FastAPI(
        title="crm",
        lifespan=lifespan,
        docs_url=None if disable_docs else "/docs",
        redoc_url=None if disable_docs else "/redoc",
        openapi_url=None if disable_docs else "/openapi.json",
    )
    application.mount("/static", StaticFiles(directory="app/static"), name="static")

    application.include_router(auth.router)
    application.include_router(admin.router)
    application.include_router(api.router)
    application.include_router(mcp.router)
    application.include_router(oauth.router)

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # Catch here so every 500 still gets the headers below. Re-raising
            # would skip this middleware and leave Starlette's bare 500.
            logger.exception("Unhandled error on %s", request.url.path)
            if request.url.path.startswith("/api") or request.url.path == "/mcp":
                response = JSONResponse({"error": "server_error"}, status_code=500)
            else:
                response = Response("Internal Server Error", status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @application.get("/", response_class=HTMLResponse)
    async def root(request: Request, user=Depends(get_current_user)):
        if user:
            return RedirectResponse("/partners")
        return RedirectResponse("/auth/login")

    @application.get("/health")
    async def health():
        # Cheap write-lock probe: proves the sqlite file is writable and not
        # stuck. A full or read-only volume is a total outage; do not report
        # healthy. Exempt from the rate limiter (this handler never calls it).
        global _health_db_warned
        try:
            with get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
        except Exception as exc:
            if not _health_db_warned:
                logger.warning("health: database check failed: %s", exc)
                _health_db_warned = True
            return JSONResponse({"status": "unavailable"}, status_code=503)
        _health_db_warned = False
        return {"status": "ok"}

    # Compat redirects: every page used to live under /admin/*. Old bookmarks
    # and links in docs (e.g. CRM_ADMIN_URL) should keep working after the
    # /admin prefix was dropped from every route.
    @application.get("/admin", include_in_schema=False)
    async def admin_root_redirect():
        return RedirectResponse("/partners", status_code=307)

    @application.get("/admin/{rest_of_path:path}", include_in_schema=False)
    async def admin_redirect(request: Request, rest_of_path: str):
        return RedirectResponse(
            compat_admin_redirect_target(rest_of_path, request.url.query),
            status_code=307,
        )

    return application


app = create_app()
templates = Jinja2Templates(directory="app/templates")
