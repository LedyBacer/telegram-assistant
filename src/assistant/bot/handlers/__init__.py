"""Bot handlers (SPEC §5-§7) — composed from focused domain routers.

Every user-facing string is resolved through the central i18n translator
(``assistant.i18n.t``) in the user's persisted language.

The public ``router`` (included in the dispatcher after ``private_guard``) is
the concatenation of the per-domain routers below, included in the same order
the handlers were originally declared so that message-handler precedence
(commands → document → free-text) and callback routing are unchanged. The
private-chats-only filter is set on this composed router, so it applies to
every included sub-router (see :mod:`aiogram` ``check_root_filters``).
"""

from __future__ import annotations

from aiogram import F, Router

from assistant.bot.callbacks import DraftCallback
from assistant.bot.handlers import (
    actions,
    chat,
    commands,
    common,
    draft,
    facts,
    files,
    items,
    menu,
    settings,
)

router = Router(name="bot")
router.message.filter = F.chat.type == "private"
router.callback_query.filter = F.message.chat.type == "private"

# Include order preserves message-handler precedence (commands → document →
# free-text) and the original callback-handler order.
router.include_router(commands.router)
router.include_router(facts.router)
router.include_router(menu.router)
router.include_router(settings.router)
router.include_router(draft.router)
router.include_router(actions.router)
router.include_router(items.router)
router.include_router(files.router)
router.include_router(chat.router)

# Re-export the shared guard and the public/tested names so
# ``from assistant.bot.handlers import ...`` keeps working (bot entrypoint,
# acceptance script, and the test suite).
private_guard = common.private_guard
reject_non_private_chat = common.reject_non_private_chat
reject_non_private_callback = common.reject_non_private_callback
TaskDraft = common.TaskDraft
_ensure_user = common._ensure_user
_parse_draft = common._parse_draft
_ai_draft_to_task_draft = common._ai_draft_to_task_draft

cmd_start = commands.cmd_start
cmd_cancel = commands.cmd_cancel
cmd_help = commands.cmd_help
cmd_remember = commands.cmd_remember
cmd_facts = commands.cmd_facts
cmd_language = commands.cmd_language
on_fact = facts.on_fact
on_menu = menu.on_menu
on_settings = settings.on_settings
on_language = settings.on_language
on_draft = draft.on_draft
on_action = actions.on_action
on_item = items.on_item
on_document = files.on_document
on_text = chat.on_text

__all__ = [
    "router",
    "private_guard",
    "reject_non_private_chat",
    "reject_non_private_callback",
    "TaskDraft",
    "DraftCallback",
    "cmd_start",
    "cmd_cancel",
    "cmd_help",
    "cmd_remember",
    "cmd_facts",
    "cmd_language",
    "on_fact",
    "on_menu",
    "on_settings",
    "on_language",
    "on_draft",
    "on_action",
    "on_item",
    "on_document",
    "on_text",
]
