from __future__ import annotations

import httpx
import pytest

from hivemcp.core.files.owui_client import OwuiError, OwuiFilesClient, OwuiNotConfigured


def client_with(handler, **kwargs) -> OwuiFilesClient:  # noqa: ANN001
    transport = httpx.MockTransport(handler)
    return OwuiFilesClient(
        "http://owui:8080",
        "sk-test",
        client=httpx.AsyncClient(
            base_url="http://owui:8080",
            transport=transport,
            headers={"Authorization": "Bearer sk-test"},
        ),
        **kwargs,
    )


async def test_upload_disables_processing() -> None:
    """Default is process=true, which runs text extraction and embedding over a file we
    just generated: wasted work, plus the documented race where the file is not ready
    when the call returns."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "file-42", "filename": "a.pptx"})

    file_id = await client_with(handler).upload(b"PK\x03\x04data", "a.pptx", "application/x")

    assert file_id == "file-42"
    assert "process=false" in seen["url"]
    assert seen["auth"] == "Bearer sk-test"
    assert b"a.pptx" in seen["body"]


async def test_upload_without_an_id_is_an_error_not_a_silent_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"filename": "a.pptx"})

    with pytest.raises(OwuiError, match="no file id"):
        await client_with(handler).upload(b"x", "a.pptx", "application/x")


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_upload_failures_include_the_status_and_body(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    with pytest.raises(OwuiError, match=f"{status}"):
        await client_with(handler).upload(b"x", "a.pptx", "application/x")


async def test_get_content_returns_raw_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/files/f-1/content"
        return httpx.Response(200, content=b"PK\x03\x04raw-ooxml")

    assert await client_with(handler).get_content("f-1") == b"PK\x03\x04raw-ooxml"


async def test_missing_file_names_the_likely_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(OwuiError, match="service account"):
        await client_with(handler).get_content("f-1")


async def test_oversized_download_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (3 * 1024 * 1024))

    with pytest.raises(OwuiError, match="limit"):
        await client_with(handler, max_bytes=1024 * 1024).get_content("f-1")


async def test_network_failure_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OwuiError, match="could not reach OpenWebUI"):
        await client_with(handler).get_content("f-1")


async def test_unconfigured_client_explains_which_settings_are_missing() -> None:
    unconfigured = OwuiFilesClient(None, None)

    assert unconfigured.configured is False
    with pytest.raises(OwuiNotConfigured, match="HIVE_OWUI_URL"):
        await unconfigured.get_content("f-1")
