"""Create .env with random local secrets; never overwrite an existing .env."""

import base64
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / ".env"
if path.exists():
    raise SystemExit(".env already exists; edit it directly. Secrets were not changed.")
value = (
    (root / ".env.example")
    .read_text()
    .replace(
        "TOKEN_ENCRYPTION_KEY=\n",
        "TOKEN_ENCRYPTION_KEY=" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode() + "\n",
    )
    .replace("SESSION_SECRET=\n", "SESSION_SECRET=" + secrets.token_urlsafe(48) + "\n")
)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write(value)
print("Created .env. Add your Google OAuth client ID and secret; customize rules.toml.")
