import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from sqlalchemy import select, text

from app.database import Base, Session, engine, user_lock
from app.models import FollowupThread, SchedulerRun, User

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="Set TEST_DATABASE_URL for real PostgreSQL integration tests",
)


@pytest.fixture(autouse=True)
def schema():
    if os.environ.get("TEST_DATABASE_URL"):
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE activity_logs, scheduler_runs, followup_threads, users RESTART IDENTITY CASCADE"
                )
            )
    yield


def test_two_workers_cannot_lock_same_account():
    barrier = Barrier(2)

    def worker():
        with user_lock(12345) as acquired:
            barrier.wait(timeout=10)
            return acquired

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(results) == [False, True]


def test_scheduler_once_and_timezone(monkeypatch):
    import app.scheduler as scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "process_user_locked", lambda user_id, now: calls.append(user_id)
    )
    with Session() as db:
        user = User(
            email="tz@example.com",
            timezone="Asia/Kolkata",
            morning_run_time="09:15",
            evening_report_time="18:35",
        )
        db.add(user)
        db.commit()
    scheduler.tick(datetime(2026, 9, 14, 3, 44, tzinfo=timezone.utc))
    assert calls == []
    scheduler.tick(datetime(2026, 9, 14, 3, 45, tzinfo=timezone.utc))
    scheduler.tick(datetime(2026, 9, 14, 3, 50, tzinfo=timezone.utc))
    assert len(calls) == 1
    scheduler.tick(datetime(2026, 9, 14, 13, 5, tzinfo=timezone.utc))
    scheduler.tick(datetime(2026, 9, 14, 13, 10, tzinfo=timezone.utc))
    with Session() as db:
        runs = list(db.scalars(select(SchedulerRun)))
        assert len(runs) == 2
        assert all(run.completed_at for run in runs)


def test_two_workers_send_once(monkeypatch):
    from app.followup_service import process_user
    from app.gmail_service import Gmail
    from tests.test_sending import NOW, FakeGmail

    gmail = FakeGmail()
    monkeypatch.setattr(Gmail, "for_user", lambda user: gmail)
    # Default argument is bound at import; patch orchestration factory explicitly.
    import app.followup_service as service

    original_process = service.process_user_locked
    monkeypatch.setattr(
        service,
        "process_user_locked",
        lambda uid: original_process(uid, gmail_factory=lambda user: gmail, now=NOW),
    )
    with Session() as db:
        user = User(
            email="owner@example.com", encrypted_google_refresh_token="fake", timezone="UTC"
        )
        db.add(user)
        db.commit()
        uid = user.id
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(process_user, [uid, uid]))
    assert gmail.sent == [1]
    with Session() as db:
        assert db.scalar(select(FollowupThread)).followup1_sent_at is not None
