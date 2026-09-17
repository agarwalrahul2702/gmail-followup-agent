import secrets

from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.config import settings


def encrypt_token(value):
    return Fernet(settings().token_encryption_key.encode()).encrypt(value.encode()).decode()


def decrypt_token(value):
    return Fernet(settings().token_encryption_key.encode()).decrypt(value.encode()).decode()


def csrf_token(request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def check_csrf(request, token):
    expected = request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(expected, token):
        raise HTTPException(403, "Invalid form token. Reload the page.")
