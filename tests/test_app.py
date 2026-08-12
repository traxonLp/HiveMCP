from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from hivemcp.app import create_app
from hivemcp.auth import Identity, sign_ui_token
from hivemcp.config import Settings
from hivemcp.core.delivery import CompositeDelivery, DeliveryResult, SignedUrlDelivery
from hivemcp.core.files.workdir import ArtifactStore
from hivemcp.core.render.base import RenderedFile


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_healthz_is_cheap_and_always_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_storage(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["storage"] == "ok"


def test_readyz_fails_when_storage_is_unwritable(settings: Settings, monkeypatch) -> None:
    """An unmounted or read-only PVC must fail readiness rather than serve errors."""
    with TestClient(create_app(settings)) as client:
        monkeypatch.setattr(
            "pathlib.Path.write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only file system")),
        )
        response = client.get("/readyz")

    assert response.status_code == 503
    assert "unwritable" in response.json()["checks"]["storage"]


def test_download_round_trip(client: TestClient, settings: Settings) -> None:
    artifact = client.app.state.store.put(b"hello world", "bericht.docx")
    token = sign_ui_token({"artifact_id": artifact.artifact_id}, settings)

    response = client.get(f"/d/{token}")

    assert response.status_code == 200
    assert response.content == b"hello world"
    assert "bericht.docx" in response.headers["content-disposition"]


def test_download_rejects_a_forged_token(client: TestClient, settings: Settings) -> None:
    other = settings.model_copy(update={"signing_key": "attacker-key"})
    token = sign_ui_token({"artifact_id": "whatever"}, other)
    assert client.get(f"/d/{token}").status_code == 403


def test_download_of_expired_artifact_explains_itself(
    client: TestClient, settings: Settings
) -> None:
    token = sign_ui_token({"artifact_id": "deadbeef"}, settings)
    response = client.get(f"/d/{token}")
    assert response.status_code == 404
    assert "generated again" in response.json()["detail"]


@pytest.mark.parametrize("artifact_id", ["../../etc", "a/b", "..", "with-dash"])
def test_artifact_ids_cannot_escape_the_store(settings: Settings, artifact_id: str) -> None:
    store = ArtifactStore(settings.tmp_dir, ttl_minutes=60)
    settings.ensure_dirs()
    assert store.get(artifact_id) is None


def test_sweep_removes_only_expired_artifacts(settings: Settings) -> None:
    settings.ensure_dirs()
    store = ArtifactStore(settings.tmp_dir, ttl_minutes=60)
    old = store.put(b"x", "old.docx")
    fresh = store.put(b"y", "fresh.docx")

    import os

    stale = time.time() - 7200
    os.utime(old.path.parent, (stale, stale))

    assert store.sweep() == 1
    assert store.get(old.artifact_id) is None
    assert store.get(fresh.artifact_id) is not None


async def test_signed_url_delivery_builds_an_absolute_url(settings: Settings) -> None:
    settings.ensure_dirs()
    delivery = SignedUrlDelivery(settings, ArtifactStore(settings.tmp_dir))
    rendered = RenderedFile(data=b"x", filename="a.pptx", media_type="application/octet-stream")

    result = await delivery.deliver(rendered, Identity(user_id="u-1"))

    assert result.download_url is not None
    assert result.download_url.startswith("http://testserver/d/")


async def test_composite_delivery_falls_back_and_says_so(settings: Settings) -> None:
    """A failed upload must still hand the user their document."""
    settings.ensure_dirs()

    class Failing:
        async def deliver(self, rendered, identity):  # noqa: ANN001, ANN202
            raise RuntimeError("OpenWebUI returned 403")

    delivery = CompositeDelivery(
        Failing(), SignedUrlDelivery(settings, ArtifactStore(settings.tmp_dir))
    )
    rendered = RenderedFile(data=b"x", filename="a.pptx", media_type="application/octet-stream")

    result = await delivery.deliver(rendered, Identity(user_id="u-1"))

    assert result.download_url is not None
    assert any("download link" in warning for warning in result.warnings)


async def test_composite_delivery_adds_a_link_even_when_upload_succeeds(
    settings: Settings,
) -> None:
    settings.ensure_dirs()

    class Succeeding:
        async def deliver(self, rendered, identity):  # noqa: ANN001, ANN202
            return DeliveryResult(file_id="owui-123")

    delivery = CompositeDelivery(
        Succeeding(), SignedUrlDelivery(settings, ArtifactStore(settings.tmp_dir))
    )
    rendered = RenderedFile(data=b"x", filename="a.pptx", media_type="application/octet-stream")

    result = await delivery.deliver(rendered, Identity(user_id="u-1"))

    assert result.file_id == "owui-123"
    assert result.download_url is not None
