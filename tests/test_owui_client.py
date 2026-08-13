from __future__ import annotations

import httpx
import pytest

from hivemcp.core.files.owui_client import OwuiError, OwuiFilesClient, OwuiNotConfigured


TOKEN = "user-session-token"


def client_with(handler, **kwargs) -> OwuiFilesClient:  # noqa: ANN001
    # No default Authorization header on the shared client: every call must carry the
    # caller's own token, so forgetting one fails instead of acting as somebody else.
    return OwuiFilesClient(
        "http://owui:8080",
        client=httpx.AsyncClient(
            base_url="http://owui:8080", transport=httpx.MockTransport(handler)
        ),
        **kwargs,
    )


async def test_upload_uses_the_callers_token_and_disables_processing() -> None:
    """Two things at once.

    The token is what makes OpenWebUI attribute the file to the right user. And
    process=true, the default, would run text extraction and embedding over a document
    we just generated: wasted work, plus the documented race where the file is not ready
    when the call returns.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "file-42", "filename": "a.pptx"})

    file_id = await client_with(handler).upload(
        b"PK\x03\x04data", "a.pptx", "application/x", TOKEN
    )

    assert file_id == "file-42"
    assert "process=false" in seen["url"]
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert b"a.pptx" in seen["body"]


@pytest.mark.parametrize("status", [401, 403])
async def test_expired_session_says_so_rather_than_reporting_a_generic_failure(
    status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(OwuiError, match="session may have expired"):
        await client_with(handler).upload(b"x", "a.pptx", "application/x", TOKEN)


async def test_a_missing_token_is_refused_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call OpenWebUI without a token")

    with pytest.raises(OwuiError, match="no session token"):
        await client_with(handler).upload(b"x", "a.pptx", "application/x", "")


async def test_upload_without_an_id_is_an_error_not_a_silent_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"filename": "a.pptx"})

    with pytest.raises(OwuiError, match="no file id"):
        await client_with(handler).upload(b"x", "a.pptx", "application/x", TOKEN)


@pytest.mark.parametrize("status", [400, 500])
async def test_upload_failures_include_the_status_and_body(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    with pytest.raises(OwuiError, match=f"{status}"):
        await client_with(handler).upload(b"x", "a.pptx", "application/x", TOKEN)


async def test_get_content_returns_raw_bytes_with_the_callers_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/files/f-1/content"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, content=b"PK\x03\x04raw-ooxml")

    assert await client_with(handler).get_content("f-1", TOKEN) == b"PK\x03\x04raw-ooxml"


async def test_missing_file_is_reported_as_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(OwuiError, match="does not exist"):
        await client_with(handler).get_content("f-1", TOKEN)


async def test_someone_elses_file_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(OwuiError, match="belongs to someone else"):
        await client_with(handler).get_content("f-1", TOKEN)


async def test_oversized_download_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (3 * 1024 * 1024))

    with pytest.raises(OwuiError, match="limit"):
        await client_with(handler, max_bytes=1024 * 1024).get_content("f-1", TOKEN)


async def test_network_failure_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OwuiError, match="could not reach OpenWebUI"):
        await client_with(handler).get_content("f-1", TOKEN)


async def test_unconfigured_client_explains_which_setting_is_missing() -> None:
    unconfigured = OwuiFilesClient(None)

    assert unconfigured.configured is False
    with pytest.raises(OwuiNotConfigured, match="HIVE_OWUI_URL"):
        await unconfigured.get_content("f-1", TOKEN)
