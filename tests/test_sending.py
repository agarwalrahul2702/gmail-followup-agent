from datetime import datetime, timedelta, timezone

from app.followup_service import discover, process_thread
from app.models import FollowupThread
from app.rules import Rules
from tests.test_policy_classifier import msg

NOW = datetime(2026, 9, 14, 10, tzinfo=timezone.utc)


class FakeGmail:
    def __init__(self, messages=None):
        self.messages = messages or [msg()]
        self.sent = []
        self.fail = False
        self.reads = 0

    def get_thread(self, thread_id):
        self.reads += 1
        return list(self.messages)

    def list_recent_sent_messages(self, days):
        return [{"threadId": "thread"}]

    def send_thread_reply(self, original, recipient, sender, body, message_id, number):
        self.sent.append(number)
        message = msg(body)
        message.id = "fu" + str(number)
        message.at = NOW
        message.headers["x-followup-agent"] = str(number)
        message.headers["message-id"] = message_id
        self.messages.append(message)
        if self.fail:
            raise TimeoutError("Unknown send outcome")
        return {"id": message.id}


def setup_thread(db, user, gmail=None):
    gmail = gmail or FakeGmail()
    discover(db, user, gmail, Rules())
    thread = db.query(FollowupThread).first()
    return thread, gmail


def manual(number=1):
    message = msg("Just checking if you had a chance to review my profile")
    message.id = f"manual{number}"
    message.at += timedelta(days=4 + number)
    return message


def test_send_once_repeated_processing(db, user):
    thread, gmail = setup_thread(db, user)
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert gmail.sent == [1]
    assert gmail.reads >= 4


def test_manual_fu1_sync_and_fu2(db, user):
    thread, gmail = setup_thread(db, user, FakeGmail([msg(), manual()]))
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert gmail.sent == [2]
    assert thread.followup1_sent_at is not None
    assert thread.status == "COMPLETED"


def test_max_two(db, user):
    thread, gmail = setup_thread(db, user, FakeGmail([msg(), manual(1), manual(2)]))
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert gmail.sent == []
    assert thread.status == "COMPLETED"


def test_crash_after_send_before_database_update(db, user):
    thread, gmail = setup_thread(db, user)
    thread.pending_number = 1
    thread.pending_message_id = "<durable-intent>"
    db.commit()
    gmail.send_thread_reply(
        msg(), thread.recipient_email, user.email, "Custom template", "<durable-intent>", 1
    )
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert gmail.sent == [1]
    assert thread.followup1_sent_at is not None
    assert thread.pending_number is None


def test_pending_unknown_never_retries(db, user):
    thread, gmail = setup_thread(db, user)
    thread.pending_number, thread.pending_message_id = 1, "<unknown>"
    db.commit()
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert not gmail.sent
    assert thread.status == "REVIEW_REQUIRED"


def test_timeout_no_retry(db, user):
    thread, gmail = setup_thread(db, user)
    gmail.fail = True
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert gmail.sent == [1]
    assert thread.pending_number == 1
    assert thread.status == "REVIEW_REQUIRED"


def test_external_reply_closes(db, user):
    reply = manual()
    reply.sent = False
    reply.headers["from"] = "other@example.org"
    thread, gmail = setup_thread(db, user, FakeGmail([msg(), reply]))
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert thread.status == "REPLIED"
    assert not gmail.sent


def test_immediate_recheck_catches_reply(db, user):
    thread, gmail = setup_thread(db, user)
    read = gmail.get_thread

    def changing(thread_id):
        messages = read(thread_id)
        if gmail.reads >= 3:
            reply = manual()
            reply.sent = False
            reply.headers["from"] = "hr@example.org"
            messages.append(reply)
        return messages

    gmail.get_thread = changing
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert not gmail.sent
    assert thread.status == "REPLIED"


def test_ambiguous_extra_sent_message(db, user):
    extra = manual()
    extra.body = "Here are the details you asked for"
    thread, gmail = setup_thread(db, user, FakeGmail([msg(), extra]))
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert thread.status == "REVIEW_REQUIRED"
    assert not gmail.sent


def test_duplicate_recipient_threads(db, user):
    thread, gmail = setup_thread(db, user)
    copy = FollowupThread(
        user_id=user.id,
        gmail_thread_id="copy",
        original_message_id="copy",
        recipient_email=thread.recipient_email,
        original_sent_at=thread.original_sent_at,
        classification="RESUME_OUTREACH",
        status="ACTIVE",
    )
    db.add(copy)
    db.commit()
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    assert not gmail.sent
    assert thread.status == "REVIEW_REQUIRED"


def test_pause_and_weekend(db, user):
    thread, gmail = setup_thread(db, user)
    user.automatic_sending_enabled = False
    process_thread(db, user, thread.id, gmail, Rules(), NOW)
    user.automatic_sending_enabled = True
    process_thread(db, user, thread.id, gmail, Rules(), NOW + timedelta(days=5))
    assert not gmail.sent
