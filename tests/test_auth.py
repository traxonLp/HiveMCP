"""Session-token authentication.

The identity here is a proof, not a claim: it comes from OpenWebUI's answer to a
validation call, so no header a caller controls can change who they are. These tests
guard that property and the two failure modes that would quietly break it — a cache that
returns the wrong user, and a token that leaks into a log line.
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from hivemcp.auth import (
    AuthError,
    Caller,
    Identity,
    RateLimiter,
    SessionValidator,
    SignatureError,
    authenticate,
    decode_jwt_claims,
    extract_token,
    seconds_until_expiry,
    sign_ui_token,
    verify_ui_token,
)
from hivemcp.config import Settings

USER = {"id": "u-1", "name": "Julian", "email": "j@example.com", "role": "admin"}
OTHER = {"id": "u-2", "name": "Ada", "email": "a@example.com", "role": "user"}


def validator_for(handler, **kwargs) -> SessionValidator:  # noqa: ANN001
    return SessionValidator(
        "http://owui:8080",
        client=httpx.AsyncClient(
            base_url="http://owui:8080", transport=httpx.MockTransport(handler)
        ),
        **kwargs,
    )


def jwt(payload: dict) -> str:
    encode = lambda part: (  # noqa: E731
        base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")
    )
    return f"{encode({'alg': 'HS256'})}.{encode(payload)}.signature"


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"authorization": "Bearer abc"}, "abc"),
        ({"authorization": "bearer abc"}, "abc"),
        ({"authorization": "Bearer  abc  "}, "abc"),
        ({"x-api-key": "abc"}, "abc"),
        ({"authorization": "Basic abc"}, None),
        ({"authorization": "Bearer"}, None),
        ({"authorization": "Bearer   "}, None),
        ({}, None),
    ],
)
def test_extract_token(headers: dict, expected: str | None) -> None:
    assert extract_token(headers) == expected


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


async def test_identity_comes_from_openwebui_not_from_headers() -> None:
    """The whole point: a caller cannot assert who they are."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=USER)

    caller = await authenticate(
        validator_for(handler),
        {"authorization": "Bearer tok", "x-hive-user-id": "somebody-else"},
    )

    assert caller.identity.user_id == "u-1"
    assert caller.identity.email == "j@example.com"
    assert caller.identity.is_admin
    assert caller.token == "tok"


async def test_endpoint_is_probed_then_remembered() -> None:
    """The validating route has moved between OpenWebUI versions; hardcoding one would
    lock everyone out with a 401 that looks like a token problem."""
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.path)
        if request.url.path == "/api/v1/users/user":
            return httpx.Response(200, json=USER)
        return httpx.Response(404)

    validator = validator_for(handler)
    assert (await validator.validate("tok")).user_id == "u-1"
    assert tried == ["/api/v1/auths/", "/api/v1/users/user"]

    validator.invalidate("tok")
    tried.clear()
    await validator.validate("tok")
    assert tried == ["/api/v1/users/user"], "the working endpoint should be remembered"


async def test_chat_context_still_comes_from_headers() -> None:
    """Chat id and model are not credentials, so header trust is fine for them."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=USER)

    caller = await authenticate(
        validator_for(handler),
        {
            "authorization": "Bearer tok",
            "x-hive-chat-id": "c-9",
            "x-hive-model": "qwen3:32b",
        },
    )
    assert caller.identity.chat_id == "c-9"
    assert caller.identity.model == "qwen3:32b"


async def test_missing_token_is_refused_with_a_setup_hint() -> None:
    """No service-account fallback: that would put files back under the wrong owner."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call OpenWebUI without a token")

    with pytest.raises(AuthError, match="Session"):
        await authenticate(validator_for(handler), {})


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_token_says_it_may_have_expired(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(AuthError, match="expired"):
        await validator_for(handler).validate("tok")


async def test_unreachable_openwebui_is_distinguishable_from_a_bad_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AuthError, match="could not reach OpenWebUI"):
        await validator_for(handler).validate("tok")


async def test_payload_without_an_id_is_not_an_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"detail": "ok"})

    with pytest.raises(AuthError):
        await validator_for(handler).validate("tok")


async def test_a_200_with_an_empty_body_is_not_accepted() -> None:
    """Measured behaviour: /api/v1/auths/user answers 200 with {} and is the fastest of
    the three candidates. Accepting it would authenticate every caller as nobody, with
    nothing in the logs to explain it."""
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.path)
        if request.url.path == "/api/v1/auths/":
            return httpx.Response(200, json=USER)
        if request.url.path == "/api/v1/auths/user":
            return httpx.Response(200, json={})
        return httpx.Response(400)

    assert (await validator_for(handler).validate("tok")).user_id == "u-1"
    assert tried == ["/api/v1/auths/"], "the first working endpoint should win"


