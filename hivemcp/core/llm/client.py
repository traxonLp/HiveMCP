"""Calling OpenWebUI's own chat-completions API.

Brief expansion runs through the model the user selected in the chat, not through a
separately configured endpoint. That keeps one model in play for the whole interaction
and removes a second credential from the deployment.

The awkward part is finding out *which* model that is. OpenWebUI offers no ``{{MODEL}}``
header template, and the ``__model__`` reserved argument reaches native Python tools
only, never an external tool server. Hence ``resolve_model`` below.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Marks requests HiveMCP makes back into OpenWebUI. If one of them ever arrives at a
# HiveMCP tool, something has wired the tool into the expansion call and we are one hop
# from an infinite loop: model -> HiveMCP -> OpenWebUI -> model -> HiveMCP -> ...
LOOP_GUARD_HEADER = "X-Hive-Internal"


class LlmError(Exception):
    """The chat-completions call failed in a way the caller should report."""


class OwuiChatClient:
    def __init__(
        self,
        base_url: str | None,
        *,
        timeout: float = 120.0,
        max_output_tokens: int = 8000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _require(self) -> httpx.AsyncClient:
        if not self.configured:
            raise LlmError(
                "OpenWebUI is not configured, so the selected model cannot be called. "
                "Set HIVE_OWUI_URL."
            )
        if self._client is None:
            # No default Authorization header: each call carries the caller's own
            # session token, so the model runs under their permissions and their quota.
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        if not token:
            raise LlmError("no session token available for this request")
        return {"Authorization": f"Bearer {token}"}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ model

    async def get_chat_model(self, chat_id: str, token: str) -> str | None:
        """Read the model a chat is using.

        With the caller's own session token this reads *their* chat, so it works for
        every user rather than only for the one who owned a service key. Still returns
        None instead of raising: an unresolvable model is a reason to fall back, not to
        fail the tool call.
        """
        client = self._require()
        try:
            response = await client.get(
                f"/api/v1/chats/{chat_id}", headers=self._auth(token)
            )
        except httpx.HTTPError as exc:
            logger.debug("chat lookup failed for %s: %s", chat_id, exc)
            return None

        if response.status_code != 200:
            logger.debug("chat lookup for %s returned %s", chat_id, response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            return None
        return _first_model(payload)

    # ------------------------------------------------------------ completions

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        token: str,
        *,
        temperature: float = 0.4,
    ) -> str:
        """Run a single non-streaming completion and return the assistant text."""
        client = self._require()
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": self.max_output_tokens,
            # No tool_ids and no chat_id on purpose. Passing either would let OpenWebUI
            # resolve tools server-side, and the model could call HiveMCP again from
            # inside HiveMCP's own expansion call.
        }
        try:
            response = await client.post(
                "/api/chat/completions",
                json=body,
                headers={**self._auth(token), LOOP_GUARD_HEADER: "1"},
            )
        except httpx.HTTPError as exc:
            raise LlmError(f"could not reach OpenWebUI: {exc}") from exc

        if response.status_code in (401, 403):
            raise LlmError(
                f"OpenWebUI refused the completion. The chat session may have expired, "
                f"or you may not have access to {model!r}."
            )
        if response.status_code >= 400:
            raise LlmError(
                f"OpenWebUI returned {response.status_code} for model {model!r}: "
                f"{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmError("OpenWebUI returned a non-JSON completion response") from exc

        text = _first_message_content(payload)
        if not text:
            raise LlmError(f"model {model!r} returned an empty response")
        return text


def _first_model(payload: Any) -> str | None:
    """Pull a model id out of a chat record.

    The shape has moved between OpenWebUI versions (sometimes ``models`` at the top
    level, sometimes nested under ``chat``), so this probes the known positions rather
    than asserting one.
    """
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = []
    for container in (payload, payload.get("chat")):
        if not isinstance(container, dict):
            continue
        candidates.append(container.get("models"))
        candidates.append(container.get("model"))

    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, list) and candidate:
            first = candidate[0]
            if isinstance(first, str) and first:
                return first
            if isinstance(first, dict):
                identifier = first.get("id") or first.get("name")
                if isinstance(identifier, str) and identifier:
                    return identifier
    return None


def _first_message_content(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    # Some providers return content as a list of typed parts.
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        joined = "".join(parts).strip()
        return joined or None
    return None
