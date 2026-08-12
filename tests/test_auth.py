from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from hivemcp.auth import (
    Identity,
    RateLimiter,
    SignatureError,
    identity_from_headers,
    sign_ui_token,
    verify_bearer,
    verify_ui_token,
)
from hivemcp.config import Settings


def test_valid_bearer_passes(settings: Settings) -> None:
    verify_bearer("Bearer test-token", settings)


@pytest.mark.parametrize(
    "header",
    [None, "", "test-token", "Basic test-token", "Bearer", "Bearer wrong-token"],
)
def test_invalid_bearer_is_rejected(settings: Settings, header: str | None) -> None:
    with pytest.raises(HTTPException) as excinfo:
        verify_bearer(header, settings)
    assert excinfo.value.status_code == 401


def test_auth_is_skipped_when_no_token_configured(settings: Settings) -> None:
    verify_bearer(None, settings.model_copy(update={"auth_token": None}))


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_token_means_no_auth_not_empty_auth(monkeypatch, blank: str) -> None:
    """A .env from the template contains `HIVE_AUTH_TOKEN=`.

    Read literally that is an empty string, which `is not None`, so auth switches on
    with an empty token and every request 401s. The symptom looks like a networking or
    header problem, so this is worth pinning down.
    """
    monkeypatch.setenv("HIVE_AUTH_TOKEN", blank)
    settings = Settings(environment="dev", _env_file=None)

    assert settings.auth_token is None
    assert settings.auth_enabled is False
    verify_bearer(None, settings)


@pytest.mark.parametrize(
    "name", ["HIVE_OWUI_URL", "HIVE_OWUI_API_KEY", "HIVE_LLM_FALLBACK_MODEL"]
)
def test_other_blank_settings_also_read_as_unset(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, "")
    settings = Settings(environment="dev", _env_file=None)

    assert getattr(settings, name.removeprefix("HIVE_").lower()) is None
    assert settings.owui_configured is False


def test_blank_signing_key_is_replaced_not_used(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_SIGNING_KEY", "")
    settings = Settings(environment="dev", _env_file=None)

    assert settings.signing_key
    assert len(settings.signing_key) > 20


def test_prod_refuses_to_start_without_credentials() -> None:
    with pytest.raises(ValueError, match="HIVE_AUTH_TOKEN"):
        Settings(environment="prod", auth_token=None, owui_url=None, owui_api_key=None)


def test_identity_parses_openwebui_header_templates() -> None:
    identity = identity_from_headers(
        {
            "X-Hive-User-Id": "u-1",
            "X-Hive-User-Email": "j@example.com",
            "X-Hive-Groups": "platform, admins ,",
            "X-Hive-Chat-Id": "c-9",
        }
    )
    assert identity.user_id == "u-1"
    assert identity.groups == ("platform", "admins")
    assert identity.chat_id == "c-9"
    assert not identity.is_anonymous


def test_identity_defaults_to_anonymous() -> None:
    assert identity_from_headers({}).is_anonymous
    assert Identity().user_id == "anonymous"


def test_ui_token_round_trip(settings: Settings) -> None:
    token = sign_ui_token({"artifact_id": "abc", "user_id": "u-1"}, settings)
    payload = verify_ui_token(token, settings)
    assert payload["artifact_id"] == "abc"
    assert payload["exp"] > time.time()


@pytest.mark.parametrize("token", ["", "no-dot", "a.b", "....", "eyJ.tampered"])
def test_malformed_ui_tokens_are_rejected(settings: Settings, token: str) -> None:
    with pytest.raises(SignatureError):
        verify_ui_token(token, settings)


def test_tampered_payload_fails_signature(settings: Settings) -> None:
    token = sign_ui_token({"artifact_id": "abc"}, settings)
    payload, _, signature = token.partition(".")
    forged = f"{payload[:-2]}XX.{signature}"
    with pytest.raises(SignatureError, match="signature|payload"):
        verify_ui_token(forged, settings)


def test_token_signed_with_another_key_is_rejected(settings: Settings) -> None:
    """Guards the multi-replica failure mode: pods must share HIVE_SIGNING_KEY."""
    token = sign_ui_token({"artifact_id": "abc"}, settings)
    other = settings.model_copy(update={"signing_key": "a-different-key"})
    with pytest.raises(SignatureError, match="Bad signature"):
        verify_ui_token(token, other)


def test_expired_token_is_rejected(settings: Settings) -> None:
    token = sign_ui_token({"artifact_id": "abc"}, settings.model_copy(update={"ui_token_ttl_seconds": -1}))
    with pytest.raises(SignatureError, match="expired"):
        verify_ui_token(token, settings)


def test_rate_limiter_blocks_after_capacity_and_refills() -> None:
    limiter = RateLimiter(capacity=3, refill_per_second=1.0)

    assert [limiter.allow("u-1", now=0.0) for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("u-2", now=0.0) is True, "buckets must be per user"
    assert limiter.allow("u-1", now=2.0) is True, "bucket must refill over time"
