import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from html import unescape

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.config import settings
from app.security import decrypt_token

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class GmailError(Exception):
    pass


@dataclass
class Message:
    id: str
    thread_id: str
    at: datetime
    headers: dict
    body: str
    attachments: list[str]
    sent: bool

    @property
    def sender(self):
        return parseaddr(self.headers.get("from", ""))[1].lower()

    @property
    def recipients(self):
        return [
            (name, address)
            for name, address in getaddresses(
                [self.headers[k] for k in ("to", "cc", "bcc") if self.headers.get(k)]
            )
            if address
        ]


def parse_message(raw):
    payload = raw.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain, html, attachments = [], [], []

    def walk(part):
        if part.get("filename"):
            attachments.append(part["filename"])
            return
        data = part.get("body", {}).get("data")
        if data and part.get("mimeType") in ("text/plain", "text/html"):
            value = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                "utf-8", "replace"
            )
            (plain if part["mimeType"] == "text/plain" else html).append(value)
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    body = "\n".join(plain) if plain else unescape(re.sub(r"<[^>]+>", " ", "\n".join(html)))
    return Message(
        raw["id"],
        raw["threadId"],
        datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc),
        headers,
        body,
        attachments,
        "SENT" in raw.get("labelIds", []),
    )


def authored_body(body):
    return re.split(
        r"(?mi)^On .+wrote:|^>.*|^-{2,}\s*Original Message|^From:|^-{2,}\s*Forwarded", body
    )[0].strip()


def is_own(message, email):
    # SENT also identifies Gmail send-as aliases without a broader settings scope.
    return message.sent or message.sender == email.lower()


def external_reply(messages, original, email):
    return any(
        m.id != original.id and m.at >= original.at and not is_own(m, email) for m in messages
    )


def followup_message(message):
    return bool(message.headers.get("x-followup-agent")) or bool(
        re.search(
            r"follow[ -]?up|following up|checking (?:in|if)|any updates|had a chance",
            authored_body(message.body),
            re.I,
        )
    )


def build_reply(original, recipient, sender, body, message_id, number):
    if not original.headers.get("message-id"):
        raise GmailError("Original has no Message-ID; manual review required")
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = sender
    subject = original.headers.get("subject", "")
    message["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
    message["In-Reply-To"] = original.headers["message-id"]
    message["References"] = (
        original.headers.get("references", "") + " " + original.headers["message-id"]
    ).strip()
    message["Message-ID"] = message_id
    message["X-Followup-Agent"] = str(number)
    message.set_content(body)
    return {
        "threadId": original.thread_id,
        "raw": base64.urlsafe_b64encode(message.as_bytes()).decode(),
    }


class Gmail:
    def __init__(self, refresh_token):
        cfg = settings()
        self.credentials = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cfg.google_client_id,
            client_secret=cfg.google_client_secret,
        )

    @classmethod
    def for_user(cls, user):
        return cls(decrypt_token(user.encrypted_google_refresh_token))

    def request(self, method, path, **kwargs):
        try:
            if not self.credentials.valid:
                self.credentials.refresh(Request())
        except Exception:
            raise GmailError("Gmail authorization failed. Reconnect Gmail.") from None
        # Never retry sends: a timeout can mean the message was accepted.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = httpx.request(
                    method,
                    "https://gmail.googleapis.com/gmail/v1/users/me/" + path,
                    headers={"Authorization": f"Bearer {self.credentials.token}"},
                    timeout=30,
                    **kwargs,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < attempts - 1:
                        time.sleep(2**attempt)
                        continue
                if response.is_error:
                    raise GmailError(f"Gmail request failed (HTTP {response.status_code}).")
                return response.json()
            except httpx.TransportError:
                if attempt == attempts - 1:
                    raise GmailError(
                        "Gmail connection failed; send outcome may be unknown."
                    ) from None
                time.sleep(2**attempt)

    def list_recent_sent_messages(self, days=30):
        token = None
        while True:
            params = {"q": f"in:sent newer_than:{days}d", "maxResults": 100}
            if token:
                params["pageToken"] = token
            page = self.request("GET", "messages", params=params)
            yield from page.get("messages", [])
            token = page.get("nextPageToken")
            if not token:
                break

    def get_message(self, message_id):
        return parse_message(
            self.request("GET", f"messages/{message_id}", params={"format": "full"})
        )

    def get_thread(self, thread_id):
        raw = self.request("GET", f"threads/{thread_id}", params={"format": "full"})
        return sorted(
            (parse_message(m) for m in raw.get("messages", [])), key=lambda m: (m.at, m.id)
        )

    def get_authenticated_user_email(self):
        return self.request("GET", "profile")["emailAddress"].lower()

    def send_thread_reply(self, original, recipient, sender, body, message_id, number):
        return self.request(
            "POST",
            "messages/send",
            json=build_reply(original, recipient, sender, body, message_id, number),
        )
