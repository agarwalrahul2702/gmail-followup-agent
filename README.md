# gmail-followup-agent

A small, self-hosted Gmail follow-up service for individual job seekers. Clone it, connect **your own Gmail account**, and customize **your own rules and signature**. No shared credentials, hosted AI agent, or paid model is required.

**Zero LLM calls by default.** Python handles scheduling, reply detection, templates, counting, and duplicate protection. Optional OpenAI classification only handles ambiguous originals, once per discovered thread, with a per-run call limit.

> Dashboard screenshot placeholder: connect a test Gmail account, then add a redacted screenshot here. No real inbox screenshot is distributed.

## Quick start

Requires Docker with Compose and Python 3. No local PostgreSQL or Python packages needed for this path.

```sh
git clone https://github.com/agarwalrahul2702/gmail-followup-agent.git
cd gmail-followup-agent
python3 scripts/setup.py
# Edit .env: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (see Google setup below).
# Edit rules.toml: set signature to YOUR name and review the follow-up text.
docker compose up --build -d
```

Open [localhost:8000](http://localhost:8000), click **Connect Gmail**, and grant both permissions. The setup script creates `.env` with new encryption and session keys, file permissions `0600`, and refuses to overwrite existing secrets. Alternatively copy `.env.example` to `.env` and generate your own secrets; blank secret placeholders intentionally fail startup.

**Automatic sending defaults to enabled**, as specified in the project brief. It can process due mail at the next scheduler tick after connecting. To inspect first, connect before the morning run time and immediately uncheck **Automatic sending**, save, then scan. For a guaranteed offline onboarding, set `SCHEDULER_ENABLED=false` before starting and pause automatic sending before pressing Scan. A scan sends due mail when automatic sending is enabled.

Each deployment owns its database, Google client, optional model key, and rules file. Multiple connected accounts are isolated by login, but `rules.toml` is installation-wide. For different rules per person, run a separate clone/deployment.

## Features and rules

- Original resume submissions, referral requests, recruiter outreach and job-help requests.
- Default FU1 on Day 3 and FU2 on Day 7, both from the original timestamp in the account timezone. Saturday/Sunday due dates move to Monday. Never send on a weekend; no holiday calendar.
- Morning processing defaults to 09:15; evening dashboard snapshot to 18:35, Asia/Kolkata. Edit timezone/times per account.
- Maximum two follow-ups. At most one follow-up per thread per local day, including overdue catch-up.
- Any external participant response in the Gmail thread ends the sequence, including automated replies and bounces. Completed/closed threads are no longer polled.
- Manual follow-ups count toward the limit. Unrecognized later sent messages stop automation for review.
- Excludes interview coordination, acknowledgements, feedback, thank-you-only messages, notifications, mailing lists, existing replies and follow-ups.
- Exactly one external recipient is required. Group messages are excluded. Greeting is `Hi,` because names are not inferred from email addresses.
- Dashboard, review queue, safe include/exclude/close/reopen actions, manual processing, pause, reconnect and disconnect.
- Daily report includes sends, recipient/domain, original date, replies, duplicates, completed/review counts, errors and the next three working days (plus overdue items).

### Customize without code or tokens

Edit [`rules.toml`](rules.toml). Changes load on the next run, including existing active sequences:

```toml
followup_days = [3, 7]  # two increasing offsets; maximum remains two
scan_days = 30         # discovery window, 1–90 days
signature = "Your Name"
exclude_phrases = ["contract work", "not interested"]
max_ai_calls_per_run = 10
```

Both templates support only `{greeting}` and `{signature}`. Invalid templates or day offsets fail validation. The blank signature falls back to the connected address, never the project author's name. Additional deterministic classification rules live in `app/classifier.py`.

`CLASSIFIER_MODE=rules` never calls OpenAI even if a key exists. Strong, explicit resume/job referral combinations can qualify automatically; unclear job requests go to **Review required**. Exclusion checks take precedence. These intentionally conservative patterns favor missed candidates over unwanted sends; they are not a perfect semantic classifier.

To enable optional classification, set `CLASSIFIER_MODE=hybrid`, `OPENAI_API_KEY`, and optionally `OPENAI_MODEL` (default `gpt-4o-mini`). Only ambiguous messages reach the model. Requests contain bounded original text (2,000 characters), subject, sender, recipients and up to five attachment names; no attachment content or complete thread is sent. Results are structured and validated; eligible confidence must be at least 0.80 and category must not be EXCLUDED. Errors, refusals and lower confidence require review. The per-run cap also applies to manual runs; use provider project budgets for a monthly spending limit. Existing results are persisted, so repeated scans do not reclassify known threads.

Implementation follows the [official OpenAI structured output documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

## Google Cloud and OAuth setup

1. Create your own project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Gmail API** in APIs & Services → Library.
3. Configure Google Auth Platform / OAuth consent screen. For a personal test deployment, choose an external app in Testing and add your Gmail address as a test user.
4. Create an OAuth client of type **Web application**. Add the exact authorized redirect URI `http://localhost:8000/auth/callback`.
5. Put its client ID and client secret in `.env`. These are your Google application's credentials, not your Gmail password.
6. For a deployed site, set `APP_ENV=production`, `APP_BASE_URL=https://your-host`, `GOOGLE_REDIRECT_URI=https://your-host/auth/callback`, and register that exact HTTPS redirect in Google.
7. Optionally set `ALLOWED_EMAILS=you@example.com` (comma-separated) to restrict who can connect to your instance.

Exact OAuth scopes:

- `https://www.googleapis.com/auth/gmail.readonly`: read sent messages, complete threads and the connected profile.
- `https://www.googleapis.com/auth/gmail.send`: send threaded follow-ups.

No delete/modify mailbox permission is requested. Threaded replies set the thread ID, subject, References and In-Reply-To as required by [Gmail threading documentation](https://developers.google.com/workspace/gmail/api/guides/threads). See [Google's scope classification](https://developers.google.com/workspace/gmail/api/auth/scopes) for verification requirements. [Testing-mode refresh tokens](https://developers.google.com/identity/protocols/oauth2#expiration) for these scopes normally expire after seven days; reconnect or publish your consent configuration as appropriate. A public multi-user OAuth application may require Google verification; cloning this repository does not bypass that process.

State, ten-minute OAuth expiry, and PKCE protect the callback. Google must return both scopes and a refresh token. The authenticated Gmail profile is the app identity; there are no passwords. Missing/revoked tokens require reconnection. Permanent refresh-token failures or HTTP 401 pause the account so the scheduler does not retry indefinitely.

## How duplicate protection works

1. A PostgreSQL **session advisory lock per user** serializes manual scans, scheduler runs and settings changes across app instances. Use a direct database connection or session pooling, **not transaction-mode PgBouncer**.
2. New threads are discovered before any sends. Records are unique per account/thread. Multiple known original outreach threads for one recipient block automatic sequences, even if one was previously completed. This is deliberately conservative; finish such contacts manually. The discovery window defaults to 30 days; older, previously untracked separate threads are outside that discovery window.
3. Each active thread is row-locked, then the complete Gmail thread is read. External replies close it. Agent headers, durable Message-ID and deterministic follow-up phrases identify existing sends. Other later sent messages trigger review.
4. Gmail sends are reconciled into missing database timestamps. A disagreement never causes an automatic resend.
5. The full thread is fetched again just before sending. A **durable send intent** (number and unique Message-ID) is committed before calling Gmail while retaining the account advisory lock.
6. A successful send updates state. If the process dies before that update, the next run reconciles Gmail. If acceptance cannot be established, it stops for review. POST sends are **never automatically retried**, including HTTP 429/5xx/timeouts, since acceptance may be unknown.

Gmail and PostgreSQL do not share a transaction and Gmail send has no client idempotency key. This project does not claim mathematically exact-once delivery. It chooses a possible missed follow-up over retrying an uncertain send. Uncertain intents cannot be reopened in the UI; inspect Gmail and handle that sequence manually. Do not erase the database or send-intent records as a retry mechanism. Manual Gmail sends or recipient replies can still race the final API check; no external client can eliminate that final network window.

## Architecture and stack

Python 3.12, FastAPI/Jinja2, SQLAlchemy 2, PostgreSQL, Alembic, Google OAuth/Gmail REST API, Fernet encryption, optional OpenAI SDK. One periodic scheduler thread; no Redis, Celery, frontend build, or per-user cron jobs.

```mermaid
flowchart TD
    U[User] --> O[Google OAuth + PKCE]
    O --> A[FastAPI dashboard]
    A --> G[Gmail API]
    A --> P[(PostgreSQL)]
    A --> R[Python rules and templates]
    R -. Optional ambiguous classification .-> AI[OpenAI]
    S[Single periodic scheduler] --> A
```

Each five-minute tick checks local times. `SchedulerRun` has a unique account/job/local-date constraint and persists completion and report data. Incomplete jobs are retried under the account lock after restart. Completed daily jobs are not repeated. Thread-level failures appear in activity; that thread is reconciled on the next morning/manual run. Long jobs are serialized; this MVP favors individual deployments over large multi-tenant mailboxes.

## Run without Docker

Use Python 3.12 and a running PostgreSQL 16+ server. Create a dedicated database and role; point `DATABASE_URL` to `postgresql+psycopg://USER:PASSWORD@localhost:5432/DATABASE`. URL-encode special password characters. SQLite is only supported in unit tests because it cannot provide the production locking guarantees.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/setup.py  # only if .env does not exist
# Edit DATABASE_URL and Google credentials in .env.
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

`GET /health` checks the database and returns `{"status":"ok"}`. Docker startup applies Alembic migrations automatically. For multi-instance production, run migrations once before rollout, then override the container command to run only Uvicorn. The image respects Cloud Run's `PORT` variable and runs as a non-root user.

## Cloud Run deployment overview

You need your own Google Cloud project, billing, Cloud SQL PostgreSQL and Secret Manager entries. This repository does not create billable cloud resources automatically.

1. Build/push the Docker image to Artifact Registry. Use an amd64 image for Cloud Run if building on Apple Silicon: `docker buildx build --platform linux/amd64 -t YOUR_IMAGE --push .`.
2. Create Cloud SQL PostgreSQL and a least-privilege database user. Connect the service with the Cloud SQL connector/socket or a private network; use `postgresql+psycopg://USER:PASSWORD@/DATABASE?host=/cloudsql/PROJECT:REGION:INSTANCE` for a mounted socket.
3. Store OAuth client secret, encryption key, session secret and database URL in Secret Manager. Grant only the necessary service account access. Keep encryption/session keys stable across revisions.
4. Apply `alembic upgrade head` once with a Cloud Run Job before serving traffic.
5. Deploy the service with HTTPS production settings and the registered redirect. For the embedded scheduler, use **[instance-based billing/always-allocated CPU](https://docs.cloud.google.com/run/docs/configuring/billing-settings) and at least one minimum instance**. Request-only CPU or scale-to-zero will not reliably run an in-process scheduler.
6. Alternatively disable the web scheduler with `SCHEDULER_ENABLED=false`, deploy a Cloud Run Job using `python -m app.worker`, and trigger that one job every five minutes using Cloud Scheduler with authenticated IAM access. Use the same database, keys and rules image. No public job-trigger HTTP endpoint is exposed.
7. Verify `/health`, restrict `ALLOWED_EMAILS`, and connect your account.

A local Compose deployment is included; a live Cloud Run deployment requires your project and credentials.

## Security and privacy

- Refresh tokens are encrypted with authenticated Fernet encryption. Cookies contain only signed login/session metadata and OAuth state/verifier; never access or refresh tokens. Production cookies are Secure, HttpOnly and SameSite=Lax.
- All browser mutations require a CSRF token; all records are scoped to the authenticated account. Disconnect clears credentials and invalidates existing app sessions, and attempts Google token revocation.
- Jinja autoescaping prevents email previews from becoming HTML. Errors are generic; logs omit tokens and complete email bodies. Uvicorn access logging is disabled to avoid OAuth codes in query logs; apply the same policy at your reverse proxy/cloud logging layer.
- Database storage: IDs, recipient/address domain, subject, at most a 500-character original preview, classification reason, send intents and timestamps, activity and report snapshots. Full bodies are fetched transiently from Gmail. A classifier reason may summarize sensitive email content.
- OpenAI is not contacted in rules mode. Hybrid mode uses `store=false`; provider retention policies still apply. Do not enable it if you do not want original email excerpts sent to OpenAI.
- Disconnect does not delete history, because it is useful for duplicate prevention on reconnect. An installation owner can remove their dedicated database/volume for full deletion; doing so also removes all send history safeguards. Back up both the database and encryption key securely.
- Compose binds HTTP to localhost and does not publish the database port. The Compose database password is a local development default on a private Docker network; use unique credentials and managed secrets for public infrastructure.

## Tests and CI

```sh
ruff check .
ruff format --check .
pytest -q
# Run the real lock/concurrency/scheduler integration tests against a DISPOSABLE DB:
TEST_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost/TEST_DB pytest -q
```

**Integration tests truncate their configured database tables. Never point TEST_DATABASE_URL at real data.** Without it, those integration tests are skipped; unit/web tests use temporary in-memory databases. Gmail and OpenAI are mocked; no live email is sent by tests. GitHub Actions runs PostgreSQL migrations, lint, all tests and Docker build on pushes and pull requests. Dependencies are pinned in `requirements.txt`; update using `uv pip compile requirements.in -o requirements.txt` and rerun tests.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Invalid encryption/session key | Run setup on a fresh clone, or restore your existing valid secrets. Do not rotate encryption keys without re-encrypting tokens. |
| OAuth redirect mismatch | Match `.env` and the Google client's authorized URI exactly, including port/path. |
| No refresh token / revoked access | Remove the app grant in Google Account permissions and reconnect. |
| Nothing sent | Check auto-send, local time, due dates, weekend, classification and activity. There must be one recipient and no reply/ambiguous later message. |
| Duplicate originals / uncertain send | Inspect Gmail, close the sequence and handle manually. Do not clear pending intents. |
| Changes to exclusions do not reclassify old mail | Existing decisions are cached. Exclude a recorded thread in the dashboard; edit rules before new discovery. |
| Gmail 429/5xx | Reads retry at most three times with backoff. Sends pause for review because acceptance may be uncertain. |
| Scheduler idle on Cloud Run | Use always-allocated CPU + minimum instance, or the authenticated scheduled worker job. |
| Disconnect revocation could not reach Google | Local credentials are still removed. Revoke the app manually in Google Account permissions. |

## Roadmap

Optional per-account templates, shorter metadata retention, richer deterministic outreach rules, and a dedicated recovery audit UI. No plans to add LLM-generated follow-ups or distributed queue infrastructure to the individual-user default.

## License

[MIT](LICENSE).
