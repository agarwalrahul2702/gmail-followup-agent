import uuid
from datetime import timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.classifier import classify
from app.database import Session, user_lock
from app.followup_policy import due_at
from app.gmail_service import Gmail, authored_body, external_reply, followup_message, is_own
from app.models import ActivityLog, FollowupThread, User, utcnow
from app.rules import load_rules


def log(db, user, event, message, thread=None):
    db.add(
        ActivityLog(
            user_id=user.id,
            followup_thread_id=thread.id if thread else None,
            event_type=event,
            message=message,
        )
    )


def close(thread, status, reason):
    thread.status, thread.closed_reason, thread.closed_at = status, reason, utcnow()


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def discover(db, user, gmail, rules):
    known = {
        t.gmail_thread_id: t
        for t in db.scalars(select(FollowupThread).where(FollowupThread.user_id == user.id))
    }
    budget = [rules.max_ai_calls_per_run]
    # Complete discovery before any sends, so copied originals can block each other.
    for item in gmail.list_recent_sent_messages(rules.scan_days):
        thread_id = item["threadId"]
        if thread_id in known:
            continue
        messages = gmail.get_thread(thread_id)
        if not messages:
            continue
        original = messages[0]
        if not is_own(original, user.email):
            continue
        recipients = original.recipients
        valid = (
            len(recipients) == 1
            and "@" in recipients[0][1]
            and recipients[0][1].lower() != user.email.lower()
        )
        classification = classify(original, rules, budget)
        recipient = recipients[0][1].lower() if valid else ""
        thread = FollowupThread(
            user_id=user.id,
            gmail_thread_id=thread_id,
            original_message_id=original.id,
            recipient_email=recipient,
            recipient_name="",
            company_or_domain=recipient.rsplit("@", 1)[-1] if recipient else "",
            subject=original.headers.get("subject", "")[:500],
            preview=authored_body(original.body)[:500],
            original_sent_at=original.at,
            classification=classification.category,
            confidence=classification.confidence,
            reason=classification.reason,
            status=classification.status if valid else "EXCLUDED",
        )
        if not valid:
            thread.reason = "Requires exactly one external recipient; group/CC/BCC mail excluded"
        db.add(thread)
        db.flush()
        known[thread_id] = thread
        log(db, user, "THREAD_DISCOVERED", "Original sent message discovered", thread)
        log(
            db,
            user,
            thread.status if thread.status != "ACTIVE" else "CLASSIFIED",
            thread.reason,
            thread,
        )
    db.commit()


def reconcile(db, user, thread, messages):
    original = next((m for m in messages if m.id == thread.original_message_id), None)
    if not original:
        close(thread, "REVIEW_REQUIRED", "Original missing from Gmail thread")
        return None, []
    thread.last_checked_at = utcnow()
    if external_reply(messages, original, user.email):
        close(thread, "REPLIED", "External participant responded")
        log(db, user, "REPLY_DETECTED", "External reply detected", thread)
        return original, []
    later = [
        m for m in messages if m.id != original.id and m.at >= original.at and is_own(m, user.email)
    ]
    followups = [
        m
        for m in later
        if followup_message(m)
        or (thread.pending_message_id and m.headers.get("message-id") == thread.pending_message_id)
    ]
    if any(m not in followups for m in later):
        close(
            thread, "REVIEW_REQUIRED", "Additional sent message is ambiguous or a copied original"
        )
        log(db, user, "REVIEW_REQUIRED", thread.closed_reason, thread)
        return original, followups
    old_count = int(thread.followup1_sent_at is not None) + int(
        thread.followup2_sent_at is not None
    )
    for number, message in enumerate(followups[:2], 1):
        setattr(thread, f"followup{number}_sent_at", message.at)
    if len(followups) > old_count:
        log(
            db,
            user,
            "DUPLICATE_PREVENTED",
            "Synchronized follow-ups already present in Gmail",
            thread,
        )
    if thread.pending_number:
        match = any(m.headers.get("message-id") == thread.pending_message_id for m in later)
        if match or len(followups) >= thread.pending_number:
            thread.pending_number = thread.pending_message_id = None
        else:
            close(
                thread,
                "REVIEW_REQUIRED",
                "Send outcome uncertain; no automatic retry. Verify Gmail manually.",
            )
            return original, followups
    if old_count > len(followups):
        close(
            thread, "REVIEW_REQUIRED", "Gmail and stored send history disagree; no automatic resend"
        )
    elif len(followups) >= 2:
        close(thread, "COMPLETED", "Two follow-ups found")
    return original, followups


