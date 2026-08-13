"""Template operations, sitting between the tools and the store.

Kept apart from ``core/service.py`` so document generation and template management stay
separately testable, and so the file-size and archive checks live in one place rather
than being repeated at each entry point.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...auth import Caller
from ...config import Settings
from ..files.owui_client import OwuiError, OwuiFilesClient
from .inspect import TemplateUnreadable, inspect_template
from .store import TemplateError, TemplateKind, TemplateStore, kind_for

logger = logging.getLogger(__name__)


class TemplateService:
    def __init__(
        self, settings: Settings, store: TemplateStore, owui: OwuiFilesClient
    ) -> None:
        self.settings = settings
        self.store = store
        self.owui = owui

    # ------------------------------------------------------------------ read

    def list(self, caller: Caller, kind: TemplateKind | None = None) -> list[dict[str, Any]]:
        # No filtering by caller: the pool is shared and readable by everyone.
        return [meta.to_dict() for meta in self.store.list(kind)]

    def inspect(self, caller: Caller, template_id: str) -> dict[str, Any]:
        found = self.store.get(template_id)
        try:
            report = inspect_template(found.path, found.meta.kind)
        except TemplateUnreadable as exc:
            raise TemplateError(
                f"template {template_id!r} could not be read: {exc}"
            ) from exc
        return {**found.meta.to_dict(), **report}

    def path_for(self, caller: Caller, template_id: str) -> Path:
        return self.store.get(template_id).path

    # ----------------------------------------------------------------- write

    async def upload_from_chat(
        self,
        caller: Caller,
        *,
        file_id: str,
        name: str,
        filename: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Pull a file the admin attached to the chat and store it as a template."""
        # Permission and file type are both checked before the download: refusing a
        # non-admin only after fetching their file would waste a round trip and read a
        # file we were never going to keep.
        self.store._require_admin(caller.identity, "add")  # noqa: SLF001
        effective_name = filename or name
        kind_for(effective_name)

        try:
            data = await self.owui.get_content(file_id, caller.token)
        except OwuiError as exc:
            raise TemplateError(str(exc)) from exc

        if len(data) > self.settings.max_upload_bytes:
            raise TemplateError(
                f"the file is {len(data) // 1024 // 1024} MB, over the "
                f"{self.settings.max_upload_mb} MB limit"
            )

        meta = self.store.put(
            data,
            name=name,
            filename=effective_name,
            identity=caller.identity,
            description=description,
        )

        # Validated by inspecting it, not by trusting the extension. A file that cannot
        # be parsed is removed again rather than left to fail at generation time, when
        # the user has already described a whole document.
        try:
            report = self.inspect(caller, meta.template_id)
        except TemplateError:
            with_suppressed_errors(
                lambda: self.store.delete(meta.template_id, caller.identity)
            )
            raise

        logger.info(
            "template %s uploaded by %s (%s)",
            meta.template_id,
            caller.identity.user_id,
            meta.kind,
        )
        return report

    def delete(self, caller: Caller, template_id: str) -> None:
        self.store.delete(template_id, caller.identity)


def with_suppressed_errors(action) -> None:  # noqa: ANN001
    try:
        action()
    except Exception:  # noqa: BLE001 - cleanup failure must not mask the real error
        logger.warning("could not clean up after a failed template upload", exc_info=True)