async def test_only_identity_fields_are_kept_from_the_response() -> None:
    """The real response also carries `token`, `permissions` and personal data. Copying
    the payload wholesale would cache a credential."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **USER,
                "token": "another-session-token",
                "permissions": {"workspace": {"models": True}},
                "date_of_birth": "1990-01-01",
                "profile_image_url": "/user.png",
            },
        )

    validator = validator_for(handler)
    identity = await validator.validate("tok")

    assert identity.user_id == "u-1"
    assert identity.role == "admin"
    assert "another-session-token" not in repr(identity)
    assert "another-session-token" not in repr(validator._cache)  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


async def test_cache_avoids_a_round_trip_per_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=USER)

    validator = validator_for(handler)
    for _ in range(5):
        await validator.validate("tok")
    assert calls == 1


async def test_different_tokens_never_share_a_cache_entry() -> None:
    """A collision here would hand one user another user's identity and token."""

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        return httpx.Response(200, json=USER if token == "tok-a" else OTHER)

    validator = validator_for(handler)
    assert (await validator.validate("tok-a")).user_id == "u-1"
    assert (await validator.validate("tok-b")).user_id == "u-2"
    assert (await validator.validate("tok-a")).user_id == "u-1"


async def test_cache_expires() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=USER)

    validator = validator_for(handler, cache_ttl=0.0)
    await validator.validate("tok")
    await validator.validate("tok")
    assert calls == 2


async def test_cache_never_stores_the_token_itself() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=USER)

    validator = validator_for(handler)
    await validator.validate("super-secret-token")

    assert "super-secret-token" not in repr(validator._cache)  # noqa: SLF001


async def test_cache_is_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=USER)

    validator = validator_for(handler, cache_max=10)
    for index in range(40):
        await validator.validate(f"tok-{index}")
    assert len(validator._cache) <= 10  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Token handling hygiene
# --------------------------------------------------------------------------- #


def test_token_is_kept_out_of_repr() -> None:
    """A Caller ends up in log lines and tracebacks; the token must not ride along."""
    caller = Caller(identity=Identity(user_id="u-1"), token="super-secret-token")

    assert "super-secret-token" not in repr(caller)
    assert "u-1" in repr(caller)
    assert caller.auth_header == {"Authorization": "Bearer super-secret-token"}


def test_audit_dict_carries_no_credential() -> None:
    identity = Identity(user_id="u-1", email="j@example.com", role="admin", chat_id="c-1")
    assert set(identity.audit_dict()) == {"user_id", "email", "role", "chat_id"}


def test_jwt_claims_and_expiry() -> None:
    expires_at = int(time.time()) + 3600
    token = jwt({"id": "u-1", "exp": expires_at})

    assert decode_jwt_claims(token)["id"] == "u-1"
    remaining = seconds_until_expiry(token)
    assert remaining is not None and 3590 < remaining <= 3600


@pytest.mark.parametrize("token", ["not.a.jwt", "two.parts", "", "opaque-token"])
def test_non_jwt_tokens_are_handled(token: str) -> None:
    assert decode_jwt_claims(token) is None
    assert seconds_until_expiry(token) is None


# --------------------------------------------------------------------------- #
# Signed download links (unchanged mechanism)
# --------------------------------------------------------------------------- #


def test_ui_token_round_trip(settings: Settings) -> None:
    payload = verify_ui_token(sign_ui_token({"artifact_id": "abc"}, settings), settings)
    assert payload["artifact_id"] == "abc"


@pytest.mark.parametrize("token", ["", "no-dot", "a.b", "....", "eyJ.tampered"])
def test_malformed_ui_tokens_are_rejected(settings: Settings, token: str) -> None:
    with pytest.raises(SignatureError):
        verify_ui_token(token, settings)


def test_token_signed_with_another_key_is_rejected(settings: Settings) -> None:
    """Guards the multi-replica failure mode: pods must share HIVE_SIGNING_KEY."""
    token = sign_ui_token({"artifact_id": "abc"}, settings)
    other = settings.model_copy(update={"signing_key": "a-different-key"})
    with pytest.raises(SignatureError, match="Bad signature"):
        verify_ui_token(token, other)


def test_expired_ui_token_is_rejected(settings: Settings) -> None:
    token = sign_ui_token(
        {"artifact_id": "abc"}, settings.model_copy(update={"ui_token_ttl_seconds": -1})
    )
    with pytest.raises(SignatureError, match="expired"):
        verify_ui_token(token, settings)


def test_blank_signing_key_is_replaced_not_used(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_SIGNING_KEY", "")
    generated = Settings(environment="dev", _env_file=None)
    assert len(generated.signing_key) > 20


def test_prod_requires_an_openwebui_url() -> None:
    """Without it nothing can authenticate, so starting up would be pointless."""
    with pytest.raises(ValueError, match="HIVE_OWUI_URL"):
        Settings(environment="prod", owui_url=None, _env_file=None)


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_rate_limiter_blocks_after_capacity_and_refills() -> None:
    limiter = RateLimiter(capacity=3, refill_per_second=1.0)

    assert [limiter.allow("u-1", now=0.0) for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("u-2", now=0.0) is True, "buckets must be per user"
    assert limiter.allow("u-1", now=2.0) is True, "bucket must refill over time"
