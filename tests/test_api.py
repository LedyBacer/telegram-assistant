"""Mini App API tests (SPEC §20) — real PostgreSQL, signed initData, fakes.

Environment variables are set before importing the app so ``get_settings()``
reads test values; no real Telegram/OpenAI credentials are used.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
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
from assistant.models.users import User  # noqa: E402
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

TODAY_NOON = datetime.now(tz=UTC).replace(
    hour=12, minute=0, second=0, microsecond=0
).isoformat()


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

    upcoming = await client.get("/api/v1/calendar/upcoming", headers=HEADERS)
    assert any(i["id"] == item_id for i in upcoming.json())

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
    assert data[0]["message"] == "Reminder: Call"
    # FastAPI serializes UTC datetimes with a Z suffix — parse, don't compare raw.
    fire_at = datetime.fromisoformat(data[0]["fire_at"].replace("Z", "+00:00"))
    expected_fire = (
        datetime.fromisoformat(TODAY_NOON).replace(tzinfo=UTC) - timedelta(minutes=30)
    )
    assert fire_at == expected_fire


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
    """A UTC noon timestamp lands in the user's local 'today' window."""
    await client.patch(
        "/api/v1/settings", headers=HEADERS, json={"timezone": "Europe/Berlin"}
    )
    # 02:00 Berlin == 00:00 UTC (winter time), so noon UTC is 14:00 Berlin.
    res = await client.post(
        "/api/v1/items",
        headers=HEADERS,
        json={"title": "Local", "starts_at": datetime.now(tz=UTC).replace(
            hour=12, minute=0, second=0, microsecond=0
        ).isoformat()},
    )
    assert res.status_code == 201
    today = await client.get("/api/v1/calendar/today", headers=HEADERS)
    assert [i["title"] for i in today.json()] == ["Local"]
