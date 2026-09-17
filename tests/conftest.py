import os

from cryptography.fernet import Fernet

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite://")
os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["SESSION_SECRET"] = "test-session-secret-with-at-least-32-characters"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["CLASSIFIER_MODE"] = "rules"
os.environ["OPENAI_API_KEY"] = ""

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def user(db):
    user = User(
        email="owner@example.com", encrypted_google_refresh_token="encrypted", timezone="UTC"
    )
    db.add(user)
    db.commit()
    return user
