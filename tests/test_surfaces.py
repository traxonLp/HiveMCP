"""Surface tests.

The two surfaces are thin adapters, so these check the wiring rather than the logic:
that the routes exist, that auth is enforced on both, that OpenWebUI gets tool names it
can use, and above all that the MCP mount actually answers. That last one is the failure
this project is most likely to hit (python-sdk #1367): the mount looks fine at import
time and 404s at runtime when the session manager was never started.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hivemcp.app import create_app
from hivemcp.auth import Identity
from hivemcp.config import Settings

EXPECTED_TOOLS = {
    "hive_create_presentation",
    "hive_create_document",
    "hive_create_spreadsheet",
}

MINIMAL_DECK = {
    "spec": {"title": "Test", "slides": [{"layout": "title", "title": "Hallo"}]},
    "options": {},
}


SESSION = {"Authorization": "Bearer session-token", "X-Hive-Chat-Id": "c-1"}


class FakeValidator:
    """Stands in for OpenWebUI when validating session tokens.

    The surfaces are what is under test here; whether OpenWebUI answers correctly is
    covered in test_auth.py.
    """

    def __init__(self, identity: Identity | None = None) -> None:
        self.identity = identity or Identity(user_id="u-1", email="j@example.com")
        self.seen: list[str] = []

    async def validate(self, token: str) -> Identity:
        self.seen.append(token)
        return self.identity


@pytest.fixture
def client(settings: Settings):
    """Authenticated client. There is no 'auth disabled' mode any more: a call without a
    session token cannot be served, because the token is also the credential the tools
    act with."""
    with TestClient(create_app(settings)) as test_client:
        test_client.app.state.validator = FakeValidator()
        yield test_client


# --------------------------------------------------------------------------- #
# OpenAPI surface
# --------------------------------------------------------------------------- #


def test_openapi_schema_exposes_the_tools_under_stable_names(client: TestClient) -> None:
    """OpenWebUI derives the tool name from operationId.

    FastAPI's generated ids look like
    'create_presentation_tools_create_presentation_post', which wastes context and reads
    badly in the UI, so they are set explicitly. Renaming one silently breaks every model
    prompt that refers to it.
    """
    schema = client.get("/openapi.json").json()
    operation_ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert EXPECTED_TOOLS <= operation_ids


def test_health_routes_stay_out_of_the_tool_schema(client: TestClient) -> None:
    """Otherwise OpenWebUI would offer the model a 'check readiness' tool."""
    paths = client.get("/openapi.json").json()["paths"]
    assert not {"/healthz", "/readyz"} & set(paths)


def test_tool_call_requires_a_session_token(client: TestClient) -> None:
    """No token, no service. A service-account fallback would put files back under the
    wrong owner, which is what this design removes."""
    response = client.post("/tools/create_presentation", json=MINIMAL_DECK)

    assert response.status_code == 401
    assert "Session" in response.json()["detail"], "the message should say how to fix it"


def test_tool_call_round_trip(client: TestClient) -> None:
    response = client.post(
        "/tools/create_presentation", json=MINIMAL_DECK, headers=SESSION
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "Test.pptx"
    assert body["slide_count"] == 1
    assert body["size_bytes"] > 0
    assert body["download_url"].startswith("http://testserver/d/")


def test_generated_file_can_actually_be_downloaded(client: TestClient) -> None:
    """End to end: render, deliver, then follow the link the model was handed."""
    body = client.post(
        "/tools/create_presentation", json=MINIMAL_DECK, headers=SESSION
    ).json()

    downloaded = client.get(body["download_url"].replace("http://testserver", ""))

    assert downloaded.status_code == 200
    # A .pptx is a zip; 'PK' is the signature that proves we got a real file back.
    assert downloaded.content[:2] == b"PK"
    assert len(downloaded.content) == body["size_bytes"]


def test_invalid_spec_is_a_422_the_model_can_act_on(client: TestClient) -> None:
    response = client.post(
        "/tools/create_presentation",
        json={"spec": {"title": "T", "slides": [{"layout": "does_not_exist"}]}},
        headers=SESSION,
    )
    assert response.status_code == 422


def test_unknown_fields_are_rejected_rather_than_ignored(client: TestClient) -> None:
    """extra='forbid' turns a hallucinated field into a correctable error instead of a
    silently wrong document."""
    response = client.post(
        "/tools/create_presentation",
        json={
            "spec": {
                "title": "T",
                "slides": [{"layout": "title", "title": "X", "image_url": "http://x"}],
            }
        },
        headers=SESSION,
    )
    assert response.status_code == 422
    assert "image_url" in response.text


def test_missing_spec_returns_an_actionable_message(client: TestClient) -> None:
    response = client.post("/tools/create_spreadsheet", json={}, headers=SESSION)
    assert response.status_code == 422
    assert "SheetSpec" in response.text


def test_spreadsheet_and_document_routes_work(client: TestClient) -> None:
    sheet = client.post(
        "/tools/create_spreadsheet",
        json={
            "spec": {
                "title": "Zahlen",
                "sheets": [
                    {
                        "name": "S1",
                        "columns": [{"header": "A", "key": "a"}],
                        "rows": [{"a": "1"}],
                    }
                ],
            }
        },
        headers=SESSION,
    )
    assert sheet.status_code == 200, sheet.text
    assert sheet.json()["sheet_names"] == ["S1"]

    doc = client.post(
        "/tools/create_document",
        json={
            "spec": {
                "title": "Bericht",
                "blocks": [{"type": "heading", "text": "Kapitel", "level": 1}],
            }
        },
        headers=SESSION,
    )
    assert doc.status_code == 200, doc.text
    assert doc.json()["page_estimate"] >= 1


# --------------------------------------------------------------------------- #
# MCP surface
# --------------------------------------------------------------------------- #


async def test_mcp_server_registers_exactly_the_expected_tools(settings: Settings) -> None:
    app = create_app(settings)
    tools = await app.state.mcp.list_tools() if hasattr(app.state, "mcp") else None
    if tools is None:  # state is only populated once the lifespan has run
        with TestClient(app):
            tools = await app.state.mcp.list_tools()

    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_mcp_tool_schemas_are_specific_enough_to_be_useful(settings: Settings) -> None:
    """The input schema is the only instruction the model reliably reads.

    Guarding that the layout enum survives into it, because a plain 'string' there is
    how you get invented layout names.
    """
    app = create_app(settings)
    with TestClient(app):
        tools = {tool.name: tool for tool in await app.state.mcp.list_tools()}

    # snake_case since SDK v2: inputSchema -> input_schema.
    schema = tools["hive_create_presentation"].input_schema
    assert {"spec", "brief", "options"} <= set(schema["properties"])
    assert "title" in str(schema)
    assert "two_content" in str(schema), "slide layout enum should be inlined"


def test_mcp_mount_answers_an_initialize_request(client: TestClient) -> None:
    """The regression test for python-sdk #1367.

    If the session manager is not entered in the app lifespan, this mount returns 404 or
    500 with nothing explaining why.
    """
    response = client.post(
        "/mcp/",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )

    assert response.status_code != 404, "MCP app is not mounted at /mcp"
    assert response.status_code == 200, response.text
    assert "HiveMCP" in response.text


def test_mcp_tool_call_without_a_session_is_refused(client: TestClient) -> None:
    """Authentication sits in the tool, not in middleware at the mount.

    The session token is not only a gate but the credential the tool acts with, so it
    has to reach the handler rather than be checked and discarded at the edge. The
    handshake itself therefore stays open; the tool call is what requires a session.
    """
    response = client.post(
        "/mcp/",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "hive_create_presentation", "arguments": MINIMAL_DECK},
        },
    )

    assert "session" in response.text.lower(), response.text[:400]
    assert "Test.pptx" not in response.text, "must not have rendered anything"
