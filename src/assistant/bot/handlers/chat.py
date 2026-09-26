"""Free-text message handler: task drafts, settings, workouts, and chat (SPEC §2,§4-§6)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.fsm.state import State
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProviderError, AITaskDraft, get_ai_provider
from assistant.ai.prompts import DRAFT_SYSTEM
from assistant.bot.handlers.common import (
    TaskDraft,
    _ai_draft_to_task_draft,
    _draft_preview,
    _ensure_user,
    _parse_draft,
    _parse_hhmm,
    _parse_workout_log,
    _parse_workout_schedule,
    _render_validation_error,
    _user_lang,
    _user_tz,
)
from assistant.bot.keyboards import (
    action_kb,
    draft_kb,
    fact_kb,
    main_menu_kb,
    settings_kb,
    workouts_kb,
)
from assistant.bot.states import SettingsStates, TaskDraftStates, WorkoutStates
from assistant.i18n import LocalizableError, t
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.services import files as files_service
from assistant.services import turns as turns_service
from assistant.services import workouts as workouts_service
from assistant.services.tg_markdown import answer_long_markdown

router = Router(name="chat")


@router.message(F.text)
async def on_text(
    message: Message, session: AsyncSession, state: State
) -> None:
    text = message.text or ""
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
    current = await state.get_state()

    if current == TaskDraftStates.waiting_for_text:
        tz = _user_tz(user)
        draft: TaskDraft | None = None
        ai_dump: dict[str, Any] | None = None
        try:
            draft = _parse_draft(text, tz)
        except ValueError:
            # Natural language: let the model produce a typed draft. The
            # manual format stays available as a deterministic fallback.
            # tz and lang are already snapshotted into locals above; commit
            # to release the DB transaction before the model's network I/O
            # (no open transaction spanning the provider call, V4 §19-20).
            await session.commit()
            try:
                async with ChatActionSender.typing(
                    bot=message.bot,
                    chat_id=message.chat.id,
                    interval=4.0,
                ):
                    ai = await get_ai_provider().chat_structured(
                        system=DRAFT_SYSTEM.format(
                            tz=tz, now=datetime.now(tz).isoformat()
                        ),
                        messages=[{"role": "user", "content": text}],
                        schema=AITaskDraft,
                    )
            except AIProviderError:
                await message.answer(
                    t(lang, "draft.failed", help=t(lang, "draft.help"))
                )
                return
            draft = _ai_draft_to_task_draft(ai, tz)
            ai_dump = ai.model_dump(mode="json")
        await state.update_data(draft_text=text, draft_ai=ai_dump)
        await state.set_state(TaskDraftStates.confirm)
        await message.answer(
            _draft_preview(draft, tz, lang), reply_markup=draft_kb(lang)
        )
    elif current == TaskDraftStates.confirm:
        await message.answer(t(lang, "draft.confirm_prompt"))
    elif current == SettingsStates.timezone:
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            await message.answer(t(lang, "settings.tz_invalid", value=text))
            return
        user.settings.timezone = text
        await state.clear()
        await message.answer(
            t(lang, "settings.tz_set", tz=text), reply_markup=settings_kb(lang)
        )
    elif current == SettingsStates.digest_time:
        try:
            clock = _parse_hhmm(text)
        except (ValueError, TypeError):
            await message.answer(t(lang, "settings.digest_invalid"))
            return
        user.settings.digest_time = clock
        await state.clear()
        await message.answer(
            t(lang, "settings.digest_set", time=f"{clock:%H:%M}"),
            reply_markup=settings_kb(lang),
        )
    elif current == WorkoutStates.waiting_log:
        try:
            name, duration, effort = _parse_workout_log(text)
        except ValueError as exc:
            await message.answer(
                _render_validation_error(exc, lang, "workouts.cant_read")
                + "\n\n"
                + t(lang, "workouts.log_prompt")
            )
            return
        try:
            log = await workouts_service.log_workout(
                session,
                user,
                name=name,
                duration_minutes=duration,
                perceived_effort=effort,
            )
        except ValueError as exc:
            await message.answer(t(lang, "workouts.cant_read", error=exc))
            return
        await state.clear()
        await message.answer(
            t(lang, "workouts.logged", name=log.name)
            + (
                t(lang, "workouts.minutes", minutes=log.duration_minutes)
                if log.duration_minutes
                else ""
            ),
            reply_markup=workouts_kb(lang),
        )
    elif current == WorkoutStates.waiting_schedule:
        tz = _user_tz(user)
        try:
            name, when = _parse_workout_schedule(text, tz)
        except ValueError as exc:
            await message.answer(
                _render_validation_error(exc, lang, "workouts.cant_read")
                + "\n\n"
                + t(lang, "workouts.schedule_prompt")
            )
            return
        try:
            item = await workouts_service.schedule_workout(
                session, user, name=name, starts_at=when
            )
        except ValueError as exc:
            await message.answer(t(lang, "workouts.cant_schedule", error=exc))
            return
        await state.clear()
        await message.answer(
            t(
                lang,
                "workouts.scheduled",
                title=item.title,
                when=item.starts_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                tz=tz,
            ),
            reply_markup=main_menu_kb(lang),
        )
    else:
        try:
            async with ChatActionSender.typing(
                    bot=message.bot,
                    chat_id=message.chat.id,
                    interval=4.0,
                ):
                result = await turns_service.run_turn(session, user, text)
        except AIProviderError:
            # The engine persists nothing on provider failure: keep the
            # user message and explain the outage in the user's language.
            session.add(
                ChatMessage(
                    user_id=user.id,
                    role=ChatRole.user.value,
                    content=text,
                    source="telegram",
                )
            )
            await message.answer(t(lang, "errors.ai_unavailable"))
            return
        except LocalizableError as exc:
            # Malformed/empty model output fails safely (SPEC §2): the user
            # message is persisted and a localized fallback is shown.
            session.add(
                ChatMessage(
                    user_id=user.id,
                    role=ChatRole.user.value,
                    content=text,
                    source="telegram",
                )
            )
            await session.flush()
            await message.answer(t(lang, exc.key, **exc.params))
            return
        if result.reply:
            text = result.reply
            if result.retrieved_chunks:
                # Deterministic provenance (SPEC §6.3): rendered from
                # retrieval metadata, never left to the model.
                citations = files_service.format_citations(result.retrieved_chunks)
                if citations:
                    text = f"{text}\n\n{citations}"
            # Long model replies are split at paragraph/line boundaries so
            # the 4096-char Telegram limit never fails the turn (V3 P24).
            await answer_long_markdown(message, text)
        for action in result.proposed_actions:
            await message.answer(
                t(lang, "action.propose", summary=action.summary),
                reply_markup=action_kb(action.id, lang),
            )
        for fact in result.proposed_facts:
            await message.answer(
                t(lang, "facts.proposed", value=fact.value),
                reply_markup=fact_kb(fact.id, confirmable=True, language=lang),
            )
        if result.skipped_actions:
            await message.answer(t(lang, "action.skipped"))
