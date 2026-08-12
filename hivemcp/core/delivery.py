"""How a rendered file reaches the user.

Two strategies behind one interface, because which one works is an open question
(plan risk R1): uploading through the OpenWebUI Files API with a service API key may
attach the file to the service account rather than to the person chatting. Keeping
both behind ``Delivery`` means the answer to that spike is a config change rather
than a refactor.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..auth import Identity, sign_ui_token
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
    async def deliver(self, rendered: RenderedFile, identity: Identity) -> DeliveryResult: ...


class SignedUrlDelivery:
    """Store on the PVC, hand back a signed, expiring URL.

    Needs no OpenWebUI credentials, which makes it the dependable fallback and the
    right default for local development.
    """

    def __init__(self, settings: Settings, store: ArtifactStore) -> None:
        self.settings = settings
        self.store = store

    async def deliver(self, rendered: RenderedFile, identity: Identity) -> DeliveryResult:
        artifact = self.store.put(rendered.data, rendered.filename)
        token = sign_ui_token(
            {"artifact_id": artifact.artifact_id, "user_id": identity.user_id},
            self.settings,
        )
        return DeliveryResult(
            download_url=f"{self.settings.public_url.rstrip('/')}/d/{token}",
        )


class OwuiDelivery:
    """Upload through the OpenWebUI Files API so the file lands in the user's list."""

    def __init__(self, client: OwuiFilesClient) -> None:
        self.client = client

    async def deliver(self, rendered: RenderedFile, identity: Identity) -> DeliveryResult:
        file_id = await self.client.upload(
            rendered.data, rendered.filename, rendered.media_type
        )
        logger.info(
            "uploaded %s (%d bytes) to OpenWebUI as %s for user %s",
            rendered.filename,
            rendered.size_bytes,
            file_id,
            identity.user_id,
        )
        return DeliveryResult(file_id=file_id)


class CompositeDelivery:
    """Try the primary strategy, fall back to the secondary on failure.

    A failed upload should degrade to a download link, not lose the document the user
    just waited for.
    """

    def __init__(self, primary: Delivery, fallback: Delivery) -> None:
        self.primary = primary
        self.fallback = fallback

    async def deliver(self, rendered: RenderedFile, identity: Identity) -> DeliveryResult:
        try:
            result = await self.primary.deliver(rendered, identity)
        except Exception as exc:  # noqa: BLE001 - any failure must still yield a file
            logger.warning("primary delivery failed, falling back: %s", exc, exc_info=True)
            result = await self.fallback.deliver(rendered, identity)
            result.warnings.append(
                "Upload to OpenWebUI failed; the file is available via the download link "
                "instead."
            )
            return result

        # Belt and braces: also mint a link, so the user has a route to the file even
        # if it is not visible in their OpenWebUI file list (see plan risk R1).
        try:
            extra = await self.fallback.deliver(rendered, identity)
            result.download_url = extra.download_url
        except Exception:  # noqa: BLE001
            logger.debug("secondary delivery link could not be created", exc_info=True)
        return result
