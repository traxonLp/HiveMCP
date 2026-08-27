"""How a finished document reaches the user, and what each mode costs.

The modes differ in what they *fail* to do, which is the part worth pinning down: 'owui'
writes nothing to the artifact volume and so has nothing to fall back on, 'link' never
puts the file in anyone's OpenWebUI file list. Both are reasonable; silently getting the
wrong one is not.
"""

from __future__ import annotations

import pytest

from hivemcp.app import _build_delivery, create_app
from hivemcp.auth import Caller, Identity
from hivemcp.config import Settings
from hivemcp.core.delivery import (
    CompositeDelivery,
    DeliveryResult,
    OwuiDelivery,
    SignedUrlDelivery,
)
from hivemcp.core.files.owui_client import OwuiFilesClient
from hivemcp.core.files.workdir import ArtifactStore
from hivemcp.core.render.base import RenderedFile


class FakeOwui(OwuiFilesClient):
    def __init__(self, *, configured: bool = True, fail: bool = False) -> None:
        super().__init__("http://owui" if configured else None)
        self.fail = fail
        self.uploads: list[str] = []

    async def upload(self, data: bytes, filename: str, media_type: str, token: str) -> str:
        if self.fail:
            raise RuntimeError("upload refused")
        self.uploads.append(filename)
        return "file-123"


