"""Central per-user translation service.

Supported languages are centralized here so no handler, service, or the API
ever hard-codes ``"ru"`` / ``"en"`` literals. A new language is added by
registering it in :class:`SupportedLanguage` and dropping a ``<code>.json``
file in ``locales/`` — no application logic changes are required.

Locales are flat ``{"section.key": "value"}`` JSON dictionaries loaded once
per process. Russian (:data:`FALLBACK_LANGUAGE`) is the default and the
fallback: an unknown/unsupported language resolves to Russian, and a key
missing from the active locale falls back to Russian. A key missing from
Russian as well returns the key itself so a missing translation never
crashes the bot.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"


class SupportedLanguage(StrEnum):
    """Every application-supported language, by code."""

    RU = "ru"
    EN = "en"


#: Language for every new user and the fallback for lookups.
DEFAULT_LANGUAGE = SupportedLanguage.RU.value
#: Locale used when the requested one is missing/unsupported or a key is absent.
FALLBACK_LANGUAGE = SupportedLanguage.RU.value

#: The set of valid language codes, derived from the enum (never re-listed).
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    member.value for member in SupportedLanguage
)


#: Language name passed to the AI as an explicit answer-language instruction
#: (e.g. "Russian" / "English"); unknown codes resolve via the fallback.
LANGUAGE_NAMES: dict[str, str] = {
    SupportedLanguage.RU.value: "Russian",
    SupportedLanguage.EN.value: "English",
}


def language_name(language: str) -> str:
    """Human-readable name of a supported language for AI instructions."""
    resolved = language if language in LANGUAGE_NAMES else FALLBACK_LANGUAGE
    return LANGUAGE_NAMES[resolved]


def is_supported(language: str) -> bool:
    """True when ``language`` is a supported language code."""
    return language in SUPPORTED_LANGUAGES


class LocalizableError(ValueError):
    """A validation error whose message is a locale key, not a fixed string.

    Handlers render it in the user's persisted language via
    ``t(language, exc.key, **exc.params)`` so user-facing validation errors
    are never hardcoded in one language. The locale key is also a safe
    ``str()`` (no raw provider/user text embedded).
    """

    def __init__(self, key: str, **params: object) -> None:
        self.key = key
        self.params = params
        super().__init__(key)


@cache
def _load(locale: str) -> dict[str, str]:
    with (LOCALES_DIR / f"{locale}.json").open(encoding="utf-8") as handle:
        data = json.load(handle)
    return {key: str(value) for key, value in data.items()}


def load_locale(locale: str) -> dict[str, str]:
    """Return the raw flat dictionary for a supported locale (for the API)."""
    return dict(_load(locale))


def _resolve(language: str) -> str:
    return language if language in SUPPORTED_LANGUAGES else FALLBACK_LANGUAGE


def t(language: str, key: str, **kwargs: object) -> str:
    """Translate ``key`` for ``language`` with ``{param}`` interpolation.

    Unknown ``language`` falls back to Russian; a key missing from the active
    locale falls back to Russian; a key missing from Russian returns the key
    (no exception).
    """
    locale = _resolve(language)
    template = _load(locale).get(key)
    if template is None and locale != FALLBACK_LANGUAGE:
        template = _load(FALLBACK_LANGUAGE).get(key)
    if template is None:
        return key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return template
    return template


class Translator:
    """A translator bound to one language.

    Equivalent to calling :func:`t` repeatedly: ``translator.get(key)`` is
    ``t(translator.language, key)``.
    """

    __slots__ = ("language",)

    def __init__(self, language: str) -> None:
        self.language = _resolve(language)

    def get(self, key: str, **kwargs: object) -> str:
        return t(self.language, key, **kwargs)

    def __call__(self, key: str, **kwargs: object) -> str:
        return self.get(key, **kwargs)


def for_language(language: str) -> Translator:
    """Convenience: a :class:`Translator` for ``language``."""
    return Translator(language)
