from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from assistant.bot import callbacks, keyboards
from assistant.bot.handlers.navigation import on_reply_navigation
from assistant.bot.states import TaskDraftStates
from assistant.i18n import t
from assistant.models.files import FileState, UserFile
from assistant.models.jobs import BackgroundJob
from assistant.models.users import User
from assistant.services import data_management
from assistant.services import files as files_service
from assistant.services.users import upsert_user


def _tg_user(user_id: int = 501):
    return SimpleNamespace(
        id=user_id, first_name="P", last_name=None, username=None, is_bot=False
    )


def _state():
    return SimpleNamespace(
        get_state=AsyncMock(return_value=None),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
        clear=AsyncMock(),
    )


async def test_reply_task_button_routes_without_llm(session) -> None:
    await upsert_user(session, user_id=501, first_name="P")
    await session.commit()
    message = SimpleNamespace(
        text=t("ru", "menu.tasks"),
        from_user=_tg_user(),
        answer=AsyncMock(),
    )
    state = _state()
    await on_reply_navigation(message, session, state)
    state.set_state.assert_awaited_once_with(TaskDraftStates.waiting_for_text)
    message.answer.assert_awaited_once()


def test_data_item_callback_roundtrip() -> None:
    packed = callbacks.DataItemCallback(kind="file", item_id=42).pack()
    assert callbacks.DataItemCallback.unpack(packed) == callbacks.DataItemCallback(
        kind="file", item_id=42
    )


def test_reply_keyboard_has_webapp_and_is_persistent() -> None:
    kb = keyboards.reply_kb("ru", "https://example.test/miniapp")
    assert kb.is_persistent is True
    flat = [b for row in kb.keyboard for b in row]
    mini = next(b for b in flat if b.text == t("ru", "menu.miniapp"))
    assert mini.web_app is not None


async def test_filename_search_is_user_scoped_and_query_aware(session) -> None:
    alice, _ = await upsert_user(session, user_id=601, first_name="A")
    bob, _ = await upsert_user(session, user_id=602, first_name="B")
    session.add_all(
        [
            UserFile(
                user_id=alice.id,
                storage_key="a1",
                original_filename="insurance-policy.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                state=FileState.indexed.value,
            ),
            UserFile(
                user_id=alice.id,
                storage_key="a2",
                original_filename="notes.txt",
                mime_type="text/plain",
                size_bytes=1,
                state=FileState.indexed.value,
            ),
            UserFile(
                user_id=bob.id,
                storage_key="b1",
                original_filename="insurance-secret.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                state=FileState.indexed.value,
            ),
        ]
    )
    await session.commit()

    rows = await files_service.search_files_by_name(
        session, alice, query="file insurance", limit=20
    )
    assert [row.original_filename for row in rows] == ["insurance-policy.pdf"]


async def test_full_erase_cascades_user_data_and_returns_storage_keys(session) -> None:
    user, _ = await upsert_user(session, user_id=701, first_name="Erase")
    file = UserFile(
        user_id=user.id,
        storage_key="erase-me",
        original_filename="private.txt",
        mime_type="text/plain",
        size_bytes=1,
        state=FileState.indexed.value,
    )
    job = BackgroundJob(user_id=user.id, type="test.erase", payload={})
    session.add_all([file, job])
    await session.commit()
    file_id, job_id, user_id = file.id, job.id, user.id

    keys = await data_management.erase_user_data(session, user)
    await session.commit()

    # PostgreSQL performs child deletion via ON DELETE CASCADE.
    # Drop stale ORM identity-map state before checking durable rows.
    session.expire_all()

    assert keys == ["erase-me"]
    assert await session.get(User, user_id) is None
    assert await session.get(UserFile, file_id) is None
    assert await session.get(BackgroundJob, job_id) is None


def test_gzip_middleware_is_installed() -> None:
    from starlette.middleware.gzip import GZipMiddleware

    from assistant.api.main import create_app

    app = create_app()
    assert any(m.cls is GZipMiddleware for m in app.user_middleware)
