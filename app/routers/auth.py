import os

from fastapi import APIRouter, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer

from app.database import get_db
from app.services.auth import (
    authenticate_user, generate_csrf_token, validate_csrf_token,
    check_rate_limit, get_rate_limit_key, should_use_secure_cookies,
)
from app.services.client_ip import get_client_ip

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
# Cookie max_age is browser-side. loads(max_age=...) is the cryptographic
# expiry: a stolen token is rejected after this many seconds even if the
# browser still sends it. Untimed cookies from before this change fail loads.
SESSION_MAX_AGE = 60 * 60 * 24 * 30
cookie_signer = URLSafeTimedSerializer(SECRET_KEY, salt="session")


def get_current_user(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        data = cookie_signer.loads(token, max_age=SESSION_MAX_AGE)
        user_id = data.get("user_id")
        if user_id:
            with get_db() as db:
                row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if row:
                    return dict(row)
    except Exception:
        pass
    return None


def require_login(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
    return user


def set_session_cookie(response: RedirectResponse, user_id: int):
    token = cookie_signer.dumps({"user_id": user_id})
    response.set_cookie(
        "session", token,
        httponly=True, samesite="lax",
        secure=should_use_secure_cookies(),
        max_age=SESSION_MAX_AGE,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "auth/login.html", {
        "request": request, "csrf_token": generate_csrf_token()
    })


@router.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    if not validate_csrf_token(csrf_token):
        return templates.TemplateResponse(request, "auth/login.html", {
            "request": request, "error": "Invalid form submission. Please try again.",
            "csrf_token": generate_csrf_token(),
        }, status_code=400)

    user = authenticate_user(email, password)
    if user:
        response = RedirectResponse("/partners", status_code=303)
        response.delete_cookie("session")
        set_session_cookie(response, user["id"])
        return response

    # Count only failed passwords. A correct password after failures still
    # succeeds; the sixth failure in a minute is 429.
    ip = get_client_ip(request)
    if not check_rate_limit(get_rate_limit_key(ip, email), action="login"):
        return templates.TemplateResponse(request, "auth/login.html", {
            "request": request, "error": "Too many attempts. Please wait a minute and try again.",
            "csrf_token": generate_csrf_token(),
        }, status_code=429)

    return templates.TemplateResponse(request, "auth/login.html", {
        "request": request, "error": "Invalid email or password",
        "csrf_token": generate_csrf_token(),
    }, status_code=400)


@router.get("/logout")
async def logout():
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie("session")
    return response
