"""Document upload message handler (SPEC §11-13)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.handlers.common import (
    _ensure_user,
    _file_rejection_text,
    _user_lang,
)
from assistant.i18n import t
from assistant.models.files import FileState
from assistant.services import files as files_service

router = Router(name="files")


@router.message(F.document)
async def on_document(message: Message, session: AsyncSession) -> None:
    user = await _ensure_user(session, message.from_user)
    doc = message.document
    if doc is None:
        return
    file = await files_service.register_upload(
        session,
        user,
        original_filename=doc.file_name,
        mime_type=doc.mime_type or "",
        size_bytes=doc.file_size,
        telegram_file_id=doc.file_id,
        telegram_file_unique_id=doc.file_unique_id,
    )
    lang = _user_lang(user)
    if file.state == FileState.rejected.value:
        await message.answer(
            t(lang, "files.rejected", error=_file_rejection_text(file, lang))
        )
    else:
        await message.answer(
            t(lang, "files.saved", filename=file.original_filename)
        )
