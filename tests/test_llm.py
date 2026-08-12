"""Brief expansion runs on the model the user selected in the chat.

OpenWebUI does not hand an external tool server the model id, so the resolution chain in
``llm/resolver.py`` is doing real work and is worth pinning down.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hivemcp.auth import Identity
from hivemcp.config import Settings
from hivemcp.core.llm.client import LOOP_GUARD_HEADER, LlmError, OwuiChatClient
from hivemcp.core.llm.expand import ExpansionError, expand_brief, extract_json
from hivemcp.core.llm.resolver import ModelUnavailable, resolve_model
from hivemcp.core.models import DeckSpec, RenderOptions

VALID_DECK = {
    "title": "Preise 2026",
    "slides": [{"layout": "title", "title": "Preise 2026", "subtitle": "Ubersicht"}],
}


def chat_client(handler, **kwargs) -> OwuiChatClient:  # noqa: ANN001
    return OwuiChatClient(
        "http://owui:8080",
        "sk-test",
        client=httpx.AsyncClient(
            base_url="http://owui:8080",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer sk-test"},
        ),
        **kwargs,
    )


def completion(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@pytest.fixture
def llm_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "llm_enabled": True,
            "owui_url": "http://owui:8080",
            "owui_api_key": "sk-test",
        }
    )


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #


async def test_pinned_header_wins(llm_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no lookup should happen when a model is pinned")

    resolved = await resolve_model(
        chat_client(handler), llm_settings, Identity(user_id="u", model="qwen3:32b")
    )
    assert resolved.model == "qwen3:32b"
    assert resolved.source == "header"
    assert "may not be the model selected" in (resolved.warning() or "")


async def test_chat_lookup_finds_the_users_selection(llm_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chats/c-1"
        return httpx.Response(200, json={"chat": {"models": ["llama4:70b"]}})

    resolved = await resolve_model(
        chat_client(handler), llm_settings, Identity(user_id="u", chat_id="c-1")
    )
    assert resolved.model == "llama4:70b"
    assert resolved.is_users_selection
    assert resolved.warning() is None, "the happy path must not add noise"


@pytest.mark.parametrize(
    "payload",
    [
        {"chat": {"models": ["m-1"]}},
        {"models": ["m-1"]},
        {"chat": {"models": [{"id": "m-1", "name": "Nice Name"}]}},
        {"chat": {"model": "m-1"}},
        {"model": "m-1"},
    ],
)
async def test_chat_payload_shapes(llm_settings: Settings, payload: dict) -> None:
    """The chat record's shape has moved between OpenWebUI versions."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    resolved = await resolve_model(
        chat_client(handler), llm_settings, Identity(user_id="u", chat_id="c-1")
    )
    assert resolved.model == "m-1"


async def test_unreadable_chat_falls_back(llm_settings: Settings) -> None:
    """A service key cannot read another user's chat; that must not be fatal."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    resolved = await resolve_model(
        chat_client(handler),
        llm_settings.model_copy(update={"llm_fallback_model": "gpt-4o-mini"}),
        Identity(user_id="u", chat_id="c-1"),
    )
    assert resolved.model == "gpt-4o-mini"
    assert resolved.source == "fallback"
    assert "could not be determined" in (resolved.warning() or "")


async def test_no_model_and_no_fallback_says_what_to_configure(
    llm_settings: Settings,
) -> None:
    """Silently answering with a different model than the user picked is worse than
    failing, so the default has no fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(ModelUnavailable, match="X-Hive-Chat-Id"):
        await resolve_model(
            chat_client(handler), llm_settings, Identity(user_id="u", chat_id="c-1")
        )


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reply",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Hier ist die Spec:\n{"a": 1}\nViel Erfolg!',
        '  \n {"a": 1}  \n',
    ],
)
def test_extract_json_handles_what_models_actually_return(reply: str) -> None:
    assert extract_json(reply) == {"a": 1}


