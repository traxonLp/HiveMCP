"""What a user's OpenWebUI interface looks like, and how to read it out of a payload.

Rich UI embeds are sandboxed, cross-origin iframes. They cannot read the parent's DOM or
CSS, and OpenWebUI's postMessage protocol carries no theme or locale message, so a card
cannot discover how the surrounding chat looks by asking. ``window.args`` would need
``allowSameOrigin``, which is off by default.

What remains is to ask OpenWebUI directly, before the markup is rendered. This module
holds the model and the parsing; the HTTP call lives in ``preferences_client`` so that
rendering a card does not transitively depend on an HTTP client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Theme = Literal["light", "dark"]

SETTINGS_ENDPOINTS = (
    "/api/v1/users/user/settings",
    "/api/v1/users/settings",
    "/api/v1/auths/user/settings",
)

# OpenWebUI writes several theme values; only the two that describe an actual appearance
# matter here. "system" and "" mean "follow the OS", which is what the card falls back to
# on its own via prefers-color-scheme.
EXPLICIT_THEMES: dict[str, Theme] = {
    "dark": "dark",
    "light": "light",
    "oled-dark": "dark",
    "her": "dark",
}

SUPPORTED_LANGUAGES = ("en", "de", "zh-CN", "zh-TW")

# Chinese is two written forms, not one language with regional spelling. Simplified and
# Traditional are mutually hard to read, so collapsing zh-TW onto zh-CN is a visible
# error rather than a rough edge. Script subtag first, then region.
TRADITIONAL_REGIONS = frozenset({"tw", "hk", "mo"})


def resolve_language(locale: str | None) -> str:
    """Map a BCP-47 locale onto a language this project has translations for."""
    if not locale or not locale.strip():
        return "en"

    parts = locale.strip().replace("_", "-").lower().split("-")
    primary = parts[0]
    subtags = set(parts[1:])

    if primary == "zh":
        if "hant" in subtags or (subtags & TRADITIONAL_REGIONS):
            return "zh-TW"
        return "zh-CN"

    return primary if primary in SUPPORTED_LANGUAGES else "en"


@dataclass(frozen=True)
class UserPreferences:
    """What the card needs to blend in. Both may be unknown."""

    theme: Theme | None = None
    locale: str | None = None

    @property
    def language(self) -> str:
        """A language this project has translations for, defaulting to English."""
        return resolve_language(self.locale)


def parse_preferences(payload: Any) -> UserPreferences:
    """Pull theme and locale out of a settings document, wherever they sit.

    The shape has varied: sometimes ``{"ui": {...}}``, sometimes flat, sometimes nested
    under ``settings``. Probing the known positions beats asserting one of them.
    """
    if not isinstance(payload, dict):
        return UserPreferences()

    containers: list[dict] = [payload]
    for key in ("ui", "settings"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
            deeper = nested.get("ui")
            if isinstance(deeper, dict):
                containers.append(deeper)

    theme: Theme | None = None
    locale: str | None = None
    for container in containers:
        if theme is None:
            raw_theme = container.get("theme")
            if isinstance(raw_theme, str):
                theme = EXPLICIT_THEMES.get(raw_theme.strip().lower())
        if locale is None:
            for key in ("locale", "language", "lang"):
                raw_locale = container.get(key)
                if isinstance(raw_locale, str) and raw_locale.strip():
                    locale = raw_locale.strip()
                    break

    return UserPreferences(theme=theme, locale=locale)
