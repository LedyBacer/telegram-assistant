"""Per-user internationalization (i18n).

Public API:

- :data:`SupportedLanguage` — the single registry of supported languages.
- :data:`DEFAULT_LANGUAGE` — the language for every new user (Russian).
- :func:`t` — translate ``key`` for ``language`` with ``{param}`` interpolation.
- :func:`for_language` — a translator bound to one language.
- :func:`load_locale` — the raw flat dictionary for a locale (served by the
  Mini App API so the frontend and the bot share one translation source).
- :func:`is_supported` — validation helper for the settings API.
"""

from assistant.i18n.service import (
    DEFAULT_LANGUAGE,
    FALLBACK_LANGUAGE,
    SUPPORTED_LANGUAGES,
    SupportedLanguage,
    Translator,
    for_language,
    is_supported,
    language_name,
    load_locale,
    t,
)

__all__ = [
    "DEFAULT_LANGUAGE",
    "FALLBACK_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "SupportedLanguage",
    "Translator",
    "for_language",
    "is_supported",
    "language_name",
    "load_locale",
    "t",
]
