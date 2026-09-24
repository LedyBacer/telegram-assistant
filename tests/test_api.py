"""Mini App API tests (SPEC §20) — real PostgreSQL, signed initData, fakes.

Environment variables are set before importing the app so ``get_settings()``
reads test values; no real Telegram/OpenAI credentials are used.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant",
)
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["OPENAI_API_KEY"] = "test-key"

from assistant.api.main import create_app  # noqa: E402
from assistant.db import get_session  # noqa: E402
from assistant.models.files import FileChunk, FileState, UserFile  # noqa: E402
from assistant.models.jobs import BackgroundJob  # noqa: E402
from assistant.models.users import User  # noqa: E402
from assistant.services import actions as actions_service  # noqa: E402
from assistant.services import files  # noqa: E402
from test_init_data import make_init_data  # noqa: E402

INIT_DATA = make_init_data(
    user={"id": 777, "first_name": "T", "last_name": "X", "username": "tx"}
)
INIT_DATA_OTHER = make_init_data(user={"id": 888, "first_name": "O"})
HEADERS = {"X-Telegram-Init-Data": INIT_DATA}
HEADERS_OTHER = {"X-Telegram-Init-Data": INIT_DATA_OTHER}


@pytest_asyncio.fixture()
async def client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_missing_init_data_is_401(client: httpx.AsyncClient) -> None:
    res = await client.get("/api/v1/me")
    assert res.status_code == 401


async def test_tampered_init_data_is_401(client: httpx.AsyncClient) -> None:
    bad = make_init_data(
        tamper=lambda p: p.update({"user": '{"id": 1, "first_name": "A"}'})
    )
    res = await client.get("/api/v1/me", headers={"X-Telegram-Init-Data": bad})
    assert res.status_code == 401


async def test_stale_init_data_is_401(client: httpx.AsyncClient) -> None:
    stale = make_init_data(auth_age_seconds=10_000)
    res = await client.get("/api/v1/me", headers={"X-Telegram-Init-Data": stale})
    assert res.status_code == 401


async def test_me_upserts_and_returns_user(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    res = await client.get("/api/v1/me", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["id"] == 777
    assert body["user"]["first_name"] == "T"
    assert body["settings"]["timezone"] == "UTC"
    assert body["settings"]["motivation_enabled"] is True

    user = await session.get(User, 777)
    assert user is not None
    assert user.first_name == "T"


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
TODAY_NOON = _NOW.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
TOMORROW_NOON = (
    _NOW + timedelta(days=1)
).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()


async def test_item_crud_flow(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "Ship the report", "starts_at": TODAY_NOON, "priority": "high"},
    )
    assert res.status_code == 201
    item = res.json()
    assert item["status"] == "scheduled"
    assert item["source"] == "miniapp"
    item_id = item["id"]

    today = await client.get("/api/v1/calendar/today", headers=HEADERS)
    assert [i["title"] for i in today.json()] == ["Ship the report"]

    # Upcoming is time-of-day dependent (a noon item is past by 12:00 UTC),
    # so verify it with a deterministic tomorrow item.
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "Tomorrow thing", "starts_at": TOMORROW_NOON},
    )
    assert res.status_code == 201
    tomorrow_id = res.json()["id"]
    try:
        upcoming = await client.get("/api/v1/calendar/upcoming", headers=HEADERS)
        assert any(i["id"] == tomorrow_id for i in upcoming.json())
    finally:
        await client.delete(f"/api/v1/items/{tomorrow_id}", headers=HEADERS)

    res = await client.patch(
        f"/api/v1/items/{item_id}", headers=HEADERS, json={"title": "Shipped"}
    )
    assert res.status_code == 200
    assert res.json()["title"] == "Shipped"

    res = await client.post(f"/api/v1/items/{item_id}/complete", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "completed"
    assert res.json()["completed_at"] is not None

    res = await client.delete(f"/api/v1/items/{item_id}", headers=HEADERS)
    assert res.status_code == 204
    res = await client.get(f"/api/v1/items/{item_id}", headers=HEADERS)
    assert res.status_code == 404


async def test_item_patch_tri_state(client: httpx.AsyncClient) -> None:
    """SPEC §4.3: omitted keys are left alone, an explicit null clears the
    value. The API must expose the same tri-state semantics as the service."""
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={
            "title": "Tri-state",
            "starts_at": TODAY_NOON,
            "ends_at": TOMORROW_NOON,
            "due_at": TOMORROW_NOON,
            "description": "keep me",
        },
    )
    assert res.status_code == 201
    item_id = res.json()["id"]

    # Explicit null clears starts_at; every omitted field is untouched.
    res = await client.patch(
        f"/api/v1/items/{item_id}", headers=HEADERS, json={"starts_at": None}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["starts_at"] is None
    assert datetime.fromisoformat(body["ends_at"].replace("Z", "+00:00")) == (
        _NOW + timedelta(days=1)
    ).replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=UTC)
    assert body["due_at"] is not None
    assert body["description"] == "keep me"

    # A value can be set back.
    res = await client.patch(
        f"/api/v1/items/{item_id}", headers=HEADERS, json={"starts_at": TOMORROW_NOON}
    )
    assert res.status_code == 200
    assert datetime.fromisoformat(res.json()["starts_at"].replace("Z", "+00:00")) == (
        _NOW + timedelta(days=1)
    ).replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=UTC)


async def test_item_cancel_flow(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "Old task", "starts_at": TODAY_NOON},
    )
    item_id = res.json()["id"]
    res = await client.post(f"/api/v1/items/{item_id}/cancel", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"


async def test_items_are_user_scoped(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/items", headers=HEADERS, json={"title": "Mine", "starts_at": TODAY_NOON}
    )
    item_id = res.json()["id"]
    assert (
        await client.get(f"/api/v1/items/{item_id}", headers=HEADERS_OTHER)
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/items/{item_id}", headers=HEADERS_OTHER)
    ).status_code == 404
    other_today = await client.get("/api/v1/calendar/today", headers=HEADERS_OTHER)
    assert other_today.json() == []


async def test_item_create_validation(client: httpx.AsyncClient) -> None:
    res = await client.post("/api/v1/items", headers=HEADERS, json={"title": ""})
    assert res.status_code == 422
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "x", "kind": "not-a-kind"},
    )
    assert res.status_code == 422


async def test_item_create_with_remind_offsets(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "Call", "starts_at": TODAY_NOON, "remind_offsets_minutes": [30]},
    )
    assert res.status_code == 201

    reminders = await client.get("/api/v1/reminders", headers=HEADERS)
    data = reminders.json()
    assert len(data) == 1
    # Stored text is the item title only; the localized "Reminder:" prefix is
    # applied at delivery time in the user's current language.
    assert data[0]["message"] == "Call"
    # FastAPI serializes UTC datetimes with a Z suffix — parse, don't compare raw.
    fire_at = datetime.fromisoformat(data[0]["fire_at"].replace("Z", "+00:00"))
    expected_fire = (
        datetime.fromisoformat(TODAY_NOON).replace(tzinfo=UTC) - timedelta(minutes=30)
    )
    assert fire_at == expected_fire


async def test_item_create_remind_offsets_bounded(client: httpx.AsyncClient) -> None:
    """SPEC §14.2: >max offsets and out-of-bounds offsets are rejected (V3 P31)."""
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "x", "starts_at": TODAY_NOON, "remind_offsets_minutes": list(range(6))},
    )
    assert res.status_code == 422
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "x", "starts_at": TODAY_NOON, "remind_offsets_minutes": [2000]},
    )
    assert res.status_code == 422


async def test_reminders_list_filter_by_item(client: httpx.AsyncClient) -> None:
    """The edit view lists a single item's pending reminders (V3 P30)."""
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "Item A", "starts_at": TODAY_NOON, "remind_offsets_minutes": [30]},
    )
    assert res.status_code == 201
    item_a = res.json()["id"]
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "Item B", "starts_at": TODAY_NOON, "remind_offsets_minutes": [60]},
    )
    assert res.status_code == 201
    item_b = res.json()["id"]

    res = await client.get(f"/api/v1/reminders?item_id={item_a}", headers=HEADERS)
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["calendar_item_id"] == item_a
    assert rows[0]["offset_minutes"] == 30
    assert rows[0]["message"] == "Item A"

    res = await client.get(f"/api/v1/reminders?item_id={item_b}", headers=HEADERS)
    rows = res.json()
    assert [r["calendar_item_id"] for r in rows] == [item_b]
    assert rows[0]["offset_minutes"] == 60

    # An item without reminders has an empty list.
    res = await client.post(
        "/api/v1/items", headers=HEADERS, json={"title": "Item C"}
    )
    item_c = res.json()["id"]
    res = await client.get(f"/api/v1/reminders?item_id={item_c}", headers=HEADERS)
    assert res.status_code == 200
    assert res.json() == []


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------


