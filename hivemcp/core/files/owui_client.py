"""Client for the OpenWebUI Files API.

Every call carries the *caller's* session token rather than a service credential, so
OpenWebUI attributes uploads to the person who asked for the document. HiveMCP holds no
credentials of its own here: the shared ``httpx.AsyncClient`` exists for connection
pooling and deliberately has no default ``Authorization`` header, so forgetting to pass
a token fails loudly instead of silently acting as somebody else.
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
            "OpenWebUI is not configured. Set HIVE_OWUI_URL so HiveMCP can read files "
            "from the chat and upload results back into it."
        )


class OwuiFilesClient:
    def __init__(
        self,
        base_url: str | None,
        *,
        timeout: float = 30.0,
        max_bytes: int = 50 * 1024 * 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _require(self) -> httpx.AsyncClient:
        if not self.configured:
            raise OwuiNotConfigured
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        if not token:
            raise OwuiError("no session token available for this request")
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------ read

    async def get_content(self, file_id: str, token: str) -> bytes:
        """Download a file the user attached to the chat.

        Raw bytes rather than OpenWebUI's extracted text: editing and template
        inspection need the OOXML structure, which extraction throws away.
        """
        client = self._require()
        try:
            response = await client.get(
                f"/api/v1/files/{file_id}/content", headers=self._auth(token)
            )
        except httpx.HTTPError as exc:
            raise OwuiError(f"could not reach OpenWebUI: {exc}") from exc

        if response.status_code in (401, 403):
            raise OwuiError(
                "OpenWebUI refused access to this file. The session may have expired, "
                "or the file belongs to someone else."
            )
        if response.status_code == 404:
            raise OwuiError(f"file {file_id!r} does not exist in OpenWebUI")
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

    async def get_metadata(self, file_id: str, token: str) -> dict[str, object]:
        """Name and size of a file already in OpenWebUI.

        Used to build the download card in ``owui`` delivery mode, where this server
        keeps no copy and therefore knows neither. The alternative was to have the model
        pass them back from the tool result, which works until the model forgets — and a
        download button that fails because a parameter was omitted is a bad trade for one
        cheap request.

        Deliberately forgiving. The exact response shape is OpenWebUI's business and has
        changed across versions, so this reads several plausible keys and returns what it
        found rather than insisting on a schema. A card with a generic name still beats no
        card, so the caller treats an empty dict as "unknown", not as an error.
        """
        client = self._require()
        try:
            response = await client.get(
                f"/api/v1/files/{file_id}", headers=self._auth(token)
            )
        except httpx.HTTPError as exc:
            raise OwuiError(f"could not reach OpenWebUI: {exc}") from exc

        if response.status_code >= 400:
            raise OwuiError(
                f"OpenWebUI returned {response.status_code} for file {file_id!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OwuiError("OpenWebUI returned a non-JSON file record") from exc
        if not isinstance(payload, dict):
            return {}

        meta = payload.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        name = payload.get("filename") or payload.get("name") or meta.get("name")
        size = payload.get("size") or meta.get("size") or meta.get("content_length")

        found: dict[str, object] = {}
        if isinstance(name, str) and name:
            found["filename"] = name
        if isinstance(size, int) and size >= 0:
            found["size_bytes"] = size
        return found

    # ----------------------------------------------------------------- write

    async def upload(self, data: bytes, filename: str, media_type: str, token: str) -> str:
        """Upload a generated document as the calling user; return its file id.

        ``process=false`` matters: the default runs text extraction and embedding over
        the upload. For a document we just generated that is wasted work, and it opens
        the documented race where the file is not ready when the call returns.
        """
        client = self._require()
        try:
            response = await client.post(
                "/api/v1/files/",
                params={"process": "false"},
                files={"file": (filename, data, media_type)},
                headers={**self._auth(token), "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OwuiError(f"could not reach OpenWebUI: {exc}") from exc

        if response.status_code in (401, 403):
            raise OwuiError(
                "OpenWebUI refused the upload. The chat session may have expired; "
                "asking again should get a fresh one."
            )
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
