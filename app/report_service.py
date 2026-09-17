from collections import Counter
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.followup_policy import due_at
from app.models import ActivityLog, FollowupThread, utcnow
from app.rules import load_rules


def report(db, user, now=None):
    local = (now or utcnow()).astimezone(ZoneInfo(user.timezone))
    start = datetime.combine(local.date(), time(), ZoneInfo(user.timezone))
    events = list(
        db.scalars(
            select(ActivityLog)
            .where(
                ActivityLog.user_id == user.id,
                ActivityLog.created_at >= start,
                ActivityLog.created_at < start + timedelta(days=1),
            )
            .order_by(ActivityLog.created_at.desc())
        )
    )
    threads = list(
        db.scalars(
            select(FollowupThread)
            .where(FollowupThread.user_id == user.id)
            .order_by(FollowupThread.original_sent_at.desc())
        )
    )
    counts = Counter(e.event_type for e in events)
    counts["REVIEW_REQUIRED"] = sum(t.status == "REVIEW_REQUIRED" for t in threads)
    counts["COMPLETED"] = sum(t.status == "COMPLETED" for t in threads)
    end, days = local.date(), 0
    while days < 3:
        end += timedelta(days=1)
        days += end.weekday() < 5
    rules = load_rules()
    upcoming = []
    for thread in threads:
        if thread.status == "ACTIVE":
            number = 2 if thread.followup1_sent_at else 1
            due = due_at(thread.original_sent_at, user.timezone, rules.followup_days[number - 1])
            if due.date() <= end:
                upcoming.append(
                    {"recipient": thread.recipient_email, "number": number, "due": due.isoformat()}
                )
    lookup = {thread.id: thread for thread in threads}
    activity = []
    for event in events:
        thread = lookup.get(event.followup_thread_id)
        activity.append(
            {
                "event": event.event_type,
                "at": event.created_at.isoformat(),
                "recipient": thread.recipient_email if thread else "",
                "domain": thread.company_or_domain if thread else "",
                "original_date": thread.original_sent_at.isoformat() if thread else "",
                "message": event.message,
            }
        )
    return (
        {
            "date": local.date().isoformat(),
            "counts": dict(counts),
            "upcoming": upcoming,
            "activity": activity,
        },
        events,
        threads,
    )
