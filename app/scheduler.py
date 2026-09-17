import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database import Session, user_lock
from app.followup_service import process_user_locked
from app.models import SchedulerRun, User, utcnow
from app.report_service import report


def tick(now=None):
    now = now or utcnow()
    with Session() as db:
        ids = list(db.scalars(select(User.id).where(User.enabled.is_(True))))
    for user_id in ids:
        with user_lock(user_id) as acquired:
            if not acquired:
                continue
            with Session() as db:
                user = db.get(User, user_id)
                if not user.enabled:
                    continue
                local = now.astimezone(ZoneInfo(user.timezone))
                for job, scheduled in (
                    ("MORNING_FOLLOWUPS", user.morning_run_time),
                    ("EVENING_REPORT", user.evening_report_time),
                ):
                    if local.strftime("%H:%M") < scheduled:
                        continue
                    run = db.scalar(
                        select(SchedulerRun).where(
                            SchedulerRun.user_id == user_id,
                            SchedulerRun.job_type == job,
                            SchedulerRun.local_date == local.date(),
                        )
                    )
                    if run and run.completed_at:
                        continue
                    if not run:
                        run = SchedulerRun(user_id=user_id, job_type=job, local_date=local.date())
                        db.add(run)
                        db.commit()
                    try:
                        if job == "MORNING_FOLLOWUPS":
                            process_user_locked(user_id, now=now)
                        run.report = report(db, user, now)[0]
                        run.completed_at = utcnow()
                        db.commit()
                    except Exception:
                        db.rollback()
                        logging.warning(
                            "Scheduled job failed for user id %s; safe retry next tick", user_id
                        )


def loop(stop):
    while not stop.is_set():
        try:
            tick()
        except Exception:
            logging.warning("Scheduler tick failed; retrying on next interval")
        stop.wait(300)
