"""Working out which model the user actually has selected.

OpenWebUI does not tell an external tool server. There is no ``{{MODEL}}`` header
template, and the ``__model__`` reserved argument is delivered to native Python tools
only. So the model is recovered in order of decreasing reliability:

1. ``X-Hive-Model`` on the connection, if an admin pinned one.
2. A lookup of the chat named by ``{{CHAT_ID}}``, using the caller's own session token.
   This is the path that genuinely follows the user's selection, and since the token
   belongs to the user, it reads their chat rather than depending on a service account
   having permission to.
3. ``HIVE_LLM_FALLBACK_MODEL``, if configured.

Step 3 is deliberately opt-in and empty by default: silently answering with a different
model than the user picked is worse than an error that says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...auth import Caller
from ...config import Settings
from .client import OwuiChatClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedModel:
    model: str
    source: str

    @property
    def is_users_selection(self) -> bool:
        return self.source == "chat"

    def warning(self) -> str | None:
        if self.is_users_selection:
            return None
        if self.source == "header":
            return (
                f"Content was generated with {self.model!r}, pinned on the HiveMCP "
                "connection, which may not be the model selected in this chat."
            )
        return (
            f"The model selected in this chat could not be determined, so the "
            f"fallback {self.model!r} generated the content."
        )


class ModelUnavailable(Exception):
    """No model could be determined and no fallback is configured."""


async def resolve_model(
    client: OwuiChatClient, settings: Settings, caller: Caller
) -> ResolvedModel:
    identity = caller.identity
    if identity.model:
        return ResolvedModel(identity.model, "header")

    if identity.chat_id:
        found = await client.get_chat_model(identity.chat_id, caller.token)
        if found:
            return ResolvedModel(found, "chat")
        logger.info(
            "could not read a model from chat %s for user %s",
            identity.chat_id,
            identity.user_id,
        )

    if settings.llm_fallback_model:
        return ResolvedModel(settings.llm_fallback_model, "fallback")

    raise ModelUnavailable(
        "The model selected in this chat could not be determined, so generating from a "
        "brief is not possible. Either pass a complete 'spec' instead, or have an admin "
        "add {{CHAT_ID}} to the X-Hive-Chat-Id header on the HiveMCP connection, pin a "
        "model with the X-Hive-Model header, or set HIVE_LLM_FALLBACK_MODEL."
    )
