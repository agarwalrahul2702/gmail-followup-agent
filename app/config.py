from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: Literal["development", "production", "test"] = "development"
    app_base_url: str = "http://localhost:8000"
    database_url: str = "postgresql+psycopg://followup:followup@db/followup"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/callback"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    classifier_mode: Literal["rules", "hybrid"] = "rules"
    token_encryption_key: str
    session_secret: str
    default_timezone: str = "Asia/Kolkata"
    scheduler_enabled: bool = True
    rules_file: str = "rules.toml"
    allowed_emails: str = ""

    @model_validator(mode="after")
    def validate_secrets(self):
        Fernet(self.token_encryption_key.encode())
        if len(self.session_secret) < 32 or "replace" in self.session_secret.lower():
            raise ValueError("Generate SESSION_SECRET with scripts/setup.py")
        ZoneInfo(self.default_timezone)
        if self.app_env == "production" and not self.app_base_url.startswith("https://"):
            raise ValueError("Production requires HTTPS")
        if not self.database_url.startswith("postgresql") and self.app_env != "test":
            raise ValueError("PostgreSQL is required for cross-worker locking")
        return self


@lru_cache
def settings():
    return Settings()
