"""Background job handlers.

Imported by ``assistant.worker.main`` so every handler is registered in the
worker process before the first poll.
"""

from assistant.services import digests, files, reminders  # noqa: F401
