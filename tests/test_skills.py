"""The bundled usage guide and the three channels that serve it.

The channels matter more than they look. A guide the model never reads is the same as no
guide, and the failure is silent — you only notice because the model keeps writing slide
text into the chat instead of calling the tool. So these tests check that the Markdown
ships inside the package, that every channel returns the same body, and that none of them
needs a session token.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hivemcp.app import create_app
from hivemcp.auth import Identity
from hivemcp.config import Settings
from hivemcp.core.skills import (
    DEFAULT_SKILL,
    SKILLS_ROOT,
    Skill,
    SkillError,
    SkillRegistry,
    _parse,
)


class FakeValidator:
    def __init__(self) -> None:
        self.identity = Identity(user_id="u-1")

    async def validate(self, token: str) -> Identity:
        return self.identity


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        test_client.app.state.validator = FakeValidator()
        yield test_client


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #


def test_the_guide_ships_inside_the_package() -> None:
    """It has to live under ``hivemcp/`` or the wheel will not carry it.

    The Dockerfile copies the package directory and nothing else, so a guide kept at the
    repository root would exist in git, pass every test run from a checkout, and be
    missing from the running container.
    """
    assert SKILLS_ROOT.is_dir()
    assert (SKILLS_ROOT / DEFAULT_SKILL / "SKILL.md").is_file()
    assert SKILLS_ROOT.parent.name == "hivemcp"


def test_the_default_skill_loads(registry: SkillRegistry) -> None:
    assert DEFAULT_SKILL in registry.names()
    skill = registry.get(DEFAULT_SKILL)
    assert skill.title
    assert skill.description
    assert len(skill.body) > 500


def test_frontmatter_is_stripped_from_the_body(registry: SkillRegistry) -> None:
    """The body is handed to a model verbatim; YAML noise at the top is just tokens."""
    body = registry.get(DEFAULT_SKILL).body
    assert not body.lstrip().startswith("---")
    assert "description:" not in body.splitlines()[0]


# Real tools the guide deliberately stays quiet about.
#
# This set used to also hold hive_open_config and hive_show_download, on the reasoning
# that a model calling them "gets an HTML card it cannot use". That was backwards: the
# card is for the user, and OpenWebUI renders it. Leaving them out meant the settings
# form only ever appeared when a user happened to ask for "Einstellungen" by name, and
# nothing ever told the model it could offer one.
UNDOCUMENTED_ON_PURPOSE = {
    # Describing itself to a model already reading it would be circular.
    "hive_usage_guide",
}


def _registered_tools() -> set[str]:
    root = Path(__file__).resolve().parent.parent / "hivemcp" / "surfaces"
    mcp = (root / "mcp_server.py").read_text(encoding="utf-8")
    api = (root / "openapi_tools.py").read_text(encoding="utf-8")
    return set(re.findall(r'name="(hive_\w+)"', mcp)) | set(
        re.findall(r'operation_id="(hive_\w+)"', api)
    )


def test_the_guide_documents_every_tool_a_model_should_call(
    registry: SkillRegistry,
) -> None:
    """Drift here is worse than no guide: it teaches calls that do not exist.

    Checked against the registrations rather than a hand-kept list, so adding a tool
    without a word about it fails instead of quietly shipping.
    """
    body = registry.get(DEFAULT_SKILL).body
    missing = {
        tool
        for tool in _registered_tools() - UNDOCUMENTED_ON_PURPOSE
        if tool not in body
    }
    assert not missing, f"the guide never mentions {sorted(missing)}"


def test_the_guide_invents_no_tools(registry: SkillRegistry) -> None:
    body = registry.get(DEFAULT_SKILL).body
    # `hive_create_*` is prose for "any of the three", not a tool name.
    mentioned = {name for name in re.findall(r"hive_\w+", body) if not name.endswith("_")}
    assert not mentioned - _registered_tools()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_parse_reads_the_three_fields() -> None:
    skill = _parse(
        "---\nname: demo\ntitle: Demo\ndescription: Eine Demo\n---\n# Body\n\nText.",
        "fallback",
    )
    assert skill == Skill(
        name="demo", title="Demo", description="Eine Demo", body="# Body\n\nText."
    )


def test_a_file_without_frontmatter_still_yields_its_body() -> None:
    """Losing the whole guide over a missing title would be the wrong trade."""
    skill = _parse("# Just a heading\n", "fallback-name")
    assert skill.name == "fallback-name"
    assert skill.body == "# Just a heading"


def test_a_colon_in_the_description_survives() -> None:
    skill = _parse("---\nname: d\ndescription: Use this: always\n---\nx", "d")
    assert skill.description == "Use this: always"


def test_crlf_frontmatter_is_handled() -> None:
    skill = _parse("---\r\nname: d\r\ntitle: T\r\n---\r\nBody", "d")
    assert skill.name == "d"
    assert skill.body == "Body"


def test_an_empty_directory_is_not_fatal(tmp_path: Path) -> None:
    assert SkillRegistry(tmp_path).names() == []
    assert SkillRegistry(tmp_path).default is None


def test_a_directory_without_a_skill_file_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "not-a-skill").mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "SKILL.md").write_text("---\nname: real\n---\nBody")
    assert SkillRegistry(tmp_path).names() == ["real"]


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


def test_unknown_name_lists_what_exists(registry: SkillRegistry) -> None:
    with pytest.raises(SkillError) as caught:
        registry.get("gibt-es-nicht")
    assert DEFAULT_SKILL in str(caught.value)


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "..", "a/b", "/abs", "Upper", "", "-lead"]
)
def test_malformed_names_are_rejected_on_shape(
    name: str, registry: SkillRegistry
) -> None:
    with pytest.raises(SkillError):
        registry.get(name)


# --------------------------------------------------------------------------- #
# Channel 1: MCP prompt
# --------------------------------------------------------------------------- #


async def test_the_guide_is_registered_as_an_mcp_prompt(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app):
        prompts = await app.state.mcp.list_prompts()

    # Hyphens are not valid in an MCP prompt name in every client, hence the underscore.
    names = {prompt.name for prompt in prompts}
    assert DEFAULT_SKILL.replace("-", "_") in names


async def test_getting_the_prompt_returns_the_guide_body(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app):
        result = await app.state.mcp.get_prompt(DEFAULT_SKILL.replace("-", "_"))
        expected = app.state.skills.get(DEFAULT_SKILL).body

    text = "".join(
        message.content.text
        for message in result.messages
        if getattr(message.content, "text", None)
    )
    assert expected in text


# --------------------------------------------------------------------------- #
# Channel 2: the tool
# --------------------------------------------------------------------------- #


async def test_the_usage_guide_tool_is_registered(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app):
        tools = await app.state.mcp.list_tools()
    assert "hive_usage_guide" in {tool.name for tool in tools}


def test_the_tool_endpoint_returns_the_guide(client: TestClient) -> None:
    response = client.get("/tools/usage_guide")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == DEFAULT_SKILL
    assert "hive_create_presentation" in payload["content"]
    assert payload["available"] == client.app.state.skills.names()


def test_the_tool_needs_no_session_token(client: TestClient) -> None:
    """The guide has to be reachable precisely when authentication is misconfigured.

    Every other tool answers 401 in that state, and this is the one that can explain why.
    """
    assert client.get("/tools/usage_guide").status_code == 200


def test_the_tool_reports_an_unknown_name(client: TestClient) -> None:
    response = client.get("/tools/usage_guide", params={"name": "gibt-es-nicht"})
    assert response.status_code == 404
    assert DEFAULT_SKILL in response.json()["detail"]


def test_the_tool_is_in_the_openapi_schema_under_its_operation_id(
    client: TestClient,
) -> None:
    """OpenWebUI names the tool after the operation id, so it is pinned."""
    schema = client.get("/openapi.json").json()
    operations = {
        operation.get("operationId")
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert "hive_usage_guide" in operations


# --------------------------------------------------------------------------- #
# Channel 3: Markdown over HTTP
# --------------------------------------------------------------------------- #


def test_markdown_route_serves_the_body(client: TestClient) -> None:
    response = client.get(f"/skills/{DEFAULT_SKILL}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text == client.app.state.skills.get(DEFAULT_SKILL).body


def test_markdown_route_is_shown_rather_than_downloaded(client: TestClient) -> None:
    assert client.get(f"/skills/{DEFAULT_SKILL}").headers[
        "content-disposition"
    ] == "inline"


def test_markdown_route_needs_no_session_token(client: TestClient) -> None:
    assert client.get(f"/skills/{DEFAULT_SKILL}").status_code == 200


def test_markdown_route_404s_on_an_unknown_name(client: TestClient) -> None:
    assert client.get("/skills/gibt-es-nicht").status_code == 404


def test_markdown_route_rejects_traversal(client: TestClient) -> None:
    for attempt in ("/skills/..", "/skills/%2e%2e", "/skills/Upper"):
        assert client.get(attempt).status_code in (404, 400), attempt


def test_the_index_lists_every_skill(client: TestClient) -> None:
    payload = client.get("/skills").json()
    assert [entry["name"] for entry in payload["skills"]] == (
        client.app.state.skills.names()
    )
    assert payload["skills"][0]["url"].startswith("/skills/")


def test_markdown_routes_stay_out_of_the_openapi_schema(client: TestClient) -> None:
    """Every operation in the schema becomes a tool the model sees.

    A second operation returning the same guide as ``hive_usage_guide`` would spend
    context and give the model a coin to flip.
    """
    schema = client.get("/openapi.json").json()
    assert not [path for path in schema["paths"] if path.startswith("/skills")]


# --------------------------------------------------------------------------- #
# The channels must agree
# --------------------------------------------------------------------------- #


async def test_all_three_channels_return_the_same_text(settings: Settings) -> None:
    """One source of truth, or the three drift and two of them start teaching fiction."""
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.validator = FakeValidator()

        via_http = client.get(f"/skills/{DEFAULT_SKILL}").text
        via_tool = client.get("/tools/usage_guide").json()["content"]
        result = await app.state.mcp.get_prompt(DEFAULT_SKILL.replace("-", "_"))

    via_prompt = "".join(
        message.content.text
        for message in result.messages
        if getattr(message.content, "text", None)
    )
    assert via_http == via_tool
    assert via_http in via_prompt
