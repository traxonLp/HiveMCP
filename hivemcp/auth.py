"""Authentication: the caller's OpenWebUI session token.

OpenWebUI forwards the signed-in user's session token when a connection is set to
``Session`` auth. HiveMCP validates that token against OpenWebUI and takes the identity
from the *answer*, not from a header. That difference is the point of the whole design:

- Identity is a **proof**, not a claim. There is no shared secret that would let a caller
  assert any user id it likes.
- The same token is then reused against the Files API, so a generated document belongs to
  the person who asked for it rather than to a service account.

Consequences, both deliberate:

- **No token, no service.** Contexts without a chat session (scheduled tasks, direct API
  calls, title generation) are refused. A service-account fallback would quietly put
  files back under the wrong owner, which is exactly what this replaces.
- **OpenWebUI only.** Other MCP clients cannot produce an OpenWebUI session token and are
  therefore not supported.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field

import httpx
from fastapi import Header, HTTPException, Request, status

from .config import Settings

logger = logging.getLogger(__name__)

HEADER_CHAT_ID = "X-Hive-Chat-Id"
HEADER_MESSAGE_ID = "X-Hive-Message-Id"
HEADER_MODEL = "X-Hive-Model"

# Endpoints that may validate a session token and return the user. Probed in this order
# on first use; whichever answers with a usable identity is remembered.
#
# Measured against OpenWebUI (spike S2, docs/M0_SPIKES.md):
#   /api/v1/auths/      200 in ~13 ms, full identity (id, name, email, role)
#   /api/v1/users/user  400
#   /api/v1/auths/user  200 in ~4 ms, but an EMPTY body
#
# That last one is the trap, and it is why acceptance is keyed on getting a user id
# rather than on the status code. A plain "200 means valid" check would prefer it — it
# is the fastest of the three — and every caller would come back authenticated as
# nobody, with no error anywhere to explain it.
VALIDATION_ENDPOINTS = ("/api/v1/auths/", "/api/v1/users/user", "/api/v1/auths/user")


class AuthError(Exception):
    """The caller could not be identified."""


@dataclass(frozen=True)
class Identity:
    """Who is calling, as confirmed by OpenWebUI."""

    user_id: str
    email: str | None = None
    name: str | None = None
    role: str | None = None
    chat_id: str | None = None
    message_id: str | None = None
    model: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def audit_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "role": self.role,
            "chat_id": self.chat_id,
        }


@dataclass(frozen=True)
class Caller:
    """An authenticated caller and the token to act on their behalf.

    The token is kept out of ``repr`` so it cannot reach a log line or traceback by
    accident. Identity is carried separately precisely so that logging the identity is
    always safe.
    """

    identity: Identity
    token: str = field(repr=False)

    @property
    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def _token_key(token: str) -> str:
    """Cache key for a token. Hashed so the cache never holds a usable credential."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_token(headers: object) -> str | None:
    get = getattr(headers, "get", None)
    if get is None:
        return None
    header = get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return get("x-api-key") or None


