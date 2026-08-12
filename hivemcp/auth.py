"""Authentication and caller identity.

Two independent mechanisms:

1. **Bearer token** — a shared secret between OpenWebUI and HiveMCP, checked on every
   tool call. Compared in constant time.
2. **Signed UI tokens** — the config GUI runs in a sandboxed, cross-origin iframe and
   therefore *cannot* send an ``Authorization`` header. Those routes authenticate with
   an HMAC-signed, short-lived token carried in the query string instead.

Caller identity arrives as plain headers that OpenWebUI fills from its own template
tokens (``{{USER_ID}}`` etc.). These are **not** a security boundary — OpenWebUI is
trusted to set them truthfully, and the bearer token is what proves the request came
from OpenWebUI at all. Identity is used for template visibility, quotas and audit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

from fastapi import Header, HTTPException, Request, status

from .config import Settings, get_settings

HEADER_USER_ID = "X-Hive-User-Id"
HEADER_USER_EMAIL = "X-Hive-User-Email"
HEADER_USER_NAME = "X-Hive-User-Name"
HEADER_GROUPS = "X-Hive-Groups"
HEADER_CHAT_ID = "X-Hive-Chat-Id"
HEADER_MODEL = "X-Hive-Model"

ANONYMOUS_USER_ID = "anonymous"


@dataclass(frozen=True)
class Identity:
    """Who is making this call, as reported by OpenWebUI."""

    user_id: str = ANONYMOUS_USER_ID
    email: str | None = None
    name: str | None = None
    groups: tuple[str, ...] = ()
    chat_id: str | None = None
    model: str | None = None
    """Model id, if the connection pins one via the X-Hive-Model header.

    OpenWebUI has no {{MODEL}} template token, and ``__model__`` reaches native Python
    tools only — never an external tool server. So this is opt-in, and the chat lookup
    in ``llm.resolver`` is what normally determines the selected model.
    """

    def audit_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "groups": list(self.groups),
            "chat_id": self.chat_id,
        }


def _split_groups(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def identity_from_headers(headers: object) -> Identity:
    """Build an Identity from any mapping-like object with a ``get`` method.

    Accepts Starlette's ``Headers`` (case-insensitive) as well as a plain dict,
    which keeps this usable from the MCP surface where there is no FastAPI request.
    """
    get = getattr(headers, "get", None)
    if get is None:
        return Identity()
    return Identity(
        user_id=get(HEADER_USER_ID) or ANONYMOUS_USER_ID,
        email=get(HEADER_USER_EMAIL),
        name=get(HEADER_USER_NAME),
        groups=_split_groups(get(HEADER_GROUPS)),
        chat_id=get(HEADER_CHAT_ID),
        model=get(HEADER_MODEL),
    )


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_bearer(authorization: str | None, settings: Settings) -> None:
    """Raise 401 unless the request carries the configured bearer token."""
    if not settings.auth_enabled:
        return
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Expected 'Authorization: Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    assert settings.auth_token is not None  # narrowed by auth_enabled
    if not _constant_time_eq(token, settings.auth_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_caller(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Identity:
    """FastAPI dependency: authenticate, then return the caller's identity.

    Settings come from the app instance rather than the process-wide cache, so an app
    built with explicit settings (tests, or two apps in one process) authenticates
    against the configuration it was actually given.
    """
    settings = getattr(request.app.state, "settings", None) or get_settings()
    verify_bearer(authorization, settings)
    return identity_from_headers(request.headers)


# --------------------------------------------------------------------------- #
# Signed tokens for the iframe GUI
# --------------------------------------------------------------------------- #


class SignatureError(Exception):
    """Raised when a UI token is malformed, tampered with, or expired."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_ui_token(payload: dict[str, object], settings: Settings) -> str:
    """Create a compact ``<payload>.<signature>`` token with an embedded expiry."""
    body = dict(payload)
    body["exp"] = int(time.time()) + settings.ui_token_ttl_seconds
    encoded = _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(
        settings.signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64e(signature)}"


def verify_ui_token(token: str, settings: Settings) -> dict[str, object]:
    """Validate signature and expiry; return the payload."""
    encoded, _, signature = token.partition(".")
    if not encoded or not signature:
        raise SignatureError("Malformed token")

    expected = hmac.new(
        settings.signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64d(signature)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise SignatureError("Malformed signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("Bad signature")

    try:
        payload = json.loads(_b64d(encoded))
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("Malformed payload") from exc
    if not isinstance(payload, dict):
        raise SignatureError("Malformed payload")

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise SignatureError("Token expired")
    return payload


@dataclass
class RateLimiter:
    """Per-user token bucket, in-process.

    Deliberately not distributed: with a handful of replicas an approximate limit
    is enough to stop one user from monopolising the render semaphore, and a shared
    Redis would add a dependency the rest of the service does not need.
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
