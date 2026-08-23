"""Production hardening: what must not be reachable, and what every response carries.

The `/_debug` endpoints reflect request headers — session token included — back to the
caller. That is exactly why they are useful during development and exactly why a
deployment that believes it is hardened must not have them. The switch is one setting, so
it is worth a test that fails loudly rather than a comment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hivemcp.app import create_app
from hivemcp.auth import Identity
from hivemcp.config import Settings

DEBUG_PATHS = ["/_debug/whoami", "/_debug/settings-probe", "/_debug/upload-check", "/_debug/richui"]


class FakeValidator:
    async def validate(self, token: str) -> Identity:
        return Identity(user_id="u-1")


def make_settings(tmp_path, environment: str) -> Settings:
    return Settings(
        environment=environment,
        data_dir=tmp_path,
        signing_key="test-signing-key",
        public_url="http://testserver",
        owui_url="http://owui:8080",
        _env_file=None,
    )


@pytest.fixture
def prod_client(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, "prod"))) as client:
        client.app.state.validator = FakeValidator()
        yield client


@pytest.fixture
def dev_client(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, "dev"))) as client:
        client.app.state.validator = FakeValidator()
        yield client


# --------------------------------------------------------------------------- #
# The debug surface
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", DEBUG_PATHS)
def test_debug_endpoints_are_absent_in_prod(prod_client: TestClient, path: str) -> None:
    assert prod_client.get(path).status_code == 404


@pytest.mark.parametrize("path", DEBUG_PATHS)
def test_debug_endpoints_exist_in_dev(dev_client: TestClient, path: str) -> None:
    """Not asserting 200 — some need a real OpenWebUI. Only that they are routed."""
    assert dev_client.get(path).status_code != 404


def test_debug_operations_are_absent_from_the_prod_schema(
    prod_client: TestClient,
) -> None:
    """OpenWebUI turns every operation in the schema into a tool the model can call."""
    schema = prod_client.get("/openapi.json").json()
    assert not [path for path in schema["paths"] if path.startswith("/_debug")]


def test_the_document_tools_are_unaffected_by_prod(prod_client: TestClient) -> None:
    """Hardening must remove the debug surface and nothing else."""
    schema = prod_client.get("/openapi.json").json()
    operations = {
        operation.get("operationId")
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert "hive_create_presentation" in operations
    assert "hive_usage_guide" in operations


# --------------------------------------------------------------------------- #
# Security headers
# --------------------------------------------------------------------------- #


def test_baseline_headers_are_present(prod_client: TestClient) -> None:
    headers = prod_client.get("/healthz").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_hsts_only_in_prod(prod_client: TestClient, dev_client: TestClient) -> None:
    """Pointless over the plain HTTP the dev setup uses, and browsers ignore it there."""
    assert "Strict-Transport-Security" in prod_client.get("/healthz").headers
    assert "Strict-Transport-Security" not in dev_client.get("/healthz").headers


def test_html_surfaces_get_a_content_security_policy(prod_client: TestClient) -> None:
    response = prod_client.get(
        "/tools/open_config?kind=pptx",
        headers={"Authorization": "Bearer session-token", "X-Hive-Chat-Id": "c-1"},
    )
    assert response.status_code == 200
    policy = response.headers["Content-Security-Policy"]
    # The card runs inline script and style, so those are allowed; what matters is that
    # it cannot reach the network. A card that cannot fetch cannot leak the token it was
    # rendered with.
    assert "default-src 'none'" in policy
    assert "connect-src" not in policy
    assert "form-action 'none'" in policy


def test_json_responses_get_no_content_security_policy(prod_client: TestClient) -> None:
    assert "Content-Security-Policy" not in prod_client.get("/healthz").headers


def test_the_card_is_still_embeddable(prod_client: TestClient) -> None:
    """No X-Frame-Options and no frame-ancestors, deliberately.

    The configuration card exists to be rendered inside OpenWebUI's iframe. Denying
    framing here would switch the feature off rather than protect anything.
    """
    response = prod_client.get(
        "/tools/open_config?kind=pptx",
        headers={"Authorization": "Bearer session-token", "X-Hive-Chat-Id": "c-1"},
    )
    assert "X-Frame-Options" not in response.headers
    assert "frame-ancestors" not in response.headers.get("Content-Security-Policy", "")


def test_no_permissive_cors_is_advertised(prod_client: TestClient) -> None:
    """Nothing here is called cross-origin by a browser.

    Adding permissive CORS would only widen what a page on another origin could do with
    a user's session token.
    """
    headers = prod_client.get(
        "/healthz", headers={"Origin": "https://evil.example"}
    ).headers
    assert headers.get("Access-Control-Allow-Origin") != "*"