async def test_workout_flow(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/workouts",
        headers=HEADERS,
        json={"name": "Legs", "duration_minutes": 40, "perceived_effort": 7},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "Legs"
    assert body["status"] == "completed"

    logs = await client.get("/api/v1/workouts", headers=HEADERS)
    assert [w["id"] for w in logs.json()] == [body["id"]]

    stats = await client.get("/api/v1/workouts/stats", headers=HEADERS)
    assert stats.json()["total"] == 1
    assert stats.json()["total_minutes"] == 40
    assert stats.json()["this_week"] == 1
    assert stats.json()["current_streak"] == 1


async def test_workout_validation(client: httpx.AsyncClient) -> None:
    res = await client.post("/api/v1/workouts", headers=HEADERS, json={"name": ""})
    assert res.status_code == 422
    res = await client.post(
        "/api/v1/workouts",
        headers=HEADERS,
        json={"name": "x", "perceived_effort": 11},
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


async def test_fact_lifecycle(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/facts",
        headers=HEADERS,
        json={"value": "Prefers morning workouts", "category": "fitness"},
    )
    assert res.status_code == 201
    fact = res.json()
    assert fact["status"] == "proposed"
    fact_id = fact["id"]

    res = await client.post(f"/api/v1/facts/{fact_id}/confirm", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "confirmed"

    confirmed = await client.get("/api/v1/facts?status=confirmed", headers=HEADERS)
    assert [f["id"] for f in confirmed.json()] == [fact_id]

    res = await client.post(
        "/api/v1/facts", headers=HEADERS, json={"value": "To be rejected"}
    )
    other_id = res.json()["id"]
    res = await client.post(f"/api/v1/facts/{other_id}/reject", headers=HEADERS)
    assert res.json()["status"] == "rejected"

    res = await client.delete(f"/api/v1/facts/{other_id}", headers=HEADERS)
    assert res.status_code == 204
    res = await client.get("/api/v1/facts", headers=HEADERS)
    assert [f["id"] for f in res.json()] == [fact_id]


async def test_fact_is_user_scoped(client: httpx.AsyncClient) -> None:
    fact_id = (
        await client.post("/api/v1/facts", headers=HEADERS, json={"value": "mine"})
    ).json()["id"]
    assert (
        await client.post(f"/api/v1/facts/{fact_id}/confirm", headers=HEADERS_OTHER)
    ).status_code == 404
    assert (
        await client.get("/api/v1/facts", headers=HEADERS_OTHER)
    ).json() == []


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


async def test_reminder_flow(client: httpx.AsyncClient) -> None:
    fire = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    res = await client.post(
        "/api/v1/reminders",
        headers=HEADERS,
        json={"fire_at": fire, "message": "Meds"},
    )
    assert res.status_code == 201
    reminder = res.json()
    assert reminder["status"] == "pending"

    listed = await client.get("/api/v1/reminders", headers=HEADERS)
    assert [r["id"] for r in listed.json()] == [reminder["id"]]

    res = await client.post(
        f"/api/v1/reminders/{reminder['id']}/cancel", headers=HEADERS
    )
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"


async def test_reminder_validation(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/reminders",
        headers=HEADERS,
        json={"fire_at": "2026-10-01T12:00:00+00:00", "message": ""},
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class _FakeProvider:
    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        return [[0.01] * 384 for _ in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        return [0.01] * 384


async def test_files_list_and_search(
    client: httpx.AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await client.get("/api/v1/me", headers=HEADERS)  # upsert user 777
    file = UserFile(
        user_id=777,
        storage_key="key-1",
        original_filename="notes.txt",
        mime_type="text/plain",
        state=FileState.indexed.value,
    )
    session.add(file)
    await session.flush()
    session.add(
        FileChunk(
            user_id=777,
            file_id=file.id,
            position=0,
            text="The quarterly plan covers launch milestones.",
            char_count=len("The quarterly plan covers launch milestones."),
            embedding=[0.02] * 384,
        )
    )
    await session.commit()

    listed = await client.get("/api/v1/files", headers=HEADERS)
    assert [f["id"] for f in listed.json()] == [file.id]
    assert listed.json()[0]["state"] == "indexed"

    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeProvider())
    res = await client.get(
        "/api/v1/files/search", headers=HEADERS, params={"q": "quarterly plan"}
    )
    assert res.status_code == 200
    hits = res.json()
    assert len(hits) == 1
    assert hits[0]["file_name"] == "notes.txt"
    assert "quarterly" in hits[0]["text"]

    res = await client.delete(f"/api/v1/files/{file.id}", headers=HEADERS)
    assert res.status_code == 204
    assert (await client.get("/api/v1/files", headers=HEADERS)).json() == []


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def test_settings_patch(client: httpx.AsyncClient) -> None:
    res = await client.patch(
        "/api/v1/settings",
        headers=HEADERS,
        json={
            "timezone": "Europe/Berlin",
            "digest_time": "07:30:00",
            "motivation_enabled": False,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["timezone"] == "Europe/Berlin"
    assert body["digest_time"] == "07:30:00"
    assert body["motivation_enabled"] is False

    fetched = await client.get("/api/v1/settings", headers=HEADERS)
    assert fetched.json() == body


async def test_settings_invalid_timezone_is_422(client: httpx.AsyncClient) -> None:
    res = await client.patch(
        "/api/v1/settings", headers=HEADERS, json={"timezone": "Not/AZone"}
    )
    assert res.status_code == 422


async def test_today_uses_user_timezone(client: httpx.AsyncClient) -> None:
    """An item at Berlin noon lands in the user's local 'today' window.

    The anchor is computed in the user's own timezone, not UTC: after
    22:00 UTC the Berlin date has already rolled over, so a UTC-noon anchor
    would land in Berlin's *previous* day and the assertion would flake.
    """
    await client.patch(
        "/api/v1/settings", headers=HEADERS, json={"timezone": "Europe/Berlin"}
    )
    berlin_noon = datetime.now(tz=ZoneInfo("Europe/Berlin")).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "Local", "starts_at": berlin_noon.isoformat()},
    )
    assert res.status_code == 201
    today = await client.get("/api/v1/calendar/today", headers=HEADERS)
    assert [i["title"] for i in today.json()] == ["Local"]


# ---------------------------------------------------------------------------
# Assistant Inbox — pending actions (SPEC §3)
# ---------------------------------------------------------------------------


async def _propose_create_item(client: httpx.AsyncClient, session: AsyncSession) -> int:
    await client.get("/api/v1/me", headers=HEADERS)  # upsert user 777
    user = await session.get(User, 777)
    assert user is not None
    action = await actions_service.propose_action(
        session,
        user,
        kind="create_item",
        payload={"title": "Proposed task", "starts_at": TODAY_NOON},
        summary="create 'Proposed task'",
    )
    await session.commit()
    return action.id


async def test_action_confirm_executes_exactly_once(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    action_id = await _propose_create_item(client, session)

    listed = await client.get("/api/v1/actions", headers=HEADERS)
    assert listed.status_code == 200
    assert [a["status"] for a in listed.json()] == ["proposed"]
    assert listed.json()[0]["kind"] == "create_item"

    res = await client.post(f"/api/v1/actions/{action_id}/confirm", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "executed"
    assert body["executed_at"] is not None
    assert body["last_result"] is not None

    # The mutation happened exactly once.
    today = await client.get("/api/v1/calendar/today", headers=HEADERS)
    assert [i["title"] for i in today.json()] == ["Proposed task"]

    # Replay is safe: stored state returned, no second item.
    res = await client.post(f"/api/v1/actions/{action_id}/confirm", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "executed"
    today = await client.get("/api/v1/calendar/today", headers=HEADERS)
    assert len(today.json()) == 1


async def test_action_reject_and_terminal_states(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    action_id = await _propose_create_item(client, session)
    res = await client.post(f"/api/v1/actions/{action_id}/reject", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"

    # Rejected actions cannot be confirmed; rejecting again is a 400.
    assert (
        await client.post(f"/api/v1/actions/{action_id}/confirm", headers=HEADERS)
    ).status_code == 400
    assert (
        await client.post(f"/api/v1/actions/{action_id}/reject", headers=HEADERS)
    ).status_code == 400

    # Nothing was mutated.
    assert (await client.get("/api/v1/calendar/today", headers=HEADERS)).json() == []


async def test_action_confirm_stale_target_is_409(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    item_id = (
        await client.post(
            "/api/v1/items", headers=HEADERS, json={"title": "Gone", "starts_at": TODAY_NOON}
        )
    ).json()["id"]
    user = await session.get(User, 777)
    assert user is not None
    action = await actions_service.propose_action(
        session,
        user,
        kind="cancel_item",
        payload={"item_id": item_id},
        summary="cancel 'Gone'",
    )
    await session.commit()
    action_id = action.id

    # The target disappears before confirmation.
    await client.delete(f"/api/v1/items/{item_id}", headers=HEADERS)

    res = await client.post(f"/api/v1/actions/{action_id}/confirm", headers=HEADERS)
    assert res.status_code == 409
    # The proposal expired rather than executing.
    listed = await client.get("/api/v1/actions", headers=HEADERS)
    assert [a["status"] for a in listed.json()] == ["expired"]


async def test_actions_are_user_scoped(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    action_id = await _propose_create_item(client, session)
    # The other user sees an empty inbox and cannot touch the action.
    assert (await client.get("/api/v1/actions", headers=HEADERS_OTHER)).json() == []
    assert (
        await client.post(f"/api/v1/actions/{action_id}/confirm", headers=HEADERS_OTHER)
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/actions/{action_id}/reject", headers=HEADERS_OTHER)
    ).status_code == 404


async def test_action_list_status_filter(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    action_id = await _propose_create_item(client, session)
    await client.post(f"/api/v1/actions/{action_id}/reject", headers=HEADERS)
    assert (
        await client.get("/api/v1/actions", headers=HEADERS, params={"status": "proposed"})
    ).json() == []
    rejected = await client.get(
        "/api/v1/actions", headers=HEADERS, params={"status": "rejected"}
    )
    assert [a["id"] for a in rejected.json()] == [action_id]


async def test_action_overdue_reports_expired_without_write(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await client.get("/api/v1/me", headers=HEADERS)  # upsert user 777
    user = await session.get(User, 777)
    assert user is not None
    action = await actions_service.propose_action(
        session,
        user,
        kind="create_item",
        payload={"title": "Late", "starts_at": TODAY_NOON},
        summary="create 'Late'",
        expires_in=timedelta(seconds=-1),
    )
    await session.commit()
    action_id = action.id

    # The read endpoint reports the effective (expired) status, and rejecting
    # an overdue action is a 400 (it is not transitioned).
    listed = await client.get("/api/v1/actions", headers=HEADERS)
    assert [a["status"] for a in listed.json()] == ["expired"]
    assert (
        await client.post(f"/api/v1/actions/{action_id}/reject", headers=HEADERS)
    ).status_code == 400

    # The read did NOT persist the transition: the stored row is still
    # ``proposed`` (only a write path or the worker's bulk pass flips it).
    stored = await actions_service.get_action(session, user, action_id)
    assert stored is not None
    assert stored.status == "proposed"


# ---------------------------------------------------------------------------
# Proactivity settings (SPEC §11)
# ---------------------------------------------------------------------------


async def test_proactive_settings_api(client: httpx.AsyncClient) -> None:
    res = await client.get("/api/v1/proactive-settings", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["weekly_review_enabled"] is True
    assert body["workout_nudge_enabled"] is True
    assert body["quiet_hours_start"] == "22:00:00"
    assert body["quiet_hours_end"] == "08:00:00"
    assert body["max_nudges_per_day"] == 3
    assert body["min_interval_minutes"] == 120

    res = await client.patch(
        "/api/v1/proactive-settings",
        headers=HEADERS,
        json={"enabled": False, "quiet_hours_start": "23:00:00", "max_nudges_per_day": 2},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert body["quiet_hours_start"] == "23:00:00"
    assert body["max_nudges_per_day"] == 2
    # Untouched fields keep their values.
    assert body["weekly_review_enabled"] is True
    assert body["quiet_hours_end"] == "08:00:00"
    assert (await client.get("/api/v1/proactive-settings", headers=HEADERS)).json() == body


async def test_proactive_settings_validation_is_422(client: httpx.AsyncClient) -> None:
    assert (
        await client.patch(
            "/api/v1/proactive-settings", headers=HEADERS, json={"max_nudges_per_day": 0}
        )
    ).status_code == 422
    assert (
        await client.patch(
            "/api/v1/proactive-settings", headers=HEADERS, json={"min_interval_minutes": 5000}
        )
    ).status_code == 422


# ---------------------------------------------------------------------------
# Fact supersede (SPEC §14)
# ---------------------------------------------------------------------------


async def test_fact_supersede_flow(client: httpx.AsyncClient) -> None:
    fact_id = (
        await client.post("/api/v1/facts", headers=HEADERS, json={"value": "old value"})
    ).json()["id"]
    res = await client.post(
        f"/api/v1/facts/{fact_id}/supersede",
        headers=HEADERS,
        json={"value": "new value"},
    )
    assert res.status_code == 201
    new = res.json()
    assert new["status"] == "proposed"
    assert new["id"] != fact_id

    by_id = {f["id"]: f for f in (await client.get("/api/v1/facts", headers=HEADERS)).json()}
    # The referenced fact keeps its state until the replacement is confirmed.
    assert by_id[fact_id]["status"] == "proposed"
    assert by_id[fact_id]["superseded_by"] is None
    assert by_id[new["id"]]["status"] == "proposed"
    assert by_id[new["id"]]["replaces_fact_id"] == fact_id

    # Confirming the replacement supersedes the old fact atomically and the
    # list exposes the "replaced by" link (Mini App memory UX, SPEC §14).
    res = await client.post(f"/api/v1/facts/{new['id']}/confirm", headers=HEADERS)
    assert res.json()["status"] == "confirmed"
    by_id = {f["id"]: f for f in (await client.get("/api/v1/facts", headers=HEADERS)).json()}
    assert by_id[fact_id]["status"] == "superseded"
    assert by_id[fact_id]["superseded_by"] == new["id"]
    assert by_id[new["id"]]["status"] == "confirmed"


async def test_fact_supersede_scoping_and_states(client: httpx.AsyncClient) -> None:
    fact_id = (
        await client.post("/api/v1/facts", headers=HEADERS, json={"value": "mine"})
    ).json()["id"]
    # Another user cannot supersede it.
    res = await client.post(
        f"/api/v1/facts/{fact_id}/supersede",
        headers=HEADERS_OTHER,
        json={"value": "theirs"},
    )
    assert res.status_code == 404
    # A rejected fact cannot be superseded (not proposed/confirmed).
    await client.post(f"/api/v1/facts/{fact_id}/reject", headers=HEADERS)
    res = await client.post(
        f"/api/v1/facts/{fact_id}/supersede", headers=HEADERS, json={"value": "v2"}
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# File ingestion retry (SPEC §11)
# ---------------------------------------------------------------------------


async def test_file_retry_flow(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await client.get("/api/v1/me", headers=HEADERS)
    failed = UserFile(
        user_id=777,
        storage_key="key-failed",
        telegram_file_id="tg-1",
        original_filename="broken.pdf",
        mime_type="application/pdf",
        state=FileState.failed.value,
        error="embedding provider unavailable",
    )
    rejected = UserFile(
        user_id=777,
        storage_key="key-rejected",
        telegram_file_id="tg-2",
        original_filename="img.png",
        mime_type="image/png",
        state=FileState.rejected.value,
        error="unsupported type",
    )
    session.add_all([failed, rejected])
    await session.commit()
    failed_id, rejected_id = failed.id, rejected.id

    res = await client.post(f"/api/v1/files/{failed_id}/retry", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "queued"
    assert body["error"] is None

    # A new ingestion job is enqueued.
    jobs = (
        await session.scalars(
            select(BackgroundJob).where(BackgroundJob.type == "files.ingest")
        )
    ).all()
    assert len(jobs) == 1
    assert jobs[0].payload == {"file_id": failed_id}
    assert jobs[0].status == "pending"

    # Only failed files are retryable.
    assert (
        await client.post(f"/api/v1/files/{failed_id}/retry", headers=HEADERS)
    ).status_code == 400
    assert (
        await client.post(f"/api/v1/files/{rejected_id}/retry", headers=HEADERS)
    ).status_code == 400
    # Another user cannot retry it.
    assert (
        await client.post(f"/api/v1/files/{failed_id}/retry", headers=HEADERS_OTHER)
    ).status_code == 400


# ---------------------------------------------------------------------------
# Workout scheduling (SPEC §10)
# ---------------------------------------------------------------------------


async def test_workout_schedule_creates_item_and_reminder(
    client: httpx.AsyncClient,
) -> None:
    res = await client.post(
        "/api/v1/workouts/schedule",
        headers=HEADERS,
        json={"name": "Legs", "starts_at": TOMORROW_NOON, "duration_minutes": 45},
    )
    assert res.status_code == 201
    item = res.json()
    assert item["title"] == "Workout: Legs"
    assert item["source"] == "workout"
    assert item["status"] == "scheduled"

    reminders = await client.get("/api/v1/reminders", headers=HEADERS)
    data = reminders.json()
    assert len(data) == 1
    assert data[0]["message"] == "Workout: Legs"
    fire_at = datetime.fromisoformat(data[0]["fire_at"].replace("Z", "+00:00"))
    expected_fire = (
        datetime.fromisoformat(TOMORROW_NOON).replace(tzinfo=UTC)
    )
    assert fire_at == expected_fire


async def test_workout_schedule_validation(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/workouts/schedule", headers=HEADERS, json={"name": "", "starts_at": TOMORROW_NOON}
    )
    assert res.status_code == 422
    res = await client.post(
        "/api/v1/workouts/schedule", headers=HEADERS, json={"name": "x"}
    )
    assert res.status_code == 422
