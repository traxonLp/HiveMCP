"""Fetching a user's OpenWebUI interface preferences.

Split from ``preferences`` so the configuration card, which only renders markup, does not
pull in an HTTP client to do it.

The settings route is probed rather than hardcoded, for the same reason as in ``auth.py``:
it has moved between versions, and being wrong here should cost a default theme rather
than an error. If none of the candidates answers, the client stops asking — three failed
requests on every settings card, forever, would be a poor trade for a colour.
"""

from __future__ import annotations

import logging
import time

import httpx

from .preferences import SETTINGS_ENDPOINTS, UserPreferences, parse_preferences

logger = logging.getLogger(__name__)


class PreferencesClient:
    def __init__(
        self,
        base_url: str | None,
        *,
        timeout: float = 5.0,
        cache_ttl: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        # Deliberately short: a card that renders in the wrong theme is a cosmetic
        # problem, while a slow one delays every settings dialog.
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._client = client
        self._endpoint: str | None = None
        self._unavailable = False
        self._cache: dict[str, tuple[UserPreferences, float]] = {}

    def _require(self) -> httpx.AsyncClient | None:
        if not self.base_url:
            return None
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, user_id: str, token: str) -> UserPreferences:
        """Best effort. Never raises: the caller has a working default either way."""
        if self._unavailable:
            return UserPreferences()

        cached = self._cache.get(user_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        client = self._require()
        if client is None:
            return UserPreferences()

        endpoints = (self._endpoint,) if self._endpoint else SETTINGS_ENDPOINTS
        answered: str | None = None
        best = UserPreferences()

        for path in endpoints:
            assert path is not None
            try:
                response = await client.get(
                    path, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                logger.debug("could not read preferences from %s: %s", path, exc)
                continue

            if response.status_code != 200:
                continue

            # A 200 is not evidence that the route exists. OpenWebUI serves its frontend
            # as a catch-all, so an unknown /api/... path comes back as 200 with the SPA's
            # index.html. Checking that the body is actually a JSON object is what tells
            # a real endpoint from the fallback — observed on /api/v1/auths/user/settings.
            if "application/json" not in response.headers.get("content-type", ""):
                logger.debug("%s answered with non-JSON; treating as absent", path)
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue

            # A 200 means the route exists, which is a different fact from whether this
            # particular user has customised anything. Conflating the two is how an
            # earlier version disabled preference lookups for the whole process because
            # the first user to open a settings card happened to run stock settings.
            answered = answered or path
            preferences = parse_preferences(payload)
            if preferences != UserPreferences():
                best = preferences
                answered = path
                break

        if answered is None:
            # Nothing answered at all, so the routes are not there. Stop asking rather
            # than pay three failed requests on every settings card for the life of the
            # process.
            logger.info(
                "no OpenWebUI settings endpoint responded; the configuration card will "
                "follow the operating system theme and use English"
            )
            self._unavailable = True
            return UserPreferences()

        if self._endpoint != answered:
            logger.info("reading interface preferences from %s", answered)
            self._endpoint = answered
        if best == UserPreferences():
            logger.debug(
                "%s answered but carried no theme or locale; this user is on defaults",
                answered,
            )
        self._cache[user_id] = (best, time.monotonic() + self.cache_ttl)
        return best