@pytest.mark.parametrize("reply", ["", "kein JSON hier", "{unclosed", "}{"])
def test_extract_json_rejects_junk(reply: str) -> None:
    with pytest.raises(ValueError):
        extract_json(reply)


# --------------------------------------------------------------------------- #
# Expansion
# --------------------------------------------------------------------------- #


async def test_expansion_returns_a_validated_spec(llm_settings: Settings) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["guard"] = request.headers.get(LOOP_GUARD_HEADER)
        return completion(json.dumps(VALID_DECK))

    spec = await expand_brief(
        chat_client(handler),
        llm_settings,
        Identity(user_id="u"),
        "llama4:70b",
        "Preisuebersicht fuer 2026",
        RenderOptions(audience="Vorstand", target_length=6),
        DeckSpec,
        "presentation",
    )

    assert isinstance(spec, DeckSpec)
    assert spec.title == "Preise 2026"
    assert seen["body"]["model"] == "llama4:70b"
    assert seen["guard"] == "1"
    # Passing tool_ids or chat_id would let OpenWebUI resolve tools server-side, and the
    # model could call HiveMCP from inside HiveMCP's own expansion call.
    assert "tool_ids" not in seen["body"]
    assert "chat_id" not in seen["body"]
    prompt = seen["body"]["messages"][0]["content"]
    assert "two_content" in prompt, "the spec's own JSON Schema should be in the prompt"
    assert "Vorstand" in seen["body"]["messages"][1]["content"]


async def test_invalid_output_is_repaired_on_the_second_attempt(
    llm_settings: Settings,
) -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion('{"title": "X", "slides": [{"layout": "erfunden"}]}')
        return completion(json.dumps(VALID_DECK))

    spec = await expand_brief(
        chat_client(handler),
        llm_settings,
        Identity(user_id="u"),
        "m",
        "brief",
        RenderOptions(),
        DeckSpec,
        "presentation",
    )

    assert spec.title == "Preise 2026"
    assert len(calls) == 2
    repair = calls[1]["messages"][-1]["content"]
    assert "did not validate" in repair
    assert "slides.0.layout" in repair, "the model needs to know which field was wrong"


async def test_giving_up_points_back_at_the_spec_parameter(llm_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return completion("ich bin ein Sprachmodell und mache was ich will")

    with pytest.raises(ExpansionError, match="pass it as 'spec'"):
        await expand_brief(
            chat_client(handler),
            llm_settings,
            Identity(user_id="u"),
            "m",
            "brief",
            RenderOptions(),
            DeckSpec,
            "presentation",
        )


async def test_repair_attempts_are_bounded(llm_settings: Settings) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion("nope")

    with pytest.raises(ExpansionError):
        await expand_brief(
            chat_client(handler),
            llm_settings.model_copy(update={"llm_max_repair_attempts": 2}),
            Identity(user_id="u"),
            "m",
            "brief",
            RenderOptions(),
            DeckSpec,
            "presentation",
        )
    assert calls == 3, "initial attempt plus two repairs"


async def test_rejected_api_key_names_the_likely_cause(llm_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(LlmError, match="allowed to use this model"):
        await chat_client(handler).complete("m", [{"role": "user", "content": "hi"}])


async def test_empty_completion_is_an_error(llm_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return completion("   ")

    with pytest.raises(LlmError, match="empty response"):
        await chat_client(handler).complete("m", [{"role": "user", "content": "hi"}])


async def test_content_returned_as_typed_parts_is_joined(llm_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": '{"a"'},
                                {"type": "text", "text": ": 1}"},
                            ]
                        }
                    }
                ]
            },
        )

    text = await chat_client(handler).complete("m", [{"role": "user", "content": "hi"}])
    assert extract_json(text) == {"a": 1}


def test_enabling_brief_mode_requires_openwebui(settings: Settings) -> None:
    with pytest.raises(ValueError, match="HIVE_OWUI_URL"):
        Settings(
            environment="prod",
            auth_token="t",
            owui_url=None,
            owui_api_key=None,
            llm_enabled=True,
        )
