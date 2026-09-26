"""Safe counters for structured-output normalization and repair events.

V5.4 §22: every tolerant normalization in :mod:`assistant.ai.schemas`
(``_infer_missing_mode``) and every validation retry / final failure in
:mod:`assistant.ai.provider` increments a process-wide counter and logs a
fixed-name event. Only non-secret, fixed-shape fields (schema class name,
attempt index, content character count) are ever logged — never model
content, user text, or credentials, so the counters are safe to enable in
production and to aggregate in live evaluation.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("assistant.ai")

EVENT_NAMES: frozenset[str] = frozenset(
    {
        "structured_missing_mode_inferred",
        "structured_bare_tool_wrapped",
        "structured_fact_id_normalized",
        "structured_top_level_replaces_fact_id_normalized",
        "structured_facts_only_proposal_normalized",
        "structured_validation_retry",
        "structured_validation_final_failure",
    }
)

_lock = threading.Lock()
_counts: dict[str, int] = {}


def count_structured_event(name: str, **fields: object) -> None:
    """Increment a §22 counter and log the event with safe fields only.

    ``fields`` must be small, non-secret values (schema name, attempt
    number, character count). The raw model response is never logged.
    """
    if name not in EVENT_NAMES:
        raise ValueError(f"unknown structured event name: {name!r}")
    with _lock:
        _counts[name] = _counts.get(name, 0) + 1
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info(
        "structured_event name=%s%s", name, f" {suffix}" if suffix else ""
    )


def structured_event_counts() -> dict[str, int]:
    """Snapshot of the process-wide counters (zero for unseen events)."""
    with _lock:
        return dict(_counts)


def reset_structured_event_counts() -> None:
    """Clear all counters (tests only)."""
    with _lock:
        _counts.clear()