@pytest.fixture
def rendered() -> RenderedFile:
    return RenderedFile(
        data=b"PK\x03\x04 pretend this is a deck",
        filename="Bericht.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


@pytest.fixture
def caller() -> Caller:
    return Caller(identity=Identity(user_id="u-1"), token="session-token")


SESSION = {"Authorization": "Bearer session-token", "X-Hive-Chat-Id": "c-1"}


class FakeValidator:
    async def validate(self, token: str) -> Identity:
        return Identity(user_id="u-1")


@pytest.fixture
def client_factory(settings: Settings):
    """A TestClient for a given delivery mode, with authentication stubbed out."""
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    @contextmanager
    def make(mode: str, public_url: str = "https://chat.example.com"):
        configured = settings.model_copy(
            update={"delivery_mode": mode, "owui_public_url": public_url}
        )
        with TestClient(create_app(configured)) as client:
            client.app.state.validator = FakeValidator()
            yield client

    return make


def build(settings: Settings, mode: str, public_url: str | None = None, **kwargs):  # noqa: ANN201, ANN003
    settings = settings.model_copy(
        update={"delivery_mode": mode, "owui_public_url": public_url}
    )
    settings.ensure_dirs()
    store = ArtifactStore(settings.tmp_dir, settings.tmp_ttl_minutes)
    return settings, _build_delivery(settings, store, FakeOwui(**kwargs)), store


# --------------------------------------------------------------------------- #
# Which implementation each mode selects
# --------------------------------------------------------------------------- #


def test_both_is_the_default(settings: Settings) -> None:
    """Existing installations must not change behaviour by upgrading."""
    assert settings.delivery_mode == "both"


def test_both_uploads_and_links(settings: Settings) -> None:
    _, delivery, _ = build(settings, "both")
    assert isinstance(delivery, CompositeDelivery)


def test_owui_mode_uploads_only(settings: Settings) -> None:
    _, delivery, _ = build(settings, "owui")
    assert isinstance(delivery, OwuiDelivery)


def test_link_mode_signs_only(settings: Settings) -> None:
    _, delivery, _ = build(settings, "link")
    assert isinstance(delivery, SignedUrlDelivery)


def test_owui_mode_without_openwebui_falls_back_rather_than_failing(
    settings: Settings,
) -> None:
    """A misconfiguration that only matters once a document exists must not stop boot."""
    _, delivery, _ = build(settings, "owui", configured=False)
    assert isinstance(delivery, SignedUrlDelivery)


# --------------------------------------------------------------------------- #
# What actually lands where
# --------------------------------------------------------------------------- #


async def test_owui_mode_writes_nothing_to_the_artifact_volume(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    """The whole point of the mode: no second copy, so the volume can go away."""
    _, delivery, store = build(settings, "owui")
    result = await delivery.deliver(rendered, caller)

    assert result.file_id == "file-123"
    assert list(store.root.iterdir()) == []


async def test_owui_mode_links_to_openwebuis_own_content_route(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    _, delivery, _ = build(settings, "owui", public_url="https://chat.example.com")
    result = await delivery.deliver(rendered, caller)
    assert result.download_url == "https://chat.example.com/api/v1/files/file-123/content"


async def test_that_link_is_absolute(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    """The download card is a cross-origin iframe without allowSameOrigin.

    A relative href inside it resolves against HiveMCP's address rather than
    OpenWebUI's, so the button would 404. Only the markdown link in the assistant's own
    message would have survived being relative. One absolute URL is right in both.
    """
    _, delivery, _ = build(settings, "owui", public_url="https://chat.example.com")
    result = await delivery.deliver(rendered, caller)
    assert result.download_url.startswith("https://")


async def test_the_public_url_falls_back_to_the_internal_one(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    """Same address on both sides is the common small deployment, so it must still work.

    It is also usually wrong in Kubernetes, which is why the startup log says so.
    """
    _, delivery, _ = build(settings, "owui")
    result = await delivery.deliver(rendered, caller)
    assert result.download_url.startswith(settings.owui_url)


async def test_a_trailing_slash_does_not_double_up(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    _, delivery, _ = build(settings, "owui", public_url="https://chat.example.com/")
    result = await delivery.deliver(rendered, caller)
    assert "//api" not in result.download_url


async def test_owui_mode_has_no_fallback(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    """Accepted cost of the mode, and it must fail loudly rather than return nothing."""
    _, delivery, _ = build(settings, "owui", fail=True)
    with pytest.raises(RuntimeError):
        await delivery.deliver(rendered, caller)


async def test_link_mode_stores_the_artifact_and_signs_a_url(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    _, delivery, store = build(settings, "link")
    result = await delivery.deliver(rendered, caller)

    assert result.file_id is None, "link mode must not put it in the user's files"
    assert result.download_url.startswith(settings.public_url)
    assert len(list(store.root.iterdir())) == 1


async def test_both_mode_yields_a_file_id_and_a_hivemcp_url(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    _, delivery, _ = build(settings, "both")
    result = await delivery.deliver(rendered, caller)

    assert result.file_id == "file-123"
    assert result.download_url.startswith(settings.public_url)


async def test_both_mode_still_delivers_when_the_upload_fails(
    settings: Settings, rendered: RenderedFile, caller: Caller
) -> None:
    """The reason 'both' stays the default: the user waited for this document."""
    _, delivery, _ = build(settings, "both", fail=True)
    result = await delivery.deliver(rendered, caller)

    assert result.file_id is None
    assert result.download_url
    assert any("download link" in warning for warning in result.warnings)


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #


def test_owui_mode_does_not_require_the_artifact_volume(settings: Settings, tmp_path) -> None:
    """Nothing writes artifacts in this mode, so an unusable volume is not a fault.

    Requiring it would leave a correctly configured pod permanently unready over a mount
    it has no use for.
    """
    from fastapi.testclient import TestClient

    hardened = settings.model_copy(update={"delivery_mode": "owui"})
    with TestClient(create_app(hardened)) as client:
        payload = client.get("/readyz").json()

    assert "storage" not in payload["checks"]
    assert "templates" in payload["checks"]


def test_other_modes_still_require_it(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as client:
        payload = client.get("/readyz").json()

    assert payload["checks"]["storage"] == "ok"


# --------------------------------------------------------------------------- #
# The download card has to survive the mode too
# --------------------------------------------------------------------------- #


def test_the_card_accepts_an_openwebui_url() -> None:
    """show_download used to assume every URL carried a HiveMCP signature.

    It pulled the last path segment and verified it as a signed token, so an OpenWebUI
    content URL — whose last segment is the word "content" — failed with "not a usable
    download link". In owui mode that meant the download button never worked at all.
    """
    from hivemcp.surfaces.openapi_tools import _owui_content_file_id

    assert (
        _owui_content_file_id("https://chat.example.com/api/v1/files/abc-1/content")
        == "abc-1"
    )
    assert _owui_content_file_id("/api/v1/files/abc-1/content/") == "abc-1"


def test_signed_hivemcp_urls_are_not_mistaken_for_openwebui_ones() -> None:
    from hivemcp.surfaces.openapi_tools import _owui_content_file_id

    assert _owui_content_file_id("https://hivemcp.example.com/d/eyJhIjoxfQ.c2ln") is None


def test_the_card_works_without_a_filename(client_factory, monkeypatch) -> None:
    """The button must not depend on the model remembering a parameter.

    An earlier version answered 422 when 'filename' was missing, which turned a working
    download into a coin flip over whether the model passed it along. The server asks
    OpenWebUI instead.
    """
    with client_factory("owui") as client:
        async def metadata(file_id: str, token: str) -> dict[str, object]:
            return {"filename": "Quartal.xlsx", "size_bytes": 4096}

        client.app.state.owui.get_metadata = metadata
        response = client.get(
            "/tools/show_download",
            params={"download_url": "https://chat.example.com/api/v1/files/a/content"},
            headers=SESSION,
        )

    assert response.status_code == 200
    assert "Quartal.xlsx" in response.text


def test_the_card_survives_openwebui_not_answering(client_factory) -> None:
    """A generic name beats an error: the link is the part that matters, and it is known."""
    from hivemcp.core.files.owui_client import OwuiError

    with client_factory("owui") as client:
        async def failing(file_id: str, token: str) -> dict[str, object]:
            raise OwuiError("nope")

        client.app.state.owui.get_metadata = failing
        response = client.get(
            "/tools/show_download",
            params={"download_url": "https://chat.example.com/api/v1/files/a/content"},
            headers=SESSION,
        )

    assert response.status_code == 200
    assert "https://chat.example.com/api/v1/files/a/content" in response.text


def test_supplied_values_win_over_a_lookup(client_factory) -> None:
    """The common path costs no extra request."""
    called = []

    with client_factory("owui") as client:
        async def metadata(file_id: str, token: str) -> dict[str, object]:
            called.append(file_id)
            return {}

        client.app.state.owui.get_metadata = metadata
        response = client.get(
            "/tools/show_download",
            params={
                "download_url": "https://chat.example.com/api/v1/files/a/content",
                "filename": "Bericht.pptx",
                "size_bytes": 2048,
            },
            headers=SESSION,
        )

    assert response.status_code == 200
    assert called == [], "metadata was fetched even though it was supplied"


def test_the_card_renders_for_an_openwebui_url(client_factory) -> None:
    with client_factory("owui") as client:
        response = client.get(
            "/tools/show_download",
            params={
                "download_url": "https://chat.example.com/api/v1/files/a/content",
                "filename": "Bericht.pptx",
                "size_bytes": 2048,
            },
            headers=SESSION,
        )
    assert response.status_code == 200
    assert response.headers["content-disposition"] == "inline"
    assert "https://chat.example.com/api/v1/files/a/content" in response.text
    assert "Bericht.pptx" in response.text


def test_the_mode_is_reported_in_the_result_shape(
    settings: Settings, rendered: RenderedFile
) -> None:
    """DeliveryResult carries both fields; a mode simply leaves one of them None."""
    result = DeliveryResult(file_id="f", download_url=None)
    assert result.file_id == "f"
    assert result.download_url is None
    assert result.warnings == []
