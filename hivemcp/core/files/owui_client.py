"""Client for the OpenWebUI Files API.

Used for both directions: pulling files the user attached to the chat, and pushing
finished documents back so they appear in their file list.

Authentication is a service API key. OpenWebUI attributes uploads to the owner of that
key, which is the open question behind plan risk R1 — hence ``CompositeDelivery``, which
always mints a download link alongside the upload.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class OwuiError(Exception):
    """The Files API could not be reached or refused the request."""


class OwuiNotConfigured(OwuiError):
    def __init__(self) -> None:
        super().__init__(
            "OpenWebUI is not configured. Set HIVE_OWUI_URL and HIVE_OWUI_API_KEY to "
            "read files from the chat or upload results back into it."
        )


class OwuiFilesClient:
    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        *,
        timeout: float = 30.0,
        max_bytes: int = 50 * 1024 * 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _require(self) -> httpx.AsyncClient:
        if not self.configured:
            raise OwuiNotConfigured
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ read

    async def get_content(self, file_id: str) -> bytes:
        """Download a file the user attached to the chat.

        The raw bytes are fetched rather than OpenWebUI's extracted text: editing and
        template inspection need the OOXML structure, which extraction throws away.
        """
        client = self._require()
        try:
            response = await client.get(f"/api/v1/files/{file_id}/content")
        except httpx.HTTPError as exc:
            raise OwuiError(f"could not reach OpenWebUI: {exc}") from exc

        if response.status_code == 404:
            raise OwuiError(
                f"file {file_id!r} does not exist in OpenWebUI, or the service account "
                "cannot see it"
            )
        if response.status_code >= 400:
            raise OwuiError(
                f"OpenWebUI returned {response.status_code} for file {file_id!r}: "
                f"{response.text[:200]}"
            )

        data = response.content
        if len(data) > self.max_bytes:
            raise OwuiError(
                f"file {file_id!r} is {len(data) // 1024 // 1024} MB, over the "
                f"{self.max_bytes // 1024 // 1024} MB limit"
            )
        return data

    # ----------------------------------------------------------------- write

    async def upload(self, data: bytes, filename: str, media_type: str) -> str:
        """Upload a generated document and return its OpenWebUI file id.

        ``process=false`` matters: the default runs text extraction and embedding over
        the upload. For a document we just generated that is wasted work, and it opens
        the documented race where the file is not ready immediately after the call
        returns.
        """
        client = self._require()
        try:
            response = await client.post(
                "/api/v1/files/",
                params={"process": "false"},
                files={"file": (filename, data, media_type)},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OwuiError(f"could not reach OpenWebUI: {exc}") from exc

        if response.status_code >= 400:
            raise OwuiError(
                f"OpenWebUI rejected the upload with {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise OwuiError("OpenWebUI returned a non-JSON response to the upload") from exc

        file_id = payload.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise OwuiError(f"upload response carried no file id: {payload!r}")
        return file_id
