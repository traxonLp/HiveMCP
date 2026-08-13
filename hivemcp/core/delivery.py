"""How a rendered file reaches the user.

Upload through the OpenWebUI Files API with the caller's own session token, so the
document lands in *their* file list. A signed download link is minted alongside it, not
as a fallback for ownership — that problem is gone — but so the model has a URL it can
put directly in its reply.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..auth import Caller, sign_ui_token
from ..config import Settings
from .files.owui_client import OwuiFilesClient
from .files.workdir import ArtifactStore
from .render.base import RenderedFile

logger = logging.getLogger(__name__)


class DeliveryResult:
    def __init__(
        self,
        *,
        file_id: str | None = None,
        download_url: str | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.file_id = file_id
        self.download_url = download_url
        self.warnings = warnings or []


class Delivery(Protocol):
    async def deliver(self, rendered: RenderedFile, caller: Caller) -> DeliveryResult: ...


class SignedUrlDelivery:
    """Store on the PVC, hand back a signed, expiring URL."""

    def __init__(self, settings: Settings, store: ArtifactStore) -> None:
        self.settings = settings
        self.store = store

    async def deliver(self, rendered: RenderedFile, caller: Caller) -> DeliveryResult:
        artifact = self.store.put(rendered.data, rendered.filename)
        token = sign_ui_token(
            {"artifact_id": artifact.artifact_id, "user_id": caller.identity.user_id},
            self.settings,
        )
        return DeliveryResult(
            download_url=f"{self.settings.public_url.rstrip('/')}/d/{token}",
        )


class OwuiDelivery:
    """Upload as the calling user, so the file appears in their own file list."""

    def __init__(self, client: OwuiFilesClient) -> None:
        self.client = client

    async def deliver(self, rendered: RenderedFile, caller: Caller) -> DeliveryResult:
        file_id = await self.client.upload(
            rendered.data, rendered.filename, rendered.media_type, caller.token
        )
        logger.info(
            "uploaded %s (%d bytes) to OpenWebUI as %s for user %s",
            rendered.filename,
            rendered.size_bytes,
            file_id,
            caller.identity.user_id,
        )
        return DeliveryResult(file_id=file_id)


class CompositeDelivery:
    """Upload, and attach a download link either way.

    A failed upload must still hand the user the document they just waited for, and a
    successful one is more useful with a link the model can paste into its reply.
    """

    def __init__(self, primary: Delivery, fallback: Delivery) -> None:
        self.primary = primary
        self.fallback = fallback

    async def deliver(self, rendered: RenderedFile, caller: Caller) -> DeliveryResult:
        try:
            result = await self.primary.deliver(rendered, caller)
        except Exception as exc:  # noqa: BLE001 - any failure must still yield a file
            logger.warning("upload failed, falling back to a link: %s", exc, exc_info=True)
            result = await self.fallback.deliver(rendered, caller)
            result.warnings.append(
                "The file could not be added to your OpenWebUI files, so it is available "
                "via the download link instead."
            )
            return result

        try:
            extra = await self.fallback.deliver(rendered, caller)
            result.download_url = extra.download_url
        except Exception:  # noqa: BLE001
            logger.debug("could not mint a download link", exc_info=True)
        return result
