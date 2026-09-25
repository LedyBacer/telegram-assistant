"""Reusable live-LLM behavioral evaluation harness for the Telegram Assistant.

This package exercises the REAL production components (turn engine, provider,
prompts, read tools, action registry, and domain services) against the real
configured chat model. It is deliberately NOT part of the offline CI suite: it
requires live model endpoints and is run explicitly via ``scripts/llm_eval.py``
against an isolated database (``assistant_llm_eval``) and a local storage dir.
The real ``CHAT_*`` / ``EMBEDDING_*`` credentials are used as-is and are never
printed.
"""

from __future__ import annotations

__all__ = ["assertions", "cases", "fixtures"]
