from contextlib import asynccontextmanager
from datetime import time
from pathlib import Path
from threading import Event, Thread
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import Session, user_lock
from app.followup_service import close, log, process_user
from app.models import FollowupThread, SchedulerRun, User
from app.oauth import router
from app.report_service import report
from app.rules import load_rules
from app.scheduler import loop
from app.security import check_csrf, csrf_token, decrypt_token


@asynccontextmanager
async def lifespan(app):
    load_rules()
    stop = Event()
    worker = Thread(target=loop, args=(stop,), daemon=True)
    if settings().scheduler_enabled:
        worker.start()
    yield
    stop.set()
    if worker.is_alive():
        worker.join(timeout=2)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings().session_secret,
    same_site="lax",
    https_only=settings().app_env == "production",
    max_age=86400,
)
app.include_router(router)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.update(
        {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'",
        }
    )
    return response


@app.exception_handler(Exception)
async def safe_error(request, exc):
    return HTMLResponse(
        "Request failed. Retry later or reconnect Gmail. No messages are retried without checking Gmail.",
        status_code=500,
    )


def current_user(request, db):
    user = db.get(User, request.session.get("user_id", 0))
    if not user or request.session.get("version") != user.session_version:
        raise HTTPException(401, "Connect Gmail to continue.")
    return user


@app.get("/health")
def health():
    with Session() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
@app.get("/review", response_class=HTMLResponse)
def dashboard(request: Request):
    with Session() as db:
        user = db.get(User, request.session.get("user_id", 0))
        if user and request.session.get("version") != user.session_version:
            user = None
        context = {
            "request": request,
            "user": user,
            "csrf": csrf_token(request),
            "rules": load_rules(),
            "review": request.url.path == "/review",
        }
        if user:
            summary, events, threads = report(db, user)
            context.update(
                summary=summary,
                events=events[:200],
                threads=[t for t in threads if t.status == "REVIEW_REQUIRED"]
                if context["review"]
                else threads,
            )
            context["evening"] = db.scalar(
                select(SchedulerRun)
                .where(
                    SchedulerRun.user_id == user.id,
                    SchedulerRun.job_type == "EVENING_REPORT",
                    SchedulerRun.completed_at.is_not(None),
                )
                .order_by(SchedulerRun.local_date.desc())
            )
        return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


@app.post("/scan")
async def scan(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    with Session() as db:
        user_id = current_user(request, db).id
    # Move synchronous Gmail work off the event loop.
    from starlette.concurrency import run_in_threadpool

    if not await run_in_threadpool(process_user, user_id):
        raise HTTPException(409, "A run or settings update is already in progress.")
    return RedirectResponse("/", 303)


@app.post("/settings")
async def update_settings(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    tz, morning, evening = (str(form.get(k, "")) for k in ("timezone", "morning", "evening"))
    try:
        ZoneInfo(tz)
        for value in (morning, evening):
            if len(value) != 5 or time.fromisoformat(value).strftime("%H:%M") != value:
                raise ValueError()
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Use a valid IANA timezone and HH:MM times.") from None
    with Session() as db:
        user = current_user(request, db)
        with user_lock(user.id) as acquired:
            if not acquired:
                raise HTTPException(409, "Processing is in progress; retry shortly.")
            user.timezone, user.morning_run_time, user.evening_report_time = tz, morning, evening
            user.automatic_sending_enabled = form.get("automatic") == "on"
            db.commit()
    return RedirectResponse("/", 303)


@app.post("/threads/{thread_id}/{action}")
async def thread_action(request: Request, thread_id: int, action: str):
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    if action not in {"include", "exclude", "close", "reopen"}:
        raise HTTPException(404)
    with Session() as db:
        user = current_user(request, db)
        with user_lock(user.id) as acquired:
            if not acquired:
                raise HTTPException(409, "Processing is in progress; retry shortly.")
            thread = db.scalar(
                select(FollowupThread)
                .where(FollowupThread.id == thread_id, FollowupThread.user_id == user.id)
                .with_for_update()
            )
            if not thread:
                raise HTTPException(404)
            if action in {"include", "reopen"}:
                if (
                    thread.pending_number
                    or thread.status in {"REPLIED", "COMPLETED"}
                    or not thread.recipient_email
                    or thread.closed_reason
                    == "Multiple original outreach threads for recipient; resolve manually"
                ):
                    raise HTTPException(
                        409,
                        "Cannot safely reopen this sequence. Inspect Gmail and close it manually.",
                    )
                thread.status, thread.closed_at, thread.closed_reason = "ACTIVE", None, None
                thread.classification, thread.confidence = "JOB_HELP_REQUEST", 1
            else:
                close(
                    thread, "EXCLUDED" if action == "exclude" else "MANUALLY_CLOSED", "User action"
                )
            log(
                db,
                user,
                "MANUALLY_CLOSED" if action == "close" else thread.status,
                "User changed thread state",
                thread,
            )
            db.commit()
    return RedirectResponse("/review" if action in {"include", "exclude"} else "/", 303)


@app.post("/disconnect")
async def disconnect(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    with Session() as db:
        user = current_user(request, db)
        with user_lock(user.id) as acquired:
            if not acquired:
                raise HTTPException(409, "Processing is in progress; retry shortly.")
            token = (
                decrypt_token(user.encrypted_google_refresh_token)
                if user.encrypted_google_refresh_token
                else None
            )
            user.encrypted_google_refresh_token, user.enabled = None, False
            user.session_version += 1
            db.commit()
            if token:
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            "https://oauth2.googleapis.com/revoke",
                            data={"token": token},
                            timeout=10,
                        )
                except httpx.HTTPError:
                    pass
    request.session.clear()
    return RedirectResponse("/", 303)
