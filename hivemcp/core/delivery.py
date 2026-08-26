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

    def __init__(
        self,
        client: OwuiFilesClient,
        *,
        with_link: bool = False,
        public_url: str | None = None,
    ) -> None:
        self.client = client
        self.with_link = with_link
        self.public_url = (public_url or "").rstrip("/")

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
        return DeliveryResult(file_id=file_id, download_url=self._link(file_id))

    def _link(self, file_id: str) -> str | None:
        """OpenWebUI's own content route, absolute.

        Absolute rather than relative, and that is not a style choice. The download card
        is embedded as a **cross-origin** iframe without ``allowSameOrigin`` — spike S7
        and the notes in ``config_ui`` — so a relative href inside it resolves against
        *HiveMCP's* address, not OpenWebUI's, and the button leads to a 404 here. Only
        the markdown link in the assistant's own message would have worked relative,
        because that one is rendered in OpenWebUI's page. One absolute URL is correct in
        both places.

        Whether the link is *clickable* is a separate question this server cannot answer:
        HiveMCP reads that route with a Bearer header, and a link click sends none. If
        OpenWebUI does not also honour its session cookie there, the click gets a 401 and
        the file has to be opened from the user's file list — it is uploaded either way.
        Worth verifying once against your instance.
        """
        if not self.with_link:
            return None
        return f"{self.public_url}/api/v1/files/{file_id}/content"


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
