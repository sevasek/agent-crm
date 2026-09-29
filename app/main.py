from contextlib import asynccontextmanager
import logging
import os
import shutil
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app.database import get_db, get_db_path, init_db
from app.routers import admin, auth, api, mcp, oauth
from app.routers.auth import get_current_user
from app.services.auth import maybe_bootstrap_admin, should_use_secure_cookies
from app.services.client_ip import warn_if_non_ip_trusted_proxies

_health_db_warned = False
_health_disk_warned = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_INSECURE_SECRET_KEYS = {"dev-secret-change-in-prod"}
_API_KEY_ENVS = ("CRM_API_KEY", "CRM_STAGES_API_KEY", "CRM_MCP_API_KEY")

# BEGIN IMMEDIATE + rollback (the DB probe in /health) never dirties a page,
# so it stays healthy on a full disk right up until a real write fails.
# Check free space on the DB's filesystem directly instead of waiting for
# that write.
HEALTH_MIN_FREE_MB = int(os.getenv("HEALTH_MIN_FREE_MB", "200"))


def _low_disk_space() -> bool:
    try:
        free_bytes = shutil.disk_usage(os.path.dirname(get_db_path()) or ".").free
    except OSError:
        return False
    return free_bytes < HEALTH_MIN_FREE_MB * 1024 * 1024


def _production_intent() -> bool:
    """True when Secure cookies would be on, or BASE_URL is https.

    `should_use_secure_cookies()` already treats unset SECURE_COOKIES +
    https BASE_URL as production. The extra BASE_URL check covers
    SECURE_COOKIES=false with an https BASE_URL so /docs still stays off.
    """
    return should_use_secure_cookies() or os.getenv("BASE_URL", "").startswith("https://")


def _api_keys_configured() -> bool:
    return any(os.getenv(name, "").strip() for name in _API_KEY_ENVS)


def _explicit_disable_docs() -> bool | None:
    raw = os.getenv("DISABLE_DOCS")
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _docs_disabled() -> bool:
    """Hide Swagger/ReDoc/OpenAPI unless this is clearly local-only.

    DISABLE_DOCS=true always hides; DISABLE_DOCS=false always shows.
    Unset: hide when production-intent (Secure cookies / https BASE_URL)
    or any CRM_*_KEY is set — an http BASE_URL leftover must not keep
    the schema public on a box that already has API keys.
    """
    explicit = _explicit_disable_docs()
    if explicit is not None:
        return explicit
    return _production_intent() or _api_keys_configured()


def _check_secret_key():
    # This app has no public-facing surface (no signup, no email links), but
    # the session cookie is still forgeable with a known SECRET_KEY, so the
    # same warn-in-dev / refuse-in-prod split applies. "Production
    # intent" is https BASE_URL or Secure cookies — not only SECURE_COOKIES=true,
    # so an https deploy that left the example default in .env still refuses.
    secret_key = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    if secret_key not in _INSECURE_SECRET_KEYS:
        return
    if _production_intent():
        raise RuntimeError(
            "SECRET_KEY is still the insecure default, but this process looks like "
            "a real deployment (SECURE_COOKIES=true or https BASE_URL). Refusing to "
            "start: this would let anyone forge session/CSRF cookies. Set a real "
            "SECRET_KEY: python -c \"import secrets; print(secrets.token_hex(32))\""
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


_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _cross_site_cookie_request(request: Request) -> bool:
    """True for a cookie-authenticated, state-changing request whose
    Origin/Referer names a different host than the one it was sent to.

    Defense-in-depth alongside the session-bound CSRF token (a token minted
    outside the target session already fails validation on its own); this
    only rejects when the browser *did* send Origin or Referer and it
    disagrees with Host, so it never breaks a client that omits both.
    """
    if request.method not in _STATE_CHANGING_METHODS:
        return False
    if not request.cookies.get("session"):
        return False
    host = request.headers.get("host", "")
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        if urlsplit(value).netloc != host:
            return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_secret_key()
    warn_if_non_ip_trusted_proxies()
    init_db()
    maybe_bootstrap_admin()
    yield


def create_app() -> FastAPI:
    disable_docs = _docs_disabled()
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
        if _cross_site_cookie_request(request):
            return Response("Cross-origin request blocked", status_code=403)
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

        global _health_disk_warned
        if _low_disk_space():
            if not _health_disk_warned:
                logger.warning("health: free disk space below HEALTH_MIN_FREE_MB=%s", HEALTH_MIN_FREE_MB)
                _health_disk_warned = True
            return JSONResponse({"status": "low_disk_space"}, status_code=503)
        _health_disk_warned = False
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