def process_thread(db, user, thread_id, gmail, rules, now):
    thread = db.scalar(
        select(FollowupThread)
        .where(FollowupThread.id == thread_id, FollowupThread.user_id == user.id)
        .with_for_update()
    )
    if not thread or thread.status != "ACTIVE":
        return
    original, followups = reconcile(db, user, thread, gmail.get_thread(thread.gmail_thread_id))
    if thread.status != "ACTIVE":
        db.commit()
        return
    # One automatic sequence per recipient, including previous completed sequences.
    duplicates = list(
        db.scalars(
            select(FollowupThread).where(
                FollowupThread.user_id == user.id,
                FollowupThread.recipient_email == thread.recipient_email,
                FollowupThread.id != thread.id,
                FollowupThread.classification != "EXCLUDED",
            )
        )
    )
    if duplicates:
        close(
            thread,
            "REVIEW_REQUIRED",
            "Multiple original outreach threads for recipient; resolve manually",
        )
        log(db, user, "DUPLICATE_PREVENTED", thread.closed_reason, thread)
        db.commit()
        return
    number = len(followups) + 1
    if (
        not user.automatic_sending_enabled
        or now.astimezone(ZoneInfo(user.timezone)).weekday() >= 5
        or now
        < due_at(aware(thread.original_sent_at), user.timezone, rules.followup_days[number - 1])
    ):
        db.commit()
        return
    # Avoid back-to-back overdue FU1/FU2 in repeated manual scans on the same day.
    if (
        followups
        and now.astimezone(ZoneInfo(user.timezone)).date()
        <= followups[-1].at.astimezone(ZoneInfo(user.timezone)).date()
    ):
        db.commit()
        return
    # A second full fetch immediately before reserving and sending.
    original, fresh_followups = reconcile(
        db, user, thread, gmail.get_thread(thread.gmail_thread_id)
    )
    if thread.status != "ACTIVE" or len(fresh_followups) != len(followups):
        db.commit()
        return
    thread.pending_number = number
    thread.pending_message_id = f"<followup-{uuid.uuid4().hex}@gmail-followup-agent.local>"
    db.commit()  # Durable intent BEFORE crossing the external side-effect boundary.
    body = getattr(rules, f"template{number}").format(
        greeting="Hi,", signature=rules.signature or user.email
    )
    try:
        gmail.send_thread_reply(
            original, thread.recipient_email, user.email, body, thread.pending_message_id, number
        )
    except Exception:
        close(
            thread,
            "REVIEW_REQUIRED",
            "Send outcome uncertain. Verify Gmail manually; automatic retry disabled.",
        )
        log(db, user, "ERROR", thread.closed_reason, thread)
        db.commit()
        return
    setattr(thread, f"followup{number}_sent_at", now)
    thread.pending_number = thread.pending_message_id = None
    if number == 2:
        close(thread, "COMPLETED", "Two follow-ups sent")
    log(db, user, f"FU{number}_SENT", f"Follow-up {number} sent", thread)
    db.commit()


def process_user_locked(user_id, gmail_factory=None, now=None):
    gmail_factory = gmail_factory or Gmail.for_user
    now = now or utcnow()
    with Session() as db:
        user = db.get(User, user_id)
        if not user or not user.enabled or not user.encrypted_google_refresh_token:
            return
        gmail, rules = gmail_factory(user), load_rules()
        try:
            discover(db, user, gmail, rules)
        except Exception:
            db.rollback()
            log(
                db,
                user,
                "ERROR",
                "Discovery failed; no sends attempted. Check Gmail authorization and connectivity.",
            )
            db.commit()
            raise RuntimeError("Discovery failed") from None
        ids = list(
            db.scalars(
                select(FollowupThread.id).where(
                    FollowupThread.user_id == user_id, FollowupThread.status == "ACTIVE"
                )
            )
        )
        for thread_id in ids:
            try:
                process_thread(db, user, thread_id, gmail, rules, now)
            except Exception:
                db.rollback()
                thread = db.get(FollowupThread, thread_id)
                log(
                    db,
                    user,
                    "ERROR",
                    "Thread processing failed; will reconcile Gmail before retrying",
                    thread,
                )
                db.commit()


def process_user(user_id):
    with user_lock(user_id) as acquired:
        if acquired:
            process_user_locked(user_id)
        return acquired
