"""Fixtures for the live-LLM behavioral evaluation harness.

Sets up an isolated PostgreSQL database (``assistant_llm_eval``), synthetic
users (ids >= 990001), and a local file-storage dir, and builds providers from
the REAL production components (imported, never copied). Model configuration is
derived from the environment; the real ``CHAT_*`` / ``EMBEDDING_*``
credentials are used as-is and are never printed.
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Environment bootstrap. This MUST run before any ``assistant`` settings are
# read (``get_settings`` is lru_cached and the engine reads it at call time).
# Real model credentials come from the shell env (preferred) or the project
# ``.env`` (backfilled only when absent). The eval DB and storage dir are then
# forced so the harness never touches production data. Nothing secret is logged.
# ---------------------------------------------------------------------------
EVAL_DB_NAME = "assistant_llm_eval"
EVAL_DB_URL = f"postgresql+asyncpg://assistant:assistant@localhost:5432/{EVAL_DB_NAME}"
EVAL_STORAGE_DIR = "/tmp/telegram-assistant-llm-eval/files"
EVAL_USER_ID_BASE = 990001
FAKE_TELEGRAM_TOKEN = "123456789:LLM-EVAL-FAKE"

# Keys we may backfill from .env when the shell env does not export them.
_BACKFILL_KEYS = {
    "CHAT_API_KEY",
    "CHAT_BASE_URL",
    "CHAT_MODEL",
    "CHAT_TIMEOUT_SECONDS",
    "CHAT_THINKING_ENABLED",
    "CHAT_THINKING_BUDGET_TOKENS",
    "EMBEDDING_API_KEY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "CHAT_HISTORY_MESSAGES",
}

_env_bootstrapped = False


def bootstrap_env() -> None:
    """Point the harness at the isolated eval DB + local storage, preserving the
    real model credentials already present in the environment."""
    global _env_bootstrapped
    if _env_bootstrapped:
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            # Strip a trailing inline comment / surrounding quotes (no secrets
            # are ever printed).
            if key in _BACKFILL_KEYS and key not in os.environ and val:
                if " #" in val:
                    val = val.split(" #", 1)[0].strip()
                val = val.strip("'\"")
                if val:
                    os.environ[key] = val
    # Force eval-target overrides AFTER backfill so they win.
    os.environ["DATABASE_URL"] = EVAL_DB_URL
    os.environ["FILE_STORAGE_DIR"] = EVAL_STORAGE_DIR
    os.environ["TELEGRAM_BOT_TOKEN"] = FAKE_TELEGRAM_TOKEN
    # Main profile is thinking ON (per the deployment env); never set
    # CHAT_REASONING_EFFORT. Leave it untouched.
    os.environ.setdefault("CHAT_THINKING_ENABLED", "true")
    Path(EVAL_STORAGE_DIR).mkdir(parents=True, exist_ok=True)
    _env_bootstrapped = True


bootstrap_env()

# ---------------------------------------------------------------------------
# Telegram network hard-block (V5.4 §29-P2).
#
# The mandated production stack imports aiogram transitively (the library
# import performs no network call), so the library cannot be banned. Instead we
# hard-block the worker's outbound seams and RECORD any attempt, so the
# ``p0_no_telegram`` check fails the run if a real Bot API call ever fires
# during a turn — even one originating from the mandated services. The root
# seam is ``notifications._get_bot`` (every real send path builds a Bot there);
# the send wrappers are patched too so a path that bypasses ``_get_bot`` is
# still blocked and recorded. Called once at the end of this module.
# ---------------------------------------------------------------------------
telegram_send_attempts: list[dict] = []


def _blocking_tg_send(seam: str, *args, **kwargs):
    telegram_send_attempts.append({"seam": seam, "args": args, "kwargs": kwargs})
    raise RuntimeError(f"Telegram network is blocked in the LLM eval ({seam})")


def guard_telegram() -> None:
    import assistant.services.notifications as n  # noqa: PLC0415
    import assistant.services.tg_text as t  # noqa: PLC0415

    def _no_bot():
        telegram_send_attempts.append({"seam": "_get_bot"})
        raise RuntimeError("Telegram Bot creation is blocked in the LLM eval")

    n._get_bot = _no_bot
    n.send_text = lambda *a, **k: _blocking_tg_send("send_text", *a, **k)
    t.send_long = lambda *a, **k: _blocking_tg_send("send_long", *a, **k)


from assistant.ai import (  # noqa: E402  (import after env bootstrap)
    OpenAIChatProvider,
    OpenAICompatibleProvider,
    OpenAIEmbeddingProvider,
)
from assistant.config import get_settings  # noqa: E402
from assistant.db import get_session_factory  # noqa: E402
from assistant.models.calendar_items import (  # noqa: E402
    CalendarItem,
    ItemKind,
    ItemPriority,
)
from assistant.models.facts import FactStatus, UserFact  # noqa: E402
from assistant.models.files import UserFile  # noqa: E402
from assistant.models.reminders import Reminder  # noqa: E402
from assistant.models.users import User, UserSettings  # noqa: E402
from assistant.models.workout_logs import WorkoutLog  # noqa: E402
from assistant.services import (  # noqa: E402
    calendar as calendar_service,
)
from assistant.services import (  # noqa: E402
    facts as facts_service,
)
from assistant.services import (  # noqa: E402
    reminders as reminders_service,
)
from assistant.services import (  # noqa: E402
    workouts as workouts_service,
)
from assistant.services.files import register_local_upload  # noqa: E402

# The engine/session factory read get_settings() lazily; clear any cache built
# from a stale environment before we rely on the eval-target settings.
get_settings.cache_clear()

_SESSION_FACTORY = None


def session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = get_session_factory()
    return _SESSION_FACTORY


@asynccontextmanager
async def session(expire_on_commit: bool = False) -> AsyncSession:
    """A session from the shared factory.

    Defaults to ``expire_on_commit=False`` to match the production session
    factory (``db/engine.py``). ``run_turn`` and ``files._run_pipeline`` commit
    internally and then read ORM attributes (``user.settings`` /
    ``file.telegram_file_id``); with committed-instance-expiry disabled those
    reads hit the populated instance dict instead of triggering a synchronous
    lazy refresh that fails in async (``MissingGreenlet`` /
    ``DetachedInstanceError``).
    """
    factory = session_factory()
    async with factory(expire_on_commit=expire_on_commit) as s:
        yield s


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Provider construction (real components, explicit thinking flag)
# ---------------------------------------------------------------------------
def make_provider(thinking_enabled: bool):
    """Build a real OpenAI-compatible chat (+ optional embedding) provider.

    ``thinking_enabled`` is honored for structured calls by passing it directly
    to ``OpenAIChatProvider``; the A/B profile passes ``False``.
    """
    s = get_settings()
    chat = OpenAIChatProvider(
        api_key=s.chat_api_key,
        base_url=s.chat_base_url,
        model=s.chat_model,
        timeout=s.chat_timeout_seconds,
        thinking_enabled=thinking_enabled,
        thinking_budget_tokens=(
            s.chat_thinking_budget_tokens if thinking_enabled else None
        ),
    )
    emb = None
    if s.embedding_configured:
        emb = OpenAIEmbeddingProvider(
            api_key=s.embedding_api_key,
            base_url=s.embedding_base_url,
            model=s.embedding_model,
            dimensions=s.embedding_dimensions,
        )
    return OpenAICompatibleProvider(chat=chat, embedding=emb)


def effective_model_profile() -> dict:
    """The recorded effective model settings (never includes the API key)."""
    s = get_settings()
    return {
        "model": s.chat_model,
        "base_url": s.chat_base_url,
        "timeout_seconds": s.chat_timeout_seconds,
        "thinking_enabled": s.chat_thinking_enabled,
        "thinking_budget_tokens": s.chat_thinking_budget_tokens,
        "reasoning_effort": s.chat_reasoning_effort,
        "history_messages": s.chat_history_messages,
        "embedding_configured": s.embedding_configured,
        "embedding_model": s.embedding_model if s.embedding_configured else None,
        "embedding_dimensions": (
            s.embedding_dimensions if s.embedding_configured else None
        ),
    }


# ---------------------------------------------------------------------------
# Synthetic users
# ---------------------------------------------------------------------------
@dataclass
class EvalUser:
    user: User
    user_id: int
    timezone: str
    language: str
    seed: dict = field(default_factory=dict)  # ids of seeded rows, for cleanup/diff


async def make_user(
    *,
    user_id: int,
    timezone: str = "UTC",
    language: str = "ru",
    first_name: str = "Евалюатор",
) -> EvalUser:
    ev = EvalUser(
        user=None, user_id=user_id, timezone=timezone, language=language
    )  # type: ignore[arg-type]
    async with session() as s:
        user = User(
            id=user_id,
            first_name=first_name,
            last_name="Тест",
            username=f"llm_eval_{user_id}",
            is_bot=False,
        )
        s.add(user)
        s.add(
            UserSettings(
                user_id=user_id, timezone=timezone, language=language
            )
        )
        await s.commit()
        # Load the settings relationship into the instance so the (soon
        # detached) ``ev.user`` carries it in its __dict__. Seeding services
        # read ``user.settings.timezone`` to normalize naive datetimes; without
        # this the detached object would trigger a lazy load and raise
        # DetachedInstanceError.
        await s.refresh(user, attribute_names=["settings"])
        ev.user = user
    return ev


# ---------------------------------------------------------------------------
# Seeding (real domain services)
# ---------------------------------------------------------------------------
async def seed_calendar_item(
    ev: EvalUser,
    *,
    title: str,
    starts_at: datetime,
    kind: str = "event",
    due_at: datetime | None = None,
    ends_at: datetime | None = None,
    priority: str = "normal",
    status: str = "scheduled",
) -> CalendarItem:
    async with session() as s:
        item = await calendar_service.create_item(
            s,
            ev.user,
            title=title,
            kind=ItemKind(kind),
            starts_at=starts_at,
            ends_at=ends_at,
            due_at=due_at,
            priority=ItemPriority(priority),
            source="eval-seed",
        )
        if status != "scheduled":
            item.status = status
        await s.commit()
        await s.refresh(item)
        ev.seed.setdefault("items", []).append(item.id)
        return item


async def seed_reminder(
    ev: EvalUser, *, fire_at: datetime, message: str
) -> Reminder:
    async with session() as s:
        rem = await reminders_service.create_reminder(
            s, ev.user, fire_at=_utc(fire_at), message=message
        )
        await s.commit()
        await s.refresh(rem)
        ev.seed.setdefault("reminders", []).append(rem.id)
        return rem


async def seed_fact(
    ev: EvalUser,
    *,
    value: str,
    category: str = "general",
    status: str = FactStatus.confirmed.value,
) -> UserFact:
    async with session() as s:
        fact = await facts_service.propose_fact(
            s, ev.user, value=value, category=category
        )
        if status == FactStatus.confirmed.value:
            await facts_service.confirm_fact(s, ev.user, fact.id)
        await s.commit()
        await s.refresh(fact)
        ev.seed.setdefault("facts", []).append(fact.id)
        return fact


async def seed_workout(
    ev: EvalUser, *, name: str, started_at: datetime, duration_minutes: int
) -> WorkoutLog:
    async with session() as s:
        log = await workouts_service.log_workout(
            s,
            ev.user,
            name=name,
            started_at=_utc(started_at),
            duration_minutes=duration_minutes,
        )
        await s.commit()
        await s.refresh(log)
        ev.seed.setdefault("workouts", []).append(log.id)
        return log


async def seed_file(
    ev: EvalUser, *, original_filename: str, text: str, mime_type: str = "text/plain"
):
    """Upload + ingest a real text file (real embeddings) so RAG can find it.

    ``register_local_upload`` writes the bytes and enqueues the ingest job; we
    then run the ingest pipeline synchronously so the chunks are queryable
    before the case's turn executes.
    """
    from assistant.models.jobs import BackgroundJob  # noqa: PLC0415
    from assistant.services.files import _run_pipeline  # noqa: PLC0415

    async with session() as s:
        file = await register_local_upload(
            s,
            ev.user,
            original_filename=original_filename,
            mime_type=mime_type,
            data=text.encode("utf-8"),
        )
        await s.commit()
        await s.refresh(file)
        ev.seed.setdefault("files", []).append(file.id)
        job = (
            await s.scalars(
                select(BackgroundJob).where(
                    BackgroundJob.idempotency_key == f"file:{file.id}"
                )
            )
        ).first()
        if job is None:
            raise RuntimeError("ingest job not enqueued for seeded file")
        # _run_pipeline revalidates the job lease (revalidate_ownership)
        # before committing chunks, so the job must be CLAIMED first — the
        # worker always claims before running the handler. A pending
        # (unclaimed) job fails the ownership recheck and the pipeline rolls
        # back silently, leaving the file stuck in "embedding".
        from assistant.services.jobs import claim_jobs  # noqa: PLC0415

        # Claim one at a time until THIS file's job is the claimed one: the
        # eval DB may hold leftover pending jobs from earlier seeds, and
        # claim_jobs claims oldest-first.
        claimed_job = None
        for _ in range(64):
            claimed = await claim_jobs(s, worker_id="eval-seed", limit=1)
            if not claimed:
                break
            for c in claimed:
                if c.id == job.id:
                    claimed_job = c
        if claimed_job is None:
            raise RuntimeError("could not claim ingest job for seeded file")
        job = claimed_job
        await _run_pipeline(s, file, job)
        await s.commit()
        await s.refresh(file)
        return file


# ---------------------------------------------------------------------------
# DB snapshot / diff — deterministic oracle for "no mutation before confirm"
# and mutation-invariant checks. Covers the user's actual mutation surfaces.
# ---------------------------------------------------------------------------
def _snap_rows(rows) -> list[tuple]:
    return sorted(
        [
            (
                r.id,
                r.title,
                r.kind,
                (r.starts_at.isoformat() if r.starts_at else None),
                (r.ends_at.isoformat() if r.ends_at else None),
                (r.due_at.isoformat() if r.due_at else None),
                r.status,
                r.priority,
            )
            for r in rows
        ]
    )


async def snapshot_user_state(ev: EvalUser) -> dict:
    """A deterministic fingerprint of the user's mutable state. Chat messages
    are intentionally excluded (they legitimately grow every turn)."""
    async with session() as s:
        items = (
            await s.scalars(select(CalendarItem).where(CalendarItem.user_id == ev.user_id))
        ).all()
        reminders = (
            await s.scalars(select(Reminder).where(Reminder.user_id == ev.user_id))
        ).all()
        workouts = (
            await s.scalars(select(WorkoutLog).where(WorkoutLog.user_id == ev.user_id))
        ).all()
        facts = (
            await s.scalars(select(UserFact).where(UserFact.user_id == ev.user_id))
        ).all()
        files = (
            await s.scalars(select(UserFile).where(UserFile.user_id == ev.user_id))
        ).all()
    return {
        "items": _snap_rows(items),
        "reminders": [
            (
                r.id,
                (r.fire_at.isoformat() if r.fire_at else None),
                r.message,
                r.status,
            )
            for r in sorted(reminders, key=lambda x: x.id)
        ],
        "workouts": [
            (
                w.id,
                w.name,
                (w.started_at.isoformat() if w.started_at else None),
                w.duration_minutes,
                w.status,
            )
            for w in sorted(workouts, key=lambda x: x.id)
        ],
        "facts": [
            (f.id, f.value, f.status, f.category)
            for f in sorted(facts, key=lambda x: x.id)
        ],
        "files": [f.id for f in sorted(files, key=lambda x: x.id)],
    }


def state_diff(before: dict, after: dict) -> dict:
    """Per-table added/removed rows (lists of the tuple rows)."""
    out = {}
    for table in before:
        b = set(before[table])
        a = set(after[table])
        out[table] = {"added": sorted(a - b), "removed": sorted(b - a)}
    return out


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
# Data tables owned by the eval DB (excludes alembic_version). The harness owns
# this database, so a full TRUNCATE gives deterministic, reproducible runs.
_DATA_TABLES = (
    "file_chunks",
    "user_files",
    "chat_messages",
    "pending_actions",
    "user_facts",
    "reminders",
    "calendar_items",
    "workout_logs",
    "background_jobs",
    "user_settings",
    "users",
)


async def reset_eval_db() -> None:
    """Wipe all eval data so a run starts from a known-empty database."""
    from sqlalchemy import text  # noqa: PLC0415

    async with session() as s:
        for table in _DATA_TABLES:
            await s.execute(
                text(
                    f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"  # noqa: S608
                )
            )
        await s.commit()


async def dispose() -> None:
    from assistant.db import dispose_engine  # noqa: PLC0415

    await dispose_engine()


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def rel(*, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    return now_utc() + timedelta(days=days, hours=hours, minutes=minutes)


# Enforce the Telegram network hard-block now that the service modules are
# imported (module scope: runs once when the harness loads).
guard_telegram()
