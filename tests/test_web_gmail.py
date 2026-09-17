import base64
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.gmail_service import Gmail, GmailError, build_reply, parse_message
from app.main import app
from app.models import FollowupThread, User
from tests.test_policy_classifier import msg


@pytest.fixture
def client(db, monkeypatch):
    import app.main as main
    import app.oauth as oauth

    factory = sessionmaker(db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(main, "Session", factory)
    monkeypatch.setattr(oauth, "Session", factory)

    @contextmanager
    def lock(uid):
        yield True

    monkeypatch.setattr(main, "user_lock", lock)
    with TestClient(app) as client:
        yield client


def login(client, user):
    import json

    payload = base64.b64encode(
        json.dumps({"user_id": user.id, "version": user.session_version, "csrf": "valid"}).encode()
    )
    cookie = TimestampSigner(settings().session_secret).sign(payload).decode()
    client.cookies.set("session", cookie)


def test_health_and_landing(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "Connect Gmail" in client.get("/").text
    assert client.post("/scan").status_code == 403


def test_invalid_oauth_state(client):
    assert client.get("/auth/callback?state=bad&code=secret").status_code == 400


def test_cross_user_and_escaped_preview(client, db, user):
    other = User(email="other@example.com")
    db.add(other)
    db.flush()
    thread = FollowupThread(
        user_id=other.id,
        gmail_thread_id="private",
        original_message_id="private",
        recipient_email="hr@example.org",
        original_sent_at=msg().at,
        subject="Private",
    )
    db.add(thread)
    db.commit()
    login(client, user)
    assert "Private" not in client.get("/").text.replace("Private by deployment", "")
    assert client.post(f"/threads/{thread.id}/include", data={"csrf": "valid"}).status_code == 404
    thread.user_id = user.id
    thread.subject = "<script>alert('bad')</script>"
    db.commit()
    page = client.get("/").text
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_settings_validation(client, user):
    login(client, user)
    assert (
        client.post(
            "/settings",
            data={"csrf": "valid", "timezone": "Nonsense", "morning": "99:00", "evening": "18:00"},
        ).status_code
        == 422
    )
    assert client.post("/settings", data={"csrf": "wrong"}).status_code == 403


def test_mime_parsing_and_thread_headers():
    encoded = base64.urlsafe_b64encode(b"I attached my CV for a job").decode()
    parsed = parse_message(
        {
            "id": "1",
            "threadId": "thread",
            "internalDate": "1788249600000",
            "labelIds": ["SENT"],
            "payload": {
                "headers": [{"name": "To", "value": "Recruiter <hr@example.org>"}],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encoded}},
                    {"filename": "resume.pdf", "mimeType": "application/pdf", "body": {}},
                ],
            },
        }
    )
    assert parsed.recipients == [("Recruiter", "hr@example.org")]
    assert parsed.attachments == ["resume.pdf"]
    assert "CV" in parsed.body
    reply = build_reply(
        msg(), "hr@example.org", "owner@example.com", "Hi", "<unique@example.com>", 1
    )
    mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(reply["raw"]))
    assert mime["In-Reply-To"] == "<original@example.com>"
    assert mime["Message-ID"] == "<unique@example.com>"
    assert mime["References"] == "<original@example.com>"
    assert reply["threadId"] == "thread"


def test_send_not_retried(monkeypatch):
    import httpx

    gmail = Gmail("fake")
    gmail.credentials = Mock(valid=True, token="fake")
    request = Mock(side_effect=httpx.ReadTimeout("Timeout"))
    monkeypatch.setattr("app.gmail_service.httpx.request", request)
    with pytest.raises(GmailError):
        gmail.request("POST", "messages/send", json={})
    assert request.call_count == 1


def test_read_retries_are_bounded(monkeypatch):
    import httpx

    gmail = Gmail("fake")
    gmail.credentials = Mock(valid=True, token="fake")
    request = Mock(side_effect=httpx.ReadTimeout("Timeout"))
    monkeypatch.setattr("app.gmail_service.httpx.request", request)
    monkeypatch.setattr("app.gmail_service.time.sleep", lambda seconds: None)
    with pytest.raises(GmailError):
        gmail.get_thread("thread")
    assert request.call_count == 3


def test_oauth_success_stores_encrypted_token(client, db, monkeypatch):
    import json
    import time

    import app.oauth as oauth
    from app.gmail_service import SCOPES
    from app.security import decrypt_token

    payload = base64.b64encode(
        json.dumps({"oauth": {"state": "expected", "verifier": "pkce", "at": time.time()}}).encode()
    )
    client.cookies.set("session", TimestampSigner(settings().session_secret).sign(payload).decode())
    response = Mock()
    response.json.return_value = {
        "refresh_token": "sensitive-test-token",
        "scope": " ".join(SCOPES),
    }
    monkeypatch.setattr(oauth.httpx, "post", Mock(return_value=response))
    monkeypatch.setattr(oauth.Gmail, "get_authenticated_user_email", lambda self: "new@example.com")
    result = client.get("/auth/callback?state=expected&code=one-use-code", follow_redirects=False)
    assert result.status_code == 303
    user = db.query(User).filter_by(email="new@example.com").one()
    assert decrypt_token(user.encrypted_google_refresh_token) == "sensitive-test-token"
    assert "sensitive-test-token" not in result.headers.get("set-cookie", "")
    assert "sensitive-test-token" != user.encrypted_google_refresh_token


def test_expired_oauth_state(client):
    import json

    payload = base64.b64encode(
        json.dumps({"oauth": {"state": "expected", "verifier": "pkce", "at": 0}}).encode()
    )
    client.cookies.set("session", TimestampSigner(settings().session_secret).sign(payload).decode())
    assert client.get("/auth/callback?state=expected&code=code").status_code == 400


def test_revoked_refresh_token_is_permanent():
    from google.auth.exceptions import RefreshError

    from app.gmail_service import GmailAuthError

    gmail = Gmail("fake")
    gmail.credentials = Mock(valid=False)
    gmail.credentials.refresh.side_effect = RefreshError("invalid_grant")
    with pytest.raises(GmailAuthError):
        gmail.get_thread("thread")
    assert gmail.credentials.refresh.call_count == 1
