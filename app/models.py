from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(Timestamps, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    encrypted_google_refresh_token: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(default="Asia/Kolkata")
    morning_run_time: Mapped[str] = mapped_column(default="09:15")
    evening_report_time: Mapped[str] = mapped_column(default="18:35")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    automatic_sending_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)


class FollowupThread(Timestamps, Base):
    __tablename__ = "followup_threads"
    __table_args__ = (
        UniqueConstraint("user_id", "gmail_thread_id"),
        Index("ix_user_status", "user_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    gmail_thread_id: Mapped[str] = mapped_column(String(128))
    original_message_id: Mapped[str] = mapped_column(String(128))
    recipient_email: Mapped[str] = mapped_column(String(320))
    recipient_name: Mapped[str] = mapped_column(default="")
    company_or_domain: Mapped[str] = mapped_column(default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    preview: Mapped[str] = mapped_column(String(500), default="")
    original_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    classification: Mapped[str] = mapped_column(default="EXCLUDED")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(default="REVIEW_REQUIRED", index=True)
    followup1_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup2_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(Text)
    pending_number: Mapped[int | None] = mapped_column(Integer)
    pending_message_id: Mapped[str | None] = mapped_column(String(255))


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    followup_thread_id: Mapped[int | None] = mapped_column(ForeignKey("followup_threads.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class SchedulerRun(Base):
    __tablename__ = "scheduler_runs"
    __table_args__ = (UniqueConstraint("user_id", "job_type", "local_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    job_type: Mapped[str] = mapped_column(String(32))
    local_date: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict | None] = mapped_column(JSON)