def decode_jwt_claims(token: str) -> dict[str, object] | None:
    """Read a JWT payload without verifying it.

    Verification is OpenWebUI's job. This only supports the expiry check below.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def seconds_until_expiry(token: str) -> int | None:
    claims = decode_jwt_claims(token)
    expires_at = claims.get("exp") if claims else None
    if not isinstance(expires_at, int):
        return None
    return expires_at - int(time.time())


class SessionValidator:
    """Validates OpenWebUI session tokens, with a short-lived cache.

    Every tool call would otherwise cost a round-trip to OpenWebUI before any work
    starts. The cache is keyed by token hash and holds only the resulting identity.
    """

    def __init__(
        self,
        base_url: str | None,
        *,
        timeout: float = 10.0,
        cache_ttl: float = 60.0,
        cache_max: int = 2000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.cache_max = cache_max
        self._client = client
        self._endpoint: str | None = None
        self._cache: dict[str, tuple[Identity, float]] = {}

    def _require(self) -> httpx.AsyncClient:
        if not self.base_url:
            raise AuthError(
                "HIVE_OWUI_URL is not set, so session tokens cannot be validated."
            )
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _cached(self, key: str) -> Identity | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        identity, expires_at = entry
        if expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return identity

    def _store(self, key: str, identity: Identity) -> None:
        if len(self._cache) >= self.cache_max:
            now = time.monotonic()
            expired = [k for k, (_, exp) in self._cache.items() if exp < now]
            for k in expired:
                self._cache.pop(k, None)
            if len(self._cache) >= self.cache_max:
                self._cache.clear()  # pathological case; correctness over hit rate
        self._cache[key] = (identity, time.monotonic() + self.cache_ttl)

    def invalidate(self, token: str) -> None:
        self._cache.pop(_token_key(token), None)

    async def validate(self, token: str) -> Identity:
        key = _token_key(token)
        cached = self._cached(key)
        if cached is not None:
            return cached

        client = self._require()
        endpoints = (self._endpoint,) if self._endpoint else VALIDATION_ENDPOINTS
        last_status: int | None = None

        for path in endpoints:
            assert path is not None
            try:
                response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise AuthError(f"could not reach OpenWebUI to validate the session: {exc}")

            if response.status_code == 200:
                identity = _identity_from_payload(response)
                if identity is None:
                    last_status = response.status_code
                    continue
                if self._endpoint != path:
                    logger.info("validating session tokens against %s", path)
                    self._endpoint = path
                self._store(key, identity)
                return identity

            last_status = response.status_code
            if response.status_code in (401, 403) and self._endpoint:
                # A known-good endpoint rejecting the token means the token is bad,
                # not that the endpoint is wrong.
                break

        if last_status in (401, 403):
            raise AuthError("OpenWebUI rejected this session token; it may have expired.")
        raise AuthError(
            f"could not validate the session token (last status {last_status}). "
            "Check HIVE_OWUI_URL and that this OpenWebUI version exposes one of "
            f"{', '.join(VALIDATION_ENDPOINTS)}."
        )


def _identity_from_payload(response: httpx.Response) -> Identity | None:
    """Extract an identity, or None if this response does not carry one.

    Only four fields are copied out on purpose. OpenWebUI's ``/api/v1/auths/`` response
    also contains ``token``, ``permissions``, ``date_of_birth``, ``gender`` and more.
    Keeping the whole payload around would put a credential and a pile of personal data
    into an object that gets logged and cached.

    A 200 alone proves nothing about the route: OpenWebUI serves its frontend as a
    catch-all, so an unknown ``/api/...`` path answers 200 with the SPA's index.html.
    Requiring JSON with a usable id is what separates a real endpoint from that.
    """
    if "application/json" not in response.headers.get("content-type", ""):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    # A usable id is the acceptance test, not the status code: one endpoint answers 200
    # with an empty body, and treating that as success authenticates everyone as nobody.
    user_id = payload.get("id")
    if not isinstance(user_id, str) or not user_id:
        return None
    return Identity(
        user_id=user_id,
        email=payload.get("email") if isinstance(payload.get("email"), str) else None,
        name=payload.get("name") if isinstance(payload.get("name"), str) else None,
        role=payload.get("role") if isinstance(payload.get("role"), str) else None,
    )


def _context_from_headers(identity: Identity, headers: object) -> Identity:
    """Attach chat context. These are not credentials, so header trust is fine here."""
    get = getattr(headers, "get", None)
    if get is None:
        return identity
    return Identity(
        user_id=identity.user_id,
        email=identity.email,
        name=identity.name,
        role=identity.role,
        chat_id=get(HEADER_CHAT_ID) or None,
        message_id=get(HEADER_MESSAGE_ID) or None,
        model=get(HEADER_MODEL) or None,
    )


async def authenticate(validator: SessionValidator, headers: object) -> Caller:
    """Turn request headers into an authenticated caller, or raise AuthError."""
    token = extract_token(headers)
    if not token:
        raise AuthError(
            "This tool needs an OpenWebUI chat session. Set the HiveMCP connection's "
            "authentication to 'Session' in Admin Settings -> Integrations. Contexts "
            "without a signed-in user, such as scheduled tasks, are not supported."
        )
    identity = await validator.validate(token)
    return Caller(identity=_context_from_headers(identity, headers), token=token)


async def require_caller(
    request: Request,
    authorization: str | None = Header(default=None),  # noqa: ARG001 - documents the header
) -> Caller:
    """FastAPI dependency."""
    validator: SessionValidator = request.app.state.validator
    try:
        return await authenticate(validator, request.headers)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# --------------------------------------------------------------------------- #
# Signed tokens for download links and the iframe GUI
# --------------------------------------------------------------------------- #


class SignatureError(Exception):
    """A UI token is malformed, tampered with, or expired."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_ui_token(payload: dict[str, object], settings: Settings) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + settings.ui_token_ttl_seconds
    encoded = _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(
        settings.signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64e(signature)}"


def verify_ui_token(token: str, settings: Settings) -> dict[str, object]:
    encoded, _, signature = token.partition(".")
    if not encoded or not signature:
        raise SignatureError("Malformed token")

    expected = hmac.new(
        settings.signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64d(signature)
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("Malformed signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("Bad signature")

    try:
        payload = json.loads(_b64d(encoded))
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("Malformed payload") from exc
    if not isinstance(payload, dict):
        raise SignatureError("Malformed payload")

    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or expires_at < int(time.time()):
        raise SignatureError("Token expired")
    return payload


@dataclass
class RateLimiter:
    """Per-user token bucket, in-process.

    Deliberately not distributed: with a handful of replicas an approximate limit is
    enough to stop one user monopolising the render semaphore, and a shared Redis would
    add a dependency nothing else here needs.
    """

    capacity: int = 20
    refill_per_second: float = 0.2
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True
