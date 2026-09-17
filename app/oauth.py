import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.database import Session
from app.gmail_service import SCOPES, Gmail
from app.models import User
from app.security import encrypt_token

router = APIRouter()


@router.get("/auth/connect")
def connect(request: Request):
    cfg = settings()
    if not cfg.google_client_id or not cfg.google_client_secret:
        raise HTTPException(503, "Configure your Google OAuth client in .env first.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    request.session["oauth"] = {"state": state, "verifier": verifier, "at": time.time()}
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "client_id": cfg.google_client_id,
        "redirect_uri": cfg.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@router.get("/auth/callback")
def callback(request: Request, state: str = "", code: str = "", error: str = ""):
    saved = request.session.pop("oauth", None)
    if (
        not saved
        or not secrets.compare_digest(saved["state"], state)
        or time.time() - saved["at"] > 600
    ):
        raise HTTPException(400, "OAuth session expired or invalid. Connect again.")
    if error or not code:
        raise HTTPException(400, "Gmail connection was not completed.")
    cfg = settings()
    try:
        response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": cfg.google_client_id,
                "client_secret": cfg.google_client_secret,
                "redirect_uri": cfg.google_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": saved["verifier"],
            },
            timeout=30,
        )
        response.raise_for_status()
        tokens = response.json()
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise HTTPException(
                400,
                "Google did not return a refresh token. Remove this app in Google Account permissions and reconnect.",
            )
        granted = set(tokens.get("scope", "").split())
        if not set(SCOPES).issubset(granted):
            raise HTTPException(
                400, "Both Gmail read and send permissions are required. Reconnect and select both."
            )
        email = Gmail(refresh).get_authenticated_user_email()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Google authorization failed. Please reconnect.") from None
    allowed = {e.strip().lower() for e in cfg.allowed_emails.split(",") if e.strip()}
    if allowed and email not in allowed:
        raise HTTPException(403, "This account is not allowed on this installation.")
    with Session() as db:
        user = db.scalar(select(User).where(User.email == email))
        if not user:
            user = User(email=email, timezone=cfg.default_timezone)
            db.add(user)
        user.encrypted_google_refresh_token = encrypt_token(refresh)
        user.enabled = True
        db.commit()
        request.session.clear()
        request.session.update({"user_id": user.id, "version": user.session_version})
    return RedirectResponse("/", status_code=303)
